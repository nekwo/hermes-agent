# Integration checkpoint 1 — 2026-09-15

Status: IN PROGRESS, NOT TESTED OR DELIVERABLE. Main and the running service are unchanged.

Candidate worktree: `X:/wt/hermes-upstream-integration-20260915`, branch `codex/upstream-integration-20260915`.
The pending merge has original main 34ad8ba33f2508ab10bb24a26f0377ddb62660cb and upstream 110baa095bc7135a0624557a9cc35df0f98ece0f.
Merge hooks were disabled for this command because the installed post-merge hook mutates runtime skills.

The companion patch is relative to pinned upstream, and captures 30 reconciled files, including new downstream modules. It is a recovery/review artifact, not a complete integration patch. The JSON records syntax checks and exact file digests. Other conflict work remains in the dedicated merge worktree; do not abort or reset it.

## Reconciliations

- Auth rotation receipts and global Codex readiness moved to downstream auth_extensions; network inference URL validation follows the new auth_nous owner.
- Native compression lineage deletion moved to downstream session_extensions. Stable session ordering and user-row finish_reason survive in upstream's new storage modules.
- Capability probing has one context-local authority in downstream auxiliary_probe, retaining the upstream probe-mode API. Real client construction counters and the Vertex proxy route survive.
- Conversation timing helpers moved to downstream conversation_observability. Request-assembled markers remain after physical transport preflight, first-byte measurements reset per attempt, native reasoning takes precedence over reply echo, usage ledger and durable-user retry reuse remain connected to the new upstream turn phases.
- Doctor reads homes at invocation/check time and no longer imports the operator environment during test collection. Windows managed-interpreter checks and context-local browser-probe injection live downstream.
- OAuth pure data remains one shared catalog for Launcher and dashboard, incorporating upstream's corrected Copilot command and external Anthropic flow. Profile promotion stays a permanent fork endpoint export.

## Installer correction

The earlier installer handoff incorrectly said upstream has no local model installer. Upstream DOES ship `hermes_cli/local_runtime` with binary acquisition, bootstrap, catalog, supervisor, presets, context policy, endpoint detection, and desktop local-model integration. Reassess reusable lower-level components. Do not replace the downstream `local-llama-hermes` manager: its explicit ownership, receipts, revision guards, active-turn leases, no-fallback routing and manual lifecycle must survive. No installer has been implemented.

## Proof and remaining work

Only AST syntax validation has run for this stage. Baseline 394 tests and the isolated real-model/tool probe in verification.md apply to the original fork, not this candidate. Combined tests, conflict completion, old test migration, contract verification, final tree/ancestry review and ff-only landing are pending.

Unmerged paths remaining: 157
