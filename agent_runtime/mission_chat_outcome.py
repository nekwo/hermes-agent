"""Mission-chat turn OUTCOME vocabulary — ``execution_state`` / ``error_kind``.

Sibling of ``mission_chat_turns``, which owns the turn LIFECYCLE states
(``pending`` -> ``executing`` -> ...). This module owns the two fields the CLI
puts on the *wire* to describe how a turn ENDED:

* ``execution_state`` — the coarse outcome class a caller switches on.
* ``error_kind``      — the typed reason a non-``completed`` outcome carries.

Why it exists
-------------
Both fields were spelled as bare string literals inside
``hermes_cli/harness_parts/persona_commands.py``: 26 ``"execution_state"``
sites across 7 values, 35 ``"error_kind"`` sites across 16 literal values plus
4 dynamic ones. That file is ``exec``'d into ``harness.py``'s globals rather
than imported, so nothing could import the vocabulary to check it, and 16
read-only Launcher consumers depend on the exact spellings. A typo was a silent
wire break: the Launcher would simply not match, and no test in either repo
would notice.

This is the same finding ``mission_chat_turns`` recorded for the turn states —
"a consumer was free to invent a spelling" — one field over. The house remedy
is the same: ONE table, structural import-time guards that raise (never
``assert``, so ``python -O`` cannot strip the contract), and every consumer
asks the table instead of re-spelling it.

Wire compatibility
------------------
Every member's VALUE is byte-identical to the literal it replaced. ``StrEnum``
members ARE ``str``, so ``json.dumps`` emits the same bytes, ``==`` against a
plain string still holds, and no call site needed a ``.value``. Adding a value
here is a WIRE CHANGE and must be treated as one.

Not owned here
--------------
Three sibling decision modules already type their own ``error_kind`` and hand
it to this lane through a decision object (``relay_policy.RelayDecision``,
``target_policy.TargetDecision``, ``PersonaChatMintError.code``). Re-spelling
their values here would create exactly the second authority this module exists
to retire, so they are declared in ``DELEGATED_ERROR_KIND_SOURCES`` for the
record and stay typed at their source.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ChatErrorKind",
    "DELEGATED_ERROR_KIND_SOURCES",
    "ExecutionState",
    "FAILURE_EXECUTION_STATES",
    "FinalizationWarning",
    "FinalizationWarningKind",
    "MISSION_CHAT_EXIT_FAILURE",
    "MISSION_CHAT_EXIT_OK",
    "MissionChatDeferredFinalization",
    "MissionChatTurnPlan",
    "OK_EXECUTION_STATES",
    "TurnOutcome",
    "classify_turn_failure",
]


# ---------------------------------------------------------------------------
# execution_state
# ---------------------------------------------------------------------------
class ExecutionState(StrEnum):
    """How a mission-chat capability call ended.

    ``budget_exhausted`` is deliberately reachable from BOTH sides of ``ok``:
    a turn that ran out of wall clock and produced nothing is a failure, and a
    turn that checkpointed with a real committed reply is a SUCCESS with a
    truncated scope. The state names the bound that was hit, not the verdict —
    ``ok`` names the verdict.
    """

    #: A refusal that never entered the write lane (retired target pre-flight).
    REFUSED = "refused"
    #: The request itself was inadmissible (bad persona, foreign root, relay).
    REJECTED = "rejected"
    #: Harness-side failure before or around the provider boundary.
    FAILED = "failed"
    #: The provider was reached but its outcome cannot be proven / progressed.
    BLOCKED = "blocked"
    #: A declared budget ended the turn. See the class docstring.
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: The turn ran and settled.
    COMPLETED = "completed"
    # S70 removed ``QUEUED``: its only emitter was the free-floating assignment
    # queue envelope, retired with that lane (a queued row had no consumer
    # since the 2026-07-30 chat-only purge removed ticking).


#: Exactly one of these two buckets holds every state. The partition is the
#: import-time guard's subject: a state added to the enum and to neither set
#: raises at import rather than becoming an unclassified outcome nobody
#: switched on (the exact shape of the 2026-07-26 wall-budget spinner).
OK_EXECUTION_STATES = frozenset(
    {
        ExecutionState.COMPLETED,
    }
)
FAILURE_EXECUTION_STATES = frozenset(
    {
        ExecutionState.REFUSED,
        ExecutionState.REJECTED,
        ExecutionState.FAILED,
        ExecutionState.BLOCKED,
    }
)
#: The dual-verdict state, held out of both buckets on purpose (see above).
#: ``ok`` — not the state — decides the exit code on this one.
DUAL_VERDICT_EXECUTION_STATES = frozenset({ExecutionState.BUDGET_EXHAUSTED})

#: The mission-chat lane's process exit codes. It does NOT participate in
#: ``hermes_cli.harness.ERROR_EXIT_CODES`` (the stage42 *error envelope*
#: taxonomy, whose keys are error CODES like ``not_found`` / ``invalid_payload``
#: and which this lane never consults): every mission-chat command returns 0 on
#: an ``ok`` envelope and 2 on a refusal, at every one of its ~25 returns.
MISSION_CHAT_EXIT_OK = 0
MISSION_CHAT_EXIT_FAILURE = 2


# ---------------------------------------------------------------------------
# error_kind
# ---------------------------------------------------------------------------
class ChatErrorKind(StrEnum):
    """The typed reason a mission-chat envelope is not ``ok``.

    Grouped by FAMILY below; every member belongs to exactly one family and the
    import-time guard proves it, so a new kind cannot be added without deciding
    what it is."""

    # -- request admission ---------------------------------------------------
    INVALID_REQUEST = "invalid_request"
    UNSUPPORTED_PERSONA = "unsupported_persona"
    PERSONA_INSTANCE_NOT_FOUND = "persona_instance_not_found"
    PERSONA_INSTANCE_MISMATCH = "persona_instance_mismatch"
    RETIRED_PERSONA_INSTANCE = "retired_persona_instance"
    INVALID_CHAT_MODEL_OVERRIDE = "invalid_chat_model_override"

    # -- chat-root ownership / concurrency -----------------------------------
    UNKNOWN_CHAT_SESSION = "unknown_chat_session"
    FOREIGN_CHAT_SESSION = "foreign_chat_session"
    CHAT_BUSY = "chat_busy"

    # -- persistence ---------------------------------------------------------
    CHAT_SESSION_DB_UNAVAILABLE = "chat_session_db_unavailable"
    CHAT_SESSION_PERSIST_FAILED = "chat_session_persist_failed"
    CHAT_MODEL_OVERRIDE_PERSIST_FAILED = "chat_model_override_persist_failed"
    CHAT_PROJECTION_INCOMPLETE = "chat_projection_incomplete"
    # S70 removed CHAT_TRANSCRIPT_PERSIST_FAILED and POST_TURN_PERSIST_FAILED:
    # both were emitted ONLY by the retired free-floating queue/runner (the
    # mission-chat lane has its own persist kinds), and the "every owned member
    # has a producer" gate is exactly the rule that retires them with it. No
    # launcher reader existed for either string.

    # -- turn lifecycle ------------------------------------------------------
    CHAT_TURN_BUDGET_EXHAUSTED = "chat_turn_budget_exhausted"
    CHAT_TURN_OUTCOME_UNKNOWN = "chat_turn_outcome_unknown"
    CHAT_TURN_NOT_SUBMITTED = "chat_turn_not_submitted"
    CHAT_TURN_RESOLUTION_MISMATCH = "chat_turn_resolution_mismatch"
    #: This exact ``client_message_id`` is the turn RUNNING on the root right
    #: now. Deliberately NOT ``chat_busy``: busy says "someone else holds the
    #: root, try again", which a caller is entitled to treat as "your message
    #: never landed". This says the opposite — the message DID land, it is being
    #: answered, and the only correct move is to re-present the SAME id later to
    #: collect the reply. The 2026-08-24 incident is the whole reason it exists:
    #: a launcher inactivity-fallback re-presented its own in-flight id, got
    #: ``chat_busy``, and painted a delivered turn as a rejection.
    CHAT_TURN_DUPLICATE_IN_FLIGHT = "chat_turn_duplicate_in_flight"


ADMISSION_ERROR_KINDS = frozenset(
    {
        ChatErrorKind.INVALID_REQUEST,
        ChatErrorKind.UNSUPPORTED_PERSONA,
        ChatErrorKind.PERSONA_INSTANCE_NOT_FOUND,
        ChatErrorKind.PERSONA_INSTANCE_MISMATCH,
        ChatErrorKind.RETIRED_PERSONA_INSTANCE,
        ChatErrorKind.INVALID_CHAT_MODEL_OVERRIDE,
    }
)
CHAT_ROOT_ERROR_KINDS = frozenset(
    {
        ChatErrorKind.UNKNOWN_CHAT_SESSION,
        ChatErrorKind.FOREIGN_CHAT_SESSION,
        ChatErrorKind.CHAT_BUSY,
    }
)
PERSISTENCE_ERROR_KINDS = frozenset(
    {
        ChatErrorKind.CHAT_SESSION_DB_UNAVAILABLE,
        ChatErrorKind.CHAT_SESSION_PERSIST_FAILED,
        ChatErrorKind.CHAT_MODEL_OVERRIDE_PERSIST_FAILED,
        ChatErrorKind.CHAT_PROJECTION_INCOMPLETE,
    }
)
TURN_LIFECYCLE_ERROR_KINDS = frozenset(
    {
        ChatErrorKind.CHAT_TURN_BUDGET_EXHAUSTED,
        ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN,
        ChatErrorKind.CHAT_TURN_NOT_SUBMITTED,
        ChatErrorKind.CHAT_TURN_RESOLUTION_MISMATCH,
        ChatErrorKind.CHAT_TURN_DUPLICATE_IN_FLIGHT,
    }
)

_ERROR_KIND_FAMILIES = {
    "ADMISSION_ERROR_KINDS": ADMISSION_ERROR_KINDS,
    "CHAT_ROOT_ERROR_KINDS": CHAT_ROOT_ERROR_KINDS,
    "PERSISTENCE_ERROR_KINDS": PERSISTENCE_ERROR_KINDS,
    "TURN_LIFECYCLE_ERROR_KINDS": TURN_LIFECYCLE_ERROR_KINDS,
}

#: ``error_kind`` values this lane FORWARDS rather than spells. Each is already
#: a typed constant in the module that decided the refusal; recording them here
#: keeps the wire universe documented in one place without minting a second
#: authority for values another module owns.
DELEGATED_ERROR_KIND_SOURCES = {
    "agent_runtime.relay_policy": (
        "relay_depth_limit",
        "relay_cycle",
        "relay_budget_exhausted",
    ),
    "agent_runtime.target_policy": ("ambiguous_target",),
    "agent_runtime.persona_chat_mint": ("PersonaChatMintError.code",),
    # The dispatch DELIVERY QUEUE's own refusals, spoken only by
    # ``harness mission-chat dispatch redeliver``. They describe a queue row's
    # delivery state, not how a chat TURN ended, so owning them here would put
    # a second lane's vocabulary in this table — the exact duplication this
    # module exists to prevent. ``dispatch_store.REARM_ERROR_KINDS`` is the
    # authority; the verb reads it.
    "agent_runtime.dispatch_store": (
        "dispatch_not_found",
        "dispatch_already_delivered",
        "dispatch_not_dropped",
        "dispatch_store_unavailable",
    ),
}


# ---------------------------------------------------------------------------
# the failure classifier
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """One decision: what a failed turn is called, and how the process exits."""

    execution_state: ExecutionState
    error_kind: ChatErrorKind
    exit_code: int = MISSION_CHAT_EXIT_FAILURE


#: The whole post-provider failure decision, as a table rather than a nested
#: conditional expression. Keyed by ``(wall_budget_exceeded, provider_submitted)``.
#:
#: The 2026-07-26 incident is the middle row: a wall-budget death was reported
#: as ``chat_turn_outcome_unknown`` ("I cannot prove what the provider did"),
#: which froze BOTH ends of a relay and cost an operator two manual
#: ``turn-resolve --action abandon`` calls plus a full re-brief. A budget death
#: is the opposite of ambiguous — the harness knows exactly why it stopped.
_FAILURE_TABLE: dict[tuple[bool, bool], TurnOutcome] = {
    (True, True): TurnOutcome(
        ExecutionState.BUDGET_EXHAUSTED, ChatErrorKind.CHAT_TURN_BUDGET_EXHAUSTED
    ),
    (False, True): TurnOutcome(
        ExecutionState.BLOCKED, ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN
    ),
    (False, False): TurnOutcome(
        ExecutionState.FAILED, ChatErrorKind.CHAT_TURN_NOT_SUBMITTED
    ),
}
# A wall-budget trip is only knowable AFTER the provider boundary — the budget
# is what the runner enforces, so nothing can trip it before submission. The
# row is unreachable by construction and stated so rather than left implicit.
_FAILURE_TABLE[(True, False)] = _FAILURE_TABLE[(False, False)]


def wall_budget_exceeded(exc: BaseException, *, provider_submitted: bool) -> bool:
    """Did ``exc`` end this turn on its declared WALL budget?

    Requires all three: the provider boundary was crossed, the exception is the
    runner's budget signal, and it carries the typed ``wall_budget`` projection
    (a non-wall trip — api calls, tokens, read/search loops — leaves it unset
    and settles as ``outcome_unknown`` instead).
    """

    if not provider_submitted:
        return False
    from .profile_runner import RunBudgetExceeded

    return bool(isinstance(exc, RunBudgetExceeded) and getattr(exc, "wall_budget", None))


def classify_turn_failure(
    exc: BaseException, *, provider_submitted: bool
) -> TurnOutcome:
    """Name a failed mission-chat turn. Pure: reads ``exc``, writes nothing."""

    return _FAILURE_TABLE[
        (wall_budget_exceeded(exc, provider_submitted=provider_submitted), provider_submitted)
    ]


# ---------------------------------------------------------------------------
# the plan/commit boundary
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MissionChatTurnPlan:
    """Everything a mission-chat turn RESOLVED before it started writing.

    ``_cmd_mission_chat_message`` used to be one 1,600-line body that re-entered
    ITSELF once to take the chat-root lease
    (``return _cmd_mission_chat_message(args)`` inside ``with lease:``). That
    re-entry is why five durable writes ran TWICE per turn — the instance
    derive, the mint, ``open_chat``, the session ensure, and the model-override
    persist all happened before the lease and then again inside it — and why the
    turn's phase state had to be smuggled across the boundary on ``args._*``
    attributes, because a second pass would otherwise rediscover "the caller
    named a session" and report every freshly minted dispatch thread as a plain
    continuation.

    This object is that boundary made explicit: the plan phase resolves it once,
    and the commit phase runs ONCE, under the lease, from these fields. It is
    frozen because a plan that could be mutated after the lease was taken would
    reintroduce exactly the ambiguity the ``args._*`` attributes encoded.

    Deliberately NOT yet true of the plan phase, and stated rather than implied:
    it is not pure. Two durable writes still precede it — the instance
    derive-from-workers that target resolution reads, and the mint that MAKES
    the ``session_id`` this lease is keyed on (there is nothing to lock before a
    thread exists). Retiring those two, and replacing the remaining
    ``getattr(args, ...)`` reads with a request object, is the rest of the
    proposal.

    Fields are typed ``object`` on purpose: they carry CLI argparse namespaces
    and store handles from ``hermes_cli``, which ``agent_runtime`` must not
    import (the dependency runs the other way).
    """

    args: object
    cfg: object
    session_db: object
    instance_store: object
    persona: object
    normalized_persona: str
    persona_instance_id: object
    display_name: str
    session_id: str
    client_message_id: str
    session_established: object
    clarify_binding: object
    stated_session_id: object
    requested_by_session: str
    turn_relay_chain: object
    relay_chain_in: object
    relay_deadline: object
    #: The turn's monotonic timeline (``mission_chat_phases.TurnPhaseMarks``),
    #: anchored at command-handler ENTRY — before the plan phase resolves
    #: anything — because "how long did admission take?" must include the
    #: resolution work, not start after it. That is the whole of gap G1.
    #:
    #: It is a MUTABLE object on a frozen plan, and that is deliberate rather
    #: than an exception being smuggled: the plan is frozen so no phase
    #: DECISION can change after the lease is taken, and a clock records
    #: nothing the turn reads back. Nothing downstream may branch on a mark.
    phases: object


@dataclass(slots=True)
class MissionChatDeferredFinalization:
    """Best-effort turn bookkeeping that runs AFTER the chat-root lease releases.

    The lease exists to serialise WRITES to one chat root. It was, however, held
    for the entire command — including a tail that writes nothing to that root.
    Live incident 2026-08-09: the reply was durable, mirrored, projected and
    emitted at 06:58:02, and the lease did not release until 06:58:48, because
    the session auto-title in between resolved the auxiliary-provider chain
    (marking two providers unhealthy), lazily pip-installed
    ``provider.anthropic``, and then retried a 401 on a revoked OAuth token. The
    operator's next message landed in that window and was refused ``chat_busy``
    — 46 seconds after the answer he was replying to was already on his screen.

    Nothing in that tail writes turn or transcript state. The rule this object
    encodes: **once the terminal frame is emitted, what is left is decoration,
    and decoration does not get to hold the root.** The commit phase PACKAGES it
    here; ``_cmd_mission_chat_message`` RUNS it once the ``with`` block exits.

    Why a mutable holder rather than a wider return type:
    ``_mission_chat_commit_turn`` returns an exit code from ~15 sites, and
    widening that to a tuple would make any missed site a ``TypeError`` on a LIVE
    turn. Why not a field on ``MissionChatTurnPlan``: that object is frozen on
    purpose (see its docstring) — a plan mutable after the lease was taken is
    exactly the ambiguity the plan/commit split retired. This is a separate,
    deliberately mutable carrier for the one value that flows the other way.

    Failure semantics, stated because the move CHANGES them: a thunk that raises
    can no longer reach the commit phase's crash-tail guard, so it can neither
    corrupt the one-JSON-object stdout contract nor flip the exit code — it is
    swallowed by ``run_once`` instead. In exchange, a deferred failure now
    happens strictly after the turn's exit code is decided, so it can never mark
    the turn. That is the intended direction: the turn was complete, durable and
    reported before the deferred work was ever attempted.
    """

    #: Zero-arg thunk packaged under the lease, invoked after it releases.
    thunk: object | None = None

    def defer(self, thunk: object) -> None:
        """Package the post-lease work. Refuses a second thunk rather than
        silently dropping one — two deferrals would mean the tail grew a second
        author, which is the shape this holder exists to prevent."""

        if self.thunk is not None:
            raise ValueError("mission-chat deferred finalization is already set")
        self.thunk = thunk

    def run_once(self) -> bool:
        """Run the deferred thunk exactly once. Never raises; True if it ran clean."""

        thunk, self.thunk = self.thunk, None
        if thunk is None:
            return False
        try:
            thunk()
            return True
        except Exception:
            return False


class FinalizationWarningKind(StrEnum):
    """Why a turn's POST-REPLY bookkeeping did not complete cleanly.

    The reply is durable by the time any of these can happen, so none of them
    fails the turn — but none of them may be silent either, which is what they
    were: the instance-state commit that returns the agent to ``idle`` and
    repoints its default chat thread sat inside a bare ``except Exception:
    pass``, in three places. A cockpit showing an agent stuck ``busy`` after a
    completed turn had no record anywhere of why.
    """

    #: The instance row could not be returned to idle / repointed at the thread.
    INSTANCE_STATE_COMMIT_FAILED = "instance_state_commit_failed"
    #: A turn-journal transition returned a non-``persisted`` outcome.
    TURN_RECORD_NOT_PERSISTED = "turn_record_not_persisted"


@dataclass(frozen=True, slots=True)
class FinalizationWarning:
    """One typed, wire-shaped account of a non-clean finalization step."""

    kind: FinalizationWarningKind
    detail: str
    #: Which write/step it happened on, when there is more than one of a kind.
    step: str | None = None

    def as_dict(self) -> dict[str, str]:
        row = {"kind": str(self.kind), "detail": self.detail}
        if self.step:
            row["step"] = self.step
        return row


# ---------------------------------------------------------------------------
# import-time contract guards
# ---------------------------------------------------------------------------
#
# Raised, not asserted, so ``python -O`` cannot strip the contract — the same
# convention as ``mission_chat_turns._guard_turn_state_vocabulary``. Each guard
# encodes a way this vocabulary could go wrong silently: a state nobody
# classified as success-or-failure, an error kind belonging to no family (so
# nobody decided what it IS), or a classifier row producing a value outside the
# enums it claims to speak.
def _guard_turn_outcome_vocabulary() -> None:  # pragma: no cover - import contract
    states = set(ExecutionState)
    buckets = {
        "OK_EXECUTION_STATES": OK_EXECUTION_STATES,
        "FAILURE_EXECUTION_STATES": FAILURE_EXECUTION_STATES,
        "DUAL_VERDICT_EXECUTION_STATES": DUAL_VERDICT_EXECUTION_STATES,
    }
    names = sorted(buckets)
    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            overlap = sorted(buckets[left] & buckets[right])
            if overlap:
                raise RuntimeError(
                    f"execution state(s) in both {left} and {right}: {overlap}"
                )
    unclassified = sorted(states - set().union(*buckets.values()))
    if unclassified:
        raise RuntimeError(
            f"execution state(s) belong to no verdict bucket: {unclassified}"
        )

    kinds = set(ChatErrorKind)
    family_names = sorted(_ERROR_KIND_FAMILIES)
    for index, left in enumerate(family_names):
        for right in family_names[index + 1 :]:
            overlap = sorted(_ERROR_KIND_FAMILIES[left] & _ERROR_KIND_FAMILIES[right])
            if overlap:
                raise RuntimeError(
                    f"error kind(s) in both {left} and {right}: {overlap}"
                )
    unfamilied = sorted(kinds - set().union(*_ERROR_KIND_FAMILIES.values()))
    if unfamilied:
        raise RuntimeError(f"error kind(s) belong to no family: {unfamilied}")

    # A delegated value must NOT also be spelled here — that is the second
    # authority this module exists to prevent.
    delegated = {
        value
        for values in DELEGATED_ERROR_KIND_SOURCES.values()
        for value in values
    }
    collisions = sorted(delegated & {str(kind) for kind in kinds})
    if collisions:
        raise RuntimeError(
            "error kind(s) are both owned here and delegated to a sibling "
            f"module: {collisions}"
        )

    # The classifier speaks only this vocabulary, and covers its whole input
    # domain (both booleans, both ways).
    domain = {(left, right) for left in (True, False) for right in (True, False)}
    missing = sorted(domain - set(_FAILURE_TABLE))
    if missing:
        raise RuntimeError(f"_FAILURE_TABLE does not cover {missing}")
    for key, outcome in _FAILURE_TABLE.items():
        if outcome.execution_state not in states:
            raise RuntimeError(
                f"_FAILURE_TABLE[{key}] names a non-state {outcome.execution_state!r}"
            )
        if outcome.error_kind not in kinds:
            raise RuntimeError(
                f"_FAILURE_TABLE[{key}] names a non-kind {outcome.error_kind!r}"
            )
        if outcome.execution_state in OK_EXECUTION_STATES:
            raise RuntimeError(
                f"_FAILURE_TABLE[{key}] classifies a FAILURE as the success "
                f"state {outcome.execution_state!r}"
            )


_guard_turn_outcome_vocabulary()
