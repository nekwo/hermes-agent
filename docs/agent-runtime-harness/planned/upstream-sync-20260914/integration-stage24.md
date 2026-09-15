# Stage 24 — published-main delivery and live verification

## Outcome

Hermes local main and origin/main are **`c112a9347a4e14ce573cdbf3d154177a6f521843`**, clean and 0/0 ahead/behind. GitHub PR #1 is merged at that same commit. Main was advanced with `git -c core.hooksPath=NUL merge --ff-only c112a9347a4e14ce573cdbf3d154177a6f521843` and `git push origin main`. No force push, squash replacement, or delivery-rule change occurred.

Original `34ad8ba33f2508ab10bb24a26f0377ddb62660cb` and pinned upstream `110baa095bc7135a0624557a9cc35df0f98ece0f` are both ancestors. Pushed recovery `codex/sync-regression-recovery-20260915` equals delivered main. Original-main/upstream/prefetch backup refs remain unchanged. All historical contributors and merge ancestry survive.

The 15-theme review index remains an index, not a forced 15-commit rewrite. The separate Local llama 8-to-3 review branch ends at `5104732788a545579aa8498f211467a55679b9e8`, with the same tracked tree as original main: `5f5a34ad214d695f5c889a0798e220d175cc91ad`. See `original-to-consolidated.csv`, `original-to-review-groups.csv` and `local-llama-consolidation.json`. The review branch was not substituted for main.

## Maintenance performed

The already-authorized Alice gateway stop drained cleanly. The only detected gateway process group was stopped through its canonical profile owner. After stopping, 72 root/profile state files were additionally copied to the local recovery directory. Main was fast-forwarded, the existing managed venv was upgraded in place, and declared dependencies were refreshed. `pip check` passed.

Live environment: **Hermes 0.21.3, Python 3.12.14, SQLite 3.53.1**. Venv path remains `X:/Eternia/.hermes/venvs/hermes-agent`; interpreter base is the separately staged `X:/Eternia/.hermes/runtimes/python/cpython-3.12.14-windows-x86_64-none/python.exe`. No global Python/PATH change was made.

Alice restarted through `hermes --profile alice gateway start`. New writer PID **33636**, parent **34288**, uses the patched interpreter. The profile's freshly written state reports Telegram connected, writer PID 33636 and zero active agents. The old WhatsApp-not-paired state predates this update; the stale WhatsApp Cloud connected record is not claimed as new health evidence. Root and Alice config SHA256 both match the pre-maintenance values after restart. Launcher was not restarted or rebuilt.

Recovery directory: `X:/Eternia/.hermes/backups/upstream-sync-20260915/` contains the full old venv, package inventory, config hashes and stopped-state files. The old venv's 7,421 non-cache files were hash-verified before maintenance. Do not reset published main to roll back; preserve history and diagnose/revert through a new descendant when necessary.

## Validation and honest remaining gaps

Stage 23 records the exact broad-run result and every final repair. The broad run passed 59,007 tests, failed three description/inventory assertions and timed out on one historical audit. Every remaining issue is covered by final focused proof: Linux 1,177 passed; Windows complete audit 1,157 passed plus 20 passing description/inventory checks. The audit is now roughly 96 seconds on Linux / 114 seconds on Windows, with no row exclusions or timeout increases. Native temporary-Git fixtures explicitly preserve LF and older Git compatibility. Full isolated real llama/model/tool-roundtrip proof passed on the patched runtime before delivery; receipt includes completed inference, unload/reload and owned shutdown. See `final-proof.json` for raw-artifact hashes and the stage notes for exact commands.

Fresh full CI on delivered main: https://github.com/nekwo/hermes-agent/actions/runs/35034006672 . It was pending at this checkpoint. Delivery used the completed broad run plus focused proof of all remaining failures; this report does not claim a fresh exact-head full-suite pass. No post-restart live provider message, second physical host, full installed-Launcher acceptance or Stage C proof was performed. Those are distinct from the isolated real-model proof and fresh Telegram connection.

Launcher local/origin main is `241391dd66a55d4375fee81609c9195d8240c1b7`. Desktop-button safeguards and producer fixtures are delivered in source: preflight history before stopping a runtime, preserve recovery refs, fast-forward when safe, refuse divergence without destructive reset. The old July pre-fold archive is not assumed to be an ancestor and still requires individual review. No rebuilt Launcher binary is claimed.

## Worktree preservation and cleanup

Unrelated Launcher primary (65 dirty entries), maps-voice (9), and local-llama-hermes (7 untracked proof directories) were preserved; other unrelated worktrees remain unchanged. The task's two landed worktrees were passed to `git worktree remove`. Git removed their registrations but returned `Directory not empty` on Windows, leaving residual files under `X:/wt/hermes-updater-history-20260915` and `X:/wt/hermes-upstream-integration-20260915`. No manual directory deletion or unrelated cleanup was attempted. The audit worktree remains registered intentionally because it owns these recovery notes and raw evidence.

## Installer handoff

Start from delivered main and `installer-handoff.md`, then revalidate the proposed installation contract. Reuse upstream bootstrap/catalog/download primitives behind the fork's sole Local llama lifecycle owner. Preserve revisions, receipts, leases and active-turn exclusion; separate install/validate/activate; qualify the actual linked SQLite and declared dependency pins. Freeze backend-produced success/error/empty/null fixtures before Launcher implementation. No installer was implemented in this synchronization.
