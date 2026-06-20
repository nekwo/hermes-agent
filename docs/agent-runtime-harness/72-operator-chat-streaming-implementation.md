# Implementation Doc — Streaming operator chat (Mission Control)

> Hand-off spec for an implementing agent (e.g. ChatGPT). Self-contained: it names
> every file, signature, and call site to change across the two repos. Follow the
> phases in order; each ends with a runnable verification.
>
> Repos:
> - hermes-agent (Python): `X:/Eternia/hermes-agent`
> - Eternia Launcher (Flutter): `X:/Unreal Engine/Engine/Launcher/EterniaLauncher`,
>   branch `feature/mission-agent-chat-s1`, code under `lib/features/mission_control/`
> - Shared chat DB / profile: `HERMES_HOME=X:\Eternia\.hermes\profiles\alice`

## Context / problem

Operator chat in the Eternia Launcher Mission Control is **slow and non-streaming**.
Today a send does this:

1. Launcher panel `_handleSend` → `onIntent` → `_submitIntent`
   (`mission_control_page.dart:290`) → `CliMissionControlActionRepository.submitIntent`
   (`mission_control_bridge.dart:80`).
2. The bridge spawns `hermes harness persona instance create|message --auto-run --json`
   via **buffered** `Process.run` (`mission_control_process_io.dart:30`). It blocks
   until the *entire* agent turn finishes, then returns one JSON object.
3. On `accepted`, the page calls `ref.invalidate(missionControlSnapshotProvider)`
   (`mission_control_page.dart:333`); the reply only becomes visible after the
   **next snapshot refresh** projects it through `persona_chat_history`.

So the operator stares at "Runtime call in progress" for the whole generation **plus**
a snapshot round-trip. Nothing appears incrementally.

The agent already supports token streaming — `AIAgent.run_conversation(..., stream_callback)`
emits text deltas (`run_agent.py:5152`; internal `_stream_callback` / `stream_delta_callback`,
see `run_agent.py:4034-4177`). The gateway's Telegram path consumes these (typed events
`MessageChunk(text)` / `MessageStop(final)` in `gateway/stream_events.py:44-66`, drained by
`gateway/stream_consumer.py` + `gateway/stream_dispatch.py`, which **progressively edits a
Telegram message**). The harness operator-chat path and the Launcher's buffered transport
simply don't carry these deltas.

**Goal (this round):** stream tokens end-to-end so the reply types out live in the chat
bubble, removing the post-completion snapshot round-trip. Model the consumer on the
Telegram streaming pattern (accumulate deltas → progressively render one message → finalize).

**Decisions (locked):**
- **Streaming only.** Do NOT tackle per-message Python cold-start (warm process / daemon)
  this round — separate follow-up.
- **On by default** for operator chat, with **automatic fallback** to the existing buffered
  `--json` path if streaming errors or the platform can't stream.
- Keep the **redaction-on-write boundary** unchanged: streamed deltas are display-only and
  are never persisted; the canonical persisted reply still goes through
  `_redact_persona_chat_text` (`hermes_cli/harness.py`).

## Architecture

Reuse the agent's existing `stream_callback`. Add a thin **NDJSON-over-stdout** transport
between the `hermes` CLI and the Launcher (the Launcher already talks to the CLI; this is the
minimal change — no gateway/socket dependency).

```
AIAgent.run_conversation(stream_callback) ──► chat_reply(stream_callback)
        emits text deltas                       (agent_runtime/persona_runtime.py)
                                                        │
                                  harness chat command callback prints NDJSON lines to stdout
                                  {"type":"chat.delta","text":"He"}        (hermes_cli/harness.py)
                                  {"type":"chat.delta","text":"llo"}
                                  {"type":"chat.final", <today's --json payload> }
                                                        │
                            Dart Process.start, read stdout line-by-line (utf8 + LineSplitter)
                                  (mission_control_process_io.dart)
                                                        │
                       Stream<MissionControlChatStreamEvent> {delta|final|error}
                                                        │
                    panel renders a live agent bubble, finalizes on chat.final
                    (mission_agent_chat_panel.dart) — Telegram "edit one message" analog
```

The NDJSON `chat.final` line carries the **exact same object** the command prints today
under `--json` (so `MissionControlActionResult` parsing is unchanged — it just reads the
final line instead of the whole stdout).

---

## Phase 1 — Hermes: thread `stream_callback` through the chat path

**1a. `agent_runtime/profile_runner.py`**
- Add field to `AgentRunRequest` (dataclass at `:34`):
  `stream_callback: Callable[[str | None], None] | None = None`
- In `ProfileAgentRunner._execute_agent_run`, pass it to **both** `agent.run_conversation`
  call sites (`:197-201` no-wall-clock path and `:233-237` wall-clock path):
  `stream_callback=request.stream_callback`.
  (Signature: `run_agent.py:5152` `run_conversation(self, user_message, system_message,
  conversation_history=None, task_id=None, stream_callback=None, persist_user_message=...)`.)
- No other runner changes. When `stream_callback is None`, behavior is byte-for-byte unchanged.

**1b. `agent_runtime/persona_runtime.py`**
- `GPTPersonaRuntime.chat_reply` (`:156`): add param
  `stream_callback: Callable[[str | None], None] | None = None` and set it on the
  `AgentRunRequest(... stream_callback=stream_callback ...)` it builds (`:180-200`).
  Everything else (hidden scratch source, recall wiring) stays.

> Note: the agent enables streaming when a callback is present (chat-completions branch
> keys on `self._stream_callback`; codex_responses always streams — see
> `run_agent.py` `_interruptible_api_call` / `_run_codex_stream`). No global flag needed.
> The implementing agent should confirm the exact gate in `run_agent.py` and, if a
> `HERMES_STREAMING_ENABLED`-style guard blocks the chat-completions branch, set it for
> this process or follow the gate already used by the gateway.

**1c. `hermes_cli/harness.py` — NDJSON streaming output**
- Add `--stream` to the two auto-run chat parsers: `persona instance create` (`:292`) and
  `persona instance message` (`:308`). Thread `stream=getattr(args,"stream",False)` into
  `_cmd_persona_instance_create`/`_message` → `_queue_free_floating_assignment` →
  `_run_free_floating_assignment_once`.
- In `_run_free_floating_assignment_once` (`~:1050`), when streaming is on, build a callback
  and pass it to `chat_reply` (the `GPTPersonaRuntime(...).chat_reply(...)` call at `~:1098`):
  ```python
  def _emit_delta(delta):
      if not delta:
          return
      sys.stdout.write(json.dumps({"type": "chat.delta", "text": str(delta)}) + "\n")
      sys.stdout.flush()
  chat_result = GPTPersonaRuntime(..., session_db=session_db).chat_reply(
      persona, chat_message, session_id=None, max_wall_seconds=max_seconds,
      stream_callback=_emit_delta if stream else None,
  )
  ```
  The callback runs synchronously inside the agent's API loop (same process/thread); raw
  `stdout.write` + `flush` per delta is safe. Deltas are **display-only** — persistence of the
  redacted canonical reply (`_append_persona_assistant_text`) is unchanged.
- **Final line:** when streaming, the command must print the existing result payload as a
  single NDJSON line tagged `chat.final` instead of pretty `--json`. Cleanest: in
  `_queue_free_floating_assignment` (`:852` `print(emit_json(data) ...)`), when `stream` is set,
  print `json.dumps({**data, "type": "chat.final"})` on one line (compact, `ensure_ascii=False`).
  Keep the non-stream `--json` branch exactly as-is.
- **Failure during stream:** if `chat_reply` raises, still print one final line
  `{"type":"chat.final","ok":false,"execution_state":"blocked","blocker":...}` so the Dart
  side always gets a terminator. (The existing blocked-path payload already has these fields.)

**1d. Tests (`tests/agent_runtime/test_persona_assignments.py` or a new
`tests/hermes_cli/test_persona_chat_streaming.py`)**
- Extend the existing `_FakeRuntime` chat_reply mock to invoke a passed `stream_callback`
  with `["He","llo"]` then return `final_response="Hello"`. Assert: (a) `chat_reply` received
  a non-None `stream_callback` when `stream=True`; (b) stdout (capsys) contains two
  `chat.delta` lines then one `chat.final` line whose object equals the buffered payload plus
  `type:"chat.final"`; (c) with `stream=False`, no `chat.delta` lines and output unchanged.
- Add a `ProfileAgentRunner` test asserting `request.stream_callback` is forwarded to
  `agent.run_conversation` (mock agent records kwargs).

---

## Phase 2 — Launcher: streaming transport + live bubble

**2a. `lib/features/mission_control/data/mission_control_process_io.dart` — streaming runner**
- Add (keep `runMissionControlCommand` as-is for buffered callers):
  ```dart
  Stream<MissionControlStreamLine> runMissionControlCommandStreaming(
    String executable, List<String> arguments, {Map<String,String>? environment}) async* {
    final env = environment ?? missionControlHermesProcessEnvironment();
    final proc = await Process.start(executable, arguments, environment: env);
    // stdout: decode UTF-8 (lenient) + split lines → yield raw JSON lines
    yield* proc.stdout.transform(_utf8Lenient.decoder).transform(const LineSplitter())
      .map((l) => MissionControlStreamLine.stdout(l));
    final code = await proc.exitCode;
    yield MissionControlStreamLine.exit(code);
  }
  ```
  Reuse the **already-present** `_utf8Lenient` codec and `missionControlHermesProcessEnvironment`
  (HERMES_HOME + PYTHONUTF8/PYTHONIOENCODING) from this file. Apply the same `hermes` →
  fallback-exe logic as `_runWithFallback` (catch `ProcessException`, retry with
  `_defaultHermesExeFallback()`). Add a stub equivalent in
  `mission_control_process_stub.dart` (yield nothing / not-supported) so non-IO builds compile.

**2b. `lib/features/mission_control/data/mission_control_actions.dart` — event model + API**
- Add a small sealed/event type and a streaming method on the repository:
  ```dart
  class MissionControlChatStreamEvent { // delta | done | failed
    final String? deltaText;            // non-null for delta
    final MissionControlActionResult? result; // non-null for done/failed
  }
  abstract class MissionControlActionRepository {
    Future<MissionControlActionResult> submitIntent(MissionControlIntent intent);
    Stream<MissionControlChatStreamEvent> submitIntentStreaming(MissionControlIntent intent); // NEW
  }
  ```
  `InMemoryMissionControlActionRepository.submitIntentStreaming` (`:546`): emit a couple of
  synthetic deltas + a done event (for tests/non-CLI).

**2c. `lib/features/mission_control/data/mission_control_bridge.dart` — route chat to streaming**
- `_argsForIntent` (capability→args map, the `'persona.instance.message'`/`'create'` arms at
  `:438`/`:461`): append `'--stream'` for these chat capabilities. (Leave message_task /
  diagnose / worker / run capabilities on the buffered path.)
- Implement `CliMissionControlActionRepository.submitIntentStreaming`:
  consume `runMissionControlCommandStreaming`, JSON-decode each line:
  - `type=="chat.delta"` → emit `MissionControlChatStreamEvent(deltaText: text)`.
  - `type=="chat.final"` → build the result via the **existing** `_acceptedActionResult`
    logic (`:138`) from that object (it already reads `assignment_id/persona_instance_id/
    run_ids/turn_id/task_id/client_message_id`); emit done.
  - exit code != 0 with no final line → emit failed (reuse `_failureDetail` shape).
  Keep `submitIntent` (buffered) unchanged as the fallback.

**2d. `lib/features/mission_control/mission_control_page.dart` — expose a streaming submit**
- Add `_submitIntentStreaming(intent) → Stream<MissionControlChatStreamEvent>` mirroring
  `_submitIntent` (`:290`): set `_pendingIntent`, delegate to
  `missionControlActionRepositoryProvider.submitIntentStreaming`, and on the done event keep
  the existing `ref.invalidate(missionControlSnapshotProvider)` (`:333`) so the canonical
  transcript still persists/projects. Pass this down beside `onIntent` to the chat panel
  (new `onIntentStreaming` callback param on the panel + its parent wrappers at
  `:432/:628/:789`).

**2e. `lib/features/mission_control/agent_chat/mission_agent_chat_panel.dart` — live bubble**
- In `_handleSend` (`:221`): when the intent is a chat capability and `onIntentStreaming` is
  available, drive the streaming path instead of `onIntent`:
  - Keep the optimistic operator bubble (already added at `:250`).
  - Add state `String? _streamingAgentText;` and append a synthetic agent `ChatMessage`
    (senderId `'harness-agent'`) built from `_streamingAgentText + ' ▌'` while streaming —
    this is the Telegram "one progressively-edited message" analog.
  - On each `deltaText`: `setState(() => _streamingAgentText = (_streamingAgentText ?? '') + delta);`
    **Batch UI updates** — coalesce deltas with a ~60-80ms timer (or `WidgetsBinding` frame
    callback) so 100+ tokens don't trigger 100 rebuilds.
  - On done: clear `_streamingAgentText`, set `_acceptedMessage`, run the existing
    completion bookkeeping; the snapshot refresh then projects the canonical reply.
  - On failed/stream error: clear `_streamingAgentText` and **fall back** — call the buffered
    `onIntent(intent)` once and surface its result (guarantees a reply even if streaming breaks).
- **Dedup the streamed bubble vs the projection.** Extend the dedup already present at `~:95`:
  it currently drops local **operator** bubbles whose body matches a projected operator turn.
  Generalize to also drop the streamed **agent** bubble once a projected **agent** turn with
  the same body arrives (compare against `conversation.messages` where
  `role == AgentChatRole.agent`, trimmed `safeText`). This prevents the live bubble +
  projected reply from showing twice (same class of bug as the operator duplicate already fixed).

**2f. Tests**
- `mission_control_process_io` / bridge: feed a fake line stream
  (`chat.delta` × N, then `chat.final`) and assert `submitIntentStreaming` yields N delta
  events then a done event whose `MissionControlActionResult` matches `_acceptedActionResult`
  on the final object. Add an exit!=0-without-final case → failed.
- panel widget test: pump with an `onIntentStreaming` that emits deltas + done; assert the
  bubble text grows and that after a projected agent turn is supplied the streamed bubble is
  deduped (one agent bubble, not two). Reuse `_harness` helper (extend it with the new
  callback) in `test/features/mission_control/mission_agent_chat_panel_test.dart`.
- Existing 136 mission_control tests must stay green; `flutter analyze` clean.

---

## NDJSON protocol (authoritative)

One JSON object per line on the CLI's **stdout**, UTF-8:
```
{"type":"chat.delta","text":"<incremental text>"}      // zero or more, in order
{"type":"chat.final", ...buffered --json payload..., "type":"chat.final"}  // exactly one, last
```
`chat.final` carries today's fields: `ok, execution_state, session_id, reply, run_ids,
task_id, turn_id, assignment_id, persona_instance_id, persona_id, client_message_id,
next_expected` (and on failure `blocker`). Anything on **stderr** is logs, not protocol.

## Edge cases

- **Tool-call rounds** (recall/memory): no text deltas arrive during a tool round. Show the
  existing "in progress" affordance until the first delta; don't render an empty bubble.
- **Fallback:** streaming disabled / `Process.start` fails / no `chat.final` / exit!=0 →
  fall back to the buffered `submitIntent` path. Operator always gets a reply.
- **Redaction:** deltas are display-only and never written to SessionDB; the persisted
  canonical reply is still redacted. (Secrets in the *input* are already redacted before the
  agent sees them.) Don't add any new SessionDB write on the stream path.
- **Interrupt / new send while streaming:** cancel the stream subscription, keep accumulated
  text as-is (drop the `▌` cursor), proceed with the new send.
- **Rate of rebuilds:** coalesce deltas (~60-80ms) to avoid jank.

## Files changed (summary)

| Repo | File | Change |
|------|------|--------|
| hermes-agent | `agent_runtime/profile_runner.py` | `AgentRunRequest.stream_callback`; forward at both `run_conversation` call sites |
| hermes-agent | `agent_runtime/persona_runtime.py` | `chat_reply(stream_callback=...)` → `AgentRunRequest` |
| hermes-agent | `hermes_cli/harness.py` | `--stream` flag; NDJSON `chat.delta` callback + `chat.final` line |
| hermes-agent | `tests/hermes_cli/test_persona_chat_streaming.py` (new) | callback forwarding + NDJSON emission |
| EterniaLauncher | `data/mission_control_process_io.dart` (+stub) | `runMissionControlCommandStreaming` (Process.start, utf8+LineSplitter) |
| EterniaLauncher | `data/mission_control_actions.dart` | `MissionControlChatStreamEvent` + `submitIntentStreaming` |
| EterniaLauncher | `data/mission_control_bridge.dart` | `--stream` for chat caps; parse NDJSON → events; reuse `_acceptedActionResult` |
| EterniaLauncher | `mission_control_page.dart` | `_submitIntentStreaming`; keep snapshot invalidate on done |
| EterniaLauncher | `agent_chat/mission_agent_chat_panel.dart` | live agent bubble; coalesce deltas; extend dedup to agent role; buffered fallback |
| EterniaLauncher | `test/features/mission_control/...` | stream parser + bubble/dedup tests |

## Verification (end-to-end)

1. **Hermes unit:** `python -m pytest tests/agent_runtime tests/hermes_cli -k "stream or persona or chat" -q`
   (1 pre-existing unrelated red allowed: `test_ticker::test_tick_uses_configured_persona_when_agent_store_empty`).
2. **Hermes live NDJSON:**
   `HERMES_HOME=X:\Eternia\.hermes\profiles\alice hermes harness persona instance create
   --persona neko_supervisor --title smoke --message "hey neko" --auto-run --stream`
   → expect `chat.delta` lines streaming, then one `chat.final` line with a `reply`,
   `run_ids:[]`, `task_id:null`.
3. **Launcher:** `flutter analyze` clean; `flutter test test/features/mission_control/` green.
4. **In-app smoke:** `flutter run -d windows` (set `HERMES_HOME`), Mission Control → Neko,
   send "hi" → reply **types out live** in one bubble (no JSON, correct emoji/quotes), your
   own message shows once, and after the snapshot refresh there is still exactly one agent
   bubble (dedup holds). Repeat for QA.

---

## Background / prior context

This builds on the chat-first operator-chat work already landed (see the audit doc
`71-mission-control-agent-chat-streaming-audit.md`). Relevant established facts:
- Operator chat is **chat-first**: sends route conversationally via
  `persona.instance.message` / `persona.instance.create` (CLI `persona instance create|message
  --auto-run`), NOT through the decision/task pipeline. `GPTPersonaRuntime.chat_reply` runs a
  plain conversational turn and returns `final_response`.
- The canonical operator transcript is persisted **redacted** to SessionDB under source
  `agent_runtime_persona_chat` (searchable for recall); the agent's scratch turns go to the
  hidden source `agent_runtime_persona_chat_scratch`. Streaming must not change this.
- The Launcher↔CLI transport already sets `HERMES_HOME` + `PYTHONUTF8`/`PYTHONIOENCODING` and
  decodes stdout as UTF-8 (`mission_control_process_io.dart`); the streaming runner must keep
  that env + decoding.
- The panel already dedupes optimistic **operator** bubbles against the projected
  `persona_chat_history`; this work extends the same pattern to the streamed **agent** bubble.
