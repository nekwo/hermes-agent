# Stage 11 — LLM State Observability Deep Audit and Fix Stages

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after Stage 11 docs are accepted. Preserve Mission Control language: Mission, Run, Persona, Human, Daemon, Proof. Avoid Kanban terminology.

**Goal:** Make the first LLM state and every downstream persona run fully auditable, correlation-safe, redaction-safe, and implementation-grade before continuing deeper agent-chain automation.

**Architecture:** Keep the Agent Runtime Harness as the deterministic state owner. Add immutable LLM execution metadata to `AgentRun`, correlate runs/events/logs/session files with `run_id` and `session_id`, deduplicate state-transition events, and expose the complete redaction-safe chain through `status`, `snapshot`, and `observe`. Fixes proceed in stages: deep audit first, then minimal TDD implementation slices.

**Tech Stack:** Python dataclasses/JSON store, Hermes `AIAgent`, `agent_runtime` modules, `hermes_cli harness`, pytest via `bash scripts/run_tests.sh`, Windows Git Bash with `venv/Scripts/python.exe`.

---

## Live audit anchor: active mission

```text
mission_id: task_d8a8111b
mission_title: allow apps to be dragged back into drawer from shell
current_state: pm_ready_for_dev
first_llm_run: run_f8c86ca46fec
first_llm_persona: pm
first_llm_session: 20260521_011033_dc05fb
session_file: X:\Eternia\.hermes\profiles\pm\sessions\session_20260521_011033_dc05fb.json
```

### First LLM state evidence

`run_f8c86ca46fec`:

```json
{
  "id": "run_f8c86ca46fec",
  "persona_id": "pm",
  "task_id": "task_d8a8111b",
  "state": "completed",
  "started_at": "2026-05-21T05:10:31.677341Z",
  "finished_at": "2026-05-21T05:10:45.925481Z",
  "final_decision": {
    "type": "propose_acceptance",
    "summary": "Define a PM-ready scope for enabling bidirectional drag-and-drop between shell and app drawer with safe swap behavior and verifiable evidence."
  },
  "session_id": null,
  "error": null
}
```

Profile session file evidence:

```text
profile: pm
session_id: 20260521_011033_dc05fb
model: gpt-5.3-codex-spark
base_url: https://chatgpt.com/backend-api/codex/
platform: agent_runtime
message_count: 2
```

Agent log evidence:

```text
2026-05-21 01:10:33.548 OpenAI client created provider=openai-codex model=gpt-5.3-codex-spark platform=agent_runtime
2026-05-21 01:10:33.667 conversation turn session=20260521_011033_dc05fb task=task_d8a8111b history=0
2026-05-21 01:10:45.902 API call #1 in=6436 out=1941 total=8377 latency=12.2s
2026-05-21 01:10:45.909 Turn ended finish_reason=stop tool_turns=0 response_len=1818
```

Event evidence from `events.jsonl`:

```text
05:10:31.687 run.opened run_f8c86ca46fec persona=pm iteration_budget=90
05:10:45.912 task.pm_fleshed criteria=5
05:10:45.912 task.transition created -> pm_ready_for_dev reason=<PM summary>
05:10:45.916 task.transition created -> pm_ready_for_dev reason=<PM summary>   # duplicate
05:10:45.930 run.closed completed
```

Current observability output:

```text
health: degraded
open_incidents: 0
running_runs: 1
stale_daemon: false
stalled_running_runs: 0
repeated_context_request_tasks: 1
context_request_count: 12
intervention: context_request_loop severity=medium task_id=task_d8a8111b
```

---

## Deep audit findings

### Finding A — Run record is not linked to LLM session

**Severity:** High

`AgentRun.session_id` remains `null`, even though Hermes created session `20260521_011033_dc05fb` under the PM profile.

**Evidence:**

- `runs/run_f8c86ca46fec.json` has `session_id: null`.
- `profiles/pm/sessions/session_20260521_011033_dc05fb.json` exists and contains the real LLM input/output.
- `agent.log` has the session id, but no run id.

**Impact:** An operator cannot click a Mission Control run and deterministically locate the exact LLM transcript without timestamp-based forensics.

**Required fix:** Persist `session_id` into `AgentRun` immediately after `AIAgent.run_conversation()` returns it, and include `run_id` in the agent runtime session/log correlation where possible.

---

### Finding B — LLM execution metadata is only in logs, not state

**Severity:** High

Token counts, latency, model, provider, base URL, API call count, tool turns, finish reason, and response length are present in logs/session files but absent from `AgentRun` and snapshots.

**Impact:** Mission Control cannot show whether a run was cheap/expensive, stuck, tool-heavy, or model-output-only. Debugging requires raw log spelunking.

**Required fix:** Add a redaction-safe `llm` metadata envelope to `AgentRun`, populated from the runtime result and/or AIAgent session data.

Target shape:

```json
{
  "llm": {
    "provider": "openai-codex",
    "model": "gpt-5.3-codex-spark",
    "base_url_host": "chatgpt.com",
    "session_id": "20260521_011033_dc05fb",
    "api_calls": 1,
    "tool_turns": 0,
    "input_tokens": 6436,
    "output_tokens": 1941,
    "total_tokens": 8377,
    "latency_ms": 12200,
    "finish_reason": "stop",
    "response_len": 1818,
    "validation_status": "valid",
    "decision_type": "propose_acceptance"
  }
}
```

Do **not** store raw prompts, raw outputs, credentials, auth JSON, MCP nonces, or full base URLs with credential-bearing query strings.

---

### Finding C — State transition events are duplicated

**Severity:** Medium

The first PM decision emitted two equivalent `task.transition created -> pm_ready_for_dev` events within the same millisecond window.

**Likely root cause:** Both `apply_planning_decision()` and `TaskStore.update()` append transition events.

**Impact:** Event-sourced timelines and observability counters may double-count transitions. Mission Control history will look noisy/untrustworthy.

**Required fix:** Make exactly one module responsible for transition events. Preferred authority: `MissionStateMachine` / `TaskStore.update()` with an explicit `transition_source`. Planning code should emit domain events like `task.pm_fleshed` but not duplicate generic transition events.

---

### Finding D — No first-class correlation id across run/event/log/session

**Severity:** Medium

Current audit required joining by timestamps, task id, persona id, and profile session file search.

**Required fix:** Add correlation fields everywhere:

```text
mission_id/task_id
run_id
session_id
tick_id
persona_id
profile
```

Minimum requirement:

- `run.opened` event includes `run_id`, `task_id`, `persona_id`, `tick_id` when known.
- `run.closed` event includes `decision_type`, `validation_status`, `session_id`, token counters when known.
- `AgentRun` stores `session_id` and `llm` envelope.
- `observe`/`snapshot` expose redaction-safe run/session links.

---

### Finding E — Dev can repeatedly request context without fulfillment or backoff

**Severity:** High for chain progress; Medium for observability because Stage 11 now detects it.

Current observability reports:

```text
context_request_loop task_id=task_d8a8111b context_request_count=12
```

**Impact:** Tokens are spent on repeated `request_file_reads` and occasional `propose_patch`, while mission state stays `pm_ready_for_dev`.

**Required fix:** After the observability/correlation stages, implement context-request fulfillment and anti-loop scheduling:

- persist context request status: `open | fulfilled | unsupported | superseded`;
- fulfill readable files into a bounded redaction-safe context bundle;
- pass fulfilled context into the next persona run;
- stop scheduling identical requests;
- escalate to Human when paths are unavailable/ambiguous after one repair attempt.

---

## Stage plan overview

1. **Stage 11.1 — Audit Snapshot + Golden Repro**: freeze redaction-safe reproductions of first LLM state and duplicate transition behavior.
2. **Stage 11.2 — Persist LLM Run Metadata**: add `AgentRun.llm` envelope and persist `session_id`/metrics.
3. **Stage 11.3 — Correlation IDs in Events and Logs**: carry `tick_id`, `run_id`, `session_id`, `persona_id`, and profile through events and safe logs.
4. **Stage 11.4 — Exactly-Once Transition Events**: deduplicate transition emission and add tests for no duplicate state transitions.
5. **Stage 11.5 — Observability Surfaces for LLM State**: update `observe`, `status`, and `snapshot` to expose safe LLM metadata and chain health.
6. **Stage 11.6 — Context Request Fulfillment / Anti-Loop**: start the next chain fix only after auditability is reliable.

---

## Stage 11.1 — Audit Snapshot + Golden Repro

### Goal

Create deterministic tests that reproduce the current audit gaps without depending on live logs or real credentials.

### Deep audit anchors

Existing files:

- `agent_runtime/models.py` — `AgentRun` currently has `session_id`, but it is not updated after persona runtime execution.
- `agent_runtime/ticker.py` — opens/closes runs and currently only stores `final_decision={type, summary}`.
- `agent_runtime/persona_runtime.py` — calls `agent.run_conversation()` and returns only parsed `AgentDecision`, losing runtime metadata.
- `agent_runtime/planning.py` and `agent_runtime/store.py` — both can append transition events.
- `agent_runtime/events.py` — event payloads are capped and allowlisted.
- `tests/agent_runtime/` — existing fake runtime tests can be extended.

### Tasks

#### Task 11.1.1 — Add failing test for session id persistence

**Files:**

- Modify: `tests/agent_runtime/test_ticker.py` or create `tests/agent_runtime/test_run_metadata.py`

**Test seed:**

Create a fake runtime returning an object or metadata-bearing decision equivalent:

```python
class RuntimeWithSession:
    def run_tick(self, persona, ctx, *, run):
        run.session_id = "session_test_123"
        return AgentDecision(
            type=DecisionType.PROPOSE_ACCEPTANCE,
            summary="ok",
            rationale="r",
            payload={"objective": "obj", "acceptance_criteria": ["done"]},
        )
```

Assert after `TickEngine(...).tick_once()`:

```python
stored = RunStore().get(result.actions_taken[0].payload["run_id"])
assert stored.session_id == "session_test_123"
```

**Expected RED:** fails if metadata is not persisted before close.

#### Task 11.1.2 — Add failing test for duplicate transition prevention

**Files:**

- Modify/Create: `tests/agent_runtime/test_transition_events.py`

**Expected behavior:** one PM decision emits one `task.transition` for `created -> pm_ready_for_dev`.

Read `EventLog().tail(...)` under isolated runtime root and assert count is exactly 1.

#### Task 11.1.3 — Add failing test for observability safe run metadata

**Files:**

- Modify: `tests/agent_runtime/test_observability.py`

**Expected behavior:** `build_observability()` includes safe run LLM summary fields but does not leak raw base URL paths, prompts, or secrets.

---

## Stage 11.2 — Persist LLM Run Metadata

### Goal

Make every completed or failed persona run self-contained enough for enterprise audit without raw sensitive data.

### Fixed decisions

- Keep `schema_version: 1` for backward compatibility; add optional fields only.
- Add `llm: dict[str, Any] | None = None` to `AgentRun`.
- Store `base_url_host`, not full `base_url`.
- Do not store prompts, raw outputs, encrypted reasoning, auth values, or credentials.
- Let `persona_runtime` update the passed `run` object before returning.
- Let `ticker` persist the mutated `run` through `RunStore.close_run()`.

### Files

- Modify: `agent_runtime/models.py`
- Modify: `agent_runtime/persona_runtime.py`
- Modify: `agent_runtime/store.py`
- Modify: `agent_runtime/snapshot.py`
- Modify: `agent_runtime/observability.py`
- Tests: `tests/agent_runtime/test_run_metadata.py`, `tests/agent_runtime/test_persona_runtime_fake.py`

### API contract

`AgentRun.llm` safe envelope:

```python
llm: dict[str, Any] | None = None
```

Recommended keys:

```text
provider
model
base_url_host
session_id
api_calls
tool_turns
input_tokens
output_tokens
total_tokens
latency_ms
finish_reason
response_len
validation_status
decision_type
```

### Implementation notes

`AIAgent.run_conversation()` currently returns a dict. Inspect live/fake result keys. If token counters are not available in the return dict, start by persisting the guaranteed subset:

```text
provider, model, session_id, validation_status, decision_type
```

Then add counters by plumbing result metadata from the agent loop in a separate small commit.

### Verification

```bash
bash scripts/run_tests.sh tests/agent_runtime/test_run_metadata.py tests/agent_runtime/test_persona_runtime_fake.py -q
bash scripts/run_tests.sh tests/agent_runtime -q
python -m compileall agent_runtime hermes_cli
```

---

## Stage 11.3 — Correlation IDs in Events and Logs

### Goal

Make `run_id -> session_id -> profile session file -> logs -> events` directly traceable.

### Fixed decisions

- `run_id` remains the central correlation id inside Harness.
- `session_id` is the Hermes profile transcript id.
- `tick_id` is the daemon/tick correlation id.
- Events remain redaction-safe; payloads may include IDs and counts only.

### Files

- Modify: `agent_runtime/ticker.py`
- Modify: `agent_runtime/store.py`
- Modify: `agent_runtime/events.py` only if allowed event payload validation needs helper constants.
- Modify: `agent_runtime/persona_runtime.py`
- Modify: `agent_runtime/snapshot.py`
- Tests: `tests/agent_runtime/test_event_correlation.py`

### Event payload contract

`run.closed` payload should include:

```json
{
  "state": "completed",
  "decision_type": "propose_acceptance",
  "session_id": "20260521_011033_dc05fb",
  "validation_status": "valid"
}
```

`run.opened` payload should include:

```json
{
  "stage_id": null,
  "iteration_budget": 90,
  "tick_id": "tick_..."
}
```

If `tick_id` cannot be passed safely without broad refactor, add it in Stage 11.3.2 after adding a `current_tick_id` field to the execution context.

### Verification

- A synthetic tick produces `run.opened` and `run.closed` with stable correlation ids.
- No event payload exceeds `EVENT_PAYLOAD_LIMIT_BYTES`.
- `json.dumps(build_snapshot())` does not contain secrets from fake errors or fake prompts.

---

## Stage 11.4 — Exactly-Once Transition Events

### Goal

Eliminate duplicate state-transition events while keeping domain-specific events like `task.pm_fleshed`.

### Deep audit

Current duplicate first-state evidence:

```text
05:10:45.912 task.transition created -> pm_ready_for_dev
05:10:45.916 task.transition created -> pm_ready_for_dev
```

Likely emitters:

- `agent_runtime/planning.py::apply_planning_decision()` appends `task.transition`.
- `agent_runtime/store.py::TaskStore.update()` appends transition events when state changes.

### Fixed decision

Use exactly one generic transition event emitter. Preferred:

- Keep `TaskStore.update()` as generic persistence transition emitter.
- Remove generic `task.transition` append from `apply_planning_decision()`.
- Keep domain events from planning code: `task.pm_fleshed`, `task.stage_added`, `task.stage_updated`, `plan.reviewed`, `context.requested`.

### Files

- Modify: `agent_runtime/planning.py`
- Modify: `tests/agent_runtime/test_transition_events.py`
- Ensure existing tests expecting events are updated to count only one transition.

### Verification

```bash
bash scripts/run_tests.sh tests/agent_runtime/test_transition_events.py tests/agent_runtime/test_planning.py tests/agent_runtime/test_ticker.py -q
```

---

## Stage 11.5 — Observability Surfaces for LLM State

### Goal

Mission Control and CLI should show the audit chain without requiring raw log access.

### Required CLI outputs

`hermes harness observe --json` should include:

```json
{
  "health": {"status": "degraded"},
  "signals": {...},
  "interventions": [...],
  "recent_events": [...],
  "recent_runs": [
    {
      "run_id": "run_f8c86ca46fec",
      "persona_id": "pm",
      "task_id": "task_d8a8111b",
      "state": "completed",
      "decision_type": "propose_acceptance",
      "session_id": "20260521_011033_dc05fb",
      "model": "gpt-5.3-codex-spark",
      "provider": "openai-codex",
      "total_tokens": 8377,
      "latency_ms": 12200,
      "validation_status": "valid"
    }
  ]
}
```

`hermes harness snapshot --json` should include enough of the same run metadata for Launcher Mission Control to render a timeline.

### Redaction rules

Never expose:

- raw prompts;
- raw assistant outputs beyond summary/decision type;
- encrypted reasoning blobs;
- API keys/tokens/auth JSON;
- full local secret paths;
- MCP nonces/control files.

### Tests

- Add fake run with `llm` metadata containing a secret-bearing base URL; assert observe/snapshot output only includes safe host.
- Add fake raw prompt in run error; assert not present.
- Assert Telegram-safe summary is concise and includes exact intervention kind.

---

## Stage 11.6 — Context Request Fulfillment / Anti-Loop

### Goal

Fix the next actual chain break: Dev repeatedly requests file context without the Harness fulfilling or blocking the request.

### Deep audit evidence

Current mission has repeated context requests:

```text
context_request_count: 12
state: pm_ready_for_dev
several completed request_file_reads and propose_patch decisions
no stages
no applied patch
no QA transition
```

### Fixed decisions

- Context requests become first-class records, not just appended raw dicts.
- Each request has `id`, `actor`, `paths`, `reason`, `status`, `created_at`, `fulfilled_at`, `artifact_id | bundle_id`, and `failure_reason`.
- Harness, not the model, resolves paths and enforces redaction/scope.
- The next Dev run receives fulfilled context in `AgentContext.recent_events` or a dedicated `context_bundles` field.
- Identical repeated requests should not trigger another model run; they should be marked duplicate/superseded and surfaced as an intervention if unresolved.

### Proposed data shape

```python
@dataclass(slots=True)
class ContextRequest:
    id: str
    task_id: str
    actor: str
    paths: list[str]
    reason: str
    status: str  # open | fulfilled | unsupported | superseded
    created_at: datetime
    fulfilled_at: datetime | None = None
    bundle_id: str | None = None
    failure_reason: str | None = None
```

Start as `dict` under current `Task.context_requests` for schema v1 compatibility, but isolate creation/update helpers in a new module:

```text
agent_runtime/context_requests.py
```

### Files

- Create: `agent_runtime/context_requests.py`
- Modify: `agent_runtime/models.py` only if adding typed class is safe; otherwise keep dicts.
- Modify: `agent_runtime/planning.py`
- Modify: `agent_runtime/context_builder.py`
- Modify: `agent_runtime/ticker.py`
- Modify: `agent_runtime/observability.py`
- Tests: `tests/agent_runtime/test_context_requests.py`

### Implementation sequence

1. Add tests for request dedupe.
2. Add tests for unsupported path escalation.
3. Add tests for fulfilled context passed into next Dev context.
4. Implement request helper functions.
5. Implement a bounded file resolver with path allowlist rooted at configured workdir/repo.
6. Add observability intervention for `context_request_unfulfilled` and `context_request_loop` with exact request id.
7. Stop scheduling repeated Dev runs when only unresolved duplicate context requests exist.

### Verification

```bash
bash scripts/run_tests.sh tests/agent_runtime/test_context_requests.py tests/agent_runtime/test_context_builder.py tests/agent_runtime/test_ticker.py -q
bash scripts/run_tests.sh tests/agent_runtime -q
python -m compileall agent_runtime hermes_cli
```

---

## Rollout and live verification checklist

After each stage:

```bash
bash scripts/run_tests.sh tests/agent_runtime -q
python -m compileall agent_runtime hermes_cli
git diff --check
```

After Stage 11.2+:

```bash
./venv/Scripts/python.exe -m hermes_cli.main harness observe --json
./venv/Scripts/python.exe -m hermes_cli.main harness snapshot --json
```

Live acceptance for Stage 11:

- First PM run shows non-null `session_id`.
- First PM run exposes safe LLM metadata in `observe`/`snapshot`.
- Event timeline has one transition for `created -> pm_ready_for_dev`.
- `run.closed` event links `run_id`, `session_id`, `decision_type`, and validation status.
- Observability detects context-loop before Stage 11.6 and reports it with exact request count.
- After Stage 11.6, repeated context loops stop; Dev either receives context, advances to stage/patch, or raises a concrete Human intervention.

---

## Commit sequence

Use small commits in this order:

```text
1. test: capture harness llm run metadata gaps
2. feat: persist harness llm run metadata
3. feat: correlate harness run events
4. fix: emit mission transitions exactly once
5. feat: expose llm run observability
6. feat: fulfill harness context requests
```

Do not push unless Tony explicitly orders a push.

---

## Out of scope for Stage 11

- Renaming internal `Task` model to `Mission`.
- Adding a database migration away from JSON store.
- Adding a broad workflow designer or Kanban bridge.
- Exposing raw LLM transcripts in Launcher.
- Making Dev apply arbitrary patches without proof gates and QA review.

---

## Final implementation handoff

The next implementer should start at **Stage 11.1** and not jump to context fulfillment first. Without run/session/event correlation, future debugging will keep depending on forensic timestamp joins. Stage 11 must make the chain auditable first, then fix the chain.
