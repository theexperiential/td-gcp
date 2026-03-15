# TouchDesigner × GCP Integration Suite

Drop-in COMPs that connect TouchDesigner to Google Cloud Platform services -- real-time data sync, cloud storage, and more. Each component handles authentication, connection management, and offline resilience so you can focus on your project.

---

### Firestore

Real-time bidirectional sync between Firestore collections and TouchDesigner tableDATs. Includes offline write queue, circuit breaker, and auto-bootstrap.

- **Real-time listeners** -- Firestore changes appear in tableDATs instantly
- **Write-back** -- edit table cells and changes push to Firestore automatically (500ms debounce)
- **Offline queue** -- writes made while disconnected are persisted to SQLite and retried on reconnect
- **Circuit breaker** -- exponential backoff prevents runaway reconnection loops
- **Auto-bootstrap** -- installs Python dependencies (`firebase-admin`) on first run via `uv`
- **Collection discovery** -- auto-discover all collections or specify an explicit list
- **Document & field filtering** -- include/exclude by document ID or field name
- **Callbacks** -- hook into `onFirestoreChange`, `onWriteComplete`, `onConnectionStateChange`
- **Cache hydration** -- populate tables from local cache on startup for instant data

### Storage *(coming soon)*

Upload, download, and manage files in Firebase Storage directly from TouchDesigner.

---

## Requirements

- **TouchDesigner** 2025.32280 or later
- **Google Cloud project** with the relevant service(s) enabled (e.g., Firestore)
- **Service account JSON key** with appropriate permissions

## Quick Start

1. Download the latest `.tox` from [Releases](https://github.com/theexperiential/td-gcp/releases)
2. Drag it into your TouchDesigner project
3. Set the **Private Key File** parameter to your service account JSON
4. The component auto-installs Python dependencies and connects

## Documentation

Full docs: **[theexperiential.github.io/td-gcp](https://theexperiential.github.io/td-gcp/)**

## License

MIT
