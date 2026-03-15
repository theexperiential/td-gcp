# Write Operations

Pushing data from TouchDesigner to Firestore.

## Write Methods

All write methods work offline -- if the circuit breaker is open and the cache is enabled, writes are queued to SQLite and retried on reconnect.

### PushDoc -- Full Replace

```python
op.firestore.PushDoc('users', 'user_42', {
    'name': 'Alice',
    'score': 100,
    'active': True
})
```

Replaces the entire document. If the document doesn't exist, it's created.

### MergeDoc -- Merge Fields

```python
op.firestore.MergeDoc('users', 'user_42', {
    'score': 200
})
```

Merges the provided fields into the existing document. Creates the document if it doesn't exist. Fields not included in the dict are left unchanged.

### UpdateDoc -- Partial Update

```python
op.firestore.UpdateDoc('users', 'user_42', {
    'score': 200
})
```

Updates only the specified fields. The document **must already exist** -- the write fails if it doesn't.

### DeleteDoc

```python
op.firestore.DeleteDoc('users', 'user_42')
```

### PushBatch -- Multiple Writes

```python
op.firestore.PushBatch([
    {
        'collection': 'users',
        'doc_id': 'u1',
        'op_type': 'set',
        'payload': {'name': 'Alice'}
    },
    {
        'collection': 'scores',
        'doc_id': 's1',
        'op_type': 'update',
        'payload': {'value': 42}
    },
    {
        'collection': 'old_data',
        'doc_id': 'd1',
        'op_type': 'delete',
        'payload': {}
    },
])
```

Each item requires:

- `collection` -- collection name
- `doc_id` -- document ID
- `op_type` -- one of `'set'`, `'set_merge'`, `'update'`, `'delete'`
- `payload` -- dict (use `{}` for deletes)

Each write in the batch is submitted as an independent ThreadManager task. They execute concurrently but are not atomic (a Firestore batch write) -- individual writes can succeed or fail independently.

## Write-Back from Table Edits

When **Enable Write-Back** is on, editing the `payload` cell of any row in a collection tableDAT triggers an automatic push to Firestore.

The write-back flow:

1. You edit the payload cell (must be valid JSON)
2. The `_dirty` flag is set to `1`
3. After a 500ms debounce, the payload is submitted as a `set` operation
4. On success, `_dirty` is cleared and the `_version` is updated

If the payload JSON is invalid, the write is skipped with a warning in the log. Fix the JSON and use **Flush Dirty** to retry all dirty rows.

## Where Writes Go

```
Write Method Called
    |
    ├── Circuit CLOSED ──> ThreadManager pool task ──> Firestore
    |                           |
    |                           ├── Success ──> onWriteComplete callback
    |                           └── Failure ──> RecordFailure + onWriteComplete
    |
    └── Circuit OPEN + Cache ON ──> SQLite write queue
                                        |
                                        └── Retried on reconnect or Flush Write Queue
```

See [Offline Queue & Cache](offline.md) for details on the queue behavior.
