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

Auth is first, and the token never travels
------------------------------------------

The SERVER speaks first. On accept it writes exactly one frame::

    {"event":"server_hello","nonce":"<64 hex chars>","boot_id":…,
     "contract":<frame schema>,"hello_contract":3,"algorithm":"hmac-sha256"}

and the client answers::

    {"op":"hello","client":…,"client_build":…,
     "proof":"<hex HMAC-SHA256(key=token, msg="v3|<dialled port>|<nonce>")>"}

**The message is not the bare nonce**, and the two spellings above were wrong in
both halves until 2026-08-27: this block said ``hello_contract`` 2 and
``msg=nonce`` while :data:`HELLO_CONTRACT_VERSION` has been 3 and
:func:`hello_proof` has bound the PORT since the relay defence landed. A client
written against the prose computes a proof over the wrong message and is refused
with ``bad_proof``, which is the least debuggable possible failure — it looks
exactly like a wrong credential. Found by the launcher's socket client actually
being built against it (launcher `527940a0e`). :func:`hello_proof` is the
authority; this paragraph is a description of it and must be re-read against it,
never trusted over it.

Before that proof is verified a connection can do exactly nothing: no ops, no
subscription, no answers. A wrong or missing proof gets ONE typed
``hello_rejected`` frame and the connection is closed.

Why a proof and not the token (the hardening this replaced)
    The first cut sent ``{"op":"hello","token":<raw>}``, and discovery could
    hand a client a target it had itself classified ``stale_dead_pid`` — so a
    local impostor that bound the dead serve's port harvested the real token in
    cleartext, live-proven. Two independent halves close that: discovery
    returns LIVE rows only (:func:`resolve_socket_target`, with a non-live row
    reachable exclusively through an explicit ``allow_stale=True`` that carries
    the classification so the caller must refuse it by name), and the token
    itself never traverses the wire at all. A stolen transcript is unreplayable
    — the nonce is fresh per CONNECTION — and an impostor server learns only
    one HMAC, bound to the port the victim DIALLED, which therefore does not
    verify at the real service's port. That binding is load-bearing and was not
    in the first pass: freshness alone stops replay but not a live relay, since
    an impostor can dial the real service, adopt its nonce as its own
    challenge, and forward the answer. Binding to anything the greeting merely
    asserts (`boot_id`, a claimed port) would be binding to a number the
    impostor echoes; the dialled port is the one value each end knows from its
    own socket. The token value still appears in no frame, no log line, no
    error, and no registry entry; the proof is the only derived artifact.

    ``hello_contract`` is versioned on the ``server_hello`` frame precisely so
    the future Launcher client asserts the handshake it was written against
    instead of proceeding on hope. There is no compatibility shim: the socket
    lane has no other clients yet, and a shim that accepted the old token hello
    would keep the cleartext lane open forever.

Repeated AUTH failures trip a rate limiter, so a local process cannot grind the
256-bit secret by reconnecting. Capacity, drain, and handshake-timeout
rejections are NOT auth failures and never touch that limiter — charging them
to it produced a permanent self-sustaining lockout (a ``rate_limited``
rejection re-armed its own window, so 12 polite retries with the RIGHT
credential over 12s never recovered). Handshake timeouts have a throttle of
their own, and pre-hello connections have a bound of their own: counting only
AUTHENTICATED peers against ``max_connections`` let 64 silent sockets sit on
the runtime's threads without a single rejection.

Two listeners, one implementation (Stage 1)
-------------------------------------------

This class binds the LOOPBACK lane, and — when an operator opts in — a second
GATEWAY listener bound beyond loopback. They are the same class with three
constructor arguments filled in, deliberately, because the hardened parts here
are the parts a second copy would get wrong: the accept loop that announces its
own death, the pre-auth bound that counts peers who have proven nothing, the two
rate limiters and the rule that server-state refusals never charge the auth one,
the lingering close that keeps a rejection frame alive. A second listener class
would be a second place all of that has to stay true.

The three arguments, all defaulted to what the loopback lane has always done, so
a server constructed without them is byte-identical to the one that shipped:

* ``port`` — 0 (ephemeral) by default; pinned for an operator who has to write a
  firewall rule and tell a phone a number that survives a restart.
* ``ssl_context`` — ``None`` by default. The loopback lane stays PLAINTEXT and
  that is the local trust model unchanged: a local process that could read the
  token file gains nothing from a TLS layer, and adding one would cost every
  local client a handshake to protect a wire that never leaves the machine. The
  gateway lane is wrapped (R1: encrypt, self-signed per-install certificate,
  fingerprint pinned by the client).
* ``authenticator`` — ``None`` means the per-root token, i.e. the code that was
  inline here before the seam existed. The gateway lane injects a per-DEVICE
  check (``serve_gateway_auth.py``), which is the only way a connection ever
  gets a ``device_id``/``device_tier`` stamp, which is in turn the only way
  ``call_authorization`` mints a ``device`` caller.

What the gateway lane does NOT get is a second dispatcher, a second op table, or
a second hello contract. It answers the same ``server_hello``, over the same
frame vocabulary, into the same ``dispatch_line`` callback. Where it must differ
— the credential, the encryption, the ops it is offered — it differs by an
argument, not by a branch.

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
``_FINGERPRINT_STORE_DIRS``, not ``stream._scope_fingerprint``, and they MUST BE
PRESENT in ``core_cache._EXCLUDED_STORE_ENTRIES``. They appear at
the first socket boot and vanish on every clean exit, which inside a
fingerprint would cold the read-model cache exactly when a fresh runtime is
warming up and make the stream emit ``state.reconciled`` on every restart. Same
standing precedent as ``dispatch_delivery.DRAIN_STATE_FILENAME``,
``serve_auth.SERVE_AUTH_TOKEN_FILENAME``, and ``serve_instances/``.

The sentence above names TWO obligations because there are two fingerprint
designs in this runtime with OPPOSITE defaults, and one doctrine written for the
first silently fails to bind the second. Serve's read-cache key and
``stream._scope_fingerprint`` are ALLOWLISTS — a file nobody enumerates is
already out, so "do not add these" is satisfied by inaction.
``core_cache.build_input_fingerprint`` is a DENYLIST walk of the whole store
root — everything not named in ``_EXCLUDED_STORE_ENTRIES`` is IN, so the same
words there require an action, and until 2026-08-18 nobody had taken it: both
files sat inside the read-model core's key and cost it a hit on every boot. A
new store-root writer has to satisfy both halves; naming only the allowlists is
how this one was missed.

Root as INPUT
-------------

Every entry point takes ``store_root``. This module never resolves a root and
never reads ``HERMES_HOME``: multiple roots coexist on this machine, and a
transport free to re-derive its own root could bind a socket for one root while
answering from another — silently, because both answers would be well-formed.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .serde import write_json_atomic

if os.name == "nt":  # pragma: no cover - platform split, both sides exercised in CI
    import msvcrt
else:  # pragma: no cover - platform split
    import fcntl

__all__ = [
    "AUTH_FAILURE_REJECT_REASONS",
    "CLASSIFICATION_OWNER_FILE_UNVERIFIED",
    "HelloAuthOutcome",
    "REJECT_TLS_HANDSHAKE_FAILED",
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_MAX_PENDING_CONNECTIONS",
    "HELLO_CONTRACT_VERSION",
    "HELLO_FAILURE_LIMIT",
    "HELLO_FAILURE_WINDOW_SECONDS",
    "HELLO_PROOF_ALGORITHM",
    "NONCE_BYTES",
    "REJECT_BAD_PROOF",
    "REJECT_DRAINING",
    "REJECT_HANDSHAKE_THROTTLED",
    "REJECT_HELLO_MALFORMED",
    "REJECT_HELLO_REQUIRED",
    "REJECT_HELLO_TIMEOUT",
    "REJECT_HELLO_TOO_LONG",
    "REJECT_RATE_LIMITED",
    "REJECT_TOO_MANY_CONNECTIONS",
    "REJECT_TOO_MANY_PENDING",
    "SOCKET_HOST",
    "SOCKET_LOCK_FILENAME",
    "SOCKET_OWNER_FILENAME",
    "HelloRateLimiter",
    "ServeHelloProtocolError",
    "ServeSocketClient",
    "ServeSocketServer",
    "SocketLockResult",
    "SocketOwnerLock",
    "SocketTarget",
    "hello_proof",
    "read_socket_owner",
    "resolve_socket_target",
    "socket_lock_path",
    "socket_owner_path",
    "verify_hello_proof",
]

#: The LOOPBACK lane's host. Never configurable, and the sentence that follows
#: is unchanged from the day it was written: a knob HERE can only ever widen
#: exposure, and this runtime executes agents with tools.
#:
#: Stage 1 did not turn this into a knob. It added a SECOND listener — off by
#: default, per-config, TLS-wrapped, and accepting only per-device credentials —
#: precisely so that widening exposure is a different object with a different
#: credential story rather than a different value in this constant. A caller
#: that reads ``SOCKET_HOST`` is asking about the local lane and still gets the
#: local answer.
SOCKET_HOST = "127.0.0.1"

SOCKET_LOCK_FILENAME = "serve_socket.lock"
SOCKET_OWNER_FILENAME = "serve_socket.owner.json"

#: Connections a single serve will hold. A durable service is meant to be
#: multi-client, not unbounded: every connection costs a reader thread, and an
#: unbounded accept loop is a local denial of service against the runtime.
DEFAULT_MAX_CONNECTIONS = 32

#: Connections that have been accepted but have not yet PROVEN anything. Kept
#: small and counted SEPARATELY from the authenticated pool, because the
#: authenticated pool is the wrong bound for the pre-auth phase: with capacity
#: measured against authenticated peers only, 64 sockets that never said hello
#: sat on 64 threads and drew not one rejection. A peer that has proven nothing
#: gets a handshake deadline and one of these slots, and no more.
DEFAULT_MAX_PENDING_CONNECTIONS = 8

#: Failed hellos tolerated inside the window before every new connection is
#: rejected outright. The secret is 256 bits, so this is not what makes guessing
#: infeasible — it is what stops a local process from spending the runtime's
#: threads and log volume trying.
HELLO_FAILURE_LIMIT = 5
HELLO_FAILURE_WINDOW_SECONDS = 10.0
#: The SEPARATE, non-auth throttle for peers that connect and then say nothing.
#: A silent peer is not an authentication failure and must never be charged to
#: the auth limiter (that is what made the lockout self-sustaining), but it is
#: still a way to spend the runtime's threads, so it has its own bound.
HELLO_TIMEOUT_LIMIT = 16
HELLO_TIMEOUT_WINDOW_SECONDS = 10.0

#: The handshake version stamped on every ``server_hello``. Bumped from the
#: (implicit) token hello to the nonce/HMAC challenge-response; a client that
#: does not recognise this number must refuse to proceed rather than guess at
#: the frame it is expected to answer with.
#: 3 binds the proof to the listening PORT (see :func:`hello_proof`). Bumped
#: rather than shimmed: no client speaks this handshake yet, so the migration
#: cost is zero today and permanent the moment one does.
HELLO_CONTRACT_VERSION = 3
#: Challenge size. 32 bytes → 64 hex characters, fresh per CONNECTION.
NONCE_BYTES = 32
HELLO_PROOF_ALGORITHM = "hmac-sha256"

#: Typed rejection reasons. The reason IS the classification: whether a
#: rejection counts against the auth rate limiter is derived from it below,
#: never from a boolean the caller passes in (a caller-supplied flag is exactly
#: how capacity refusals came to be charged as auth failures).
REJECT_TOO_MANY_CONNECTIONS = "too_many_connections"
REJECT_TOO_MANY_PENDING = "too_many_pending"
REJECT_RATE_LIMITED = "rate_limited"
REJECT_HANDSHAKE_THROTTLED = "handshake_throttled"
REJECT_DRAINING = "draining"
REJECT_HELLO_TIMEOUT = "hello_timeout"
REJECT_HELLO_REQUIRED = "hello_required"
REJECT_HELLO_MALFORMED = "hello_malformed"
REJECT_HELLO_TOO_LONG = "hello_too_long"
REJECT_BAD_PROOF = "bad_proof"
#: The peer could not complete TLS. Only reachable on a listener configured with
#: an ``ssl_context`` (the gateway lane); the loopback lane never mints it.
#:
#: NOT an auth failure — a TLS failure says nothing about whether the peer holds
#: a credential, and charging it to the auth limiter is the same mistake that
#: made capacity refusals self-sustaining. It IS a way to spend the runtime's
#: threads, so it charges the SILENCE throttle instead, alongside the peer that
#: connects and says nothing: from the accept loop's point of view a half-open
#: TLS handshake is exactly that.
REJECT_TLS_HANDSHAKE_FAILED = "tls_handshake_failed"

#: The ONLY reasons that charge the auth rate limiter: a peer that presented a
#: bad credential, or that spoke something other than a hello where a hello was
#: mandatory. Capacity, drain, timeout, and throttle refusals say nothing about
#: whether the peer holds the secret — and counting them made a blocked window
#: extend itself forever, so a well-behaved client with the RIGHT credential
#: could never recover.
AUTH_FAILURE_REJECT_REASONS = frozenset(
    {
        REJECT_BAD_PROOF,
        REJECT_HELLO_REQUIRED,
        REJECT_HELLO_MALFORMED,
        REJECT_HELLO_TOO_LONG,
    }
)

#: Total budget for one broadcast across every attached connection. A drain
#: announcement must not be summed over N wedged readers: worst case is now one
#: parked ``sendall`` (bounded by IO_TIMEOUT_SECONDS) plus this, instead of
#: N × IO_TIMEOUT. Skipped connections are counted and logged, never silent.
BROADCAST_BUDGET_SECONDS = 2.0
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
            write_json_atomic(self._owner_path, dict(record))
        except Exception:
            pass

    def release(self) -> None:
        """Unlock, close, and drop the owner sidecar. Idempotent; never raises.

        The LOCK FILE ITSELF IS NEVER UNLINKED. It used to be, and on POSIX that
        is a two-owner race: ``flock`` is held on an OPEN DESCRIPTION, not on a
        path, so a contender that has already opened the file but not yet
        flocked it keeps a descriptor to an inode this release then unlinks —
        the contender locks a file that no longer has a name, the next boot
        creates a NEW inode at that path and locks that, and both processes hold
        an "exclusive" lock on the one root. A persistent zero-byte lock file is
        the standard pattern for exactly this reason, and it costs nothing: the
        lock's authority is the OS lock on the open file, never the file's
        existence, and :meth:`acquire` opens with ``a+b`` so a surviving file is
        re-lockable immediately.

        Windows behaviour is unchanged in substance (``msvcrt.locking`` is
        mandatory and released with the handle); it simply stops depending on an
        unlink that the AV/indexer can lose anyway.

        The owner SIDECAR is still removed: it is discovery data, it advertises
        a port this process no longer serves, and it holds no lock at all.
        """

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
        # sidecar's writer can be considered done with the directory.
        try:
            self._owner_path.unlink()
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


# ── the challenge-response proof ─────────────────────────────────────────────


class ServeHelloProtocolError(Exception):
    """The peer did not speak the handshake this contract requires.

    Carries the offending ``frame`` when there was one, because the two
    interesting cases look identical from a bare exception: a service that
    rejected us before the challenge (``hello_rejected``, and the reason is the
    answer), and something on the port that is not this service at all.
    """

    def __init__(self, detail: str, *, frame: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.frame = frame if isinstance(frame, dict) else None

    @property
    def reason(self) -> str | None:
        if self.frame is None:
            return None
        value = self.frame.get("reason")
        return value if isinstance(value, str) else None


def hello_proof(token: str, nonce: str, *, port: int) -> str:
    """``HMAC-SHA256(key=token, msg="v3|<port>|<nonce>")`` as lowercase hex.

    The ONE derivation in this stack: the client computes it, the server
    recomputes it, and a second copy of this line is how the two ends start
    disagreeing about what a proof is. The token is the KEY and never the
    message — a proof therefore reveals nothing about it, which is the whole
    point of not putting the token on the wire.

    **The proof is bound to the PORT, and that is what makes it unrelayable.**
    A fresh nonce stops a captured transcript from being replayed, but it does
    NOT stop a live relay: an impostor listener can dial the real service, take
    its nonce, present that same nonce as its own challenge, and forward the
    answer it receives. Every value in the ``server_hello`` frame is chosen by
    whoever sent it, so binding to `boot_id` would be bound to a number the
    impostor simply echoes. The port is different: each end takes it from its
    OWN socket — the server from what it listens on, the client from what it
    dialled — so a proof minted for the impostor's port cannot verify at the
    real one. Channel binding has to use something both ends know independently
    of anything the other one claims.
    """

    message = f"v{HELLO_CONTRACT_VERSION}|{int(port)}|{nonce}"
    return hmac.new(
        str(token).encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_hello_proof(
    presented: Any, nonce: str, token: str | None, *, port: int
) -> bool:
    """Constant-time check of *presented* against the proof this nonce demands.

    Fails CLOSED on every missing input — no token for this root, no proof in
    the hello, an empty nonce — because the alternative ("nothing configured,
    let everyone in") is the failure mode that turns a hardening into a bypass.
    """

    if not token or not nonce:
        return False
    if not isinstance(presented, str) or not presented.strip():
        return False
    # BYTES, never str. `hmac.compare_digest` RAISES TypeError when either str
    # operand is non-ASCII, and `presented` is attacker-controlled on a path no
    # credential is needed to reach: one accented character used to unwind out
    # of the handshake, so the peer got no rejection frame, the attempt was
    # never counted, the rate limiter was never charged, and the traceback was
    # written to a stderr that `serve_loop` has redirected onto the NDJSON
    # protocol stream. Encoding first makes a hostile proof merely wrong.
    return hmac.compare_digest(
        presented.strip().lower().encode("utf-8", "replace"),
        hello_proof(token, nonce, port=port).encode("ascii"),
    )


# ── who a hello turned out to be ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class HelloAuthOutcome:
    """What an :class:`ServeSocketServer` authenticator decided about one hello.

    The seam that lets ONE listener implementation serve two credential models
    without either one growing a branch on the other. The loopback lane's
    authenticator checks the per-root token and returns no identity beyond
    "yes"; the gateway lane's checks a per-device credential and returns the
    device it belongs to.

    ``reject_reason`` is a member of the typed vocabulary above rather than free
    text, because ``_reject`` derives whether a refusal charges the auth rate
    limiter FROM the reason and from nothing else — a caller-supplied flag there
    is exactly how capacity refusals came to be counted as attacks.
    """

    ok: bool
    reject_reason: str = REJECT_BAD_PROOF
    #: Set only when ``ok`` and only by a device-credential authenticator, so a
    #: connection cannot be stamped with a device whose proof did not verify.
    device_id: str | None = None
    device_tier: str | None = None
    #: A credential MINTED by this handshake — the pairing ceremony's second
    #: half, and the only frame in this lane that ever carries a secret. It
    #: exists because a code that cannot be redeemed by the party it was printed
    #: for is half a ceremony: the operator runs `harness gateway pair`, reads
    #: eight characters onto a phone, and the phone has to be able to turn them
    #: into something durable over the link it just pinned.
    #:
    #: Handed to the ``hello_payload`` builder through a one-shot slot on the
    #: connection and cleared there, so it lives for the microseconds between
    #: the handshake and the reply and is never a field anything can read later.
    issued_token: str | None = None
    #: Set only when ``ok`` and only by a PEER-credential authenticator (gateway
    #: Stage 6): which paired INSTALL this connection is. Never set beside
    #: ``device_id`` — a hello names one credential or the other, and the
    #: authenticator refuses a frame that names both.
    peer_install_id: str | None = None
    #: The peer half of ``issued_token``: the symmetric secret a ``peer_code``
    #: hello just minted, riding the one ``hello_ok`` that carries it and read-
    #: and-cleared exactly as the device token is. Two slots rather than one
    #: because the two ceremonies mint different credentials into different
    #: stores, and a single field would let a future edit put a device token on
    #: a peer's greeting by getting one branch wrong.
    issued_peer_secret: str | None = None


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
    #: WHICH paired device this is, and the tier its record holds — both ``None``
    #: on the loopback lane, forever. ``call_authorization.caller_for_connection``
    #: reads exactly these two fields to mint a ``device`` caller, and reads them
    #: through ``getattr`` so this module and that one need not import each
    #: other. Set once, in ``_handshake``, after the proof verified.
    device_id: str | None = None
    device_tier: str | None = None
    #: WHICH paired INSTALL this is (gateway Stage 6), or ``None`` on every
    #: other connection this runtime ever accepts. ``caller_for_connection``
    #: reads it through ``getattr`` to mint a ``peer`` caller, and reads it
    #: BEFORE the device pair, refusing a connection that somehow carries both.
    #: Set once, in ``_handshake``, after the proof verified.
    peer_install_id: str | None = None
    #: ONE-SHOT, and the only secrets this dataclass ever holds. Set by
    #: ``_handshake`` when a pairing code was redeemed, read and CLEARED by the
    #: ``hello_payload`` builder on the very next statement. Both are
    #: deliberately absent from ``payload()`` — the block that renders a
    #: connection to an operator, to a log, and to every other attached client.
    pairing_token: str | None = None
    peer_secret: str | None = None
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
            payload = {
                "connection": self.key,
                "client": self.client,
                "client_build": self.client_build,
                "authenticated": self.authenticated,
                "subscribed": self.subscribed,
                "connected_at": self.connected_at,
                "frames_out": self.frames_out,
                "bytes_out": self.bytes_out,
            }
            if self.device_id is not None:
                # ADDITIVE, and only on a row that has one — so every existing
                # `connections` consumer reads the shape it was written against.
                # A device id is not a secret (the device names it in its own
                # hello, in the clear under TLS), and "which of my paired devices
                # is attached right now" is the question this block exists to
                # answer once there is more than one client.
                payload["device_id"] = self.device_id
                payload["device_tier"] = self.device_tier
            if self.peer_install_id is not None:
                # Additive on a PEER row only, same rule and same reason: an
                # install id is not a secret (the peer names it in its own
                # hello, in the clear under TLS), and "which paired install is
                # attached right now" is the question an operator asks the
                # moment a cross-install call misbehaves. No tier key beside it,
                # because a peer holds an allowlist rather than a tier — a
                # ``peer_tier: null`` here would invite a reader to look for one.
                payload["peer_install_id"] = self.peer_install_id
            return payload

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
        token_provider: Callable[[], str | None],
        on_disconnect: Callable[[SocketConnection], None] | None = None,
        log: Callable[[dict[str, Any]], None] | None = None,
        host: str = SOCKET_HOST,
        port: int = 0,
        ssl_context: Any = None,
        authenticator: Callable[[dict[str, Any], str, int], HelloAuthOutcome]
        | None = None,
        transport_name: str = "socket",
        frame_contract: int = 1,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        max_pending_connections: int = DEFAULT_MAX_PENDING_CONNECTIONS,
        rate_limiter: HelloRateLimiter | None = None,
        timeout_limiter: HelloRateLimiter | None = None,
        hello_deadline_seconds: float = HELLO_DEADLINE_SECONDS,
        io_timeout_seconds: float = IO_TIMEOUT_SECONDS,
        reject_penalty_seconds: float = HELLO_REJECT_PENALTY_SECONDS,
    ) -> None:
        self._store_root = Path(store_root)
        self._boot_id = str(boot_id)
        self._dispatch_line = dispatch_line
        self._hello_payload = hello_payload
        #: Returns THIS root's shared secret, or None. Called once per
        #: handshake and never retained: the server needs the key to recompute
        #: a proof, and nothing else — no frame, log, or error derived from it
        #: ever carries the value.
        self._token_provider = token_provider
        self._on_disconnect = on_disconnect
        self._log = log
        self._host = host
        #: 0 = ephemeral, which is what the loopback lane has always done and
        #: what every existing caller gets by omission. A FIXED port exists for
        #: the gateway lane, where an operator has to write a firewall rule and
        #: a phone has to be told a number that survives a restart.
        self._bind_port = max(0, int(port))
        #: ``None`` on the loopback lane — local trust model unchanged, and a
        #: local process pays no handshake to reach a service it could read the
        #: token file of anyway. Set on the gateway lane (R1).
        self._ssl_context = ssl_context
        #: The credential model. ``None`` means the per-root token, i.e. exactly
        #: what this class did before the seam existed — see
        #: :meth:`_authenticate_hello`.
        self._authenticator = authenticator
        #: What every frame this listener answers is tagged with, and what
        #: ``call_authorization`` keys its structural guard on. ``socket`` for
        #: loopback, ``gateway`` for the second listener.
        self._transport_name = str(transport_name or "socket")
        self._frame_contract = int(frame_contract)
        self._max_connections = max(1, int(max_connections))
        self._max_pending = max(1, int(max_pending_connections))
        self._rate_limiter = rate_limiter or HelloRateLimiter()
        self._timeout_limiter = timeout_limiter or HelloRateLimiter(
            limit=HELLO_TIMEOUT_LIMIT, window_seconds=HELLO_TIMEOUT_WINDOW_SECONDS
        )
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
        #: Accepted, not yet authenticated. Bounded separately (see
        #: DEFAULT_MAX_PENDING_CONNECTIONS) and reported, because a peer that
        #: has proven nothing is exactly the peer no other counter was watching.
        self._pending = 0
        self._pending_peak = 0
        self._hello_timeouts = 0
        #: Every way the accept loop can fail, counted and SURFACED. The loop
        #: dying used to be invisible while the port stayed advertised — a
        #: service that answers its discovery record and nothing else.
        self._accept_errors = 0
        self._accept_loop_exited: str | None = None
        #: A handshake that raised instead of deciding. Counted separately from
        #: the rejections it is now converted into, because "we could not even
        #: process this peer" is a different operational fact from "we refused
        #: it", and the first one is the one that used to leave no trace at all.
        self._handshake_errors = 0
        self._last_handshake_error: str | None = None
        self._rejected_by_reason: dict[str, int] = {}
        self._started_at: str | None = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def started_at(self) -> str | None:
        return self._started_at

    def bind(self) -> int:
        """Bind this listener's host/port and listen. Returns the port.

        Ephemeral by default, which is the loopback lane's unchanged behaviour;
        a non-zero ``port`` constructor argument pins it. Still no
        ``SO_REUSEADDR``, and that matters MORE with a fixed port than it ever
        did with an ephemeral one: on Windows it permits a second process to
        bind a port already in use, so on a pinned port it would turn "the
        address is taken" into a silent hijack of a listener a phone is about to
        dial. A bind that cannot have the port RAISES, and the caller reports a
        typed outcome rather than serving from somewhere else.
        """

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # No SO_REUSEADDR — see the module docstring.
        listener.bind((self._host, self._bind_port))
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

    def broadcast(
        self,
        frame: dict[str, Any],
        *,
        budget_seconds: float | None = BROADCAST_BUDGET_SECONDS,
    ) -> int:
        """Send *frame* to every authenticated connection. Returns the count.

        Bounded as a WHOLE, not per connection. This runs on the drain path,
        where the old per-connection budget summed: 32 subscribers whose
        readers had stopped reading could each park a ``sendall`` for
        IO_TIMEOUT_SECONDS, so announcing a drain could outlast the drain. With
        one deadline across the pass, the worst case is a single parked write
        plus this budget, and the connections that did not get the frame are
        COUNTED and logged rather than silently missed.
        """

        delivered = 0
        skipped = 0
        deadline = (
            None if budget_seconds is None else time.monotonic() + float(budget_seconds)
        )
        for connection in self.connections():
            if not connection.authenticated:
                continue
            if deadline is not None and time.monotonic() >= deadline:
                skipped += 1
                continue
            lock_timeout = 0.5
            if deadline is not None:
                lock_timeout = max(0.0, min(lock_timeout, deadline - time.monotonic()))
            if connection.try_emit(frame, lock_timeout=lock_timeout):
                delivered += 1
            else:
                skipped += 1
        if skipped:
            self._emit_log(
                {
                    "event": "serve_socket_broadcast_incomplete",
                    "frame_event": frame.get("event"),
                    "delivered": delivered,
                    "skipped": skipped,
                }
            )
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
            pending, pending_peak = self._pending, self._pending_peak
            accept_errors = self._accept_errors
            accept_loop_exited = self._accept_loop_exited
            hello_timeouts = self._hello_timeouts
            handshake_errors = self._handshake_errors
            last_handshake_error = self._last_handshake_error
            by_reason = dict(self._rejected_by_reason)
        return {
            "port": self._port,
            "host": self._host,
            "count": len(rows),
            "max_connections": self._max_connections,
            "accepted_total": accepted,
            "rejected_total": rejected,
            "draining": draining,
            # The pre-auth lane, which no counter used to watch at all.
            "pending": pending,
            "pending_peak": pending_peak,
            "max_pending_connections": self._max_pending,
            "hello_timeouts": hello_timeouts,
            "rejected_by_reason": by_reason,
            # An accept loop that stopped while the port stayed advertised is
            # the one failure a client cannot detect by connecting; both the
            # error count and the loop's exit reason are stated, always.
            "accept_errors": accept_errors,
            "accept_loop_exited": accept_loop_exited,
            # A handshake that RAISED. Surfaced next to the accept-loop
            # failures for the same reason: an unauthenticated peer must never
            # be able to make the service misbehave without leaving a number.
            "handshake_errors": handshake_errors,
            "last_handshake_error": last_handshake_error,
            "hello_contract": HELLO_CONTRACT_VERSION,
            "connections": rows,
        }

    # ── accept loop ─────────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        """Accept until stopped, and ANNOUNCE the ending whatever it is.

        Nothing in here may terminate the loop quietly. The listener stays
        bound and the discovery record stays published for as long as this
        process lives, so a loop that stopped without a word leaves a service
        that answers "connect to me" and then never answers anything — the
        exact false-all-clear shape this workstream exists to retire. Every
        exit therefore records ``accept_loop_exited`` (readable in the
        ``connections`` block) and emits a typed log line.
        """

        outcome = "stopped"
        try:
            while not self._stop.is_set():
                listener = self._listener
                if listener is None:
                    outcome = "listener_closed"
                    return
                try:
                    sock, peer = listener.accept()
                except OSError as exc:
                    if self._stop.is_set() or self._listener is None:
                        outcome = "listener_closed"
                        return
                    # A typed line, never process death: an accept that fails on
                    # a transient OS condition must not take the runtime with it.
                    with self._lock:
                        self._accept_errors += 1
                    self._emit_log(
                        {
                            "event": "serve_socket_accept_error",
                            "phase": "accept",
                            "reason": type(exc).__name__,
                        }
                    )
                    if _is_fatal_accept_error(exc):
                        outcome = f"fatal_accept_error:{type(exc).__name__}"
                        return
                    time.sleep(0.05)
                    continue
                # INSIDE the loop's error handling, deliberately. A thread
                # spawn that fails (the interpreter is out of threads — which
                # is precisely what an unbounded pre-auth flood produces) used
                # to propagate out of the accept loop and kill it, silently,
                # while the port stayed advertised. It is now one accounted
                # rejection of one peer, and the lane keeps serving everybody
                # else.
                try:
                    threading.Thread(
                        target=self._serve_connection,
                        args=(sock, peer),
                        name="harness-serve-socket-conn",
                        daemon=True,
                    ).start()
                except BaseException as exc:  # noqa: BLE001 - reported, never raised out
                    with self._lock:
                        self._accept_errors += 1
                    self._emit_log(
                        {
                            "event": "serve_socket_accept_error",
                            "phase": "spawn",
                            "reason": type(exc).__name__,
                        }
                    )
                    try:
                        sock.close()
                    except OSError:
                        pass
                    time.sleep(0.05)
                    continue
        except BaseException as exc:  # noqa: BLE001 - reported, never raised out
            outcome = f"error:{type(exc).__name__}"
            with self._lock:
                self._accept_errors += 1
        finally:
            with self._lock:
                self._accept_loop_exited = outcome
            self._emit_log(
                {"event": "serve_socket_accept_loop_exit", "outcome": outcome}
            )

    def _serve_connection(self, sock: socket.socket, peer: Any) -> None:
        with self._lock:
            self._next_connection += 1
            key = f"conn-{self._next_connection}"
            at_capacity = len(self._connections) >= self._max_connections
            pending_full = self._pending >= self._max_pending
            admitted = not (at_capacity or pending_full)
            if admitted:
                self._pending += 1
                self._pending_peak = max(self._pending_peak, self._pending)
        connection = SocketConnection(
            key=key,
            sock=sock,
            peer=_peer_text(peer),
            connected_at=_now_iso(),
            transport=self._transport_name,
        )
        try:
            sock.settimeout(self._hello_deadline)
        except OSError:
            pass
        reader: "_LineReader | None" = None
        try:
            # TLS BEFORE the admission checks, not after, and the cost is
            # deliberate. Every one of those checks answers with a typed
            # ``hello_rejected`` frame, and on an encrypted listener a frame
            # written to a socket the peer has not negotiated is bytes it cannot
            # read — so refusing before the wrap would turn every capacity and
            # throttle refusal into an unexplained disconnect. The typed reason
            # IS the observability; paying a handshake to deliver one is the
            # right trade.
            if self._ssl_context is not None:
                wrapped = self._wrap_tls(sock)
                if wrapped is None:
                    # Nothing readable can be sent to a peer that never
                    # negotiated, so this refusal is COUNTED rather than
                    # announced — and counted against the silence throttle,
                    # because a half-open TLS handshake is, from the accept
                    # loop's point of view, exactly a peer that said nothing.
                    with self._lock:
                        self._rejected += 1
                        self._rejected_by_reason[REJECT_TLS_HANDSHAKE_FAILED] = (
                            self._rejected_by_reason.get(REJECT_TLS_HANDSHAKE_FAILED, 0)
                            + 1
                        )
                    self._timeout_limiter.record_failure()
                    self._emit_log(
                        {
                            "event": "serve_socket_connection_rejected",
                            "connection": connection.key,
                            "peer": connection.peer,
                            "reason": REJECT_TLS_HANDSHAKE_FAILED,
                        }
                    )
                    connection.close(REJECT_TLS_HANDSHAKE_FAILED)
                    return
                connection.sock = wrapped
                sock = wrapped
            if at_capacity:
                self._reject(connection, REJECT_TOO_MANY_CONNECTIONS)
                return
            if pending_full:
                # The bound that did not exist: 64 peers that said nothing sat
                # on 64 threads because only AUTHENTICATED connections counted.
                self._reject(connection, REJECT_TOO_MANY_PENDING)
                return
            if self._rate_limiter.blocked():
                self._reject(connection, REJECT_RATE_LIMITED)
                return
            if self._timeout_limiter.blocked():
                self._reject(connection, REJECT_HANDSHAKE_THROTTLED)
                return
            if self._draining:
                self._reject(connection, REJECT_DRAINING)
                return
            try:
                reader = self._handshake(connection, sock, key)
            except Exception as exc:
                # The pre-auth path is driven entirely by an unauthenticated
                # peer, so ONE uncaught call on it is one too many: an escaping
                # exception used to mean no rejection frame, no accounting, no
                # limiter charge, a socket left to the garbage collector, and a
                # traceback on the protocol stream. Anything that gets here is
                # a peer we could not process — charged as an auth failure so
                # it cannot be used to hammer the handshake for free — and it
                # is COUNTED, because the whole point of the finding was that
                # the attempt was invisible.
                reader = None
                with self._lock:
                    self._handshake_errors += 1
                    self._last_handshake_error = type(exc).__name__
                try:
                    self._reject(connection, REJECT_HELLO_MALFORMED)
                except Exception:
                    connection.close("handshake_failed")
        finally:
            if admitted:
                with self._lock:
                    self._pending = max(0, self._pending - 1)
        if reader is None:
            return
        self._read_loop(connection, reader)

    def _handshake(
        self, connection: SocketConnection, sock: socket.socket, key: str
    ) -> "_LineReader | None":
        """Challenge, verify, admit. Returns the reader, or None on rejection.

        The SERVER speaks first — one ``server_hello`` carrying a nonce minted
        for THIS connection — and the client answers with an HMAC over it. The
        token never appears on the wire in either direction, so a captured
        transcript authenticates nothing and cannot be replayed: the next
        connection demands a proof over a different nonce.

        What an unauthenticated peer learns is deliberately bounded to the
        challenge itself (nonce, boot id, contract numbers, algorithm). No
        build, no runtime root, no answer to any op — the boot id is disclosed
        because a client must be able to tell "the service I was talking to
        restarted" from "a different service answered" BEFORE it commits to a
        handshake, and it is a per-boot random value that authorises nothing.
        """

        reader = _LineReader(sock)
        nonce = secrets.token_hex(NONCE_BYTES)
        try:
            connection.emit(
                {
                    "event": "server_hello",
                    "nonce": nonce,
                    "boot_id": self._boot_id,
                    "contract": self._frame_contract,
                    "hello_contract": HELLO_CONTRACT_VERSION,
                    "algorithm": HELLO_PROOF_ALGORITHM,
                }
            )
        except Exception:
            connection.close("server_hello_write_failed")
            return None
        try:
            hello_line = reader.read_line(deadline_seconds=self._hello_deadline)
        except _LineTooLong:
            # A peer that FLOODED is not a peer that was silent. Charging this
            # to the timeout throttle accounted an attack as an absence, and
            # let it consume the budget reserved for genuinely quiet clients.
            self._reject(connection, REJECT_HELLO_TOO_LONG)
            return None
        except Exception:
            hello_line = None
        if hello_line is None:
            # NOT an auth failure: a peer that said nothing presented no
            # credential to be wrong about. It gets its own throttle instead,
            # so silence can still be bounded without locking out clients that
            # hold the right secret.
            with self._lock:
                self._hello_timeouts += 1
            self._timeout_limiter.record_failure()
            self._reject(connection, REJECT_HELLO_TIMEOUT)
            return None
        message = _parse_object(hello_line)
        if message is None:
            self._reject(connection, REJECT_HELLO_MALFORMED)
            return None
        if message.get("op") != "hello":
            self._reject(connection, REJECT_HELLO_REQUIRED)
            return None
        # `self._port` — what this server actually listens on — never a value
        # from the peer's frame, which is the whole point of the binding.
        outcome = self._authenticate_hello(message, nonce, self._port or 0)
        if not outcome.ok:
            # ONE typed frame, and nothing else: a rejected connection never
            # learns anything about the runtime it failed to reach. On the
            # gateway lane every credential failure — no device named, unknown
            # id, revoked row, wrong proof — collapses into this single reason,
            # so a peer cannot map which device ids exist by watching it change.
            self._reject(connection, outcome.reject_reason)
            return None
        connection.device_id = outcome.device_id
        connection.device_tier = outcome.device_tier
        connection.pairing_token = outcome.issued_token
        connection.peer_install_id = outcome.peer_install_id
        connection.peer_secret = outcome.issued_peer_secret
        self._rate_limiter.record_success()
        # Symmetry the first pass missed: a completed handshake proves the lane
        # is reachable and answering, so the SILENCE throttle has nothing left
        # to protect against either. Cleared only by time, a burst of abandoned
        # connections went on refusing the client holding the right credential —
        # the same "the server's own state locks out a good client" shape the
        # auth limiter was given `record_success` to retire.
        self._timeout_limiter.record_success()
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
        open_log: dict[str, Any] = {
            "event": "serve_socket_connection_open",
            "connection": key,
            "client": connection.client,
            "client_build": connection.client_build,
            "peer": connection.peer,
        }
        if connection.device_id is not None:
            # Additive on a gateway row only, so every existing consumer of this
            # line reads the shape it was written against. A device id is the
            # one thing that makes a remote connection auditable after the fact;
            # the credential that proved it appears here as it appears
            # everywhere else, which is nowhere.
            open_log["transport"] = self._transport_name
            open_log["device_id"] = connection.device_id
            open_log["device_tier"] = connection.device_tier
        if connection.peer_install_id is not None:
            # The peer half of the same line, and the reason is the same one:
            # an install id is what makes a cross-install connection auditable
            # after the fact. The credential that proved it appears here as it
            # appears everywhere else, which is nowhere.
            open_log["transport"] = self._transport_name
            open_log["peer_install_id"] = connection.peer_install_id
        self._emit_log(open_log)
        try:
            connection.emit(self._hello_payload(message, connection))
        except Exception:
            self._drop_connection(connection, reason="hello_write_failed")
            return None
        return reader

    def _authenticate_hello(
        self, message: dict[str, Any], nonce: str, port: int
    ) -> HelloAuthOutcome:
        """Who is this? The per-root token by default; a device when injected.

        The DEFAULT arm is the loopback lane's original code, moved and not
        rewritten: read this root's shared secret, recompute the proof over the
        nonce and the listening port, compare in constant time. A server built
        with no ``authenticator`` therefore behaves byte-for-byte as it did
        before this seam existed, which is the invariant Stage 1 owes the local
        launcher and the CLI.

        An authenticator that RAISES is a refusal, never an admission. The
        gateway lane's reads a store off disk on a path an unauthenticated peer
        drives, so the failure is ordinary rather than exotic — and the one
        answer that must never come out of an exception handler here is "yes".
        """

        if self._authenticator is not None:
            try:
                outcome = self._authenticator(message, nonce, port)
            except Exception:
                with self._lock:
                    self._handshake_errors += 1
                    self._last_handshake_error = "authenticator_failed"
                return HelloAuthOutcome(ok=False, reject_reason=REJECT_BAD_PROOF)
            if not isinstance(outcome, HelloAuthOutcome):  # pragma: no cover
                return HelloAuthOutcome(ok=False, reject_reason=REJECT_BAD_PROOF)
            return outcome
        try:
            token = self._token_provider()
        except Exception:
            token = None
        ok = verify_hello_proof(message.get("proof"), nonce, token, port=port)
        del token
        return HelloAuthOutcome(ok=ok, reject_reason=REJECT_BAD_PROOF)

    def _wrap_tls(self, sock: socket.socket) -> Any | None:
        """Server-side TLS handshake. Returns the wrapped socket, or ``None``.

        Never raises: a peer that cannot speak TLS — a port scanner, a browser,
        a client that has not been told this lane is encrypted — is an ordinary
        event on a listener bound beyond loopback, and an exception escaping
        here would land on the pre-auth path the module docstring says must have
        no uncaught calls on it.

        The raw socket is closed by the caller's rejection path; this only
        reports.
        """

        try:
            return self._ssl_context.wrap_socket(sock, server_side=True)
        except Exception:
            return None

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

    def _reject(self, connection: SocketConnection, reason: str) -> None:
        """Refuse one connection, typed, and charge the RIGHT counter.

        Whether this counts as an authentication failure is derived from the
        reason (:data:`AUTH_FAILURE_REJECT_REASONS`) and from nothing else. It
        used to be a boolean the caller passed, defaulting to True, so capacity
        and drain refusals were charged as attacks — and a ``rate_limited``
        refusal re-armed the very window that produced it. That is a permanent,
        self-sustaining lockout: proven live, 12 polite retries with the RIGHT
        credential over 12 seconds, never recovering. A refusal caused by the
        SERVER's own state can never extend a block against the client.
        """

        if reason in AUTH_FAILURE_REJECT_REASONS:
            self._rate_limiter.record_failure()
        with self._lock:
            self._rejected += 1
            self._rejected_by_reason[reason] = (
                self._rejected_by_reason.get(reason, 0) + 1
            )
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


#: What the owner sidecar can honestly claim about liveness: nothing. It is
#: written by the lock holder and is precise while that process lives, and it
#: cannot prove the process still does. Never ``None`` any more — a null
#: classification read as "no objection" at exactly the call site that had to
#: object.
CLASSIFICATION_OWNER_FILE_UNVERIFIED = "unverified_owner_file"


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
    #: The registry's read-time classification, or
    #: ``unverified_owner_file`` when the sidecar was the source. ``live`` is
    #: the only value that is evidence of a process; everything else is why a
    #: connect may fail — and, before this was enforced, why a client could
    #: hand its credential to whatever had taken over a dead serve's port.
    classification: str

    @property
    def live(self) -> bool:
        return self.classification == "live"

    def payload(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "boot_id": self.boot_id,
            "source": self.source,
            "classification": self.classification,
            "live": self.live,
        }


def resolve_socket_target(
    store_root: Path | str, *, probe: Any = None, allow_stale: bool = False
) -> SocketTarget | None:
    """Find the LIVE socket service for *store_root*, or None.

    The REGISTRY is the only source that classifies liveness at read time (a
    dead pid, a recycled pid, and an unreadable probe are three different
    answers there, and none of them is "live"), so a live registry row is the
    only thing this returns by default.

    That default is load-bearing. This used to fall back — ``for row in (live
    or candidates)`` — and then to the owner sidecar with no classification at
    all, so a caller asking "where is the service" got back a row the registry
    had ALREADY classified ``stale_dead_pid`` and connected to it. A dead
    serve's port is reusable by any local process, and the first cut of the
    handshake sent the raw token, so the fallback was a credential handed to an
    impostor. Both halves are closed now (the token no longer travels either),
    and the returned target still has to be live.

    ``allow_stale=True`` is the deliberate diagnostic path: it returns the best
    non-live candidate CARRYING its classification, so a caller can name what it
    is refusing ("stale_dead_pid", "unverified_owner_file") instead of reporting
    the far less useful "nothing found". A caller that passes it and then
    connects anyway has made that choice explicitly, in the open.
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
    for row in live:
        target = _target_from_row(row)
        if target is not None:
            return target
    if not allow_stale:
        return None
    for row in candidates:
        target = _target_from_row(row)
        if target is not None:
            return target
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
        classification=CLASSIFICATION_OWNER_FILE_UNVERIFIED,
    )


def _target_from_row(row: dict[str, Any]) -> SocketTarget | None:
    port = _int_or_none(row.get("port"))
    if port is None:  # pragma: no cover - callers filter on this already
        return None
    return SocketTarget(
        host=SOCKET_HOST,
        port=port,
        pid=_int_or_none(row.get("pid")),
        boot_id=row.get("boot_id") if isinstance(row.get("boot_id"), str) else None,
        source="registry",
        classification=str(row.get("classification") or "unknown"),
    )


class ServeSocketClient:
    """The other end of the handshake: connect, hello, then read frames.

    Deliberately small and dependency-free — it is the reference client the CLI
    probe verb uses, and the shape the Launcher's own client will mirror when it
    migrates off the stdio child.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_seconds: float = 10.0,
        tls: bool = False,
        cert_fingerprint: str | None = None,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._timeout = float(timeout_seconds)
        #: Off by default — the loopback lane is plaintext and stays that way.
        #: On for the gateway lane, where the client trusts NO certificate
        #: authority and pins instead (R1).
        self._tls = bool(tls) or cert_fingerprint is not None
        self._cert_fingerprint = (
            str(cert_fingerprint).strip().lower() if cert_fingerprint else None
        )
        self._sock: socket.socket | None = None
        self._reader: _LineReader | None = None
        #: The challenge frame this connection was greeted with, once
        #: :meth:`hello` has read it. Kept so a caller can report the contract
        #: and boot id it actually answered — not re-read, and never a place
        #: the token is stored.
        self.server_hello: dict[str, Any] | None = None

    def connect(self) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        if self._tls:
            sock = self._wrap_tls(sock)
        self._sock = sock
        self._reader = _LineReader(sock)

    def _wrap_tls(self, sock: socket.socket) -> Any:
        """Negotiate TLS and PIN the certificate. The reference pinning path.

        There is no CA and no hostname to check: the install's certificate is
        self-signed and the address is whatever the operator's LAN gave the
        machine, so both of the usual checks would fail on a link that is
        exactly as it should be. What replaces them is a fingerprint the client
        was given out of band — through the pairing payload — and comparing it
        is not optional decoration: without it the encryption stops any
        eavesdropper and stops no impostor, and the pairing payload's
        ``cert_fingerprint`` field would be a value nobody uses.

        Written here rather than only in a test because this class is the
        reference client — the shape the launcher's own connector mirrors, where
        the same comparison lives inside ``badCertificateCallback``.
        """

        import ssl

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        wrapped = context.wrap_socket(sock, server_hostname=None)
        if self._cert_fingerprint is not None:
            presented = wrapped.getpeercert(binary_form=True) or b""
            actual = hashlib.sha256(presented).hexdigest()
            if not hmac.compare_digest(
                actual.encode("ascii"), self._cert_fingerprint.encode("ascii")
            ):
                try:
                    wrapped.close()
                except OSError:
                    pass
                raise ServeHelloProtocolError(
                    "the peer's certificate does not match the pinned fingerprint"
                )
        wrapped.settimeout(self._timeout)
        return wrapped

    def hello(
        self,
        *,
        token: str,
        client: str,
        client_build: str | None = None,
        expect_hello_contract: int | None = HELLO_CONTRACT_VERSION,
    ) -> dict[str, Any] | None:
        """Answer the server's challenge and return the single reply frame.

        The SERVER speaks first, so this reads before it writes: one
        ``server_hello`` carrying the nonce, then the proof. **The token never
        goes on the wire** — it is the HMAC key, it is not logged, not echoed
        into the returned frame, and not retained on this object. A transcript
        of this exchange authenticates nobody: the nonce is fresh per
        connection, so a replayed proof is a proof over the wrong challenge.

        Anything other than a well-formed ``server_hello`` raises
        :class:`ServeHelloProtocolError` with the offending frame attached
        rather than pressing on: the two ways that happens are a service that
        refused us before the challenge (its ``hello_rejected`` reason is the
        answer) and something on this port that is not this service — and
        neither is a case where sending a credential is the right next move.
        """

        greeting = self._challenge(expect_hello_contract)
        nonce = greeting.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < 2 * NONCE_BYTES:
            raise ServeHelloProtocolError(
                "server_hello carried no usable nonce", frame=greeting
            )
        self.send(
            {
                "op": "hello",
                "client": client,
                "client_build": client_build,
                # `self._port` — the port THIS client dialled, taken from its
                # own socket rather than from anything the greeting claims. A
                # relay that forwards our answer to a different port cannot
                # use it.
                "proof": hello_proof(token, nonce, port=self._port),
            }
        )
        return self.read_frame()

    def device_hello(
        self,
        *,
        device_id: str,
        token: str,
        client: str,
        client_build: str | None = None,
        expect_hello_contract: int | None = HELLO_CONTRACT_VERSION,
    ) -> dict[str, Any] | None:
        """The GATEWAY lane's hello: same frames, a per-device credential.

        Structurally identical to :meth:`hello` — server speaks first, one
        ``server_hello`` carrying a fresh nonce, one answer carrying a proof —
        and that sameness is the point: the gateway lane is this contract made
        reachable beyond loopback, not a second protocol. The two differences
        are that the frame NAMES a device (so the server knows which key to
        recompute with) and that the proof derivation binds that name
        (``serve_gateway_auth.device_proof``).

        The device token never goes on the wire, in either direction — and it is
        not even the HMAC key: its digest is, so the value on this device and
        the value in the install's store are different bytes. See
        ``serve_gateway_auth``'s docstring for the honest limit of that.
        """

        from .serve_gateway_auth import device_proof

        greeting = self._challenge(expect_hello_contract)
        nonce = greeting.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < 2 * NONCE_BYTES:
            raise ServeHelloProtocolError(
                "server_hello carried no usable nonce", frame=greeting
            )
        self.send(
            {
                "op": "hello",
                "client": client,
                "client_build": client_build,
                "device_id": device_id,
                # `self._port` again — the port THIS client dialled, from its own
                # socket rather than from anything the greeting claims.
                "proof": device_proof(
                    token, nonce, port=self._port, device_id=device_id
                ),
            }
        )
        return self.read_frame()

    def peer_hello(
        self,
        *,
        peer_install_id: str,
        verifier: str,
        client: str,
        client_build: str | None = None,
        expect_hello_contract: int | None = HELLO_CONTRACT_VERSION,
    ) -> dict[str, Any] | None:
        """The GATEWAY lane's PEER hello: same frames, a per-INSTALL credential.

        Structurally identical to :meth:`hello` and :meth:`device_hello`, and
        that sameness is again the point — Stage 6 is not a third protocol.
        Two things differ, and both are what keep the credentials from being
        interchangeable: the frame names ``peer_install_id`` where a device
        names ``device_id`` (so the server knows which store to look in as well
        as which key to recompute with), and the derivation carries a different
        prefix (``gateway_peers.peer_proof``).

        ``verifier`` — ``sha256(secret)`` — is what a paired install actually
        holds; see ``gateway_peers``' docstring for why both ends store the
        digest and key the HMAC with it directly, and for the honest limit of
        that. It never goes on the wire, in either direction.
        """

        from .gateway_peers import peer_proof

        greeting = self._challenge(expect_hello_contract)
        nonce = greeting.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < 2 * NONCE_BYTES:
            raise ServeHelloProtocolError(
                "server_hello carried no usable nonce", frame=greeting
            )
        self.send(
            {
                "op": "hello",
                "client": client,
                "client_build": client_build,
                "peer_install_id": peer_install_id,
                # `self._port` again — the port THIS client dialled, from its own
                # socket rather than from anything the greeting claims.
                "proof": peer_proof(
                    verifier, nonce, port=self._port, peer_install_id=peer_install_id
                ),
            }
        )
        return self.read_frame()

    def peer_join_hello(
        self,
        *,
        peer_code: str,
        peer_install_id: str,
        display_name: str | None = None,
        endpoints: Any = None,
        cert_fingerprint: str | None = None,
        client: str = "hermes-peer",
        client_build: str | None = None,
        expect_hello_contract: int | None = HELLO_CONTRACT_VERSION,
    ) -> dict[str, Any] | None:
        """Redeem a PEER code and become a paired install, in one round trip.

        The ceremony's second half and the mirror of :meth:`pair_hello`. What is
        different is the direction the facts flow: a phone redeeming a device
        code tells the install nothing about itself worth storing, while a
        joining INSTALL must tell the other side who it is (``peer_install_id``),
        what to call it, where to dial it back, and what certificate to pin —
        because the edge is symmetric and the other install will one day be the
        one dialling.

        Those four fields are ASSERTIONS by the joining side, and are treated as
        such: the server bounds and cleans them (``gateway_peers.clean_endpoints``)
        and stores them as a starting point rather than as a proof. What makes
        them trustworthy is not the wire — it is that an operator at the other
        machine minted the code seconds earlier and is standing there. That is
        R5's "both sides", and it is the only thing that makes the assertion
        safe to keep.

        No proof is computed and none is possible: the code IS the credential for
        this one exchange, exactly as in the device ceremony, protected by the
        same three properties (a pinned TLS link, a one-shot code deleted before
        the secret is minted, and a failed redeem that looks like every other
        credential failure and charges the same limiter).

        A caller MUST store ``hello_ok["peered"]["peer_secret"]``: the remote
        install keeps only a digest of it and cannot reissue it, so a client that
        drops the frame has paired an edge it can never use.
        """

        self._challenge(expect_hello_contract)
        frame: dict[str, Any] = {
            "op": "hello",
            "client": client,
            "client_build": client_build,
            # A DIFFERENT key from the device ceremony's ``pairing_code``, and
            # deliberately: the two codes redeem into different stores, and a
            # shared field name is the one thing that could make a server's
            # branch pick the wrong one.
            "peer_code": str(peer_code).strip().upper(),
            "peer_install_id": peer_install_id,
        }
        if display_name:
            frame["peer_display_name"] = display_name
        if endpoints:
            frame["peer_endpoints"] = endpoints
        if cert_fingerprint:
            frame["peer_cert_fingerprint"] = cert_fingerprint
        self.send(frame)
        return self.read_frame()

    def _challenge(self, expect_hello_contract: int | None) -> dict[str, Any]:
        """Read and validate the ``server_hello``. The common half of four hellos.

        Extracted when Stage 6 added the fourth and fifth: the same fifteen
        lines had been copied per hello, and a fifth copy is how one of them
        eventually stops checking the contract. The validation is unchanged —
        anything that is not a well-formed ``server_hello`` RAISES with the
        offending frame attached rather than pressing on, because the two ways
        that happens (a service that refused us before the challenge, and
        something on this port that is not this service) are both cases where
        sending a credential is the wrong next move.
        """

        greeting = self.read_frame()
        if not isinstance(greeting, dict) or greeting.get("event") != "server_hello":
            raise ServeHelloProtocolError(
                "the peer did not open with a server_hello challenge", frame=greeting
            )
        contract = greeting.get("hello_contract")
        if expect_hello_contract is not None and contract != expect_hello_contract:
            raise ServeHelloProtocolError(
                f"unsupported hello_contract {contract!r} "
                f"(this client speaks {expect_hello_contract})",
                frame=greeting,
            )
        self.server_hello = greeting
        return greeting

    def pair_hello(
        self,
        *,
        pairing_code: str,
        client: str,
        client_build: str | None = None,
        expect_hello_contract: int | None = HELLO_CONTRACT_VERSION,
    ) -> dict[str, Any] | None:
        """Redeem a pairing code and become a device, in one round trip.

        The only exchange in this lane where the client presents something it
        was given out of band — eight characters off the operator's terminal —
        and the only reply that carries a secret. A caller MUST store
        ``hello_ok["paired"]["device_token"]``: the install keeps a digest of it
        and cannot reissue it, so a client that drops the frame has paired a
        device it can never be again.

        No proof is computed and none is possible: the code IS the credential
        for this one exchange. What protects it is the pinned TLS link it rides
        (an impostor cannot receive it), its one-shot nature (redeemed, it is
        deleted before the token is minted), and the store's lockout.
        """

        self._challenge(expect_hello_contract)
        self.send(
            {
                "op": "hello",
                "client": client,
                "client_build": client_build,
                "pairing_code": str(pairing_code).strip().upper(),
            }
        )
        return self.read_frame()

    def send(self, message: dict[str, Any]) -> None:
        if self._sock is None:
            raise RuntimeError("connect() first")
        payload = json.dumps(message, ensure_ascii=False, default=str) + "\n"
        self._sock.sendall(payload.encode("utf-8"))

    def set_timeout(self, seconds: float) -> None:
        """Re-arm the socket timeout after the handshake.

        Gateway Stage 7 needs the DIAL and the READ to be bounded differently,
        and they are different questions. R8's "bounded per-attempt dial
        timeout" is about how long to wait for an install that may simply be
        off — seconds, because an unreachable peer should converge rather than
        hang. Waiting for a chat TURN to finish on that install is a wall
        budget the sender chose, measured in minutes, and reusing the dial's
        number for it would kill every remote turn that took longer than a
        handshake.

        Deliberately a method rather than a second constructor argument: the
        value that matters changes at a moment (the ack), not at construction,
        and a client with two timeouts baked in would still have to be told
        when to switch.
        """

        self._timeout = float(seconds)
        if self._sock is not None:
            self._sock.settimeout(self._timeout)

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
