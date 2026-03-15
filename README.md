# td-gcp

Google Cloud Platform integration for TouchDesigner.

Drop-in COMPs that connect TouchDesigner to GCP services with built-in authentication, connection management, and offline resilience.

## Components

### Firestore

Real-time bidirectional sync between Firestore collections and TouchDesigner tableDATs.

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

## Quick Start

1. Download the latest `.tox` from [Releases](https://github.com/theexperiential/td-gcp/releases)
2. Drag it into your TouchDesigner project
3. Set **Private Key File** to your GCP service account JSON key
4. Approve the one-time dependency install when prompted
5. The COMP connects and collection tables appear automatically

## Requirements

- TouchDesigner 2025.32280+
- GCP project with the relevant service enabled
- Service account JSON key with appropriate permissions

## Documentation

Full documentation: **[theexperiential.github.io/td-gcp](https://theexperiential.github.io/td-gcp/)**

## License

MIT
