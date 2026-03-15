# Troubleshooting

Common issues and how to resolve them.

## Bootstrap fails with a network error

**Symptom**: The dependency install step fails with a connection or timeout error.

**Cause**: The machine can't reach `pypi.org` or `astral.sh` (for `uv` download).

**Fix**:

1. Check your network connection and any firewall/proxy settings
2. If behind a corporate firewall, you may need to install packages manually:
   ```
   pip install firebase-admin google-cloud-firestore --target <project_folder>/venv/gcp/lib/python3.11/site-packages/
   ```
3. If `uv` can't be downloaded, install it manually from [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/)

## Status shows "error" and nothing syncs

**Symptom**: The status table shows `state: error` with a message in `last_error`.

**Common causes**:

- **Bad key path**: The Private Key File parameter points to a file that doesn't exist or isn't valid JSON
- **Wrong permissions**: The service account doesn't have the Cloud Datastore User role
- **Firestore not enabled**: The GCP project doesn't have Firestore provisioned -- go to the [Firebase Console](https://console.firebase.google.com/) and create a database
- **Missing project_id**: The service account JSON file doesn't contain a `project_id` field

Check the **log** FIFO DAT inside the Firestore COMP for the full error message.

## Circuit breaker is open

**Symptom**: Status shows `circuit: open`. Connect attempts are blocked.

**What it means**: The COMP has failed to connect `Circuit Failure Threshold` times in a row (default: 5). It's waiting `Circuit Timeout` seconds (default: 60) before allowing another attempt.

**Fix**:

- Wait for the timeout to expire -- the COMP will automatically probe with a single attempt
- If you've fixed the underlying issue, call `Reset()` to clear the circuit breaker immediately
- To tune sensitivity, adjust **Circuit Failure Threshold** and **Circuit Timeout** on the Conn parameter page

## Table edits aren't pushing to Firestore

**Symptom**: You edit a payload cell but the change doesn't appear in Firestore.

**Check**:

1. **Enable Write-Back** is on
2. The cell contains valid JSON -- check the log for "invalid JSON" warnings
3. The `_dirty` flag is set to `1` after your edit (if it's `0`, the write-back already fired or was suppressed)
4. The circuit breaker is closed (`circuit: closed` in the status table)
5. You edited the `payload` column (column index 1), not another column

If the JSON was invalid and you've fixed it, pulse **Flush Dirty** to retry all dirty rows.

## Duplicate data or echo writes

**Symptom**: A document you pushed to Firestore appears to be re-applied, or you see the same change processed twice.

**Explanation**: The COMP uses version-based echo prevention. When it writes a document, it stores the Firestore `update_time` and skips any inbound snapshot with an equal or older timestamp. This should prevent self-echoes automatically.

**When it can fail**:

- If a `Reset()` was called between the write and the echo arriving (version cache is cleared)
- If the write was queued offline and flushed much later (the version for that doc may have been overwritten)

This is rare in practice and resolves itself on the next real change.

## The venv/gcp folder is large

**Expected**: The `venv/gcp` folder can be 50-100 MB+ because `firebase-admin` pulls in gRPC, protobuf, and other Google Cloud dependencies.

This is a one-time cost. The venv is reused across sessions and can be excluded from version control (it's in `.gitignore` by default for TD projects).

## Collections don't appear

**Symptom**: Connected successfully but no collection tableDATs are created.

**Check**:

1. **Enable Listener** is on
2. The **Collections** parameter has the right collection names (or is blank for auto-discovery)
3. The Firestore database actually has data -- check in the [Firebase Console](https://console.firebase.google.com/)
4. Auto-discovery only finds top-level collections. Subcollections must be watched manually with `WatchCollection('parent_doc/subcollection')`

## Log output is missing or truncated

The internal `log` FIFO DAT holds a maximum of 200 lines. For longer retention, set the **External Log DAT** parameter to point to your own fifoDAT with a higher `maxlines`.

For programmatic access, `GetLogs()` reads from a 500-entry ring buffer that includes all log levels (including DEBUG).
