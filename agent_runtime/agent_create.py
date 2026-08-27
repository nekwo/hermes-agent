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

from .models import (
    PLACEMENT_ID_NOT_DISCRIMINABLE_REASON,
    looks_like_deliberate_placement,
    placement_id_not_discriminable_message,
)
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
    #: ``None`` means the client did not aim — the layout policy chooses (D2).
    #: It is NOT a missing value to be defaulted somewhere later: the ONE site
    #: that resolves it is :func:`resolve_placement_position`, and it runs where
    #: the actor set it reads is the set the write lands beside.
    position: tuple[float, float] | None
    #: ``None`` means the client sent no ``skills`` key at all, and the new
    #: instance's ``skill_overrides`` stays ``None`` (inherit the persona's,
    #: live). A LIST — including an empty one — is an explicit assignment, and
    #: ``[]`` therefore means "override with nothing", which is a different
    #: agent from one that inherits. See :func:`_skills` (D5).
    skills: tuple[str, ...] | None
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

    The ``_agent_`` marker is not decoration: it is what
    ``DELIBERATE_PLACEMENT_SUFFIX`` matches, and it was MISSING here until
    2026-08-27. This function claimed parity with the launcher mint and did not
    have it, so every server-minted placement derived an instance id the
    launcher classified as a conversational channel — the wrong-alice incident
    reached through the door that requires no operator input at all.
    """

    token = safe_assignment_token(persona_id) or "persona"
    return f"{token}_agent_{uuid.uuid4().hex[:8]}"


def _position(value: Any) -> tuple[float, float] | None:
    """The operator's aim, or ``None`` when there was none (plan D2).

    ``None`` — an omitted key or an explicit JSON ``null`` — is ABSENCE, and
    absence is a legal request answered by the layout policy. It used to refuse
    ``position_invalid``, which is why ``--pos`` was required and why every door
    without a canvas had nothing to send.

    Treating explicit ``null`` as absence rather than as a malformed value is
    the deliberate half of that: a JSON client that spells "no opinion" as
    ``null`` means the same thing as one that omits the key, and refusing one
    while accepting the other would make the wire's meaning depend on a
    serializer's `omit-none` setting.

    Everything else refuses exactly as before — a one-element list, a string, a
    bool pair, an infinity — because those are an aim that did not survive
    transport, not the absence of one.
    """

    if value is None:
        return None
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


#: The instance store's own override cap (``_safe_skill_overrides`` slices to
#: 40), re-spelled as a REFUSAL rather than inherited as a silent truncation: a
#: create whose 41st skill vanished on the way to the store would report
#: ``assigned`` with 41 entries and hold 40.
MAX_SKILLS = 40


def _skills(value: Any) -> tuple[str, ...] | None:
    """The requested skill ids, or ``None`` when the client sent no opinion.

    SHAPE only. Whether an id RESOLVES — and whether its installed copy matches
    the repo's — is the skills PHASE's question, asked after the placement, and
    it must be: a refusal here refuses before any write, and D4 rules that a
    skills fault never costs an agent its placement.

    Absence and ``null`` both mean inherit, for the same reason
    :func:`_position` reads them as one: a client spelling "no opinion" as
    ``null`` means what one omitting the key means, and the wire's meaning must
    not depend on a serializer's omit-none setting. An empty LIST is not
    absence — it is an explicit "no skills", recorded as ``[]``.

    Every member is stringified and stripped; blanks are dropped and duplicates
    collapse to their first appearance, which is the store's own
    ``_safe_skill_overrides`` behaviour re-spelled so the ack's ``assigned``
    list is the list that was written rather than a superset of it.
    """

    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise AgentCreateInvalid(
            "skills_invalid", "invalid params: skills must be a list of skill ids"
        )
    ids: list[str] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (str, int, float)):
            # A dict/list/None member is a client that built the wrong payload,
            # not a skill nobody has: refusing it here is honest, and letting it
            # through as ``str(item)`` would place an agent and then refuse the
            # skills phase on an id spelled ``{'id': 'x'}``.
            raise AgentCreateInvalid(
                "skills_invalid",
                "invalid params: every skills entry must be a skill id string",
            )
        text = str(item).strip()
        if not text or text in ids:
            continue
        ids.append(text)
    if len(ids) > MAX_SKILLS:
        raise AgentCreateInvalid(
            "skills_invalid",
            f"invalid params: skills must name {MAX_SKILLS} ids or fewer",
        )
    return tuple(ids)


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
    skills = _skills(params.get("skills"))

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
        # Asked of the SENT id only. A minted id clears this by construction,
        # and an id that reaches the store from anywhere else is a row that
        # already exists — this fence validates operator input, it does not
        # police the id space.
        if not looks_like_deliberate_placement(placement_id):
            raise AgentCreateInvalid(
                PLACEMENT_ID_NOT_DISCRIMINABLE_REASON,
                placement_id_not_discriminable_message(placement_id),
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
        skills=skills,
        idempotency_key=idempotency_key,
        placement_id=placement_id,
        display_name=display_name,
        default_display_name=honest_default_display_name(persona_id, persona),
        realm_id=realm_id,
        folder=folder,
        correlation_id=correlation_id,
    )


def placement_actor_payload(
    request: AgentCreateRequest,
    *,
    display_name: str,
    position: tuple[float, float] | None = None,
) -> dict[str, Any]:
    """The actor payload for a freshly minted placement.

    ``position`` is the RESOLVED one — the operator's aim when they had one, the
    layout policy's slot when they did not (D2). It is a parameter rather than a
    read of ``request.position`` because by the time this is called the policy
    may already have answered, and a payload builder that re-derived the
    position would be a second place the answer could come from.

    Deliberately the SAME shape ``harness office actor-upsert`` takes on
    ``--actor-json`` and the launcher's ``officeActorPayloadsFromLayout``
    produces — one schema across three writers, so a lane switch can never read
    as an edit. It is instance-keyed by construction (``persona_instance_id``
    is the id the mint just returned), which is what makes the class-key
    collision the office fence exists to refuse unreachable from this method.
    """

    resolved = position if position is not None else request.position
    if resolved is None:
        # Not reachable from ``perform_agent_create`` (it resolves first), and
        # loud rather than silent for anyone who calls this directly: a payload
        # with a made-up origin would place an agent at (0, 0) and read as a
        # deliberate placement forever.
        raise ValueError(
            "placement_actor_payload: no position — resolve one with "
            "resolve_placement_position() before building the payload"
        )
    x, y = resolved
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


def resolve_placement_position(
    store: Any, request: AgentCreateRequest
) -> tuple[float, float]:
    """The slot an UNAIMED create lands on (plan D2). The only caller of the
    layout policy on this lane.

    Scans the workspace's live actors in the REQUEST's folder and returns the
    first free lattice slot. The lane is the AGENT lane whatever the folder is
    called, because this verb writes exactly one ``kind: "agent"`` item and
    nothing else (D6) — the desk lane's diagonal nudge exists to keep unaimed
    desks off the agent lattice, and there are no unaimed desks on this lane.

    The actor this create is about to write is excluded from the scan. A resumed
    attempt (``instance_minted`` receipt, placement already landed, ``mark_done``
    never reached) would otherwise read its OWN previous item as a blocker and
    move the agent one slot along on every retry — an idempotent replay that
    walks.

    WHERE THIS READ SITS RELATIVE TO ``office_lock``, AND WHY
    --------------------------------------------------------
    OUTSIDE it, immediately before ``OfficeStore.upsert_actor``, which takes
    ``office_lock(workspace_id)`` itself.

    That is forced, not preferred. ``locks._file_lock`` is a real file lock —
    ``msvcrt.locking`` on Windows, ``flock`` elsewhere — held through a fresh
    ``open()`` per acquisition, so it is NOT reentrant: a second acquisition
    from this same process would block against the first and time out
    ``HarnessLockUnavailable`` after the configured 15 s. Wrapping the read and
    the write in one ``office_lock`` therefore deadlocks the write it is meant
    to protect. Moving the policy INTO ``upsert_actor`` would close the window
    honestly, and that is a store change this slice's strip does not carry.

    The window it leaves, stated rather than implied: two creates that both
    omit a position and race between this read and their writes can compute the
    same free slot, and the second lands on top of the first. Bounded — one
    slot, visible on the canvas, fixed by a drag — and no worse than the
    prediction the launcher has always sent, which reads a snapshot that is
    already a round trip stale. It is filed as a queue row rather than left for
    the next reader to discover.
    """

    from .office_layout_policy import (
        lane_offset_for_kind,
        next_free_slot,
        occupied_positions,
    )

    mine = request.persona_instance_id
    actors = [
        actor
        for actor in store.list_actors(request.workspace_id)
        if getattr(actor, "persona_instance_id", None) != mine
    ]
    return next_free_slot(
        occupied_positions(actors, folder=request.folder),
        lane_offset=lane_offset_for_kind("agent"),
    )


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
#: The THIRD phase, added by plan S4. It is the one phase whose refusals leave
#: durable state behind ON PURPOSE (D4): the agent is placed, messageable and
#: correct, and only its skill assignment is missing. Every arm below therefore
#: stamps ``rolled_back: false`` — not as a hedge, but as the literal truth,
#: with ``next_expected`` naming the same-key retry that resumes it.
PHASE_SKILLS = "skills"

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


# ── the skills phase (plan S4 / D5) ──────────────────────────────────────────


class AgentCreateSkillsRefused(Exception):
    """A skills-phase refusal, raised where it is decided and rendered once.

    Carries only the arm-specific block; the fields every skills refusal shares
    — ``phase``, ``rolled_back``, ``persona_instance_id``, ``next_expected`` —
    are stamped at the ONE rendering site in :func:`perform_agent_create`, so a
    new arm cannot ship without them.
    """

    def __init__(self, code: int, message: str, data: dict[str, Any]):
        super().__init__(message)
        self.code = code
        self.data = dict(data)


def run_skills_phase(
    skills: Any,
    *,
    instance_id: str,
    requested_by: str = "runtime.agent.create",
) -> dict[str, Any]:
    """Install, verify, resolve, then assign — in that order, each for a reason.

    Returns the ack block ``{assigned, installed}``; raises
    :class:`AgentCreateSkillsRefused` on every refusal.

    **Every id must survive BOTH sanitizers unchanged before any root is
    walked.** ``safe_id`` is D5's named gate; ``safe_assignment_token`` is the
    one the persona-instance store applies on the way in
    (``_safe_skill_overrides``), and it is the stricter of the two — it maps
    ``:`` to ``_`` where ``safe_id`` keeps it. Requiring identity under both is
    what makes the ack's ``assigned`` list the list the store actually HOLDS
    rather than a request the store quietly re-spelled, and it is also what
    makes "never path-joined from input" true: no separator, no drive letter and
    no leading dot survives either function, so the name handed to
    ``resolve_skills`` cannot address anything outside a skills root.

    **Install BEFORE resolve, deliberately.** D5 lists the resolve gate first
    and the install gate second, and the code runs them the other way round
    because a canonical skill's resolvable copy IS the installed one: on a
    machine where the shared root has never been written, resolving first would
    refuse ``skill_unresolved: missing`` for a skill the very next line would
    have installed. Resolving AFTER the install asks the question of the world
    the assignment will actually run against.

    **Assign LAST.** Nothing writes ``skill_overrides`` until every id has both
    gates behind it, so a two-skill request cannot leave one assigned and the
    other refused.

    **What a refusal here does NOT do.** It does not compensate the placement.
    That is D4 and it is not a convenience: a placed agent without its skills is
    the state every launcher drop produces today — valid, visible, messageable —
    and retiring it to satisfy atomicity would archive a working agent to undo a
    file copy. The reservation is already at ``placed`` when this runs, so the
    same idempotency key resumes here and nowhere else.
    """

    from hermes_constants import CANONICAL_SHARED_SKILL_IDS

    from .persona_assignments import PersonaInstanceStore, safe_assignment_token
    from .serde import safe_id

    ids = [str(item) for item in (skills or ())]

    # Gate 0 — the spelling, asked before any root is walked.
    for identifier in ids:
        if (
            safe_id(identifier) != identifier
            or safe_assignment_token(identifier) != identifier
        ):
            raise AgentCreateSkillsRefused(
                ERR_INVALID_PARAMS,
                f"skill id cannot be resolved: {identifier!r}",
                {
                    "reason": "skill_unresolved",
                    "skill": identifier,
                    # ``missing`` and not a fourth status: the resolver's own
                    # vocabulary is {missing, collision, invalid_source} and a
                    # name no skill root can hold is missing from all of them.
                    "status": "missing",
                },
            )

    # Gate 1 — the canonical ids are installed and PROVEN hash-equal.
    installed: list[dict[str, Any]] = []
    for identifier in ids:
        if identifier not in CANONICAL_SHARED_SKILL_IDS:
            # A non-canonical id has no repo package to compare against, so
            # there is nothing to install and nothing to verify — it is answered
            # by the resolver alone.
            continue
        from .skill_install import (
            HarnessSkillInstallDiverged,
            install_and_verify_harness_skill,
        )

        try:
            receipt = install_and_verify_harness_skill(identifier)
        except HarnessSkillInstallDiverged as exc:
            raise AgentCreateSkillsRefused(
                ERR_HANDLER_FAILED,
                str(exc),
                {
                    "reason": "skill_install_diverged",
                    "skill": exc.skill,
                    "source_hash": exc.source_hash,
                    "installed_hash": exc.installed_hash,
                },
            ) from exc
        except Exception as exc:  # noqa: BLE001 - a copy fault IS a divergence
            # The staged ``copytree``/``os.replace`` can fail for a dozen OS
            # reasons, and every one of them ends the same way: the installed
            # bytes are not known to match the repo's. Answering that with a
            # traceback out of the RPC boundary would strand a placed agent
            # behind a -32000 with no ``data`` at all, so it renders as the
            # divergence it is, with the hashes it could not establish left
            # explicitly null rather than guessed.
            raise AgentCreateSkillsRefused(
                ERR_HANDLER_FAILED,
                f"skill install failed: {identifier}: {type(exc).__name__}: {exc}",
                {
                    "reason": "skill_install_diverged",
                    "skill": identifier,
                    "source_hash": None,
                    "installed_hash": None,
                },
            ) from exc
        installed.append(
            {
                "skill": receipt.skill,
                "changed": bool(receipt.changed),
                "installed_hash": receipt.installed_hash,
            }
        )

    # Gate 2 — every id resolves, in the runtime this create is answering out of.
    if ids:
        from agent.skill_utils import resolve_skills

        resolutions = resolve_skills(list(ids))
        for identifier in ids:
            resolution = resolutions.get(identifier)
            status = getattr(resolution, "status", "missing")
            if status != "resolved":
                raise AgentCreateSkillsRefused(
                    ERR_INVALID_PARAMS,
                    f"skill does not resolve: {identifier} ({status})",
                    {
                        "reason": "skill_unresolved",
                        "skill": identifier,
                        "status": status,
                    },
                )

    # The write. INSTANCE tier and never the persona template: a persona-tier
    # write would silently reconfigure every other instance of that persona, and
    # no operator verb has ever done that (D5, F12/F13).
    try:
        updated = PersonaInstanceStore().update_profile(
            instance_id, skills=list(ids), requested_by=requested_by
        )
    except Exception as exc:  # noqa: BLE001
        raise AgentCreateSkillsRefused(
            ERR_HANDLER_FAILED,
            f"skill assignment failed: {type(exc).__name__}: {exc}",
            {"reason": "skill_assign_failed", "skills": list(ids)},
        ) from exc

    return {
        # Read BACK off the row rather than echoed from the request. Gate 0
        # already guarantees the two agree; reading the store is what keeps that
        # a guarantee instead of a claim.
        "assigned": list(updated.skill_overrides or []),
        "installed": installed,
        # This phase RAN, so the instance now carries its own overrides —
        # whatever the list turned out to be, including an explicitly empty one.
        # ``inherited`` is what separates that empty list from the absent
        # request that leaves the persona's skills in force (D11): both render
        # ``assigned: []``, and a client that has only ``assigned`` cannot tell
        # "this agent was overridden with nothing" from "this agent inherits
        # everything its persona has".
        "inherited": False,
    }


def _inherited_skills_ack() -> dict[str, Any]:
    """The ``skills`` block for a create that sent no ``skills`` at all.

    ONE shape on every reply (D11): the block is present whether or not the
    phase ran, so a client never has to read an absent key as an answer. What
    the absent request means is carried by ``inherited: True`` — the new
    instance's ``skill_overrides`` stays ``None`` and it therefore inherits its
    persona's skills, LIVE, rather than being pinned to a copy of them.

    Without this flag ``assigned: []`` is two different agents wearing one
    reply: the inheriting one, and the one an operator deliberately overrode
    with ``skills: []`` (an agent with no skills at all). The launcher renders
    those differently and could not previously tell them apart.

    One window where the flag is a statement about the REQUEST rather than a
    re-read of the row: a create that crashed between ``update_profile`` and
    ``mark_done``, resumed under the same key with no ``skills``, leaves the
    overrides the crashed attempt wrote and still answers ``inherited: True``.
    Re-reading the row here would make the ack a second authority for what this
    key decided (the same argument that keeps ``persona_instance_id`` out of
    :func:`replayed_result`'s re-read), so the flag stays a statement of the
    request and this paragraph is the accounting for it.
    """

    return {"assigned": [], "installed": [], "inherited": True}


def replayed_result(record_result: dict[str, Any]) -> dict[str, Any]:
    """The recorded ack, with its actor keys RE-READ off the live row.

    A ``done`` receipt records the ack the FIRST attempt returned, and the
    office actor has been mutable ever since: an operator drags the agent, a
    realm pull moves it, ``resolve_conflict`` bumps it. Returning the recorded
    ``position``/``actor``/``revision`` verbatim therefore hands a replaying
    client the coordinates the agent had at 09:00 and calls them current.

    That was harmless while ``actor`` was decoration. It stops being harmless in
    plan S7, where the launcher ADOPTS the ack's actor — key, position and
    revision — into its scene and its ``expect_revision`` bookkeeping. A stale
    ``revision`` adopted from a replay makes the client's very next guarded
    write refuse ``stale_revision``, and a stale ``position`` snaps the agent
    back to where it used to be. So the re-read happens here, hermes-side,
    BEFORE that adoption exists rather than after it has been debugged.

    What is deliberately NOT re-read: ``persona_instance_id``, ``placement_id``,
    ``default_chat_session_id`` and ``skills``. Those are IDENTITY and the
    recorded decision, not observations — re-deriving them would be a second
    authority for what this key created.

    **The "no second write happened" witness moves.** It used to be the ack's
    ``revision``, which is exactly the field this function stops freezing. The
    witness is now the RECEIPT FILE: state ``done`` is written once and a replay
    does not touch it, so a test that wants to prove nothing was written asserts
    on the receipt (and on the actor's own revision read from the store), never
    on the reply.

    ``actor_fresh`` is the honesty valve. When the actor cannot be read — it was
    archived, the workspace was deleted, the file will not decode — the recorded
    row is returned UNCHANGED and the flag says so. It is never fabricated and
    never omitted: a client that must know whether it may adopt gets an answer
    on every reply rather than having to infer one from a missing key.
    """

    from .office_store import OfficeStore
    from .office_models import office_actor_wire_row

    result = dict(record_result)
    workspace_id = result.get("workspace_id")
    actor_key = result.get("actor_key")
    if not workspace_id or not actor_key:
        # A receipt from before ``workspace_id`` rode the ack, or a hand-edited
        # one. Nothing to re-read against, and inventing a lookup key would be
        # the fabrication this function exists to avoid.
        result["actor_fresh"] = False
        return result
    try:
        actor = OfficeStore().get_actor(str(workspace_id), str(actor_key))
    except Exception:  # noqa: BLE001 - NotFound, a decode fault, a gone surface
        result["actor_fresh"] = False
        return result
    # The AGENT item's position, because that is what the ack's ``position``
    # named when it was written: this verb writes exactly one item and it is of
    # kind ``agent`` (D6 — it authors no desk). The fallback to "the first item
    # that has a position" is for a row something else has since added an item
    # to, where answering ``None`` would be worse than answering the row's own
    # first coordinate.
    items = list(getattr(actor, "items", ()) or ())
    position = None
    for candidate in (
        [item for item in items if getattr(item, "kind", None) == "agent"] + items
    ):
        raw = getattr(candidate, "position", None)
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            position = [float(raw[0]), float(raw[1])]
            break
    result["actor"] = office_actor_wire_row(actor)
    result["revision"] = actor.revision
    if position is not None:
        # Only when the row actually carries one. An actor whose items lost
        # their coordinates is a store fault, and echoing the recorded position
        # beside a freshly-read actor would be the half-fresh reply that is
        # worse than either honest answer.
        result["position"] = position
    result["actor_fresh"] = True
    return result


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


#: What every skills refusal tells the operator to do. It names the SAME key on
#: purpose: the reservation is at ``placed``, so a same-key retry re-enters at
#: the skills phase alone and neither re-mints the roster row nor re-writes the
#: actor. A NEW key would mint a SECOND agent beside the one already standing.
_SKILLS_RETRY_SENTENCE = (
    "the agent is placed and was kept; fix the named skill and retry with the "
    "SAME idempotency_key to resume the skills phase alone"
)


def _skills_refusal(
    exc: AgentCreateSkillsRefused, *, instance_id: str
) -> AgentCreateOutcome:
    """The ONE rendering site for a skills-phase refusal.

    ``persona_instance_id`` is carried because an operator whose skill id was
    wrong needs the id of the agent that IS standing — to message it, to retire
    it, or to name it in the retry. **Cross-repo note for S7:** the launcher
    reads that key off any refusal with ``rolled_back != true`` and publishes it
    as ``orphanInstanceId`` (``mission_agent_create_rpc.dart``). A skills-phase
    instance is NOT an orphan — it is a correctly placed agent — so S7 must
    branch on ``phase == "skills"`` there. No live gesture reaches this today
    (the launcher sends no ``skills``), which is why the useful field wins over
    a decoder that is being changed in the same plan.
    """

    return _refused(
        exc.code,
        exc,
        {
            **exc.data,
            "phase": PHASE_SKILLS,
            "rolled_back": False,
            "persona_instance_id": instance_id,
            "next_expected": _SKILLS_RETRY_SENTENCE,
        },
    )


def perform_agent_create(
    params: dict[str, Any],
    *,
    updated_by: str = "operator",
    persona: Any | None = None,
) -> AgentCreateOutcome:
    """ONE call places an agent: roster row, chat root and placement together.

    Params: ``persona_id``, ``workspace_id`` and ``idempotency_key`` (required);
    ``position: [x, y]``, ``skills: [id, ...]``, ``display_name``,
    ``placement_id``, ``realm_id``, ``folder``, ``correlation_id`` (all
    optional).

    ``position`` ABSENT means the client did not aim, and the layout policy
    chooses the slot (D2 — :func:`resolve_placement_position`, which documents
    where that read sits relative to ``office_lock``). Present, it is taken
    verbatim, exactly as it always was.

    ``skills`` ABSENT leaves the new instance's ``skill_overrides`` at ``None``
    (inherit the persona's, live). A list assigns it at the INSTANCE tier after
    the placement, behind two gates: every canonical id is installed and proven
    hash-equal to the repo package, and every id must resolve. See
    :func:`run_skills_phase` — including why a refusal there keeps the agent.

    Result::

        {persona_instance_id, persona_id, placement_id, display_name,
         default_chat_session_id, actor_key, revision, workspace_id,
         position: [x, y], actor: {...},
         skills: {assigned: [...], installed: [{skill, changed, installed_hash}]},
         actor_fresh: bool,
         phases: {instance_ms, placement_ms, skills_ms, total_ms},
         idempotent_replay}

    ``position`` is what was WRITTEN and ``actor`` is the row as STORED, in the
    same shape ``runtime.office.get`` renders. ``skills`` is what was assigned
    and what the install actually did. All of them are additive: an old client
    ignores them, and none of them moves ``RPC_CONTRACT_VERSION`` or the
    manifest's method list.

    On an ``idempotent_replay`` the actor is RE-READ rather than echoed from the
    receipt, so a client adopting it adopts the row as it is now
    (:func:`replayed_result`). ``actor_fresh`` is ``false`` when that re-read
    could not be made — the actor was archived, the surface is gone — and the
    recorded row is returned unchanged rather than fabricated.

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

    Three phases, and only two of them are atomic
    ---------------------------------------------
    ``instance`` and ``placement`` are the pair the reservation joins: a failure
    in the second compensates the first away. ``skills`` is deliberately NOT in
    that join (D4). It runs after both writes are durable and after the receipt
    reads ``placed``, and its refusals stamp ``rolled_back: false`` because the
    agent they refuse for is standing, correct and messageable — only its skill
    assignment is owed. The cure is the SAME idempotency key, which re-enters at
    the skills phase alone.
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
        STATE_PLACED,
        STATE_ROLLED_BACK,
        AgentCreateReservationError,
        reserve_agent_create,
    )
    from .errors import StaleRevision, SyncConflict
    from .office_class_key_guard import ClassKeyedPlacementRefused
    from .office_models import office_actor_wire_row
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
                # The recorded reply, with the actor RE-READ so a client that
                # adopts it adopts the row as it is NOW and not as it was when
                # this key first completed (:func:`replayed_result`). Still no
                # second write — the witness for that is the receipt file and
                # the actor's own revision in the store, never this reply.
                return AgentCreateOutcome(
                    result={
                        **replayed_result(record.result),
                        "idempotent_replay": True,
                    }
                )
            if record.state == STATE_PLACED:
                # Both writes landed under this key; only the skills phase is
                # owed. Re-enter THERE and nowhere else — re-minting or
                # re-placing would be the duplicate-agent bug the ledger exists
                # to prevent.
                #
                # The recorded ack is NOT the reply, though. It is the ack the
                # FIRST attempt rendered, and the office actor has been mutable
                # ever since — an operator drags the agent while they go and
                # look up the skill id they mistyped, and the retry that fixes
                # the typo would otherwise answer with the coordinates and the
                # revision the row had before the drag. Same argument, same
                # cure and the same re-read as the ``done`` arm above
                # (:func:`replayed_result`), because it is the same defect:
                # S7's launcher ADOPTS this actor.
                instance_id = record.persona_instance_id or ""
                if not instance_id or not record.result:
                    # A shape this code never writes: ``mark_placed`` always
                    # runs on a record that already carries both. Answered with
                    # the reason the module already spends on an unusable
                    # receipt rather than a new one, and NOT rolled back —
                    # whatever this receipt names may well be standing.
                    return _refused(
                        ERR_HANDLER_FAILED,
                        "agent-create reservation is at 'placed' but names no "
                        "instance or no recorded result",
                        {"reason": "reservation_corrupt", "rolled_back": False},
                    )
                skills_started = time.monotonic()
                result = replayed_result(record.result)
                # The CURRENT request's list, not the receipt's. The whole
                # point of the resume is that an operator who mistyped a skill
                # id fixes it and retries under the same key; answering with the
                # recorded list would refuse the corrected call for the old
                # typo, forever.
                requested = request.skills
                if requested is None:
                    skills_ack: dict[str, Any] = _inherited_skills_ack()
                else:
                    if list(requested) != list(record.skills or []):
                        reservation.mark_placed(result, skills=list(requested))
                    try:
                        skills_ack = run_skills_phase(
                            requested, instance_id=instance_id
                        )
                    except AgentCreateSkillsRefused as exc:
                        return _skills_refusal(exc, instance_id=instance_id)
                result["skills"] = skills_ack
                phases = dict(result.get("phases") or {})
                phases["skills_ms"] = int((time.monotonic() - skills_started) * 1000)
                phases["total_ms"] = int((time.monotonic() - started) * 1000)
                result["phases"] = phases
                reservation.mark_done(
                    result,
                    skills=list(requested) if requested is not None else None,
                )
                # ``False``: this attempt DID work — it ran the phase the first
                # one could not finish. ``idempotent_replay`` means "nothing
                # happened, here is the recorded answer", and that is the
                # ``done`` arm above, not this one.
                return AgentCreateOutcome(result={**result, "idempotent_replay": False})
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
            # D2: the aim if there was one, the policy's slot if there was not.
            # Read here — after the mint, immediately before the write — so the
            # actor set the policy sees is as close as this lane can get to the
            # set the write lands beside. See ``resolve_placement_position`` for
            # why "as close as it can get" and not "the same set".
            position = (
                request.position
                if request.position is not None
                else resolve_placement_position(store, request)
            )
            payload = placement_actor_payload(
                request, display_name=instance.display_name, position=position
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
                # D2/D11, both additive. ``position`` is what was WRITTEN —
                # policy or verbatim — so a client that sent none learns where
                # its agent went without a second read, and one that sent a
                # position can see it was taken verbatim.
                #
                # ``actor`` is the row as STORED, in ``runtime.office.get``'s
                # own item shape (``office_models.office_actor_wire_row``), and
                # it is taken off the store's return value rather than rebuilt
                # from the request — the whole point is that the client stops
                # trusting its predicted key/position/revision and adopts the
                # server's.
                "position": [position[0], position[1]],
                "actor": office_actor_wire_row(actor),
                # ONE shape per method: present on every reply, so a client
                # never has to read "the key is absent" as "yes, it is fresh".
                # Trivially ``True`` here — this IS the row just written.
                "actor_fresh": True,
                "phases": {
                    "instance_ms": instance_ms,
                    "placement_ms": placement_ms,
                    "total_ms": int((time.monotonic() - started) * 1000),
                },
            }
            if request.correlation_id:
                result["correlation_id"] = request.correlation_id

            # ── phase 3: skills (plan S4 / D5) ───────────────────────────────
            # Durable BEFORE the phase runs, and the receipt carries the request
            # — that is what makes a crash mid-install resumable at the skills
            # phase instead of re-entering the two writes above.
            skills_started = time.monotonic()
            if request.skills is None:
                # No opinion sent: ``skill_overrides`` stays ``None`` (inherit
                # the persona's, live) and NOTHING is written — not even an
                # empty list, which would be a different agent (D5, F13's
                # ``is not None`` contract). The ack block is still present and
                # empty, so a client reads one shape whatever was asked.
                skills_ack = _inherited_skills_ack()
            else:
                reservation.mark_placed(result, skills=list(request.skills))
                try:
                    skills_ack = run_skills_phase(
                        request.skills, instance_id=instance.id
                    )
                except AgentCreateSkillsRefused as exc:
                    return _skills_refusal(exc, instance_id=instance.id)
            result["skills"] = skills_ack
            result["phases"]["skills_ms"] = int(
                (time.monotonic() - skills_started) * 1000
            )
            # Re-stamped: ``total_ms`` was measured before the phase existed and
            # would under-report every create that installs a cold skill by
            # exactly the cost this plan set out to make visible.
            result["phases"]["total_ms"] = int((time.monotonic() - started) * 1000)
            reservation.mark_done(
                result,
                skills=list(request.skills) if request.skills is not None else None,
            )
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
