# Stage 13 — Run Progress Streaming and Worker Handoff Contracts

> **For Hermes:** This is an implementation-ready, no-guesswork handoff. Use `subagent-driven-development` only after this document is accepted, then implement with strict TDD. Do not change product behavior from prose alone; every code change below starts with a failing test.

## Goal

Make every Harness persona run visibly alive while it works, and make every PM/Dev/QA/Neko handoff machine-verifiable so the next persona never has to infer proof from prose.

## Architecture decision

Add one reusable redaction-safe progress pipeline to the Agent Runtime Harness:

```text
AIAgent / fake runtime callbacks
  -> agent_runtime.progress.RunProgressSink
  -> AgentRun.progress + last_heartbeat_at + append-only EventLog
  -> status / observe / snapshot / watch
```

Add one reusable proof-safe handoff pipeline:

```text
AgentDecision.payload proof_artifacts / reviewed_proof_ids / acceptance_results
  -> role-specific payload validation
  -> proof_handoff converts safe Dev artifacts into Proof records
  -> planning/proof_gates apply QA verdicts only from reviewed proof IDs
  -> Launcher/Mission Control sees proof gaps through snapshot JSON
```

Fixed decisions:

- Keep persisted model `schema_version: 1`; add optional fields only.
- Use `AgentRun.progress: dict[str, Any] | None`, not a new schema version or separate DB.
- Store append-only progress in the existing NDJSON event log through new allowed event types.
- Reuse existing `AIAgent` callbacks (`tool_progress_callback`, `step_callback`, `status_callback`, `stream_delta_callback`) first. Add only tiny callback shims if an exact model-call start/end gap remains.
- Treat progress as liveness only. Progress events are never proof.
- Treat proof as explicit `Proof` records. Worker summaries are never proof.
- Keep Mission Control language: goal/mission, active runs, proof gaps, human intervention. No Kanban language.
- Do not introduce Postgres, a background websocket bus, or Launcher direct file reads in this stage.

Rejected alternatives:

- **UI-only spinner:** rejected because it does not fix stale detection or proof handoffs.
- **Raw model/tool transcript streaming:** rejected because it risks secrets, logs, and excessive payloads.
- **Trust Dev prose:** rejected because QA must verify artifacts through Harness proof IDs.
- **Schema-v2 migration now:** rejected because current dataclass/serde can accept optional schema-v1 fields with less risk.
- **Harness-specific fork of AIAgent:** rejected because normal Hermes runtime already exposes callbacks and activity hooks.

## Current repo audit evidence

Audited local checkout: repository root.

Load-bearing findings:

- `agent_runtime/models.py`
  - `AgentRun` currently has `llm`, `final_decision`, `error`, and `last_heartbeat_at`, but no live progress field.
  - `Proof` already has `type`, `title`, `path_or_value`, `metadata`, and `redaction_status`, so Dev handoff artifacts can become first-class proof without inventing another record.
- `agent_runtime/store.py`
  - `RunStore.heartbeat()` already refreshes `last_heartbeat_at` and appends `run.heartbeat`.
  - `RunStore.close_run()` already folds safe LLM counters into `run.closed` payload.
  - There is no atomic helper for `progress + heartbeat + progress event` yet.
- `agent_runtime/events.py`
  - `ALLOWED_EVENT_TYPES` is strict and payloads are capped at `4096` bytes.
  - Progress event types must be explicitly added here.
- `agent_runtime/persona_runtime.py`
  - `GPTPersonaRuntime._invoke_agent()` constructs `AIAgent(... quiet_mode=True, platform="agent_runtime", session_id=run.session_id, max_iterations=run.iteration_budget)` and then synchronously waits for `agent.run_conversation(...)`.
  - It currently passes no progress callbacks to `AIAgent`.
  - `_apply_llm_metadata()` already extracts provider/model/session/api/tool/token metadata after completion.
- `run_agent.py` / `agent/conversation_loop.py` / `agent/tool_executor.py`
  - `AIAgent.__init__()` already accepts `tool_progress_callback`, `tool_start_callback`, `tool_complete_callback`, `step_callback`, `status_callback`, `stream_delta_callback`, and `thinking_callback`.
  - `agent/conversation_loop.py` increments `api_call_count`, fires `step_callback(api_call_count, prev_tools)`, and sets `agent._api_call_count`.
  - `agent/tool_executor.py` already emits `tool_progress_callback("tool.started", name, preview, args)` and `tool_progress_callback("tool.completed", function_name, None, None, duration=..., is_error=...)`.
  - Therefore Stage 13 should first wire these callbacks into `RunProgressSink`; deeper provider hooks are optional only if tests show model-call completion cannot be represented from `step_callback`/final result.
- `agent_runtime/observability.py`
  - Current stalled-run logic uses `_run_age_seconds(ref, run) > threshold`, where `_run_age_seconds` is heartbeat age if present.
  - It does not surface active run details or progress fields.
- `agent_runtime/status.py` and `agent_runtime/snapshot.py`
  - Current summary exposes `running_runs` counts but no active run list.
  - Snapshot run summaries include safe `llm` but not `progress`.
- `agent_runtime/decision_contracts.py`
  - `REQUEST_QA_REVIEW` currently has no payload-specific validation.
  - QA `REPORT_QA_VERDICT` currently only supports plan-scope reviews and rejects implementation-scope verdicts.
  - This is the direct reason QA can receive a task where Dev prose says proof exists but `task.proof_ids` is empty.
- `agent_runtime/planning.py`
  - `REQUEST_QA_REVIEW` currently moves task to `QA_REVIEW_PLAN` without checking/attaching proof.
  - `PROPOSE_PATCH` moves to `DEV_READY_FOR_QA` but proof gates are not applied there.
- `agent_runtime/proof_gates.py`
  - Existing gates require safe diff/change proof, passed test proof, and QA verdict proof.
  - The module is reusable but needs role verdict application to create/require `ProofType.QA_VERDICT` for implementation review.

## Stage-level invariants

- Progress payloads must fit in `EVENT_PAYLOAD_LIMIT_BYTES` and never include raw prompts, raw stdout, tool arguments, auth headers, tokens, API keys, Keycloak `code`/`state`, MCP nonces/control paths, `.env` content, or full file paths unless explicitly approved as safe proof metadata.
- `RunProgressSink` callbacks must swallow/log their own exceptions. Progress failure must not crash a persona run.
- Any update to `AgentRun.progress` must also refresh `last_heartbeat_at` unless the run is already terminal.
- Every new mutating behavior needs a RED test first.
- Every role payload rule must fail closed with `DecisionPayloadInvalid` and a repair-friendly missing-key message.
- Temp-root tests must not read or mutate Tony's live Harness store.

---

## Ordered implementation stages

### 13.1 — Add `AgentRun.progress` schema-v1 field

**Objective:** Persist the latest redaction-safe progress snapshot on each run.

**Deep audit finding:** `AgentRun` has no place for live progress. `RunStore.heartbeat()` updates only `last_heartbeat_at`, so status can tell “recent heartbeat” but not “what is Dev doing?”

**Files:**

- Modify: `agent_runtime/models.py`
- Modify if serde tests reveal needed compatibility: `agent_runtime/serde.py`
- Test: create `tests/agent_runtime/test_run_progress.py`

**Exact model change:**

```python
@dataclass(slots=True)
class AgentRun:
    ...
    llm: dict[str, Any] | None = None
    progress: dict[str, Any] | None = None
    final_decision: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    schema_version: int = 1
```

Keep default `None` so old JSON without `progress` deserializes.

**Allowed progress keys v1:**

```python
RUN_PROGRESS_ALLOWED_KEYS = {
    "state",
    "summary",
    "last_update_at",
    "elapsed_seconds",
    "heartbeat_age_seconds",
    "api_calls",
    "tool_turns",
    "last_tool_name",
    "last_tool_status",
    "last_tool_started_at",
    "last_tool_finished_at",
    "response_chars",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "validation_status",
    "decision_type",
}
```

Use these constants later in `agent_runtime/progress.py`, not in `models.py`, unless tests need import reuse.

**Valid states:**

```text
starting
waiting_model
thinking
tool_calling
validating_decision
repairing_decision
completed
failed
cancelled
```

**RED tests:**

1. `test_agent_run_without_progress_deserializes`
   - Build JSON for `AgentRun` with no `progress` key.
   - `from_jsonable(AgentRun, raw)` returns `progress is None`.
2. `test_agent_run_progress_round_trips`
   - Create `AgentRun(progress={"state":"waiting_model","api_calls":1})`.
   - `to_jsonable` then `from_jsonable` preserves the dict.
3. `test_progress_field_does_not_change_schema_version`
   - Assert serialized `schema_version == 1`.

**Implementation tasks:**

1. Add optional field to `AgentRun` after `llm`.
2. Run RED tests and verify they fail before the field exists.
3. Implement field.
4. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_run_progress.py -q
```

**Acceptance criteria:**

- Old run JSON remains readable.
- New progress field round-trips.
- No schema-version bump.

**Commit suggestion:** `feat: add run progress field`

---

### 13.2 — Add append-only progress event types

**Objective:** Preserve a small safe activity timeline for active and recent runs.

**Deep audit finding:** `EventLog.append()` already rejects unknown types and payloads over 4096 bytes. We should reuse that safety by adding explicit progress event names instead of adding a parallel log.

**Files:**

- Modify: `agent_runtime/events.py`
- Test: `tests/agent_runtime/test_events.py` or `tests/agent_runtime/test_run_progress.py`

**Allowed event types to add:**

```python
"run.progress",
"run.model_call.started",
"run.model_call.finished",
"run.tool.started",
"run.tool.finished",
"run.decision.validation_started",
"run.decision.validation_finished",
```

**Event payload v1 contract:**

```json
{
  "state": "tool_calling",
  "summary": "terminal running",
  "api_calls": 4,
  "tool_turns": 6,
  "last_tool_name": "terminal",
  "last_tool_status": "running"
}
```

No raw args, no raw stdout, no file contents.

**RED tests:**

1. `test_progress_event_type_is_allowed`
   - Append `Event(type="run.progress", payload={"state":"waiting_model"})` in temp runtime root.
   - Tail returns event.
2. `test_progress_event_payload_size_limit_still_applies`
   - Append oversized `run.progress` payload.
   - Assert `EventPayloadTooLarge`.
3. `test_unknown_progress_like_event_rejected`
   - Append `run.progress.raw_stdout`.
   - Assert `ValueError`.

**Implementation tasks:**

1. Add event names to `ALLOWED_EVENT_TYPES`.
2. Verify payload cap remains unchanged.
3. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_events.py tests/agent_runtime/test_run_progress.py -q
```

**Acceptance criteria:**

- Only explicit progress event names are accepted.
- Payload limit still protects progress events.

**Commit suggestion:** `feat: allow harness progress events`

---

### 13.3 — Implement `RunProgressSink`

**Objective:** Centralize safe progress writes so fake runtimes, `GPTPersonaRuntime`, and future providers update runs consistently.

**Deep audit finding:** Without a single sink, progress safety will be duplicated across persona runtime, CLI watch, and callbacks. `RunStore.heartbeat()` exists but does not accept state summaries.

**Files:**

- Create: `agent_runtime/progress.py`
- Modify: `agent_runtime/store.py` only if adding a helper is cleaner
- Test: `tests/agent_runtime/test_run_progress.py`

**Public API:**

```python
from dataclasses import dataclass, field
from typing import Any

@dataclass(slots=True)
class RunProgressCounters:
    api_calls: int = 0
    tool_turns: int = 0

class RunProgressSink:
    def __init__(self, *, run_store: RunStore, event_log: EventLog, run_id: str): ...

    def update(self, *, state: str, summary: str = "", event_type: str = "run.progress", **safe_fields: Any) -> None: ...

    def model_call_started(self, *, api_calls: int | None = None) -> None: ...
    def model_call_finished(self, *, tokens: dict[str, Any] | None = None, response_chars: int | None = None) -> None: ...
    def tool_started(self, tool_name: str) -> None: ...
    def tool_finished(self, tool_name: str, *, status: str, duration: float | None = None) -> None: ...
    def decision_validation_started(self) -> None: ...
    def decision_validation_finished(self, *, status: str, decision_type: str | None = None) -> None: ...
    def completed(self, *, decision_type: str | None = None) -> None: ...
    def failed(self, *, summary: str) -> None: ...
    def cancelled(self, *, summary: str = "cancelled") -> None: ...
```

**Sanitization rules:**

- Allow only known keys from Stage 13.1.
- Convert datetimes to ISO strings through existing serde path where possible.
- Convert numbers/bools/short strings only.
- Truncate `summary` to 160 chars.
- Tool names must match `^[A-Za-z0-9_.:-]{1,80}$`; otherwise store `unknown`.
- Drop any key containing `secret`, `token`, `key`, `auth`, `password`, `credential`, `nonce`, `control`, `env`, `stdout`, `stderr`, `prompt`, `args`, or `arguments`.
- Never store callback `args` or tool previews from `AIAgent`.

**Store update behavior:**

```text
sink.update()
  -> run = run_store.get(run_id)
  -> if run.state terminal: return
  -> run.progress = sanitized dict plus last_update_at/elapsed_seconds
  -> run.last_heartbeat_at = now()
  -> run_store.update(run)
  -> event_log.append(Event(type=event_type, task_id=run.task_id, run_id=run.id, persona_id=run.persona_id, payload=sanitized subset))
```

**RED tests:**

1. `test_sink_updates_progress_and_heartbeat`
   - Open run, capture heartbeat, call `sink.update(state="waiting_model")`.
   - Assert progress set and heartbeat advanced.
2. `test_sink_appends_progress_event`
   - Tail event log and assert `run.progress` with run/task/persona IDs.
3. `test_sink_drops_unsafe_fields`
   - Call `sink.update(state="tool_calling", token="abc", stdout="raw", summary="Authorization: Bearer abc")`.
   - Assert token/stdout absent and summary redacted/truncated.
4. `test_sink_does_not_mutate_terminal_run`
   - Close run, call update, assert terminal state and progress unchanged or only final terminal progress from close path if designed.

**Implementation tasks:**

1. Write tests.
2. Create `progress.py` with constants and sink.
3. If needed, add `RunStore.update_progress(run_id, progress)` but prefer keeping write logic inside sink using existing `RunStore.update()`.
4. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_run_progress.py -q
```

**Acceptance criteria:**

- One sink owns redaction, heartbeat, event append.
- Callback exceptions cannot crash a run.
- Unsafe fields are dropped or scrubbed.

**Commit suggestion:** `feat: add run progress sink`

---

### 13.4 — Surface active progress in status, observe, and snapshot

**Objective:** One read command should answer: “is Dev working, what is it doing, and is it stale?”

**Deep audit finding:** `status.py` only exposes `running_runs` count. `observability.py` has recent run summaries but no active run detail. `snapshot.py` exposes run summaries but not a Mission Control active-run list.

**Files:**

- Modify: `agent_runtime/observability.py`
- Modify: `agent_runtime/status.py`
- Modify: `agent_runtime/snapshot.py`
- Test: `tests/agent_runtime/test_observability.py`
- Test: `tests/agent_runtime/test_status.py` if existing
- Test: `tests/agent_runtime/test_snapshot.py` if existing

**Add helper in `observability.py`:**

```python
def active_run_summary(run: Any, *, reference_time: datetime | None = None) -> dict[str, Any]: ...
```

**Safe active run shape:**

```json
{
  "run_id": "run_...",
  "task_id": "task_...",
  "stage_id": "stage_1",
  "persona_id": "dev",
  "state": "running",
  "started_at": "...",
  "elapsed_seconds": 282,
  "last_heartbeat_age_seconds": 4,
  "progress_state": "tool_calling",
  "summary": "terminal running",
  "api_calls": 12,
  "tool_turns": 15,
  "last_tool_name": "terminal",
  "last_tool_status": "running",
  "provider": "openai-codex",
  "model": "gpt-5.5",
  "stalled": false
}
```

**Rules:**

- `observability.active_runs` contains only `RunState.RUNNING` runs.
- `observability.recent_runs` may include final progress for completed runs.
- Stalled means heartbeat age > configured threshold, not elapsed runtime.
- Redaction-safe keys only; no `final_decision.payload`, no proof paths, no incident summaries.

**Important bug to fix:** current `stalled_runs` uses `_run_age_seconds(ref, run)` which is heartbeat age if present, but the variable name reads like elapsed runtime. Keep behavior but rename locally to `heartbeat_age` to avoid future false incidents.

**RED tests:**

1. `test_observability_lists_active_run_progress`
2. `test_observability_marks_fresh_heartbeat_not_stalled_even_if_elapsed_long`
3. `test_observability_marks_stale_heartbeat_stalled`
4. `test_snapshot_contains_active_runs_contract`
5. `test_completed_progress_only_in_recent_runs`

**Implementation tasks:**

1. Add `_safe_progress()` next to `_safe_llm()`.
2. Add `active_runs` to `build_observability()` return.
3. Add top-level `active_runs` to `build_snapshot()` by reusing observability helper; do not duplicate field policy.
4. Add status surface if not already embedded through `observability`.
5. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_observability.py tests/agent_runtime/test_snapshot.py -q
```

**Acceptance criteria:**

- Operator can distinguish active vs stalled.
- Completed runs do not remain active.
- Output remains recursively redaction-safe.

**Commit suggestion:** `feat: surface active harness run progress`

---

### 13.5 — Wire minimal progress into `GPTPersonaRuntime`

**Objective:** Get immediate liveness for Harness persona ticks before touching deeper agent internals.

**Deep audit finding:** `GPTPersonaRuntime.run_tick()` has natural milestones: attempt start, invoke agent, raw response received, validation start, validation result, repair attempt, failure. `_invoke_agent()` can pass callbacks to `AIAgent` because the constructor already supports them.

**Files:**

- Modify: `agent_runtime/persona_runtime.py`
- Test: `tests/agent_runtime/test_persona_runtime.py` or create `tests/agent_runtime/test_persona_runtime_progress.py`

**Constructor change:**

```python
class GPTPersonaRuntime:
    def __init__(..., progress_sink_factory=None):
        ...
        self._progress_sink_factory = progress_sink_factory
```

Default sink factory should create `RunProgressSink(run_store=RunStore(), event_log=EventLog(), run_id=run.id)`. Tests can inject a fake sink.

**Runtime milestones:**

- Before first attempt: `state=starting`, summary `invoking {persona.id}`.
- Before `_invoke_agent`: `state=waiting_model`, increment model call intent if useful.
- After raw result: `state=validating_decision`, `response_chars=len(raw)`.
- On validation success: `decision_validation_finished(status="valid", decision_type=...)` then `completed(...)`.
- On validation failure attempt 1: `state=repairing_decision`, summary contains safe missing-key message truncated.
- On final failure: `failed(summary="decision validation failed")`.

**Do not persist raw response.** Only `response_chars`.

**RED tests:**

1. `test_runtime_emits_progress_for_successful_decision`
   - Fake agent returns valid JSON.
   - Assert fake sink saw `starting`, `waiting_model`, `validating_decision`, `completed`.
2. `test_runtime_emits_repair_progress_on_invalid_first_response`
   - Fake agent returns invalid then valid.
   - Assert `repairing_decision` emitted with missing-key summary.
3. `test_runtime_failure_progress_on_two_invalid_responses`

**Implementation tasks:**

1. Add sink creation at start of `run_tick()`.
2. Thread sink to `_invoke_agent()`.
3. Add safe callbacks in Stage 13.6 or pass existing callback wrapper now if simple.
4. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_persona_runtime.py tests/agent_runtime/test_persona_runtime_progress.py -q
```

**Acceptance criteria:**

- Long fake runtime can update progress before returning.
- Decision repair/failure is visible.
- Success leaves final `progress.state == "completed"`.

**Commit suggestion:** `feat: emit persona runtime progress milestones`

---

### 13.6 — Wire existing `AIAgent` callbacks to the sink

**Objective:** Make real Harness personas visibly behave like normal Hermes agents during model/tool turns.

**Deep audit finding:** This stage does **not** need a broad new callback interface first. Existing callback points cover most liveness:

- `step_callback(api_call_count, prev_tools)` fires each model iteration.
- `tool_progress_callback("tool.started"...)` and `tool_progress_callback("tool.completed"...)` fire in `agent/tool_executor.py`.
- `status_callback(kind, message)` exists for lifecycle warnings.
- `run_conversation()` result carries `api_calls`, token fields, and final `messages` for `tool_turns`.

**Files:**

- Modify: `agent_runtime/persona_runtime.py`
- Modify only if tests reveal a missing callback: `agent/conversation_loop.py`
- Test: `tests/agent_runtime/test_persona_runtime_progress.py`
- Optional upstream-style unit: `tests/test_run_agent_callbacks.py` if existing conventions fit

**Callback mapping in `_invoke_agent()`:**

```python
def _make_callbacks(sink: RunProgressSink):
    def step_callback(api_call_count: int, prev_tools: list[dict]) -> None:
        sink.model_call_started(api_calls=api_call_count)
        if prev_tools:
            sink.update(state="waiting_model", summary="model reviewing tool results", api_calls=api_call_count)

    def tool_progress_callback(event_type: str, name: str, preview: str | None = None, args: dict | None = None, **kwargs) -> None:
        if event_type == "tool.started":
            sink.tool_started(name)
        elif event_type in {"tool.completed", "tool.finished"}:
            status = "error" if kwargs.get("is_error") else "ok"
            sink.tool_finished(name, status=status, duration=kwargs.get("duration"))
        elif event_type == "reasoning.available":
            sink.update(state="thinking", summary="model reasoning available")

    def status_callback(kind: str, message: str) -> None:
        sink.update(state="thinking", summary=f"agent {kind}")

    return {...}
```

Do **not** pass tool args or preview to the sink. Preview may contain user/file data.

**Factory call addition:**

```python
agent = factory(
    ...,
    quiet_mode=True,
    platform="agent_runtime",
    tool_progress_callback=callbacks.tool_progress_callback,
    step_callback=callbacks.step_callback,
    status_callback=callbacks.status_callback,
    stream_delta_callback=None,
)
```

Keep `quiet_mode=True`; progress is persisted, not printed.

**After result:**

- Call `_apply_llm_metadata(run, result, agent=agent)` as today.
- Then `sink.model_call_finished(tokens={...}, response_chars=...)` using safe result counters.

**RED tests:**

1. Fake `agent_factory` captures callbacks, calls `step_callback(1, [])`, calls `tool_progress_callback("tool.started", "terminal", "raw preview", {"command":"secret"})`, then returns valid result.
2. Assert persisted progress has `api_calls=1`, `last_tool_name="terminal"`, no `command`, no preview.
3. Assert `quiet_mode=True` still passed to factory.

**Implementation tasks:**

1. Add small callback adapter inside `persona_runtime.py` or `agent_runtime/progress.py` if reusable.
2. Wire callbacks to factory.
3. Run targeted tests.

**Acceptance criteria:**

- Active run progress increments during real/fake tool turns.
- `api_calls`/`tool_turns` align with final `run.llm` where available.
- Quiet mode suppresses console spam but not Harness progress.

**Commit suggestion:** `feat: connect agent callbacks to harness progress`

---

### 13.7 — Fix timeout, stale-run, interrupt, and cancel semantics

**Objective:** Prevent false timeout incidents for healthy long-running agents.

**Deep audit finding:** The observed “hang” was a silent synchronous run. Current recovery logic is heartbeat-based, but the wrapper/operator path can still infer failure from no stdout. This stage makes the rules explicit in code and CLI.

**Files:**

- Modify: `agent_runtime/recovery.py`
- Modify: `agent_runtime/ticker.py`
- Modify: `agent_runtime/daemon.py` if daemon stale handling mentions stdout/timeouts
- Modify: `hermes_cli/harness.py`
- Test: `tests/agent_runtime/test_recovery.py`
- Test: `tests/agent_runtime/test_ticker.py`
- Test: `tests/agent_runtime/test_harness_cli_progress.py`

**Rules to encode:**

- No stdout is not a hang.
- Running run with fresh `last_heartbeat_at` is active even if elapsed runtime is long.
- Running run with stale heartbeat is stalled.
- A watch command Ctrl+C detaches by default.
- Explicit cancel marks `RunState.CANCELLED` and a cancellation/error kind, not provider failure.
- If a wrapper timeout incident exists but the run later completes, observability should surface it as stale incident cleanup needed or auto-close if deterministic mapping exists.

**CLI behavior:**

- `hermes harness tick --timeout-seconds N` should mean wrapper wait timeout, not automatic persona failure unless `--cancel-on-timeout` is supplied.
- Add or document `hermes harness run cancel <run_id> --reason ...` if cancel exists; otherwise Stage 13.8 watch only detaches.

**RED tests:**

1. `test_long_running_fresh_heartbeat_not_marked_stale`
2. `test_stale_heartbeat_marked_stalled`
3. `test_cancelled_run_not_provider_failure`
4. `test_completed_run_not_left_with_timeout_intervention_when_incident_closed`

**Implementation tasks:**

1. Audit current recovery APIs before coding.
2. Add tests around existing behavior.
3. Adjust stale/intervention code to use heartbeat age naming.
4. Add explicit cancel semantics only if not already present.

**Acceptance criteria:**

- Fresh heartbeats prevent false stalled incidents.
- Explicit cancellation is audited distinctly.
- The operator is not encouraged to kill healthy silent runs.

**Commit suggestion:** `fix: make harness run liveness heartbeat based`

---

### 13.8 — Add streaming CLI/watch mode

**Objective:** Let an operator watch a tick/run without tailing raw logs.

**Deep audit finding:** Existing `hermes_cli/harness.py` owns `tick`, `status`, `observe`, daemon commands. Progress is persisted in events/status after prior stages, so watch mode can poll safe state rather than attaching to raw model streams.

**Files:**

- Modify: `hermes_cli/harness.py`
- Create if cleaner: `agent_runtime/watch.py`
- Test: `tests/agent_runtime/test_harness_cli_progress.py`

**Commands:**

```bash
hermes harness tick --stream
hermes harness run watch <run_id>
hermes harness observe --watch
```

**Minimum implementation:**

- `tick --stream` starts a normal tick, captures started run IDs from returned tick result or event tail, then polls active run summaries every 1s until all started runs are terminal.
- `run watch <run_id>` polls `RunStore.get(run_id)` plus event tail for that run.
- `observe --watch` periodically prints `build_observability().active_runs` and interventions.

**Safe text output shape:**

```text
tick tick_123 started
run run_abc dev started for task_768cb054
[00:12] dev model call 1 started
[00:18] dev tool terminal running
[00:42] dev tool terminal ok
[06:37] dev decision request_qa_review valid
tick complete: task_768cb054 -> qa_review_plan
```

**JSON mode shape:**

For `--json --stream`, emit NDJSON, one safe object per line:

```json
{"type":"run.progress","run_id":"run_abc","persona_id":"dev","elapsed_seconds":18,"progress_state":"tool_calling","last_tool_name":"terminal"}
```

**Interrupt behavior:**

- Ctrl+C during watch: print `detached from run run_abc; run is still active` and exit 130 or 0 by existing CLI convention.
- `--cancel-on-ctrl-c`: explicitly cancel the run if Stage 13.7 cancel command exists.

**RED tests:**

1. CLI formatter redacts/omits raw payload fields.
2. Watch loop exits when run becomes terminal.
3. Ctrl+C path calls detach by default, not cancel.
4. `--json --stream` emits parseable NDJSON.

**Implementation tasks:**

1. Build pure formatting helpers first; test without sleeping.
2. Add watch polling with injectable sleep/clock for tests.
3. Wire argparse/fire command surfaces in `hermes_cli/harness.py`.
4. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_harness_cli_progress.py -q
```

**Acceptance criteria:**

- Stream shows progress without raw secrets/logs.
- Stream exits on completion.
- Ctrl+C detaches unless explicit cancel flag is set.

**Commit suggestion:** `feat: add harness run watch stream`

---

### 13.9 — Define worker reply templates and handoff contracts

**Objective:** Make PM/Dev/QA/Neko outputs interface contracts, not prose.

**Deep audit finding:** `decision_schema.py` gives a generic `AgentDecision.payload` object. `decision_contracts.py` validates only some role payloads. The missing piece is exact role-specific payload contract text that prompts and validators share.

**Files:**

- Create: `agent_runtime/reply_templates.py`
- Optional docs split only if needed: `docs/agent-runtime-harness/13-worker-reply-contracts.md`
- Test: `tests/agent_runtime/test_reply_templates.py`

**Public API:**

```python
ROLE_REPLY_CONTRACTS: dict[str, str]

def reply_contract_for_role(role: str) -> str: ...
```

Use compact markdown snippets, not giant examples.

**PM `propose_acceptance.payload` v1:**

```json
{
  "objective": "string",
  "acceptance_criteria": ["string"],
  "non_goals": ["string"],
  "affected_repos": ["path-or-repo"],
  "required_proof": [
    {"kind": "diff_stat|test_run|analyze|screenshot|video", "required": true, "description": "string"}
  ],
  "handoff_to_dev": {
    "scope": "string",
    "allowed_paths": ["path"],
    "max_fix_passes": 1
  }
}
```

**Dev `request_qa_review.payload` v1:**

```json
{
  "changed_paths": ["path"],
  "commands_run": [
    {"command": "safe command label or exact command if no secrets", "exit_code": 0, "artifact_path": "path"}
  ],
  "proof_ids": ["proof_..."],
  "proof_artifacts": [
    {"type": "test_run|diff|diff_stat|screenshot|video|analyze", "title": "string", "path": "path", "redaction_status": "safe|needs_scan", "metadata": {"exit_code": 0}}
  ],
  "known_gaps": [],
  "qa_instructions": ["string"]
}
```

**QA implementation `report_qa_verdict.payload` v1:**

```json
{
  "review_scope": "implementation",
  "verdict": "approved|needs_fixes|blocked",
  "reviewed_proof_ids": ["proof_..."],
  "acceptance_results": [
    {"criterion": "string", "status": "pass|fail|not_proven", "evidence": ["proof_..."]}
  ],
  "remaining_gaps": [],
  "fix_instructions": [],
  "final_handoff_to_pm": {"ready_for_pm": true, "release_risk": "low|medium|high"}
}
```

Keep existing plan-scope QA payload valid for plan review.

**Neko `resolve_incident.payload` v1 extension:**

```json
{
  "incident_id": "inc_...",
  "resolution": "string",
  "next_state": "optional TaskState value",
  "reconciliation_actions": [
    {"kind": "attach_missing_proof|correct_state|block|request_human", "reason": "string", "evidence": ["proof_or_run_or_event_id"]}
  ],
  "remaining_gaps": []
}
```

**RED tests:**

1. Every role has a non-empty reply contract.
2. Contracts mention required decision types and payload keys.
3. Contracts contain no secrets or local absolute credential paths.

**Implementation tasks:**

1. Create module with compact strings.
2. Add tests.
3. Later Stage 13.10 injects into prompts.

**Acceptance criteria:**

- Implementers and personas share one source of reply contract truth.
- The next worker’s required inputs are named explicitly.

**Commit suggestion:** `feat: define harness reply templates`

---

### 13.10 — Patch role prompts with reply templates

**Objective:** Make personas emit contract-compatible decisions consistently.

**Deep audit finding:** `build_system_prompt()` currently hardcodes generic payload contracts. It mentions plan-scope QA only and does not tell Dev that proof-less QA handoff is invalid.

**Files:**

- Modify: `agent_runtime/persona_runtime.py`
- Modify: `agent_runtime/prompts/pm.md`
- Modify: `agent_runtime/prompts/dev.md`
- Modify: `agent_runtime/prompts/qa.md`
- Modify: `agent_runtime/prompts/alice_supervisor.md`
- Test: `tests/agent_runtime/test_persona_runtime.py` or `test_reply_templates.py`

**Implementation decision:**

Inject `reply_contract_for_role(role.value)` from `build_system_prompt()` after the universal Harness rules and before the compact JSON schema. Keep prompts human-readable but short.

**Prompt rules:**

- PM: preserve product repo scope, required proof, non-goals, and one bounded fix pass.
- Dev: may not request QA review unless `proof_ids`, `proof_artifacts`, or explicit blocking `known_gaps` are present.
- QA: may not approve implementation without `reviewed_proof_ids` and `acceptance_results`.
- Neko: may reconcile only from evidence; must not invent proof.

**RED tests:**

1. `test_build_system_prompt_includes_dev_proof_handoff_contract`
2. `test_build_system_prompt_includes_qa_implementation_verdict_contract`
3. `test_prompt_contract_not_too_large`
   - Assert injected contract length under a fixed budget, e.g. `< 6000` chars per role.

**Implementation tasks:**

1. Add reply contract import to `persona_runtime.py`.
2. Inject role-specific contract.
3. Update bundled prompts only for high-level role behavior; keep exact JSON shape in `reply_templates.py` to avoid drift.
4. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_persona_runtime.py tests/agent_runtime/test_reply_templates.py -q
```

**Acceptance criteria:**

- Prompts and validators agree.
- Contracts are concise.
- Persona prompts do not claim proof from prose.

**Commit suggestion:** `feat: inject worker handoff contracts into persona prompts`

---

### 13.11 — Strengthen decision payload validation

**Objective:** Fail closed before invalid handoffs mutate task state.

**Deep audit finding:** `REQUEST_QA_REVIEW` has no validation, and QA implementation verdicts are rejected because `decision_contracts.py` currently says Stage 3 only accepts plan-scope reviews.

**Files:**

- Modify: `agent_runtime/decision_contracts.py`
- Test: `tests/agent_runtime/test_decision_contracts.py`

**Validation changes:**

- `PROPOSE_ACCEPTANCE`
  - Existing `objective` and `acceptance_criteria` remain required.
  - If payload contains `required_proof`, validate list of objects with non-empty `kind` and boolean `required` defaulting to true.
  - If payload contains `handoff_to_dev`, validate object, `scope`, optional `allowed_paths`, optional integer `max_fix_passes >= 0`.
- `REQUEST_QA_REVIEW`
  - Payload must include at least one of:
    - non-empty `proof_ids`,
    - non-empty `proof_artifacts`,
    - non-empty `known_gaps` plus `blocker_reason` or decision type `block` instead.
  - If `proof_artifacts` present, each requires `type`, `title`, `path` or `value`, and `redaction_status`.
  - `changed_paths`, `commands_run`, `qa_instructions`, `known_gaps` must be lists when present.
- `REPORT_QA_VERDICT` / `APPROVE`
  - Keep existing plan-scope validation when `review_scope == "plan"`.
  - Add implementation-scope validation when `review_scope == "implementation"`:
    - `verdict` in `approved|needs_fixes|blocked`.
    - `reviewed_proof_ids` required and non-empty for `approved`.
    - `acceptance_results` required for `approved` and `needs_fixes`.
    - Approved result cannot contain `fail` or `not_proven` unless `waiver_id` exists.
- `RESOLVE_INCIDENT`
  - If `next_state` is ready/done/approved-like, require evidence through `reconciliation_actions[*].evidence` or `proof_ids`.

**RED tests:**

1. `test_dev_request_qa_review_rejects_prose_only_payload`
2. `test_dev_request_qa_review_accepts_proof_artifacts`
3. `test_qa_implementation_approved_requires_reviewed_proof_ids`
4. `test_qa_implementation_approved_rejects_not_proven_without_waiver`
5. `test_plan_scope_qa_contract_still_valid`
6. `test_neko_ready_reconciliation_requires_evidence`

**Implementation tasks:**

1. Add helper validators in `decision_contracts.py`; keep small and pure.
2. Preserve compatibility for plan review tests.
3. Ensure errors name missing keys exactly.
4. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_decision_contracts.py -q
```

**Acceptance criteria:**

- Current observed failure is reproduced: Dev summary/prose with no proof artifacts is rejected.
- Repair attempt context names missing `proof_ids`/`proof_artifacts`/`known_gaps`.
- Existing plan-review behavior still passes.

**Commit suggestion:** `fix: validate worker handoff payloads`

---

### 13.12 — Auto-attach Dev proof artifacts

**Objective:** Convert Dev handoff data into Harness `Proof` records before QA sees the task.

**Deep audit finding:** `ProofStore.attach()` already persists records and emits `proof.attached`, but `planning.py` does not call it for `REQUEST_QA_REVIEW`.

**Files:**

- Create: `agent_runtime/proof_handoff.py`
- Modify: `agent_runtime/planning.py`
- Modify: `agent_runtime/proof_rules.py` if type mapping needs aliases
- Test: `tests/agent_runtime/test_proof_handoff.py`
- Test: `tests/agent_runtime/test_planning.py`

**Public API:**

```python
@dataclass(slots=True)
class ProofHandoffResult:
    proof_ids: list[str]
    warnings: list[str]


def attach_dev_proof_artifacts(
    *,
    task: Task,
    decision: AgentDecision,
    actor: str,
    proof_store: ProofStore,
    event_log: EventLog,
) -> ProofHandoffResult: ...
```

**Type mapping:**

```text
test_run -> ProofType.TEST_RUN
diff -> ProofType.DIFF
diff_stat -> ProofType.DIFF_STAT
screenshot -> ProofType.SCREENSHOT
video -> ProofType.VIDEO
analyze -> ProofType.TEST_RUN with metadata.command_kind="analyze" unless ProofType.ANALYZE exists
commit -> ProofType.COMMIT
```

Check `agent_runtime/proof_rules.py` before implementation and add aliases only if existing enum supports them.

**Path/value safety:**

- Accept relative paths under an affected repo or the Hermes repo artifact/log path.
- Accept absolute paths only when under one of `task.affected_repos` or current Harness runtime artifact/log directory.
- Reject `.env`, credential, auth, token, nonce, control-file, and home-profile secret paths.
- Reject URLs with query tokens unless redacted.
- For `redaction_status == "safe"`, require artifact metadata to state how it was scanned or generated safely if available; otherwise allow but mark `needs_scan` if uncertain.

**Planning integration:**

Change `apply_planning_decision(... proof_store=None ...)` signature if needed. For `REQUEST_QA_REVIEW`:

1. Validate decision.
2. Attach any `proof_artifacts` via helper.
3. Merge returned proof IDs and existing `payload.proof_ids` into `task.proof_ids` deduped.
4. If no proof IDs and no blocking gaps, validation should already have failed.
5. Move implementation handoff to `TaskState.QA_TESTING` or existing `QA_REVIEW_PLAN` depending current state design. Prefer `QA_TESTING` for implementation proof review if current state machine supports it; otherwise document compatibility and use `QA_REVIEW_PLAN` until Stage 13.13.

**RED tests:**

1. `test_dev_proof_artifacts_create_proof_records_and_task_ids`
2. `test_existing_proof_ids_are_preserved_and_deduped`
3. `test_unsafe_proof_path_rejected`
4. `test_qa_context_includes_attached_proof_ids` if context builder can be tested here.
5. `test_request_qa_review_without_proof_does_not_transition`

**Implementation tasks:**

1. Inspect `proof_rules.py` enum before mapping.
2. Write helper tests.
3. Create helper.
4. Wire `planning.py` with optional `proof_store`; tests should fail if caller forgets store and artifacts exist.
5. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_proof_handoff.py tests/agent_runtime/test_planning.py -q
```

**Acceptance criteria:**

- Dev proof artifacts become proof records on the same task.
- QA context includes proof IDs.
- Unsafe paths are rejected before transition.

**Commit suggestion:** `feat: attach dev proof handoff artifacts`

---

### 13.13 — Strengthen QA verdict application and proof gate

**Objective:** Apply implementation QA verdicts from reviewed proof, not from generic approval prose.

**Deep audit finding:** `planning.py` currently uses QA verdict/approve primarily for plan review (`QA_REVIEW_PLAN -> DEV_IMPLEMENTING`). `proof_gates.py` can check proof totals but there is no implementation verdict application path that creates a QA verdict proof and advances to PM proof review.

**Files:**

- Modify: `agent_runtime/planning.py`
- Modify: `agent_runtime/proof_gates.py` if gate needs reviewed-proof awareness
- Modify: `agent_runtime/context_builder.py` if QA needs proof details in context
- Test: `tests/agent_runtime/test_planning.py`
- Test: `tests/agent_runtime/test_proof_gates.py`

**State transitions:**

```text
DEV_READY_FOR_QA or QA_TESTING
  + QA report_qa_verdict(review_scope="implementation", verdict="approved")
  -> QA_APPROVED or PM_PROOF_REVIEW

DEV_READY_FOR_QA or QA_TESTING
  + verdict="needs_fixes"
  -> QA_NEEDS_FIXES

DEV_READY_FOR_QA or QA_TESTING
  + verdict="blocked"
  -> BLOCKED with exact remaining_gaps/intervention
```

If existing ticker routes do not know `QA_APPROVED`, prefer `PM_PROOF_REVIEW` as the immediate next state and add TODO for PM final integration only if needed.

**QA verdict proof:**

When QA approves implementation, create a `Proof` record:

```python
Proof(
    id=f"proof_{...}",
    task_id=task.id,
    stage_id=task.current_stage_id,
    type=ProofType.QA_VERDICT,
    title="QA implementation verdict: approved",
    path_or_value="qa_verdict",
    created_by=actor,
    metadata={
        "verdict": "approved",
        "review_scope": "implementation",
        "reviewed_proof_ids": [...],
        "acceptance_results": [...],
    },
    redaction_status="safe",
)
```

**RED tests:**

1. `test_qa_approved_implementation_creates_qa_verdict_proof`
2. `test_qa_approved_implementation_advances_to_pm_proof_review`
3. `test_qa_needs_fixes_moves_to_qa_needs_fixes`
4. `test_qa_blocked_moves_to_blocked_with_gap_event`
5. `test_qa_cannot_approve_unattached_or_unknown_proof_id`

**Implementation tasks:**

1. Add implementation-scope branch in `apply_planning_decision()` before current plan-review branch or inside it by scope.
2. Require `proof_store` when applying implementation QA verdicts.
3. Validate every reviewed proof ID exists and belongs to task.
4. Create QA verdict proof and append to `task.proof_ids`.
5. Emit `qa.verdict_recorded` with safe counts only.

**Acceptance criteria:**

- QA cannot approve empty/unknown proof.
- Approved QA verdict becomes proof itself.
- PM receives a proof-gated task state.

**Commit suggestion:** `feat: apply proof-gated QA implementation verdicts`

---

### 13.14 — Add redaction and path-safety tests

**Objective:** Prove progress and proof metadata cannot leak secrets.

**Deep audit finding:** `events.py` caps payload size but does not inspect payload content. Existing code has multiple redaction-safe surfaces but no Stage 13-specific tests for progress and proof artifacts.

**Files:**

- Create or modify: `agent_runtime/redaction.py`
- Test: `tests/agent_runtime/test_progress_redaction.py`
- Test: `tests/agent_runtime/test_proof_handoff.py`
- Test: `tests/agent_runtime/test_observability.py`

**Reusable functions:**

```python
SENSITIVE_KEY_RE = re.compile(r"(secret|token|api[_-]?key|auth|password|credential|nonce|control|env|stdout|stderr|prompt|args|arguments)", re.I)

def safe_short_text(value: Any, *, max_chars: int = 160) -> str: ...
def scrub_progress_payload(payload: dict[str, Any]) -> dict[str, Any]: ...
def is_safe_artifact_path(path: str, *, allowed_roots: list[str]) -> bool: ...
```

If similar helpers already exist, reuse them; do not duplicate.

**Must reject or scrub:**

- `Authorization: Bearer ...`
- API keys/tokens/passwords
- Keycloak `code`/`state` callback URLs
- token-shaped signed URLs/query params
- MCP nonces/control paths
- raw env var dumps
- raw large stdout/stderr
- full raw prompts/tool args
- `.env`, `auth.json`, credential stores, profile control files

**RED tests:**

1. `test_progress_summary_redacts_bearer_token`
2. `test_progress_payload_drops_sensitive_keys`
3. `test_progress_payload_truncates_large_text`
4. `test_proof_handoff_rejects_env_file_path`
5. `test_observability_does_not_emit_final_decision_payload`

**Implementation tasks:**

1. Centralize helpers.
2. Update `RunProgressSink` to use helpers.
3. Update proof handoff path validation to use helpers.
4. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_progress_redaction.py tests/agent_runtime/test_proof_handoff.py tests/agent_runtime/test_observability.py -q
```

**Acceptance criteria:**

- Progress and proof metadata tests cover secret-like values.
- Event payload limits still prevent spam.
- Snapshot/observe remain recursively safe.

**Commit suggestion:** `test: harden progress and proof redaction`

---

### 13.15 — Minimal vertical slice with fake runtime

**Objective:** Prove the full architecture without real model/provider flakiness.

**Deep audit finding:** The Harness already supports fake/stub persona runtimes in tests through direct `PersonaRuntime`/store usage. This stage should prove state, progress, proof handoff, and QA verdict together.

**Files:**

- Test: create `tests/agent_runtime/test_stage13_vertical_slice.py`
- Modify only code required by failed integration tests.

**Flow:**

1. Use temp runtime root.
2. Create task with acceptance criteria and affected repo.
3. Fake PM emits `propose_acceptance` with `required_proof` and `handoff_to_dev`.
4. Fake Dev emits progress and `request_qa_review` with `proof_artifacts`.
5. Harness attaches proof records and task has `proof_ids`.
6. Fake QA context sees `proof_ids`.
7. Fake QA emits `report_qa_verdict(review_scope="implementation", verdict="approved", reviewed_proof_ids=[...])`.
8. Harness creates QA verdict proof and advances state.
9. `build_observability()` and `build_snapshot()` expose safe active/recent run/proof gap fields.

**RED test name:**

```python
def test_stage13_fake_pm_dev_qa_progress_and_proof_handoff_slice(...):
    ...
```

**Required assertions:**

- `run.progress` updates during fake Dev before completion.
- `task.proof_ids` is non-empty before QA.
- QA cannot approve if reviewed proof IDs are omitted.
- Safe snapshot contains active/recent run counters but not raw artifact path content beyond allowed proof summary.
- No secret string seeded in fake payload appears in `json.dumps(snapshot)`.

**Implementation tasks:**

1. Write the vertical test last after unit slices are green.
2. Fix integration seams only, not broad refactors.
3. Run:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_stage13_vertical_slice.py -q
```

**Acceptance criteria:**

- Fake PM → Dev → QA passes end-to-end.
- Proof handoff is machine-verifiable.
- Progress is visible and safe.

**Commit suggestion:** `test: add stage 13 fake vertical slice`

---

### 13.16 — Real `AIAgent` live smoke

**Objective:** Prove the real persona runtime path after fake tests pass.

**Deep audit finding:** The original issue occurred only on real `AIAgent` execution because `quiet_mode=True` made it appear silent. This stage verifies callbacks flow from real runtime into Harness progress.

**Files:**

- No production code unless smoke reveals callback gap.
- Optional script: `scripts/harness_stage13_smoke.py` if repeatable smoke is valuable.
- Evidence log under a safe temp artifacts directory, not committed if it includes environment-specific paths.

**Smoke prerequisites:**

- Use temporary Harness runtime root.
- Use same profile binding style as live Harness (`pm`, `gpt-launcher`, `launcher-qa` where available), but never mutate live task store.
- Stop live daemon or use isolated root to avoid racing.

**Commands:**

```bash
HERMES_AGENT_RUNTIME_ROOT="$(mktemp -d)" venv/Scripts/python -m pytest tests/agent_runtime/test_stage13_vertical_slice.py -q
HERMES_AGENT_RUNTIME_ROOT="$(mktemp -d)" hermes harness task create "Stage 13 smoke: safe no-op proof handoff" --json
HERMES_AGENT_RUNTIME_ROOT="..." hermes harness tick --json
HERMES_AGENT_RUNTIME_ROOT="..." hermes harness tick --stream
HERMES_AGENT_RUNTIME_ROOT="..." hermes harness observe --json
```

On Git Bash/Windows, if `mktemp` path handling is awkward, use a Python-created temp dir.

**Smoke assertions:**

- During Dev tick, `observe --json` shows active run progress with fresh heartbeat.
- Dev completion includes `run.llm.api_calls` and progress counters.
- If Dev emits proof artifacts, they become proof IDs.
- QA does not ask generic proof if proof IDs are present.
- No false `provider_failure` or timeout incident appears.

**Handoff evidence:**

Record redaction-safe:

- temp runtime root basename only, not full secret paths if sensitive;
- run IDs/task IDs;
- commands with exit codes;
- `observe` active/recent run summary;
- any incidents by kind only.

**Acceptance criteria:**

- Real AIAgent callbacks update Harness progress.
- Silent long runs are no longer mistaken for hangs.
- Live smoke result is documented in final handoff.

**Commit suggestion:** no commit unless adding repeatable smoke script/docs.

---

### 13.17 — Mission Control / Launcher read-model contract

**Objective:** Give Launcher enough JSON to show a live mission cockpit without reading raw Harness files.

**Deep audit finding:** `snapshot.py` already provides a redaction-safe read model with agents/tasks/runs/proofs. It needs a stable `active_runs` and `proof_gaps` contract that Launcher can consume through CLI JSON bridge.

**Files:**

- Modify: `agent_runtime/snapshot.py`
- Modify: `agent_runtime/observability.py`
- Test: `tests/agent_runtime/test_snapshot.py`
- Documentation only for Launcher until Tony requests UI implementation.

**Snapshot additions:**

```json
{
  "active_runs": [
    {
      "mission_id": "task_...",
      "mission_title": "...",
      "owner": "Dev",
      "persona_id": "dev",
      "profile": "gpt-launcher",
      "model": "gpt-5.5",
      "provider": "openai-codex",
      "elapsed_seconds": 397,
      "progress_state": "tool_calling",
      "api_calls": 16,
      "tool_turns": 15,
      "last_safe_activity": "terminal completed",
      "last_update_age_seconds": 2,
      "stalled": false
    }
  ],
  "proof_gaps": [
    {"mission_id": "task_...", "required": "focused_tests", "status": "missing"}
  ]
}
```

**Field mapping:**

- `mission_id` = `task.id`.
- `mission_title` = task title from task lookup.
- `owner` = display label from persona role: PM/Dev/QA/Neko.
- `profile` = agent persona `hermes_profile` if known.
- `provider/model` = run.llm first, then persona config.
- `last_safe_activity` = derived from progress state/tool name/status only.

**Copy constraints:**

- Use “Goal/Mission”, “Active Runs”, “Run Agent Tick”, “Human intervention”.
- Do not use card/board/column/Kanban language.

**RED tests:**

1. `test_snapshot_active_runs_launcher_contract`
2. `test_snapshot_proof_gaps_from_gate_results`
3. `test_snapshot_active_runs_no_raw_paths_or_payloads`

**Implementation tasks:**

1. Add helper that joins running runs to tasks/personas.
2. Add proof gap summaries from `can_dev_ready_for_qa`/`can_qa_approve` as appropriate.
3. Reuse safe progress helper.

**Acceptance criteria:**

- Launcher can render live active runs from `hermes harness snapshot --json` only.
- No direct runtime file reads required.

**Commit suggestion:** `feat: add mission control active run snapshot contract`

---

### 13.18 — Telegram/operator summaries

**Objective:** Let Alice answer “where is it?” accurately from Harness state.

**Deep audit finding:** Alice should not guess from chat history. After Stage 13.4/13.17, `observe`/`snapshot` contain enough safe state for pull-based summaries.

**Files:**

- Usually no production code required in this stage.
- If adding CLI helper: modify `hermes_cli/harness.py` with `summary` command.
- Test if code added: `tests/agent_runtime/test_harness_cli_progress.py`.

**Initial implementation:** pull-based only.

When asked, Alice/tooling should call:

```bash
hermes harness observe --json
```

Then summarize:

```text
Dev is still working:
- elapsed: 6m 20s
- api calls: 16
- tool turns: 15
- last activity: terminal ok
- heartbeat age: 2s
- incident: none
```

**No auto-spam in this stage.** Threshold pings for user-initiated long runs are later/optional.

**If adding command:**

```bash
hermes harness run summary <run_id> --json
```

Safe output only mirrors active/recent run summary.

**Acceptance criteria:**

- Summary is based on `observe`/`snapshot`, not memory or guessing.
- No raw logs/secrets.
- No recurring notifications unless explicitly scheduled later.

**Commit suggestion:** docs-only unless a CLI formatter is added.

---

### 13.19 — Final regression gate

**Objective:** Prove Stage 13 did not regress the Harness brainstem.

**Commands:**

```bash
venv/Scripts/python -m pytest tests/agent_runtime -q
venv/Scripts/python -m compileall -q agent_runtime tests/agent_runtime hermes_cli
venv/Scripts/python - <<'PY'
from pathlib import Path
bad=[]
for p in Path('agent_runtime').rglob('*.py'):
    text=p.read_text(encoding='utf-8')
    if 'kanban' in text.lower():
        bad.append(str(p))
if bad:
    raise SystemExit('Kanban import/leak in agent_runtime: '+', '.join(bad))
PY
git diff --check
```

**Expected:**

- Targeted Harness tests pass.
- Compile check passes.
- Diff whitespace check passes.
- No Kanban imports or terminology leaks into `agent_runtime`.

**If full tests fail:**

- Separate Stage 13 regressions from pre-existing failures.
- Do not claim full PASS unless the command exits 0.
- Record exact failing test names and whether they block launch readiness.

**Commit suggestion:** no code commit; this is verification before final commit.

---

### 13.20 — Local commit and handoff

**Objective:** Save the completed Stage 13 implementation locally and provide an enterprise-grade handoff.

**Commit command:**

```bash
git add agent_runtime hermes_cli tests docs/agent-runtime-harness
git commit -m "feat: add harness run progress and worker handoff contracts"
```

Do not push unless Tony explicitly orders a push.

**Handoff must include:**

- branch;
- HEAD commit;
- files changed;
- tests run with exit codes;
- temp-root fake vertical slice result;
- real AIAgent live smoke result, or explicit reason not performed;
- remaining gaps/interventions;
- whether Launcher UI work is still docs-only or ready for a separate implementation task.

**Enterprise-grade final checklist:**

- [ ] Progress field is schema-v1 compatible.
- [ ] Progress events are append-only and capped.
- [ ] Progress callbacks cannot leak args/stdout/secrets.
- [ ] Active runs visible in status/observe/snapshot.
- [ ] Heartbeat-based stale detection prevents false hang incidents.
- [ ] Watch mode streams safe progress and detaches safely.
- [ ] Dev proof-less QA handoff is rejected.
- [ ] Dev proof artifacts become `Proof` records.
- [ ] QA implementation verdict requires reviewed proof IDs.
- [ ] QA verdict becomes proof.
- [ ] Fake vertical slice passes.
- [ ] Real AIAgent smoke confirms quiet mode still updates progress.
- [ ] No Kanban semantics introduced.

---

## Implementation sequence summary

Recommended commit order:

1. 13.1 + 13.2: progress model/events.
2. 13.3: progress sink.
3. 13.4: read surfaces.
4. 13.5 + 13.6: persona/AIAgent callback wiring.
5. 13.7 + 13.8: stale/cancel/watch operator semantics.
6. 13.9 + 13.10: reply templates and prompts.
7. 13.11: decision validation.
8. 13.12: proof handoff attach.
9. 13.13: QA verdict proof gate.
10. 13.14: redaction/path hardening.
11. 13.15: fake vertical slice.
12. 13.16: real AIAgent smoke.
13. 13.17 + 13.18: Mission Control/operator read model.
14. 13.19 + 13.20: regression gate and local handoff.

## Definition of done

Stage 13 is complete only when all are true:

1. A long Dev run visibly updates `status`/`observe`/`snapshot` while running.
2. A watch command streams safe progress.
3. Stale detection is heartbeat/progress based, not stdout based.
4. Dev cannot request QA review with only prose proof.
5. Dev proof artifacts become Harness `Proof` records before QA runs.
6. QA sees proof IDs in context and can issue an implementation-scope verdict.
7. QA approval creates a QA verdict proof and advances to PM proof review / ready state.
8. Neko can reconcile handoff gaps only from evidence.
9. Launcher/Mission Control has a redaction-safe active-run/proof-gap JSON contract.
10. Tests and compile/diff gates pass, or exact blockers are reported without overstating readiness.
