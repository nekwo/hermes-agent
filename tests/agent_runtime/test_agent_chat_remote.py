"""``call_peer_method`` — one JSON-RPC call to a paired install (S2b, R-S2-12).

Four callers share this: two read tools, the directory tool, and the outbound
announce. One implementation, because the only cross-install client before it
was ``agent_chat_dispatch``'s — a supervisor built around a chat turn's retry
posture, its durable row and its delivery promise, none of which applies to a
read that has to answer inside a model's turn.

Everything here is about the two guarantees a TOOL's client owes its caller: it
never raises past a typed refusal (a traceback in a tool is a turn that ends in
an unhandled exception where "that machine did not answer" would have let the
agent do something else), and it closes the connection on every path.
"""

from __future__ import annotations

import pytest

from tools.agent_chat_remote import (
    CAPABILITY_MISSING_REASON,
    PEER_READ_DIAL_TIMEOUT_SECONDS,
    PEER_READ_REPLY_TIMEOUT_SECONDS,
    PEER_UNREACHABLE_REASON,
    call_peer_method,
)


class _Connection:
    """A dialled peer that answers with a scripted sequence of frames."""

    def __init__(self, frames) -> None:
        self.frames = list(frames)
        self.sent: list[dict] = []
        self.closed = False

    def send(self, frame):
        self.sent.append(frame)

    def read_frame(self):
        return self.frames.pop(0) if self.frames else None

    def close(self):
        self.closed = True


def _dial(monkeypatch, connection):
    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda root, peer, timeout_seconds=None: (connection, {"event": "hello_ok"}),
    )
    return connection


def test_the_timeouts_are_the_in_turn_budget_and_not_the_dispatch_lanes():
    """A dispatch has somewhere to wait — a durable row and a background
    supervisor. These reads run INSIDE a model's turn and have nowhere, so the
    budget is a LAN round trip on a machine that is awake and short enough that
    an install which is switched off costs a sentence rather than a stall."""

    assert PEER_READ_DIAL_TIMEOUT_SECONDS == 5.0
    assert PEER_READ_REPLY_TIMEOUT_SECONDS == 10.0

    from agent_runtime.gateway_peers import dial_peer  # noqa: F401 - the sibling
    from tools.agent_chat_dispatch import PEER_DIAL_TIMEOUT_SECONDS

    assert PEER_READ_DIAL_TIMEOUT_SECONDS < PEER_DIAL_TIMEOUT_SECONDS


def test_a_result_comes_back_as_a_result_and_the_connection_is_closed(monkeypatch):
    connection = _dial(
        monkeypatch, _Connection([{"id": "peer-peer.ping-1", "result": {"pong": True}}])
    )

    outcome = call_peer_method("/root", "inst_far", "peer.ping", {"echo": "x"})

    assert outcome == {"result": {"pong": True}}
    assert connection.closed is True
    assert connection.sent[0]["method"] == "peer.ping"
    assert connection.sent[0]["params"] == {"echo": "x"}


def test_frames_that_are_not_our_reply_are_skipped(monkeypatch):
    """Stream frames and notifications ride the same socket. Skipping anything
    that is not OUR reply is what lets this be one call rather than a
    subscription with a filter."""

    connection = _dial(
        monkeypatch,
        _Connection(
            [
                {"event": "state"},
                {"id": "somebody-else", "result": {"nope": True}},
                {"id": "peer-peer.ping-1", "result": {"pong": True}},
            ]
        ),
    )

    assert call_peer_method("/root", "inst_far", "peer.ping") == {
        "result": {"pong": True}
    }
    assert connection.closed is True


def test_a_dead_port_is_a_typed_transport_refusal_within_the_dial_timeout(monkeypatch):
    """``dial_peer`` raises ``ConnectionError`` for every not-reachable
    condition — no endpoint answered, a revoked row, an expired credential — and
    each is a state a caller RENDERS rather than a fault it reports."""

    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("nothing answered")),
    )

    outcome = call_peer_method("/root", "inst_far", "peer.ping")

    assert outcome["refusal"]["reason"] == PEER_UNREACHABLE_REASON
    assert "nothing answered" in outcome["refusal"]["message"]


def test_a_far_error_travels_verbatim_with_its_own_reason(monkeypatch):
    """The far side's ``data`` block travels intact: a caller that had to learn
    a second refusal vocabulary for the same question would be branching on
    which hop answered."""

    connection = _dial(
        monkeypatch,
        _Connection(
            [
                {
                    "id": "peer-peer.thread.read-1",
                    "error": {
                        "code": -32000,
                        "message": "peer.thread.read refused",
                        "data": {"reason": "foreign_session", "error_kind": "foreign_session"},
                    },
                }
            ]
        ),
    )

    outcome = call_peer_method("/root", "inst_far", "peer.thread.read", {"target": "dev"})

    assert outcome["refusal"]["reason"] == "foreign_session"
    assert outcome["refusal"]["data"]["error_kind"] == "foreign_session"
    assert connection.closed is True


def test_a_far_method_not_found_is_capability_missing_and_not_unreachable(monkeypatch):
    """R-IP16, and the one mapping this module makes. An install that ANSWERS
    and does not know the verb is a build older than this one — a row state —
    and calling it "unreachable" would send an operator looking at a network
    that is fine."""

    _dial(
        monkeypatch,
        _Connection(
            [
                {
                    "id": "peer-peer.roster.list-1",
                    "error": {"code": -32601, "message": "no such method"},
                }
            ]
        ),
    )

    outcome = call_peer_method("/root", "inst_far", "peer.roster.list")

    assert outcome["refusal"]["reason"] == CAPABILITY_MISSING_REASON


def test_a_peer_that_closes_before_answering_is_a_transport_refusal(monkeypatch):
    connection = _dial(monkeypatch, _Connection([]))

    outcome = call_peer_method("/root", "inst_far", "peer.ping")

    assert outcome["refusal"]["reason"] == PEER_UNREACHABLE_REASON
    assert "closed the connection" in outcome["refusal"]["message"]
    assert connection.closed is True


def test_a_reply_that_never_arrives_is_bounded_by_the_reply_timeout(monkeypatch):
    """A read that could block forever is a turn that could block forever."""

    class _Silent(_Connection):
        def read_frame(self):
            return {"event": "noise"}

    connection = _dial(monkeypatch, _Silent([]))

    outcome = call_peer_method(
        "/root", "inst_far", "peer.ping", reply_timeout=0.05
    )

    assert outcome["refusal"]["reason"] == PEER_UNREACHABLE_REASON
    assert "did not answer" in outcome["refusal"]["message"]
    assert connection.closed is True


def test_an_exception_inside_the_read_loop_still_closes_the_connection(monkeypatch):
    """The ``finally`` is the guarantee, and it is asserted rather than
    trusted: a connection leaked on an error path is a socket held for the life
    of a serve."""

    class _Broken(_Connection):
        def read_frame(self):
            raise RuntimeError("socket exploded")

    connection = _dial(monkeypatch, _Broken([]))

    outcome = call_peer_method("/root", "inst_far", "peer.ping")

    assert outcome["refusal"]["reason"] == PEER_UNREACHABLE_REASON
    assert connection.closed is True


def test_a_non_dict_result_answers_with_an_empty_dict_rather_than_the_raw_value(
    monkeypatch,
):
    """Every caller reads the result as a mapping. A list or a string arriving
    where a dict is expected would be a ``TypeError`` three frames later, in a
    tool, in front of a model."""

    _dial(monkeypatch, _Connection([{"id": "peer-peer.ping-1", "result": ["nope"]}]))

    assert call_peer_method("/root", "inst_far", "peer.ping") == {"result": {}}
