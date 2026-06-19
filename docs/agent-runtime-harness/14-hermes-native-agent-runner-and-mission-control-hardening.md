# Stage 14 — Hermes-Native Agent Runner and Mission Control Hardening

> **For Hermes:** This is an AAA implementation-ready deep audit and staged handoff. Use strict TDD for every production change. The architectural correction is explicit: Mission Control / Agent Runtime Harness must orchestrate Hermes agents, not grow a second agent runtime.

## Goal

Promote profile-bound Harness persona execution from a bespoke `AIAgent(...)` construction inside `agent_runtime/persona_runtime.py` into a reusable Hermes-native agent runner API, then harden readiness, observability, incident classification, daemon/manual modes, and end-to-end smoke verification around that runner.

## Product stance

Mission Control is the brainstem:

```text
goal / mission -> PM -> Dev -> QA -> proof gate -> ready or intervention
```

Hermes Agent remains the execution engine:

```text
profile context + model/provider/auth + skills + tools + session + callbacks + AIAgent
```

Mission Control must not duplicate the agent class, auth stack, model routing, skill loading, tool registry, gateway behavior, or session engine. It should call a stable Hermes runner surface and own only deterministic mission state, proof gates, incidents, and operator-facing observability.

## Fixed architecture decisions

- **Do not fork or replace `AIAgent`.** Reuse `run_agent.AIAgent` and existing Hermes runtime modules.
- **Add a reusable runner seam.** Create a Hermes-owned runner API that can run one agent turn under a named profile with explicit tool/skill/session/progress constraints.
- **Keep Harness state separate.** Runner APIs do not know task states, proof gates, or Mission Control transitions.
- **Keep the Harness store shared.** Profile-bound runs must not split the mission store by entering another `HERMES_HOME` before stores/paths are pinned.
- **Fail readiness before dispatch.** Runtime dependency, interpreter, auth, profile, skill, and MCP issues should be surfaced as readiness/intervention data before a model run when possible.
- **Progress is liveness, not proof.** Progress events help operators understand long quiet runs; proof still requires explicit `Proof` records.
- **Manual mode is valid.** An offline daemon is not inherently critical when the operator is intentionally running foreground/manual ticks.
- **Incident taxonomy must preserve root cause.** Missing Python packages, wrong interpreter, auth failures, quota, invalid JSON, tool-deny violations, and product test failures must not all collapse into `provider_failure`.
- **Add a temp-root smoke lane.** The Harness needs a repo-safe end-to-end smoke goal that proves PM -> Dev -> QA -> proof -> done without touching product repos.

## Rejected alternatives

- **Harness-specific agent clone:** rejected; it would duplicate provider/auth/tools/session behavior and drift from Hermes.
- **Shelling out to `hermes` for every persona as the primary API:** rejected as the core seam because it hides structured callbacks and makes tests slower. Keep CLI subprocess only as an optional isolation adapter later.
- **Launcher UI first:** rejected; the read model must be truthful before UI actions rely on it.
- **Treat daemon offline as always critical:** rejected; it creates false alarms during manual tick operation.
- **Trust model prose as proof:** rejected; Harness proof gates must validate explicit `Proof` records.

## Current repo deep audit evidence

Audited files and findings in the current checkout:

### Existing Hermes runtime anchors

- `run_agent.py`
  - `AIAgent.__init__()` already accepts `provider`, `model`, `api_mode`, `enabled_toolsets`, `disabled_toolsets`, `blocked_tool_names`, `session_id`, `credential_pool`, callbacks, `platform`, `skip_memory`, and `skip_context_files`.
  - `AIAgent.run_conversation()` is the correct synchronous one-turn execution API for Harness persona ticks.
  - Callback support exists: `tool_progress_callback`, `tool_start_callback`, `tool_complete_callback`, `thinking_callback`, `reasoning_callback`, and related status/stream hooks.
- `hermes_cli/runtime_provider.py`
  - Provider resolution already centralizes auth, base URL, API mode, and credential pool behavior.
  - Harness should consume this through normal `AIAgent` construction / runner readiness, not duplicate provider-specific checks.
- `agent/skill_commands.py`
  - Skill loading already exists and is profile-aware through Hermes home resolution.
  - Harness should keep using `agent_runtime/skill_context.py` as a bounded wrapper, then move generic runner skill context behavior into Hermes-owned runner code if broadly useful.
- `hermes_cli/profiles.py` and `hermes_constants.py`
  - Existing profile primitives support named profile resolution and context-local Hermes home overrides.
  - `get_default_hermes_root()` behavior is a known shared-store hazard when entering profile contexts.

### Harness-specific anchors

- `agent_runtime/persona_runtime.py`
  - `GPTPersonaRuntime._invoke_agent()` directly constructs `AIAgent` with persona provider/model/toolsets and calls `run_conversation()`.
  - This proves the current implementation is not a separate agent class, but the construction logic is still too Harness-local and should be moved behind a reusable runner API.
  - It enters `persona_profile_context(binding, runtime_root=paths.store_root())`, which is correct in spirit but should become runner-owned profile execution behavior with explicit store-root pinning.
  - It has post-run `_apply_llm_metadata()` but does not own a full live progress pipeline in this checkout.
- `agent_runtime/profile_readiness.py`
  - Checks profile existence, skills, MCP config, and provider readiness by calling `resolve_runtime_provider()` inside the persona profile context.
  - It does not prove the actual command/interpreter used to run Harness can import runtime dependencies such as the OpenAI SDK.
  - This caused a false-ready state in manual testing: the wrong Python interpreter could launch Harness and fail with `No module named 'openai'` while profile/provider auth was healthy.
- `agent_runtime/ticker.py`
  - `_execute_action()` catches all non-`DecisionPayloadInvalid` exceptions as `provider_failure`.
  - This loses root cause for interpreter/package failures, runtime dependency gaps, tool-policy violations, and product verification failures.
- `agent_runtime/observability.py`
  - Observability reports `daemon_offline` as critical whenever status is offline.
  - It tracks running run counts and stall thresholds, but active run detail is limited to `recent_runs`; there is no first-class manual/daemon mode distinction.
  - Stalled-run logic correctly avoids false stalls until the threshold; this worked during a long quiet Dev run.
- `agent_runtime/daemon.py`
  - `start_daemon()` uses `sys.executable -m hermes_cli.main ...`, which preserves the current interpreter. This is safe only if the current interpreter is the intended Hermes runtime.
  - It records redaction-safe daemon status and prevents duplicate daemon start by PID check.
- `hermes_cli/harness.py`
  - `tick` and daemon foreground construct `GPTPersonaRuntime` directly.
  - Incident CLI only lists incidents; there is no operator close/resolve command even though `IncidentStore.close()` exists.
- `agent_runtime/models.py`
  - `AgentRun` stores `llm`, `final_decision`, and `error`, but no `progress` field in this checkout.
  - `Incident.kind` is an unconstrained string, which makes taxonomy possible without schema migration, but also permits inconsistent incident kinds unless centralized.
- `agent_runtime/events.py`
  - Strict `ALLOWED_EVENT_TYPES` and `EVENT_PAYLOAD_LIMIT_BYTES = 4096` are good redaction/size guardrails.
  - Progress event types are not present in this checkout.

### Test anchors

- `tests/agent_runtime/test_profile_readiness.py` already covers missing profiles, profile-scoped skills, and auth attention.
- `tests/agent_runtime/test_daemon.py` covers daemon foreground loop, status/snapshot daemon health, duplicate daemon starts, and status writes.
- `tests/agent_runtime/test_context_requests.py` and `tests/agent_runtime/test_issue_discovery.py` already cover parts of observability/intervention behavior.
- There is no generic Hermes runner test because the runner seam does not exist yet.

## Observed live smoke evidence motivating this stage

A manual Harness tick on an existing mission exposed two important issues:

1. Running the command with the wrong global Python interpreter failed before model execution because the OpenAI package was unavailable.
2. Running through the Hermes virtual environment succeeded: QA approved the plan, Dev completed a long quiet run, and observability kept the running run visible without marking it stalled before the threshold.

These observations prove the profile/auth fix is working, but also prove Stage 14 needs interpreter/runtime readiness, a runner seam, better incident taxonomy, and progress/manual-mode polish.

## Stage 14 implementation slices

### 14.0 — Local operator entrypoint stabilization

**Objective:** Ensure the command used for Mission Control invokes the intended Hermes runtime environment.

**Files:**

- Inspect/modify if needed: `pyproject.toml`
- Inspect/modify if needed: installer / script entrypoint files under `scripts/` and `hermes_cli/`
- Modify: local docs/runbook only if no code change is required
- Test: `tests/hermes_cli/` entrypoint or Windows-specific test if a code path is changed

**Implementation tasks:**

1. Write a failing test or diagnostic asserting the Harness CLI can report its interpreter/runtime package availability without starting a model call.
2. Add a small internal helper, for example `hermes_cli/runtime_environment.py`, with:

```python
@dataclass(frozen=True)
class RuntimeEnvironmentStatus:
    executable: str
    package_available: dict[str, bool]
    issues: list[dict[str, str]]
```

3. Check import availability with `importlib.util.find_spec()` for packages needed by configured provider modes.
4. Add a CLI/debug surface only if useful, for example `hermes harness agents --json` includes `runtime_environment` in diagnostic mode, or `hermes doctor` includes Harness runtime checks.
5. Do not hard-code local absolute paths in docs or tests.

**Acceptance criteria:**

- Wrong interpreter / missing `openai` is detected before a persona run where possible.
- The operator sees `runtime_dependency_missing` or `interpreter_mismatch`, not `provider_failure`.
- No secrets or local absolute paths are emitted.

---

### 14.1 — Create `HermesAgentRunner` as the reusable execution seam

**Objective:** Move profile-bound one-turn agent construction into Hermes-owned runner code.

**Files:**

- Create: `agent/profile_runner.py` or `hermes_cli/agent_runner.py`.
- Test: create `tests/test_profile_agent_runner.py` or `tests/hermes_cli/test_agent_runner.py`.
- Keep Harness adapter in: `agent_runtime/persona_runtime.py`.

**Public internal API:**

```python
@dataclass(slots=True)
class AgentRunRequest:
    profile: str | None
    provider: str | None = None
    model: str | None = None
    api_mode: str | None = None
    enabled_toolsets: list[str] | None = None
    disabled_toolsets: list[str] | None = None
    blocked_tool_names: list[str] | None = None
    skills: list[str] | None = None
    session_id: str | None = None
    platform: str = "agent_runtime"
    quiet_mode: bool = True
    skip_context_files: bool = True
    skip_memory: bool = True
    max_iterations: int = 90
    system_message: str | None = None
    user_message: str = ""
    task_id: str | None = None
    progress_callback: Callable[[dict[str, Any]], None] | None = None
    runtime_root: Path | None = None

@dataclass(slots=True)
class AgentRunResult:
    final_response: str
    session_id: str | None
    provider: str | None
    model: str | None
    base_url: str | None
    messages: list[dict[str, Any]]
    api_calls: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)
```

**Runner behavior:**

1. Resolve profile binding using existing profile primitives.
2. Enter a profile context with `HERMES_HOME=<profile_home>` and `HOME=<profile_home>/home` for subprocesses.
3. Preserve/pin `HERMES_AGENT_RUNTIME_ROOT` if supplied.
4. Load bounded skill context if requested, but keep role/Harness JSON rules supplied by the caller.
5. Construct `AIAgent` with provided model/provider/tool/tool-deny/session parameters.
6. Wire progress callbacks into existing `AIAgent` callback parameters.
7. Return normalized `AgentRunResult` without leaking credentials.

**RED tests:**

- `test_runner_enters_profile_context_and_restores_environment`
- `test_runner_passes_toolsets_and_blocked_tools_to_ai_agent`
- `test_runner_preserves_runtime_root_env_inside_profile_context`
- `test_runner_returns_normalized_result_from_dict_response`
- `test_runner_reports_missing_profile_before_agent_construction`

**Acceptance criteria:**

- Harness no longer owns direct `AIAgent` construction details.
- The runner is generic and has no Mission Control state-machine imports.
- Environment restoration works on success and exception.

---

### 14.2 — Refactor Harness persona runtime to use the runner

**Objective:** Make `GPTPersonaRuntime` a thin adapter: build prompt/context, call the runner, parse/validate `AgentDecision`.

**Files:**

- Modify: `agent_runtime/persona_runtime.py`
- Test: `tests/agent_runtime/test_persona_runtime.py`

**Implementation tasks:**

1. Inject a `runner` object into `GPTPersonaRuntime` instead of `agent_factory`.
2. Replace `_default_agent_factory` with the new runner default.
3. Keep `build_system_prompt()` and `render_context()` in Harness because they are mission-specific.
4. Move `_apply_llm_metadata()` to consume `AgentRunResult` rather than a raw `AIAgent` object.
5. Preserve the two-attempt structured decision repair loop.

**RED tests:**

- `test_persona_runtime_calls_runner_with_bound_profile`
- `test_persona_runtime_passes_harness_prompt_and_context`
- `test_persona_runtime_records_llm_metadata_from_runner_result`
- `test_persona_runtime_repair_loop_still_runs_on_invalid_decision`

**Acceptance criteria:**

- `persona_runtime.py` imports the runner seam, not `run_agent.AIAgent` directly.
- Existing decision parsing/validation behavior is unchanged.
- No Kanban or product-specific logic enters the runner.

---

### 14.3 — Runtime readiness that matches actual dispatch

**Objective:** Readiness should prove the runner can be constructed in the same environment that a tick will use.

**Files:**

- Modify: `agent_runtime/profile_readiness.py`
- Add/modify: `agent/profile_runner.py` or `hermes_cli/agent_runner.py`
- Test: `tests/agent_runtime/test_profile_readiness.py`

**Implementation tasks:**

1. Add `runner_check=True` or equivalent dry-run readiness method to the runner.
2. Return structured readiness issues:

```text
missing_profile
missing_skill
mcp_attention
auth_attention
runtime_dependency_missing
interpreter_mismatch
config_error
ready
```

3. Update dominant severity ordering so dependency/interpreter failures outrank auth attention when dispatch cannot start.
4. Keep summaries redaction-safe.

**RED tests:**

- `test_readiness_reports_runtime_dependency_missing_without_model_call`
- `test_readiness_reports_auth_attention_when_dependency_ok_but_auth_missing`
- `test_readiness_does_not_leak_executable_private_paths_in_summary`
- `test_readiness_uses_same_profile_runner_environment_as_tick`

**Acceptance criteria:**

- A missing SDK package cannot produce `profile_readiness: ready`.
- Auth-good/runtime-bad is represented accurately.
- Runtime readiness uses the same runner path as `tick`.

---

### 14.4 — Incident taxonomy and CLI incident operations

**Objective:** Preserve root cause and give the operator safe incident controls.

**Files:**

- Create: `agent_runtime/incidents.py`
- Modify: `agent_runtime/ticker.py`
- Modify: `agent_runtime/observability.py`
- Modify: `hermes_cli/harness.py`
- Test: `tests/agent_runtime/test_incidents.py` and CLI tests if available

**Incident kind constants:**

```python
RUNTIME_DEPENDENCY_MISSING = "runtime_dependency_missing"
INTERPRETER_MISMATCH = "interpreter_mismatch"
PROVIDER_AUTH_FAILURE = "provider_auth_failure"
PROVIDER_RATE_LIMIT = "provider_rate_limit"
MODEL_INVALID_OUTPUT = "model_invalid_output"
TOOL_POLICY_VIOLATION = "tool_policy_violation"
HARNESS_ACTION_FAILURE = "harness_action_failure"
PRODUCT_VERIFICATION_FAILURE = "product_verification_failure"
```

**Implementation tasks:**

1. Add `classify_exception(exc) -> IncidentClassification`.
2. Map `DecisionPayloadInvalid` to `model_invalid_output`.
3. Map `ImportError` / `ModuleNotFoundError` for provider dependencies to `runtime_dependency_missing`.
4. Map `AuthError` to `provider_auth_failure` unless code indicates rate/quota.
5. Add `hermes harness incident close <incident_id> --reason ... --json` and optionally `resolve` later.
6. Store close reason in event payload; do not mutate incident schema unless needed.

**Acceptance criteria:**

- Wrong interpreter no longer creates `provider_failure`.
- Operator can close a known-bad/manual-test incident without Python snippets.
- Observability severity maps by kind, not string guesses.

**Implementation progress:**

- Added first TDD slice for `ModuleNotFoundError("No module named 'openai'")` during persona execution.
- Created centralized `agent_runtime/incidents.py` with `classify_exception()` and critical incident constants.
- Updated `agent_runtime/ticker.py` so `ImportError` / `ModuleNotFoundError` become `runtime_dependency_missing` instead of `provider_failure`.
- Updated `agent_runtime/proof_gates.py` and `agent_runtime/observability.py` to use shared critical incident kind constants.
- Added `hermes harness incident close <incident_id> --reason ... --json` and `IncidentStore.close(..., reason=...)` event payload support.
- Verified targeted ticker/status/snapshot/CLI tests and `compileall agent_runtime hermes_cli`.

**Remaining gaps for this slice:**

- Add auth/rate-limit/tool-policy/product-verification mappings after auditing concrete exception types.
- Add direct unit coverage for observability severity of each new incident kind.

---

### 14.5 — Progress and active-run visibility on the runner path

**Objective:** Long quiet agent runs must show safe liveness and latest activity.

**Files:**

- Add: `agent_runtime/progress.py`
- Modify: `agent_runtime/models.py` to add optional `AgentRun.progress`
- Modify: `agent_runtime/store.py`
- Modify: `agent_runtime/events.py`
- Modify: runner API to accept progress callback/sink
- Modify: `agent_runtime/observability.py`, `status.py`, `snapshot.py`
- Test: `tests/agent_runtime/test_run_progress.py`

**Implementation tasks:**

1. Add optional `AgentRun.progress: dict[str, Any] | None = None` with `schema_version = 1` unchanged.
2. Add allowed event types:

```text
run.progress
run.model_call.started
run.model_call.finished
run.tool.started
run.tool.finished
run.validation.started
run.validation.failed
```

3. Implement `RunProgressSink` with redaction-safe payload filtering and payload-size enforcement.
4. Have `RunProgressSink` update `AgentRun.progress`, refresh `last_heartbeat_at`, and append safe events.
5. Wire runner callbacks from `AIAgent` into the sink.
6. Add `active_runs` to observability/status/snapshot with persona, task, state, safe progress summary, heartbeat age, and elapsed seconds.

**Acceptance criteria:**

- A 15-minute quiet run shows heartbeat/progress instead of looking dead.
- Progress callback failures do not crash model execution.
- Raw prompts, raw tool args, tokens, env content, and local absolute paths are not emitted.

---

### 14.6 — Manual vs daemon mode observability

**Objective:** Avoid false critical daemon alerts during intentional manual operation.

**Files:**

- Modify: `agent_runtime/runtime_config.py`
- Modify: `agent_runtime/config.py`
- Modify: `agent_runtime/observability.py`
- Modify: `agent_runtime/status.py` and `snapshot.py`
- Test: `tests/agent_runtime/test_observability.py` or `test_daemon.py`

**Config shape:**

```yaml
agent_runtime:
  execution_mode: manual   # manual | daemon
  daemon:
    enabled: false
```

**Implementation tasks:**

1. Add `execution_mode` to runtime config, defaulting to `manual` when daemon is disabled.
2. Pass config mode into `build_observability()`.
3. In manual mode, `daemon_offline` is informational or absent.
4. In daemon mode, offline/stale daemon remains critical.
5. Surface `execution_mode` in status and snapshot.

**Acceptance criteria:**

- Manual tick mode can be healthy with daemon offline.
- Daemon mode still blocks on offline/stale daemon.
- Launcher can label the surface accurately.

---

### 14.7 — Temp-root Mission Control smoke goal

**Objective:** Provide a safe end-to-end Harness proof path that does not depend on product repos.

**Files:**

- Add: `agent_runtime/smoke.py`
- Modify: `hermes_cli/harness.py`
- Test: `tests/agent_runtime/test_smoke_goal.py`

**Command shape:**

```bash
hermes harness smoke --json --temp-root
```

**Smoke behavior:**

1. Creates a temporary Harness store root.
2. Seeds fake or deterministic test personas if `--no-model` is used.
3. Runs PM -> Dev -> QA -> proof gate -> done.
4. Attaches a safe proof artifact inside the temp root.
5. Emits a redaction-safe result envelope.

**Acceptance criteria:**

- CI can run the no-model smoke without credentials.
- A live-model smoke can be run manually with profile-bound personas.
- The smoke proves transitions, proof records, and observability without modifying product repos.

---

### 14.8 — Launcher Mission Control read model contract

**Objective:** Stabilize the JSON surface Launcher consumes after runner/readiness/observability hardening.

**Files:**

- Modify: `agent_runtime/snapshot.py`
- Modify: `agent_runtime/status.py`
- Update docs: Launcher Mission Control docs in the Launcher repo if/when implementing UI
- Test: `tests/agent_runtime/test_snapshot_redaction.py`

**Read model fields:**

```json
{
  "execution_mode": "manual",
  "daemon": {"state": "offline"},
  "agents": [{"persona_id": "pm", "profile_readiness": "ready"}],
  "active_runs": [{"run_id": "run_...", "persona_id": "dev", "progress": {"state": "waiting_model"}}],
  "missions": [{"task_id": "task_...", "state": "dev_implementing", "proof_gaps": []}],
  "interventions": [{"kind": "runtime_dependency_missing", "severity": "critical"}]
}
```

**Acceptance criteria:**

- Launcher does not read Harness files directly.
- Snapshot is recursively redaction-safe.
- UI can distinguish manual mode, daemon mode, active run, proof gap, and human intervention.

---

### 14.9 — Upstream/local split

**Objective:** Keep generic Hermes improvements upstreamable and Tony-local Mission Control choices local.

**Upstream candidates:**

- Generic profile runner API if scoped cleanly.
- Runtime dependency/interpreter readiness checks.
- Incident taxonomy constants if not product-specific.
- Existing global Codex credential pool fallback fix.

**Tony-local / product-specific:**

- Default persona bindings (`pm`, `gpt-launcher`, `launcher-qa`, `alice`).
- Launcher Mission Control copy and visual metaphor.
- Eternia-specific proof commands, Stage C, and product repo paths.

**Acceptance criteria:**

- Upstream PR branches start from `upstream/main` and contain only generic changes.
- Local docs do not leak private absolute paths or profile data.
- Tony-local defaults stay in profile/config/docs, not Hermes core.

## Ordered implementation plan

### Implementation completion status

Stage 14 has now been implemented as an end-to-end local slice using TDD/proof-first discipline:

- **14.0 runtime environment detection:** added `hermes_cli/runtime_environment.py`; readiness can surface missing provider SDK packages such as `openai` before dispatch.
- **14.1 Hermes profile runner seam:** added `agent_runtime/profile_runner.py` with `AgentRunRequest`, `AgentRunResult`, `ProfileAgentRunner`, profile context preservation, tool/blocked-tool forwarding, progress callback adapters, and missing-profile preflight.
- **14.2 Harness persona runtime refactor:** `GPTPersonaRuntime` now calls `ProfileAgentRunner` instead of directly constructing `AIAgent`; Harness still owns prompts, context rendering, and `AgentDecision` validation/repair.
- **14.3 readiness parity:** `profile_readiness_for_persona()` now reports `runtime_dependency_missing` above auth attention when the actual interpreter lacks required provider packages.
- **14.4 incident taxonomy / close CLI:** centralized `agent_runtime/incidents.py`; ticker maps dependency/auth/rate-limit/tool-policy/model-output failures to distinct incident kinds; `hermes harness incident close <incident_id> --reason ... --json` closes manual/test incidents safely.
- **14.5 progress / active runs:** added optional `AgentRun.progress`, progress event types, `RunProgressSink`, runner callback wiring, and `observability.active_runs` progress summaries.
- **14.6 manual vs daemon observability:** added `agent_runtime.execution_mode` config; manual mode does not make daemon-offline critical, daemon mode still does.
- **14.7 temp-root smoke:** added `hermes harness smoke --json --temp-root --no-model`, proving PM -> Dev -> QA -> proof -> done without model credentials or product repo writes.
- **14.8 Launcher read model contract:** snapshot/observability now include `execution_mode`, `active_runs`, progress, interventions, persona readiness, proof gaps, and daemon state through stable JSON surfaces rather than file reads.

**Remaining non-blocking hardening gaps:**

- Live-model smoke mode is intentionally rejected until runner/readiness behavior is proven in a separate credentialed smoke slice.
- Exception taxonomy uses conservative heuristic classification for auth/rate-limit/tool-policy errors; provider-specific exception classes can be tightened as concrete failures are observed.
- Upstream split is still documentation/planning only; generic runner/readiness/taxonomy changes should be carved into focused upstream branches only after Tony approves upstream work.

**Second-pass AAA gap closed during final audit:**

- `agent_runtime/smoke.py` now restores `HERMES_AGENT_RUNTIME_ROOT` in a `finally` block even when smoke execution fails inside the temp-root context. Regression: `tests/agent_runtime/test_smoke_goal.py::test_no_model_smoke_restores_runtime_root_when_smoke_fails`.

1. Implement 14.0 runtime environment detection and local entrypoint fix.
2. Implement 14.1 runner API with fake-agent tests.
3. Refactor 14.2 Harness persona runtime to the runner.
4. Implement 14.3 readiness checks using the runner dry-run.
5. Implement 14.4 incident taxonomy and CLI incident close.
6. Implement 14.5 progress sink + active-run read model.
7. Implement 14.6 manual/daemon execution mode.
8. Implement 14.7 temp-root smoke command.
9. Implement 14.8 Launcher read model contract.
10. Split 14.9 generic improvements into upstream-targeted branches only after local tests are green.

## Verification matrix

Run after each slice as applicable:

```bash
venv/Scripts/python -m pytest tests/agent_runtime/test_profile_readiness.py -q
venv/Scripts/python -m pytest tests/agent_runtime/test_persona_runtime.py -q
venv/Scripts/python -m pytest tests/agent_runtime/test_daemon.py -q
venv/Scripts/python -m pytest tests/agent_runtime/test_snapshot_redaction.py -q
venv/Scripts/python -m pytest tests/hermes_cli/test_auth_profile_fallback.py -q
venv/Scripts/python -m compileall agent_runtime hermes_cli agent run_agent.py
venv/Scripts/python -m hermes_cli.main harness smoke --json --temp-root --no-model
```

If running on Windows and the repo pytest config selects a POSIX-only timeout method, use the project runner or explicitly neutralize incompatible addopts while preserving the targeted test evidence. Do not claim full-suite pass from targeted tests.

### Final local verification evidence

Executed from the Windows Hermes checkout with `PYTHONPATH=.` and `venv/Scripts/python.exe`:

- `venv/Scripts/python.exe -m pytest --timeout-method=thread tests/agent_runtime tests/hermes_cli/test_harness_cli.py -q` — **PASS**, 165 passed, 1 pre-existing Discord `audioop` deprecation warning.
- `venv/Scripts/python.exe -m compileall agent_runtime hermes_cli -q` — **PASS**.
- `venv/Scripts/python.exe -m hermes_cli.main harness smoke --json --temp-root --no-model` — **PASS**, returned `ok: true`, `final_state: done`, and proof IDs `proof_smoke_test`, `proof_smoke_qa`.
- `venv/Scripts/python.exe -m ruff check agent_runtime hermes_cli tests/agent_runtime/test_incidents.py tests/agent_runtime/test_profile_runner.py tests/agent_runtime/test_run_progress.py tests/agent_runtime/test_smoke_goal.py tests/hermes_cli/test_harness_cli.py` — **PASS**.
- `git diff --check` — **PASS**.

## AAA acceptance checklist

- Harness persona execution uses a Hermes-native runner seam, not direct bespoke `AIAgent` construction.
- Profile-bound execution preserves shared Harness store root.
- Readiness catches interpreter/dependency/auth/profile/skill/MCP issues before dispatch where possible.
- Incident kind identifies root cause accurately.
- Long quiet runs expose safe active progress and heartbeat.
- Manual mode is not falsely critical when daemon is intentionally offline.
- Smoke command proves PM -> Dev -> QA -> proof -> done in a temp root.
- Snapshot/status/observe are recursively redaction-safe.
- Generic improvements are separable for upstream PRs.
