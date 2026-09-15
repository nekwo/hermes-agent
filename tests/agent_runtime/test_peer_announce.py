"""``peer.announce`` — the only WRITING verb on the peer surface (S2c, R-S2-9).

It exists because the alternative to being told is polling: before it, a
rename, a moved address, a rotated certificate and a revocation on the far side
were each discovered as the NEXT call's failure, by an agent that had already
written the message.

Everything in this file is about the boundary that makes a write from another
machine acceptable at all. Three properties, and each is asserted as a property
of the CODE rather than as a rule somebody follows:

1. the row written is the caller's, and no parameter can say otherwise;
2. nothing it writes is consulted by a credential path — in particular an
   announced fingerprint is a NOTICE beside the pin, never the pin;
3. ``revoked_you`` is one-way: an announce may set it and only a trust write
   clears it, so no install can announce itself back into an edge that was cut.

The hostile-payload arm is the one worth reading twice. It sends every field an
attacker would try — another install's id, a fingerprint that is not the pin, an
un-revoke — and asserts ``peers.json`` is byte-identical afterwards.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.gateway_peers import (
    PeerCacheRow,
    apply_peer_announce,
    peer_store_path,
    read_peer_cache,
    record_peer,
    revoke_peer,
)

PEER_A = "inst_aaaaaaaaaaaa"
PEER_B = "inst_bbbbbbbbbbbb"


@pytest.fixture
def paired(tmp_path):
    """One edge, written by a trust ceremony, with a pinned fingerprint."""

    record_peer(
        tmp_path,
        peer_install_id=PEER_A,
        secret="f" * 64,
        display_name="workstation",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        cert_fingerprint="ab" * 32,
    )
    return tmp_path


# ── the store function ───────────────────────────────────────────────────────


def test_announce_writes_cache_only_and_reports_the_fields_it_wrote(paired):
    """``cache_written`` is what a caller reads to tell an
    accepted-and-applied from an accepted-and-dropped without a second round
    trip."""

    before = peer_store_path(paired).read_bytes()

    written = apply_peer_announce(
        paired,
        PEER_A,
        {"display_name": "the laptop", "endpoints": [{"host": "10.0.0.9", "port": 8765}]},
    )

    assert set(written) == {"announced_display_name", "endpoints"}
    cached = read_peer_cache(paired)[PEER_A]
    assert cached.announced_display_name == "the laptop"
    assert cached.endpoints == ({"host": "10.0.0.9", "port": 8765},)
    assert cached.last_announce_at
    assert peer_store_path(paired).read_bytes() == before


def test_an_announce_cannot_un_revoke_or_rename_the_trust_row(paired):
    """The hostile payload, in one call. Every field an attacker would reach for
    — the trust row's own name, its revocation, its verifier — and afterwards
    ``peers.json`` is byte-identical. Not "the values are unchanged": the BYTES
    are, because a cache writer opening that file at all is the thing this test
    exists to catch."""

    revoke_peer(paired, PEER_A)
    before = peer_store_path(paired).read_bytes()

    apply_peer_announce(
        paired,
        PEER_A,
        {
            "display_name": "renamed",
            "revoked": False,
            "revoked_at": None,
            "secret_verifier": "0" * 64,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "approved_at": "1999-01-01T00:00:00+00:00",
        },
    )

    assert peer_store_path(paired).read_bytes() == before
    stored = json.loads(before.decode())["peers"][PEER_A]
    assert stored["revoked"] is True
    assert stored["display_name"] == "workstation"


def test_an_announce_may_write_only_the_callers_own_row(paired):
    """The store half of the caller-only rule: the id is POSITIONAL, so there is
    no payload key that could redirect the write. A hostile ``peer_install_id``
    in the body is data the function never reads — the handler refuses it out
    loud (below), and even if it did not, it could not land anywhere."""

    record_peer(paired, peer_install_id=PEER_B, secret="e" * 64, display_name="other")

    apply_peer_announce(
        paired,
        PEER_A,
        {"peer_install_id": PEER_B, "install_id": PEER_B, "display_name": "hijacked"},
    )

    cache = read_peer_cache(paired)
    assert cache[PEER_A].announced_display_name == "hijacked"
    assert PEER_B not in cache


def test_revoked_you_is_one_way_until_a_trust_write(paired):
    """An install that could announce itself back in would have granted itself
    access an operator refused. Only a trust write — a re-pair — clears it."""

    apply_peer_announce(paired, PEER_A, {"revoked_you": True})
    assert read_peer_cache(paired)[PEER_A].revoked_you is True
    assert read_peer_cache(paired)[PEER_A].revoked_you_at

    apply_peer_announce(paired, PEER_A, {"revoked_you": False})
    assert read_peer_cache(paired)[PEER_A].revoked_you is True

    # The trust write that DOES clear it: re-pairing writes the credential
    # afresh, which is the same authority that could revoke, and the cache row's
    # flag is cleared with it. That exit is a property of the CALL GRAPH — only
    # ``record_peer`` and ``redeem_peer_code`` reach ``_clear_revoked_you`` — so
    # this line is asserting the ceremony, not a helper.
    record_peer(
        paired,
        peer_install_id=PEER_A,
        secret="f" * 64,
        display_name="workstation",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        cert_fingerprint="ab" * 32,
    )
    assert read_peer_cache(paired)[PEER_A].revoked_you is False
    assert read_peer_cache(paired)[PEER_A].revoked_you_at is None


def test_a_fingerprint_rotation_is_recorded_and_never_applied_to_the_pin(paired):
    """The single most important line in the file. A peer that could nominate
    the certificate it is checked against could become a different machine —
    which is the one thing pinning exists to prevent — so a disagreeing
    fingerprint becomes a NOTICE an operator reads and the pin does not move."""

    from agent_runtime.gateway_peers import lookup_peer

    apply_peer_announce(paired, PEER_A, {"cert_fingerprint": "cd" * 32})

    cached = read_peer_cache(paired)[PEER_A]
    assert cached.cert_fingerprint == "cd" * 32
    assert cached.fingerprint_rotation["new_fingerprint"] == "cd" * 32
    assert cached.fingerprint_rotation["announced_at"]
    # The PIN, untouched.
    assert lookup_peer(paired, PEER_A).cert_fingerprint == "ab" * 32


def test_an_announced_fingerprint_that_agrees_is_not_a_rotation(paired):
    """A notice for a value that did not change would be a rotation alert an
    operator learns to ignore, which is the same as having none."""

    apply_peer_announce(paired, PEER_A, {"cert_fingerprint": "ab" * 32})

    assert read_peer_cache(paired)[PEER_A].fingerprint_rotation is None


def test_roster_changed_drops_the_cached_roster_and_fetches_nothing(paired):
    """This edge carries a NOTIFICATION, never a roster. A handler that answered
    an inbound announce by dialling back would make one push edge into a loop
    with two installs in it; the next read fetches, when somebody wants it."""

    from agent_runtime.gateway_peers import cache_peer_roster

    cache_peer_roster(paired, PEER_A, workspace_id="ws-1", rows=[{"handle": "dev"}])
    assert read_peer_cache(paired)[PEER_A].roster["rows"] == [{"handle": "dev"}]

    written = apply_peer_announce(paired, PEER_A, {"roster_changed": True})

    assert "roster" in written
    assert read_peer_cache(paired)[PEER_A].roster is None


def test_an_empty_announce_is_a_liveness_stamp_and_is_accepted(paired):
    written = apply_peer_announce(paired, PEER_A, {})

    assert written == []
    assert read_peer_cache(paired)[PEER_A].last_announce_at


# ── the handler ──────────────────────────────────────────────────────────────


def _call(params, *, caller_peer=PEER_A):
    from agent_runtime import serve_rpc
    from agent_runtime.call_authorization import RpcCaller

    caller = (
        None
        if caller_peer is None
        else RpcCaller(kind="peer", peer_install_id=caller_peer)
    )
    context = serve_rpc.RpcContext(caller=caller)
    return serve_rpc._METHODS["peer.announce"]("a-1", params, context)


def test_announce_refuses_a_non_peer(tmp_path, monkeypatch):
    """A legitimate local console client is refused too, and that is the case
    worth spelling: an announce from "some console" would be a fact about a peer
    with no peer behind it."""

    from agent_runtime.serve_rpc import PEER_CHAT_NOT_A_PEER_REASON

    monkeypatch.setattr(
        "agent_runtime.gateway_targets.peer_store_root", lambda: tmp_path
    )
    reply = _call({"display_name": "x"}, caller_peer=None)

    assert reply["error"]["data"]["reason"] == PEER_CHAT_NOT_A_PEER_REASON


def test_announce_refuses_a_payload_naming_another_install_and_ignores_its_own(
    paired, monkeypatch
):
    """Refused rather than ignored: a caller doing this is either confused or
    probing, and both deserve to be told. A caller echoing its OWN id is
    harmless and is accepted."""

    from agent_runtime.serve_rpc import PEER_ANNOUNCE_NAMES_OTHER_REASON

    monkeypatch.setattr(
        "agent_runtime.gateway_targets.peer_store_root", lambda: paired
    )

    refused = _call({"peer_install_id": PEER_B, "display_name": "hijack"})
    assert refused["error"]["data"]["reason"] == PEER_ANNOUNCE_NAMES_OTHER_REASON
    assert read_peer_cache(paired) == {}

    echoed = _call({"peer_install_id": PEER_A, "display_name": "fine"})
    assert echoed["result"]["accepted"] is True
    assert read_peer_cache(paired)[PEER_A].announced_display_name == "fine"


def test_the_result_names_the_caller_and_the_fields_that_landed(paired, monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.gateway_targets.peer_store_root", lambda: paired
    )

    reply = _call({"display_name": "the laptop", "correlation_id": "g-1"})

    assert reply["result"] == {
        "accepted": True,
        "contract": 1,
        "peer": PEER_A,
        "cache_written": ["announced_display_name"],
        "correlation_id": "g-1",
    }


def test_an_unfit_correlation_token_is_refused_rather_than_repaired(paired, monkeypatch):
    from agent_runtime.serve_rpc import CORRELATION_ID_INVALID_REASON

    monkeypatch.setattr(
        "agent_runtime.gateway_targets.peer_store_root", lambda: paired
    )

    reply = _call({"correlation_id": "not a token"})

    assert reply["error"]["data"]["reason"] == CORRELATION_ID_INVALID_REASON


# ── the predicate the announce feeds ─────────────────────────────────────────


def test_revoked_you_makes_the_next_send_refuse_deterministically_before_any_dial(
    paired, monkeypatch
):
    """The whole point of the push edge, end to end.

    Before it, a revoke on the far side was indistinguishable from that install
    being down: the send was written, the dial was attempted, and the refusal
    arrived after the work. Now the resolver refuses with its own reason before
    a socket exists — proved with a client that raises if it is constructed.
    """

    from agent_runtime import serve_socket
    from agent_runtime.gateway_targets import (
        REASON_PEER_REVOKED_YOU,
        TargetRefusal,
        parse_install_target,
        resolve_install_target,
    )

    parsed = parse_install_target("@workstation/dev")
    assert resolve_install_target(paired, parsed).install_id == PEER_A

    apply_peer_announce(paired, PEER_A, {"revoked_you": True})

    monkeypatch.setattr(
        serve_socket,
        "ServeSocketClient",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("resolution dialled a peer that revoked us")
        ),
    )

    refusal = resolve_install_target(paired, parsed)
    assert isinstance(refusal, TargetRefusal)
    assert refusal.reason == REASON_PEER_REVOKED_YOU
    # The operator is told WHICH machine's decision this was, because no amount
    # of work at this one will fix it.
    assert "revoked this install" in refusal.message


def test_the_cache_row_defaults_are_safe_for_a_peer_nothing_has_said_anything_about():
    """``unknown`` reachability and no revocation: a row that defaulted to
    ``reachable`` would be a dial an operator was promised, and one that
    defaulted to ``revoked_you`` would hide an edge that works."""

    row = PeerCacheRow(PEER_A)

    assert row.reachability == "unknown"
    assert row.revoked_you is False
    assert row.roster is None
