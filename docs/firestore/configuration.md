# Configuration

All parameters on the Firestore COMP, organized by section.

## Creds

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Private Key File** | File | *(empty)* | Path to your GCP service account JSON key file. The project ID is extracted from this file automatically. Changing this parameter triggers an automatic reconnect. |
| **Database ID** | Str | `(default)` | Firestore database ID. Only needed if you're using a [named database](https://firebase.google.com/docs/firestore/manage-databases) instead of the default. |

## Sync

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Enable Listener** | Toggle | On | Starts real-time snapshot listeners for your collections. When off, no inbound data flows from Firestore. |
| **Enable Write-Back** | Toggle | On | When on, editing the `payload` cell in a collection tableDAT automatically pushes the change to Firestore (with a 500ms debounce). |
| **Collections** | Str | *(blank)* | Space-separated list of collection names to watch. Leave blank or set to `*` for auto-discovery mode, which polls Firestore every 30 seconds for all available collections. |
| **Filter Docs** | Str | *(blank)* | Space-separated document IDs to include or exclude (depending on Doc Filter Mode). |
| **Doc Filter Mode** | Menu | Include | `Include`: only sync documents listed in Filter Docs. `Exclude`: sync all documents except those listed. Has no effect if Filter Docs is blank. |
| **Filter Fields** | Str | *(blank)* | Space-separated field names to include or exclude from document payloads. |
| **Field Filter Mode** | Menu | Exclude | `Exclude`: remove listed fields from payloads. `Include`: only keep listed fields. Has no effect if Filter Fields is blank. |

!!! note "Filters apply at snapshot time"
    Filters are evaluated when the listener receives a snapshot, not retroactively on existing table rows. Changing a filter takes effect on the next document update.

## Conn

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Auto Connect** | Toggle | On | Automatically connect to Firestore after bootstrap completes. When off, you must pulse **Connect** manually. |
| **Circuit Failure Threshold** | Int | `5` | Number of consecutive failures before the circuit breaker opens and blocks further connection attempts until the timeout expires. |
| **Circuit Timeout** | Float | `60` | Seconds to wait after the circuit opens before allowing a single probe attempt (half-open state). |
| **Backoff Base** | Float | `2` | Base multiplier for exponential backoff between reconnect attempts. Delay = `base ^ min(failure_count, 10)`. |
| **Backoff Max** | Float | `300` | Maximum delay in seconds between reconnect attempts, capping exponential growth. |

??? info "How the circuit breaker works"
    The circuit breaker has three states:

    - **Closed** -- normal operation, all attempts allowed
    - **Open** -- too many failures, all attempts blocked for `Circuit Timeout` seconds
    - **Half-Open** -- timeout expired, one probe attempt is allowed. If it succeeds, the circuit closes. If it fails, it re-opens.

    The backoff formula is: `delay = Backoff Base ^ min(failure_count, 10)`, capped at `Backoff Max`. With default settings, delays escalate: 2s, 4s, 8s, 16s, 32s, 64s, 128s, 256s, 300s (cap).

## Cache

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Enable Cache** | Toggle | On | Enable the SQLite-backed offline write queue. When on, writes made while disconnected are persisted to disk and retried on reconnect. |
| **Cache Folder** | Folder | `cache` | Folder for the SQLite database (`gcp.db`), relative to the TouchDesigner project folder. |
| **Hydrate on Start** | Toggle | On | On connect, populate collection tableDATs from the SQLite cache before Firestore listeners deliver fresh data. This gives you instant local data while waiting for the network. |

## Callbacks

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Create** | Pulse | -- | Creates a sibling textDAT named `callbacks` with pre-filled stub functions for all three callback hooks. Also sets the Callbacks DAT parameter to point to it. |
| **Callbacks DAT** | DAT | *(blank)* | Path to a textDAT containing your callback functions (`onFirestoreChange`, `onWriteComplete`, `onConnectionStateChange`). Leave blank to disable callbacks. |
| **External Log DAT** | DAT | *(blank)* | Path to an external DAT (e.g., a fifoDAT) that receives log entries in addition to the internal log and Textport output. |

## Controls

| Parameter | Type | Description |
|-----------|------|-------------|
| **Flush Dirty** | Pulse | Push all documents with `_dirty=1` to Firestore. Useful for batch-pushing edits or retrying after fixing invalid JSON. |
| **Connect** | Pulse | Connect to Firestore using the current credentials. Starts listeners if Enable Listener is on. |
| **Disconnect** | Pulse | Cleanly disconnect from Firestore and stop all listeners. Preserves collection tables and queued writes. |
| **Reset** | Pulse | Full teardown -- disconnects, clears the write queue, deletes all collection tables, and resets the circuit breaker. Prompts for confirmation. |
| **Flush Write Queue** | Pulse | Force-retry all pending offline writes in the SQLite queue. |

## About

| Parameter | Type | Description |
|-----------|------|-------------|
| **Build Number** | Int | Current build number (read-only). |
| **Build Date** | Str | Build timestamp (read-only). |
| **Touch Build** | Str | TouchDesigner build the component was last saved with (read-only). |
