# API Reference

All promoted public methods on the Storage COMP. Call them via the Global OP Shortcut (`op.storage`) or Parent Shortcut (`parent.storage`).

## Connection

| Method | Description |
|--------|-------------|
| `Connect()` | Connect to Firebase Storage using the current credentials. Blocks briefly during Firebase initialization. |
| `Disconnect()` | Cleanly disconnect from Firebase Storage. |
| `Reset()` | Full teardown -- prompts for confirmation, then disconnects, cancels transfers, clears the transfers table, and resets the circuit breaker. |

## File Transfers

### Upload

```python
op.storage.Upload(local_path=None, remote_path=None, make_public=None)
```

Upload a file to Firebase Storage. Returns a `transfer_id` string for tracking.

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `local_path` | str | `None` | File path, relative to the **Local Path** parameter or absolute. |
| `remote_path` | str | `None` | Remote blob path, relative to the **Remote Path** parameter. If `None`, mirrors the local relative path. |
| `make_public` | bool | `None` | Override the **Make Public** parameter for this upload. `None` uses the parameter value. |

**Path resolution examples:**

```python
# Assuming Local Path = "/project/assets", Remote Path = "media/"

# Relative paths -- resolved against Local Path / Remote Path
op.storage.Upload('photo.jpg')
# Uploads /project/assets/photo.jpg -> media/photo.jpg

op.storage.Upload('textures/brick.png')
# Uploads /project/assets/textures/brick.png -> media/textures/brick.png

# Custom remote path
op.storage.Upload('photo.jpg', 'images/hero.jpg')
# Uploads /project/assets/photo.jpg -> media/images/hero.jpg

# Absolute paths bypass Local Path / Remote Path
op.storage.Upload('/tmp/export.zip', '/backups/export.zip')
# Uploads /tmp/export.zip -> backups/export.zip
```

### Download

```python
op.storage.Download(remote_path=None, local_path=None)
```

Download a file from Firebase Storage. Returns a `transfer_id` string.

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `remote_path` | str | `None` | Remote blob path, relative to the **Remote Path** parameter. |
| `local_path` | str | `None` | Local destination, relative to the **Local Path** parameter. If `None`, mirrors the remote relative path. |

```python
# Download a single file
op.storage.Download('video.mp4')
# Downloads media/video.mp4 -> /project/assets/video.mp4

# Download to a custom local path
op.storage.Download('video.mp4', '/tmp/preview.mp4')
# Downloads media/video.mp4 -> /tmp/preview.mp4
```

### Delete

```python
op.storage.Delete(remote_path)
```

Delete a remote blob. Returns a `transfer_id` string.

| Argument | Type | Description |
|----------|------|-------------|
| `remote_path` | str | Remote blob path, relative to the **Remote Path** parameter. |

```python
op.storage.Delete('old/unused_file.png')
# Deletes media/old/unused_file.png from the bucket
```

## Folder Sync

### SyncFolder

```python
op.storage.SyncFolder(direction='both', delete_remote=None, delete_local=None)
```

Sync the entire **Local Path** folder with the **Remote Path** prefix. Compares files by name and modified time. Returns a `transfer_id` string.

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `direction` | str | `'both'` | `'both'`, `'upload'`, or `'download'`. |
| `delete_remote` | bool | `None` | Delete remote blobs not present locally. `None` uses the **Delete Remote Orphans** parameter. |
| `delete_local` | bool | `None` | Delete local files not present remotely. `None` uses the **Delete Local Orphans** parameter. |

The sync worker runs as a single long-running background task. It walks the local directory, lists remote blobs, compares them, and performs all necessary uploads/downloads/deletes. Results arrive via the `onSyncComplete` callback.

```python
# Upload-only sync (push local changes to bucket)
op.storage.SyncFolder(direction='upload')

# Download-only sync (pull remote changes to local)
op.storage.SyncFolder(direction='download')

# Full bidirectional sync with orphan cleanup
op.storage.SyncFolder(direction='both', delete_remote=True, delete_local=True)
```

!!! note "Pulse buttons"
    The **Upload**, **Download**, and **Sync** pulse buttons on the Controls page call `SyncFolder` with `direction='upload'`, `'download'`, and `'both'` respectively. The orphan deletion toggles on the Storage page apply.

## Listing & Metadata

### ListFiles

```python
op.storage.ListFiles(prefix='', delimiter='/')
```

List blobs under a prefix. Returns a `transfer_id` string. Results are delivered via the `onListComplete` callback.

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `prefix` | str | `''` | Sub-prefix under the **Remote Path** parameter. |
| `delimiter` | str | `'/'` | Directory delimiter. Use `''` for recursive listing. |

```python
# List files in the remote root
op.storage.ListFiles()

# List files in a subfolder
op.storage.ListFiles('textures/')

# Recursive listing (all files, all depths)
op.storage.ListFiles('', delimiter='')
```

### GetMetadata

```python
op.storage.GetMetadata(remote_path)
```

Get metadata for a remote blob. Returns a `transfer_id` string. Results delivered via the `onTransferComplete` callback.

The metadata dict includes: `name`, `size`, `content_type`, `updated`, `created`, `md5_hash`, `public_url`, `metadata`.

```python
op.storage.GetMetadata('photo.jpg')
```

## URLs

### GetPublicUrl

```python
url = op.storage.GetPublicUrl(remote_path)
```

Returns the public URL for a blob. **Synchronous** -- no network call, just constructs the URL. The blob must actually be public for the URL to work.

### GetSignedUrl

```python
transfer_id = op.storage.GetSignedUrl(remote_path, expiration_minutes=60)
```

Generate a temporary signed URL. **Asynchronous** -- runs in a ThreadManager pool thread. The signed URL is delivered via the `onTransferComplete` callback with `transfer_type='signed_url'` and the URL in the `local_path` argument.

| Argument | Type | Default | Description |
|----------|------|---------|-------------|
| `remote_path` | str | -- | Remote blob path, relative to the **Remote Path** parameter. |
| `expiration_minutes` | int | `60` | Minutes until the URL expires. |

```python
# Request a URL valid for 24 hours (result comes via callback)
op.storage.GetSignedUrl('private/report.pdf', expiration_minutes=1440)
```

In your callbacks DAT:
```python
def onTransferComplete(transfer_id, transfer_type, remote_path, local_path, success, error):
    if transfer_type == 'signed_url' and success:
        url = local_path  # signed URL is passed as local_path
        debug(f'Signed URL for {remote_path}: {url}')
```

## Transfer Management

### CancelTransfers

```python
op.storage.CancelTransfers()
```

Cancel all pending (queued, not yet started) transfers. Active transfers that are already running complete normally.

## Diagnostics

| Method | Returns | Description |
|--------|---------|-------------|
| `GetStatus()` | `dict` | Current status with keys: `state`, `circuit`, `last_error`, `active_transfers`, `connected_at`, `bucket`. |
| `GetLogs(level=None, limit=50)` | `list[dict]` | Recent log entries from the internal ring buffer (max 500 entries). Filter by `level` (`'INFO'`, `'WARNING'`, `'ERROR'`, `'DEBUG'`) or pass `None` for all. Each entry has `id`, `timestamp`, `frame`, `level`, `message`. |

## Callbacks

| Method | Description |
|--------|-------------|
| `CreateCallbacksDat()` | Create a sibling textDAT named `storage_callbacks` with pre-filled stub functions for all four callback hooks. |
