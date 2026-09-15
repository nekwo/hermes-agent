# Neko persona identity and prompt ownership

Neko's live Mission Control persona is bound to the `neko` Hermes profile. This
document records the current ownership model and the checks needed when identity
or prompt assembly changes. It replaces the obsolete one-time deploy runbook.

## Current ownership model

The Mission Control chat system prompt is assembled in this order:

1. **Hermes core** — universal runtime capabilities and environment guidance.
2. **Runtime identity** — Mission Control names the selected persona and prevents
   self-relay.
3. **Profile SOUL** — the persona profile owns durable character, values, and
   voice.
4. **Operator-channel rules** — Mission Control owns tool, permission, goal,
   clarification, and anti-fabrication behavior.
5. **Workspace/session context** — optional AGENTS.md and surface override.
6. **Profile memory** — optional volatile profile context.
7. **Conversation history** — prior redaction-safe turns.
8. **Runtime Situation HUD** — injected into the operator's user turn, not the
   system prompt.

For Neko, the SOUL source is
`X:\Eternia\.hermes\profiles\neko\SOUL.md`. The repository reference is
`docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/neko_SOUL_draft.md`
(moved here in the 2026-08-22 docs consolidation; bytes still match the live
profile SOUL, stripped).

## What belongs in SOUL.md

SOUL owns Neko's durable identity, personality, values, mission instincts,
quality bar, and voice. It must not duplicate runtime mechanics such as tool
names, permission policy, self-relay guards, role routing rules, or per-turn HUD
behavior. Those are enforced by the runtime identity and operator-channel rule
layers, where every Mission Control persona receives them consistently.

## Staleness checks

When this contract changes:

- Confirm the live persona remains bound to the `neko` profile.
- Compare the live SOUL with the repository reference.
- Inspect Context Inspector and verify the distinct Runtime identity, Profile
  SOUL, and Operator-channel rules rows.
- Verify Profile SOUL reports its source path/hash and whether it was injected.
- Verify a blank session surface override is shown as inactive, not as a missing
  persona layer.
- Verify Runtime Situation HUD appears under turn injection.
- Send a direct Neko message and confirm it answers without relaying to itself.

## Historical incident

The original self-relay incident came from a channel that lacked an explicit
runtime identity while profile memory described Neko as another agent. The
runtime now supplies an explicit first-person identity guard, memory loading is
opt-in, and Neko owns a dedicated profile. Those safeguards belong to runtime
code; they should not be copied back into SOUL.md.
