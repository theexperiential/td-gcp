# Callbacks

The Storage COMP can call functions in your own textDAT when events occur. This lets you react to completed transfers, file listings, sync operations, and connection state changes.

## Setup

1. Pulse the **Create** button on the Callbacks section of the Storage COMP (or call `CreateCallbacksDat()`)
2. This creates a sibling textDAT named `storage_callbacks` with pre-filled stub functions
3. The **Callbacks DAT** parameter is automatically set to point to it

You can also create your own textDAT manually and point the **Callbacks DAT** parameter to it.

## Callback Functions

### onTransferComplete

Called when an upload, download, or delete completes.

```python
def onTransferComplete(transfer_id, transfer_type, remote_path, local_path, success, error):
    """
    Args:
        transfer_id (str):    Unique transfer identifier.
        transfer_type (str):  'upload', 'download', or 'delete'.
        remote_path (str):    Remote blob path.
        local_path (str):     Local file path (empty for delete).
        success (bool):       True if the operation succeeded.
        error (str|None):     Error message on failure, None on success.
    """
    # Example: refresh a moviefilein TOP after download
    if transfer_type == 'download' and success:
        op('movie1').par.reloadpulse.pulse()

    # Example: log failures
    if not success:
        debug(f'Transfer failed: {transfer_type} {remote_path} - {error}')
```

### onListComplete

Called when a `ListFiles()` operation completes.

```python
def onListComplete(transfer_id, prefix, files, success, error):
    """
    Args:
        transfer_id (str):  Unique transfer identifier.
        prefix (str):       The prefix that was listed.
        files (list[dict]): List of file metadata dicts with keys:
                            name, size, updated, content_type.
        success (bool):     True if the operation succeeded.
        error (str|None):   Error message on failure, None on success.
    """
    # Example: populate a table with file names
    if success:
        table = op('file_list')
        table.clear(keepFirstRow=True)
        for f in files:
            table.appendRow([f['name'], f['size'], f['content_type']])
```

### onSyncComplete

Called when a `SyncFolder()` operation completes.

```python
def onSyncComplete(transfer_id, direction, uploaded, downloaded, deleted, errors, success, error):
    """
    Args:
        transfer_id (str):      Unique transfer identifier.
        direction (str):        'both', 'upload', or 'download'.
        uploaded (list[str]):   Relative paths of files uploaded.
        downloaded (list[str]): Relative paths of files downloaded.
        deleted (list[str]):    Paths removed (prefixed 'remote:' or 'local:').
        errors (list[dict]):    List of dicts with 'path' and 'error' keys.
        success (bool):         True if all operations succeeded.
        error (str|None):       Summary error message, or None.
    """
    # Example: log a sync summary
    debug(f'Sync {direction}: {len(uploaded)} up, {len(downloaded)} down, '
          f'{len(deleted)} deleted, {len(errors)} error(s)')
```

### onConnectionStateChange

Called when the connection state changes.

```python
def onConnectionStateChange(state, error):
    """
    Args:
        state (str):      'connecting', 'connected', 'disconnected',
                          'error', or 'reconnecting'.
        error (str|None): Error description, or None.
    """
    # Example: show connection status on a text TOP
    op('status_text').par.text = state

    if state == 'error':
        debug(f'Storage error: {error}')
```

## Notes

- Callbacks run on the **main thread** and are called synchronously during queue processing
- If a callback raises an exception, it's caught and logged as a warning -- it won't crash the COMP or stop other processing
- Callbacks are only fired when the **Callbacks DAT** parameter is set to a valid DAT path
- `onTransferComplete` fires once per individual transfer (upload, download, or delete)
- `onSyncComplete` fires once per `SyncFolder()` call, summarizing all file operations
