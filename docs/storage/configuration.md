# Configuration

All parameters on the Storage COMP, organized by section.

## Creds

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Private Key File** | File | *(empty)* | Path to your GCP service account JSON key file. The project ID and default bucket are derived from this file. Changing this parameter triggers an automatic reconnect. |
| **Bucket Name** | Str | *(empty)* | Firebase Storage bucket name. Leave blank to use the project's default bucket (`<project_id>.firebasestorage.app`). |

## Storage

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Local Path** | Folder | *(empty)* | Local root folder for uploads and downloads. Subfolder structure is preserved in both directions relative to Remote Path. |
| **Remote Path** | Str | *(empty)* | Remote prefix in the bucket (e.g. `media/`). All upload, download, and sync operations are relative to this path. |
| **Make Public** | Toggle | Off | When on, uploaded blobs are made public automatically. Can be overridden per-call via the `Upload()` method's `make_public` argument. |
| **Delete Remote Orphans** | Toggle | Off | During upload or sync, delete remote blobs under Remote Path that don't exist locally. Effectively makes local the source of truth. |
| **Delete Local Orphans** | Toggle | Off | During download or sync, delete local files under Local Path that don't exist remotely. Effectively makes remote the source of truth. |
| **Max Concurrent** | Int | `3` | Maximum number of simultaneous upload/download operations. Additional transfers are queued and submitted as active ones complete. |

!!! note "Path resolution"
    Relative paths passed to `Upload()`, `Download()`, etc. are resolved against Local Path and Remote Path. Absolute paths bypass these parameters entirely. See [API Reference](api.md) for details.

!!! warning "Orphan deletion"
    The **Delete Remote Orphans** and **Delete Local Orphans** toggles are powerful -- they permanently remove files. Use them intentionally. They apply during `SyncFolder()` and the pulse buttons (Upload, Download, Sync) on the Controls page.

## Conn

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Auto Connect** | Toggle | On | Automatically connect to Firebase Storage after bootstrap completes. When off, you must pulse **Connect** manually. |
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

## Callbacks

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| **Create** | Pulse | -- | Creates a sibling textDAT named `storage_callbacks` with pre-filled stub functions for all four callback hooks. Also sets the Callbacks DAT parameter to point to it. |
| **Callbacks DAT** | DAT | *(blank)* | Path to a textDAT containing your callback functions (`onTransferComplete`, `onListComplete`, `onSyncComplete`, `onConnectionStateChange`). Leave blank to disable callbacks. |
| **External Log DAT** | DAT | *(blank)* | Path to an external DAT (e.g., a fifoDAT) that receives log entries in addition to the internal log and Textport output. |

## Controls

| Parameter | Type | Description |
|-----------|------|-------------|
| **Upload** | Pulse | Upload all files from Local Path to Remote Path, preserving subfolder structure. Equivalent to `SyncFolder(direction='upload')`. |
| **Download** | Pulse | Download all files from Remote Path to Local Path, preserving subfolder structure. Equivalent to `SyncFolder(direction='download')`. |
| **Sync** | Pulse | Bidirectional sync -- uploads missing/newer local files and downloads missing/newer remote files. Equivalent to `SyncFolder(direction='both')`. |
| **Connect** | Pulse | Connect to Firebase Storage using the current credentials. |
| **Disconnect** | Pulse | Cleanly disconnect from Firebase Storage. |
| **Reset** | Pulse | Full teardown -- disconnects, cancels pending transfers, clears the transfers table, and resets the circuit breaker. Prompts for confirmation. |
| **Cancel Transfers** | Pulse | Cancel all pending (not yet started) transfers. Active transfers complete normally. |

## About

| Parameter | Type | Description |
|-----------|------|-------------|
| **Build Number** | Int | Current build number (read-only). |
| **Build Date** | Str | Build timestamp (read-only). |
| **Touch Build** | Str | TouchDesigner build the component was last saved with (read-only). |
