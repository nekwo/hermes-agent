# Stage 37 — Swarm-Ready Specialist Agent Model

## Goal

Record the compatibility and safety contract for expanding Mission Control from a small fixed PM/Dev/QA/Neko set into a collection of specialist agents, including the new backend implementation specialist.

This stage is documentation-only for the current mission. The implementation remains Harness-native and uses the existing `ProfileAgentRunner` / `AIAgent` path; no extra execution engine, Kanban bridge, or profile credential shortcut is introduced.

## Product stance

Mission Control should be able to show and run multiple Dev-like specialists while preserving the simple proof-gated loop:

```text
Mission → Neko/PM scope → specialist Dev implementation → independent QA proof review → Ready or Intervention
```

Specialists are operator-facing labels and repo/profile bindings, not new approval authorities. A Dev-role specialist can implement and collect proof, but cannot approve implementation, emit QA verdicts, close the mission, write durable memory, send messages, run cron, delegate, or bypass role/tool-deny policy.

## Stable persona identifiers

| Persona ID | Operator label | Role | Hermes profile binding | Primary repo grounding | Compatibility notes |
| --- | --- | --- | --- | --- | --- |
| `pm` | PM Agent | `pm` | profile-configured | Harness task/planning context | Existing identifier remains valid. |
| `dev` | Launcher Dev Agent / Frontend Dev | `dev` | `gpt-launcher` when configured | `EterniaLauncher` | **Compatibility ID.** Persisted active and archived data with `persona_id="dev"` must continue to parse and display as a frontend/launcher implementation specialist, never as Unknown Agent. |
| `backend_dev` | Backend Dev Agent | `dev` | `backend-dev` | `EterniaBackend` | New stable backend implementation specialist. Uses the same Dev role policy and runner seam as `dev`. |
| `qa` | QA Agent | `qa` | profile-configured | Verification repo from task/proof context | Independent verifier. No patch/write authority. |
| `neko_supervisor` | Neko Mission Lead | supervisor | profile-configured | Mission/scoping context | Supervisor/slicer/intervention reconciler. |

The collection can grow beyond these IDs, but existing `pm`, `dev`, `qa`, and `neko_supervisor` identifiers are backward-compatible API and persistence keys.

## Repo grounding and redaction-safe scope surfaces

Harness config, snapshots, readiness, CLI bridge JSON, and Launcher Mission Control read models should expose repo scope through safe labels/resolution flags rather than raw sensitive local paths unless the operator explicitly requested a repo-scope identifier.

Approved repo-scope identifiers for this mission:

- Harness: `C:\Users\beast\AppData\Local\hermes\hermes-agent` → safe label `hermes-agent`.
- Frontend/Launcher: `X:\Unreal Engine\Engine\Launcher\EterniaLauncher` → safe label `EterniaLauncher`.
- Backend: `X:\Unreal Engine\Engine\EterniaBackend` / `X:\Unreal Engine\Engine\EterniaBackend\eternia-backend` → safe label `EterniaBackend`.

Runtime behavior:

1. `AgentPersona.repo_scope` is an optional explicit workdir override for specialists that must always start in one repo, such as `backend_dev`.
2. `AgentPersona.repo_scope_label` is the UI/snapshot label. Use it for display instead of showing the raw path.
3. `repo_execution_context_for_task(..., explicit_workdir=persona.repo_scope)` resolves the explicit repo before falling back to task `affected_repos`.
4. `safe_affected_repo_labels()` and snapshot `repo_scopes` expose labels and whether aliases resolve, not tokens, auth files, profile control paths, MCP nonces, or raw model output.

Backend Dev must be repo-grounded to the backend repo even when the mission also mentions Launcher or Harness. Frontend Dev/compat `dev` remains Launcher-grounded for frontend implementation work. Harness implementation/proof commands run from the hermes-agent repo when the mission affects the runtime itself.

## Harness implementation contract

The specialist model is collection-based:

- `default_personas()` includes all stable agents, including `dev` and `backend_dev`.
- `configured_personas()` merges overrides by persona ID while preserving unknown-future collection semantics.
- Snapshot/readiness surfaces emit an `agents` collection; consumers should not infer the full roster from hard-coded PM/Dev/QA/Neko slots.
- `backend_dev` uses role `dev`, display `Backend Dev Agent`, profile `backend-dev`, Dev toolsets, backend skills, and `repo_scope_label="EterniaBackend"`.
- `dev` keeps persona ID `dev` but displays as Launcher/Frontend Dev.
- Both Dev-role specialists use the existing Hermes-native profile runner path. Do not create a backend-specific runner, separate subprocess engine, or Kanban worker bridge.
- Tool-deny policy remains role/persona enforced. Dev-role specialists may implement but cannot call blocked delivery/approval side-effect tools.

## Launcher Mission Control contract

Launcher Mission Control bridge/model/UI should treat agents as a scalable collection:

- Parse each agent from the Harness snapshot independently by `persona_id`, `display_name`, `role`, `hermes_profile`, readiness summary, skills, missing skills, MCP requirements, toolsets, blocked tool count, model/provider configured flags, autonomy, and `repo_scope_label`.
- Preserve backward compatibility for old `persona_id="dev"` data by rendering it as Launcher Dev / Frontend Dev when no newer label is present.
- Render Frontend Dev, Backend Dev, QA, and Neko/Supervisor as clean specialist entries without assuming exactly one Dev card or a fixed four-agent layout.
- Keep large-swarm layouts scrollable/wrapping/adaptive; avoid fixed small-agent rows that clip if more specialists are added.
- Keep terminal/log inspector, run-log, profile, toolset, skill, MCP, and readiness data redaction-safe. Never display raw credentials, bearer tokens, signed URLs, auth JSON, MCP nonces, profile control paths, hidden chain-of-thought, or raw diffs.

## Proof and compatibility requirements

RED tests must exist before implementation and final proof should cover:

- default Harness persona config includes `backend_dev` and compatibility `dev`;
- `backend_dev` is bound to profile `backend-dev` and role `dev` without a duplicate execution engine;
- snapshot serialization is collection-based and redaction-safe;
- backend repo alias/explicit grounding resolves to backend context and loads backend repo instructions/brain context where available;
- role/tool-deny policy prevents Dev-role self-approval and QA verdict authority;
- Launcher bridge parsing supports multiple specialist agents and old `persona_id="dev"` snapshots;
- Launcher UI renders Frontend Dev, Backend Dev, QA, and Neko/Supervisor in a large-swarm-friendly surface;
- targeted tests/analyzers/diff checks pass for changed Harness and Launcher surfaces.

## Redaction and safety constraints

- Do not expose tokens, API keys, bearer headers, auth callback codes, signed query strings, MCP nonces, profile control paths, hidden chain-of-thought, raw model output, or raw diffs in snapshots/readiness/Launcher surfaces.
- Do not guess missing profile credentials. If `backend-dev` profile auth/model setup is missing, readiness should surface a concrete redaction-safe human intervention rather than fabricating credentials or falling back to another profile silently.
- Do not push commits unless Tony explicitly asks.
- Local commits are expected after verified diffs in each affected repo; commit hashes become proof/handoff evidence, not approval.

## Verification commands for this stage

Docs/diff hygiene:

```bash
cd C:/Users/beast/AppData/Local/hermes/hermes-agent
venv/Scripts/python.exe -m pytest -o addopts='' -p no:timeout tests/agent_runtime/test_specialist_agents_red.py tests/agent_runtime/test_specialist_personas.py -q
git diff --check -- docs/agent-runtime-harness/37-swarm-ready-specialist-agent-model.md docs/agent-runtime-harness/00-index.md
```

Final cross-repo verification belongs to Stage 7 and should include the already-targeted Harness/Launcher/Backend commands plus local commit evidence.
