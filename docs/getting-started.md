# Getting Started

This guide walks through setting up the GCP COMPs from scratch. The steps are nearly identical for Firestore and Storage.

## 1. Download

Download the latest `.tox` files from [GitHub Releases](https://github.com/theexperiential/td-gcp/releases):

- `firestore-bXX.tox` -- Firestore real-time sync
- `storage-bXX.tox` -- Firebase Storage file transfers

## 2. Add to Your Project

Drag the `.tox` file(s) into your TouchDesigner network. Each COMP appears as a single baseCOMP.

## 3. Set Credentials

You need a GCP service account JSON key file:

1. Go to the [GCP Console IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Create a service account (or use an existing one)
3. Grant it the appropriate role(s):
    - **Cloud Datastore User** for Firestore
    - **Storage Admin** (or **Storage Object Admin**) for Firebase Storage
4. Create a JSON key and download it

On each COMP, set **Private Key File** to the path of your downloaded JSON key. The same key file works for both COMPs if the service account has both roles.

!!! warning "Keep your key file secure"
    Never commit service account keys to version control. Add `*.json` key files to your `.gitignore` or store them outside your project directory.

## 4. Bootstrap

On first run, each COMP detects that the required Python packages are not installed and prompts you to approve installation:

- **Firestore**: `firebase-admin`, `google-cloud-firestore`
- **Storage**: `firebase-admin`, `google-cloud-storage`

When you click **Install**, the COMP:

1. Locates or downloads [uv](https://docs.astral.sh/uv/) (a fast Python package manager)
2. Creates a virtual environment at `<project_folder>/venv/gcp/`
3. Installs the required packages into the venv
4. Injects the venv's site-packages into TouchDesigner's Python path

This happens once. On subsequent launches, the existing venv is reused automatically. Both COMPs share the same venv at `venv/gcp/`, so packages installed by one are available to the other.

!!! tip "Network access required"
    Bootstrap needs to reach `pypi.org` to download packages. If you're behind a firewall, you may need to run the install manually. See [Troubleshooting](firestore/troubleshooting.md).

## 5. Connect

If **Auto Connect** is on (the default), the COMP connects immediately after bootstrap completes. Otherwise, pulse the **Connect** button.

### Firestore

Once connected, you should see:

- The **status** table shows `state: connected`
- Collection tableDATs appear inside the `collections` sub-COMP
- The log FIFO shows connection messages

### Storage

Once connected, you should see:

- The **status** table shows `state: connected` and the bucket name
- The log FIFO shows a "Connected to bucket" message
- You can now call `Upload()`, `Download()`, `SyncFolder()`, etc.

## 6. Verify

Check the **status** tableDAT inside each COMP for connection state. If something isn't working, check the **log** FIFO DAT for error messages.

## Next Steps

### Firestore
- [Configuration](firestore/configuration.md) -- all parameters explained
- [Sync & Filtering](firestore/sync.md) -- how data flows between Firestore and TD
- [Write Operations](firestore/writes.md) -- pushing data to Firestore
- [Callbacks](firestore/callbacks.md) -- reacting to changes in your own scripts

### Storage
- [Configuration](storage/configuration.md) -- all parameters explained
- [API Reference](storage/api.md) -- upload, download, sync, and more
- [Callbacks](storage/callbacks.md) -- reacting to transfers in your scripts
