# Mission Control Goal-Room & Chat-Parity Stages

> **2026-07-30 — partially describes a removed subsystem.** The goal-room framing
> ("a running goal staffs each blueprint slot with a task-bound persona instance";
> the room as instances sharing a `task_id`) was removed by
> `docs/agent-runtime-harness/16-mission-lane-removal.md`. The chat-parity /
> trace-projection design (tool name + summary + changed-file labels, the
> redaction-safe trace) **is still the live mission-chat display model** — that
> half remains current.

**Goal:** When a goal runs, make its agent chats feel like talking to a real Hermes
agent — accessible as one **goal room**, messageable like a normal chat, and
**showing the work** (tool name + summary + changed-file labels — the redaction-safe trace,
not raw diffs), not just decision summaries. Close the display-parity gap between Mission
Control persona chat and the standard gateway (Alice-on-Telegram) experience.

**Controlling principle:** The harness already *does* the work with tools; the gap is
**projection**, not capability. The trace is **additive and filterable** — never
remove the curated operator/agent summaries.

---

## Current state (verified)

- A running goal staffs each blueprint slot with a **task-bound persona instance** = a
  `WorkerSession` carrying `task_id` + `session_id` ([worker_sessions.py](../worker_sessions.py)).
  So **one goal → many agent chats** (dev, qa, backend, …), each its own session.
- A chat/session record already carries a nullable `goal_id`/`task_id`
  (Launcher `mission_control_snapshot.dart:~436`). The goal lives on the chat
  (chats-have-goals, single goal today).
- The harness **already runs a tool-using agent and emits** `run.tool.started` /
  `run.tool.finished` / `run.progress` + a `stream_callback`
  ([profile_runner.py](../profile_runner.py)). The data exists.
- But [persona_chat_history.py](../persona_chat_history.py) **deliberately drops
  tool/system rows** (~line 220, `if role not in {"operator","agent"}: continue`), so the
  chat shows decision summaries, not the live tool/code stream. This curation is
  intentional and stays.
- The Launcher chat panel already tags messages with senderIds
  (`harness-agent/-system/-proof/-blocker`) and **already has a trace filter** (chips:
  Agent / You / Harness / Proof / Intervention — `_buildTraceFilter` / `_passesFilter` in
  `mission_agent_chat_panel.dart`). Send parity works: `_canSendPersonaMessage` returns
  true for `taskBound` instances with a `taskId`.

### Parity ceilings found in audit (2026-06-22)

Three gaps that break the "feels like a real Hermes agent chat" goal and that the
original Stages 0–4 do **not** close. They are folded into the stages below.

- **Depth cap = 8 (both sides).** The visible transcript is hard-capped at **8 rows** on
  *both* repos: the harness tail in `_safe_recent_messages(..., limit=8)` + `rows[-limit:]`
  ([persona_chat_history.py:208,255](../persona_chat_history.py)), and the client in
  `events.take(8)` / `entry.messages.take(8)`
  (`mission_agent_chat_adapter.dart:34,83`). Every stage here feeds the same capped
  `conversation.messages`, so even after the trace channel lands, a goal's chat shows at
  most 8 entries — no scrollback, no pagination. A standard gateway chat shows the full
  thread. **This is the headline parity gap.** → **Stage 4**.
- **Trace can't be default-off on the existing chips.** Stage 3 wants the trace **off by
  default** but reuses `harness-system`/`harness-proof`, and the filter is **all-on**
  (`_visibleSenders = {..._filterKinds.keys}`, `mission_agent_chat_panel.dart:101`).
  Those two chips *already* carry today's non-trace notices (session marker, accepted/
  runtime-status, `_roleForEvent`→system/proof). So mapping trace onto them either shows
  the trace by default (contradicts Stage 3) or, if defaulted off, hides today's system
  notices. The trace needs its **own** sender kind/chip to be independently default-off.
  → folded into **Stage 3**.
- **No live transport for autonomous turns.** The panel streams a live bubble only for the
  operator's *own* send (`_handleStreamingSend` / `_streamingAgentText`). A running goal's
  agents tick autonomously; nothing streams those — they surface only on the next snapshot
  poll (`_runtimeProjectionTimer`) of the capped-8 history. So "watch the trace
  render live" has no transport for agent-initiated work. → **Stage 5**.

### Entity model (LOCKED 2026-06-22 — see Stage 76)

Template → durable **Level Instance** (a placement on the level) → swappable
**Chat** (1:N over the instance's life; one active) → **Goal/Task owned by the
chat**. The Neko operator chat owns the **goal**; a worker chat carries a
**task**. The goal staffs blueprint slots with worker chats. The Harness ticks the
goal owned by that chat and runs the bound worker chats. Navigation is
**chat-first**. Full model + cardinalities + the soft-task/HUD principle:
[01 — Mission Control Architecture](../../docs/agent-runtime-harness/01-architecture.md).

The chat→goal cardinality knob is **closed**: a goal *is* a chat that owns it, so
`0..N` goals = `N` Neko operator chats (peers). This work stays naturally agnostic
to goal count — adding a goal is adding a chat.

---

## Stage 0 — Trace contract & room key

**Goal:** Lock the small data contract both repos depend on before building UI.

### The harness trace entry (already emitted — this is a *projection*, not a new write)

Historical task-run progress landed in the RunStore and append-only EventLog;
that writer lane is retired. Live persona-chat traces use
`ChatProgressSink.emit` to write `run.tool.started` / `run.tool.finished` /
`run.progress` events keyed by session and persona. Each payload is run through
`_safe_progress_payload` and bounded by the EventLog payload cap. Stage 0 pins
the client-facing projection of those historical events; it does not define a
new task-run writer:

```yaml
# projected from an existing run.tool.* / run.progress EventLog Event
kind: harness_trace            # client kind; source event type = run.tool.*/run.progress
task_id: <goal>                # Event.task_id  → room key
persona_id: <agent in slot>    # Event.persona_id → thread key
run_id: <run>                  # Event.run_id
stage_id: <stage_id from payload>
event: tool_started | tool_finished | progress
tool_name: <payload.tool_name / tool>
summary: <payload.summary>     # + patch_summary / code_summary / command_label
files: [<changed_files labels>]   # SAFE FILE LABELS — not raw diffs (see note)
status: <payload.status>       # ok | error / exit_code
ts: <Event.ts>
```

**Redaction reality (scope-fixing):** `_SAFE_PROGRESS_KEYS` carries `summary`,
`patch_summary`, `code_summary`, `command_label`, and `changed_files` **as labels** — it
deliberately does **not** carry raw file contents or diffs. So "show the work" here means
*tool name + summary + changed-file labels*, which is genuine working-trace parity at the
summary altitude. **Raw code/diff blocks are out of scope for this channel** (they would
bypass the established redaction posture); rendering real diffs would need a separate,
deliberately-redacted diff source (proof/sandbox artifacts) and is deferred — see Stage 3.

### Room grouping key

The "goal room" is the set of task-bound instances sharing a `task_id`/`goal_id`.
No new key needed — `WorkerSession.task_id` and the chat record's `goal_id` already
provide it.

### Acceptance

- Trace entry shape is documented and redaction-safe (no secrets/raw env).
- Grouping is expressible from existing keys (`task_id`/`goal_id`) — no schema churn.
- Trace is a separate channel from curated history (never overwrites it).

---

## Stage 1 — Goal Room grouped view (Task A · navigation)

**Goal:** Selecting a goal shows all its agent threads in one room, not separate chats.

### Behavior

- Room header: goal title + status + bound slots.
- Threads: one per task-bound instance (dev/qa/…) as tabs or a left rail; switching
  threads keeps the room/goal context. Reuse `MissionAgentChatPanel` per thread.
- Goal-less chats (`goalId == null`, e.g. Neko Operator Channel) still render standalone.
- **Cardinality-agnostic:** group by `goalId`; works whether a chat names 0..1 goal today
  or many later (the room is keyed on the goal, the threads on its instances).

### Files touched (Launcher)

- `lib/features/mission_control/mission_control_page.dart` — panel composition
  (~3300–3375): when a goal is selected, render a room instead of a single chat.
- `lib/features/mission_control/agent_chat/mission_agent_instance_picker.dart` — group
  instances by `goalId`/`taskId`.
- `lib/features/mission_control/data/mission_agent_instance.dart` — read `taskId`,
  `instanceMode == taskBound`.
- `lib/features/mission_control/data/mission_control_snapshot.dart` — `goalId` on the
  chat record.

### Acceptance

- Selecting a goal shows every task-bound agent thread in one view; switching threads
  preserves goal context.
- Goal-less chats unaffected.
- Send parity preserved per thread.
- `flutter analyze <changed paths>` → clean.

---

## Stage 2 — Harness projects the tool trace (Task B · harness)

**Goal:** Expose the **already-persisted** tool/progress events as a per-thread trace
channel in the snapshot, **additively** — no new write path, no new emit points.

### Approach

- The chat trace events already exist: `ChatProgressSink.emit` (`progress.py`) writes each
  `run.tool.started` / `run.tool.finished` / `run.progress` to the `EventLog`, keyed by
  `session_id` + `run_id` + `persona_id`, already redacted by `_safe_progress_payload`. So
  **do not** add writes at the `profile_runner.py` callbacks — they're the *source* of these
  events and stay as-is.
- Add a projection helper (mirrors `persona_chat_history_summary`): for each task-bound
  persona instance, pull its trace via `EventLog.for_task(task_id, limit=…)` filtered to
  `persona_id` and the `run.tool.*`/`run.progress` types, map each to the Stage 0
  `harness_trace` shape (tool_name + summary + file labels + status + ts), bounded by the
  same `message_tail` knob as Stage 4 so trace depth tracks history depth.
- Expose it in `build_snapshot` as a **new** top-level field `persona_chat_trace` (a list of
  `{persona_instance_id, persona_id, task_id, entries:[…]}`), parallel to
  `persona_chat_history`. **Do not touch** `persona_chat_history.py` curation — curated and
  trace are two independent snapshot fields the client merges by `ts`.
- Redaction/size come for free: entries are already `_safe_progress_payload`-scrubbed and
  ≤4 KB per `EventLog` Event. No raw diffs flow (Stage 0 redaction note).

### Files touched (Harness)

- `agent_runtime/snapshot.py` — add the `persona_chat_trace` projection beside the existing
  `persona_chat_history` call (~line 200), gated by `persona_instance_runtime_enabled`.
- `agent_runtime/persona_chat_history.py` (or a new `persona_chat_trace.py`) — the
  `EventLog.for_task`-based projection helper.
- `agent_runtime/profile_runner.py` / `agent_runtime/progress.py` — **unchanged** (already
  the source of the events).
- `agent_runtime/persona_chat_history.py` curation — **unchanged**.

### Tests

- A run that uses tools yields `persona_chat_trace` entries (tool_started/finished) with
  safe summaries + file labels; `persona_chat_history` is byte-for-byte unchanged.
- The projection filters by `persona_id` so a goal's dev/qa threads don't cross-bleed.
- An entry whose source payload carried a secret-shaped string is already scrubbed (assert
  no raw secret survives projection).
- `python -m pytest tests/agent_runtime -q` stays green.

---

## Stage 3 — Launcher renders the trace (Task B · client)

**Goal:** With the `Trace` chip on, a goal's agent chat shows the working trace (tool name +
summary + changed-file labels); off, it's today's clean summary.

### Behavior

- Parse the new `persona_chat_trace` snapshot field (Stage 2) into the conversation, merged
  with the curated `persona_chat_history` rows by `ts`. The trace is a **second source list**
  on `MissionAgentInstance`/snapshot, not a reclassification of the existing event log — so
  `_roleForEvent` is untouched; trace rows arrive already typed.
- Give the trace its **own** sender kind (`harness-trace`) + one new filter chip (`Trace`),
  rather than reusing `harness-system`/`harness-proof`. Those two chips already carry today's
  non-trace notices and are on-by-default, so they can't be the toggle for default-off trace
  (audit ceiling #2).
- Make the new `Trace` chip the **one** entry in `_filterKinds` that starts **off**:
  initialize `_visibleSenders` to all keys *except* `harness-trace`. Existing chips keep
  their all-on default, so today's view is byte-for-byte unchanged with the chip off.
- Render each trace row as a compact tool-call line: `tool_name · summary · [file labels] ·
  status`. **No raw code/diff blocks** — the channel carries summaries + labels only (Stage 0
  redaction note). Raw-diff rendering is deferred until a redacted diff source exists.
- Default: `Trace` chip **off** (clean summary view); toggling on reveals the working trace —
  preserving today's default exactly.

### Files touched (Launcher)

- `lib/features/mission_control/data/mission_control_snapshot.dart` — parse
  `persona_chat_trace` into a `MissionPersonaChatTraceEntry` model (mirrors
  `MissionPersonaChatHistoryEntry`, ~line 204).
- `lib/features/mission_control/agent_chat/mission_agent_chat_adapter.dart` —
  add an `AgentChatRole.trace`; merge trace entries into the conversation by `ts`;
  `_senderIdForRole` maps `AgentChatRole.trace` → `harness-trace`.
- `lib/features/mission_control/agent_chat/mission_agent_chat_panel.dart` —
  render the compact trace line in `_missionEventsAsChatMessages`; add `harness-trace` to
  `_filterKinds` and seed `_visibleSenders` with every key *except* `harness-trace`.

### Acceptance

- `Trace` chip ON → tool name + summary + file labels visible, in `ts` order with curated
  rows, in a running goal's chat.
- `Trace` chip OFF (default) → unchanged decision-summary view (today's behavior); the
  existing Agent/You/Harness/Proof/Intervention chips and their messages are unaffected.
- Curated operator/agent history unchanged; send parity intact.
- `flutter analyze <changed paths>` → clean.

---

## Stage 4 — Depth parity (deeper tail)

**Goal:** Lift the hard 8-row cap so a goal's chat shows a real conversation depth, not the
last 8 rows. Without this, every other stage projects into an 8-entry window and never
*feels* like a real chat. (Audit ceiling #1.)

### Architecture note (why this is a tail bump, not cursor paging)

The Launcher does **not** request chat history — it reads whatever the periodic
`harness snapshot --json` dump contains (`mission_control_bridge.dart` `_loadSnapshotFromCli`,
polled by `missionControlRefreshIntervalProvider`). Chat depth is therefore set entirely by
what `build_snapshot` emits. The 8-cap lives in one place server-side
(`_safe_recent_messages` `rows[-limit:]`, default `limit=8`); `SessionDB.get_messages()`
already returns the **full** message list, so the tail is purely a projection choice.

So Stage 4 = **raise the bounded per-session tail in the dump** (e.g. 8 → 40) and render all
of it. True on-demand scroll-back *past* that tail can't ride the static snapshot (it would
bloat every 5 s dump: ~`sessions × tail` rows) — that's a separate capability, deferred below.

### Approach

- Harness: add a `message_tail: int` param to `persona_chat_history_summary` and thread it
  to `_safe_recent_messages` (replace the literal `8`); keep a hard ceiling (≤ ~40) for
  redaction/size safety. Pass it from the snapshot build site. The trace channel (Stage 2)
  takes the same `message_tail` so trace depth tracks history depth.
- Client: drop the `events.take(8)` / `entry.messages.take(8)` truncation in
  `mission_agent_chat_adapter.dart` (and the matching `.take(8)` loop bounds); render the
  full provided tail through the existing `MessageFeed`/`_scrollController`.
- Stays cardinality- and channel-agnostic: depth applies per thread, curated and trace
  alike; curation rules (Stage 2) are unchanged — only the *count* of projected rows grows.

### Files touched

- `agent_runtime/persona_chat_history.py` — add `message_tail` param; replace `limit=8` in
  `_safe_recent_messages` with it (bounded).
- `agent_runtime/snapshot.py` — pass `message_tail=<N>` in the `persona_chat_history_summary(
  persona_instances=…)` call (~line 200).
- `mission_agent_chat_adapter.dart` — remove the two `take(8)` caps in
  `missionAgentChatConversationFromInstance` / `_historyMessages`.

### Deferred (needs a new capability, not the snapshot)

On-demand scroll-back beyond the tail: expose a callable harness capability wrapping
`SessionDB.get_messages_around(session_id, around_id, window)` (already exists) and a
Launcher `callHarnessCapability` intent + load-older-on-scroll wiring. Only build this if a
deeper-than-tail thread is actually needed in a run.

### Acceptance

- A goal thread with >8 turns shows up to the new tail (not 8), capped at the ceiling.
- Snapshot size stays bounded (tail ceiling × session list); curated/trace separation and
  redaction unchanged.
- `python -m pytest tests/agent_runtime -q` + `flutter analyze <changed paths>` → clean.

---

## Stage 5 — Live transport for autonomous turns

**Goal:** Trace rows (tool name + summary + file labels) appear **as the agent works**, not
only on the next snapshot poll — including agent-initiated (non-operator) turns.
(Audit ceiling #3.)

### Approach

- Today the panel only streams the operator's *own* send (`_handleStreamingSend` /
  `_streamingAgentText`); autonomous goal ticks surface only when the snapshot is re-polled.
  That poll is driven by `missionControlRefreshIntervalProvider`
  (`mission_control_provider.dart`, currently a flat `Duration(seconds: 5)`). Two paths:
  1. **Adaptive poll (cheap):** make the refresh interval shorter when any visible thread is
     `running` (e.g. ~1–2 s) and fall back to 5 s when idle; newly-arrived `harness_trace`
     rows then render within one tighter interval. No new transport — reuses Stages 2–4.
  2. **Push (richer):** extend the existing stream channel so the harness `stream_callback`
     for a bound worker appends into the room without an operator send.
- Start with (1) — change one provider from a constant to a function of snapshot run-state.
  Treat (2) as a follow-up only if the tighter poll still feels laggy in a real run.

### Files touched

- `lib/features/mission_control/state/mission_control_provider.dart` —
  `missionControlRefreshIntervalProvider`: return the short interval when the snapshot has a
  `running` worker/goal, else the 5 s default.
- (Push variant, deferred) `agent_runtime/profile_runner.py` + snapshot stream plumbing.

### Acceptance

- With `Trace` on, a running goal's agent shows new trace rows appear within one
  refresh interval **without** the operator sending anything.
- No extra load when no goal is running (cadence only tightens for `running` threads).

---

## Stage 6 — Parity verification & hardening

**Goal:** Confirm a goal's chat reaches gateway-style parity and is safe.

### Checklist

- Accessible: every task-bound agent of a running goal is reachable in its room.
- Messageable: you can send to a working agent like a normal chat (persona enabled via
  `_supportsOperatorPersonaChat`; flag any bound persona not in the supported set —
  otherwise its thread is locked).
- Deep: the thread shows the deeper tail, not 8 rows (Stage 4); on-demand scroll-back past
  the tail is acknowledged as deferred, not a silent truncation.
- Watchable: trace rows (tool name + summary + file labels) render live (Stage 5),
  filterable under the `Trace` chip, redaction-safe, size-bounded.
- No regression: curated history, goal-less chats, the default-off `Trace` chip, and
  existing filter behavior intact.

### Acceptance

- A real goal run, viewed in its room with trace on, shows the agent doing work with
  tools — parity with the standard Hermes agent chat.
- Locked threads surface a clear reason, not a silent dead end.

---

## Hard invariants

- The operator/agent curation stays — the trace is **additive and filterable**, never a
  replacement.
- Don't break send parity or goal-less (`goalId == null`) chats.
- The trace gets its **own** sender kind (`harness-trace`) and is the only chip that starts
  **off** — never repurpose `harness-system`/`harness-proof` (they carry today's notices).
- The trace is a **projection of already-persisted, already-redacted** `run.tool.*`/
  `run.progress` events — no new write path, and it carries **summaries + file labels, never
  raw diffs/secrets** (`_safe_progress_payload` posture). Raw-diff rendering is deferred.
- Depth is a **bounded tail bump, not unbounded** — lifting the 8-cap keeps a hard ceiling
  for snapshot-size/redaction safety; deeper-than-tail scroll-back is a deferred capability.
- Stay **agnostic to the chat→goal cardinality** (0..1 today vs 0..N cockpit) — the room
  keys on the goal, threads on instances, so either model works.
- Launcher is on a shared WIP branch: stage only changed files by path; never sweep the
  unrelated WIP. The app can't be run by the implementer — "done" = `flutter analyze` +
  `pytest` pass, plus a list of what the operator should click to confirm.

## Recommended build order

1. Stage 0 — trace contract + room key (cheap, unblocks both sides)
2. Stage 1 — Goal Room view (navigation; lower risk, ship first for the cockpit feel)
3. Stage 4 — Depth parity (lift the 8-cap; do this early — every other stage projects into
   the same window, so the cap limits what they can show)
4. Stage 2 — Harness projects trace (new `persona_chat_trace` snapshot field from existing
   EventLog entries — no new write path)
5. Stage 3 — Launcher renders trace (parse `persona_chat_trace`; new default-off `Trace` chip)
6. Stage 5 — Live transport (tighten refresh while running; push variant deferred)
7. Stage 6 — parity verification + hardening

## Resolved decision (2026-06-22)

Whether a chat names **one** goal or a room holds **many** is no longer open. A goal
*is* a chat that owns it, so **N concurrent goals = N Neko operator chats** (peers) —
the cockpit's many-goals view is just many conductor chats, no singleton orchestrator.
The Goal Room still groups by `goalId`, so this remains an additive change, not a
rework. See [01 — Mission Control Architecture](../../docs/agent-runtime-harness/01-architecture.md).
