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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, TextIO

SERVE_SCHEMA_VERSION = 1
DEFAULT_POOL_SIZE = 4

# Chat turns must survive supervisor recycles (recording safety): these argv
# shapes mark a request as an in-flight chat turn for the busy/ping frame.
_CHAT_TURN_COMMANDS = (("mission-chat", "message"), ("mission-chat", "steer"))

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
        for line in lines:
            self._frames.emit({"id": rid, "event": self._event, "line": line})
        return len(text)

    def flush(self) -> None:  # pragma: no cover - io protocol
        return None

    def flush_request(self, rid: str | None) -> None:
        """Emit a request's unterminated tail (handler printed without a
        trailing newline) and drop its buffer."""
        with self._lock:
            remainder = self._buffers.pop(rid, "")
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
) -> int:
    """Core dispatch loop over explicit streams. stdio is transport #1; a
    future remote lane feeds the same loop (design doc §Future)."""
    frames = _FrameWriter(writer)
    stdout_proxy = _LineFrameProxy(frames, "line")
    stderr_proxy = _LineFrameProxy(frames, "stderr")

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
        try:
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
            _request_id.reset(token)
            with inflight_lock:
                inflight.pop(request.rid, None)
            frames.emit({"id": request.rid, "event": "exit", "code": code})

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
    # The real stdout belongs to the frame writer for the process lifetime;
    # reconfigure to line-buffered UTF-8 so frames survive Windows pipes.
    stdout = sys.stdout
    try:
        stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    except Exception:
        pass
    return serve_loop(
        sys.stdin,
        stdout,
        pool_size=getattr(args, "pool_size", DEFAULT_POOL_SIZE) or DEFAULT_POOL_SIZE,
    )
