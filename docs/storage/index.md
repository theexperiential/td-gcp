# Storage

The Storage COMP provides file transfer between Firebase Cloud Storage and TouchDesigner.

## What It Does

- **Upload**: Push local files to a Firebase Storage bucket, preserving subfolder structure
- **Download**: Pull remote files to a local folder, recreating the directory tree
- **Sync**: Bidirectional folder sync -- uploads missing/newer local files and downloads missing/newer remote files
- **Delete**: Remove remote blobs by path
- **List & Metadata**: Enumerate files and retrieve blob metadata asynchronously
- **URLs**: Generate public or time-limited signed URLs for sharing

## Data Flow

```
Local Filesystem                          Firebase Storage Bucket
    |                                          |
    | Upload / SyncFolder(direction='upload')  |
    | ---------------------------------------->|
    |                                          |
    | Download / SyncFolder(direction='download')
    | <----------------------------------------|
    |                                          |
    |   SyncFolder(direction='both')           |
    | <--------------------------------------->|
```

All transfers run in ThreadManager pool threads. Results flow back to the main thread via `queue.Queue`, drained every frame by a keepalive RefreshHook. The `transfers` tableDAT tracks every operation's status in real time.

## Accessing the COMP

The Storage COMP registers both a **Parent Shortcut** and a **Global OP Shortcut** named `storage`:

```python
# From anywhere in the project
op.storage.Connect()
op.storage.Upload('photo.jpg')
op.storage.Download('media/video.mp4')

# From inside the COMP's parent
parent.storage.GetStatus()
```

## Sections

- [Configuration](configuration.md) -- every parameter explained
- [Callbacks](callbacks.md) -- reacting to transfers in your scripts
- [API Reference](api.md) -- complete method listing
- [Architecture](architecture.md) -- threading model and extension breakdown
- [Troubleshooting](troubleshooting.md) -- common issues and fixes
