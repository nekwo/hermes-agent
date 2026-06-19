# Stage 20 — Runtime Liveness and Execution Truth

## Goal

Make Mission Control AAA-truthful when Harness work is queued, running, stalled, or daemon-managed. The operator must not see a stale/critical daemon warning while fresh agent work is happening, and must not be able to start duplicate persona runs for the same mission/stage.

## Deep-audit evidence

- `agent_runtime/daemon.py` writes daemon heartbeat before and after `tick_once()`, but not while a long synchronous persona tick runs and not during long idle sleeps.
- `agent_runtime/config.py` exposes `daemon_heartbeat_seconds`, but `MissionDaemon` did not use it.
- `agent_runtime/observability.py` classifies only exact `RunState.RUNNING` as active, missing `queued`, `starting`, `waiting_on_tool`, and `waiting_on_approval`.
- `agent_runtime/store.py::RunStore.open_run()` created a new run unconditionally, so duplicate active runs for the same `(task, persona, stage)` were possible.
- `agent_runtime/ticker.py` did not check for existing active runs before scheduling another persona action.
- Launcher Mission Control bridge currently needs Harness snapshot fields that distinguish active/in-flight run truth from workflow next-action text.

## Stage A — Daemon heartbeat hardening

### Implementation

- Add a heartbeat thread around `MissionDaemon.run_foreground()`.
- Refresh `daemon_status.json` every `daemon_heartbeat_seconds` while the daemon process is alive, including during long `tick_once()` calls and long idle sleeps.
- Preserve safe status fields while refreshing heartbeat.
- Write safe `state: error` status if a tick raises.

### Tests

- `test_daemon_refreshes_heartbeat_during_long_tick`
- `test_daemon_writes_error_status_when_tick_raises`

## Stage B — Active-run and stale semantics

### Implementation

- Introduce a single active-run predicate covering `queued`, `starting`, `running`, `waiting_on_tool`, and `waiting_on_approval`.
- Expose `active_runs`, `queued_runs`, `running_runs`, `waiting_runs`, and `stalled_active_runs` in observability and snapshot/status summaries.
- Classify non-offline daemon states with missing heartbeat as stale/unknown rather than healthy.
- Include the active-run fields in task summaries so Launcher can show execution truth without guessing from copy.

### Tests

- `test_active_runs_include_queued_starting_running_and_waiting_states`
- `test_non_offline_daemon_without_heartbeat_is_critical`

## Stage C — Duplicate run guard

### Implementation

- Make `RunStore.open_run()` refuse a duplicate active run for the same `(task_id, persona_id, stage_id)`.
- Make `TickEngine.tick_once()` skip tasks that already have an active run instead of invoking the persona runtime again.
- Return deterministic skip reasons for observability/debugging.

### Tests

- `test_open_run_rejects_duplicate_active_run_for_same_task_persona_stage`
- `test_tick_skips_task_with_existing_active_run`

## Stage D — Full-stage QA handoff guard

### Implementation

- Dev cannot send a mission to QA until every planned stage is dev-complete (`ready_for_qa` or already `passed`).
- A premature `request_qa_review` marks the current stage dev-complete, advances `current_stage_id` to the next incomplete stage, and keeps the mission in `dev_implementing`.
- QA approval converts all dev-complete stages to `passed`; terminal close requires every stage to be `passed`.
- PM post-QA close guards no longer rescope or close missions with unfinished stages; they route back to Dev for the remaining stage.

### Tests

- `test_dev_request_qa_review_before_all_stages_complete_stays_in_dev`
- `test_dev_request_qa_review_after_all_stages_complete_enters_qa`
- `test_qa_approval_marks_all_stages_passed_only_after_full_stage_handoff`
- `test_complete_task_is_blocked_until_all_stages_passed`

## Stage E — Launcher contract handoff

### Harness snapshot fields

- `summary.active_runs`
- `summary.queued_runs`
- `summary.running_runs`
- `summary.waiting_runs`
- `summary.stalled_runs`
- `tasks[].execution_status`
- `tasks[].active_run_ids`
- `tasks[].active_persona_ids`
- `tasks[].can_start_run`
- `tasks[].run_blocked_reason`

### Launcher follow-up

Update the Launcher bridge/UI to consume these fields so Active Missions separates mission owner from actual execution state. This is a product UI follow-up after the Harness contract exists.

## Verification matrix

- Targeted runtime tests: `tests/agent_runtime/test_daemon.py tests/agent_runtime/test_observability.py tests/agent_runtime/test_store.py tests/agent_runtime/test_ticker.py`
- Compile gate: `python -m compileall agent_runtime tests/agent_runtime`
- Diff hygiene: `git diff --check`
- Live smoke: `hermes harness daemon status --json`, `hermes harness snapshot --json`, and observe active-run fields.

## Acceptance criteria

- Daemon heartbeat freshness does not falsely go stale during long work.
- Observability no longer treats only exact `running` as active work.
- Duplicate active runs for the same task/persona/stage are prevented.
- QA is mission-complete, not first-stage-complete: Dev must finish every planned stage before QA, QA approval marks all stages passed, and terminal close requires all stages passed.
- Snapshot/status exports enough redaction-safe fields for Launcher to render queued vs running truth.
- Remaining Launcher UI consumption is explicitly tracked as a follow-up if not implemented in the same repo slice.
