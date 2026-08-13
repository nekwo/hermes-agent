"""Slice 3: serve becomes a durable, multi-client, reconnectable service.

These run the REAL ``serve_loop`` with the REAL socket lane over REAL loopback
sockets, against the isolated runtime root the autouse fixture provides. No
transport is faked, because the things worth pinning here are exactly the ones a
fake transport cannot fail: that an unauthenticated peer gets NOTHING, that two
clients cannot collide, and that a drain reaches every attached client before
the lane closes under them.

The stdio lane is asserted alongside on purpose: this slice refactored the op
dispatcher to be transport-agnostic, and "stdio is unchanged" is a claim that
has to be tested, not asserted in a commit message.
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from agent_runtime.serve_auth import read_token
from agent_runtime.serve_socket import (
    HELLO_CONTRACT_VERSION,
    NONCE_BYTES,
    REJECT_BAD_PROOF,
    REJECT_DRAINING,
    REJECT_HELLO_MALFORMED,
    REJECT_HELLO_REQUIRED,
    REJECT_HELLO_TIMEOUT,
    REJECT_RATE_LIMITED,
    REJECT_TOO_MANY_CONNECTIONS,
    REJECT_TOO_MANY_PENDING,
    HelloRateLimiter,
    ServeHelloProtocolError,
    ServeSocketClient,
    ServeSocketServer,
    SocketOwnerLock,
    hello_proof,
    read_socket_owner,
    resolve_socket_target,
    socket_lock_path,
    socket_owner_path,
)
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 15.0


class _Clock:
    """A monotonic clock the test moves by hand.

    The rate limiter's window is real seconds. Waiting them out would make the
    recovery assertion below either slow or flaky, and — worse — a wall-clock
    test cannot distinguish "recovered because the window lapsed" from
    "recovered because something else reset it". Driving the clock makes the
    lapse the only variable.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


@contextmanager
def bare_server(**kwargs):
    """A real ``ServeSocketServer`` on real loopback, with its knobs exposed.

    ``running_serve`` below is the whole runtime and is the right seam for
    behaviour that involves the dispatcher, the drain, or the registry. It is
    the WRONG seam for the admission policy: capacity, the pre-auth bound and
    the two rate limiters are constructor arguments the serve loop fixes at
    production values, and a test that cannot set them can only assert the
    policy exists, never that it admits and refuses the right peers. This binds
    the same class the loop binds, over the same loopback, with those knobs in
    the test's hands.
    """

    logs: list[dict] = []
    token = kwargs.pop("token", "the-shared-secret")
    server = ServeSocketServer(
        _store_root(),
        boot_id=kwargs.pop("boot_id", "test-boot"),
        dispatch_line=kwargs.pop(
            "dispatch_line",
            lambda line, connection: connection.emit({"event": "echo", "line": line}),
        ),
        hello_payload=kwargs.pop(
            "hello_payload",
            lambda message, connection: {
                "event": "hello_ok",
                "connection": connection.key,
            },
        ),
        token_provider=kwargs.pop("token_provider", lambda: token),
        log=logs.append,
        **kwargs,
    )
    port = server.bind()
    server.start_accepting()
    try:
        yield server, port, logs
    finally:
        server.close()


def raw_handshake(port: int, *, token: str | None, client: str = "peer",
                  proof: object = None, hello: dict | None = None) -> tuple[dict | None, dict | None]:
    """One handshake driven by hand. Returns ``(server_hello, reply)``.

    Deliberately NOT ``ServeSocketClient``: half the cases below are peers that
    answer the challenge WRONGLY, and the reference client is written to answer
    it correctly.

    A peer can be refused BEFORE the challenge — capacity, the pre-auth bound,
    the two rate limiters and a draining lane are all decided without minting a
    nonce — in which case the FIRST frame is the rejection and there is nothing
    to answer. That is returned as ``(None, rejection)``, because sending a
    hello into a closed connection and then reading end-of-stream would report
    ``None`` for every one of those refusals and hide which it was.
    """

    connection = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
    connection.connect()
    try:
        greeting = connection.read_frame()
        if not isinstance(greeting, dict) or greeting.get("event") != "server_hello":
            return None, greeting
        if hello is not None:
            connection.send(hello)
        else:
            answer: dict = {"op": "hello", "client": client}
            if proof is not None:
                answer["proof"] = proof
            elif token is not None:
                answer["proof"] = hello_proof(token, greeting.get("nonce") or "")
            connection.send(answer)
        return greeting, connection.read_frame()
    finally:
        connection.close()


class _WireTap:
    """A socket that keeps a copy of every byte written through it.

    Wrapped around the client's socket AFTER ``connect``, so the reference
    client is still the thing under test and the reader still holds the real
    socket. ``socket.socket`` refuses attribute assignment, which is why this
    is a proxy rather than a patched method.
    """

    def __init__(self, sock, sent: list[bytes]) -> None:
        self._sock = sock
        self._sent = sent

    def sendall(self, data) -> None:
        self._sent.append(bytes(data))
        self._sock.sendall(data)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class _StdioPipe:
    """The inherited-pipe side: an iterable the test feeds lines into."""

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()

    def __iter__(self) -> "_StdioPipe":
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
    """serve's stdout, readable from the test thread while serve writes."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def text(self) -> str:
        with self._lock:
            return "".join(self._parts)

    def frames(self) -> list[dict]:
        rows = []
        for line in self.text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:  # a partially flushed tail
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
    def __init__(self, pipe: _StdioPipe, sink: _Sink, thread: threading.Thread) -> None:
        self.pipe = pipe
        self.sink = sink
        self.thread = thread
        self.ready: dict = {}
        self.code: int | None = None

    @property
    def port(self) -> int:
        return int(self.ready["socket"]["port"])


@contextmanager
def running_serve(**kwargs):
    """A live serve with the socket lane on, torn down at the end of the block."""

    pipe, sink = _StdioPipe(), _Sink()
    handle = _RunningServe(pipe, sink, None)  # type: ignore[arg-type]

    def _run() -> None:
        handle.code = serve_loop(
            pipe,
            sink,
            socket_lane=True,
            dispatch=kwargs.pop("dispatch", lambda argv: 0),
            liveness_pump_interval_seconds=kwargs.pop(
                "liveness_pump_interval_seconds", 60.0
            ),
            **kwargs,
        )

    thread = threading.Thread(target=_run, name="serve-under-test", daemon=True)
    handle.thread = thread
    thread.start()
    try:
        handle.ready = sink.wait_for("ready")
        yield handle
    finally:
        pipe.close()
        thread.join(WAIT)


@contextmanager
def client(handle: _RunningServe, *, token: str | None = None, name: str = "test-client",
           build: str | None = None):
    root = _store_root()
    resolved = token if token is not None else (read_token(root) or "")
    connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
    connection.connect()
    try:
        reply = connection.hello(token=resolved, client=name, client_build=build)
        yield connection, reply
    finally:
        connection.close()


def _store_root():
    from agent_runtime import paths

    return paths.store_root()


def _read_until(connection: ServeSocketClient, event: str, *, limit: int = 200) -> dict:
    for _ in range(limit):
        frame = connection.read_frame()
        if frame is None:
            raise AssertionError(f"connection closed before {event!r}")
        if frame.get("event") == event:
            return frame
    raise AssertionError(f"no {event!r} within {limit} frames")


# ── the lane comes up, and says so ──────────────────────────────────────────


def test_the_ready_frame_publishes_the_port_and_the_registry_records_it():
    from agent_runtime.serve_registry import list_serve_instances

    with running_serve() as handle:
        assert handle.ready["socket"]["outcome"] == "listening"
        assert handle.ready["socket"]["host"] == "127.0.0.1"
        port = handle.ready["socket"]["port"]
        assert isinstance(port, int) and port > 0

        rows = list_serve_instances(_store_root())
        assert len(rows) == 1
        assert rows[0]["transport"] == "stdio+socket"
        assert rows[0]["port"] == port
        assert rows[0]["socket_started_at"]

        # Discovery works from the sidecar too, and neither file leaks the token.
        owner = read_socket_owner(_store_root())
        assert owner["port"] == port
        assert "token" not in json.dumps(owner)
        assert "token" not in json.dumps(rows[0])


def test_a_stdio_only_serve_says_the_socket_is_disabled_and_records_no_port():
    from agent_runtime.serve_registry import list_serve_instances

    pipe, sink = _StdioPipe(), _Sink()
    pipe.send({"op": "shutdown"})
    code = serve_loop(pipe, sink, dispatch=lambda argv: 0)

    ready = next(f for f in sink.frames() if f.get("event") == "ready")
    assert code == 0
    assert ready["socket"] == {"outcome": "disabled"}
    assert not socket_lock_path(_store_root()).exists()
    assert list_serve_instances(_store_root()) == []


def test_the_second_serve_for_a_root_degrades_to_stdio_and_names_the_owner():
    """One socket owner per root, decided by a lock and REPORTED, never silent."""

    root = _store_root()
    incumbent = SocketOwnerLock(root)
    assert incumbent.acquire().acquired is True
    incumbent.publish_owner({"pid": 4242, "port": 61000, "boot_id": "incumbent"})
    try:
        with running_serve() as handle:
            assert handle.ready["socket"]["outcome"] == "lock_held_by"
            assert handle.ready["socket"]["pid"] == 4242

            from agent_runtime.serve_registry import list_serve_instances

            row = list_serve_instances(root)[0]
            assert row["transport"] == "stdio"
            assert row["port"] is None
    finally:
        incumbent.release()


# ── auth is first, and it is everything ─────────────────────────────────────


def test_a_good_token_gets_the_build_handshake():
    with running_serve() as handle:
        with client(handle, name="probe", build="deadbeefdeadbeef") as (_conn, reply):
            assert reply["event"] == "hello_ok"
            assert reply["contract"] == serve_module.SERVE_SCHEMA_VERSION
            assert reply["boot_id"] == handle.ready["boot_id"]
            assert set(reply["build"]) == {"commit", "dirty", "source", "resolved_at"}
            assert reply["transport"] == "socket"
            assert reply["draining"] is False
            # The client named a build; either it disagrees with this runtime's
            # commit (True) or the runtime could not measure its own (None).
            assert reply["build_mismatch"] in (True, None)


def test_a_client_on_the_same_build_is_not_flagged_and_one_on_another_build_is():
    from agent_runtime.build_stamp import build_stamp

    commit = build_stamp().commit
    if not commit:
        pytest.skip("this checkout could not measure its own commit")
    with running_serve() as handle:
        with client(handle, name="same", build=commit) as (_c, same):
            assert same["build_mismatch"] is False
        with client(handle, name="short", build=commit[:10]) as (_c, short):
            # A short hash is the same build, not a different one.
            assert short["build_mismatch"] is False
        with client(handle, name="other", build="0" * 40) as (_c, other):
            assert other["build_mismatch"] is True
            # Visible, never fatal: it is still a working connection.
            assert other["event"] == "hello_ok"


def test_the_server_speaks_first_and_the_token_never_travels():
    """F1, half two: the handshake is a challenge, not a credential hand-off.

    The transcript below is the whole point. The SERVER opens with a nonce; the
    client answers with an HMAC over it; the token appears in neither
    direction. A captured transcript is therefore unreplayable — the next
    connection demands a proof over a different nonce — and an impostor that
    binds a dead serve's port learns one HMAC over a challenge it chose, which
    authenticates it nowhere.
    """

    sent: list[bytes] = []
    with running_serve() as handle:
        token = read_token(_store_root()) or ""
        assert token

        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        connection._sock = _WireTap(connection._sock, sent)
        try:
            greeting = connection.read_frame()
            # The server spoke first, and what it said is only a challenge.
            assert greeting["event"] == "server_hello"
            assert greeting["hello_contract"] == HELLO_CONTRACT_VERSION
            assert greeting["algorithm"] == "hmac-sha256"
            assert len(greeting["nonce"]) == 2 * NONCE_BYTES
            int(greeting["nonce"], 16)  # hex, so it is the value it claims to be
            # An unauthenticated peer learns the boot id and nothing else: no
            # build, no runtime root, no ports, no answer to any op.
            assert set(greeting) == {
                "event",
                "nonce",
                "boot_id",
                "contract",
                "hello_contract",
                "algorithm",
            }

            connection.send(
                {
                    "op": "hello",
                    "client": "transcript",
                    "client_build": None,
                    "proof": hello_proof(token, greeting["nonce"]),
                }
            )
            reply = connection.read_frame()
            assert reply["event"] == "hello_ok"
        finally:
            connection.close()

    # Every byte this client put on the wire, checked against the secret.
    wire = b"".join(sent)
    assert token.encode() not in wire
    answer = json.loads(wire.decode().strip())
    assert answer["proof"] == hello_proof(token, greeting["nonce"])
    assert "token" not in answer
    # ...and the nonce is per CONNECTION, so the proof above cannot be reused.
    with running_serve() as second:
        first_nonce = greeting["nonce"]
        peer = ServeSocketClient("127.0.0.1", second.port, timeout_seconds=WAIT)
        peer.connect()
        try:
            fresh = peer.read_frame()
            assert fresh["nonce"] != first_nonce
        finally:
            peer.close()


def test_a_wrong_proof_gets_one_typed_rejection_and_no_data_at_all():
    """The anti-vacuity test for auth.

    A ``hello_rejected`` frame appearing proves only that SOMETHING was
    refused. What matters is that the peer learned nothing and could do
    nothing: after the challenge it is sent EXACTLY ONE more frame, that frame
    is the rejection, no op it pipelined is answered, and the stream ends.
    """

    with running_serve() as handle:
        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        try:
            greeting = connection.read_frame()
            assert greeting["event"] == "server_hello"
            connection.send(
                {
                    "op": "hello",
                    "client": "thief",
                    "proof": hello_proof("not-the-token", greeting["nonce"]),
                }
            )
            # Sent regardless of the rejection: an unauthenticated peer must not
            # be able to run ops by simply continuing to talk.
            connection.send({"op": "version"})
            connection.send({"id": "req-1", "argv": ["harness", "status", "--json"]})
            received = []
            while True:
                frame = connection.read_frame()
                if frame is None:
                    break
                received.append(frame)
            # ONE frame after the challenge, and then end of stream. Not "a
            # rejection appeared somewhere in the transcript".
            assert received == [
                {"event": "hello_rejected", "reason": REJECT_BAD_PROOF}
            ]
            blob = json.dumps(received)
            for leaked in ("build", "runtime_root", "commit", "version", "port"):
                assert leaked not in blob
        finally:
            connection.close()

        # The refusal did not take the lane down: a real client still works.
        with client(handle, name="legit") as (_conn, reply):
            assert reply["event"] == "hello_ok"


def test_a_missing_or_malformed_hello_is_refused_the_same_way():
    """Every way of not answering the challenge, and the reason for each.

    The reasons are not decoration: three of these charge the auth rate limiter
    and two must not, and the limiter derives that from the reason alone.
    """

    # A generous limiter, deliberately: five of these cases ARE auth
    # failures, so against the production limit of five the last of them
    # would be answered ``rate_limited`` and this test would be asserting
    # the limiter rather than the taxonomy. The limiter has its own tests.
    token = "the-shared-secret"
    with bare_server(
        rate_limiter=HelloRateLimiter(limit=100, window_seconds=600.0),
        reject_penalty_seconds=0.0,
    ) as (_server, port, _logs):
        cases = [
            ({"op": "version"}, REJECT_HELLO_REQUIRED),
            ({"op": "hello", "client": "no-proof"}, REJECT_BAD_PROOF),
            ({"op": "hello", "proof": 12345, "client": "wrong-type"}, REJECT_BAD_PROOF),
            ({"op": "hello", "proof": "", "client": "empty"}, REJECT_BAD_PROOF),
            # The token itself, offered the OLD way. There is no compatibility
            # shim by design: accepting it would keep the cleartext lane open.
            ({"op": "hello", "token": token, "client": "old-contract"}, REJECT_BAD_PROOF),
        ]
        for message, reason in cases:
            greeting, reply = raw_handshake(port, token=None, hello=message)
            # Every one of these got as far as the challenge, so the
            # rejection below is a verdict on the ANSWER, not a refusal
            # that happened before the peer said anything.
            assert greeting is not None and greeting["event"] == "server_hello", message
            assert reply == {"event": "hello_rejected", "reason": reason}, message

        # And a line that is not a JSON object at all.
        connection = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
        connection.connect()
        try:
            assert connection.read_frame()["event"] == "server_hello"
            connection._sock.sendall(b"this is not json\n")
            assert connection.read_frame() == {
                "event": "hello_rejected",
                "reason": REJECT_HELLO_MALFORMED,
            }
        finally:
            connection.close()


def test_a_peer_that_says_nothing_is_timed_out_and_is_not_an_auth_failure():
    """F3's other half: silence presented no credential to be wrong about."""

    clock = _Clock()
    limiter = HelloRateLimiter(limit=2, window_seconds=10.0, clock=clock)
    with bare_server(
        rate_limiter=limiter, hello_deadline_seconds=0.2, reject_penalty_seconds=0.0
    ) as (server, port, _logs):
        for _ in range(3):
            silent = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
            try:
                reader = silent.makefile("rb")
                assert json.loads(reader.readline())["event"] == "server_hello"
                frame = json.loads(reader.readline())
                assert frame == {
                    "event": "hello_rejected",
                    "reason": REJECT_HELLO_TIMEOUT,
                }
            finally:
                silent.close()

        # Three timeouts against a limit of two: had they been charged to the
        # AUTH limiter this next handshake would be refused. It is not.
        assert limiter.blocked() is False
        _greeting, reply = raw_handshake(port, token="the-shared-secret")
        assert reply["event"] == "hello_ok"
        assert server.connections_payload()["hello_timeouts"] == 3


def test_the_rate_limit_bites_and_then_lets_a_valid_client_back_IN():
    """F3, the defect and its recovery in one transcript.

    The old code charged EVERY rejection to the auth limiter, including the
    ``rate_limited`` rejection itself — so each polite retry pushed the window
    forward and a client holding the RIGHT credential could never get back in
    (proven live: 12 retries over 12s, never recovering). Both halves are
    asserted here: the window closes on real auth failures, and it OPENS again
    once they age out, with nothing the blocked client did extending it.
    """

    clock = _Clock()
    limiter = HelloRateLimiter(limit=3, window_seconds=10.0, clock=clock)
    with bare_server(rate_limiter=limiter, reject_penalty_seconds=0.0) as (
        server,
        port,
        _logs,
    ):
        for _ in range(3):
            _g, reply = raw_handshake(port, token="wrong-secret")
            assert reply == {"event": "hello_rejected", "reason": REJECT_BAD_PROOF}

        # The window is closed for everyone — the honest cost of a shared door.
        clock.advance(5.0)
        _g, blocked = raw_handshake(port, token="the-shared-secret")
        assert blocked == {"event": "hello_rejected", "reason": REJECT_RATE_LIMITED}

        # THE RECOVERY. Now 11s past the last real failure but only 6s past the
        # rejection above: if that rejection had re-armed the window (the
        # defect), this is still blocked. It must not be.
        clock.advance(6.0)
        _g, recovered = raw_handshake(port, token="the-shared-secret")
        assert recovered["event"] == "hello_ok", (
            "a valid client never recovered — the blocked rejection extended "
            "its own block"
        )
        counts = server.connections_payload()["rejected_by_reason"]
        assert counts[REJECT_BAD_PROOF] == 3
        assert counts[REJECT_RATE_LIMITED] == 1


def test_a_capacity_refusal_is_not_charged_to_the_auth_limiter():
    """F3's first half, at the sharpest setting the knobs allow.

    One auth failure is enough to close this window, and the capacity refusal
    below is the only rejection that happens. If it were charged — as every
    non-auth rejection used to be — the valid client afterwards would be
    ``rate_limited``. The server's OWN state can never block a client.
    """

    clock = _Clock()
    limiter = HelloRateLimiter(limit=1, window_seconds=600.0, clock=clock)
    with bare_server(
        max_connections=1, rate_limiter=limiter, reject_penalty_seconds=0.0
    ) as (server, port, _logs):
        incumbent = ServeSocketClient("127.0.0.1", port, timeout_seconds=WAIT)
        incumbent.connect()
        try:
            assert incumbent.hello(
                token="the-shared-secret", client="incumbent"
            )["event"] == "hello_ok"

            _g, refused = raw_handshake(port, token="the-shared-secret")
            assert refused == {
                "event": "hello_rejected",
                "reason": REJECT_TOO_MANY_CONNECTIONS,
            }
            assert limiter.blocked() is False
        finally:
            incumbent.close()

        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and server.connections_payload()["count"]:
            time.sleep(0.02)
        _g, admitted = raw_handshake(port, token="the-shared-secret")
        assert admitted["event"] == "hello_ok"


def test_peers_that_prove_nothing_are_bounded_separately_from_authenticated_ones():
    """F4: 64 silent sockets once sat on 64 threads and drew not one rejection,
    because ``at_capacity`` counted AUTHENTICATED connections only."""

    with bare_server(
        max_connections=32,
        max_pending_connections=2,
        hello_deadline_seconds=WAIT,
        reject_penalty_seconds=0.0,
    ) as (server, port, _logs):
        silent = []
        try:
            for _ in range(2):
                sock = socket.create_connection(("127.0.0.1", port), timeout=WAIT)
                silent.append(sock)
                assert json.loads(sock.makefile("rb").readline())["event"] == "server_hello"

            deadline = time.monotonic() + WAIT
            while (
                time.monotonic() < deadline
                and server.connections_payload()["pending"] < 2
            ):
                time.sleep(0.02)
            payload = server.connections_payload()
            # The lane is nowhere near its AUTHENTICATED bound...
            assert payload["count"] == 0
            assert payload["max_connections"] == 32
            # ...and is nonetheless full, on the bound that watches this phase.
            assert payload["pending"] == 2
            assert payload["max_pending_connections"] == 2

            _g, refused = raw_handshake(port, token="the-shared-secret")
            assert refused == {
                "event": "hello_rejected",
                "reason": REJECT_TOO_MANY_PENDING,
            }
        finally:
            for sock in silent:
                sock.close()

        # And it is a bound, not a wall: the slots come back.
        deadline = time.monotonic() + WAIT
        while (
            time.monotonic() < deadline and server.connections_payload()["pending"]
        ):
            time.sleep(0.02)
        _g, admitted = raw_handshake(port, token="the-shared-secret")
        assert admitted["event"] == "hello_ok"
        assert server.connections_payload()["pending_peak"] >= 2


def test_the_accept_loop_reports_how_it_ended_instead_of_dying_quietly():
    """F4's other half. A loop that stopped without a word leaves a service
    that answers its discovery record and then never answers anything."""

    with bare_server() as (server, port, logs):
        payload = server.connections_payload()
        # Both facts are stated while healthy, so an operator reading them
        # after a failure is reading the same two fields, not a new one.
        assert payload["accept_errors"] == 0
        assert payload["accept_loop_exited"] is None
        server.close()
        deadline = time.monotonic() + WAIT
        while (
            time.monotonic() < deadline
            and server.connections_payload()["accept_loop_exited"] is None
        ):
            time.sleep(0.02)
        assert server.connections_payload()["accept_loop_exited"] == "listener_closed"
        assert [row for row in logs if row["event"] == "serve_socket_accept_loop_exit"]


def test_the_token_never_appears_in_any_frame_the_service_writes():
    token = None
    with running_serve() as handle:
        token = read_token(_store_root())
        assert token
        with client(handle, name="probe") as (connection, reply):
            connection.send({"op": "version"})
            version = _read_until(connection, "version")
            connection.send({"op": "connections"})
            connections = _read_until(connection, "socket_connections")
            assert token not in json.dumps([reply, version, connections])
    assert token not in handle.sink.text()


# ── the impostor: the live-proven attack, from the attacker's chair ─────────
#
# The CRITICAL finding was not "the handshake is weak in theory". Discovery
# handed a client a row it had ITSELF classified ``stale_dead_pid``, the client
# checked only that the row existed, and the raw token went out in cleartext to
# whatever had taken the dead serve's port. Two independent halves close it, so
# the tests come in two halves: what the CLIENT will put on a wire (never the
# token), and what discovery will hand it in the first place (live rows only).


class _ImpostorServer:
    """A plain listener that is not this service, and records what it is told.

    Not a mock of anything: a local process that binds a recycled port and
    speaks whatever it likes is exactly the situation, so this speaks whatever
    the test tells it to and keeps every byte it receives.
    """

    def __init__(self, greeting: dict | None) -> None:
        self.greeting = greeting
        self.received = bytearray()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = int(self._listener.getsockname()[1])
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._done = threading.Event()
        self._thread.start()

    def _serve(self) -> None:
        try:
            sock, _peer = self._listener.accept()
        except OSError:
            self._done.set()
            return
        try:
            if self.greeting is not None:
                sock.sendall((json.dumps(self.greeting) + "\n").encode())
            sock.settimeout(2.0)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.received += chunk
        finally:
            self._done.set()
            try:
                sock.close()
            except OSError:
                pass

    def wait(self, timeout: float = WAIT) -> None:
        self._done.wait(timeout)

    def close(self) -> None:
        try:
            self._listener.close()
        except OSError:
            pass


def test_an_impostor_on_a_recycled_port_harvests_a_proof_and_never_the_token():
    """The attack, replayed against the fixed client.

    The impostor gets to choose the nonce, so it gets the strongest thing this
    handshake can ever give it: one HMAC over a challenge of its own. That
    authenticates it to nothing — it is not the key — and the key itself never
    leaves this process.
    """

    secret = "S3CRET-" + "a" * 40
    impostor = _ImpostorServer(
        {
            "event": "server_hello",
            "nonce": "b" * (2 * NONCE_BYTES),
            "boot_id": "impostor",
            "contract": 1,
            "hello_contract": HELLO_CONTRACT_VERSION,
            "algorithm": "hmac-sha256",
        }
    )
    try:
        connection = ServeSocketClient("127.0.0.1", impostor.port, timeout_seconds=5.0)
        connection.connect()
        try:
            connection.hello(token=secret, client="victim", client_build="deadbeef")
        except Exception:
            # The impostor answers nothing, so the read after the proof ends or
            # times out. What it was SENT is the whole question.
            pass
        finally:
            connection.close()
        impostor.wait()
    finally:
        impostor.close()

    wire = bytes(impostor.received)
    assert wire, "the client sent nothing at all — this proves nothing"
    assert secret.encode() not in wire
    answer = json.loads(wire.decode().strip())
    assert set(answer) == {"op", "client", "client_build", "proof"}
    assert answer["op"] == "hello"
    assert "token" not in answer
    # It is a proof over the impostor's OWN nonce: worthless anywhere else,
    # and worthless here on the next connection, which mints a fresh one.
    assert answer["proof"] == hello_proof(secret, "b" * (2 * NONCE_BYTES))


@pytest.mark.parametrize(
    ("greeting", "expected"),
    [
        (None, "the peer did not open with a server_hello challenge"),
        ({"event": "banner", "service": "something else"}, "did not open with"),
        ({"event": "server_hello", "hello_contract": 2}, "no usable nonce"),
        (
            {"event": "server_hello", "nonce": "c" * 64, "hello_contract": 1},
            "unsupported hello_contract",
        ),
        (
            {"event": "hello_rejected", "reason": "rate_limited"},
            "did not open with",
        ),
    ],
)
def test_a_client_sends_nothing_at_all_to_a_peer_that_does_not_speak_this_contract(
    greeting, expected
):
    """Refused, not adapted.

    A client that guesses at an unknown handshake is a client that will
    eventually guess "send the token" — which is the shape this contract exists
    to retire. Each case asserts the STRONGEST form of that: zero bytes sent.
    """

    secret = "S3CRET-" + "d" * 40
    impostor = _ImpostorServer(greeting)
    try:
        connection = ServeSocketClient("127.0.0.1", impostor.port, timeout_seconds=3.0)
        connection.connect()
        try:
            with pytest.raises(ServeHelloProtocolError) as caught:
                connection.hello(token=secret, client="victim")
            assert expected in str(caught.value)
            if greeting is not None and greeting.get("event") == "hello_rejected":
                # The refusal's own reason IS the answer, and it is carried.
                assert caught.value.reason == "rate_limited"
        finally:
            connection.close()
        impostor.wait()
    finally:
        impostor.close()

    assert bytes(impostor.received) == b""


# ── discovery returns live rows, and callers gate on the classification ─────


def _write_registry_row(**overrides) -> int:
    from agent_runtime.serve_registry import register_serve_instance

    pid = int(overrides.pop("pid"))
    register_serve_instance(
        _store_root(),
        transport=overrides.pop("transport", "stdio+socket"),
        pid=pid,
        boot_id=overrides.pop("boot_id", "row-boot"),
        port=overrides.pop("port", 61999),
        socket_started_at="2026-08-13T00:00:00Z",
        probe=overrides.pop("probe", None),
    )
    return pid


def _dead_pid() -> int:
    """A pid this machine will classify as gone, FOUND rather than guessed.

    Asked through the registry's own probe, deliberately. ``os.kill(pid, 0)``
    is the obvious way to ask and is wrong on Windows — CPython routes signal 0
    through ``GenerateConsoleCtrlEvent``, so the liveness probe Ctrl-C's the
    target's console group (bpo-14484), which ``serve_registry._pid_alive``
    documents at length. Asking the same way the classifier asks also means
    this helper cannot disagree with the verdict the test is about to assert.
    """

    from agent_runtime.serve_registry import default_process_probe

    probe = default_process_probe()
    for candidate in range(60_000, 65_000):
        if probe.alive(candidate) is False:
            return candidate
    raise AssertionError("no provably-dead pid available on this machine")


def test_discovery_returns_no_target_at_all_when_the_only_row_is_dead():
    """The CRITICAL finding's first half.

    ``resolve_socket_target`` used to fall back to ``candidates`` — every row
    with a port, including one it had classified ``stale_dead_pid`` — and then
    to the owner sidecar with no classification at all. A dead serve's port is
    reusable by any local process, so that fallback was a target chosen from a
    record that says the owner is gone.
    """

    dead = _dead_pid()
    _write_registry_row(pid=dead, port=61999)

    from agent_runtime.serve_registry import list_serve_instances

    rows = list_serve_instances(_store_root())
    assert [row["classification"] for row in rows] == ["stale_dead_pid"]

    # By default: nothing. Not a row "you could try".
    assert resolve_socket_target(_store_root()) is None

    # The diagnostic form returns it CARRYING the verdict, so a caller can
    # refuse it by name rather than report the far less useful "not found".
    stale = resolve_socket_target(_store_root(), allow_stale=True)
    assert stale is not None
    assert stale.classification == "stale_dead_pid"
    assert stale.live is False
    assert stale.port == 61999


_LIVE_TICKS = 4242424242


def _probe_for(live_pid: int):
    """A probe that answers ``live`` for exactly one pid.

    Injected, because classification is a question about the OS and this test
    is about what the RESOLVER does with the three different answers it can
    get. An in-process serve can never classify itself ``live`` here anyway:
    the classifier requires a command line containing both "hermes" and
    "serve", and the process running these tests is pytest. The live path
    against a real serve is covered end to end in
    ``test_serve_socket_child_e2e.py``; the CHOICE is covered here.
    """

    from agent_runtime.serve_registry import ProcessProbe

    return ProcessProbe(
        alive=lambda pid: pid == live_pid,
        start_time=lambda pid: _LIVE_TICKS if pid == live_pid else None,
        cmdline=lambda pid: (
            "hermes harness serve --ndjson" if pid == live_pid else "notepad.exe"
        ),
    )


def test_discovery_prefers_the_live_row_over_the_dead_one_beside_it():
    from agent_runtime.serve_registry import list_serve_instances

    dead = _dead_pid()
    live = dead - 1
    probe = _probe_for(live)
    _write_registry_row(pid=dead, port=61999)
    _write_registry_row(pid=live, port=62000, probe=probe)

    rows = {
        row["pid"]: row["classification"]
        for row in list_serve_instances(_store_root(), probe=probe)
    }
    # The two rows really are classified DIFFERENTLY. Without this the test
    # would pass against a resolver that returned whatever came first.
    assert rows[dead] == "stale_dead_pid"
    assert rows[live] == "live"

    target = resolve_socket_target(_store_root(), probe=probe)
    assert target is not None
    assert target.live is True
    assert target.classification == "live"
    assert target.pid == live
    assert target.port == 62000
    assert target.source == "registry"

    # ...and the dead row is not the answer even on the diagnostic path, which
    # returns the BEST candidate rather than any candidate.
    stale = resolve_socket_target(_store_root(), probe=probe, allow_stale=True)
    assert stale.port == 62000


def test_the_owner_sidecar_alone_is_never_a_live_target():
    """The sidecar is discovery data written by a process that may be gone. It
    used to be returned with NO classification, which read as "no objection"
    at the one call site that had to object."""

    root = _store_root()
    lock = SocketOwnerLock(root)
    assert lock.acquire().acquired is True
    lock.publish_owner({"pid": 4242, "port": 61998, "boot_id": "sidecar-only"})
    try:
        assert resolve_socket_target(root) is None
        stale = resolve_socket_target(root, allow_stale=True)
        assert stale.source == "owner_file"
        assert stale.classification == "unverified_owner_file"
        assert stale.live is False
    finally:
        lock.release()


def test_the_connect_verb_refuses_a_target_that_is_not_live_and_names_why():
    """The CLIENT half of the same finding, at the verb an operator types."""

    import argparse
    import io
    from contextlib import redirect_stdout

    from hermes_cli.harness_parts.serve import (
        SERVE_CONNECT_NO_SERVICE_EXIT_CODE,
        _cmd_serve_connect,
    )

    dead = _dead_pid()
    _write_registry_row(pid=dead, port=61999)
    args = argparse.Namespace(probe=False, drain=False, deadline_seconds=None,
                              client=None, timeout=2.0)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = _cmd_serve_connect(args)
    report = json.loads(buffer.getvalue())

    assert code == SERVE_CONNECT_NO_SERVICE_EXIT_CODE
    assert report["ok"] is False
    assert report["error"] == "socket_service_not_live"
    # Named, not merely refused: "nothing found" would have hidden the row.
    assert report["classification"] == "stale_dead_pid"
    assert report["target"]["port"] == 61999
    assert report["target"]["live"] is False


# ── requests: one dispatcher, N transports ──────────────────────────────────


def test_a_socket_request_is_answered_on_the_socket_and_not_on_stdout():
    def _dispatch(argv):
        print(f"answer:{argv[-1]}")
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="cli") as (connection, _reply):
            connection.send({"id": "req-1", "argv": ["harness", "status", "alpha"]})
            line = _read_until(connection, "line")
            assert line == {"id": "req-1", "event": "line", "line": "answer:alpha"}
            exit_frame = _read_until(connection, "exit")
            assert exit_frame == {"id": "req-1", "event": "exit", "code": 0}

        # The answer went to the asker, not to whoever owns stdout.
        assert not [f for f in handle.sink.frames() if f.get("id") == "req-1"]

        # ...and the stdio lane still answers its own requests, unchanged.
        handle.pipe.send({"id": "s-1", "argv": ["harness", "status", "beta"]})
        stdio_exit = next(
            f
            for f in _poll(handle.sink, lambda rows: [r for r in rows if r.get("id") == "s-1" and r.get("event") == "exit"])
        )
        assert stdio_exit == {"id": "s-1", "event": "exit", "code": 0}
        assert {"id": "s-1", "event": "line", "line": "answer:beta"} in handle.sink.frames()


def _poll(sink: _Sink, select, timeout: float = WAIT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = select(sink.frames())
        if found:
            return found
        time.sleep(0.01)
    raise AssertionError("nothing matched within the timeout")


def test_two_clients_may_use_the_same_request_id_without_colliding():
    """Request ids are chosen by CLIENTS. Once the service is multi-client, two
    of them WILL pick ``req-1``, and neither may see the other's answer."""

    def _dispatch(argv):
        print(f"from:{argv[-1]}")
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="a") as (first, _r1), client(handle, name="b") as (second, _r2):
            first.send({"id": "req-1", "argv": ["harness", "status", "first"]})
            second.send({"id": "req-1", "argv": ["harness", "status", "second"]})
            first_line = _read_until(first, "line")
            second_line = _read_until(second, "line")
            assert first_line["line"] == "from:first"
            assert second_line["line"] == "from:second"
            assert _read_until(first, "exit")["code"] == 0
            assert _read_until(second, "exit")["code"] == 0


def test_a_client_cannot_cancel_another_clients_request():
    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(dispatch=_dispatch, pool_size=4) as handle:
        with client(handle, name="owner") as (owner, _r1), client(handle, name="stranger") as (stranger, _r2):
            owner.send({"id": "req-1", "argv": ["harness", "status"]})
            assert started.wait(WAIT)
            stranger.send({"op": "cancel", "id": "req-1"})
            denied = _read_until(stranger, "cancel_denied")
            # Not even visible: another connection's request id is not a thing
            # this client has any standing to name.
            assert denied["state"] == "unknown"
            release.set()
            assert _read_until(owner, "exit")["code"] == 0


def test_shutdown_is_refused_on_the_socket_because_drain_is_the_multi_client_verb():
    with running_serve() as handle:
        with client(handle, name="impatient") as (connection, _reply):
            connection.send({"op": "shutdown"})
            error = _read_until(connection, "error")
            assert error["error"] == "op_not_available_on_socket"
            # Still serving everybody else.
            connection.send({"op": "ping"})
            assert _read_until(connection, "busy")["pending"] == 0


# ── subscriptions ───────────────────────────────────────────────────────────


def _fake_stream(gate: threading.Event):
    def _factory():
        def _generate():
            index = 0
            yield {"type": "hydrate", "index": index}
            while not gate.is_set():
                index += 1
                time.sleep(0.005)
                yield {"type": "delta", "index": index}

        return _generate()

    return _factory


def test_subscribe_pushes_the_stream_to_every_subscriber_and_unsubscribe_ends_it():
    gate = threading.Event()
    try:
        with running_serve(stream_source_factory=_fake_stream(gate)) as handle:
            with client(handle, name="a") as (first, _r1), client(handle, name="b") as (second, _r2):
                first.send({"op": "subscribe", "lane": "stream"})
                assert _read_until(first, "subscribed")["lane"] == "stream"
                second.send({"op": "subscribe"})
                assert _read_until(second, "subscribed")["lane"] == "stream"

                # Both are fed, and both start at a hydrate.
                for connection in (first, second):
                    frames = [connection.read_frame() for _ in range(4)]
                    assert any(f.get("type") == "hydrate" for f in frames)
                    assert any(f.get("type") == "delta" for f in frames)

                first.send({"op": "unsubscribe"})
                ended = _read_until(first, "unsubscribed")
                assert ended["was_subscribed"] is True

                # Still reported honestly for the one that stayed.
                second.send({"op": "connections"})
                summary = _read_until(second, "socket_connections")
                assert summary["subscriptions"]["subscribers"] == 1
    finally:
        gate.set()


def test_a_disconnect_unsubscribes_and_does_nothing_else():
    gate = threading.Event()
    try:
        with running_serve(stream_source_factory=_fake_stream(gate)) as handle:
            with client(handle, name="stayer") as (stayer, _r1):
                leaver = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
                leaver.connect()
                leaver.hello(
                    token=read_token(_store_root()) or "", client="leaver", client_build=None
                )
                leaver.send({"op": "subscribe"})
                _read_until(leaver, "subscribed")
                leaver.close()

                # The runtime outlives its client: the subscription is gone, the
                # connection is gone, and everything else is exactly as it was.
                deadline = time.monotonic() + WAIT
                summary = None
                while time.monotonic() < deadline:
                    stayer.send({"op": "connections"})
                    summary = _read_until(stayer, "socket_connections")
                    if summary["count"] == 1:
                        break
                    time.sleep(0.05)
                assert summary["count"] == 1
                assert summary["subscriptions"]["subscribers"] == 0
                assert [row["client"] for row in summary["connections"]] == ["stayer"]
    finally:
        gate.set()


def test_an_unsupported_lane_is_refused_by_name():
    with running_serve() as handle:
        with client(handle, name="a") as (connection, _reply):
            connection.send({"op": "subscribe", "lane": "telemetry"})
            denied = _read_until(connection, "subscribe_denied")
            assert denied == {
                "event": "subscribe_denied",
                "lane": "telemetry",
                "reason": "unsupported_lane",
            }


# ── observability ───────────────────────────────────────────────────────────


def test_the_version_reply_carries_the_connections_block_on_both_transports():
    with running_serve() as handle:
        with client(handle, name="cockpit", build="0" * 40) as (connection, _reply):
            connection.send({"op": "version"})
            version = _read_until(connection, "version")
            assert version["transport"] == "socket"
            assert version["socket"]["outcome"] == "listening"
            rows = version["connections"]["connections"]
            assert [row["client"] for row in rows] == ["cockpit"]
            assert rows[0]["client_build"] == "0" * 40
            assert rows[0]["frames_out"] >= 1
            assert rows[0]["bytes_out"] > 0
            assert rows[0]["subscribed"] is False

            # The stdio lane's own version reply still says stdio.
            handle.pipe.send({"op": "version"})
            stdio_version = handle.sink.wait_for("version")
            assert stdio_version["transport"] == "stdio"
            assert stdio_version["connections"]["count"] == 1


def test_every_connection_open_close_and_rejection_emits_one_structured_log_line():
    with running_serve() as handle:
        with client(handle, name="watched") as (_conn, _reply):
            pass
        # A proof this test can recognise on sight, so "the proof was not
        # logged" is a check against THAT value rather than against the
        # word — which is a substring of the rejection reason itself.
        presented = "f" * 64
        _greeting, rejected_frame = raw_handshake(
            handle.port, token=None, proof=presented, client="thief"
        )
        assert rejected_frame["reason"] == REJECT_BAD_PROOF

        def _service_logs(rows):
            found = [
                json.loads(row["line"])
                for row in rows
                if row.get("event") == "stderr" and row.get("line", "").startswith("{")
            ]
            # Wait for the LAST of the three, not for any log at all: the
            # rejection is written on the rejected connection's own thread and
            # lands after the ones this test already provoked.
            events = {row.get("event") for row in found}
            return found if "serve_socket_connection_rejected" in events else []

        logs = _poll(handle.sink, _service_logs)
        by_event = {row["event"]: row for row in logs}
        assert by_event["serve_socket_connection_open"]["client"] == "watched"
        assert by_event["serve_socket_connection_open"]["boot_id"] == handle.ready["boot_id"]
        assert by_event["serve_socket_connection_close"]["client"] == "watched"
        assert by_event["serve_socket_connection_close"]["reason"]
        rejected = by_event["serve_socket_connection_rejected"]
        assert rejected["reason"] == REJECT_BAD_PROOF
        # The rejected peer's own label is recorded; nothing derived from the
        # real secret is — and the proof it DID present is not logged either,
        # because a log full of HMACs over known nonces is an offline corpus.
        assert rejected["peer"]
        token = read_token(_store_root()) or ""
        blob = json.dumps(logs)
        assert token not in blob
        # A log full of HMACs over known nonces is an offline corpus, so
        # the presented proof is not written down either.
        assert presented not in blob


# ── drain over the socket ───────────────────────────────────────────────────


def test_a_drain_asked_over_the_socket_tells_every_client_then_closes_the_lane():
    """The live proof slice 2 could not run: a drain observed from OUTSIDE the
    process, by a client that stays attached to watch it finish."""

    from agent_runtime.serve_registry import list_serve_instances

    root = _store_root()
    with running_serve() as handle:
        with client(handle, name="watcher") as (watcher, _r1), client(handle, name="operator") as (operator, _r2):
            operator.send({"op": "drain", "force": True, "deadline_seconds": 10})

            # Every attached client hears it, not just the one that asked.
            assert _read_until(watcher, "draining")["pid"] == handle.ready["pid"]
            assert _read_until(operator, "draining")["boot_id"] == handle.ready["boot_id"]

            complete = _read_until(operator, "drain_complete")
            assert complete["requests_refused"] == 0
            assert complete["requests_completed"] == 0

            # The watcher — which asked for nothing — is told how it ended too.
            assert _read_until(watcher, "drain_complete")["drain_ms"] >= 0

            # And then the lane closes under both of them, which is the point:
            # a client's next read returns end-of-stream, not silence.
            assert watcher.read_frame() is None
            assert operator.read_frame() is None

        # The lane is released: registry entry gone, owner sidecar gone.
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and list_serve_instances(root):
            time.sleep(0.05)
        assert list_serve_instances(root) == []
        assert not socket_owner_path(root).exists()

        # The LOCK FILE, however, stays — deliberately, and this is the
        # assertion that changed. Releasing used to unlink it, and on POSIX
        # that is a two-owner race: ``flock`` is held on an open DESCRIPTION,
        # not on a path, so a contender that has opened the file but not yet
        # locked it ends up holding a lock on an unnamed inode while the next
        # boot creates a fresh inode at that path and locks THAT. Both would
        # believe they own the one root. A persistent zero-byte file costs
        # nothing and closes it.
        assert socket_lock_path(root).exists()
        assert socket_lock_path(root).stat().st_size <= 1

        # And it is still just a file with a lock on it: a fresh serve takes
        # the lane over immediately, which is the property the unlink was
        # (wrongly) thought to provide.
        replacement = SocketOwnerLock(root)
        assert replacement.acquire().acquired is True
        replacement.release()
        assert socket_lock_path(root).exists()

    assert handle.code == 0


def test_a_drain_over_the_socket_requires_force_and_the_refusal_is_typed():
    """`shutdown` is refused on this lane outright; `drain` is the safe
    replacement verb — but it still ends the service for every attached
    client, so it has to be asked for on purpose."""

    with running_serve() as handle:
        with client(handle, name="careless") as (connection, _reply):
            connection.send({"op": "drain"})
            error = _read_until(connection, "error")
            assert error["error"] == "drain_requires_force"
            assert error["transport"] == "socket"
            # Nothing was started: the service is still serving.
            connection.send({"op": "ping"})
            assert _read_until(connection, "busy")["pending"] == 0
            connection.send({"op": "version"})
            assert _read_until(connection, "version")["draining"] is False


def test_a_socket_client_cannot_clamp_the_drain_deadline_to_nothing():
    """F2's first half. ``drain`` on this lane is reachable by any local
    process holding the root's secret, and the timeout path calls ``hard_exit``
    — which is ``os._exit`` — so a client that could ask for 0.05s could turn
    the restart verb into a kill over work it cannot see."""

    with running_serve(drain_socket_minimum_deadline_seconds=25.0) as handle:
        with client(handle, name="impatient") as (connection, _reply):
            connection.send({"op": "drain", "force": True, "deadline_seconds": 0.05})
            draining = _read_until(connection, "draining")
            # Floored, and SAYING SO: what was asked for sits beside what was
            # granted, so a client cannot mistake a floor for an honoured ask.
            assert draining["deadline_seconds"] == 25.0
            assert draining["requested_deadline_seconds"] == 0.05
            assert draining["minimum_deadline_seconds"] == 25.0


def test_a_stdio_drain_deadline_is_still_the_askers_to_choose():
    """The other side of the same fix, and the reason the floor is per-lane.

    The stdio asker is the PARENT that spawned this process and owns its stdin.
    It can end this runtime with a signal whether or not the drain cooperates,
    so flooring it buys no safety — and would silently rewrite a contract the
    socket slice promised to leave byte-identical.
    """

    with running_serve(drain_socket_minimum_deadline_seconds=25.0) as handle:
        handle.pipe.send({"op": "drain", "deadline_seconds": 0.5})
        draining = handle.sink.wait_for("draining")
        assert draining["deadline_seconds"] == 0.5
        assert draining["minimum_deadline_seconds"] == 0.05
        handle.sink.wait_for("drain_complete")


def test_a_live_chat_turn_survives_a_socket_drain_and_the_hold_is_reported():
    """F2's second half, and the finding that made it [HIGH].

    ``serve``'s own contract says a supervisor must never recycle this process
    while ``chat_turns > 0`` — recording safety. A drain deadline firing
    ``hard_exit(3)`` over a live turn is that recycle by another name, and any
    local process holding the token could ask for it. So an expiry with a chat
    turn in flight is NOT terminal: it says so, keeps serving, and re-arms.

    The assertion that matters is the one that would pass vacuously if the turn
    had simply been killed: the turn LANDS, with its own exit frame, AFTER the
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
        with client(handle, name="turn-holder") as (connection, _reply):
            # The argv shape serve recognises as a chat turn.
            connection.send(
                {"id": "turn-1", "argv": ["harness", "mission-chat", "message", "hi"]}
            )
            assert started.wait(WAIT)

            connection.send({"op": "drain", "force": True, "deadline_seconds": 0.2})
            assert _read_until(connection, "draining")["pending"] == 1

            # The deadline lapses, repeatedly, and is HELD each time.
            held = _read_until(connection, "drain_timeout")
            assert held["terminal"] is False
            assert held["held_by_chat_turns"] == 1
            assert held["chat_turn_request_ids"] == ["conn-1:turn-1"]
            assert held["deadline_seconds"] == 0.2

            # Nothing was killed while it was held — the whole point.
            assert exits == []
            connection.send({"op": "ping"})
            assert _read_until(connection, "busy")["chat_turns"] == 1

            # A second lapse proves it re-arms rather than stopping at one.
            second = _read_until(connection, "drain_timeout")
            assert second["terminal"] is False
            assert second["deadline_holds"] >= 2

            # And now the turn is allowed to LAND.
            release.set()
            assert _read_until(connection, "exit") == {
                "id": "turn-1",
                "event": "exit",
                "code": 0,
            }
            complete = _read_until(connection, "drain_complete")
            assert complete["requests_completed"] == 1
            assert complete["deadline_holds"] >= 2

    # A clean drain, never a forced kill.
    assert exits == [] or exits == [0]
    assert handle.code == 0


def test_a_drain_refuses_new_connections_while_it_finishes():
    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    with running_serve(dispatch=_dispatch) as handle:
        with client(handle, name="worker") as (worker, _r1):
            worker.send({"id": "slow-1", "argv": ["harness", "status"]})
            assert started.wait(WAIT)
            worker.send({"op": "drain", "force": True, "deadline_seconds": 10})
            assert _read_until(worker, "draining")["pending"] == 1

            # New work is refused on the socket exactly as it is on stdio —
            # typed, plus the terminal exit a waiting client needs.
            worker.send({"id": "late-1", "argv": ["harness", "status"]})
            refusal = _read_until(worker, "draining")
            assert refusal["id"] == "late-1"
            refused_exit = _read_until(worker, "exit")
            assert refused_exit == {
                "id": "late-1",
                "event": "exit",
                "code": serve_module.DRAINING_EXIT_CODE,
                "draining": True,
            }

            latecomer = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
            try:
                latecomer.connect()
            except OSError:
                # The listener is already closed — the strongest form of refusal.
                pass
            else:
                try:
                    frame = latecomer.read_frame()
                    # Refused BEFORE the challenge, which is stronger than
                    # refused after it: a draining lane does not mint a nonce
                    # for a peer it has already decided not to serve.
                    assert frame is None or frame == {
                        "event": "hello_rejected",
                        "reason": REJECT_DRAINING,
                    }
                finally:
                    latecomer.close()

            release.set()
            complete = _read_until(worker, "drain_complete")
            # The in-flight request was allowed to LAND, which is the whole
            # difference between a drain and a kill.
            assert complete["requests_completed"] == 1


def test_two_connections_asking_to_drain_at_once_start_exactly_one_drain():
    """F7: the ``drain_state`` transition is a guarded critical section.

    It was a bare read-modify-write on a closure variable — harmless while the
    only caller was the single stdio reader, and a genuine race the moment N
    connection threads could ask. Two of them could both observe ``None``, both
    install a ``_DrainState``, and the process would run two monitors, publish
    two terminal frames, and split its counters across two objects.

    The window is normally microseconds, so it is widened here to 250ms INSIDE
    the state's own constructor — which is inside the critical section, so with
    the lock the loser simply waits and is told ``drain_in_progress``, and
    without it the loser has already decided ``None`` and proceeds to install a
    second state of its own.
    """

    original_init = serve_module._DrainState.__init__

    def _slow_init(self, deadline_seconds):
        time.sleep(0.25)
        original_init(self, deadline_seconds)

    started = threading.Event()
    release = threading.Event()

    def _dispatch(argv):
        started.set()
        release.wait(WAIT)
        return 0

    serve_module._DrainState.__init__ = _slow_init
    try:
        with running_serve(dispatch=_dispatch, drain_poll_interval_seconds=0.01) as handle:
            with client(handle, name="first") as (first, _r1), client(handle, name="second") as (second, _r2):
                # Real work in flight, so the drain cannot finish before both
                # asks land and the race is actually run.
                first.send({"id": "slow-1", "argv": ["harness", "status"]})
                assert started.wait(WAIT)

                seen: dict[str, list[dict]] = {"first": [], "second": []}

                def _drain_reader(name, connection):
                    while True:
                        try:
                            frame = connection.read_frame()
                        except Exception:
                            return
                        if frame is None:
                            return
                        seen[name].append(frame)

                readers = [
                    threading.Thread(
                        target=_drain_reader, args=(name, connection), daemon=True
                    )
                    for name, connection in (("first", first), ("second", second))
                ]
                for thread in readers:
                    thread.start()

                barrier = threading.Barrier(2)
                errors: list[BaseException] = []

                def _ask(connection):
                    try:
                        barrier.wait(WAIT)
                        connection.send(
                            {"op": "drain", "force": True, "deadline_seconds": 30}
                        )
                    except BaseException as exc:  # noqa: BLE001 - reported below
                        errors.append(exc)

                askers = [
                    threading.Thread(target=_ask, args=(connection,), daemon=True)
                    for connection in (first, second)
                ]
                for thread in askers:
                    thread.start()
                for thread in askers:
                    thread.join(WAIT)
                assert not errors, errors

                # The loser is TOLD, by name, on its own connection.
                def _refusals():
                    return [
                        row
                        for rows in seen.values()
                        for row in rows
                        if row.get("event") == "drain_in_progress"
                    ]

                deadline = time.monotonic() + WAIT
                while time.monotonic() < deadline and not _refusals():
                    time.sleep(0.02)
                assert len(_refusals()) == 1

                release.set()
                completions = _poll(
                    handle.sink,
                    lambda rows: [r for r in rows if r.get("event") == "drain_complete"],
                )
                # Exactly ONE drain started, exactly ONE terminal frame, and its
                # counters are not split across two states: the single request
                # landed and is counted once.
                announcements = [
                    row
                    for row in handle.sink.frames()
                    if row.get("event") == "draining" and row.get("id") is None
                ]
                assert len(announcements) == 1, (
                    f"{len(announcements)} drains started — the transition is "
                    "not a critical section"
                )
                assert len(completions) == 1
                assert completions[0]["requests_completed"] == 1
    finally:
        release.set()
        serve_module._DrainState.__init__ = original_init


# ── fingerprint exclusion ───────────────────────────────────────────────────


def test_the_socket_files_move_no_freshness_fingerprint():
    """A file that appears at first boot must not cold the read-model cache or
    make the stream emit ``state.reconciled`` on every restart. Same standing
    precedent as ``dispatch_delivery.DRAIN_STATE_FILENAME``."""

    from agent_runtime import stream as stream_module

    root = _store_root()
    root.mkdir(parents=True, exist_ok=True)
    before = (
        serve_module._runtime_state_fingerprint(),
        stream_module._scope_fingerprint(),
    )
    lock = SocketOwnerLock(root)
    assert lock.acquire().acquired is True
    lock.publish_owner({"pid": 1, "port": 12345})
    try:
        assert socket_lock_path(root).exists()
        assert socket_owner_path(root).exists()
        after = (
            serve_module._runtime_state_fingerprint(),
            stream_module._scope_fingerprint(),
        )
        assert after == before
    finally:
        lock.release()
