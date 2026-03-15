# Callbacks

The Firestore COMP can call functions in your own textDAT when events occur. This lets you react to document changes, write completions, and connection state changes.

## Setup

1. Pulse the **Create** button on the Callbacks section of the Firestore COMP (or call `CreateCallbacksDat()`)
2. This creates a sibling textDAT named `callbacks` with pre-filled stub functions
3. The **Callbacks DAT** parameter is automatically set to point to it

You can also create your own textDAT manually and point the **Callbacks DAT** parameter to it.

## Callback Functions

### onFirestoreChange

Called when a remote document is added, modified, or removed.

```python
def onFirestoreChange(collection, doc_id, change_type, payload):
    """
    Args:
        collection (str):  Collection name (e.g. 'users').
        doc_id (str):      Document ID.
        change_type (str): 'added', 'modified', or 'removed'.
        payload (dict):    Document data (empty dict on removal).
    """
    # Example: update a text TOP when a specific doc changes
    if collection == 'config' and doc_id == 'display':
        op('title_text').par.text = payload.get('title', '')

    # Example: log removals
    if change_type == 'removed':
        debug(f'Document removed: {collection}/{doc_id}')
```

### onWriteComplete

Called after a write operation (immediate or queued) completes.

```python
def onWriteComplete(collection, doc_id, success, error):
    """
    Args:
        collection (str): Collection name.
        doc_id (str):     Document ID.
        success (bool):   True if the write succeeded.
        error (str|None): Error message on failure, None on success.
    """
    if not success:
        debug(f'Write failed: {collection}/{doc_id} - {error}')
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
    # Example: show connection status
    op('status_text').par.text = state

    if state == 'error':
        debug(f'Firestore error: {error}')
```

## Notes

- Callbacks run on the **main thread** and are called synchronously during queue processing
- If a callback raises an exception, it's caught and logged as a warning -- it won't crash the COMP or stop other processing
- Callbacks are only fired when the **Callbacks DAT** parameter is set to a valid DAT path
- `onFirestoreChange` fires for every individual document change -- a snapshot with 10 changes fires 10 callbacks
