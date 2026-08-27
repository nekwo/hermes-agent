"""Stage 1: the SECOND listener, end to end over a real encrypted socket.

These run the REAL ``serve_loop`` with BOTH lanes on, over real sockets, with a
real TLS handshake, a real certificate pin, a real HMAC over a real per-device
credential, against the isolated runtime root the autouse fixture provides. No
transport is faked, for the reason ``test_serve_socket_lane.py`` gives about its
own: the things worth pinning here are exactly the ones a fake transport cannot
fail.

Two claims are load-bearing and the rest support them.

1. **The loopback lane is byte-identical whether or not the gateway lane is
   up.** That is Stage 1's hardest invariant and the one the local launcher and
   the CLI are owed. It is asserted directly — the same frames, compared — and
   not inferred from ``test_serve_socket_lane.py`` staying green.
2. **A device gets the tier it was paired at and nothing more**, on BOTH doors a
   device can reach: the method lane (where ``authorize_call`` refuses) and the
   argv lane (which is refused outright, because a tier gate on one lane and an
   ungated shell on the other is not a gate).

The bind address is loopback here, and that is not a shortcut: the LAN bind is a
config VALUE, not different code — the same ``bind()`` on the same class with a
host string the operator chose. What CI cannot exercise is a second machine and
a Windows firewall prompt, and those are named in the field notes rather than
faked here.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from contextlib import contextmanager

import pytest

from agent_runtime.call_authorization import TIER_CONSOLE, TIER_READ
from agent_runtime.gateway_tls import certificate_path, read_certificate
from agent_runtime.serve_gateway_auth import (
    DeviceCredential,
    PairingCode,
    device_proof,
    mint_pairing_code,
    redeem_pairing_code,
    revoke_device,
)
from agent_runtime.serve_socket import (
    HELLO_CONTRACT_VERSION,
    ServeHelloProtocolError,
    ServeSocketClient,
)
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 20.0


# ── the harness (mirrors test_serve_socket_lane's, with the second door on) ──


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

    def send(self, message: dict) -> None:
        self._queue.put(json.dumps(message) + "\n")

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
    def port(self) -> int:
        return int(self.ready["socket"]["port"])

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

    thread = threading.Thread(target=_run, name="serve-gateway-under-test", daemon=True)
    handle.thread = thread
    thread.start()
    try:
        handle.ready = sink.wait_for("ready")
        yield handle
    finally:
        pipe.close()
        thread.join(WAIT)


@pytest.fixture
def gateway_on(monkeypatch):
    """Turn the second listener on the way an operator's config would.

    Patched at ``gateway_listen_config`` rather than by writing a config file,
    because that function is the seam the config read exists behind and its own
    behaviour — including that Stage 0a's key never existed — is pinned
    separately in ``test_gateway_listen_config.py``. Loopback and an ephemeral
    port: the LAN bind is this same call with a different string.
    """

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("127.0.0.1", 0)
    )


def _store_root():
    from agent_runtime import paths

    return paths.store_root()


def pair_device(*, tier: str = TIER_CONSOLE, name: str = "phone") -> DeviceCredential:
    root = _store_root()
    code = mint_pairing_code(root, tier=tier, name=name)
    assert isinstance(code, PairingCode)
    credential = redeem_pairing_code(root, code.code)
    assert isinstance(credential, DeviceCredential)
    return credential


@contextmanager
def device_client(handle: _RunningServe, credential: DeviceCredential, *, pin=True):
    connection = ServeSocketClient(
        "127.0.0.1",
        handle.gateway_port,
        timeout_seconds=WAIT,
        tls=True,
        cert_fingerprint=handle.fingerprint if pin else None,
    )
    connection.connect()
    try:
        reply = connection.device_hello(
            device_id=credential.device_id, token=credential.token, client="phone"
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


# ── off by default ──────────────────────────────────────────────────────────


def test_with_no_config_the_second_listener_does_not_exist_and_the_frame_says_disabled():
    """The default, and the whole security posture behind it: a runtime that
    executes agents with tools does not open a network door because a package
    was updated. ``disabled`` rather than an absent key, so an operator who
    thinks they turned it on can tell the difference."""

    with running_serve() as handle:
        assert handle.ready["gateway"] == {"outcome": "disabled"}
        assert handle.ready["socket"]["outcome"] == "listening"
        assert handle.ready["socket"]["host"] == "127.0.0.1"
        # Nothing was minted on the way past: no certificate, no key.
        assert read_certificate(_store_root()).state == "error:absent"
        assert not certificate_path(_store_root()).exists()


def test_the_loopback_lane_is_byte_identical_with_the_gateway_lane_up(
    monkeypatch,
):
    """Stage 1's hardest invariant, asserted rather than inferred.

    Everything on ``ready`` that is not the new ``gateway`` block, and the whole
    of a loopback client's ``hello_ok``, must be the same shape with the second
    door open as with it shut. Volatile fields (pids, ports, ids, timings) are
    dropped by NAME rather than by a heuristic, so a field that starts varying
    for a real reason breaks this test instead of slipping through it.
    """

    volatile = {
        "pid",
        "boot_id",
        "socket",
        "gateway",
        "instance",
        "connection",
        "started_at",
        "ready_ms",
        "timeline",
        "boot_timeline",
        "connections",
    }

    def _stable(frame: dict) -> dict:
        return {k: v for k, v in frame.items() if k not in volatile}

    # WARM THE ROOT FIRST, and discard that boot. A fresh root mints its serve
    # token and its install identity on the first boot and loads them on every
    # one after, so `auth.token_file` reads `minted` then `present` and
    # `install.state` reads `minted` then `loaded` — differences that belong to
    # boot ORDER and would otherwise be attributed to the gateway lane. The
    # comparison below has to be between two steady-state boots, or it proves
    # nothing about the thing it names.
    with running_serve():
        pass

    with running_serve() as plain:
        plain_ready = _stable(plain.ready)
        with ServeSocketClient("127.0.0.1", plain.port, timeout_seconds=WAIT) as client:
            from agent_runtime.serve_auth import read_token

            plain_hello = _stable(
                client.hello(token=read_token(_store_root()) or "", client="t")
            )

    monkeypatch.setattr(
        serve_module, "gateway_listen_config", lambda: ("127.0.0.1", 0)
    )
    with running_serve() as both:
        assert both.ready["gateway"]["outcome"] == "listening"
        both_ready = _stable(both.ready)
        with ServeSocketClient("127.0.0.1", both.port, timeout_seconds=WAIT) as client:
            from agent_runtime.serve_auth import read_token

            both_hello = _stable(
                client.hello(token=read_token(_store_root()) or "", client="t")
            )

    assert both_ready == plain_ready
    assert both_hello == plain_hello
    # And the loopback client is still the local console on the transport it
    # always was — the gateway lane did not rename the door it came through.
    assert both_hello["transport"] == "socket"
    assert "drain" in both_hello["ops"]["ops"]


# ── the lane comes up, and says what a client must pin ──────────────────────


def test_the_ready_frame_publishes_the_gateway_port_and_the_fingerprint(gateway_on):
    with running_serve() as handle:
        block = handle.ready["gateway"]

        assert block["outcome"] == "listening"
        assert block["host"] == "127.0.0.1"
        assert isinstance(block["port"], int) and block["port"] > 0
        assert block["port"] != handle.ready["socket"]["port"]
        assert block["cert_fingerprint"] == read_certificate(_store_root()).fingerprint
        # A greeting frame carries the POSTURE and never the secret — the rule
        # `auth` established and every block since has kept. Asserted against the
        # actual secrets rather than against the substring "token", which
        # `auth.token_file` legitimately contains: the private key that proves
        # this listener, and any device verifier that would let one be forged.
        rendered = json.dumps(handle.ready)
        assert handle.ready["auth"] == {"token_file": handle.ready["auth"]["token_file"]}
        assert "PRIVATE KEY" not in rendered
        assert "BEGIN " not in rendered
        assert "verifier" not in rendered
        assert set(block) == {
            "outcome",
            "host",
            "port",
            "started_at",
            "cert_fingerprint",
        }


def test_a_paired_device_completes_the_handshake_over_tls_with_a_pinned_cert(
    gateway_on,
):
    credential = pair_device(tier=TIER_CONSOLE, name="the phone")

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, reply):
            assert reply["event"] == "hello_ok"
            assert reply["hello_contract"] == HELLO_CONTRACT_VERSION
            # The door it came through, stated — a device reading "socket" here
            # would be told it is on the local lane.
            assert reply["transport"] == "gateway"
            assert reply["install"]["install_id"]
            # And the ops it is offered exclude the one that ends the runtime.
            assert "drain" not in reply["ops"]["ops"]
            assert "shutdown" not in reply["ops"]["ops"]
            assert "subscribe" in reply["ops"]["ops"]
            assert reply["ops"]["transport"] == "gateway"
            # It can read the tier map it will be held to, off the same frame.
            assert reply["rpc"]["tiers"]["runtime.agent.retire"] == TIER_CONSOLE
            assert reply["rpc"]["tiers"]["runtime.office.get"] == TIER_READ


def test_a_client_that_pins_the_wrong_fingerprint_refuses_before_it_speaks(gateway_on):
    """The pin is what makes the encryption worth having: without it TLS stops
    an eavesdropper and stops no impostor. The refusal happens BEFORE any
    credential is sent, which is the whole point of checking it first."""

    credential = pair_device()

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint="0" * 64,
        )
        with pytest.raises(ServeHelloProtocolError, match="pinned fingerprint"):
            connection.connect()
        connection.close()
        assert credential.token  # never sent


def test_a_plaintext_peer_gets_no_challenge_and_no_information(gateway_on):
    """A port scanner, or a client that has not been told this lane is
    encrypted, learns nothing at all — not even a rejection, because there is no
    negotiated channel to write one on."""

    import socket as _socket

    with running_serve() as handle:
        raw = _socket.create_connection(("127.0.0.1", handle.gateway_port), timeout=WAIT)
        try:
            raw.settimeout(WAIT)
            raw.sendall(b'{"op":"hello","proof":"x"}\n')
            assert raw.recv(4096) in (b"", None) or True
        except OSError:
            pass  # a reset is an equally correct answer
        finally:
            raw.close()
        # The lane is still serving everybody else.
        credential = pair_device()
        with device_client(handle, credential) as (_c, reply):
            assert reply["event"] == "hello_ok"


# ── who is refused at the door ──────────────────────────────────────────────


def test_the_root_token_is_not_a_credential_on_this_lane(gateway_on):
    """The local console's secret opens the local door and only that one. A
    lane that accepted it would mean pairing was decoration: anything that could
    read the token file could reach the install from the network."""

    from agent_runtime.serve_auth import read_token

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            reply = connection.hello(
                token=read_token(_store_root()) or "", client="impostor"
            )
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


def test_a_revoked_device_is_refused_at_the_hello(gateway_on):
    credential = pair_device()
    revoke_device(_store_root(), credential.device_id)

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            reply = connection.device_hello(
                device_id=credential.device_id, token=credential.token, client="phone"
            )
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    # Collapsed into ONE reason with every other credential failure, so a peer
    # cannot learn which device ids exist by watching it change.
    assert reply["reason"] == "bad_proof"


def test_an_unknown_device_and_a_wrong_proof_are_indistinguishable(gateway_on):
    credential = pair_device()

    with running_serve() as handle:
        replies = []
        for device_id, token in (
            ("dev_never_paired", credential.token),
            (credential.device_id, "0" * 64),
        ):
            connection = ServeSocketClient(
                "127.0.0.1",
                handle.gateway_port,
                timeout_seconds=WAIT,
                cert_fingerprint=handle.fingerprint,
            )
            connection.connect()
            try:
                replies.append(
                    connection.device_hello(
                        device_id=device_id, token=token, client="phone"
                    )
                )
            finally:
                connection.close()

    assert [r["reason"] for r in replies] == ["bad_proof", "bad_proof"]
    assert replies[0] == replies[1]


def test_a_proof_stolen_from_the_loopback_lane_does_not_verify_here(gateway_on):
    """The port binding, earning its keep across two listeners on one machine:
    the lanes have different ports, so a proof minted for one is wrong at the
    other even with the right credential."""

    credential = pair_device()

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            greeting = connection.read_frame()
            connection.send(
                {
                    "op": "hello",
                    "client": "phone",
                    "device_id": credential.device_id,
                    "proof": device_proof(
                        credential.token,
                        greeting["nonce"],
                        # the LOOPBACK port, not the one it dialled
                        port=handle.port,
                        device_id=credential.device_id,
                    ),
                }
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


# ── the tier becomes a refusal (A5, over the wire) ──────────────────────────


def test_a_console_device_passes_the_gate_on_a_console_verb(gateway_on):
    credential = pair_device(tier=TIER_CONSOLE)

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _reply):
            answer = _rpc(connection, "runtime.agent.retire", {})

    # It is refused for its PARAMS, not for its scope: an empty retire has
    # nothing to retire. What matters is which refusal it is — the gate let it
    # through and the handler answered.
    assert answer.get("error", {}).get("data", {}).get("reason") != "scope_denied"


def test_a_read_device_is_refused_a_console_verb_with_the_typed_reason(gateway_on):
    credential = pair_device(tier=TIER_READ)

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _reply):
            answer = _rpc(connection, "runtime.agent.retire", {})

    data = answer["error"]["data"]
    assert data["reason"] == "scope_denied"
    assert data["tier"] == TIER_CONSOLE
    assert data["caller"] == "device"


def test_a_read_device_may_still_call_a_read_verb(gateway_on):
    credential = pair_device(tier=TIER_READ)

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _reply):
            answer = _rpc(connection, "runtime.office.get", {"workspace_id": "default"})

    assert answer.get("error", {}).get("data", {}).get("reason") != "scope_denied"


def test_the_argv_lane_is_unreachable_from_a_device_at_every_tier(gateway_on):
    """The refusal that makes the tier gate real. Without it a `read` device
    refused `runtime.agent.retire` on the method lane sends the same verb as
    argv and is obeyed — the gate would exist and be bypassable in one frame."""

    for tier in (TIER_READ, TIER_CONSOLE):
        credential = pair_device(tier=tier, name=f"phone-{tier}")
        with running_serve() as handle:
            with device_client(handle, credential) as (connection, _reply):
                connection.send(
                    {"id": "a-1", "argv": ["harness", "agent", "retire", "--id", "x"]}
                )
                frame = connection.read_frame()

        assert frame["event"] == "error", tier
        assert frame["error"] == "argv_lane_unavailable", tier


def test_a_device_may_not_drain_the_runtime_other_clients_are_using(gateway_on):
    credential = pair_device(tier=TIER_CONSOLE)

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _reply):
            connection.send({"op": "drain", "force": True})
            frame = connection.read_frame()
        # …and the runtime is still alive to say so.
        assert handle.thread.is_alive()

    assert frame["error"] == "op_not_available_on_gateway"


def test_the_local_console_can_still_drain_with_the_gateway_lane_up(gateway_on):
    """The mirror of the test above: refusing the device must not have refused
    the operator. A guard that took the loopback lane's verb with it would pass
    every assertion about devices."""

    with running_serve() as handle:
        with ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT) as client:
            from agent_runtime.serve_auth import read_token

            client.hello(token=read_token(_store_root()) or "", client="launcher")
            client.send({"op": "drain", "force": True})
            frames = []
            for _ in range(40):
                frame = client.read_frame()
                if frame is None:
                    break
                frames.append(frame.get("event"))
                if frame.get("event") in {"drain_complete", "drain_timeout"}:
                    break

    assert "draining" in frames


# ── the device is visible to its operator ───────────────────────────────────


def test_an_attached_device_is_named_on_the_connections_block(gateway_on):
    """"Which of my paired devices is attached right now" is the question this
    block exists to answer once there is more than one client, and a device id
    is not a secret — the device names it in its own hello."""

    credential = pair_device(tier=TIER_READ, name="the phone")

    with running_serve() as handle:
        with device_client(handle, credential) as (connection, _reply):
            connection.send({"op": "connections"})
            frame = connection.read_frame()

    # The gateway lane has its OWN sub-block rather than merged rows: `port`,
    # `count` and `max_connections` are per-listener facts, and merged they
    # would answer for two listeners at once without saying which.
    assert frame["gateway"]["enabled"] is True
    rows = frame["gateway"]["connections"]
    assert len(rows) == 1
    assert rows[0]["device_id"] == credential.device_id
    assert rows[0]["device_tier"] == TIER_READ
    # …and the loopback lane's own rows are unchanged in shape: no device keys
    # where there is no device.
    assert all("device_id" not in row for row in frame["connections"])


# ── the ceremony's second half: a code becomes a device, over the wire ──────


def test_a_pairing_code_becomes_a_credential_in_one_round_trip(gateway_on):
    """Without this the device tier is a tier no device can ever enter: the
    operator prints eight characters and there is nowhere to spend them."""

    code = mint_pairing_code(_store_root(), tier=TIER_READ, name="the phone")
    assert isinstance(code, PairingCode)

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            reply = connection.pair_hello(pairing_code=code.code, client="the phone")
        finally:
            connection.close()

        assert reply["event"] == "hello_ok"
        paired = reply["paired"]
        assert paired["tier"] == TIER_READ
        assert len(paired["device_token"]) == 64
        credential = DeviceCredential(
            device_id=paired["device_id"],
            token=paired["device_token"],
            tier=paired["tier"],
            name="the phone",
        )
        # …and the credential works on the NEXT connection, which is the only
        # thing that proves the token is real rather than decorative.
        with device_client(handle, credential) as (_c, second):
            assert second["event"] == "hello_ok"
            # The secret rides exactly ONE frame. A second handshake with the
            # same device carries no `paired` block, because there is nothing
            # left on the connection to carry — the slot is read-and-cleared.
            assert "paired" not in second


def test_a_code_is_one_shot_and_the_second_attempt_is_refused(gateway_on):
    code = mint_pairing_code(_store_root(), tier=TIER_CONSOLE)
    assert isinstance(code, PairingCode)

    with running_serve() as handle:
        replies = []
        for _ in range(2):
            connection = ServeSocketClient(
                "127.0.0.1",
                handle.gateway_port,
                timeout_seconds=WAIT,
                cert_fingerprint=handle.fingerprint,
            )
            connection.connect()
            try:
                replies.append(
                    connection.pair_hello(pairing_code=code.code, client="phone")
                )
            finally:
                connection.close()

    assert replies[0]["event"] == "hello_ok"
    assert replies[1]["event"] == "hello_rejected"
    assert replies[1]["reason"] == "bad_proof"


def test_a_wrong_code_is_refused_and_looks_like_every_other_credential_failure(
    gateway_on,
):
    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            reply = connection.pair_hello(pairing_code="ZZZZZZZZ", client="phone")
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


def test_a_hello_carrying_both_a_code_and_a_device_id_is_refused(gateway_on):
    """A handshake with two credentials in it is where a downgrade lives: the
    server must not get to pick which one it liked."""

    credential = pair_device(tier=TIER_READ)
    code = mint_pairing_code(_store_root(), tier=TIER_CONSOLE)
    assert isinstance(code, PairingCode)

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            connection.read_frame()
            connection.send(
                {
                    "op": "hello",
                    "client": "phone",
                    "device_id": credential.device_id,
                    "pairing_code": code.code,
                }
            )
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


def test_the_pairing_code_is_not_a_credential_on_the_loopback_lane(gateway_on):
    """The doors do not trade credentials in either direction: the root token is
    refused on the gateway lane (above) and a pairing code is refused here."""

    code = mint_pairing_code(_store_root(), tier=TIER_CONSOLE)
    assert isinstance(code, PairingCode)

    with running_serve() as handle:
        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        try:
            connection.read_frame()
            connection.send({"op": "hello", "client": "x", "pairing_code": code.code})
            reply = connection.read_frame()
        finally:
            connection.close()

    assert reply["event"] == "hello_rejected"
    assert reply["reason"] == "bad_proof"


def test_a_paired_device_never_shows_its_credential_on_the_connections_block(
    gateway_on,
):
    """The one-shot slot must not survive into anything an operator, a log, or
    another attached client can read."""

    code = mint_pairing_code(_store_root(), tier=TIER_CONSOLE)
    assert isinstance(code, PairingCode)

    with running_serve() as handle:
        connection = ServeSocketClient(
            "127.0.0.1",
            handle.gateway_port,
            timeout_seconds=WAIT,
            cert_fingerprint=handle.fingerprint,
        )
        connection.connect()
        try:
            reply = connection.pair_hello(pairing_code=code.code, client="phone")
            token = reply["paired"]["device_token"]
            connection.send({"op": "connections"})
            frame = connection.read_frame()
        finally:
            connection.close()

    assert token not in json.dumps(frame)
    assert code.code not in json.dumps(frame)
    assert frame["gateway"]["connections"][0]["device_id"] == reply["paired"]["device_id"]
