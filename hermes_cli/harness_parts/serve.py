"""``hermes harness serve --ndjson`` — persistent stdio bridge (schema v1).

One warm process replaces the per-call CLI spawns the Launcher Mission
Control bridge pays ~3s import tax on today. Requests dispatch into the
EXISTING harness argparse tree and ``_cmd_*`` handlers, unchanged — argv
arrives verbatim as the bridge already builds it, so intent→argv mapping,
the capability registry, and the per-call CLI fallback stay byte-identical.

Design doc: ``docs/agent-runtime-harness/harness-serve-design.md``
(settled 2026-07-08). Explicit non-goals: no network listener, no auth
(a local stdio child IS the security model), not the mission daemon, no
second chat pipeline.

Protocol (NDJSON, one frame per line):

- boot:      ``{"event":"ready","pid":…,"schema_version":1,"runtime_root":…}``
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
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, TextIO

SERVE_SCHEMA_VERSION = 1
DEFAULT_POOL_SIZE = 4

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
        self._buffers: dict[str | None, str] = {}
        self._captures: dict[str | None, list[str]] = {}
        self._lock = threading.Lock()

    def writable(self) -> bool:  # pragma: no cover - io protocol
        return True

    def isatty(self) -> bool:
        # Handlers key default output on isatty(); serve is a pipe.
        return False

    def write(self, text: str) -> int:
        if not text:
            return 0
        rid = _request_id.get()
        with self._lock:
            buffered = self._buffers.get(rid, "") + str(text)
            *lines, remainder = buffered.split("\n")
            self._buffers[rid] = remainder
            capture = self._captures.get(rid)
            if capture is not None:
                capture.extend(lines)
        for line in lines:
            self._frames.emit({"id": rid, "event": self._event, "line": line})
        return len(text)

    def flush(self) -> None:  # pragma: no cover - io protocol
        return None

    def begin_capture(self, rid: str | None) -> None:
        """Start mirroring [rid]'s emitted lines for the read-model cache."""
        with self._lock:
            self._captures[rid] = []

    def end_capture(self, rid: str | None) -> list[str]:
        """Stop mirroring and return everything captured for [rid]."""
        with self._lock:
            return self._captures.pop(rid, [])

    def flush_request(self, rid: str | None) -> None:
        """Emit a request's unterminated tail (handler printed without a
        trailing newline) and drop its buffer."""
        with self._lock:
            remainder = self._buffers.pop(rid, "")
            if remainder:
                capture = self._captures.get(rid)
                if capture is not None:
                    capture.append(remainder)
        if remainder:
            self._frames.emit({"id": rid, "event": self._event, "line": remainder})


class _ArgvRequest:
    __slots__ = ("rid", "argv", "is_chat_turn", "is_runtime_stream", "cancel_event")

    def __init__(self, rid: str, argv: list[str]):
        self.rid = rid
        self.argv = argv
        tail = argv[1:] if argv and argv[0] == "harness" else argv
        self.is_chat_turn = any(
            tuple(tail[: len(shape)]) == shape for shape in _CHAT_TURN_COMMANDS
        )
        self.is_runtime_stream = bool(tail and tail[0] == "stream")
        self.cancel_event = threading.Event()


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
) -> int:
    """Core dispatch loop over explicit streams. stdio is transport #1; a
    future remote lane feeds the same loop (design doc §Future).

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

    def _busy_frame() -> dict[str, Any]:
        with inflight_lock:
            pending = len(inflight)
            chat_turns = sum(1 for item in inflight.values() if item.is_chat_turn)
        return {"event": "busy", "chat_turns": chat_turns, "pending": pending}

    def _run(request: _ArgvRequest) -> None:
        from agent_runtime.request_control import request_cancel_scope

        token = _request_id.set(request.rid)
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
                    frames.emit({"id": request.rid, "event": "line", "line": line})
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
                    frames.emit(
                        {
                            "id": request.rid,
                            "event": "error",
                            "error": "argv_parse_failed",
                            "detail": "argparse rejected the request argv; usage was forwarded as stderr frames",
                        }
                    )
            except BaseException as exc:  # dispatch() already enveloped harness errors
                frames.emit(
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
            with inflight_lock:
                inflight.pop(request.rid, None)
                inflight_futures.pop(request.rid, None)
            exit_frame: dict[str, Any] = {
                "id": request.rid,
                "event": "exit",
                "code": code,
            }
            if served_from_cache:
                exit_frame["served_from_cache"] = True
                exit_frame["cache_age_ms"] = cache_age_ms
            frames.emit(exit_frame)

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = stdout_proxy, stderr_proxy
    try:
        try:
            from agent_runtime import paths as _paths

            runtime_root = str(_paths.store_root())
        except Exception:
            runtime_root = None
        timeline.mark("store_root_ms")
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
        with ThreadPoolExecutor(
            max_workers=max(1, pool_size), thread_name_prefix="harness-serve"
        ) as pool:
            for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    frames.emit(
                        {
                            "id": None,
                            "event": "error",
                            "error": "invalid_request",
                            "detail": "request line is not valid JSON",
                        }
                    )
                    continue
                if not isinstance(message, dict):
                    frames.emit(
                        {
                            "id": None,
                            "event": "error",
                            "error": "invalid_request",
                            "detail": "request must be a JSON object",
                        }
                    )
                    continue
                op = message.get("op")
                if op == "ping":
                    frames.emit(_busy_frame())
                    continue
                if op == "stacks":
                    # Operator diagnostic: dump every thread's stack as
                    # stderr frames (hung-request forensics without py-spy).
                    import traceback

                    for thread_id, frame in sys._current_frames().items():
                        frames.emit(
                            {
                                "id": None,
                                "event": "stderr",
                                "line": f"--- thread {thread_id} ---",
                            }
                        )
                        for entry in traceback.format_stack(frame):
                            for line in entry.rstrip().splitlines():
                                frames.emit(
                                    {"id": None, "event": "stderr", "line": line}
                                )
                    frames.emit({"event": "stacks_dumped"})
                    continue
                if op == "shutdown":
                    break
                if op == "cancel":
                    cancel_id = message.get("id")
                    cancel_id = cancel_id.strip() if isinstance(cancel_id, str) else ""
                    if not cancel_id:
                        frames.emit(
                            {
                                "id": None,
                                "event": "error",
                                "error": "invalid_request",
                                "detail": 'cancel needs {"op": "cancel", "id": "<request id>"}',
                            }
                        )
                        continue
                    with inflight_lock:
                        future = inflight_futures.get(cancel_id)
                        running_request = inflight.get(cancel_id)
                        known = running_request is not None
                    if future is not None and future.cancel():
                        with inflight_lock:
                            inflight.pop(cancel_id, None)
                            inflight_futures.pop(cancel_id, None)
                        frames.emit(
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
                        frames.emit(
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
                        frames.emit(
                            {
                                "id": cancel_id,
                                "event": "cancel_denied",
                                "state": "running" if known else "unknown",
                            }
                        )
                    continue
                rid = message.get("id")
                argv = message.get("argv")
                if (
                    not isinstance(rid, str)
                    or not rid.strip()
                    or not isinstance(argv, list)
                    or not argv
                    or not all(isinstance(item, str) for item in argv)
                ):
                    frames.emit(
                        {
                            "id": rid if isinstance(rid, str) else None,
                            "event": "error",
                            "error": "invalid_request",
                            "detail": 'request needs {"id": "<non-empty>", "argv": ["harness", …]}',
                        }
                    )
                    continue
                request = _ArgvRequest(rid.strip(), [str(item) for item in argv])
                with inflight_lock:
                    if request.rid in inflight:
                        frames.emit(
                            {
                                "id": request.rid,
                                "event": "error",
                                "error": "duplicate_request_id",
                                "detail": "a request with this id is still in flight",
                            }
                        )
                        continue
                    inflight[request.rid] = request
                future = pool.submit(_run, request)
                with inflight_lock:
                    # _run may already have finished and popped the request;
                    # only track the future while the request is in flight so
                    # the registry cannot leak completed entries.
                    if request.rid in inflight:
                        inflight_futures[request.rid] = future
            # Context-manager exit drains in-flight work before shutdown.
        liveness_stop.set()
        frames.emit({"event": "shutdown", "pid": os.getpid()})
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

    try:
        return serve_loop(
            _raw_fd_lines(protocol_in),
            writer,
            pool_size=getattr(args, "pool_size", DEFAULT_POOL_SIZE)
            or DEFAULT_POOL_SIZE,
            boot_timeline=timeline,
            snapshot_prewarm=_prewarm_read_model_snapshot,
            root_anchor=publish_store_root_anchor,
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
