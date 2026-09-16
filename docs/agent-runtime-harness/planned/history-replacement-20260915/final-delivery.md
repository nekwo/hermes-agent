# Final delivery — 2026-09-15

The operator clarified that Launcher distribution was not requested, removed
the binary-release dependency, and instructed immediate Hermes main promotion.
The exact-tree replacement is now on local/main and origin/main. GitHub's own
comparison API reports **15 ahead / 642 behind** NousResearch main.

## References

| Reference | SHA |
| --- | --- |
| Original pre-upstream fork snapshot | `34ad8ba33f2508ab10bb24a26f0377ddb62660cb` |
| Pinned integrated upstream | `110baa095bc7135a0624557a9cc35df0f98ece0f` |
| Prior history-preserving integration merge | `97633709cff4b7213b031884a700fe13bafd9f19` |
| Frozen original main / permanent backup | `c112a9347a4e14ce573cdbf3d154177a6f521843` |
| Consolidated candidate / Hermes local and remote main | `0d5b7b8abccc07eb21d20d789fd0e14b68591aa4` |
| Latest observed upstream main | `416a8177c25d87aa9929dfcf31f7964137d7fcdd` |
| Launcher updater implementation | `77422fe63c5cf89216beb02a82b75034bd62b958` |
| Launcher local and remote main, release-only bump undone | `f1b6923fa2034501b03139e3746cb3e86bce9da2` |

Hermes backup branch: `codex/pre-history-replacement-20260915`.
Final compact branch: `codex/compact-main-final-20260915`.
Full mapping and notes: `codex/history-replacement-20260915`.
Migration data: `codex/hermes-history-migrations` at
`f82705d6c3` (the manifest names the final candidate).

Original and replacement whole tracked tree are exactly
`b533fff1abaa2289abc90d4664c07f421e00de19`. The Git diff is empty. Main was
published using an explicit lease requiring the old remote SHA, followed by
an expected-old local ref update. No checkout/reset changed tracked files.
Hermes primary is clean and up to date. Launcher local/main equals origin/main;
its 65 unrelated primary-worktree changes remain preserved, as do the voice
worktree changes and Local llama QA artifacts.

## Mapping, conflicts and behavior

`tree-proof.json` records all 15 dependency-oriented theme checkpoints and SHAs.
`path-groups.csv`, `original-to-consolidated.csv` and `original-commits.json`
record net paths and all 1,234 original commits, including author/coauthor
credit, merge ancestry and archive-only history. The checkpoints have shared
dependencies and are delivered together; they are not independently runnable
releases. Original history remains reachable from the permanent backup.

This operation introduced no source conflicts or behavioral changes: it retains
the result of the prior 187-conflict upstream integration exactly. The existing
runtime, auth/config/provider behavior, Local llama ownership/receipts/revision
guards/routing and active-turn exclusion are unchanged. Refer to the prior
upstream-sync conflict ledger for those resolutions.

## Exact focused proof

Launcher command:

```text
flutter test --no-pub test/features/mission_control/hermes_history_migration_test.dart test/features/mission_control/mission_control_hermes_update_test.dart test/features/mission_control/state/hermes_update_apply_controller_test.dart --reporter expanded
```

Exit 0: **50 passed**, 193 seconds. Thirteen cases use real temporary Git
repositories; the service case stubs Python reinstall only. Coverage includes
old installations, staged/unstaged/untracked work, ignored collisions, shallow
history, wrong tree metadata, concurrent branch/HEAD changes, target commits
after the fold, interruption after the identity swap, retry and daily updates.
Formatting-only lint fixes followed, then focused analysis of all six changed
Dart/test files passed with no issues (exit 0). The first compile failure and
initial lint findings were corrected, not waived.

Preserved final test-log SHA256:
`053c5dd03f86d853ca6ffa94b8ae32ae9ac90675118151fcc1e3142005f908ff`.
Final analysis-log SHA256:
`e6cadad442e57fd0673f70bd1cfd1b116ffc18ef0891c87cc7aea606748458f6`.
Raw logs remain in the audit worktree's
`qa-artifacts/history-replacement-20260915/` directory.

No repeat runtime/model suite was run for this history-only operation; the
prior integration's focused runtime/auth/provider/Local llama and real-model
proof applies to the identical tracked tree. Frozen-main CI passed Python,
e2e, mutation and OS checks but has one unchanged upstream Electron quickstart
UI test failure documented in stage 2. Full CI is therefore not claimed green.
No new visual Stage C proof is claimed.

## No Launcher distribution

All release attempts were cancelled before any publish job ran. No Launcher
binary was distributed. The temporary Actions signing secret was removed and
the unnecessary version bump reverted. Launcher release branch remains at
`77422fe63` after its earlier fast-forward; it was not force-rewound. Its prior
tip remains at `codex/release-backup-20260915`.

The installed older Launcher binary may refuse this history transition until
it receives the source fix. The operator explicitly chose immediate Hermes
main promotion with source-only Launcher delivery. No runtime configuration,
live Hermes process or running Launcher was changed or restarted.

## Local llama installer handoff

Start from Hermes main `0d5b7b8abc`. This history replacement changes no backend
contract. Continue from the existing planned installation contract and prior
upstream-sync installer handoff; installer implementation remains out of scope.
Keep installation ownership, receipts, revision guards, inference routing and
active-turn exclusion intact and verify them against an isolated runtime.
For subsequent upstream updates use a dedicated branch, review the 15 themes,
resolve actual source conflicts and run focused tests. Daily updates need not
rewrite history; another fold requires a retained migration chain for skipped
updates, which schema v1 does not yet implement.
