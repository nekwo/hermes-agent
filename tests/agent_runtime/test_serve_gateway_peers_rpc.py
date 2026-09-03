"""The serve's peer-directory door for its own launcher (S2d, launcher S3-R13).

**Why this lane exists at all**, because the code is only sensible next to the
measurement: the launcher's hermes stream carries no events. Its hydrate core is
``agents, boards, offices, persona_instances, running_work, …`` and its fold
entities are ``persona_instance, incident, office_*, scope``; hermes reads
``event_log.tail(20)`` only for parity warnings. So the five ``gateway.peer.*``
contracts S2c registers reach a stream consumer, a snapshot and an operator, and
reach a launcher NEVER — no matter how many are emitted. Canon 03 invariant 6
routes new server→client push over JSON-RPC notifications, which is this.

Three claims are load-bearing here:

1. **One write, one notification.** Subscribe, then record / revoke / announce,
   and exactly one ``changed`` frame arrives per write, carrying the whole row.
2. **No secret ever crosses.** Every payload is scanned against the verifier and
   the raw secret, not merely against the word "verifier".
3. **A non-console caller is refused**, at every one of the three surfaces —
   including a paired CONSOLE device, which is the case worth spelling: the
   directory is the operator's map of their own network, and a console phone
   holds a real credential and no business with it.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.call_authorization import (
    LOCAL_CONSOLE_METHODS,
    TIER_CONSOLE,
    TIER_READ,
    RpcCaller,
    authorize_call,
)
from agent_runtime.gateway_peers import (
    apply_peer_announce,
    record_peer,
    revoke_peer,
)
from agent_runtime.serve_gateway_peers_rpc import (
    PEER_DIRECTORY_CHANGED_METHOD,
    PEER_DIRECTORY_CONTRACT,
    PEER_DIRECTORY_SUBSCRIPTIONS,
    PeerDirectorySubscriptions,
    peer_directory_rows,
)

PEER_A = "inst_aaaaaaaaaaaa"
PEER_B = "inst_bbbbbbbbbbbb"

METHODS = (
    "runtime.gateway.peers.subscribe",
    "runtime.gateway.peers.list",
    "runtime.gateway.peers.roster",
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A peer store with one live edge, and every surface pointed at it."""

    record_peer(
        tmp_path,
        peer_install_id=PEER_A,
        secret="f" * 64,
        display_name="mac",
        endpoints=[{"host": "10.0.0.4", "port": 9000}],
        cert_fingerprint="ab" * 32,
    )
    monkeypatch.setattr(
        "agent_runtime.gateway_targets.peer_store_root", lambda: tmp_path
    )
    return tmp_path


@pytest.fixture(autouse=True)
def clean_subscribers():
    """The registry is process-global; a leaked subscriber would make the next
    test's "exactly one notification" assertion read someone else's frames."""

    for key in list(PEER_DIRECTORY_SUBSCRIPTIONS._sinks):  # noqa: SLF001
        PEER_DIRECTORY_SUBSCRIPTIONS.release(key)
    yield
    for key in list(PEER_DIRECTORY_SUBSCRIPTIONS._sinks):  # noqa: SLF001
        PEER_DIRECTORY_SUBSCRIPTIONS.release(key)


class _Sink:
    def __init__(self) -> None:
        self.frames: list[dict] = []

    def __call__(self, frame: dict) -> None:
        self.frames.append(frame)

    @property
    def notifications(self) -> list[dict]:
        return [
            frame
            for frame in self.frames
            if frame.get("method") == PEER_DIRECTORY_CHANGED_METHOD
        ]


def _call(method: str, params: dict | None = None, *, sink=None, caller=None):
    from agent_runtime import serve_rpc

    context = serve_rpc.RpcContext(
        connection_key="conn-1",
        emit=sink,
        caller=caller if caller is not None else serve_rpc.STDIO_OWNER,
    )
    return serve_rpc._METHODS[method]("r-1", params or {}, context)


# ── the row ──────────────────────────────────────────────────────────────────


def test_the_row_is_the_cli_verbs_row_and_carries_no_credential(store):
    """The launcher greets by running ``harness gateway peers list --json``, so
    a push lane whose rows differed would make every consumer branch on which
    door a row came through."""

    from agent_runtime.gateway_peers import peer_store_path

    rows = peer_directory_rows(store)

    assert len(rows) == 1
    row = rows[0]
    assert row["peer_install_id"] == PEER_A
    assert row["usable"] is True
    assert row["ref"] == "mac"
    assert row["unusable_reason"] is None
    assert row["expires_at"] is None
    assert "cache" in row

    stored = json.loads(peer_store_path(store).read_bytes().decode())
    verifier = stored["peers"][PEER_A]["secret_verifier"]
    rendered = json.dumps(rows)
    assert verifier not in rendered
    assert "secret_verifier" not in rendered
    assert "f" * 64 not in rendered


def test_unusable_rows_are_kept_and_say_why(store):
    """A directory that hid them would make "never paired" and "thrown out" the
    same answer, and the second is the one an operator auditing a
    decommissioned machine needs. The words are the RESOLVER's, so one condition
    has one spelling everywhere."""

    record_peer(store, peer_install_id=PEER_B, secret="e" * 64, display_name="cut")
    apply_peer_announce(store, PEER_B, {"revoked_you": True})
    revoke_peer(store, PEER_A)

    rows = {row["peer_install_id"]: row for row in peer_directory_rows(store)}

    assert rows[PEER_A]["usable"] is False
    assert rows[PEER_A]["unusable_reason"] == "peer_revoked"
    assert rows[PEER_B]["usable"] is False
    assert rows[PEER_B]["unusable_reason"] == "peer_revoked_you"


# ── subscribe ────────────────────────────────────────────────────────────────


def test_subscribe_answers_the_directory_and_registers_in_one_call(store):
    """One call and not two, for ``runtime.office.subscribe``'s reason: a read
    followed by a separate join is two reads of one truth with a window between
    them, and nothing tells the client whether anything moved inside it."""

    sink = _Sink()
    reply = _call("runtime.gateway.peers.subscribe", sink=sink)

    result = reply["result"]
    assert result["contract"] == PEER_DIRECTORY_CONTRACT
    assert result["subscribed"] is True
    assert result["count"] == 1
    assert result["peers"][0]["peer_install_id"] == PEER_A
    assert len(result["store_revision"]) == 2
    assert PEER_DIRECTORY_SUBSCRIPTIONS.subscriber_count() == 1


def test_a_caller_with_no_push_channel_is_refused_rather_than_registered(store):
    """A subscription that can never deliver is a promise the runtime cannot
    keep, and the honest moment to say so is the call that asked for it."""

    reply = _call("runtime.gateway.peers.subscribe", sink=None)

    assert reply["error"]["data"]["reason"] == "no_push_channel"
    assert PEER_DIRECTORY_SUBSCRIPTIONS.subscriber_count() == 0
    # …and the read-only door is named in the refusal, so a caller in that state
    # has somewhere to go.
    assert "runtime.gateway.peers.list" in reply["error"]["message"]


def test_list_answers_the_same_body_without_subscribing(store):
    reply = _call("runtime.gateway.peers.list")

    assert reply["result"]["count"] == 1
    assert "subscribed" not in reply["result"]
    assert PEER_DIRECTORY_SUBSCRIPTIONS.subscriber_count() == 0


# ── the notification ─────────────────────────────────────────────────────────


def test_subscribe_then_a_write_produces_exactly_one_changed_frame(store):
    """One write, one notification, carrying the WHOLE row — so a client
    re-renders from the payload and never fetches, and a dropped frame costs one
    row's freshness rather than a resync."""

    sink = _Sink()
    _call("runtime.gateway.peers.subscribe", sink=sink)
    assert sink.notifications == []

    record_peer(store, peer_install_id=PEER_B, secret="e" * 64, display_name="studio")

    assert len(sink.notifications) == 1
    params = sink.notifications[0]["params"]
    assert params["contract"] == PEER_DIRECTORY_CONTRACT
    assert params["event"] == "gateway.peer.recorded"
    assert params["peer_install_id"] == PEER_B
    assert params["peer"]["display_name"] == "studio"
    assert params["peer"]["usable"] is True
    assert len(params["store_revision"]) == 2


@pytest.mark.parametrize(
    "write, event",
    [
        (lambda root: revoke_peer(root, PEER_A), "gateway.peer.revoked"),
        (
            lambda root: apply_peer_announce(root, PEER_A, {"display_name": "renamed"}),
            "gateway.peer.updated",
        ),
    ],
)
def test_every_store_door_reaches_the_launcher(store, write, event):
    """The fan-out is wired into ``_emit_peer_event`` — the same call site the
    EventLog append happens at — so the two lanes cannot disagree about WHEN
    something changed. A second trigger (a watcher, a poll) would be a second
    opinion about one fact, and they would drift on exactly the writes that
    matter."""

    sink = _Sink()
    _call("runtime.gateway.peers.subscribe", sink=sink)

    write(store)

    assert [frame["params"]["event"] for frame in sink.notifications] == [event]
    assert sink.notifications[0]["params"]["peer_install_id"] == PEER_A


def test_no_notification_payload_carries_a_secret(store):
    sink = _Sink()
    _call("runtime.gateway.peers.subscribe", sink=sink)

    record_peer(store, peer_install_id=PEER_B, secret="e" * 64, display_name="studio")
    apply_peer_announce(store, PEER_B, {"cert_fingerprint": "cd" * 32})
    revoke_peer(store, PEER_B)

    rendered = json.dumps(sink.notifications)
    assert "secret_verifier" not in rendered
    assert "e" * 64 not in rendered
    assert "f" * 64 not in rendered


def test_a_released_subscriber_stops_receiving(store):
    """The teardown the disconnect path calls. A subscriber that outlived its
    connection would be a sink written to forever, which is how a fan-out starts
    holding a dead socket open."""

    sink = _Sink()
    _call("runtime.gateway.peers.subscribe", sink=sink)
    PEER_DIRECTORY_SUBSCRIPTIONS.release("conn-1")

    record_peer(store, peer_install_id=PEER_B, secret="e" * 64, display_name="studio")

    assert sink.notifications == []


def test_a_sink_that_raises_is_dropped_and_never_fails_the_write(store):
    """The opposite posture from ``RpcContext.push``, deliberately: a push raised
    inside a handler is reportable on the call that tried it, while a fan-out has
    no call to report on — and a raising sink would take down a store write that
    had already succeeded."""

    registry = PeerDirectorySubscriptions()

    def _explode(_frame):
        raise RuntimeError("socket gone")

    registry.register("dead", _explode)
    assert registry.publish({"event": "x"}) == 0
    assert registry.subscriber_count() == 0


def test_a_runtime_with_no_subscriber_pays_nothing(store, monkeypatch):
    """Checked before any store read, so a serve with no launcher attached does
    not open two files on every peer write."""

    from agent_runtime import serve_gateway_peers_rpc

    monkeypatch.setattr(
        "agent_runtime.gateway_peers.list_peers",
        lambda root: (_ for _ in ()).throw(AssertionError("read the store")),
    )

    serve_gateway_peers_rpc.publish_peer_event(
        "gateway.peer.updated", {"peer_install_id": PEER_A}
    )


# ── the fetch-through ────────────────────────────────────────────────────────


def test_roster_fetches_through_as_a_peer_and_caches_what_came_back(store, monkeypatch):
    """The launcher cannot call ``peer.roster.list`` itself: it is a PEER method
    and a launcher holds a DEVICE credential. So it asks its own hermes — which
    IS a peer of that install — and the answer lands in the cache, where the
    next ``changed`` frame carries it. One directory, two readers."""

    from agent_runtime.gateway_peers import read_peer_cache

    calls = []

    def _call_peer(root, install_id, method, params, **kwargs):
        calls.append((install_id, method))
        return {
            "result": {
                "workspace_id": "ws-1",
                "truncated": False,
                "rows": [{"handle": "personainst_dev", "persona_id": "dev"}],
            }
        }

    monkeypatch.setattr("tools.agent_chat_remote.call_peer_method", _call_peer)

    reply = _call("runtime.gateway.peers.roster", {"install": "mac"})

    assert calls == [(PEER_A, "peer.roster.list")]
    assert reply["result"]["install"]["install_id"] == PEER_A
    assert reply["result"]["count"] == 1
    assert read_peer_cache(store)[PEER_A].roster["workspace_id"] == "ws-1"


def test_the_roster_fetch_notifies_subscribers_with_the_cached_roster(
    store, monkeypatch
):
    """Cached BEFORE the reply, so the notification and the reply describe the
    same roster: a client that got the reply first and the notification second
    would briefly render two answers."""

    monkeypatch.setattr(
        "tools.agent_chat_remote.call_peer_method",
        lambda *a, **k: {
            "result": {"workspace_id": "ws-1", "rows": [{"handle": "personainst_dev"}]}
        },
    )
    sink = _Sink()
    _call("runtime.gateway.peers.subscribe", sink=sink)

    _call("runtime.gateway.peers.roster", {"install": "mac"})

    assert [frame["params"]["event"] for frame in sink.notifications] == [
        "gateway.peer.roster"
    ]
    row = sink.notifications[0]["params"]["peer"]
    assert row["cache"]["roster"]["rows"] == [{"handle": "personainst_dev"}]


def test_a_far_refusal_travels_rather_than_answering_with_an_empty_roster(
    store, monkeypatch
):
    """An empty list over an unmade call is the most misleading answer
    available — ``agent_chat_open``'s reason, applied here."""

    monkeypatch.setattr(
        "tools.agent_chat_remote.call_peer_method",
        lambda *a, **k: {
            "refusal": {"reason": "capability_missing", "message": "no such method"}
        },
    )

    reply = _call("runtime.gateway.peers.roster", {"install": "mac"})

    assert reply["error"]["data"]["reason"] == "capability_missing"
    assert reply["error"]["data"]["install_id"] == PEER_A


def test_the_roster_verb_refuses_an_unknown_or_unusable_install(store):
    revoke_peer(store, PEER_A)

    unknown = _call("runtime.gateway.peers.roster", {"install": "nowhere"})
    assert unknown["error"]["data"]["reason"] in {
        "unknown_peer_install",
        "peer_revoked",
    }

    missing = _call("runtime.gateway.peers.roster", {})
    assert missing["error"]["code"] == -32602


# ── the KIND gate ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", METHODS)
def test_a_non_console_caller_is_refused_at_every_surface(method):
    """A paired CONSOLE device is the case worth spelling: it holds a real
    credential of the right STRENGTH and is refused anyway, because the peer
    directory is the operator's own map of their network — which machines they
    paired, what those are called, the addresses they answer at.

    The tier vocabulary cannot say that (two words, both about strength), which
    is the gap ``LOCAL_CONSOLE_METHODS`` was added for.
    """

    from agent_runtime import serve_rpc

    assert method in LOCAL_CONSOLE_METHODS

    tier = serve_rpc.method_tier(method)
    for caller in (
        RpcCaller(kind="device", device_id="dev_1", device_tier=TIER_CONSOLE),
        RpcCaller(kind="device", device_id="dev_2", device_tier=TIER_READ),
        RpcCaller(kind="peer", peer_install_id=PEER_A),
        RpcCaller(kind="unknown"),
    ):
        verdict = authorize_call(tier, caller, method=method)
        assert verdict.ok is False, (method, caller.kind)
        assert verdict.reason == "scope_denied"
        assert "own console" in (verdict.detail or "")


@pytest.mark.parametrize("method", METHODS)
def test_this_installs_own_console_is_admitted(method):
    from agent_runtime import serve_rpc
    from agent_runtime.call_authorization import LOCAL_CONSOLE, STDIO_OWNER

    tier = serve_rpc.method_tier(method)
    assert authorize_call(tier, STDIO_OWNER, method=method).ok is True
    assert authorize_call(tier, LOCAL_CONSOLE, method=method).ok is True


def test_the_two_read_surfaces_declare_read_and_the_dialling_one_declares_console():
    """The tier is the honest answer to STRENGTH and the set is the answer to
    KIND, and they say different things: reading two files wants nothing
    special, while opening a socket to another machine and spending this
    install's peer credential wants a level-mutation-strength caller."""

    from agent_runtime import serve_rpc

    assert serve_rpc.method_tier("runtime.gateway.peers.subscribe") == TIER_READ
    assert serve_rpc.method_tier("runtime.gateway.peers.list") == TIER_READ
    assert serve_rpc.method_tier("runtime.gateway.peers.roster") == TIER_CONSOLE


def test_the_three_methods_are_published_on_the_manifest():
    """A launcher feature-detects on ``rpcManifest.methods``: an older hermes
    lacks these names and the launcher renders "live peer updates need a newer
    Hermes" rather than polling (R-IP16 — a missing capability is a row state,
    never a refusal)."""

    from agent_runtime import serve_rpc

    published = set(serve_rpc.manifest()["methods"])
    assert set(METHODS) <= published
    tiers = serve_rpc.manifest()["tiers"]
    assert tiers["runtime.gateway.peers.roster"] == TIER_CONSOLE
