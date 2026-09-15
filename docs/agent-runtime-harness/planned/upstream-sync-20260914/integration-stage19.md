# Stage 19 — regression repairs and pre-deployment runtime check

Hermes main remains `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`; no service stop, configuration edit, or main update occurred. Launcher delivery remains `241391dd66a55d4375fee81609c9195d8240c1b7`.

Recovery candidate progressed through `d5e29ab7b4`, `88123867e4`, `4fc82e74b8`, `311912593c`, `81106e5596`, and `fc55c2ffec`. The PR branch is intentionally held at a3d33e95a9 while this known regression batch is repaired; separate recovery ref `codex/sync-regression-recovery-20260915` is pushed.

## Product repairs and evidence

- Completion publication: finished-process waiters previously observed completion before the notification was queued. The completion event is now published after queue insertion, and waiters include finished processes with unpublished completion. Deterministic event-blocking regression failed before and passes after. Real CLI/local HTTP provider/shell child/fresh receipt-reader roundtrip now passes both tests on Windows and Linux. `completion-publication-race-before.log`, `completion-publication-race-after.log`: 131 passed, 2 skipped after repair.
- Full tool descriptions follow current process_manage/todo_list naming, while retaining brief fork wire schemas. Descriptions preserve current upstream capabilities and clarify handoff semantics.
- Path identity uses shared canonical comparison in execution environments, write guards, terminal cwd receipts, and TTS artifact preservation. Gate plus focused tests: 122 passed, 12 skipped, 3 Windows POSIX-shell cwd tests failed. The latter require Linux confirmation; they invoke unquoted Windows paths as POSIX cd operands. `path-identity-repairs.log`.
- Launchd approval pattern was an unanchored pair of whole-input lookaheads, causing repeated full scans at every character. Anchoring once preserves its predicate and eliminates quadratic rescans. `command-safety-scaling-repair.log`: 79 passed, 6 skipped.

## Fixtures and documentation

- Boot-task fixtures now return settled asyncio futures rather than non-awaitable mocks. Cron shutdown fixtures set the separate cron drain budget. Fleet fixtures use an isolated advancing clock for the 30-second convergence window. Dashboard state teardown uses the backing dictionary to prevent duplicate Starlette attribute deletion. `async-fixture-repairs.log`: 98 passed.
- Timestamp child fixture relocated to gateway test ownership; child only records argv. Wire-boundary assertion resolves AST call nodes. Uninstall warning accepts the current equivalent wording. Focused initial file results: timestamp 8, wire boundary 20, uninstall 2 passed.
- Canonical documentation follows current owners and symbols; removed one stale citation waiver and added none. Queue-status documented. Thirteen stale test references repaired. `documentation-owner-repairs.log`: 47 passed.
- Full 54-file Linux recheck of the original CI failures at 88123867e4: 1,679 passed, 20 failed plus five timed-out files and dashboard teardown errors. This predates the repairs above; it is not final release proof. `linux-regression-recheck-focused-881238.log`. The earlier accidentally unbounded invocation was interrupted and is not proof.

## Remaining checks

- Tombstone scan now prunes excluded dependency trees before walking. It completed instead of timing out, exposing three upstream-name collisions and two missing direct test references. Scope/coverage repair under validation; no fork feature is being removed to satisfy these names.
- Source-inspection policy decision remains pending: retain 71 unchanged upstream assertions in a separate hash-pinned register, or rewrite them. Existing closed registers have not been expanded. The one new fork positive-source assertion was replaced by resolved AST calls.
- Linux isolated environment lacked defusedxml; installed it without touching live dependencies. Recheck environment/frozen-home gates and shell fixtures.
- Native live interpreter is Python 3.12.5 / SQLite 3.45.3. Existing upstream WAL vulnerability handling must remain intact. Isolated Python 3.12.14 obtained through isolated uv 0.12.15 contains SQLite 3.53.1. Its candidate dependency installation and focused runtime validation are underway. Python 3.12.13 from the older uv manifest still had vulnerable SQLite 3.50.4 and is not a deployment candidate. No global Python registration or PATH changes were made.
- Installer still design-only. Deployment/runtime qualification must include the linked SQLite version and preserve existing runtime ownership and receipts.

Worktree inspection preserved unrelated changes: local-llama-hermes 7 untracked artifacts; Launcher primary 65 agent-memory changes; maps-voice 9 changes. Other registered worktrees clean, except this task's own active candidate edits. All test commands used scripts/run_tests.sh; raw logs are in the audit worktree's ignored qa-artifacts directory.
