"""The policy layer ONE gesture-driven agent create shares across its lanes.

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
``AGENT_CREATE_ONE_CALL_PLAN_2026-08-16.md`` AC-0 says the extraction goes
"UPWARD, not downward" — into the CLI's ``persona_commands.py``. That direction
is not available: ``persona_commands.py`` is not an importable module. It is
``exec``'d into ``hermes_cli/harness.py``'s globals
(``harness.py:3667-3674``), and its functions close over names that live in
those globals and nowhere else — importing it and calling ``_persona_by_id``
raises ``NameError``. So the shared layer has to land somewhere BOTH lanes can
import, which is here, and the CLI calls down into it. The plan's requirement
("the CLI and the RPC handler share one copy", "do not invent a second rule")
is met; only its stated direction is not.
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
        from .config import ensure_persisted_personas, load_agent_runtime_config

        personas = list(ensure_persisted_personas(load_agent_runtime_config()))
    except Exception:
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
