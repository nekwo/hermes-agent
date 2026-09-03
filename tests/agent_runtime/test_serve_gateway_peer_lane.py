"""Stage 6: the PEER hello, end to end over a real encrypted socket.

The same discipline as ``test_serve_gateway_lane.py`` and for the same reason:
the REAL ``serve_loop``, real sockets, a real TLS handshake, a real certificate
pin, a real HMAC over a real per-install credential, against the isolated
runtime root the autouse fixture provides. No transport is faked, because the
things worth pinning here are exactly the ones a fake transport cannot fail.

Two claims are load-bearing.

1. **Device-tier and peer-tier credentials are never interchangeable**, on the
   WIRE and in both directions — a device credential presented on a peer hello,
   a peer credential presented on a device hello, a device code presented as a
   peer code, and a frame naming both. The store suite proves the layer beneath;
   this proves the frame, so neither is the only thing holding the rule up.
2. **A peer inherits every refusal the gateway door already carried and gains
   one of its own.** The argv lane and `drain` were refused to devices in Stage
   1 by keying on the LANE rather than on the device stamp, so a peer is refused
   both for free — that is asserted rather than assumed, because "for free" is a
   claim about code nobody re-read. The new one is the allowlist: a peer calling
   `runtime.agent.retire` is refused `scope_denied` over the wire.

ONE serve here. The two-isolated-roots acceptance — both CLI verbs, two real
serve children, the full ceremony — is
``test_gateway_peer_two_roots_e2e.py``; what this file adds is the frame-level
coverage that would be slow and hard to read through two subprocesses.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from contextlib import contextmanager

import pytest

from agent_runtime.call_authorization import TIER_CONSOLE, TIER_READ
from agent_runtime.gateway_identity import ensure_install_identity
from agent_runtime.gateway_peers import (
    PeerCredential,
    PeerPairingCode,
    PeerRecord,
    lookup_peer,
    mint_peer_code,
    peer_secret_verifier,
    record_peer,
    redeem_peer_code,
    revoke_peer,
)
from agent_runtime.serve_gateway_auth import (
    DeviceCredential,
    PairingCode,
    device_proof,
    mint_pairing_code,
    redeem_pairing_code,
)
from agent_runtime.serve_socket import ServeSocketClient
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 20.0
#: The install id the tests dial in AS — a stand-in for the other machine's
#: Stage 0 identity, which is all this side ever knows about it.
FAR_INSTALL = "inst_far_side_0001"


# ── the harness (test_serve_gateway_lane's, with a peer client on top) ───────


class _StdioPipe:
    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        item = self._queue.get()
        if item is None:
            raise StopIteration
        return item

    def close(self) -> None:
        self._queue.put(None)


class _Sink:
    def __init__(self) -> None:
        self._parts: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def frames(self) -> list[dict]:
        with self._lock:
            text = "".join(self._parts)
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def wait_for(self, event: str, timeout: float = WAIT) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for frame in self.frames():
                if frame.get("event") == event:
                    return frame
            time.sleep(0.01)
        raise AssertionError(f"no {event!r} frame within {timeout}s")


class _RunningServe:
    def __init__(self, pipe, sink, thread) -> None:
        self.pipe = pipe
        self.sink = sink
        self.thread = thread
        self.ready: dict = {}

    @property
    def gateway_port(self) -> int:
        return int(self.ready["gateway"]["port"])

    @property
    def fingerprint(self) -> str:
        return str(self.ready["gateway"]["cert_fingerprint"])


@contextmanager
def running_serve(**kwargs):
    pipe = _StdioPipe()
    sink = _Sink()
    handle = _RunningServe(pipe, sink, None)

    def _run() -> None:
        serve_loop(
            pipe,
            sink,
            socket_lane=True,
            dispatch=kwargs.pop("dispatch", lambda argv: 0),
            liveness_pump_interval_seconds=60.0,
            **kwargs,
        )

    thread = threading.Thread(target=_run, name="serve-peer-under-test", daemon=True)
    handle.thread = thread
    thread.start()
    try:
        handle.ready = sink.wait_for("ready")
        yield handle
    finally:
        pipe.close()
        thread.join(WAIT)


@pytest.fixture(autouse=True)
def gateway_on(monkeypatch):
    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("127.0.0.1", 0)
    )


def _store_root():
    from agent_runtime import paths

    return paths.store_root()


def pair_peer(*, peer_install_id: str = FAR_INSTALL, **kwargs) -> PeerCredential:
    """The store half of a completed ceremony, so the wire half has a row to use."""

    root = _store_root()
    code = mint_peer_code(root, note="the other machine")
    assert isinstance(code, PeerPairingCode)
    credential = redeem_peer_code(
        root, code.code, peer_install_id=peer_install_id, **kwargs
    )
    assert isinstance(credential, PeerCredential), credential
    return credential


def pair_device(*, tier: str = TIER_CONSOLE) -> DeviceCredential:
    root = _store_root()
    code = mint_pairing_code(root, tier=tier, name="a phone")
    assert isinstance(code, PairingCode)
    credential = redeem_pairing_code(root, code.code)
    assert isinstance(credential, DeviceCredential)
    return credential


def _client(handle: _RunningServe, *, pin=True) -> ServeSocketClient:
    connection = ServeSocketClient(
        "127.0.0.1",
        handle.gateway_port,
        timeout_seconds=WAIT,
        tls=True,
        cert_fingerprint=handle.fingerprint if pin else None,
    )
    connection.connect()
    return connection


@contextmanager
def peer_client(handle: _RunningServe, credential: PeerCredential):
    connection = _client(handle)
    try:
        reply = connection.peer_hello(
            peer_install_id=credential.peer_install_id,
            verifier=peer_secret_verifier(credential.secret),
            client="hermes-peer",
        )
        yield connection, reply
    finally:
        connection.close()


def _rpc(connection: ServeSocketClient, method: str, params: dict | None = None) -> dict:
    connection.send(
        {"jsonrpc": "2.0", "id": f"r-{method}", "method": method, "params": params or {}}
    )
    for _ in range(50):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before a reply to {method}")
        if frame.get("id") == f"r-{method}":
            return frame
    raise AssertionError(f"no reply to {method}")


# ── the peer gets in ─────────────────────────────────────────────────────────


def test_a_paired_install_completes_the_handshake_over_tls_with_a_pinned_cert():
    credential = pair_peer()

    with running_serve() as handle:
        with peer_client(handle, credential) as (_connection, reply):
            assert reply["event"] == "hello_ok"
            # The door it came through, stated. A peer reading "socket" here
            # would be told it is on the local lane.
            assert reply["transport"] == "gateway"
            # Which install it just reached — the fact a `runtime_root` path
            # cannot give a caller on another machine.
            assert reply["install"]["install_id"]
            # The refusals it inherits from the door, advertised by MEMBERSHIP
            # rather than discovered by trying.
            assert "drain" not in reply["ops"]["ops"]
            assert "shutdown" not in reply["ops"]["ops"]
            assert reply["ops"]["transport"] == "gateway"
            # It reads the same tier map every other client reads.
            assert reply["rpc"]["tiers"]["peer.ping"] == TIER_READ
            assert reply["rpc"]["tiers"]["runtime.agent.retire"] == TIER_CONSOLE
            # A credential handshake carries NO minted secret: those ride the
            # ceremony's one frame and never a later one.
            assert "peered" not in reply
            assert "paired" not in reply


def test_peer_ping_answers_over_the_gateway_listener_with_the_peer_credential():
    """The stage's acceptance verb, on the lane it exists for."""

    credential = pair_peer()

    with running_serve() as handle:
        with peer_client(handle, credential) as (connection, _reply):
            answer = _rpc(connection, "peer.ping", {"echo": "are-you-there"})

    assert answer["result"]["pong"] is True
    assert answer["result"]["echo"] == "are-you-there"
    # The install id the TRANSPORT proved, echoed back — so the dialer learns it
    # is still recognised as the install it means to be.
    assert answer["result"]["peer"] == FAR_INSTALL


def test_the_last_seen_stamp_lands_on_the_cache_after_a_handshake():
    """S2c: the stamp still lands on every verified hello — in
    ``peers_cache.json``, and the credential store is not touched at all."""

    from agent_runtime.gateway_peers import (
        REACHABILITY_REACHABLE,
        peer_store_path,
        read_peer_cache,
    )

    credential = pair_peer()
    assert read_peer_cache(_store_root()) == {}
    before = peer_store_path(_store_root()).read_bytes()

    with running_serve() as handle:
        with peer_client(handle, credential) as (_c, reply):
            assert reply["event"] == "hello_ok"

    cached = read_peer_cache(_store_root())[FAR_INSTALL]
    assert cached.last_seen
    assert cached.reachability == REACHABILITY_REACHABLE
    assert peer_store_path(_store_root()).read_bytes() == before


# ── the exclusion, over the wire ─────────────────────────────────────────────


def test_a_peer_calling_runtime_agent_retire_is_refused_scope_denied():
    """The canon's exclusion at the door an agent on another install would
    actually reach. The iterated version — every registered verb against the
    allowlist — is ``test_peer_authorization.py``; this is the one that proves
    the refusal survives the whole transport."""

    credential = pair_peer()

    with running_serve() as handle:
        with peer_client(handle, credential) as (connection, _reply):
            answer = _rpc(connection, "runtime.agent.retire", {})

    data = answer["error"]["data"]
    assert data["reason"] == "scope_denied"
    assert data["tier"] == TIER_CONSOLE
    assert data["caller"] == "peer"


def test_a_peer_is_refused_a_read_verb_a_device_may_call():
    """The asymmetry that makes the allowlist worth having, on the wire: the
    same verb, two remote callers, two answers."""

    peer = pair_peer()
    device = pair_device(tier=TIER_READ)

    with running_serve() as handle:
        with peer_client(handle, peer) as (connection, _reply):
            refused = _rpc(connection, "runtime.office.get", {"workspace_id": "default"})
        allowed_connection = _client(handle)
        try:
            allowed_connection.device_hello(
                device_id=device.device_id, token=device.token, client="phone"
            )
            allowed = _rpc(
                allowed_connection, "runtime.office.get", {"workspace_id": "default"}
            )
        finally:
            allowed_connection.close()

    assert refused["error"]["data"]["reason"] == "scope_denied"
    assert allowed.get("error", {}).get("data", {}).get("reason") != "scope_denied"


def test_the_argv_lane_is_unreachable_from_a_peer():
    """Stage 1 keyed this refusal on the LANE rather than the device stamp, so a
    peer inherits it for free. "For free" is a claim about code nobody re-read,
    so it is asserted — and it is what makes "agents can never mint peers" hold
    against a remote caller, since the peer verbs are CLI verbs."""

    credential = pair_peer()

    with running_serve() as handle:
        with peer_client(handle, credential) as (connection, _reply):
            connection.send(
                {"id": "a-1", "argv": ["harness", "gateway", "peers", "list"]}
            )
            frame = connection.read_frame()

    assert frame["event"] == "error"
    assert frame["error"] == "argv_lane_unavailable"


def test_a_peer_may_not_drain_the_runtime_other_clients_are_using():
    credential = pair_peer()

    with running_serve() as handle:
        with peer_client(handle, credential) as (connection, _reply):
            connection.send({"op": "drain", "force": True})
            frame = connection.read_frame()
        assert handle.thread.is_alive()

    assert frame["error"] == "op_not_available_on_gateway"


# ── the two credentials do not trade doors ───────────────────────────────────


def test_a_device_credential_is_refused_on_the_peer_hello():
    """A device token, a device's own proof derivation, presented under the
    field that names an install. The prefix split (`gwv` vs `pwv`) is what makes
    this fail even before the store lookup does."""

    device = pair_device()
    pair_peer()

    with running_serve() as handle:
        connection = _client(handle)
        try:
            greeting = connection.read_frame()
            connection.send(
                {
                    "op": "hello",
                    "client": "impostor",
                    "peer_install_id": device.device_id,
                    "proof": device_proof(
                        device.token,
                        greeting["nonce"],
                        port=handle.gateway_port,
                        device_id=device.device_id,
                    ),
                }
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


def test_a_peer_credential_is_refused_on_the_device_hello():
    """The other direction. A peer holding a real, live credential cannot spend
    it as a device and pick up a device's tier."""

    credential = pair_peer()

    with running_serve() as handle:
        connection = _client(handle)
        try:
            reply = connection.device_hello(
                device_id=FAR_INSTALL,
                token=credential.secret,
                client="impostor",
            )
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


def test_a_hello_naming_both_a_device_and_a_peer_is_refused():
    """A handshake with two credentials in it is where a downgrade lives, and
    the counting rule refuses rather than picking a winner."""

    device = pair_device()
    credential = pair_peer()

    with running_serve() as handle:
        connection = _client(handle)
        try:
            greeting = connection.read_frame()
            connection.send(
                {
                    "op": "hello",
                    "client": "confused",
                    "device_id": device.device_id,
                    "peer_install_id": FAR_INSTALL,
                    "proof": device_proof(
                        device.token,
                        greeting["nonce"],
                        port=handle.gateway_port,
                        device_id=device.device_id,
                    ),
                }
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"
    assert credential.secret  # never sent


def test_a_device_pairing_code_is_refused_as_a_peer_code_and_the_reverse():
    """The ceremonies do not trade codes either, and the refusal is the same
    reason as every other credential failure — so a caller cannot learn which of
    the two it just failed."""

    device_code = mint_pairing_code(_store_root())
    peer_code = mint_peer_code(_store_root())
    assert isinstance(device_code, PairingCode)
    assert isinstance(peer_code, PeerPairingCode)

    with running_serve() as handle:
        as_peer = _client(handle)
        try:
            wrong_way = as_peer.peer_join_hello(
                peer_code=device_code.code, peer_install_id=FAR_INSTALL
            )
        finally:
            as_peer.close()
        as_device = _client(handle)
        try:
            other_way = as_device.pair_hello(
                pairing_code=peer_code.code, client="phone"
            )
        finally:
            as_device.close()

    assert wrong_way["reason"] == other_way["reason"] == "bad_proof"
    assert wrong_way["event"] == other_way["event"] == "hello_rejected"


def test_a_revoked_peer_is_refused_at_the_hello():
    credential = pair_peer()
    assert isinstance(revoke_peer(_store_root(), FAR_INSTALL), PeerRecord)

    with running_serve() as handle:
        connection = _client(handle)
        try:
            reply = connection.peer_hello(
                peer_install_id=FAR_INSTALL,
                verifier=peer_secret_verifier(credential.secret),
                client="hermes-peer",
            )
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    # Collapsed into ONE reason with every other credential failure, so a caller
    # cannot map which installs are paired by watching it change.
    assert reply["reason"] == "bad_proof"


def test_an_unpaired_install_and_a_wrong_secret_are_indistinguishable():
    credential = pair_peer()

    with running_serve() as handle:
        replies = []
        for install_id, verifier in (
            ("inst_never_paired", peer_secret_verifier(credential.secret)),
            (FAR_INSTALL, "0" * 64),
        ):
            connection = _client(handle)
            try:
                replies.append(
                    connection.peer_hello(
                        peer_install_id=install_id,
                        verifier=verifier,
                        client="hermes-peer",
                    )
                )
            finally:
                connection.close()

    assert [reply["reason"] for reply in replies] == ["bad_proof", "bad_proof"]
    assert replies[0] == replies[1]


def test_a_peer_proof_minted_for_the_loopback_port_does_not_verify_here():
    """The port binding, across two listeners on one machine: a proof minted for
    one port is wrong at the other even with the right credential."""

    credential = pair_peer()

    with running_serve() as handle:
        from agent_runtime.gateway_peers import peer_proof

        connection = _client(handle)
        try:
            greeting = connection.read_frame()
            connection.send(
                {
                    "op": "hello",
                    "client": "hermes-peer",
                    "peer_install_id": FAR_INSTALL,
                    "proof": peer_proof(
                        peer_secret_verifier(credential.secret),
                        greeting["nonce"],
                        # the LOOPBACK port, not the one it dialled
                        port=int(handle.ready["socket"]["port"]),
                        peer_install_id=FAR_INSTALL,
                    ),
                }
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


def test_the_root_token_is_not_a_credential_on_the_peer_field_either():
    from agent_runtime.serve_auth import read_token

    with running_serve() as handle:
        connection = _client(handle)
        try:
            greeting = connection.read_frame()
            from agent_runtime.serve_socket import hello_proof

            connection.send(
                {
                    "op": "hello",
                    "client": "impostor",
                    "peer_install_id": FAR_INSTALL,
                    "proof": hello_proof(
                        read_token(_store_root()) or "",
                        greeting["nonce"],
                        port=handle.gateway_port,
                    ),
                }
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


# ── the ceremony's second half, over the wire ────────────────────────────────


def test_a_peer_code_becomes_an_edge_in_one_round_trip():
    """The join, which is what makes the peer lane enterable at all — and the
    facts the joining install asserts about itself land on the row, bounded."""

    code = mint_peer_code(_store_root(), note="the laptop")
    assert isinstance(code, PeerPairingCode)

    with running_serve() as handle:
        connection = _client(handle)
        try:
            reply = connection.peer_join_hello(
                peer_code=code.code,
                peer_install_id=FAR_INSTALL,
                display_name="the laptop",
                endpoints=[{"host": "10.0.0.9", "port": 8765}],
                cert_fingerprint="ab" * 32,
            )
        finally:
            connection.close()

        assert reply["event"] == "hello_ok"
        peered = reply["peered"]
        assert peered["peer_install_id"] == FAR_INSTALL
        assert len(peered["peer_secret"]) == 64
        # The block that names WHICH install was just paired with is the
        # greeting's own `install`, not a second copy inside `peered`.
        assert reply["install"]["install_id"]

        record = lookup_peer(_store_root(), FAR_INSTALL)
        assert record.display_name == "the laptop"
        assert record.endpoints == ({"host": "10.0.0.9", "port": 8765},)
        assert record.cert_fingerprint == "ab" * 32

        # …and the credential works on the NEXT connection, which is the only
        # thing that proves the secret is real rather than decorative.
        credential = PeerCredential(
            peer_install_id=FAR_INSTALL,
            secret=peered["peer_secret"],
            display_name="the laptop",
        )
        with peer_client(handle, credential) as (_c, second):
            assert second["event"] == "hello_ok"
            # The secret rides exactly ONE frame. A second handshake carries no
            # `peered` block because the slot is read-and-cleared.
            assert "peered" not in second


def test_a_peer_code_is_one_shot_and_the_second_attempt_is_refused():
    code = mint_peer_code(_store_root())
    assert isinstance(code, PeerPairingCode)

    with running_serve() as handle:
        replies = []
        for install_id in (FAR_INSTALL, "inst_second_comer"):
            connection = _client(handle)
            try:
                replies.append(
                    connection.peer_join_hello(
                        peer_code=code.code, peer_install_id=install_id
                    )
                )
            finally:
                connection.close()

    assert replies[0]["event"] == "hello_ok"
    assert replies[1]["event"] == "hello_rejected"
    assert replies[1]["reason"] == "bad_proof"
    assert lookup_peer(_store_root(), "inst_second_comer") is None


def test_a_join_that_names_no_install_is_refused_and_writes_nothing():
    code = mint_peer_code(_store_root())
    assert isinstance(code, PeerPairingCode)

    with running_serve() as handle:
        connection = _client(handle)
        try:
            greeting = connection.read_frame()
            assert greeting["event"] == "server_hello"
            connection.send(
                {"op": "hello", "client": "nameless", "peer_code": code.code}
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    from agent_runtime.gateway_peers import list_peers

    assert list_peers(_store_root()) == []


def test_the_peer_code_is_not_a_credential_on_the_loopback_lane():
    """The doors do not trade credentials in any direction — the third pair, on
    top of Stage 1's two."""

    code = mint_peer_code(_store_root())
    assert isinstance(code, PeerPairingCode)

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1", int(handle.ready["socket"]["port"]), timeout_seconds=WAIT
        )
        connection.connect()
        try:
            connection.read_frame()
            connection.send(
                {"op": "hello", "client": "x", "peer_code": code.code,
                 "peer_install_id": FAR_INSTALL}
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


# ── the peer is visible to its operator, the credential is not ───────────────


def test_an_attached_peer_is_named_on_the_connections_block_without_a_tier():
    """"Which paired install is attached right now" is what an operator asks the
    moment a cross-install call misbehaves. No tier key beside it: a peer holds
    an allowlist, and a `peer_tier: null` would invite a reader to look for one.
    """

    credential = pair_peer()

    with running_serve() as handle:
        with peer_client(handle, credential) as (connection, _reply):
            connection.send({"op": "connections"})
            frame = connection.read_frame()

    rows = frame["gateway"]["connections"]
    assert len(rows) == 1
    assert rows[0]["peer_install_id"] == FAR_INSTALL
    assert "peer_tier" not in rows[0]
    assert "device_id" not in rows[0]
    # …and the loopback lane's rows are unchanged in shape.
    assert all("peer_install_id" not in row for row in frame["connections"])


def test_a_joined_peer_never_shows_its_secret_on_the_connections_block():
    """The one-shot slot must not survive into anything an operator, a log, or
    another attached client can read."""

    code = mint_peer_code(_store_root())
    assert isinstance(code, PeerPairingCode)

    with running_serve() as handle:
        connection = _client(handle)
        try:
            reply = connection.peer_join_hello(
                peer_code=code.code, peer_install_id=FAR_INSTALL
            )
            secret = reply["peered"]["peer_secret"]
            connection.send({"op": "connections"})
            frame = connection.read_frame()
        finally:
            connection.close()

    rendered = json.dumps(frame)
    assert secret not in rendered
    assert code.code not in rendered
    assert frame["gateway"]["connections"][0]["peer_install_id"] == FAR_INSTALL


def test_the_greeting_never_carries_a_peer_verifier():
    """Asserted against the actual stored secret rather than against a
    substring, which is the rule every greeting block in this lane follows."""

    credential = pair_peer()
    verifier = peer_secret_verifier(credential.secret)

    with running_serve() as handle:
        with peer_client(handle, credential) as (_connection, reply):
            rendered = json.dumps(reply)

    assert verifier not in rendered
    assert credential.secret not in rendered
    assert "secret_verifier" not in rendered


# ── the dialer reads the record and nothing else ─────────────────────────────


def test_dial_peer_uses_the_endpoint_on_the_row_and_pins_its_fingerprint():
    """The plan's Stage 6 risk line as code: a peer's address is a fact recorded
    at pair time, never a registry read — the registry names ports on THIS
    machine, and a cross-machine read of it is not stale but impossible.

    Dialled against this same serve, with the row pointing at it, because what
    is being proved is WHERE the dialer looked rather than which machine
    answered.
    """

    from agent_runtime.gateway_peers import dial_peer

    identity = ensure_install_identity(_store_root())

    with running_serve() as handle:
        # A row for "the other install" that in fact points back here. The
        # dialer has no way to tell, which is the point: it reads the row.
        credential = pair_peer(peer_install_id=identity.install_id)
        record_peer(
            _store_root(),
            peer_install_id=identity.install_id,
            secret=credential.secret,
            display_name="loopback stand-in",
            endpoints=[{"host": "127.0.0.1", "port": handle.gateway_port}],
            cert_fingerprint=handle.fingerprint,
        )

        connection, reply = dial_peer(_store_root(), identity.install_id)
        try:
            assert reply["event"] == "hello_ok"
            answer = _rpc(connection, "peer.ping", {})
        finally:
            connection.close()

    assert answer["result"]["pong"] is True
    assert answer["result"]["peer"] == identity.install_id


def test_dial_peer_refuses_a_revoked_or_unknown_row_before_it_opens_a_socket():
    from agent_runtime.gateway_peers import dial_peer

    pair_peer()
    revoke_peer(_store_root(), FAR_INSTALL)

    with pytest.raises(ConnectionError, match="revoked"):
        dial_peer(_store_root(), FAR_INSTALL)
    with pytest.raises(ConnectionError, match="paired"):
        dial_peer(_store_root(), "inst_never_paired")


def test_dial_peer_reports_an_unreachable_endpoint_as_a_transport_failure():
    """Unreachable is not refused — the distinction Stage 7's retry posture (R8)
    rests on, so it is pinned at the point the distinction is made."""

    from agent_runtime.gateway_peers import dial_peer

    ensure_install_identity(_store_root())
    credential = pair_peer()
    record_peer(
        _store_root(),
        peer_install_id=FAR_INSTALL,
        secret=credential.secret,
        endpoints=[{"host": "127.0.0.1", "port": 9}],
        cert_fingerprint="ab" * 32,
    )

    with pytest.raises(ConnectionError, match="answered"):
        dial_peer(_store_root(), FAR_INSTALL, timeout_seconds=2.0)


def test_dial_peer_refuses_a_root_that_cannot_name_itself_rather_than_minting_one():
    """``dial_peer`` READS this root's identity and does not ensure one, which
    is a deliberate asymmetry with the pairing verbs. Minting an install id as a
    side effect of a dial would mean the id a peer knows this install by could
    be created by an outbound call nobody thought of as a write — and an id is
    the one thing in this lane that must never be quietly re-minted, because a
    paired install names it.
    """

    from agent_runtime.gateway_peers import dial_peer

    credential = pair_peer()
    record_peer(
        _store_root(),
        peer_install_id=FAR_INSTALL,
        secret=credential.secret,
        endpoints=[{"host": "127.0.0.1", "port": 9}],
    )

    with pytest.raises(ConnectionError, match="no install identity"):
        dial_peer(_store_root(), FAR_INSTALL, timeout_seconds=2.0)


def test_the_peered_block_carries_the_expiry_when_the_code_had_a_ttl_and_null_otherwise():
    """S2 (R-IP15 as amended). ``peered.expires_at`` is ADDITIVE and it has to
    travel: the redeeming side computed the stamp and the joining side has no
    other way to learn it, so an edge whose two ends each derived their own
    would lapse at two different moments — which is the divergence ``_row``
    exists to prevent.

    ``None`` on every edge the manual ceremony mints, which is what keeps a
    joining install that predates this key reading exactly what it read before.
    """

    code = mint_peer_code(_store_root(), credential_ttl_seconds=60)
    assert isinstance(code, PeerPairingCode)

    with running_serve() as handle:
        connection = _client(handle)
        try:
            reply = connection.peer_join_hello(
                peer_code=code.code,
                peer_install_id=FAR_INSTALL,
                display_name="the laptop",
            )
        finally:
            connection.close()

        assert reply["event"] == "hello_ok"
        assert reply["peered"]["expires_at"]
        # Both ends hold the SAME value: what the frame carried is what the
        # redeeming side stored, not a second derivation of it.
        assert reply["peered"]["expires_at"] == (
            lookup_peer(_store_root(), FAR_INSTALL).expires_at
        )


def test_the_manual_ceremony_still_mints_an_edge_that_never_expires():
    code = mint_peer_code(_store_root())

    with running_serve() as handle:
        connection = _client(handle)
        try:
            reply = connection.peer_join_hello(
                peer_code=code.code, peer_install_id=FAR_INSTALL
            )
        finally:
            connection.close()

        assert reply["peered"]["expires_at"] is None
        assert lookup_peer(_store_root(), FAR_INSTALL).expires_at is None
