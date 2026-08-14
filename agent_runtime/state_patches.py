"""S7-A producer: op-based, WIRE-LEVEL state-patch log entries (flagged, shipped on).

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

Inertness. With the flag off, :func:`emit_state_patch` and the per-entity
emitters return before any projection or append and never mutate the log — the
diff is provably inert, and the (moderate) projection cost is paid only when the
lane is live. Config-load failures also degrade to "off" (never take a store
mutation down), mirroring the observe-and-warn posture of the EventLog contract
validator — but they now WARN, because an unannounced off is the failure mode
this lane already lost its whole life to.

The flag SHIPS ON as of 2026-08-14
(:data:`agent_runtime.runtime_config.SHIPPED_DELTA_PATCHES`); off is now an
operator's explicit ROOT-config ``false``, or an accounted fault.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from hermes_time import now

from .config import AgentRuntimeConfig, load_root_runtime_config
from .events import EVENT_PAYLOAD_LIMIT_BYTES, EventLog
from .models import Event
from .runtime_config import FALLBACK_DELTA_PATCHES, SHIPPED_DELTA_PATCHES
from .serde import to_jsonable

logger = logging.getLogger(__name__)

#: Sentinel for "this config object carried no ``delta_patches`` at all", which
#: must not collapse into the same answer as an operator's explicit ``False``.
_UNRESOLVED = object()

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


def _root_config_fault() -> tuple[str, str] | None:
    """``(what, detail)`` when the ROOT ``config.yaml`` is unreadable, else None.

    This exists because the loader CANNOT report the difference. Every
    ``agent_runtime`` config read goes through
    ``parse_cache.cached_yaml_file(path, default=None)``, and
    :func:`parse_cache.cached_by_mtime` returns that ``default`` for a loader
    exception just as it does for a missing file. So a ``config.yaml`` full of
    unparseable YAML does not raise out of
    :func:`config.load_root_runtime_config` — it silently produces an EMPTY
    config, and every key in it resolves to its shipped default.

    That is tolerable for a knob whose default is "off": absent and broken agree.
    It is NOT tolerable once a default ships ON, because the two cases stop
    agreeing and the loader answers the wrong one — a broken config would silently
    ACTIVATE a lane the operator may have written ``false`` for, in the very file
    the runtime just failed to read. Measured directly: before this probe existed,
    a root ``config.yaml`` containing invalid YAML resolved
    ``delta_patches_enabled()`` to ``True``, saying nothing.

    Absence is deliberately NOT a fault. A runtime root with no ``config.yaml``
    is a FRESH root — the case the shipped default is for — and it must resolve
    ON, silently.

    Cost on the happy path is zero: this hits the same ``(path, mtime, size)``
    cache entry ``load_agent_runtime_config`` is about to use.
    """

    from .config import harness_root_config_path
    from .parse_cache import cached_yaml_file

    try:
        path = harness_root_config_path()
        if not path.is_file():
            return None  # fresh root — the shipped default's whole purpose
        loaded = cached_yaml_file(path, default=_UNRESOLVED)
    except Exception as exc:  # resolving/statting the root is itself a fault
        return ("could not be examined", f"{type(exc).__name__}: {exc}")
    if loaded is _UNRESOLVED:
        return ("exists but did not parse", str(path))
    if loaded is not None and not isinstance(loaded, dict):
        return ("parsed to a non-mapping", f"{type(loaded).__name__} at {path}")
    return None


def delta_patches_enabled(config: AgentRuntimeConfig | None = None) -> bool:
    """Whether the S7-A producer lane is on (``read_model.delta_patches``).

    Shipped ON (:data:`runtime_config.SHIPPED_DELTA_PATCHES`) since 2026-08-14 —
    silence in the ROOT ``config.yaml`` resolves to the lane being LIVE, so a
    fresh clone against a fresh runtime root patches instead of re-shipping an
    822 KB core per field change. An operator's explicit ``false`` still wins:
    ``config._read_model_config`` falls back to the shipped default only for an
    ABSENT key.

    The flag is root-only, so ``config=None`` resolves through
    :func:`config.load_root_runtime_config` and never through the sticky-active
    profile. The three NON-OBSERVATIONS degrade to
    :data:`runtime_config.FALLBACK_DELTA_PATCHES` (off) rather than to the
    shipped default, and all three WARN:

    * the root ``config.yaml`` EXISTS but did not parse into a mapping — see
      :func:`_root_config_fault`, and read its header before trusting any other
      "config fault" reasoning in this module;
    * the load raised outright (``harness_root_config_path`` itself failing, an
      import error, …). A broken config must never take a store mutation down
      (observe-and-warn), and it must not be read as an instruction either;
    * the resolved config carries no ``read_model.delta_patches`` at all — which
      a real :class:`AgentRuntimeConfig` never does, only a stub or a
      partially-built object, i.e. the caller told us nothing.

    An ABSENT root config is NOT a fault — it is a fresh runtime root, the exact
    case the shipped default exists for, and it resolves ON.

    All three warn because an UNANNOUNCED off is the failure this default
    retires. The lane going dark is worth exactly one log line, and had one
    existed the 2026-08-13 misplacement would not have gone its whole life
    unnoticed.
    """

    cfg = config
    if cfg is None:
        fault = _root_config_fault()
        if fault is not None:
            logger.warning(
                "delta-patch lane OFF: the root runtime config %s (%s) — "
                "read_model.delta_patches ships on (%s), but a config the "
                "runtime cannot read is not an instruction; the stream falls "
                "back to full-core deltas until it parses",
                fault[0],
                fault[1],
                SHIPPED_DELTA_PATCHES,
            )
            return FALLBACK_DELTA_PATCHES
        try:
            cfg = load_root_runtime_config()
        except Exception as exc:
            logger.warning(
                "delta-patch lane OFF: could not load the root runtime config "
                "(%s: %s) — read_model.delta_patches ships on (%s); the stream "
                "falls back to full-core deltas until the config parses",
                type(exc).__name__,
                exc,
                SHIPPED_DELTA_PATCHES,
            )
            return FALLBACK_DELTA_PATCHES
    read_model = getattr(cfg, "read_model", None)
    resolved = getattr(read_model, "delta_patches", _UNRESOLVED)
    if resolved is _UNRESOLVED:
        logger.warning(
            "delta-patch lane OFF: the resolved runtime config carries no "
            "read_model.delta_patches (config object %s) — the stream falls "
            "back to full-core deltas",
            type(cfg).__name__,
        )
        return FALLBACK_DELTA_PATCHES
    return bool(resolved)


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
    ``read_model.delta_patches`` (shipped ON; flag off → no-op, provably inert).

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


def project_office_actor_wire_row(actor: Any) -> dict[str, Any]:
    """The office-actor WIRE row, produced by the SNAPSHOT's own row builder.

    Unlike the persona-instance projection above — which reproduces
    ``persona_instance_summary``'s derived-field logic field-for-field and is
    held to it by a golden — this one CALLS ``snapshot._office_actor_summary_row``
    directly. It can, because that builder is already pure (a field copy off an
    ``OfficeActor`` plus the caller-supplied ``unpublished``): there is no
    ``_profile_visibility_persona`` equivalent to seed a store or emit a stray
    domain event into the mutation's own batch. Byte-parity with a full rebuild
    is therefore STRUCTURAL rather than asserted, which is the stronger of the
    two — a field added to the summary row reaches the patch lane in the same
    commit and cannot drift out of it.

    ``unpublished`` is the one field the builder does not compute, and it MUST be
    recomputed here rather than left to the fold's merge: a drag changes the
    actor's content hash, so a realm-bound actor flips ``unpublished`` False→True
    on the same write. A patch that omitted the key would leave the launcher's
    "unpublished" badge showing the pre-drag answer for the rest of the session —
    the exact stale-derived-field failure S6→S7-A was rewritten to end. The
    derivation mirrors ``snapshot._offices_summary``: no realm behind the
    workspace → no baseline → the key is OMITTED (not False), because "this
    workspace does not publish" and "this actor is published" are different facts.
    """

    from .snapshot import _office_actor_summary_row

    return _office_actor_summary_row(actor, unpublished=_office_actor_unpublished(actor))


def _office_actor_unpublished(actor: Any) -> bool | None:
    """``True``/``False`` for a realm-bound actor, ``None`` (→ key omitted) when
    the workspace has no realm or the lookup fails.

    Mirrors ``snapshot._offices_summary``'s ``_actor_unpublished`` closure,
    including its archived-workspace behaviour: that function builds its
    workspace→realm map from ``WorkspaceStore.list_all()``, which excludes
    archived workspaces, so an actor under an archived workspace resolves to
    ``None`` there and must resolve to ``None`` here.

    Best-effort by design: a publication-honesty flag must never be able to take
    an office write down, and ``None`` degrades to the same key-omitted shape a
    non-realm workspace produces.
    """

    try:
        from .office_models import office_content_hash
        from .office_sync import read_office_baseline
        from .store import WorkspaceStore

        workspace = WorkspaceStore().get(str(getattr(actor, "workspace_id", "") or ""))
        if getattr(workspace, "archived", False):
            return None
        realm_id = getattr(workspace, "realm_id", None)
        if not realm_id:
            return None
        baseline = read_office_baseline(str(realm_id))
        key = f"{actor.workspace_id}:actor:{actor.actor_key}"
        return baseline.get(key) != office_content_hash(actor)
    except Exception:
        return None


# What an office patch INVALIDATES on disk: nothing — and that is a measured
# fact about a lane NOBODY owns, not a choice this leg made.
#
# The launcher paints from a disk-cached snapshot at boot before authoritative
# truth arrives. That cache is ``paths.snapshot_path()`` (``snapshot.json`` in
# the store root), and the question "what happens to it when a patch folds" has
# a flat answer: it is not touched, it cannot be touched from here, and it was
# already going stale before this leg existed.
#
# * The ONLY writer is :func:`snapshot.write_snapshot`, reached from exactly one
#   production call site — ``read_model.resolve_snapshot_frame``, i.e. the
#   ``harness snapshot`` CLI verb. The stream lane calls ``build_snapshot()``
#   and never ``write_snapshot``; a ``harness snapshot`` answered from serve's
#   20-second read cache does not write either.
# * The launcher has NO writer at all — it only reads the file — and a folded
#   core never reaches it (``MissionReadModel.commitFold`` mutates memory only).
#   The cache is also never used as a fold BASE, so it can neither corrupt nor
#   be corrected by this lane.
# * The launcher's cached-boot lane gates on CONTRACT SHAPE, not freshness: no
#   TTL, no age check, no watermark comparison. A ``snapshot.json`` written
#   weeks ago paints on boot if it parses.
#
# So the staleness the office push leg is accused of creating is pre-existing
# and lane-wide: every persona-instance patch since S7-A, and every full-core
# stream delta too, has left that file untouched. This leg does not widen the
# window; it makes reaching it cheaper, because cheap patches flow more often.
#
# What this leg therefore does NOT do, deliberately: invalidate or rewrite
# ``snapshot.json`` from the office chokepoint. Doing so would put a
# cross-process file mutation on a drag's hot path, and deleting it would take
# the boot-paint fast path away from every surface to fix one section of it.
#
# WHAT REMAINS OPEN, and it is not this slice's to close: the cached boot lane
# has no defined staleness bound and no receipt saying which it painted. The
# fix is one of (a) a freshness gate on the cached read — the file's
# ``generated_at`` is already in it — or (b) a boot receipt naming the cache's
# age, the same shape that made the READ leg verifiable. Whoever owns the
# cached-boot lane must take it; nothing in the read, write, or push leg can.
# Note the office is the one section where a client CAN already detect its own
# staleness unaided: ``runtime.office.get`` puts the actor ``revision`` on every
# item, so a cached canvas can be diffed against server truth for ~2.5 KB.

OFFICE_ACTOR_ENTITY = "office_actor"


def office_actor_patch_id(workspace_id: Any, actor_key: Any) -> str:
    """The patch identity for one office actor: ``"<workspace_id>/<actor_key>"``.

    An ``actor_key`` alone is NOT an identity. Actor files live at
    ``office/<workspace_id>/actors/<token>.json``, so uniqueness is per-workspace
    by construction and two workspaces may legitimately hold the same key — a
    bare key on the wire would address whichever one the fold happened to find.

    ``/`` is the separator because it is the one character that CANNOT appear in
    either half: ``office_store._safe_id`` and ``paths._safe_path_token`` both
    keep only ``alnum`` plus ``_.:-`` and rewrite everything else to ``_``. Note
    ``:`` survives that filter and so could not have been used. Split on the
    FIRST ``/`` — single authority, mirrored by the launcher's fold.
    """

    return f"{workspace_id}/{actor_key}"


def emit_office_actor_patch(
    event_log: EventLog,
    actor: Any,
    *,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit an office-actor ``upsert`` carrying the actor's COMPLETE wire row.

    Complete, not a changed-field subset, and that is deliberate: the store has
    no per-field office write — ``upsert_actor`` rewrites the whole actor file
    from a whole payload — so a subset would be an invention of the patch lane,
    and the whole row measures 663–764 bytes against the live canvas (four
    actors, 2026-08-14), an order of magnitude inside the 3584-byte per-value
    budget. It is also what lets the fold be a plain row replace.

    **Emitted ONLY for a write that changed nothing outside this actor's row.**
    The office core section is workspace-keyed with a nested actor list, and the
    parent row carries derived state — ``actor_count``, ``actors_truncated``,
    ``folders``, ``archived_actor_keys``, the SURFACE's own ``revision`` and
    ``updated_at``. A create moves ``actor_count``; a re-add of an archived key
    rewrites the surface's resurrection ledger; an archive moves both. None of
    those are expressible as an actor-row patch, so the chokepoint calls
    :func:`emit_office_actor_refresh` for them instead and the batch takes the
    honest full-core lane. See ``OfficeStore.upsert_actor``.
    """

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=OFFICE_ACTOR_ENTITY,
        entity_id=office_actor_patch_id(actor.workspace_id, actor.actor_key),
        op=PATCH_OP_UPSERT,
        changed=project_office_actor_wire_row(actor),
        persona_id=getattr(actor, "persona_id", None),
        config=config,
    )


def emit_office_actor_refresh(
    event_log: EventLog,
    workspace_id: Any,
    actor_key: Any,
    *,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """The accounted degrade for an office write that moved the SURFACE row too.

    ``refresh`` is not in :data:`FOLDABLE_PATCH_OPS`, so the batch carrying it
    demotes to a full core — which IS the refetch. Emitting this rather than
    simply staying silent is what makes the degrade visible in the log: a silent
    skip would leave the paired (covered) ``office.actor.upserted`` as the only
    entry in an otherwise-coverable batch, which would ship a patch frame with an
    EMPTY ``patches`` list — the launcher would advance its watermark having
    folded nothing and keep the pre-write surface row forever.
    """

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=OFFICE_ACTOR_ENTITY,
        entity_id=office_actor_patch_id(workspace_id, actor_key),
        op=PATCH_OP_REFRESH,
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

