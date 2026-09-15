# MCP Expansion — fleshed-out stages

This folder fleshes out the staged build in [`../mcp-expansion-roadmap.md`](../mcp-expansion-roadmap.md). It is **docs only** — no code is written here; each stage doc is a contract that an implementation PR can be measured against.

## Reading order

### Stage docs

1. [`00-deep-audit.md`](00-deep-audit.md) — pass-1 inventory of what already exists in Hermes / Eternia / Arcadia.
2. [`01-stage-0-discovery.md`](01-stage-0-discovery.md) — verdict-matrix audit gate before any code lands.
3. [`02-stage-1-launcher-mcp.md`](02-stage-1-launcher-mcp.md) — close out the Stage C launcher MCP that already exists at `tool/stagec_qa_mcp_server`.
4. [`03-stage-2-hermes-control-mcp.md`](03-stage-2-hermes-control-mcp.md) — read-only Hermes control plane (kanban / cron / profiles / sessions / skills / tools / health / webhooks). Upstream-worthy.
5. [`11-stage-2.5-hermes-control-mutate.md`](11-stage-2.5-hermes-control-mutate.md) — mutating control-plane verbs, separate server, triple-gated.
6. [`04-stage-3-arcadia-brain-mcp.md`](04-stage-3-arcadia-brain-mcp.md) — typed brain bridge with vault allowlist, append-only mutations.
7. [`05-stage-4-agentops-mcp.md`](05-stage-4-agentops-mcp.md) — typed spawn / monitor / reap for the 11 worker profiles.
8. [`09-stage-4.5-arcadia-pm-mcp.md`](09-stage-4.5-arcadia-pm-mcp.md) — PM routing, recovery cards, escalation, doctrine-bullet citations.
9. [`06-stage-5-release-mcp.md`](06-stage-5-release-mcp.md) — encode the company release classification rubric.
10. [`10-stage-6-eternia-backend-mcp.md`](10-stage-6-eternia-backend-mcp.md) — wrap `scripts/test.sh` + Docker stack + staging deploy.
11. [`13-upstream-mcp-stdio-env-overrides.md`](13-upstream-mcp-stdio-env-overrides.md) — upstream-targeted one-shot stdio env overlays for MCP discovery/test/tool-call workflows.

### Cross-cutting + audit passes

- [`07-cross-cutting.md`](07-cross-cutting.md) — permissions matrix, error classes, redaction pipeline, audit-log targeting, verification checklist.
- [`08-second-pass-audit-and-expansion.md`](08-second-pass-audit-and-expansion.md) — pass-2 revisions; surgical diff table.
- [`12-third-pass-addendum.md`](12-third-pass-addendum.md) — pass-3 routines audit, `mcp_serve` ↔ control reconciliation, token/cache/concurrency rules, worker handoff schema pin, codex-runtime migration path.

### Pinned contracts ([schemas/](schemas/))

- [`verification.schema.jsonc`](schemas/verification.schema.jsonc) — per-server closure verify.json
- [`control_events.sql`](schemas/control_events.sql) — audit-log table DDL
- [`worker_handoff.schema.json`](schemas/worker_handoff.schema.json) — Stage 4 envelope
- [`closure_manifest.schema.json`](schemas/closure_manifest.schema.json) — Stage 1 / Stage 5 manifest
- [`release_classification.schema.json`](schemas/release_classification.schema.json) — Stage 5 classify output
- [`tool_catalog.example.jsonc`](schemas/tool_catalog.example.jsonc) — Hermes-side single-source-of-truth shape

### Example configs ([examples/](examples/))

One `*.mcpServers.example.json` per server, with placeholder env vars only — see [`examples/README.md`](examples/README.md) for the placeholder list.

### Markdown skeletons ([templates/](templates/))

- [`handoff.template.md`](templates/handoff.template.md)
- [`closure_note.template.md`](templates/closure_note.template.md)
- [`escalation.template.md`](templates/escalation.template.md)
- [`recovery_card.template.md`](templates/recovery_card.template.md)

## What changed across the three passes

| Pass | Output | High-impact corrections |
|------|--------|-------------------------|
| **1** | Audit (`00`), Stages 0–5 (`01`–`06`), cross-cutting (`07`) | First inventory of MCP entry points, profile classes, doctrine surfaces. |
| **2** | Revisions doc (`08`) | Audit-log targeting (state.db is FTS, not events; kanban.db is shared; `task_events` keyed on task_id — new sibling `control_events` table); multi-board kanban with active board = `eternia-launcher`; Stage C credential contract pinned; `inherit_toolsets` unsafe for `spark_logreader`; dispatch via `model_tools.handle_function_call`; codex-app-server validates standalone-server choice. |
| **3** | Stages 4.5 + 6 + 2.5 (`09`, `10`, `11`), addendum (`12`), examples/, schemas/, templates/, in-place Pass-2 diff application | Missing stage docs filled (PM, Backend MCP, mutating control); routines doc audit (webhook + script-injection patterns); messaging ↔ control reconciliation (independent servers, surface-suffixed CLI); token / cache / concurrency rules; worker handoff schema pinned; migration path for the codex-runtime kanban tools vs Stage 2.5 (they coexist, prefix differentiates audit trail). |

## What this folder does NOT do

- It does not commit code. Implementation is gated on the Stage 0 discovery audit doc landing first.
- It does not write `mcpServers` JSON into any real profile. Example JSON in `examples/` uses placeholders only.
- It does not duplicate the roadmap. The roadmap is the why; these docs are the how, with code references.
- It does not page anyone. Even PM escalation tools (Stage 4.5) write notes; out-of-band paging is human-only.
- It does not expose production deploys. Per doctrine, that is Tony only.
