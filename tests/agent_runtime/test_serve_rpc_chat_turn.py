"""Gateway Stage 3 — the remote WRITE path: ``runtime.chat.message`` / ``.steer``.

Four things are worth pinning here and the rest is bookkeeping:

1. **A paired device can send a chat turn at all.** Stage 1 refused the argv
   lane to devices and mission-chat had no method lane, so a remote device
   could place an agent and could not talk to it — on a gateway whose entire
   point is chat.
2. **An RPC chat turn joins the SAME chat-turn safety ledger a local one does.**
   ``serve``'s contract is that a supervisor must never recycle this process
   while ``chat_turns > 0``. A turn the ledger cannot see because it arrived on
   the other lane is that recycle by another name, and it is the defect this
   file exists to make unreachable.
3. **Exactly-once.** The same ``turn_request_id`` twice runs ONE turn and
   replays the first ack with ``idempotent_replay: True``.
4. **The two doors are one execution.** The RPC door lowers to the argv a local
   send would have used, so no future edit can move one without the other.

The turns are driven by an INJECTED ``dispatch``, which is the same seam every
other serve-loop test uses. That is not a weakening: what is under test is the
lane — accept, dedupe, hand off, account, settle — and running a real provider
turn here would test the provider. The real chat handler is exercised by its
own suites, and the argv this lane builds is asserted literally against it.
"""

from __future__ import annotations

import threading

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    TIER_CONSOLE,
    TIER_READ,
    RpcCaller,
    authorize_call,
)
from agent_runtime.chat_turn import (
    CHAT_MESSAGE_METHOD,
    CHAT_STEER_METHOD,
    CHAT_TURN_METHODS,
    ChatTurnSpawnRefused,
    normalize_chat_message,
    normalize_chat_steer,
    perform_chat_turn,
)
from agent_runtime.chat_turn_reservations import (
    STATE_ACCEPTED,
    STATE_SETTLED,
    reserve_chat_turn,
    turn_request_digest,
)
from tests.agent_runtime.test_serve_socket_lane import (
    WAIT,
    client,
    running_serve,
)

# ── the advertisement ───────────────────────────────────────────────────────


def test_the_manifest_grew_by_two_names_and_the_integer_did_not_move():
    """A set plus an integer. A client only calls what it FOUND in the set, so
    adding a method needs no version bump — the same argument
    ``runtime.persona.prewarm`` and ``runtime.agent.retire`` landed on."""

    manifest = serve_rpc.manifest()
    assert CHAT_MESSAGE_METHOD in manifest["methods"]
    assert CHAT_STEER_METHOD in manifest["methods"]
    assert manifest["contract"] == 1
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    # The ops still ride BESIDE this and do not join it. ``params`` (R-C8) DID
    # join it, additively: a client that ignores the key keeps working, and the
    # keys it lists are exactly the ones these two verbs already honour.
    assert set(manifest) == {"contract", "methods", "tiers", "params"}
    assert set(manifest["params"]) == {CHAT_MESSAGE_METHOD, CHAT_STEER_METHOD}


def test_every_verb_in_the_chat_turn_vocabulary_is_advertised_and_console_tiered():
    """The reader ``CHAT_TURN_METHODS`` did not have until 2026-09-01.

    Everything else in this section names its verbs by hand, and the peer verb
    is asserted in a different file entirely. So the two facts that must hold
    for ANY method whose handler ends in ``perform_chat_turn`` — it is in the
    manifest, and it is ``console`` — were pinned three times, once per verb,
    by whoever remembered. A fourth verb added tomorrow inherits nothing from
    that.

    This enumerates from the tuple instead, and asks the RUNTIME rather than
    the source: ``serve_rpc.manifest()`` is built by the ``@method`` registry at
    import, so a verb spelled into the tuple and never registered fails here,
    and so does one registered at a tier below console. The membership
    assertion is the direction that matters — a source walk would answer a
    question about spelling.

    The reverse direction is deliberately NOT asserted. The manifest carries
    many methods that are not chat turns; what this owns is that the chat-turn
    vocabulary is a SUBSET of what the server advertises, at the right tier.
    """

    manifest = serve_rpc.manifest()

    # The tuple is the vocabulary, so an empty or truncated one must not pass
    # vacuously: it is the thing under test, not the harness.
    assert len(CHAT_TURN_METHODS) >= 3
    assert len(set(CHAT_TURN_METHODS)) == len(CHAT_TURN_METHODS)
    assert CHAT_MESSAGE_METHOD in CHAT_TURN_METHODS
    assert CHAT_STEER_METHOD in CHAT_TURN_METHODS

    for verb in CHAT_TURN_METHODS:
        assert verb in manifest["methods"], f"{verb} runs chat turns but is not advertised"
        assert serve_rpc.method_tier(verb) == TIER_CONSOLE, (
            f"{verb} runs an agent with tools; a tier below console is a door "
            "around console"
        )


def test_both_chat_verbs_declare_console_and_a_read_device_is_refused():
    """The tier decision, asserted where it bites rather than where it is
    written.

    A chat turn runs an agent with tools — it can place, retire, write and
    dispatch — so a tier below ``console`` would be a door around ``console``.
    The second assertion is the one that would have caught a new ``chat`` tier
    word: ``authorize_call``'s device arm is an EQUALITY against the stored
    word, not an ordering, so a third tier would refuse every already-paired
    console device the very thing R11 says it may do.
    """

    assert serve_rpc.method_tier(CHAT_MESSAGE_METHOD) == TIER_CONSOLE
    assert serve_rpc.method_tier(CHAT_STEER_METHOD) == TIER_CONSOLE

    reader = RpcCaller(
        kind=CALLER_DEVICE,
        transport="gateway",
        device_id="phone-1",
        device_tier=TIER_READ,
    )
    console = RpcCaller(
        kind=CALLER_DEVICE,
        transport="gateway",
        device_id="phone-2",
        device_tier=TIER_CONSOLE,
    )
    refused = authorize_call(TIER_CONSOLE, reader)
    assert refused.ok is False
    assert refused.reason == "scope_denied"
    assert authorize_call(TIER_CONSOLE, console).ok is True


# ── one service, two doors ──────────────────────────────────────────────────


def test_the_rpc_door_lowers_to_the_argv_a_local_send_would_have_used():
    """The no-divergence pin, and it is a LITERAL rather than a shape check.

    The whole argument for lowering to argv instead of calling a service
    function is that a remote turn and a local turn become the same execution.
    That claim is only worth anything if this list is asserted, because it is
    the exact string a local ``harness mission-chat message`` is dispatched
    with — including that ``turn_request_id`` arrives as ``--client-message-id``
    with its bytes untouched, which is what makes the turn journal's existing
    exactly-once key on a REMOTE send.
    """

    request = normalize_chat_message(
        {
            "turn_request_id": "outbox-7",
            "persona_id": "neko",
            "message": "status?",
            "session_id": "root-1",
        }
    )
    assert request.argv == [
        "harness",
        "mission-chat",
        "message",
        "--persona",
        "neko",
        "--message",
        "status?",
        "--client-message-id",
        "outbox-7",
        "--requested-by",
        "gateway_device",
        "--json",
        "--session-id",
        "root-1",
    ]
    # And serve reads that argv as a chat turn, which is the join between this
    # lane and the drain ledger. Asserted here rather than trusted, because the
    # ledger's key is a PREFIX MATCH on argv shapes and a re-ordering of the
    # flags above would silently break it.
    from hermes_cli.harness_parts.serve import _ArgvRequest

    assert _ArgvRequest("r1", request.argv).is_chat_turn is True


def test_a_client_cannot_smuggle_a_flag_through_a_value():
    """Values are elements, never text that is re-split.

    The one hazard of lowering to argv. A message that reads like a flag is a
    message: it lands as the element after ``--message`` and argparse takes it
    as that option's value.
    """

    request = normalize_chat_message(
        {
            "turn_request_id": "k1",
            "persona_id": "neko",
            "message": "--json --new-session --persona root",
        }
    )
    assert request.argv.count("--message") == 1
    assert request.argv[request.argv.index("--message") + 1] == (
        "--json --new-session --persona root"
    )
    assert request.argv.count("--persona") == 1
    assert "--new-session" not in request.argv


def test_the_steer_door_lowers_to_the_steer_verb():
    request = normalize_chat_steer(
        {
            "turn_request_id": "steer-1",
            "session_id": "root-1",
            "message": "stop",
            "persona_id": "neko",
        }
    )
    assert request.argv == [
        "harness",
        "mission-chat",
        "steer",
        "--session-id",
        "root-1",
        "--message",
        "stop",
        "--client-message-id",
        "steer-1",
        "--json",
        "--persona",
        "neko",
    ]
    from hermes_cli.harness_parts.serve import _ArgvRequest

    assert _ArgvRequest("r1", request.argv).is_chat_turn is True


# ── refusals at the boundary ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "params,reason",
    [
        ({"persona_id": "neko", "message": "hi"}, "turn_request_id_required"),
        ({"turn_request_id": "k", "message": "hi"}, "persona_id_required"),
        ({"turn_request_id": "k", "persona_id": "neko"}, "message_required"),
        (
            {"turn_request_id": "k", "persona_id": "neko", "message": "hi",
             "correlation_id": "not a token!"},
            "correlation_id_invalid",
        ),
        (
            {"turn_request_id": "k", "persona_id": "neko", "message": "hi",
             "new_session": "yes"},
            "new_session_invalid",
        ),
        (
            {"turn_request_id": "k", "persona_id": "neko", "message": "hi",
             "max_seconds": 0},
            "max_seconds_invalid",
        ),
    ],
)
def test_a_malformed_send_is_refused_with_a_machine_readable_reason(params, reason):
    outcome = perform_chat_turn(
        params, verb=CHAT_MESSAGE_METHOD, spawn=lambda *_: None
    )
    assert outcome.result is None
    assert outcome.refusal.code == serve_rpc.ERR_INVALID_PARAMS
    assert outcome.refusal.data["reason"] == reason


def test_a_transport_with_no_worker_lane_refuses_instead_of_running_inline():
    """``spawn=None`` is the honest state of a test-built or non-duplex context,
    and the refusal is the point: the tempting fallback — run it here — would
    stall whatever loop asked for the whole length of the turn, which is
    precisely why the method lane hands chat turns off in the first place."""

    outcome = perform_chat_turn(
        {"turn_request_id": "k", "persona_id": "neko", "message": "hi"},
        verb=CHAT_MESSAGE_METHOD,
        spawn=None,
    )
    assert outcome.result is None
    assert outcome.refusal.data["reason"] == "chat_turn_lane_unavailable"


def test_a_turn_request_id_reused_against_another_root_is_refused():
    """A key names ONE turn. Answering the second one with the first one's ack
    would hand a client somebody else's turn."""

    spawned: list[tuple[str, list[str]]] = []

    def _spawn(request_id, argv, turn_request_id):
        spawned.append((request_id, argv))

    base = {"turn_request_id": "shared", "persona_id": "neko", "message": "hi"}
    first = perform_chat_turn(
        {**base, "session_id": "root-a"}, verb=CHAT_MESSAGE_METHOD, spawn=_spawn
    )
    assert first.result["accepted"] is True

    second = perform_chat_turn(
        {**base, "session_id": "root-b"}, verb=CHAT_MESSAGE_METHOD, spawn=_spawn
    )
    assert second.result is None
    assert second.refusal.code == serve_rpc.ERR_CONFLICT
    assert second.refusal.data["reason"] == "turn_request_conflict"
    assert len(spawned) == 1


def test_a_refused_spawn_leaves_no_receipt_behind():
    """A drain is a decision this process made after the durable write and can
    still undo. A receipt that outlived it would answer the client's honest
    retry — against the replacement runtime — with ``idempotent_replay`` for a
    turn that never ran."""

    from agent_runtime import paths

    def _refuse(request_id, argv, turn_request_id):
        raise ChatTurnSpawnRefused("draining", "serve is draining")

    outcome = perform_chat_turn(
        {"turn_request_id": "drained", "persona_id": "neko", "message": "hi"},
        verb=CHAT_MESSAGE_METHOD,
        spawn=_refuse,
    )
    assert outcome.result is None
    assert outcome.refusal.data["reason"] == "draining"
    assert not paths.chat_turn_reservation_path(
        turn_request_digest("drained")
    ).exists()

    # And the id is free: the retry that follows a reconnect is a fresh accept,
    # not a replay.
    accepted = perform_chat_turn(
        {"turn_request_id": "drained", "persona_id": "neko", "message": "hi"},
        verb=CHAT_MESSAGE_METHOD,
        spawn=lambda *_: None,
    )
    assert accepted.result["idempotent_replay"] is False


# ── exactly-once, over a real socket ────────────────────────────────────────


def _rpc(connection, rid: str, method: str, params: dict) -> dict:
    connection.send(
        {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    )
    for _ in range(200):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before a reply to {rid}")
        if frame.get("id") == rid and ("result" in frame or "error" in frame):
            return frame
    raise AssertionError(f"no reply to {rid}")


def _await_exit(connection, request_id: str) -> dict:
    for _ in range(200):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError("connection closed before the turn's exit")
        if frame.get("id") == request_id and frame.get("event") == "exit":
            return frame
    raise AssertionError("no exit frame for the turn")


def test_the_same_turn_request_id_twice_runs_one_turn_and_replays_the_ack():
    """THE acceptance, server-side.

    A client that lost its ack — a dropped link, a killed app, an outbox
    draining after a reconnect — re-presents the same ``turn_request_id``. It
    must not send a second turn, and it must be told so in a field it can
    branch on. The dispatch counter is the assertion that matters: it counts
    EXECUTIONS, and it would be 2 for every plausible near-miss implementation.
    """

    dispatched: list[list[str]] = []

    def _dispatch(argv):
        dispatched.append(list(argv))
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="outbox") as (connection, _hello):
            params = {
                "turn_request_id": "outbox-42",
                "persona_id": "neko",
                "message": "status?",
                "session_id": "root-1",
                "correlation_id": "mc.chat.7",
            }
            first = _rpc(connection, "c1", CHAT_MESSAGE_METHOD, params)["result"]
            assert first["accepted"] is True
            assert first["state"] == STATE_ACCEPTED
            assert first["idempotent_replay"] is False
            assert first["turn_request_id"] == "outbox-42"
            assert first["correlation_id"] == "mc.chat.7"
            request_id = first["request_id"]
            assert request_id.startswith("chat-")

            assert _await_exit(connection, request_id)["code"] == 0

            # The retry. Byte-identical params, as an outbox row replays.
            second = _rpc(connection, "c2", CHAT_MESSAGE_METHOD, params)["result"]
            assert second["idempotent_replay"] is True
            # It names the ORIGINAL stream, which is the one field of the ack a
            # reconnecting client provably cannot rebuild for itself.
            assert second["request_id"] == request_id
            assert second["turn_request_id"] == "outbox-42"
            # And the receipt learned its worker ended, so a client can tell a
            # turn that finished from one whose serve was killed mid-flight.
            assert second["settled"] is True
            assert second["exit_code"] == 0

    assert len(dispatched) == 1
    assert dispatched[0][:3] == ["harness", "mission-chat", "message"]
    assert "--client-message-id" in dispatched[0]
    assert dispatched[0][dispatched[0].index("--client-message-id") + 1] == "outbox-42"


def test_a_retry_that_arrives_while_the_first_turn_is_still_running_is_a_replay():
    """The window the acceptance actually lands in.

    A link that dies mid-send dies while the turn is running, so the retry
    arrives before any exit. This is the case the turn journal alone cannot
    answer — its first write happens inside the chat-root lease, after the
    worker is already going — and it is the whole reason an accept receipt
    exists.
    """

    started = threading.Event()
    release = threading.Event()
    dispatched: list[list[str]] = []

    def _dispatch(argv):
        dispatched.append(list(argv))
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="outbox") as (connection, _hello):
            params = {
                "turn_request_id": "mid-flight",
                "persona_id": "neko",
                "message": "hi",
                "session_id": "root-1",
            }
            first = _rpc(connection, "m1", CHAT_MESSAGE_METHOD, params)["result"]
            assert started.wait(WAIT)

            replay = _rpc(connection, "m2", CHAT_MESSAGE_METHOD, params)["result"]
            assert replay["idempotent_replay"] is True
            assert replay["settled"] is False
            assert replay["request_id"] == first["request_id"]

            release.set()
            assert _await_exit(connection, first["request_id"])["code"] == 0

    assert len(dispatched) == 1


# ── the chat-turn safety ledger ─────────────────────────────────────────────


def test_an_rpc_chat_turn_holds_the_drain_open_exactly_as_a_local_one_does():
    """The load-bearing test of this stage.

    ``serve``'s own contract: a supervisor must never recycle this process
    while ``chat_turns > 0``, and a drain deadline firing ``hard_exit`` over a
    live turn is that recycle by another name. The ledger is keyed at the ARGV
    boundary (``_CHAT_TURN_COMMANDS``), so a chat turn that reached the runtime
    on the METHOD lane and did not join it would be invisible to exactly the
    protection it needs — a serve recycling mid-turn because the turn came in
    the other door.

    The assertion that would pass vacuously if the turn had simply been killed
    is the last one: the turn LANDS, with its own exit frame, after the
    deadline it outlived.
    """

    started = threading.Event()
    release = threading.Event()
    exits: list[int] = []

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(
        dispatch=_dispatch,
        drain_socket_minimum_deadline_seconds=0.2,
        drain_poll_interval_seconds=0.01,
        hard_exit=exits.append,
    ) as handle:
        with client(handle, name="turn-holder") as (connection, _hello):
            accepted = _rpc(
                connection,
                "d1",
                CHAT_MESSAGE_METHOD,
                {
                    "turn_request_id": "held-1",
                    "persona_id": "neko",
                    "message": "long one",
                    "session_id": "root-1",
                },
            )["result"]
            request_id = accepted["request_id"]
            assert started.wait(WAIT)

            connection.send({"op": "ping"})
            busy = _read_event(connection, "busy")
            assert busy["chat_turns"] == 1

            connection.send({"op": "drain", "force": True, "deadline_seconds": 0.2})
            assert _read_event(connection, "draining")["pending"] == 1

            held = _read_event(connection, "drain_timeout")
            assert held["terminal"] is False
            assert held["held_by_chat_turns"] == 1
            assert held["chat_turn_request_ids"] == [f"conn-1:{request_id}"]

            # Nothing was killed while it was held — the whole point.
            assert exits == []

            # A second lapse proves it re-arms rather than stopping at one.
            second = _read_event(connection, "drain_timeout")
            assert second["terminal"] is False
            assert second["held_by_chat_turns"] == 1

            release.set()
            assert _await_exit(connection, request_id)["code"] == 0
            assert exits == []


def test_a_draining_serve_refuses_a_new_chat_turn_and_accounts_for_it():
    """The method lane keeps answering during a drain, deliberately, on the
    argued grounds that an inline handler cannot be cut off half-done. A chat
    turn is the counter-example that argument itself names — it is the work the
    drain exists to protect — so it is refused, and the refusal is COUNTED on
    the terminal frame exactly as an argv refusal is.
    """

    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(
        dispatch=_dispatch,
        drain_socket_minimum_deadline_seconds=0.05,
        drain_poll_interval_seconds=0.01,
    ) as handle:
        with client(handle, name="late") as (connection, _hello):
            # One request in flight, so the drain has something to wait FOR.
            # A drain with nothing pending completes and closes the lane before
            # a second frame can be sent at all, which is a different test.
            connection.send({"id": "holder", "argv": ["harness", "status"]})
            assert started.wait(WAIT)

            connection.send({"op": "drain", "force": True, "deadline_seconds": 5.0})
            assert _read_event(connection, "draining")["pending"] == 1

            reply = _rpc(
                connection,
                "late-1",
                CHAT_MESSAGE_METHOD,
                {
                    "turn_request_id": "too-late",
                    "persona_id": "neko",
                    "message": "hi",
                    "session_id": "root-1",
                },
            )
            assert reply["error"]["code"] == serve_rpc.ERR_CONFLICT
            assert reply["error"]["data"]["reason"] == "draining"

            release.set()
            complete = _read_event(connection, "drain_complete")
            # ACCOUNTED, exactly as an argv refusal is: "the restart turned a
            # remote turn away" is a number an operator can read.
            assert complete["requests_refused"] == 1
            assert complete["requests_completed"] == 1

    # And no receipt survives the refusal, so the retry against the replacement
    # runtime is a fresh accept rather than a replay of a turn that never ran.
    from agent_runtime import paths

    assert not paths.chat_turn_reservation_path(
        turn_request_digest("too-late")
    ).exists()


def _read_event(connection, event: str, *, limit: int = 200) -> dict:
    for _ in range(limit):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before {event!r}")
        if frame.get("event") == event:
            return frame
    raise AssertionError(f"no {event!r} within {limit} frames")


# ── the receipt itself ──────────────────────────────────────────────────────


def test_the_receipt_is_KEYED_by_digest_and_the_ack_it_replays_is_verbatim():
    """Two different questions, and the first draft of this test conflated them.

    The KEY is a digest — the filename and the field — because a client-chosen
    string must never become a path component: ``../`` and a 200-character name
    are both things a remote device can send. That is NOT a claim that the id is
    absent from the file. The ack is recorded verbatim so the replay is
    byte-identical to the original accept, and an ack echoes the
    ``turn_request_id`` the client itself sent and is waiting to see. Digesting
    the key and echoing the ack answer different questions; the launcher
    acceptance asserted the wrong one first and this is what it found.
    """

    import json

    from agent_runtime import paths

    hostile = "../../escape me"
    with reserve_chat_turn(
        turn_request_id=hostile,
        verb=CHAT_MESSAGE_METHOD,
        session_scope="root-1",
    ) as reservation:
        reservation.mark_accepted(
            {"accepted": True, "turn_request_id": hostile}, request_id="chat-abc"
        )

    digest = turn_request_digest(hostile)
    path = paths.chat_turn_reservation_path(digest)
    # The id reached no path component: the file sits in the receipts directory
    # under its digest, and nothing walked out of it.
    assert path.parent == paths.chat_turn_reservations_dir()
    assert path.name == f"{digest}.json"

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["turn_request_id_sha256"] == digest
    assert raw["state"] == STATE_ACCEPTED
    assert raw["request_id"] == "chat-abc"
    # And the ack is what it was, so the replay can be what the client saw.
    assert raw["ack"]["turn_request_id"] == hostile


def test_settling_a_receipt_records_the_exit_and_never_raises():
    from agent_runtime.chat_turn_reservations import settle_chat_turn

    with reserve_chat_turn(
        turn_request_id="settle-me",
        verb=CHAT_MESSAGE_METHOD,
        session_scope="root-1",
    ) as reservation:
        reservation.mark_accepted({"accepted": True}, request_id="chat-1")

    assert settle_chat_turn(turn_request_id="settle-me", exit_code=7) is True
    with reserve_chat_turn(
        turn_request_id="settle-me",
        verb=CHAT_MESSAGE_METHOD,
        session_scope="root-1",
    ) as replayed:
        assert replayed.state == STATE_SETTLED
        ack = replayed.replay_ack()
        assert ack["idempotent_replay"] is True
        assert ack["settled"] is True
        assert ack["exit_code"] == 7

    # An id nobody accepted is a False, not a raise: this runs in a worker's
    # ``finally``, where an exception would replace the turn's real exit frame.
    assert settle_chat_turn(turn_request_id="never-seen", exit_code=0) is False


def test_a_settled_replay_does_not_report_state_accepted():
    """One payload must not describe one turn two ways.

    ``mark_accepted`` writes ``state: "accepted"`` INTO the ack, because that
    is what was true when it wrote it. ``replay_ack`` copied that ack and
    stamped ``settled``/``exit_code`` beside it without touching ``state``, so
    a replay after ``settle_chat_turn`` came back saying ``state: "accepted"``
    and ``settled: true`` at once. ``settled`` is the live discriminator and
    stays so; ``state`` now reports what the RECORD says.
    """

    from agent_runtime.chat_turn_reservations import settle_chat_turn

    with reserve_chat_turn(
        turn_request_id="state-honesty",
        verb=CHAT_MESSAGE_METHOD,
        session_scope="root-1",
    ) as reservation:
        # The ack the accept path really writes — see ``chat_turn.py``.
        reservation.mark_accepted(
            {
                "turn_request_id": "state-honesty",
                "accepted": True,
                "state": STATE_ACCEPTED,
                "verb": CHAT_MESSAGE_METHOD,
            },
            request_id="chat-9",
        )

    # Before settling, the replay still says accepted — nothing changed there.
    with reserve_chat_turn(
        turn_request_id="state-honesty",
        verb=CHAT_MESSAGE_METHOD,
        session_scope="root-1",
    ) as replayed:
        ack = replayed.replay_ack()
        assert ack["state"] == STATE_ACCEPTED
        assert ack["settled"] is False

    assert settle_chat_turn(turn_request_id="state-honesty", exit_code=0) is True

    with reserve_chat_turn(
        turn_request_id="state-honesty",
        verb=CHAT_MESSAGE_METHOD,
        session_scope="root-1",
    ) as replayed:
        ack = replayed.replay_ack()
        assert ack["state"] == STATE_SETTLED
        # The discriminator the launcher's decoder actually reads is untouched.
        assert ack["settled"] is True
        assert ack["exit_code"] == 0
        assert ack["idempotent_replay"] is True
        # And the fields that describe the REQUEST are still the client's own.
        assert ack["turn_request_id"] == "state-honesty"
        assert ack["verb"] == CHAT_MESSAGE_METHOD
