# Pre-Commit / Pre-Push Checklist

**This is a BLOCKING requirement.** When the user asks to commit or push, you MUST complete every step below before running `git commit` or `git push`. Do NOT skip steps. Do NOT commit with items outstanding. If any step fails, stop and report what needs to be fixed.

## 1. Full Change Evaluation

- Run `git status` and `git diff` (staged + unstaged) to see ALL modified, added, and deleted files.
- **Read every changed file in full** — not just the diff, but enough context to understand the impact of each change.
- Summarize what changed and why before proceeding to the next steps.

## 2. Build / Version Number Check

- The Firestore COMP carries a `Build` parameter (auto-bumped by `dev/gcp/release_exec.py` on TD project save).
- Verify the build number referenced in `docs/changelog.md` matches the current `Build` value on the COMP. If TD is running, check via MCP (`get_parameter` on the Firestore COMP's `Build` par). If TD is not running, check the `.tdn` file or ask the user.
- If the changelog references a stale build number, update it before committing.

## 3. Changelog Update

- Open `docs/changelog.md` and confirm there is an entry for the **current build**.
- The entry MUST describe **every functional change** included in this commit — new features, bug fixes, behavior changes, removed functionality.
- If the changelog is missing or incomplete, draft the missing entries and show them to the user for approval before writing.
- Follow the existing format: `## Build NN -- YYYY-MM-DD` heading, grouped under `### Features`, `### Fixes`, `### Changes`, etc. as appropriate.

## 4. Documentation Audit

- For every functional change (new feature, changed behavior, new/removed parameter, API change), identify which documentation page(s) in `docs/` must be updated:
  - Parameter or config changes → `docs/firestore/configuration.md`
  - API / extension method changes → `docs/firestore/api.md`
  - Sync or filtering behavior → `docs/firestore/sync.md`
  - Write operations → `docs/firestore/writes.md`
  - Offline queue / cache behavior → `docs/firestore/offline.md`
  - Callback changes → `docs/firestore/callbacks.md`
  - Architecture changes → `docs/firestore/architecture.md`
  - Troubleshooting additions → `docs/firestore/troubleshooting.md`
  - New integrations or recipes → `docs/recipes.md`
  - Top-level project changes → `docs/index.md` or `docs/getting-started.md`
- **Read each affected doc page** and verify it reflects the current state after the changes.
- If any doc is outdated or missing coverage, update it before committing.

## 5. Final Gate

- Re-run `git diff --staged` to confirm all updates (code + changelog + docs) are staged.
- Present a summary to the user:
  - What changed (code)
  - Build number
  - Changelog entry (quoted)
  - Docs updated (list of files)
- Only proceed with `git commit` after the user confirms the summary.
