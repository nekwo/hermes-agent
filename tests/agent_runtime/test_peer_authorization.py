"""The peer caller kind, its allowlist, and the exclusion that holds by construction.

Gateway Stage 6 / canon 06's remote-connector table: *"Peer tier (an agent on
install A addressing install B) — deliberately excluded: agents never mint or
retire agents on another install; a remote OPERATOR does."*

This file is that sentence's enforcement, and the shape of the enforcement is
the subject. A peer is not a third TIER word compared against a verb's
declaration; it is a caller KIND answered from an explicit allowlist. The
difference only shows up when the registry GROWS — which is why the central test
here iterates ``serve_rpc.method_names()`` rather than naming
``runtime.agent.create`` and ``runtime.agent.retire``. A rule pinned by two
literals stops being pinned the moment a third verb arrives, and the whole point
of an allowlist is that it admits nothing it was not edited to admit.

The position of the peer arm inside ``authorize_call`` is the other load-bearing
fact. Read-tier verbs are open to every caller including ``unknown`` — A5 kept
that deliberately — so a peer evaluated after the read arm would inherit this
runtime's entire read surface: the office core, the subscribe lane, and every
read verb nobody has written yet. There is a test for exactly that ordering,
because it is invisible in any test that only asks about console verbs.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import (
    CALLER_DEVICE,
    CALLER_PEER,
    CALLER_UNKNOWN,
    LOCAL_CONSOLE_METHODS,
    PEER_METHOD_ALLOWLIST,
    REASON_SCOPE_DENIED,
    TIER_CONSOLE,
    TIER_READ,
    TRANSPORT_GATEWAY,
    LOCAL_CONSOLE,
    RpcCaller,
    STDIO_OWNER,
    authorize_call,
    caller_for_connection,
)

PEER = RpcCaller(
    kind=CALLER_PEER, transport=TRANSPORT_GATEWAY, peer_install_id="inst_far_away"
)


class _Connection:
    """The duck-typed shape ``caller_for_connection`` reads through ``getattr``."""

    def __init__(self, **fields):
        self.key = "conn-1"
        self.transport = TRANSPORT_GATEWAY
        self.authenticated = True
        self.device_id = None
        self.device_tier = None
        self.peer_install_id = None
        for name, value in fields.items():
            setattr(self, name, value)


# ── the exclusion, iterated ──────────────────────────────────────────────────


def test_a_peer_is_refused_every_registered_method_except_the_allowlist():
    """THE test of this stage, and it walks the registry on purpose.

    Naming ``runtime.agent.create`` and ``runtime.agent.retire`` would pin the
    canon's two examples and nothing else — so a verb registered next week would
    join the surface with no test noticing. Iterating means the assertion is
    about the RULE ("a peer may call the allowlist and nothing else"), which is
    the property the exclusion actually rests on.
    """

    registry = serve_rpc.method_names()
    assert registry, "the registry is empty; this test would pass vacuously"

    allowed, refused = [], []
    for name in registry:
        decision = authorize_call(serve_rpc.method_tier(name), PEER, method=name)
        (allowed if decision.ok else refused).append(name)

    assert allowed == sorted(PEER_METHOD_ALLOWLIST)
    assert set(refused) == set(registry) - PEER_METHOD_ALLOWLIST
    # The canon's two, named here as a READABILITY assertion on top of the
    # iterated one — never as the thing doing the work.
    assert {"runtime.agent.create", "runtime.agent.retire"} <= set(refused)


def test_the_refusal_is_the_typed_scope_denied_the_launcher_already_branches_on():
    decision = authorize_call(TIER_CONSOLE, PEER, method="runtime.agent.retire")

    assert decision.ok is False
    assert decision.refusal_data() == {
        "reason": REASON_SCOPE_DENIED,
        "tier": TIER_CONSOLE,
        "caller": CALLER_PEER,
    }


def test_a_peer_is_refused_read_verbs_too_which_is_the_arm_ordering(
):
    """The ordering test, and it is the one a reader should be most suspicious of.

    ``authorize_call``'s read arm returns ok for EVERY caller including
    ``unknown``. If the peer arm ran after it, a peer would silently hold this
    runtime's whole read surface — every office read, the subscribe lane, and
    each read verb added from now on — and no console-verb test would ever
    notice. So the claim is asserted directly against a real read-tier method.
    """

    reads = [
        name
        for name in serve_rpc.method_names()
        if serve_rpc.method_tier(name) == TIER_READ
        and name not in PEER_METHOD_ALLOWLIST
    ]
    assert reads, "no read-tier verb outside the allowlist; this proves nothing"

    for name in reads:
        assert authorize_call(TIER_READ, PEER, method=name).ok is False, name

    # …while the same verbs stay open to the callers A3 and A5 left open. The
    # sample EXCLUDES ``LOCAL_CONSOLE_METHODS``, and that exclusion is the
    # correction rather than a convenience: those verbs are restricted by KIND
    # and not by strength (WS4 / R-B, and S2d's peer-directory door), so "a read
    # verb is open to ``unknown``" was never a claim about them. Picking one
    # blindly made this assertion depend on which name sorted first.
    open_reads = [name for name in reads if name not in LOCAL_CONSOLE_METHODS]
    assert open_reads, "every read verb is kind-restricted; this proves nothing"
    assert authorize_call(TIER_READ, STDIO_OWNER, method=open_reads[0]).ok is True
    assert authorize_call(TIER_READ, None, method=open_reads[0]).ok is True


def test_a_peer_naming_no_method_is_refused_rather_than_defaulted():
    """Absence of a name is absence of a decision, and this module's second rule
    is that absence of a decision is never an allow."""

    assert authorize_call(TIER_READ, PEER).ok is False
    assert authorize_call(TIER_READ, PEER, method="").ok is False
    assert authorize_call(TIER_READ, PEER, method="   ").ok is False
    assert authorize_call(TIER_CONSOLE, PEER, method=None).ok is False


def test_the_allowlist_is_exactly_its_verbs_and_all_methods_exist():
    """A membership set naming a verb nobody registered would be an allowlist
    that admits nothing — green, and describing a lane that does not work.

    Stage 7 widened it by ONE name, Stage P4 by one more, S2c by a fourth and
    S2b by two, and the literal here is the counterweight that makes each
    widening cost something: a set spelled out in a test is a set nobody grows
    without editing this line and saying why.

    P4's name is ``peer.media.get`` (ruling R-P3): read-only, handle-only, and
    LOCAL-scope-only on the far side, so a paired install can spend a handle
    that install's own reply minted and can enumerate nothing.

    S2c's is ``peer.announce`` (R-IP12), and it is the only name here that
    WRITES. What it writes is the caller's own row in ``peers_cache.json`` — a
    file that gates nothing and that no credential path reads — and the three
    properties that keep it from being more than that are asserted in
    ``test_peer_announce.py``: the row is addressed by the id the transport
    proved and there is no parameter for another, an announced fingerprint is a
    notice and never the pin, and ``revoked_you`` is one-way. The arguments live
    on ``PEER_METHOD_ALLOWLIST`` and on the handler; this line is the price.

    S2b's two are READS (R-IP9): ``peer.roster.list`` answers who is addressable
    here, projected by THIS install's own workspace rules so the caller never
    guesses at a scope only this install can resolve; and ``peer.thread.read``
    answers with the tail of ONE thread the caller was already handed the
    session id for. Both are narrow the way ``peer.media.get`` is — no
    enumeration beyond one workspace, no path, no browse — and
    ``peer.thread.read`` requires the ``target`` as well as the session, so the
    SAME lane guard the local ``agent_chat_open`` applies runs on the far side
    too."""

    assert PEER_METHOD_ALLOWLIST == frozenset(
        {
            "peer.ping",
            "peer.agent_chat.execute",
            "peer.media.get",
            "peer.announce",
            "peer.roster.list",
            "peer.thread.read",
        }
    )
    for name in PEER_METHOD_ALLOWLIST:
        assert name in serve_rpc.method_names()
        assert authorize_call(TIER_READ, PEER, method=name).ok is True
        assert authorize_call(TIER_READ, PEER, method=name).reason == "peer_allowlisted"


def test_a_peer_cannot_widen_its_own_grant_by_naming_a_method_it_is_not_calling():
    """The gate reads the name the DISPATCHER resolved, never a params key — so
    this is a test of the call site as much as the predicate. ``handle_request``
    passes the method it looked up in ``_METHODS``; there is no argument by which
    a request names a different one."""

    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "r1",
            "method": "runtime.agent.retire",
            # Everything a caller can type is an assertion, and none of it is read.
            "params": {"method": "peer.ping", "tier": "read", "caller": "stdio_owner"},
        },
        serve_rpc.RpcContext(caller=PEER, transport=TRANSPORT_GATEWAY),
    )

    assert reply["error"]["data"]["reason"] == REASON_SCOPE_DENIED
    assert reply["error"]["data"]["caller"] == CALLER_PEER


# ── the other callers are untouched ──────────────────────────────────────────


def test_stage_six_moved_nothing_for_the_console_the_cli_or_a_device():
    """A5's grandfather clause and the device equality, asserted after the peer
    arm landed in front of them. The peer arm returns early only for peers."""

    for caller in (STDIO_OWNER, LOCAL_CONSOLE):
        assert authorize_call(TIER_CONSOLE, caller, method="runtime.agent.retire").ok
        assert authorize_call(TIER_READ, caller, method="runtime.office.get").ok

    console_device = RpcCaller(
        kind=CALLER_DEVICE,
        transport=TRANSPORT_GATEWAY,
        device_id="dev_1",
        device_tier=TIER_CONSOLE,
    )
    read_device = RpcCaller(
        kind=CALLER_DEVICE,
        transport=TRANSPORT_GATEWAY,
        device_id="dev_2",
        device_tier=TIER_READ,
    )
    assert authorize_call(TIER_CONSOLE, console_device, method="runtime.agent.retire").ok
    assert (
        authorize_call(TIER_CONSOLE, read_device, method="runtime.agent.retire").ok
        is False
    )
    # A device keeps the whole read surface — it is a client of THIS install,
    # whose operator chose its tier. That is the asymmetry with a peer, and it
    # is deliberate rather than an oversight in one of the two arms.
    assert authorize_call(TIER_READ, read_device, method="runtime.office.get").ok


def test_a_device_may_also_ping_because_the_row_says_read_and_means_it():
    """The tier map is not narrowed by the allowlist; the PEER lane is. A row
    saying ``read`` that a read-tier device could not call would be the map
    lying to the reader it exists for."""

    device = RpcCaller(
        kind=CALLER_DEVICE,
        transport=TRANSPORT_GATEWAY,
        device_id="dev_1",
        device_tier=TIER_READ,
    )

    assert serve_rpc.manifest()["tiers"]["peer.ping"] == TIER_READ
    assert authorize_call(TIER_READ, device, method="peer.ping").ok is True


# ── the transport builds the caller, nothing else does ───────────────────────


def test_a_peer_stamped_connection_becomes_a_peer_caller():
    caller = caller_for_connection(_Connection(peer_install_id="inst_far_away"))

    assert caller.kind == CALLER_PEER
    assert caller.peer_install_id == "inst_far_away"
    assert caller.transport == TRANSPORT_GATEWAY
    assert caller.device_id is None and caller.device_tier is None
    assert caller.describe()["peer_install_id"] == "inst_far_away"


def test_a_connection_wearing_both_stamps_is_unknown_not_a_precedence():
    """The transport never produces this — a hello names one credential and the
    authenticator refuses a frame that names two — so it is answered with the
    LEAST authority rather than by a rule about which identity wins. A rule like
    "peer beats device" is a rule somebody can flip, and the flip is invisible.
    """

    caller = caller_for_connection(
        _Connection(peer_install_id="inst_x", device_id="dev_1", device_tier=TIER_CONSOLE)
    )

    assert caller.kind == CALLER_UNKNOWN
    assert authorize_call(TIER_CONSOLE, caller, method="runtime.agent.retire").ok is False


def test_an_unauthenticated_gateway_connection_is_never_a_peer():
    caller = caller_for_connection(
        _Connection(peer_install_id="inst_x", authenticated=False)
    )

    assert caller.kind == CALLER_UNKNOWN


def test_a_loopback_connection_is_still_the_local_console():
    """The mirror: Stage 6 added an arm in front of the grandfathered one, and a
    guard that took the local lane with it would pass every peer assertion."""

    connection = _Connection()
    connection.transport = "socket"

    caller = caller_for_connection(connection)

    assert caller.kind != CALLER_PEER
    assert authorize_call(TIER_CONSOLE, caller, method="runtime.agent.retire").ok


# ── peer.ping's own contract ─────────────────────────────────────────────────


def test_the_ping_answers_without_reading_a_store(tmp_path, monkeypatch):
    """The cheapest verb on the wire must not open a file. Proved by breaking
    root resolution outright and calling it anyway — if the handler ever grows a
    store read, this reddens instead of quietly costing an I/O per ping."""

    from agent_runtime import paths

    def _explode():
        raise AssertionError("peer.ping resolved a runtime root")

    monkeypatch.setattr(paths, "store_root", _explode)

    reply = serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "p1", "method": "peer.ping", "params": {}},
        serve_rpc.RpcContext(caller=PEER, transport=TRANSPORT_GATEWAY),
    )

    assert reply["result"]["pong"] is True
    assert reply["result"]["contract"] == serve_rpc.PEER_PING_CONTRACT
    assert reply["result"]["peer"] == "inst_far_away"
    assert reply["result"]["at"]


def test_the_ping_echoes_the_caller_the_transport_proved_not_a_param():
    """"Is my credential still the one you know me by" is a real question and no
    client-side check can answer it. It comes off the CALLER, so a params key
    naming another install changes nothing."""

    reply = serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": "p1",
            "method": "peer.ping",
            "params": {"peer": "inst_somebody_else"},
        },
        serve_rpc.RpcContext(caller=PEER, transport=TRANSPORT_GATEWAY),
    )

    assert reply["result"]["peer"] == "inst_far_away"


def test_the_echo_is_bounded_and_the_key_absent_when_nothing_was_sent():
    long = serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "p1", "method": "peer.ping", "params": {"echo": "x" * 500}},
        serve_rpc.RpcContext(caller=PEER),
    )
    empty = serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "p2", "method": "peer.ping", "params": {}},
        serve_rpc.RpcContext(caller=PEER),
    )

    assert len(long["result"]["echo"]) == 128
    assert "echo" not in empty["result"]
    # A non-string echo is dropped rather than coerced onto the wire.
    numeric = serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "p3", "method": "peer.ping", "params": {"echo": 7}},
        serve_rpc.RpcContext(caller=PEER),
    )
    assert "echo" not in numeric["result"]


def test_a_ping_from_a_non_peer_names_no_peer_rather_than_omitting_the_key():
    """The key is present either way so a client never branches on absence."""

    reply = serve_rpc.handle_request(
        {"jsonrpc": "2.0", "id": "p1", "method": "peer.ping", "params": {}},
        serve_rpc.RpcContext(caller=STDIO_OWNER),
    )

    assert reply["result"]["peer"] is None


def test_the_method_joined_the_manifest_without_moving_the_contract_integer():
    """A set plus an integer — ``runtime.persona.prewarm``'s argument and
    ``runtime.chat.*``'s. A client only calls what it FOUND in the set."""

    manifest = serve_rpc.manifest()

    assert "peer.ping" in manifest["methods"]
    assert manifest["tiers"]["peer.ping"] == TIER_READ
    assert manifest["contract"] == 1
    assert serve_rpc.RPC_CONTRACT_VERSION == 1
    assert set(manifest) == {"contract", "methods", "tiers"}


def test_the_peer_prefix_is_the_declaration_that_it_touches_no_level():
    """``runtime.*`` verbs act on this install's level; ``peer.*`` verbs are
    about the EDGE. A client can tell them apart without a table, which matters
    most for the surface an operator on another machine is asked to trust."""

    peer_named = [n for n in serve_rpc.method_names() if n.startswith("peer.")]
    assert peer_named == sorted(PEER_METHOD_ALLOWLIST)
    assert all(
        name.startswith("runtime.")
        for name in serve_rpc.method_names()
        if not name.startswith("peer.")
    )
