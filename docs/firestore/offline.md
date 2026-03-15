# Offline Queue & Cache

The Firestore COMP includes a SQLite-backed system for offline write persistence and local data caching.

## Offline Write Queue

When **Enable Cache** is on and a write cannot be sent (circuit breaker is open or Firestore is unreachable), the write is persisted to a SQLite database instead of being lost.

### How It Works

1. A write method is called (`PushDoc`, `UpdateDoc`, etc.)
2. The circuit breaker is checked -- if it's open, the write is queued
3. The write is stored in SQLite with a unique `queue_id`, operation type, payload, and timestamp
4. When the connection is re-established, queued writes are automatically retried
5. Successfully flushed writes are removed from the queue

### Inspecting the Queue

The `write_queue_dat` tableDAT inside the Firestore COMP mirrors the SQLite queue in real time. Its columns:

| Column | Description |
|--------|-------------|
| `queue_id` | Unique identifier for the queued write |
| `collection` | Target collection |
| `doc_id` | Target document ID |
| `op_type` | `set`, `update`, `delete`, or `set_merge` |
| `payload` | JSON string of the write data |
| `queued_at` | ISO 8601 timestamp of when the write was queued |
| `retry_count` | Number of failed retry attempts |
| `last_error` | Error message from the most recent failed retry |

### Manual Queue Management

- **Flush Write Queue** -- force-retry all pending writes immediately
- **Clear Write Queue** (via `ClearWriteQueue()`) -- discard all pending writes without retrying

!!! warning
    Clearing the queue permanently discards those writes. Use this only when you're certain the queued data is no longer needed.

## Cache Hydration

When **Hydrate on Start** is on, the COMP populates collection tableDATs from the SQLite document cache immediately on connect -- before Firestore listeners have delivered any data.

This is useful for:

- **Instant local data** -- your tableDATs have content from the previous session immediately, without waiting for network round-trips
- **Offline-first workflows** -- if the network is slow or intermittent, cached data is available right away

Once the real-time listeners start receiving snapshots, table rows are updated in place with fresh data. The cache is updated continuously as new snapshots arrive.

## Database Location

The SQLite database (`gcp.db`) is stored in the folder specified by the **Cache Folder** parameter, relative to the TouchDesigner project folder.

Default location: `<project_folder>/cache/gcp.db`

The database contains two tables:

- **`write_queue`** -- pending offline writes
- **`documents`** -- cached document snapshots for hydration

## Writes and Ordering

Queued writes are stored and retried in `queued_at` order (oldest first), preserving the sequence of operations. If a write fails on retry, its `retry_count` is incremented and it stays in the queue for the next attempt.
