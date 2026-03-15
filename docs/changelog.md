# Changelog

## Build 87 -- 2026-03-15

Initial public release of the Firestore COMP.

### Features

- Real-time bidirectional sync between Firestore collections and TouchDesigner tableDATs
- Lossless type preservation -- Firestore timestamps, references, geopoints, and bytes are round-tripped via `__type` markers in JSON payloads (no data loss on write-back)
- Auto-bootstrap with `uv` -- installs `firebase-admin` and `google-cloud-firestore` into a local venv
- Offline write queue backed by SQLite with automatic retry on reconnect
- Circuit breaker with exponential backoff for connection resilience
- Collection auto-discovery or explicit collection list
- Document and field filtering (include/exclude modes)
- Debounced cell write-back (table edits push to Firestore with 500ms debounce)
- Version-based echo prevention
- User callbacks: `onFirestoreChange`, `onWriteComplete`, `onConnectionStateChange`
- Cache hydration -- populate tables from SQLite on startup for instant local data
- Structured logging with ring buffer, FIFO DAT, and optional external log DAT
- Status tableDAT for monitoring connection state, circuit state, and queue depth
- Full project documentation site (MkDocs Material) with GitHub Pages deployment
