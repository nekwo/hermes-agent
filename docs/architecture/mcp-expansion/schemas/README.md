# Schemas

Pinned contracts referenced from the stage docs. These are the **source of truth**; any implementation drift is a bug.

| File | What it pins | Cited by |
|------|--------------|----------|
| [`verification.schema.jsonc`](verification.schema.jsonc) | Per-server verify.json shape every closure produces | [`07-cross-cutting.md` §6](../07-cross-cutting.md#6-verification-checklist-executable-form) |
| [`control_events.sql`](control_events.sql) | `control_events` table DDL in shared kanban.db | [`08` §R1](../08-second-pass-audit-and-expansion.md#r1--audit-log-location-stage-2--stage-4--cross-cutting-4-correction), [`11`](../11-stage-2.5-hermes-control-mutate.md), [`09`](../09-stage-4.5-arcadia-pm-mcp.md), [`10`](../10-stage-6-eternia-backend-mcp.md) |
| [`worker_handoff.schema.json`](worker_handoff.schema.json) | Stage 4 `summarize_worker_result` envelope | [`05-stage-4-agentops-mcp.md`](../05-stage-4-agentops-mcp.md), [`12` §F](../12-third-pass-addendum.md#f-worker-handoff-json-schema) |
| [`closure_manifest.schema.json`](closure_manifest.schema.json) | Stage 1 / Stage 5 closure manifest shape | [`02` §5](../02-stage-1-launcher-mcp.md#5-closure-artifact-manifest), [`06`](../06-stage-5-release-mcp.md) |
| [`release_classification.schema.json`](release_classification.schema.json) | Stage 5 `classify` output | [`06`](../06-stage-5-release-mcp.md#classification-rubric-encoded) |
| [`tool_catalog.example.jsonc`](tool_catalog.example.jsonc) | The Hermes-side equivalent of [`tool/stagec_qa_mcp_server/lib/tools.dart`](../../../../../../Unreal%20Engine/Engine/Launcher/EterniaLauncher/tool/stagec_qa_mcp_server/lib/tools.dart) — single source of truth for tool schemas, dispatch contract | [`03` Decision-1](../03-stage-2-hermes-control-mcp.md#decision-1-new-server-vs-extend-hermes_tools_mcp_serverpy), [`12` §H](../12-third-pass-addendum.md#h-test-fixture-conventions) |
