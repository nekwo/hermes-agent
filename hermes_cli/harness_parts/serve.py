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
from concurrent.futures import ThreadPoolExecutor
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
# daemon status, scope pointers, the store directories (record add/rename
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
    "daemon_status.json",
    "active_realm.json",
    "active_workspace.json",
)
_FINGERPRINT_STORE_DIRS = (
    "tasks",
    "runs",
    "incidents",
    "agents",
    "proofs",
    "worker_sessions",
    "repo_bundles",
    "runtime_instances",
    "persona_instances",
    "persona_assignments",
    "goals",
    "workspaces",
    "realms",
)


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
    try:
        from hermes_state import SessionDB

        db_path = str(SessionDB().db_path)
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
    __slots__ = ("rid", "argv", "is_chat_turn")

    def __init__(self, rid: str, argv: list[str]):
        self.rid = rid
        self.argv = argv
        tail = argv[1:] if argv and argv[0] == "harness" else argv
        self.is_chat_turn = any(
            tuple(tail[: len(shape)]) == shape for shape in _CHAT_TURN_COMMANDS
        )


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


def serve_loop(
    reader: TextIO,
    writer: TextIO,
    *,
    pool_size: int = DEFAULT_POOL_SIZE,
    dispatch: Callable[[list[str]], int] = dispatch_argv,
    fingerprint: Callable[[], tuple | None] = _runtime_state_fingerprint,
    read_cache_max_age: float = _READ_CACHE_MAX_AGE_SECONDS,
) -> int:
    """Core dispatch loop over explicit streams. stdio is transport #1; a
    future remote lane feeds the same loop (design doc §Future)."""
    frames = _FrameWriter(writer)
    stdout_proxy = _LineFrameProxy(frames, "line")
    stderr_proxy = _LineFrameProxy(frames, "stderr")
    read_cache = _ReadModelCache(read_cache_max_age)

    inflight: dict[str, _ArgvRequest] = {}
    inflight_lock = threading.Lock()

    def _busy_frame() -> dict[str, Any]:
        with inflight_lock:
            pending = len(inflight)
            chat_turns = sum(1 for item in inflight.values() if item.is_chat_turn)
        return {"event": "busy", "chat_turns": chat_turns, "pending": pending}

    def _run(request: _ArgvRequest) -> None:
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
        frames.emit(
            {
                "event": "ready",
                "pid": os.getpid(),
                "schema_version": SERVE_SCHEMA_VERSION,
                "runtime_root": runtime_root,
            }
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
                pool.submit(_run, request)
            # Context-manager exit drains in-flight work before shutdown.
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
    try:
        return serve_loop(
            _raw_fd_lines(protocol_in),
            writer,
            pool_size=getattr(args, "pool_size", DEFAULT_POOL_SIZE)
            or DEFAULT_POOL_SIZE,
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
