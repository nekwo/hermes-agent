"""The INVERSE of the placement verb: one call takes an agent off the level.

``perform_agent_retire`` is to ``agent retire`` / ``runtime.agent.retire`` what
``agent_create.perform_agent_create`` is to the two create doors — the one
function both lanes answer with, so a lane switch cannot become a behaviour
change (placement plan D7, the UC-H1 rule applied to the other direction).

Why this exists at all, since the store already did the work
------------------------------------------------------------
``PersonaInstanceStore.retire`` has always archived BOTH halves — the roster row
into ``persona_instances_archive/<ts>_retire/``, and, through
``OfficeStore.archive_actors_for_instance``, every office actor bound to the
instance. So the inverse of a placement was never missing; what was missing was
a DOOR and a RECEIPT:

* there was no ``runtime.*`` method for it, so the launcher removed a deliberate
  placement through two unjoined lanes (a ``persona.instance.retire`` argv
  capability AND a ``runtime.office.remove``), and a half-state — actor archived
  with the row still live, or the reverse — was representable with nothing to
  detect it; and
* the office half was best-effort AND SILENT. Its outcome was discarded inside
  ``_archive_office_placements``, so "the desk is still on the canvas after the
  retire said it was gone" was a fact no caller could be told.

Both are answered here: ONE call archives both halves, and the ack NAMES every
actor it archived (``archived_actor_keys``) beside every one it could not
(``office_archive_failures``). An empty failures list is the positive claim that
every bound actor is off the level.

Idempotence, and why it is not a nicety
---------------------------------------
A remote client that lost the ack must be able to ask again (plan D11, and the
gateway's exactly-once obligation in §A.11). So a retire of an ALREADY-archived
id answers the same ack with ``already_retired: true`` rather than
``not_found`` — including the same ``archive_path`` and the same
``archived_actor_keys``, re-read from the archive rather than reconstructed, so
a retrying client resolves its intent on the replay exactly as it would have on
the original.

The refusal vocabulary is the store's, one-to-one
-------------------------------------------------
``PersonaInstanceRetireError.code`` is not re-spelled here. ``not_found`` maps to
``ERR_NOT_FOUND`` and every other code — ``canonical_persona_channel``,
``instance_active``, ``assignment_active``, ``assignments_unknowable`` — to
``ERR_CONFLICT`` with ``data.reason`` carrying the code verbatim, because the
launcher decodes ``data.reason`` first and the numeric code second. A refusal
this lane invented would be a second vocabulary for one set of guards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

#: The JSON-RPC error codes this service answers in. Re-spelled rather than
#: imported from ``serve_rpc`` for the same reason ``agent_create`` re-spells
#: them: importing that module would drag the whole method registry into every
#: CLI process, and would invert the dependency (serve_rpc imports THIS module).
#: Drift is fenced by test, not prevented — see
#: ``tests/agent_runtime/test_agent_retire_service.py``.
ERR_INVALID_PARAMS = -32602
ERR_NOT_FOUND = 4001
ERR_CONFLICT = 4090

#: The ONE retire refusal that is not a store guard: the request never named a
#: target. Spelled like ``agent_create``'s ``persona_id_required`` so the two
#: verbs' missing-parameter refusals read the same way.
REASON_INSTANCE_ID_REQUIRED = "persona_instance_id_required"

#: Every ``PersonaInstanceRetireError.code`` that is a CONFLICT rather than a
#: missing target. Not consulted as a membership test — the mapping below is
#: "not_found, or conflict" — but recorded so the 1:1 claim in the docstring is
#: readable beside the code it describes.
CONFLICT_REASONS = (
    "canonical_persona_channel",
    "instance_active",
    "assignment_active",
    "assignments_unknowable",
)


@dataclass(frozen=True)
class AgentRetireRefusal:
    """One refused retire, in the vocabulary the RPC lane answers in."""

    code: int
    message: str
    data: dict[str, Any]


@dataclass(frozen=True)
class AgentRetireOutcome:
    """Exactly one of ``result`` / ``refusal`` is set."""

    result: dict[str, Any] | None = None
    refusal: AgentRetireRefusal | None = None


def _refused(code: int, message: Any, data: dict[str, Any]) -> AgentRetireOutcome:
    return AgentRetireOutcome(
        refusal=AgentRetireRefusal(code=code, message=str(message), data=data)
    )


def _canonical_instance_id(raw: Any) -> str | None:
    from .persona_assignments import canonical_persona_instance_id, safe_assignment_token

    token = safe_assignment_token(raw)
    if not token:
        return None
    return canonical_persona_instance_id(token) or token


def _already_retired_ack(
    instance_id: str,
    tombstone,
    *,
    reason: str,
    requested_by: str | None,
) -> dict[str, Any]:
    """The replay ack for an id whose row is already in the retire archive.

    Every DURABLE field is read back from the store — the archived row for the
    identity fields, the archive path for where it went, and the office archive
    for which actors left with it — so the replay agrees with the original ack
    on everything the original ack could promise. The two fields that are the
    REQUEST's rather than the store's (``reason``, ``requested_by``) are this
    call's, and that is deliberate: echoing the first call's reason would mean
    inventing a value from a payload nothing persisted.
    """

    from .models import PersonaInstance
    from .office_store import OfficeStore
    from .serde import from_jsonable

    persona_id: str | None = None
    display_name: str | None = None
    mode: str | None = None
    try:
        row = from_jsonable(
            PersonaInstance, json.loads(tombstone.read_text(encoding="utf-8"))
        )
    except Exception:  # noqa: BLE001 - an unreadable tombstone is still a tombstone
        pass
    else:
        persona_id = row.persona_id
        display_name = row.display_name
        mode = row.mode
    try:
        archived_actor_keys = OfficeStore().archived_actor_keys_for_instance(instance_id)
    except Exception:  # noqa: BLE001 - the office projection is never authoritative here
        archived_actor_keys = []
    return {
        "persona_instance_id": instance_id,
        "persona_id": persona_id,
        "display_name": display_name,
        "mode": mode,
        "reason": reason,
        "requested_by": requested_by,
        "archive_path": str(tombstone),
        "archive_dir": str(tombstone.parent),
        "archived_actor_keys": archived_actor_keys,
        # A replay archives nothing, so it can fail at nothing. An empty list is
        # the honest answer and NOT a claim that the first call had none — that
        # claim was the first ack's to make, which is why losing that ack costs
        # a client the failures only, never the identities.
        "office_archive_failures": [],
        "already_retired": True,
    }


def perform_agent_retire(params: dict[str, Any]) -> AgentRetireOutcome:
    """ONE call retires an agent: the roster row AND every actor bound to it.

    Params: ``persona_instance_id`` (required); ``reason`` and ``requested_by``
    (optional, normalised by the store exactly as ``persona instance retire``
    normalises them).

    Result::

        {persona_instance_id, persona_id, display_name, mode, reason,
         requested_by, archive_path, archive_dir,
         archived_actor_keys: [...], office_archive_failures: [{actor_key,
         workspace_id, error}], already_retired}

    ``already_retired`` is ``False`` for the call that did the work and ``True``
    for every replay of it. ``office_archive_failures`` entries carry
    ``workspace_id`` beside the two keys D11 names — additive, and the field an
    operator needs to go look at the desk that did not archive; a fault in the
    office projection ITSELF carries ``actor_key: None``, because it is not one
    actor's fault and naming one would be a guess.

    Authorization scope (plan §A.11 / D10-iv): this verb is **console**-scope —
    it mutates the level exactly as ``runtime.office.*`` does — and it is NOT on
    any peer-tier allowlist. An agent on one install never retires an agent on
    another; a remote OPERATOR (device tier) does.
    """

    from .persona_assignments import PersonaInstanceRetireError, PersonaInstanceStore

    raw_id = (params or {}).get("persona_instance_id")
    instance_id = _canonical_instance_id(raw_id)
    if not instance_id:
        return _refused(
            ERR_INVALID_PARAMS,
            "persona_instance_id is required",
            {"reason": REASON_INSTANCE_ID_REQUIRED},
        )

    reason = (params or {}).get("reason") or "placement removed"
    requested_by = (params or {}).get("requested_by")

    store = PersonaInstanceStore()
    try:
        result = store.retire(instance_id, reason=reason, requested_by=requested_by)
    except PersonaInstanceRetireError as exc:
        if exc.code == "not_found":
            # Asked AFTER the attempt, never before it, so the answer covers the
            # race as well as the replay: an id retired by another process
            # between a pre-flight probe and this call would have been refused
            # by a check-then-act version of this arm.
            tombstone = store.retired_instance_archive_path(instance_id)
            if tombstone is not None:
                return AgentRetireOutcome(
                    result=_already_retired_ack(
                        instance_id,
                        tombstone,
                        reason=reason,
                        requested_by=requested_by,
                    )
                )
            return _refused(
                ERR_NOT_FOUND,
                exc.message,
                {
                    "reason": exc.code,
                    "persona_instance_id": exc.persona_instance_id,
                    **exc.detail,
                },
            )
        return _refused(
            ERR_CONFLICT,
            exc.message,
            {
                "reason": exc.code,
                "persona_instance_id": exc.persona_instance_id,
                **exc.detail,
            },
        )

    return AgentRetireOutcome(result={**result, "already_retired": False})
