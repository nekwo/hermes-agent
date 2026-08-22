# Turn durability — design (2026-07-08)

Status: **shipped and superseded by MC-CHAT-CONTINUITY (2026-07-20).** This
document retains the original incremental durability design and its hardening
history. The production state machine is now `pending | executing |
outcome_unknown | native_committed | projected | abandoned | budget_exhausted`;
native structured SessionDB history is the sole continuation authority. Sibling
design: `harness-serve-design.md` (§Follow-up slices item 2 is this document,
expanded). The `budget_exhausted` terminal and the graceful wall-budget
checkpoint that produces it are specified in **Wall-budget checkpoint
(2026-07-26)** below.

The current recovery contract is stricter than the original
`running/completed/failed/interrupted` design below:

- the journal is keyed by stable root, `client_message_id`, and turn ID;
- the provider boundary is recorded immediately before submission;
- an unprovable in-flight result becomes `outcome_unknown` and is never
  automatically resubmitted;
- a turn that ran out of WALL CLOCK is not unprovable — it becomes
  `budget_exhausted`, which is terminal and needs no operator resolution;
- `native_committed` retries repair projections without calling the provider;
- explicit abandon requires an exact tuple match, after which resend uses a
  fresh client message ID;
- a cross-process root lease covers recovery, rehydrate, provider execution,
  native commit, and projection.

Use the persona-chat commands in
`harness-skills/harness-runtime-model/SKILL.md` for the current operator path.

## Wall-budget checkpoint (2026-07-26)

**Incident.** An operator sent a Neko mission-chat turn with `--max-seconds
540`. Neko relayed to Dev via `agent_chat_send`; the whole chain runs in ONE
process sharing one wall. At exhaustion the harness killed mid-API-call (`⚡
Interrupted during API call`, blocker `live run budget exceeded:
wall_seconds=540`, `error_kind=chat_turn_outcome_unknown`) and **both** turns
froze as `outcome_unknown`, each needing a manual `turn-resolve --action
abandon` plus a full re-brief — the native context was gone.

Two structural gaps, fixed as one slice:

### 1. Budget visibility

`agent_runtime/turn_budget.py` is the single authority for a turn's wall
window (`TurnWallBudget`: `total_seconds`, absolute `deadline_epoch`, `shared`).
`resolve_turn_wall_budget` folds `--max-seconds` and the shared
`--relay-deadline-epoch` into ONE object; the mission-chat command resolves it
once and uses it for **both** the agent-visible HUD line and the wall the runner
enforces, so the two can never drift.

- The line rides the runtime-context envelope's **volatile tail**
  (`render_runtime_context_envelope(volatile_content=…)`), which is emitted on
  *every* delivery — `snapshot`, `unchanged`, and `unavailable`. A cached
  `unchanged` body would otherwise show the agent a stale countdown.
- `turn_budget` is declared `volatile` on its `runtime_hud.HUD_FIELDS` row,
  which is the ONE declaration both `situational_hud_revision` and
  `render_situational_hud_block` derive from (`stable_hud_fields`), so a
  per-turn countdown never re-snapshots the whole stable HUD block *and* cannot
  be rendered into the hashed body by a later edit. It still rides the resolved
  HUD dict, so the operator's CONTEXT peek and the observability row show the
  same number the agent saw.
- A relayed hop inherits the chain deadline, so the **target's** HUD shows the
  shared remaining budget: a supervisor can see what window a dispatch has
  instead of briefing 50 minutes of work into a 9-minute hop.

### 2. Graceful checkpoint

Reserve at the end of a turn: `max(60s, 15% of the original budget)`, capped so
at least `CHECKPOINT_MIN_WORKING_SECONDS` (30s) of working window survives. A
budget too small to reserve anything has **no** graceful phase (`supports_
checkpoint == False`) and keeps only the hard wall — honest, not silently
degraded.

`profile_runner.WallBudgetCheckpoint` engages once, from whichever fires first:

- the **tool gate** — checked before each tool execution via the existing
  tool-start progress seam (deterministic, unit-testable without sleeping);
- a **timer** armed at `deadline - reserve` (backstop for a turn stuck in a long
  provider call with no tool events).

Engaging does three things and never interrupts:

1. `agent.steer(nudge)` injects a system-side "produce your final checkpoint
   reply now; report state honestly" into the next tool result — in-band, so the
   model actually sees it;
2. `turn_budget.drain_iteration_budget(agent)` consumes the agent's remaining
   loop iterations through the documented `IterationBudget.consume()` API. The
   upstream loop therefore launches **no new tool executions** and exits at its
   next iteration boundary, and its finalizer takes exactly ONE toolless
   "summarise" call — the final checkpoint reply — reusing the mechanism upstream
   already has for iteration exhaustion. **No upstream file is modified.** The
   in-flight tool batch is deliberately allowed to finish: aborting a running
   tool is precisely the mid-kill this replaces;
3. emits a typed `run.progress` event (`phase=wall_budget_checkpoint`,
   `step=wall_budget_checkpoint_opened`).

The old hard wall stays armed at the real deadline as the **last resort** — it is
no longer the first thing that happens when a turn runs long. When it fires,
`RunBudgetExceeded` now carries a typed `wall_budget` block so the caller can
still settle the turn as `budget_exhausted`.

Granularity note: the stop lands at a loop-iteration boundary, which is the
honest boundary — "before starting each provider call or tool execution" means
before the next batch, not mid-tool.

### Terminal state + CLI contract

`budget_exhausted` transitions:

| from | to |
| --- | --- |
| `pending` / `executing` / `outcome_unknown` | `budget_exhausted` |
| `budget_exhausted` | `budget_exhausted`, `native_committed` |

- It is **not** in `INFLIGHT_TURN_STATES`: no repair sweep reopens it, no
  next-send flip turns it into `interrupted`.
- It does **not** resurrect to `pending`; a retry uses a new
  `client_message_id`, like any settled turn.
- `budget_exhausted → native_committed` preserves the legacy-interrupted
  convention: a reply proven durable *after* the settle still wins, and can then
  project.
- `turn-resolve` still accepts **only** `outcome_unknown`. A budget-settled turn
  is not resolvable and does not need to be.

CLI JSON (`harness mission-chat message`), documented shape:

- **Graceful checkpoint that produced a reply** — `ok: true`, exit `0`,
  `execution_state: "budget_exhausted"`, `budget_exhausted: true`,
  `turn_resolution_required: false`, `wall_budget: {…}`. `ok` stays true because
  a real reply was produced and committed; a relay caller must not treat it as an
  error. The journal keeps its normal `native_committed → projected` walk (the
  reply must project like any other) and carries `budget_exhausted` /
  `budget_trigger` / `budget_summary` metadata for provenance.
- **Hard wall — not even the final call fit** — `ok: false`, exit `2`,
  `execution_state: "budget_exhausted"`, `error_kind:
  "chat_turn_budget_exhausted"`, `turn_resolution_required: false`,
  `checkpoint_summary` naming the tool calls that DID complete. The record
  settles at `budget_exhausted`.
- **Resend of an already-budget-settled `client_message_id`** — `ok: false`,
  exit `2`, same typed kind, `error: "…settled and needs no resolution"`.

In every case `next_expected` points at a **new `client_message_id`** and never
at `turn-resolve` (guarded by `tests/hermes_cli/test_mission_chat_budget_payload.py`).

Tests: `tests/agent_runtime/test_turn_budget_checkpoint.py` (threshold math,
relay clamp, loop gate, state machine, HUD lane) and
`tests/hermes_cli/test_mission_chat_budget_payload.py` (CLI payload contract).

### Terminal marker rows + tool settlement (2026-07-26)

A terminal state has a second obligation the state machine above does not
express: **it must say so in the transcript**, because the marker row is what
settles the turn's still-running tool rows.

The chain, and where it broke. `persona_chat_history._terminal_turn_marker_rows`
synthesizes a typed system row for a turn that settled terminally **without a
recorded reply**; `operator_channels._settle_terminal_tool_calls` reads those
marker rows and flips every still-`running` `tool_call` of the same turn to a
settled status. Both halves keyed on the legacy `interrupted` state alone. So
the hard-wall case above — which lands `budget_exhausted` and never writes an
assistant row — emitted **no marker**, settled **nothing**, and left any
`tool_started`-without-`tool_finished` row spinning in the Mission Control
cockpit forever, for a turn that had been over for minutes.

Both halves are now **table-driven**, so a third terminal state settles its
tools by construction rather than by remembering to update two string
comparisons in two modules:

| turn state | marker `kind` | row id slug | conversation `status` / title |
| --- | --- | --- | --- |
| `interrupted` | `turn_interrupted` | `turn-interrupted` | `interrupted` / "Turn interrupted" |
| `budget_exhausted` | `budget_exhausted` | `turn-budget-exhausted` | `budget_exhausted` / "Wall budget reached" |

Producer: `persona_chat_history.TERMINAL_TURN_MARKERS`, guarded at import
against declaring a state the turn store does not call terminal (a marker on an
in-flight state would announce a LIVE turn as over — the worse failure).
Presenter: `operator_channels._TERMINAL_TURN_MARKER_PRESENTATION`, whose keys
are pinned equal to the producer's kinds.

Three deliberate choices:

- **No new marker vocabulary.** The Launcher already consumes both kinds
  (`mission_agent_chat_adapter.dart`: `_turnInterruptedFlowMessage` → retry
  affordance, `_budgetExhaustedFlowMessage` → graceful-checkpoint marker).
  **No launcher change is required** for either half of this fix.
- **The prose distinguishes the two.** The budget marker must not say "Retry the
  message" — the turn settled gracefully and its work may be committed, so
  telling the operator to re-run is a lie that costs a full re-run.
- **The settled tool status stays `interrupted`; the reason rides beside it.**
  `interrupted` describes the CALL (cut off, will never finish) and is the only
  settled-tool vocabulary the Launcher's trace renderer already recognises
  (`mission_trace_content_renderer.dart`:
  `interrupted|cancelled|canceled|aborted` → stop glyph). Inventing a
  `budget_exhausted` tool status would stop the spinner but render as an unknown
  state. The turn-level reason travels as the additive typed `settled_reason`
  (and `settled_state` on the marker), carrying the turn store's own state name
  — so a settled call never has to lie about **why** in order to stop spinning.

Tests: `tests/agent_runtime/test_terminal_turn_settlement.py` (both layers, both
states, the table guards, the graceful-reply case that must synthesize nothing,
the in-flight turn that must never be marked over, the pre-`settled_state`
archive row, and the finished call that must keep its real outcome).

### One vocabulary, three buckets, guarded (2026-07-27)

The section above fixed the same defect **twice**, in two modules ~700 lines
apart. Recurrence is the finding: the bug was never either literal, it was that
a consumer was free to spell one. So the vocabulary is now a single owned table
in `agent_runtime/mission_chat_turns.py`, and every consumer reads it.

Every known state belongs to **exactly one** lifecycle bucket:

| bucket | states | meaning |
| --- | --- | --- |
| `INFLIGHT_TURN_STATES` | `pending`, `executing`, `outcome_unknown`, `running` | an executor still owes a settlement; repair sweeps flip these, retention/GC protect them |
| `SETTLING_TURN_STATES` | `native_committed` | the reply is durable, the projection is not: never repair-flipped (a flip would destroy a recorded reply), not yet terminal |
| `TERMINAL_TURN_STATES` | `projected`, `abandoned`, `budget_exhausted`, `completed`, `failed`, `interrupted` | settled; no repair, no operator resolution |

`SETTLING_TURN_STATES` is new **as a name only**. `native_committed` had always
been in neither set — an unclassified state no guard could see, which is
precisely the shape of the wall-budget bug. Naming it makes the partition
provable; it changes no decision.

Three decision sets sit on top, each one a question a consumer actually asks:

| set | states | asked by |
| --- | --- | --- |
| `REPLY_RECOVERABLE_TURN_STATES` | `executing`, `outcome_unknown`, `budget_exhausted` | a resend that finds a durable reply promotes to `native_committed` from these |
| `RESEND_BLOCKING_TURN_STATES` | `executing`, `outcome_unknown` | …and with no such proof, a resend from these is refused pending resolution |
| `OPERATOR_RESOLVABLE_TURN_STATES` | `outcome_unknown` | the only state `turn-resolve --action abandon` accepts |

Import-time guards (raised, not asserted, so `python -O` cannot strip them —
same convention as `TERMINAL_TURN_MARKERS`): every set names only known states;
the three buckets are pairwise disjoint AND cover the known universe; the
refusal ladder is nested (`resolvable ⊆ blocking ⊆ recoverable`);
`REPLY_RECOVERABLE_TURN_STATES` equals the set of states from which
`_JOURNAL_TRANSITIONS` accepts a promotion to `native_committed` (it is a VIEW
of the transition table, not a second opinion); and the transition table plus
the legacy→journal alias map name only real states.

Consumers hold no literals. `hermes_cli/harness_parts/persona_commands.py` runs
inside `harness.py`'s globals, so a vocabulary name it uses but `harness.py`
does not import is a `NameError` on a live chat turn rather than an import
error — an AST guard covers that seam too.

Tests: `tests/agent_runtime/test_turn_state_vocabulary.py` — the full state ×
bucket classification asserted through the real store functions (repair sweep,
boot sweep, retention cap, `abandon`), the journal transition acceptance
matrix, an independent copy of the classification table that must agree with
the runtime one, and one case per import guard proving each can actually fail.

Known gap, deliberately unchanged: the marker synthesizer produces rows for
`interrupted` and `budget_exhausted` only. `projected` / `completed` have a
reply and `abandoned` was resolved by the operator, so those need none — but
`failed` is terminal, reply-less, and marker-less, which is a real hole. Closing
it is a behavior change and was out of scope for a consolidation pass; it is
recorded here rather than left silent.

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
- New `mark_stale_inflight_turns_interrupted(session_id, active_client_message_id)
  -> list[str]`: every record in the session with `state == "running"` and a
  different client id flips to `interrupted`; returns flipped client ids.
  This is repair-on-next-write: a killed turn is visibly `interrupted` no
  later than the next send in that session.
  - 2026-07-25 extension: renamed `mark_stale_inflight_turns_interrupted` and
    widened to every in-flight state (`running`, `pending`, `executing`,
    `outcome_unknown`) after a live incident left a journal record frozen at
    `executing` when its executor process was killed mid-turn — the old
    `running`-only filter never repaired journal-lane corpses. A serve-boot
    orphan sweep (`repair_orphaned_chat_turns` in `persona_chat_continuity`)
    additionally probes each in-flight session's root lease non-blocking and
    repairs sessions whose lease is free — kernel lease release on process
    death makes "in-flight record + acquirable lease" proof of a dead
    executor, so a launcher restart settles frozen turns with no operator
    action.

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
   - `mark_stale_inflight_turns_interrupted(session_id, client_message_id)`
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
- `mark_stale_inflight_turns_interrupted` flips only other-client running
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
`mark_stale_inflight_turns_interrupted`) go through `_mutate_store`, which
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

### Performance (2026-07-08)

Branch `turn-durability-perf`. The store rewrites the whole file under the
cross-process lock on every persist (~69ms at ~1,000 records pre-slice), and
in streaming mode every delta chunk triggered one full locked rewrite. Three
changes defuse the growth bomb without touching any durability guarantee:

**Debounced incremental flushes.** `delta()`-driven `on_update` flushes are
debounced to at most one per interval
(`_CHAT_TURN_INCREMENTAL_FLUSH_INTERVAL_SECONDS = 0.25` in
`persona_commands.py`), via an injectable monotonic-clock seam on the emitter
(`clock=time.monotonic` ctor param) — pure bookkeeping on the synchronous
call path, no threads/timers. Segment ends and tool events flush
immediately (rare, and they carry the trace visibility operators debug
from). The write-ahead `running` marker, the terminal `completed`/`failed`
persists, and the `mark_stale` repair are handler calls that never pass
through the debounced path — the durable lane stays immediate and
unthrottled. A suppressed delta is never lost: elements accumulate in
place, so the next flush of any flavor carries the full accumulated text.
**Traded guarantee:** after a mid-stream kill, the recovered partial text
may be up to one debounce interval (250ms) stale. Losing tool events or
terminal states remains impossible.

**Retention on write.** `_mutate_store` applies deterministic retention on
every changed write, inside the same lock and atomic tmp-replace: the
`_RETENTION_MAX_TURNS_PER_SESSION = 100` most recent turn records per
session by `updated_at`, and the `_RETENTION_MAX_SESSIONS = 50` most
recently updated sessions (older sessions drop wholesale). `running`
records are never evicted (a live concurrent turn must not lose its
write-ahead marker), nor is the record/session being written — a session
may transiently exceed its bound rather than lose live state. Retention is
invisible to the caller's typed outcome. The per-session bound sits safely
above the projection's displayable tail
(`MAX_PERSONA_CHAT_MESSAGE_TAIL = 40` in `persona_chat_history.py`), so no
displayable agent row can lose its `turn_elements` and interrupted-turn
synthesis keeps working for every turn the Launcher can still display.

**Compact serialization.** `_write_store` uses `separators=(",", ":")`
instead of `indent=2` (still `sort_keys=True, ensure_ascii=False`): ~22%
smaller file at 1,000 records.

Measured (same machine, ~1,000-record store): 69.0 → 31.0 ms/persist;
store 2,301KB → 1,797KB; a simulated 200-delta streamed turn over 20s went
from 201 incremental store writes to 67 (interval-bound, ≤80). Coverage:
`tests/agent_runtime/test_mission_chat_turns_perf.py` plus the
retention-churn synthesis test in
`tests/agent_runtime/test_persona_chat_history_curation.py`.
