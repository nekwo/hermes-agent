# Integration checkpoint 7

All merge-index conflicts reconciled. The merge is not committed or delivery-ready: behavioral fixes, broader regression tests and isolated model proof remain. Main and origin/main are unchanged.

## Decisions

- Kept downstream mutation-claims CI job alongside upstream unsliced tests; removed obsolete slice-generation job. Upstream custom 96-core runner availability in this fork remains unverified.
- Retained downstream MCP overrides and upstream interpolation docs; removed obsolete automatic session-expiry policy documentation (upstream now has explicit lifecycle boundaries).
- Archived original fork profile-builder and Telegram-plan records under docs/downstream/archive.
- Relocated four fork storage tests to tests/hermes_state/test_downstream_session_contracts.py; retained upstream storage suites. Retained fork surrogate tests because the fork repaired the offline fixture/root cause for upstream's flaky-file deletion.
- Moved sidebar priority regression to the current session-dot-state store, retaining upstream sentinel-based E2E tests that eliminate background-dot timing races. Desktop tests pending.
- Migrated fixtures to current module owners, preserved async Feishu home-resolution tests, native Windows paths, typed profile guards, tool-disclosure permissions and brief/full descriptions. Preserved upstream adaptive replace/V4A patch schema.
- Repaired test-runner probe roots (both temp probes now have pytest.ini), and narrowed timeout retry detection so pytest's ordinary timeout configuration header cannot classify an import error as a timeout.
- Human-facing process completion keeps upstream status headers and bounded redacted output; downstream opt-in agent-turn policy remains.

## Candidate proof since stage 6

All through scripts/run_tests.sh; raw logs remain in audit qa-artifacts. No whole-repo suite run.

- candidate-pinned-env-stage6.log: exit 1, 175 passed, 3 failed, 2 skipped; runner file passed only on retry before temp-root fix.
- candidate-reconciliation-stage7.log: exit 1, 404 passed, 6 failed; runner timeout recovered on isolated retry. Subsequent fixes changed the affected files.
- candidate-reconciliation-stage7b.log: exit 1, 553 passed, 17 failed, with platform skips and one known Slack xfail. Clean file passes include 64 tool-search, 70 file-operations, 51 file-tool, 22 runner, 38 background-notification, 13 terminal, 23 execute-code approval, 11 noninteractive-auth tests. Failures include stale test imports/patch owners and doctor/browser isolation; follow-up fixes applied, rerun stage7c in progress. These totals overlap earlier runs and must not be summed as unique coverage.
- Original Local llama candidate tests: 48 passed in eight files under the previous shared environment; must repeat under pinned candidate environment for final evidence.

Remaining: cache-routing test migration and legacy-persona invariants; skills collision and schema compatibility; broader runtime/auth/config/provider regression; real llama/tool roundtrip; installer contract reassessment; merge commit, recovery push and safe main delivery only after proof.

## Recovery

Cumulative patch relative to pinned upstream, not a runnable standalone commit.

```json
{
  "paths": 256,
  "patch_bytes": 1256642,
  "patch_sha256": "1f6e05660789f44b1fe827095a28414884ce7b5d108c096fea6ea8339b120c61",
  "remaining_unmerged": []
}
```
