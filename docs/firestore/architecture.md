# Architecture

Internal design of the Firestore COMP for contributors and curious users.

## Extension Decomposition

The Firestore COMP uses four extensions, each handling a distinct responsibility:

| Extension | File | Role |
|-----------|------|------|
| **FirestoreExt** | `ext_firestore.py` | Main engine -- connection lifecycle, listener management, queue processing, write routing, logging |
| **BootstrapExt** | `ext_bootstrap.py` | Dependency installation -- locates/installs `uv`, creates a venv, installs `firebase-admin` and `google-cloud-firestore` |
| **WriteQueueExt** | `ext_write_queue.py` | SQLite-backed offline write queue and document cache |
| **ConnectionExt** | `ext_connection.py` | Circuit breaker state machine with exponential backoff |

Only `FirestoreExt` is promoted (its methods are callable directly on the COMP). The others are accessed internally via `self.my.ext.BootstrapExt`, etc.

## Internal Operators

The Firestore COMP network is organized into three annotation groups:

**Extensions** -- the four textDATs containing extension code

**Data** -- runtime state operators:

- `log` -- fifoDAT (max 200 lines) for live log inspection
- `status` -- tableDAT with connection state, circuit state, queue depth, etc.
- `write_queue_dat` -- tableDAT mirroring the SQLite write queue
- `collections` -- baseCOMP containing dynamically created collection tableDATs and their cellwatch datexecuteDATs

**Exec** -- `par_exec` parameterexecuteDAT that routes button presses and parameter changes to extension methods

## Threading Model

TouchDesigner's main thread runs at the project frame rate. The Firestore COMP must perform network I/O without blocking it. Here's how threads are organized:

### Main Thread (TD Cook)

All TouchDesigner object access happens here:

- `Connect()` / `Disconnect()` -- Firebase app initialization (blocks briefly during gRPC setup)
- `_drain_inbound()` -- processes up to 10 items from the inbound queue per frame
- Write result processing, status updates, callback dispatch
- All tableDAT reads and writes

### ThreadManager Pool Tasks

Short-lived tasks submitted to TD's built-in thread pool:

- **Bootstrap** -- `uv venv` creation and package installation
- **Discovery** -- `db.collections()` call to list available collections
- **Write tasks** -- individual Firestore write operations
- **Reconnect sleep** -- blocks for backoff delay before attempting reconnect

Pool tasks communicate results back to the main thread via `queue.Queue`. The main thread drains these queues every frame in `_drain_inbound()`.

### ThreadManager Standalone Task

- **Keepalive** -- blocks on a `threading.Event` until shutdown. Its `RefreshHook` (called every frame on the main thread) drives `_drain_inbound()`.

### Firebase SDK Threads

- **Snapshot listeners** -- managed by the Firebase SDK. The `on_snapshot` callback runs on an SDK-managed thread and **must not access any TD objects**. It only calls `_inbound_queue.put()` with plain Python data.

```
Main Thread          ThreadManager Pool       Firebase SDK Threads
-----------          ------------------       --------------------
_drain_inbound() <-- queue.Queue <----------- on_snapshot callback
Connect()             bootstrap_worker
Disconnect()          _execute_write_queued
tableDAT writes       _discover_collections
callback dispatch     _reconnect_sleep
```

### Why No asyncio

TouchDesigner has its own event loop that is not compatible with Python's `asyncio`. The COMP uses `threading` + `queue.Queue` exclusively, with the `queue.Queue` serving as the thread-safe bridge between worker threads and the main thread.

## Data Flow: Inbound Change

```
1. Firestore document changes
2. Firebase SDK calls on_snapshot (SDK thread)
3. Callback serializes data with __type markers (see Sync & Filtering > Type Preservation) and puts (collection, doc_id, change_type, payload, update_time) in _inbound_queue
4. _drain_inbound() picks it up on the main thread (next frame)
5. Version check: if update_time <= stored version, skip (echo prevention)
6. _write_to_table() updates the collection tableDAT
7. WriteQueueExt.SaveDocument() caches to SQLite
8. _fire_callback('onFirestoreChange', ...) notifies user code
```

## Data Flow: Outbound Write

```
1. PushDoc() / MergeDoc() / etc. called on main thread
2. Circuit breaker checked:
   a. CLOSED -> _submit_write_item() creates a TDTask
   b. OPEN + cache enabled -> WriteQueueExt.Enqueue() persists to SQLite
3. TDTask runs _execute_write_queued() in thread pool (deserializes __type markers back to SDK types before writing)
4. Result dict put in _write_results queue
5. _drain_inbound() picks up result on main thread
6. On success: remove from queue, store version, fire onWriteComplete
7. On failure: RecordRetry in queue, RecordFailure on circuit, fire onWriteComplete
```

## Snapshot Callback Safety

The `_make_snapshot_callback()` method returns a closure that captures only plain Python objects (queue reference, filter sets, collection name). It never captures `self`, `self.my`, or any TD object reference. This is critical -- the closure runs on a Firebase SDK thread where TD object access would cause a thread conflict crash.
