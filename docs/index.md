# TouchDesigner x GCP Integration Suite

Drop-in COMPs that connect TouchDesigner to Google Cloud Platform services -- real-time data sync, cloud storage, and more. Each component handles authentication, connection management, and offline resilience so you can focus on your project.

---

<div class="grid cards" markdown>

-   **Firestore**

    ---

    Real-time bidirectional sync between Firestore collections and TouchDesigner tableDATs. Includes offline write queue, circuit breaker, and auto-bootstrap.

    [:octicons-arrow-right-24: Firestore docs](firestore/index.md)

-   **Storage** *(coming soon)*

    ---

    Upload, download, and manage files in Firebase Storage directly from TouchDesigner.

</div>

---

## Requirements

- **TouchDesigner** 2025.32280 or later
- **Google Cloud project** with the relevant service(s) enabled (e.g., Firestore)
- **Service account JSON key** with appropriate permissions

## Quick Start

1. Download the latest `.tox` from [GitHub Releases](https://github.com/theexperiential/td-gcp/releases)
2. Drag it into your TouchDesigner project
3. Set the **Private Key File** parameter to your service account JSON
4. The component auto-installs Python dependencies and connects

[:octicons-arrow-right-24: Full getting started guide](getting-started.md)
