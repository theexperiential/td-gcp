# Troubleshooting

Common issues and how to resolve them.

## Bootstrap fails with a network error

**Symptom**: The dependency install step fails with a connection or timeout error.

**Cause**: The machine can't reach `pypi.org` or `astral.sh` (for `uv` download).

**Fix**:

1. Check your network connection and any firewall/proxy settings
2. If behind a corporate firewall, you may need to install packages manually:
   ```
   pip install firebase-admin google-cloud-storage --target <project_folder>/venv/gcp/lib/python3.11/site-packages/
   ```
3. If `uv` can't be downloaded, install it manually from [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/)

## Status shows "error" and nothing works

**Symptom**: The status table shows `state: error` with a message in `last_error`.

**Common causes**:

- **Bad key path**: The Private Key File parameter points to a file that doesn't exist or isn't valid JSON
- **Wrong permissions**: The service account doesn't have the **Storage Admin** or **Storage Object Admin** role
- **Storage not enabled**: The GCP project doesn't have Firebase Storage provisioned -- go to the [Firebase Console](https://console.firebase.google.com/) and enable Storage
- **Missing project_id**: The service account JSON file doesn't contain a `project_id` field
- **Wrong bucket name**: The Bucket Name parameter doesn't match an existing bucket

Check the **log** FIFO DAT inside the Storage COMP for the full error message.

## Circuit breaker is open

**Symptom**: Status shows `circuit: open`. Connect attempts and transfers are blocked.

**What it means**: The COMP has failed `Circuit Failure Threshold` times in a row (default: 5). It's waiting `Circuit Timeout` seconds (default: 60) before allowing another attempt.

**Fix**:

- Wait for the timeout to expire -- the COMP will automatically probe with a single attempt
- If you've fixed the underlying issue, call `Reset()` to clear the circuit breaker immediately
- To tune sensitivity, adjust **Circuit Failure Threshold** and **Circuit Timeout** on the Conn parameter page

## Uploads fail with "Not connected"

**Symptom**: Calling `Upload()` or other transfer methods returns an empty string and logs "Not connected".

**Fix**:

1. Check that **Private Key File** is set to a valid service account JSON
2. Pulse **Connect** or enable **Auto Connect**
3. Check the status table -- `state` should be `connected`

## Files aren't appearing after sync

**Symptom**: You pulsed Upload/Download/Sync but files didn't transfer.

**Check**:

1. **Local Path** is set to an existing folder (for uploads, the folder must contain files)
2. **Remote Path** is set to the correct prefix (or blank for bucket root)
3. For downloads, the bucket actually contains files under the remote prefix -- use `ListFiles()` to verify
4. The transfers tableDAT shows the operation's status -- look for `failed` entries with error messages

## Signed URLs fail with "credentials" error

**Symptom**: `GetSignedUrl()` raises an error about missing credentials or signing.

**Cause**: Signed URLs require the service account's private key for local signing. This should work automatically with a JSON key file, but can fail if:

- The key file is a P12/PKCS12 format instead of JSON
- The `google-auth` library is outdated

**Fix**: Ensure you're using a JSON key file (not P12) and that packages are up to date. Re-run bootstrap if needed by calling `Reset()` then `Connect()`.

## Transfers are slow or queued

**Symptom**: Transfers seem stuck as "pending" in the transfers table.

**Explanation**: The **Max Concurrent** parameter (default: 3) limits simultaneous transfers. Additional transfers wait in a queue and are promoted as active ones complete.

**Fix**: Increase **Max Concurrent** if your network and use case support more parallelism. Note that `SyncFolder()` runs as a single task that handles all files internally and bypasses the concurrency limit.

## The venv/gcp folder is large

**Expected**: The `venv/gcp` folder can be 50-100 MB+ because `firebase-admin` pulls in gRPC, protobuf, and other Google Cloud dependencies.

This is a one-time cost. The venv is reused across sessions and shared with the Firestore COMP if both are installed. It can be excluded from version control (it's in `.gitignore` by default for TD projects).

## Log output is missing or truncated

The internal `log` FIFO DAT holds a maximum of 200 lines. For longer retention, set the **External Log DAT** parameter to point to your own fifoDAT with a higher `maxlines`.

For programmatic access, `GetLogs()` reads from a 500-entry ring buffer that includes all log levels (including DEBUG).
