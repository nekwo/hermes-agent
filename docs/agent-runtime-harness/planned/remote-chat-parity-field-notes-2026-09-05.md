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
