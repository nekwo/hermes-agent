"""Fill the per-persona memos a create's wire-row projection is about to hit.

Stage 3a of ``mission-control-agent-drop-latency-2026-08-21``. Stage 0 convicted
C2 with numbers off the operator's own drop log: ``instance_ms`` for the FIRST
drop of a persona type this serve process had not resolved recently ran
**1,906 / 3,203 / 2,858 ms**, against **141 / 125 ms** for a second drop of the
same type seconds later. The mechanism behind those numbers is reproduced as a
unit fact rather than quoted — see ``tests/agent_runtime/test_persona_prewarm.py``.
(Measured directly while building this module, on a hermetic root with the whole
``check_fn`` cache genuinely cold: the first create cost 25 registry probe rounds
and ``instance_ms`` 2,828; the second cost 0 rounds and 62 ms. The suite does not
pin those magnitudes — a wall-clock budget is a flake generator — it pins the
counted mechanism.)

Where the milliseconds are
--------------------------
``perform_agent_create`` → ``PersonaInstanceStore.add_instance`` →
``emit_persona_instance_create`` → ``project_persona_instance_full_wire_row``
(``state_patches.py``) → ``persona_instance_summary``
(``persona_assignments.py``) → ``resolve_tool_visibility``
(``tool_visibility.py``). That last call is INLINE on the create's critical path
— the RPC does not return until the wire row is projected — and on a cold
persona type every expensive input of it misses its memo:

* ``tool_visibility._cached_tool_names_for_toolsets`` — ``lru_cache(128)``,
  process lifetime;
* ``tool_visibility._cached_profile_readiness_for_visibility`` — 15 s TTL;
* ``profile_readiness._provider_issue`` — 60 s TTL;
* ``tools.registry._check_fn_cached`` — 30 s TTL per ``check_fn``, and the
  sweep behind ``all_registered_toolsets()`` runs one probe ROUND per toolset.

Every one of those is PROCESS-WIDE and keyed on inputs a prewarm can reproduce
before the drop happens. So this module runs the same resolution on a background
worker and throws the answer away: the create that follows finds the memos
already filled. Nothing about the create changes — no wire contract moves, no
call site in the create path is touched, and with this module never called the
create behaves exactly as it does today.

What it is allowed to do, and what it provably does not
-------------------------------------------------------
It fills PROCESS-LIFETIME MEMOS AND NOTHING ELSE. It writes no store state,
emits no event, mints no id, and takes no lock. That is a property of the path,
not an intention:

* ``permission_options_for_chat`` reads ``ChatToolPermissionStore`` and — with
  ``session_id=None``, which is what this module passes — returns before even
  that read;
* ``apply_chat_lane_tool_scope`` composes ``effective_toolsets`` /
  ``all_registered_toolsets`` / ``chat_lane_capability_drops`` /
  ``mission_chat_workdir_for_persona``, all pure reads;
* ``profile_readiness_for_persona`` routes its provider check through
  ``probe_runtime_provider``, the named NON-persisting resolver that exists
  precisely so a readiness read cannot move ``auth.json``
  (``hermes_cli/runtime_provider.py:2264``), and the ``openai-codex`` arm peeks
  the credential pool without refreshing.

Which is why the warm can be fire-and-forget: there is no half-written state a
killed worker could leave behind. A failure inside it is swallowed and logged —
it warms caches or it does nothing, and either way the create that follows is
correct, only slower.

Why ONE worker thread and not one per call
-------------------------------------------
The palette opens with N persona chips and the launcher's trigger fires once per
chip. N threads would each find the ``check_fn`` TTL cache cold and each run the
full toolset sweep — N concurrent ``docker version`` subprocesses, N playwright
probes — which is the thundering herd the memo exists to prevent, paid all at
once. Serialising through one worker means the FIRST warm fills the shared
registry cache and every warm behind it is cheap. The in-flight set makes a
repeat call for a persona already queued a no-op rather than a second entry, so
"call it on every palette open" cannot grow the queue.

W2-H3 re-opened that as a question and MEASURED it rather than re-arguing it.
A fresh process, isolated home, six synthetic personas warmed back to back::

    warm #1  elapsed_ms=2172
    warm #2  elapsed_ms=0
    warm #3  elapsed_ms=16
    warm #4  elapsed_ms=0
    warm #5  elapsed_ms=16
    warm #6  elapsed_ms=0

That shape settles it, and against a second worker rather than for one. The
queue's whole cost is ONE item: the memos the first warm fills are keyed on the
callable and the toolset tuple, not on the persona, so every warm behind it is
already free. A second worker cannot shorten a 2,172 ms item; it can only run a
SECOND cold sweep beside it — the herd this section opens by refusing — and
would buy, at best, 16 ms. **Bounded concurrency is therefore NOT implemented,
and this paragraph is why**; the number to re-measure before anyone revisits it
is warm #2, not warm #1.

Against the pacing question the stage actually asked — can the warm beat a drop
roughly 100 s after boot — 2.2 s cold plus ~10 ms per additional persona wins by
about 98 s. (W2-H1's grace-window backoff does not move warm #1: with no prior
success recorded, a cold process has no grace to serve from and honours its
failures immediately. It moves the steady state, where a flapping probe used to
re-run at full cost on every call.)

The worker is a daemon: a serve process shutting down must not wait on a cache
fill, and there is nothing to flush.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: The worker's completion receipt, format-pinned by
#: ``tests/agent_runtime/test_persona_prewarm.py``. One INFO line per warm the
#: worker finishes, with the elapsed cost of THAT warm.
#:
#: Why it exists: this module's whole claim is "the memos are filled before the
#: drop arrives", and that claim is a race against the operator's gesture. It was
#: unfalsifiable — a single FIFO worker with no completion line means the pacing
#: question ("did the warm finish first, and how much slack was there?") could
#: only be answered by inferring from the create's own cost, which is exactly the
#: number the warm is supposed to change. A start with no finish measures nothing.
#:
#: TIMINGS ONLY. Never a persona display name, never a resolved toolset, never
#: anything the warm read — the receipt says how long, about which id, and stops.
PREWARM_DONE_RECEIPT = "persona_prewarm done persona=%s elapsed_ms=%d"

#: A warm was queued for this persona by THIS call.
PREWARM_STATE_STARTED = "started"

#: A warm for this persona was already queued or running; this call started
#: nothing and the caller is not being told to wait for anything new. Reported
#: rather than collapsed into ``started`` because "idempotent" is a claim a
#: client should be able to SEE, not one it has to trust.
PREWARM_STATE_ALREADY_RUNNING = "already_running"

#: The call named no persona at all.
PERSONA_ID_REQUIRED_REASON = "persona_id_required"

#: The call named a ``profile:`` id. ``runtime.agent.create`` deliberately
#: ACCEPTS those (decision D-U1 — the launcher's template browser sends ids for
#: profiles that own no persona row), and the create's wire-row projection then
#: resolves its visibility persona from the freshly minted INSTANCE
#: (``persona_assignments._profile_visibility_persona``: instance ``profile_id``,
#: instance ``skill_overrides``, instance ``display_name``). No instance exists
#: at prewarm time, so the memo keys that resolution will hit cannot be known
#: here. Refusing with its own reason is the honest answer; guessing a persona
#: shape would fill a NEIGHBOURING key and warm nothing the create reads.
PROFILE_PERSONA_NOT_PREWARMABLE_REASON = "profile_persona_not_prewarmable"


@dataclass(frozen=True)
class PersonaPrewarmRefusal:
    code: int
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class PersonaPrewarmOutcome:
    """Exactly one of ``result`` / ``refusal`` is set.

    Mirrors :class:`agent_runtime.agent_create.AgentCreateOutcome` so the RPC
    shim over this service is the same three lines the create's shim is.
    """

    result: dict[str, Any] | None = None
    refusal: PersonaPrewarmRefusal | None = None


def _refused(code: int, message: Any, data: dict[str, Any]) -> PersonaPrewarmOutcome:
    return PersonaPrewarmOutcome(
        refusal=PersonaPrewarmRefusal(code=code, message=str(message), data=data)
    )


# ── the warm itself ──────────────────────────────────────────────────────────


def warm_persona_memos(persona: Any) -> None:
    """Run the create's visibility resolution for ``persona`` and DISCARD it.

    Synchronous, and the whole unit of work. The return value is thrown away on
    purpose: this function's entire output is the state left behind in the four
    memos named in the module docstring.

    ``session_id=None`` rather than a fabricated one. The create resolves with
    ``session_id=instance.default_chat_session_id`` — an id minted moments
    earlier, which therefore has no ``ChatToolPermissionStore`` record and no
    session-scoped toolset state, so both resolve to the SAME runtime-default
    posture and therefore the same memo keys. Fabricating an id here would not
    make the match closer; it would only add a store read that a ``None`` skips
    outright.

    Not exception-guarded: the caller that runs this on the worker owns the
    swallow-and-log, and a direct caller (a test, a future warm-on-placement)
    should see its own failure.
    """

    from .persona_runtime import apply_chat_lane_tool_scope
    from .tool_permissions import permission_options_for_chat
    from .tool_visibility import resolve_tool_visibility

    options = permission_options_for_chat(persona, session_id=None)
    # The same chat-lane scoping ``persona_instance_summary`` applies before it
    # resolves (``persona_assignments.py:2972``). Not optional: it is what sets
    # ``chat_lane_blocked_tool_names``, and ``_cached_tool_names_for_toolsets``
    # is keyed on (toolsets, BLOCKED). Warming without it fills a neighbouring
    # key — under the unbounded default the toolset half would still match, so
    # the warm would look like it worked and prime nothing the create reads.
    apply_chat_lane_tool_scope(persona, options, session_id=None)
    resolve_tool_visibility(persona, options)


# ── the background worker ────────────────────────────────────────────────────

_queue: "queue.Queue[Any]" = queue.Queue()
_lock = threading.Lock()
_pending: set[str] = set()
_worker: threading.Thread | None = None


def _drain() -> None:
    """Warm one persona at a time, forever, and never die.

    Every failure mode is contained here rather than at the call site, because
    the call site is a JSON-RPC handler that has already answered. A raise that
    escaped this loop would kill the worker thread and silently turn every LATER
    prewarm into a no-op — the failure shape that is hardest to notice, since
    the only symptom is the slow create the prewarm was supposed to prevent.
    """

    while True:
        persona_id, persona = _queue.get()
        # Started AFTER the get, so the receipt measures the warm and not the
        # idle wait for work — a queue that sat empty for a minute must not
        # report a minute-long warm.
        started = time.monotonic()
        try:
            warm_persona_memos(persona)
        except Exception:
            # Swallowed on purpose: the caller was answered long ago, and a
            # cache that did not fill costs latency, never correctness. The
            # elapsed cost rides this line too: a warm that failed after seconds
            # of probing occupied the single worker for exactly that long, and a
            # pacing census that could only see the successes would under-count
            # the queue's real service time.
            logger.warning(
                "persona prewarm failed for %s after %d ms; the next create "
                "pays the cold cost it would have avoided",
                persona_id,
                int(max(0.0, time.monotonic() - started) * 1000),
                exc_info=True,
            )
        else:
            logger.info(
                PREWARM_DONE_RECEIPT,
                persona_id,
                int(max(0.0, time.monotonic() - started) * 1000),
            )
        finally:
            with _lock:
                _pending.discard(persona_id)
            _queue.task_done()


def _ensure_worker() -> None:
    """Start the single daemon worker on first use. Call under ``_lock``."""

    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = threading.Thread(
        target=_drain, name="persona-prewarm", daemon=True
    )
    _worker.start()


# ── the service entry point ──────────────────────────────────────────────────


def request_persona_prewarm(params: dict[str, Any]) -> PersonaPrewarmOutcome:
    """Validate, resolve, queue — and return without waiting for the warm.

    Params: ``persona_id`` (required), ``correlation_id`` (optional, echoed).

    Result::

        {persona_id, accepted: true, state: "started" | "already_running"}

    The persona is resolved SYNCHRONOUSLY, before anything is queued, through
    ``agent_create.resolve_persona`` — the same lookup ``runtime.agent.create``
    refuses on. Two reasons it is not deferred to the worker. An id that names
    nothing must be answered rather than silently dropped into a queue, which is
    what "no probing done for it" means in practice. And sharing the create's
    resolver means "unknown persona" is ONE fact with one spelling across the
    two verbs, so a launcher that prewarms an id it can then create never gets
    contradictory verdicts from the pair.
    """

    # Every name here comes from ``agent_create`` — including the error code,
    # which that module already re-spells rather than importing from
    # ``serve_rpc`` (see its comment at the constant: serve_rpc imports the
    # service, not the other way round, and a CLI process that only creates an
    # agent must not drag the method registry in). Borrowing its copy keeps this
    # module out of that dependency AND avoids minting a third spelling of
    # ``-32602``; the drift fence in ``test_agent_create_service.py`` still holds
    # the one copy to serve_rpc's.
    from .agent_create import (
        ERR_INVALID_PARAMS,
        PERSONA_NOT_FOUND_REASON,
        PERSONA_ROSTER_UNAVAILABLE_REASON,
        PersonaRosterUnavailable,
        persona_not_found_message,
        persona_roster_unavailable_message,
        resolve_persona,
    )

    raw = params.get("persona_id")
    persona_id = raw.strip() if isinstance(raw, str) else ""
    if not persona_id:
        return _refused(
            ERR_INVALID_PARAMS,
            "persona_id is required",
            {"reason": PERSONA_ID_REQUIRED_REASON},
        )
    if persona_id.lower().startswith("profile:"):
        return _refused(
            ERR_INVALID_PARAMS,
            f"{persona_id!r} names a profile, not a roster persona; its "
            "visibility memos are keyed on the instance the create mints, so "
            "there is nothing to warm before that create runs",
            {
                "reason": PROFILE_PERSONA_NOT_PREWARMABLE_REASON,
                "persona_id": persona_id,
            },
        )

    try:
        persona = resolve_persona(persona_id)
    except PersonaRosterUnavailable as exc:
        # ``resolve_persona`` swallows this today, but it is the caller's job to
        # keep "fix your id" and "fix your runtime" apart, and a resolver that
        # grows the raise later must not silently start reading as a typo here.
        return _refused(
            ERR_INVALID_PARAMS,
            persona_roster_unavailable_message(exc),
            {"reason": PERSONA_ROSTER_UNAVAILABLE_REASON},
        )
    if persona is None:
        return _refused(
            ERR_INVALID_PARAMS,
            persona_not_found_message(persona_id),
            {"reason": PERSONA_NOT_FOUND_REASON, "persona_id": persona_id},
        )

    with _lock:
        already = persona_id in _pending
        if not already:
            _pending.add(persona_id)
            _ensure_worker()
            # Inside the lock: the worker discards from ``_pending`` only after
            # it has finished the item, so enqueueing here cannot race a discard
            # into a state where a queued id looks absent.
            _queue.put((persona_id, persona))

    result: dict[str, Any] = {
        "persona_id": persona_id,
        "accepted": True,
        "state": (
            PREWARM_STATE_ALREADY_RUNNING if already else PREWARM_STATE_STARTED
        ),
    }
    correlation_id = params.get("correlation_id")
    if isinstance(correlation_id, str) and correlation_id.strip():
        result["correlation_id"] = correlation_id.strip()
    return PersonaPrewarmOutcome(result=result)
