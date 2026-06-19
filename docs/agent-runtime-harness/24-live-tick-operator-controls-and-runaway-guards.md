# Stage 24 — Live Tick Operator Controls and Runaway Guards

## Goal

Fix the high-impact Harness gaps exposed by the live smoke run: live ticks can spend too much work on the wrong mission, operators lack safe cancel/target controls, active runs do not expose provider/model metadata early enough, and post-terminal progress events can make cancelled runs look alive.

## Product stance

Mission Control remains a simple goal → PM → Dev → QA → proof → ready/intervention brainstem. These fixes prioritize operator control and truthful state over adding new workflow ceremony.

## Audit evidence

- Live smoke `run_bc5fce1526de` consumed excessive model work (`total_tokens` persisted only at close) while newer smoke goals were skipped.
- Operators had to use direct Python store calls to cancel a stuck run and cleanup smoke tasks.
- During active execution, provider/model/session metadata surfaced as `null` until close, making Launcher Agent Detail less truthful.
- After operator cancellation, late progress events could still update/append against terminal run records.

## Stage A — Targeted tick control

Affected files:
- `agent_runtime/ticker.py`
- `hermes_cli/harness.py`
- `tests/agent_runtime/test_ticker.py`

Implementation:
- Add optional `task_id` targeting to `TickEngine.tick_once()`.
- Fail closed when `task_id` does not exist.
- When targeted, process only that task so smoke/debug runs do not starve behind older eligible missions.
- Wire `hermes harness tick --task <task_id> --json`.

Acceptance:
- Targeted tick executes the named task even when an older open task exists.
- Non-targeted tick behavior remains unchanged.
- Missing task target reports a safe error instead of silently doing nothing.

## Stage B — First-class cancellation controls

Affected files:
- `agent_runtime/store.py`
- `hermes_cli/harness.py`
- `tests/agent_runtime/test_store.py`

Implementation:
- Add `RunStore.cancel(run_id, reason=...)` wrapping terminal close with `state=cancelled` and fixed operator-safe error metadata.
- Add `TaskStore.cancel(task_id, reason=..., actor=...)` setting task state to `cancelled` via the normal update/event path.
- Wire `hermes harness run cancel <run_id> --reason ... --json` and `hermes harness task cancel <task_id> --reason ... --json`.
- Do not persist or echo raw operator-provided cancellation reasons; record only a generic safe cancellation summary.
- Preserve terminal task/run records: normal cancel/close/update paths must not rewrite `done`, `completed`, `failed`, `stale`, or `cancelled` history.

Acceptance:
- Operators can cancel runs/tasks without editing store JSON or using ad-hoc Python.
- Cancellation emits existing safe terminal events.
- Raw cancellation reason text is never persisted or echoed.
- Stale in-memory run/task objects cannot overwrite terminal cancellation state.

## Stage C — Early active-run metadata

Affected files:
- `agent_runtime/ticker.py`
- `tests/agent_runtime/test_ticker.py`

Implementation:
- Immediately after opening a run, persist redaction-safe LLM metadata from the selected persona/config: provider, model, api_mode, retry attempt/max.
- Do this before calling the persona runtime so snapshots/Launcher show who is running even while the model is still working.

Acceptance:
- A fake runtime can inspect the persisted run during execution and see provider/model metadata.
- Final metadata from the actual runner may still overwrite/extend token/session data on close.

## Stage D — Post-terminal progress suppression

Affected files:
- `agent_runtime/progress.py`
- `tests/agent_runtime/test_run_progress.py`

Implementation:
- `RunProgressSink.emit()` should no-op when the run is already in a terminal state.
- Do not update heartbeat/progress or append progress events for cancelled/completed/failed/stale runs.

Acceptance:
- A cancelled run remains unchanged when a late progress callback fires.
- No post-terminal `run.progress` event is appended.

## Verification matrix

- `bash scripts/run_tests.sh tests/agent_runtime/test_ticker.py tests/agent_runtime/test_store.py tests/agent_runtime/test_run_progress.py`
- `python -m compileall agent_runtime hermes_cli/harness.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_store.py tests/agent_runtime/test_run_progress.py`
- `git diff --check`
- Independent pre-commit review before commit.

## Deferred / future stages

- Windows process-tree hard kill for background `terminal()` processes is Hermes tool/runtime infrastructure, not only Harness store logic. Keep as a follow-up if store-level cancel controls are insufficient.
- Token/runaway hard budget based on live model counters may require AIAgent runtime budget hooks; this stage adds operator-targeting and metadata groundwork first.
