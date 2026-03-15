# Sync & Filtering

How data flows between Firestore and TouchDesigner, and how to control what gets synced.

## Collection Tables

Each watched collection gets a tableDAT inside the `collections` sub-COMP. The table has these columns:

| Column | Description |
|--------|-------------|
| `doc_id` | Firestore document ID (unique per row) |
| `payload` | JSON string containing all document fields |
| `_version` | Firestore `update_time` timestamp (used for echo prevention) |
| `_synced_at` | Local timestamp of last sync |
| `_dirty` | `0` or `1` -- set to `1` when a local edit hasn't been pushed yet |

Tables are created automatically when a collection is first watched and destroyed when unwatched.

## Collection Discovery

There are two modes for determining which collections to watch:

### Explicit List

Set the **Collections** parameter to a space-separated list of collection names:

```
users scenes products
```

The COMP watches exactly these collections -- no network call is needed to discover them.

### Auto-Discovery

Leave **Collections** blank (or set to `*`). The COMP queries Firestore for all top-level collections and watches them. It re-polls every 30 seconds to pick up newly created collections.

!!! note
    Auto-discovery only finds top-level collections. Subcollections (nested under documents) must be watched explicitly using `WatchCollection()`.

### Changing Collections While Connected

If you modify the **Collections** parameter while connected, the COMP reconciles automatically:

- New collections in the list are watched
- Collections removed from the list are unwatched (their tableDATs and cellwatch operators are destroyed)
- Switching between explicit and auto-discover mode works in real time

## Document Filtering

Use **Filter Docs** and **Doc Filter Mode** to control which documents are synced:

- **Include mode**: Only documents whose IDs are listed in Filter Docs are synced
- **Exclude mode**: All documents are synced except those listed

```
# Only sync these three documents
Filter Docs: user_42 user_99 admin
Doc Filter Mode: Include

# Sync everything except internal documents
Filter Docs: _internal _config _schema
Doc Filter Mode: Exclude
```

## Field Filtering

Use **Filter Fields** and **Field Filter Mode** to control which fields appear in the payload:

- **Exclude mode** (default): Listed fields are stripped from payloads
- **Include mode**: Only listed fields are kept

```
# Remove internal metadata fields
Filter Fields: _metadata _internal_id
Field Filter Mode: Exclude

# Only keep name and score
Filter Fields: name score
Field Filter Mode: Include
```

Filters are applied when the snapshot listener receives data, before it enters the inbound queue. This means filtered fields never reach the tableDAT or trigger callbacks.

## Type Preservation

Firestore supports types that have no direct JSON equivalent (timestamps, document references, geopoints, bytes). Rather than losing this type information by flattening to plain strings, the COMP wraps these values with `__type` markers in the JSON payload:

| Firestore Type | Marker Format |
|----------------|---------------|
| Timestamp / DateTime | `{"__type": "timestamp", "value": "2025-06-15T12:00:00+00:00"}` |
| DocumentReference | `{"__type": "reference", "value": "users/abc123"}` |
| GeoPoint | `{"__type": "geopoint", "latitude": 40.7128, "longitude": -74.006}` |
| Bytes | `{"__type": "bytes", "value": "AAEC/w=="}` (base64) |

Primitive types (null, bool, int, float, string) and nested maps/arrays pass through unchanged.

When writing data back to Firestore (via write-back, `PushDoc`, etc.), these markers are automatically deserialized back into the corresponding Firestore SDK types. This means a document round-trips losslessly -- a timestamp stays a timestamp, a reference stays a reference.

!!! note "Backward compatibility"
    Plain values without markers (e.g., a raw ISO string) pass through unchanged on deserialization. This means payloads from older versions or hand-crafted JSON still work -- they just won't be reconstructed into SDK types.

## Echo Prevention

When the COMP writes a document to Firestore, the snapshot listener will receive that same change back. Without handling, this would create a feedback loop.

The COMP prevents this using version tracking:

1. Each outbound write captures the Firestore `update_time` from the write result
2. The version is stored in `_versions[(collection, doc_id)]`
3. When an inbound snapshot arrives, its `update_time` is compared to the stored version
4. If `inbound_update_time <= stored_version`, the change is skipped (it came from us)

This happens transparently -- you don't need to do anything to enable it.

## Write-Back

When **Enable Write-Back** is on, editing the `payload` cell in a collection tableDAT triggers an automatic push to Firestore:

1. The cellwatch datexecuteDAT detects the edit
2. The row's `_dirty` flag is set to `1`
3. A 500ms debounce timer starts (cancelling any pending timer for the same document)
4. After 500ms, the payload JSON is validated and submitted as a `set` operation
5. The `_dirty` flag is cleared

If the JSON is invalid, the write is skipped and a warning is logged. You can fix the JSON and pulse **Flush Dirty** to retry.

!!! tip "Debouncing prevents rapid-fire writes"
    If you edit the same cell multiple times within 500ms, only the final value is sent to Firestore. This is especially useful when driving payload values from CHOPs or animations.
