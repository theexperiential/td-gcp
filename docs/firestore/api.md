# API Reference

All promoted public methods on the Firestore COMP. Call them via the Global OP Shortcut (`op.firestore`) or Parent Shortcut (`parent.firestore`).

## Connection

| Method | Description |
|--------|-------------|
| `Connect()` | Connect to Firestore using the current credentials. Blocks during Firebase initialization. Starts listeners if Enable Listener is on. |
| `Disconnect()` | Cleanly disconnect and stop all listeners. Preserves collection tables and queued writes. |
| `Reset()` | Full teardown -- prompts for confirmation, then disconnects, clears the write queue, deletes all collection tables, and resets the circuit breaker. |

## Write Operations

| Method | Description |
|--------|-------------|
| `PushDoc(collection, doc_id, data)` | Set a document (full replace). `data` is a Python dict. Works offline -- queues if disconnected. |
| `MergeDoc(collection, doc_id, data)` | Merge fields into an existing document (`set` with `merge=True`). Creates the document if it doesn't exist. |
| `UpdateDoc(collection, doc_id, data)` | Partial update -- only modifies specified fields. The document must already exist or the write fails. |
| `DeleteDoc(collection, doc_id)` | Delete a document. |
| `PushBatch(operations)` | Submit multiple writes at once. Each item in the list is a dict with keys: `collection`, `doc_id`, `op_type` (`'set'`, `'update'`, `'delete'`, `'set_merge'`), and `payload` (dict). |

### PushBatch Format

```python
op.firestore.PushBatch([
    {'collection': 'users', 'doc_id': 'u1', 'op_type': 'set', 'payload': {'name': 'Alice'}},
    {'collection': 'users', 'doc_id': 'u2', 'op_type': 'update', 'payload': {'score': 42}},
    {'collection': 'users', 'doc_id': 'u3', 'op_type': 'delete', 'payload': {}},
])
```

## Local Reads

These read from the local tableDAT cache -- no network call is made.

| Method | Returns | Description |
|--------|---------|-------------|
| `GetDoc(collection, doc_id)` | `dict` or `None` | Return the document's parsed payload from the local table, or `None` if not found. |
| `GetCollection(collection)` | `list[dict]` | Return all documents in a collection as a list of dicts. Each dict includes a `_doc_id` key. |

## Subscriptions

| Method | Description |
|--------|-------------|
| `WatchCollection(collection_path)` | Start a real-time snapshot listener for a collection. Creates the collection tableDAT if it doesn't exist. |
| `UnwatchCollection(collection_path)` | Stop the listener for a collection and remove its tableDAT and cellwatch operators. |
| `RefreshCollections()` | Re-discover collections from Firestore and watch any new ones. In auto-discover mode, this happens automatically every 30 seconds. |

## Queue Management

| Method | Description |
|--------|-------------|
| `FlushWriteQueue()` | Force-retry all pending offline writes in the SQLite queue. |
| `ClearWriteQueue()` | Discard all pending offline writes. |
| `FlushDirty()` | Find all rows with `_dirty=1` across all collection tables and push them to Firestore. |

## Diagnostics

| Method | Returns | Description |
|--------|---------|-------------|
| `GetStatus()` | `dict` | Current status with keys: `state`, `circuit`, `last_error`, `queue_depth`, `connected_at`, `collections`. |
| `GetLogs(level=None, limit=50)` | `list[dict]` | Recent log entries from the internal ring buffer (max 500 entries). Filter by `level` (`'INFO'`, `'WARNING'`, `'ERROR'`, `'DEBUG'`) or pass `None` for all. Each entry has `id`, `timestamp`, `frame`, `level`, `message`. |

## Callbacks

| Method | Description |
|--------|-------------|
| `CreateCallbacksDat()` | Create a sibling textDAT named `callbacks` with pre-filled stub functions for all three callback hooks. |
