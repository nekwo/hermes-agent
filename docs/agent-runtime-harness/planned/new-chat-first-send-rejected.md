# Planned — the first send into a fresh new chat settled REJECTED, and nothing recorded why

**Owner domain:** the chat turn lane ([05-chat-turn-lane.md](../05-chat-turn-lane.md) §1)
**Status:** OPEN, unreproduced. This is a BUG RECORD, not a plan — there is no fix
to sequence until the next occurrence names its refusal.
**Raised:** 2026-08-22. **Instrumented, not fixed:** `[MissionChatOutcome]` landed the
same day and has not yet caught one.

## The incident

2026-08-22 02:00:47Z — the timestamp the launcher's own anchor records
(`mission_agent_chat_runtime_controller.dart:1556`). An operator opened a new chat
with `alice` and the FIRST send settled `REJECTED`. The refusal envelope was recorded
NOWHERE: a bare `[MissionChatTiming]` line (correct — no phases happened) and nothing
else. The `error_kind` and message lived only in widget state until the next send
cleared them.

## What was ruled out

- **The mint is sound.** `_cmd_persona_instance_open_new_chat`
  (`hermes_cli/harness_parts/persona_commands.py:1003`) ensures the SessionDB row
  BEFORE binding, and that row existed in `profiles/base/state.db` 3.5 s before the
  rejected settle, with the correct source and owner.
- **Neither §1 guard reproduces it.** `unknown_chat_session` (`:2236-2251`) and
  `foreign_chat_session` (`:2252-2280`) were replayed against the same inputs under
  BOTH `HERMES_HOME` values; every refusal either can produce PASSES.

## What is still implicated

The explicit-`session_id` lane. Default-chat sends omit `session_id` and never reach
those two guards at all, so a refusal on a *new chat* send points at the lane that does.

Why the evidence was missing rather than lost: the serve runs sends in-process via argv
dispatch, so a guard rejection returns an envelope to the caller and logs **nothing**
server-side. There was no server log to go and read.

## The instrument that now stands guard

`[MissionChatOutcome] turn_id=… status=… error_kind=… message="…"` fires at the settle
chokepoint for every turn that settles WITHOUT acceptance
(`mission_agent_chat_runtime_controller.dart:1555-1571`, launcher `76b200ab9`), through
`Logger` so the release diag tee carries it. It will name the `error_kind` of the next
occurrence in `%TEMP%\eternia_launcher_diag.log`.

## What to compare when it recurs

1. The `[MissionChatOutcome]` `error_kind` — if it is `unknown_chat_session` or
   `foreign_chat_session`, the guard replay above was wrong about its inputs, and the
   inputs are what to capture next.
2. Whether the SessionDB row exists in `profiles/base/state.db` at settle time, and its
   owner and source columns — the same read that acquitted the mint.
3. The `HERMES_HOME` the serve actually ran under versus the one measured against; the
   launcher spawns with `profiles/base` (see [04](../04-boot-and-lifecycle.md) Stage 0).
4. Whether the send carried an explicit `session_id` at all — that is the branch this
   record is narrowed to, and the first thing a recurrence can confirm or clear.

## Prior art — the same phantom-root class

Fixed twice before, both times by making the root exist before something dereferenced
it: `63768819a3` (drag-drop create) and `b912cce88a` (dispatch mint). A third instance
of that class is the leading hypothesis; nothing yet proves it.
