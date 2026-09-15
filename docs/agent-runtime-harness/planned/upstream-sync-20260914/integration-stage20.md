# Stage 20 — pushed candidate, focused proof, remaining decision

## Recovery state

- Original, local main, origin/main: `34ad8ba33f2508ab10bb24a26f0377ddb62660cb` (main not updated).
- Pinned upstream: `110baa095bc7135a0624557a9cc35df0f98ece0f`.
- Candidate and both pushed recovery/PR branches: `a27fc85350c458113c3b75f03c89a9a2145bbffa` (`codex/updater-history-safety-20260915`, `codex/sync-regression-recovery-20260915`).
- Review-only Local llama consolidation: `5104732788a545579aa8498f211467a55679b9e8`; original tracked tree `5f5a34ad214d695f5c889a0798e220d175cc91ad` remains its exact tree.
- Launcher main: `241391dd66a55d4375fee81609c9195d8240c1b7`.
- Fresh CI: https://github.com/nekwo/hermes-agent/actions/runs/35026617880 (in progress at checkpoint). Draft PR: https://github.com/nekwo/hermes-agent/pull/1 . No FF delivery until validation and the pending test-policy decision are resolved.

## Additional proof

All Python test runs below used `scripts/run_tests.sh`.

- `tombstone-scope-repairs.log`: 1,161 passed, 0 failed across the tombstone registry and two restored behavior tests. The scanner now prunes already-excluded dependency directories before descent. It preserves retired node-control registration keys without confusing `_run_node_bootstrap` with the `run_node` registration; the retired projector lease guard covers agent_runtime, while upstream's independent agent turn lease remains present. The usage swallow guard follows its intended fork harness owner, preserving the upstream CLI usage reader relocated from cli.py. Restored code-scoped installation-stamp and rich radio-label behavior tests cover two live subjects formerly referenced only by deleted tests.
- `linux-final-regression-recheck.log`: 198 passed, 1 failed, 1 skipped in ten files. Includes path-identity gate, all six cwd receipt tests, environment and frozen-home gates, shell keygen, host guard, gateway reconnect/cron shutdown, fleet fixture and command detector. The remaining failure is the real external `sort --compress-program` probe: this WSL host resolves uutils coreutils 0.8.0, which did not execute the marker for the test's small input. No Hermes approval assertion failed. GitHub's GNU-tool environment remains the required confirmation. The last requested dashboard path carried CR at the shell-script boundary and was not selected; Windows dashboard proof is 33 passed. Script line endings corrected for future reuse.
- `patched-runtime-llama-wal-focused.log`: 73 passed, 0 failed on isolated Python 3.12.14 / SQLite 3.53.1. All 48 Local llama tests passed immediately. The SQLite file first exceeded its 30-second test budget while cold imports searched executable paths; the runner's one-worker bounded retry passed in 16.3 seconds. Report this recovered timeout, not an uninterrupted green run.
- Patched runtime dependency install used candidate `[messaging,web,cli,mcp]`. The current project has no `cli` extra, so that requested extra was ignored. uv's broad cryptography override selected 50.0.1 despite the package/lock pin of 50.0.0; restored the exact declared pin with pip. `pip check`: `No broken requirements found.` Raw `patched-runtime-install.log`, `patched-runtime-cryptography-pin.log`.
- The attempted nonexistent `test_local_llama_runtime.py` target exited 1 before running tests; it is not proof. The nine actual paths were discovered and run in the focused log above.

## Required next steps

1. Resolve the exact source-inspection policy proposal in `source-assertion-policy-decision.md`. No approval has been inferred from elapsed time; neither closed register was expanded. The source gate still intentionally rejects the unchanged upstream assertions pending that choice.
2. Read current CI results, address any actual regressions, and verify the selected policy implementation. Do not transplant the old a3d full-suite result as current proof.
3. Before deployment, recheck registered worktrees, remotes, current main and active managed runtime ownership. The approved maintenance window is limited to Hermes; no Launcher restart/config edits. Main remains a history-preserving fast-forward descendant, never the consolidated review branch.
4. Qualify and stage a patched Python build outside the operator's active interpreter. Back up the live venv and package inventory before any in-place interpreter/dependency refresh. Keep the canonical venv path and preserve optional installed features; verify dependency consistency, SQLite version, original config hashes and gateway receipts. Do not copy a relocated virtualenv and assume its entry-point paths are portable. No live venv replacement or service restart has occurred yet.
5. Update local main and origin/main only after proof, perform the approved brief managed-gateway maintenance, and verify the actual restarted incarnation. Leave unrelated services and test worktrees alone.

## Installer handoff addition

The existing installer handoff remains design-only. Qualification must record actual embedded runtime/library versions, not infer SQLite safety from the Python minor version: the isolated 3.12.13 build still contained vulnerable SQLite 3.50.4, while 3.12.14 contained 3.53.1. Respect declared dependency pins and verify package metadata consistency after using resolver overrides. Keep Local llama ownership, revision guards, receipts, active-turn exclusion and model routing unchanged; Launcher installer implementation still waits for the frozen producer contract.
