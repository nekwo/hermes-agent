# Stage 27 — Live Run Budget Enforcement and Provider Interrupt

## Goal

Turn the Stage 26 budget visibility warnings into bounded execution controls for live Harness persona runs.

Stage 27 is intentionally scoped to the existing Harness/AIAgent execution path. It must not add a scheduler, broker, daemon sidecar, database, tracing service, or new log service.

## Product stance

A live Dev/QA/Neko run that exceeds known budget limits is not “still working”; it is an operator-visible runtime failure that must stop consuming the Harness lane, produce redaction-safe evidence, and require an explicit retry/intervention.

## Current audit evidence

- `agent_runtime/ticker.py` opens persona runs synchronously and only emits Stage 26 warnings after the persona call returns.
- `agent_runtime/profile_runner.py` owns the unified Hermes-native `AIAgent` construction and currently passes `max_iterations` but no wall-clock/API/token enforcement fields.
- `run_agent.py::AIAgent.interrupt()` exists and is the safest currently available in-process cancellation hook; it propagates interrupt state to active tool workers and child agents.
- `agent_runtime/incidents.py` has no dedicated `run_budget_exceeded` incident kind, so budget failures would otherwise collapse into generic `provider_failure`.
- `agent_runtime/store.py::RunStore.close_run()` already protects terminal run writes and emits `run.closed` events.
- `agent_runtime/progress.py::RunProgressSink` already suppresses post-terminal progress, so late background callback events after a budget terminal state are dropped.

## Fixed architecture decisions

1. Budget enforcement lives in the existing persona runner seam: `ProfileAgentRunner` / `AgentRunRequest`.
2. Wall-clock enforcement uses a same-thread `Timer` around `AIAgent.run_conversation()` and calls `agent.interrupt("live run budget exceeded")` when the wall budget is exceeded. The agent must return cooperatively after interrupt; no detached worker may outlive `persona_profile_context`.
3. API-call and total-token ceilings are enforced immediately after the provider result is normalized. These are post-result enforcement because the current `AIAgent` result metrics are only authoritative at completion. Defaults match the Stage 26 warning thresholds (`api_calls=20`, `total_tokens=750000`) and set wall-clock at five minutes.
4. The ticker classifies `RunBudgetExceeded` as `run_budget_exceeded`, closes the run as failed, opens an incident, and does not apply any persona decision.
5. Cancellation/terminal guards from Stages 24 and 26 remain authoritative: terminal run/task state is re-read before decision/proof application and post-terminal progress is suppressed.
6. Operator-facing payloads must be redaction-safe: numeric limits/observed values only; no raw prompt, provider response, command args, paths, secrets, or operator-entered cancellation text.

## Rejected alternatives

- New daemon watcher: rejected; violates observability-first/no-new-moving-parts constraint.
- Killing the whole Hermes gateway process: rejected; too broad and unsafe for Telegram/profile operations.
- Trusting Stage 26 warning events only: rejected; severe over-budget runs must terminally stop the lane.
- Persisting raw provider/tool payloads to diagnose budget failures: rejected; redaction boundary violation.

## Implementation stages

### Stage 27.1 — Budget config and request contract

Files:

- `agent_runtime/runtime_config.py`
- `agent_runtime/config.py`
- `agent_runtime/profile_runner.py`
- `agent_runtime/persona_runtime.py`
- `agent_runtime/ticker.py`

Tasks:

- Add live budget fields to `RuntimeConfig` / YAML load:
  - `live_run_max_wall_seconds`
  - `live_run_max_api_calls`
  - `live_run_max_total_tokens`
  - `live_run_iteration_budget`
- Add matching optional fields to `AgentRunRequest`.
- Open runs with `iteration_budget=config.live_run_iteration_budget`.
- Pass wall/API/token budgets from `GPTPersonaRuntime` into `ProfileAgentRunner`.

Acceptance:

- Existing config without fields remains schema-compatible.
- Existing tests that construct `AgentRunRequest` continue to work.
- Run records show the configured iteration budget.

### Stage 27.2 — Runner wall-clock/provider interrupt

Files:

- `agent_runtime/profile_runner.py`
- `tests/agent_runtime/test_profile_runner.py`

Tasks:

- Add `RunBudgetExceeded(ProfileRunnerError)`.
- Arm a same-thread wall-clock `Timer` before `AIAgent.run_conversation()`.
- On timeout, call `agent.interrupt("live run budget exceeded")` when available, emit a safe progress event if callback exists, and raise `RunBudgetExceeded` once the cooperative interrupt returns or raises.
- Do not detach a worker thread that can outlive `persona_profile_context` or mutate global profile/environment state after the Harness run is terminal.

Acceptance:

- A fake slow agent returns promptly with `RunBudgetExceeded`.
- The fake agent’s `interrupt()` hook is called.
- The error string includes only safe numeric budget facts.

### Stage 27.3 — API/token ceiling enforcement

Files:

- `agent_runtime/profile_runner.py`
- `tests/agent_runtime/test_profile_runner.py`

Tasks:

- After `_normalize_result`, compare `api_calls` and `total_tokens` to configured ceilings.
- Raise `RunBudgetExceeded` if either ceiling is exceeded.
- Include only numeric observed/limit fields in the message.

Acceptance:

- Over-API and over-token fake results raise `RunBudgetExceeded`.
- Under-budget results pass.

### Stage 27.4 — Ticker incident and no-decision semantics

Files:

- `agent_runtime/incidents.py`
- `agent_runtime/ticker.py`
- `tests/agent_runtime/test_ticker.py`

Tasks:

- Add `run_budget_exceeded` incident kind.
- Ticker handles `RunBudgetExceeded` as non-retryable.
- Run closes `failed` with safe error payload.
- Task state remains unchanged.
- Incident opens with kind `run_budget_exceeded`.

Acceptance:

- A runtime that raises `RunBudgetExceeded` leaves task state unchanged, run failed, and opens exactly one budget incident.
- The action result is not OK and carries the run/incident IDs.

### Stage 27.5 — Verification and deep audit

Commands:

```bash
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime/test_profile_runner.py tests/agent_runtime/test_ticker.py tests/agent_runtime/test_config.py
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m compileall agent_runtime hermes_cli/harness.py tests/agent_runtime
PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model
git diff --check
```

Independent review must check:

- no raw secrets/paths/provider payloads are persisted;
- timeout does not apply stale decisions;
- terminal-state guards still win;
- no new scheduler/service was added;
- fake timeout threads cannot block the Harness tick past the configured wall budget.

## Remaining known limitation

Python cannot safely kill an arbitrary in-process provider call. Stage 27 uses the existing `AIAgent.interrupt()` hook from a timer and terminals the Harness run when the cooperative interrupt returns/raises. If a provider call ignores interrupt, the current thread can still wait until the provider/request timeout; Stage 27 deliberately avoids a detached worker thread because `persona_profile_context` mutates process-global profile/environment state.

If cooperative interruption proves insufficient in live operation, the next stage should isolate persona execution in a killable subprocess with redaction-safe IPC. That is intentionally deferred because it is a larger moving-part addition.
