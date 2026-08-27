"""The ONE agent-create sequence, and the policy layer above its stores.

Two callers reach the same durable chokepoints — ``PersonaInstanceStore.
add_instance`` and ``OfficeStore.upsert_actor`` — and would silently disagree
about everything that sits ABOVE them:

* the argv lane, ``harness persona instance open-chat --add-instance``
  (``hermes_cli/harness_parts/persona_commands.py``), and
* the method lane, ``runtime.agent.create`` (``serve_rpc.py``).

The store methods are callable and unwelded (verified 2026-08-16), so nothing
had to be extracted out of ``persona_assignments.py``. What DID have to move is
the layer above them, because a handler calling the store directly drops it
without a word — the placement-id validation, the token/text normalisation, and
above all the **honest default display name** rule.

Why the naming rule is the load-bearing one
-------------------------------------------
An omitted name must fall back to the persona's OWN configured
``display_name`` ("QA Agent"), never to the title-cased persona id ("Qa") the
store template mints when it is handed nothing. The launcher's conversational
fold keys on persona+display_name, so a lane that minted "Qa" would fold a new
placement onto a channel it does not belong to. This module is the ONE copy of
that rule; :func:`honest_default_display_name` is what both lanes call.

Where this deviates from the plan, out loud
-------------------------------------------
``archive/2026-08-22-pre-consolidation/AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`` AC-0 says the extraction goes
"UPWARD, not downward" — into the CLI's ``persona_commands.py``. That direction
is not available: ``persona_commands.py`` is not an importable module. It is
``exec``'d into ``hermes_cli/harness.py``'s globals
(``harness.py:3667-3674``), and its functions close over names that live in
those globals and nowhere else — importing it and calling ``_persona_by_id``
raises ``NameError``. So the shared layer has to land somewhere BOTH lanes can
import, which is here, and the CLI calls down into it. The plan's requirement
("the CLI and the RPC handler share one copy", "do not invent a second rule")
is met; only its stated direction is not.

UC-H1: the ORCHESTRATION moved here too
---------------------------------------
AC-1 shared the *policy* (naming, normalisation, payload shape) but left the
sequence — reserve → mint → place → compensate/resume — welded inline into
``serve_rpc._runtime_agent_create``, and therefore welded to JSON-RPC
``rid``/``ok``/``err`` envelopes. That made the atomic create reachable from
exactly one door: a live ``harness serve``. :func:`perform_agent_create` is
that sequence with the envelope peeled off; ``serve_rpc`` is now a translation
shim over it, ``harness agent create`` (UC-H3) calls it directly, and any
future MCP tool wraps whichever of the two it prefers. One sequence, zero
copies — a lane switch cannot become a behaviour change.

The refusal codes are still the JSON-RPC vocabulary (:data:`ERR_CONFLICT` and
friends) because the RPC lane is the fielded consumer and its ``data.reason``
strings are decoded by the launcher (``mission_agent_create_rpc.dart``). Other
lanes map that vocabulary to their own; they do not get to re-spell it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .persona_assignments import (
    _display_name_for_template,
    _normalize_instance_source_persona,
    safe_assignment_text,
    safe_assignment_token,
)

#: The store's own first default folder, reused rather than re-spelled.
DEFAULT_AGENT_FOLDER = "Agents"

#: Same bound the chat-mint ledger enforces (``persona_chat_mints._validated_key``).
MAX_IDEMPOTENCY_KEY_LENGTH = 240


class AgentCreateInvalid(ValueError):
    """A create request this lane refuses before touching any store.

    ``reason`` is the machine-readable branch point a client switches on; the
    message is prose for an operator and is free to change.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class AgentCreateRequest:
    """A normalised create. Every field is already store-safe."""

    persona_id: str
    workspace_id: str
    position: tuple[float, float]
    idempotency_key: str
    placement_id: str
    display_name: str | None
    default_display_name: str
    realm_id: str | None
    folder: str
    correlation_id: str | None

    @property
    def persona_instance_id(self) -> str:
        from .persona_assignments import persona_instance_id_for_placement

        return persona_instance_id_for_placement(self.placement_id)


def honest_default_display_name(persona_id: str, persona: Any | None = None) -> str:
    """The name a create falls back to when the client sends none.

    The persona's configured ``display_name`` first ("QA Agent"), and only then
    the title-cased id. NEVER the store template's own fallback, which is the
    bare title-cased persona id and reads to an operator as a different agent.

    ``persona`` is optional so the CLI can pass the persona object it already
    resolved through its own richer ``_persona_by_id`` (preserving that lane's
    behaviour byte-for-byte) while the RPC lane, which has no such object, is
    answered by the importable lookup below. The lookup is a SUBSET of the CLI
    resolver, never a superset, so the two can only ever agree.
    """

    configured = (
        safe_assignment_text(getattr(persona, "display_name", None), limit=120)
        if persona is not None
        else ""
    )
    if configured:
        return configured
    resolved = resolve_persona(persona_id)
    configured = safe_assignment_text(
        getattr(resolved, "display_name", None), limit=120
    )
    if configured:
        return configured
    return _display_name_for_template(str(persona_id or ""))


class PersonaRosterUnavailable(RuntimeError):
    """The agent roster could not be READ. A runtime fault, not a bad id.

    The distinction exists because UC-H2 made the roster load-bearing. Before
    it, a config this process could not read degraded quietly to a title-cased
    display name and nothing else noticed. After it, the same fault would have
    turned EVERY bare-id create on EVERY lane into ``persona_not_found`` — an
    error that names the operator's id as the problem when the id was fine and
    the runtime is the problem. An operator reading that would go looking for a
    typo that does not exist.

    So the two are separated at the source. "The roster says this persona does
    not exist" is a refusal the caller can act on; "the roster could not be
    read" is a fault the caller cannot act on, and it gets its own reason.
    Neither degrades to the other, and neither degrades to silence — which is
    the standing rule that a missing answer is a LOUD error, never a quiet
    guess.
    """


def persona_roster() -> list[Any]:
    """Every persisted persona, or raise :class:`PersonaRosterUnavailable`.

    The STRICT half of the lookup. An EMPTY list is a real answer — a runtime
    that genuinely has no personas — and is distinct from a raise. That
    distinction matters more than it looks: UC-0 measured a hermetic root's
    default roster as empty, so "no personas" is a reachable state, not an
    impossible one, and a create against it should be refused rather than
    excused.
    """

    try:
        from .config import ensure_persisted_personas, load_agent_runtime_config

        return list(ensure_persisted_personas(load_agent_runtime_config()))
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed fault below
        raise PersonaRosterUnavailable(str(exc)) from exc


def resolve_persona(persona_id: str) -> Any | None:
    """The persisted persona for *persona_id*, or ``None``.

    Deliberately narrow: exact id, then the safe-token spelling. It does NOT
    synthesise a ``profile:`` persona the way the CLI's ``_persona_by_id`` does,
    because the only field this module reads off the result is
    ``display_name`` and a synthesised profile persona's is derived from the
    profile token — which is exactly what the fallback below computes anyway.
    Never raises: a config this process cannot load is "no persona", and the
    caller's fallback covers it.
    """

    try:
        personas = persona_roster()
    except PersonaRosterUnavailable:
        return None
    raw = str(persona_id or "").strip()
    token = safe_assignment_token(raw)
    for persona in personas:
        if getattr(persona, "id", None) == raw:
            return persona
    for persona in personas:
        if getattr(persona, "id", None) == token:
            return persona
    return None


#: The machine-readable branch point for "that persona names nothing".
PERSONA_NOT_FOUND_REASON = "persona_not_found"

#: The machine-readable branch point for "the roster could not be read at all".
#:
#: A SEPARATE reason rather than a flavour of the one above, because the two
#: need opposite responses: ``persona_not_found`` means the caller should send a
#: different id, and this one means the caller should send the SAME id once the
#: runtime is healthy. Collapsing them would send an operator hunting a typo
#: that does not exist — and would send a client's retry logic the wrong way.
PERSONA_ROSTER_UNAVAILABLE_REASON = "persona_roster_unavailable"


def persona_roster_unavailable_message(cause: Any = None) -> str:
    """The ONE spelling of the roster-fault refusal, shared by every lane.

    Names the runtime as the subject, never the id — the id was fine.
    """

    detail = str(cause or "").strip()
    return (
        "the agent roster could not be read, so this create was refused "
        "before touching any store; the persona id is not the problem"
        + (f" ({detail})" if detail else "")
    )


def persona_not_found_message(persona_id: Any) -> str:
    """The ONE spelling of the refusal, shared by every lane (UC-H2/UC-H4).

    It names the cure, because the operator who hits this has usually typed a
    plausible-looking id (``qa_agent`` for ``qa``) and has no way to know the
    roster from the error alone.
    """

    return (
        f"unknown persona: {str(persona_id or '')!r} is not in the agent roster; "
        "run `harness agent list` to see the personas that exist, or add it "
        "before creating an instance for it"
    )


def _persona_is_unknown(persona_id: str, persona: Any | None = None) -> bool:
    """Does this create name a persona that does not exist?

    Decision **D-U1**, and it is load-bearing: a ``profile:``-prefixed id is
    deliberately NOT checked. The launcher's template/preset browser sends
    ``profile:<token>`` ids for profiles that own no persona row at all, and
    the CLI's ``_persona_by_id`` SYNTHESISES a persona for them on purpose
    (``profile_persona_resolution`` returns ``None`` matches without raising).
    Making validation uniform would break that lane silently, since nothing
    else covers it — so the carve-out has its own witness test.

    ``persona`` is the caller's already-resolved persona object. The CLI
    resolver is a strict SUPERSET of :func:`resolve_persona` (it also handles
    profile synthesis and instance-id spellings), so "the caller found one"
    settles the question without a second, narrower lookup contradicting it.

    Asks :func:`persona_roster` rather than :func:`resolve_persona`, and the
    difference is the whole point. ``resolve_persona`` answers ``None`` for BOTH
    "no such persona" and "this process could not read the config" — correct for
    its original job (a display-name fallback, where either miss is harmless)
    and WRONG here, where the answer decides whether a durable write is refused.
    Routed through the forgiving lookup, an unreadable config would refuse every
    bare-id create on every lane with a message blaming the operator's id.

    So this raises :class:`PersonaRosterUnavailable` instead of returning
    ``True``. A fault the caller cannot act on must not wear the costume of a
    refusal the caller can.
    """

    if persona is not None:
        return False
    if str(persona_id or "").lower().startswith("profile:"):
        return False
    raw = str(persona_id or "").strip()
    token = safe_assignment_token(raw)
    personas = persona_roster()  # raises PersonaRosterUnavailable
    return not any(
        getattr(persona_row, "id", None) in (raw, token) for persona_row in personas
    )


def require_known_persona(
    persona_id: str, persona: Any | None = None
) -> dict[str, Any] | None:
    """The argv lanes' shape of the same refusal, or ``None`` to proceed.

    The unified lane raises :class:`AgentCreateInvalid` from
    :func:`normalize_agent_create`; the legacy ``persona instance`` verbs print
    an ``{"ok": false, …}`` payload and exit 2. Same predicate, same message,
    same D-U1 carve-out — one spelling, two envelopes.
    """

    try:
        unknown = _persona_is_unknown(persona_id, persona)
    except PersonaRosterUnavailable as exc:
        return {
            "ok": False,
            "error": persona_roster_unavailable_message(exc),
            "reason": PERSONA_ROSTER_UNAVAILABLE_REASON,
            "persona_id": persona_id,
            "next_expected": (
                "run `harness doctor` to find why the agent roster cannot be "
                "read, then re-run — the persona id is not the problem"
            ),
        }
    if not unknown:
        return None
    return {
        "ok": False,
        "error": persona_not_found_message(persona_id),
        "reason": PERSONA_NOT_FOUND_REASON,
        "persona_id": persona_id,
        "next_expected": (
            "run `harness agent list` to see the personas that exist, then "
            "re-run with one of them"
        ),
    }


def mint_placement_id(persona_id: str) -> str:
    """A server-side placement id, shaped like the launcher's own.

    The launcher normally mints this itself (``missionMintDeliberatePlacementId``)
    and sends it, so this is the fallback for a caller that only knows "put a
    <persona> at (x, y)". The hex tail is what keeps two rapid creates of one
    persona distinct — the id is IDENTITY, and the cosmetic "(2)" suffix is a
    separate concern that stays with the client (decision D-A1).
    """

    token = safe_assignment_token(persona_id) or "persona"
    return f"{token}_{uuid.uuid4().hex[:8]}"


def _position(value: Any) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        raise AgentCreateInvalid(
            "position_invalid", "invalid params: position must be [x, y]"
        )
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError) as exc:
        raise AgentCreateInvalid(
            "position_invalid", "invalid params: position must be numeric"
        ) from exc
    # ``bool`` is an ``int``: ``[True, False]`` would otherwise place an agent
    # at (1.0, 0.0) and read as a deliberate placement forever.
    if isinstance(value[0], bool) or isinstance(value[1], bool):
        raise AgentCreateInvalid(
            "position_invalid", "invalid params: position must be numeric"
        )
    if x != x or y != y or abs(x) == float("inf") or abs(y) == float("inf"):
        raise AgentCreateInvalid(
            "position_invalid", "invalid params: position must be finite"
        )
    return (x, y)


def normalize_agent_create(
    params: dict[str, Any], *, persona: Any | None = None
) -> AgentCreateRequest:
    """Validate + normalise one create request. Raises :class:`AgentCreateInvalid`.

    Runs BEFORE any store is touched, so a refusal here provably wrote nothing.
    That property is why the roster check (UC-H2) belongs here and not in the
    store: until this returns, the create has left no roster row, no chat root,
    no placement and no reservation receipt to clean up.
    """

    persona_raw = params.get("persona_id")
    if not isinstance(persona_raw, str) or not persona_raw.strip():
        raise AgentCreateInvalid(
            "persona_id_required",
            "invalid params: persona_id must be a non-empty string",
        )
    # ``_normalize_instance_source_persona`` COLLAPSES an unusable id to the
    # literal token ``persona`` rather than refusing, which is right for the
    # send path (it must never drop a turn) and wrong here: it would mint a
    # durable roster row and a placement for an agent class nobody asked for.
    # So the collapse is detected before it happens, on the same tokenizer.
    if not safe_assignment_token(
        persona_raw.split(":", 1)[1]
        if persona_raw.strip().lower().startswith("profile:")
        else persona_raw
    ):
        raise AgentCreateInvalid(
            "persona_id_required",
            "invalid params: persona_id must be a non-empty string",
        )
    persona_id = _normalize_instance_source_persona(persona_raw)
    # UC-H2. Syntax first, roster second, and in that order deliberately: an
    # unusable id is a client bug with its own reason string, and re-labelling
    # it ``persona_not_found`` would send the launcher's decoder down the wrong
    # branch. Everything above this line is still pure string work, so this is
    # the FIRST question asked of the world — and it is asked here, in the one
    # function that provably runs before any store is touched, rather than in
    # the store (``add_instance`` is also the restore/rebind chokepoint, and
    # refusing there would need an audit of every historical row's persona id).
    try:
        persona_unknown = _persona_is_unknown(persona_id, persona)
    except PersonaRosterUnavailable as exc:
        # A fault, not a bad id — and it keeps its own reason all the way to the
        # client so a decoder can tell "fix your id" from "fix your runtime".
        raise AgentCreateInvalid(
            PERSONA_ROSTER_UNAVAILABLE_REASON,
            persona_roster_unavailable_message(exc),
        ) from exc
    if persona_unknown:
        raise AgentCreateInvalid(
            PERSONA_NOT_FOUND_REASON, persona_not_found_message(persona_id)
        )

    workspace_raw = params.get("workspace_id")
    workspace_id = (
        workspace_raw.strip() if isinstance(workspace_raw, str) else ""
    )
    if not workspace_id:
        # The same reason string the office legs spend, on purpose: one client
        # branch covers "the launcher forgot the workspace" on every method.
        raise AgentCreateInvalid(
            "workspace_id_required",
            "invalid params: workspace_id must be a non-empty string",
        )

    position = _position(params.get("position"))

    key_raw = params.get("idempotency_key")
    idempotency_key = key_raw.strip() if isinstance(key_raw, str) else ""
    if not idempotency_key:
        raise AgentCreateInvalid(
            "idempotency_key_required",
            "invalid params: idempotency_key must be a non-empty string",
        )
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise AgentCreateInvalid(
            "idempotency_key_invalid",
            "invalid params: idempotency_key must be "
            f"{MAX_IDEMPOTENCY_KEY_LENGTH} characters or fewer",
        )

    raw_placement = params.get("placement_id")
    if raw_placement is None:
        placement_id = mint_placement_id(persona_id)
    else:
        placement_id = safe_assignment_token(raw_placement)
        if not placement_id:
            # The CLI's own refusal, kept rather than softened to a mint: a
            # client that SENT a placement id is predicting an actor key from
            # it, and quietly substituting another one strands that prediction.
            raise AgentCreateInvalid(
                "placement_id_invalid",
                "invalid params: placement_id must be a non-empty token when sent",
            )

    display_name = safe_assignment_text(params.get("display_name"), limit=120) or None

    realm_raw = params.get("realm_id")
    realm_id = safe_assignment_token(realm_raw) or None if realm_raw is not None else None

    folder = safe_assignment_text(params.get("folder"), limit=80) or DEFAULT_AGENT_FOLDER

    correlation_raw = params.get("correlation_id")
    correlation_id = (
        safe_assignment_text(correlation_raw, limit=200) or None
        if correlation_raw is not None
        else None
    )

    return AgentCreateRequest(
        persona_id=persona_id,
        workspace_id=workspace_id,
        position=position,
        idempotency_key=idempotency_key,
        placement_id=placement_id,
        display_name=display_name,
        default_display_name=honest_default_display_name(persona_id, persona),
        realm_id=realm_id,
        folder=folder,
        correlation_id=correlation_id,
    )


def placement_actor_payload(
    request: AgentCreateRequest, *, display_name: str
) -> dict[str, Any]:
    """The actor payload for a freshly minted placement.

    Deliberately the SAME shape ``harness office actor-upsert`` takes on
    ``--actor-json`` and the launcher's ``officeActorPayloadsFromLayout``
    produces — one schema across three writers, so a lane switch can never read
    as an edit. It is instance-keyed by construction (``persona_instance_id``
    is the id the mint just returned), which is what makes the class-key
    collision the office fence exists to refuse unreachable from this method.
    """

    x, y = request.position
    return {
        "persona_id": request.persona_id,
        "persona_instance_id": request.persona_instance_id,
        "items": [
            {
                "item_id": request.persona_instance_id,
                "persona_id": request.persona_id,
                "kind": "agent",
                "position": [x, y],
                "folder": request.folder,
                "display_name": display_name,
            }
        ],
    }


# ── the shared create sequence (UC-H1) ───────────────────────────────────────

# The JSON-RPC error codes this service answers in. Re-spelled here rather than
# imported from ``serve_rpc`` on purpose: importing that module would drag the
# entire method registry (and its store imports) into every CLI process that
# only wants to create one agent, and would invert the dependency — serve_rpc
# imports THIS module. Drift is fenced instead of prevented:
# ``tests/agent_runtime/test_agent_create_service.py`` asserts each constant
# equals ``serve_rpc``'s same-named one, so a change to either goes red.
ERR_INVALID_PARAMS = -32602
ERR_HANDLER_FAILED = -32000
ERR_NOT_FOUND = 4001
ERR_CONFLICT = 4090

# ── ``data.phase``: WHICH half of the two-write sequence a refusal failed in ──
#
# The launcher's ``MissionAgentCreateFault.phase`` documents exactly three
# values — ``instance | placement | null`` — and its parser is
# ``dataMap?['phase']?.toString()``, i.e. it accepts any string and renders it
# verbatim into the drop log (``mission_control_page.dart``:
# ``phase=${fault.phase ?? 'none'}``). That is precisely why the vocabulary is
# closed HERE: a value the enum does not document would decode without
# complaint and print a word nobody can grep the client for.
#
# Only the two placement arms ever spelled a phase, so every mint-phase refusal
# logged ``phase=none`` and the whole point of the field — telling the operator
# whether the roster row or the desk was the half that failed — was carried by
# exactly the arms where the answer was already obvious from ``rolled_back``.
#
# ``null`` is a value, not an omission, and the arms that carry no phase below
# carry none DELIBERATELY: ``workspace_not_found`` and the reservation faults
# are refused before either half is attempted, so naming one of them would be a
# third false claim in a payload this lane is here to make honest.
PHASE_INSTANCE = "instance"
PHASE_PLACEMENT = "placement"

# ── ``data.rolled_back`` for the reservation faults, one code at a time ───────
#
# :class:`AgentCreateReservationError` is raised by ``reserve_agent_create``
# BEFORE ``perform_agent_create`` writes anything, so it is tempting to answer
# every code with "nothing survives". Two of the three codes make that a lie
# about the world rather than about this attempt, and the launcher's sentence is
# about the world: ``rolled_back: true`` prints "the placement was refused and
# nothing was written."
#
# ``idempotency_conflict``
#     The receipt on disk was READ and validated against this request, and it
#     names a DIFFERENT persona or workspace — so it is another gesture's, and
#     nothing belonging to this one exists. Inventoried: zero paths change.
#     ``True``.
# ``create_lock_unavailable``
#     Another process holds this key's file lock and is mid-sequence. This
#     attempt wrote nothing, but the holder may be between its mint and its
#     placement right now, and we cannot read its receipt — that is what "lock
#     unavailable" means. ``False`` is not a claim that something survived; it
#     is a refusal to claim that nothing did, which is the direction the
#     launcher's parser already calls "the safe direction".
# ``reservation_corrupt``
#     The receipt file EXISTS and will not decode. Its state is unknown and by
#     construction unknowable, so it may name a minted roster row. ``False`` —
#     and this is the one arm on the whole method where "Check the runtime" is
#     the literally correct instruction.
#
# A code missing from this table is answered with NO ``rolled_back`` key, which
# the launcher reads as ``false``. That is deliberate: a new fault whose
# inventory nobody has established must not inherit an optimistic default.
_RESERVATION_ROLLED_BACK: dict[str, bool] = {
    "idempotency_conflict": True,
    "create_lock_unavailable": False,
    "reservation_corrupt": False,
}


@dataclass(frozen=True)
class AgentCreateRefusal:
    """One refused create, in the vocabulary the RPC lane answers in.

    ``code`` is the JSON-RPC error code, ``message`` the operator prose, and
    ``data`` the machine-readable block whose ``reason`` the launcher's
    ``missionAgentCreateReasonFrom`` decoder switches on. Every string here is
    byte-identical to what the handler returned before the hoist.
    """

    code: int
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class AgentCreateOutcome:
    """Exactly one of ``result`` / ``refusal`` is set."""

    result: dict[str, Any] | None = None
    refusal: AgentCreateRefusal | None = None


def _refused(code: int, message: Any, data: dict[str, Any]) -> AgentCreateOutcome:
    return AgentCreateOutcome(
        refusal=AgentCreateRefusal(code=code, message=str(message), data=data)
    )


def roster_unavailable_outcome(cause: Any = None) -> AgentCreateOutcome:
    """The roster fault as a REFUSAL, for a lane that met it before the service.

    RD-H6 item 2. A roster fault answered the two create lanes differently:
    ``runtime.agent.create`` refused typed ``persona_roster_unavailable``
    (:func:`normalize_agent_create`'s arm, reached because the RPC lane passes
    no pre-resolved persona), while ``harness agent create`` read the roster
    ITSELF first — ``_persona_by_id`` -> ``ensure_persisted_personas``, the same
    call :func:`persona_roster` wraps, but unwrapped — so a config that process
    could not read tracebacked out of the CLI. One fault, two renderings, and
    the argv one was a stack trace.

    This is the shape the CLI needs and the service cannot give it: the fault
    happens BEFORE ``perform_agent_create`` is entered, so there is no outcome
    to carry it. Returning the outcome (rather than letting the CLI hand-roll an
    envelope) is what keeps the code — and therefore the exit code, via
    ``_AGENT_CREATE_EXIT_CODES`` — from being re-guessed at the call site.

    ``ERR_INVALID_PARAMS`` matches the service's arm exactly: the roster fault
    arrives there through :class:`AgentCreateInvalid`, which the generic
    normaliser arm answers with that code. The two constructions are compared
    for EQUALITY by test rather than trusted to agree — see
    ``tests/hermes_cli/test_agent_create_verb.py``'s parity witness, which
    drives a corrupt-roster fixture down both lanes and asserts the reason and
    the message match.
    """

    return _refused(
        ERR_INVALID_PARAMS,
        persona_roster_unavailable_message(cause),
        # Carries the same stamp as the service's own arm, and must: the whole
        # point of this constructor is that the two lanes render ONE refusal,
        # and a ``data`` block that differed by a key would put the parity
        # witness in ``tests/hermes_cli/test_agent_create_verb.py`` back to
        # comparing two blocks that agree on the fields it happens to check.
        # The fault is met BEFORE ``perform_agent_create`` is entered, so
        # "nothing was written" is if anything more literally true here.
        {"reason": PERSONA_ROSTER_UNAVAILABLE_REASON, "rolled_back": True},
    )


def compensate_failed_placement(
    reservation, *, instance_id: str, failure: dict[str, Any]
) -> dict[str, Any]:
    """Compensate a failed placement and return the ``data`` block for the reply.

    Order matters and is the same order the whole sequence uses: undo the write
    that DID land before answering, so the caller's failure and the store agree.
    ``rolled_back`` is the field a client branches on and it is never optimistic
    — a compensation that raised reports ``false`` and names the instance that
    survived, because the alternative (claiming a rollback that did not happen)
    is the half-state this sequence exists to abolish, now with a lie on top.
    """

    from .persona_assignments import PersonaInstanceStore

    try:
        PersonaInstanceStore().retire(
            instance_id,
            reason="runtime.agent.create placement failed",
            requested_by="runtime.agent.create",
        )
    except Exception as exc:  # noqa: BLE001 - every retire refusal lands here
        reservation.mark_rollback_failed(
            failure, rollback_error=f"{type(exc).__name__}: {exc}"
        )
        return {
            **failure,
            "rolled_back": False,
            "persona_instance_id": instance_id,
            "rollback_error": f"{type(exc).__name__}: {exc}",
        }
    reservation.mark_rolled_back(failure)
    return {**failure, "rolled_back": True}


def perform_agent_create(
    params: dict[str, Any],
    *,
    updated_by: str = "operator",
    persona: Any | None = None,
) -> AgentCreateOutcome:
    """ONE call places an agent: roster row, chat root and placement together.

    Params: ``persona_id``, ``workspace_id``, ``position: [x, y]`` and
    ``idempotency_key`` (all required); ``display_name``, ``placement_id``,
    ``realm_id``, ``folder``, ``correlation_id`` (all optional).

    Result::

        {persona_instance_id, persona_id, placement_id, display_name,
         default_chat_session_id, actor_key, revision, workspace_id,
         phases: {instance_ms, placement_ms, total_ms}, idempotent_replay}

    ``persona`` is the CLI's richer pre-resolved persona object, threaded to
    :func:`normalize_agent_create` so the argv lane's naming behaviour is
    preserved byte-for-byte; the RPC lane has no such object and passes ``None``.

    Why one call and not two
    ------------------------
    The two-call flow awaits ``persona.instance.create`` on the argv lane and
    then, ≥600 ms later, flushes ``runtime.office.upsert`` on another. Two
    transports, two independent failures, no join — and BOTH half-states are
    reachable: an instance whose placement never lands (R#37), and a placement
    naming an instance the runtime never minted, which the office store accepts
    because it validates payload shape and class keys, not instance existence.
    Here the two writes happen inside one function milliseconds apart, so the
    stream producer's 200 ms settle joins them into one batch by construction
    rather than by luck.

    The durable ORDER is instance-first, and that is not arbitrary. A placement
    written first would BE the second half-state for as long as the mint took,
    and the launcher's codec refuses on principle to derive a binding for an
    actor that has none — so a crash there leaves an actor nothing can ever
    thread. Instance-first's failure mode is a roster row with no desk, which is
    both visible and retireable, and which the compensation below removes.

    What it deliberately does NOT do
    --------------------------------
    It does not make the create foldable and it is not a speed change; the batch
    carries exactly the events the two-call flow carries, which is what lets it
    inherit D3's foldable ``persona_instance`` create for free instead of
    colliding with it. It also does not move naming authority: an explicit
    ``display_name`` from the client still wins (decision D-A1), and an omitted
    one falls back through the ONE shared rule the argv lane uses
    (:func:`honest_default_display_name`) rather than through the store
    template, which would mint "Qa" where the operator expects "QA Agent".

    Every lock in the path is a cross-process FILE lock, so this is correct with
    or without a live ``harness serve`` beside it.
    """

    import time

    from .agent_create_phases import (
        CreateSubphases,
        log_create_subphases,
        timed_create_subphase,
        using_create_subphases,
    )
    from .agent_create_reservations import (
        STATE_DONE,
        STATE_INSTANCE_MINTED,
        STATE_ROLLED_BACK,
        AgentCreateReservationError,
        reserve_agent_create,
    )
    from .errors import StaleRevision, SyncConflict
    from .office_class_key_guard import ClassKeyedPlacementRefused
    from .office_store import OfficeStore
    from .persona_assignments import (
        PersonaInstanceStore,
        RetiredPersonaInstanceError,
    )
    from .persona_chat_durability import PersonaChatPersistenceError

    started = time.monotonic()

    try:
        request = normalize_agent_create(params, persona=persona)
    except AgentCreateInvalid as exc:
        # ``rolled_back: True`` on EVERY arm of this except, and the claim is
        # structural rather than per-reason: :func:`normalize_agent_create` runs
        # before ``OfficeStore`` is even constructed, so there is no roster row,
        # no chat root, no placement and no reservation receipt for any of its
        # eight refusals to have left behind. Its own docstring is the guarantee
        # ("a refusal here provably wrote nothing").
        #
        # It was absent, and an absent stamp is not neutral: the launcher's
        # decoder reads a missing ``rolled_back`` as ``false`` and renders "the
        # placement could not be undone" — so every mistyped persona id told the
        # operator to go check the runtime for wreckage that could not exist.
        # ``workspace_not_found`` one arm below has carried the stamp since
        # 1da669d908 for exactly this reason; these arms refuse EARLIER than it
        # does.
        return _refused(
            ERR_INVALID_PARAMS, exc, {"reason": exc.reason, "rolled_back": True}
        )

    store = OfficeStore()
    # Before the reservation, and before any store write: an unknown workspace
    # is the ONE refusal that must leave no receipt behind, or a client fixing a
    # typo would be answered with its own stale error under the same key. Mirrors
    # ``runtime.office.upsert``'s refusal to lazily author a surface for a typo.
    if not store.surface_exists(request.workspace_id):
        # Inventoried on a throwaway store root, every path under
        # ``store_root()`` diffed across the call: this arm changes NOTHING —
        # not even the ``locks/agent_creates/<digest>.lock`` its siblings take,
        # because it refuses before ``reserve_agent_create`` is entered and the
        # lock lives inside that context manager. It is the emptiest refusal on
        # the method, and ``rolled_back: True`` is its literal reading.
        #
        # No ``phase``: the surface check runs before the mint is attempted, so
        # neither half of the sequence has a verdict to report. The launcher
        # documents ``null`` for exactly that, and its own taxonomy comment for
        # this reason already says "a CREATE refused for it wrote nothing".
        return _refused(
            ERR_NOT_FOUND,
            f"unknown workspace: {request.workspace_id}",
            {
                "reason": "workspace_not_found",
                "workspace_id": request.workspace_id,
                "rolled_back": True,
            },
        )

    try:
        with reserve_agent_create(
            idempotency_key=request.idempotency_key,
            persona_id=request.persona_id,
            workspace_id=request.workspace_id,
        ) as reservation:
            record = reservation.record

            if record.state == STATE_DONE:
                # The same reply, and provably no second write: the actor's
                # revision in the recorded result is the witness.
                return AgentCreateOutcome(
                    result={**record.result, "idempotent_replay": True}
                )
            if record.state == STATE_ROLLED_BACK:
                # D-A3: the placement id is burned by the retirement tombstone,
                # so this key can never complete. Say so again rather than
                # inventing a different placement the client did not predict.
                return _refused(
                    ERR_CONFLICT,
                    "this create was already attempted and rolled back; "
                    "retry as a new gesture with a new idempotency_key",
                    {**record.failure, "rolled_back": True, "idempotent_replay": True},
                )

            instance_ms = 0
            if record.state == STATE_INSTANCE_MINTED:
                # Resume: the mint already happened (or its compensation could
                # not). Re-minting here is the duplicate-agent bug the whole
                # ledger exists to prevent.
                from dataclasses import replace as _replace

                request = _replace(
                    request, placement_id=record.placement_id or request.placement_id
                )
                try:
                    instance = PersonaInstanceStore().get(
                        record.persona_instance_id or request.persona_instance_id
                    )
                except Exception as exc:  # noqa: BLE001
                    # THE arm whose honest answer is the opposite of its
                    # siblings', and the reason this lane is a sweep rather
                    # than a one-line copy.
                    #
                    # Reaching here means ``record.state`` was
                    # ``instance_minted``, and that state is only ever written
                    # by ``mark_instance_minted`` (or ``mark_rollback_failed``,
                    # which deliberately keeps it) — the module's own "first
                    # durable write". So a receipt naming a roster row is on
                    # disk BEFORE this attempt begins, and this attempt does not
                    # remove it: inventoried on a throwaway root, the call
                    # changes nothing at all, and the ``instance_minted``
                    # receipt plus whatever the earlier attempt left are still
                    # there afterwards.
                    #
                    # ``rolled_back: True`` here would therefore be the same lie
                    # the absent field was telling, only pointed the other way:
                    # the operator would be told "nothing was written" while a
                    # receipt this key cannot get past sits on disk naming a row
                    # nothing can read. ``False`` is the truth, and it is also
                    # the value that makes the launcher publish
                    # ``persona_instance_id`` as ``orphanInstanceId`` (it reads
                    # that key only when ``rolled_back`` is not ``true``), which
                    # is exactly the id an operator needs.
                    #
                    # This lane is NOT a compensation site, and that is a
                    # decision rather than an omission. Retiring the row is
                    # impossible — it is the row we could not read — and marking
                    # the receipt ``rolled_back`` would BURN the placement id
                    # (D-A3: a rolled-back key is answered with its recorded
                    # refusal forever) over an instance that may simply be
                    # unreadable for a minute. Left at ``instance_minted`` the
                    # key stays resumable, which is why ``next_expected`` offers
                    # the same-key cure FIRST.
                    return _refused(
                        ERR_NOT_FOUND,
                        f"reserved instance is gone: {exc}",
                        {
                            "reason": "reserved_instance_missing",
                            "persona_instance_id": record.persona_instance_id,
                            "phase": PHASE_INSTANCE,
                            "rolled_back": False,
                            "next_expected": (
                                "this idempotency_key's receipt still names a minted "
                                "persona instance that cannot be read; restore that "
                                "instance and retry with the SAME idempotency_key to "
                                "resume the placement, or retire it and retry the "
                                "gesture with a NEW idempotency_key"
                            ),
                        },
                    )
            else:
                mint_started = time.monotonic()
                # W3-H1: ``instance_ms`` below is ONE number for everything in
                # this arm, and W3 arrived unable to say which of its half-dozen
                # cost blocks owned the ~2 s a first-of-session drop pays. This
                # recorder collects named spans from the sites inside
                # ``add_instance`` (and from the ``spawned_by`` write below) and
                # bills them to a LOG receipt — never to the ``phases`` block on
                # the result, which is a client-visible shape whose last
                # observability addition landed as a cross-stack fixture change.
                # See ``agent_create_phases``.
                subphases = CreateSubphases()
                try:
                    with using_create_subphases(subphases):
                        instance = PersonaInstanceStore().add_instance(
                            persona_id=request.persona_id,
                            placement_id=request.placement_id,
                            display_name=request.display_name,
                            default_display_name=request.default_display_name,
                            workspace_id=request.workspace_id,
                            realm_id=request.realm_id,
                        )
                except RetiredPersonaInstanceError as exc:
                    # ``add_instance`` decides this in ``assert_bindable``,
                    # which runs before ``_durable_chat_root`` and before
                    # ``open_chat`` — "every refusal this bind can raise is
                    # decidable without writing, so it is decided before the
                    # root is made durable" (``persona_assignments.py``). The
                    # reservation record is still unwritten here: the three
                    # states that mean "on disk" are all handled above, so this
                    # branch is only reached with a brand-new key.
                    #
                    # Inventoried against a REAL retirement tombstone (create,
                    # retire, re-create under a new key): the single path this
                    # refusal adds under ``store_root()`` is the empty
                    # ``locks/agent_creates/<digest>.lock`` every attempt takes.
                    # The tombstone that caused the refusal is older than the
                    # gesture, so it is not this refusal's residue.
                    return _refused(
                        ERR_CONFLICT,
                        exc,
                        {
                            "reason": "instance_retired",
                            "placement_id": request.placement_id,
                            "phase": PHASE_INSTANCE,
                            "rolled_back": True,
                        },
                    )
                except ValueError as exc:
                    # Same inventory, same reason: every ``ValueError``
                    # ``add_instance`` raises is raised from its validation
                    # prologue — a blank placement token, or a placement id that
                    # already belongs to another persona — all of it above
                    # ``assert_bindable`` and therefore above the first write.
                    # Inventoried with a REAL collision (``dev`` re-using
                    # ``qa``'s placement id), not just an injected raise: one
                    # empty lock file, nothing else.
                    return _refused(
                        ERR_INVALID_PARAMS,
                        exc,
                        {
                            "reason": "instance_invalid",
                            "placement_id": request.placement_id,
                            "phase": PHASE_INSTANCE,
                            "rolled_back": True,
                        },
                    )
                except PersonaChatPersistenceError as exc:
                    # The mint could not make this agent's chat root durable, so
                    # it bound NOTHING — ``_durable_chat_root`` raises on the way
                    # into ``open_chat``'s ``session_id`` argument, before the
                    # instance row is written. Nothing to compensate, therefore,
                    # and deliberately no half-created agent: the alternative
                    # this replaces was binding a phantom root and answering
                    # ``ok`` — the agent appeared in the office and every message
                    # to it was refused ``unknown_chat_session`` forever (live
                    # 2026-08-20).
                    #
                    # Same typed vocabulary as the argv open-chat lane's
                    # ``chat_session_persist_failed`` frame, so a launcher that
                    # already decodes that kind from ``mission-chat`` reads this
                    # refusal too.
                    #
                    # ``rolled_back: True`` — and it is the LITERAL truth, not a
                    # courtesy. Nothing was written under this key at all: this
                    # arm is reached before ``mark_instance_minted``, which
                    # ``reserve_agent_create`` names as "the first durable
                    # write", so not even the reservation receipt exists (there
                    # is no ``reserved`` state in that module; ``_VALID_STATES``
                    # is instance_minted/done/rolled_back). Inventoried on a
                    # throwaway store root: the only path this refusal leaves
                    # behind is the empty ``locks/agent_creates/<digest>.lock``
                    # every attempt takes, and there is no
                    # ``persona_instances/`` directory, no office actor and no
                    # reservation directory.
                    #
                    # Stamping it matters because the launcher's parser reads
                    # ``data.rolled_back`` off EVERY error frame and defaults it
                    # to ``false`` as a fail-safe
                    # (``mission_agent_create_rpc.dart``). Without the key this
                    # -32000 fell through to ``handlerRaised`` with
                    # ``rolledBack: false``, and the operator was told "the
                    # roster row could not be undone. Check the runtime." —
                    # sent to look for wreckage that does not exist. Same
                    # spelling as the sibling refusals
                    # (:func:`compensate_failed_placement` and the
                    # ``STATE_ROLLED_BACK`` replay arm above) because it is the
                    # field a client branches on and a second vocabulary for
                    # "nothing survives" is a field no client reads.
                    #
                    # ``next_expected`` does NOT ask for the same idempotency
                    # key. It used to, and that was unreachable advice: the only
                    # client on this lane mints a fresh micros-stamped key per
                    # gesture, deliberately, so a re-click is a new agent. Since
                    # no receipt was recorded under this key, same-key and
                    # fresh-key retries are the SAME operation and the key
                    # discipline the old sentence implied bought nothing.
                    from .mission_chat_outcome import ChatErrorKind

                    return _refused(
                        ERR_HANDLER_FAILED,
                        exc,
                        {
                            "reason": str(ChatErrorKind.CHAT_SESSION_PERSIST_FAILED),
                            "persistence_operation": exc.operation,
                            "placement_id": request.placement_id,
                            "phase": PHASE_INSTANCE,
                            "rolled_back": True,
                            "next_expected": (
                                "restore canonical persona chat transcript storage "
                                "and retry the gesture; nothing was recorded under "
                                "this idempotency_key"
                            ),
                        },
                    )
                # The argv lane's own provenance stamp, matched so the two lanes
                # cannot be told apart by the row they leave. Deliberately NOT
                # ``updated_by``: that names the author of the office WRITE,
                # while this names who the agent was spawned by, and no
                # coordinator reaches either lane that calls this function.
                instance.spawned_by = "operator"
                with using_create_subphases(subphases), timed_create_subphase(
                    "spawned_by_write_ms"
                ):
                    instance = PersonaInstanceStore().update(instance)
                instance_ms = int((time.monotonic() - mint_started) * 1000)
                # Emitted here rather than beside the result: this is where the
                # mint's span CLOSES, and every arm between here and the return
                # is a placement failure whose cost belongs to ``placement_ms``.
                log_create_subphases(
                    subphases,
                    persona_id=request.persona_id,
                    instance_ms=instance_ms,
                )
                reservation.mark_instance_minted(
                    persona_instance_id=instance.id,
                    placement_id=request.placement_id,
                )

            placement_started = time.monotonic()
            payload = placement_actor_payload(
                request, display_name=instance.display_name
            )
            try:
                # CI-4's FREE half (EG-2.3): this method already reserved
                # ``correlation_id`` from birth and already echoes it on the
                # result, so the only hop it was missing is the one the office
                # store now takes — the placement's domain event and its paired
                # patch row. With this kwarg a create's OFFICE half joins the
                # same token as an ordinary drag, and the named partial-coverage
                # window (Plan D §V3) shrinks to the roster half alone. The 38
                # argv capability lanes stay deferred.
                #
                # The token is re-normalized inside the store: this method's own
                # parser admits up to 200 characters, which is looser than the
                # payload cap, so a long id degrades to "no id" rather than
                # riding the wire.
                actor = store.upsert_actor(
                    request.workspace_id,
                    payload,
                    updated_by=updated_by,
                    correlation_id=request.correlation_id,
                )
            except ClassKeyedPlacementRefused as exc:
                # ``placement_actor_payload`` is instance-keyed by construction,
                # so the store's class-key fence can never fire from this
                # sequence AS IT STANDS — and a defence that is only correct "by
                # construction" is one refactor away from being absent, so this
                # arm exists and is tested by injecting the payload shape that
                # refactor would produce (EG-6.6). It is a compensated refusal
                # like every other placement failure: the roster row this method
                # just minted must not outlive the placement it was minted for.
                #
                # ``placement_reason`` keeps the string it has always spent, and
                # the fence's own evidence rides beside it: a refusal that does
                # not name the actor it collided with is one nobody can act on,
                # and this lane had been dropping exactly that.
                data = compensate_failed_placement(
                    reservation,
                    instance_id=instance.id,
                    failure={
                        "reason": "placement_failed",
                        "phase": PHASE_PLACEMENT,
                        "placement_reason": "class_key_collision",
                        "workspace_id": request.workspace_id,
                        "reasons": exc.safe_details["reasons"],
                        "class_actor_key": exc.safe_details["class_actor_key"],
                        "conflicting_actor_keys": exc.safe_details["conflicting_actor_keys"],
                    },
                )
                return _refused(ERR_CONFLICT, "class-keyed placement refused", data)
            except (StaleRevision, SyncConflict, ValueError) as exc:
                data = compensate_failed_placement(
                    reservation,
                    instance_id=instance.id,
                    failure={
                        "reason": "placement_failed",
                        "phase": PHASE_PLACEMENT,
                        "placement_reason": type(exc).__name__,
                        "workspace_id": request.workspace_id,
                    },
                )
                return _refused(ERR_CONFLICT, exc, data)
            except Exception as exc:  # noqa: BLE001
                # An unexpected store fault is still a placement that did not
                # land, and the roster row must not survive it. The RPC
                # boundary would have turned this into a -32000 with the
                # instance stranded — which is R#37 with a nicer error code.
                data = compensate_failed_placement(
                    reservation,
                    instance_id=instance.id,
                    failure={
                        "reason": "placement_failed",
                        "phase": PHASE_PLACEMENT,
                        "placement_reason": type(exc).__name__,
                        "workspace_id": request.workspace_id,
                    },
                )
                return _refused(ERR_HANDLER_FAILED, exc, data)

            placement_ms = int((time.monotonic() - placement_started) * 1000)
            result: dict[str, Any] = {
                "persona_instance_id": instance.id,
                "persona_id": instance.persona_id,
                "placement_id": request.placement_id,
                "display_name": instance.display_name,
                "default_chat_session_id": instance.default_chat_session_id,
                "actor_key": actor.actor_key,
                "revision": actor.revision,
                "workspace_id": request.workspace_id,
                "phases": {
                    "instance_ms": instance_ms,
                    "placement_ms": placement_ms,
                    "total_ms": int((time.monotonic() - started) * 1000),
                },
            }
            if request.correlation_id:
                result["correlation_id"] = request.correlation_id
            reservation.mark_done(result)
            return AgentCreateOutcome(result={**result, "idempotent_replay": False})
    except AgentCreateReservationError as exc:
        # One ``except`` for three faults that do NOT agree about what survives
        # them — see :data:`_RESERVATION_ROLLED_BACK` for the per-code argument.
        # Answering them with one value would have been the same shortcut this
        # lane exists to undo, one level up.
        #
        # No ``phase`` on any of them: ``reserve_agent_create`` raises before the
        # mint is attempted, so neither half has a verdict.
        refusal_data: dict[str, Any] = {"reason": exc.code}
        if exc.code in _RESERVATION_ROLLED_BACK:
            refusal_data["rolled_back"] = _RESERVATION_ROLLED_BACK[exc.code]
        return _refused(
            ERR_CONFLICT
            if exc.code in {"idempotency_conflict", "create_lock_unavailable"}
            else ERR_HANDLER_FAILED,
            exc,
            refusal_data,
        )
