# Stage 2 — GPT Persona Runtime

## Goal

Run PM/dev/QA personas as Hermes-native GPT actors. The persona runtime calls existing Hermes model + tool infrastructure directly through `AIAgent`, returns a *structured* `AgentDecision`, and never wraps `delegate_task`, the Claude CLI, or any external CLI. The harness — not the model — owns transitions, proof, and durability.

## Codebase anchors (verified)

| Concern | Existing file/symbol | Used how |
|---|---|---|
| Synchronous agent entrypoint | `run_agent.py::AIAgent.run_conversation` (forwarder) → `agent/conversation_loop.py::run_conversation` (line 187) | Persona ticks call `AIAgent.run_conversation(user_message, system_message, task_id)` directly. We do **not** call `AIAgent.chat()` — it discards the message list. |
| Init (60+ params) | `agent/agent_init.py::init_agent` (line 74) | Pass-through. `api_mode` accepts `"chat_completions" / "codex_responses" / "anthropic_messages" / "bedrock_converse" / "codex_app_server"` (line 227). |
| Provider/model resolution | `agent/agent_init.py` lines 227–305 — auto-routes GPT-5/Codex paths to `codex_responses` | Persona config can pin `provider`/`model`/`api_mode` explicitly; if omitted, auto-routing applies. |
| Per-tick isolation | `agent/conversation_loop.py` line 267 — `effective_task_id = task_id or str(uuid.uuid4())` sets `agent._current_task_id` for tool isolation | Each tick passes `task_id=<run_id>` so terminal/file/VM state never bleeds across runs. |
| Subagent inspiration (don't copy verbatim) | `tools/delegate_tool.py::_build_child_agent` (line 870) — fresh `AIAgent` with restricted toolsets, blocked tool list (`DELEGATE_BLOCKED_TOOLS`), per-child `subagent_id` | Reuse the construction pattern; do **not** wire personas as `delegate_task` calls (no durability, no stage semantics). |
| Toolset constants | `toolsets.py::TOOLSETS`, `_HERMES_CORE_TOOLS` (lines 31–73) | Persona toolset lists must be subsets of these names. Validation lives in `agent_runtime/personas.py`. |
| Session DB | `hermes_state.py::SessionDB` | Each run's `AgentRun.session_id` references a real session row so `hermes logs --session <id>` finds the transcript. |
| Auxiliary client live-main hook | `agent/conversation_loop.py::set_runtime_main` (line 228) | Personas inherit the tick's provider/model into auxiliary tooling without config-yaml round-trips. |
| Skill / context isolation | `AIAgent(skip_context_files=True, skip_memory=True, ...)` | Persona ticks pass both `True` — personas do **not** pollute MEMORY.md or load the user's CLAUDE.md skills. Their durable knowledge lives in the harness, not in the agent's memory provider. |

## Why not `delegate_task`

`tools/delegate_tool.py` proves Hermes can spawn isolated child agents, but for our use case it is:

1. **Synchronous from the parent's POV** (`_run_single_child` blocks the calling thread). The harness ticker needs to *open* a run, persist it, then **return** to its loop — the tick must not block.
2. **Result-summary oriented.** It collapses the child's trajectory into a one-shot text summary. We need a typed `AgentDecision`, not prose.
3. **Not durable across restarts.** A `delegate_task` mid-flight dies with its parent. Harness runs must survive a process kill.
4. **No stage / proof / gate awareness.** The blocked-tools list is right for "leaf workers", wrong for a QA persona that *must* be able to invoke screenshot or test tools.
5. **No per-task event stream.** All state is in-process locals.

We **reuse** the *construction pattern* from `_build_child_agent` (fresh `AIAgent`, restricted toolsets, role-scoped system prompt, parent-API-key inheritance) but instantiate it from `agent_runtime/persona_runtime.py` directly, capture the structured decision, and return.

## Package additions

```text
agent_runtime/
  personas.py              # AgentRole enum, default persona definitions, validate_toolsets()
  decision_schema.py       # AgentDecision dataclass + JSON Schema + parser
  context_builder.py       # AgentContext builder — what the persona is allowed to see
  persona_runtime.py       # PersonaRuntime (interface) + GPTPersonaRuntime (concrete)
  prompts/                 # System-prompt templates (markdown), one per role
    pm.md
    dev.md
    qa.md
    alice_supervisor.md
```

Tests:

```text
tests/agent_runtime/
  test_personas.py
  test_decision_schema.py
  test_context_builder.py
  test_persona_runtime_fake.py     # FakeAIAgent — no network
  test_persona_runtime_invalid.py  # malformed JSON / wrong shape / tool-out-of-role
```

## Persona definition

`personas.py`:

```python
from enum import StrEnum


class AgentRole(StrEnum):
    PM = "pm"
    DEV = "dev"
    QA = "qa"
    ALICE_SUPERVISOR = "alice_supervisor"


class AutonomyLevel(StrEnum):
    PROPOSE_ONLY = "propose_only"       # decision goes to Tony for approval
    APPLY_WITH_REVIEW = "apply_with_review"
    AUTONOMOUS = "autonomous"            # only used for QA test-run ticks


# Toolsets each role MAY enable. Final per-persona toolset is
# (configured) ∩ (allowed_for_role). Anything else is dropped + logged.
ALLOWED_TOOLSETS_BY_ROLE: dict[AgentRole, frozenset[str]] = {
    AgentRole.PM: frozenset({
        "file",            # read-only on PM ticks — see per-role deny list below
        "session_search",
        "todo",
    }),
    AgentRole.DEV: frozenset({
        "file", "search", "terminal",
        "session_search", "todo",
        "code_execution",
    }),
    AgentRole.QA: frozenset({
        "file", "search", "terminal",
        "browser", "vision", "session_search",
    }),
    AgentRole.ALICE_SUPERVISOR: frozenset({
        "file", "search", "session_search", "todo",
    }),
}

# Tools the harness blocks regardless of toolset membership.
# Mirrors tools/delegate_tool.py::DELEGATE_BLOCKED_TOOLS but scoped to *personas*.
PERSONA_BLOCKED_TOOLS = frozenset({
    "delegate_task",   # personas cannot spawn subagents — the harness orchestrates
    "clarify",         # no human-loop prompts inside a tick
    "memory",          # personas don't write MEMORY.md
    "send_message",    # no cross-platform side effects from a tick
    "cronjob",         # personas cannot schedule cron jobs
    "kanban_create", "kanban_complete", "kanban_block",
    "kanban_link", "kanban_comment", "kanban_unblock",
    "kanban_heartbeat",
    # Kanban work is gated by HERMES_KANBAN_TASK env (toolsets.py:60-70).
    # Personas explicitly run without that env to keep the surface clean.
})

# Per-role tool subset overrides (applied AFTER toolset expansion).
# PM never writes files; QA can run tests but never patch.
PER_ROLE_TOOL_DENIES: dict[AgentRole, frozenset[str]] = {
    AgentRole.PM: frozenset({"write_file", "patch", "terminal"}),
    AgentRole.DEV: frozenset({"send_message"}),
    AgentRole.QA: frozenset({"write_file", "patch"}),
    AgentRole.ALICE_SUPERVISOR: frozenset({"write_file", "patch", "terminal"}),
}
```

A persona file lives at `<root>/agent-runtime/agents/<persona_id>.json`:

```json
{
  "id": "dev",
  "display_name": "Dev Agent",
  "role": "dev",
  "model": null,
  "provider": null,
  "api_mode": null,
  "toolsets": ["file", "search", "terminal", "session_search"],
  "system_prompt_path": "personas/dev/system.md",
  "autonomy": "apply_with_review",
  "schema_version": 1
}
```

Default personas seed from `agent_runtime/prompts/<role>.md`; users can override with their own `<root>/agent-runtime/personas/<id>/system.md`. `personas.load_persona(id)` falls back from user dir → bundled prompt.

Stage 9 adds profile-bound persona execution fields (`hermes_profile`, `skills`, and optional `soul_overlay_path`) so the same PM/dev/QA persona IDs can run inside specific Hermes profiles such as `pm`, `gpt-launcher`, `launcher-qa`, and `alice` while keeping task state in the shared Harness store. See [Stage 9 — Profile-Bound Personas, Souls, and Skills](09-profile-bound-personas-souls-and-skills.md) for the AAA plan, SOUL overlays, skill manifests, readiness checks, and redaction requirements.

## Structured decisions

`decision_schema.py`:

```python
class DecisionType(StrEnum):
    NEEDS_CONTEXT = "needs_context"
    PROPOSE_ACCEPTANCE = "propose_acceptance"
    PROPOSE_STAGE_PLAN = "propose_stage_plan"
    CORRECT_STAGE = "correct_stage"
    REQUEST_FILE_READS = "request_file_reads"
    PROPOSE_PATCH = "propose_patch"
    REQUEST_TEST_RUN = "request_test_run"
    REQUEST_SCREENSHOT = "request_screenshot"
    REQUEST_VIDEO = "request_video"
    REQUEST_QA_REVIEW = "request_qa_review"
    REPORT_QA_VERDICT = "report_qa_verdict"
    APPROVE = "approve"
    BLOCK = "block"
    COMPLETE = "complete"
    REQUEST_HUMAN = "request_human"


@dataclass(slots=True)
class AgentDecision:
    type: DecisionType
    summary: str                     # <= 280 chars, shown in events + Launcher
    rationale: str                   # full prose, capped at 4 KB
    payload: dict[str, Any]          # type-specific (e.g. {"stages":[...]} for PROPOSE_STAGE_PLAN)
    requires_approval: bool = False
    schema_version: int = 1
```

Per-`DecisionType` payload contracts live alongside the enum (one TypedDict each). Schema is enforced at parse time; out-of-shape payloads raise `DecisionPayloadInvalid` and the harness records an `incident.opened` event of kind `model_invalid_output`.

The full JSON Schema (single source of truth, served to the model in the system prompt) lives at `agent_runtime/decision_schema.json`. It is the same schema used by `tests/agent_runtime/test_decision_schema.py` so the prompt and the parser cannot drift.

## Hermes integration

`persona_runtime.py`:

```python
class PersonaRuntime(Protocol):
    def run_tick(self, persona: AgentPersona, ctx: AgentContext, *, run: AgentRun) -> AgentDecision: ...


class GPTPersonaRuntime:
    def __init__(self, *, default_provider, default_model, credential_pool=None, session_db=None):
        self._default_provider = default_provider
        self._default_model = default_model
        self._credential_pool = credential_pool
        self._session_db = session_db

    def run_tick(self, persona, ctx, *, run):
        from run_agent import AIAgent

        toolsets = personas.effective_toolsets(persona)   # role ∩ config − blocked
        system_message = build_system_prompt(persona, ctx)
        user_message = render_context(ctx)                # AgentContext → markdown blob

        agent = AIAgent(
            provider=persona.provider or self._default_provider,
            model=persona.model or self._default_model or "",
            api_mode=persona.api_mode,
            enabled_toolsets=toolsets,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform="agent_runtime",
            session_id=run.session_id,
            credential_pool=self._credential_pool,
            session_db=self._session_db,
            max_iterations=run.iteration_budget,
            tool_progress_callback=_progress_into_run(run.id),
            status_callback=_status_into_run(run.id),
        )

        try:
            result = agent.run_conversation(
                user_message=user_message,
                system_message=system_message,
                task_id=run.id,                # binds tool isolation to *this* run
            )
        except Exception as exc:
            return _decision_from_runtime_error(exc, run)

        raw = result["final_response"]
        return parse_structured_decision(raw)   # raises DecisionPayloadInvalid on bad shape
```

Key contract points:

1. **Per-run session ID.** `AgentRun.session_id` is populated before the tick; `AIAgent` writes the transcript to `hermes_state.SessionDB` keyed on it. Launcher / Unreal observability links straight to that session.
2. **`task_id=run.id`.** `agent._current_task_id` is set by `conversation_loop.run_conversation` line 272, so any tool dispatched inside the tick (terminal, file, browser) lands in `<task_id>` isolation. We use the *run* id (not the harness task id) because each tick is its own VM/terminal scope.
3. **Tool progress streams into the run record.** `tool_progress_callback` → `runs/<run_id>.json::events`. The Launcher shows live activity per run.
4. **No subagent recursion.** `delegate_task` is in `PERSONA_BLOCKED_TOOLS`; personas are leaf actors. Orchestration is the harness's job.
5. **`skip_context_files=True` + `skip_memory=True`.** Personas are *task-bound* actors, not assistants. They must not load `CLAUDE.md` or write to memory providers like honcho/mem0. Durable knowledge lives in `<root>/agent-runtime/`.
6. **No prompt-cache breakage.** Personas keep a single fixed system prompt per role. We never mutate the system prompt mid-conversation (AGENTS.md "Prompt Caching Must Not Break" rule).

## Parse strategy

First version: ask the model for JSON in a fenced ```json block, parse tolerantly:

```python
def parse_structured_decision(text: str) -> AgentDecision:
    blob = _extract_first_json_block(text)         # ```json … ``` or first {...}
    raw = json.loads(blob)                          # strict; surrogate-clean (uses agent.message_sanitization helpers)
    _jsonschema_validate(raw)                      # against decision_schema.json
    return _decision_from_jsonable(raw)
```

Bounded repair: on `DecisionPayloadInvalid`, the harness performs **one** repair attempt by re-rendering the context with the validator error appended and `requires_repair=True`. If the second attempt also fails, the harness opens an `incident.opened` of kind `model_invalid_output` and leaves the task state untouched (no `task.transition` event).

Future evolution: providers that expose native structured output (OpenAI `response_format`, Anthropic tool-use JSON, Gemini schema) get a dedicated adapter. The decision schema stays canonical.

## Persona prompt contracts

Each role has a checked-in system prompt at `agent_runtime/prompts/<role>.md`. The harness composes the final system message as:

```
<role prompt>
<universal harness rules>
<decision schema (compact)>
<task summary>
<stage summary>
<agent-context window>
```

### PM (`pm.md` highlights)

- Read task; produce acceptance criteria, non-goals, suggested roles.
- Decide `requires_visual_proof` based on task class (Launcher UI / Unreal / gameplay / animation / media → true; backend-only / docs / CLI-only → false). QA may upgrade later.
- Review final proof bundle. Approve integration only when proof gates pass.
- Allowed decisions: `PROPOSE_ACCEPTANCE`, `APPROVE`, `BLOCK`, `REQUEST_HUMAN`, `NEEDS_CONTEXT`.

### Dev (`dev.md` highlights)

- Audit repo before planning (use `search`/`file` toolset).
- Create staged plan (`PROPOSE_STAGE_PLAN`).
- Deep-audit each stage on a subsequent tick (`CORRECT_STAGE`).
- Design tests (`PROPOSE_STAGE_PLAN` with `test_plan` filled).
- Propose patches (`PROPOSE_PATCH`); harness applies under autonomy policy.
- Allowed decisions: `NEEDS_CONTEXT`, `REQUEST_FILE_READS`, `PROPOSE_STAGE_PLAN`, `CORRECT_STAGE`, `PROPOSE_PATCH`, `REQUEST_TEST_RUN`, `REQUEST_QA_REVIEW`, `BLOCK`.

### QA (`qa.md` highlights)

- Review the plan + test design *before* implementation (decision: `APPROVE` or `CORRECT_STAGE`).
- Run/request tests (`REQUEST_TEST_RUN`).
- For UI/game tasks, capture screenshot/video (`REQUEST_SCREENSHOT` / `REQUEST_VIDEO`) — the harness owns the actual capture so the model cannot self-report.
- Report `REPORT_QA_VERDICT` with `payload.verdict in {"approved","needs_fixes"}` and an explicit list of proof IDs they relied on.
- Allowed decisions: `REQUEST_FILE_READS`, `REQUEST_TEST_RUN`, `REQUEST_SCREENSHOT`, `REQUEST_VIDEO`, `CORRECT_STAGE`, `REPORT_QA_VERDICT`, `BLOCK`.

### Alice (supervisor / `alice_supervisor.md`)

- Periodic high-level audit: any open task with no progress > N hours, missing proof, stale runs, duplicate effort.
- Only emits `BLOCK`, `REQUEST_HUMAN`, or `NEEDS_CONTEXT`. Never approves, never patches.

## Failure modes (per role)

| Failure | Outcome | State change |
|---|---|---|
| Model HTTP 5xx / timeout | `incident.opened` (`provider_failure`), run state → `FAILED`, task state unchanged | none |
| Invalid JSON after one repair | `incident.opened` (`model_invalid_output`), run state → `FAILED` | none |
| Decision type not allowed for role | `incident.opened` (`decision_role_mismatch`), run state → `FAILED` | none |
| Tool out-of-role attempted inside tick | Tool blocked at `enabled_toolsets`; if model still requests it, harness records `decision_tool_misuse` incident | none |
| Run heartbeat stops > TTL | Stage 5 (`Ticker`) marks run `STALE`, opens incident, agent state → `CRASHED` | none |

The principle: **process/model failures never become product task failures.** A task only moves to `TaskState.FAILED` via explicit `BLOCK`-then-PM-escalation or Tony's manual command.

## Tests (Stage 2)

- `FakeAIAgent` fixture in `tests/agent_runtime/fakes/` returns canned `final_response` strings. No network, no providers.
- `test_persona_runtime_fake.py`: dev persona tick → valid `PROPOSE_STAGE_PLAN` decision.
- `test_decision_schema.py`: every `DecisionType` parses and re-serializes losslessly.
- `test_persona_runtime_invalid.py`: bad JSON → one repair → still bad → `incident.opened`, task unchanged.
- `test_personas.py::test_pm_cannot_write_files`: PM persona's `enabled_toolsets` is filtered against `PER_ROLE_TOOL_DENIES`; `write_file`, `patch`, `terminal` are stripped.
- `test_personas.py::test_blocked_tools_always_dropped`: even if a user persona config lists `delegate_task`, it's removed.

## Acceptance criteria

1. A PM tick on a `CREATED` task produces a parseable `PROPOSE_ACCEPTANCE` decision; Stage 3 wiring will then transition the task to `PM_READY_FOR_DEV`.
2. No CLI wrappers, no `delegate_task`, no Claude shell-outs anywhere in `agent_runtime/`.
3. `tests/agent_runtime/test_persona_runtime_*.py` all pass with `FakeAIAgent` — no live network.
4. Personas only see the toolsets they are role-allowed to see; `test_personas.py` proves it.
5. Each tick writes one transcript row to `hermes_state.SessionDB` reachable via `hermes logs --session <session_id>`.
6. Repair-on-bad-JSON is bounded to ≤ 1 retry; no infinite loops.

## Risks / interventions

- **Free-form output rot**: the strict JSON Schema is the only thing standing between us and "the dev model started writing markdown plans again". Schema is checked into source and asserted in tests.
- **Provider routing surprises**: `agent_init.py` upgrades many GPT/Codex flows to `codex_responses` (lines 229–240). For deterministic harness behavior, all default persona configs **pin** `api_mode` so the upgrade path doesn't silently flip modes between ticks.
- **Tool isolation regressions**: `agent._current_task_id` is set inside `run_conversation` (line 272). If `conversation_loop` is refactored to defer that assignment, tools dispatched on the first iteration could collide. Tests assert isolation by inspecting the registered task_id on a stub `terminal` handler.
- **Cost runaway**: `iteration_budget` is per-run; default 90 (matches Hermes default in `run_agent.py:360`). The ticker (Stage 5) refuses to spawn more than one tick per task per tick interval, capping cost to *N tasks × 1 tick × iteration_budget*.
- **Prompt-cache breakage**: keeping the system prompt static per role per session is mandatory. Don't introduce dynamic "current stage" content into the system message — that content goes in the *user* message of each tick.
