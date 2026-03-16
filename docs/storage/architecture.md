# Architecture

Internal design of the Storage COMP for contributors and curious users.

## Extension Decomposition

The Storage COMP uses three extensions, each handling a distinct responsibility:

| Extension | File | Role |
|-----------|------|------|
| **StorageExt** | `ext_storage.py` | Main engine -- connection lifecycle, transfer management, queue processing, path resolution, logging |
| **BootstrapExt** | `ext_bootstrap.py` | Dependency installation -- locates/installs `uv`, creates a venv, installs `firebase-admin` and `google-cloud-storage` |
| **ConnectionExt** | `ext_connection.py` | Circuit breaker state machine with exponential backoff |

Only `StorageExt` is promoted (its methods are callable directly on the COMP). The others are accessed internally via `self.my.ext.BootstrapExt`, etc.

## Internal Operators

The Storage COMP network is organized into three annotation groups:

**Extensions** -- the three textDATs containing extension code

**Data** -- runtime state operators:

- `log` -- fifoDAT (max 200 lines) for live log inspection
- `log_callbacks` -- textDAT (docked to log) for FIFO truncation callbacks
- `status` -- tableDAT with connection state, circuit state, active transfers, bucket name
- `transfers` -- tableDAT tracking every transfer operation (upload, download, delete, list, metadata, sync)

**Exec** -- `par_exec` parameterexecuteDAT that routes button presses and parameter changes to extension methods

## Threading Model

TouchDesigner's main thread runs at the project frame rate. The Storage COMP must perform file transfers without blocking it. Here's how threads are organized:

### Main Thread (TD Cook)

All TouchDesigner object access happens here:

- `Connect()` / `Disconnect()` -- Firebase app initialization (blocks briefly during gRPC setup)
- `_drain_results()` -- processes up to 10 items from the transfer results queue per frame
- Transfer result processing, status updates, callback dispatch
- All tableDAT reads and writes

### ThreadManager Pool Tasks

Short-lived tasks submitted to TD's built-in thread pool:

- **Bootstrap** -- `uv venv` creation and package installation
- **Upload/Download** -- per-file transfers, results put in `_transfer_results` queue
- **Delete/List/Metadata** -- per-operation tasks, results put in `_transfer_results` queue
- **SyncFolder** -- walks local dir + lists remote, compares, transfers, deletes orphans
- **Reconnect sleep** -- blocks for backoff delay before reconnect attempt

Pool tasks communicate results back to the main thread via `queue.Queue`. The main thread drains these queues every frame in `_drain_results()`.

### ThreadManager Standalone Task

- **Keepalive** -- blocks on a `threading.Event` until shutdown. Its `RefreshHook` (called every frame on the main thread) drives `_drain_results()`.

```
Main Thread          ThreadManager Pool
-----------          ------------------
_drain_results() <-- queue.Queue <--- _upload_worker
Connect()                              _download_worker
Disconnect()                           _delete_worker
tableDAT writes                        _list_worker
callback dispatch                      _metadata_worker
                                       _sync_worker
                                       _bootstrap_worker
                                       _reconnect_sleep
```

### Why No asyncio

TouchDesigner has its own event loop that is not compatible with Python's `asyncio`. The COMP uses `threading` + `queue.Queue` exclusively, with the `queue.Queue` serving as the thread-safe bridge between worker threads and the main thread.

## Transfer Concurrency

The **Max Concurrent** parameter limits how many transfers run simultaneously. When the limit is reached, new transfers are queued in a `deque` and submitted as active ones complete.

```
Upload('a.jpg')  -->  active (slot 1 of 3)
Upload('b.jpg')  -->  active (slot 2 of 3)
Upload('c.jpg')  -->  active (slot 3 of 3)
Upload('d.jpg')  -->  queued (pending)
Upload('e.jpg')  -->  queued (pending)
                      ...
                      'a.jpg' completes --> 'd.jpg' promoted to active
```

`SyncFolder()` bypasses the concurrency limit -- it runs as a single long-running pool task that handles all file operations internally.

## Data Flow: Upload

```
1. Upload('photo.jpg') called on main thread
2. Path resolution: local_path + remote_path resolved against par values
3. Transfer recorded in transfers tableDAT (status: pending)
4. Concurrency check:
   a. Under limit -> submit _upload_worker to ThreadManager pool (status: active)
   b. At limit -> queue in _pending_transfers deque
5. _upload_worker runs in pool thread (no TD access):
   - bucket.blob(remote_path).upload_from_filename(local_path)
   - Optional: blob.make_public()
   - Put result dict in _transfer_results queue
6. _drain_results() picks up result on main thread (next frame)
7. Update transfers tableDAT (status: complete or failed)
8. Decrement active count, submit next pending if any
9. Fire onTransferComplete callback
```

## Data Flow: SyncFolder

```
1. SyncFolder(direction='both') called on main thread
2. Transfer recorded in transfers tableDAT (status: pending)
3. Submit _sync_worker to ThreadManager pool (bypasses concurrency limit)
4. _sync_worker runs in pool thread (no TD access):
   a. Walk local directory -> build local file index (rel_path -> mtime)
   b. List remote blobs -> build remote file index (rel_path -> blob)
   c. Compare by name + modified time
   d. Upload missing/newer local files (if direction is 'upload' or 'both')
   e. Download missing/newer remote files (if direction is 'download' or 'both')
   f. Delete remote orphans (if delete_remote flag set)
   g. Delete local orphans (if delete_local flag set)
   h. Put result dict in _transfer_results queue
5. _drain_results() picks up result on main thread
6. Update transfers tableDAT, fire onSyncComplete callback
```

## Worker Safety

All module-level worker functions (`_upload_worker`, `_download_worker`, etc.) are defined at module scope and receive only plain Python objects as arguments (bucket reference, strings, queue). They never capture `self`, `self.my`, or any TouchDesigner object. This is critical -- workers run on pool threads where TD object access would cause a thread conflict crash.
