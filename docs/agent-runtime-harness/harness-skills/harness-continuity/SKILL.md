---
name: harness-continuity
description: Spawn a helper for heavy investigation, sample bounded progress, return a distilled summary into the parent chat, and resume without replaying the child transcript.
---

# Harness Continuity

Use this when a Harness goal needs a helper agent, a deep investigation, or a narrow
side quest that should not bloat the parent context.

## Spawn And Resume

1. Spawn or route exactly one helper at a time through the visible steering surface.
2. Give the helper a narrow objective, explicit stop condition, and the parent session id
   or return target.
3. Let the helper work in its own context. Do not copy the parent transcript into the
   helper unless a short excerpt is required.
4. Sample progress with the topology `progress_peek` and proof/artifact refs. Never read
   or paste the helper's full transcript into the parent.
5. When the helper is done, return only:
   - one short summary paragraph;
   - concrete proof ids or artifact refs;
   - any blocker or next action.
6. Resume the parent from that summary and refs.

## Return Command

Use the first-class return primitive when you need the helper result to appear in the
parent chat:

```text
hermes harness persona instance return-summary <persona_instance_id> \
  --parent-session-id <session_id> \
  --summary "<bounded summary>" \
  --proof-id <proof_id> \
  --artifact-ref <artifact_ref> \
  --task <task_id> \
  --stage <stage_id> \
  --json
```

The return is bounded by design. It posts a redaction-safe assistant message into the
parent session, records lineage via `returned_to`, and emits `steer.returned` with refs.

## Progress Peek

Use `agent_topology.nodes[].progress_peek` for a last-few-lines sample. Treat it as a
signal, not a source of truth. Intervene only on stall, block, failed gate, or scope drift.

## Never Slurp

Do not inline raw logs, full command output, hidden reasoning, or child chat history into
the parent. Carry pointers. The Harness preserves artifacts; the parent carries the
decision-relevant summary.
