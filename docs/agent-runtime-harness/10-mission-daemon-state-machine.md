# Stage 10 — Mission Daemon and AAA State Machine

## Goal

Promote Agent Runtime Harness from manual `hermes harness tick` calls into a central **Mission Daemon**: one long-running coordinator that advances the mission state machine in an optimized, bounded, observable, and recoverable way.

This is **not** a new task/class abstraction. Product language is:

- **Goal / Mission**: what the human asked for.
- **Run**: one bounded persona/action attempt.
- **Mission Daemon**: the central coordinator loop.
- **State Machine**: the deterministic owner of progression.

The current Python implementation still uses historical `Task` names in `agent_runtime.models` and stores. Treat those as internal legacy names until a safe schema/API rename is planned. Launcher and operator-facing UX should say **Mission**, **Goal**, **Human**, **Active Runs**, and **Daemon**.

---

## Current repo reality

### Already exists

- `agent_runtime/states.py`
  - Defines `TaskState`, `AgentState`, `RunState`, and `StageStatus`.
- `agent_runtime/transitions.py`
  - Defines `TRANSITION_TABLE` and `apply_transition()`.
- `agent_runtime/ticker.py`
  - Defines `TickEngine.tick_once()` and `choose_next_action()`.
  - Uses `tick_lock()` so overlapping ticks do not mutate the store concurrently.
- `docs/agent-runtime-harness/05-ticker-daemon-and-recovery.md`
  - Already sketches foreground tick → daemon, stale run recovery, max actions per tick, and idempotency rules.

### Gaps to close for AAA

- Manual ticks are not enough; Mission Control feels idle/dead between clicks.
- `choose_next_action()` only covers early PM/Dev/QA-plan states.
- Several state mutations happen directly in planning code instead of through one transition authority.
- The daemon command is designed in docs but not implemented as a resilient service loop.
- Snapshot does not expose daemon health, next wake time, backoff, or current lease owner.
- Product naming still leaks `task` in docs/CLI/runtime JSON.

---

## Mission lifecycle state machine

Use this as the canonical operator-facing state machine. Internal enum names can map to this without immediate storage migration.

```text
CAPTURED
  -> PM_SCOPING
  -> READY_FOR_DEV
  -> DEV_PLANNING
  -> QA_PLAN_REVIEW
  -> DEV_IMPLEMENTING
  -> READY_FOR_QA
  -> QA_VERIFYING
  -> PM_PROOF_REVIEW
  -> READY
  -> SHIPPED / ARCHIVED
```

Exception lanes:

```text
ANY_NON_TERMINAL -> BLOCKED_NEEDS_HUMAN
ANY_NON_TERMINAL -> FAILED_RECOVERABLE
ANY_NON_TERMINAL -> CANCELLED
QA_VERIFYING     -> DEV_FIXING
DEV_FIXING       -> READY_FOR_QA
FAILED_RECOVERABLE -> PM_SCOPING / DEV_PLANNING / BLOCKED_NEEDS_HUMAN
```

### Internal mapping for current code

```text
created                  => CAPTURED / PM_SCOPING candidate
pm_triage                => PM_SCOPING
pm_ready_for_dev         => READY_FOR_DEV

dev_audit                => DEV_PLANNING
dev_stage_planning       => DEV_PLANNING
dev_test_design          => DEV_PLANNING
qa_review_plan           => QA_PLAN_REVIEW

dev_implementing         => DEV_IMPLEMENTING
dev_ready_for_qa         => READY_FOR_QA
qa_testing               => QA_VERIFYING
qa_needs_fixes           => DEV_FIXING
qa_approved              => PM_PROOF_REVIEW
pm_proof_review          => PM_PROOF_REVIEW
pm_ready_for_integration => READY
integrating              => SHIPPED transition in progress
done                     => SHIPPED / ARCHIVED
blocked                  => BLOCKED_NEEDS_HUMAN
failed                   => FAILED_RECOVERABLE
cancelled                => CANCELLED
```

---

## Daemon responsibilities

The Mission Daemon is a single coordinator loop. It should be boring and reliable.

Every loop:

1. Acquire a runtime lease for the shared Harness root.
2. Refresh config and profile readiness with TTL caching.
3. Recover stale runs before scheduling new work.
4. Build a priority queue of eligible missions.
5. Execute at most `max_actions_per_tick` bounded actions.
6. Persist run/task/mission/event/proof/incident updates through the state machine authority.
7. Emit a redaction-safe heartbeat and daemon status snapshot.
8. Sleep using adaptive backoff.

It must never:

- spawn duplicate runs for the same mission+owner;
- bypass proof gates;
- let Dev self-approve;
- continue blindly after repeated invalid model output;
- leak tokens, MCP nonces, control paths, raw auth JSON, or env values;
- turn into Kanban, a workflow designer, or a general shell-script runner.

---

## Scheduling policy

Priority order:

1. Human-unblocked missions with a recently resolved incident.
2. Missions with active owner and no open run.
3. Oldest mission waiting for PM/Dev/QA progression.
4. Missions waiting for proof capture/QA verification.
5. Low-priority maintenance/retry work after cooldown.

Per-loop caps:

```yaml
agent_runtime:
  daemon:
    enabled: false
    interval_seconds: 10
    idle_interval_seconds: 30
    max_actions_per_tick: 1
    max_parallel_runs: 1
    heartbeat_seconds: 5
    stale_run_seconds: 600
    retry_cooldown_seconds: 120
    max_retries_per_state: 3
```

Start conservative: one active run. Add concurrency only after idempotency and per-mission leases are proven.

---

## Adaptive loop behavior

```text
If action executed:       next loop in interval_seconds
If no eligible work:      next loop in idle_interval_seconds
If provider failure:      exponential backoff for that mission/persona
If invalid model output:  retry after cooldown; block after retry budget
If stale run recovered:   do not schedule same mission in same loop
If human intervention:    pause that mission until human clears it
```

Mission Control should display:

- Daemon: Running / Paused / Offline / Error
- Last heartbeat
- Current action, if any
- Next wake
- Active runs
- Eligible next action
- Blocked reason / human intervention

---

## Transition authority

Add a single module that owns mission state transitions. Existing `apply_planning_decision()` currently mutates states directly; Stage 10 should route mutations through one authority.

Proposed file:

```text
agent_runtime/state_machine.py
```

Responsibilities:

- map decision type + current state + gates to target state;
- validate transition table;
- enforce proof gates;
- emit state transition events;
- reject illegal self-approval;
- keep state changes atomic enough for current JSON store constraints.

Minimal API shape:

```python
@dataclass(frozen=True, slots=True)
class StateMachineResult:
    from_state: TaskState
    to_state: TaskState
    events: list[Event]
    blocked_reason: str | None = None

class MissionStateMachine:
    def apply_decision(self, mission, decision, *, actor: str) -> StateMachineResult:
        ...

    def next_action(self, mission) -> HarnessAction:
        ...
```

Note: keep internal `TaskState` for compatibility in the first implementation, but call variables `mission` in new code where practical.

---

## Daemon process shape

CLI first:

```bash
hermes harness daemon --foreground --interval 10 --max-actions-per-tick 1
```

Optional service later:

```bash
hermes harness daemon install
hermes harness daemon status --json
hermes harness daemon stop
```

Do not embed into Gateway until foreground daemon has passed recovery/idempotency tests.

---

## Implementation tasks

1. Add tests documenting the current mission state mapping.
2. Add `agent_runtime/state_machine.py` with `MissionStateMachine.next_action()` equivalent to current `choose_next_action()`.
3. Route `TickEngine` through `MissionStateMachine.next_action()` without changing behavior.
4. Move decision-to-transition logic behind `MissionStateMachine.apply_decision()`.
5. Add daemon config fields under `agent_runtime.daemon`.
6. Add `agent_runtime/daemon.py` foreground loop with stop signal handling and adaptive sleep.
7. Add `hermes harness daemon --foreground --json` CLI command.
8. Add daemon heartbeat/status JSON to `snapshot.py` or `status.py`.
9. Add Launcher Mission Control fields for daemon health and next wake.
10. Only after foreground proof, add service install/start/stop wrappers.

---

## Tests

Required focused tests:

```text
tests/agent_runtime/test_state_machine.py
tests/agent_runtime/test_daemon.py
tests/agent_runtime/test_ticker.py
tests/agent_runtime/test_snapshot.py
```

Must prove:

- no duplicate active run for same mission/persona;
- daemon backs off when idle;
- daemon stops cleanly after current action;
- stale run recovery does not schedule duplicate work in same loop;
- state machine rejects illegal transitions;
- proof gates block READY/SHIPPED without required proof;
- Dev cannot approve its own implementation;
- human-blocked mission is not retried until cleared;
- snapshot redacts daemon/config/runtime fields.

---

## Acceptance criteria

- One foreground daemon can advance a mission from capture through PM scoping and Dev/QA planning without manual button clicks.
- Mission Control shows daemon health and no longer looks stuck when `Active Runs` is zero.
- Manual `Run Agent Tick` remains available as an operator override.
- Repeated daemon restarts do not create duplicate runs or missions.
- Failures become incidents with retry/backoff, not silent dead states.
- All changes preserve no-Kanban separation.
