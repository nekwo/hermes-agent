# Stage 45 — Stage 44 implementation dedup audit

Date: 2026-06-05

Purpose: audit the current Harness + Launcher implementation against
`44-merged-mission-control-aaa-plan.md` before coding, so Stage 44 is implemented by
closing true gaps rather than double-building existing behavior.

## Baseline status

- Harness repo dirty before implementation:
  - `M docs/agent-runtime-harness/00-index.md`
  - `?? .claude/`
  - `?? docs/agent-runtime-harness/44-merged-mission-control-aaa-plan.md`
- Launcher repo clean.
- `python -m hermes_cli.main harness status --json`: exit 0.
  - runtime root: `X:\Eternia\.hermes\agent-runtime`
  - profile: `alice`
  - open tasks/runs/incidents: 0/0/0
  - daemon: `offline`
- `python -m hermes_cli.main harness snapshot --json`: exit 0.
- `flutter test test/features/mission_control`: exit 0, 52 tests passed.
- `python -m pytest -q tests/agent_runtime`: exit 1 because repo addopts reference a
  pytest-timeout option not installed in this shell.
- `python -m pytest -o addopts='' -q tests/agent_runtime`: exit 1, 354 passed / 2
  failed. Both failures are pre-existing Windows command-proof shell issues:
  - `tests/agent_runtime/test_proof_runner.py::test_command_proof_runner_attaches_redacted_test_run_proof`
  - `tests/agent_runtime/test_proof_runner.py::test_command_proof_runner_uses_posix_shell_for_quoted_paths`

## Codex implementation overlay, 2026-06-05

These items are now implemented in the working tree after this dedup audit. Future
agents should treat them as code to inspect/extend, not as blank design work:

- **Stage 0 partial close:** `harness verify --mode live-tony|ci|temp-root --json`
  exists and runs status, snapshot, archive help, config show, migrate check, plus a
  focused test tier unless `--skip-tests` is passed. Live Tony mode asserts
  `X:\Eternia\.hermes\agent-runtime`.
- **Stage 0 proof-runner baseline fixed:** `CommandProofRunner` now avoids WSL
  `bash.exe` on Windows and prefers Git Bash, closing the two baseline proof-runner
  failures recorded above.
- **Stage 0.7 partial close:** effective config, schema validation, migration status,
  `harness config show --json`, and `harness migrate --check --json` exist. Status and
  snapshot now include the redaction-safe runtime config/migration summary.
- **Stage 1 partial close:** archive refusal-only calls no longer create empty
  `deleted_archive` batches; JSON returns `skipped_tasks` with operator-readable
  messages; successful archives write `manifest.prepare.json`, then moved evidence, then
  `manifest.json`, then emit `task.archived` with `manifest_path`. The current lock is
  still a file `archive.lock`; Stage 2.3 must migrate it to the typed claim registry
  instead of adding a second archive lock.
- **Launcher Stage 1 close:** Mission Control no longer falls back to
  `readyGoals.first` when the selected mission is not ready; the archive button disables
  instead of archiving the wrong mission.
- **Stage 2.4 partial close:** `harness daemon run-once --json` exists and reuses the
  existing foreground daemon loop with `max_loops=1`. Singleton claims/backoff remain
  Stage 2.4 work.
- **Stage 2 wait-semantics partial close:** post-scoping unresolved context requests no
  longer return `NOOP`; they route to Neko for bounded context repair/reroute. Initial
  scope still routes to Neko scoping, matching Tony's "wait only at the beginning from
  Neko" constraint.
- **Stage 5 backend partial close:** `harness task show <task_id> --events N --since
  ISO --json` returns task-scoped events from the raw append-only log. Launcher compact
  row rendering, pagination UI, and ordering fixes remain Stage 5 work.

Verification after overlay:

- `python -m pytest -o addopts='' -q tests/agent_runtime/test_store.py
  tests/agent_runtime/test_events.py tests/agent_runtime/test_daemon.py
  tests/agent_runtime/test_migrations.py tests/hermes_cli/test_harness_cli.py`: exit 0,
  45 passed.
- `python -m pytest -o addopts='' -q tests/agent_runtime/test_context_requests.py
  tests/agent_runtime/test_state_machine.py tests/agent_runtime/test_ticker.py
  tests/agent_runtime/test_status.py tests/agent_runtime/test_snapshot.py`: exit 0,
  83 passed.
- `flutter test test/features/mission_control`: exit 0, 52 passed.
- `python -m hermes_cli.main harness status --json`: exit 0, runtime root
  `X:\Eternia\.hermes\agent-runtime`, profile `alice`, open tasks/runs/incidents 0/0/0.
- `python -m hermes_cli.main harness snapshot --json`: exit 0.
- `python -m hermes_cli.main harness task archive --help`: exit 0.
- `python -m hermes_cli.main harness task archive-ready --help`: exit 0.
- `python -m hermes_cli.main harness config show --json`: exit 0, validation ok.
- `python -m hermes_cli.main harness migrate --check --json`: exit 0, no pending
  migration.
- `python -m hermes_cli.main harness verify --mode live-tony --skip-tests --json`:
  exit 0, runtime root assertion passed.
- `python -m hermes_cli.main harness verify --mode live-tony --json`: exit 0; built-in
  focused Harness tier exit 0, 45 passed.
- `powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Set-Location
  'X:\Unreal Engine\Engine\Launcher\EterniaLauncher'; flutter build windows --debug
  --target lib/main_marionette.dart"`: exit 0; produced
  `build\windows\x64\runner\Debug\eternia_launcher.exe`.
- `tool_search` for Launcher QA / Stage C / marionette semantic MCP controls: 0 tools
  found in this Codex thread. Visual semantic screenshot proof remains blocked by tool
  exposure, not by build/test readiness.

## Stage-by-stage dedup map

| Stage | Current status | Existing implementation / tests | Do not double-implement | Confirmed remaining work |
|---|---|---|---|---|
| 0 — Baseline proof harness / secret scan | Partial, overlay implemented | Runtime health is exposed by `agent_runtime/status.py`; CLI status/snapshot work; proof runner redacts command artifacts; `agent_runtime/redaction.py` exists; `harness verify` now exists; Windows proof-runner shell failures are fixed. | Do not create a second status/health system or second redaction scanner before checking `proof_runner.py`, `redaction.py`, `provider_health.py`. | Add artifact scan metadata (`secrets_found`, `redacted`, storage pressure), artifact persistence paths, Launcher focused tests, and marionette build into the proof packet. |
| 0.5 — Regression matrix | Partial | Many focused tests already exist under `tests/agent_runtime/`; Launcher Mission Control has 52 focused tests. | Do not replace existing focused tests; extend them or add matrix wrapper/markers. | Add named matrix/tier command and coverage ownership for G1-G29; record xfails for not-yet-implemented gaps. |
| 0.6 — Shared test infrastructure | Partial | `tests/agent_runtime/conftest.py` isolates runtime roots; many fake runtimes exist ad hoc inside tests. | Do not scatter new fakes in per-stage tests. | Add shared fake provider/meter, fake clock, crash harness, concurrency harness, fixture brains/repos, and CI tier markers in `tests/support/` or an existing support path. |
| 0.7 — Runtime config/schemas/migrations | Partial, overlay implemented | `agent_runtime/runtime_config.py` and `agent_runtime/config.py` load core and Stage 44 config; models carry `schema_version=1`; `harness config show`; `harness migrate --check`; status/snapshot config readiness. | Do not introduce another config loader. Extend `AgentRuntimeConfig` / `RuntimeConfig`. | Add real forward migrations when persisted Stage 2+ fields land; add legacy fixture snapshots and storage-pressure checks. |
| 1 — Archive correctness/UI feedback | Partial, overlay implemented | `TaskStore.archive/archive_ready` exist; archive moves tasks/runs/proofs; snapshot reads deleted archives; Launcher bridge maps `archiveReadyGoal` to `harness task archive <goal_id> --json`; Launcher tests cover archive CLI mapping/refusal; archive now returns `skipped_tasks`, avoids pure-miss batches, writes prepare/final manifests, emits event after final manifest, and the Launcher no longer falls back to archiving `readyGoals.first`. | Do not rebuild archive bridge. Keep the existing CLI intent path. | Backfill/synthesize `task.archived` for legacy batches; migrate `archive.lock` to Stage 2.3 typed claim; add interrupted-archive recovery scanner. |
| 2 — Deterministic orchestration | Partial, overlay implemented | `MissionStateMachine.next_action`, `TickEngine.run_until_settled`, deterministic proof handoff in `_apply_deterministic_proof_handoff`, budget incident route to Neko, specialist persona selection, and many tests already exist. Post-scoping unresolved context requests now route to Neko instead of `NOOP`; initial scope still routes to Neko scoping. | Do not rewrite orchestration wholesale. Patch exact decision points. | Deterministic specialist release still uses `neko_qa_coordination_released` flag; no bounded `initial_scope_wait` field/deadline; status/tick parity is not centrally enforced. |
| 2.1 — Anti-freeze leases/watchdogs | Partial | Heartbeats, stale-run detection, daemon stale observability, run-until-settled boundaries, WAITING_ON_APPROVAL protection, and tests exist. | Do not add a parallel stale-run detector. Extend `recovery.py`, `RunStore`, and status/snapshot. | No persisted progress lease fields (`lease_expires_at`, progress fingerprint); no task-level repeated-next-action stall detector; WAITING_ON_TOOL lacks timeout/recovery action; no MCP process watchdog state. |
| 2.2 — Neko steering/self-heal | Mostly missing | Budget-exceeded same-session continuation via Neko is implemented in `planning.py`/`ticker.py`; prompt guidance mentions Neko budget recovery. | Do not duplicate budget continuation approval. Build self-heal around existing incident/recovery path. | Add resume brief builder, failure classifier, self-heal event schema, idempotency keys, approved/protected repair actions, no-blind-retry enforcement, `cannot_self_heal` status/snapshot/UI fields. |
| 2.3 — Swarm claims/concurrency | Mostly missing | `agent_runtime/locks.py` provides file locks for tick/task/run/events; archive uses task locks indirectly. | Do not keep archive-specific locking separate after adding claim primitive. | Add typed claim registry, CAS `revision`, worktree claims, brain-index claim, conflict routing, status/snapshot claim visibility. |
| 2.4 — Autonomous daemon driver | Partial, overlay implemented | `agent_runtime/daemon.py` has foreground loop, `start_daemon`, `stop_daemon`, `read_daemon_status`; CLI has `harness daemon start/status/stop/foreground/run-once`; tests cover heartbeat, start duplicate by pid, status/snapshot, parser exposure. | Do not rebuild daemon CLI. Harden existing daemon. | Singleton is pid/status-file based, not claim-based; no persisted provider backoff state; transient/logic classifier only exists at ticker retry level; no Mission Control proof that daemon is expected/running. |
| 2.5 — State-machine simplification | Not implemented | Current `TaskState` still includes `DEV_AUDIT`, `PM_PROOF_REVIEW`, `PM_READY_FOR_INTEGRATION`, `INTEGRATING`, `FAILED`; `transitions.py` still models the linear tail; tests depend on some dead states. | Do not delete states until tests are migrated deliberately. | Remove task-level `FAILED` and dead states or add a compatibility migration; make transition table/state machine the single authority; eliminate direct `COMPLETE_TASK` bypass or explicitly encode it. |
| 3 — Budget/context/brain/ledger | Partial | Live run budgets are enforced in `profile_runner.py`; budget incidents and same-session continuation exist; `context_builder.py` injects bounded task/proof/incident/repo context; brain search guidance exists as prompt text. | Do not add a second budget meter. Extend `profile_runner.py`/`ticker.py` budget data. | Soft ceilings/mission deadline not implemented; context ledger absent; brain index absent; untrusted-context delimiter absent; prompt-injection tests absent; storage/ledger quota absent. |
| 4 — Agent HUD + analytics | Mostly missing | Run metadata captures provider/model/tokens; Launcher bridge displays token labels for logs. | Do not infer HUD from Launcher-only token labels. | Add `agent_runtime/analytics.py`, stable prelude/HUD assembly, three-ceiling HUD line, context ledger watermark, Neko self-heal breadcrumb, prompt cache ordering tests. |
| 5 — Mission Control log UX | Partial, overlay implemented | Launcher parses agent logs/events, filters event categories, expands proof/log details, suppresses unsafe paths, displays daemon mode; bridge maps archived logs; Harness now exposes `harness task show --events/--since` for task-scoped event feeds. | Do not replace the Mission Control page wholesale. Patch data model/widgets. | Status/snapshot still include global `tail(20)` for dashboard use; visible UI still reverses some playback rows; no event pagination metadata, daemon/storage indicators, self-heal rows, terminal notification banner. |
| 6 — Visual/MCP proof path | Partial/blocker | `lib/main_marionette.dart`, `tool/stagec_qa_mcp_server`, and Launcher Stage C docs exist; marionette build was previously proven after killing a locked process. | Do not claim visual proof from shell-only build. | Expose/verify `launcher_qa` MCP tools in the current agent thread; attach screenshot proof to Harness proof record; add clean-thread registration proof. |
| 7 — E2E one-shot proof | Not implemented as Stage 44 capstone | Existing live task history shows completed missions and archived tasks, but not after new Stage 44 behavior. | Do not reuse old Stage 42 proof as final proof. | Run one new small Mission Control-started goal through daemon to terminal/archive with proof packet and screenshot/MCP or exact blocker. |
| 8 — Release hardening | Partial | Existing docs/runbooks and readiness hints exist; Launcher docs have Mission Control stages; Harness docs index modified. | Do not duplicate docs; update current docs/index. | Add readiness gate command, migration/storage/daemon/MCP checklist, fixture snapshots, perf budget, final implementation-readiness audit. |

## Cross-stage implementation warnings

- `agent_runtime/proof_runner.py` is already the Stage 0 proof-capture spine. Fix it
  before adding new proof packet behavior because two current tests fail there.
- `agent_runtime/daemon.py` already owns daemon lifecycle. Stage 2.4 should harden it
  with claims/backoff/run-once/status fields, not create a new daemon module.
- `agent_runtime/profile_runner.py` is already the external budget enforcement point.
  Stage 3 should extend it with soft/mission ceilings and analytics, not create a second
  token meter.
- `agent_runtime/context_builder.py` is already the persona context seam. Stage 3/4
  should add stable prelude/HUD/ledger there or through a helper it calls.
- `agent_runtime/locks.py` is small but already imported by stores/events/ticker.
  Stage 2.3 must migrate existing locks carefully to avoid deadlocks or double-locking.
- Launcher Mission Control is test-covered and already maps archive intents correctly.
  UI work should be additive: daemon/storage/self-heal indicators, truthful pagination,
  and compact event rows.

## Recommended implementation order after dedup

1. Fix current baseline failures in `CommandProofRunner`.
2. Implement Stage 0.7 config/schema/migration and Stage 0 verify packet around the
   existing status/snapshot/proof infrastructure.
3. Implement shared test support for fake clock/provider/crash/concurrency before
   touching leases/claims/daemon.
4. Patch archive transactionality/JSON reasons in the existing `ArchiveStore` and
   existing Launcher bridge tests.
5. Harden existing state machine/ticker/daemon in-place: no-freeze leases, self-heal
   resume brief, typed claims, singleton daemon claim/backoff.
6. Add context ledger/brain index/HUD/analytics through existing context and runtime
   seams.
7. Add Mission Control observability fields and visual/MCP proof.
8. Run capstone proof and readiness audit before commit.
