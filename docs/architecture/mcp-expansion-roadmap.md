# MCP Expansion Roadmap — Hermes, Arcadia, and Eternia

## Purpose

This document preserves the staged architecture idea for expanding our MCP surface area without building one oversized, unrestricted “god server.”

The key split is:

1. **Generic Hermes Control MCP** — upstream-worthy control plane for Hermes itself.
2. **Private Arcadia MCPs** — Tony-specific company/agent/brain/release operating layer.
3. **Product-specific Eternia MCPs** — Launcher/Backend QA and runtime control.

This lets us contribute generally useful Hermes control-plane work upstream while keeping private enterprise workflows, Obsidian vault paths, QA contracts, release gates, and agent hierarchy isolated.

---

## Layer 1: Hermes Control MCP

**Goal:** expose Hermes’ own control plane as typed MCP tools.

This layer should be generic enough to be useful outside Arcadia/Eternia.

### Candidate tool families

#### Kanban

- `hermes_kanban_list_boards`
- `hermes_kanban_create_card`
- `hermes_kanban_show_card`
- `hermes_kanban_assign_card`
- `hermes_kanban_link_cards`
- `hermes_kanban_comment`
- `hermes_kanban_dispatch`
- `hermes_kanban_status`

#### Profiles and workers

- `hermes_profiles_list`
- `hermes_profiles_show`
- `hermes_profiles_spawn_worker`
- `hermes_profiles_check_toolsets`
- `hermes_profiles_check_skills`
- `hermes_workers_tail_log`
- `hermes_workers_stop`

#### Cron

- `hermes_cron_list`
- `hermes_cron_run`
- `hermes_cron_pause`
- `hermes_cron_resume`
- `hermes_cron_status`
- `hermes_cron_last_output`

#### Sessions and memory

- `hermes_sessions_list`
- `hermes_sessions_search`
- `hermes_sessions_export`
- `hermes_memory_status`

#### Skills

- `hermes_skills_list`
- `hermes_skills_view`
- `hermes_skills_sync_profile`
- `hermes_skills_check`

#### Tools and health

- `hermes_tools_list`
- `hermes_toolsets_list`
- `hermes_status`
- `hermes_doctor`
- `hermes_logs_tail`

### Generic requirements

- Typed JSON schemas for every tool.
- Read-only and mutating tools separated.
- Profile/role-scoped permissions.
- Explicit `dry_run` for side effects.
- Audit log for every mutation.
- Redaction before any tool result reaches the model.
- Deterministic error classes.
- Self-test command.
- Unit tests plus live discovery/tool-call tests.

---

## Layer 2: Arcadia MCPs

**Goal:** expose Tony’s private company operating system through safe, typed tools.

This layer is private and opinionated. It should not be assumed upstreamable because it encodes TonyBrain, ArcadiaLabs_Brain, PM/QA/reviewer conventions, launch gates, and company workflow.

### `arcadia_brain_mcp`

Tools for TonyBrain and ArcadiaLabs_Brain.

Candidate tools:

- `arcadia_brain_search`
- `arcadia_brain_append_note`
- `arcadia_brain_create_handoff`
- `arcadia_brain_update_project_state`
- `arcadia_brain_link_artifact`
- `arcadia_brain_sync_shared_context`

Rules:

- TonyBrain remains personal.
- ArcadiaLabs_Brain is the shared operative layer.
- Shared links should use relative paths.
- Personal parent paths stay private/gitignored.

### `arcadia_agentops_mcp`

Tools for spawning and supervising agents.

Candidate tools:

- `arcadia_spawn_claude_worker`
- `arcadia_spawn_codex_worker`
- `arcadia_spawn_spark_worker`
- `arcadia_tail_worker_logs`
- `arcadia_summarize_worker_result`
- `arcadia_reap_stale_processes`

Rules:

- Long-running work should run durably, not inside fragile foreground chat turns.
- Worker outputs need verifiable handles: card IDs, log paths, artifact manifests, commit hashes.
- Parent agents own spec/integration/checks.

### `arcadia_pm_mcp`

Tools for PM escalation and workflow routing.

Candidate tools:

- `arcadia_pm_escalate_gap`
- `arcadia_pm_create_recovery_card`
- `arcadia_pm_route_qa_gate`
- `arcadia_pm_classify_closure`

Rules:

- `NEEDS_FIX`, QA failure, reviewer failure, and release/process gate failure are normal continuation states.
- PM should route scoped recovery by default.
- PM asks Tony/Alice first only when scope, credentials, external access, backend/API semantics, or optional work changes.

### `arcadia_release_mcp`

Tools for release-readiness classification.

Candidate tools:

- `arcadia_release_collect_gate_results`
- `arcadia_release_verify_artifacts`
- `arcadia_release_verify_redaction`
- `arcadia_release_classify`
- `arcadia_release_create_closure_note`

Rules:

- Use artifact manifests, not loose paths.
- Include commit hash, branch status, gates run, gates not run, and honest blockers.
- Classifications should be explicit: `PASS`, `NEEDS_FIX`, `FAIL_NON_BLOCKING_TOOLING_PARITY`, `NOT_RUN_MISSING_CONTEXT`, etc.

---

## Layer 3: Eternia Product MCPs

**Goal:** provide product-specific runtime and QA control for Eternia Launcher and Backend.

### `eternia_launcher_mcp`

Tools for Launcher QA/runtime control.

Candidate tools:

- `eternia_launcher_get_runtime_state`
- `eternia_launcher_get_auth_state`
- `eternia_launcher_get_navigation_state`
- `eternia_launcher_set_tab`
- `eternia_launcher_capture_screenshot`
- `eternia_launcher_run_stage34_label`
- `eternia_launcher_run_stagec_full_parity`
- `eternia_launcher_collect_artifacts`
- `eternia_launcher_redact_artifacts`
- `eternia_launcher_reap_processes`

Acceptance notes:

- Keep direct HTTP/debug runner for diagnosis.
- Final acceptance should require Hermes Native MCP discovery and real tool calls.
- Do not mistake direct shell/PowerShell wrapper success for first-class MCP acceptance.
- Screenshot acceptance requires semantic preconditions, not blind retries.

### `eternia_backend_mcp`

Tools for Backend gates and service health.

Candidate tools:

- `eternia_backend_run_postgres_full_gate`
- `eternia_backend_start_local_infra`
- `eternia_backend_start_keycloak_stack`
- `eternia_backend_check_services`
- `eternia_backend_classify_deploy_readiness`

Acceptance notes:

- Postgres Docker full gate is required before backend push/deploy readiness claims.
- SQLite is only an explicit Tony-requested escape hatch.
- GitHub Actions may be authoritative if local WSL full gate hangs, but local PASS should not be claimed without the local full gate.

---

## Recommended staged build

### Stage 0 — Discovery

Before building custom glue, inspect the latest Hermes built-in Windows, MCP, plugin, proxy, and Codex runtime support.

Questions:

- Does Hermes already expose the needed control-plane tools?
- Can `hermes mcp serve` or `agent/transports/hermes_tools_mcp_server.py` cover part of the design?
- Does the Codex MCP preset or Codex app-server runtime reduce custom implementation?
- What needs to remain private to Arcadia/Eternia?

### Stage 1 — Eternia Launcher MCP hardening

Prioritize Launcher QA because Stage C already proved the path.

Deliverables:

- stable self-test mode
- fresh Hermes Native MCP discovery proof
- real tool-call proof
- direct-runner vs MCP parity where applicable
- screenshot and redaction gates
- closure artifact manifest

### Stage 2 — Hermes Control MCP MVP

Start read-only.

Deliverables:

- list/show/status tools for Kanban, profiles, cron, sessions, skills, tools
- `hermes_doctor` / health probe wrapper
- no mutating tools until permission and audit design is proven

### Stage 3 — Arcadia Brain MCP

Deliverables:

- vault allowlist
- brain search
- handoff creation
- artifact linking
- project-state update
- append-only mutation log

### Stage 4 — AgentOps MCP

Deliverables:

- spawn/monitor Claude, Codex, Spark, QA workers
- standard log paths
- PID/session tracking
- worker result summarization
- stale process cleanup

### Stage 5 — Release MCP

Deliverables:

- aggregate project gates
- collect artifact manifests
- verify redaction scans
- combine PM/QA/reviewer status
- produce launch/revenue-aligned readiness classification

---

## Permission model

Avoid all-or-nothing access.

Suggested profile scopes:

- **Alice:** broad orchestration, but dangerous actions require explicit tools with audit/dry-run.
- **PM:** Kanban, brain project state, release classification, escalation.
- **QA:** product MCPs, artifacts, redaction, gate execution.
- **Claude/Codex dev workers:** repo-limited implementation tools, limited brain read, limited artifact write.
- **Spark/logreader workers:** read-only logs/artifacts unless explicitly promoted.

---

## Error classes

Standardize tool failures so agents can route recovery correctly.

Suggested classes:

- `MISSING_CONTEXT`
- `AUTH_REQUIRED`
- `HOST_ENV_MISSING`
- `TOOLING_PARITY_FAIL`
- `MCP_DISCOVERY_FAIL`
- `MCP_TOOL_CALL_FAIL`
- `QA_GATE_FAIL`
- `REDACTION_FAIL`
- `ARTIFACT_MISSING`
- `PROCESS_STALE`
- `NOT_RUN_MISSING_CONTEXT`

---

## Pitfalls

- Do not create one unrestricted MCP server with every power.
- Do not give every worker every write tool.
- Do not return raw logs containing secrets, tokens, service URIs, or VM-service details.
- Do not rely only on MCP package tests; require fresh Hermes discovery and real tool calls.
- Do not commit real profile config, secrets, or per-run local paths.
- Prefer committed example configs with placeholder-safe `mcpServers` JSON.
- Keep acceptance evidence precise about whether the path was native MCP, direct HTTP, PowerShell, or manual.

---

## Verification checklist

For each MCP server:

- `hermes mcp list` shows the server from the intended profile.
- `hermes mcp test <server>` passes.
- A fresh Hermes session exposes expected `mcp_<server>_<tool>` tools.
- Self-test produces deterministic PASS/FAIL.
- At least one real tool call succeeds through Hermes Native MCP.
- Mutating tools write audit entries.
- Artifact manifests exist and paths are valid.
- Redaction scan passes.
- Profile permissions block out-of-scope tools.
- Final handoff includes commit hash, branch status, commands run, artifact paths, and honest classification.
