# Stage 35 — Specialist Dev Agent Stage 0: Autonomous Capability and Launcher Dev Deep Audit

## Goal

Fold Tony's correction into the specialist-agent/swarm mission as **Stage 0** before adding Backend Dev or swarm UI scale-out.

Tony's product stance:

- The existing `dev` Harness persona becomes the operator-facing **Launcher Dev Agent** / **Frontend Dev** while preserving the persisted `dev` persona ID for compatibility.
- Launcher Dev must have Alice-level coding capability for its bounded role: full GPT-5.5 model class, repo-grounded tool power, relevant Alice skills, profile memory/context, and autonomous patch/test/proof loops.
- Dev should not burn tokens guessing. It should inspect, patch, run focused tests/analyzers, patch again, and only request QA with real proof IDs or block with concrete evidence.
- Dev still must not self-approve, write memory, send messages, run cron, delegate, or touch unrelated repos.

## Current Codebase Audit Evidence

### Harness persona model

- `agent_runtime/models.py::AgentPersona` supports `hermes_profile`, `skills`, `soul_overlay_path`, `required_mcp_servers`, and `readiness`, but does **not** support profile-memory opt-in.
- `agent_runtime/personas.py::default_personas()` currently keeps `id="dev"`, `display_name="Dev Agent"`, `autonomy="apply_with_review"`, and toolsets `file/search/terminal/session_search/code_execution`.
- `agent_runtime/config.py::configured_personas()` merges provider/model/api/profile/skills/MCP/toolsets, but not `display_name`, `autonomy`, or profile-memory options.
- `agent_runtime/persona_runtime.py::_invoke_agent()` already starts Dev inside the resolved affected repo workdir and sets `skip_context_files=False`, but always passes `skip_memory=True`.
- `agent_runtime/prompts/dev.md` currently says "The harness applies patches and runs commands; you return AgentDecision JSON only." This conflicts with Tony's desired Stage 0 autonomous Dev work-session loop.

### Alice vs Launcher Dev profile audit

Redaction-safe profile audit performed on `X:/Eternia/.hermes/profiles/alice` and `X:/Eternia/.hermes/profiles/gpt-launcher`:

- Alice Harness config binds `agent_runtime.personas.dev.hermes_profile: gpt-launcher`, provider `openai-codex`, model `gpt-5.5`, api mode `codex_responses`.
- `gpt-launcher` profile exists and is model `gpt-5.5` but provider config is `auto`/OpenRouter while Harness runtime override supplies `openai-codex`/GPT-5.5.
- `gpt-launcher` already has 100 skill directories, but is missing 8 Alice skills that matter for Alice-level delivery parity:
  - `creative/cozy-alice-image`
  - `devops/harness-handoff-recovery`
  - `devops/hermes-agent-operations`
  - `devops/mcp-expansion-roadmap`
  - `devops/windows-disk-cleanup`
  - `software-development/aaa-feature-delivery`
  - `software-development/hermes-agent`
  - `software-development/staged-deep-audit-delivery`
- `gpt-launcher` profile memory is stale and lacks current Tony/Mission Control durable preferences from Alice's active memory.

### Safety boundary

- `agent_runtime/personas.py::PERSONA_BLOCKED_TOOLS` blocks `delegate_task`, `clarify`, `memory`, `send_message`, `cronjob`, and Kanban tools globally for Harness personas.
- `PER_ROLE_TOOL_DENIES[DEV]` blocks `send_message` only; Dev can use file/search/terminal/code tools through toolsets.
- QA cannot patch/write. Neko/PM cannot patch/terminal.
- Stage 0 should allow Dev to **read profile memory/context** during the tick, not call the `memory` tool or persist memory writes.

## Fixed Architecture Decisions

1. Preserve persisted persona ID `dev` for compatibility; rename only the display/operator label to **Launcher Dev Agent**.
2. Add a safe per-persona `include_profile_memory` boolean. Default remains `False`; Launcher Dev opts in through Alice Harness config.
3. Keep role/tool deny policy unchanged. Profile memory opt-in does not unblock the `memory` tool.
4. Treat Dev's live tick as a bounded autonomous work session inside the affected repo. The model/tool runner already supports tools and workdir; Stage 0 fixes prompt/config/runtime gates so Dev is allowed and expected to use that capability.
5. Copy missing Alice skill directories and current Alice memory files into `gpt-launcher` because Tony explicitly asked Launcher Dev to have the same skills/personality/memory context Alice has.
6. Backend Dev and swarm UI remain later stages; Stage 0 hardens the existing Dev/Launcher Dev foundation first.

## Rejected Alternatives

- **Create `frontend_dev` as a new persona ID immediately:** rejected for Stage 0 because existing tasks/runs and Launcher read models expect `dev`. Operator-facing rename is safer now; new specialist IDs come after this capability baseline.
- **Let Dev write memories:** rejected. Dev can consume profile memory/personality context but the `memory` tool remains blocked to avoid durable side effects from Harness ticks.
- **Make QA share Dev's capabilities:** rejected. QA remains independent and non-mutating.
- **Use a separate worker/Kanban soul:** rejected. Harness stays Harness-native and no-Kanban.

## Stage 0 Implementation Tasks

### 0.1 Persona/config/schema support

Affected files:

- `agent_runtime/models.py`
- `agent_runtime/config.py`
- `agent_runtime/personas.py`
- `agent_runtime/persona_runtime.py`
- `tests/agent_runtime/test_config.py`
- `tests/agent_runtime/test_personas.py`
- `tests/agent_runtime/test_persona_runtime_fake.py`

Tasks:

- Add `AgentPersona.include_profile_memory: bool = False`.
- Merge `display_name`, `autonomy`, and `include_profile_memory` from config persona overrides.
- Rename default Dev display name to `Launcher Dev Agent` while keeping `id="dev"` and `role="dev"`.
- Pass `skip_memory=not persona.include_profile_memory` to `AgentRunRequest`.
- Add tests proving default compatibility and config override behavior.

### 0.2 Dev soul/prompt correction

Affected file:

- `agent_runtime/prompts/dev.md`

Tasks:

- Replace the obsolete "Harness applies patches and runs commands" line.
- Instruct Dev to run a bounded repo-scoped patch/test/proof loop with its allowed tools.
- Require request_qa_review only with existing proof IDs.
- Require block/report_issue_discovery for concrete failures instead of guessing loops.

### 0.3 Alice-level Launcher Dev profile parity

Affected profile files, explicitly requested by Tony:

- `X:/Eternia/.hermes/profiles/gpt-launcher/skills/...`
- `X:/Eternia/.hermes/profiles/gpt-launcher/memories/MEMORY.md`
- `X:/Eternia/.hermes/profiles/gpt-launcher/memories/USER.md`
- `X:/Eternia/.hermes/profiles/alice/config.yaml`

Tasks:

- Copy the 8 missing Alice skill directories into `gpt-launcher`.
- Refresh `gpt-launcher` memory/user memory from Alice's active profile memory so Launcher Dev has Tony's current Mission Control/UX/Hermes preferences.
- Update Alice Harness config for `agent_runtime.personas.dev`:
  - `display_name: Launcher Dev Agent`
  - `autonomy: autonomous`
  - `include_profile_memory: true`
  - expanded skills manifest including staged AAA delivery, Harness recovery, Flutter UI, Launcher QA screenshot, Hermes Agent, review, and GitHub workflow skills.

## Tests / Proof

Focused gates:

```bash
venv/Scripts/python.exe -m pytest -o addopts='' -p no:timeout tests/agent_runtime/test_config.py tests/agent_runtime/test_personas.py tests/agent_runtime/test_persona_runtime_fake.py tests/agent_runtime/test_persona_prompts.py -q
venv/Scripts/python.exe -m compileall agent_runtime tests/agent_runtime/test_config.py tests/agent_runtime/test_personas.py tests/agent_runtime/test_persona_runtime_fake.py tests/agent_runtime/test_persona_prompts.py
git diff --check
```

Profile proof:

```bash
venv/Scripts/python.exe -m hermes_cli.main --profile alice harness snapshot --json
```

Verify `agents` includes `dev` / `Launcher Dev Agent`, `hermes_profile: gpt-launcher`, GPT-5.5 provider/model metadata, non-missing skills, and no open readiness issue caused by Stage 0 config.

## Later Stages

### Stage 1 — Backend Dev persona/profile binding

- Add `backend_dev` persona with role `dev`, display `Backend Dev Agent`, profile `gpt_backend`, repo scope `X:/Unreal Engine/Engine/EterniaBackend`, backend skills, and Postgres gate expectations.
- Add compatibility tests for multiple Dev-role personas.

### Stage 2 — Swarm-ready data/read model

- Snapshot/CLI/Launcher bridge must expose agent collections and specialist metadata without assuming PM/Dev/QA singleton layout.
- Keep legacy `dev` consumers compatible.

### Stage 3 — Launcher Mission Control multi-agent UX

- Render specialist agents as a unified command deck/table/cockpit, not disconnected cards.
- Show Launcher Dev and Backend Dev readiness, current repo, active run/proof state, and safe terminal transcript filters.

### Stage 4 — Autonomous proof-loop hardening

- Convert more `request_test_run` cases into same-session Dev command proof where safe.
- Persist proof handles from Dev's own test runs when the tool result is redaction-safe.
- Add stronger no-token-burn loop detection.

## AAA Gap Register

- **High:** Backend Dev not yet bound. Required after Stage 0.
- **High:** Launcher UI still assumes old Dev labels in places. Required after Harness read model is updated.
- **Medium:** Dev self-collected tool results are visible as progress/work events, but not every tool test run becomes a Proof record automatically yet. Stage 4 hardening.
- **Medium:** Skill/profile sync is manual. Consider a reusable profile parity sync script after Stage 1.
