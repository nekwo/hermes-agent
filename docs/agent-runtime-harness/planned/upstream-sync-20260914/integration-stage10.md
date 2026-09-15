# Integration checkpoint 10 — candidate complete, live delivery held

Code candidate `b547f783a85a456c3e269b80936c4e85d5c7144a` is pushed. The original fork and pinned upstream are both ancestors. Zero unmerged paths. The independent Local llama review branch remains exactly tree-equivalent to original main; see delivery-readiness.json.

## Final seam proof

Canonical wrapper final followup: 68 passed (53 upstream local-runtime contract tests + 15 TUI kanban notification tests), 0 failed. Auth profile fallback 12, provider scope 3, pool operations 5, store-read failure 6, peer authorization 18 and local-runtime recovery 38 passed (one platform skip). Earlier upstream stub tests waited for an unload state the stub never supplied; corrected HTTP stub now reports unloaded. No production timing change. Notification tests explicitly opt into agent turns and separately prove default visibility without a turn.

Candidate evidence includes all 48 Local llama tests and successful real isolated model/tool probe, 138 Codex transport tests, credential rotation/durability, provider/config/readiness, redacted crash evidence, registry/skill paths, 35 opt-in pet tests and desktop dot priority. Per-run counts overlap. Use candidate-test-evidence.json rather than summing them.

## Exact delivery blocker

Fresh origin fetch confirmed local main and origin/main both remain `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`. Running Hermes serve PID 33896 uses a managed environment with an editable import finder pointing `agent`, `hermes_cli` and `run_agent` at `X:/Eternia/hermes-agent`. A fast-forward of that checkout while the process lives can mix already-loaded old modules with newly imported upstream modules. User explicitly forbids disturbing/restarting the live service. Therefore no main update is justified now. No runtime config changed and no operator process stopped.

FF-only publication itself has no history conflict: the candidate descends from original main. In an authorized maintenance window, recheck all worktrees and main movement, stop/restart only with explicit authorization, fast-forward main to the candidate, push origin/main, verify local/remote equality, and run runtime smoke. If main moves, do not blindly rebase this merge series (ordinary rebase can flatten upstream ancestry); integrate the new main on another descendant candidate or obtain a specific decision if a repository rebase-only rule must change. Never force main.

## Preserved state and gaps

All baseline unrelated worktree heads/status lists match. Dirty Launcher primary (65 entries), maps-voice (9) and local-llama-hermes (7 untracked directories) remain untouched. Audit/candidate worktrees remain deliberately available for evidence and maintenance delivery; they are not landed/disposable.

No full-repository suite, full Launcher/Stage C, second physical host, installer wire fixtures or GitHub CI success is claimed. Upstream workflow uses an unverified `ubuntu-latest-96-core` runner label in this fork. Candidate Python's SQLite activates upstream DELETE-journal safety fallback; no live environment upgrade was attempted. System Node stays unchanged; candidate dependency install used bundled Node 24.19.0.

Installer handoff is source-reassessed, proposed only. Reuse upstream primitives with one fork lifecycle owner; preserve receipt/revision/inference guards; installer fixture and served-wire proof must precede Launcher integration.
