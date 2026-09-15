# Agent Runtime Harness — Index

> Purpose: Hermes-native persona runtime for Mission Control. **Chat is the only
> lane** — an operator (or agent) messages an on-level persona instance's chat
> root; the runtime owns identity, chat continuity, the board, realms/workspaces,
> and an enforcement-free agent graph. The goal/task mission lane (daemon, stage
> graph, proof gates, role gating) was removed on 2026-07-30 — see doc 16.

## Live truth (read these)

1. **[16 — Mission Lane Removal](16-mission-lane-removal.md)** — the executed
   removal plan, S0–S12, with the dependency-map corrections, operator rulings,
   hazards table, and final acceptance. **What the system is now, defined by what
   it no longer is.**

2. **[17 — Upstream Boundary Ledger](17-upstream-boundary-ledger.md)** — which
   files outside the fork boundary the removal touched, verified fork-vs-upstream
   ownership, merge guidance, and the verified revert recipe for the S12
   security-posture commit (`933aa3d97`).

3. **The mission-chat lane docs** — the live operating surface:
   - [mission-chat-turn-context.md](mission-chat-turn-context.md) — the chat turn
     contract.
   - [mission-chat-mcp-admission.md](mission-chat-mcp-admission.md) — MCP
     admission (profile-declares-the-server rule; records the R-1 role-floor
     removal).
   - [mission-chat-terminal-envelope-grants.md](mission-chat-terminal-envelope-grants.md) —
     terminal envelope grants (records the R-2 floor removal; §2.1 records the
     2026-08-09 permission-mode grants).
   - [UNBOUNDED_DEFAULT_PLAN_2026-08-09.md](UNBOUNDED_DEFAULT_PLAN_2026-08-09.md) —
     **the standing tool-access posture (implemented).** `unbounded` is the
     runtime-wide default (`agent_runtime.tool_permissions.default_mode`); the
     session permission store is the RESTRICTION lane; registry hygiene and MCP
     cross-persona scoping never yield to it; every formerly-refused terminal
     command is receipted with the mode that allowed it.
   - [mission-chat-lane-gap-audit.md](mission-chat-lane-gap-audit.md) — the lane
     gap audit.
   - [chat-session-presence-authority.md](chat-session-presence-authority.md),
     [turn-durability-design.md](turn-durability-design.md),
     [run-budget-accounting.md](run-budget-accounting.md).

4. **Read/write path hardening** — live data-layer docs:
   [12 — Read-Path Freshness](12-read-path-freshness-hardening.md) ·
   [13 — Write-Path Intent Integrity](13-write-path-intent-integrity.md) ·
   [14 — Snapshot Core Build Performance](14-snapshot-core-build-performance.md) ·
   [mission-control-stream.md](mission-control-stream.md).

5. **Serve + operations** —
   [harness-serve-design.md](harness-serve-design.md) ·
   [serve-runtime-truth.md](serve-runtime-truth.md) ·
   [env-determinism-audit.md](env-determinism-audit.md).

6. **Skills** — the agent-facing skills live in
   [harness-skills/](harness-skills/) (repo source; installed to the shared root
   by `harness install-harness-skills` — never edit the shared copy). Rewritten
   for the chat-only lane on 2026-07-30.
   [SKILL_INBOX_PROMOTION_DESIGN_2026-07-24.md](SKILL_INBOX_PROMOTION_DESIGN_2026-07-24.md)
   covers skill-inbox promotion.

7. **Personas** —
   [neko-persona-identity-deploy.md](neko-persona-identity-deploy.md) ·
   [neko_SOUL_draft.md](neko_SOUL_draft.md). Personas and profiles are **data**;
   nothing in code declares them (S11).

## Historical — describes the removed mission lane

Retained for archaeology with dated headers; do not implement from these.

- **[01 — Architecture](01-architecture.md)** — the locked entity model. Part C's
  node/steering-edge graph is the design origin of the kept
  `agent_runtime/flow_graph.py` + `steered_by` runtime graph; the Goal/Task chain
  is gone.
- **[02 — Blueprint Goal-Flow Engine](02-execution-engine.md)** — the removed
  stage-graph engine. Its §Identity profile→persona substrate survives via the
  permanent `blueprints/resolve.py` shim.
- **[04 — Decision/HUD Simplification](04-decision-hud-simplification-map.md)** —
  its §Steering sections fathered the kept `steered_by` edges.
- **[05 — Runtime Data Storage](05-runtime-data-enterprise-storage.md)** — mostly
  live (projector, feed, resolution, backups); its goal/run/proof/incident DDL is
  gone.
- **[08 — Root Node + Self-Looped Sub-Agents](08-blueprint-as-script-collapse.md)** —
  the "no judgment in Python" principle that shaped the chat-only lane.
- **[delivery-directive.md](delivery-directive.md)** — declaration path removed;
  `delivery_directive.py` still has importers.

Deleted on 2026-07-30 (operator ruling; recoverable from git history):
`03-retirement-ledger.md`, `06-implementation-prompt.md`,
`06-recursive-agent-supervised-execution.md`,
`07-selfdrive-gap-audit-prompt.md`, `09-root-node-execution-prompts.md`,
`10-root-node-n3-burn-in-ledger.md`, `11-neko-fork-join-dehardwire.md`,
`15-legacy-orchestrator-retirement.md`. The old implementation reference
`agent_runtime/docs/blueprint_goal_flow_stages.md` went with the engine in S7.

*Historical note:* Stages 01–77 (the exploratory journey) were removed from the
working tree on 2026-06-25. To read the original staging, check out a commit
before that date, e.g.
`git show baf366cb4:docs/agent-runtime-harness/76-unified-template-instance-chat-goal-model.md`.
