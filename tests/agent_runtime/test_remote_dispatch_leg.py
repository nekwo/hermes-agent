"""Gateway Stage 7 — the sender's half: an install-qualified dispatch.

Two things are pinned here and they are the whole stage on this side of the
wire.

**Deterministic refusals cost nothing.** An unknown install, a revoked edge, an
ambiguous name and a malformed qualifier are all pure functions of THIS
install's peer store, so they are answered before a row exists — never after
eight identical dials. That is ``dispatch_delivery``'s own fail-fast rule
applied one lane over, and the assertion that carries it is that no dispatch row
was written at all.

**A transport failure is not a refusal.** An install that is off is retried to
R8's cap and then settled with a terminal answer the sender is TOLD, carrying
``peer_unreachable``. The retry is safe only because the ``turn_request_id`` is
derived from the dispatch id rather than minted per attempt, so a second attempt
against an install that already accepted the turn replays rather than re-runs —
which is asserted directly.

The far install is a fake connection object here, deliberately. What is under
test is the LEG: which failures retry, which settle, what lands on the row. The
real wire is proven end to end by ``test_gateway_peer_cross_install_chat_e2e``,
which runs two serve children.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import dispatch_delivery, dispatch_store
from agent_runtime.dispatch_store import (
    MAX_DELIVERY_ATTEMPTS,
    REMOTE_UNREACHABLE_REASON,
    STATE_COMPLETED,
    STATE_ERROR,
    STATE_RUNNING,
    get_dispatch,
    list_dispatches,
    record_dispatch,
)
from agent_runtime.gateway_peers import record_peer, revoke_peer
from tools import agent_chat_dispatch
from tools.agent_chat_tool import agent_chat_send

SENDER_ROOT = "persona_chat_personainst_neko_aaaaaaaaaaaa"


@pytest.fixture
def store_home(tmp_path, monkeypatch):
    home = tmp_path / "bg-home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_HEAD_HOME", str(home))
    return home


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    """A peer store of this install's own, reached the way production reaches it.

    The env layer wins in ``resolve_runtime``, so pinning
    ``HERMES_AGENT_RUNTIME_ROOT`` makes ``gateway_targets.peer_store_root()``
    answer here — and exercises that resolver rather than stubbing it.
    """

    root = tmp_path / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    return root


@pytest.fixture
def deliverable_lane(monkeypatch):
    from gateway.session_context import (
        _SESSION_ASYNC_DELIVERY,
        declare_async_delivery_channel,
    )

    token = _SESSION_ASYNC_DELIVERY.set(_SESSION_ASYNC_DELIVERY.get())
    declare_async_delivery_channel()
    monkeypatch.setattr(
        dispatch_delivery,
        "_sender_persona",
        lambda root: ("neko_supervisor", "personainst_neko") if root == SENDER_ROOT else None,
    )
    yield
    _SESSION_ASYNC_DELIVERY.reset(token)


@pytest.fixture
def queued(monkeypatch):
    calls: list[dict] = []

    def fake_queue(*, dispatch_id, spec, max_concurrent):
        calls.append({"dispatch_id": dispatch_id, "spec": spec})

    monkeypatch.setattr(agent_chat_dispatch, "dispatch_detached_turn", fake_queue)
    return calls


def _pair(root, *, install_id: str, name: str) -> None:
    record_peer(
        root,
        peer_install_id=install_id,
        secret="a" * 64,
        display_name=name,
        endpoints=[{"host": "127.0.0.1", "port": 9000}],
    )


def _send(**overrides):
    payload = {
        "persona_id": "@workstation/dev",
        "message": "Run the launcher suite on your machine and report failures.",
        "wait": False,
        "requested_by_session": SENDER_ROOT,
    }
    payload.update(overrides)
    return json.loads(agent_chat_send(**payload))


# ── the send: deterministic refusals cost no row ─────────────────────────────


def test_an_unknown_install_refuses_and_writes_no_row(
    store_home, runtime_root, deliverable_lane, queued
):
    result = _send()

    assert result["ok"] is False
    assert result["error_kind"] == "unknown_peer_install"
    assert queued == []
    assert list_dispatches(limit=10) == []


def test_a_revoked_edge_refuses_with_its_own_reason(
    store_home, runtime_root, deliverable_lane, queued
):
    _pair(runtime_root, install_id="install-b", name="workstation")
    revoke_peer(runtime_root, "install-b")

    result = _send()

    assert result["error_kind"] == "peer_revoked"
    assert list_dispatches(limit=10) == []


def test_two_installs_with_one_name_refuse_and_hand_back_both_ids(
    store_home, runtime_root, deliverable_lane, queued
):
    _pair(runtime_root, install_id="install-b", name="workstation")
    _pair(runtime_root, install_id="install-c", name="workstation")

    result = _send()

    assert result["error_kind"] == "ambiguous_peer_install"
    # The candidates ride the refusal so the agent can retry against an exact
    # id instead of guessing which machine it meant.
    assert result["candidates"] == ["install-b", "install-c"]


def test_a_malformed_qualifier_refuses_rather_than_addressing_this_machine(
    store_home, runtime_root, deliverable_lane, queued
):
    result = _send(persona_id="@/dev")

    assert result["error_kind"] == "install_qualifier_empty"
    assert queued == []


def test_a_cross_install_send_requires_the_detached_lane(
    store_home, runtime_root, deliverable_lane, queued
):
    _pair(runtime_root, install_id="install-b", name="workstation")

    result = _send(wait=True)

    assert result["error_kind"] == "remote_requires_detached"
    assert "wait=false" in result["error"]
    assert list_dispatches(limit=10) == []


def test_an_unqualified_target_is_untouched_by_any_of_this(
    store_home, runtime_root, deliverable_lane, queued
):
    """The compatibility assertion. A bare persona never reaches the peer
    resolver, so a machine with no peers at all still dispatches locally."""

    result = _send(persona_id="dev")

    assert result["ok"] is True
    assert result["target_persona"] == "dev"
    assert queued[0]["spec"].get("remote_install_id") is None
    assert get_dispatch(result["dispatch_id"])["remote_install_id"] == ""


# ── the send: a real cross-install row ───────────────────────────────────────


def test_the_row_lives_on_the_sender_and_names_the_install(
    store_home, runtime_root, deliverable_lane, queued
):
    _pair(runtime_root, install_id="install-b", name="workstation")

    result = _send()
    row = get_dispatch(result["dispatch_id"])

    assert result["ok"] is True
    assert row["state"] == STATE_RUNNING
    assert row["sender_session_id"] == SENDER_ROOT
    # The id is the machine-readable half; the spelling is what an operator
    # reads in Activity.
    assert row["remote_install_id"] == "install-b"
    assert row["target_persona"] == "@workstation/dev"
    # No local instance handle: the string after the `/` is B's vocabulary.
    assert row["target_instance_id"] == ""


def test_the_spec_carries_the_id_not_the_name(
    store_home, runtime_root, deliverable_lane, queued
):
    _pair(runtime_root, install_id="install-b", name="workstation")
    _send()

    spec = queued[0]["spec"]
    assert spec["remote_install_id"] == "install-b"
    assert spec["remote_display_name"] == "workstation"
    assert spec["remote_target"] == "dev"


def test_an_install_id_addresses_a_machine_whose_name_is_ambiguous(
    store_home, runtime_root, deliverable_lane, queued
):
    _pair(runtime_root, install_id="install-b", name="workstation")
    _pair(runtime_root, install_id="install-c", name="workstation")

    result = _send(persona_id="@install-c/dev")

    assert result["ok"] is True
    assert get_dispatch(result["dispatch_id"])["remote_install_id"] == "install-c"


# ── the params on the wire ───────────────────────────────────────────────────


def test_the_turn_request_id_is_derived_from_the_dispatch_and_the_chain_stays_home():
    spec = {
        "client_message_id": "agent-dispatch-dispatch-abc",
        "remote_target": "dev",
        "message": "go",
        "max_seconds": 300.0,
        "title": "suite",
        "relay_chain": ["neko_supervisor", "alice"],
        "requested_by": "agent:root-1",
        "clarify_token": "tok",
    }
    params = agent_chat_dispatch.build_peer_execute_params("dispatch-abc", spec)

    assert params["turn_request_id"] == "agent-dispatch-dispatch-abc"
    assert params["target"] == "dev"
    assert params["title"] == "suite"
    # Not forwarded, each for its own stated reason: a chain B cannot verify is
    # a cycle guard that can be talked past; a clarify token is a ticket in THIS
    # install's store; provenance is B's to decide from the connection.
    assert "relay_chain" not in params
    assert "clarify_token" not in params
    assert "requested_by" not in params


# ── the leg: which failures retry and which settle ───────────────────────────


class _FakeConnection:
    """One dialled edge. Answers exactly the frames it was constructed with."""

    def __init__(self, frames: list[dict]):
        self._frames = list(frames)
        self.sent: list[dict] = []
        self.timeouts: list[float] = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def read_frame(self):
        return self._frames.pop(0) if self._frames else None

    def set_timeout(self, seconds):
        self.timeouts.append(seconds)

    def close(self):
        self.closed = True


def _ack(rid: str, request_id: str = "chat-1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "result": {"accepted": True, "request_id": request_id, "peer": "install-a"},
    }


def _turn_frames(request_id: str, payload: dict, code: int = 0) -> list[dict]:
    """One turn's frames as serve actually emits them.

    The event name comes from the module under test rather than being spelled
    here, and that is not tidiness — it is the correction this file needed. It
    said ``"stdout"`` originally, the production reader said ``"stdout"``, and
    the two agreed with each other and with nothing else: serve builds its out
    proxy as ``_LineFrameProxy(frames, "line")``. Both were green until the
    two-roots acceptance ran a real one. A fake that spells the wire itself is a
    fake that can be wrong in the same direction as the code it tests.
    """

    return [
        {"id": request_id, "event": agent_chat_dispatch.SERVE_STDOUT_EVENT, "line": line}
        for line in json.dumps(payload, indent=2).split("\n")
    ] + [{"id": request_id, "event": "exit", "code": code}]


def test_the_stdout_event_name_is_taken_from_serve_rather_than_guessed():
    """The fence under the correction above: one grep against the ONE line that
    decides the name, so a rename in serve reds here instead of silently
    emptying every remote payload."""

    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "hermes_cli" / "harness_parts" / "serve.py"
    ).read_text(encoding="utf-8")

    assert (
        f'_LineFrameProxy(frames, "{agent_chat_dispatch.SERVE_STDOUT_EVENT}")' in source
    )
    # …and the error stream really is the one named after itself, which is what
    # makes the out stream's name surprising in the first place.
    assert '_LineFrameProxy(frames, "stderr")' in source


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(agent_chat_dispatch.time, "sleep", lambda _s: None)


@pytest.fixture
def remote_row(store_home):
    dispatch_id = "dispatch-remote1"
    record_dispatch(
        dispatch_id=dispatch_id,
        sender_session_id=SENDER_ROOT,
        target_persona="@workstation/dev",
        ask="go",
        remote_install_id="install-b",
    )
    return dispatch_id


def _spec(dispatch_id: str) -> dict:
    return {
        "client_message_id": f"agent-dispatch-{dispatch_id}",
        "remote_install_id": "install-b",
        "remote_display_name": "workstation",
        "remote_target": "dev",
        "message": "go",
        "max_seconds": 60.0,
    }


def test_an_offline_install_converges_after_the_cap_and_the_sender_is_told(
    remote_row, no_sleep, monkeypatch
):
    dials = []

    def dead_dial(root, install_id, *, timeout_seconds):
        dials.append(timeout_seconds)
        raise ConnectionError(f"no endpoint on the {install_id!r} row answered")

    monkeypatch.setattr("agent_runtime.gateway_peers.dial_peer", dead_dial)

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    assert len(dials) == MAX_DELIVERY_ATTEMPTS
    # R8's "bounded per-attempt dial timeout", spent per attempt.
    assert set(dials) == {agent_chat_dispatch.PEER_DIAL_TIMEOUT_SECONDS}

    row = get_dispatch(remote_row)
    # NOT `dropped`: the sender is owed this fact, and a dropped row is
    # indistinguishable from a dispatch that evaporated.
    assert row["state"] == STATE_ERROR
    assert row["delivery_state"] == dispatch_store.DELIVERY_PENDING
    assert REMOTE_UNREACHABLE_REASON in row["result"]["error"]
    assert row["result"]["remote"] == {
        "install_id": "install-b",
        "attempts": MAX_DELIVERY_ATTEMPTS,
        "reason": REMOTE_UNREACHABLE_REASON,
    }


def test_a_refusal_from_the_far_install_settles_on_the_first_attempt(
    remote_row, no_sleep, monkeypatch
):
    """Deterministic: an unknown persona on B is refused for the same reason on
    attempt eight as on attempt one, so burning the cap buys nothing and ends
    with the operator reading the wrong verdict."""

    connections = []

    def dial(root, install_id, *, timeout_seconds):
        rid = f"peer-exec-{remote_row}"
        connection = _FakeConnection(
            [
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {
                        "code": -32000,
                        "message": "no persona 'dev' on this install",
                        "data": {"reason": "unknown_persona"},
                    },
                }
            ]
        )
        connections.append(connection)
        return connection, {"event": "hello_ok"}

    monkeypatch.setattr("agent_runtime.gateway_peers.dial_peer", dial)

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    assert len(connections) == 1
    row = get_dispatch(remote_row)
    assert row["state"] == STATE_ERROR
    assert row["result"]["remote"]["attempts"] == 1
    assert row["result"]["remote"]["reason"] == "unknown_persona"
    assert "workstation refused the request" in row["result"]["error"]


def test_a_completed_remote_turn_lands_on_the_row_like_a_local_one(
    remote_row, no_sleep, monkeypatch
):
    rid = f"peer-exec-{remote_row}"
    payload = {
        "capability_id": "mission_chat_message",
        "ok": True,
        "reply": "suite is green, 4102 passed",
        "session_id": "root-on-b",
        "total_tokens": 1234,
    }
    connection = _FakeConnection([_ack(rid)] + _turn_frames("chat-1", payload))

    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda root, install_id, *, timeout_seconds: (connection, {"event": "hello_ok"}),
    )

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    row = get_dispatch(remote_row)
    assert row["state"] == STATE_COMPLETED
    assert row["result"]["reply"] == "suite is green, 4102 passed"
    assert row["result"]["target_session_id"] == "root-on-b"
    assert row["result"]["remote"] == {"install_id": "install-b", "attempts": 1}
    # The dial timeout bounded the DIAL; the turn read was re-armed to the
    # sender's own budget.
    assert connection.timeouts == [60.0 + agent_chat_dispatch.KILL_GRACE_SECONDS]
    assert connection.closed is True
    # And the request that went out carried the derived id.
    assert connection.sent[0]["method"] == "peer.agent_chat.execute"
    assert connection.sent[0]["params"]["turn_request_id"] == f"agent-dispatch-{remote_row}"


def test_an_edge_that_dies_mid_turn_retries_with_the_same_request_id(
    remote_row, no_sleep, monkeypatch
):
    """The property that makes R8's retry safe at all: B's reservation and turn
    journal are keyed on ``turn_request_id``, so a second attempt against an
    install that already accepted the turn REPLAYS instead of running the agent
    a second time."""

    rid = f"peer-exec-{remote_row}"
    payload = {"capability_id": "mission_chat_message", "ok": True, "reply": "done"}
    dead = _FakeConnection([_ack(rid)])  # ack, then the socket ends
    alive = _FakeConnection([_ack(rid)] + _turn_frames("chat-1", payload))
    dialled = [dead, alive]

    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda root, install_id, *, timeout_seconds: (dialled.pop(0), {"event": "hello_ok"}),
    )

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    row = get_dispatch(remote_row)
    assert row["state"] == STATE_COMPLETED
    assert row["result"]["remote"]["attempts"] == 2
    assert (
        dead.sent[0]["params"]["turn_request_id"]
        == alive.sent[0]["params"]["turn_request_id"]
    )


def test_a_settled_replay_terminates_instead_of_waiting_for_frames_that_are_gone(
    remote_row, no_sleep, monkeypatch
):
    """The retry posture's SUCCESS path, and the shape of it is not obvious.

    B's per-request frames go to the sink of the connection that ASKED. So when
    the same ``turn_request_id`` comes back on a new socket, B replays its ack
    and emits nothing — a reader that waited for an exit frame would sit until
    its own timeout for a turn that finished somewhere else. (This is not a
    hypothetical: the Stage 7 acceptance hung on exactly it before this arm
    existed.) The row settles from the receipt, and says plainly that the answer
    is in the thread on the other install."""

    rid = f"peer-exec-{remote_row}"
    connection = _FakeConnection(
        [
            {
                "jsonrpc": "2.0",
                "id": rid,
                "result": {
                    "accepted": True,
                    "request_id": "chat-1",
                    "idempotent_replay": True,
                    "settled": True,
                    "exit_code": 0,
                },
            }
        ]
    )
    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda root, install_id, *, timeout_seconds: (connection, {"event": "hello_ok"}),
    )

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    row = get_dispatch(remote_row)
    assert row["state"] == STATE_COMPLETED
    assert row["result"]["remote"]["reason"] == "peer_turn_replayed"
    assert "in the thread on that install" in row["result"]["reply"]
    # It never armed the long read, because it never intended to read.
    assert connection.timeouts == []


def test_an_unsettled_replay_means_it_is_still_running_over_there_and_retries(
    remote_row, no_sleep, monkeypatch
):
    running = {
        "jsonrpc": "2.0",
        "id": f"peer-exec-{remote_row}",
        "result": {
            "accepted": True,
            "request_id": "chat-1",
            "idempotent_replay": True,
            "settled": False,
        },
    }
    dialled = [_FakeConnection([running]) for _ in range(MAX_DELIVERY_ATTEMPTS)]
    seen = list(dialled)

    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda root, install_id, *, timeout_seconds: (dialled.pop(0), {"event": "hello_ok"}),
    )

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    assert dialled == []
    assert all(connection.closed for connection in seen)
    row = get_dispatch(remote_row)
    # It converged rather than hanging, and the reason names the CAP rather than
    # pretending the install was unreachable — it answered every time.
    assert row["state"] == STATE_ERROR
    assert row["result"]["remote"]["reason"] == REMOTE_UNREACHABLE_REASON
    assert "still running on workstation" in row["result"]["error"]


def test_a_turn_with_no_payload_is_unknown_rather_than_an_empty_reply(
    remote_row, no_sleep, monkeypatch
):
    rid = f"peer-exec-{remote_row}"
    connection = _FakeConnection(
        [_ack(rid), {"id": "chat-1", "event": "exit", "code": 3}]
    )
    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda root, install_id, *, timeout_seconds: (connection, {"event": "hello_ok"}),
    )

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    row = get_dispatch(remote_row)
    assert row["state"] == dispatch_store.STATE_UNKNOWN
    assert "without a reply payload" in row["result"]["error"]


def test_frames_for_another_request_on_the_same_socket_are_ignored(
    remote_row, no_sleep, monkeypatch
):
    rid = f"peer-exec-{remote_row}"
    payload = {"capability_id": "mission_chat_message", "ok": True, "reply": "ours"}
    frames = [
        _ack(rid),
        {
            "id": "chat-someone-else",
            "event": agent_chat_dispatch.SERVE_STDOUT_EVENT,
            "line": '{"capability_id": "x", "ok": false}',
        },
        *_turn_frames("chat-1", payload),
    ]
    connection = _FakeConnection(frames)
    monkeypatch.setattr(
        "agent_runtime.gateway_peers.dial_peer",
        lambda root, install_id, *, timeout_seconds: (connection, {"event": "hello_ok"}),
    )

    agent_chat_dispatch._run_remote_dispatch(remote_row, _spec(remote_row))

    assert get_dispatch(remote_row)["result"]["reply"] == "ours"


def test_a_local_spec_never_takes_the_remote_leg(monkeypatch):
    """The fork is on one key, and a local dispatch must not read it by
    accident."""

    taken = []
    monkeypatch.setattr(
        agent_chat_dispatch,
        "_run_remote_dispatch",
        lambda *args: taken.append("remote"),
    )
    monkeypatch.setattr(
        agent_chat_dispatch.subprocess,
        "Popen",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("spawned")),
    )

    from agent_runtime import dispatch_store as store

    record_dispatch(
        dispatch_id="dispatch-local1",
        sender_session_id=SENDER_ROOT,
        target_persona="dev",
        ask="go",
    )
    agent_chat_dispatch._run_dispatch_guarded(
        "dispatch-local1",
        {"persona_id": "dev", "message": "go", "max_seconds": 5.0},
    )

    assert taken == []
    assert get_dispatch("dispatch-local1")["state"] == store.STATE_ERROR
