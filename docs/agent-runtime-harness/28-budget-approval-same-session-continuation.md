# Stage 28 — Budget Approval Same-Session Continuation

## Goal

When a live Harness run reaches configured Stage 27 limits, the runtime should pause/block for explicit operator approval and then continue the same agent session when approved and safe. The same continuity rule applies to QA steering, Neko supervisor steering, and PM steering: follow-up runs for the same task/persona/stage should reuse the previous session handle when one exists, rather than silently starting a fresh session.

This closes the gap Tony identified after Stage 27: budget enforcement currently fails/blocks the run with a `run_budget_exceeded` incident, but there is not yet a first-class approve/resume flow that continues the same session.

## Current audit evidence

- `agent_runtime/states.py` already defines `RunState.WAITING_ON_APPROVAL`.
- `agent_runtime/store.py` treats `WAITING_ON_APPROVAL` as an active run state.
- `agent_runtime/profile_runner.py` passes `session_id` into `AIAgent`, so same-session continuation is technically possible when a previous `session_id` was persisted.
- `agent_runtime/ticker.py` currently handles `RunBudgetExceeded` by closing the run as `FAILED`, opening a `run_budget_exceeded` incident, and not applying a decision.
- `hermes_cli/harness.py` currently has task/run cancel controls, but no approval/resume command for budget-limited runs.

## Required behavior

1. A run that reaches configured live limits must not silently discard session context.
2. The run should enter an operator approval/intervention state instead of being treated as an ordinary provider failure.
3. The approval prompt/snapshot should expose only redaction-safe fields:
   - run_id
   - task_id
   - persona_id
   - stage_id
   - session_id handle if present
   - budget kind/observed/limit
   - recommended action
4. On explicit approval, the Harness should continue/resume the same agent session when safe:
   - pass original `session_id` into the next `AgentRunRequest`
   - preserve task/stage context
   - record approval as redaction-safe event/proof metadata
5. If same-session continuation is not safe, the Harness must block with an explicit reason instead of silently starting fresh.

## Proposed implementation shape

### Stage 28.1 — Persist budget approval request

Files:

- `agent_runtime/ticker.py`
- `agent_runtime/store.py`
- `agent_runtime/models.py`
- `agent_runtime/incidents.py`
- tests in `tests/agent_runtime/test_ticker.py` / `test_store.py`

Change Stage 27 failure path:

- Set run state to `WAITING_ON_APPROVAL` for budget limit hits where same-session continuation is possible.
- Persist `run.error` / `run.progress` with sanitized budget fields.
- Open or attach a non-secret approval/intervention item.
- Do not apply persona decision.

Acceptance:

- Budget hit leaves task state unchanged.
- Run is visible as `waiting_on_approval`.
- Snapshot/status shows a concrete approval needed intervention.

### Stage 28.2 — CLI approval command

Files:

- `hermes_cli/harness.py`
- `agent_runtime/store.py`
- tests in CLI/runtime test files

Add a first-class command such as:

```bash
hermes harness run approve <run_id> --json
```

Behavior:

- validates run exists and is `waiting_on_approval`
- validates approval target is a budget approval, not arbitrary failed run
- records `run.approved` / `run.approval_recorded` event
- never persists raw approval text

### Stage 28.3 — Same-session continuation

Files:

- `agent_runtime/ticker.py`
- `agent_runtime/persona_runtime.py`
- `agent_runtime/profile_runner.py`
- tests in `tests/agent_runtime/test_ticker.py` / `test_profile_runner.py`

Behavior:

- After approval, create a continuation run that reuses the previous run's `session_id`.
- Pass a concise safe continuation instruction, e.g. “Operator approved continuing after budget limit; continue from prior session and produce the required AgentDecision.”
- Keep budgets bounded; do not disable budgets forever.
- If there is no session_id, or profile/model/provider changed incompatibly, block with `same_session_not_safe`.

Acceptance:

- Approved budget-limited run resumes with same `session_id`.
- No fresh-session fallback happens silently.
- Same-session-not-safe path opens explicit intervention.

- Add helper to find the latest safe session id for the same task/persona/stage, with same-persona fallback when the stage changed.
- New PM, QA, Dev, and Neko steering runs should pass that session id into `AgentRunRequest`.
- This is continuity, not approval bypass: deterministic state/proof gates still decide what action may run.

Acceptance:

- Follow-up PM steering uses previous PM session id.
- Follow-up QA steering uses previous QA session id.
- Follow-up Neko steering uses previous Neko session id.
- Sensitive/path-like session ids are ignored rather than reused.

### Stage 28.4 — Mission Control visibility

Files:

- Launcher Mission Control bridge/UI if needed after Harness surface exists.

Behavior:

- Show “Waiting for approval to continue session” with run/session handle.
- Approval button should call the Harness approval command only after the CLI contract exists.

## Verification

```bash
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m compileall agent_runtime hermes_cli/harness.py tests/agent_runtime
git diff --check
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model
```

Independent review must verify:

- no raw approval text, prompt, provider payload, or path is persisted;
- approval cannot resume arbitrary failed/cancelled runs;
- same-session continuation really reuses `session_id`;
- unsafe same-session cases block explicitly;
- no extra daemon/service/broker is introduced.

## Status

Implementation pending. This document records Tony's requirement and the current gap so the fresh Harness goal can implement it deliberately rather than treating Stage 27's failure incident as sufficient.
