#!/usr/bin/env python3
"""Detached execution for ``agent_chat_send(wait=false)``.

The synchronous relay runs the target's turn INSIDE the sender's turn: the
sender blocks for the whole thing, which caps agent-to-agent work at whatever
fits in a conversational window and makes "go run the suite and tell me when
it's done" impossible to express. This module is the other half — the target's
turn moves onto a background executor, the sender's tool call returns a handle
immediately, and :mod:`agent_runtime.dispatch_delivery` forges the answer back
into the sender's thread later, when the sender is idle.

Three things this file is careful about, each of which cost a real incident
somewhere in this tree:

**The executor is daemon-threaded.** ``concurrent.futures``' stock workers are
joined unconditionally by an ``atexit`` hook, so one wedged dispatch would hold
the whole process open forever. :class:`tools.daemon_pool.DaemonThreadPoolExecutor`
is the repo's one answer to that and is shared with the delegation lane.

**Context is propagated, not re-derived.** A bare worker thread starts with an
EMPTY ``contextvars.Context``. Two things in it matter here and are invisible
when they go missing: the recorded HEAD home (``persona_profile_context``
records it set-once, and it is what keeps a dispatch made inside a persona turn
writing to the operator's stores rather than the persona profile's), and the
approval/session vars that decide whether a dangerous command prompts. So the
worker runs through :func:`tools.thread_context.propagate_context_to_thread`,
captured on the DISPATCHING thread where those values are still correct.

**Nothing is printed.** The target's handler hands its payload back through the
``payload_sink`` seam, so a detached turn cannot interleave with any other
request's stdout. This is why the seam had to land before this lane could: the
old ``contextlib.redirect_stdout`` capture rebinds ``sys.stdout``
process-globally, and two concurrent dispatches would have shared — and
corrupted — one buffer.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["dispatch_detached_turn", "shutdown_dispatch_executor"]

_executor = None
_executor_lock = threading.Lock()
_executor_max_workers = 0


def _get_executor(max_workers: int):
    """The shared dispatch executor, resized when the configured cap grows.

    Deliberately never SHRINKS a live pool: an in-flight dispatch holds a
    worker, and tearing the pool down under it to honour a smaller cap would
    abandon exactly the long-running work this lane exists to host. A lowered
    cap takes effect on the next process.
    """

    global _executor, _executor_max_workers
    from tools.daemon_pool import DaemonThreadPoolExecutor

    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            if _executor is not None:
                _executor.shutdown(wait=False)
            _executor = DaemonThreadPoolExecutor(
                max_workers=max(1, int(max_workers)),
                thread_name_prefix="agent-chat-dispatch",
            )
            _executor_max_workers = max(1, int(max_workers))
        return _executor


def shutdown_dispatch_executor() -> None:
    """Drop the shared pool (tests; never on a live lane)."""

    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0


def _run_dispatch(dispatch_id: str, args: SimpleNamespace) -> None:
    """Execute one detached target turn and record its outcome.

    Never raises: this runs on a detached worker with nobody to catch anything,
    and an unrecorded exception would leave the row ``running`` forever — the
    sender waiting on an answer that can never arrive. Every exit path writes a
    terminal state, which is what makes the completion deliverable.
    """

    from agent_runtime import dispatch_store

    payloads: list[dict] = []
    args.payload_sink = payloads.append
    # A detached worker inherits the dispatching request's serve id through the
    # copied context. Clear it best-effort so any stray line this turn prints is
    # attributed to no request rather than to a request that already exited.
    try:  # pragma: no cover - serve-only
        from hermes_cli import harness as _harness

        request_id = getattr(_harness, "_request_id", None)
        if request_id is not None:
            request_id.set(None)
    except Exception:
        pass

    exit_code = 2
    try:
        from hermes_cli import harness as _harness

        exit_code = _harness._cmd_mission_chat_message(args)
    except Exception as exc:  # noqa: BLE001 - a detached turn must never vanish
        logger.exception("detached dispatch %s failed", dispatch_id)
        dispatch_store.record_completion(
            dispatch_id,
            state=dispatch_store.STATE_ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )
        return

    payload = payloads[-1] if payloads else None
    if payload is None:
        # The handler returned without emitting a payload. That is a runtime
        # bug, not a result — say so instead of inventing an empty reply.
        dispatch_store.record_completion(
            dispatch_id,
            state=dispatch_store.STATE_UNKNOWN,
            error=(
                "the target's turn returned no reply payload "
                f"(exit code {exit_code}); the outcome is unknown"
            ),
        )
        return

    ok = bool(payload.get("ok")) and exit_code == 0
    dispatch_store.record_completion(
        dispatch_id,
        state=dispatch_store.STATE_COMPLETED if ok else dispatch_store.STATE_ERROR,
        reply=str(payload.get("reply") or ""),
        error="" if ok else str(payload.get("error") or payload.get("blocker") or "relay turn failed"),
        target_session_id=str(payload.get("session_id") or payload.get("chat_session_id") or ""),
        total_tokens=payload.get("total_tokens"),
    )


def dispatch_detached_turn(
    *,
    dispatch_id: str,
    args: SimpleNamespace,
    max_concurrent: int,
) -> None:
    """Queue one detached target turn on the shared executor.

    Queued, not refused, when the cap is saturated: the caller has already been
    told ``dispatched: true`` and a durable row already exists, so dropping the
    work here would be a lie the sender could never detect.
    """

    from tools.thread_context import propagate_context_to_thread

    executor = _get_executor(max_concurrent)
    executor.submit(propagate_context_to_thread(_run_dispatch), dispatch_id, args)


def summarize_for_caller(row: dict[str, Any]) -> dict[str, Any]:
    """The bounded shape ``agent_chat_dispatches`` returns for one row.

    Read-only projection of a store row: enough for an agent to answer "did the
    thing I asked for come back yet?", never the whole reply (that arrives as
    its own delivered turn, and duplicating it here would double the context
    cost of every status check).
    """

    result = row.get("result") or {}
    reply = str(result.get("reply") or "")
    return {
        "dispatch_id": row.get("dispatch_id"),
        "target_persona": row.get("target_persona"),
        "target_instance_id": row.get("target_instance_id") or None,
        "title": row.get("title") or None,
        "ask_excerpt": str(row.get("ask") or "")[:200],
        "state": row.get("state"),
        "delivery_state": row.get("delivery_state"),
        "notify_operator": bool(row.get("notify_operator")),
        "dispatched_at": row.get("dispatched_at"),
        "completed_at": row.get("completed_at"),
        "elapsed_seconds": int(
            max(
                0.0,
                (row.get("completed_at") or time.time()) - (row.get("dispatched_at") or 0.0),
            )
        ),
        "session_id": row.get("target_session_id") or None,
        "reply_chars": len(reply),
        "reply_excerpt": reply[:400],
        "error": str(result.get("error") or "") or None,
    }
