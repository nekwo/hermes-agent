# Remote chat parity — C1h field notes (hermes), 2026-09-05

Running record for stage C1h of `remote-chat-parity.md`, written as the work
happened. Worktree `wt/c1-open-chat`, branch `feat/c1-open-chat-method`, from
main `4e8c053a0a`. Nothing here touched `<store>/.hermes` or the live venv: every
run was `scripts/run_tests.sh` in the sandboxed root the `agent_runtime`
conftest pins.

## 0. THE REPLY-PATH ANSWER, first, because C1l's shape turns on it

**The `stream` lane does not carry a turn's reply. It carries the turn's
EXISTENCE and the name of the chat root it ran in, and nothing more.** Measured
on a real serve, both listeners up, a device paired at `console`, holding a
`stream` subscription on the same connection it sent the turn on:

| Fact | Per-request frame lane (under the ack's `request_id`) | `stream` lane |
|---|---|---|
| the ack (`accepted`/`state`) | yes, as the RPC reply | — |
| the turn's START | yes (first `line` frame) | yes, as a `running_work` `chat_turn` row — *see the caveat* |
| the reply TEXT | **yes**, in the `--json` payload (`ok`, `reply`, `session_id`) | **never** |
| the exit code | yes (`exit` frame) | no |
| the ack's `request_id` | it IS the frame id | **never appears anywhere on the lane** |
| the chat ROOT id | yes, in the payload | yes |
| join key available to a second console | `request_id` | `work_id` = `chat_turn:<turn_request_id>` |

So, for C1l:

1. **The `chat_turn_reply … first_frame=+N ms` receipt (R-C3) belongs on the
   per-request frame lane**, on the connection that sent the turn. That is the
   only lane the reply arrives on, and the only one that can time it.
2. **A second console CAN paint a pending bubble for a turn it did not start** —
   `mission_external_turn_presence`'s fold has a row to work with, and it is
   keyed on `chat_turn:<turn_request_id>`, i.e. **on the id the launcher itself
   minted and sent**, not the server-minted `request_id`. That is better than the
   plan assumed: the console's own `client_message_id` is the join.
3. **That second console cannot read the reply off the stream lane.** It learns
   the root moved (`persona_chat.projected`) and must re-read the transcript.
4. **The caveat, and it is load-bearing: nothing publishes a stream frame on a
   turn's own account.** The hub is event-driven and a chat turn running a model
   appends nothing of its own until it is over, so a running turn's row is
   *present in the projection* but only *delivered* when something else moves the
   event log. Measured directly — see §3.

## 1. What was built, item by item

| Plan item | Commit | What landed |
|---|---|---|
| 1 — the method | `41d8c20a00` | `runtime.persona.instance.open_chat` at `TIER_CONSOLE` in `agent_runtime/serve_rpc.py`; the service in a new `agent_runtime/persona_open_chat.py`; a `payload_sink` seam (`_emit_persona_open_chat_payload`) added to the CLI handler's two arms and its shared error helper |
| 1 — its tests | `b3b802ce47` | `tests/agent_runtime/test_serve_rpc_open_chat.py`, 21 tests |
| 2 — the measurement | `cd6935d65a` | `tests/agent_runtime/test_serve_gateway_chat_reply_lanes.py`, 2 tests on a real serve |
| 3 — these notes | this commit | — |

No wire contract moved: `RPC_CONTRACT_VERSION` is still `1`. The manifest's
`methods` set grew from 25 names to 26, which is the set-plus-integer rule the
`serve_rpc` header states and the rule `runtime.agent.retire` and the two scope
verbs already landed on.

## 2. The shim, and the one thing it needed that the plan did not mention

`_runtime_agent_create`'s discipline is *the sequence lives in a function both
doors call*. For `runtime.agent.create` that is `agent_create.perform_agent_create`;
here the shim has to land one step lower, exactly as `_runtime_chat_message`'s
does, because the sequence is an argparse handler and not a `perform_*`.
`_cmd_persona_instance_open_chat` owns the coordinator budget, the
`--add-instance` mint, the retirement tombstone, the `foreign_chat_session`
ownership fence, the `--new-session` mint reservation and the actor prewarm —
roughly 350 lines — and hoisting them would have produced the second
implementation the discipline exists to forbid.

So the service builds the handler's own `SimpleNamespace` and calls it. **The
row has to come back without `redirect_stdout`**, and that is not a preference:
this handler runs INLINE on a serve's reader loop, `redirect_stdout` rebinds
`sys.stdout` process-globally, and a serve whose stdout is the frame protocol
every other client reads would have that protocol stolen for the length of the
call. The same argument is already written out at `_emit_mission_chat_payload`,
one verb over, where it was learned from the agent-to-agent relay. So this stage
added the same seam for open-chat: every exit of both arms and of
`_emit_persona_open_chat_error` now goes through
`_emit_persona_open_chat_payload`, which prints byte-for-byte what each call
site printed before when no sink is attached.

The identity of the two doors is proved rather than asserted: the method's
result row is compared to the argv verb's row **key for key**, taken through the
real CLI parser and handler. The one key that must differ — `idempotent_replay`,
because the argv call in that test is a replay of the method call — is excluded
and asserted separately.

### Five deviations from the plan's §C1h wording, each with its reason

1. **`client_message_id` is not what this verb's CLI accepts.** The plan says
   "the CLI already accepts `--client-message-id`". It does not on `persona
   instance open-chat`; that flag is on `persona instance create` and on the
   send lane. This verb's flag is `--idempotency-key`, and the launcher's own
   argv template spells the key `idempotency_key` for it. Both param names are
   accepted, `idempotency_key` first, so one client vocabulary reaches one flag.
2. **`title` is not accepted.** Neither the CLI verb nor the launcher's
   `allowedArgs` for `persona.instance.open_chat` has one, and the handler mints
   the root's title itself as `<display name> chat`. Accepting a param the verb
   would silently drop is worse than not having it.
3. **`session_id` and `kill_active` ARE accepted**, though the plan's param list
   names neither. Without `session_id` every `new_session: false` call refuses
   with "session_id is required unless add_instance is true" — the whole rebind
   arm would be dead, and that arm is what C2l needs for opening an existing
   chat.
4. **The minting flags are closed, not passed through.** `add_instance`,
   `placement_id`, `display_name`, `workspace_id` and `realm_id` are pinned
   `False`/`None` rather than read off params: minting a placement over the
   method lane is `runtime.agent.create`'s whole job and it does all of it with a
   reservation. A second minting door here would be the two-lanes-one-fact shape
   `runtime.agent.retire` was built to close. The coordinator-budget arm is
   unreachable as a consequence — it opens only under `add_instance` or
   `kill_active` with a `coordinator:` requester.
5. **`requested_by` defaults to `"cli"` for a non-device caller**, not to a new
   word. It is the argparse default this verb has always had, so the two doors
   answer with the same string; a device caller's own id replaces it, read off
   the `RpcCaller` the transport built and never off params.

Refusal translation is a map from the CLI's own `error_kind` to `serve_rpc`'s
numbers, and the shim authors no strings: the row's `error` becomes the message
and every other key on it — `next_expected` in particular — rides `data`
unchanged. An untyped refusal (the handler has two that predate the typed
vocabulary) falls to `ERR_INVALID_PARAMS` with `reason: "open_chat_refused"`
rather than being given an invented kind.

## 3. The measurement (§2 of the stage), and what it took to make it honest

`tests/agent_runtime/test_serve_gateway_chat_reply_lanes.py`. Real: `serve_loop`
with both listeners, TLS with a pinned certificate, a device paired at `console`
through the real ceremony, a `stream` subscription on that same connection, the
real `dispatch_argv`, the real `runtime.chat.message` accept, and the real
`mission-chat message` handler on the serve's worker pool with its real turn
journal, chat-root lease, transcript persistence and event log. Not real: the
model — `profile_runner._default_agent_factory` is replaced by a scripted agent
answering a fixed string, patched at the module attribute so the production
construction path is the one under test.

The ack is exactly the shape the plan states:

```
{"turn_request_id": "gesture-from-windows-1", "accepted": true,
 "state": "accepted", "verb": "runtime.chat.message",
 "request_id": "chat-<16 hex>", "idempotent_replay": false, "settled": false}
```

### Timings, three consecutive runs, milliseconds after the send

| | run A | run B | run C |
|---|---|---|---|
| ack (RPC reply) | +16 | +62 | +16 |
| first stream delta | +875 | +671 | +454 |
| `--json` payload + `exit`, per-request lane | +1813 | +1015 | +782 |
| last stream frame of the turn | +2250 | +1296 | +1094 |

Run A is the cold one. **The stream lane's news about the chat root arrived
AFTER the per-request lane had already delivered the whole reply, in every
run** — which is the practical form of §0's answer: a console waiting on the
stream lane would be waiting for something that comes later and says less.

What the stream lane carried across the whole turn: `hydrate` on subscribe, then
`delta` frames with `op: "event.appended"` for event types
`persona_instance.created`, `persona_instance.chat_opened` and
`persona_chat.projected`, plus a `patch` frame and (on the slow turn) a
`heartbeat`. The scripted reply text appears in none of them; the ack's
`request_id` appears in none of them; the minted `session_id` does.

**No `chat.trace.appended` delta was seen, and that is a gap in this measurement
rather than a finding.** `stream._delta_op` produces that op from `run.tool.*` /
`run.progress` events, and the scripted agent runs no tools. A real turn that
calls a tool would produce them; whether they carry anything C1l can fold is
unmeasured here.

### The second test had to be built twice, and the reason is the finding

The first version simply waited for a `running_work` `chat_turn` row during a
slow turn. It passed on some runs and timed out on others. That is not flake to
be tuned away: the hub publishes when the event log moves, and a chat turn
running a model appends nothing of its own until it finishes. So "was a row
present at t+4.5 s" is a question about the poll phase, not about the lane.

The test now **forces publishes** — up to five real `runtime.agent.create` calls
across a 12-second dwell, the shape of an operator moving the office while a
turn runs — and asks what the projection says at a moment it chose. Answer, from
a frame taken +3187 ms into the turn:

```json
{"work_id": "chat_turn:slow-gesture-1", "kind": "chat_turn",
 "label": "personainst_qa_agent_9edd7812", "status": "running",
 "owner": {"persona_id": null,
           "persona_instance_id": "personainst_qa_agent_9edd7812",
           "session_id": "persona_chat_personainst_qa_agent_9edd7812_1b64a91433d7"},
 "started_at": "…", "elapsed_seconds": 2, "source_lane": "durable",
 "progress": {"api_calls": null, "in_tool": null,
              "seconds_since_progress": null, "source": "unavailable"},
 "tail_preview": "", "cancellable": false}
```

Three things to carry into C1l: the `work_id` is the launcher's own
`turn_request_id`; `owner.persona_id` is **null** while both other owner fields
are populated; and `progress.source` is `"unavailable"`, so a remote console gets
no progress detail from this row.

### One harness fact worth recording

The measurement first came back `chat_session_db_unavailable` for every turn.
The cause is not a defect: inside a serve, every request runs under
`profile_context.process_home_scope(serve_request_home)`, which IS a
`HERMES_HOME` override, and `persona_chat_durability.default_persona_session_db`
fails **closed** when an override is live and no authority resolved a chat head.
In the field the launcher always starts serve with `HERMES_HEAD_HOME` and the
boot publishes it. The fixture now sets it, which reproduces the field's
arrangement rather than working around the guard — a harness that omitted it
would have measured the guard instead of the lanes.

## 4. Verification

```
$ bash scripts/run_tests.sh tests/agent_runtime/test_serve_rpc_open_chat.py \
    tests/agent_runtime/test_serve_rpc_authorization.py \
    tests/agent_runtime/test_serve_rpc_method_tiers.py \
    tests/agent_runtime/test_serve_rpc_agent_create.py \
    tests/agent_runtime/test_serve_rpc_agent_retire.py \
    tests/agent_runtime/test_serve_rpc_chat_turn.py \
    tests/agent_runtime/test_scope_use_methods.py \
    tests/agent_runtime/test_registry_hygiene.py
=== Summary: 8 files, 190 tests passed, 0 failed (100% complete) in 57.0s (8 workers) ===

$ bash scripts/run_tests.sh tests/agent_runtime/test_peer_authorization.py \
    tests/agent_runtime/test_remote_cockpit_method_carriage.py \
    tests/agent_runtime/test_serve_gateway_lane.py \
    tests/agent_runtime/test_serve_gateway_auth.py \
    tests/agent_runtime/test_gateway_capabilities.py \
    tests/agent_runtime/test_serve_socket_lane.py --file-timeout 900
=== Summary: 6 files, 182 tests passed, 0 failed (100% complete) in 30.8s (8 workers) ===

$ bash scripts/run_tests.sh tests/hermes_cli/test_harness_cli.py \
    tests/agent_runtime/test_persona_assignments.py \
    tests/agent_runtime/test_persona_instance_identity.py \
    tests/agent_runtime/test_persona_chat_continuity.py \
    tests/agent_runtime/test_persona_chat_actor_prewarm.py \
    tests/agent_runtime/test_persona_spelling_authority.py \
    tests/agent_runtime/test_persona_roster_bypass_contract.py \
    tests/agent_runtime/test_chat_scope_instance_rung.py \
    tests/agent_runtime/test_hex_placement_ids.py \
    tests/agent_runtime/test_operator_channels.py
=== Summary: 10 files, 428 tests passed, 0 failed (100% complete) in 247.1s (8 workers) ===

$ bash scripts/run_tests.sh \
    tests/agent_runtime/test_serve_gateway_chat_reply_lanes.py --file-timeout 900
=== Summary: 1 files, 2 tests passed, 0 failed (100% complete) in 30.9s (8 workers) ===
```

The third block is the one that proves the seam did not change the CLI: those
suites are the fielded consumers of the open-chat rows this stage rerouted.

## 5. Open, and owed

- **`owner.persona_id` is `null` on the `running_work` `chat_turn` row** while
  `persona_instance_id` and `session_id` are both populated. Not this stage's to
  fix, and worth a row: a console rendering "who is talking" off that field gets
  nothing.
- **Nothing publishes a stream frame on a chat turn's own account.** A second
  console holding only that lane sees a running turn late, or not until an
  unrelated write moves the log. If C1l wants the Mac's own launcher to show a
  pending bubble promptly, that is a hermes-side change (an event on turn start)
  and it is not in this plan.
- **`chat.trace.appended` is unmeasured** — the scripted agent runs no tools. A
  turn that calls one would produce `run.tool.*` events; whether the resulting
  delta carries anything a console can fold is an open question.
- **No two-machine proof.** Every listener here binds loopback; the LAN bind is
  a config value, not different code. C5 owns the real one.
- **C1l and C2l are unblocked** by §0's table. C2l's lowering has its method and
  its manifest membership; C1l's reply receipt has its lane.

---

# C1h-bis — the turn publishes on its own account

Stage C1h-bis of the same plan, written the same day, from main `997900010e` on
branch `feat/c1-chat-turn-publish` in worktree `wt/c1-open-chat`. It closes the
one item §5 above left open and load-bearing: **nothing published a stream frame
on a chat turn's own account.** Same discipline as C1h — every run was
`scripts/run_tests.sh` in the sandboxed root the `agent_runtime` conftest pins,
and nothing touched `<store>/.hermes` or the live venv.

## 6. What was measured BEFORE, and what it cost the measurement

C1h's §3 is the before-state and it is worth restating as a number rather than a
sentence. The slow measurement could not sample the `running_work` `chat_turn`
row by waiting for it: the hub publishes when the event log moves, and a chat
turn running a model appends nothing of its own between its write-ahead record
and its projection commit. So the test had to **force** publishes — up to five
real `runtime.agent.create` calls across a 12-second dwell — and read the
projection off a frame those writes caused. The row it found was taken +3187 ms
into the turn, at a moment the test chose, with `owner.persona_id` null.

Two consequences, and the second is the one the operator would have felt:

1. the measurement was about the PROJECTION, never about the lane;
2. a second console holding only the `stream` lane (the Mac's own launcher while
   Windows prompts it — C5's arrangement exactly) saw a running turn late, or
   not until an unrelated write moved the log. On an idle machine "not until"
   means "not at all".

## 7. What was built

| Plan item | Commit | What landed |
|---|---|---|
| 1 — the publish | `6ef511230a` | `agent_runtime/chat_turn_presence.py` (`ChatTurnPresence`), two event contracts (`persona_chat.turn_started` / `persona_chat.turn_ended`), both call sites in the chat-turn core, the count golden moved 64 → 66, seven stream goldens regenerated |
| 2 — the owner field | `8be38db8e0` | `running_work._collect_chat_turns` resolves the persona through `_owner_of` with the build-scoped memo every other lane uses |
| 3 — the assertions | `50d71186d0` | the forcing writes removed from the C1h measurement test; `tests/agent_runtime/test_chat_turn_presence.py` (11 tests) for the publisher's contract and the two AST pins |
| 4 — these notes | this commit | — |

### The frame is the delta, and the publish is an event append

There is no new frame kind and there deliberately is none. The projection
already carried the row; what was missing was a reason for the hub to look. An
`EventLog` append IS that reason and is the only one — `stream_frames`' other
wake-ups are its heartbeat and the Stage-12 freshness backstop, and that backstop
exists precisely to NAME a write that appended no event as a producer bug. So
the chat-turn lane now does what the DISPATCH lane of the same projection has
always done: `dispatch.recorded` when work starts, `dispatch.completed` when it
settles, and the row appears and disappears on subscribers' screens because of
them. That symmetry is why the chat lane's rows were invisible and the dispatch
lane's were not, and it is the whole fix.

Two types rather than one with a `change_kind`: the row APPEARS on one and
DISAPPEARS on the other, so "how many turns began here" and "which of them
settled" are different questions about different rows.

### Both doors, one place

`runtime.chat.message` lowers to argv and runs the same argparse tree
`harness mission-chat message` runs (`agent_runtime/chat_turn.py`'s header states
this as the no-divergence guarantee), so the publish sits in the core the two
share — `_mission_chat_commit_turn` for the START, `_cmd_mission_chat_message`
for the END. A publish in the method shim would have left every locally-typed
turn silent, which is the lane the local launcher still uses. It is pinned by
AST rather than left to a reader:
`test_chat_turn_presence.py::test_the_publishes_live_in_the_chat_turn_core`.

## 8. What was measured AFTER

Same file, same fixture, three consecutive runs, **no forcing writes anywhere in
it**. The slow turn holds the provider open for 12 s; nothing else touches the
runtime between the ack and the row.

| | run A | run B | run C |
|---|---|---|---|
| ack (RPC reply), fast turn | +15 ms | +16 ms | +47 ms |
| `--json` payload + `exit`, fast turn | +765 ms | +734 ms | +1719 ms |
| **frame naming `persona_chat.turn_started` AND carrying the row** | **+1891 ms after the ack** | **+1969 ms** | **+2156 ms** |
| the row's own `elapsed_seconds` on that frame | 1 | 1 | 1 |
| **frame naming `persona_chat.turn_ended` with the row gone** | **+468 ms after the exit** | **+547 ms** | **+390 ms** |

The row itself, off a live frame, with the field C1h found null:

```json
{"work_id": "chat_turn:slow-gesture-1", "kind": "chat_turn",
 "label": "personainst_qa_agent_4b457dd3", "status": "running",
 "owner": {"persona_id": "qa",
           "persona_instance_id": "personainst_qa_agent_4b457dd3",
           "session_id": "persona_chat_personainst_qa_agent_4b457dd3_039cfe254cb8"},
 "elapsed_seconds": 1, "source_lane": "durable",
 "progress": {"source": "unavailable"}, "cancellable": false}
```

### The assertion had to be about CAUSALITY, not about arrival

The first version of the new assertion simply waited for a frame carrying the
row. It passed — and it would have passed without this stage on a good run, which
makes it worthless. With `new_session: true` the runtime also appends
`persona_instance.created` and `persona_instance.chat_opened` around the same
moment, and a core built for one of THOSE picks the row up whenever the
write-ahead happens to land first. That is exactly the race C1h measured (passes
some runs, times out on others). Measured here directly: on the first
post-change runs the row arrived at **+281..313 ms after the ack**, on a frame
whose only event was `persona_instance.chat_opened`.

So the predicate is a conjunction: the frame must **name
`persona_chat.turn_started`** and **carry the row**. That asks the question this
stage owns — did the turn's own publish put the row in front of a subscriber —
and it is why the after-numbers above (~1.9–2.2 s) are LARGER than the numbers
of the version that was measuring the wrong thing (~0.3 s). The end predicate is
the twin: names `persona_chat.turn_ended`, and no longer carries the row.

### One measurement bug found on the way

`_stream_says_about`'s event-type extraction read `type` off the top of each
`events` row, where `_delta_entity` does not put it (it nests the event under
`event`). So a COALESCED batch's types went unreported in the C1h lane report —
under-reporting, in the file whose whole job is to report. One extraction now,
shared with both publish predicates. It changes no C1h conclusion: every type
C1h listed was carried on a single-event frame.

## 9. Deviations from the stage's wording, each with its reason

1. **"within one publish interval of the ack" is not measurable as written, and
   the reason is a fact worth keeping.** The ack is returned when the turn is
   ACCEPTED; the row cannot exist until the write-ahead record lands, which is
   after the session mint, the chat open and the actor prewarm — **1.4–1.7 s** of
   real work in this fixture. So the test bounds the start TWICE: off the clock
   (inside half the dwell of the ack — it cannot be the turn's end arriving) and
   off the ROW (`elapsed_seconds` at publish, anchored on the write-ahead stamp,
   ≤ 3 — one publish interval, one core build, one integer floor). The second is
   the tight one and it is the gap this stage actually closed.
2. **The END publish rides the caller's `finally`, not the terminal
   transitions.** `_mission_chat_commit_turn` has fourteen of them and can also
   raise past its caller; a publish "at the end" would be a publish at one of
   fourteen. Pinned as a `finally` by AST for the same reason.
3. **The END event's `state` is READ from the journal at publish time**, not
   passed in. The caller sees only an exit code, and asking the store what state
   the record is in is the one answer that cannot drift from what the projection
   will report. A record already garbage-collected answers `absent` — a fact, and
   a different one from "unreadable".
4. **The END publish's new coverage is the UNHAPPY paths.** A turn that projects
   successfully already moved the log with `persona_chat.projected`, so on a
   healthy turn the end frame was mostly already there. A turn that fails, is
   interrupted, exhausts its wall budget or settles `outcome_unknown` published
   NOTHING and left a phantom `running` row on every second console until an
   unrelated write. That is what `persona_chat.turn_ended` closes, and it also
   makes the healthy path deterministic instead of incidental.
5. **Two registrations move `contract_hash()`, so seven stream goldens moved.**
   The hash is a fingerprint of the whole event catalog and is baked into every
   core as `decision_contract_hash`; registering anything moves it. The byte diff
   on each of the seven files is that one hex string — no frame shape, no key, no
   row. **LAUNCHER MIRROR OWED**, recorded in
   `tests/fixtures/stream_frames/README.md` beside the still-open S0a one; the
   same copy settles both.
6. **The fast measurement keeps its shape.** At ~750 ms a turn can finish inside
   a single publish window, so asserting a start row there would be asserting a
   race — the thing this stage removed from the slow test, re-introduced. It
   still records what the lane carried, and it now shows
   `persona_chat.turn_started` / `.turn_ended` among the types.
7. **`owner.persona_id` uses the session authority, and blanks on
   disagreement.** The journal's own `persona_instance_id` stays the row's
   instance — it recorded which instance RAN the turn, which outranks a
   re-derivation from the root's current binding. If a root was rebound while an
   older turn is still in flight, the persona is left blank rather than pairing
   this turn's instance with another instance's persona: a blank renders as "no
   owning agent", a mismatched pair is a confident falsehood.

## 10. Verification

```
$ bash scripts/run_tests.sh tests/agent_runtime/test_serve_stream_hub.py \
    tests/agent_runtime/test_serve_stream_lane_parity.py \
    tests/agent_runtime/test_serve_stream_resume_lane.py \
    tests/agent_runtime/test_stream.py tests/agent_runtime/test_stream_coalescing.py \
    tests/agent_runtime/test_stream_patch.py tests/agent_runtime/test_stream_resume.py \
    tests/agent_runtime/test_stream_contract_fixture.py \
    tests/agent_runtime/test_stream_stale_first_routing.py \
    tests/agent_runtime/test_stream_boot_build_liveness.py \
    tests/agent_runtime/test_serve_rpc_office_subscribe_live_hub.py --file-timeout 900
=== Summary: 11 files, 159 tests passed, 0 failed (100% complete) in 50.5s (8 workers) ===

$ bash scripts/run_tests.sh tests/agent_runtime/test_chat_lease_finalization_tail.py \
    tests/agent_runtime/test_chat_turn_presence.py \
    tests/agent_runtime/test_mission_chat_outcome.py \
    tests/agent_runtime/test_mission_chat_send_refused_guard_events.py \
    tests/agent_runtime/test_mission_chat_steer.py \
    tests/agent_runtime/test_mission_chat_turn_context.py \
    tests/agent_runtime/test_mission_chat_turn_run_budget.py \
    tests/agent_runtime/test_mission_chat_turns_hardening.py \
    tests/agent_runtime/test_mission_chat_turns_per_session.py \
    tests/agent_runtime/test_persona_chat_continuity.py \
    tests/agent_runtime/test_persona_chat_actor_prewarm.py \
    tests/agent_runtime/test_persona_chat_mints.py \
    tests/agent_runtime/test_persona_chat_wire_boundary.py \
    tests/agent_runtime/test_running_work.py \
    tests/agent_runtime/test_silent_turn_projection.py \
    tests/agent_runtime/test_turn_state_vocabulary.py \
    tests/agent_runtime/test_turn_visibility.py --file-timeout 900
=== Summary: 17 files, 597 tests passed, 0 failed (100% complete) in 45.0s (8 workers) ===

$ bash scripts/run_tests.sh tests/hermes_cli/test_harness_cli.py \
    tests/hermes_cli/test_mission_chat_payload_seam.py \
    tests/hermes_cli/test_mission_chat_turn_envelope.py \
    tests/hermes_cli/test_mission_chat_relay_guard.py \
    tests/hermes_cli/test_mission_chat_title_offpath.py \
    tests/hermes_cli/test_c8_one_order_guards.py \
    tests/agent_runtime/test_relay_session_lifecycle.py \
    tests/agent_runtime/test_agent_chat_log_path.py \
    tests/agent_runtime/test_s30_retired_mission_chat_task_id_response.py --file-timeout 900
=== Summary: 9 files, 188 tests passed, 0 failed (100% complete) in 77.7s (8 workers) ===

$ bash scripts/run_tests.sh tests/agent_runtime/test_serve_rpc_*.py \
    tests/agent_runtime/test_registry_hygiene.py \
    tests/agent_runtime/test_scope_use_methods.py --file-timeout 900
=== Summary: 19 files, 457 tests passed, 0 failed (100% complete) in 90.9s (8 workers) ===

$ bash scripts/run_tests.sh \
    tests/agent_runtime/test_serve_gateway_chat_reply_lanes.py --file-timeout 900
=== Summary: 1 files, 2 tests passed, 0 failed (100% complete) in 34.1s (8 workers) ===
```

Plus the registry/projection quartet the two new contracts and the owner field
move:

```
$ bash scripts/run_tests.sh tests/agent_runtime/test_s15_event_contract_pruning.py \
    tests/agent_runtime/test_s55_registered_events_have_emitters.py \
    tests/agent_runtime/test_s16b_live_event_registration.py \
    tests/agent_runtime/test_running_work.py
=== Summary: 4 files, 122 tests passed, 0 failed (100% complete) in 80.1s (8 workers) ===
```

### Flakes seen, named rather than swallowed

Two, both on this workstation while the reply-lane file was being run in a loop
beside the other suites (16 CPUs, 8 workers, a second session's suite running
alongside). Both are WALL-CLOCK waits in fixtures this stage did not touch, both
passed on the runner's retry, and neither is an assertion this stage added:

- `test_serve_gateway_chat_reply_lanes.py` — *"no frame for the stream
  subscription ack within 20.0s"* (`WAIT`, inherited from
  `test_serve_gateway_lane`), twice in ~12 runs, once on each test in the file.
  It fires before any C1h-bis assertion is reached.
- `test_serve_rpc_office_subscribe_live_hub.py` — *"timed out after 5.0s waiting
  for: the hub's re-baselining hydrate to reach the sink"*, once. Office lane, no
  chat turn in it; the same file passed green twice more, alone and in the group.

Both are the same shape — a fixed second-scale deadline on a contended box — and
neither is evidence about the publish path. They are recorded so a later reader
does not rediscover them as new.

## 11. Open, and owed

- **The launcher's byte mirror of the seven stream goldens**, per deviation 5.
  Not a Dart change: the moved value is an opaque fingerprint. It settles the
  still-open S0a mirror in the same copy.
- **`chat.trace.appended` remains unmeasured** — unchanged from C1h §5, and this
  stage does not touch it. The scripted agent runs no tools.
- **No two-machine proof.** Every listener here binds loopback. C5 owns the real
  one, and C1h-bis is what should make its "on BOTH screens" half work: a second
  console now learns of a running turn from the turn itself.
- **A second console still cannot read the REPLY off this lane.** Unchanged and
  by design (§0's table): it learns the root moved and re-reads the transcript.
  C1l's receipt still belongs on the per-request frame lane.
- **The two fixture flakes above** are pre-existing tight deadlines, not owed
  work of this stage — but they are the kind of thing that eventually costs a
  wave, and they are written down here for whoever decides to widen them.
