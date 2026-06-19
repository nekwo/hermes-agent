# Stage 26 — Deterministic Proof Handoff and Live Run Visibility/Budgets

## Goal

Fix the two high-severity live Harness gaps observed after Stage 25:

1. A successful `request_test_run` command proof leaves the mission in `dev_implementing`, requiring another expensive Dev model tick just to hand off to QA.
2. Live AIAgent progress/tool events still persist as low-information `payload: {type: ...}` rows, so Mission Control cannot distinguish useful work from loops before a runaway is obvious.

This stage improves the existing Harness event/store path only. It must not add a new daemon, broker, database, tracing service, or analyzer.

## Current audit evidence

- `agent_runtime/ticker.py::_execute_action()` applies `request_test_run`, collects proof, appends proof IDs, then persists the task without deterministic QA handoff. The state remains whatever `planning.apply_planning_decision(... REQUEST_TEST_RUN ...)` set, normally `dev_implementing`.
- `agent_runtime/planning.py` already has robust `REQUEST_QA_REVIEW` logic that marks the current stage `ready_for_qa` and moves to `dev_ready_for_qa` when all stages are dev-complete. Stage 26 should reuse that state-machine semantics instead of creating a parallel route.
- `agent_runtime/store.py::ProofStore.attach()` now emits rich `proof.attached` events with `phase=proof`, `step=command_proof`, `status`, `exit_code`, `duration_ms`, and `next_expected=request_qa_review` for passing test proofs. This is sufficient evidence to trigger deterministic proof handoff when all command proofs pass.
- `agent_runtime/profile_runner.py::_progress_adapter()` currently discards the actual AIAgent callback arguments and emits only `type`, `args_count`, and `keys`. That is why real live events persist as `payload: {"type":"run.progress"}` despite `RunProgressSink` allowing richer fields.
- `agent_runtime/progress.py` already contains a sanitizer and allowed-key list. Stage 26 should improve adapter payloads and sanitizer tests, not bypass redaction.

## Fixed architecture decisions

- Deterministic command-proof handoff belongs in the Harness ticker after command proof collection, before run close.
- The handoff should mark the stage `ready_for_qa` and route the task to `dev_ready_for_qa` when all stages are dev-complete. It should emit an auditable `task.transition`/handoff event with `source=deterministic_proof_handoff`.
- Only passing command proof should trigger the deterministic handoff. Failed/timeout/missing proof stays in Dev/fix flow and surfaces evidence.
- AIAgent progress enrichment belongs in `profile_runner._progress_adapter()`, translating existing callback signatures into redaction-safe fields consumed by `RunProgressSink`.
- Budget/runaway visibility should start as redaction-safe warning events from existing run metadata/callback counters. Hard cancellation policy can be a later stage if it needs cross-thread interruption support.

## Stage 26A — Deterministic proof handoff

### Affected files

- `agent_runtime/ticker.py`
- `agent_runtime/planning.py` if a reusable helper is needed
- `tests/agent_runtime/test_ticker.py` or a new focused test file

### Implementation tasks

1. Add a helper that inspects proof IDs returned by `_collect_command_proof()` and confirms every command proof for the decision passed.
2. If passing proofs exist, synthesize the same safe effect as a valid Dev `request_qa_review` decision:
   - merge proof IDs;
   - set `current_stage_id` from the decision/stage;
   - mark that stage `ready_for_qa`;
   - advance to the next incomplete stage if needed;
   - set `dev_ready_for_qa` when all stages are dev-complete.
3. Emit a redaction-safe event with:
   - `phase=handoff`
   - `step=deterministic_proof_handoff`
   - `status=ready_for_qa`
   - `proof_count`
   - `stage_id`
   - `next_expected=qa_verification`
   - `summary=Passing command proof attached; routed to QA without another Dev model tick.`
4. Do not trigger the handoff if proof failed, timed out, or proof metadata is malformed.

### Tests

- `request_test_run` with passing command proof routes to `dev_ready_for_qa` and next action is QA, not Dev.
- Failed command proof remains `dev_implementing` and next action remains Dev/fix pass.
- Multi-stage mission advances only when all stages are dev-complete; otherwise it moves to the next remaining stage.

## Stage 26B — Live AIAgent progress enrichment

### Affected files

- `agent_runtime/profile_runner.py`
- `agent_runtime/progress.py`
- `tests/agent_runtime/test_profile_runner.py` or `tests/agent_runtime/test_progress.py`

### Implementation tasks

1. Replace `_progress_adapter()` with a translator for the existing callback signatures:
   - `tool.started`, `tool.completed`, `reasoning.available`, `_thinking`, plus start/complete callbacks.
2. Emit stable fields:
   - `phase`: `tool`, `inspect`, `handoff`, or `runaway_warning` where applicable;
   - `step`: `tool_started`, `tool_finished`, `reasoning_available`;
   - `tool_name`;
   - `status`: `started`, `passed`, `failed`, `running`;
   - `duration_ms` from callback duration when available;
   - `summary`: short operator-readable sentence.
3. Keep raw args/result out of persisted events unless reduced to safe derived facts.
4. Preserve redaction safety: path-like values, credentials, bearer/API key markers, and raw command strings must not leak.

### Tests

- Tool start callback emits `tool_name`, `phase=tool`, `status=started`, meaningful summary.
- Tool complete callback emits `status=passed` or `failed`, duration, and meaningful summary.
- Sensitive/path-like tool names or summaries are suppressed/sanitized.

## Stage 26C — Budget/runaway visibility warnings

### Affected files

- `agent_runtime/profile_runner.py`
- `agent_runtime/progress.py`
- optional `agent_runtime/observability.py` if snapshot projection needs a field

### Implementation tasks

1. Track per-run callback counts in the adapter closure.
2. Emit a `run.progress` warning if the same tool/event repeats excessively in one run.
3. Emit a run-close warning payload in ticker when `api_calls`, `total_tokens`, or `max_iterations` exceeds conservative thresholds for proof-only handoff runs.
4. Warning fields must be visible to Launcher through existing event payload mapping.

### Tests

- Repeated same tool callback emits one `runaway_warning` payload, not spam.
- Run close with high token/api-call count emits a warning event.
- Warning payloads pass redaction tests.

## Verification

- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m pytest -q -o addopts='' tests/agent_runtime/test_ticker.py tests/agent_runtime/test_profile_runner.py`
- Broader `tests/agent_runtime` if targeted passes.
- `PYTHONIOENCODING=utf-8 venv/Scripts/python.exe -m compileall agent_runtime hermes_cli/harness.py tests/agent_runtime`
- `git diff --check`
- Independent deep-audit review before commit.

## Acceptance criteria

- Passing command proof no longer requires a second Dev model tick to route to QA.
- Live tool/progress events answer what happened, which tool, status, duration, and why it matters.
- Redaction tests cover all newly surfaced fields.
- No new services or storage systems are added.
- Existing raw events remain available for Mission Control.
