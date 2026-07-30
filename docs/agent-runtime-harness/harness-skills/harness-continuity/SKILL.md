---
name: harness-continuity
description: Spawn a helper for heavy investigation, sample bounded progress, return a distilled summary into the parent chat, and resume without replaying the child transcript.
metadata:
  hermes:
    surfaces: [mission_chat]
    modes: [standard]
    load_policy: recommended
---

# Harness Continuity

Use this when a chat turn needs a helper agent, a deep investigation, or a narrow
side quest that should not bloat the parent context.

## Spawn And Resume

1. Message exactly one helper at a time — `agent_chat_send` from inside a turn, or
   `mission-chat message` against that instance's chat root. There is no assignment
   or dispatch surface; a message is the whole handoff.
2. Give the helper a narrow objective, an explicit stop condition, and the parent
   session id or return target.
3. Let the helper work in its own context. Do not copy the parent transcript into the
   helper unless a short excerpt is required.
4. Sample progress from what the helper actually returns and from artifact refs.
   Never read or paste the helper's full transcript into the parent.
5. When the helper is done, return only:
   - one short summary paragraph;
   - concrete artifact refs or Stage C proof ids;
   - any blocker or next action.
6. Resume the parent from that summary and refs.

## Return Command

Use the first-class return primitive when you need the helper result to appear in the
parent chat:

```text
hermes harness persona instance return-summary <persona_instance_id> \
  --parent-session-id <session_id> \
  --summary "<bounded summary>" \
  --proof-id <stage_c_proof_id> \
  --artifact-ref <artifact_ref> \
  --json
```

The return is bounded by design. It posts a redaction-safe assistant message into the
parent session, records lineage via `returned_to`, and emits `steer.returned` with refs.
The summary is truncated to a hard limit and refs are capped — send pointers, not payload.

## Progress Peek

There is no daemon, run row, or topology `progress_peek` field to poll — those were removed
on 2026-07-30. Progress is what the helper says in chat plus the artifacts it names.
Intervene only on a stall, an explicit block, or scope drift, and intervene by sending
the helper another message on the same chat root so it keeps its context and prompt
cache.

## Never Slurp

Do not inline raw logs, full command output, hidden reasoning, or child chat history into
the parent. Carry pointers. Artifacts stay where they were written; the parent carries the
decision-relevant summary.
