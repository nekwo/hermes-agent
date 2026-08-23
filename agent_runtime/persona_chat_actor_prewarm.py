"""Build a chat's resident actor BEFORE its first message arrives.

Why this module exists
----------------------
``write_ahead → agent_ready`` is bimodal. On the second and later turns of one
chat root it is 60-600 ms; on the FIRST turn of a chat root — the exact turn an
operator is watching after opening a chat, and the only turn a fresh chat ever
has — it is 3.0-3.6 s, because that is where ``ProfileRunner._execute_agent_run``
constructs the agent: the OpenAI SDK client, the tool-definition build with its
own ``check_fn`` sweep, and ``tool_search`` activation. Live receipts, 2026-08-23
(serve booted with ``persona_chat.hot_sessions_enabled: true``):

* ``17:33:01Z`` — first message after the boot: ``agent_init_cold=true``,
  bootstrap 3,782 ms of which ``agent_construct_ms=3000``, first byte 10.0 s.
* ``17:33:17Z`` — second message, same chat: ``resident_actor_reused=1``,
  ``agent_init_cold=false``, bootstrap 62 ms, first byte 3.4 s.

The registry and the factory that make the second number possible already
exist (``profile_runner.py``'s ``acquire()`` branch). What did not exist was
anything that called them off the turn's critical path. That is this module: the
same construction, run on a background worker, so the first turn of a chat finds
an already-registered resident entry and pays the second number.

The one property everything here serves: BYTE-MATCH
---------------------------------------------------
``PersonaChatRuntimeRegistry.acquire`` reuses an entry only when the caller's
``signature`` and ``revision`` match the stored ones exactly; a mismatch CLOSES
the entry and rebuilds. So a pre-built actor whose signature the next turn does
not reproduce is not merely useless — it is pure cost, and worse than no prewarm
at all. (This is not hypothetical: the 2026-08-23T14:45:14Z live turn recorded
``resident_rebuild_runtime_signature_changed`` because the signature folded
persona-instance row liveness. That defect is fixed; this module must not
reintroduce its shape.)

Three rules follow, and they explain every awkward-looking import below:

1. **The signature is COMPUTED by the turn's own code, never re-derived.**
   :func:`agent_runtime.mission_chat_turn_context.mission_chat_runtime_signature`
   is the single authority, called here with the same arguments
   ``build_mission_chat_turn_context`` passes it. This module's job is to
   reproduce the INPUTS, and it reproduces each of them through the turn's own
   resolver as well: ``_persona_by_id`` for the persona,
   ``apply_instance_model_overrides`` for the instance tier,
   ``_chat_effective_model_payload`` for the model cascade,
   ``_session_model_config`` for the chat-scoped override,
   ``load_agent_runtime_config`` for the config, ``chat_lane_bundle`` (through
   the signature's own resolvers) for the tool contract and permission state.
2. **The revision and the active session id are read from the SAME places.**
   ``_persona_chat_native_revision`` and ``_persona_chat_native_tip``, the
   helpers the send path calls, on a SessionDB opened exactly as the send path
   opens it. A revision mismatch is a rebuild, not a re-prepare.
3. **The construction runs under the real scope stack.** Not a hand-rolled
   context: ``ProfileAgentRunner.prewarm`` runs ``_execute_agent_run`` with
   ``prewarm_only=True``, so the agent is built inside ``_WORKDIR_LOCK``,
   ``persona_profile_context`` (the profile ``.env``), the workdir,
   tool-execution / chat-root / terminal-envelope / skill scopes, and this
   persona's MCP admission — and torn down the same way. An agent constructed
   under different scopes would be a different agent.

What it will not do
-------------------
**It never guesses a turn input it cannot know.** One input is genuinely
unknowable here: ``--agents-file``, the operator's workspace ``AGENTS.md``,
which the launcher attaches per turn from a selection it holds client-side. This
module prewarms with no workspace file (the state of every chat that has none).
For a WORKSPACE-BOUND chat the first turn's signature therefore differs and
``acquire`` rebuilds — that turn pays exactly what it pays today, and the
prewarmed actor is discarded. That is the honest cost, stated rather than
papered over with a guessed path; the alternative (fabricating a workspace
pointer) would ground a real agent's terminal at a directory the operator never
chose.

**It sends nothing to a model.** Construction builds an OpenAI client object —
``OpenAI client created (agent_init, shared=True)`` — which is local object
creation over already-resolved credentials; the first byte on the wire is
``codex_stream_request``, which lives in ``run_conversation`` and is on the far
side of this module's early return. So no prompt, no completion, no token spend.

**Two side effects it does inherit from the real path, named rather than
denied.** Both are the first turn's own work, performed earlier:

* ``_resolve_request_runtime`` → ``resolve_runtime_provider`` reads credentials.
  On ``openai-codex`` (this lane's live provider) that is a local ``auth.json``
  read, but the resolver is provider-shaped: a Vertex persona mints a short-lived
  OAuth2 access token, and a Nous credential pool may refresh an expired agent
  key. Those are network calls — the SAME ones the chat's first turn would make,
  and its result is cached for the turn behind it
  (``RUNTIME_RESOLVE_CACHE_TTL_SECONDS``), so the prewarm pays them instead of
  the operator. It is not a new class of call, and it is not sent to a model.
* MCP admission spawns this persona's declared servers and registers their
  tools, exactly as the first turn would have — and tears them down on the way
  out, while the run still holds ``_WORKDIR_LOCK``.

**It yields to real work.** ``_WORKDIR_LOCK`` serializes every run in the
process, so a construction holding it while an operator message arrives ADDS
~3 s to that turn instead of removing it. The guard is
``profile_runner.agent_runs_in_flight()``, read immediately before the scope
stack is entered: if any real run is in flight this item stands down
(``skipped_turn_active``) rather than queueing behind it. Constructions are
serialized one at a time on a single daemon worker, so at most one is ever in
flight; and the residual race — a turn arriving DURING a construction — is
bounded by that one construction and is a NO-OP when the turn is for the same
chat root, which is the common case at chat-open: that turn would have built
this exact actor itself, and instead finds it.

Triggers
--------
* **serve boot** — one pass after the ready frame, behind the read-model build
  and the provider warmup on the same thread (see ``serve.py``'s ordering
  doctrine), warming at most ``persona_chat.max_hot_sessions`` chats,
  most-recently-active first.
* **chat open** — ``persona instance open-chat``, the capability the launcher
  fires when an operator opens or creates a chat. Deliberately NOT
  ``PersonaInstanceStore.open_chat``, which the SEND path re-enters on every
  turn: hooking there would fire a background construction against every live
  turn.

Both go through :func:`request_chat_actor_prewarm`, which is a no-op whenever
``persona_chat_runtime_registry()`` is ``None`` — so a CLI one-shot, which never
calls ``initialize_persona_chat_runtime_registry``, pays nothing and starts no
thread.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── receipts ─────────────────────────────────────────────────────────────────
#
# TIMINGS AND IDS ONLY, in the vocabulary of the emitter list in
# ``docs/agent-runtime-harness/07-observability.md``: how long, about which
# root, and what happened. Never a persona display name, never a resolved
# toolset, never anything the construction read.

#: One INFO line per prewarm the worker finishes, with THAT item's outcome and
#: cost. The per-entry half of the receipt — a pass line with only totals cannot
#: answer "which chat was it that took four seconds", and a prewarm whose whole
#: claim is a race against the operator's first message has to be answerable at
#: that grain.
CHAT_ACTOR_PREWARM_DONE_RECEIPT = (
    "persona_chat_actor_prewarm root=%s outcome=%s elapsed_ms=%d"
)

#: One INFO line per boot pass, emitted when the pass has been QUEUED (not when
#: the last item finishes — the worker owns that, one line each). ``candidates``
#: is what the roster offered, ``queued`` what the cap and the dedupe let
#: through; the difference is the cap doing its job, stated rather than inferred.
CHAT_ACTOR_PREWARM_PASS_RECEIPT = (
    "persona_chat_actor_prewarm pass candidates=%d queued=%d skipped=%d elapsed_ms=%d"
)

#: An actor was constructed and is now resident for this chat root.
OUTCOME_WARMED = "warmed"

#: The registry already held an entry for this root under this exact signature
#: and revision — a real turn (or an earlier prewarm) got there first.
#: ``acquire`` returned it and nothing was built. Success, not a miss.
OUTCOME_ALREADY_RESIDENT = "already_resident"

#: ``persona_chat.hot_sessions_enabled`` is off, so there is no registry to put
#: an actor in. Every hook is inert in this state; reported rather than silent
#: so "why did nothing warm" has an answer.
OUTCOME_REGISTRY_OFF = "registry_off"

#: A real agent run was in flight in this process. The prewarm stood down rather
#: than queue behind it on ``_WORKDIR_LOCK``. See the module docstring.
OUTCOME_SKIPPED_TURN_ACTIVE = "skipped_turn_active"

#: No chat root was named, or no persona instance owns the one that was.
OUTCOME_SKIPPED_NO_CHAT_ROOT = "skipped_no_chat_root"

#: The instance names a persona the roster does not resolve.
OUTCOME_SKIPPED_PERSONA_UNRESOLVED = "skipped_persona_unresolved"

#: The persona binds no usable Hermes profile, or its provider is unhealthy.
#: A real turn refuses for the same reason; there is nothing to warm.
OUTCOME_SKIPPED_PROFILE_UNREADY = "skipped_profile_unready"

#: Construction itself raised. The next real turn pays the cold cost it would
#: have avoided and reports its own error — never this one.
OUTCOME_SKIPPED_CONSTRUCT_FAILED = "skipped_construct_failed"


# ── the unit of work ─────────────────────────────────────────────────────────


def prewarm_chat_actor(root_session_id: str, *, instance: Any = None) -> str:
    """Construct and register one chat root's resident actor. Returns an outcome.

    Synchronous and self-contained: everything from resolving the chat's owner
    to the ``acquire()`` that leaves the actor resident. The return value is one
    of the ``OUTCOME_*`` tokens above — this function reports, it never raises
    for an ordinary refusal, and its one raise-through (a construction fault) is
    caught at the bottom and reported as :data:`OUTCOME_SKIPPED_CONSTRUCT_FAILED`.

    Callable directly (a test, an operator diagnosing a chat that will not
    warm); the worker below is just a queue in front of it.
    """

    from .persona_chat_continuity import persona_chat_runtime_registry

    if persona_chat_runtime_registry() is None:
        return OUTCOME_REGISTRY_OFF
    root = str(root_session_id or "").strip()
    if not root:
        return OUTCOME_SKIPPED_NO_CHAT_ROOT

    from .profile_runner import agent_runs_in_flight

    # The yield decision, taken BEFORE anything expensive and before the scope
    # stack. See the module docstring: standing down costs a warm actor; queueing
    # behind a live turn costs that turn ~3 s.
    if agent_runs_in_flight() > 0:
        return OUTCOME_SKIPPED_TURN_ACTIVE

    try:
        prepared = _prepare(root, instance)
    except _PrewarmRefused as refusal:
        return refusal.outcome
    except Exception:
        logger.debug(
            "chat-actor prewarm could not assemble a request for %s", root, exc_info=True
        )
        return OUTCOME_SKIPPED_CONSTRUCT_FAILED

    request, runner = prepared
    # Re-read the gauge: assembling the request above reads SessionDB and
    # resolves the lane bundle, which is where a turn that arrived meanwhile
    # would now be. Cheap insurance against the widest part of the window.
    if agent_runs_in_flight() > 0:
        return OUTCOME_SKIPPED_TURN_ACTIVE
    try:
        timing = runner.prewarm(request)
    except Exception:
        logger.debug("chat-actor prewarm construction failed for %s", root, exc_info=True)
        return OUTCOME_SKIPPED_CONSTRUCT_FAILED
    return (
        OUTCOME_ALREADY_RESIDENT
        if timing.get("resident_actor_reused")
        else OUTCOME_WARMED
    )


class _PrewarmRefused(Exception):
    """An ordinary, typed refusal from :func:`_prepare`."""

    def __init__(self, outcome: str):
        super().__init__(outcome)
        self.outcome = outcome


def _prepare(root: str, instance: Any) -> tuple[Any, Any]:
    """Assemble the EXACT ``AgentRunRequest`` this chat's first turn would build.

    Every value here is produced by the same authority the send path uses. The
    imports from ``hermes_cli.harness_parts.persona_commands`` are the point,
    not an accident: those six helpers ARE the turn's answers, and a private
    copy of any of them is a second answer that would drift from the first
    exactly when it mattered (the drift is silent — it costs a rebuild, not an
    error). They are function-local because that module is large and this one is
    imported at serve boot.
    """

    from hermes_cli.harness_parts.persona_commands import (
        _chat_effective_model_payload,
        _chat_model_override_from_config,
        _persona_by_id,
        _persona_chat_native_revision,
        _persona_chat_native_tip,
        _session_model_config,
    )

    from pathlib import Path

    from . import paths
    from .chat_lane_bundle import chat_lane_bundle
    from .config import load_agent_runtime_config
    from .mcp_admission import LANE_MISSION_CHAT
    from .mission_chat_turn_context import mission_chat_runtime_signature
    from .mission_chat_workdir import mission_chat_workdir_for_persona
    from .models import apply_instance_model_overrides
    from .persona_chat_durability import default_persona_session_db
    from .persona_runtime import PERSONA_CHAT_SCRATCH_SOURCE
    from .persona_chat_continuity import persona_chat_runtime_registry
    from .profile_context import resolve_persona_profile
    from .profile_runner import AgentRunRequest, ProfileAgentRunner
    from .provider_health import assert_provider_health_for_persona
    from .terminal_envelope import scope_for_persona as terminal_envelope_scope_for_persona

    if instance is None:
        instance = _instance_for_root(root)
    if instance is None:
        raise _PrewarmRefused(OUTCOME_SKIPPED_NO_CHAT_ROOT)

    cfg = load_agent_runtime_config()
    persona = _persona_by_id(cfg, str(getattr(instance, "persona_id", "") or ""))
    if persona is None:
        raise _PrewarmRefused(OUTCOME_SKIPPED_PERSONA_UNRESOLVED)
    # The send path folds the instance model-override tier into the persona
    # BEFORE it resolves anything else (persona_commands: `persona =
    # apply_instance_model_overrides(persona, instance)`), and the folded
    # persona is what reaches both the signature and the runtime. Folding it
    # anywhere but here would key the actor on a persona nobody runs.
    persona = apply_instance_model_overrides(persona, instance)

    binding = resolve_persona_profile(persona)
    if binding.readiness == "missing_profile":
        raise _PrewarmRefused(OUTCOME_SKIPPED_PROFILE_UNREADY)

    session_db = default_persona_session_db()
    session_model_config = _session_model_config(session_db, root)
    model_selection = _chat_effective_model_payload(
        persona=persona,
        config=cfg,
        override=_chat_model_override_from_config(session_model_config),
        instance=instance,
    )
    runtime_provider = model_selection.get("effective_provider") or getattr(
        persona, "provider", None
    )
    runtime_model = model_selection.get("effective_model") or getattr(
        persona, "model", None
    ) or ""
    try:
        _assert_provider_health(
            assert_provider_health_for_persona, persona, runtime_provider, runtime_model
        )
    except Exception:
        # A turn on this persona would refuse before it ran; warming an actor it
        # cannot use is work for nobody. Reported, not raised.
        raise _PrewarmRefused(OUTCOME_SKIPPED_PROFILE_UNREADY)

    signature = mission_chat_runtime_signature(
        persona=persona,
        instance=instance,
        config=cfg,
        session_id=root,
        session_model_config=session_model_config,
        model_selection=model_selection,
        # No ``--agents-file``: see the module docstring's "what it will not do".
        # A chat with no workspace selection matches exactly; a workspace-bound
        # one rebuilds on its first turn and costs what it costs today.
        workspace_agents_receipt=None,
        surface_prompt="",
    )
    # The tip and the revision the FIRST turn will pass to ``acquire``. Both are
    # read here, from the same helpers that turn calls; a chat that is written
    # between this read and that turn rebuilds, which is correct — its history
    # moved.
    active_session_id = _persona_chat_native_tip(session_db, root)
    native_revision = _persona_chat_native_revision(session_db, root)

    lane_bundle = chat_lane_bundle(persona, session_id=root)
    workdir = mission_chat_workdir_for_persona(persona, workspace_agents_path=None)
    envelope_scope = terminal_envelope_scope_for_persona(
        persona,
        lane=LANE_MISSION_CHAT,
        session_id=root,
        runtime_root=paths.store_root(),
        permission_mode=lane_bundle.permission_mode,
    )

    request = AgentRunRequest(
        prewarm_only=True,
        profile=binding.hermes_profile,
        provider=runtime_provider,
        model=runtime_model,
        api_mode=getattr(persona, "api_mode", None),
        reasoning_effort=getattr(instance, "reasoning_effort", None),
        terminal_envelope_scope=envelope_scope,
        mcp_admission=lane_bundle.admission,
        enabled_toolsets=list(lane_bundle.enabled_toolsets),
        blocked_tool_names=list(lane_bundle.blocked_tool_names),
        quiet_mode=True,
        skip_context_files=not bool(
            getattr(persona, "include_core_context_files", False)
        ),
        skip_memory=not bool(getattr(persona, "include_profile_memory", False)),
        platform=PERSONA_CHAT_SCRATCH_SOURCE,
        skill_surface="mission_chat",
        skill_root_node_mode=False,
        session_id=active_session_id,
        cache_scope_id=root,
        tool_execution_scope_id=root,
        root_chat_session_id=root,
        persona_chat_runtime_registry=persona_chat_runtime_registry(),
        persona_chat_runtime_signature=signature,
        persona_chat_native_revision=native_revision,
        runtime_root=paths.store_root(),
        workdir=Path(workdir.path) if workdir.grounded else None,
        # Every callback stays None. A prewarm has no operator watching it, no
        # stream to feed and no turn record to decorate; the resident actor's
        # per-turn handles are (re)bound by
        # ``_prepare_resident_persona_chat_agent`` on the first real turn, which
        # is the seam that exists for exactly this.
    )
    runner = ProfileAgentRunner(session_db=session_db)
    return request, runner


def _assert_provider_health(assert_fn: Any, persona: Any, provider: Any, model: Any) -> None:
    """``assert_provider_health_for_persona`` against the EFFECTIVE model tier.

    Mirrors ``mission_chat_reply``: the health check runs on a copy carrying the
    provider/model this run would actually use, never on the roster row's
    declared pair.
    """

    from .models import AgentPersona

    health_persona = AgentPersona(
        **{
            field: getattr(persona, field)
            for field in getattr(persona, "__dataclass_fields__", {})
        }
    )
    health_persona.provider = provider
    health_persona.model = model
    assert_fn(health_persona)


def _instance_for_root(root: str) -> Any:
    """The persona instance whose bound chat root is *root*."""

    from .persona_assignments import PersonaInstanceStore

    try:
        instances = PersonaInstanceStore().list_all()
    except Exception:
        return None
    for instance in instances:
        bound = str(
            getattr(instance, "default_chat_session_id", None)
            or getattr(instance, "session_id", None)
            or ""
        ).strip()
        if bound == root:
            return instance
    return None


# ── the background worker ────────────────────────────────────────────────────
#
# The same single-daemon-worker shape as ``persona_prewarm``, for a stronger
# reason: there the argument was cache thrash, here it is the workdir lock. Two
# workers would be two constructions racing for a lock every real turn also
# needs, which is the contention this module exists to avoid — so the queue is
# not merely serialized by preference, it is serialized by contract.

_queue: "queue.Queue[str]" = queue.Queue()
_lock = threading.Lock()
_pending: set[str] = set()
_worker: threading.Thread | None = None


def _drain() -> None:
    """Warm one chat root at a time, forever, and never die.

    Every failure is contained here. A raise that escaped would kill the thread
    and silently turn every LATER prewarm into a no-op — the failure shape that
    is hardest to notice, since its only symptom is the slow first turn the
    prewarm was supposed to prevent.
    """

    while True:
        root = _queue.get()
        started = time.monotonic()
        try:
            outcome = prewarm_chat_actor(root)
        except Exception:
            outcome = OUTCOME_SKIPPED_CONSTRUCT_FAILED
            logger.warning(
                "chat-actor prewarm raised for %s after %d ms; the next turn on "
                "that chat pays the cold construction it would have avoided",
                root,
                int(max(0.0, time.monotonic() - started) * 1000),
                exc_info=True,
            )
        logger.info(
            CHAT_ACTOR_PREWARM_DONE_RECEIPT,
            root,
            outcome,
            int(max(0.0, time.monotonic() - started) * 1000),
        )
        with _lock:
            _pending.discard(root)
        _queue.task_done()


def _ensure_worker() -> None:
    """Start the single daemon worker on first use. Call under ``_lock``."""

    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = threading.Thread(
        target=_drain, name="persona-chat-actor-prewarm", daemon=True
    )
    _worker.start()


def request_chat_actor_prewarm(root_session_id: str | None) -> str:
    """Queue a chat root for background prewarm. Returns what it did.

    ``registry_off`` — and no thread, no queue entry — whenever
    ``persona_chat_runtime_registry()`` is ``None``. That is the state of every
    CLI one-shot and of any serve whose root config leaves
    ``persona_chat.hot_sessions_enabled`` off, so every hook in the harness is
    inert by default and this module costs an import.

    Idempotent by chat root: a root already queued or in flight is reported as
    ``already_running`` rather than queued twice, so a hook that fires on every
    chat-open gesture cannot grow the queue.
    """

    from .persona_chat_continuity import persona_chat_runtime_registry

    if persona_chat_runtime_registry() is None:
        return OUTCOME_REGISTRY_OFF
    root = str(root_session_id or "").strip()
    if not root:
        return OUTCOME_SKIPPED_NO_CHAT_ROOT
    with _lock:
        if root in _pending:
            return "already_running"
        _pending.add(root)
        _ensure_worker()
        # Inside the lock: the worker discards from ``_pending`` only after it
        # has finished the item, so enqueueing here cannot race a discard into a
        # state where a queued root looks absent.
        _queue.put(root)
    return "started"


# ── the boot pass ────────────────────────────────────────────────────────────


def prewarm_chat_actors_on_boot() -> dict[str, int]:
    """Queue the operator's most recently active chats, up to the registry cap.

    Runs on ``serve``'s existing prewarm thread, AFTER the read-model build and
    the provider warmup (ordering doctrine: ``serve.py``). Third rather than
    first for two reasons — the launcher's canvas is waiting on the build, and
    an agent construction that runs after ``_load_openai_cls`` is far cheaper
    than one that pays the SDK import itself.

    ``max_hot_sessions`` is the cap and it is the registry's own: warming more
    chats than the registry can hold would evict the earliest warms before
    anyone used them. Most-recently-active first, because that is the order an
    operator is likely to reopen them in and the order eviction would preserve.

    Gated on ``hot_sessions_enabled`` and nothing else — see the note in
    :class:`agent_runtime.runtime_config.PersonaChatConfig` for why this pass
    does not get its own key.

    Returns the pass's counts (also emitted as
    :data:`CHAT_ACTOR_PREWARM_PASS_RECEIPT`); it never raises, because a warm
    that did not happen costs latency and never correctness.
    """

    started = time.monotonic()
    counts = {"candidates": 0, "queued": 0, "skipped": 0}
    try:
        from .config import load_root_runtime_config
        from .persona_chat_continuity import persona_chat_runtime_registry

        if persona_chat_runtime_registry() is None:
            return counts
        persona_chat_cfg = load_root_runtime_config().persona_chat
        roots = _boot_candidates(limit=max(1, int(persona_chat_cfg.max_hot_sessions)))
        counts["candidates"] = len(roots)
        for root in roots:
            if request_chat_actor_prewarm(root) == "started":
                counts["queued"] += 1
            else:
                counts["skipped"] += 1
    except Exception:  # pragma: no cover - a boot must never die on a warmup
        logger.debug("chat-actor boot prewarm pass did not complete", exc_info=True)
        return counts
    logger.info(
        CHAT_ACTOR_PREWARM_PASS_RECEIPT,
        counts["candidates"],
        counts["queued"],
        counts["skipped"],
        int(max(0.0, time.monotonic() - started) * 1000),
    )
    return counts


def _boot_candidates(*, limit: int) -> list[str]:
    """Bound chat roots, most-recently-active first, capped at *limit*.

    Only an instance that HAS a bound chat root is a candidate: a placement with
    no ``default_chat_session_id`` has no root to key a resident actor on, and
    minting one here would be this module writing store state — which it does
    not do.

    Recency is ``updated_at``, the field every instance write stamps. Rows that
    carry none sort last rather than being dropped: an unsorted candidate is
    still a real chat, and a missing timestamp must not read as "never used".
    """

    from .persona_assignments import PersonaInstanceStore

    try:
        instances = list(PersonaInstanceStore().list_all())
    except Exception:
        return []
    dated: list[tuple[str, str]] = []
    undated: list[str] = []
    seen: set[str] = set()
    for instance in instances:
        root = str(getattr(instance, "default_chat_session_id", None) or "").strip()
        if not root or root in seen:
            continue
        seen.add(root)
        stamp = str(getattr(instance, "updated_at", "") or "").strip()
        if stamp:
            dated.append((stamp, root))
        else:
            undated.append(root)
    # ISO-8601 stamps sort lexicographically, newest last — hence the reverse.
    # ``sorted`` is stable, so equal stamps keep store order.
    ordered = [root for _, root in sorted(dated, key=lambda row: row[0], reverse=True)]
    return (ordered + undated)[:limit]


__all__ = [
    "CHAT_ACTOR_PREWARM_DONE_RECEIPT",
    "CHAT_ACTOR_PREWARM_PASS_RECEIPT",
    "OUTCOME_ALREADY_RESIDENT",
    "OUTCOME_REGISTRY_OFF",
    "OUTCOME_SKIPPED_CONSTRUCT_FAILED",
    "OUTCOME_SKIPPED_NO_CHAT_ROOT",
    "OUTCOME_SKIPPED_PERSONA_UNRESOLVED",
    "OUTCOME_SKIPPED_PROFILE_UNREADY",
    "OUTCOME_SKIPPED_TURN_ACTIVE",
    "OUTCOME_WARMED",
    "prewarm_chat_actor",
    "prewarm_chat_actors_on_boot",
    "request_chat_actor_prewarm",
]
