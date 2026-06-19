# Stage 60 - Foreground Goal Runtime Cleanup and Scheduler

## Problem

Live goal creation is not yet equivalent to starting one uninterrupted competent worker lane.

The current `harness task create --start-daemon` path creates a task, runs new-goal hygiene, and starts the Mission Daemon. The daemon is global. It does not receive the new task id, and its foreground loop calls `TickEngine.run_until_settled(max_actions=10)` without a task target. That falls through to `tick_once(task_id=None)`, which uses `TaskStore.list_open()`.

This means every non-terminal historical task competes with the freshly created goal. Old open tasks can consume the first daemon action, open a role session, or leave an active run that makes the fresh goal appear frozen even though the daemon is technically alive.

The fix is not hard deletion. Archive/history must remain evidence-preserving. The fix is to split runtime execution state from task history:

```text
archive/history: preserved terminal and non-terminal evidence
foreground runtime lane: the one goal the daemon is actively driving now
background/open queue: preserved tasks that are not allowed to steal the foreground lane
```

## Code Evidence

Current implementation anchors:

- `hermes_cli/harness.py::_cmd_task_create`
  - calls `prepare_new_goal_runtime(...)`
  - creates a `Task`
  - calls `_start_daemon_for_new_goal(args, config)`
  - does not pass `task.id` into daemon startup
- `hermes_cli/harness.py::_start_daemon_for_new_goal`
  - calls `start_daemon(interval_seconds=..., idle_interval_seconds=...)`
  - no foreground task id, no run instance id, no queue mode
- `agent_runtime/daemon.py::start_daemon`
  - spawns `python -m hermes_cli.main harness daemon foreground`
  - command has no `--task` or `--foreground-goal`
- `agent_runtime/daemon.py::MissionDaemon.run_foreground`
  - calls `engine.run_until_settled(max_actions=10)`
  - no target task id is supplied
- `agent_runtime/ticker.py::TickEngine.run_until_settled`
  - accepts `task_id`
  - works correctly when called with `task_id`
  - global daemon path does not use it
- `agent_runtime/ticker.py::TickEngine.tick_once`
  - with no `task_id`, uses `self.task_store.list_open()`
- `agent_runtime/store.py::TaskStore.list_all`
  - sorts by id via `_list_models(..., key=lambda item: item.id)`
  - this is not a runnable-priority queue
- `agent_runtime/goal_hygiene.py::prepare_new_goal_runtime`
  - clears dead daemon status
  - marks stale runs
  - closes worker sessions
  - cancels orphan active runs only when task is missing or terminal
  - optionally cancels Stage 47 temp tasks
  - does not park old open tasks or isolate a new foreground lane
- `agent_runtime/dirty_state.py::build_dirty_state`
  - treats all open tasks as runtime dirtiness
  - no distinction between archived history, preserved background work, and active foreground execution

## Root Causes

1. New goal hygiene is cleanup, not execution isolation.

It handles dead daemon status, stale runs, orphan active runs, and worker-session cleanup. It does not define which task owns the next daemon actions.

2. The daemon has global semantics.

Starting the daemon means "drive every open task", not "drive the task that was just created." This is why a new goal can require manual ticks even though `--start-daemon` returned success.

3. `list_open()` is being used as a scheduler.

Open task visibility is a history/query concept. It is not a runnable foreground queue. Sorting by task id is not an enterprise scheduling policy.

4. Active run ownership is not tied to a run instance.

A stale or unrelated active run can hold the execution boundary for the runtime. The Harness can mark stale runs, but it cannot explain "this active run belongs to the previous foreground goal and was parked/preempted/waited on."

5. Dirty-state reporting conflates preserved evidence with unsafe runnable state.

A runtime can be "dirty" because old tasks exist, but those old tasks should not necessarily block or compete with the new foreground goal.

6. The live NSFW investigation exposed a real scheduler gap.

`task create --start-daemon` did start daemon work, but the daemon selected older open tasks before the new task. Manual recovery was required: stop daemon, cancel an old active run, and run targeted ticks for the fresh task. That is the exact behavior Stage 60 must remove.

7. The 3-decision / high-token Dev run needs root-cause instrumentation, not guesswork.

The backend investigation run may have legitimately needed the extra context. The Harness currently lacks enough structured attribution to prove whether the extra decisions were necessary progress or avoidable loop cost.

## Product Decision

New goal creation creates a foreground goal instance.

That instance owns daemon execution until it reaches a terminal boundary, blocked boundary, explicit pause, or operator handoff. Old open tasks are preserved, but they are parked in the background queue unless explicitly resumed.

This stage is implementation-ready only if it treats foreground execution as a runtime concern, not as a new task lifecycle state. Do not add `FOREGROUND`, `BACKGROUND`, or `PARKED` to `TaskState`. Those are scheduler/runtime lanes. `TaskState` continues to describe product-goal progress such as `created`, `dev_implementing`, `qa_testing`, `done`, or `cancelled`.

Do not:

- delete tasks, runs, proofs, packets, context logs, or archive batches;
- auto-archive non-terminal work;
- hide old tasks from snapshot/history;
- silently cancel a fresh unrelated active run without an event and policy reason;
- rely on a wrapper script to babysit daemon startup.

Do:

- make the Harness command itself create and drive a foreground runtime instance;
- make daemon status show the foreground task id and lane;
- make stale/foreign runtime state visible and recoverable;
- keep archive/history separate from active scheduler state;
- record root-cause metrics for extra agent decisions before optimizing them away.

## Target Flow

```text
hermes harness task create "goal" --start-daemon
  -> prepare foreground runtime lane, excluding no new task yet
  -> park existing open tasks as background/preserved
  -> cancel stale/orphan foreground blockers only with events
  -> create task
  -> create foreground goal instance for task
  -> finalize foreground hygiene for the new task id
  -> start daemon in targeted foreground mode
  -> daemon runs run_until_settled(task_id=<new_task>)
  -> old open tasks do not consume ticks
```

Global daemon mode may still exist for maintenance, but product goal creation must use targeted foreground mode.

## Stage 60A - Runtime Queue Model

Add a first-class runtime queue/lane model. Prefer a small separate store over adding scheduler-only fields directly to `Task`, because the user-facing task record is evidence/history while the queue is mutable execution state.

Suggested model:

```python
@dataclass(slots=True)
class GoalRuntimeInstance:
    id: str
    task_id: str
    lane: str                 # foreground | background
    state: str                # active | waiting | parked | terminal
    created_at: datetime
    updated_at: datetime
    started_by: str
    run_generation: int = 1
    active_run_ids: list[str] = field(default_factory=list)
    parked_reason: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    schema_version: int = 1
```

Files:

- add `agent_runtime/runtime_instances.py`
- add path helpers in `agent_runtime/paths.py`
- include summaries in `agent_runtime/status.py` and `agent_runtime/snapshot.py`
- add migration only if existing JSON files require backfill; otherwise store can be additive

Required behavior:

- a task may exist without an active runtime instance;
- only one foreground instance is active by default;
- old open tasks can have parked/background instances;
- terminal task archival must include runtime instance manifests if they exist;
- deleting runtime instance state is never required to preserve task/proof history.

Store details:

- directory: `paths.runtime_instances_dir() -> store_root() / "runtime_instances"`
- file: `runtime_instance_path(instance_id)`
- secondary lookup may scan by `task_id`; this is acceptable for the current JSON store scale
- ids: `goalrt_<12 hex>`
- event payloads must include only ids, states, lane, and safe reason tokens

Do not store:

- raw prompts;
- model responses;
- provider request bodies;
- product file contents;
- secrets or environment variable values.

## Stage 60B - New Goal Foreground Hygiene

Extend `prepare_new_goal_runtime(...)` with an explicit foreground mode, but do not require a `foreground_task_id` before task creation. The safe shape is either two-phase or one post-create activation call.

Preferred two-phase API:

```python
prepare_new_goal_runtime(
    foreground_mode: bool = False,
    park_open_tasks: bool = False,
    preempt_background_runs: bool = False,
    exclude_task_ids: set[str] | None = None,
)

activate_foreground_runtime(
    task_id: str,
    started_by: str,
    preempt_background_runs: bool = False,
)
```

Responsibilities:

- clear dead daemon status;
- mark stale runs;
- close stale/foreign worker sessions;
- park old non-terminal tasks in the runtime queue, not archive;
- preserve task JSON, run JSON, proofs, packets, context logs, and events;
- cancel only:
  - active runs whose task is missing;
  - active runs whose task is terminal;
  - stale active runs older than the heartbeat TTL;
  - background-lane runs when explicit foreground preemption policy allows it.
- never cancel the just-created foreground task because it is in `exclude_task_ids`.

Events to emit:

- `foreground_runtime.prepared`
- `foreground_runtime.parked_task`
- `foreground_runtime.cancelled_stale_run`
- `foreground_runtime.waiting_on_fresh_run`
- `foreground_runtime.preempted_background_run`

Dirty-state output must split:

```json
{
  "runtime": {
    "foreground_task_id": "task_...",
    "foreground_clean": true,
    "background_open_tasks": 3,
    "foreground_active_runs": 0,
    "background_active_runs": 0,
    "blocking_active_run_ids": []
  },
  "history": {
    "open_preserved_task_count": 3
  }
}
```

## Stage 60C - Targeted Daemon Mode

Add target support to the daemon:

```powershell
python -m hermes_cli.main harness daemon foreground --task task_1234
python -m hermes_cli.main harness daemon start --task task_1234
```

Code changes:

- `agent_runtime/daemon.py::MissionDaemon.__init__(target_task_id: str | None = None)`
- `MissionDaemon.run_foreground(...)` calls:

```python
engine.run_until_settled(task_id=self.target_task_id, max_actions=10)
```

- `start_daemon(task_id: str | None = None, ...)` includes `--task <id>` in the spawned command.
- `hermes_cli/harness.py` parser adds `--task` to:
  - `harness daemon start`
  - `harness daemon foreground`
  - `harness daemon run-once`
- `_cmd_daemon` passes `args.task` into both `start_daemon(...)` and `MissionDaemon(...)`.
- preserve compatibility for existing top-level `harness daemon --foreground` by accepting `--task` there too or by documenting it as global maintenance mode only.
- daemon status includes:

```json
{
  "target_task_id": "task_...",
  "queue_mode": "foreground",
  "foreground_runtime_instance_id": "goalrt_..."
}
```

Existing global daemon behavior should remain available for maintenance/testing, but `task create --start-daemon` must use targeted daemon mode.

Duplicate daemon policy:

- if a daemon is already alive with the same `target_task_id`, return `started=False` and `state` unchanged;
- if a daemon is alive with a different `target_task_id`, do not silently reuse it for the new goal;
- stop/restart is allowed only when config explicitly permits foreground replacement, and the status/event must name both old and new task ids;
- otherwise return a structured blocker: `daemon_target_conflict`.

## Stage 60D - Scheduler API

Stop using `TaskStore.list_open()` as the runnable scheduler source.

Add a runtime-aware selection API. Prefer placing this outside `TaskStore` if the runtime instance store grows beyond simple lookup logic.

```python
class RuntimeScheduler:
    def runnable_tasks(self, *, mode: str = "foreground", target_task_id: str | None = None) -> list[Task]:
        ...
```

Behavior:

- targeted ticks use exactly the requested task;
- foreground daemon mode uses the active foreground runtime instance;
- global daemon mode sorts runnable tasks by:
  1. active foreground instance;
  2. unblocked active runtime instance;
  3. explicit resume order;
  4. created_at fallback;
  5. id fallback only as a final stable tie-breaker.

`list_open()` remains for UI/history visibility.

Minimal implementation path:

- targeted daemon mode can initially bypass global scheduling and call `run_until_settled(task_id=target_task_id)`;
- global scheduler refactor can then be narrow and tested without blocking targeted daemon correctness;
- do not rewrite the state machine while adding runtime lanes.

## Stage 60E - Active Run Boundary Policy

New foreground goals must not silently freeze behind unrelated active runs.

Policy:

- stale unrelated active run: existing recovery marks it `STALE`, records an incident, and clears it from active-run selection; if it remains active after recovery, cancel it with a foreground-runtime event;
- orphan unrelated active run: cancel, record event, continue;
- fresh foreground run for another task: report `foreground_runtime.waiting_on_fresh_run` and do not create duplicate run;
- fresh background run: park or preempt depending on config;
- same-task active run: treat as a valid active boundary and monitor heartbeat.

Config defaults:

```python
foreground_preempts_background_runs: bool = True
foreground_waits_on_fresh_foreground_run: bool = True
foreground_active_run_ttl_seconds: int = heartbeat_ttl_seconds
```

This gives automatic cleanup for broken old state while avoiding unsafe cancellation of genuinely active work.

The implementation must reuse existing run-state constants from `agent_runtime/store.py::ACTIVE_RUN_STATES`. Do not create a second definition of active run states.

## Stage 60F - Root-Cause Metrics for Multi-Decision Runs

Before reducing budgets or forcing one-shot behavior, record why a role took multiple decisions.

Add per-run counters:

- decision count;
- API call count;
- model call latency;
- tool call count;
- read/search count;
- context request count;
- unique files read;
- duplicate file reads;
- packet repair count;
- proof requests;
- changed file count;
- new proof count;
- new evidence count since previous decision;
- reason for continuing same session;
- reason for ending same session.

Classification:

```text
necessary_progress:
  new relevant file/context/proof/packet appeared between decisions

possible_loop:
  repeated same action with no new evidence

blocked_environment:
  proof or tool failed due to external dependency

contract_repair:
  malformed/invalid packet repaired after HUD feedback
```

Acceptance for the NSFW-style investigation:

- if Backend Dev uses multiple decisions, the final run summary must show whether each extra decision added new inspected files, new context, new proof, or packet repair;
- no optimization should claim waste without those metrics.

Implementation hooks:

- update `agent_runtime/persona_runtime.py` where LLM timing metadata is already applied;
- update `agent_runtime/ticker.py` where decisions, tool requests, proof requests, and packet repairs are recorded;
- update Mission Control snapshot/status only with redaction-safe summaries;
- preserve raw detailed artifacts in existing run/proof/packet storage rather than adding raw content to daemon status.

## Stage 60G - CLI and Mission Control Contract

CLI:

- `harness task create --start-daemon --json` returns:

```json
{
  "id": "task_...",
  "foreground_runtime": {
    "instance_id": "goalrt_...",
    "target_task_id": "task_...",
    "queue_mode": "foreground",
    "parked_open_task_ids": []
  },
  "daemon_start": {
    "attempted": true,
    "target_task_id": "task_...",
    "started": true
  }
}
```

Mission Control:

- dirty state panel shows foreground clean/dirty separately from preserved history;
- daemon badge shows `Driving task_...` instead of only `running`;
- old open tasks remain visible as background/preserved, not active;
- if daemon is waiting on a fresh active run, show that exact blocker.

Archive integration:

- update `ArchiveStore._archive_one(...)` in `agent_runtime/store.py`;
- add `_archive_runtime_instance_evidence(task.id, archive_dir)` beside existing helper-style archive calls for worker sessions, persona assignments, repo bundles, packet artifacts, self tests, and role envelope state;
- include `runtime_instance_ids` and `runtime_instances_archived` in the archived task item;
- include `runtime_instance_count` in the redaction-safe `task.archived` event payload;
- archive only runtime instance files for the task being archived.

## Stage 60H - Tests

Add focused tests before live burn.

Required tests:

1. `test_task_create_start_daemon_passes_target_task`
   - monkeypatch `start_daemon`
   - create task with `--start-daemon`
   - assert daemon start receives the new task id

2. `test_daemon_foreground_uses_target_task_id`
   - fake engine records `run_until_settled(task_id=...)`
   - assert daemon passes the configured task id

3. `test_new_goal_hygiene_parks_open_tasks_without_archive`
   - create old non-terminal tasks
   - run foreground hygiene
   - assert old task JSON still exists
   - assert runtime queue marks them background/parked
   - assert no archive batch was created

4. `test_new_goal_hygiene_cancels_stale_foreign_active_run`
   - create old active run beyond heartbeat TTL
   - run foreground hygiene
   - assert run is cancelled/stale with event evidence

5. `test_new_goal_hygiene_waits_on_fresh_foreground_run`
   - create fresh active run for another foreground task
   - run foreground hygiene
   - assert no silent cancellation
   - assert dirty output includes `blocking_active_run_ids`

6. `test_scheduler_does_not_select_background_open_task_for_foreground_goal`
   - create old open task and fresh foreground task
   - run global/tick path configured for foreground
   - assert first action targets fresh task

7. `test_archive_preserves_runtime_instance_manifest`
   - archive terminal task
   - assert archive manifest includes runtime instance files

8. `test_dirty_state_splits_foreground_from_history`
   - old parked open task should not make foreground lane dirty
   - active foreground blocker should make foreground lane dirty

9. `test_multi_decision_run_records_root_cause_metrics`
   - simulate multiple decisions
   - assert metrics distinguish new evidence from repeated no-op loop

Regression tests:

- existing `tests/agent_runtime/test_daemon.py`
- existing `tests/agent_runtime/test_dirty_state.py`
- existing `tests/agent_runtime/test_goal_runner.py`
- existing `tests/agent_runtime/test_ticker.py`
- existing `tests/agent_runtime/test_store.py`

Suggested new test files:

- `tests/agent_runtime/test_runtime_instances.py`
- `tests/agent_runtime/test_foreground_goal_hygiene.py`
- extend `tests/agent_runtime/test_daemon.py`
- extend `tests/agent_runtime/test_dirty_state.py`
- extend `tests/agent_runtime/test_store.py`

## Stage 60I - Live Certification

Certification setup:

1. Create or preserve at least one old non-terminal open task.
2. Create a new goal with `harness task create --start-daemon --json`.
3. Do not manually tick.
4. Monitor daemon once per minute.

Pass conditions:

- daemon status shows `target_task_id` equal to the fresh task;
- first role action is Neko for the fresh task;
- no old task opens a run before the fresh task reaches a boundary;
- stale old active run state is cancelled or reported with exact reason;
- foreground dirty state becomes clean when the fresh task is complete/archived;
- old open tasks are still visible as preserved background/history;
- archive of fresh terminal task preserves proofs, runs, packets, context logs, and runtime instance manifest.

Suggested live goal:

```text
Investigate why NSFW content can slip through filters and produce a backend-only hardening plan.
No product edits. Inspect backend moderation/media/feed paths. Compare cheap local model options.
Return staged questions, risks, and implementation stages.
```

This is intentionally large enough to validate Neko scoping, Backend Dev context gathering, QA proof, daemon target behavior, and multi-decision root-cause metrics without requiring product edits.

## Risk Audit

High risk:

- blindly cancelling fresh active runs from another goal. Mitigation: only cancel stale/orphan/background runs by default; emit wait state for fresh foreground conflicts.

Medium risk:

- adding runtime instance state without archive preservation. Mitigation: archive manifest tests and snapshot/status tests.

Medium risk:

- Mission Control interpreting parked open tasks as failed/archived tasks. Mitigation: add explicit `foreground/background/preserved` labels.

Low risk:

- daemon target mode. `TickEngine.run_until_settled(task_id=...)` already exists and is used by the in-process goal runner.

Low risk:

- task-create daemon response shape extension. It is additive JSON.

Compatibility risks:

- `task create --start-daemon` currently calls hygiene before the task exists. The patch must not pass a nonexistent task id into cleanup logic.
- existing tests instantiate fake engines with `run_until_settled(self, *, max_actions=None)`. Targeted daemon tests must update fakes or keep `task_id` optional in daemon calls when no target is configured.
- old snapshots and Mission Control clients may not know runtime instance fields. Keep new fields additive and preserve existing `daemon.state`, `tasks_seen_last_tick`, and dirty-state summary keys.
- runtime instances are mutable active state. Archive should move them only when the task is terminal and archive-ready, never during parking.

## Implementation Order

1. Add runtime instance model/store/path helpers and unit tests.
2. Add archive preservation for runtime instance files and unit tests.
3. Add status/snapshot summaries for foreground/background runtime state, keeping fields additive.
4. Extend new-goal hygiene to park old open tasks and classify active run blockers.
5. Add targeted daemon support and CLI `--task` wiring.
6. Change `task create --start-daemon` to:
   - prepare foreground lane;
   - create the task;
   - activate a foreground runtime instance for that task;
   - start daemon with that task id.
7. Add scheduler API for global maintenance mode; keep targeted daemon correctness independent from this refactor.
8. Add multi-decision root-cause metrics.
9. Add/extend tests listed in Stage 60H.
10. Run standard Harness tests.
11. Run live daemon certification without manual ticks.

## Implementation Readiness Checklist

Before coding, confirm these choices:

- Runtime lane state is separate from `TaskState`.
- `task create --start-daemon` uses targeted daemon mode by default.
- `task create --no-start-daemon` still creates the task and foreground runtime instance but leaves daemon offline/manual.
- Old open tasks are parked in runtime state only; task files remain untouched unless terminal archive is explicitly requested.
- Old active stale/orphan runs are cancelled with event evidence.
- Fresh unrelated foreground active runs produce `daemon_target_conflict` or `foreground_runtime.waiting_on_fresh_run`, not a silent hang.
- Mission Control reads foreground/background labels from snapshot/status, not by guessing from task state.
- Archive moves runtime instance evidence only for terminal archived tasks.
- Live certification starts from a deliberately dirty runtime with at least one old open task, proving the new task is not stolen by history.

## Done Criteria

Stage 60 is done only when:

- creating a goal with daemon auto-start needs no manual tick in the normal path;
- daemon status names the task it is driving;
- old open tasks cannot steal the fresh goal's first action;
- old open tasks are preserved outside the foreground lane;
- stale/orphan active runs are cleaned with event evidence;
- fresh unrelated active runs are surfaced as an explicit wait/preempt decision, not a freeze;
- dirty state distinguishes foreground execution dirtiness from preserved history;
- archive remains evidence-preserving;
- the Harness can explain whether multi-decision Dev runs were justified by new evidence or were loops;
- a live token goal passes using daemon mode from creation through QA and archive.

## Implementation Status

Implemented in this repo:

- `GoalRuntimeInstance` model and `GoalRuntimeInstanceStore`.
- Runtime instance path helpers under `runtime_instances/`.
- Foreground runtime activation and old foreground parking.
- New-goal foreground hygiene that parks old open tasks without archiving/deleting task evidence.
- Targeted daemon mode via `harness daemon start --task`, `foreground --task`, and `run-once --task`.
- `task create --start-daemon` now activates a foreground runtime instance and starts daemon with the new task id.
- Daemon target conflict detection when an existing daemon is alive for another target or global mode.
- Status and snapshot foreground-runtime summaries.
- Dirty-state foreground/background split while preserving existing dirty summary fields.
- Archive preservation for runtime instance manifests.
- Event contracts for foreground runtime events.
- Redaction-safe per-run `decision_metrics` classification for necessary progress, possible loops, blockers, and contract repair.
- Burn-in product repo modification detection now compares before/after repo dirty signatures so pre-existing product repo dirtiness does not create a false burn-in failure.
- Task creation records a repo-clean baseline from the post-hygiene dirty-state snapshot so live no-product-edit goals can proceed when pre-existing dirty work is unchanged.
- Preflight repo-clean checks accept unchanged dirty baselines and block only when affected repo dirtiness changes after goal creation or no baseline exists.
- The daemon foreground loop now exits if `daemon_status.json` is owned by another live daemon PID, preventing stale daemon processes from reclaiming status after restart.
- Targeted daemon mode now exits when its target reaches `task_terminal`, preventing later archive cleanup from turning a completed target into a daemon `NotFound` error.

Verified:

```powershell
pytest tests/agent_runtime/test_daemon.py tests/agent_runtime/test_runtime_instances.py tests/agent_runtime/test_task_create_foreground_daemon.py
pytest tests/agent_runtime/test_dirty_state.py tests/agent_runtime/test_store.py tests/agent_runtime/test_goal_runner.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_snapshot.py tests/agent_runtime/test_status.py
pytest tests/agent_runtime
```

Latest full result: `788 passed, 1 warning`.

Additional live-burn certification on `2026-06-09`:

- Created `task_0735211a` with `harness task create --start-daemon --json`.
- The command created foreground runtime instance `goalrt_b65c67fe1389`, parked old open task `task_1500c055` as background, and started targeted daemon mode.
- First attempt correctly targeted the fresh task but blocked on repo-clean preflight because pre-existing `hermes-agent` dirty work was treated as unsafe. Root cause fixed with task-created repo-clean baselines and unchanged-baseline preflight acceptance.
- After daemon restart to load the patched code, the task reached `done` through targeted daemon mode without manual ticks.
- QA approved proof `proof_qa_9f4fec7f`.
- Command proofs:
  - `test_task_0735211a_harness_runtime_status_snapshot_run_6c565ca59325_0_5fbe6c98`: `python -m hermes_cli.main harness status --json`
  - `test_task_0735211a_harness_runtime_status_snapshot_run_6c565ca59325_1_86dd86fb`: `python -m hermes_cli.main harness snapshot --json`
  - `test_task_0735211a_harness_runtime_status_snapshot_run_6c565ca59325_2_0b09979c`: `python -m hermes_cli.main harness contracts verify-examples --json`
- Archived the terminal task with `harness task archive task_0735211a --json`; archive batch `20260609T235453482239Z_archive_ready` preserved runtime instance, runs, proofs, incidents, packet artifacts, and repo bundle evidence.
- The live burn exposed a daemon duplicate-process/status-writer gap. That is now covered by `test_daemon_foreground_exits_when_status_is_owned_by_other_live_pid`.

Clean follow-up live-burn certification on `2026-06-10`:

- Created `task_f8fb17be` with `harness task create --start-daemon --json`.
- Daemon pid `1744` targeted `task_f8fb17be` and foreground runtime instance `goalrt_cf69a6d1bd5c`.
- Preserved old open task `task_1500c055` remained parked in the background lane and did not steal the fresh task's first action.
- Task reached `done` through targeted daemon mode. No manual tick was used.
- Neko, Dev, and QA all produced worker sessions:
  - `run_30c637d82f1b`: Neko mission lead
  - `run_3ecdddab49d7`: Dev proof-only verification
  - `run_7fd43383fe28`: QA release verdict
- QA approved proof `proof_qa_7c8b2cde`.
- Command proofs:
  - `test_task_f8fb17be_run_a_proof-only_stage_60_daemon_live-burn_verif_run_3ecdddab49d7_0_20dd6a0f`
  - `test_task_f8fb17be_run_a_proof-only_stage_60_daemon_live-burn_verif_run_3ecdddab49d7_1_9949f25b`
  - `test_task_f8fb17be_run_a_proof-only_stage_60_daemon_live-burn_verif_run_3ecdddab49d7_2_7c60ea73`
- Archived the task with `harness task archive task_f8fb17be --json`; archive batch `20260610T001511723929Z_archive_ready` preserved runtime instance, worker sessions, worker context, role state, packets, repo bundle, proof batch, proofs, and runs.
- Monitoring note: `task create --json` returns the task summary with top-level `id`; monitor scripts should read `.id`, not `.task.id`.
- Post-archive daemon check exposed that targeted daemon mode kept polling after `task_terminal` and errored once the archived task no longer existed. Fixed by exiting targeted daemon mode at the terminal boundary and covered by `test_targeted_daemon_exits_when_target_reaches_terminal_boundary`.

## Claude Gap Closure - 2026-06-10

Claude reported five remaining runtime/operator gaps. Current closure status:

- `task_1500c055` blocked/parked with consumed recovery signal:
  - Added `harness task unblock <task_id> --reason ... [--rescope] [--foreground] [--state ...]`.
  - The command closes stale open incidents for the task, clears consumed recovery markers, removes `neko_block_recovery_attempted`, optionally clears old scope/stages, and can reactivate the task as the foreground runtime lane without starting token work.
  - Verified on live runtime: `task_1500c055` moved from `blocked` to `created`, `open_incident_ids=[]`, `risk_flags=[]`, `harness_self_heal={}`, foreground runtime active, daemon offline.
- `task show` archived traceback:
  - `harness task show <archived_task_id> --json` now searches `deleted_archive/*/manifest.json` and returns archived metadata plus archived task JSON instead of raising `NotFound`.
  - Verified with archived `task_37bc473c`, archive batch `20260610T013055542255Z_archive_ready`.
- `status --json` undercount / hidden parked tasks / dead daemon PID:
  - `status --json` now exposes `open_task_ids`, `background_open_tasks`, `background_task_ids`, `unparked_open_tasks`, and `unparked_open_task_ids` at top level.
  - `read_daemon_status()` reports non-offline dead-PID status files as `{"state":"offline","last_pid":...,"cleared_reason":"dead_pid"}`.
- Skill doc drift:
  - Updated `C:\Users\beast\.codex\skills\mission-control-harness\SKILL.md` to state that plain `task create` may leave the daemon offline in current config and `--start-daemon` is required for targeted daemon mode.
- Concurrent daemon token-spend window:
  - The foreground daemon now checks for another live owner before status registration and again immediately after writing `state=running`, before constructing/running `TickEngine`.
  - Covered by `test_daemon_foreground_rechecks_owner_after_status_registration`.

Focused verification:

```powershell
pytest tests/agent_runtime/test_daemon.py tests/agent_runtime/test_status.py tests/agent_runtime/test_task_operator_cli.py
```

Result: `26 passed`.
