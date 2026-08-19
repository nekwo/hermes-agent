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
three genuinely concurrent turns.

The nested-dispatch deadlock is gone too, but NOT because a child spawns a
grandchild — an earlier version of this note claimed that and it was wrong. The
child is a cold one-shot CLI, so its delivery capability is False and a nested
``wait: false`` is REFUSED (``async_delivery_unavailable``) before anything
runs. That is the honest outcome rather than a limitation: the child's process
ends with its turn, so a grandchild would outlive the only thing that could
record what happened to it. A dispatched agent that needs a teammate uses
``wait: true`` and gets the reply inline.

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
    "supervised_dispatch_ids",
    "parse_child_payload",
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
#:
#: The bound keeps the TAIL and drops the head — see :class:`_BoundedTail`. The
#: first implementation did the opposite and it was a data-loss bug, not a
#: tuning choice: the payload is the LAST thing the child prints, so a chatty
#: turn that crossed the cap had its own answer discarded and every successful
#: 30-minute dispatch was reported ``unknown``.
_MAX_STREAM_CHARS = 512_000
#: How much stderr rides into a failure record. Enough to name the failure,
#: nowhere near the 8KB reply bound.
_STDERR_EXCERPT = 800

#: The key every mission-chat payload carries, on every exit path — success,
#: refusal, blocker, replay. It is what tells the handler's payload apart from
#: any other JSON object a child happens to print.
_PAYLOAD_MARKER = "capability_id"

_executor = None
_executor_lock = threading.Lock()
_executor_max_workers = 0

#: Dispatch ids this process is actively supervising — and the guard that keeps
#: the orphan sweep from answering for them.
#:
#: The sweep became PERIODIC in the previous commit, which turned a theoretical
#: second writer into a real one: from the instant a child exits until this
#: supervisor's ``record_completion`` lands — across ``proc.wait()`` and two
#: pump joins — the sweep sees a dead PID on a ``running`` row and settles it
#: ``unknown``. The 5s drain then delivers "the outcome is unknown" for a
#: dispatch that COMPLETED, and the supervisor's real answer, written moments
#: later, is absorbed by the delivery-turn replay dedup — so the sender is told
#: nothing is known and never receives the answer sitting in the row.
#:
#: A row still supervised HERE is not an orphan by definition, so the sweep
#: skips it. Process-local on purpose, and sufficient: the race is between two
#: threads of one serve process, and a row whose supervisor died is exactly the
#: row the sweep SHOULD settle.
_supervised: set[str] = set()
_supervised_lock = threading.Lock()


def _mark_supervised(dispatch_id: str) -> None:
    with _supervised_lock:
        _supervised.add(str(dispatch_id))


def _forget_supervised(dispatch_id: str) -> None:
    with _supervised_lock:
        _supervised.discard(str(dispatch_id))


def supervised_dispatch_ids() -> set[str]:
    """A snapshot of every dispatch this process is actively supervising.

    The orphan sweep reads this to know which ``running`` rows are not orphans
    at all. Returns a COPY: the sweep iterates while supervisors come and go,
    and handing out the live set would make that a mutation-during-iteration
    bug on a background thread.
    """

    with _supervised_lock:
        return set(_supervised)


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
    marked: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            # PREFER the handler's own payload. "last object wins" was wrong in
            # both directions and neither was theoretical: a shutdown notice
            # (``{"event":"mcp_shutdown","ok":false}``) printed after a
            # successful turn made it an error, and a trailing ``{}`` made it a
            # success with an EMPTY reply — which reads as "they had nothing to
            # report". Every mission-chat payload carries ``capability_id`` on
            # every exit path, so the discriminator is the producer's own, not a
            # guess about ordering.
            if _PAYLOAD_MARKER in value:
                marked = value
            elif marked is None:
                fallback = value
        index = text.find("{", max(end, index + 1))
    # The fallback keeps a pre-marker or hand-stubbed payload readable rather
    # than reporting a turn that did answer as having produced nothing.
    #
    # It is NOT an inversion of the old "last object wins", though it reads like
    # one: `fallback` is overwritten by every unmarked object while `marked` is
    # still None, so when NO marked payload exists at all — the only case where
    # `fallback` is ever returned — it holds the last object, exactly as before.
    # The `elif` only stops it tracking objects printed AFTER a marked payload,
    # and in that case `marked` wins regardless. Stated here because the shape
    # invites the wrong reading twice over.
    return marked if marked is not None else fallback


# --------------------------------------------------------------------------
# running one dispatch
# --------------------------------------------------------------------------


class _BoundedTail:
    """A capture that keeps the LAST ``limit`` characters and drops the head.

    WHICH END IS KEPT IS THE WHOLE POINT. The child prints its JSON payload
    LAST, after everything else it has to say, so a head-keeping bound throws
    away exactly the thing the parent came for: a chatty turn that crossed the
    cap had its own answer discarded, ``parse_child_payload`` returned None, and
    a successful thirty-minute dispatch was recorded ``unknown`` — with an empty
    stderr excerpt, because the noise was all on stdout. Reproduced at 583,070
    characters in, 512,033 captured, payload gone.

    The running total is not a micro-optimisation either. Re-summing the sink on
    every line is O(n²), so a child that printed past the cap pinned a pump
    thread to a core for the rest of a half-hour run, inside a long-lived serve.

    WHAT ``limit`` ACTUALLY BOUNDS, stated because it is not quite what it
    looks like: the eviction loop stops at one remaining chunk, so a SINGLE
    chunk larger than the limit is kept whole. That is deliberate — a chunk is
    one line, the payload is a single line of JSON, and truncating it produces
    something ``parse_child_payload`` cannot read, which is precisely the
    data-loss this class exists to prevent. The effective ceiling is therefore
    ``max(limit, longest single line)``, not ``limit``, and this runs inside a
    long-lived serve process. Bounding it for real would have to happen at the
    producer (a child that bounds its own payload line), never here, where the
    only available tool is the truncation that breaks it.
    """

    __slots__ = ("_chunks", "_limit", "_total", "dropped_chars")

    def __init__(self, limit: int):
        from collections import deque

        self._chunks: Any = deque()
        self._limit = int(limit)
        self._total = 0
        self.dropped_chars = 0

    def append(self, text: str) -> None:
        if not text:
            return
        self._chunks.append(text)
        self._total += len(text)
        while self._total > self._limit and len(self._chunks) > 1:
            oldest = self._chunks.popleft()
            self._total -= len(oldest)
            self.dropped_chars += len(oldest)

    def text(self) -> str:
        return "".join(self._chunks)


def _drain(stream, sink: _BoundedTail) -> threading.Thread:
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


def _release_pumps(proc, threads) -> None:
    """Join the pumps, then force them loose if a survivor still holds the pipe.

    ``readline`` blocks until EOF, and EOF only arrives when every writer has
    closed. A grandchild that inherited the pipe — which is exactly what "go run
    the suite" spawns — or a tree member that survived the kill keeps it open,
    so a plain join leaks two daemon threads PER DISPATCH, permanently, inside a
    process that is meant to run for days.

    Closing the parent's handle unblocks the reader (it raises, and the pump
    swallows it), which bounds the thread even when the pipe does not close on
    its own. Best effort by contract: a thread that still will not budge is one
    leak, not a growing one, and never a failed dispatch.
    """

    for thread in threads:
        thread.join(timeout=10)
    if not any(thread.is_alive() for thread in threads):
        return
    for stream in (getattr(proc, "stdout", None), getattr(proc, "stderr", None)):
        try:
            if stream is not None and not stream.closed:
                stream.close()
        except Exception:
            pass
    for thread in threads:
        thread.join(timeout=2)


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

    Never raises, and that is now STRUCTURAL rather than a claim this docstring
    makes about the code below it. The body used to run unguarded, so a raise
    before the spawn — ``build_dispatch_argv`` subscripting a malformed spec,
    ``child_environment`` failing — left the row ``running`` and owned by the
    sender's still-live serve PID, which the orphan sweep can therefore never
    settle: running forever, with nobody waiting on a Future to notice, because
    ``executor.submit`` discards it. The wrapper below turns every such raise
    into a recorded terminal state.
    """

    try:
        _run_dispatch_guarded(dispatch_id, spec)
    except BaseException as exc:  # noqa: BLE001 - a detached turn must never vanish
        logger.exception("detached dispatch %s failed unrecoverably", dispatch_id)
        try:
            from agent_runtime import dispatch_store

            dispatch_store.record_completion(
                dispatch_id,
                state=dispatch_store.STATE_ERROR,
                error=(
                    "the dispatch supervisor failed before it could record a result: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        except Exception:  # pragma: no cover - the store is the last resort
            logger.exception(
                "dispatch %s could not be settled after a supervisor failure", dispatch_id
            )
        return
    finally:
        # ONE release site. The inner handler used to release too, which was
        # harmless (the operation is idempotent) but read as though the outer
        # `finally` did not cover the early return — it does, and a second call
        # invites the next reader to assume one of them is load-bearing.
        _forget_supervised(dispatch_id)


def _run_dispatch_guarded(dispatch_id: str, spec: dict[str, Any]) -> None:
    """The supervisor body. See :func:`_run_dispatch` for the failure contract."""

    from agent_runtime import dispatch_store

    # ``spec["max_seconds"]`` everywhere: the tool always sets it, and reading
    # the same key two ways (subscript here, ``.get(...) or 1800`` there) is how
    # a spec-shape bug hides behind a default that looks deliberate.
    budget = float(spec["max_seconds"])
    # Minted HERE: the turn's clock starts when the turn starts, not when the
    # dispatch was enqueued behind the concurrency cap.
    deadline_epoch = time.time() + budget
    argv = build_dispatch_argv(spec, deadline_epoch=deadline_epoch)
    env = child_environment(spec)

    stdout_tail = _BoundedTail(_MAX_STREAM_CHARS)
    stderr_tail = _BoundedTail(_MAX_STREAM_CHARS)
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

    out_thread = _drain(proc.stdout, stdout_tail)
    err_thread = _drain(proc.stderr, stderr_tail)

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

    # Join the pumps so nothing the child wrote is missed, then force them loose
    # rather than leaking a thread per dispatch on a pipe a survivor holds open.
    _release_pumps(proc, (out_thread, err_thread))

    stdout_text = stdout_tail.text()
    stderr_text = stderr_tail.text()
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
    # This process is the LAST one holding the child's payload — the row is all
    # the sender ever sees — so whether that turn produced anything visible has
    # to be recorded here or it is gone. `ok` does not answer it: a turn whose
    # model returned no content comes back `ok` with an empty reply, and the
    # sender then reads the same blank as a reply that never made it out.
    # Read, never re-derived: `agent_runtime.turn_visibility` owns the verdict.
    from agent_runtime.turn_visibility import TurnVisibility

    dispatch_store.record_completion(
        dispatch_id,
        state=dispatch_store.STATE_COMPLETED if ok else dispatch_store.STATE_ERROR,
        reply=str(payload.get("reply") or ""),
        error="" if ok else _detached_error_text(payload),
        target_session_id=str(payload.get("session_id") or payload.get("chat_session_id") or ""),
        total_tokens=payload.get("total_tokens"),
        visibility=TurnVisibility.from_payload(payload).as_dict(),
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
    # Marked BEFORE submit, not inside the worker: a dispatch queued behind the
    # concurrency cap has not started yet, but its row is already `running` and
    # the sweep must not answer for it either.
    _mark_supervised(dispatch_id)
    try:
        executor.submit(_run_dispatch, dispatch_id, spec)
    except Exception:
        _forget_supervised(dispatch_id)
        raise


def summarize_for_caller(row: dict[str, Any]) -> dict[str, Any]:
    """The bounded shape ``agent_chat_dispatches`` returns for one row.

    Read-only projection of a store row: enough for an agent to answer "did the
    thing I asked for come back yet?", never the whole reply (that arrives as
    its own delivered turn, and duplicating it here would double the context
    cost of every status check).

    ``delivery_error`` is here because ``delivery_state`` alone leaves the one
    party actually owed the answer — the agent that dispatched the work — able
    to see THAT delivery was abandoned but never why, at any point. Its whole
    recourse is to decide whether to re-dispatch, and "dropped" without a reason
    does not support that decision: a vanished chat root means re-sending is
    pointless, while an exhausted attempt cap means it is exactly right.
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
        # Bounded like every other field here; the reason is a short token
        # (`attempt_cap`, `no_sender_session`, …), never prose.
        "delivery_error": str(row.get("delivery_error") or "")[:200] or None,
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
