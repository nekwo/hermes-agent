# Stage 30 — Autonomous Run Until Settled

## Goal

Replace hand-cranked one-tick-per-persona testing with a bounded autonomous mission loop. The Harness should keep routing the next eligible mission action at full speed until the mission reaches a meaningful boundary: `done`, `blocked`, `waiting_on_approval`, open incident, no eligible action, active run, wall-clock/action budget, or explicit operator cancellation.

This is the backend brainstem slice for making the daemon the normal workflow while keeping `tick --task` as a debug escape hatch.

## Product stance

- Normal operator flow: create goal → daemon/run-until-settled keeps moving → operator sees compact proof/intervention.
- Debug flow: `harness tick --task <id>` remains available for surgical inspection.
- Do not let “full speed” mean runaway model spend. Full speed means no artificial operator gaps between safe handoffs.

## Current audit evidence

- `agent_runtime/ticker.py::TickEngine.tick_once()` already performs one safe bounded action and enforces open-incident, active-run, terminal-state, proof-handoff, and cancellation-race guards.
- `agent_runtime/daemon.py::MissionDaemon.run_foreground()` currently calls one `tick_once()` per loop and sleeps even after successful actions, which recreates hand-crank/operator-gap behavior in daemon form.
- `hermes_cli/harness.py` exposes `tick --task`, `daemon foreground/start/status/stop`, and cancellation/approval controls, but lacks a compact “settle this mission now” command.
- Stage 29 smoke evidence showed ~45% wall time was manual/operator gaps between ticks. The state machine/proof gates worked; the orchestration cadence was wrong.

## Fixed architecture decisions

1. Reuse `TickEngine.tick_once()` as the single-action primitive. Do not duplicate persona execution or state transition code.
2. Add `TickEngine.run_until_settled(...)` as a bounded loop around `tick_once()`.
3. Add CLI command `hermes harness run-until-settled --task <task_id> --json` for operator/debug and smoke proof.
4. Update `MissionDaemon` to use the same settled-loop primitive per daemon wake, so one wake drains safe handoffs before sleeping.
5. Keep the first implementation single-process/synchronous with conservative budgets. Do not introduce new schedulers, queues, brokers, services, or databases.
6. Stop on meaningful boundaries and report the stop reason; do not hide failures by continuing blindly.

## Stage implementation tasks

### 30.1 Engine settled-loop primitive

Affected files:
- `agent_runtime/ticker.py`
- `tests/agent_runtime/test_ticker.py`

Actions:
- Add `RunUntilSettledResult` dataclass with `settle_id`, timestamps, `task_id`, `ticks`, `actions_taken`, `stop_reason`, `final_task_state`, `open_incidents`, and budget metadata.
- Add `TickEngine.run_until_settled(task_id=None, max_actions=10, max_seconds=None)`.
- Loop `tick_once(task_id=task_id)` until:
  - no action happened;
  - an action failed;
  - target task is terminal/cancelled/blocked;
  - any run is `waiting_on_approval`;
  - open incident exists for target/all considered tasks;
  - `max_actions` reached;
  - `max_seconds` reached.
- Preserve existing `tick_once()` guards; do not bypass open-incident or active-run checks.

Acceptance:
- A fake Neko→Dev→QA→complete flow reaches `done` in one `run_until_settled()` call.
- A blocking/failing action stops immediately and reports `action_failed`/`incident_opened`.
- `max_actions` stops before runaway loops and reports `max_actions`.

### 30.2 CLI settled command

Affected files:
- `hermes_cli/harness.py`
- tests in `tests/agent_runtime/` or `tests/hermes_cli/` if parser coverage is needed.

Actions:
- Add `harness run-until-settled` parser command.
- Arguments: `--task`, `--max-actions`, `--max-seconds`, `--json`.
- Use live `GPTPersonaRuntime` like `tick`, but output compact JSON by default under `--json`.

Acceptance:
- Command is discoverable and returns compact result fields.
- It does not dump full observe/task JSON.

### 30.3 Daemon uses settled loop per wake

Affected files:
- `agent_runtime/daemon.py`
- `tests/agent_runtime/test_daemon.py`

Actions:
- `MissionDaemon.run_foreground()` should call `engine.run_until_settled(max_actions=<bounded>)` when available.
- Status should record `actions_last_tick` as total actions from the settled result and include `settle_stop_reason`.
- If no actions happened, use idle interval. If actions happened, default wait should be zero/short so the daemon remains responsive but avoids tight idle spin.

Acceptance:
- A fake daemon engine returning multiple settled actions sleeps based on active/idle status correctly.
- Status shows stop reason and total actions.

## Verification matrix

- RED targeted tests for engine loop and daemon status.
- GREEN targeted tests:
  ```bash
  PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime/test_ticker.py tests/agent_runtime/test_daemon.py
  ```
- Broader Harness gate:
  ```bash
  PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime
  ```
- Compile/hygiene:
  ```bash
  PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m compileall agent_runtime hermes_cli/harness.py tests/agent_runtime
  git diff --check
  ```
- Smoke:
  ```bash
  PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model
  ```

## AAA risks and guardrails

- Risk: daemon runaway spends too many live tokens. Guard: bounded `max_actions`, existing per-run budget, stop on approval/incident/failure.
- Risk: CLI hides needed details. Guard: compact step summaries plus IDs; raw events remain available via existing observe/task/proof commands.
- Risk: daemon drains unrelated tasks unexpectedly. Guard: initial CLI supports targeted `--task`; daemon remains bounded and stops on incidents.
- Risk: active run race. Guard: settled loop uses `tick_once()` which already checks active runs and terminal races.

## Remaining future gaps after Stage 30

- Tune default daemon action budgets from live evidence.
- Add richer compact timing/slow-segment summaries from Stage 25 log fields.
- Add Launcher button/action wiring after backend behavior is stable.
