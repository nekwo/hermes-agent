"""The socket lane: one durable, multi-client serve per runtime root.

What this module owns
---------------------

Transport mechanism only — bind, accept, authenticate, frame, count, close —
plus the ONE-OWNER lock that decides which serve gets to run the lane for a
root. It owns no protocol policy: every authenticated line is handed to the
injected ``dispatch_line`` callback, which is the SAME dispatcher the stdio
lane feeds. One dispatcher, N transports; a second copy of the op table is
exactly the duplicated-authority shape this stack keeps retiring.

Why localhost TCP and not a named pipe
--------------------------------------

A Windows named pipe would give a real ACL — the correct control — but only
through ``pywin32``, a dependency this runtime does not have and will not take
on for one transport slice. Loopback TCP on an EPHEMERAL port is available
everywhere Python is, keeps macOS/Linux on the same code path, and is reachable
by any local process — which is precisely why the hello handshake below is
mandatory and why the per-root token landed one slice EARLIER than this file
(``agent_runtime/serve_auth.py``). ``SO_REUSEADDR`` is deliberately NOT set: on
Windows it permits a second process to bind a port already in use, which would
turn "the address is taken" into a silent hijack.

Auth is first, and it is the only thing that is first
-----------------------------------------------------

The first line a client sends MUST be
``{"op":"hello","token":…,"client":…,"client_build":…}``. Before that line is
verified, a connection can do exactly nothing: no ops, no subscription, no
answers. A wrong or missing token gets ONE typed ``hello_rejected`` frame and
the connection is closed; repeated failures trip a rate limiter, so a local
process cannot grind the 256-bit token by reconnecting. The token value never
appears in a frame, a log line, an error, or a registry entry — the connection
records only whether verification passed.

One owner per root
------------------

Two serves can legitimately run against one root (a launcher restart overlaps
its replacement; a QA lane spawns its own). Only one may own the socket, or
"connect to the service for root X" has two answers. The winner is decided by
an OS-held exclusive lock on ``<store_root>/serve_socket.lock``, held for the
process's lifetime, following the same ``msvcrt.locking`` / ``fcntl.flock``
pattern as ``agent_runtime/locks.py``. The loser does not fail and does not
retry: it runs stdio-only and SAYS SO on its ready frame
(``socket: {"outcome": "lock_held_by", "pid": …}``). A silent degrade here
would be indistinguishable from a socket that never worked.

The holder's identity lives in a sidecar, ``serve_socket.owner.json``, and not
in the lock file itself: on Windows ``msvcrt.locking`` is a MANDATORY lock, so
a loser cannot read the bytes of the file it just lost.

Fingerprint exclusion (load-bearing)
------------------------------------

``serve_socket.lock`` and ``serve_socket.owner.json`` MUST NOT be added to any
freshness fingerprint — not serve's ``_FINGERPRINT_ROOT_FILES`` /
``_FINGERPRINT_STORE_DIRS``, not ``stream._scope_fingerprint``. They appear at
the first socket boot and vanish on every clean exit, which inside a
fingerprint would cold the read-model cache exactly when a fresh runtime is
warming up and make the stream emit ``state.reconciled`` on every restart. Same
standing precedent as ``dispatch_delivery.DRAIN_STATE_FILENAME``,
``serve_auth.SERVE_AUTH_TOKEN_FILENAME``, and ``serve_instances/``.

Root as INPUT
-------------

Every entry point takes ``store_root``. This module never resolves a root and
never reads ``HERMES_HOME``: multiple roots coexist on this machine, and a
transport free to re-derive its own root could bind a socket for one root while
answering from another — silently, because both answers would be well-formed.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

if os.name == "nt":  # pragma: no cover - platform split, both sides exercised in CI
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl

__all__ = [
    "DEFAULT_MAX_CONNECTIONS",
    "HELLO_FAILURE_LIMIT",
    "HELLO_FAILURE_WINDOW_SECONDS",
    "SOCKET_HOST",
    "SOCKET_LOCK_FILENAME",
    "SOCKET_OWNER_FILENAME",
    "HelloRateLimiter",
    "ServeSocketClient",
    "ServeSocketServer",
    "SocketLockResult",
    "SocketOwnerLock",
    "SocketTarget",
    "read_socket_owner",
    "resolve_socket_target",
    "socket_lock_path",
    "socket_owner_path",
]

#: Loopback only. Never configurable: a knob here can only ever widen exposure,
#: and this runtime executes agents with tools.
SOCKET_HOST = "127.0.0.1"

SOCKET_LOCK_FILENAME = "serve_socket.lock"
SOCKET_OWNER_FILENAME = "serve_socket.owner.json"

#: Connections a single serve will hold. A durable service is meant to be
#: multi-client, not unbounded: every connection costs a reader thread, and an
#: unbounded accept loop is a local denial of service against the runtime.
DEFAULT_MAX_CONNECTIONS = 32

#: Failed hellos tolerated inside the window before every new connection is
#: rejected outright. The token is 256 bits, so this is not what makes guessing
#: infeasible — it is what stops a local process from spending the runtime's
#: threads and log volume trying.
HELLO_FAILURE_LIMIT = 5
HELLO_FAILURE_WINDOW_SECONDS = 10.0
#: Charged to the REJECTED connection's own thread, never to the accept loop.
HELLO_REJECT_PENALTY_SECONDS = 0.25
#: How long a rejected connection half-closes and drains before the final close,
#: so the rejection frame survives (see ``SocketConnection.close``).
_REJECT_LINGER_SECONDS = 0.25

#: A connection that has not said hello by then is not a client.
HELLO_DEADLINE_SECONDS = 5.0
#: Socket timeout after the handshake. Receive timeouts are a normal idle lap;
#: a SEND timeout is fatal to that connection — a write that cannot land inside
#: this budget is a reader that has stopped reading, and a partially written
#: frame cannot be resynchronised.
IO_TIMEOUT_SECONDS = 10.0
#: Hard bound on a single client line. The ops are small JSON objects; anything
#: past this is a client streaming garbage at the runtime.
MAX_LINE_BYTES = 1 << 20

LOCK_OUTCOME_ACQUIRED = "acquired"
LOCK_OUTCOME_HELD = "lock_held_by"


# ── the one-owner lock ───────────────────────────────────────────────────────


def socket_lock_path(store_root: Path | str) -> Path:
    return Path(store_root) / SOCKET_LOCK_FILENAME


def socket_owner_path(store_root: Path | str) -> Path:
    return Path(store_root) / SOCKET_OWNER_FILENAME


@dataclass(frozen=True, slots=True)
class SocketLockResult:
    """Who owns the socket lane for this root, and how we know."""

    #: ``acquired`` | ``lock_held_by`` | ``error:<reason>``
    outcome: str
    #: The CURRENT holder's pid when known — ours on ``acquired``, the winner's
    #: (from the sidecar) on ``lock_held_by``, None when the sidecar is absent.
    pid: int | None
    path: str

    @property
    def acquired(self) -> bool:
        return self.outcome == LOCK_OUTCOME_ACQUIRED

    def payload(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "pid": self.pid, "path": self.path}


class SocketOwnerLock:
    """An exclusive OS lock held for the life of the process.

    Non-blocking by design. A serve that loses this race has a job to do
    (stdio) and must not spend its boot waiting for a lock whose holder is
    healthy — the loser degrades loudly instead.
    """

    def __init__(self, store_root: Path | str) -> None:
        self._path = socket_lock_path(store_root)
        self._owner_path = socket_owner_path(store_root)
        self._handle = None
        self._acquired = False
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> SocketLockResult:
        with self._lock:
            if self._acquired:
                return SocketLockResult(
                    outcome=LOCK_OUTCOME_ACQUIRED, pid=os.getpid(), path=str(self._path)
                )
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(self._path, "a+b")
            except OSError as exc:
                return SocketLockResult(
                    outcome=f"error:{_os_error_token(exc)}",
                    pid=None,
                    path=str(self._path),
                )
            try:
                _lock_first_byte(handle)
            except _LockUnavailable:
                handle.close()
                owner = read_socket_owner(self._owner_path.parent)
                return SocketLockResult(
                    outcome=LOCK_OUTCOME_HELD,
                    pid=_int_or_none(owner.get("pid")) if owner else None,
                    path=str(self._path),
                )
            except OSError as exc:
                handle.close()
                return SocketLockResult(
                    outcome=f"error:{_os_error_token(exc)}",
                    pid=None,
                    path=str(self._path),
                )
            self._handle = handle
            self._acquired = True
            return SocketLockResult(
                outcome=LOCK_OUTCOME_ACQUIRED, pid=os.getpid(), path=str(self._path)
            )

    def publish_owner(self, record: dict[str, Any]) -> None:
        """Write the identity sidecar. Best effort; the LOCK is the authority."""

        if not self._acquired:
            return
        try:
            _write_json_atomic(self._owner_path, dict(record))
        except Exception:
            pass

    def release(self) -> None:
        """Unlock, close, and remove both files. Idempotent; never raises."""

        with self._lock:
            handle, self._handle = self._handle, None
            was_acquired, self._acquired = self._acquired, False
        if handle is not None:
            try:
                _unlock_first_byte(handle)
            except Exception:
                pass
            try:
                handle.close()
            except Exception:
                pass
        if not was_acquired:
            return
        # Order matters on Windows: the handle above must be closed before the
        # file can be unlinked at all.
        for path in (self._owner_path, self._path):
            try:
                path.unlink()
            except OSError:
                pass


def read_socket_owner(store_root: Path | str) -> dict[str, Any]:
    """The published owner sidecar for *store_root*, or ``{}``.

    Advisory by contract: the sidecar is how a client DISCOVERS the port, and
    how a lock loser names the winner. It is never proof of liveness — that is
    the registry's read-time classification, and the hello handshake's.
    """

    try:
        raw = socket_owner_path(store_root).read_bytes().decode("utf-8", "replace")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── hello rate limiting ──────────────────────────────────────────────────────


class HelloRateLimiter:
    """Sliding-window failure counter. Clock injected so it is unit-testable."""

    def __init__(
        self,
        *,
        limit: int = HELLO_FAILURE_LIMIT,
        window_seconds: float = HELLO_FAILURE_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = max(1, int(limit))
        self._window = float(window_seconds)
        self._clock = clock
        self._failures: list[float] = []
        self._lock = threading.Lock()

    def blocked(self) -> bool:
        with self._lock:
            self._evict()
            return len(self._failures) >= self._limit

    def record_failure(self) -> int:
        with self._lock:
            self._evict()
            self._failures.append(self._clock())
            return len(self._failures)

    def record_success(self) -> None:
        """A good hello clears the window: this bounds abuse, not real clients."""

        with self._lock:
            self._failures.clear()

    def _evict(self) -> None:
        cutoff = self._clock() - self._window
        self._failures = [item for item in self._failures if item >= cutoff]


# ── connections ──────────────────────────────────────────────────────────────


@dataclass
class SocketConnection:
    """One authenticated (or pending) client on the socket lane."""

    key: str
    sock: Any
    peer: str
    connected_at: str
    client: str | None = None
    client_build: str | None = None
    authenticated: bool = False
    subscribed: bool = False
    frames_out: int = 0
    bytes_out: int = 0
    closed: bool = False
    close_reason: str | None = None
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _stats_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    #: The transport name every frame this connection answers is tagged with.
    transport: str = "socket"

    def emit(self, frame: dict[str, Any], *, lock_timeout: float | None = None) -> None:
        """Write ONE NDJSON frame. Atomic per connection, mirroring _FrameWriter.

        Raises on a dead or wedged socket — the caller (a pump thread, a pool
        worker) decides what that means for its own lane.

        ``lock_timeout`` bounds the wait for the per-connection write lock. It
        exists for the BROADCAST path: a subscriber whose ``sendall`` is parked
        against a reader that stopped reading holds this lock for up to the
        socket timeout, and a drain announcement must not queue behind it.
        """

        payload = json.dumps(frame, ensure_ascii=False, default=str) + "\n"
        data = payload.encode("utf-8")
        if lock_timeout is None:
            acquired = self._write_lock.acquire()
        else:
            acquired = self._write_lock.acquire(timeout=lock_timeout)
        if not acquired:
            raise TimeoutError("connection write lock is busy")
        try:
            if self.closed:
                raise ConnectionError("connection closed")
            self.sock.sendall(data)
        finally:
            self._write_lock.release()
        with self._stats_lock:
            self.frames_out += 1
            self.bytes_out += len(data)

    def try_emit(self, frame: dict[str, Any], *, lock_timeout: float = 0.5) -> bool:
        try:
            self.emit(frame, lock_timeout=lock_timeout)
            return True
        except Exception:
            return False

    def payload(self) -> dict[str, Any]:
        with self._stats_lock:
            return {
                "connection": self.key,
                "client": self.client,
                "client_build": self.client_build,
                "authenticated": self.authenticated,
                "subscribed": self.subscribed,
                "connected_at": self.connected_at,
                "frames_out": self.frames_out,
                "bytes_out": self.bytes_out,
            }

    def close(self, reason: str | None = None, *, linger_seconds: float = 0.0) -> None:
        # Deliberately NOT under the write lock: a connection is closed exactly
        # when a writer may be stuck inside ``sendall``, and waiting for that
        # lock would make teardown as slow as the wedged client. Shutting the
        # socket down is what unblocks that writer.
        with self._stats_lock:
            if self.closed:
                return
            self.closed = True
            self.close_reason = reason
        if linger_seconds > 0:
            # Closing a socket that still has UNREAD data in its receive buffer
            # makes the stack send an RST instead of a FIN, and an RST can
            # discard data already queued for the peer — so the client loses the
            # very frame that explains why it was closed. This is not
            # theoretical: a rejected client pipelines its next op immediately,
            # which is exactly the unread data that triggers it.
            #
            # Half-close (FIN out, keep reading), drain briefly, then close.
            try:
                self.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            deadline = time.monotonic() + float(linger_seconds)
            try:
                self.sock.settimeout(0.05)
                while time.monotonic() < deadline:
                    if not self.sock.recv(65536):
                        break
            except OSError:
                pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# ── the server ───────────────────────────────────────────────────────────────


class ServeSocketServer:
    """Bind, accept, authenticate, and feed the shared dispatcher.

    Two-phase start on purpose. :meth:`bind` happens EARLY — before the serve
    registry entry and the ready frame, so both can carry the real port — while
    :meth:`start_accepting` happens after the request pool exists. A client that
    connects in between waits in the listen backlog, which is exactly what a
    backlog is for; dispatching a request into a pool that does not exist yet is
    not.
    """

    def __init__(
        self,
        store_root: Path | str,
        *,
        boot_id: str,
        dispatch_line: Callable[[str, SocketConnection], Any],
        hello_payload: Callable[[dict[str, Any], SocketConnection], dict[str, Any]],
        verify_token: Callable[[str | None], bool],
        on_disconnect: Callable[[SocketConnection], None] | None = None,
        log: Callable[[dict[str, Any]], None] | None = None,
        host: str = SOCKET_HOST,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        rate_limiter: HelloRateLimiter | None = None,
        hello_deadline_seconds: float = HELLO_DEADLINE_SECONDS,
        io_timeout_seconds: float = IO_TIMEOUT_SECONDS,
        reject_penalty_seconds: float = HELLO_REJECT_PENALTY_SECONDS,
    ) -> None:
        self._store_root = Path(store_root)
        self._boot_id = str(boot_id)
        self._dispatch_line = dispatch_line
        self._hello_payload = hello_payload
        self._verify_token = verify_token
        self._on_disconnect = on_disconnect
        self._log = log
        self._host = host
        self._max_connections = max(1, int(max_connections))
        self._rate_limiter = rate_limiter or HelloRateLimiter()
        self._hello_deadline = float(hello_deadline_seconds)
        self._io_timeout = float(io_timeout_seconds)
        self._reject_penalty = float(reject_penalty_seconds)

        self._listener: socket.socket | None = None
        self._port: int | None = None
        self._accept_thread: threading.Thread | None = None
        self._connections: dict[str, SocketConnection] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._draining = False
        self._next_connection = 0
        self._accepted = 0
        self._rejected = 0
        self._started_at: str | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def started_at(self) -> str | None:
        return self._started_at

    def bind(self) -> int:
        """Bind an ephemeral loopback port and listen. Returns the port."""

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # No SO_REUSEADDR — see the module docstring.
        listener.bind((self._host, 0))
        listener.listen(max(8, self._max_connections))
        self._listener = listener
        self._port = int(listener.getsockname()[1])
        self._started_at = _now_iso()
        return self._port

    def start_accepting(self) -> None:
        if self._listener is None:
            raise RuntimeError("bind() must run before start_accepting()")
        if self._accept_thread is not None:
            return
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="harness-serve-socket-accept", daemon=True
        )
        self._accept_thread.start()

    def begin_drain(self) -> None:
        """Stop accepting NEW connections; existing ones stay up to be told.

        The refusal of new OPS is not this object's decision — it belongs to the
        shared dispatcher, which already refuses every request the same way on
        both transports. Duplicating that rule here is how two transports start
        disagreeing about what draining means.
        """

        with self._lock:
            if self._draining:
                return
            self._draining = True
        self._close_listener()
        self._emit_log({"event": "serve_socket_draining"})

    def broadcast(self, frame: dict[str, Any]) -> int:
        """Send *frame* to every authenticated connection. Returns the count."""

        delivered = 0
        for connection in self.connections():
            if not connection.authenticated:
                continue
            if connection.try_emit(frame):
                delivered += 1
        return delivered

    def close(self, reason: str = "shutdown") -> None:
        """Stop accepting, close every connection, and release nothing else.

        The LOCK is not released here: its owner is the serve loop, which holds
        it for the process's lifetime and drops it as the last act of the drain.
        """

        self._stop.set()
        self._close_listener()
        for connection in self.connections():
            self._drop_connection(connection, reason=reason)
        thread = self._accept_thread
        if thread is not None and thread.is_alive():
            thread.join(2.0)

    # ── introspection ───────────────────────────────────────────────────────

    def connections(self) -> list[SocketConnection]:
        with self._lock:
            return list(self._connections.values())

    def connections_payload(self) -> dict[str, Any]:
        rows = [connection.payload() for connection in self.connections()]
        with self._lock:
            accepted, rejected, draining = self._accepted, self._rejected, self._draining
        return {
            "port": self._port,
            "host": self._host,
            "count": len(rows),
            "max_connections": self._max_connections,
            "accepted_total": accepted,
            "rejected_total": rejected,
            "draining": draining,
            "connections": rows,
        }

    # ── accept loop ─────────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                sock, peer = listener.accept()
            except OSError as exc:
                if self._stop.is_set() or self._listener is None:
                    return
                # A typed line, never process death: an accept that fails on a
                # transient OS condition must not take the runtime with it.
                self._emit_log(
                    {"event": "serve_socket_accept_error", "reason": type(exc).__name__}
                )
                if _is_fatal_accept_error(exc):
                    return
                time.sleep(0.05)
                continue
            threading.Thread(
                target=self._serve_connection,
                args=(sock, peer),
                name="harness-serve-socket-conn",
                daemon=True,
            ).start()

    def _serve_connection(self, sock: socket.socket, peer: Any) -> None:
        with self._lock:
            self._next_connection += 1
            key = f"conn-{self._next_connection}"
            at_capacity = len(self._connections) >= self._max_connections
        connection = SocketConnection(
            key=key, sock=sock, peer=_peer_text(peer), connected_at=_now_iso()
        )
        try:
            sock.settimeout(self._hello_deadline)
        except OSError:
            pass
        if at_capacity:
            self._reject(connection, "too_many_connections")
            return
        if self._rate_limiter.blocked():
            self._reject(connection, "rate_limited")
            return
        if self._draining:
            self._reject(connection, "draining")
            return
        reader = _LineReader(sock)
        try:
            hello_line = reader.read_line(deadline_seconds=self._hello_deadline)
        except Exception:
            hello_line = None
        if hello_line is None:
            self._reject(connection, "hello_timeout", count_failure=False)
            return
        message = _parse_object(hello_line)
        if message is None or message.get("op") != "hello":
            self._reject(connection, "hello_required")
            return
        token = message.get("token")
        if not isinstance(token, str) or not self._verify_token(token):
            # ONE typed frame, and nothing else: a rejected connection never
            # learns anything about the runtime it failed to reach.
            self._reject(connection, "bad_token")
            return
        self._rate_limiter.record_success()
        connection.client = _client_text(message.get("client"))
        connection.client_build = _client_text(message.get("client_build"))
        connection.authenticated = True
        with self._lock:
            self._connections[key] = connection
            self._accepted += 1
        try:
            sock.settimeout(self._io_timeout)
        except OSError:
            pass
        self._emit_log(
            {
                "event": "serve_socket_connection_open",
                "connection": key,
                "client": connection.client,
                "client_build": connection.client_build,
                "peer": connection.peer,
            }
        )
        try:
            connection.emit(self._hello_payload(message, connection))
        except Exception:
            self._drop_connection(connection, reason="hello_write_failed")
            return
        self._read_loop(connection, reader)

    def _read_loop(self, connection: SocketConnection, reader: "_LineReader") -> None:
        reason = "client_disconnect"
        try:
            while not self._stop.is_set() and not connection.closed:
                try:
                    line = reader.read_line(deadline_seconds=None)
                except socket.timeout:  # noqa: UP041 - alias differs across versions
                    continue
                except OSError as exc:
                    reason = f"read_error:{type(exc).__name__}"
                    break
                if line is None:
                    break
                if not line.strip():
                    continue
                try:
                    self._dispatch_line(line, connection)
                except Exception as exc:  # a handler fault is not a process fault
                    self._emit_log(
                        {
                            "event": "serve_socket_dispatch_error",
                            "connection": connection.key,
                            "client": connection.client,
                            "reason": type(exc).__name__,
                        }
                    )
        except _LineTooLong:
            reason = "line_too_long"
        finally:
            self._drop_connection(connection, reason=reason)

    # ── connection teardown / rejection ─────────────────────────────────────

    def _reject(
        self, connection: SocketConnection, reason: str, *, count_failure: bool = True
    ) -> None:
        if count_failure:
            self._rate_limiter.record_failure()
        with self._lock:
            self._rejected += 1
        connection.try_emit({"event": "hello_rejected", "reason": reason})
        self._emit_log(
            {
                "event": "serve_socket_connection_rejected",
                "connection": connection.key,
                "client": connection.client,
                "peer": connection.peer,
                "reason": reason,
            }
        )
        if self._reject_penalty > 0:
            # Charged here, on the rejected connection's own thread — the accept
            # loop stays responsive for legitimate clients.
            time.sleep(self._reject_penalty)
        # Lingering close: a rejected client has almost always pipelined its
        # next op already, and closing on top of that unread data would RST the
        # connection and can destroy the rejection frame in flight. The typed
        # reason IS the observability of an auth failure; losing it would leave
        # the peer with an unexplained disconnect.
        connection.close(reason, linger_seconds=_REJECT_LINGER_SECONDS)

    def _drop_connection(self, connection: SocketConnection, *, reason: str) -> None:
        with self._lock:
            existed = self._connections.pop(connection.key, None) is not None
        connection.close(reason)
        if not existed:
            return
        if self._on_disconnect is not None:
            try:
                self._on_disconnect(connection)
            except Exception:
                pass
        self._emit_log(
            {
                "event": "serve_socket_connection_close",
                "connection": connection.key,
                "client": connection.client,
                "reason": reason,
                "frames_out": connection.frames_out,
                "bytes_out": connection.bytes_out,
            }
        )

    def _close_listener(self) -> None:
        listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.close()
        except OSError:
            pass

    def _emit_log(self, payload: dict[str, Any]) -> None:
        if self._log is None:
            return
        try:
            self._log({"boot_id": self._boot_id, **payload})
        except Exception:  # pragma: no cover - an instrument may not raise
            pass


# ── discovery + client ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SocketTarget:
    """Where the socket service for a root is, and how confidently we know."""

    host: str
    port: int
    pid: int | None
    boot_id: str | None
    #: ``registry`` (an entry the registry classified) or ``owner_file`` (the
    #: sidecar, when the registry could not answer).
    source: str
    #: The registry's read-time classification, or None when the sidecar was
    #: the source. ``live`` is the only value that is evidence of a process;
    #: everything else is why a connect may fail.
    classification: str | None

    def payload(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "source": self.source,
            "classification": self.classification,
        }


def resolve_socket_target(store_root: Path | str, *, probe: Any = None) -> SocketTarget | None:
    """Find the live socket service for *store_root*, or None.

    The REGISTRY is asked first, because it is the only source that classifies
    liveness at read time (a dead pid, a recycled pid, and an unreadable probe
    are three different answers there, and none of them is "live"). The owner
    sidecar is the fallback: it is written by the lock holder and is precise
    while that process lives, but it cannot prove the process still does.
    """

    try:
        from .serve_registry import CLASSIFICATION_LIVE, list_serve_instances

        rows = list_serve_instances(store_root, probe=probe)
    except Exception:
        rows = []
    candidates = [
        row
        for row in rows
        if _int_or_none(row.get("port")) and "socket" in str(row.get("transport") or "")
    ]
    live = [row for row in candidates if row.get("classification") == CLASSIFICATION_LIVE]
    for row in live or candidates:
        port = _int_or_none(row.get("port"))
        if port is None:  # pragma: no cover - filtered above
            continue
        return SocketTarget(
            host=SOCKET_HOST,
            port=port,
            pid=_int_or_none(row.get("pid")),
            boot_id=row.get("boot_id") if isinstance(row.get("boot_id"), str) else None,
            source="registry",
            classification=str(row.get("classification") or ""),
        )
    owner = read_socket_owner(store_root)
    port = _int_or_none(owner.get("port"))
    if port is None:
        return None
    return SocketTarget(
        host=str(owner.get("host") or SOCKET_HOST),
        port=port,
        pid=_int_or_none(owner.get("pid")),
        boot_id=owner.get("boot_id") if isinstance(owner.get("boot_id"), str) else None,
        source="owner_file",
        classification=None,
    )


class ServeSocketClient:
    """The other end of the handshake: connect, hello, then read frames.

    Deliberately small and dependency-free — it is the reference client the CLI
    probe verb uses, and the shape the Launcher's own client will mirror when it
    migrates off the stdio child.
    """

    def __init__(self, host: str, port: int, *, timeout_seconds: float = 10.0) -> None:
        self._host = host
        self._port = int(port)
        self._timeout = float(timeout_seconds)
        self._sock: socket.socket | None = None
        self._reader: _LineReader | None = None

    def connect(self) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        self._sock = sock
        self._reader = _LineReader(sock)

    def hello(
        self,
        *,
        token: str,
        client: str,
        client_build: str | None = None,
    ) -> dict[str, Any] | None:
        """Send the hello and return the single reply frame.

        The token goes on the wire and NOWHERE else: it is not logged, not
        echoed into the returned frame, and not retained on this object.
        """

        self.send(
            {
                "op": "hello",
                "token": token,
                "client": client,
                "client_build": client_build,
            }
        )
        return self.read_frame()

    def send(self, message: dict[str, Any]) -> None:
        if self._sock is None:
            raise RuntimeError("connect() first")
        payload = json.dumps(message, ensure_ascii=False, default=str) + "\n"
        self._sock.sendall(payload.encode("utf-8"))

    def read_frame(self) -> dict[str, Any] | None:
        if self._reader is None:
            raise RuntimeError("connect() first")
        while True:
            line = self._reader.read_line(deadline_seconds=None)
            if line is None:
                return None
            if not line.strip():
                continue
            return _parse_object(line)

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._reader = None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def __enter__(self) -> "ServeSocketClient":
        self.connect()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# ── line framing ─────────────────────────────────────────────────────────────


class _LineTooLong(Exception):
    pass


class _LineReader:
    """NDJSON line assembly over a byte stream, with a hard per-line bound."""

    def __init__(self, sock: Any, *, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self._sock = sock
        self._buffer = b""
        self._max = int(max_line_bytes)

    def read_line(self, *, deadline_seconds: float | None) -> str | None:
        """Next line, or None at end of stream.

        ``deadline_seconds`` bounds the WHOLE read (the hello phase); None means
        "wait as long as the socket's own timeout allows", and a socket timeout
        propagates so the caller can treat it as an idle lap.
        """

        deadline = None if deadline_seconds is None else time.monotonic() + deadline_seconds
        while True:
            if b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                return line.decode("utf-8", errors="replace")
            if len(self._buffer) > self._max:
                raise _LineTooLong()
            if deadline is not None and time.monotonic() >= deadline:
                return None
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:  # noqa: UP041
                if deadline is None:
                    raise
                continue
            except ConnectionResetError:
                # A peer that closed rudely (RST) is a peer that is gone. The
                # distinction between FIN and RST is not one any caller of this
                # reader can act on differently, and raising it would turn an
                # ordinary disconnect into an error path.
                chunk = b""
            if not chunk:
                if self._buffer:
                    line, self._buffer = self._buffer, b""
                    return line.decode("utf-8", errors="replace")
                return None
            self._buffer += chunk


# ── helpers ──────────────────────────────────────────────────────────────────


class _LockUnavailable(Exception):
    pass


def _lock_first_byte(handle) -> None:
    """Exclusive, NON-BLOCKING lock on byte 0 — the locks.py pattern.

    The file is padded to one byte first because ``msvcrt.locking`` cannot lock
    a region of an empty file.
    """

    if os.name == "nt":
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EDEADLK, 13, 36}:
                raise _LockUnavailable() from exc
            raise
        return
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
            raise _LockUnavailable() from exc
        raise


def _unlock_first_byte(handle) -> None:
    if os.name == "nt":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_json_atomic(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, ensure_ascii=False, default=str, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            handle.write(payload)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _parse_object(line: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _client_text(value: Any) -> str | None:
    """A client-supplied label, bounded and flattened.

    Client-controlled text lands in log lines and in ``harness status`` output,
    so it is capped and stripped of anything that could forge a second line.
    """

    if not isinstance(value, str):
        return None
    flattened = " ".join(value.split())
    return flattened[:64] or None


def _peer_text(peer: Any) -> str:
    try:
        return f"{peer[0]}:{peer[1]}"
    except Exception:
        return str(peer)


def _is_fatal_accept_error(exc: OSError) -> bool:
    return exc.errno in {errno.EBADF, errno.EINVAL, errno.ENOTSOCK}


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _os_error_token(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "root_missing"
    if isinstance(exc, NotADirectoryError):
        return "root_not_a_directory"
    return type(exc).__name__


def _now_iso() -> str:
    return (
        datetime.now(tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
