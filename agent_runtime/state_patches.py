"""S7-A producer: op-based, WIRE-LEVEL state-patch log entries (flagged, dark).

Read-model workstream Stage S7-A, producer half. When ``read_model.delta_patches``
is on, a store *chokepoint* that mutates a keyed entity emits a ``state.patched``
EventLog entry carrying a **wire-level op**::

    {"entity": "<class>", "id": "<actor_id>", "op": "upsert", "changed": {...}}
    {"entity": "<class>", "id": "<actor_id>", "op": "remove"}
    {"entity": "<class>", "id": "<actor_id>", "op": "refresh"}

The design move (plan §S7-A). S6 shipped RAW store-field patches
(``changed: {model: "x"}``) which the launcher could not fold with fidelity —
the wire rows carry hermes-PROJECTED derived fields (``effective_model``,
``model_is_override``, ``reasoning_supported``, ``skills``, the
``agent_profile_display_name`` mirror, …) that a raw field-merge would leave
stale. S7-A fixes it at the source: at emit time hermes projects the changed
entity **through the exact per-entity projection ``snapshot.py`` uses** and the
patch carries the projected WIRE fields (op ``upsert``) or a removal (op
``remove``). The launcher folds the projected fields verbatim — one authority
(hermes projects; the launcher never re-derives), fidelity trivial for every
covered field, no per-field allowlist.

* ``upsert`` — ``changed`` is the projected wire fields the mutation affected
  (the changed store fields' wire projections PLUS the derived wire fields that
  depend on them, recomputed). The launcher merges ``changed`` into
  ``Map<id, row>[id]`` (creating the row if absent).
* ``remove`` — the actor left the live frame (an open-only incident closed; a
  persona instance closed / task-terminal fan-out). The launcher deletes the row.
* ``refresh`` — the projected payload does not fit the 4 KB EventLog cap even
  after per-field oversize marking (a full persona-instance row is ~18 KB; a goal
  row ~80 KB). An accounted degrade, never a silent drop: the launcher re-fetches
  that actor via checkpoint / rides the next full core.

The ``seq``/``ts`` come from the EventLog's own envelope (the stream assigns the
sequence; ``Event.ts`` is the timestamp) — the payload carries only the op.

Sizing. The payload must fit the hard ``EVENT_PAYLOAD_LIMIT_BYTES = 4096`` cap
``EventLog.append`` enforces. For an ``upsert`` a ``changed`` value whose
serialized size would overflow is first replaced by an **accounted**
``{"oversize": true, "bytes": N}`` marker; if the assembled payload STILL
overflows, the whole patch degrades to ``op: "refresh"`` (dropping ``changed``)
— accounted, never a partial merge the launcher cannot vouch for.

Inertness. With the flag off (default), :func:`emit_state_patch` and the
per-entity emitters return before any projection or append and never mutate the
log — the diff is provably inert, and the (moderate) projection cost is paid only
when the lane is live. Config-load failures also degrade to "off" (never take a
store mutation down), mirroring the observe-and-warn posture of the EventLog
contract validator.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from hermes_time import now

from .config import AgentRuntimeConfig, load_root_runtime_config
from .events import EVENT_PAYLOAD_LIMIT_BYTES, EventLog
from .models import Event
from .serde import to_jsonable

STATE_PATCHED_EVENT_TYPE = "state.patched"

#: Wire ops (plan §S7-A). ``upsert`` carries projected wire fields to merge;
#: ``remove`` deletes the keyed row; ``refresh`` is the accounted oversize/too-big
#: degrade → the launcher re-fetches that actor from a checkpoint.
PATCH_OP_UPSERT = "upsert"
PATCH_OP_REMOVE = "remove"
PATCH_OP_REFRESH = "refresh"

#: Ops the launcher can fold in place (a covered batch may contain only these).
#: ``refresh`` is NOT foldable — it forces a full core / checkpoint refetch.
FOLDABLE_PATCH_OPS: frozenset[str] = frozenset({PATCH_OP_UPSERT, PATCH_OP_REMOVE})

# Headroom reserved for the ``{entity, id, op, changed:{...}}`` scaffold plus the
# field keys, so a patch assembled from within-budget values still clears the
# hard 4096-byte payload cap. A single field value that serializes larger than
# this per-value budget is replaced by an accounted oversize marker.
PATCH_ENVELOPE_HEADROOM_BYTES = 512
PATCH_VALUE_BUDGET_BYTES = EVENT_PAYLOAD_LIMIT_BYTES - PATCH_ENVELOPE_HEADROOM_BYTES

#: Per persona-instance STORE field → the WIRE fields it projects to. A steer or
#: profile mutation names the store attributes it wrote; this maps each to the
#: projected wire fields (itself + every derived dependent) so the ``upsert``
#: carries the recomputed derived values, not just the raw field. Kept beside the
#: producer because it MUST track ``persona_instance_summary`` — the projection it
#: selects from — and the launcher folds whatever wire fields arrive (no allowlist).
_PERSONA_INSTANCE_STORE_TO_WIRE: dict[str, tuple[str, ...]] = {
    "steered_by": ("steered_by",),
    "spawned_by": ("spawned_by",),
    "goal_id": ("goal_id",),
    "mode": ("mode", "lifecycle_mode"),
    # S70 (contract 54) dropped the ``attached_task_id`` alias from the full
    # snapshot row; it leaves the patch lane in the same wave. A patch that kept
    # projecting it would ADD a key the full rebuild no longer has — the launcher
    # folds whatever wire fields arrive with no allowlist, so the two lanes would
    # disagree about the row's shape after the first incremental update.
    "current_task_id": ("current_task_id",),
    "display_name": ("display_name", "agent_profile_display_name"),
    "current_chat_goal": ("current_chat_goal",),
    "skill_overrides": ("skill_overrides", "skills"),
    "model": ("model", "effective_model", "model_is_override", "reasoning_supported"),
    "provider": ("provider", "effective_provider", "model_is_override"),
    "api_mode": ("api_mode",),
    "reasoning_effort": ("reasoning_effort", "model_is_override", "reasoning_supported"),
}


def _value_bytes(value: Any) -> int:
    """Serialized byte size of ``value`` under the exact encoding
    :meth:`EventLog.append` measures the payload with."""

    return len(json.dumps(to_jsonable(value), ensure_ascii=False).encode("utf-8"))


def _oversize_marker(byte_count: int) -> dict[str, Any]:
    return {"oversize": True, "bytes": int(byte_count)}


def _is_oversize_marker(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("oversize") is True
        and set(value.keys()) == {"oversize", "bytes"}
    )


def _assemble(entity: str, entity_id: str, op: str, changed: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"entity": str(entity), "id": str(entity_id), "op": str(op)}
    if changed is not None:
        payload["changed"] = changed
    return payload


def build_state_patch(
    entity: str,
    entity_id: str,
    op: str = PATCH_OP_UPSERT,
    changed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an op-based ``state.patched`` payload, sized to fit the 4 KB cap.

    * ``remove`` / ``refresh`` carry no ``changed`` → always tiny.
    * ``upsert`` carries the projected ``changed`` fields. A field value larger
      than :data:`PATCH_VALUE_BUDGET_BYTES` is replaced by an accounted
      ``{oversize: true, bytes: N}`` marker; if the assembled payload still
      exceeds :data:`EVENT_PAYLOAD_LIMIT_BYTES` (many mid-sized fields), the
      largest still-inline values are marked deterministically (largest-first,
      ties broken by field name). If it STILL overflows (or every value is an
      oversize marker), the whole patch degrades to ``op: "refresh"`` — an
      accounted "re-fetch this actor", never a partial merge.
    """

    if op != PATCH_OP_UPSERT or not changed:
        return _assemble(entity, entity_id, PATCH_OP_REFRESH if op == PATCH_OP_UPSERT else op, None)

    safe_changed: dict[str, Any] = {}
    for field_name, value in changed.items():
        size = _value_bytes(value)
        safe_changed[str(field_name)] = _oversize_marker(size) if size > PATCH_VALUE_BUDGET_BYTES else value

    payload = _assemble(entity, entity_id, PATCH_OP_UPSERT, safe_changed)
    while _value_bytes(payload) > EVENT_PAYLOAD_LIMIT_BYTES:
        inline = [(name, val) for name, val in safe_changed.items() if not _is_oversize_marker(val)]
        if not inline:
            # Nothing left to shrink and the payload still overflows — degrade the
            # whole patch to an accounted ``refresh`` (the launcher re-fetches this
            # actor via checkpoint) rather than ship a marker-only merge it cannot
            # fold with fidelity.
            return _assemble(entity, entity_id, PATCH_OP_REFRESH, None)
        name = max(inline, key=lambda item: (_value_bytes(item[1]), item[0]))[0]
        safe_changed[name] = _oversize_marker(_value_bytes(safe_changed[name]))
        payload = _assemble(entity, entity_id, PATCH_OP_UPSERT, safe_changed)
    return payload


def delta_patches_enabled(config: AgentRuntimeConfig | None = None) -> bool:
    """Whether the S7-A producer lane is on (``read_model.delta_patches``).

    Default False — the producer is dark until the launcher fold exists. A
    config-load failure degrades to False so a broken config never takes a store
    mutation down (observe-and-warn posture)."""

    cfg = config
    if cfg is None:
        try:
            cfg = load_root_runtime_config()
        except Exception:
            return False
    read_model = getattr(cfg, "read_model", None)
    return bool(getattr(read_model, "delta_patches", False))


def emit_state_patch(
    event_log: EventLog,
    *,
    entity: str,
    entity_id: str,
    op: str = PATCH_OP_UPSERT,
    changed: dict[str, Any] | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    persona_id: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Append one op-based ``state.patched`` entry, gated by
    ``read_model.delta_patches`` (default off → no-op, provably inert).

    Returns True when an entry was appended, False when the flag is off or an
    ``upsert`` carried an empty ``changed``. Log-only: this appends to the
    EventLog; the stream promotes coverable batches to v2 ``patch`` frames.
    """

    if op == PATCH_OP_UPSERT and not changed:
        return False
    if not delta_patches_enabled(config):
        return False
    payload = build_state_patch(entity, entity_id, op, changed)
    event_log.append(
        Event(
            ts=now(),
            type=STATE_PATCHED_EVENT_TYPE,
            task_id=task_id,
            run_id=run_id,
            persona_id=persona_id,
            payload=payload,
        )
    )
    return True


# --------------------------------------------------------------------------- #
# Per-entity wire projection (mirrors snapshot.py's projection, side-effect-free)
# --------------------------------------------------------------------------- #
def project_persona_instance_wire_fields(instance: Any, changed_store_fields: Iterable[str]) -> dict[str, Any]:
    """Project the wire fields a persona-instance store mutation affected.

    Computes exactly the wire fields ``persona_instance_summary`` produces for
    the changed store fields (its derived-field logic reproduced field-for-field
    — a golden in ``test_state_patches.py`` asserts byte-parity against
    ``persona_instance_summary`` for the live rows, so it can never drift). This
    is a **read-only** derivation on purpose: routing through
    ``persona_instance_summary`` for a profile instance would fall into
    ``_profile_visibility_persona`` → ``ensure_persisted_personas``, which SEEDS
    the agent store and emits a stray ``persona.updated`` into the mutation's own
    event batch (demoting a coverable batch to a full core). The persona is
    resolved read-only from ``AgentStore().list_all()``,
    neither of which writes); a profile instance whose persona is absent yields
    the same ``model=None / provider=None / skills=overrides`` fallback the
    visibility-persona standin does.
    """

    persona = _resolve_persona_for(instance)
    row = _persona_instance_wire_row(instance, persona)
    wire_fields: dict[str, Any] = {}
    for store_field in changed_store_fields:
        for wire_field in _PERSONA_INSTANCE_STORE_TO_WIRE.get(store_field, (store_field,)):
            if wire_field in row:
                wire_fields[wire_field] = row[wire_field]
    return wire_fields


def _persona_instance_wire_row(instance: Any, persona: Any) -> dict[str, Any]:
    """The subset of ``persona_instance_summary`` fields a steer/profile mutation
    can touch, derived read-only. ``persona`` is the backing agent (or None for a
    profile instance → ``model``/``provider`` default None, ``skills`` fall back
    to the instance overrides), mirroring the summary's
    ``getattr(visibility_persona, ...)`` exactly."""

    from .persona_assignments import _model_supports_reasoning_effort

    overrides = instance.skill_overrides
    persona_model = getattr(persona, "model", None)
    persona_provider = getattr(persona, "provider", None)
    persona_skills = list(getattr(persona, "skills", []) or [])
    effective_model = instance.model or persona_model
    return {
        "steered_by": list(instance.steered_by),
        "spawned_by": instance.spawned_by,
        "goal_id": instance.goal_id,
        "mode": instance.mode,
        "lifecycle_mode": instance.mode,
        "current_task_id": instance.current_task_id,
        "display_name": instance.display_name,
        "agent_profile_display_name": instance.display_name,
        "current_chat_goal": instance.current_chat_goal,
        "skill_overrides": list(overrides) if overrides is not None else None,
        "skills": list(overrides) if overrides is not None else persona_skills,
        "model": instance.model,
        "effective_model": effective_model,
        "provider": instance.provider,
        "effective_provider": instance.provider or persona_provider,
        "api_mode": instance.api_mode,
        "reasoning_effort": instance.reasoning_effort,
        "model_is_override": bool(instance.model or instance.provider or instance.reasoning_effort),
        "reasoning_supported": _model_supports_reasoning_effort(effective_model),
    }


def _resolve_persona_for(instance: Any) -> Any:
    """The backing persona for ``instance``, resolved READ-ONLY (never seeds) and
    exactly as ``snapshot.py`` resolves it — so the projected derived fields match
    a full rebuild. A profile instance whose persona isn't a stored agent resolves
    to None (the wire-row derivation then uses the visibility-persona fallback
    values). Best-effort: any resolution failure falls back to None."""

    try:
        from .store import AgentStore

        persona_id = str(getattr(instance, "persona_id", "") or "")
        personas = AgentStore().list_all()
        return {p.id: p for p in personas}.get(persona_id)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Chokepoint emitters (op-based; each dark unless the flag is on)
# --------------------------------------------------------------------------- #
def emit_persona_instance_patch(
    event_log: EventLog,
    instance: Any,
    changed_store_fields: Iterable[str],
    *,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit a persona-instance ``upsert`` for the wire fields a steer/profile
    write affected. Dark (and projection-free) unless the flag is on."""

    fields = list(changed_store_fields)
    if not fields:
        return False
    if not delta_patches_enabled(config):
        return False
    changed = project_persona_instance_wire_fields(instance, fields)
    if not changed:
        return False
    return emit_state_patch(
        event_log,
        entity="persona_instance",
        entity_id=instance.id,
        op=PATCH_OP_UPSERT,
        changed=changed,
        task_id=getattr(instance, "current_task_id", None),
        run_id=getattr(instance, "active_run_id", None),
        persona_id=getattr(instance, "persona_id", None),
        config=config,
    )


def emit_persona_instance_remove(
    event_log: EventLog,
    instance: Any,
    *,
    reason: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit a persona-instance ``remove`` (the instance left the active frame —
    a close / task-terminal fan-out). Dark unless the flag is on."""

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity="persona_instance",
        entity_id=instance.id,
        op=PATCH_OP_REMOVE,
        task_id=getattr(instance, "current_task_id", None),
        persona_id=getattr(instance, "persona_id", None),
        config=config,
    )


# S54 removed ``emit_task_refresh``: the task-refresh patch op, orphaned since
# the ``Task`` record went at S8.
#
# S66 removed ``emit_incident_remove`` for the same reason one wave later: its
# only chokepoint was ``IncidentStore.close``, which S65 retired when the store
# became a historical reader. The paired ``incident.closed`` domain event was
# de-registered in that same wave, so nothing could produce either half. See
# ``patch_coverage.HISTORICAL_COVERED_DOMAIN_EVENT_TYPES``, which now names the
# fold-classifier entries that outlived their producers instead of leaving them
# indistinguishable from live ones.

