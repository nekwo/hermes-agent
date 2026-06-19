# Stage 5 — Ticker, Daemon, and Recovery

## Goal

Implement a reliable ticking loop that advances tasks, runs personas, executes harness-owned actions, records events, and recovers from stale/crashed runs **without creating duplicate work**.

Stage 5 turns the Stage 1-4 library into an operator-usable brainstem. It still starts as foreground CLI ticks, not a gateway service.

## Deep audit findings from current repo

### Existing code to reuse

- `agent_runtime/locks.py`
  - `tick_lock()` and per-task locks already exist; the ticker must use them to prevent overlapping ticks.
- `agent_runtime/store.py`
  - `TaskStore`, `RunStore`, `ProofStore`, and `IncidentStore` provide JSON persistence.
- `agent_runtime/persona_runtime.py`
  - `GPTPersonaRuntime.run_tick()` invokes the persona and returns a structured decision.
- `agent_runtime/transitions.py`
  - State transitions are table-validated, but Stage 5 must call Stage 3/4 gates before applying transitions.
- `cron/scheduler.py`
  - Has long-running loop lessons, locking patterns, script timeouts, and error surfacing, but should not be imported wholesale.
- `hermes_cli/kanban_db.py`
  - Has useful claim TTL and heartbeat concepts. Reuse the mental model, not the schema or board statuses.
- `gateway/` and Telegram integration
  - Useful later for notifications, but embedding the ticker into gateway too early makes debugging harder.

### Current gaps

- There is no `AgentState` persistence per persona beyond `AgentRun.state`.
- `RunStore.find_stale()` exists but no ticker calls it.
- Bad persona output currently raises from runtime; Stage 5 must convert that into `Incident` and run failure without mutating product task state.
- Stage 3/4 decision appliers and proof gates must be called by a single orchestrator so state mutations are serialized.

## Package additions

```text
agent_runtime/
  ticker.py                # TickEngine, tick_once(), choose_next_action()
  actions.py               # HarnessAction enum/dataclasses for pending work
  recovery.py              # stale-run detection, incident creation, retry policy
  status.py                # status summary for CLI/UI polling
  runtime_config.py        # tick interval, stale TTL, per-tick caps
```

Stage 5 should not add gateway embedding. CLI integration comes in Stage 6.

## Tick model

A tick is a bounded state-machine step:

```python
@dataclass(slots=True)
class TickResult:
    tick_id: str
    started_at: datetime
    finished_at: datetime
    tasks_seen: int
    actions_taken: list[HarnessActionResult]
    incidents_opened: list[str]
    skipped: list[str]
```

Every tick:

1. Acquire `tick_lock()`.
2. Load open tasks.
3. Mark stale runs using `RunStore.find_stale()`.
4. For each task, choose at most one next action.
5. Execute the action or persona tick.
6. Validate decision payloads and gates.
7. Persist task/run/proof/incident changes atomically.
8. Append safe events.
9. Release lock.

## Eligible actions by task state

```text
CREATED                  -> run PM persona for PROPOSE_ACCEPTANCE
PM_TRIAGE                -> run PM persona if not fleshed
PM_READY_FOR_DEV         -> transition/request DEV_AUDIT
DEV_AUDIT                -> run Dev persona for REQUEST_FILE_READS or PROPOSE_STAGE_PLAN
DEV_STAGE_PLANNING       -> run Dev persona for PROPOSE_STAGE_PLAN / CORRECT_STAGE
DEV_TEST_DESIGN          -> run Dev persona for test design / REQUEST_QA_REVIEW
QA_REVIEW_PLAN           -> run QA persona for APPROVE / CORRECT_STAGE / BLOCK
DEV_IMPLEMENTING         -> Stage 6+ implementation actions; Stage 5 can no-op/status only
DEV_READY_FOR_QA         -> Stage 4 QA proof actions
QA_TESTING               -> Stage 4 proof actions / QA verdict
QA_NEEDS_FIXES           -> route to Dev planning/implementation depending finding
QA_APPROVED              -> PM proof review action
PM_PROOF_REVIEW          -> PM approval gate
PM_READY_FOR_INTEGRATION -> integration action waits for human/CLI command
BLOCKED/FAILED           -> no automatic branch; show incident/status
DONE/CANCELLED           -> terminal
```

Stage 5's first vertical slice should stop at QA plan approval / `DEV_IMPLEMENTING`; implementation patch execution can be a later Stage 6/extension if not ready.

## Run lifecycle

1. `open_run(persona_id, task_id, stage_id)` before model call.
2. Set `run.state = RUNNING`.
3. Persona runtime receives `task_id=run.id` for tool isolation.
4. Heartbeat before/after model call and before long harness actions.
5. On valid decision, close run as `COMPLETED` with `final_decision`.
6. On provider/model/parser failure, close run as `FAILED` and open incident.
7. On heartbeat timeout, mark as `STALE` and open incident.

Do not create a new task for failed/stale runs.

## Recovery policy

### Invalid model output

- Stage 2 runtime already performs one repair attempt.
- If still invalid, ticker opens `Incident(kind="model_invalid_output")`, closes run failed, leaves task state untouched.
- Next tick may retry same task with same persona unless retry budget exceeded.

### Provider/model failure

- Open `Incident(kind="provider_failure")` with redaction-safe error class/message.
- Close run failed.
- Do not transition task to `FAILED`.

### Harness action failure

Examples: file read denied, command timeout, screenshot capture failed.

- Open `Incident(kind="harness_action_failure")`.
- Attach log proof only if safe/scanned.
- Leave task state untouched unless action explicitly requested `BLOCK`.

### Stale run

- If `now - last_heartbeat_at > heartbeat_ttl`, mark run `STALE`.
- Open `Incident(kind="stale_run")`.
- Do not spawn a duplicate run in the same tick.
- Next tick may retry after cooldown.

## Idempotency rules

- `tick_once()` with no eligible actions must append no task mutation events.
- Retrying a completed decision must not duplicate stages/proofs/corrections.
- Incidents should dedupe by `(task_id, run_id, kind)` while open.
- A tick crash after task write but before event append is forbidden for state transitions; mutation helpers must write event before releasing lock or roll back/abort.
- If rollback cannot be guaranteed with JSON files, write event first with `pending=true`, then mark complete after task write. Document and test the chosen approach.

## Daemon shape

Foreground first:

```bash
hermes harness tick --once
hermes harness status
```

Then daemon:

```bash
hermes harness daemon --interval 30 --max-actions-per-tick 1
```

Daemon rules:

- One tick lock per shared runtime root.
- SIGINT/SIGTERM closes cleanly after current action.
- No gateway embedding until CLI ticks and daemon are reliable.
- No recursive cron scheduling.

## Status model

`status.py` should produce a pure JSON-safe summary:

```json
{
  "open_tasks": 3,
  "running_runs": 1,
  "stale_runs": 0,
  "blocked_tasks": 1,
  "open_incidents": 2,
  "next_actions": [
    {"task_id": "task_abc", "action": "run_dev_planning", "reason": "needs stage plan"}
  ]
}
```

This becomes the source for CLI status and Stage 7 snapshots.

## Implementation tasks

1. Add failing tests for stale-run recovery opening incident and not mutating task state.
2. Add failing tests for invalid persona output opening incident and not mutating task state.
3. Add failing tests for no eligible actions being idempotent.
4. Add failing tests for one action per task/tick cap.
5. Add `runtime_config.py` defaults for heartbeat TTL and max actions.
6. Add `actions.py` with action types/results.
7. Add `recovery.py` stale/failed-run helpers.
8. Add `ticker.py::TickEngine.tick_once()` using fake persona runtime in tests.
9. Add `status.py` summary builder.
10. Run all `tests/agent_runtime/`.

## Tests

Required test files:

```text
tests/agent_runtime/test_ticker.py
tests/agent_runtime/test_recovery.py
tests/agent_runtime/test_status.py
tests/agent_runtime/test_actions.py
```

Test matrix:

- `CREATED` task triggers PM persona tick.
- Valid PM decision mutates task through PM fleshing applier.
- Invalid model output opens incident and leaves task byte-identical.
- Stale run becomes `RunState.STALE` and opens incident.
- Duplicate open stale incident is not created on repeated tick.
- Tick lock prevents overlapping ticks.
- `max_actions_per_tick=1` is honored.
- No eligible action appends no mutation events.
- Recovery never creates a new task.

## Acceptance criteria

- `TickEngine.tick_once()` can advance one synthetic task using fake persona runtime.
- Process/model failures become incidents, not product task failures.
- Repeated ticks are idempotent when nothing is eligible.
- No duplicate tasks are created during retries.
- Ticker does not require gateway, Telegram, Launcher, Unreal, or network in tests.

## Risks / interventions

- **Cost runaway:** cap actions/model calls per tick.
- **Duplicate work:** never branch automatically; recover in place.
- **Gateway debugging pain:** do not embed in gateway before CLI/daemon passes.
- **JSON atomicity limits:** if exact state+event atomicity becomes too complex, document the pending-event approach or move to SQLite only when Stage 8 thresholds are hit.
- **Overbroad ticker:** keep implementation/proof capture actions small and typed; do not let the ticker become a shell-script runner.
