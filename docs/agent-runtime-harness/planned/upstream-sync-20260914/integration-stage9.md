# Integration checkpoint 9

Candidate `567770b10dddcc5ff48019a932432f71bbaf156d` is committed and pushed. Two-parent merge `97633709cff4b7213b031884a700fe13bafd9f19` preserves original main and pinned upstream as ancestors. Main is still unchanged; final auth/local-runtime seam checks and delivery review remain.

## Reconciliations proven

- Restore downstream rotation mixin on upstream credential pool; lazy auth imports remove the re-export cycle. Cursor/least-used durability and upstream selection tests pass.
- Keep Local llama small-context exception on the same verified endpoint/model only. All 48 Local llama tests pass under the candidate pinned environment. Real model text/tool/compression/reload/owned shutdown probe passes.
- Preserve both Codex modes: upstream logical body cache scope, separate explicit downstream persona header scope. All 138 transport tests pass.
- Restore atlas slot helper (35 opt-in synthetic image tests pass), kanban crash-worker ownership and redacted sidecar evidence (39 tests pass), Windows skill path separators and extracted search owners (51 skills tests pass).
- Portable explicit-interpreter hook fixtures prove modification, exit-2 blocking and timeout behavior without relying on Windows shebang execution.
- Preserve background visibility-only defaults; display-format proof explicitly opts into agent turns. Upstream sole-credential cooldown is shorter; readiness fixture now uses fresh exhaustion and retains singleton/missing-credential positive controls.
- Preserve operator todo checklist with upstream `todo_list` and historical `todo` tool names. Provider runner tests pass with both names.
- Sync gate now invokes the canonical per-file wrapper instead of direct pytest. Gate construction tests pass; full gate and Launcher suite not executed.
- Desktop status priority migrated to actual current computed store: one test passed with Node 24.19.0, npm 10.8.2; no app launch. System Node 20 was rejected before installation; candidate-only dependencies installed with scripts disabled.

## Evidence

See `candidate-test-evidence.json` for per-run counts, targets, log hashes and real probe receipt hash. Runs overlap: do not sum them as unique tests. Failed exploratory runs remain recorded; final followups supersede the named failures only. Known existing Slack parity xfail and documented platform skips remain. No full-suite or remote-host/Launcher acceptance claim.

The 7–15 target remains a 15-theme review index, with only the independently tree-equivalent 8-to-3 Local llama review reconstruction. Actual update preserves all published history. No force push, runtime restart/config edit or installer implementation.
