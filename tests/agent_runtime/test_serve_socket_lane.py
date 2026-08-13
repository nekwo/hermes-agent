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
    ServeSocketClient,
    SocketOwnerLock,
    read_socket_owner,
    socket_lock_path,
    socket_owner_path,
)
from hermes_cli.harness_parts import serve as serve_module
from hermes_cli.harness_parts.serve import serve_loop

WAIT = 15.0


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


def test_a_wrong_token_gets_one_typed_rejection_and_no_data_at_all():
    """The anti-vacuity test for auth.

    A ``hello_rejected`` frame appearing proves only that SOMETHING was
    refused. What matters is that the peer learned nothing and could do
    nothing: one frame, no build block, no runtime root, no answer to the op it
    sent anyway, and a closed connection.
    """

    with running_serve() as handle:
        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        try:
            connection.send({"op": "hello", "token": "not-the-token", "client": "thief"})
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
            assert received == [{"event": "hello_rejected", "reason": "bad_token"}]
            blob = json.dumps(received)
            for leaked in ("build", "runtime_root", "boot_id", "commit", "version"):
                assert leaked not in blob
        finally:
            connection.close()

        # The refusal did not take the lane down: a real client still works.
        with client(handle, name="legit") as (_conn, reply):
            assert reply["event"] == "hello_ok"


def test_a_missing_or_malformed_hello_is_refused_the_same_way():
    with running_serve() as handle:
        for message, reason in (
            ({"op": "version"}, "hello_required"),
            ({"op": "hello", "client": "no-token"}, "bad_token"),
            ({"op": "hello", "token": 12345, "client": "wrong-type"}, "bad_token"),
        ):
            connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
            connection.connect()
            try:
                connection.send(message)
                assert connection.read_frame() == {
                    "event": "hello_rejected",
                    "reason": reason,
                }
            finally:
                connection.close()


def test_hammering_the_token_trips_the_rate_limit_even_for_a_valid_client():
    """The token is 256 bits; this is what stops a local process from spending
    the runtime's threads and log volume finding that out."""

    from agent_runtime.serve_socket import HELLO_FAILURE_LIMIT

    with running_serve() as handle:
        for _ in range(HELLO_FAILURE_LIMIT):
            connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
            connection.connect()
            try:
                connection.send({"op": "hello", "token": "wrong", "client": "thief"})
                assert connection.read_frame()["reason"] == "bad_token"
            finally:
                connection.close()

        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        try:
            token = read_token(_store_root()) or ""
            connection.send({"op": "hello", "token": token, "client": "legit"})
            # Refused BEFORE the token is even read: the window is closed for
            # everyone, which is the honest cost of a shared front door.
            assert connection.read_frame() == {
                "event": "hello_rejected",
                "reason": "rate_limited",
            }
        finally:
            connection.close()


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
        connection = ServeSocketClient("127.0.0.1", handle.port, timeout_seconds=WAIT)
        connection.connect()
        connection.send({"op": "hello", "token": "wrong", "client": "thief"})
        connection.read_frame()
        connection.close()

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
        assert rejected["reason"] == "bad_token"
        # The rejected peer's own label is recorded; its token is not.
        assert "wrong" not in json.dumps(logs)


# ── drain over the socket ───────────────────────────────────────────────────


def test_a_drain_asked_over_the_socket_tells_every_client_then_closes_the_lane():
    """The live proof slice 2 could not run: a drain observed from OUTSIDE the
    process, by a client that stays attached to watch it finish."""

    from agent_runtime.serve_registry import list_serve_instances

    root = _store_root()
    with running_serve() as handle:
        with client(handle, name="watcher") as (watcher, _r1), client(handle, name="operator") as (operator, _r2):
            operator.send({"op": "drain", "deadline_seconds": 10})

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

        # The lane is released: registry entry gone, lock and sidecar gone.
        deadline = time.monotonic() + WAIT
        while time.monotonic() < deadline and list_serve_instances(root):
            time.sleep(0.05)
        assert list_serve_instances(root) == []
        assert not socket_owner_path(root).exists()
        assert not socket_lock_path(root).exists()

        # And a fresh serve can take the lane over immediately.
        replacement = SocketOwnerLock(root)
        assert replacement.acquire().acquired is True
        replacement.release()

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
            worker.send({"op": "drain", "deadline_seconds": 10})
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
                latecomer.send(
                    {"op": "hello", "token": read_token(_store_root()) or "", "client": "late"}
                )
                frame = latecomer.read_frame()
                assert frame is None or frame.get("event") == "hello_rejected"
                latecomer.close()

            release.set()
            complete = _read_until(worker, "drain_complete")
            # The in-flight request was allowed to LAND, which is the whole
            # difference between a drain and a kill.
            assert complete["requests_completed"] == 1


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
