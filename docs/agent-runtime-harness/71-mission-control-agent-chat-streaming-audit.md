# Mission Control Agent Chat Streaming Deep Audit

Date: 2026-06-19
Owner: Alice
Scope: Hermes Agent gateway streaming, the Hermes `SessionDB` + recall/enrichment stack, Hermes Harness persona-instance chat/runtime, and the Eternia Launcher Mission Control Agent Console.
Status: implementation-ready. Every evidence path below was re-verified against both repos on 2026-06-19; line anchors are included so each stage can be opened and edited directly. Spine decision: **link the persona chat path into the shared Hermes chat/recall stack, don't synthesize a parallel one** (see Architecture decision).

Repos:

- Hermes Agent / Harness — `X:/Eternia/hermes-agent`
- Eternia Launcher (Flutter) — `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`

## Implementation status (2026-06-19)

Built and verified across both repos (branches `feature/mission-agent-chat-sessiondb` / `feature/mission-agent-chat-s1`):

- **DONE + tested:** S1 (projection-warning timer + `didUpdateWidget` reconcile); S2A (SessionDB wiring, session binding, operator+agent persistence, continuity, auto-title); S2C (bridge forwards `persona_chat_history` with redaction-safe message tail); S3 (adapter renders history); S4 partial (`--client-message-id` end-to-end + idempotency dedup + widened `MissionControlActionResult`); HERMES_HOME propagation in the bridge spawn env; S2B per-persona recall enable (free-floating risk flag) + **redaction-on-write boundary** with secret-stripping before SessionDB writes.
- **Tests:** Hermes 231 pass (incl. new `tests/hermes_cli/test_persona_chat_session.py`, `tests/agent_runtime/test_persona_memory_scope.py`); Launcher 134 pass; bug fixed during verification: decision-reply dedup compared normalized-vs-raw content and never matched.
- **Remaining (intentional):** S5 conversational `persona_message.reply` type (replies are still decision-shaped — P2); the dynamic *cross-persona* shared mission scope (correctly a no-op for free-floating chat, which has no active goal); live MCP smoke (manual). Pre-existing unrelated red test: `test_ticker.py::test_tick_uses_configured_persona_when_agent_store_empty` (profile resolution, fails on committed HEAD independent of this work).

## Executive summary

Tony reported Agent Console messages reaching `Runtime call completed. Waiting for the refreshed Harness snapshot…` and not rendering an agent reply. A deep audit against Hermes' mature streaming architecture confirms the gap is structural, not a Flutter copy bug:

- Hermes gateway streaming has a typed event vocabulary, a single async stream consumer, explicit finalization semantics, and fallback delivery flags.
- Harness persona-instance chat has durable assignment/run/instance stores, but `persona.instance.create/message --auto-run` does not persist a first-class persona chat transcript or a final assistant message into the read model the Launcher Agent Console consumes. The agent's "reply" only survives as a **run decision** (`decision_summary`/`decision_rationale`), never as a chat message.
- Launcher Mission Control Agent Chat renders `MissionAgentInstance.sourceLog?.events`; Harness-backed persona instances intentionally set `sourceLog: null`, so a refreshed snapshot can contain completed runs/events while the Agent Console has no authoritative chat messages to render and no terminal UI state.

AAA conclusion (revised — enterprise-grade): the root cause is that the persona-instance chat path is **disconnected from the mature Hermes chat stack**. Hermes chat sessions already persist to a shared `SessionDB` (FTS-searchable) and enrich every new session's starting context via the agent-layer recall system (memory-provider prefetch + the `session_search` tool). The free-floating persona path runs with `session_db=None` and `skip_memory=True`, so persona chats persist nothing, contribute nothing to the recall corpus, and receive no cross-session enrichment. The right fix is therefore **link, don't synthesize**: route Mission Control persona chat through the same SessionDB + memory stack so it becomes a first-class Hermes chat surface, then expose it through the existing `persona_chat_history` read model. A bespoke `agent_chat_streams` projection is demoted to an optional correlation/health layer. Short term, the Launcher must still replace the indefinite waiting banner with a projection-warning state. See **Baseline D** and the **Architecture decision** below.

### What the original audit got right vs. what changed after verification

Confirmed exactly as written:

- The 7-event gateway vocabulary, the single-consumer boundary, and the `final_response_sent`/`final_content_delivered` flags.
- The free-floating CLI flow, the `next_expected: … refresh Harness snapshot for transcript/proof readback` promise, and that `persona_chat_history_summary` never reads raw transcripts.
- `MissionAgentInstance.fromHarness(...)` sets `sourceLog: null`; `missionAgentChatConversationFromInstance` renders only `sourceLog?.events`; `deriveMissionAgentInstances` prefers Harness instances and bypasses `agent_logs`.

Corrected / sharpened after reading the code (drives the stages below):

1. **There is no assistant transcript today — so create one (Path B), don't synthesize around it.** `PersonaDiagnosticResult` ([persona_diagnostics.py:47](../../agent_runtime/persona_diagnostics.py)) carries no final-message field, and the free-floating run persists nothing to `SessionDB`; the only currently readable "reply" is `run.final_decision.{summary,rationale}` via `_run_summary` ([snapshot.py:1285](../../agent_runtime/snapshot.py)). The chosen fix is to make the persona turn write a real `SessionDB` transcript (Stage 2A) rather than synthesize a bubble from the decision. The decision-projection fallback survives only in the optional overlay (Stage 2D) for runs that produced no transcript.
2. **Reuse the existing redaction-safe projection contract, don't invent one.** The snapshot already standardizes on `display_kind` / `display_title` / `display_summary` / `redaction_status` (`_event_display_projection` [snapshot.py:1171](../../agent_runtime/snapshot.py)) and `safe_assignment_text` / `_safe_display_text` helpers. The target schema below adopts those field names instead of the original draft's `safe_text` / `source`.
3. **Correlation is more broken than "idempotency_key not carried through".** The CLI already emits `assignment_id`, `persona_instance_id`, `run_ids`, `turn_id`, `task_id` ([harness.py:806-844](../../hermes_cli/harness.py)). The Launcher bridge's accepted path **discards CLI stdout entirely** and returns a generic `safeMessage` ([mission_control_bridge.dart:114-118](X:/Unreal%20Engine/Engine/Launcher/EterniaLauncher/lib/features/mission_control/data/mission_control_bridge.dart)). `MissionControlActionResult` only has `status` + `safeMessage` ([mission_control_actions.dart:520](X:/Unreal%20Engine/Engine/Launcher/EterniaLauncher/lib/features/mission_control/data/mission_control_actions.dart)), and `onIntent` is `Future<void>` ([mission_control_page.dart:290](X:/Unreal%20Engine/Engine/Launcher/EterniaLauncher/lib/features/mission_control/mission_control_page.dart)).
4. **`persona_instance_id` is the real correlation key, already in hand.** `MissionAgentInstance.fromHarness` sets `instanceId = instance.personaInstanceId` ([mission_agent_instance.dart:81](X:/Unreal%20Engine/Engine/Launcher/EterniaLauncher/lib/features/mission_control/data/mission_agent_instance.dart)). The selected panel already knows the stream key, so Stage 1/3 selection needs no new round-trip; `client_message_id` is only needed to disambiguate *which turn* within a stream.

## Baseline A: Hermes gateway streaming invariants (verified)

Evidence:

- `gateway/stream_events.py` — 7 frozen event dataclasses at lines 44 (`MessageChunk`), 56 (`MessageStop`), 72 (`Commentary`), 85 (`ToolCallChunk`), 104 (`ToolCallFinished`), 122 (`LongToolHint`), 135 (`GatewayNotice`).
- `gateway/stream_consumer.py` — `final_response_sent` / `final_content_delivered` state at lines 161/165, properties at 199/209, set during finalization at 517/519.
- `gateway/platforms/api_server.py` — `toolCallId` orphan protection: `_started_tool_call_ids` set at 1860; start at 1877 (`_on_tool_start`); completion drop-if-no-matching-start at 1895 (`_on_tool_complete`) and again on the SSE path at 2453-2462.
- Tests: `tests/gateway/test_stream_events.py`, `tests/gateway/test_stream_consumer.py`, `tests/gateway/test_stream_consumer_thread_routing.py`.

Architecture: typed vocabulary → single sync→async consumer boundary (thread-safe queue, buffered/rate-limited edits, segment breaks finalize the current message before tool chrome) → delivery-truth flags → correlation/orphan protection → adapter separation (agent emits facts, dispatcher decides rendering).

AAA invariant to mirror: every visible runtime transition must be backed by a typed event and a finalization state, not by local UI copy.

## Baseline B: Harness persona-instance flow (verified)

Evidence:

- `hermes_cli/harness.py` — `_queue_free_floating_assignment(...)` at 761; auto-run branch at 824; emitted payload at 806-822; `_run_free_floating_assignment_once(...)` at 848; run payload at 912-925; the over-promising `next_expected` at 922.
- `agent_runtime/persona_diagnostics.py` — `PersonaDiagnosticResult` at 47 (no final-message field), `PersonaDiagnosticController.diagnose` at 109.
- `agent_runtime/persona_assignments.py` — `PersonaAssignmentSpec` at 23 (no `metadata` / `client_message_id` field), `PersonaAssignmentStore._event` at 375 region, instance `_event` emit at 226.
- `agent_runtime/persona_chat_history.py` — `persona_chat_history_summary(...)` at 14 (docstring at 23-25: "never reads raw transcripts"); `_history_row(...)` at 98 (already emits `persona_instance_id`, `last_message_preview`, `message_count`, `state`, `redaction_status`).
- `agent_runtime/snapshot.py` — snapshot keys assembled at 150-166 (`runs`, `persona_instances`, `persona_chat_history`, `persona_assignments.active/recent`), `_run_summary` at 1285 (`decision_summary`/`decision_rationale` from `run.final_decision`), `_role_streams` at 1025, `_event_display_projection` at 1171, redaction convention values `safe`/`redacted`.
- `agent_runtime/decision_contract_registry.py` — persona lifecycle `EventContract`s at 1154-1157.

Current `persona.instance.create --auto-run`:

1. CLI queues a `free_floating_message` assignment (`evidence_kind=free_floating`, `production_proof_eligible=False`).
2. Auto-run invokes `_run_free_floating_assignment_once(...)`.
3. Runtime creates a sandbox diagnostic task/run via `PersonaDiagnosticController` + `TickEngine` + `GPTPersonaRuntime`.
4. Assignment store attaches `run_ids` and closes the assignment; instance set to `IDLE`, `mode=free_floating`.
5. CLI prints JSON: `ok`, `assignment_id`, `persona_instance_id`, `persona_id`, `task_id`, `state`, `execution_state`, `turn_id`, `run_ids`, `stop_reason`, `latest_decision_type`, `latest_validation_status`, `latest_total_tokens`, `next_expected`.
6. Snapshot exposes `persona_instances`, `persona_assignments.active/recent`, `observability.recent_events`, `runs`, `tasks[].role_streams`, `persona_chat_history`.

Critical gap (read-model): `persona_chat_history_summary(...)` only projects metadata for SessionDB sessions already bound to a persona instance; free-floating auto-runs never create or bind such a session, and the assistant text only exists as a run **decision**. So the CLI's `next_expected: refresh … for transcript readback` promises more than any read model delivers.

Critical gap (events): the registry has `persona_instance.created/.chat_opened` and `persona_assignment.created/.closed` but **no** chat-message lifecycle events (`persona_message.accepted/.finalized/.projection_failed`, `persona_run.started`, etc.).

## Baseline C: Launcher Mission Control Agent Console flow (verified)

Evidence:

- `lib/features/mission_control/agent_chat/mission_agent_chat_adapter.dart` — `missionAgentChatConversationFromInstance(...)` at 16; renders `instance.sourceLog?.events ?? const []` at 20, capped at `take(8)` at 29; event→bubble role mapping `_roleForEvent` at 431; redaction handling at 129-140.
- `lib/features/mission_control/agent_chat/mission_agent_chat_panel.dart` — `_handleSend` at 202; `_watchRuntimeCall` at 309 (awaits the future, sets the completed banner at 317, **no timeout / no projection check**); the banner string `Runtime call completed. Waiting for the refreshed Harness snapshot…` at 679.
- `lib/features/mission_control/data/mission_agent_instance.dart` — `MissionAgentInstance.fromHarness` at 73 sets `sourceLog: null` at 98 (and `idleFromPersona` at 129); `instanceId = instance.personaInstanceId` at 81; `deriveMissionAgentInstances` at 197 prefers `harnessPersonaInstances` and bypasses `agent_logs` at 206-222.
- `lib/features/mission_control/data/mission_control_snapshot.dart` — parses `persona_instances` at 193, `persona_assignments` at 201, `persona_chat_history` at 204; builds instances at 222-228.
- `lib/features/mission_control/data/mission_control_bridge.dart` — accepted path discards stdout at 114-118; failure detail parsing at 130-140.
- `lib/features/mission_control/data/mission_control_actions.dart` — `MissionControlActionResult` (only `status`, `safeMessage`) at 520.
- `lib/features/mission_control/mission_control_page.dart` — `_submitIntent` at 290 (`Future<void>`, invalidates `missionControlSnapshotProvider` on accepted at 339).
- Tests: `test/features/mission_control/mission_agent_chat_panel_test.dart` (asserts `persona.instance.create` routing at 106), `mission_agent_instance_test.dart` (accepts empty `personaChatHistory` for a free-floating assignment at 85).

Critical Launcher gap: a selected Harness persona instance can be real, updated, and backed by completed runs/events, yet the Agent Console has `sourceLog == null`, so nothing authoritative renders, and `_watchRuntimeCall` leaves the completed banner up forever.

Test gap: existing tests assert routing/accepted copy only. No test proves "completed Harness run ⇒ visible final/progress bubble or explicit projection warning".

## Baseline D: Hermes session enrichment / recall stack (verified)

The "past-session search enriches new sessions" behaviour is an **agent-layer** capability keyed on the agent's `session_db` + profile/memory scope — not a gateway-only feature. Any chat surface that runs the agent with a `session_db` inherits it.

Evidence:

- `hermes_state.py` — single shared DB at `DEFAULT_DB_PATH = get_hermes_home() / "state.db"` (line 34); FTS5 search tables `messages_fts` / `messages_fts_trigram` (528, 557); search APIs `search_messages` (3133), `search_sessions` (3491); `create_session` (1206), `append_message` (2266), `list_sessions_rich` (1875).
- Who writes transcripts today: gateway platforms via `get_or_create_session` + `append_message` (`gateway/mirror.py:159`, slack/telegram/yuanbao/api_server), and the CLI chat path (`hermes_cli/cli_commands_mixin.py:836-854`).
- Recall/enrichment (agent layer): `agent/memory_provider.py` (`prefetch(query)` background recall before each turn), `agent/memory_manager.py` (injects "recalled memory context"), the `session_search` tool (`agent/prompt_builder.py:157,173`), all hung off `agent._get_session_db_for_recall()` (`agent/agent_runtime_helpers.py:1741`, `agent/tool_executor.py:985`).
- Rails into the persona path already exist: `GPTPersonaRuntime.__init__(session_db=…)` forwards to `ProfileAgentRunner(session_db=…)` (persona_runtime.py:46-52); the runner threads `session_db` into the agent factory (profile_runner.py:185); the persona toolset **includes `session_search`** (persona_runtime.py:211; profile_runner.py:30 `READ_SEARCH_TOOLS`).

Where the persona path is disconnected (the actionable gaps):

1. **No `session_db`.** Free-floating runs construct `GPTPersonaRuntime(default_provider=…, default_model=…)` with no `session_db` (harness.py:868) → `session_db=None` → no transcript persisted, no corpus contribution, recall has nothing to read from.
2. **Passive recall disabled.** `AgentRunRequest.skip_memory` defaults to **`True`** (profile_runner.py:48), passed to the agent (181). So even with a `session_db`, memory-provider prefetch is skipped; only explicit `session_search` tool calls would work. Enabling enrichment requires `skip_memory=False` for chat turns.
3. **No session binding on auto-run.** `persona.instance.open_chat` already binds `instance.session_id` + `mode="chat"` (persona_assignments.py:108-131), but `create/message --auto-run` never creates or reuses that session, so there is no stable session identity to persist into or recall from.
4. **DB-home parity unconfirmed.** SessionDB resolves to `get_hermes_home()/state.db`; the harness sets `HERMES_AGENT_RUNTIME_ROOT` and tracks `HERMES_HOME` (harness.py:862,1768). Cross-surface enrichment (gateway chat ↔ Mission Control persona chat) only works if both resolve to the **same** `HERMES_HOME`. Must-verify before relying on shared recall.

AAA invariant to mirror: a persona chat turn must read from and write to the same enrichment corpus as every other Hermes chat surface, under an explicit memory scope and a redaction-safe write boundary.

## Other unused Hermes chat capabilities (gap inventory)

Beyond persistence + recall, the persona/Mission Control path leaves these mature Hermes chat capabilities unused. Each row: capability (evidence) → status in persona path → recommendation/priority. Most "light up" once S2A binds a real session; a few are independent.

| Capability | Evidence | Status in persona/MC path | Recommendation |
| --- | --- | --- | --- |
| **Streaming delivery** (typed events, buffered edits, fallback final) | `gateway/stream_consumer.py`, `stream_events.py` (Baseline A) | Unused — MC polls snapshots; no live tokens, no `Commentary` (thinking/progress), no `ToolCallChunk/Finished` chrome, no `LongToolHint` | P2: Stage 6 (SSE tail). Until then S1 fallback stands in for "fallback final". |
| **Auto-title** | `agent/title_generator.py` `generate_title`/`auto_title_session`/`maybe_auto_title` | Unused — persona chat sessions get no human title; `persona_chat_history.title` falls back to generic | P1 (cheap): call `maybe_auto_title` after the first bound turn so the picker/history shows real titles. Lights up with S2A. |
| **Multi-turn continuity** | `PersonaDiagnosticController._create_task` per call; `max_actions=1`, new task/run each turn | **Broken** — every operator message is a fresh, context-free task; "hi" then "and?" do not share context | P0 for real chat: once S2A binds a session, thread prior session turns into the run (load history) so follow-ups have context. Without this, "chat" is one-shot Q&A. |
| **Trajectory compression / context window** | `trajectory_compressor.py`; instances already carry `compression_receipt_id` | Unused for persona chat | P2: enable once continuity (above) makes sessions long enough to need it. |
| **Explicit memory tool** (durable save/load) | `tools/memory_tool.py` `MemoryStore` | Persona toolset grants `session_search` but **not** the memory tool (persona_runtime.py:211) | P2 + scope decision: decide whether personas may write durable memory (ties to Stage 2B scope/redaction). |
| **Attachments / images / voice** | gateway media paths; agent run request | Unused — operator cannot send an image; persona returns only MCP-screenshot proof, not inline media | P3: out of scope for v1; note for roadmap. |
| **Idempotency dedup** | `gateway/platforms/api_server.py` `_IdempotencyCache` (619), `Idempotency-Key` header | **Not honored** — MC passes `idempotencyKey`, but the harness CLI path has no dedup, so a retried send creates a duplicate assignment/run | P1: carry `client_message_id` (Stage 4) into the assignment and dedup on it harness-side. |
| **Run lifecycle API** (`/v1/runs/{id}`, `/events` SSE, `/stop`, `/approval`) | `api_server.py:17-21` | Unused — MC reimplements via `run.cancel`/`run.approve` CLI + snapshot polling | P2: reuse the run-events SSE for Stage 6 instead of a bespoke transport. |

The two that change product behaviour most: **multi-turn continuity** (without it, persona "chat" is stateless Q&A even after S2A) and **auto-title** (cheap, makes history usable). Both are folded into the stages below.

## Comparative invariant matrix

| Invariant | Hermes gateway | Harness persona flow | Launcher Agent Console | Gap → stage |
| --- | --- | --- | --- | --- |
| Typed lifecycle events | Strong (7 events) | Partial (instance/assignment only) | Ad hoc local strings | Add chat events + read model → S2/S5 |
| Correlation IDs | Strong (toolCallId/SSE) | assignment/run/turn/task IDs emitted by CLI | Discarded at bridge; `onIntent` returns void | Parse + widen result, correlate by `persona_instance_id` → S4 |
| Authoritative transcript | Agent history owns transcript | Run produces a **decision**, not a chat message | Expects legacy `sourceLog` events | Persist a real `SessionDB` transcript → S2A/S2C/S3 |
| Finalization semantics | `final_response_sent`/`final_content_delivered` | CLI `execution_state=completed` | Banner waits forever | Snapshot-confirmed/final/projection-failed states → S1/S2 |
| Orphan protection | Drops completion w/o start | Events scattered across stores | UI may match nothing | Correlate by run/assignment/instance → S2 |
| Fallback delivery | Consumer sends fallback final | No chat fallback | No projection warning | Projection warning + run link → S1 |
| Redaction | Tested | Safe summaries only (`display_*`/`redaction_status`) | Reads `redactionStatus` | Reuse same fields; extend sanitizer tests → S2 |
| Thread/session routing | Tested | `open_chat` binds session; send uses assignment | Picker opens history; send uses assignment | Unify session + assignment send semantics → S2A/S5 |
| Cross-session enrichment | Recall via memory provider + `session_search` + FTS corpus | **Disconnected** (`session_db=None`, `skip_memory=True`) | None | Wire SessionDB + enable memory under a scope → S2A/S2B |
| Transcript persistence | All surfaces write to shared `state.db` | Free-floating writes nothing | Expects events that never arrive | Persist + bind session → S2A |

## Architecture decision: link, don't synthesize

Two ways to give the Agent Console authoritative messages:

- **Path A — synthesize** an `agent_chat_streams` read model from assignment/run/decision data (no runtime change). Content is decision-shaped; it is a parallel, bespoke chat system; it never enriches or contributes to the Hermes recall corpus.
- **Path B — link/unify (chosen).** Route the free-floating persona turn through the shared `SessionDB` + memory stack so it becomes a first-class Hermes chat surface: transcripts persist, the turn is enriched by past-session recall, and it contributes to the corpus future sessions search. Expose it through the **existing** `persona_chat_history` read model rather than a new one.

Decision: **Path B is the spine** (Stages 2A/2B/2C below). It is less net-new code, architecturally correct, and is exactly the alternative already named in P0 #2. `agent_chat_streams` survives only as an **optional** correlation/projection-health overlay (Stage 2D) for the cases `persona_chat_history` doesn't cover (in-flight correlation, projection warnings). Path B introduces two genuinely new enterprise concerns that Path A did not: a **memory-scope policy** and a **redaction-safe write boundary** (Stage 2B).

Memory-scope policy — **DECIDED: per-persona by default + a shared mission/operator scope that engages *dynamically*, never by manual flag.** Personas are already profiles (Neko/QA/Dev/Backend) and the memory provider is profile-scoped, so:

- **Default = per-persona scope.** Each persona recalls only its own history; Neko cannot see QA's or Dev's. This is the isolation baseline and the steady state.
- **Dynamic shared scope.** The common mission/operator scope is consulted **only when both hold**: (a) the persona instance is attached to a goal/task that is **currently in progress**, and (b) the turn is **relevant to that goal/task**. The harness derives the mission scope key from the instance's active task/goal at run time — there is no operator toggle and no CLI flag.
- **The relevance guard is the point:** if there is no active goal/task, the reply has nothing to do with any mission, so the shared scope is never touched. If a goal *is* active but the message is off-topic, mission-scoped recall returns nothing relevant and contributes nothing — so it self-gates by relevance rather than by a switch.
- **Rollout:** ship *transcript-only* first (S2A, `skip_memory=True`), then per-persona recall, then the dynamic shared scope. Each step is independently revertable.

Implication for redaction: the **shared scope is where cross-persona leakage can happen**, so the redaction-on-write boundary (Stage 2B) is mandatory for anything written to the shared scope; per-persona writes still sanitize but can't leak across roles.

## Optional overlay read model: `agent_chat_streams`

Only needed once Path B is in place and a correlation/health overlay is wanted (Stage 2D). If `persona_chat_history` plus projection-health on the instance is sufficient, this can be skipped. Field names reuse existing snapshot conventions (`display_*`, `redaction_status`, `safe_*`), so the sanitizer and Dart parsers already have precedent.

```jsonc
"agent_chat_streams": [
  {
    "persona_instance_id": "personainst_neko_supervisor",  // == MissionAgentInstance.instanceId
    "persona_id": "neko_supervisor",
    "session_id": null,                 // present only when bound to SessionDB
    "active_assignment_id": null,
    "latest_assignment_id": "assign_...",
    "latest_run_id": "run_...",
    "latest_task_id": "task_...",
    "client_message_id": null,          // echoed from S4 when present
    "state": "completed",               // running|completed|blocked|projection_failed
    "messages": [
      {
        "message_id": "msg_...",
        "role": "operator",             // operator|agent|system|tool|proof
        "display_summary": "...",       // redaction-safe text (safe_assignment_text)
        "redaction_status": "safe",     // safe|redacted
        "created_at": "...",
        "origin": "assignment"          // assignment|run_decision|run_event|session_db
      }
    ],
    "events": [
      {
        "event_id": "...",
        "type": "persona_run.completed", // typed (Stage 5 vocabulary)
        "display_kind": "decision",
        "display_title": "...",
        "display_summary": "...",
        "redaction_status": "safe",
        "run_id": "...",
        "assignment_id": "..."
      }
    ],
    "projection_health": {
      "status": "ok",                   // ok|missing_final_message|missing_run|redacted
      "display_summary": "..."
    }
  }
]
```

Source-of-truth rules (mirrors `persona_chat_history.py`):

- `operator` message ⇐ assignment `message` (the queued prompt), via `safe_assignment_text`.
- `agent` message ⇐ the run's `final_decision.summary`/`.rationale` (the only readable reply). If absent, **do not fabricate** one — set `projection_health.status = missing_final_message`.
- `events` ⇐ role-stream / recent events already passed through `_event_display_projection`.
- Never emit raw transcripts; every text field flows through `safe_assignment_text` / `_safe_display_text`.
- `state = projection_failed` when a terminal run exists but no agent message could be projected.

Launcher consumes this model (not `sourceLog`) for Harness-backed persona instances.

## Implementation stages

Stages are ordered so each is independently shippable and testable:

- **S1** — Launcher-only: replace the indefinite wait with a projection warning (stops the bleeding now, no Harness change).
- **S2A** — wire `SessionDB` into the free-floating turn + bind the session (the *transcript-only* increment; makes `persona_chat_history` real).
- **S2B** — enable memory/recall under the chosen scope + redaction-safe write boundary (gated by the memory-scope product decision; can be deferred).
- **S2C** — forward `persona_chat_history` through the bridge so the transcript reaches the UI.
- **S2D** — *optional* `agent_chat_streams` correlation/health overlay.
- **S3** — Launcher renders the transcript (and overlay) instead of `sourceLog`.
- **S4** — carry correlation IDs from CLI → bridge → UI.
- **S5** — typed event vocabulary incl. the conversational `persona_message.reply` type (Open gap #3).
- **S6** — optional real-time transport.

Minimum shippable "no more infinite wait + real persisted, visible chat": **S1 + S2A + S2C + S3.** Enrichment (S2B) and overlay (S2D) layer on after. **Write the failing tests in P1 (below) before S1.**

### Stage 1 — Truthful projection-warning fallback (Launcher only)

Objective: stop indefinite waiting immediately, with no Harness change.

Files:

- `mission_agent_chat_panel.dart` (`_watchRuntimeCall` at 309, banner at 679)
- `mission_agent_chat_adapter.dart` (status/meta helpers)
- tests under `test/features/mission_control/`

Data-flow note (verified): the panel is a `StatefulWidget` that holds local state (`_localMessages`, `_runtimeStatusMessage`). Snapshot refresh is driven by `_submitIntent` calling `ref.invalidate(missionControlSnapshotProvider)` (page:339); `MissionControlPage` rebuilds and passes a **fresh** `widget.instance` to the panel. So reconciliation is `didUpdateWidget`-driven, **not** a poll: the resolved future cannot itself see the new snapshot.

Actions:

- In `_watchRuntimeCall`, when the future resolves, set the completed banner **and** arm a bounded timeout timer (e.g. ~10–20s) keyed to the in-flight run.
- Add/extend `didUpdateWidget`: when the refreshed `widget.instance` (Stage 3: its stream) carries an authoritative message/event correlated to the in-flight run/instance, clear the banner and let the rendered bubble stand.
- If the timeout fires first with no correlated message, replace the banner with an explicit, non-final projection warning: `Run completed but no Agent Chat message was projected (run <runId>). Open the Run Inspector for details.`
- Never present the completed banner as a final answer.

Note: in Stage 1 (before S2A/S2C/S3) there is no persisted transcript yet, so the timeout path is the normal outcome — that is acceptable and is the whole point (truthful warning beats infinite wait). `didUpdateWidget` correlation becomes live once S3 lands.

Proof:

- Widget test: completed command + no correlated message within the window ⇒ projection warning, not the indefinite waiting banner.
- Widget test (post-S3): refreshed instance carrying a correlated agent message ⇒ banner cleared, bubble shown.
- `flutter analyze` clean + targeted `test/features/mission_control/` green.

### Stage 2A — Wire SessionDB into the free-floating persona turn + bind the session (Harness)

Objective: persona chat turns persist to the shared `SessionDB` under a stable, instance-bound session identity. This alone makes `persona_chat_history` light up and is the *transcript-only* increment.

Files:

- `hermes_cli/harness.py` — `_run_free_floating_assignment_once` at 848 (construct the runtime with a `session_db` and a bound `session_id`); `_queue_free_floating_assignment` at 761 (ensure/create the bound session before the run).
- `agent_runtime/persona_runtime.py` — confirm `GPTPersonaRuntime(session_db=…)` is forwarded (it is, 46-52); set the run's `session_id`.
- `agent_runtime/persona_assignments.py` — reuse the `open_chat` binding (`instance.session_id`, `mode="chat"`, 108-131) so auto-run uses the same session the picker opens.
- `hermes_state.py` — `SessionDB` (shared `get_hermes_home()/state.db`), `create_session` (1206) / `append_message` (2266).

Actions:

- Resolve/create a stable `session_id` for the persona instance (reuse `instance.session_id` if bound, else create one and bind it via the same path `open_chat` uses).
- Construct the free-floating `GPTPersonaRuntime` with `session_db=SessionDB()` (replacing the no-arg construction at harness.py:868) and pass the bound `session_id` into the run request so the agent appends the operator message + reply to that session.
- **Multi-turn continuity:** pass the same bound `session_id` on every turn to the same instance so the agent loads prior session history (currently each turn is a fresh `_create_task` with no context — persona_diagnostics.py). Without this, S2A gives durable-but-stateless Q&A.
- **Auto-title:** after the first bound turn, call `agent/title_generator.maybe_auto_title` so `persona_chat_history.title` and the picker show a real title instead of a generic fallback.
- **Fix `HERMES_HOME` propagation (Launcher-side, required — see Open gap #6, checked CONDITIONAL FAIL).** The bridge spawns `hermes` with no `environment:` (mission_control_process_io.dart:22), so an un-exported `HERMES_HOME` makes the harness use `LOCALAPPDATA\hermes\state.db` — disjoint from the gateway's `…profiles\alice\state.db`. Pass `environment` to `Process.run` with `HERMES_HOME` (+ `HERMES_AGENT_RUNTIME_ROOT`) resolved to the gateway's profile, merged over inherited env. Without this, S2A writes to the wrong corpus and enrichment never crosses surfaces.
- Set `instance.session_id` from the run result so the binding persists for readback.

Proof:

- Harness test: `persona instance create --auto-run` produces a SessionDB session containing the operator message and the agent turn, bound to `instance.session_id`.
- Snapshot test: `persona_chat_history` now returns a row for that instance (previously empty).
- Continuity test: two sequential messages to the same instance share one `session_id`, and the second turn's loaded context includes the first turn.
- Title test: after the first turn, `persona_chat_history.title` is a generated title, not the generic fallback.

### Stage 2B — Memory enablement, scope policy, and redaction-safe write boundary (Harness)

Objective: turn on cross-session enrichment safely under the **decided** policy — *per-persona by default + a shared mission/operator scope that engages dynamically (no flag)*. Ship transcript-only first (leave `skip_memory=True`), then per-persona recall, then the dynamic shared scope.

Files:

- `agent_runtime/profile_runner.py` — `AgentRunRequest.skip_memory` default `True` at 48 (set `False` for chat turns); session_db threading at 185.
- `agent_runtime/persona_runtime.py` — memory provider / profile scope selection (211 toolset already grants `session_search`).
- `agent_runtime/personas.py` — per-persona profile/scope mapping.
- `agent_runtime/persona_diagnostics.py` / `agent_runtime/persona_assignments.py` — resolve the active goal/task for the instance at run time (`instance.current_task_id`, `assignment.task_id`, and the task/goal in-progress state already in the snapshot) to derive the mission scope key. **No CLI flag.**
- redaction: the existing `safe_assignment_text` / `_safe_display_text` helpers (persona_chat_history.py) — extend to the **write** path.

Actions:

- **Step 1 (per-persona recall):** set `skip_memory=False` for free-floating chat turns and bind the memory provider to the **persona's own profile scope**, so each persona recalls only its own history. The `session_search` tool is already available.
- **Step 2 (dynamic shared scope):** at run time, check whether the instance is attached to an **in-progress** goal/task (derive from `instance.current_task_id`/`assignment.task_id` + task state). Only then additionally consult a mission scope keyed by that goal/task. Relevance self-gates the rest: the mission-scoped recall query returns nothing when the turn is off-topic, so an unrelated reply pulls no mission context even while a goal is active. When no goal/task is in progress, the shared scope is **not consulted at all**.
- **Redaction-safe write boundary (mandatory for the shared scope).** Today Mission Control redaction only sanitizes the *read* projection. Anything written to the **shared** scope becomes recall-reachable by other personas, so sanitize-on-write there is required (per-persona writes also sanitize but can't leak across roles). Never let a raw secret enter the shared FTS corpus.

Proof:

- Harness test (per-persona isolation): a prior Neko-session fact recalls into a new Neko turn but **not** into a QA turn.
- Harness test (dynamic engage): with the instance on an **in-progress** goal/task, a mission-relevant fact written by Neko is recallable by QA on the same goal; with **no** active goal, the shared scope is never consulted (assert no mission-scoped read happens).
- Harness test (relevance gate): instance on an active goal but the message is off-topic ⇒ no mission context is injected (recall returns nothing relevant).
- Redaction test: a secret in a per-persona turn is never returned by `search_messages`/recall in another persona's scope, and a secret is never written verbatim into the shared scope.

### Stage 2C — Expose persona chat to the Launcher via `persona_chat_history` (Harness + bridge)

Objective: make the now-real transcript reach the UI through the existing read model — **no new snapshot model required**.

Files:

- `agent_runtime/snapshot.py` — `persona_chat_history_summary` already wired at 161-163; confirm it includes the freshly bound session.
- **`mission_control_bridge.dart` (`_mapHarnessSnapshot` at 737, map literal at 831-878) — forward `persona_chat_history`** (see load-bearing gap below).

> **Load-bearing gap (verified).** The live snapshot does not reach `MissionControlSnapshot.fromJson` directly — `_mapHarnessSnapshot` rebuilds the map **field-by-field** and forwards only a fixed key set (bridge:831-878). It forwards `persona_instances` and `persona_assignments` but **not** `persona_chat_history` — so `persona_chat_history` is silently dropped on the live path today and only survives in tests that call `MissionControlSnapshot.fromJson` directly. Add `'persona_chat_history': _list(raw['persona_chat_history'])` (and, if Stage 2D ships, `'agent_chat_streams'`) to the bridge map literal. (Same root cause: `role_streams` is flattened from a single chosen `typedTask`, so free-floating sandbox-task events never reach the UI.)

Proof:

- Bridge test: a raw snapshot with `persona_chat_history` survives `_mapHarnessSnapshot` into `MissionControlSnapshot.personaChatHistory`.

### Stage 2D — Optional `agent_chat_streams` correlation/health overlay (Harness)

Objective: only if a per-instance in-flight correlation + projection-health overlay is needed beyond `persona_chat_history`.

Files: new `agent_runtime/agent_chat_streams.py` (mirror `persona_chat_history.py`), `agent_runtime/snapshot.py` (assembly block 150-166), tests in `tests/agent_runtime/test_agent_chat_streams.py`.

Actions:

- One row per instance keyed by `persona_instance_id`; carry `latest_assignment_id`/`latest_run_id`/`session_id` and `projection_health`.
- `projection_health`: `missing_final_message` (terminal run, no transcript message + no decision text), `missing_run`, `redacted`, else `ok`.
- Reuse `display_*`/`redaction_status` field names; never emit raw transcripts (defer to SessionDB summary for text).

Proof: completed turn ⇒ `state=completed`; terminal run with neither transcript nor decision ⇒ `projection_failed`; secret text ⇒ `redacted` and absent from output.

### Stage 3 — Launcher renders persona chat (adapter migration)

Objective: render the authoritative transcript from `persona_chat_history` (and the overlay if Stage 2D shipped).

Files:

- `mission_control_snapshot.dart` (`personaChatHistory` already parsed at 204; if Stage 2D, parse `agent_chat_streams` near 193-211)
- `mission_agent_chat_adapter.dart` (`missionAgentChatConversationFromInstance` at 16)
- new `lib/features/mission_control/data/mission_agent_chat_stream.dart` only if Stage 2D ships
- `mission_agent_chat_panel.dart`, `mission_control_page.dart`

Actions:

- In `missionAgentChatConversationFromInstance`, when the instance has a bound session, render messages from its `persona_chat_history` entry (and the Stage 2D overlay for state/health). Select by `persona_instance_id` first, then `persona_id`/`session_id`.
- Map `redaction_status` exactly as the current `MissionAgentLogEvent` path does (129-140).
- Keep the legacy `sourceLog` path only for old snapshots that carry neither.

Proof:

- Dart test: snapshot with `sourceLog == null` but a populated `persona_chat_history` (and overlay) renders messages.
- Existing legacy `agent_logs`/`sourceLog` tests still pass.

### Stage 4 — Command-result correlation contract

Objective: carry the send's identity from UI through Harness back to readback.

Files:

- `mission_control_bridge.dart` (accepted path at 114-118 — stop discarding stdout)
- `mission_control_actions.dart` (`MissionControlActionResult` at 520 — add fields)
- `mission_control_page.dart` (`_submitIntent` at 290 — propagate result) / `mission_agent_chat_panel.dart`
- Harness: `hermes_cli/harness.py` (`--client-message-id` arg → `_queue_free_floating_assignment`), `agent_runtime/persona_assignments.py` (`PersonaAssignmentSpec` at 23 — add `client_message_id`/metadata), `agent_runtime/snapshot.py` / `agent_chat_streams.py` (echo it)

Actions:

- Bridge: on accepted, parse the CLI JSON tail and populate new `MissionControlActionResult` fields `assignmentId`, `personaInstanceId`, `runId`, `turnId`, `taskId`, `clientMessageId` (data already emitted at harness.py:806-844).
- Widen `MissionControlActionResult` (currently `status` + `safeMessage` only) with those nullable fields; thread them back so the panel can correlate the exact turn instead of only the instance.
- Add an optional `client_message_id` end-to-end: CLI arg → `PersonaAssignmentSpec` field → persisted on the assignment → echoed in `agent_chat_streams.client_message_id`. (`PersonaAssignmentSpec` has no metadata field today, so this is a real struct addition, not a passthrough.)
- Note: primary stream selection does **not** require this — `persona_instance_id` (== `instanceId`) already correlates. `client_message_id` only disambiguates concurrent turns within one stream.
- **Idempotency dedup (gap inventory):** the gateway honors `Idempotency-Key` (`_IdempotencyCache`, api_server.py:619) but the harness CLI path does not — a retried send creates a duplicate assignment/run. Dedup harness-side on `client_message_id`: if an assignment with the same `client_message_id` exists for the instance, return it instead of queuing a new one.

Proof:

- Bridge unit test: CLI JSON ⇒ `MissionControlActionResult` correlation fields populated.
- Harness CLI test: `--client-message-id` round-trips into the assignment and snapshot.

### Stage 5 — Hermes-style typed event vocabulary

Objective: formalize Agent Chat as typed events in the registry.

Files: `agent_runtime/decision_contract_registry.py` (add `EventContract`s next to 1154-1157), emit sites in `persona_assignments.py` / `persona_diagnostics.py`, `agent_chat_streams.py` (map into `events[].type`).

New `EventContract`s (required/optional keys per existing pattern):

- `persona_message.accepted` (`persona_instance_id`, `assignment_id`; opt `client_message_id`)
- `persona_run.started` (`persona_instance_id`, `run_id`)
- `persona_progress.recorded`, `persona_tool.started`, `persona_tool.completed`
- `persona_message.finalized` (`persona_instance_id`, `run_id`)
- `persona_message.reply` (`persona_instance_id`, `run_id`) — conversational operator reply, distinct from a task-scoping decision; see Open gap #3. When present, rendering prefers it over the scoping decision for the agent bubble.
- `persona_projection.warning` (`persona_instance_id`; opt `run_id`, `status`)

Proof: registry contract tests; snapshot projection tests; Launcher rendering tests for typed events.

### Stage 6 — Optional real-time transport

Objective: move from polling to streaming later without changing UI semantics.

Actions: SSE/WebSocket tail for `agent_chat_streams` deltas; snapshot remains the reconciliation source; reuse the same typed vocabulary.

Proof: integration smoke — live `hi` to Neko/QA shows running → final/projection-warning without a manual refresh.

## Test plan — end-to-end message flow (widget ⇄ Hermes ⇄ run ⇄ display)

Goal: prove the full round-trip — operator types a message in the Flutter widget → intent → bridge → CLI → harness run → SessionDB → snapshot → bridge map → Dart model → adapter → **both the operator message and the agent reply render**, with no indefinite wait. Tests are layered so a failure localizes to one boundary, plus one golden integration test that exercises the whole Dart pipeline and one live smoke.

Write the layers that guard a stage **before** that stage (red-first). Existing infra to reuse: Python `pytest` under `tests/`; Dart injects a fake `MissionControlCommandRunner` returning `MissionControlCommandResult(stdout: …)` (see `mission_control_bridge_test.dart`); widget tests use the `_harness(instance:, intents:)` helper and capture `onIntent` (see `mission_agent_chat_panel_test.dart`).

### The contract fixture (anti-drift spine)

Capture one real `hermes harness snapshot --json` **after** a bound persona turn into a shared fixture, e.g. `tests/fixtures/persona_chat_snapshot.json` (Python) mirrored to `test/features/mission_control/fixtures/persona_chat_snapshot.json` (Dart). Both sides assert against it: Python proves the **producer** emits that shape; Dart proves the **consumer** parses it. This is the single guarantee that the two repos can't silently drift — every other test builds on it.

### Layer 1 — Harness / Python (`tests/agent_runtime/`, `tests/hermes_cli/`)

| Test | Guards | Assert |
| --- | --- | --- |
| `test_persona_chat_session_persisted` | S2A | `create --auto-run` writes operator + agent messages to a `SessionDB` session bound to `instance.session_id` |
| `test_persona_chat_continuity` | S2A | two messages to one instance share `session_id`; turn 2 context includes turn 1 |
| `test_persona_chat_autotitle` | S2A | after turn 1, `persona_chat_history.title` is generated, not the fallback |
| `test_persona_chat_history_projection` | S2C | snapshot `persona_chat_history` has a redaction-safe row for the instance |
| `test_persona_recall_per_persona_isolated` | S2B | a Neko fact recalls into a Neko turn but not a QA turn (default per-persona isolation) |
| `test_persona_recall_shared_when_goal_active` | S2B | instance on an in-progress goal ⇒ a mission-relevant Neko fact is recallable by QA on the same goal |
| `test_persona_recall_no_goal_no_shared` | S2B | no active goal ⇒ shared scope is never consulted (no mission-scoped read) |
| `test_persona_recall_offtopic_no_inject` | S2B | active goal but off-topic message ⇒ no mission context injected (relevance gate) |
| `test_persona_chat_recall_redaction` | S2B | a secret is not recalled across persona scopes and is never written verbatim into the shared scope |
| `test_cli_create_emits_correlation_json` | S4 | `create --auto-run --json` stdout carries `assignment_id`, `persona_instance_id`, `run_ids`, `turn_id`, `task_id` (+ `client_message_id` when passed) |
| `test_cli_resend_is_idempotent` | S4 | same `client_message_id` returns the existing assignment, no duplicate run |
| `test_agent_chat_streams_overlay` | S2D (opt) | `projection_health` = `ok` / `missing_final_message` / `redacted` per case; no fabricated agent message |

### Layer 2 — Bridge (Dart, `mission_control_bridge_test.dart`)

- `_mapHarnessSnapshot forwards persona_chat_history` — feed the contract fixture as snapshot stdout; assert `MissionControlSnapshot.personaChatHistory` is non-empty (guards the silent-drop gap; S2C). Add `agent_chat_streams` variant if S2D ships.
- `accepted result parses correlation IDs` — runner returns the real create JSON; assert `MissionControlActionResult.{assignmentId,personaInstanceId,runId,turnId,clientMessageId}` populated (S4).
- `failure path unchanged` — non-zero exit still yields `failed` + safe message (regression).
- `spawn env carries HERMES_HOME` — the bridge passes an `environment` to the process runner containing `HERMES_HOME` (and `HERMES_AGENT_RUNTIME_ROOT`) matching the gateway profile (guards Open gap #6; S2A).

### Layer 3 — Adapter (Dart, `mission_agent_chat_adapter_test.dart`)

- `renders persona_chat_history when sourceLog null` — instance with `sourceLog == null` + a history entry ⇒ conversation has the operator + agent messages (S3).
- `redaction mapping` — `redaction_status: redacted` ⇒ withheld bubble, no raw text.
- `legacy sourceLog fallback` — no history + legacy `sourceLog` ⇒ old path still renders (regression).

### Layer 4 — Widget (Dart, `mission_agent_chat_panel_test.dart`)

- `send emits persona.instance.* intent` — extend existing; assert intent + accepted copy.
- `runtime timeout with no correlated message ⇒ projection warning` — resolve the runtime future, advance the timeout timer, assert the projection-warning text replaces the waiting banner and is **not** styled as a final answer (S1).
- `didUpdateWidget reconciliation ⇒ banner cleared + agent bubble` — after send, rebuild the panel with a fresh `instance` carrying the history entry; assert the waiting banner is gone and the agent reply bubble is shown (S1 + S3).
- `redacted reply renders withheld` — history entry with redacted text ⇒ withheld bubble.

### Layer 5 — Golden end-to-end (Dart integration, new `mission_agent_chat_flow_test.dart`)

The single test that proves "message flows and displays". Uses the **real** bridge mapping + adapter + widgets, with a fake `MissionControlCommandRunner` scripted to behave like the live CLI:

1. Arrange: runner returns the pre-send snapshot (empty history) for `harness snapshot`, the real create JSON for `persona instance create`, and the **contract-fixture snapshot** (with the turn in `persona_chat_history`) for the next `harness snapshot`.
2. Act: pump `MissionControlPage`, select the persona, type `hi`, tap send.
3. Assert: operator `hi` bubble shows immediately (pending → confirmed); after the action repo accepts and `missionControlSnapshotProvider` is invalidated and reloaded, the page rebuilds and the **agent reply bubble renders**; the indefinite waiting banner never persists.

This exercises every Dart boundary against representative Harness JSON without a real process — the closest deterministic proxy for the live round-trip.

### Layer 6 — Live MCP smoke (`integration_test/`)

Real `hermes` CLI + real Launcher: send `hi` to Neko and QA, assert via MCP screenshot/state that each shows a visible reply **or** an explicit projection warning with a run/assignment ID, and that a follow-up message shares context (continuity). This is acceptance criterion 8.

### Coverage-to-stage map

S1 → Layer 4 (timeout/reconcile). S2A → Layer 1 (persist/continuity/title) + the fixture. S2B → Layer 1 (recall/redaction). S2C → Layer 2 (bridge forward) + Layer 1 (projection). S2D → Layer 1 (overlay) + Layer 2 variant. S3 → Layer 3 + Layer 5. S4 → Layer 1 (CLI JSON/idempotency) + Layer 2 (parse). End-to-end → Layer 5 + Layer 6.

## Severity-ranked gaps

### P0 — Persona chat is disconnected from the Hermes chat + enrichment stack

Evidence: free-floating runtime built with `session_db=None` (harness.py:868); `AgentRunRequest.skip_memory=True` default (profile_runner.py:48); no session binding on auto-run. So persona chats persist no transcript, contribute nothing to the FTS recall corpus, and get no cross-session enrichment — unlike every other Hermes chat surface (Baseline D). Fix: **S2A** (wire SessionDB + bind session) then **S2B** (memory + scope + redaction-on-write).

### P0 — Final answer/projection disappears behind indefinite waiting

Evidence: run completed and snapshot held events, but the Console had no `sourceLog` messages; `missionAgentChatConversationFromInstance` renders only `sourceLog?.events` (adapter:20); Harness instances set `sourceLog: null` (instance:98); `_watchRuntimeCall` (panel:309) has no timeout/projection transition. Fix: S1 (immediate) then S2C/S3 (authoritative transcript).

### P0 — CLI promises transcript/proof readback the read model cannot deliver

Evidence: `_run_free_floating_assignment_once` returns `next_expected: … transcript/proof readback` (harness.py:922); `persona_chat_history_summary` summarizes only bound SessionDB sessions and never reads transcripts (persona_chat_history.py:23-25). Fix: **S2A makes the promise true** by persisting/binding a SessionDB session so `persona_chat_history` has a real transcript to summarize (preferred over softening the CLI wording).

### P1 — No end-to-end regression test for completed free-floating turn readback

Evidence: Launcher tests verify accepted/waiting copy only (panel_test:106); Harness CLI tests don't cover `persona instance create/message/run-once`. Fix: implement the **Test plan** above (Layers 1–6 + the contract fixture), writing each layer red before its stage.

### P1 — Correlation incomplete across UI → assignment/run → readback

Evidence: CLI emits all IDs (harness.py:806-844); bridge discards stdout (bridge:114-118); `MissionControlActionResult` has only `status`/`safeMessage` (actions:520); `onIntent` is `Future<void>` (page:290). Fix: S4.

### P1 — Chat history and assignment-based chat are two separate concepts

Evidence: `persona.instance.open_chat` binds a `session_id` without ticking; `create/message --auto-run` creates a free-floating assignment/run, not a SessionDB message. The persona also has no conversational reply path — it answers operator chat with a task-scoping decision (Open gap #3). Fix: **S2A unifies them** (auto-run uses the same bound SessionDB session the picker opens; assignments/runs attach as evidence on that session). Plus the product decision and a `persona_message.reply` conversational decision type (S5).

## Acceptance criteria

Mission Control Agent Chat is AAA-ready when:

1. Sending `hi` to Neko, QA, Dev, Backend Dev produces either a visible final agent response or an explicit projection warning carrying the run/assignment ID.
2. No state remains indefinitely at `Runtime call completed. Waiting for the refreshed Harness snapshot…`.
3. A completed Harness run has a deterministic UI projection path.
4. **Persona chat turns persist to the shared `SessionDB` and are visible via `persona_chat_history`, bound to `instance.session_id`** (S2A).
5. **Persona chat turns are enriched by per-persona recall by default; the shared mission/operator scope engages only when the instance is on an in-progress, relevant goal/task; and a secret is never recallable across persona scopes nor written verbatim into the shared scope** (S2B).
6. The Launcher renders the transcript independent of legacy `agent_logs`/`sourceLog`; tests cover `sourceLog == null` personas (S2C + S3).
7. Redaction tests cover both the read projection and the **write boundary** into the shared corpus.
8. Live MCP smoke proves Neko and QA operator channels via screenshot/state evidence, including a recall-enriched turn.

## Open gaps — status after runtime verification

**Status: no open decisions or unknowns remain.** All seven items are RESOLVED, DECIDED, or ADDRESSED-in-plan below. The only non-code item left is a security-review sign-off (#7) before the dynamic shared scope is enabled in production — a gate, not a blocker for the minimum slice (S1+S2A+S2C+S3) or for per-persona recall.

1. **Does a bounded free-floating run persist a usable `final_decision`? — RESOLVED (yes).** Inspected persisted runs in `X:/Eternia/agent-runtime-harness/runs`: every completed bounded persona run carries a populated `final_decision` with `summary` and `rationale` (e.g. `propose_acceptance`, `report_qa_verdict`). So `_run_summary` (snapshot.py:1285) will have text to project; S2 will not be stuck on `missing_final_message` for the normal completed case.

2. **Are diagnostic/sandbox runs in the snapshot `runs` list? — RESOLVED (yes).** `PersonaDiagnosticController` uses the default `RunStore()` (persona_diagnostics.py:101), the same store the snapshot lists at top-level `runs` (snapshot.py:150). No sandbox filtering. Builder can correlate via `result.run_ids` ↔ `runs[].run_id`.

3. **The projected "agent reply" is a task-scoping decision, not a conversational answer — OPEN (product gap, highest-value).** This is the deepest finding. When the operator sends `hi`, the persona does **not** answer conversationally; it routes the message through the task-scoping decision contract. A real captured run for the message `"hi"` produced:
   - `type`: `propose_acceptance`
   - `summary`: *"Scope a minimal Hermes Agent diagnostic handoff for the operator-channel hello task."*
   - `rationale`: *"The task only says \"hi\" and has no acceptance criteria, so the safest bounded move is a no-product-edit … diagnostic/smoke assignment with command proof."*

   This yields **usable-but-robotic** text, not a chat reply — and note **Path B (persistence) does not fix tone**: even with a real `SessionDB` transcript (Stage 2A), the persona is decision-constrained, so the persisted content is decision-shaped. Persistence/enrichment and conversational tone are orthogonal.

   **DECIDED:** add a conversational `persona_message.reply` decision type (Stage 5) that the persona may emit for free-floating operator chat; rendering prefers it over the scoping decision. Staged delivery: **S1–S3 ship with the decision-projection as the interim bubble** (already satisfies acceptance criteria 1–7); **S5 adds the conversational reply** so criterion 1 is met in spirit, not just technically. Not a blocker for the minimum slice; it is now an in-plan stage, not an untracked "maybe."

4. **`onIntent` return contract — DECIDED.** Widen `onIntent` from `Future<void>` (page:290) to return `MissionControlActionResult` in Stage 4. This carries the correlation IDs (`assignmentId`/`personaInstanceId`/`runId`/`turnId`/`clientMessageId`) back to the panel for exact-turn correlation and idempotency feedback. `persona_instance_id` remains the primary stream-selection key (so S1/S3 don't depend on this), and `client_message_id` is the additive disambiguator for concurrent turns. Confirmed no `--client-message-id` exists in the CLI today (harness.py argparse), so S4's CLI arg is genuinely new.

5. **Memory-scope policy — DECIDED.** *Per-persona by default + a shared mission/operator scope that engages dynamically (no flag).* The shared scope is consulted only when the instance is on an **in-progress** goal/task and the turn is relevant to it; otherwise per-persona only. Rollout: transcript-only → per-persona recall → dynamic shared scope. Redaction-on-write mandatory for the shared scope. Implemented in Stage 2B.

6. **`HERMES_HOME` parity — CHECKED 2026-06-19: CONDITIONAL FAIL (must fix in S2A; currently not guaranteed).** Resolved values in the dev environment:
   - Gateway/CLI SessionDB: `get_hermes_home()/state.db` = `X:\Eternia\.hermes\profiles\alice\state.db` (with `HERMES_HOME=X:\Eternia\.hermes\profiles\alice`; the file exists).
   - Harness own stores: `paths.store_root()` = `X:\Eternia\.hermes\agent-runtime` (separate — runs/tasks/assignments, **not** SessionDB; not the parity concern).
   - **The risk:** `get_hermes_home()` (hermes_constants.py:53) returns `HERMES_HOME` if set, else the **platform default `LOCALAPPDATA/hermes`** on Windows. Its docstring is explicit: *"Subprocess spawners are expected to propagate HERMES_HOME explicitly."*
   - **The Launcher does not.** The bridge spawns `hermes` via `Process.run(executable, arguments)` (mission_control_process_io.dart:22) with **no `environment:`** — it inherits the parent app env only; nothing in the Launcher (nor `load_stagec_env.ps1`) sets `HERMES_HOME`.
   - **Consequence:** parity holds only when the Launcher happens to be launched from a shell that already exported `HERMES_HOME=…profiles\alice`. Launched any other way, the spawned harness writes/reads `LOCALAPPDATA\hermes\state.db` — a **disjoint** DB from the gateway's. Persona transcripts (S2A), recall (S2B), and `persona_chat_history` would all silently target the wrong corpus, with **no cross-surface enrichment**.
   - **Fix (S2A, Launcher-side):** the bridge must pass `environment` to `Process.run` with `HERMES_HOME` (and `HERMES_AGENT_RUNTIME_ROOT`) resolved to the same profile the gateway uses, merged over the inherited env. Add a bridge test asserting the spawn env carries `HERMES_HOME`. Until fixed, treat shared enrichment as unverified in production.

7. **Recall write-boundary — ADDRESSED in S2B; one review gate remains.** Persisting persona turns into the shared FTS corpus makes them recall-reachable. The mitigation is in-plan: sanitize-on-write is **mandatory for the shared scope** in Stage 2B, with redaction tests (Layer 1) proving secrets are neither recalled across persona scopes nor written verbatim into the shared scope. Remaining action is a **security review sign-off** before the dynamic shared scope is enabled in production — a process gate, not an unresolved design question. (Per-persona recall and transcript-only carry no cross-role exposure and need no gate.)

### Resolved during enterprise pass

- **Persona agent already has the `session_search` tool** (persona_runtime.py:211; profile_runner.py:30) — explicit recall works the moment a `session_db` is wired; only passive prefetch is gated by `skip_memory`.
- **Rails are complete:** `GPTPersonaRuntime(session_db=…)` → `ProfileAgentRunner(session_db=…)` → agent factory (persona_runtime.py:46-52; profile_runner.py:185). The disconnect is the no-arg construction at harness.py:868 plus `skip_memory=True`.
- **`open_chat` binding is reusable** (`instance.session_id`, `mode="chat"`, persona_assignments.py:108-131) — S2A reuses it rather than inventing a new binding.

### Newly closed flow-breakers (verified during this pass)

- **Bridge maps the capability IDs to real CLI subcommands.** `_argsForIntent` (bridge:209/376) emits `harness persona instance {create,message,run-once}` with `--auto-run`/`--max-actions`/`--max-seconds`/`--json` (bridge:429-495). The `--json` flag is already passed, so the correlation payload S4 needs is present in `result.stdout` — it is only discarded, never missing.
- **CLI subcommands exist** with matching flags (harness.py:289-319): `create` (`--auto-run` at 294), `message` (309), `run-once` (314).

## Verification log (2026-06-19)

All evidence paths exist and all line anchors above were read directly in both repos. Key structural confirmations: gateway 7-event vocabulary + final flags + toolCallId orphan drop; `PersonaDiagnosticResult` has no final-message field; agent reply readable only as `run.final_decision`; snapshot redaction convention is `display_*`/`redaction_status`; Launcher `instanceId == persona_instance_id`; bridge accepted path discards CLI stdout; `MissionControlActionResult` is `status`+`safeMessage`; `PersonaAssignmentSpec` has no metadata/`client_message_id` field.

Enterprise-pass confirmations (Baseline D): shared `SessionDB` at `get_hermes_home()/state.db` with FTS5 search (`search_messages`/`search_sessions`); recall is agent-layer (`memory_provider.prefetch`, `session_search` tool, `memory_manager` injection) keyed on the agent's `session_db`; free-floating runtime built with `session_db=None` (harness.py:868); `AgentRunRequest.skip_memory=True` default (profile_runner.py:48); persona toolset already includes `session_search` (persona_runtime.py:211); `session_db` rails complete through `ProfileAgentRunner` (profile_runner.py:185); `open_chat` binding reusable (persona_assignments.py:108-131). Inspected persisted runs in `X:/Eternia/agent-runtime-harness/runs`: bounded persona turns record a structured `final_decision` (e.g. `propose_acceptance`), not a conversational reply — hence Open gap #3.

`HERMES_HOME` parity **checked** (Open gap #6): gateway/CLI SessionDB = `X:\Eternia\.hermes\profiles\alice\state.db` (exists); `get_hermes_home()` falls back to `LOCALAPPDATA\hermes` when `HERMES_HOME` is unset; the Launcher bridge spawns `hermes` with no `environment:` and sets `HERMES_HOME` nowhere — so production parity is **not guaranteed** and must be fixed in S2A.
