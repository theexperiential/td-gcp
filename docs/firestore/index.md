# Firestore

The Firestore COMP provides real-time bidirectional synchronization between Google Cloud Firestore and TouchDesigner.

## What It Does

- **Inbound**: Firestore snapshot listeners push document changes into local tableDATs in real time
- **Outbound**: Python API methods (`PushDoc`, `UpdateDoc`, etc.) and cell edits in tableDATs push data back to Firestore
- **Offline**: Writes made while disconnected are persisted to a SQLite queue and automatically retried on reconnect
- **Resilient**: A circuit breaker with exponential backoff prevents runaway reconnection loops

## Data Flow

```
Firestore Cloud
    |
    | on_snapshot (Firebase SDK thread)
    v
[ Inbound Queue ] ---> Main Thread (_drain_inbound)
    |                        |
    |                        v
    |                 Collection tableDATs
    |                   (doc_id | payload | _version | _synced_at | _dirty)
    |
    |   Cell edit (write-back, 500ms debounce)
    |   or API call (PushDoc, etc.)
    v
[ Write Task ] ---> ThreadManager Pool ---> Firestore Cloud
    |
    | (if circuit open)
    v
[ SQLite Write Queue ] ---> Retry on reconnect
```

Each watched collection gets its own tableDAT inside the `collections` sub-COMP. Documents are stored as rows with a JSON payload column. The `_version` column enables echo prevention -- the COMP knows which changes came from its own writes and skips re-applying them.

## Accessing the COMP

The Firestore COMP registers both a **Parent Shortcut** and a **Global OP Shortcut** named `firestore`:

```python
# From anywhere in the project
op.firestore.Connect()
op.firestore.PushDoc('users', 'user1', {'name': 'Alice'})

# From inside the COMP's parent
parent.firestore.GetStatus()
```

## Sections

- [Configuration](configuration.md) -- every parameter explained
- [Sync & Filtering](sync.md) -- collection discovery, filters, echo prevention
- [Write Operations](writes.md) -- pushing data to Firestore
- [Offline Queue & Cache](offline.md) -- offline persistence and cache hydration
- [Callbacks](callbacks.md) -- reacting to changes in your scripts
- [API Reference](api.md) -- complete method listing
- [Architecture](architecture.md) -- threading model and extension breakdown
- [Troubleshooting](troubleshooting.md) -- common issues and fixes
