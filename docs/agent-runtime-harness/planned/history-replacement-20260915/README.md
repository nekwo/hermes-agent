# Same-content history replacement — 2026-09-15

## Authorization and invariant

The operator explicitly replaced the previous no-rewrite constraint: “yes lets
rewrite but make sure the code stays the same.” This exception applies to
Hermes main only. Launcher retains normal fast-forward delivery.

Frozen main: `c112a9347a4e14ce573cdbf3d154177a6f521843`.
Pinned upstream: `110baa095bc7135a0624557a9cc35df0f98ece0f`.
Final candidate: `0d5b7b8abccc07eb21d20d789fd0e14b68591aa4` on
`codex/compact-main-final-20260915`.
Both tracked trees: `b533fff1abaa2289abc90d4664c07f421e00de19`.
Git diff is empty, including modes, deletions, binary files and documentation.
No newer upstream source is introduced by this operation.

The candidate has exactly 15 commits above the pinned upstream. Upstream may
advance independently, increasing the behind count. The 1,234 original commits
remain reachable on pushed `codex/pre-history-replacement-20260915`.

## Review and attribution

`tree-proof.json` lists actual commit order and every group SHA.
`path-groups.csv` maps the 1,737 net changed paths (including mode changes) to
groups. `original-to-consolidated.csv` and `original-commits.json` preserve all
1,234 original SHAs, author identity, parents, messages and coauthors.

Mappings express historical contribution to surviving net paths, not a claim
that every historical patch survives in today's code. Superseded/removed work
is preserved in the archive and explicitly marked archive-only when no changed
path survives. First-parent merge diffs may attribute shared paths broadly.
Consolidated messages preserve associated author/coauthor credit; upstream
ancestry remains intact below the pinned base.

The themes are platform/build, storage, auth/config/providers, core/tool seams,
skills/MCP, personas, conversations, Local llama, transport, projections,
office/board/graph, realm sync, character assets, mobile, and documentation.
Foundations precede consumers where practicable. Accumulated whole-file changes
have cross-theme dependencies: these are review checkpoints delivered together,
not 15 independently deployable releases. This is preferable to inventing code
changes merely to make intermediate snapshots runnable.

Independent review verified tree identity, count and attribution; corrected
misclassified dashboard authentication and storage/provider/character/core tests.
The first candidate `4abf5fa8181aca227413bff9cf3f9d4aa4d5cc5a` remains a pushed
review recovery ref only; do not promote it.

## Update Hermes compatibility

Launcher must ship the migration-capable button before main replacement.
Its metadata comes from this origin's `codex/hermes-history-migrations` branch,
at `hermes-history-migration.json`. This metadata is deliberately outside the
replacement's tracked tree. It contains data only, never a script to execute.

The updater verifies attached clean main, full ancestry, no hidden sparse/index
flags, old/new tree identity, local HEAD ancestry in archived history, and the
fetched target's descent from the replacement. It preserves a local recovery ref,
fast-forwards to the archived tip, swaps only the identical-tree commit identity
with an expected-old-value check, then fast-forwards to the pinned fetched target.
Existing active-turn/ownership/runtime maintenance gates remain authoritative.
Unknown divergence, local commits, dirty files and shallow ancestry fail closed.
Do not run another checkout operation concurrently with an update.

This schema handles this one replacement and ordinary subsequent daily updates.
Before another history replacement, extend metadata to retain/traverse all
bridges; replacing this record would strand users who skipped this migration.
Daily updates do not require repeated squashing.

## Promotion and recovery

Before promotion, verify the normal signed Launcher update channel offers the
tested migration-capable build. Then verify origin/main still equals frozen main
and publish with an explicit expected-old lease, never an unconditional force.
If main moved, stop and rebuild/revalidate against the new snapshot.

Reconcile clean primary main using expected-old `update-ref` only after proving
its index and tracked tree match the candidate; no checkout or hard reset is
necessary. Confirm local/main and origin/main equal the candidate and GitHub's
ahead count is 15. Do not merge the archival history back into compact main.

Recovery refs are permanent. An unsuccessful rollout is investigated using the
archive and local `refs/eternia/history-recovery/` refs; do not blindly reset user
installations or force-push an unverified rollback.

## Future upstream and installer handoff

Integrate upstream in an isolated branch based on compact main, record the new
baseline, resolve conflicts explicitly, and run focused regression tests through
`scripts/run_tests.sh` before ordinary published delivery. Fifteen themes make
review easier but do not remove real source conflicts.

The Local llama installer remains planned, not implemented. This replacement
changes no runtime/backend contract. The prior upstream synchronization's
`upstream-sync-20260914/installer-handoff.md` remains the contract handoff;
reverify ownership, receipts, revision guards, routing and active-turn exclusion
before implementing installation. Prior runtime/model proof remains tied to the
identical source tree; this operation requires migration proof, not another
full runtime suite.

## Current stage

Complete: Hermes local and remote main now name the 15-commit replacement.
The operator explicitly removed the Launcher distribution dependency; updater
source is on Launcher main, with no binary distribution. See final-delivery.md
for final SHAs, tests, recovery refs, remaining gaps and installer handoff.
