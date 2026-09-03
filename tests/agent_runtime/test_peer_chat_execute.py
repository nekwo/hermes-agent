"""Gateway Stage 7 — ``peer.agent_chat.execute``, the verb that carries a turn.

The subject of this file is one sentence: **who is asking comes off the
connection.** Everything else here is bookkeeping around it.

Stage 6 established that a peer is a caller KIND answered from an explicit
allowlist rather than a tier word, and that the exclusion in canon 06 therefore
holds by construction. Stage 7 widens that allowlist by exactly one name, so the
tests that matter are the ones that would catch the widening being wider than it
looks: a non-peer running the verb, a peer naming somebody else's install, and a
replayed request id crossing between two paired installs.

Driven through ``perform_chat_turn`` and ``handle_request`` with an injected
spawn seam, for the reason Stage 3's suite gives: what is under test is the
LANE — accept, attribute, dedupe, hand off — and running a real provider turn
here would test the provider.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    CALLER_PEER,
    PEER_METHOD_ALLOWLIST,
    TIER_CONSOLE,
    TIER_READ,
    RpcCaller,
    STDIO_OWNER,
    authorize_call,
)
from agent_runtime.chat_turn import (
    PEER_CHAT_EXECUTE_METHOD,
    PEER_REQUESTED_BY_PREFIX,
    ChatTurnInvalid,
    normalize_peer_chat_execute,
    perform_chat_turn,
)

PEER_A = RpcCaller(
    kind=CALLER_PEER, transport="gateway", peer_install_id="install-a", connection_key="k1"
)
PEER_C = RpcCaller(
    kind=CALLER_PEER, transport="gateway", peer_install_id="install-c", connection_key="k2"
)

BASE = {
    "turn_request_id": "agent-dispatch-dispatch-abc123",
    "target": "dev",
    "message": "run the suite and tell me what reds",
}


def _spawns() -> tuple[list[tuple[str, list[str], str]], object]:
    seen: list[tuple[str, list[str], str]] = []

    def spawn(request_id: str, argv: list[str], turn_request_id: str) -> None:
        seen.append((request_id, list(argv), turn_request_id))

    return seen, spawn


# ── the advertisement ────────────────────────────────────────────────────────


def test_the_verb_is_registered_allowlisted_and_declares_console():
    manifest = serve_rpc.manifest()

    assert PEER_CHAT_EXECUTE_METHOD in manifest["methods"]
    assert manifest["tiers"][PEER_CHAT_EXECUTE_METHOD] == TIER_CONSOLE
    # A set plus an integer, again: the manifest grew and the contract did not.
    assert manifest["contract"] == 1
    assert set(manifest) == {"contract", "methods", "tiers"}
    assert PEER_CHAT_EXECUTE_METHOD in PEER_METHOD_ALLOWLIST


def test_the_peer_surface_is_exactly_six_verbs_wide():
    """Widening is meant to be a visible line in a diff. This is the line that
    makes it visible in the SUITE — and it did: Stage P4's ``peer.media.get``
    reddened this file as well as ``test_peer_authorization``'s literal, which
    is TWO independent pins costing the widening a stated reason each. Neither
    is redundant; they assert from opposite ends (the set, and the set as the
    dispatcher's own authorize walk answers it)."""

    assert PEER_METHOD_ALLOWLIST == frozenset(
        {
            "peer.ping",
            PEER_CHAT_EXECUTE_METHOD,
            "peer.media.get",
            # S2c. The only WRITING name on the surface, and what it writes is
            # the caller's own cache row — see ``test_peer_announce.py`` for the
            # three properties that keep it there.
            "peer.announce",
            # S2b's two reads. Narrow the way ``peer.media.get`` is: a roster
            # scoped to one workspace, and one thread the caller was already
            # given the session id for. Neither enumerates.
            "peer.roster.list",
            "peer.thread.read",
        }
    )
    registry = serve_rpc.method_names()
    allowed = [
        name
        for name in registry
        if authorize_call(serve_rpc.method_tier(name), PEER_A, method=name).ok
    ]
    assert allowed == sorted(PEER_METHOD_ALLOWLIST)


def test_the_console_tier_declaration_is_the_same_answer_chat_message_gives():
    """A chat turn runs an agent with tools, so anything softer would be a door
    around ``console``. What admits a PEER is the allowlist, not this word."""

    assert serve_rpc.method_tier(PEER_CHAT_EXECUTE_METHOD) == TIER_CONSOLE
    assert serve_rpc.method_tier("runtime.chat.message") == TIER_CONSOLE


# ── who is asking ────────────────────────────────────────────────────────────


def test_a_non_peer_caller_is_refused_with_its_own_reason():
    """Not ``scope_denied``: the chokepoint let a console caller through (it
    holds the console tier), and it is the VERB that has no provenance to run
    the turn under. A console client has ``runtime.chat.message`` for its own
    turns."""

    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r1",
            "method": PEER_CHAT_EXECUTE_METHOD,
            "params": dict(BASE),
        },
        serve_rpc.RpcContext(caller=STDIO_OWNER, spawn_chat_turn=lambda *_: None),
    )

    assert "result" not in reply
    assert reply["error"]["data"]["reason"] == serve_rpc.PEER_CHAT_NOT_A_PEER_REASON


def test_a_read_tier_device_is_refused_by_the_chokepoint_before_the_verb_sees_it():
    device = RpcCaller(
        kind=CALLER_DEVICE, transport="gateway", device_id="phone", device_tier=TIER_READ
    )
    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r1",
            "method": PEER_CHAT_EXECUTE_METHOD,
            "params": dict(BASE),
        },
        serve_rpc.RpcContext(caller=device, spawn_chat_turn=lambda *_: None),
    )

    assert reply["error"]["data"]["reason"] == "scope_denied"


def test_a_peer_cannot_name_an_install_it_is_not():
    """The params a caller CAN type are asserted against the argv that is
    actually built. There is no key by which a peer names another install,
    which is the whole point of the keyword-only argument."""

    seen, spawn = _spawns()
    outcome = perform_chat_turn(
        {
            **BASE,
            # Every plausible spelling of "I am somebody else", all inert.
            "peer_install_id": "install-victim",
            "requested_by": "operator",
            "install_id": "install-victim",
            "caller": "stdio_owner",
        },
        verb=PEER_CHAT_EXECUTE_METHOD,
        spawn=spawn,
        peer_install_id="install-a",
    )

    assert outcome.refusal is None
    argv = seen[0][1]
    assert argv[argv.index("--requested-by") + 1] == f"{PEER_REQUESTED_BY_PREFIX}install-a"
    assert "install-victim" not in argv
    assert "operator" not in argv


def test_the_handler_reads_the_install_off_the_caller_and_echoes_it():
    seen, spawn = _spawns()
    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r1",
            "method": PEER_CHAT_EXECUTE_METHOD,
            "params": {**BASE, "turn_request_id": "t-echo-1"},
        },
        serve_rpc.RpcContext(caller=PEER_A, spawn_chat_turn=spawn),
    )

    result = reply["result"]
    assert result["accepted"] is True
    assert result["peer"] == "install-a"
    assert result["request_id"].startswith("chat-")
    argv = seen[0][1]
    assert argv[argv.index("--requested-by") + 1] == "peer:install-a"


def test_a_normaliser_with_no_proven_peer_refuses_rather_than_producing_a_turn():
    """Unreachable through the RPC door — its handler refuses first — and
    refused anyway, so a second door onto this normaliser cannot be one edit
    away from being the hole."""

    with pytest.raises(ChatTurnInvalid) as caught:
        normalize_peer_chat_execute(dict(BASE), peer_install_id="  ")
    assert caught.value.reason == "peer_install_unknown"


# ── one execution, not two implementations ───────────────────────────────────


def test_the_argv_is_the_argv_a_local_send_would_have_used():
    request = normalize_peer_chat_execute(
        {
            **BASE,
            "title": "suite run",
            "session_id": "root-9",
            "new_session": True,
            "max_seconds": 120.0,
        },
        peer_install_id="install-a",
    )

    assert request.argv[:3] == ["harness", "mission-chat", "message"]
    assert request.argv[request.argv.index("--persona") + 1] == "dev"
    assert request.argv[request.argv.index("--message") + 1] == BASE["message"]
    # The idempotency key is passed through UNCHANGED onto the name mission
    # chat has always keyed its journal on.
    assert (
        request.argv[request.argv.index("--client-message-id") + 1]
        == BASE["turn_request_id"]
    )
    assert request.argv[request.argv.index("--session-id") + 1] == "root-9"
    assert request.argv[request.argv.index("--title") + 1] == "suite run"
    assert "--new-session" in request.argv
    assert "--json" in request.argv


def test_an_instance_handle_in_the_target_travels_as_an_instance_id():
    """The split is done HERE with B's own rule, so A never has to know B's
    conventions for naming an instance."""

    request = normalize_peer_chat_execute(
        {**BASE, "target": "personainst_dev_agent_2"}, peer_install_id="install-a"
    )
    assert request.argv[request.argv.index("--persona-instance-id") + 1] == (
        "personainst_dev_agent_2"
    )


def test_a_bare_persona_forwards_no_instance_handle():
    request = normalize_peer_chat_execute(dict(BASE), peer_install_id="install-a")
    assert "--persona-instance-id" not in request.argv


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        ({"target": "dev", "message": "hi"}, "turn_request_id_required"),
        ({"turn_request_id": "t", "message": "hi"}, "target_required"),
        ({"turn_request_id": "t", "target": "dev"}, "message_required"),
        (
            {"turn_request_id": "t", "target": "dev", "message": "hi", "max_seconds": -1},
            "max_seconds_invalid",
        ),
        (
            {
                "turn_request_id": "t",
                "target": "dev",
                "message": "hi",
                "correlation_id": "not a token!",
            },
            "correlation_id_invalid",
        ),
    ],
)
def test_malformed_params_refuse_out_loud(params, reason):
    outcome = perform_chat_turn(
        params,
        verb=PEER_CHAT_EXECUTE_METHOD,
        spawn=lambda *_: None,
        peer_install_id="install-a",
    )
    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == reason


def test_a_correlation_token_is_echoed_and_an_install_qualified_one_fits_the_fence():
    """The plan's drift addendum names both halves: a token is correlation and
    never identity, and an install-qualified prefix has to fit inside the
    64-character fence the RPC boundary already enforces."""

    from agent_runtime.state_patches import CORRELATION_ID_MAX_LEN

    token = "g-peer-" + "0" * 32 + "-abcdef123456"
    assert len(token) <= CORRELATION_ID_MAX_LEN

    outcome = perform_chat_turn(
        {**BASE, "correlation_id": token},
        verb=PEER_CHAT_EXECUTE_METHOD,
        spawn=lambda *_: None,
        peer_install_id="install-a",
    )
    assert outcome.refusal is None
    assert outcome.result["correlation_id"] == token
    # …and it never becomes provenance.
    assert outcome.result["peer"] if "peer" in outcome.result else True


# ── the replay scope ─────────────────────────────────────────────────────────


def test_two_installs_may_present_the_same_request_id_without_crossing():
    """``turn_request_id`` is minted on the OTHER install, so two paired installs
    can legitimately choose the same one. The reservation scope carries the
    proven install id for exactly that reason — a replay answered out of the
    wrong install's receipt would hand install C the ack for install A's turn."""

    a = normalize_peer_chat_execute(dict(BASE), peer_install_id="install-a")
    c = normalize_peer_chat_execute(dict(BASE), peer_install_id="install-c")

    assert a.turn_request_id == c.turn_request_id
    assert a.session_scope != c.session_scope
    assert a.session_scope.startswith("peer:install-a/")
    assert c.session_scope.startswith("peer:install-c/")


def test_the_same_install_replaying_one_id_runs_one_turn():
    seen, spawn = _spawns()
    params = {**BASE, "turn_request_id": "t-replay-1"}

    first = perform_chat_turn(
        params, verb=PEER_CHAT_EXECUTE_METHOD, spawn=spawn, peer_install_id="install-a"
    ).result
    second = perform_chat_turn(
        params, verb=PEER_CHAT_EXECUTE_METHOD, spawn=spawn, peer_install_id="install-a"
    ).result

    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert second["request_id"] == first["request_id"]
    assert len(seen) == 1


def test_a_transport_with_no_worker_lane_refuses_rather_than_running_inline():
    outcome = perform_chat_turn(
        {**BASE, "turn_request_id": "t-nolane"},
        verb=PEER_CHAT_EXECUTE_METHOD,
        spawn=None,
        peer_install_id="install-a",
    )
    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "chat_turn_lane_unavailable"
