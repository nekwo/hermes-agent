# Turn durability — design (2026-07-08)

Status: **settled with Tony, not yet implemented.** Implementation and
review prompts are delivered operator-side (not committed). Sibling design:
`harness-serve-design.md` (§Follow-up slices item 2 is this document,
expanded).

## Problem

Mission-chat recording is write-ahead for the operator message and
incremental for **streamed** turns only. Two gaps lose provider replies:

1. **Non-streamed turns persist nothing between the operator message and
   completion.** `_cmd_mission_chat_message` and the free-floating chat turn
   (both in `hermes_cli/harness_parts/persona_commands.py`) construct
   `_ChatProtocolV2Emitter` only under `--stream`; without it, a mid-turn
   process death loses the reply and every tool call.
2. **No terminal turn marker.** A killed turn leaves no record — the
   transcript silently lacks a reply. Nothing distinguishes "agent never
   answered" from "turn died mid-flight".

Turn records live in `agent_runtime/mission_chat_turns.py`
(`mission_chat_turns.json`, atomic tmp-replace writes) and are projected into
launcher-visible history by `agent_runtime/persona_chat_history.py` (~line
737: `turn_elements` attached to agent rows keyed by `client_message_id`).

## Contract

### Store — `agent_runtime/mission_chat_turns.py`

- Turn record gains `"state"`: `running | completed | failed | interrupted`.
  Legacy records without `state` read as `completed` (they were only written
  by flows that reached persistence).
- Turn record gains `"updated_at"` (ISO-8601 `Z`), refreshed on every
  persist — the projection needs it to order synthesized rows.
- `persist_mission_chat_turn(..., state: str | None = None)`:
  - `state` given → stored verbatim (validated against the four values).
  - `state=None` → preserve the existing record's state, default `running`
    for a new record.
  - When `state` is provided, an **empty elements list is persistable**
    (write-ahead marker). The current `not elements` early-return applies
    only when `state` is None (legacy call shape).
- New `mission_chat_turn_record(session_id, client_message_id) -> dict | None`
  returning `{turn_id, state, updated_at, elements}` (safe-parsed).
- New `mark_stale_running_turns_interrupted(session_id, active_client_message_id)
  -> list[str]`: every record in the session with `state == "running"` and a
  different client id flips to `interrupted`; returns flipped client ids.
  This is repair-on-next-write: a killed turn is visibly `interrupted` no
  later than the next send in that session.

### Emitter — `_ChatProtocolV2Emitter` (persona_commands.py ~1535)

- New ctor param `emit_frames: bool = True`. When False, **every**
  `_emit_chat_frame` call in the class is suppressed (turn.start in
  `__init__`, segment.delta, segment.end, turn.end, and the tool
  started/finished frames) — element accumulation and `on_update`
  persistence still run. Rationale: non-stream callers guarantee exactly one
  JSON object on stdout (`agent_chat_send._parse_last_json_object` and the
  Launcher's buffered runner depend on it).

### Handlers — both chat-turn sites in persona_commands.py

Applies to `_cmd_mission_chat_message` AND the free-floating chat turn
(the `stream_emitter = ... if stream else None` site near
`_run_free_floating_chat_turn` / `_queue_free_floating_assignment`, ~2379):

1. Construct the emitter **unconditionally** with `emit_frames=<stream flag>`.
2. Before the provider call (after the operator message is appended):
   - `mark_stale_running_turns_interrupted(session_id, client_message_id)`
   - `persist_mission_chat_turn(..., elements=emitter.elements, state="running")`
3. `on_update` lambda passes `state="running"`.
4. Exception path: `emitter.finish(state="failed")` unconditionally
   (frames only when streaming) + persist with `state="failed"`.
5. Success path: persist unconditionally with `state="completed"`
   (replace the current `if stream_emitter is not None` guard).
6. Idempotent-replay path: untouched (record already terminal).
7. `trace_callback` wiring: tool progress must reach the emitter in
   non-stream mode too (in `_cmd_mission_chat_message` it already does via
   the unconditional `trace_callback=_stream_progress`; in the free-floating
   site the `if stream_emitter is not None` gating collapses once the
   emitter always exists).

### Projection — `agent_runtime/persona_chat_history.py`

- Agent rows keep attaching `turn_elements` exactly as today.
- New: for each turn record in the session with `state == "interrupted"`
  that has **no** assistant SessionDB row for its `client_message_id`,
  synthesize a system row:
  - `id`: `"{session_id}:turn-interrupted:{client_message_id}"`
  - `role`: `"system"`, `redaction_status`: `"safe"`
  - `text`: `"Agent turn interrupted before a reply was recorded. Retry the message to run a fresh turn."`
  - `client_message_id` + `turn_id` attached; `timestamp` = record
    `updated_at`.
  - Synthesized rows count toward the existing message tail bound.
- `running` records are NOT synthesized (they are either genuinely live or
  will be repaired to `interrupted` by the next send).
- No `operator_channels.py` change needed: the synthesized row flows through
  the history messages into `_conversation_history_message` and renders as a
  `system_message` in the Launcher with zero Dart changes.

## Explicit non-goals

- No provider-invocation changes (`stream_callback` stays stream-only; we do
  not force streaming to capture partial text of non-streamed turns).
- No launcher (Dart) changes; the "retry" affordance on interrupted rows is
  a later UX slice.
- No new store; `mission_chat_turns.json` keeps its shape plus two fields.
- No changes outside fork-owned code (`agent_runtime/`,
  `hermes_cli/harness_parts/`).

## Test plan (minimum)

Store (`tests/agent_runtime/test_persona_chat_history_curation.py` or new
module):
- state persists with empty elements; `state=None` preserves existing state;
  new record defaults `running`; invalid state rejected/ignored.
- `mark_stale_running_turns_interrupted` flips only other-client running
  records in the same session; returns the flipped ids.
- Legacy record without `state` reads as `completed`.

Emitter:
- `emit_frames=False` writes nothing to stdout while elements accumulate and
  `on_update` fires (capture stdout to assert emptiness).

Handlers (monkeypatched `GPTPersonaRuntime`):
- Non-stream turn: record goes `running` → `completed`; stdout carries
  exactly one JSON object (no protocol-v2 frames).
- Provider raises: record ends `failed`.
- A prior `running` record in the session flips to `interrupted` when a new
  turn starts.

Projection:
- `interrupted` record with no assistant row → synthesized system row with
  `client_message_id`, `turn_id`, `updated_at` timestamp.
- `completed` record → no synthesized row. Assistant-row-present
  `interrupted` record → no synthesized row.

Known pre-existing failures (do not mask, not yours to own): 2 tests in
`tests/agent_runtime/test_persona_assignments.py` are stale against the S1
ISO-timestamp/`kind`/`live_mission` projection shape.

## Hardening (2026-07-08)

Shipped on top of the base implementation after review; branch
`turn-durability-hardening`. The feature contract above is unchanged; the
following retire the structural weaknesses the review flagged.

### Store — single write chokepoint + cross-process lock (W1)

All mutations (`persist_mission_chat_turn`,
`mark_stale_running_turns_interrupted`) go through `_mutate_store`, which
holds an exclusive cross-process file lock (`mission_chat_turns.lock`,
`msvcrt.locking` on Windows / `flock` elsewhere) for the whole
read-modify-write window. Concurrent CLI turns can no longer lose each
other's records. The lock wait is bounded (2s, 10ms polls): on timeout the
mutation is **skipped, never blocked** — a chat turn must not hang on a stuck
lock — and the skip is surfaced as a typed outcome. The opportunistic repair
(`mark_stale…`) returns `[]` on timeout by design; the next send retries it.

### Store — typed persist outcomes (W2)

`persist_mission_chat_turn` returns `MissionChatTurnPersistOutcome`
(`persisted | skipped_no_keys | skipped_empty_legacy |
rejected_invalid_state | rejected_stale_transition | skipped_lock_timeout`).
Handlers attach non-`persisted` outcomes to the response payload
(`turn_persist_outcome`, `turn_write_ahead_outcome`) so no write is lost
silently. The incremental `on_update` lane deliberately ignores its outcome —
it is best-effort by contract (mirrors SEND_POLICY's durable vs best-effort
split); the write-ahead and terminal persists are the durable lane.

### Store — transition authority (W3)

`next_turn_state(current, requested, write_ahead=)` is the single state
authority, consulted inside the locked mutation. Terminal
(`completed`/`failed`) and repair (`interrupted`) writes always win; a fresh
turn start passes `write_ahead=True` and may reopen any record (same-client
retry); an incremental `on_update` flush (`running`, `write_ahead=False`) can
only continue a live record — it is REJECTED (`rejected_stale_transition`)
against settled records, so a late flush from a turn another process already
repaired can no longer resurrect it. The full table is unit-tested in
`tests/agent_runtime/test_mission_chat_turns_hardening.py`.

### Handlers — terminal persist on every post-provider exit (W4)

In both chat-turn sites, everything after the provider reply (assistant
append, token counts, auto-title, instance bookkeeping, payload build/emit)
runs under a guard: a crash there persists `failed` (with the reply preserved
in the error payload, `error_kind: post_turn_persist_failed`) and still emits
exactly one JSON object. If the crash lands after the terminal persist
already succeeded, the handler re-raises instead of emitting a second JSON
object — the record is settled and the stdout contract stays intact.

### Emitter — idempotent finish, no redundant settle race (W5)

`_ChatProtocolV2Emitter.finish` is idempotent (a crash-path second call
cannot emit a duplicate `turn.end` frame) and suppresses `on_update` for the
whole finish window: the caller's terminal persist is the single settling
write, with no stray `running` persist racing it.
