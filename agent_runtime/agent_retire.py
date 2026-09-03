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

Idempotent is not the same as INERT, and treating them as the same was a bug
(D2). The replay used to only re-read; a live actor that had come back between
the two calls was therefore reported by neither arm, and "ask again" — the
operator's one obvious move when a desk is still on the canvas — was the single
gesture guaranteed to do nothing about it. The replay now sweeps live
placements bound to the instance and archives them, reporting per-actor
failures exactly as the fresh arm does. What stays identical is the ANSWER's
shape: same keys, ``already_retired`` still ``true``.

What a replay could still not report was the FIRST call's failures — they lived
only on the ack that was lost. Every retire now persists a receipt beside its
tombstone (``PersonaInstanceStore._write_retire_receipt``, H-H5) and the replay
carries it as ``first_attempt``, so "the desk did not archive and nobody can
ever be told which one" stops being a reachable state.

The refusal vocabulary is the store's, one-to-one
-------------------------------------------------
``PersonaInstanceRetireError.code`` is not re-spelled here. ``not_found`` maps to
``ERR_NOT_FOUND`` and every other code — ``canonical_persona_channel``,
``instance_active`` — to
``ERR_CONFLICT`` with ``data.reason`` carrying the code verbatim, because the
launcher decodes ``data.reason`` first and the numeric code second. A refusal
this lane invented would be a second vocabulary for one set of guards.

AX2 (2026-08-31) removed the store's two ASSIGNMENT guards, so
``assignment_active`` and ``assignments_unknowable`` left this vocabulary with
them — see :class:`persona_assignments.PersonaInstanceRetireError` for the
argument. Nothing here re-spelled them, which is why the change is one tuple and
one sentence.
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
#: The generic handler-failure code, spelled here because Stage A6's backstop
#: answers in it — the SAME code and the same ``data`` block the RPC front door
#: spends for a refused tier, so a client cannot tell which of the two layers
#: refused it and does not have to.
ERR_HANDLER_FAILED = -32000

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


def _correlation_id(raw: Any) -> str | None:
    """The gesture token off the params map, normalised EXACTLY as
    ``agent_create._request_from_params`` normalises its own.

    One spelling on purpose: the create half and the retire half of a single
    operator gesture must accept or reject the identical token, or a launcher
    that mints one id for both would find it on one patch and not the other —
    which is the two-correlation-spaces defect in a subtler dress.

    ``safe_assignment_text`` is the LOOSER of the two fences this token passes.
    The payload-side ``normalize_correlation_id`` in ``state_patches`` is the
    strict one and drops anything this lets through, so an illegal id reaches no
    event payload however it got here — this cap only decides what the ACK
    echoes.
    """

    from .persona_assignments import safe_assignment_text

    if raw is None:
        return None
    return safe_assignment_text(raw, limit=200) or None


def _with_correlation(
    result: dict[str, Any], correlation_id: str | None
) -> dict[str, Any]:
    """Echo the gesture token onto an ack — ONLY when the caller sent one.

    ``agent_create``'s rule verbatim (``if request.correlation_id: result[...]``)
    and for its reason: a key stamped unconditionally would put ``None`` on
    every ack a script has ever parsed, and "absent" and "null" are not the same
    answer to "which gesture was this".

    Both arms go through here — the fresh ack and the REPLAY. The replay's
    ``reason``/``requested_by`` are already this call's rather than the first
    call's, on the stated grounds that echoing a payload nothing persisted would
    be inventing it; the token is the same kind of field and gets the same
    treatment, so a client that lost its ack and asked again joins the reply to
    the gesture it actually made.
    """

    if not correlation_id:
        return result
    return {**result, "correlation_id": correlation_id}


def _sweep_live_placements(
    instance_id: str,
    *,
    reason: str,
    correlation_id: str | None,
) -> dict[str, Any]:
    """Archive every LIVE actor still bound to an already-retired instance.

    The replay's self-heal (D2). Same chokepoint the fresh arm reaches through
    ``PersonaInstanceStore._archive_office_placements``, and the same
    projection-fault posture: a fault in the office projection ITSELF is not one
    actor's fault, so it is reported with ``actor_key: None`` rather than a
    guess, and it never raises — the retirement is authoritative with or without
    the office projection, and a replay that raised would leave an operator
    unable to ask a second time at all.
    """

    try:
        from .office_store import OfficeStore

        return OfficeStore().archive_actors_for_instance(
            instance_id,
            reason=reason,
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001 - the retirement is authoritative regardless
        return {
            "archived": 0,
            "failed": 1,
            "archived_actor_keys": [],
            "failures": [
                {
                    "actor_key": None,
                    "workspace_id": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            ],
        }


def _already_retired_ack(
    instance_id: str,
    tombstone,
    *,
    reason: str,
    requested_by: str | None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """The replay ack for an id whose row is already in the retire archive.

    Every DURABLE field is read back from the store — the archived row for the
    identity fields, the archive path for where it went, and the office archive
    for which actors left with it — so the replay agrees with the original ack
    on everything the original ack could promise. The two fields that are the
    REQUEST's rather than the store's (``reason``, ``requested_by``) are this
    call's, and that is deliberate: echoing the first call's reason would mean
    inventing a value from a payload nothing persisted.

    A replay also ARCHIVES (D2), and until 2026-08-27 it did not. The docstring
    here used to say "a replay archives nothing, so it can fail at nothing",
    which was the wedge's own words: measured live, a retire acked cleanly, a
    launcher re-pushed the archived actors as ``state: active``, and every
    subsequent retire answered ``already_retired: true`` while the desks stayed
    on the canvas — because this arm only ever RE-READ the archive and had no
    way to put back what something else had taken out. D1 stops the re-push at
    the store; this is the other half, because an install already wedged does
    not un-wedge itself by the fence arriving.

    So the replay sweeps live placements first and reports what it could not
    archive, exactly as the fresh arm does. ``archived_actor_keys`` is still
    RE-READ from the archive rather than taken from the sweep's return — the
    archive is the superset (another lane may have archived one earlier) and a
    replay names the union, never one call's list.

    ``first_attempt`` is the FIRST call's recorded ack (H-H5), and it is the one
    thing a replay could never previously recover. ``office_archive_failures``
    below is and stays THIS call's — the positive claim "every actor bound to
    this instance is off the level as of this answer" has to keep meaning that —
    so the first attempt's failures ride their own key rather than being unioned
    into a list whose emptiness is load-bearing. ``None`` when there is no
    receipt to read: a retire from before receipts existed, or one whose receipt
    would not write. That absence is the honest statement of what every replay
    used to answer silently.
    """

    from .models import PersonaInstance
    from .office_store import OfficeStore
    from .persona_assignments import PersonaInstanceStore
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
    # BEFORE the archived-keys read, so the keys this sweep just archived are in
    # the list the replay reports. The order is the whole self-heal: reading
    # first would answer with the wedge's own empty list and then quietly fix it.
    sweep = _sweep_live_placements(
        instance_id, reason="instance_reaped", correlation_id=correlation_id
    )
    office_archive_failures = list(sweep["failures"])
    try:
        archived_actor_keys = OfficeStore().archived_actor_keys_for_instance(instance_id)
    except Exception as exc:  # noqa: BLE001 - the office projection is never authoritative here
        # It is not authoritative, so the replay still answers — but the empty
        # list it answers with is no longer allowed to look like the positive
        # claim. The fault rides the SAME list the sweep's per-actor failures
        # ride, with ``actor_key: None`` because the read that failed was about
        # no single actor (C4).
        archived_actor_keys = []
        office_archive_failures.append(
            {
                "actor_key": None,
                "workspace_id": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
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
        # THIS call's failures, not the first call's. A replay now archives, so
        # it can fail, and an empty list is again the positive claim the fresh
        # arm makes with it: every actor bound to this instance is off the level
        # as of this answer. It is still NOT a claim about what the first call
        # found — that claim was the first ack's to make, which is why losing
        # that ack costs a client the earlier failures only, never the
        # identities.
        "office_archive_failures": office_archive_failures,
        # What the FIRST call reported, recovered from the receipt it persisted
        # beside the tombstone (H-H5) — or ``None`` when there is none to read.
        "first_attempt": PersonaInstanceStore().read_retire_receipt(instance_id),
        "already_retired": True,
    }




def perform_agent_retire(
    params: dict[str, Any], *, caller: Any | None = None
) -> AgentRetireOutcome:
    """ONE call retires an agent: the roster row AND every actor bound to it.

    Params: ``persona_instance_id`` (required); ``reason`` and ``requested_by``
    (optional, normalised by the store exactly as ``persona instance retire``
    normalises them); ``correlation_id`` (optional, threaded and echoed).

    Result::

        {persona_instance_id, persona_id, display_name, mode, reason,
         requested_by, archive_path, archive_dir,
         archived_actor_keys: [...], office_archive_failures: [{actor_key,
         workspace_id, error}], already_retired, correlation_id?,
         retire_receipt_path?, retire_receipt_error?, first_attempt?}

    The last three are H-H5's, and they split by arm on purpose. The FRESH ack
    carries ``retire_receipt_path`` (``None`` plus ``retire_receipt_error`` when
    the receipt would not write) — where this call's outcome was recorded, so
    the client that is about to lose this ack knows whether it can be recovered.
    The REPLAY carries ``first_attempt``: the recorded first ack, or ``None``
    when there is none. Neither key appears on the other arm, because on the
    fresh arm ``first_attempt`` would point at itself and on the replay arm
    nothing was written to have a path.

    ``correlation_id`` is the LEVEL-MUTATION join, and it was this verb's alone
    to be missing (S8b). ``runtime.agent.create``, ``runtime.office.*`` and
    ``runtime.persona.prewarm`` all thread the gesture token onto the patches
    they emit; a retire emitted ``office.actor.removed`` with no token, so one
    operator gesture's create half and delete half lived in two correlation
    spaces and no single grep joined them. It rides through
    ``PersonaInstanceStore.retire`` → ``_archive_office_placements`` →
    ``OfficeStore.archive_actors_for_instance`` → ``remove_actor`` → the
    ``office.actor.removed`` event AND the ``state.patched`` remove row, and it
    is echoed here — present on the ack ONLY when the caller sent one, so a call
    without it is byte-identical to before the key existed.

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

    ``caller`` is the BACKSTOP for that scope (Stage A6, plan H-H13): the
    ``RpcCaller`` the transport minted, checked here as well as at the RPC front
    door, so the console tier is a property of this FUNCTION and not only of the
    dispatcher that usually calls it. It is not the gate and does not move it
    (Ruling A picked (b)); it is the assertion that a decision was made. Omitted
    means the local console, which is what every existing caller is
    (:func:`call_authorization.service_backstop`).
    """

    from .call_authorization import service_backstop
    from .persona_assignments import PersonaInstanceRetireError, PersonaInstanceStore

    # FIRST — before the id is even normalised. This refusal reads nothing and
    # writes nothing, which is the only honest place for a scope refusal to sit:
    # a caller who may not retire must not learn from the error code whether the
    # id they named exists.
    denied = service_backstop(caller, method="runtime.agent.retire")
    if denied is not None:
        return _refused(
            ERR_HANDLER_FAILED,
            f"runtime.agent.retire requires the {denied.tier} tier",
            denied.refusal_data(),
        )

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
    correlation_id = _correlation_id((params or {}).get("correlation_id"))

    store = PersonaInstanceStore()
    try:
        result = store.retire(
            instance_id,
            reason=reason,
            requested_by=requested_by,
            correlation_id=correlation_id,
        )
    except PersonaInstanceRetireError as exc:
        if exc.code == "not_found":
            # Asked AFTER the attempt, never before it, so the answer covers the
            # race as well as the replay: an id retired by another process
            # between a pre-flight probe and this call would have been refused
            # by a check-then-act version of this arm.
            tombstone = store.retired_instance_archive_path(instance_id)
            if tombstone is not None:
                return AgentRetireOutcome(
                    result=_with_correlation(
                        _already_retired_ack(
                            instance_id,
                            tombstone,
                            reason=reason,
                            requested_by=requested_by,
                            # The replay's sweep emits office writes of its own
                            # (D2), so the gesture token has to reach it or the
                            # self-heal lands in the unjoinable correlation
                            # space this verb's own S8b fix removed.
                            correlation_id=correlation_id,
                        ),
                        correlation_id,
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

    # S2c: the roster moved, so every paired install's cached copy is stale.
    # Off-thread and swallowed inside ``announce_roster_changed``.
    from .gateway_announce import announce_roster_changed

    announce_roster_changed()
    return AgentRetireOutcome(
        result=_with_correlation({**result, "already_retired": False}, correlation_id)
    )
