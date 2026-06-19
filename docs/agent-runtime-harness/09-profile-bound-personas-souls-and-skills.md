# Stage 9 — Profile-Bound Personas, Souls, and Skills

## Goal

Bind Agent Runtime Harness personas to real Hermes profiles so PM, Dev, QA, and Alice Supervisor can use the correct OAuth credentials, MCP servers, model defaults, skills, and profile-specific operating context while the Harness still owns the deterministic mission state machine.

The result must feel like:

```text
Goal/Mission → PM profile → Dev profile → QA profile → proof gate → Alice/Tony ready or intervention
```

Not like:

```text
Kanban card → worker lane → board ceremony → status drift
```

This stage upgrades persona execution power without importing Kanban concepts into Mission Control.

## Product stance

Use existing Hermes profile infrastructure as the execution environment for Harness personas. Do not duplicate OAuth storage, MCP config, skills, gateway setup, profile config, or model/provider routing.

Enterprise-grade here means:

- each persona runs under a deliberate profile identity;
- each profile has a tightly scoped SOUL and skill set;
- OAuth/MCP/tools are available where useful and denied where dangerous;
- gateway side effects stay blocked inside ticks unless routed through Alice/Supervisor policy;
- Launcher Mission Control can show profile identity and readiness without leaking secrets;
- tests prove profile routing, blocked tools, and redaction behavior.

## Deep audit findings from current repo / host

### Current Harness state

- `agent_runtime/personas.py` defines built-in persona IDs: `pm`, `dev`, `qa`, `alice_supervisor`.
- `agent_runtime/models.py::AgentPersona` currently stores `provider`, `model`, `api_mode`, `toolsets`, `system_prompt_path`, and `autonomy`; it does **not** yet store `hermes_profile`.
- `agent_runtime/config.py::configured_personas()` merges `agent_runtime.personas.<id>` overrides from `config.yaml`, but only for provider/model/api_mode/toolsets today.
- `agent_runtime/persona_runtime.py::GPTPersonaRuntime` creates `AIAgent(...)` directly in the current process/profile. It does not yet enter another profile context.
- `hermes_cli/harness.py::_cmd_tick()` passes runtime defaults to `GPTPersonaRuntime(default_provider=..., default_model=...)` and then the ticker chooses `pm`, `dev`, or `qa` by task state.
- `hermes harness agents --json` currently shows all persona `provider` and `model` values as `null`, so they inherit the active `alice` profile default (`openai-codex / gpt-5.5` on the audited host).

### Existing profile inventory observed on Tony's host

`hermes profile list` currently includes these useful profiles:

- `pm` — likely PM/scoping lane.
- `gpt-launcher` — GPT/Codex Launcher implementation profile.
- `launcher-qa` — Launcher QA profile.
- `launcher-qa-direct` — direct Launcher QA profile alias.
- `reviewer` — independent review profile.
- `alice` — Alice supervisor/operator profile with gateway active.
- `spark_launcher`, `spark_testwriter`, `spark_logreader` — fast execution/support profiles; useful later only if Harness adds explicit subordinate execution, not for the first profile-bound persona slice.

Do not hardcode these names in core code. They belong in profile config defaults and local deployment docs.

### SOUL / skills audit anchors

- Hermes profiles already support profile-specific `SOUL.md` files.
- Existing Launcher/backend SOUL files contain valuable AAA rules, self-QA expectations, MCP freshness warnings, and anti-sprawl policy, but many are Kanban-worded.
- Harness persona SOULs must adapt those lessons into mission-loop language: goal, persona tick, proof, intervention — not card/board completion.
- Skills are already profile-scoped and can be loaded through normal Hermes skill discovery. The Harness should allow profile skills to be available to the persona runtime but should still deny dangerous side-effect tools at the Harness/tool policy layer.

## Code-level deep audit addendum — 2026-05-20

This addendum turns Stage 9 from a concept into an implementation-ready code plan against the current Hermes and Launcher codebases.

### Hermes profile primitives already available

Relevant code anchors:

- `hermes_constants.py`
  - `set_hermes_home_override(path)` and `reset_hermes_home_override(token)` provide context-local `HERMES_HOME` scoping without mutating global `os.environ`.
  - `get_hermes_home()` honors the context-local override first, then `HERMES_HOME`, then default `~/.hermes`.
  - `get_default_hermes_root()` deliberately reads `os.environ["HERMES_HOME"]`, not the context override. This is useful for keeping the Agent Runtime Harness store shared, but it means code that enters a persona profile context must avoid accidentally loading profile-local `agent_runtime.store_root` from `get_config_path()` unless explicitly intended.
  - `get_config_path()` and `get_skills_dir()` are context-override-aware because they call `get_hermes_home()`.
- `hermes_cli/profiles.py`
  - `normalize_profile_name(name)` validates/canonicalizes profile IDs.
  - `get_profile_dir(name)` resolves `default` or `<root>/profiles/<name>`.
  - `profile_exists(name)` is the cheap readiness primitive for missing profile detection.
  - `get_active_profile_name()` infers the active profile from `get_hermes_home()`.

Implementation implication:

- `agent_runtime/profile_context.py` should use `hermes_cli.profiles.get_profile_dir/profile_exists/normalize_profile_name` and `hermes_constants.set_hermes_home_override`.
- It should also set a temporary subprocess env overlay for tools that spawn child processes: `HERMES_HOME=<profile_home>` and, if `{profile_home}/home` exists, `HOME=<profile_home>/home` for subprocesses only.
- It must restore context/env in `finally` on success and exception.

### Shared store root hazard

Current `agent_runtime/paths.py::store_root()` does this:

1. honors `HERMES_AGENT_RUNTIME_ROOT`;
2. reads `agent_runtime.store_root` from `get_config_path()`;
3. falls back to `get_default_hermes_root() / "agent-runtime"`.

If Stage 9 simply enters `pm` / `gpt-launcher` / `launcher-qa` profile context before store operations, `get_config_path()` can point at the persona profile config and may accidentally change the runtime store root. That would split Mission Control into per-profile mission stores.

Required implementation rule:

- Load and pin the shared runtime root before entering any persona profile context.
- During persona ticks, either set `HERMES_AGENT_RUNTIME_ROOT` to the shared root in the temporary env overlay or pass stores/config objects that were constructed before profile entry.
- Add a regression test proving `TaskStore`, `RunStore`, and `snapshot_path()` still use the same shared root while the persona profile context is active.

### AgentPersona schema compatibility hazard

Current `agent_runtime/models.py::AgentPersona` has no `hermes_profile`, `skills`, or `soul_overlay_path`.

Current `agent_runtime/serde.py::upgrade()` rejects any `schema_version != 1`:

```python
if version != 1:
    raise ValueError(f"unsupported schema_version: {version}")
```

Therefore Stage 9 must not blindly set `AgentPersona.schema_version = 2` unless `serde.upgrade()` is updated first.

Safe implementation options:

1. **Preferred:** add nullable/default fields while keeping `schema_version = 1` for now; older JSON loads because dataclass defaults fill missing fields.
2. Later, add a formal v1→v2 upgrade path and only then bump schema versions.

Tests must cover both old persona JSON and new persona JSON.

### Config merge gap

Current `agent_runtime/config.py::AgentRuntimeConfig` contains:

```python
store_root: str | None
personas: dict[str, dict[str, Any]]
```

`configured_personas()` currently merges only:

- `provider`
- `model`
- `api_mode`
- `toolsets`

Stage 9 must extend this to merge:

- `hermes_profile`
- `skills`
- `soul_overlay_path`
- optional `required_mcp_servers` if readiness checks need it; default QA requirement can be inferred as `launcher_qa` for Launcher missions.

Existing test anchor:

- `tests/agent_runtime/test_config.py::test_config_merges_persona_overrides` already proves default model + toolset filtering. Extend this file first.

### Persona runtime profile gap

Current `agent_runtime/persona_runtime.py::GPTPersonaRuntime._invoke_agent()` constructs:

```python
AIAgent(
    provider=persona.provider or self._default_provider,
    model=persona.model or self._default_model or "",
    api_mode=persona.api_mode,
    enabled_toolsets=effective_toolsets(persona),
    quiet_mode=True,
    skip_context_files=True,
    skip_memory=True,
    platform="agent_runtime",
    session_id=run.session_id,
    credential_pool=self._credential_pool,
    session_db=self._session_db,
    max_iterations=run.iteration_budget,
)
```

Gaps:

- no profile context;
- no skill manifest injection;
- no disabled toolsets or per-tool deny enforcement beyond role-filtered toolsets;
- no explicit session DB path under the bound profile;
- no profile readiness failure before model call.

Implementation must enter profile context before constructing `AIAgent`, but it must also audit import-time caches. Some modules, such as `hermes_state.py`, define module-level defaults from `get_hermes_home()` at import time. If profile-bound session DB isolation is required, pass an explicit profile-aware `SessionDB` instead of relying on already-imported module defaults.

### Tool policy gap

Current `agent_runtime/personas.py` defines:

- `ALLOWED_TOOLSETS_BY_ROLE`
- `PERSONA_BLOCKED_TOOLS`
- `PER_ROLE_TOOL_DENIES`
- `validate_toolsets()`
- `effective_toolsets()`

But the active `AIAgent` call only passes `enabled_toolsets=effective_toolsets(persona)`. Hermes `model_tools.get_tool_definitions()` filters by toolset, not by individual blocked tool names. `PER_ROLE_TOOL_DENIES` currently names tools (`write_file`, `patch`, `terminal`) while `validate_toolsets()` compares those against toolset names. That means some per-tool deny intent is not actually enforced by the current code path.

Stage 9 must not claim blocked tools are enforced until code exists.

Required implementation choices:

1. Add an agent/runtime-level `disabled_tools` or `blocked_tool_names` filter, or
2. Split risky toolsets into safer toolsets, or
3. Intercept tool calls in the Harness tool dispatch path and reject blocked tool names.

Minimum Stage 9 acceptance:

- PM cannot call `terminal`, `write_file`, or `patch`.
- QA cannot call `write_file` or `patch`.
- All personas cannot call `delegate_task`, `clarify`, `memory`, `send_message`, `cronjob`, or Kanban tools.
- Tests must inspect actual tool definitions available to the constructed fake/real agent, not only `validate_toolsets()` output.

### Skill loading primitives

Relevant code anchors:

- `agent/skill_commands.py::_load_skill_payload()` loads a skill by name/path through `tools.skills_tool.skill_view()` and returns loaded payload + skill dir.
- `agent/skill_commands.py::_build_skill_message()` formats skill content into a message payload and handles skill config injection/template expansion.
- Cron uses `tools.skills_tool.skill_view` to inject scheduled job skills.

Stage 9 should not duplicate all skill parsing logic. Create a thin `agent_runtime/skill_context.py` wrapper that uses existing skill loading in a profile context and returns bounded text blocks.

Implementation constraints:

- Skill content must be appended before the universal JSON-only Harness rules, or the final composed prompt must restate JSON-only after skills so skills cannot override output shape.
- Cap skill payload size per persona tick; use full content for small skills, but add a hard cap and missing/truncated metadata for very large skills.
- Missing skills should become profile readiness issues (`missing_skill`), not model-call crashes.

### Prompt composition gap

Current `agent_runtime/persona_runtime.py::build_system_prompt()` composes:

1. bundled role prompt;
2. universal Harness rules;
3. compact JSON schema.

Stage 9 adds shared SOUL overlay + skill manifests. To preserve prompt-cache and role authority, use this order:

1. role prompt (`agent_runtime/prompts/<role>.md`);
2. shared Harness SOUL overlay;
3. bounded persona skill context;
4. final universal Harness rules, including JSON-only/no-Kanban/no-direct-message rules;
5. compact JSON schema.

Task-specific context must stay in the user message from `render_context(ctx)`, not the system prompt.

### Readiness/status/snapshot gaps

Current `agent_runtime/status.py::build_status()` returns counts and next actions only.

Current `agent_runtime/snapshot.py::build_snapshot()` returns:

- `summary`
- `tasks`
- `agents`
- `runs`
- `incidents`
- `proofs`

Current `_agent_summary()` includes only:

```python
persona_id, display_name, role, model_configured, provider_configured, autonomy
```

Stage 9 must extend this with redaction-safe persona runtime metadata:

```json
{
  "persona_id": "qa",
  "display_name": "QA Agent",
  "role": "qa",
  "hermes_profile": "launcher-qa",
  "profile_readiness": "mcp_attention",
  "readiness_summary": "launcher_qa MCP server not configured or not discoverable",
  "skills": ["agent-runtime-harness", "flutter-ui-development", "launcher-stagec-mcp-screenshot"],
  "toolsets": ["file", "search", "terminal", "browser", "vision", "session_search"],
  "blocked_tools_count": 10,
  "model_configured": false,
  "provider_configured": false
}
```

Never include profile home paths if they contain local-only secrets/nonces; if paths are useful, include only profile name and high-level status.

Existing redaction test anchor:

- `tests/agent_runtime/test_snapshot.py::test_snapshot_contains_task_summary_and_no_raw_logs` should be expanded.
- `tests/agent_runtime/test_redaction.py` can hold deeper pattern tests.

### Launcher Mission Control gaps

Current Launcher code anchors:

- `lib/features/mission_control/data/mission_control_bridge.dart`
  - calls `hermes harness snapshot --json`;
  - maps harness `tasks` into Launcher `goals`;
  - currently hardcodes `auth_profile: stagec-smoke`, `auth_status: authenticated`, and Launcher QA statuses as ready/missing/not_run.
- `lib/features/mission_control/data/mission_control_snapshot.dart`
  - has no persona/profile/readiness model yet;
  - only top-level redaction guard is `_containsUnsafeKey()` on top-level keys. It does not recursively inspect nested maps/lists.
- `lib/features/mission_control/mission_control_page.dart`
  - shows runtime metrics, goal list/detail, proof drawer, Stage C QA, and actions;
  - has no persona profile panel yet.
- `lib/features/mission_control/state/mission_control_provider.dart`
  - defaults to CLI-backed repository/action repository.

Stage 9 Launcher implementation must:

1. Add a `MissionPersonaRuntime` / `MissionPersonaProfileStatus` model parsed from snapshot `agents`.
2. Extend redaction detection recursively so nested `auth`, `token`, `authorization`, `cookie`, `raw_log`, `raw_model_output`, `secret`, `password`, `api_key`, and `connection_string` keys are unsafe.
3. Stop hardcoding Stage C auth/MCP as authenticated/ready once Harness exposes persona readiness; map real readiness into UI.
4. Add a compact `Persona Runtime` panel:
   - persona;
   - profile;
   - readiness;
   - skills;
   - allowed toolsets;
   - blocked tools summary.
5. Add tests in `test/features/mission_control/mission_control_snapshot_test.dart`, `mission_control_bridge_test.dart`, and `mission_control_page_test.dart`.

### CLI bridge gaps

Current `hermes_cli/harness.py`:

- `harness init` seeds personas from `configured_personas(load_agent_runtime_config())` into `AgentStore`.
- `harness agents --json` returns `AgentStore().list_all() or default_personas()`.
- `harness tick --json` creates `GPTPersonaRuntime(default_provider=cfg.default_provider, default_model=cfg.default_model)`.

Stage 9 must decide whether `harness agents --json` should show configured personas even before `harness init`. Preferred:

- use `configured_personas(load_agent_runtime_config())` as the fallback instead of `default_personas()` so profile bindings show without requiring init;
- `harness init` remains idempotent and writes configured persona records.

### No-Kanban guardrails already exist

Existing test:

- `tests/agent_runtime/test_no_kanban_dependency.py` scans `agent_runtime/*.py` imports and fails if any import includes `kanban`.

Stage 9 should extend this test or add a companion test to assert:

- no `hermes_cli.kanban*` imports;
- no Kanban tool names in persona `enabled_toolsets`;
- no Stage 9 code opens or writes Kanban DB paths.

## Architecture decision

Add `hermes_profile` as a persona execution binding:

```yaml
agent_runtime:
  personas:
    pm:
      hermes_profile: pm
    dev:
      hermes_profile: gpt-launcher
    qa:
      hermes_profile: launcher-qa
    alice_supervisor:
      hermes_profile: alice
```

Rules:

1. `hermes_profile` selects the profile context used to instantiate `AIAgent` for that persona tick.
2. Persona model/provider/api_mode overrides still work. If unset, the persona inherits from its bound profile config.
3. Runtime task state remains in the shared Agent Runtime Harness store, not in the bound profile home.
4. Profile-bound OAuth/MCP/skills/config can power the tick, but `PERSONA_BLOCKED_TOOLS` and role denies still apply.
5. Mission Control displays profile identity and redaction-safe readiness only. It must never display tokens, auth JSON, API keys, connection strings, or raw environment values.

## Proposed default persona → profile mapping

For the Launcher Mission Control vertical slice:

- PM persona
  - Harness persona ID: `pm`
  - Hermes profile: `pm`
  - Model/provider: profile default; likely fast GPT/Codex/Spark-class model if configured.
  - Purpose: scope missions, acceptance criteria, non-goals, risk flags, and smallest next executable step.

- Dev persona
  - Harness persona ID: `dev`
  - Hermes profile: `gpt-launcher`
  - Model/provider: profile default; currently GPT/OpenAI-Codex-style Launcher implementation lane.
  - Purpose: inspect repo, plan, implement, run self-QA, attach implementation proof. Dev cannot approve its own work.

- QA persona
  - Harness persona ID: `qa`
  - Hermes profile: `launcher-qa` or `launcher-qa-direct` after live MCP readiness is proven.
  - Model/provider: profile default.
  - Purpose: independently verify tests, Stage C/Launcher MCP evidence, screenshots/video, redaction scans, and proof bundle sufficiency.

- Alice Supervisor persona
  - Harness persona ID: `alice_supervisor`
  - Hermes profile: `alice`
  - Purpose: high-level intervention detection, stale-run triage, readiness summary, Tony/Alice escalation. This profile may have gateway configured, but persona ticks still must not call `send_message`; external delivery is owned by the Harness/Alice layer after structured decision validation.

## SOUL setup plan

Create Harness-specific profile SOULs or SOUL overlays that are short, deterministic, and role-scoped. Avoid copying giant Kanban SOULs wholesale.

### Shared Harness SOUL overlay

Every profile-bound Harness persona should receive a shared overlay with these rules:

```markdown
# Agent Runtime Harness Persona Rules

You are running inside Tony's Agent Runtime Harness / Mission Control brainstem.
Return exactly one AgentDecision JSON object. Do not produce prose outside JSON.
You do not own orchestration; the Harness owns state, transitions, proof gates, and retries.
Work in the basic flow: Goal/Mission → PM → Dev → QA → proof gate → Ready or Intervention.
Do not use Kanban vocabulary, create Kanban cards, or mutate Kanban state.
Do not message Tony directly. Escalate by returning REQUEST_HUMAN or BLOCK with exact intervention details.
Do not write memory or schedule cron jobs.
Never claim proof you did not obtain from the Harness context or allowed tools.
Enterprise-grade means tested, redaction-safe, maintainable, reliable, and launch/revenue aligned.
```

Implementation options:

1. Preferred: add `agent_runtime/prompts/shared_harness_overlay.md` and compose it after the role prompt.
2. Optional later: generate profile-local `SOUL.harness.md` files for operator readability, but do not require manual profile edits for correctness.

### PM SOUL

PM profile should optimize for scoping and risk reduction:

- Convert Tony/Alice mission text into the smallest executable goal.
- Produce acceptance criteria, non-goals, affected repos, visual-proof requirement, and risk flags.
- Prefer one next implementation slice, not a broad project plan.
- Block if the mission needs Tony's product decision, credentials, legal/brand approval, or destructive action authorization.
- Never patch code, run shell commands, or approve final proof.

Required skills for PM profile:

- `agent-runtime-harness`
- `writing-plans`
- `obsidian` only if/when mission context must be written to brain docs; otherwise do not load by default.
- `session_search` toolset for recalling prior decisions.

### Dev SOUL

Dev profile should optimize for implementation velocity with self-QA:

- Inspect repo instructions and nearby docs before editing.
- Implement only the current scoped stage/goal.
- Keep business logic out of widgets; preserve Launcher architecture and polished UI.
- Run targeted tests/analyze/build checks relevant to the change.
- Attach proof: commands, exit codes, changed paths, logs/artifacts, known untested scope.
- Request QA only after self-QA is green or a true blocker is documented.
- Never approve its own work as ready.

Required skills for Dev profile:

- `flutter-ui-development` for Launcher UI/Riverpod/MCP work.
- `systematic-debugging` for failures.
- `test-driven-development` for behavior changes and regressions.
- `requesting-code-review` before readiness handoff.
- `agent-runtime-harness` for structured decision/output rules.

Dev optional skills by mission class:

- `hermes-agent` when editing Hermes/Harness itself.
- `launcher-stagec-mcp-screenshot` when proof requires Stage C Launcher screenshots.
- `eternia-local-gates` when backend/local stack proof is in scope.

### QA SOUL

QA profile should optimize for independent proof and redaction safety:

- Verify Dev's evidence; do not trust Dev claims blindly.
- Re-run targeted tests where practical.
- For UI/Launcher work, use semantic MCP/Stage C tools over brittle coordinates.
- Capture screenshot/video evidence when required.
- Run redaction scans on logs/screenshots/artifacts before approving.
- Report `approved` only when required proof exists and is independently reproducible.
- Report `needs_fixes` with exact failing command, artifact path, observed UI issue, or missing proof.
- Never patch code; QA may request corrections but not implement them.

Required skills for QA profile:

- `flutter-ui-development`
- `launcher-stagec-mcp-screenshot`
- `systematic-debugging`
- `requesting-code-review` for evidence/review expectations
- `agent-runtime-harness`

QA MCP/profile requirements:

- `launcher_qa` MCP configured and testable.
- Stage C smoke credential path available through existing profile/env setup.
- Browser/vision toolsets available only as needed.
- Redaction scanner path documented and tested.

### Alice Supervisor SOUL

Alice Supervisor should optimize for protecting Tony's time:

- Summarize exact mission status and next intervention.
- Detect stale runs, missing proof, profile readiness failures, and repeated invalid JSON incidents.
- Escalate only actionable gaps: missing credentials, stale MCP binary, failed auth, required Tony product decision, unavailable provider, or serious quality gap.
- Never patch code, approve Dev work, or run broad tests.
- Never directly message from inside a tick; return structured intervention for the outer Alice/gateway layer.

Required skills:

- `agent-runtime-harness`
- `session_search`
- `obsidian` only for explicit brain-update missions.

## Skill loading strategy

Do not rely on free-form profile memory to make personas efficient. Give each persona a deterministic skill manifest:

```yaml
agent_runtime:
  personas:
    dev:
      hermes_profile: gpt-launcher
      skills:
        - agent-runtime-harness
        - flutter-ui-development
        - systematic-debugging
        - test-driven-development
        - requesting-code-review
    qa:
      hermes_profile: launcher-qa
      skills:
        - agent-runtime-harness
        - flutter-ui-development
        - launcher-stagec-mcp-screenshot
        - systematic-debugging
        - requesting-code-review
```

Runtime behavior:

1. Resolve persona role prompt.
2. Add shared Harness SOUL overlay.
3. Add profile SOUL or selected profile context if safe.
4. Load only the persona's configured skills into the tick prompt/context.
5. Render task context in the user message, not in the system prompt, to preserve prompt caching.
6. Keep `skip_memory=True` unless a future stage adds explicit read-only mission memory context. Personas must not write persistent memory.

Guardrails:

- Skills can teach procedures, but they must not override Harness JSON-only output.
- If a skill conflicts with role policy, Harness role policy wins.
- If a skill instructs messaging, memory writes, cron, delegation, or Kanban side effects, those tools remain blocked.
- Skills loaded into QA should bias toward verification, not implementation.

## Profile execution design

Add a small profile context adapter rather than subprocessing `hermes -p <profile> chat`.

Proposed module:

```text
agent_runtime/
  profile_context.py
```

Responsibilities:

- Resolve named profile path and config.
- Temporarily apply profile-local Hermes home/config/auth context for the duration of one persona tick.
- Load profile model/provider/MCP/tool config through normal Hermes config loaders.
- Restore the original process context after the tick, even on exception.
- Expose redaction-safe readiness info for snapshots.

Do not change the Agent Runtime Harness store root inside the profile context. Use the shared-root rule from Stage 6.

Pseudo-contract:

```python
@dataclass(slots=True)
class PersonaProfileBinding:
    persona_id: str
    hermes_profile: str | None
    profile_home: str | None
    readiness: str      # ready | missing_profile | config_error | auth_attention | mcp_attention
    redacted_summary: dict[str, Any]

@contextmanager
def persona_profile_context(binding: PersonaProfileBinding):
    ...
```

## Data model changes

Extend `AgentPersona`:

```python
@dataclass(slots=True)
class AgentPersona:
    id: str
    display_name: str
    role: str
    model: str | None
    provider: str | None
    api_mode: str | None
    toolsets: list[str]
    system_prompt_path: str
    autonomy: str = "review"
    hermes_profile: str | None = None
    skills: list[str] = field(default_factory=list)
    soul_overlay_path: str | None = None
    schema_version: int = 1
```

Do **not** bump `schema_version` in the first implementation pass. Current `agent_runtime/serde.py::upgrade()` rejects versions other than `1`, so a version bump would break existing store reads unless a formal upgrader lands in the same change.

Backward compatibility:

- Existing persona JSON without `hermes_profile`, `skills`, or `soul_overlay_path` still loads.
- Snapshot JSON includes the fields only as redaction-safe labels.
- CLI `harness agents --json` includes `hermes_profile` and `skills`, never credential values.

## Config shape

Recommended local Launcher Mission Control config:

```yaml
agent_runtime:
  default_api_mode: codex_responses
  max_actions_per_tick: 1
  personas:
    pm:
      hermes_profile: pm
      skills: [agent-runtime-harness, writing-plans]
      toolsets: [file, session_search, todo]
    dev:
      hermes_profile: gpt-launcher
      skills:
        - agent-runtime-harness
        - flutter-ui-development
        - systematic-debugging
        - test-driven-development
        - requesting-code-review
      toolsets: [file, search, terminal, session_search, code_execution]
    qa:
      hermes_profile: launcher-qa
      skills:
        - agent-runtime-harness
        - flutter-ui-development
        - launcher-stagec-mcp-screenshot
        - systematic-debugging
        - requesting-code-review
      toolsets: [file, search, terminal, browser, vision, session_search]
    alice_supervisor:
      hermes_profile: alice
      skills: [agent-runtime-harness]
      toolsets: [file, search, session_search, todo]
```

Optional later mapping after `launcher-qa-direct` is proven fresher/more reliable than `launcher-qa`:

```yaml
agent_runtime:
  personas:
    qa:
      hermes_profile: launcher-qa-direct
```

Do not silently switch QA profiles. Mission Control should show the active binding and profile readiness.

## Mission Control visibility additions

Launcher Mission Control should display profile-bound runtime status:

- `PM Agent · profile: pm · ready`
- `Dev Agent · profile: gpt-launcher · ready`
- `QA Agent · profile: launcher-qa · MCP attention`
- `Alice Supervisor · profile: alice · ready`

Detail panel should show:

- persona ID;
- bound profile name;
- redaction-safe model/provider label if available;
- allowed toolsets;
- loaded skills;
- blocked tools summary;
- readiness/intervention message.

Never display:

- OAuth tokens;
- API keys;
- auth JSON values;
- raw env vars;
- connection strings;
- MCP local control paths containing nonces or secrets.

## Implementation stages inside Stage 9

### 9.1 — Data model + config parsing

Objective: Add profile/skills fields without changing runtime behavior.

Files:

- Modify: `agent_runtime/models.py`
- Modify: `agent_runtime/personas.py`
- Modify: `agent_runtime/config.py`
- Modify: `hermes_cli/harness.py`
- Tests: `tests/agent_runtime/test_config.py`, `tests/agent_runtime/test_personas.py`, `tests/hermes_cli/test_harness_cli.py`

Tasks:

1. Add failing tests for `hermes_profile`, `skills`, and `soul_overlay_path` parsing in `tests/agent_runtime/test_config.py`.
2. Add failing test that legacy persona JSON with no new fields still loads through `AgentStore` / `serde.from_jsonable`.
3. Add fields to `AgentPersona` with backward-compatible defaults and keep `schema_version=1` unless `serde.upgrade()` is extended in the same patch.
4. Extend `configured_personas()` to merge new fields.
5. Ensure role toolset filtering still applies.
6. Change `hermes_cli/harness.py::_cmd_agents` fallback from `default_personas()` to `configured_personas(load_agent_runtime_config())` so configured profile labels show before `harness init`.
7. Verify `hermes harness agents --json` includes labels and excludes secrets.

Acceptance:

- Existing persona JSON still loads.
- New config keys round-trip in JSON output.
- `harness agents --json` shows configured profile/skill labels even before init.
- No live profile switching yet.

### 9.2 — Enforce per-persona blocked tools

Objective: Make role policy real before profile-bound OAuth/MCP power is enabled.

Files:

- Modify: `agent_runtime/personas.py`
- Modify: `agent_runtime/persona_runtime.py`
- Possibly modify: `model_tools.py`, `agent/agent_init.py`, or add `agent_runtime/tool_policy.py`
- Tests: `tests/agent_runtime/test_persona_tool_policy.py`, `tests/agent_runtime/test_no_kanban_dependency.py`

Tasks:

1. Add failing tests that PM's actual agent tool schema excludes `terminal`, `write_file`, and `patch`.
2. Add failing tests that QA's actual agent tool schema excludes `write_file` and `patch` while preserving read/search/browser/vision tools.
3. Add failing tests that every persona excludes `delegate_task`, `clarify`, `memory`, `send_message`, `cronjob`, and Kanban tools even when a profile/skill requests them.
4. Implement the smallest policy hook: either add a blocked tool name filter before tool schemas are sent to the model, or intercept tool dispatch for blocked names and record an incident.
5. Extend no-Kanban tests to cover tool names and imports.

Acceptance:

- Role-denied tool names are absent from the persona's actual available tool schema or rejected before execution.
- Profile SOULs/skills cannot re-enable blocked tools.
- No Kanban imports or tool side effects enter `agent_runtime`.

### 9.3 — Profile context adapter

Objective: Run a persona tick under a named Hermes profile while restoring the original profile afterward.

Files:

- Create: `agent_runtime/profile_context.py`
- Modify: `agent_runtime/persona_runtime.py`
- Tests: `tests/agent_runtime/test_profile_context.py`, `tests/agent_runtime/test_persona_runtime_profile_binding.py`

Tasks:

1. Add fake profile resolver tests for existing/missing profile.
2. Add context manager that temporarily applies profile home/config context with `set_hermes_home_override()`.
3. Add temporary env overlay for subprocesses: `HERMES_HOME`, optional profile-local `HOME`, and pinned `HERMES_AGENT_RUNTIME_ROOT`.
4. Add restoration tests for success and exception paths, including env restoration.
5. Wire `GPTPersonaRuntime` to enter profile context when `persona.hermes_profile` is set.
6. Keep store root shared and prove with a test that `paths.store_root()`/`snapshot_path()` do not switch to the persona profile.
7. Add import-order regression around `SessionDB` or pass an explicit session DB if profile-scoped sessions are required.

Acceptance:

- Persona tick uses bound profile config in tests.
- Original Alice/current process profile is restored after tick.
- Missing profile opens a safe incident and leaves task state unchanged.

### 9.4 — Skill manifest loading

Objective: Load explicit persona skills efficiently without broad profile context pollution.

Files:

- Modify: `agent_runtime/persona_runtime.py`
- Modify: `agent_runtime/personas.py`
- Create or modify: `agent_runtime/skill_context.py`
- Tests: `tests/agent_runtime/test_persona_skill_context.py`

Tasks:

1. Add tests proving configured skill names are included in persona system/user context.
2. Add tests proving skill content cannot remove JSON-only output rules.
3. Add missing-skill behavior: incident or warning depending on strictness config.
4. Keep loaded skill list visible in snapshot/agents output.
5. Cap skill content to avoid prompt bloat; prefer summaries or frontmatter descriptions where possible.

Acceptance:

- PM/Dev/QA load only their configured skills.
- Missing skills are visible as readiness issues.
- Tool policy remains authoritative over skill text.

### 9.5 — SOUL overlays

Objective: Give personas concise role-specific soul while preserving deterministic structured output.

Files:

- Create: `agent_runtime/prompts/shared_harness_overlay.md`
- Modify: `agent_runtime/prompts/pm.md`
- Modify: `agent_runtime/prompts/dev.md`
- Modify: `agent_runtime/prompts/qa.md`
- Modify: `agent_runtime/prompts/alice_supervisor.md`
- Tests: `tests/agent_runtime/test_persona_prompts.py`

Tasks:

1. Add tests for prompt composition order.
2. Add shared Harness overlay text.
3. Trim role prompts if they duplicate overlay rules.
4. Add role-specific mission-loop rules from the SOUL plan above.
5. Assert prompt contains JSON-only and no-Kanban rules.

Acceptance:

- Prompts are short enough for efficient repeated ticks.
- All role prompts include no-Kanban, no-direct-message, no-memory-write rules.
- Dev and QA prompts clearly separate self-QA from independent QA approval.

### 9.6 — Tool/MCP/OAuth readiness checks

Objective: Before running a tick, report whether the bound profile has the required profile/config/tool/MCP readiness.

Files:

- Create: `agent_runtime/profile_readiness.py`
- Modify: `agent_runtime/status.py`
- Modify: `agent_runtime/snapshot.py`
- Tests: `tests/agent_runtime/test_profile_readiness.py`, `tests/agent_runtime/test_snapshot_redaction.py`

Tasks:

1. Add readiness schema: `ready`, `missing_profile`, `config_error`, `auth_attention`, `mcp_attention`, `missing_skill`.
2. Check profile existence and config load.
3. Check required skill names exist.
4. For QA profile, check MCP config presence for `launcher_qa`; do not start the MCP server during normal status.
5. Redact all secrets and local nonces.
6. Add snapshot fields for Mission Control.

Acceptance:

- `hermes harness status --json` and `snapshot --json` show persona profile readiness.
- Missing QA MCP config becomes a clear intervention, not a failed mission.
- Redaction tests pass.

### 9.7 — Launcher Mission Control display

Objective: Surface persona profile bindings and readiness in Launcher without adding workflow complexity.

Files:

- Launcher repo: `lib/features/mission_control/data/mission_control_snapshot.dart`
- Launcher repo: `lib/features/mission_control/mission_control_page.dart`
- Launcher tests: `test/features/mission_control/*`

Tasks:

1. Extend Launcher snapshot model to parse `agents/personas` profile labels.
2. Add a compact `Persona Runtime` panel.
3. Add detail rows for profile, skills, toolsets, readiness.
4. Add intervention display for missing profile/MCP/skill/auth readiness.
5. Add widget tests for redaction and readiness display.

Acceptance:

- Tony can see which profile each persona will use before pressing Run Next Tick.
- UI shows QA MCP readiness issues explicitly.
- UI remains a mission cockpit, not a profile admin console.

### 9.8 — Live smoke and rollout

Objective: Prove one safe live mission can route PM → Dev → QA with profile-bound personas.

Commands:

```bash
hermes harness agents --json
hermes harness status --json
hermes harness task create --title "Profile-bound smoke" --description "Safe docs-only runtime smoke" --requested-by launcher --json
hermes harness tick --json
hermes harness snapshot --json
```

Rollout rules:

- First smoke should be docs-only or read-only to avoid destructive edits.
- Use a temporary runtime root for early integration smoke.
- Do not claim end-to-end readiness until profile readiness, one PM tick, one Dev planning tick, and one QA plan-review tick have passed with structured decisions.
- If QA MCP readiness is missing, classify as profile setup intervention, not Harness failure.

Acceptance:

- Profile-bound persona labels appear in CLI and Launcher.
- PM/Dev/QA ticks use the intended profiles.
- The shared runtime store remains consistent.
- No Kanban side effects occur.

## Final implementation-ready handoff — no remaining design gaps

This section resolves the remaining implementation choices so an implementation agent should not need to ask product/architecture questions before coding Stage 9.

### Fixed decisions

- **Schema version:** keep every persisted Harness model at `schema_version = 1` in this stage. Add nullable/default fields only. Do not introduce v2 until `agent_runtime/serde.py::upgrade()` has explicit migration tests.
- **Profile context style:** use in-process context scoping via `set_hermes_home_override()` plus a temporary `os.environ` overlay. Do not shell out to `hermes -p <profile> chat` from persona ticks.
- **Shared store root:** pin `HERMES_AGENT_RUNTIME_ROOT` before profile entry in CLI/ticker/runtime paths. The Harness mission store is global to Mission Control, never profile-local.
- **Tool policy enforcement:** implement a **tool-name deny list** at the AIAgent/tool-schema boundary first. Toolset filtering alone is insufficient because `PER_ROLE_TOOL_DENIES` contains tool names, not toolset names.
- **Skill loading:** reuse `agent.skill_commands._load_skill_payload()` and `_build_skill_message()` from inside a small Harness wrapper. Do not duplicate skill frontmatter/config/template parsing.
- **Readiness strictness:** missing profile and missing configured skill are preflight blockers for that persona tick. Missing QA MCP config is `mcp_attention` and should block QA execution but not corrupt task state.
- **Launcher mapping:** Harness snapshot `agents` is the single source of truth for persona profile/readiness. Launcher must stop inventing authenticated/ready values after Stage 9 lands.

### Exact Python implementation contracts

#### `agent_runtime/models.py`

Add these fields to `AgentPersona` after `autonomy` and before `schema_version`:

```python
    hermes_profile: str | None = None
    skills: list[str] = field(default_factory=list)
    soul_overlay_path: str | None = None
    required_mcp_servers: list[str] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
```

Notes:

- `models.py` already imports `field` and `Any`, so no import churn is needed.
- `required_mcp_servers` makes QA MCP readiness data-driven instead of hardcoding `launcher_qa` forever.
- `readiness` is safe persisted metadata only; it must not contain paths, env values, tokens, or raw config.

#### `agent_runtime/config.py`

Extend `configured_personas()` with simple typed merge helpers:

```python
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]

# inside overrides loop, after api_mode merge
p.hermes_profile = overrides.get("hermes_profile", p.hermes_profile)
p.soul_overlay_path = overrides.get("soul_overlay_path", p.soul_overlay_path)
if "skills" in overrides:
    p.skills = _string_list(overrides["skills"])
if "required_mcp_servers" in overrides:
    p.required_mcp_servers = _string_list(overrides["required_mcp_servers"])
```

Validation expectations:

- Unknown persona IDs remain ignored, matching current behavior.
- Invalid `toolsets` continue to be filtered through `validate_toolsets()`.
- Invalid/non-list `skills` and `required_mcp_servers` become `[]`, not crashes.

#### `agent_runtime/personas.py`

Replace the misleading `validate_toolsets()` deny comparison with explicit toolset-only filtering, then add tool-name helpers:

```python
def validate_toolsets(role: AgentRole | str, configured: list[str]) -> list[str]:
    resolved_role = role if isinstance(role, AgentRole) else AgentRole(role)
    allowed = ALLOWED_TOOLSETS_BY_ROLE[resolved_role]
    return [toolset for toolset in configured if toolset in allowed]


def blocked_tool_names(persona: AgentPersona) -> frozenset[str]:
    role = role_from_persona(persona)
    return PERSONA_BLOCKED_TOOLS | PER_ROLE_TOOL_DENIES[role]
```

This preserves the current public API and makes the per-tool deny model explicit.

#### `model_tools.py` / `agent/agent_init.py` / `run_agent.py`

Preferred minimal API addition:

```python
def get_tool_definitions(
    enabled_toolsets: List[str] = None,
    disabled_toolsets: List[str] = None,
    quiet_mode: bool = False,
    blocked_tool_names: List[str] | None = None,
) -> List[Dict[str, Any]]:
```

Implementation requirements:

- Add `frozenset(blocked_tool_names or [])` to the quiet-mode cache key.
- After dynamic schema rebuilds, filter final schemas:

```python
if blocked_tool_names:
    blocked = set(blocked_tool_names)
    filtered_tools = [
        tool for tool in filtered_tools
        if tool.get("function", {}).get("name") not in blocked
    ]
    available_tool_names = {tool["function"]["name"] for tool in filtered_tools}
```

- Thread a matching optional `blocked_tool_names` parameter through `agent/agent_init.py::init_agent()` and `AIAgent.__init__` in `run_agent.py`.
- Store it on the agent only if needed for debugging; do not expose it to prompts as authority.
- `agent_runtime/persona_runtime.py` must pass `blocked_tool_names=list(blocked_tool_names(persona))` to the factory.

Fallback if changing `AIAgent` API is too broad:

- Add `agent_runtime/tool_policy.py::filter_tool_definitions_for_persona(persona, tools)` and use a custom `agent_factory` wrapper only in Harness. This is less ideal because dispatch can still be reachable if a future code path bypasses the schema filter; use the API addition if possible.

#### `agent_runtime/profile_context.py`

Create this module with this public shape:

```python
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Iterator

from hermes_constants import set_hermes_home_override, reset_hermes_home_override
from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists


@dataclass(slots=True)
class PersonaProfileBinding:
    persona_id: str
    hermes_profile: str | None
    profile_home: Path | None
    readiness: str = "ready"
    summary: str = "ready"
    metadata: dict[str, Any] = field(default_factory=dict)


def resolve_persona_profile(persona) -> PersonaProfileBinding:
    if not persona.hermes_profile:
        return PersonaProfileBinding(
            persona_id=persona.id,
            hermes_profile=None,
            profile_home=None,
            readiness="ready",
            summary="inherits active Harness profile",
        )
    name = normalize_profile_name(persona.hermes_profile)
    if not profile_exists(name):
        return PersonaProfileBinding(
            persona_id=persona.id,
            hermes_profile=name,
            profile_home=None,
            readiness="missing_profile",
            summary=f"Hermes profile '{name}' does not exist",
        )
    home = get_profile_dir(name)
    return PersonaProfileBinding(
        persona_id=persona.id,
        hermes_profile=name,
        profile_home=home,
        readiness="ready",
        summary="profile exists",
    )


@contextmanager
def persona_profile_context(binding: PersonaProfileBinding, *, runtime_root: Path | None = None) -> Iterator[None]:
    if binding.profile_home is None:
        yield
        return
    previous_env = {
        "HERMES_HOME": os.environ.get("HERMES_HOME"),
        "HOME": os.environ.get("HOME"),
        "HERMES_AGENT_RUNTIME_ROOT": os.environ.get("HERMES_AGENT_RUNTIME_ROOT"),
    }
    token = set_hermes_home_override(binding.profile_home)
    try:
        os.environ["HERMES_HOME"] = str(binding.profile_home)
        profile_home = binding.profile_home / "home"
        if profile_home.exists():
            os.environ["HOME"] = str(profile_home)
        if runtime_root is not None:
            os.environ["HERMES_AGENT_RUNTIME_ROOT"] = str(runtime_root)
        yield
    finally:
        reset_hermes_home_override(token)
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
```

Implementation note: tests may monkeypatch `profile_exists/get_profile_dir` or create temporary profile roots; do not require Tony's real profiles in unit tests.

#### `agent_runtime/skill_context.py`

Create a small wrapper:

```python
from dataclasses import dataclass, field

MAX_SKILL_CHARS_PER_PERSONA = 24_000

@dataclass(slots=True)
class PersonaSkillContext:
    text: str
    loaded: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)


def load_persona_skill_context(skill_names: list[str], *, task_id: str | None = None) -> PersonaSkillContext:
    from agent.skill_commands import _build_skill_message, _load_skill_payload
    ...
```

Rules for implementation:

- For each skill name, call `_load_skill_payload(name, task_id=task_id)`.
- Missing payload: append to `missing` and continue.
- Build message text using `_build_skill_message(...)`; if exact signature differs, inspect `agent/skill_commands.py` and adapt, but keep all skill parsing centralized there.
- Accumulate text until `MAX_SKILL_CHARS_PER_PERSONA`; record any truncated skill names.
- Return plain markdown text; `build_system_prompt()` composes it before final universal rules.

#### `agent_runtime/persona_runtime.py`

Update imports:

```python
from .paths import store_root
from .personas import blocked_tool_names, effective_toolsets, load_bundled_prompt, role_from_persona
from .profile_context import persona_profile_context, resolve_persona_profile
from .skill_context import load_persona_skill_context
```

Update `_invoke_agent()` flow:

```python
binding = resolve_persona_profile(persona)
if binding.readiness == "missing_profile":
    raise DecisionPayloadInvalid(binding.summary)
with persona_profile_context(binding, runtime_root=store_root()):
    agent = factory(
        ...,
        enabled_toolsets=effective_toolsets(persona),
        blocked_tool_names=list(blocked_tool_names(persona)),
        ...,
    )
    result = agent.run_conversation(
        user_message=render_context(ctx),
        system_message=build_system_prompt(persona, task_id=run.id),
        task_id=run.id,
    )
```

If the implementation chooses to convert missing profile into an `Incident` instead of `DecisionPayloadInvalid`, make that conversion in `TickEngine`, not by letting the model call run.

Update prompt signature:

```python
def build_system_prompt(persona: AgentPersona, *, task_id: str | None = None) -> str:
```

Composition order:

1. bundled role prompt;
2. `agent_runtime/prompts/shared_harness_overlay.md` if present;
3. `persona.soul_overlay_path` if set and safely readable under the Hermes home/profile root or repo prompts dir;
4. skill context text;
5. final universal Harness rules;
6. compact JSON schema.

The final universal rules and schema must remain the last two blocks.

#### `agent_runtime/profile_readiness.py`

Create explicit status helpers:

```python
READINESS_READY = "ready"
READINESS_MISSING_PROFILE = "missing_profile"
READINESS_CONFIG_ERROR = "config_error"
READINESS_AUTH_ATTENTION = "auth_attention"
READINESS_MCP_ATTENTION = "mcp_attention"
READINESS_MISSING_SKILL = "missing_skill"

SAFE_READINESS_KEYS = {
    "readiness",
    "summary",
    "hermes_profile",
    "skills",
    "missing_skills",
    "required_mcp_servers",
    "missing_mcp_servers",
}
```

Minimum behavior:

- `profile_readiness_for_persona(persona) -> dict[str, Any]` returns only safe labels.
- Profile missing: `missing_profile`.
- Config read exception: `config_error` with exception class name only, not paths/secrets.
- Missing skill: `missing_skill` with names only.
- Missing required MCP server: `mcp_attention` with server names only.
- A persona with multiple issues should use the most severe readiness and include all safe issue labels.

MCP check implementation:

- Read the profile's `config.yaml` under `persona_profile_context`.
- Detect server keys from the existing MCP config shape; if uncertain, support both `mcp.servers.<name>` and `mcp_servers.<name>` and document any discovered actual shape in tests.
- Do not start MCP processes in status/snapshot.

#### `agent_runtime/snapshot.py` and `status.py`

`_agent_summary(agent)` should include exactly these Stage 9-safe fields:

```python
readiness = profile_readiness_for_persona(agent)
return {
    "persona_id": agent.id,
    "display_name": agent.display_name,
    "role": agent.role,
    "hermes_profile": agent.hermes_profile,
    "profile_readiness": readiness["readiness"],
    "readiness_summary": readiness["summary"],
    "skills": list(agent.skills),
    "missing_skills": readiness.get("missing_skills", []),
    "required_mcp_servers": list(agent.required_mcp_servers),
    "missing_mcp_servers": readiness.get("missing_mcp_servers", []),
    "toolsets": effective_toolsets(agent),
    "blocked_tools_count": len(blocked_tool_names(agent)),
    "model_configured": bool(agent.model),
    "provider_configured": bool(agent.provider),
    "autonomy": agent.autonomy,
}
```

`build_status()` should add a `personas`/`agents` array with the same readiness subset or call a shared summary helper to avoid drift.

#### `hermes_cli/harness.py`

Make these exact CLI changes:

```python
def _configured_or_stored_personas():
    return AgentStore().list_all() or configured_personas(load_agent_runtime_config())
```

Use it in `_cmd_agents()`.

In `_cmd_tick()`:

```python
root = paths.store_root()
os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(root))
```

Import `agent_runtime.paths as paths`. If tests dislike mutating process env, move this pinning into `GPTPersonaRuntime`/`profile_context` and assert restoration.

### Exact Dart implementation contracts

#### `mission_control_snapshot.dart`

Add enums/classes:

```dart
enum MissionPersonaReadiness {
  ready,
  missingProfile,
  configError,
  authAttention,
  mcpAttention,
  missingSkill,
  unknown,
}

class MissionPersonaRuntime {
  const MissionPersonaRuntime({
    required this.personaId,
    required this.displayName,
    required this.role,
    required this.hermesProfile,
    required this.readiness,
    required this.readinessSummary,
    required this.skills,
    required this.toolsets,
    required this.blockedToolsCount,
  });
  ...
}
```

Add `final List<MissionPersonaRuntime> personas;` to `MissionControlSnapshot`, parse it from `json['personas']` or `json['agents']`, and pass it through `_mapHarnessSnapshot()` as:

```dart
'personas': _list(raw['agents']),
```

Recursive redaction replacement:

```dart
bool _containsUnsafeValue(Object? value) {
  const unsafeKeys = <String>{
    'token', 'access_token', 'refresh_token', 'authorization', 'cookie',
    'raw_log', 'raw_model_output', 'secret', 'password', 'api_key',
    'connection_string', 'credential', 'nonce', 'control_path',
  };
  if (value is Map) {
    for (final entry in value.entries) {
      final key = entry.key.toString().toLowerCase();
      if (unsafeKeys.any((unsafe) => key.contains(unsafe))) return true;
      if (_containsUnsafeValue(entry.value)) return true;
    }
  }
  if (value is List) {
    return value.any(_containsUnsafeValue);
  }
  return false;
}
```

Set `hasUnsafeFields: _containsUnsafeValue(json)`.

#### `mission_control_bridge.dart`

Mapping rules:

- Preserve raw Harness `agents` into the Launcher snapshot under `personas`.
- Remove hardcoded `auth_status: authenticated` from the final design. Until Harness exposes an explicit auth summary, use `unknown` unless every persona readiness is `ready` and no `auth_attention` exists.
- Derive `launcher_qa.mcp_status` from QA persona readiness:
  - `ready` → `ready`
  - `mcp_attention` → `missing`
  - `missing_profile` / `config_error` → `failed`
  - otherwise → `unknown`

#### `mission_control_page.dart`

Add a compact `Persona Runtime` card above or beside Stage C QA:

- Header: `Persona Runtime`.
- One row per persona: `{displayName} · profile: {hermesProfile ?? "active"} · {readinessLabel}`.
- Expanded/detail text can show skills/toolsets as comma-separated chips/text.
- If any persona readiness is not `ready`, show the intervention color/warning style already used for mission interventions.
- Do not add profile editing controls.

### Exact first implementation sequence

Implement and commit in this order to keep Stage 9 reviewable:

1. **Harness model/config tests and fields**
   - Files: `tests/agent_runtime/test_config.py`, `tests/agent_runtime/test_models_serde.py`, `agent_runtime/models.py`, `agent_runtime/config.py`.
   - Run: `bash scripts/run_tests.sh tests/agent_runtime/test_config.py tests/agent_runtime/test_models_serde.py`.

2. **Tool-name policy enforcement**
   - Files: `tests/agent_runtime/test_persona_tool_policy.py`, `agent_runtime/personas.py`, `model_tools.py`, `agent/agent_init.py`, `run_agent.py`, `agent_runtime/persona_runtime.py`.
   - Run: `bash scripts/run_tests.sh tests/agent_runtime/test_persona_tool_policy.py tests/agent_runtime/test_persona_runtime_fake.py`.

3. **Profile context adapter**
   - Files: `tests/agent_runtime/test_profile_context.py`, `tests/agent_runtime/test_persona_runtime_profile_binding.py`, `agent_runtime/profile_context.py`, `agent_runtime/persona_runtime.py`.
   - Run: `bash scripts/run_tests.sh tests/agent_runtime/test_profile_context.py tests/agent_runtime/test_persona_runtime_profile_binding.py`.

4. **Skill context and prompt overlays**
   - Files: `tests/agent_runtime/test_persona_skill_context.py`, `tests/agent_runtime/test_persona_prompts.py`, `agent_runtime/skill_context.py`, `agent_runtime/persona_runtime.py`, `agent_runtime/prompts/shared_harness_overlay.md`.
   - Run: `bash scripts/run_tests.sh tests/agent_runtime/test_persona_skill_context.py tests/agent_runtime/test_persona_prompts.py`.

5. **Readiness/status/snapshot/CLI**
   - Files: `tests/agent_runtime/test_profile_readiness.py`, `tests/agent_runtime/test_snapshot_redaction.py`, `tests/hermes_cli/test_harness_cli.py`, `agent_runtime/profile_readiness.py`, `agent_runtime/snapshot.py`, `agent_runtime/status.py`, `hermes_cli/harness.py`.
   - Run: `bash scripts/run_tests.sh tests/agent_runtime/test_profile_readiness.py tests/agent_runtime/test_snapshot_redaction.py tests/hermes_cli/test_harness_cli.py`.

6. **Launcher Mission Control display**
   - Files: Launcher `mission_control_snapshot.dart`, `mission_control_bridge.dart`, `mission_control_page.dart`, tests under `test/features/mission_control/`.
   - Run from Launcher repo: `flutter test test/features/mission_control --reporter=compact && flutter analyze lib/features/mission_control test/features/mission_control`.

7. **Integration smoke**
   - Use a temporary runtime root and a docs-only mission first.
   - Run the Stage 9 live smoke commands from section 9.8.
   - Only then remove the temporary root for Tony's real Mission Control runtime.

### Copy-pasteable unit test seeds

Add or adapt these seeds during implementation.

#### `tests/agent_runtime/test_config.py`

```python
def test_config_merges_profile_skills_and_readiness_fields(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "agent_runtime:\n"
        "  personas:\n"
        "    qa:\n"
        "      hermes_profile: launcher-qa\n"
        "      skills: [agent-runtime-harness, launcher-stagec-mcp-screenshot]\n"
        "      soul_overlay_path: prompts/qa_harness.md\n"
        "      required_mcp_servers: [launcher_qa]\n",
        encoding="utf-8",
    )
    cfg = load_agent_runtime_config(p)
    qa = next(persona for persona in configured_personas(cfg) if persona.id == "qa")
    assert qa.hermes_profile == "launcher-qa"
    assert qa.skills == ["agent-runtime-harness", "launcher-stagec-mcp-screenshot"]
    assert qa.soul_overlay_path == "prompts/qa_harness.md"
    assert qa.required_mcp_servers == ["launcher_qa"]
```

#### `tests/agent_runtime/test_models_serde.py`

```python
def test_agent_persona_legacy_json_defaults_new_stage9_fields():
    raw = {
        "id": "qa",
        "display_name": "QA Agent",
        "role": "qa",
        "model": None,
        "provider": None,
        "api_mode": "codex_responses",
        "toolsets": ["file"],
        "system_prompt_path": "personas/qa/system.md",
        "autonomy": "autonomous",
        "schema_version": 1,
    }
    persona = from_jsonable(AgentPersona, raw)
    assert persona.hermes_profile is None
    assert persona.skills == []
    assert persona.soul_overlay_path is None
    assert persona.required_mcp_servers == []
```

#### `tests/agent_runtime/test_persona_tool_policy.py`

```python
def _tool_names(toolsets, blocked):
    from model_tools import get_tool_definitions
    return {
        tool["function"]["name"]
        for tool in get_tool_definitions(
            enabled_toolsets=toolsets,
            blocked_tool_names=list(blocked),
            quiet_mode=True,
        )
    }


def test_pm_actual_tool_schema_excludes_write_patch_terminal():
    pm = next(persona for persona in default_personas() if persona.id == "pm")
    names = _tool_names(effective_toolsets(pm), blocked_tool_names(pm))
    assert "terminal" not in names
    assert "write_file" not in names
    assert "patch" not in names


def test_qa_actual_tool_schema_excludes_write_patch_but_keeps_verification_tools():
    qa = next(persona for persona in default_personas() if persona.id == "qa")
    names = _tool_names(effective_toolsets(qa), blocked_tool_names(qa))
    assert "write_file" not in names
    assert "patch" not in names
    assert "terminal" in names


def test_all_personas_exclude_side_effect_orchestration_tools():
    forbidden = {"delegate_task", "clarify", "memory", "send_message", "cronjob"}
    for persona in default_personas():
        names = _tool_names(effective_toolsets(persona), blocked_tool_names(persona))
        assert names.isdisjoint(forbidden)
        assert not any(name.startswith("kanban_") for name in names)
```

### Definition of implementation-ready done

Stage 9 is implementation-ready when this document remains true after coding starts:

- Every new field has a target file, default value, JSON behavior, and test seed.
- Every new module has a public contract and restoration/redaction expectations.
- The risky design choices are fixed above, not left to the implementation agent.
- The implementation sequence is ordered so each commit can pass focused tests.
- Launcher and Harness agree on the snapshot fields before UI work starts.
- The first live smoke is docs-only/read-only and uses a temporary runtime root.

## Test matrix

Required new/updated tests:

- `tests/agent_runtime/test_config.py`
  - parses `hermes_profile`, `skills`, `soul_overlay_path`.
  - missing fields remain backward compatible.

- `tests/agent_runtime/test_personas.py`
  - role toolset filtering still applies after profile binding.
  - blocked tools cannot be re-enabled by profile or skill config.

- `tests/agent_runtime/test_persona_tool_policy.py`
  - PM actual available tools exclude `terminal`, `write_file`, and `patch`.
  - QA actual available tools exclude `write_file` and `patch` but preserve verification tools.
  - every persona excludes delegation, clarify, memory, messaging, cron, and Kanban tools.

- `tests/agent_runtime/test_profile_context.py`
  - profile context enters/restores correctly.
  - missing profile fails safe.
  - shared store root remains shared.

- `tests/agent_runtime/test_persona_skill_context.py`
  - configured skills load.
  - missing skills create readiness issue.
  - JSON-only Harness rules stay last/authoritative.

- `tests/agent_runtime/test_profile_readiness.py`
  - profile readiness statuses are redaction-safe.
  - QA MCP readiness detects missing `launcher_qa` config without launching it.

- `tests/agent_runtime/test_snapshot_redaction.py`
  - snapshot includes profile labels and skills.
  - snapshot excludes token/API/env/secret-like values.

- Launcher Mission Control widget/model tests
  - profile panel renders.
  - readiness interventions render.
  - redaction cases never display secrets.

Verification commands:

```bash
bash scripts/run_tests.sh tests/agent_runtime/test_config.py tests/agent_runtime/test_personas.py tests/agent_runtime/test_persona_tool_policy.py tests/agent_runtime/test_profile_context.py tests/agent_runtime/test_persona_skill_context.py tests/agent_runtime/test_profile_readiness.py tests/agent_runtime/test_snapshot_redaction.py tests/agent_runtime/test_no_kanban_dependency.py
python -m compileall agent_runtime hermes_cli
hermes harness agents --json
hermes harness snapshot --json
git diff --check -- agent_runtime hermes_cli tests/agent_runtime docs/agent-runtime-harness
```

For Launcher display work:

```bash
flutter test test/features/mission_control --reporter=compact
flutter analyze lib/features/mission_control test/features/mission_control
git diff --check -- lib/features/mission_control test/features/mission_control
```

## Security and redaction rules

- Never print raw OAuth tokens, API keys, refresh tokens, auth codes, state values, Authorization headers, or credential pool entries.
- Never include profile `.env` contents in snapshots.
- Never expose MCP control file paths, nonces, or local transport secrets.
- Gateway delivery remains outside persona ticks.
- Profile-bound personas may use OAuth/MCP via normal tool execution, but final external messaging goes through the validated Harness/Alice layer.
- Missing credentials are an intervention: `auth_attention`, not a product-task blocker.

## Acceptance criteria for Stage 9

1. `AgentPersona` supports `hermes_profile`, `skills`, and `soul_overlay_path` with backward compatibility.
2. `agent_runtime.config.configured_personas()` loads profile/skill bindings from config.
3. `GPTPersonaRuntime` runs ticks under the bound Hermes profile and restores the original profile context.
4. Tool and role restrictions remain enforceable regardless of profile SOUL/skills.
5. Persona prompts include shared Harness SOUL overlay plus role-specific mission-loop guidance.
6. Persona skill manifests load deterministically and are visible in redaction-safe status/snapshot output.
7. `hermes harness agents/status/snapshot --json` show persona profile readiness without secrets.
8. Launcher Mission Control shows profile bindings/readiness before Run Next Tick.
9. QA profile readiness includes Launcher MCP presence checks.
10. No Kanban imports, commands, cards, board transitions, or worker semantics are introduced.

## Risks / interventions

- **Profile context leakage:** entering a persona profile must not permanently mutate Alice/current process config. Tests must cover exception restoration.
- **Credential leakage:** profile readiness must report only status and redacted labels. Redaction tests are mandatory.
- **MCP startup cost:** readiness should check config presence quickly; live MCP smoke belongs to explicit QA/proof ticks.
- **Skill prompt bloat:** keep skill manifests role-specific. Do not load all profile skills by default.
- **SOUL conflict:** profile SOUL may contain Kanban-era language. Harness overlay and role prompt must be authoritative for JSON-only, no-Kanban, no-direct-message behavior.
- **Gateway misuse:** profile-bound `alice` may have Telegram gateway configured, but persona ticks must not call `send_message`. Delivery is a parent/runtime concern.
- **Wrong QA profile:** choose `launcher-qa` first unless `launcher-qa-direct` is explicitly proven fresher. Mission Control must show the selected binding.
- **Over-engineering:** do not add a profile admin UI, workflow designer, or broad agent marketplace. The stage exists only to power the simple mission loop.
