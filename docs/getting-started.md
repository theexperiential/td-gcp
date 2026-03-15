# Getting Started

This guide walks through setting up the Firestore COMP from scratch.

## 1. Download

Download the latest `firestore-bXX.tox` from [GitHub Releases](https://github.com/theexperiential/td-gcp/releases).

## 2. Add to Your Project

Drag the `.tox` file into your TouchDesigner network. The Firestore COMP will appear as a single baseCOMP.

## 3. Set Credentials

You need a GCP service account JSON key file:

1. Go to the [GCP Console IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Create a service account (or use an existing one)
3. Grant it the **Cloud Datastore User** role (for Firestore)
4. Create a JSON key and download it

On the Firestore COMP, set **Private Key File** to the path of your downloaded JSON key.

!!! warning "Keep your key file secure"
    Never commit service account keys to version control. Add `*.json` key files to your `.gitignore` or store them outside your project directory.

## 4. Bootstrap

On first run, the COMP detects that the required Python packages (`firebase-admin`, `google-cloud-firestore`) are not installed. It will prompt you to approve installation.

When you click **Install**, the COMP:

1. Locates or downloads [uv](https://docs.astral.sh/uv/) (a fast Python package manager)
2. Creates a virtual environment at `<project_folder>/venv/gcp/`
3. Installs the required packages into the venv
4. Injects the venv's site-packages into TouchDesigner's Python path

This happens once. On subsequent launches, the existing venv is reused automatically.

!!! tip "Network access required"
    Bootstrap needs to reach `pypi.org` to download packages. If you're behind a firewall, you may need to run the install manually. See [Troubleshooting](firestore/troubleshooting.md).

## 5. Connect

If **Auto Connect** is on (the default), the COMP connects to Firestore immediately after bootstrap completes. Otherwise, pulse the **Connect** button.

Once connected, you should see:

- The **status** table shows `state: connected`
- Collection tableDATs appear inside the `collections` sub-COMP
- The log FIFO shows connection messages

## 6. Verify

Check the **status** tableDAT for connection state:

| Column | Example Value |
|--------|---------------|
| `state` | `connected` |
| `circuit` | `closed` |
| `last_error` | *(empty)* |
| `queue_depth` | `0` |
| `connected_at` | `2026-03-15T06:53:54` |
| `collections` | `users scenes` |

If something isn't working, check the **log** FIFO DAT inside the Firestore COMP for error messages.

## Next Steps

- [Configuration](firestore/configuration.md) -- all parameters explained
- [Sync & Filtering](firestore/sync.md) -- how data flows between Firestore and TD
- [Write Operations](firestore/writes.md) -- pushing data to Firestore
- [Callbacks](firestore/callbacks.md) -- reacting to changes in your own scripts
