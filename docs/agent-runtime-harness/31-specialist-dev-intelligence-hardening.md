# Stage 31 — Specialist Dev Intelligence Hardening

## Goal

Close the Harness gap where Launcher Dev / Backend Dev can burn large live-run budgets in broad `search_files` / `read_file` loops without producing patch, proof, or a precise blocker.

Tony clarified the first target is **Dev Launcher**, and the implementation must remain compatible with **Dev Backend**.

## Scope

Repo: `X:/Eternia/hermes-agent`

Affected runtime surfaces:

- `agent_runtime/persona_runtime.py` — prompt construction and persona overlays.
- `agent_runtime/profile_runner.py` — live tool-progress loop guard.
- `agent_runtime/prompts/dev.md` — shared Dev discipline wording if needed.
- `agent_runtime/dev_discipline.py` — progress telemetry interpretation if needed.
- `tests/agent_runtime/test_persona_prompts.py`
- `tests/agent_runtime/test_profile_runner.py`

Out of scope:

- Do not modify Eternia Launcher product code for this hardening stage.
- Do not modify Eternia Backend product code for this hardening stage.
- Do not alter Hermes Agent Telegram/media sending.
- Do not make Dev personalities Neko/Alice-chatty; copy operational discipline, not user-facing persona roleplay.

## Audit Evidence

Current persona config:

- `dev` has display name `Launcher Dev Agent`, repo label `EterniaLauncher`, profile `gpt-launcher`, role `dev`.
- `backend_dev` has display name `Backend Dev Agent`, repo scope `X:/Unreal Engine/Engine/EterniaBackend/eternia-backend`, profile `backend-dev`, role `dev`.
- Both use role `dev`, so `build_system_prompt()` currently loads `agent_runtime/prompts/dev.md` for both.

Observed failure:

- Launcher BMP goal runs hit repeated `search_files`/`read_file` warnings and one Dev run consumed ~573k tokens before invalid/budget-pressure failure.
- A focused retry still repeated read/search events until Alice cancelled it.

## Architecture Decision

Use a compatible two-layer design:

1. Shared Dev discipline applies to both Launcher Dev and Backend Dev.
2. Persona-specific overlay applies based on `persona.id` / `repo_scope_label`:
   - `dev` / `EterniaLauncher`: Flutter/Launcher/MCP/dirty-tree/visual-proof discipline.
   - `backend_dev` / `EterniaBackend`: backend contract/Postgres gate/security/migration discipline.

Do not fork runtime roles; keep both as `role=dev` so existing decision contracts and state machine behavior remain compatible.

## Stage A — Prompt Intelligence

### Objective

Make Dev Launcher and Dev Backend receive specific, repo-native operating rules while preserving shared Dev contracts.

### Acceptance

- Prompt for Launcher Dev includes `Launcher Dev Specialist Overlay` and Launcher-specific guidance.
- Prompt for Backend Dev includes `Backend Dev Specialist Overlay` and Backend-specific guidance.
- Both prompts include a shared `Specialist Dev Loop Guard`.
- Generic Dev prompt tests still pass.

## Stage B — Runtime Loop Guard

### Objective

Turn repeated read/search warning from a passive log into a cooperative interrupt for Harness Dev runs when no patch/test/proof progress exists.

### Acceptance

- `AgentRunRequest` can enable repeated read/search stop behavior.
- Harness Dev runs enable it by default.
- Repeated `search_files`/`read_file` events raise `RunBudgetExceeded` with a clear `read_search` reason.
- Skill fanout warning remains a warning, not an immediate hard stop.
- General non-Harness/default runner behavior remains compatible unless the flag is enabled.

## Stage C — Verification and BMP Retry

### Objective

Run targeted Hermes tests, then restart the Launcher BMP goal with minimal babysitting.

### Acceptance

- Targeted pytest tests pass.
- Harness status has no active runaway Dev run.
- Launcher BMP goal is run again only after hardening is active.
- If Dev still loops, the runtime stops it deterministically before another huge token burn.

## Gap Checklist

- [ ] Prompt tests added and observed RED.
- [ ] Runtime loop guard test added and observed RED.
- [ ] Prompt overlays implemented.
- [ ] Runtime loop guard implemented.
- [ ] Targeted tests GREEN.
- [ ] BMP Harness goal retried with minimal babysitting.
- [ ] Remaining gaps documented or escalated.
