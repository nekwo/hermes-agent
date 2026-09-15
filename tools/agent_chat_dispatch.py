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
    "PEER_DIAL_TIMEOUT_SECONDS",
    "PEER_RETRY_BACKOFF_SECONDS",
    "build_dispatch_argv",
    "build_peer_execute_params",
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

#: Gateway Stage 7 (R8), the "bounded per-attempt dial timeout" half. How long
#: ONE attempt waits for a paired install to complete TCP, TLS and the peer
#: hello. Short on purpose: an install that is off, asleep or moved answers
#: nothing, and eight attempts at a generous timeout would take an hour to
#: converge on a fact that is knowable in seconds. It is not the budget for the
#: TURN — see :data:`~agent_runtime.serve_socket.ServeSocketClient.set_timeout`.
PEER_DIAL_TIMEOUT_SECONDS = 15.0

#: The ``event`` a serve request's STDOUT lines carry
#: (``serve.py:1784``: ``_LineFrameProxy(frames, "line")``). Not ``"stdout"`` —
#: only the error stream is named after itself — and named here because a
#: reader that guesses collects nothing and sees an empty payload rather than an
#: error. Fenced by ``test_gateway_peer_cross_install_chat_e2e``, which is the
#: thing that caught the guess.
SERVE_STDOUT_EVENT = "line"

#: The gap between dial attempts. Deliberately flat rather than exponential:
#: the case this cap exists for is a machine that is off, and backing off
#: geometrically would turn a converge-in-a-minute answer into a converge-in-an-
#: hour one for no information gained. With the cap at
#: ``MAX_DELIVERY_ATTEMPTS`` an offline install settles in roughly two minutes,
#: which is the same order as the local lane's own attempt-cap convergence.
PEER_RETRY_BACKOFF_SECONDS = 5.0

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


def build_peer_execute_params(dispatch_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """The ``peer.agent_chat.execute`` params for one cross-install dispatch.

    The mirror of :func:`build_dispatch_argv`, and the differences between the
    two are the whole of what "the far side runs it" costs:

    * ``turn_request_id`` is derived from the DISPATCH ID rather than minted per
      attempt, and that is what makes R8's retry posture safe. A dial that dies
      after install B accepted the turn is a transport failure the sender must
      retry; if each retry carried a fresh id, B would run the turn again. With
      the dispatch's own id, B's reservation and its turn journal answer the
      second attempt ``idempotent_replay`` with the first attempt's ack — the
      exactly-once property Stage 3 built, used here for the first time by
      something that actually retries. It is the same derivation
      ``dispatch_delivery.delivery_client_message_id`` uses on the sender's own
      side, for the same reason.
    * The relay CHAIN does not travel. Locally the chain is seeded in-process by
      the handler and forwarded so depth and cycle detection run at B's
      chokepoint; across an install boundary it would be an assertion B cannot
      check, and a chain a sender can understate is a cycle guard that can be
      talked past. So a cross-install dispatch is a fresh chain root on B —
      which is what a DETACHED dispatch already is locally (the 2026-08-03 relay
      ruling), applied one boundary further out. The honest consequence is
      named in the plan's notes rather than hidden: A→B→A across two installs is
      not detected as a cycle by either side's guard.
    * There is no ``clarify_token`` and no ``requested_by``. The first is a
      ticket in THIS install's store and means nothing on the other one; the
      second is the field install B decides for itself, from the connection.
    """

    params: dict[str, Any] = {
        "turn_request_id": str(spec.get("client_message_id") or f"agent-dispatch-{dispatch_id}"),
        "target": str(spec["remote_target"]),
        "message": str(spec["message"]),
        "max_seconds": float(spec["max_seconds"]),
    }
    if spec.get("title"):
        params["title"] = str(spec["title"])
    if spec.get("session_id"):
        params["session_id"] = str(spec["session_id"])
    if spec.get("new_session") is True:
        params["new_session"] = True
    return params


def _remote_reply_payload(connection: Any, request_id: str) -> dict[str, Any] | None:
    """Read one accepted remote turn's frames to its exit, and parse the payload.

    Install A does here exactly what the local launcher does with a local turn,
    and that is the point: the ack is an ACCEPT (Stage 3's constraint — B's
    dispatcher answers the method lane inline, so it cannot run a turn there),
    and the turn's real output rides the per-request frame lane under the
    returned ``request_id``. The lines are re-joined and handed to
    :func:`parse_child_payload` — the same function the local child's stdout
    goes through — so a remote turn's payload and a local turn's payload are
    read by one implementation rather than two that agree today.

    ``None`` means the connection ended before the exit frame. The caller treats
    that as TRANSPORT, not as a result, which is the whole R8 distinction.

    **The stdout event is called ``line``, not ``stdout``**, and this comment is
    here because the obvious guess is wrong and cost the Stage 7 acceptance a
    round: ``serve.py`` builds its two proxies as
    ``_LineFrameProxy(frames, "line")`` and ``_LineFrameProxy(frames, "stderr")``
    — the OUT stream is the unqualified one and only the error stream is named
    after itself. Spelled as a constant so the next reader is told rather than
    having to find it.
    """

    lines: list[str] = []
    while True:
        frame = connection.read_frame()
        if frame is None:
            return None
        if frame.get("id") != request_id:
            # Another request's frames, or a push on the same socket. Not ours.
            continue
        event = frame.get("event")
        if event == SERVE_STDOUT_EVENT:
            lines.append(str(frame.get("line") or ""))
        elif event == "exit":
            return {
                "payload": parse_child_payload("\n".join(lines)),
                "code": frame.get("code"),
            }


def _run_remote_dispatch(dispatch_id: str, spec: dict[str, Any]) -> None:
    """Perform a dispatch whose target lives on a PAIRED INSTALL (Stage 7).

    The substitution is for the child PROCESS and for nothing else. The row was
    written before this ran, is written again when this finishes, and the
    delivery drain forges the answer into the sender's chat afterwards — all
    unchanged. What changes is that the turn happens on install B, under B's
    admission rules, recorded in B's own chat store.

    **R8's posture, and the one line that decides which class a failure is in.**
    A failure to REACH the install is transport: nothing was refused, nothing
    ran, and the same attempt tomorrow might work — so it costs an attempt out
    of :data:`~agent_runtime.dispatch_store.MAX_DELIVERY_ATTEMPTS` and retries.
    A failure the far install ANSWERED with is deterministic: an unknown
    persona, an admission refusal, a revoked edge are pure functions of B's
    state that another attempt cannot change, so the row settles immediately —
    the same fail-fast rule ``_terminal_forge_rejections`` applies on the
    delivery side, and for the same reason (burning eight attempts ends with the
    operator reading "attempts ran out" instead of the verdict that happened).

    The attempts are spent HERE, inside one supervised run, rather than by
    re-queueing the row on the drain's cadence. That holds a slot on the
    dispatch pool for as long as the retries take — which is the honest cost and
    a small one: a LOCAL dispatch holds its slot for the whole of a
    thirty-minute turn, so a remote one holding it for two minutes of dialling
    is well inside what the cap already admits. The alternative wanted a durable
    copy of the spec, a second claim protocol and a second attempt counter on a
    row that already has one, to buy a property (surviving a serve restart
    mid-dial) that the local lane does not have either.
    """

    from agent_runtime import dispatch_store
    from agent_runtime.gateway_peers import dial_peer
    from agent_runtime.gateway_targets import peer_store_root

    install_id = str(spec["remote_install_id"])
    display = str(spec.get("remote_display_name") or install_id)
    budget = float(spec["max_seconds"])
    params = build_peer_execute_params(dispatch_id, spec)
    root = peer_store_root()

    failures: list[str] = []
    attempts = 0
    while attempts < dispatch_store.MAX_DELIVERY_ATTEMPTS:
        attempts += 1
        connection = None
        try:
            connection, _hello = dial_peer(
                root, install_id, timeout_seconds=PEER_DIAL_TIMEOUT_SECONDS
            )
        except Exception as exc:
            # ``dial_peer`` raises ``ConnectionError`` for every unreachable
            # endpoint and for a revoked or credential-less row. The revoked
            # case is already refused deterministically at send time, so what
            # reaches here is transport — and anything else that raises out of a
            # dial is transport too, because a dial that raised did not run a
            # turn.
            failures.append(f"attempt {attempts}: {type(exc).__name__}: {exc}")
            if attempts < dispatch_store.MAX_DELIVERY_ATTEMPTS:
                time.sleep(PEER_RETRY_BACKOFF_SECONDS)
            continue

        try:
            rid = f"peer-exec-{dispatch_id}"
            connection.send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": "peer.agent_chat.execute",
                    "params": params,
                }
            )
            ack = None
            while True:
                frame = connection.read_frame()
                if frame is None:
                    break
                if frame.get("id") == rid and (
                    "result" in frame or "error" in frame
                ):
                    ack = frame
                    break
            if ack is None:
                failures.append(f"attempt {attempts}: the edge closed before an ack")
                if attempts < dispatch_store.MAX_DELIVERY_ATTEMPTS:
                    time.sleep(PEER_RETRY_BACKOFF_SECONDS)
                continue
            if "error" in ack:
                # THE far install answered. Deterministic — settle now.
                error = ack["error"] or {}
                reason = str((error.get("data") or {}).get("reason") or "")
                dispatch_store.record_completion(
                    dispatch_id,
                    state=dispatch_store.STATE_ERROR,
                    error=(
                        f"{display} refused the request: "
                        f"{str(error.get('message') or 'no reason given')[:300]}"
                    ),
                    remote={
                        "install_id": install_id,
                        "attempts": attempts,
                        "reason": reason or "peer_refused",
                    },
                )
                return

            result = ack.get("result") or {}

            # **A REPLAY carries no frames, and waiting for them is a hang.**
            # Found by the acceptance rather than reasoned to: B's per-request
            # frames go to the sink of the connection that ASKED, so a turn
            # started on a socket that then died emits nothing on the socket
            # that retries — the reader would sit until its own timeout for an
            # exit frame that already happened somewhere else. This arm is
            # reached exactly when the retry posture worked (the same
            # ``turn_request_id`` stopped B running the agent twice), so it is
            # the success path of the property, not an error path.
            if result.get("idempotent_replay"):
                if not result.get("settled"):
                    # Accepted by an earlier attempt and STILL RUNNING over
                    # there. Nothing to read and nothing to fix; another attempt
                    # after the backoff may find it settled.
                    failures.append(
                        f"attempt {attempts}: the turn is still running on {display}"
                    )
                    if attempts < dispatch_store.MAX_DELIVERY_ATTEMPTS:
                        time.sleep(PEER_RETRY_BACKOFF_SECONDS)
                    continue
                code = result.get("exit_code")
                dispatch_store.record_completion(
                    dispatch_id,
                    state=(
                        dispatch_store.STATE_COMPLETED
                        if code == 0
                        else dispatch_store.STATE_ERROR
                    ),
                    error=(
                        ""
                        if code == 0
                        else (
                            f"{display} ran the turn and it ended with code {code}."
                        )
                    ),
                    # Honest about what is NOT here: the reply text went to the
                    # connection that started the turn, and that connection is
                    # gone. The thread on the other install has it.
                    reply=(
                        f"[{display} ran this turn on an earlier attempt; its "
                        "answer is in the thread on that install — the "
                        "connection that carried it did not survive.]"
                    ),
                    remote={
                        "install_id": install_id,
                        "attempts": attempts,
                        "reason": "peer_turn_replayed",
                    },
                )
                return

            # Accepted, fresh. The turn's own budget governs the read from here
            # — the dial timeout was about reaching the machine, not about how
            # long its agent may think.
            connection.set_timeout(budget + KILL_GRACE_SECONDS)
            request_id = str(result.get("request_id") or "")
            if not request_id:  # pragma: no cover - the ack always carries one
                failures.append(f"attempt {attempts}: the ack named no request id")
                continue
            answered = _remote_reply_payload(connection, request_id)
        except Exception as exc:
            failures.append(f"attempt {attempts}: {type(exc).__name__}: {exc}")
            if attempts < dispatch_store.MAX_DELIVERY_ATTEMPTS:
                time.sleep(PEER_RETRY_BACKOFF_SECONDS)
            continue
        finally:
            try:
                connection.close()
            except Exception:  # pragma: no cover - defensive
                pass

        if answered is None:
            # The edge died mid-turn. Retrying is SAFE and this is the reason
            # ``turn_request_id`` is derived from the dispatch id: the next
            # attempt presents the same key, and B replays its first ack rather
            # than running the agent twice.
            failures.append(f"attempt {attempts}: the edge closed mid-turn")
            if attempts < dispatch_store.MAX_DELIVERY_ATTEMPTS:
                time.sleep(PEER_RETRY_BACKOFF_SECONDS)
            continue

        payload = answered["payload"]
        remote = {"install_id": install_id, "attempts": attempts}
        if payload is None:
            dispatch_store.record_completion(
                dispatch_id,
                state=dispatch_store.STATE_UNKNOWN,
                error=(
                    f"{display} ran the turn and its process exited with code "
                    f"{answered['code']} without a reply payload; the outcome is "
                    "unknown. Anything it wrote is in its own chat thread."
                ),
                remote=remote,
            )
            return

        ok = bool(payload.get("ok")) and answered["code"] == 0
        from agent_runtime.turn_visibility import TurnVisibility

        dispatch_store.record_completion(
            dispatch_id,
            state=(
                dispatch_store.STATE_COMPLETED if ok else dispatch_store.STATE_ERROR
            ),
            reply=str(payload.get("reply") or ""),
            error="" if ok else _detached_error_text(payload),
            target_session_id=str(
                payload.get("session_id") or payload.get("chat_session_id") or ""
            ),
            total_tokens=payload.get("total_tokens"),
            visibility=TurnVisibility.from_payload(payload).as_dict(),
            remote=remote,
            # Stage P4 (R-P3). The far install minted these — it is the only one
            # that could, because a handle is a digest of bytes it alone holds —
            # and this is the ONLY moment they are in reach of the row that has
            # to outlive them. The reply text lands in the sender's transcript
            # with `MEDIA:` lines naming paths on B's disk; without the map, A's
            # media scope can never name those pictures and every fetch answers
            # `unknown_handle`. Read here, validated by `dispatch_store` (shape)
            # and by `media_handles` (grammar, allowlist, cap) — never trusted.
            media=payload.get("media"),
        )
        return

    # Every attempt was transport. R8's cap, converging on a terminal answer the
    # sender is actually TOLD — see ``dispatch_store.REMOTE_UNREACHABLE_REASON``
    # for why this is not a ``dropped`` delivery.
    logger.info(
        "dispatch %s: %s unreachable after %d attempts",
        dispatch_id,
        install_id,
        attempts,
    )
    dispatch_store.record_completion(
        dispatch_id,
        state=dispatch_store.STATE_ERROR,
        error=(
            f"{display} did not answer: {attempts} attempts over "
            f"~{attempts * PEER_RETRY_BACKOFF_SECONDS:.0f}s reached no endpoint on "
            f"its paired row ({dispatch_store.REMOTE_UNREACHABLE_REASON}). The "
            "install may be off, asleep, or moved to a new address — its "
            f"operator re-runs `harness gateway peers pair` to update it. Last: "
            f"{failures[-1][:200] if failures else 'no detail'}"
        ),
        remote={
            "install_id": install_id,
            "attempts": attempts,
            "reason": dispatch_store.REMOTE_UNREACHABLE_REASON,
        },
    )


def _run_dispatch_guarded(dispatch_id: str, spec: dict[str, Any]) -> None:
    """The supervisor body. See :func:`_run_dispatch` for the failure contract.

    **Gateway Stage 7's fork is the first line, and it is a fork rather than a
    branch inside the spawn**: a cross-install dispatch does not spawn a child
    at all, so everything below — the argv, the environment, the PID stamp, the
    kill-after-grace — describes work that is not happening on this machine. The
    two legs meet again at ``record_completion``, which is the only thing the
    rest of the lane reads.
    """

    if spec.get("remote_install_id"):
        _run_remote_dispatch(dispatch_id, spec)
        return

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
        # Gateway Stage 7. Present only when the dispatch left this machine, so
        # a local status check reads exactly as it always has. The asking agent
        # needs it for the same decision ``delivery_error`` supports: "should I
        # re-send?" has a different answer when the other install was simply
        # not answering than when its agent refused.
        "remote_install_id": row.get("remote_install_id") or None,
        "remote": result.get("remote") or None,
    }
