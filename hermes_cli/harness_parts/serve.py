"""``hermes harness serve --ndjson`` — persistent stdio bridge (schema v1).

One warm process replaces the per-call CLI spawns the Launcher Mission
Control bridge pays ~3s import tax on today. Requests dispatch into the
EXISTING harness argparse tree and ``_cmd_*`` handlers, unchanged — argv
arrives verbatim as the bridge already builds it, so intent→argv mapping,
the capability registry, and the per-call CLI fallback stay byte-identical.

Design doc: ``docs/agent-runtime-harness/harness-serve-design.md``
(settled 2026-07-08). Explicit non-goals: no network listener, not the
mission daemon, no second chat pipeline. "No auth (a local stdio child IS
the security model)" held while the transport was an inherited pipe; the
durable runtime-root service replaces that pipe with one any local process
can reach, so a per-root token is now minted at boot (unwired — see
``agent_runtime/serve_auth.py``) rather than retrofitted after the socket
exists.

Protocol (NDJSON, one frame per line):

- boot:      ``{"event":"ready","pid":…,"schema_version":1,"runtime_root":…}``
             plus the durable-service foundations, all additive:
             ``"build"`` (which commit this runtime is on —
             ``agent_runtime/build_stamp.py``), ``"auth"``
             (``{"token_file":"present"|"minted"|"error:<reason>"}`` — the
             posture, NEVER the token itself), and ``"instance"`` (this
             serve's registry entry under ``<store_root>/serve_instances/``).
- request:   ``{"id":"req-7","argv":["harness","status","--json"]}``
- reply:     ``{"id":"req-7","event":"line","line":…}`` × N then
             ``{"id":"req-7","event":"exit","code":0}``
             (a status/snapshot poll replayed from the read-model cache adds
             ``"served_from_cache": true, "cache_age_ms": N`` to its exit
             frame — additive; see _ReadModelCache below)
- stderr:    ``{"id":<request id or null>,"event":"stderr","line":…}``
- ping:      ``{"op":"ping"}`` → ``{"event":"busy","chat_turns":N,"pending":M}``
             (the Launcher supervisor must NEVER recycle serve while
             ``chat_turns`` > 0 — recording safety)
- shutdown:  ``{"op":"shutdown"}`` → drain in-flight requests, exit 0
- version:   ``{"op":"version"}`` → ``{"event":"version","build":{…},
             "runtime_root":…,"boot_id":…,"transport":"stdio","auth":{…}}``
             — the SAME stamp the ready frame carried, re-askable at any
             time. A durable service outlives the install it was started
             from; this is how a client proves it is not talking to last
             week's code.
- drain:     ``{"op":"drain"[,"deadline_seconds":30]}`` → stop accepting new
             requests (each is answered ``{"id":…,"event":"draining",…}`` and
             a terminal ``exit`` frame with code 75), let in-flight requests
             finish, then ``{"event":"drain_complete","requests_refused":N,
             "requests_completed":M,"drain_ms":X}`` and exit 0. If the
             deadline elapses first: ``{"event":"drain_timeout",…,
             "stuck_request_ids":[…]}`` and a NONZERO exit — a drain that can
             hang forever is not a drain. Progress is reported as
             ``{"event":"drain_progress",…}`` while the wait runs, so a
             draining service never looks dead to a watchdog.
- cancel:    ``{"op":"cancel","id":"req-7"}`` → a QUEUED request is dropped
             and answers ``{"id":"req-7","event":"exit","code":130,
             "cancelled":true}``; a request already RUNNING (or unknown)
             answers ``{"id":…,"event":"cancel_denied","state":
             "running"|"unknown"}`` — its side effects may still happen, so
             mutation verbs carry their own replay guard (``--issued-at``).
             A RUNNING read-only ``harness stream`` is cooperatively cancelled
             and releases its pool worker; it is the sole running exception.
- errors:    ``{"id":…,"event":"error","error":"invalid_request"|…,"detail":…}``

Per-request stdout: handlers ``print()`` directly and streaming turns emit
deltas live, so ``sys.stdout``/``sys.stderr`` are swapped once for
contextvar-dispatching proxies; each pool worker binds its request id and a
single write lock keeps frames atomic. Writes from threads a handler spawns
itself carry no request id and are forwarded with ``"id": null``.

The socket lane (slice 3)
-------------------------

``serve_loop`` is now transport-agnostic: ONE dispatcher answers ops arriving
on stdio and on a localhost socket alike. Everything above this line is
unchanged on stdio — every frame, reply, and exit code is byte-identical,
because the socket lane is injected and OFF unless ``_cmd_serve`` turns it on.

- ownership: one serve per root owns the socket, decided by an OS-held
  exclusive lock (``agent_runtime/serve_socket.py``). The loser runs
  stdio-only and says so on ``ready`` under ``"socket"``:
  ``{"outcome":"lock_held_by","pid":…}``. The winner's ``ready`` carries
  ``{"outcome":"listening","host":"127.0.0.1","port":…}`` and its registry
  entry records ``transport:"stdio+socket"`` plus the port.
- hello:     the FIRST line on a socket must be
             ``{"op":"hello","token":…,"client":…,"client_build":…}``, verified
             against the per-root token (``agent_runtime/serve_auth.verify`` —
             constant-time, fails closed). Success →
             ``{"event":"hello_ok","build":{…},"boot_id":…,"contract":1,
             "build_mismatch":true|false|null}``; failure → ONE
             ``{"event":"hello_rejected","reason":…}`` and the connection is
             closed, with a rate limit against hammering. Before that line is
             verified a connection can do NOTHING. The token appears in no
             frame, log, error, or registry entry.
- subscribe: ``{"op":"subscribe","lane":"stream"}`` pushes the SAME hydrate /
             delta / heartbeat frames ``harness stream`` produces, from ONE
             shared producer fanned out to every subscriber (a per-batch
             snapshot rebuild is why it is not one generator per client). A
             subscriber that outruns its bounded buffer gets
             ``{"event":"subscription_dropped","reason":"backpressure",…}`` and
             is unsubscribed — never silently stalled, and never able to wedge
             the producer or another subscriber. ``{"op":"unsubscribe"}`` ends
             it cleanly, and so does a disconnect.
- connections: ``{"op":"connections"}`` → ``{"event":"socket_connections",…}``
             (count, and per client: name, build, subscribed, connected_at,
             frames and bytes pushed). The same block rides the ``version``
             reply, so "who is attached to this runtime" is answerable from the
             handshake a client already performs.

Client disconnect unsubscribes and does NOTHING else: the backend state a
client was watching is the runtime's, not the client's, and surviving a client
is the entire point of the durable service.
"""

from __future__ import annotations

import argparse
import contextvars
import io
import json
import os
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TextIO

SERVE_SCHEMA_VERSION = 1
DEFAULT_POOL_SIZE = 4

# ── Drain ────────────────────────────────────────────────────────────────────
#
# A durable service must be replaceable WITHOUT killing work: `drain` refuses
# new requests, lets the in-flight ones land, and exits. Every path emits its
# typed terminal frame BEFORE exiting — the frame is the observability, and a
# drain that exited without one would be indistinguishable from the crash it
# exists to avoid.
DEFAULT_DRAIN_DEADLINE_SECONDS = 30.0
#: Bounds on a client-supplied deadline. Zero would make `drain` a `kill`; an
#: unbounded value would restore "can hang forever" through the front door.
_DRAIN_DEADLINE_MIN_SECONDS = 0.05
_DRAIN_DEADLINE_MAX_SECONDS = 3600.0
_DRAIN_POLL_INTERVAL_SECONDS = 0.05
#: While draining, this replaces the `busy` liveness pump (which stops with the
#: delivery drain the moment draining starts). A watchdog keyed on "no frames
#: for N seconds" must not declare a healthily-draining runtime dead.
_DRAIN_PROGRESS_INTERVAL_SECONDS = 5.0
#: Refused-because-draining. 75 is EX_TEMPFAIL: "try again", which is exactly
#: what a client should do — against the replacement runtime. The refusal also
#: carries this terminal `exit` frame so a client that predates the typed
#: `draining` event still terminates its request instead of waiting forever.
DRAINING_EXIT_CODE = 75
#: In-flight work outlived the deadline. Nonzero on purpose: a supervisor must
#: be able to tell "drained" from "gave up with work still running".
DRAIN_TIMEOUT_EXIT_CODE = 3
#: How long the drain monitor waits for the reader loop to actually unwind
#: after a clean drain before forcing the process down. The pool is provably
#: empty at that point, so this only covers a reader blocked on an idle pipe.
_DRAIN_EXIT_GRACE_SECONDS = 5.0
#: The mirror image: how long the READER waits for the drain monitor to publish
#: its terminal frame when the transport closed first (a `shutdown` op or EOF
#: arriving mid-drain). The pool has already been joined by then, so the monitor
#: is normally one poll interval away; past this bound the drain is declared
#: abandoned IN A FRAME rather than exiting silently.
_DRAIN_ABANDON_GRACE_SECONDS = 5.0

# Chat turns must survive supervisor recycles (recording safety): these argv
# shapes mark a request as an in-flight chat turn for the busy/ping frame.
_CHAT_TURN_COMMANDS = (("mission-chat", "message"), ("mission-chat", "steer"))

# ── Read-model cache (follow-up slice 1 of the serve design doc) ─────────────
#
# The Launcher polls `harness status --json` / `harness snapshot --json` on a
# fixed cadence; each build recomputes the full projection (~1.7s status /
# ~7s snapshot warm) even when NOTHING changed. Serve is a warm process, so it
# caches the exact stdout payload of these read-only requests keyed by a
# runtime-state fingerprint (the sequence check) and replays it while the
# fingerprint holds.
#
# The fingerprint stats the cheap change signals: events.jsonl (every store
# mutation appends an event — the architecture's change feed), the turn store,
# scope pointers, the live store directories (record add/rename
# flips a directory's mtime), and the SessionDB files (chat writes; -wal /
# -journal included because a SQLite WAL commit does not touch the main db's
# mtime). Signals that live OUTSIDE the runtime root (git working trees for
# dirty state, provider health) cannot flip the fingerprint, so a TTL bounds
# their staleness: a cached payload older than _READ_CACHE_MAX_AGE_SECONDS is
# rebuilt even on a fingerprint match.
#
# Visibility: a replayed response stamps `served_from_cache` + `cache_age_ms`
# on its exit frame (additive), and the payload's own parity envelope keeps
# the honest original `generated_at`.

_CACHEABLE_ARGV: dict[tuple[str, ...], str] = {
    ("harness", "status", "--json"): "status",
    ("harness", "snapshot", "--json"): "snapshot",
}
_READ_CACHE_MAX_AGE_SECONDS = 20.0

_FINGERPRINT_ROOT_FILES = (
    "events.jsonl",
    "mission_chat_turns.json",
    "active_realm.json",
    "active_workspace.json",
)
_FINGERPRINT_STORE_DIRS = (
    "runs",
    "incidents",
    "agents",
    # S57 dropped "repo_bundles" here with the store: this list exists to
    # invalidate the read cache when a store directory changes, and no code path
    # can write that tree any more (S52 took the last writer, S57 the module).
    # Stat'ing it every poll was cost against a directory that cannot move. Same
    # rule S56 applied to "worker_sessions".
    "runtime_instances",
    "persona_instances",
    "persona_assignments",
    "workspaces",
    "realms",
    # DELIBERATELY ABSENT: "serve_instances". Its entries appear and vanish at
    # every serve boot/exit, and the ``serve_auth_token`` file appears at first
    # boot — inside a fingerprint either one would cold the read-model cache
    # exactly when a fresh runtime is warming up, and make the stream emit
    # ``state.reconciled`` on every restart. Same standing precedent as
    # ``dispatch_delivery.DRAIN_STATE_FILENAME``; the rule is restated at both
    # ``agent_runtime/serve_registry.py`` and ``agent_runtime/serve_auth.py``.
)

# The ``running_work`` durable stores (``processes.json``, ``state.db``) are
# fingerprinted too, but they live under the HERMES **home** rather than the
# agent-runtime store root — on a profiled install those are genuinely
# different directories — so they cannot ride the two tuples above. Their one
# path authority is ``agent_runtime.running_work.running_work_store_paths``,
# called from ``_runtime_state_fingerprint`` below; duplicating the names here
# would stand up a second list free to drift from the projection's.


_FINGERPRINT_BOARD_CARD_CAP = 600  # bounded per-board card stat; remainder is rare + also evented


def _stat_board_tree(root: Any, _stat) -> None:
    """Bounded stat of the boards/ subtree: the root, each board's def + card
    files + conflict dir. Card files are stat'd individually so in-place edits
    (move/edit rewrite a file without touching the dir mtime) still flip the
    fingerprint. Capped per board to stay cheap on the hot poll path."""

    boards_root = root / "boards"
    _stat(boards_root)
    try:
        board_dirs = sorted(p for p in boards_root.iterdir() if p.is_dir())
    except OSError:
        return
    for board_dir in board_dirs:
        _stat(board_dir / "board.json")
        cards_dir = board_dir / "cards"
        _stat(cards_dir)
        _stat(board_dir / "conflicts")
        try:
            card_files = sorted(cards_dir.glob("*.json"))
        except OSError:
            continue
        for card_path in card_files[:_FINGERPRINT_BOARD_CARD_CAP]:
            _stat(card_path)


_FINGERPRINT_TURN_FILE_CAP = 200  # session cap is 50; defensive bound only


def _stat_turn_store_tree(root: Any, _stat) -> None:
    """Bounded stat of the per-session turn store (mission_chat_turns/<key>.json).

    The turn-store split (one file per chat session) made the legacy
    `mission_chat_turns.json` root-file stat a dead signal: after migration the
    monolith is renamed aside and every streamed-turn flush rewrites ONE
    session file in place — which does not reliably move the directory mtime.
    Stat each session file individually (the board-tree pattern) so a cached
    snapshot can never serve stale turn elements. The legacy root file stays in
    _FINGERPRINT_ROOT_FILES so the one-time migration rename also flips the
    fingerprint."""

    turns_root = root / "mission_chat_turns"
    _stat(turns_root)
    try:
        session_files = sorted(turns_root.glob("*.json"))
    except OSError:
        return
    for session_path in session_files[:_FINGERPRINT_TURN_FILE_CAP]:
        _stat(session_path)


def _runtime_state_fingerprint() -> tuple | None:
    """Cheap stat-based sequence check over the harness read-model inputs.

    Returns None when the runtime root cannot be resolved — callers must
    treat None as "never cache"."""
    try:
        from agent_runtime import paths as _paths

        root = _paths.store_root()
    except Exception:
        return None
    parts: list[tuple[str, int, int]] = []

    def _stat(path: Any) -> None:
        try:
            st = os.stat(path)
        except OSError:
            parts.append((str(path), -1, -1))
            return
        parts.append((str(path), st.st_mtime_ns, st.st_size))

    for name in _FINGERPRINT_ROOT_FILES:
        _stat(root / name)
    for name in _FINGERPRINT_STORE_DIRS:
        _stat(root / name)
    # Background-work stores hang off the HERMES home, not the store root, and
    # resolve through the same head authority their WRITERS use
    # (``get_hermes_background_work_home``): ``persona_profile_context`` flips
    # ambient HERMES_HOME process-globally while a persona turn runs in THIS
    # process, so an ambient read here would fingerprint whichever profile
    # happened to be mid-turn.
    #
    # An EMPTY tuple means the authority could not resolve a home — "I cannot
    # fingerprint these", not "there is nothing to watch". Both that case and a
    # raised exception get the same sentinel, because caching against a silently
    # missing signal is exactly how a stale HUD gets served.
    try:
        from agent_runtime.running_work import running_work_store_paths

        store_paths = running_work_store_paths()
        if not store_paths:
            parts.append(("running_work_stores", -1, -1))
        for path in store_paths:
            _stat(path)
    except Exception:
        parts.append(("running_work_stores", -1, -1))
    # Event-log rotation (C6a) moves appends off the static "events.jsonl" onto a
    # rotating live slice, so the _FINGERPRINT_ROOT_FILES entry above freezes once
    # the log rotates. Stat the manifest (flips on each rotation) AND the resolved
    # live slice (flips on every append) so a cached snapshot never serves stale
    # frames after rotation. Pre-rotation the live slice IS events.jsonl (a
    # harmless duplicate stat); the manifest is absent (a stable -1/-1 signal).
    try:
        from agent_runtime import event_rotation as _event_rotation

        _stat(_event_rotation.manifest_path())
        _stat(_event_rotation.live_path())
    except Exception:
        parts.append(("event_log_rotation", -1, -1))
    # Mission Board tree is nested two levels deep (boards/<id>/cards/<card>.json),
    # so a top-level dir stat alone misses card adds/moves/in-place edits and
    # pull-materialized cards. Every board mutation also advances events.jsonl
    # (already fingerprinted), but a bounded subtree walk here keeps cached
    # snapshots honest even for event-less file materialization (realm pull).
    _stat_board_tree(root, _stat)
    # Per-session turn store: streamed-turn flushes rewrite one session file in
    # place and emit NO EventLog event, so without these stats a cached snapshot
    # would serve stale turn elements.
    _stat_turn_store_tree(root, _stat)
    try:
        # Fingerprint the database the CHAT LANE actually writes, not the one
        # ambient HERMES_HOME resolution happens to hand this process. A bare
        # ``SessionDB()`` keyed the cache on ``HERMES_HOME/state.db`` while every
        # chat write goes to the resolved chat scope; whenever the two diverge a
        # cached snapshot could serve a frozen Chat History for the life of the
        # serve process (defect D1 in
        # ``docs/agent-runtime-harness/chat-session-presence-authority.md``,
        # the serve twin of the stream-lane fix 639242901). Resolving the PATH
        # also stops the poll loop from opening — and potentially creating — a
        # database just to read its own filename.
        from agent_runtime.chat_session_scope import chat_session_db_path

        db_path = str(chat_session_db_path())
        for suffix in ("", "-wal", "-journal"):
            _stat(db_path + suffix)
    except Exception:
        # Chat persistence unavailable → its absence is itself stable.
        parts.append(("session_db", -1, -1))
    return tuple(parts)


class _ReadModelCacheEntry:
    __slots__ = ("fingerprint", "lines", "code", "built_monotonic")

    def __init__(
        self, fingerprint: tuple, lines: list[str], code: int, built_monotonic: float
    ):
        self.fingerprint = fingerprint
        self.lines = lines
        self.code = code
        self.built_monotonic = built_monotonic


class _ReadModelCache:
    """Per-serve-loop response cache for the read-only poll commands."""

    def __init__(self, max_age_seconds: float = _READ_CACHE_MAX_AGE_SECONDS):
        self._entries: dict[str, _ReadModelCacheEntry] = {}
        self._lock = threading.Lock()
        self._max_age = max_age_seconds

    def get(
        self, key: str, fingerprint: tuple | None, now_monotonic: float
    ) -> _ReadModelCacheEntry | None:
        if fingerprint is None:
            return None
        with self._lock:
            entry = self._entries.get(key)
        if entry is None or entry.fingerprint != fingerprint:
            return None
        if now_monotonic - entry.built_monotonic > self._max_age:
            return None
        return entry

    def put(
        self,
        key: str,
        fingerprint: tuple | None,
        lines: list[str],
        code: int,
        now_monotonic: float,
    ) -> None:
        # Only successful builds are worth replaying; a failed build must
        # re-run so the error stays live, not fossilized.
        if fingerprint is None or code != 0:
            return
        with self._lock:
            self._entries[key] = _ReadModelCacheEntry(
                fingerprint, lines, code, now_monotonic
            )

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "harness_serve_request_id", default=None
)

#: WHERE this request's frames go. Bound by ``_run`` alongside the request id,
#: for the same span and in the same pool-worker context.
#:
#: A durable service answers more than one transport, and a handler's ``print``
#: belongs to the client that asked — not to whoever owns stdout. Unset (the
#: default) means stdout, which is every stdio request and every thread a
#: handler spawns for itself, so the stdio lane is byte-identical to the
#: pre-socket loop: same proxy, same frames, same order.
_request_sink: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "harness_serve_request_sink", default=None
)


def current_serve_request_id() -> str | None:
    """The serve frame-protocol request id bound to THIS context, or None.

    ``_run`` binds it for exactly the span of one serve-dispatched request, in
    the pool-worker context that dispatches to the command handler — so a
    non-None answer is DIRECT provenance that the current work arrived as a
    serve frame request. Every other lane reads None: a one-shot CLI turn, the
    delivery drain's forged turns, background threads.

    This is the honest "did this turn arrive via serve" fact, and the only one.
    Two proxies have already been retired for impersonating it (both live
    2026-08-09 findings): ``persona_chat_runtime_registry() is not None`` is
    really "the hot-sessions CACHE is enabled" (default off, so every live
    serve read False), and ``delivery_drain_is_live()`` is really "a delivery
    consumer exists" — a serve whose drain died is still a serve. Provenance
    questions read THIS; capability questions read the drain.
    """

    return _request_id.get()


class _FrameWriter:
    """Sole owner of the real stdout; one lock keeps frames atomic."""

    def __init__(self, stream: TextIO):
        self._stream = stream
        self._lock = threading.Lock()

    def emit(self, frame: dict[str, Any]) -> None:
        payload = json.dumps(frame, ensure_ascii=False, default=str)
        with self._lock:
            self._stream.write(payload + "\n")
            self._stream.flush()


class _LineFrameProxy(io.TextIOBase):
    """Stand-in for sys.stdout/sys.stderr that re-emits handler output as
    tagged line frames, buffered per request id until a newline."""

    def __init__(self, frames: _FrameWriter, event: str):
        super().__init__()
        self._frames = frames
        self._event = event
        self._buffers: dict[tuple[int | None, str | None], str] = {}
        self._captures: dict[tuple[int | None, str | None], list[str]] = {}
        self._lock = threading.Lock()

    def writable(self) -> bool:  # pragma: no cover - io protocol
        return True

    def isatty(self) -> bool:
        # Handlers key default output on isatty(); serve is a pipe.
        return False

    @staticmethod
    def _slot(rid: str | None) -> tuple[int | None, str | None]:
        """The partial-line buffer this write belongs to.

        Keyed by (destination, request id), not by request id alone. Request
        ids are chosen by CLIENTS, so once more than one transport is attached
        two connections may legitimately both be running ``req-1`` — and a
        buffer keyed on the id alone would splice one client's half-written
        line into the other's. Stdio's destination is None (stdout), which is
        what every pre-socket request already was.
        """

        sink = _request_sink.get()
        return (id(sink) if sink is not None else None, rid)

    def write(self, text: str) -> int:
        if not text:
            return 0
        rid = _request_id.get()
        slot = self._slot(rid)
        with self._lock:
            buffered = self._buffers.get(slot, "") + str(text)
            *lines, remainder = buffered.split("\n")
            self._buffers[slot] = remainder
            capture = self._captures.get(slot)
            if capture is not None:
                capture.extend(lines)
        sink = _request_sink.get() or self._frames
        for line in lines:
            sink.emit({"id": rid, "event": self._event, "line": line})
        return len(text)

    def flush(self) -> None:  # pragma: no cover - io protocol
        return None

    def begin_capture(self, rid: str | None) -> None:
        """Start mirroring [rid]'s emitted lines for the read-model cache."""
        with self._lock:
            self._captures[self._slot(rid)] = []

    def end_capture(self, rid: str | None) -> list[str]:
        """Stop mirroring and return everything captured for [rid]."""
        with self._lock:
            return self._captures.pop(self._slot(rid), [])

    def flush_request(self, rid: str | None) -> None:
        """Emit a request's unterminated tail (handler printed without a
        trailing newline) and drop its buffer."""
        slot = self._slot(rid)
        with self._lock:
            remainder = self._buffers.pop(slot, "")
            if remainder:
                capture = self._captures.get(slot)
                if capture is not None:
                    capture.append(remainder)
        if remainder:
            sink = _request_sink.get() or self._frames
            sink.emit({"id": rid, "event": self._event, "line": remainder})


class _SafeSink:
    """A frame sink that never raises — the socket lane's request path.

    A pool worker's ``finally`` MUST emit its terminal ``exit`` frame and clean
    up its inflight entry; a client that hung up mid-request would otherwise
    take that bookkeeping down with it, leaking the request id forever and
    stalling any drain waiting on it. Stdout is deliberately NOT wrapped: the
    stdio pipe failing is the process losing its transport, and that has always
    propagated.
    """

    __slots__ = ("_target", "write_failures")

    def __init__(self, target: Any):
        self._target = target
        self.write_failures = 0

    def emit(self, frame: dict[str, Any]) -> None:
        try:
            self._target.emit(frame)
        except Exception:
            self.write_failures += 1


class _ArgvRequest:
    __slots__ = (
        "rid",
        "argv",
        "is_chat_turn",
        "is_runtime_stream",
        "cancel_event",
        "key",
        "owner",
        "sink",
    )

    def __init__(
        self,
        rid: str,
        argv: list[str],
        *,
        owner: str = "stdio",
        sink: Any = None,
    ):
        self.rid = rid
        self.argv = argv
        tail = argv[1:] if argv and argv[0] == "harness" else argv
        self.is_chat_turn = any(
            tuple(tail[: len(shape)]) == shape for shape in _CHAT_TURN_COMMANDS
        )
        self.is_runtime_stream = bool(tail and tail[0] == "stream")
        self.cancel_event = threading.Event()
        #: Which connection asked. ``stdio`` for the inherited pipe.
        self.owner = owner
        #: The inflight-table key. Request ids are chosen by CLIENTS, so two
        #: connections may legitimately both use ``req-1``; the table is keyed
        #: per owner so neither can collide with — or cancel — the other's work.
        #: Stdio keeps the bare id, so its frames and its drain reports are
        #: byte-identical to the single-transport loop.
        self.key = rid if owner == "stdio" else f"{owner}:{rid}"
        #: Where this request's frames go. None means stdout.
        self.sink = sink


class _DrainState:
    """One drain in progress, and everything its terminal frame must account for.

    The counters are the point. A `drain_complete` that only said "done" would
    be a frame with the right NAME and no evidence — it could not distinguish a
    drain that let three turns land from one that refused them all, which is
    the difference between a safe restart and lost work.
    """

    __slots__ = ("started_monotonic", "deadline_seconds", "refused", "completed", "lock")

    def __init__(self, deadline_seconds: float):
        self.started_monotonic = time.monotonic()
        self.deadline_seconds = deadline_seconds
        self.refused = 0
        self.completed = 0
        self.lock = threading.Lock()

    def note_refused(self) -> int:
        with self.lock:
            self.refused += 1
            return self.refused

    def note_completed(self) -> None:
        with self.lock:
            self.completed += 1

    def counters(self) -> dict[str, Any]:
        with self.lock:
            return {"requests_refused": self.refused, "requests_completed": self.completed}

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_monotonic) * 1000)


def _drain_deadline_seconds(raw: Any, default: float) -> float:
    """Clamp a client-supplied deadline into the sane band, or take the default."""

    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    return max(_DRAIN_DEADLINE_MIN_SECONDS, min(float(raw), _DRAIN_DEADLINE_MAX_SECONDS))


def _build_harness_parser() -> argparse.ArgumentParser:
    """A fresh top-level parser holding only the harness tree. Built per
    request: cheap next to any handler, and avoids sharing one parser
    across pool threads."""
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_parser(subparsers)
    return parser


def dispatch_argv(argv: list[str]) -> int:
    """Parse and run one request exactly as ``hermes <argv…>`` would,
    including the harness error-envelope contract."""
    from hermes_cli.harness import emit_harness_error

    parser = _build_harness_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if func is None:
        parser.parse_args([*argv, "--help"])  # exits 0 after printing help
        return 0
    try:
        code = func(args)
    except SystemExit:
        raise
    except BaseException as exc:  # mirror hermes_cli.main harness dispatch
        return emit_harness_error(exc, args=args)
    return code if isinstance(code, int) else 0


def _prewarm_read_model_snapshot() -> None:
    """Build ONE read-model core in the background right after ``ready``.

    A fresh serve child's first snapshot build costs ~7.5s against ~2.2s warm
    (measured 2026-08-09, B4/B11): ~5s of that is per-process cache fill —
    YAML parse cache, tool-visibility memos, skill resolution, the event tail.
    Serve is long-lived, so paying it on a daemon thread the moment the child
    is ready takes it off whichever request would otherwise have been first.

    Read-only by construction: ``build_snapshot`` projects, it does not write
    (``write_snapshot`` is the writer, and is not called here). Concurrency is
    handled by the builder's own coalescing — a real request arriving mid-build
    joins it (hydrate) or waits and shares the next one; it never double-builds.
    Best effort by contract: a failure here surfaces on the first real request
    exactly as it would have without the prewarm.
    """

    try:
        from agent_runtime.snapshot import build_snapshot

        build_snapshot()
    except Exception:
        import logging as _logging

        _logging.getLogger(__name__).debug(
            "serve snapshot prewarm did not complete", exc_info=True
        )


def _prewarm_provider_runtime() -> None:
    """Best-effort warmup of the per-process one-time costs a chat turn pays.

    Runs on a daemon thread right after the ready frame. Each step is
    independent and failure-isolated: a broken CA bundle or missing provider
    dependency surfaces on the first real turn with its normal typed error,
    exactly as it would without prewarm.
    """
    try:
        from agent.process_bootstrap import _load_openai_cls, shared_ssl_context

        _load_openai_cls()
        shared_ssl_context()
    except Exception:
        pass
    try:
        from agent.ssl_guard import verify_ca_bundle

        verify_ca_bundle()
    except Exception:
        pass
    try:
        from model_tools import get_tool_definitions

        # The exact cache key varies per persona toolset; this call warms the
        # shared parts (tool module imports, registry build, config parse).
        get_tool_definitions(quiet_mode=True)
    except Exception:
        pass


def serve_loop(
    reader: TextIO,
    writer: TextIO,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    dispatch: Callable[[list[str]], int] = dispatch_argv,
    fingerprint: Callable[[], tuple | None] = _runtime_state_fingerprint,
    read_cache_max_age: float = _READ_CACHE_MAX_AGE_SECONDS,
    liveness_pump_interval_seconds: float = 5.0,
    boot_timeline: Any = None,
    snapshot_prewarm: Callable[[], None] | None = None,
    root_anchor: Callable[[], Any] | None = None,
    drain_deadline_seconds: float = DEFAULT_DRAIN_DEADLINE_SECONDS,
    drain_poll_interval_seconds: float = _DRAIN_POLL_INTERVAL_SECONDS,
    drain_wakeup: Callable[[], None] | None = None,
    hard_exit: Callable[[int], None] | None = None,
    socket_lane: bool = False,
    stream_source_factory: Callable[[], Any] | None = None,
    stream_buffer_limit: int | None = None,
) -> int:
    """Core dispatch loop over explicit streams. stdio is transport #1; the
    localhost socket is transport #2, and both feed THIS dispatcher.

    ``socket_lane`` is injected and OFF by default — the same contract as
    ``root_anchor`` and ``hard_exit`` — so every pre-socket test observes the
    byte-identical stdio loop, and a caller that wants the durable service says
    so explicitly. When it is on, this loop races for the per-root socket lock,
    binds an ephemeral loopback port before ``ready`` (so the ready frame and
    the registry entry can both carry it), and starts accepting only after the
    request pool exists.

    ``stream_source_factory`` is the shared subscription producer, likewise
    injectable: the default builds the real ``agent_runtime.stream``
    generator, and a test hands over a finite fake so a subscription test costs
    milliseconds instead of a projection build.

    ``drain_wakeup`` and ``hard_exit`` are the two process-level levers the
    drain needs and a unit test must not be given: the first unblocks a reader
    parked on an idle pipe once the drain has finished (the real entry point
    closes the protocol descriptor), the second takes the process down when
    in-flight work outlived the deadline. The timeout case CANNOT be a plain
    return: ``concurrent.futures`` registers an atexit hook that JOINS every
    worker thread, so an interpreter carrying a stuck worker hangs on the way
    out — which is the same "forever" the deadline exists to bound. Both are
    injected and OFF by default, the same contract as ``snapshot_prewarm`` and
    ``root_anchor``, so ``serve_loop``'s own tests observe the frames and the
    return code without ever exiting the test process.

    ``boot_timeline`` is the caller's already-running :class:`BootTimeline`
    (``_cmd_serve`` starts one at the process's first hermes instruction, so
    ``interpreter_ms`` covers the import tax); the loop starts its own when a
    caller supplies none. ``snapshot_prewarm`` is the post-``ready`` warmup
    policy — injected, and OFF unless the real entry point turns it on, so the
    loop's own unit tests never fire a multi-second projection build.
    """

    from agent_runtime.boot_timeline import BootTimeline

    timeline = boot_timeline if boot_timeline is not None else BootTimeline()
    frames = _FrameWriter(writer)
    # Emitted before ANY heavy boot work (the agent_runtime import, root
    # config load, registry init, and the pre-ready orphan sweep below): a
    # supervising launcher can tell a live cold boot from a wedged child by
    # this frame alone. A cold-cache boot can run past any short watchdog
    # before ``ready``; killing it mid-boot respawns into another cold boot
    # forever (2026-07-26 launcher kill-loop incident). Consumers that
    # predate this frame ignore unknown events, so it is purely additive.
    #
    # ``boot``: the self-attributing cold-boot stamp (T9). A >25s cold boot has
    # been recorded and is not reproducible on demand (the OS file cache is
    # warm on any machine that just ran the launcher), so the boot measures
    # itself instead: ``interpreter_ms`` here is the interpreter + hermes CLI
    # import tax the supervisor can see NO other way, and the ``ready`` frame
    # below carries the per-phase breakdown of everything after it.
    frames.emit(
        {
            "event": "booting",
            "pid": os.getpid(),
            "schema_version": SERVE_SCHEMA_VERSION,
            "boot": timeline.stamps(),
        }
    )
    from agent_runtime.persona_chat_continuity import (
        initialize_persona_chat_runtime_registry,
    )
    from agent_runtime.config import load_root_runtime_config

    persona_chat_cfg = load_root_runtime_config().persona_chat
    initialize_persona_chat_runtime_registry(
        enabled=persona_chat_cfg.hot_sessions_enabled,
        max_entries=persona_chat_cfg.max_hot_sessions,
        ttl_seconds=persona_chat_cfg.idle_ttl_seconds,
    )
    timeline.mark("chat_registry_ms")
    # Publish this process's EXPLICIT chat head home into the shared runtime
    # store root — the ONE writer of that pointer. The Launcher always starts
    # serve with HERMES_HEAD_HOME; a plain CLI turn started later names no head
    # and, without the pointer, degrades to its own profile database, minting
    # the transcript where the cockpit never looks while writing the binding
    # into the shared store (the 2026-07-27 read-lane gap). No-op when this
    # process named no head of its own, and best effort by contract.
    from agent_runtime.chat_session_scope import publish_chat_head_home

    publish_chat_head_home()
    timeline.mark("head_publish_ms")
    # Publish the machine root anchor: `agent_runtime.store_root` into the
    # PLATFORM DEFAULT home's config.yaml, so a later ambient process (no
    # HERMES_HOME, no HERMES_AGENT_RUNTIME_ROOT) resolves this serve's real
    # runtime root — and therefore finds the chat-head pointer above — instead
    # of the %LOCALAPPDATA% shadow runtime (the 2026-08-12 ambient
    # chat-history incident: `ok: true, count: 0` from the wrong root).
    # Injected and OFF unless the real entry point turns it on — the same
    # contract as ``snapshot_prewarm`` — so the loop's unit tests can never
    # write the machine-global config. Best effort by contract, but ACCOUNTED:
    # the typed outcome is emitted as its own frame either way, because a
    # silent skip here is exactly the false-all-clear class the anchor
    # retires. Consumers that predate this frame ignore unknown events.
    #
    # Since 2026-08-13 the same call also DECLARES `agent_runtime.head_home`
    # when this serve was started with an explicit head, and the frame carries
    # that outcome additively under `head`. That is the runtime declaring its
    # own identity: the Launcher's `HERMES_HEAD_HOME` pin demotes from sole
    # authority to an override plus a consistency check, and the launcher
    # compares its pin against this frame (a disagreement is a durable
    # `root_declaration_mismatch` transport receipt, never a silent divergence).
    if root_anchor is not None:
        try:
            anchor_report = root_anchor()
            anchor_frame = {"event": "root_anchor", **anchor_report.payload()}
        except Exception as exc:  # must never take the boot down
            anchor_frame = {
                "event": "root_anchor",
                "outcome": "unwritable",
                "detail": type(exc).__name__,
            }
        frames.emit(anchor_frame)
    timeline.mark("root_anchor_ms")
    stdout_proxy = _LineFrameProxy(frames, "line")
    stderr_proxy = _LineFrameProxy(frames, "stderr")
    read_cache = _ReadModelCache(read_cache_max_age)
    from agent_runtime.snapshot import SnapshotBuildContext

    read_build_context = SnapshotBuildContext()

    inflight: dict[str, _ArgvRequest] = {}
    # Futures by request id so ``{"op":"cancel"}`` can drop work that is
    # still queued behind the pool. A running request is uninterruptible —
    # cancel() then returns False and the client is told the side effect may
    # still land.
    inflight_futures: dict[str, Future] = {}
    inflight_lock = threading.Lock()

    # Drain state. ``None`` until a `drain` op arrives; from then on it is the
    # single answer to "are we still accepting work", read by the request path
    # and written once by the op.
    drain_state: _DrainState | None = None
    drain_exit_code = 0
    drain_finished = threading.Event()
    reader_unwound = threading.Event()
    pool_shutdown_wait = True
    boot_id = uuid.uuid4().hex

    # ── socket lane state (all None unless ``socket_lane`` is on AND this
    # serve wins the per-root ownership lock) ────────────────────────────────
    socket_server: Any = None
    socket_lock: Any = None
    #: The ONE stream producer, built on the first ``subscribe`` and stopped
    #: when the last subscriber leaves. Never per client: a delta batch rebuilds
    #: a full snapshot core, so N generators would cost N of them.
    stream_hub: Any = None
    stream_hub_lock = threading.Lock()

    def _busy_frame() -> dict[str, Any]:
        with inflight_lock:
            pending = len(inflight)
            chat_turns = sum(1 for item in inflight.values() if item.is_chat_turn)
        return {"event": "busy", "chat_turns": chat_turns, "pending": pending}

    def _service_log(payload: dict[str, Any]) -> None:
        """One structured line per transport event, on the serve's own stderr.

        Which means it arrives at the supervisor as an ordinary
        ``{"id":null,"event":"stderr","line":…}`` frame — the lane serve already
        uses for everything a handler writes to stderr. No new frame type, no
        new sink, and correlatable by ``boot_id`` against the ready frame.
        """

        try:
            sys.stderr.write(
                json.dumps(payload, ensure_ascii=False, default=str) + "\n"
            )
        except Exception:
            pass

    def _run(request: _ArgvRequest) -> None:
        from agent_runtime.request_control import request_cancel_scope

        token = _request_id.set(request.rid)
        # Answers go back to whoever asked. ``request.sink`` is None on stdio,
        # which leaves the contextvar unset and the proxy on stdout — the
        # pre-socket path, unchanged.
        sink_token = _request_sink.set(request.sink)
        sink: Any = request.sink if request.sink is not None else frames
        code = 1
        cache_key = _CACHEABLE_ARGV.get(tuple(request.argv))
        request_fingerprint: tuple | None = None
        served_from_cache = False
        cache_age_ms = 0
        capturing = False
        try:
            cached = None
            if cache_key is not None:
                request_fingerprint = fingerprint()
                cached = read_cache.get(
                    cache_key, request_fingerprint, time.monotonic()
                )
            if cached is not None:
                served_from_cache = True
                cache_age_ms = int(
                    (time.monotonic() - cached.built_monotonic) * 1000
                )
                code = cached.code
                for line in cached.lines:
                    sink.emit({"id": request.rid, "event": "line", "line": line})
                return
            if cache_key is not None and request_fingerprint is not None:
                stdout_proxy.begin_capture(request.rid)
                capturing = True
            try:
                with request_cancel_scope(request.cancel_event):
                    if cache_key is not None:
                        from agent_runtime.snapshot import snapshot_build_context_scope

                        with snapshot_build_context_scope(read_build_context):
                            code = dispatch(list(request.argv))
                    else:
                        code = dispatch(list(request.argv))
            except SystemExit as exc:  # argparse usage errors land here
                raw = exc.code
                code = raw if isinstance(raw, int) else (0 if raw is None else 2)
                if code != 0:
                    sink.emit(
                        {
                            "id": request.rid,
                            "event": "error",
                            "error": "argv_parse_failed",
                            "detail": "argparse rejected the request argv; usage was forwarded as stderr frames",
                        }
                    )
            except BaseException as exc:  # dispatch() already enveloped harness errors
                sink.emit(
                    {
                        "id": request.rid,
                        "event": "error",
                        "error": "dispatch_failed",
                        "detail": f"{type(exc).__name__}",
                    }
                )
        finally:
            stdout_proxy.flush_request(request.rid)
            stderr_proxy.flush_request(request.rid)
            if capturing:
                read_cache.put(
                    cache_key,
                    request_fingerprint,
                    stdout_proxy.end_capture(request.rid),
                    code,
                    time.monotonic(),
                )
            _request_id.reset(token)
            _request_sink.reset(sink_token)
            with inflight_lock:
                inflight.pop(request.key, None)
                inflight_futures.pop(request.key, None)
                # Accounted here rather than by the monitor's before/after
                # arithmetic: the monitor only ever sees the pending SET, so a
                # request that both started and finished during the drain would
                # be invisible to it.
                #
                # And accounted INSIDE the same critical section as the pop,
                # which it did not used to be. With the increment outside, a
                # request sat in a window where it was gone from ``inflight``
                # and not yet in ``completed`` — the monitor could observe an
                # empty pending set and publish ``drain_complete`` with a
                # completion count LOWER than the number of exits it had
                # actually let land (reproduced: 5 reported for 8 exits). The
                # counters are the drain's only evidence, so an under-count
                # reads to an operator as work the restart dropped.
                #
                # Lock order is inflight_lock → _DrainState.lock, and it is the
                # only nesting of the two: every other site takes them one after
                # the other, never one inside the other.
                if drain_state is not None:
                    drain_state.note_completed()
            exit_frame: dict[str, Any] = {
                "id": request.rid,
                "event": "exit",
                "code": code,
            }
            if served_from_cache:
                exit_frame["served_from_cache"] = True
                exit_frame["cache_age_ms"] = cache_age_ms
            sink.emit(exit_frame)

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_proxy, stderr_proxy
    try:
        store_root_path: Any = None
        try:
            from agent_runtime import paths as _paths

            store_root_path = _paths.store_root()
            runtime_root = str(store_root_path)
        except Exception:
            store_root_path = None
            runtime_root = None
        timeline.mark("store_root_ms")
        # ── Durable-service foundations (slice 2) ───────────────────────────
        #
        # These three run BEFORE ``ready`` because ``ready`` is the frame that
        # carries them: a client that has to ask a second question to learn
        # what code it just connected to has a window in which it does not
        # know, and windows like that are how a stale service serves a whole
        # session before anyone notices.
        #
        # 1. WHICH CODE. Today serve is a per-client child, so a launcher
        #    restart picks up landed fixes for free and nobody ever had to
        #    ask. A durable service silently pins last week's code instead —
        #    the shape of the dispatch dead-flag-proxy incident, which ran
        #    green for a week. Resolved once per process and cached.
        try:
            from agent_runtime.build_stamp import build_stamp

            build_block = build_stamp().frame_payload()
        except Exception as exc:  # an instrument must never take the boot down
            build_block = {
                "commit": None,
                "dirty": None,
                "source": "unknown",
                "resolved_at": None,
                "reason": f"stamp_failed:{type(exc).__name__}",
            }
        # 2. THE SECRET. Unwired to any transport (stdio needs none), minted
        #    now so the socket slice starts with a lock already on the door
        #    rather than shipping open. The frame carries the POSTURE only —
        #    the token value must never appear in a frame, a log, or an event.
        auth_block: dict[str, Any] = {"token_file": "error:root_unresolved"}
        if store_root_path is not None:
            try:
                from agent_runtime.serve_auth import ensure_token

                auth_block = ensure_token(store_root_path).payload()
            except Exception as exc:
                auth_block = {"token_file": f"error:{type(exc).__name__}"}
        # 3. THE TRANSPORT (slice 3). One serve per root owns the socket lane,
        #    decided by an OS-held exclusive lock rather than by who booted
        #    first: two serves against one root is a real, ordinary concurrency
        #    (a launcher restart overlaps its replacement), and "connect to the
        #    service for root X" must have exactly one answer. The loser keeps
        #    serving stdio and SAYS so on the ready frame — a socket that
        #    silently never came up is indistinguishable from one that is
        #    broken.
        #
        #    Bound here, BEFORE the registry entry and the ready frame, so both
        #    can carry the real port; accepting starts later, once the request
        #    pool exists (see ``start_accepting`` below). A client that connects
        #    in between waits in the listen backlog, which is what a backlog is
        #    for.
        socket_block: dict[str, Any] = {"outcome": "disabled"}
        socket_transport = "stdio"
        if socket_lane and store_root_path is not None:
            try:
                from agent_runtime.serve_socket import (
                    SOCKET_HOST,
                    ServeSocketServer,
                    SocketOwnerLock,
                )
                from agent_runtime.serve_auth import verify as _verify_serve_token

                socket_lock = SocketOwnerLock(store_root_path)
                lock_result = socket_lock.acquire()
                if lock_result.acquired:
                    socket_server = ServeSocketServer(
                        store_root_path,
                        boot_id=boot_id,
                        # Late-bound on purpose: these two closures are defined
                        # further down (they need the pool and the drain state),
                        # and a Python closure resolves its enclosing names at
                        # CALL time — which cannot happen before the accept loop
                        # starts, which is after both exist.
                        dispatch_line=lambda line, connection: _handle_socket_line(
                            line, connection
                        ),
                        hello_payload=lambda message, connection: _hello_ok_frame(
                            message, connection
                        ),
                        # The token is verified against THIS root, and the value
                        # never leaves ``serve_auth``: the transport only ever
                        # learns the boolean.
                        verify_token=lambda presented: _verify_serve_token(
                            presented, store_root_path
                        ),
                        on_disconnect=lambda connection: _release_subscription(
                            connection
                        ),
                        log=_service_log,
                    )
                    port = socket_server.bind()
                    socket_lock.publish_owner(
                        {
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            "host": SOCKET_HOST,
                            "port": port,
                            "started_at": socket_server.started_at,
                            "store_root": runtime_root,
                        }
                    )
                    socket_transport = "stdio+socket"
                    socket_block = {
                        "outcome": "listening",
                        "host": SOCKET_HOST,
                        "port": port,
                        "started_at": socket_server.started_at,
                    }
                else:
                    socket_block = lock_result.payload()
            except Exception as exc:
                # A transport that failed to come up must not take the runtime
                # with it: stdio still works, and the typed outcome is how an
                # operator learns the socket did not.
                try:
                    if socket_lock is not None:
                        socket_lock.release()
                except Exception:
                    pass
                socket_server = None
                socket_lock = None
                socket_block = {"outcome": f"error:{type(exc).__name__}"}
        # 4. DISCOVERY. Multiple runtime roots legitimately coexist on this
        #    machine (QA lanes, isolated worktree roots), and until now
        #    "how many serves are running against this root, on what code"
        #    had no answer at all. The entry is removed on every clean exit
        #    (shutdown AND drain); a crash leaves it, which is why liveness is
        #    proven at READ time and never trusted from the file.
        #
        #    The socket fields ride the SAME entry (additive): a client
        #    discovering "the service for root X" reads the port from the
        #    instance whose liveness the registry has just classified, rather
        #    than from a second file with its own staleness story.
        instance_block: dict[str, Any] = {"outcome": "error:root_unresolved"}
        if store_root_path is not None:
            try:
                from agent_runtime.serve_registry import register_serve_instance

                instance_block = register_serve_instance(
                    store_root_path,
                    transport=socket_transport,
                    build=build_block,
                    boot_id=boot_id,
                    port=socket_server.port if socket_server is not None else None,
                    socket_started_at=(
                        socket_server.started_at if socket_server is not None else None
                    ),
                ).payload()
            except Exception as exc:
                instance_block = {"outcome": f"error:{type(exc).__name__}"}
        timeline.mark("service_foundations_ms")
        # Orphaned-turn sweep BEFORE the ready frame: serve boot is the moment
        # a launcher restart replaces a dead runtime, and the first hydrate is
        # only requested after ready — so records a dead executor left frozen
        # in-flight (lease provably free) already project as typed
        # ``turn_interrupted`` markers in that hydrate instead of a console
        # stuck "running" forever. Bounded (≤50 session files) and fail-open.
        orphaned_repaired: list[str] = []
        try:
            from agent_runtime.persona_chat_continuity import repair_orphaned_chat_turns

            orphaned_repaired = repair_orphaned_chat_turns()
        except Exception:
            orphaned_repaired = []
        timeline.mark("orphaned_turn_sweep_ms")
        # Same moment, same reason, for detached dispatches: a row still marked
        # ``running`` whose owning process is provably gone can never finish, and
        # the sender is owed that answer too. Reclassifying it here — BEFORE the
        # drain starts — turns "the agent I dispatched went silent forever" into
        # a delivered "the outcome is unknown, re-send if you still need it".
        # Identity-verified (a recycled PID is not the old owner) and fail-open.
        dispatches_restored = 0
        try:
            from agent_runtime.dispatch_store import restore_undelivered_dispatches

            dispatches_restored = int(
                (restore_undelivered_dispatches() or {}).get("restored") or 0
            )
        except Exception:
            dispatches_restored = 0
        timeline.mark("dispatch_restore_ms")
        ready_frame: dict[str, Any] = {
            "event": "ready",
            "pid": os.getpid(),
            "schema_version": SERVE_SCHEMA_VERSION,
            "runtime_root": runtime_root,
            # Additive, always present (never conditional on success): a
            # missing block would read as "old runtime", while a block whose
            # own fields say `unknown`/`error:…` reads as what it is — the
            # measurement was attempted and this is what it found.
            "boot_id": boot_id,
            "build": build_block,
            "auth": auth_block,
            "instance": instance_block,
            # ``disabled`` (no socket lane asked for), ``listening`` with the
            # port, ``lock_held_by`` with the winner's pid, or ``error:<reason>``
            # — the outcome is stated either way, never inferred from absence.
            "socket": socket_block,
        }
        if orphaned_repaired:
            ready_frame["orphaned_turns_repaired"] = len(orphaned_repaired)
        if dispatches_restored:
            ready_frame["dispatches_restored"] = dispatches_restored
        # Every phase this boot actually paid, on the frame the supervisor
        # already waits for — and the same line in agent.log, because the boot
        # worth attributing (the cold one) is the boot nobody is watching a
        # console for. Emission is defensive: a broken instrument must never be
        # the reason a runtime fails to come up.
        try:
            ready_frame["boot_timeline"] = timeline.stamps()
        except Exception:
            pass
        # Read-model warmup starts BEFORE ``ready`` is announced, unlike the
        # provider warmup below. The launcher's first request lands within
        # milliseconds of this frame, and only the build that STARTED FIRST can
        # be shared: if the request wins the race it leads its own build and
        # the warmup then queues a second, redundant one behind it. Starting a
        # daemon thread costs microseconds, so ``ready`` is not delayed.
        if snapshot_prewarm is not None:
            threading.Thread(
                target=snapshot_prewarm,
                name="harness-serve-snapshot-prewarm",
                daemon=True,
            ).start()
        frames.emit(ready_frame)
        try:
            import logging as _logging

            _logging.getLogger(__name__).info(
                timeline.log_line("harness serve boot timeline:")
            )
        except Exception:
            pass
        # Prewarm the first chat turn's one-time costs in the background:
        # lazy OpenAI SDK import (~1.7s), shared SSL context / CA-guard
        # verification (~0.7s), and the tool-definition module imports +
        # registry build (~1.2s). Serve boots eagerly at Mission Control
        # open, so this runs while the operator is still looking at the
        # canvas — without it the FIRST message of every launcher session
        # pays the whole warmup inline. Best-effort: failures surface on
        # the first real turn exactly as they do today.
        threading.Thread(
            target=_prewarm_provider_runtime,
            name="harness-serve-prewarm",
            daemon=True,
        ).start()
        # (The read-model warmup runs on its own thread, started just before
        # the ready frame above — it must not queue behind this one's ~3s SDK
        # import, and it must start its build before the first request does.)
        # A busy serve must never look dead. The launcher's stream watchdog
        # keys on "no frames for N seconds", and when pool workers are deep in
        # chat-turn work the infinite `stream` request's generator can starve
        # past that budget — Mission Control then raised the loud "Runtime
        # offline" banner DURING healthy turns (live incident 2026-07-23,
        # two flaps inside one 4-minute Neko turn). This dedicated thread
        # emits the same typed `busy` frame the `ping` op returns whenever
        # requests are in flight: pure liveness telemetry on the shared
        # stdout, independent of every pool worker, so the launcher can
        # distinguish "busy running your turn" from "gone".
        liveness_stop = threading.Event()

        def _liveness_pump() -> None:
            while not liveness_stop.wait(liveness_pump_interval_seconds):
                with inflight_lock:
                    busy = bool(inflight)
                if not busy:
                    continue
                try:
                    frames.emit(_busy_frame())
                except Exception:
                    # Writer gone — the main loop is on its way down too.
                    return

        threading.Thread(
            target=_liveness_pump,
            name="harness-serve-liveness",
            daemon=True,
        ).start()
        # Detached-dispatch delivery. This is the half that makes
        # `agent_chat_send(wait=false)` honest: the target's turn ran in the
        # background, its answer is durable, and this thread forges it back into
        # the SENDER's thread once that thread is idle. It lives HERE, and only
        # here, because serve is the one long-lived process that hosts persona
        # turns — a one-shot CLI exits long before a 30-minute dispatch lands.
        #
        # Started after `ready` (so a cold boot is never delayed by a delivery)
        # and stopped with the liveness pump before `shutdown` (so it cannot
        # forge a turn into a process that is on its way down). Best effort by
        # contract: a runtime that cannot start the drain still serves, and the
        # completions stay pending for the next boot rather than being lost.
        try:
            from agent_runtime.dispatch_delivery import start_delivery_drain

            # Rehydrate durable delegation completions BEFORE the drain that
            # will deliver them starts — explicit at serve boot, never as an
            # import side effect (same #16856 class as module-scope MCP
            # discovery; see
            # docs/agent-runtime-harness/eager-tool-discovery-audit-2026-08-09.md).
            from tools.process_registry import process_registry

            process_registry.restore_durable_completions()
            start_delivery_drain(stop_event=liveness_stop)
        except Exception:
            # Function-local: parts files are exec'd into harness.py's globals,
            # which carry no module logger.
            #
            # WARNING, not debug: a drain that fails to start disables the
            # entire `agent_chat_send(wait=false)` lane for the life of this
            # serve — every dispatch is refused with
            # `async_delivery_unavailable` — and at debug level that
            # feature-killing fact was invisible in every live log
            # (2026-08-09 dispatch-lane investigation).
            import logging as _logging

            _logging.getLogger(__name__).warning(
                "dispatch delivery drain did not start; "
                "agent_chat_send(wait=false) will be refused for this serve",
                exc_info=True,
            )

        def _unregister_instance() -> None:
            """Drop this serve's registry entry. Idempotent, never raises."""

            if store_root_path is None:
                return
            try:
                from agent_runtime.serve_registry import (
                    serve_instance_path,
                    unregister_serve_instance,
                )

                if not unregister_serve_instance(store_root_path):
                    # Reported, not swallowed. A clean exit that leaves its
                    # entry behind makes the registry claim a serve that is on
                    # its way out, and the next client's discovery would try to
                    # connect to it. The read-time classification eventually
                    # calls it dead — "eventually" is the part an operator has
                    # to be able to see coming.
                    if serve_instance_path(store_root_path, os.getpid()).exists():
                        _service_log(
                            {
                                "event": "serve_instance_unregister_failed",
                                "boot_id": boot_id,
                                "pid": os.getpid(),
                                "path": str(
                                    serve_instance_path(store_root_path, os.getpid())
                                ),
                            }
                        )
            except Exception:
                pass

        # ── socket lane plumbing ────────────────────────────────────────────
        #
        # Everything below is inert on a stdio-only serve: ``socket_server`` is
        # None, no connection ever exists, and the stdio path never reaches a
        # branch that touches it.

        connection_sinks: dict[str, _SafeSink] = {}
        connection_sinks_lock = threading.Lock()

        def _emit_safely(sink: Any, frame: dict[str, Any]) -> None:
            try:
                sink.emit(frame)
            except Exception:
                pass

        def _sink_for(connection: Any) -> Any:
            """The STABLE per-connection request sink.

            Stable matters twice: the partial-line buffers in
            ``_LineFrameProxy`` are keyed on the sink's identity, and a sink
            rebuilt per line would split one handler's output across two
            buffers mid-line.
            """

            if connection is None:
                return frames
            with connection_sinks_lock:
                sink = connection_sinks.get(connection.key)
                if sink is None:
                    sink = _SafeSink(connection)
                    connection_sinks[connection.key] = sink
                return sink

        def _owner_of(connection: Any) -> str:
            return "stdio" if connection is None else str(connection.key)

        def _stream_source() -> Any:
            """The shared subscription producer. One per serve, never per client."""

            if stream_source_factory is not None:
                return stream_source_factory()
            from agent_runtime.serde import to_jsonable
            from agent_runtime.stream import stream_frames

            def _generate():
                for frame in stream_frames():
                    # Byte-for-byte the frames ``harness stream`` writes: a
                    # subscriber folds the same hydrate/delta/patch/heartbeat
                    # shapes it already folds, so the socket lane introduces no
                    # second stream contract to keep in sync.
                    yield to_jsonable(frame)

            return _generate()

        def _ensure_stream_hub() -> Any:
            nonlocal stream_hub
            with stream_hub_lock:
                if stream_hub is None:
                    from agent_runtime.serve_stream_hub import (
                        DEFAULT_BUFFER_LIMIT,
                        StreamHub,
                    )

                    stream_hub = StreamHub(
                        _stream_source,
                        buffer_limit=int(stream_buffer_limit or DEFAULT_BUFFER_LIMIT),
                        log=_service_log,
                    )
                return stream_hub

        def _release_subscription(connection: Any) -> None:
            """A client left. Unsubscribe it, and do NOTHING else.

            Not a cancellation, not a shutdown, not a state change: the runtime
            outliving its clients is the entire point of the durable service,
            and a disconnect that touched backend state would reintroduce the
            per-client lifecycle ownership this workstream exists to retire.
            """

            key = _owner_of(connection)
            if stream_hub is not None:
                try:
                    stream_hub.unsubscribe(key)
                except Exception:
                    pass
            if connection is not None:
                connection.subscribed = False
                with connection_sinks_lock:
                    connection_sinks.pop(connection.key, None)

        def _close_socket_lane(reason: str) -> None:
            """Stop the hub, close every connection, release the ownership lock.

            Idempotent and never raises: it runs on the drain path, the
            shutdown path, and the EOF path, and any of them may be second.
            """

            nonlocal socket_server, socket_lock, stream_hub
            hub, stream_hub = stream_hub, None
            if hub is not None:
                try:
                    hub.stop()
                except Exception:
                    pass
            server, socket_server = socket_server, None
            if server is not None:
                try:
                    server.close(reason=reason)
                except Exception:
                    pass
            lock, socket_lock = socket_lock, None
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    pass

        def _build_mismatch(client_build: Any) -> bool | None:
            """Does the client's build disagree with the code answering it?

            None means NOT COMPARABLE — the client named no build, or this
            runtime could not measure its own. A fabricated ``false`` there
            would answer "you are current" for a runtime that does not know,
            which is exactly the false-all-clear the build stamp exists to
            retire. Prefix comparison so a short hash and a full one agree.
            """

            serve_commit = build_block.get("commit")
            if not isinstance(serve_commit, str) or not serve_commit:
                return None
            if not isinstance(client_build, str) or len(client_build.strip()) < 7:
                return None
            claimed = client_build.strip().lower()
            actual = serve_commit.lower()
            return not (
                actual.startswith(claimed) or claimed.startswith(actual)
            )

        def _hello_ok_frame(message: dict[str, Any], connection: Any) -> dict[str, Any]:
            """The version handshake, enforced end to end at the door."""

            return {
                "event": "hello_ok",
                "pid": os.getpid(),
                "boot_id": boot_id,
                # The frame-protocol contract this service speaks. A client that
                # does not recognise it must not proceed on hope.
                "contract": SERVE_SCHEMA_VERSION,
                "schema_version": SERVE_SCHEMA_VERSION,
                "transport": "socket",
                "connection": connection.key,
                "runtime_root": runtime_root,
                "build": build_block,
                # Visible, never fatal: a client on other code still gets to
                # work, and now KNOWS it is talking to a different build.
                "build_mismatch": _build_mismatch(connection.client_build),
                "draining": drain_state is not None,
            }

        def _connections_frame() -> dict[str, Any]:
            payload: dict[str, Any] = {"event": "socket_connections", "boot_id": boot_id}
            if socket_server is None:
                payload["enabled"] = False
                payload["socket"] = socket_block
                payload["count"] = 0
                payload["connections"] = []
            else:
                payload["enabled"] = True
                payload.update(socket_server.connections_payload())
            payload["subscriptions"] = (
                stream_hub.stats() if stream_hub is not None else {"subscribers": 0}
            )
            return payload

        def _handle_socket_line(line: str, connection: Any) -> None:
            """Every authenticated socket line enters the SHARED dispatcher."""

            _handle_line(line, _sink_for(connection), connection=connection)

        def _finish_drain(code: int, frame: dict[str, Any]) -> None:
            """Emit the drain's terminal frame, then get the process out.

            Order is the contract: the frame is written and flushed BEFORE any
            exit path, because a drain that took the process down without
            accounting for what it refused and what it completed is
            indistinguishable from the crash the drain exists to replace.
            """

            nonlocal drain_exit_code, pool_shutdown_wait

            drain_exit_code = code
            frames.emit(frame)
            # Socket clients are owed the SAME terminal frame: a client that
            # asked for the drain over the socket, and every client that was
            # merely attached, learns how it ended on the transport it is on.
            # Broadcast before teardown — after ``_close_socket_lane`` there is
            # nobody left to tell.
            if socket_server is not None:
                try:
                    socket_server.broadcast(frame)
                except Exception:
                    pass
            _close_socket_lane(reason="drain")
            _unregister_instance()
            if code != 0:
                # Stuck workers: do NOT let the pool's context manager join
                # them (it would hang exactly as long as "forever"), and do not
                # trust a plain return either — concurrent.futures' atexit hook
                # joins worker threads on the way out of the interpreter.
                pool_shutdown_wait = False
            drain_finished.set()
            # The forced-exit watchdog is armed BEFORE the wakeup, and on a
            # thread of its own, because THE WAKEUP ITSELF CAN BLOCK. Observed
            # live on Windows against a real serve child (2026-08-13, the first
            # end-to-end drain this stack has ever run): the wakeup closes the
            # protocol descriptor the reader is parked on, and closing a handle
            # with a synchronous read pending does not return until that read
            # does. With the exit gated behind it, the process outlived its own
            # completed drain — the terminal frame was published, the socket
            # lane released, the registry entry removed, and the child then sat
            # there forever. A drain that can hang forever is not a drain, so
            # nothing on the path to the exit may be allowed to block.
            if hard_exit is not None:
                threading.Thread(
                    target=_force_exit_when_reader_is_stuck,
                    args=(code,),
                    name="harness-serve-drain-exit",
                    daemon=True,
                ).start()
            if drain_wakeup is not None:
                try:
                    drain_wakeup()
                except Exception:
                    pass
            if hard_exit is None:
                # Unit-test path: the loop returns ``drain_exit_code`` and the
                # caller observes the frames. No process-level lever is pulled.
                return
            if code != 0:
                hard_exit(code)
                return
            # Clean drain: the reader gets its chance to unwind normally
            # (closed sockets, flushed writer, restored stdio) and the watchdog
            # above forces the exit if it does not. Nothing is waited on here.

        def _force_exit_when_reader_is_stuck(code: int) -> None:
            """Force the process down if the reader never unwinds after a drain.

            The grace covers the normal case — the wakeup lands, the reader sees
            end-of-stream, and the loop returns through its own teardown — so
            this fires only when the platform would otherwise leave a drained
            runtime running.
            """

            if reader_unwound.wait(_DRAIN_EXIT_GRACE_SECONDS):
                return
            if hard_exit is not None:
                hard_exit(code)

        def _drain_monitor(state: _DrainState) -> None:
            deadline = state.started_monotonic + state.deadline_seconds
            last_progress = state.started_monotonic
            while True:
                with inflight_lock:
                    remaining = sorted(inflight)
                if not remaining:
                    _finish_drain(
                        0,
                        {
                            "event": "drain_complete",
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            **state.counters(),
                            "drain_ms": state.elapsed_ms(),
                        },
                    )
                    return
                now = time.monotonic()
                if now >= deadline:
                    _finish_drain(
                        DRAIN_TIMEOUT_EXIT_CODE,
                        {
                            "event": "drain_timeout",
                            "pid": os.getpid(),
                            "boot_id": boot_id,
                            **state.counters(),
                            "drain_ms": state.elapsed_ms(),
                            "deadline_seconds": state.deadline_seconds,
                            # WHICH requests are stuck, by id — a timeout that
                            # only reported a count would leave the operator
                            # with nothing to correlate against the stack dump.
                            "stuck_request_ids": remaining,
                        },
                    )
                    return
                if now - last_progress >= _DRAIN_PROGRESS_INTERVAL_SECONDS:
                    frames.emit(
                        {
                            "event": "drain_progress",
                            "pending": len(remaining),
                            "request_ids": remaining,
                            "drain_ms": state.elapsed_ms(),
                        }
                    )
                    last_progress = now
                time.sleep(max(0.0, min(drain_poll_interval_seconds, deadline - now)))

        # ── the shared dispatcher ───────────────────────────────────────────
        #
        # ONE op table, N transports. ``sink`` is where this message's answers
        # go (stdout for stdio, the originating connection for a socket client)
        # and ``connection`` is None on stdio. Every branch below was previously
        # inline in the stdio reader loop and is unchanged in behaviour: on
        # stdio, ``sink is frames`` and ``connection is None``, so the frames,
        # their order, and the exit codes are byte-identical.

        def _handle_line(line: str, sink: Any, *, connection: Any = None) -> str | None:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                sink.emit(
                    {
                        "id": None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": "request line is not valid JSON",
                    }
                )
                return None
            if not isinstance(message, dict):
                sink.emit(
                    {
                        "id": None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": "request must be a JSON object",
                    }
                )
                return None
            return _handle_message(message, sink, connection=connection)

        def _handle_message(
            message: dict[str, Any], sink: Any, *, connection: Any = None
        ) -> str | None:
            """Answer one op. Returns ``"shutdown"`` to stop the stdio reader."""

            nonlocal drain_state

            op = message.get("op")
            if op == "ping":
                sink.emit(_busy_frame())
                return None
            if op == "hello":
                # The socket lane authenticates BEFORE this dispatcher ever
                # sees a line, so a hello arriving here is a second one (or a
                # stdio client speaking the socket handshake at a pipe that
                # needs no handshake). Typed, and never a second auth path.
                sink.emit(
                    {
                        "event": "error",
                        "error": "unexpected_hello",
                        "detail": (
                            "this connection is already established; hello is the "
                            "first line of a SOCKET connection only"
                        ),
                    }
                )
                return None
            if op == "version":
                # Re-askable at any time, and deliberately NOT re-measured:
                # the answer is what code THIS interpreter loaded, which
                # cannot change while it lives. A client comparing against
                # its own install is how "the service is stale" becomes a
                # measurement instead of a theory.
                try:
                    from agent_runtime.build_stamp import build_stamp

                    version_build = build_stamp().payload()
                except Exception as exc:
                    version_build = {
                        "commit": None,
                        "dirty": None,
                        "source": "unknown",
                        "reason": f"stamp_failed:{type(exc).__name__}",
                    }
                sink.emit(
                    {
                        "event": "version",
                        "schema_version": SERVE_SCHEMA_VERSION,
                        "pid": os.getpid(),
                        "boot_id": boot_id,
                        # The transport THIS reply came over — honest per
                        # connection, and unchanged for every stdio consumer.
                        "transport": "stdio" if connection is None else "socket",
                        "runtime_root": runtime_root,
                        "build": version_build,
                        "auth": auth_block,
                        "draining": drain_state is not None,
                        # Additive: what else is attached to this runtime, on
                        # the reply a client already asks for.
                        "socket": socket_block,
                        "connections": _connections_frame(),
                    }
                )
                return None
            if op == "connections":
                sink.emit(_connections_frame())
                return None
            if op == "subscribe":
                lane = message.get("lane", "stream")
                if lane != "stream":
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": lane,
                            "reason": "unsupported_lane",
                        }
                    )
                    return None
                if drain_state is not None:
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "draining",
                        }
                    )
                    return None
                key = _owner_of(connection)
                hub = _ensure_stream_hub()
                if hub.has(key):
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "already_subscribed",
                        }
                    )
                    return None
                raw_sink = connection.emit if connection is not None else frames.emit

                def _on_drop(reason: str, stats: dict[str, Any]) -> None:
                    # Typed, never silent: an unsubscribed client that was told
                    # nothing would keep folding a stream that stopped arriving
                    # and believe itself current.
                    _emit_safely(
                        sink,
                        {
                            "event": "subscription_dropped",
                            "lane": "stream",
                            "reason": reason,
                            "frames_delivered": stats.get("frames_delivered"),
                            "frames_discarded": stats.get("frames_discarded"),
                            "buffer_limit": stream_buffer_limit,
                        },
                    )
                    _release_subscription(connection)
                    _service_log(
                        {
                            "event": "serve_stream_subscription_dropped",
                            "boot_id": boot_id,
                            "connection": key,
                            "client": getattr(connection, "client", None),
                            "reason": reason,
                        }
                    )

                # The ACK precedes the subscription, deliberately. The producer
                # starts pushing the moment ``subscribe`` returns, so acking
                # afterwards would let the hydrate overtake the ack — and a
                # client reading "everything up to my ack is a reply to
                # something else" would discard its own baseline.
                if connection is not None:
                    connection.subscribed = True
                sink.emit(
                    {
                        "event": "subscribed",
                        "lane": "stream",
                        "connection": key,
                        "buffer_limit": hub.stats().get("buffer_limit"),
                    }
                )
                if not hub.subscribe(key, sink=raw_sink, on_drop=_on_drop):
                    # Lost a race with another subscribe for the same key. Say
                    # so rather than leave a client believing it is attached.
                    if connection is not None:
                        connection.subscribed = False
                    sink.emit(
                        {
                            "event": "subscribe_denied",
                            "lane": "stream",
                            "reason": "already_subscribed",
                        }
                    )
                return None
            if op == "unsubscribe":
                key = _owner_of(connection)
                was_subscribed = stream_hub is not None and stream_hub.has(key)
                _release_subscription(connection)
                sink.emit(
                    {
                        "event": "unsubscribed",
                        "lane": "stream",
                        "connection": key,
                        "was_subscribed": was_subscribed,
                    }
                )
                return None
            if op == "drain":
                if drain_state is not None:
                    sink.emit(
                        {
                            "event": "drain_in_progress",
                            "drain_ms": drain_state.elapsed_ms(),
                            **drain_state.counters(),
                        }
                    )
                    return None
                drain_state = _DrainState(
                    _drain_deadline_seconds(
                        message.get("deadline_seconds"), drain_deadline_seconds
                    )
                )
                # Stop the delivery drain (and with it the busy pump) the
                # moment we stop accepting work: it forges completed
                # dispatches back into a sender's thread, and doing that to
                # a process on its way down is exactly what the shutdown
                # path already refuses to allow. `drain_progress` frames
                # take over the liveness duty for the rest of the wait.
                liveness_stop.set()
                with inflight_lock:
                    pending_at_start = sorted(inflight)
                draining_frame = {
                    "event": "draining",
                    "id": None,
                    "pid": os.getpid(),
                    "boot_id": boot_id,
                    "pending": len(pending_at_start),
                    "request_ids": pending_at_start,
                    "deadline_seconds": drain_state.deadline_seconds,
                }
                frames.emit(draining_frame)
                if socket_server is not None:
                    # New sockets are refused from here (existing ones stay up
                    # to be told how it ends), and every attached client hears
                    # it at the same moment the stdio supervisor does.
                    try:
                        socket_server.begin_drain()
                        socket_server.broadcast(draining_frame)
                    except Exception:
                        pass
                threading.Thread(
                    target=_drain_monitor,
                    args=(drain_state,),
                    name="harness-serve-drain",
                    daemon=True,
                ).start()
                return None
            if op == "stacks":
                # Operator diagnostic: dump every thread's stack as
                # stderr frames (hung-request forensics without py-spy).
                import traceback

                for thread_id, frame in sys._current_frames().items():
                    sink.emit(
                        {
                            "id": None,
                            "event": "stderr",
                            "line": f"--- thread {thread_id} ---",
                        }
                    )
                    for entry in traceback.format_stack(frame):
                        for line in entry.rstrip().splitlines():
                            sink.emit(
                                {"id": None, "event": "stderr", "line": line}
                            )
                sink.emit({"event": "stacks_dumped"})
                return None
            if op == "shutdown":
                if connection is not None:
                    # A socket client does NOT get to kill a service other
                    # clients are using. `drain` is the multi-client lifecycle
                    # verb — it refuses new work, lets in-flight work land, and
                    # accounts for both — and `shutdown` stays what it has
                    # always been: the verb of the process that owns the pipe.
                    sink.emit(
                        {
                            "event": "error",
                            "error": "op_not_available_on_socket",
                            "detail": (
                                "shutdown is the stdio owner's verb; use "
                                '{"op":"drain"} to replace the service safely'
                            ),
                        }
                    )
                    return None
                return "shutdown"
            if op == "cancel":
                cancel_id = message.get("id")
                cancel_id = cancel_id.strip() if isinstance(cancel_id, str) else ""
                if not cancel_id:
                    sink.emit(
                        {
                            "id": None,
                            "event": "error",
                            "error": "invalid_request",
                            "detail": 'cancel needs {"op": "cancel", "id": "<request id>"}',
                        }
                    )
                    return None
                # Scoped to the asker's OWN work: the inflight table is keyed
                # per owner, so one client can neither cancel nor even observe
                # another's request id.
                owner = _owner_of(connection)
                cancel_key = (
                    cancel_id if owner == "stdio" else f"{owner}:{cancel_id}"
                )
                with inflight_lock:
                    future = inflight_futures.get(cancel_key)
                    running_request = inflight.get(cancel_key)
                    known = running_request is not None
                if future is not None and future.cancel():
                    with inflight_lock:
                        inflight.pop(cancel_key, None)
                        inflight_futures.pop(cancel_key, None)
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "exit",
                            "code": 130,
                            "cancelled": True,
                        }
                    )
                elif running_request is not None and running_request.is_runtime_stream:
                    # The state stream is read-only and infinite. Unlike a
                    # mutation, it has a cooperative cancellation seam and
                    # MUST release its worker when the Launcher reconnects;
                    # otherwise four watchdog cycles exhaust the entire
                    # serve pool with abandoned streams.
                    running_request.cancel_event.set()
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "cancel_accepted",
                            "state": "running",
                        }
                    )
                else:
                    # Already running (uninterruptible) or unknown — the
                    # side effect may still land; mutation verbs' own
                    # --issued-at replay guard is what makes that safe.
                    sink.emit(
                        {
                            "id": cancel_id,
                            "event": "cancel_denied",
                            "state": "running" if known else "unknown",
                        }
                    )
                return None
            rid = message.get("id")
            argv = message.get("argv")
            if (
                not isinstance(rid, str)
                or not rid.strip()
                or not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) for item in argv)
            ):
                sink.emit(
                    {
                        "id": rid if isinstance(rid, str) else None,
                        "event": "error",
                        "error": "invalid_request",
                        "detail": 'request needs {"id": "<non-empty>", "argv": ["harness", …]}',
                    }
                )
                return None
            if drain_state is not None:
                # Refused, and ACCOUNTED: the count lands on the terminal
                # drain frame, so "the restart dropped work" is a number an
                # operator can read rather than an inference.
                drain_state.note_refused()
                sink.emit(
                    {
                        "id": rid.strip(),
                        "event": "draining",
                        "detail": (
                            "serve is draining and is not accepting new requests; "
                            "reconnect to the replacement runtime"
                        ),
                        "drain_ms": drain_state.elapsed_ms(),
                    }
                )
                # Terminal frame too: a client that predates the `draining`
                # event is waiting for an `exit` and would otherwise hang
                # for the life of its request.
                sink.emit(
                    {
                        "id": rid.strip(),
                        "event": "exit",
                        "code": DRAINING_EXIT_CODE,
                        "draining": True,
                    }
                )
                return None
            request = _ArgvRequest(
                rid.strip(),
                [str(item) for item in argv],
                owner=_owner_of(connection),
                sink=None if connection is None else sink,
            )
            with inflight_lock:
                if request.key in inflight:
                    sink.emit(
                        {
                            "id": request.rid,
                            "event": "error",
                            "error": "duplicate_request_id",
                            "detail": "a request with this id is still in flight",
                        }
                    )
                    return None
                inflight[request.key] = request
            future = pool.submit(_run, request)
            with inflight_lock:
                # _run may already have finished and popped the request;
                # only track the future while the request is in flight so
                # the registry cannot leak completed entries.
                if request.key in inflight:
                    inflight_futures[request.key] = future
            return None

        pool = ThreadPoolExecutor(
            max_workers=max(1, pool_size), thread_name_prefix="harness-serve"
        )
        # The socket starts ACCEPTING only now: the listener has been bound
        # since before the ready frame (so the port could be published), but a
        # connection whose first request landed before this pool existed would
        # have nowhere to dispatch. The backlog holds them for the microseconds
        # in between.
        if socket_server is not None:
            try:
                socket_server.start_accepting()
            except Exception as exc:
                _service_log(
                    {
                        "event": "serve_socket_accept_start_failed",
                        "boot_id": boot_id,
                        "reason": type(exc).__name__,
                    }
                )
                _close_socket_lane(reason="accept_start_failed")
        # Explicit construction + shutdown rather than ``with``: the drain's
        # timeout path must be able to stop waiting on work that has proven it
        # will not finish, and a context manager always joins.
        try:
            for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                if _handle_line(line, frames) == "shutdown":
                    break
        finally:
            # The reader is done; from here the process is unwinding normally,
            # which is what the drain monitor's grace window is waiting to see.
            reader_unwound.set()
            # ``wait`` is True everywhere except after a drain TIMEOUT, where
            # the whole point is that the remaining work has already outlived
            # its deadline and joining it would restore the hang.
            pool.shutdown(wait=pool_shutdown_wait)
        liveness_stop.set()
        if drain_state is not None:
            # The drain owns the terminal frame (``drain_complete`` /
            # ``drain_timeout``); emitting ``shutdown`` as well would tell a
            # consumer that a TIMED-OUT drain ended cleanly — the one thing it
            # must not conclude.
            #
            # But the reader can get here BEFORE the monitor has published
            # anything: a `shutdown` op, or the pipe reaching EOF, while a drain
            # is still in progress. That path used to fall straight to
            # ``return drain_exit_code`` — no terminal frame at all, the registry
            # entry left on disk, and code 0 even when the drain had TIMED OUT.
            # A drain that exits silently is exactly the crash it exists to
            # replace, so wait a bounded moment for the monitor (the pool is
            # already joined above, so it is normally one poll away) and, if it
            # never publishes, say so in a typed frame of its own.
            if not drain_finished.wait(_DRAIN_ABANDON_GRACE_SECONDS):
                abandoned = {
                    "event": "drain_abandoned",
                    "pid": os.getpid(),
                    "boot_id": boot_id,
                    **drain_state.counters(),
                    "drain_ms": drain_state.elapsed_ms(),
                    "detail": (
                        "the transport closed while a drain was still in "
                        "progress; the drain published no terminal frame"
                    ),
                }
                frames.emit(abandoned)
                if socket_server is not None:
                    try:
                        socket_server.broadcast(abandoned)
                    except Exception:
                        pass
                _close_socket_lane(reason="drain_abandoned")
                _unregister_instance()
                # Nonzero on purpose, and the SAME code a timeout uses: a
                # supervisor must be able to tell "drained" from "gave up".
                return DRAIN_TIMEOUT_EXIT_CODE
            # ``_finish_drain`` published the frame, closed the socket lane, and
            # unregistered; ``drain_exit_code`` is its verdict, not this path's.
            return drain_exit_code
        shutdown_frame = {"event": "shutdown", "pid": os.getpid()}
        # Socket clients hear it BEFORE the transport closes under them: an
        # attached client whose socket simply died could not tell a clean
        # service shutdown from a crash, which is the distinction the durable
        # service exists to make legible.
        if socket_server is not None:
            try:
                socket_server.broadcast(shutdown_frame)
            except Exception:
                pass
        _close_socket_lane(reason="shutdown")
        _unregister_instance()
        frames.emit(shutdown_frame)
        return 0
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr


def _raw_fd_lines(fd: int):
    """Yield lines from [fd] as they arrive on an OPEN interactive pipe.

    Reads the descriptor directly (``os.read`` returns per pipe write, lines
    are assembled manually) instead of iterating a text wrapper, so no stdio
    layer can buffer a request until EOF."""
    buffer = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:
            if buffer:
                yield buffer.decode("utf-8", errors="replace")
            return
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")


def _claim_protocol_pipes() -> tuple[int, int]:
    """Move the NDJSON protocol onto private descriptors and detach fd 0/1.

    Handlers spawn subprocesses (git for dirty state, proof runners, …) that
    inherit the standard descriptors. With serve's stdin pipe left on fd 0,
    any child that reads stdin blocks forever against the Launcher's open
    pipe — ``git status`` deadlocked the whole status handler (observed live
    2026-07-08; the piped smoke passed only because a closed pipe is
    instant EOF). A child writing raw output to an inherited fd 1 would
    likewise corrupt the frame stream. Serve therefore dups the protocol
    pipes to private fds and points fd 0 at the null device (children read
    EOF) and fd 1 at the null device (stray child writes vanish; every
    handler print already flows through the contextvar proxy)."""
    protocol_in = os.dup(0)
    protocol_out = os.dup(1)
    devnull_read = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull_read, 0)
    os.close(devnull_read)
    devnull_write = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull_write, 1)
    os.close(devnull_write)
    return protocol_in, protocol_out


def _cmd_serve(args) -> int:
    # Started before anything else this command does: everything from process
    # creation up to here is the interpreter + hermes import tax, and it is the
    # single largest term in a cold boot.
    from agent_runtime.boot_timeline import BootTimeline

    timeline = BootTimeline()
    if not getattr(args, "ndjson", False):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "unsupported_transport",
                    "detail": "hermes harness serve currently requires --ndjson (schema v1)",
                }
            )
        )
        return 2
    protocol_in, protocol_out = _claim_protocol_pipes()
    writer = os.fdopen(protocol_out, "w", encoding="utf-8", newline="\n")
    # Function-local on purpose: this file is exec'd into harness.py's globals.
    from agent_runtime.root_anchor import publish_store_root_anchor

    def _wake_reader() -> None:
        """Unblock a reader parked on an idle protocol pipe after a drain.

        Closing the descriptor makes the in-progress (or next) ``os.read``
        fail, which ``_raw_fd_lines`` already treats as end-of-stream. The
        second close in the ``finally`` below is a harmless no-op.
        """

        try:
            os.close(protocol_in)
        except OSError:
            pass

    try:
        return serve_loop(
            _raw_fd_lines(protocol_in),
            writer,
            pool_size=getattr(args, "pool_size", DEFAULT_POOL_SIZE)
            or DEFAULT_POOL_SIZE,
            boot_timeline=timeline,
            snapshot_prewarm=_prewarm_read_model_snapshot,
            root_anchor=publish_store_root_anchor,
            drain_wakeup=_wake_reader,
            # ``os._exit``, not ``sys.exit``: after a drain TIMEOUT the
            # interpreter cannot be trusted to come down at all — the
            # concurrent.futures atexit hook joins worker threads, and the
            # stuck ones are precisely why the deadline fired. Every frame is
            # flushed at emit, so nothing observable is lost.
            hard_exit=os._exit,
            # The durable service's transport. ON here and nowhere else: every
            # ``serve_loop`` unit test observes the byte-identical stdio loop
            # unless it asks for the socket by name.
            socket_lane=not getattr(args, "no_socket", False),
        )
    finally:
        try:
            writer.flush()
        except Exception:
            pass
        try:
            os.close(protocol_in)
        except OSError:
            pass


# ── the first real client ────────────────────────────────────────────────────
#
# ``harness serve connect`` is the operator/agent lane onto the socket: it
# resolves the root's live service from the registry, performs the mandatory
# hello, and prints what it got as JSON. It exists for three reasons, in order
# of importance:
#
# 1. A transport with no client is a transport nobody has proven. This performs
#    the REAL handshake against the REAL auth token over the REAL socket.
# 2. ``--drain`` gives the durable service its restart verb from the outside —
#    the thing slice 2 could describe but could not exercise, because a drain
#    over stdio ends the only connection that could observe it.
# 3. It is the shape the Launcher's client will mirror when it migrates.

#: Nothing to connect to (no live socket service for this root).
SERVE_CONNECT_NO_SERVICE_EXIT_CODE = 4
#: The service is there; this client could not authenticate to it.
SERVE_CONNECT_REJECTED_EXIT_CODE = 5
#: The connection itself failed (refused, timed out, died mid-handshake).
SERVE_CONNECT_TRANSPORT_EXIT_CODE = 6


def _cmd_serve_connect(args) -> int:
    from agent_runtime import paths
    from agent_runtime.build_stamp import build_stamp
    from agent_runtime.serve_auth import read_token
    from agent_runtime.serve_socket import ServeSocketClient, resolve_socket_target

    def _emit(payload: dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))

    store_root = paths.store_root()
    target = resolve_socket_target(store_root)
    if target is None:
        _emit(
            {
                "ok": False,
                "error": "no_socket_service",
                "detail": (
                    "no live serve with a socket transport is registered for this "
                    "runtime root"
                ),
                "runtime_root": str(store_root),
            }
        )
        return SERVE_CONNECT_NO_SERVICE_EXIT_CODE
    token = read_token(store_root)
    if not token:
        # Fails CLOSED, and says which side is missing: a client with no token
        # cannot authenticate, and pretending otherwise would send a hello that
        # can only ever be rejected.
        _emit(
            {
                "ok": False,
                "error": "no_auth_token",
                "detail": "this runtime root has no serve auth token to present",
                "runtime_root": str(store_root),
                "target": target.payload(),
            }
        )
        return SERVE_CONNECT_REJECTED_EXIT_CODE
    client_build = build_stamp().commit
    report: dict[str, Any] = {
        "ok": False,
        "runtime_root": str(store_root),
        "target": target.payload(),
        "client": getattr(args, "client", None) or "harness-serve-connect",
        "client_build": client_build,
    }
    timeout = float(getattr(args, "timeout", 10.0) or 10.0)
    connection = ServeSocketClient(target.host, target.port, timeout_seconds=timeout)
    try:
        connection.connect()
    except OSError as exc:
        report["error"] = "connect_failed"
        report["detail"] = type(exc).__name__
        _emit(report)
        return SERVE_CONNECT_TRANSPORT_EXIT_CODE
    try:
        hello = connection.hello(
            token=token, client=report["client"], client_build=client_build
        )
        report["hello"] = hello
        if not isinstance(hello, dict) or hello.get("event") != "hello_ok":
            report["error"] = (
                "hello_rejected" if isinstance(hello, dict) else "no_hello_reply"
            )
            _emit(report)
            return SERVE_CONNECT_REJECTED_EXIT_CODE
        if getattr(args, "probe", False):
            connection.send({"op": "version"})
            report["version"] = connection.read_frame()
        if getattr(args, "drain", False):
            deadline = getattr(args, "deadline_seconds", None)
            request: dict[str, Any] = {"op": "drain"}
            if deadline is not None:
                request["deadline_seconds"] = float(deadline)
            connection.send(request)
            # Read to the TERMINAL frame, not to the first one: the drain's
            # evidence (what it refused, what it completed) is on the terminal
            # frame, and a client that stopped at ``draining`` would report a
            # restart it never watched finish.
            observed: list[dict[str, Any]] = []
            terminal = {
                "drain_complete",
                "drain_timeout",
                "drain_abandoned",
                "drain_in_progress",
            }
            while True:
                frame = connection.read_frame()
                if frame is None:
                    break
                observed.append(frame)
                if frame.get("event") in terminal:
                    break
            report["drain"] = observed
            report["drain_outcome"] = (
                observed[-1].get("event") if observed else "no_frames"
            )
        report["ok"] = True
        _emit(report)
        return 0
    except OSError as exc:
        report["error"] = "transport_failed"
        report["detail"] = type(exc).__name__
        _emit(report)
        return SERVE_CONNECT_TRANSPORT_EXIT_CODE
    finally:
        connection.close()
