#!/usr/bin/env python3
"""Detached execution for ``agent_chat_send(wait=false)`` — one child process per dispatch.

The synchronous relay runs the target's turn INSIDE the sender's turn: the
sender blocks for the whole thing, which caps agent-to-agent work at whatever
fits in a conversational window and makes "go run the suite and tell me when
it's done" impossible to express. This module is the other half — the target's
turn moves into its own process, the sender's tool call returns a handle
immediately, and :mod:`agent_runtime.dispatch_delivery` forges the answer back
into the sender's thread later, when the sender is idle.

WHY A SUBPROCESS AND NOT A THREAD — the correction that defines this file
------------------------------------------------------------------------
The first implementation ran detached turns in-process on a daemon executor.
That is unshippable, and not for a subtle reason: every persona turn in this
runtime executes under ``profile_runner._WORKDIR_LOCK``, a PROCESS-WIDE
``RLock`` held for the ENTIRE model turn (``_execute_agent_run``). Serve handles
requests concurrently, but persona turns serialize on that lock. A single
30-minute detached dispatch would therefore have:

* frozen every foreground operator turn — and frozen it AFTER the journal's
  ``executing`` transition, so the console shows a running turn with no typed
  refusal, no timeout, and nothing to expire it (the wall budget only starts
  ticking INSIDE the lock);
* frozen the SENDER's own next turn, exactly inverting the feature;
* made ``dispatch_max_concurrent`` a fiction — three dispatches would be three
  30-minute waits in series, not in parallel;
* left queued waiters holding the TARGET's chat-root lease while blocked, so an
  operator messaging that agent gets ``chat_busy`` for hours;
* frozen the delivery drain itself, whose forge takes the same lock;
* deadlocked outright on a nested dispatch, where a parent waiting on a child
  holds the lock the child needs.

A child process has its own ``_WORKDIR_LOCK``, its own ``HERMES_HOME`` override,
and its own cwd. The executor thread here spawns and WAITS — holding NO lock
while it waits, which is the entire point — so the parent stays free to run
operator turns and deliveries throughout. ``dispatch_max_concurrent`` now means
three genuinely concurrent turns, and a nested dispatch simply spawns another
process.

The cost is a cold start per dispatch. That is the right trade for work whose
budget is measured in minutes, and it buys a second property worth as much: a
dispatch that wedges or explodes cannot take the serve process with it.

WHAT THE PARENT STILL OWNS
--------------------------
The child is an ordinary ``hermes harness mission-chat message`` turn and knows
nothing about dispatches. Bookkeeping stays here: this thread stamps the child's
PID identity onto the row when it spawns, enforces the wall budget with a
kill-after-grace, and writes the terminal completion the delivery drain reads.
Because the row carries the CHILD's pid + start time, the orphan sweep can tell
"still working" from "its process is gone" without needing this thread alive.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "KILL_GRACE_SECONDS",
    "build_dispatch_argv",
    "child_environment",
    "dispatch_detached_turn",
    "parse_child_payload",
    "shutdown_dispatch_executor",
    "summarize_for_caller",
]

#: How long a child gets to honour its own ``--max-seconds`` before the parent
#: stops asking. The child enforces the budget itself and settles the turn
#: gracefully (``budget_exhausted``); this is the backstop for a child that
#: cannot — wedged in a syscall, stuck inside an unkillable tool — and it is
#: generous on purpose, because killing a turn that was about to write its
#: result loses the result.
KILL_GRACE_SECONDS = 120.0

#: Bound on the child's captured streams. Stdout only has to carry one JSON
#: payload; stderr is diagnostics. Neither may grow without limit inside a
#: long-lived parent.
_MAX_STREAM_CHARS = 512_000
#: How much stderr rides into a failure record. Enough to name the failure,
#: nowhere near the 8KB reply bound.
_STDERR_EXCERPT = 800

_executor = None
_executor_lock = threading.Lock()
_executor_max_workers = 0


def _get_executor(max_workers: int):
    """The shared dispatch executor, resized when the configured cap grows.

    These workers no longer RUN turns — they supervise child processes — so the
    pool is a concurrency cap rather than a compute pool. It stays
    daemon-threaded: a supervisor blocked on a 30-minute child must never hold
    interpreter exit open (stdlib pool workers are joined unconditionally by an
    atexit hook).

    Deliberately never SHRINKS a live pool: an in-flight dispatch holds a
    worker, and tearing the pool down under it to honour a smaller cap would
    abandon exactly the long work this lane exists to host. A lowered cap takes
    effect on the next process.
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


# --------------------------------------------------------------------------
# building the child invocation
# --------------------------------------------------------------------------


def build_dispatch_argv(spec: dict[str, Any], *, deadline_epoch: float) -> list[str]:
    """The child's argv — a plain ``harness mission-chat message`` turn.

    The relay deadline is minted by the caller of this function AT SPAWN TIME,
    not when the dispatch was enqueued. That distinction fixes a real erosion:
    a dispatch waiting behind the concurrency cap used to burn its wall budget
    sitting in a queue, so the budget the sender was told about and the budget
    the turn actually got drifted apart silently. The clock now starts when the
    turn does.

    ``--defer-thread-policy`` carries the tri-state ``new_session`` argparse
    cannot otherwise express (absent means False, not unset), so the child
    threads exactly as the in-process lane did.
    """

    argv = [
        sys.executable,
        "-m",
        "hermes_cli.main",
        "harness",
        "mission-chat",
        "message",
        "--persona",
        str(spec["persona_id"]),
        "--message",
        str(spec["message"]),
        "--json",
        "--intent-hint",
        str(spec.get("intent_hint") or "chat"),
        "--requested-by",
        str(spec.get("requested_by") or "agent-chat-relay"),
        "--max-seconds",
        f"{float(spec['max_seconds']):.3f}",
        "--relay-deadline-epoch",
        f"{float(deadline_epoch):.3f}",
    ]
    if spec.get("client_message_id"):
        argv += ["--client-message-id", str(spec["client_message_id"])]
    if spec.get("persona_instance_id"):
        argv += ["--persona-instance-id", str(spec["persona_instance_id"])]
    if spec.get("session_id"):
        argv += ["--session-id", str(spec["session_id"])]
    if spec.get("clarify_token"):
        argv += ["--clarify-token", str(spec["clarify_token"])]
    if spec.get("title"):
        argv += ["--title", str(spec["title"])]
    if spec.get("requested_by_session"):
        argv += ["--requested-by-session", str(spec["requested_by_session"])]
    # The chain travels WHOLE, so depth and cycle detection still run at the
    # handler's chokepoint inside the child. Only the CLOCK is fresh (the
    # operator ruling); the reach is not.
    chain = [str(item) for item in (spec.get("relay_chain") or []) if str(item).strip()]
    if chain:
        argv += ["--relay-chain", ",".join(chain)]
    new_session = spec.get("new_session")
    if new_session is True:
        argv.append("--new-session")
    elif new_session is None:
        argv.append("--defer-thread-policy")
    # new_session is False → omit both: argparse's absent default IS False.
    return argv


def child_environment(spec: dict[str, Any]) -> dict[str, str]:
    """The child's environment: writers and readers made to converge, explicitly.

    Both homes are STATED rather than inherited. ``HERMES_HOME`` is the
    operator/ambient home the dispatch was made from, captured through the
    existing authority before this supervisor's own environment could drift.
    ``HERMES_HEAD_HOME`` is the resolved background-work home, so the child's own
    background writers (its ``processes.json``, its delegations) land where the
    Activity projection and this parent read them. Nothing is re-derived here.

    ``PYTHONPATH`` is pinned to the tree THIS process runs from, so a serve
    booted out of a worktree spawns children from the same worktree instead of
    whatever the interpreter would otherwise import.
    """

    env = dict(os.environ)
    if spec.get("hermes_home"):
        env["HERMES_HOME"] = str(spec["hermes_home"])
    if spec.get("head_home"):
        env["HERMES_HEAD_HOME"] = str(spec["head_home"])
    try:
        import hermes_cli

        root = str(Path(hermes_cli.__file__).resolve().parents[1])
        existing = env.get("PYTHONPATH") or ""
        if root not in existing.split(os.pathsep):
            env["PYTHONPATH"] = root + (os.pathsep + existing if existing else "")
    except Exception:  # pragma: no cover - defensive
        pass
    return env


def parse_child_payload(text: str) -> dict[str, Any] | None:
    """The LAST complete JSON object in the child's stdout, or None.

    ``emit_json`` writes indented multi-line JSON, so this cannot be a line
    scan, and the child's stdout legitimately carries other lines (a SQLite WAL
    advisory, provider warnings) both before and after the payload. Decoding
    with ``raw_decode`` from every ``{`` and keeping the last success is the only
    shape that survives noise on both sides.
    """

    if not text:
        return None
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found = value
        index = text.find("{", max(end, index + 1))
    return found


# --------------------------------------------------------------------------
# running one dispatch
# --------------------------------------------------------------------------


def _drain(stream, sink: list[str]) -> threading.Thread:
    """Consume a child pipe for its WHOLE lifetime on a daemon thread.

    Both pipes get one of these, always. A stderr pipe nobody reads fills its OS
    buffer and wedges the child mid-write — a hang that looks exactly like a slow
    turn, and the standing reason this repo requires draining both streams
    rather than only the one being parsed.
    """

    def _pump() -> None:
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                if sum(len(item) for item in sink) < _MAX_STREAM_CHARS:
                    sink.append(line)
        except Exception:  # pragma: no cover - pipe torn down under us
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()
    return thread


def _child_identity(pid: int) -> int | None:
    try:
        from gateway.status import get_process_start_time

        return get_process_start_time(int(pid))
    except Exception:  # pragma: no cover - defensive
        return None


def _kill_child(pid: int, started_at: int | None) -> None:
    """Identity-verified tree-kill through the repo's ONE implementation.

    ``ProcessRegistry._terminate_host_pid`` re-validates the recorded start time
    before it signals anything, which is what stops a recycled PID from turning
    a budget timeout into a killed stranger. A second tree-kill here would be a
    second place for that guard to be forgotten.
    """

    try:
        from tools.process_registry import ProcessRegistry

        ProcessRegistry._terminate_host_pid(int(pid), started_at)
    except Exception:  # pragma: no cover - best effort by contract
        logger.debug("dispatch child %s tree-kill failed", pid, exc_info=True)


def _detached_error_text(payload: dict[str, Any]) -> str:
    """The refusal text, rewritten for the lane it will actually be READ on.

    Typed refusals from the handler are written for a caller who is BLOCKED on
    the reply — ``relay_budget_exhausted`` ends "Answer your caller with what you
    have", which is sound advice mid-turn and nonsense inside a delivered error
    turn that arrives minutes later, addressed to an agent who has already moved
    on and has no caller waiting.

    Only the ones whose guidance is lane-specific are rewritten; everything else
    passes through verbatim, because a refusal an agent can act on is worth more
    than a uniformly-phrased one it cannot.
    """

    kind = str(payload.get("error_kind") or "")
    raw = str(payload.get("error") or payload.get("blocker") or "the dispatched turn failed")
    if kind == "relay_budget_exhausted":
        return (
            "The dispatch was refused before it ran: the relay chain it belongs to had no wall "
            "budget left. Nothing was executed and there is no partial result. Re-dispatch it as "
            "a fresh request if you still need it."
        )
    if kind in {"relay_cycle", "relay_depth_limit"}:
        return (
            f"The dispatch was refused before it ran ({kind}): this request would have looped back "
            "through an agent already on the relay chain, or gone deeper than the chain allows. "
            "Nothing was executed. Ask the agent directly, or do it yourself."
        )
    return raw[:600]


def _run_dispatch(dispatch_id: str, spec: dict[str, Any]) -> None:
    """Spawn one child turn, wait for it, and record its outcome.

    Never raises: this runs on a detached supervisor with nobody to catch
    anything, and an unrecorded exception would leave the row ``running``
    forever — the sender waiting on an answer that can never arrive. Every exit
    path writes a terminal state, which is what makes the completion
    deliverable.
    """

    from agent_runtime import dispatch_store

    budget = float(spec.get("max_seconds") or 1800.0)
    # Minted HERE: the turn's clock starts when the turn starts, not when the
    # dispatch was enqueued behind the concurrency cap.
    deadline_epoch = time.time() + budget
    argv = build_dispatch_argv(spec, deadline_epoch=deadline_epoch)
    env = child_environment(spec)

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    try:
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            # Never inherit the parent's stdin: in serve that is the launcher's
            # request pipe, and a child reading from it would steal requests.
            "stdin": subprocess.DEVNULL,
            "env": env,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":
            try:
                from hermes_cli._subprocess_compat import windows_hide_flags

                popen_kwargs["creationflags"] = windows_hide_flags()
            except Exception:
                pass
        proc = subprocess.Popen(argv, **popen_kwargs)
    except Exception as exc:
        logger.exception("detached dispatch %s could not spawn", dispatch_id)
        dispatch_store.record_completion(
            dispatch_id,
            state=dispatch_store.STATE_ERROR,
            error=f"the dispatch process could not be started: {type(exc).__name__}: {exc}",
        )
        return

    # The row's owner becomes the CHILD the moment it exists. That is what keeps
    # the orphan sweep coherent without this supervisor: "is the work still
    # running" becomes a question about the process actually doing it, so a
    # serve that dies mid-dispatch leaves a row that settles when the child
    # exits rather than one frozen on a dead thread's PID.
    started_at = _child_identity(proc.pid)
    try:
        dispatch_store.set_dispatch_owner(
            dispatch_id, owner_pid=proc.pid, owner_started_at=started_at
        )
    except Exception:  # pragma: no cover - bookkeeping must not abort the run
        logger.debug("dispatch %s owner stamp failed", dispatch_id, exc_info=True)

    out_thread = _drain(proc.stdout, stdout_lines)
    err_thread = _drain(proc.stderr, stderr_lines)

    exit_reason = ""
    try:
        returncode = proc.wait(timeout=budget + KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # The child was supposed to settle itself at --max-seconds. It did not,
        # so it is wedged rather than slow, and the sender is owed a terminal
        # answer instead of an indefinitely `running` row.
        exit_reason = "budget_exceeded"
        _kill_child(proc.pid, started_at)
        try:
            returncode = proc.wait(timeout=30)
        except Exception:
            returncode = -1
    except Exception as exc:  # pragma: no cover - defensive
        exit_reason = f"wait_failed:{type(exc).__name__}"
        returncode = -1

    # Join the pumps so nothing the child wrote is missed, but never wait
    # forever on a pipe a killed child left open.
    out_thread.join(timeout=10)
    err_thread.join(timeout=10)

    stdout_text = "".join(stdout_lines)
    stderr_text = "".join(stderr_lines)
    payload = parse_child_payload(stdout_text)

    if exit_reason:
        dispatch_store.record_completion(
            dispatch_id,
            state=dispatch_store.STATE_ERROR,
            error=(
                f"the dispatch exceeded its {budget:.0f}s budget (plus a "
                f"{KILL_GRACE_SECONDS:.0f}s grace) and was stopped. Anything it wrote "
                "before that is in its own chat thread."
                if exit_reason == "budget_exceeded"
                else f"the dispatch supervisor failed: {exit_reason}"
            ),
            target_session_id=str((payload or {}).get("session_id") or ""),
        )
        return

    if payload is None:
        # No payload is a runtime failure, not a result — say so rather than
        # inventing an empty reply the sender would read as "nothing to report".
        dispatch_store.record_completion(
            dispatch_id,
            state=dispatch_store.STATE_UNKNOWN,
            error=(
                f"the dispatch process exited with code {returncode} without a reply "
                "payload; the outcome is unknown."
                + (f" stderr: {stderr_text[-_STDERR_EXCERPT:]}" if stderr_text.strip() else "")
            ),
        )
        return

    ok = bool(payload.get("ok")) and returncode == 0
    dispatch_store.record_completion(
        dispatch_id,
        state=dispatch_store.STATE_COMPLETED if ok else dispatch_store.STATE_ERROR,
        reply=str(payload.get("reply") or ""),
        error="" if ok else _detached_error_text(payload),
        target_session_id=str(payload.get("session_id") or payload.get("chat_session_id") or ""),
        total_tokens=payload.get("total_tokens"),
    )


def dispatch_detached_turn(
    *,
    dispatch_id: str,
    spec: dict[str, Any],
    max_concurrent: int,
) -> None:
    """Queue one detached target turn on the shared supervisor pool.

    Queued, not refused, when the cap is saturated: the caller has already been
    told ``dispatched: true`` and a durable row already exists, so dropping the
    work here would be a lie the sender could never detect. A queued dispatch no
    longer burns budget while it waits — the clock is minted at spawn.
    """

    executor = _get_executor(max_concurrent)
    executor.submit(_run_dispatch, dispatch_id, spec)


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
