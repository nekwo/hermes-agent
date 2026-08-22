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
  after per-field oversize marking (a goal row is ~80 KB). An accounted degrade,
  never a silent drop: the launcher re-fetches that actor via checkpoint / rides
  the next full core.

  **The "~18 KB persona-instance row" this line used to name is gone**, and the
  correction is worth keeping because the stale figure cost a 6.5-second
  regression a full day's ownership. R2's residue slimming evicted the
  tool-detail payloads — ``tool_resolution`` / ``turn_tool_context`` /
  ``permission_state`` / ``blocked_tools``, ~97% of the row's bytes — behind a
  typed ``visibility_ref``. Measured against the operator's live roster
  (2026-08-16, 17 instances): the largest COMPLETE row is 3,012 bytes, the
  largest assembled ``{entity,id,op,changed,created}`` payload 3,133, and the
  largest single value 504 — comfortably inside the 4,096-byte cap and the
  3,584-byte per-value budget. That measurement is what unblocked D3 (the
  create-upsert below); it is re-taken by a test rather than trusted, because a
  field added to ``persona_instance_summary`` moves it.

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
import re
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

#: The persona-instance entity class, hoisted beside :data:`OFFICE_ACTOR_ENTITY`
#: so the emitters, the coverage gate and the launcher's ``_entitySection`` table
#: all name it from one place. It was three string literals until D3 gave the
#: entity a THIRD reader (the create gate in ``patch_coverage``), which is the
#: point at which a literal starts drifting silently.
PERSONA_INSTANCE_ENTITY = "persona_instance"

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
    # ── the ``open_chat`` half (office fold-promotion plan §V2, 2026-08-16) ──
    # ``persona_instance.chat_opened`` was covered with NO paired producer: the
    # bind writes real wire-visible state and emitted no ``state.patched`` at
    # all, so covering the event without these rows would silently drop every
    # field below from a connected client for the rest of its session. Each maps
    # exactly as ``persona_instance_summary`` derives it (the golden in
    # ``test_state_patches.py`` is the drift fence).
    "workspace_id": ("workspace_id",),
    "realm_id": ("realm_id",),
    # One store field, three wire names: the summary projects ``profile_id`` (or
    # the visibility persona's ``hermes_profile``) into all three.
    "profile_id": ("profile_id", "backing_profile", "source_profile_id"),
    # ``default_chat_session_id`` is the SOLE authority; ``chat_session_id`` and
    # ``session_id`` are its read-compatible mirrors on the wire row. The store's
    # own ``session_id`` mirror maps to the same trio, so whichever field name a
    # chokepoint names in its diff, the client folds all three consistently — a
    # patch that moved one and not the others would leave a v1 consumer reading a
    # session the row no longer points at.
    #
    # STATED AFTER MUTATION, because the pair is deliberately redundant and a
    # reader should not mistake that for coverage: ``open_chat`` always writes
    # BOTH store fields from the same value, so collapsing EITHER entry to its
    # own name alone stays green — the sibling entry still carries the trio.
    # Neither is dead; each is the one that answers when the other's store field
    # did not move, and a future chokepoint that writes only one is exactly the
    # case this shape exists for.
    "default_chat_session_id": ("default_chat_session_id", "chat_session_id", "session_id"),
    "session_id": ("default_chat_session_id", "chat_session_id", "session_id"),
    # Always diffable, and that is load-bearing rather than cosmetic: a bind that
    # moved only ``chat_head_home`` (which has no wire field) would otherwise
    # project an EMPTY ``changed`` → no patch → a covered ``chat_opened`` event
    # riding alone in an otherwise-coverable batch, i.e. a promoted frame whose
    # patches list omits the row that moved.
    "updated_at": ("updated_at",),
}


#: The end-to-end correlation key (Plan D / EG-2.3). ONE name, reused from the
#: slot ``stream._delta_entity`` already surfaces — so a producer that places it
#: into an event payload rides BOTH frame kinds with zero wire changes: the delta
#: lane lifts it to ``entity.correlation_id`` (``stream.py:319``) and the patch
#: lane spreads the whole payload into the row (``stream.py:436``).
CORRELATION_ID_KEY = "correlation_id"

#: Boundary cap, mirroring the idempotency-key cap discipline
#: (``persona_chat_mints``). A correlation id is a GENERATED token, never
#: operator text, so 64 characters is generous rather than tight.
CORRELATION_ID_MAX_LEN = 64

#: The token charset. Deliberately narrow — the id is minted by a client
#: (`g-<lane>-<micros>-<rand4>`), so anything outside this is either a bug or an
#: attempt to smuggle free text through a diagnostic field. Refused at the RPC
#: boundary and dropped here; never sanitized into a different token, because a
#: repaired id would print a value neither side used.
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]+$")


def normalize_correlation_id(value: Any) -> str | None:
    """``value`` as a legal correlation token, or ``None``.

    THE payload-side fence. Every producer path funnels through here, so an
    illegal token can never reach an event payload no matter which caller
    threaded it: the RPC boundary REFUSES a bad id out loud (a client bug is
    worth a refusal), and this drops it silently for the in-process callers that
    have no channel to be told on (``agent_create``'s own 200-char validator is
    looser than this cap, so its tokens are re-checked here rather than trusted).

    ``None`` in, ``None`` out — which is what keeps every payload without a
    gesture behind it byte-identical to before this key existed.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > CORRELATION_ID_MAX_LEN:
        return None
    if not _CORRELATION_ID_RE.match(token):
        return None
    return token


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


def _assemble(
    entity: str,
    entity_id: str,
    op: str,
    changed: dict[str, Any] | None,
    created: bool | None = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"entity": str(entity), "id": str(entity_id), "op": str(op)}
    if changed is not None:
        payload["changed"] = changed
    if created is not None:
        payload["created"] = bool(created)
    if correlation_id is not None:
        payload[CORRELATION_ID_KEY] = str(correlation_id)
    return payload


def build_state_patch(
    entity: str,
    entity_id: str,
    op: str = PATCH_OP_UPSERT,
    changed: dict[str, Any] | None = None,
    created: bool | None = None,
    correlation_id: str | None = None,
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

    ``created`` is an ADDITIVE optional key (office fold-promotion plan §V4,
    2026-08-16) meaning "this row did not exist before the write, so a fold must
    INSERT it rather than merge onto an absent target". It is carried inside the
    existing 4 KB accounting — the shrink loop re-measures the assembled payload
    including it, so a create can never overflow the cap by the width of a
    boolean. It rides only where a producer states it; ``None`` keeps the payload
    byte-identical to before this key existed, which is what makes every fielded
    reader's fixed key-set read unaffected.

    It is deliberately NOT part of the fold itself: the launcher's office fold
    inserts-on-absent unconditionally. The key exists so
    :func:`~agent_runtime.patch_coverage.event_is_patch_coverable` can GATE the
    widened op behind the ``office_actor_lifecycle`` capability token, i.e. so an
    un-updated client is never PROMOTED a row its fold would answer with a
    re-hydrate.

    ``correlation_id`` is the SECOND additive optional key, on exactly the
    ``created`` pattern and for the same reason it can be (EG-2.3 / Plan D §V2):
    a payload key past required-field validation is free, and absent-when-unset
    keeps every existing payload byte-identical. It is carried INSIDE the shrink
    loop's accounting below, so a gesture token can never overflow the cap by the
    width of a 64-character string — the loop re-measures the assembled payload
    with it present and marks one more value if it has to.

    Note the ``refresh`` degrades keep the id. A refresh says *this row is not
    expressible, re-fetch it*, and "which gesture caused the re-fetch" is the
    single most valuable thing to know about a demote, so the id is the one key a
    degrade must not drop.
    """

    if op != PATCH_OP_UPSERT or not changed:
        return _assemble(
            entity,
            entity_id,
            PATCH_OP_REFRESH if op == PATCH_OP_UPSERT else op,
            None,
            # A ``refresh`` degrade is no longer a create-shaped row: the client
            # refetches, so telling it the row was new would be a claim about a
            # patch that no longer carries one. ``remove`` keeps the marker,
            # because the lifecycle gate reads the op there instead.
            created if op != PATCH_OP_UPSERT else None,
            correlation_id,
        )

    safe_changed: dict[str, Any] = {}
    for field_name, value in changed.items():
        size = _value_bytes(value)
        safe_changed[str(field_name)] = _oversize_marker(size) if size > PATCH_VALUE_BUDGET_BYTES else value

    payload = _assemble(entity, entity_id, PATCH_OP_UPSERT, safe_changed, created, correlation_id)
    while _value_bytes(payload) > EVENT_PAYLOAD_LIMIT_BYTES:
        inline = [(name, val) for name, val in safe_changed.items() if not _is_oversize_marker(val)]
        if not inline:
            # Nothing left to shrink and the payload still overflows — degrade the
            # whole patch to an accounted ``refresh`` (the launcher re-fetches this
            # actor via checkpoint) rather than ship a marker-only merge it cannot
            # fold with fidelity.
            return _assemble(entity, entity_id, PATCH_OP_REFRESH, None, None, correlation_id)
        name = max(inline, key=lambda item: (_value_bytes(item[1]), item[0]))[0]
        safe_changed[name] = _oversize_marker(_value_bytes(safe_changed[name]))
        payload = _assemble(entity, entity_id, PATCH_OP_UPSERT, safe_changed, created, correlation_id)
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
    created: bool | None = None,
    correlation_id: str | None = None,
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

    ``created`` is the additive lifecycle marker — see :func:`build_state_patch`.
    ``correlation_id`` is the gesture token, normalized HERE so no caller can put
    an illegal one on the wire (see :func:`normalize_correlation_id`).
    """

    if op == PATCH_OP_UPSERT and not changed:
        return False
    if not delta_patches_enabled(config):
        return False
    payload = build_state_patch(
        entity, entity_id, op, changed, created, normalize_correlation_id(correlation_id)
    )
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
    # ``persona_instance_summary`` derives all three profile names from this one
    # value (``instance.profile_id or visibility_persona.hermes_profile``); the
    # standin's fallback is None, mirrored here by ``getattr(persona, ...)``.
    profile_id = instance.profile_id or getattr(persona, "hermes_profile", None)
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
        # The ``open_chat`` fields. Same rule as everything above: these are the
        # WIRE names and the WIRE derivations, not the store attributes.
        "workspace_id": instance.workspace_id,
        "realm_id": instance.realm_id,
        "profile_id": profile_id,
        "backing_profile": profile_id,
        "source_profile_id": profile_id,
        "default_chat_session_id": instance.default_chat_session_id,
        "chat_session_id": instance.default_chat_session_id,
        "session_id": instance.default_chat_session_id,
        "updated_at": instance.updated_at,
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
        entity=PERSONA_INSTANCE_ENTITY,
        entity_id=instance.id,
        op=PATCH_OP_UPSERT,
        changed=changed,
        task_id=getattr(instance, "current_task_id", None),
        run_id=getattr(instance, "active_run_id", None),
        persona_id=getattr(instance, "persona_id", None),
        config=config,
    )


def project_persona_instance_full_wire_row(instance: Any) -> dict[str, Any]:
    """The COMPLETE persona-instance wire row — the exact row a full core holds.

    This is the create half of the projection pair, and it is deliberately a
    different mechanism from :func:`project_persona_instance_wire_fields` above.
    That one reproduces ``persona_instance_summary``'s derived-field logic
    field-for-field (held to it by a golden) because it only ever needs the
    SUBSET a steer/profile write moved, and reproducing a subset read-only is
    cheaper than building the whole row. A create has no subset: the client holds
    no row at all, so the patch must carry every key the rebuild would, and the
    only thing that can promise that is the rebuild's own builder. So this one
    CALLS ``persona_instance_summary`` — the same structural-parity argument
    :func:`project_office_actor_wire_row` makes, and the stronger of the two: a
    field added to the summary reaches the create patch in the same commit and
    cannot drift out of it.

    The persona is resolved READ-ONLY through :func:`_resolve_persona_for`, and
    handed to the summary rather than left for it to look up — the same resolution
    ``snapshot.py`` performs (``personas_by_id.get(persona_id)``), so a profile
    instance whose persona is not a stored agent resolves to ``None`` HERE too and
    falls into the summary's own ``_profile_visibility_persona`` standin exactly
    as the full rebuild does. Byte-parity with the core needs that fallback to
    stay in play, not to be routed around.

    ``profile_readiness`` is not threaded: ``snapshot.py`` passes it, but it feeds
    only ``tool_resolution``'s ``profile_readiness``/``..._summary`` keys, and the
    summary row copies none of those out — it reads ``permission_mode``,
    ``mutation_boundary``, ``final_tool_count``, ``blocked_tools`` and
    ``effective_toolsets``. Omitting it costs one recomputation inside
    ``resolve_tool_visibility`` and moves no wire byte; the parity golden in
    ``test_state_patches.py`` is what holds that claim.
    """

    from .agent_create_phases import timed_create_subphase
    from .persona_assignments import persona_instance_summary

    # W3-H1: the projection alone, so the create receipt can separate it from
    # the ``state.patched`` append that ``emit_persona_instance_create`` performs
    # around it. Free for every other caller — see ``timed_create_subphase``.
    with timed_create_subphase("wire_row_ms"):
        return persona_instance_summary(instance, _resolve_persona_for(instance))


def emit_persona_instance_create(
    event_log: EventLog,
    instance: Any,
    *,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit the CREATE patch for a brand-new persona instance: a complete-row
    ``upsert`` stamped ``created: true`` — or an honest ``refresh`` if that row
    cannot be carried losslessly (D3, 2026-08-16).

    Why this exists. ``open_chat``'s create arm emitted ``op: refresh``, and one
    unfoldable row demotes its whole batch, so every Mission Office "add an
    agent" gesture took the perfectly foldable ``office_actor created:true``
    upsert down with it and paid a full ``build_snapshot()`` — measured at 6.3–6.6
    s of a 6.94 s gesture (plan §10.1). The refresh was not a measurement, it was
    a deferral: the ~18 KB figure in this module's header predates the R2
    residue slimming that evicted the tool-detail payloads (~97% of the row)
    behind ``visibility_ref``. Measured on the operator's live roster, 2026-08-16:
    17 instances, worst assembled payload 3,133 bytes against the 4,096-byte cap,
    worst single value 504 bytes against the 3,584-byte per-value budget.

    Why the degrade is checked HERE rather than left to
    :func:`build_state_patch`'s shrink loop. That loop is right for a SUBSET
    upsert: marking one oversize value still ships a merge the launcher can apply,
    with the marked field accounted and refetched. For a CREATE it would be a
    lie — the launcher INSERTS this row wholesale, so a marker would become the
    inserted row's value for that field, i.e. a fabricated roster row rather than
    an accounted degrade. So a create is all-or-nothing: any marker, or any
    degrade the loop already made, and the whole patch becomes the ``refresh``
    that was the pre-D3 behaviour. That keeps the worst case exactly today's
    wire — a full core — for a roster row that outgrows the cap, instead of a
    silently corrupt insert.

    Dark (and projection-free) unless the flag is on.
    """

    if not delta_patches_enabled(config):
        return False
    row = project_persona_instance_full_wire_row(instance)
    payload = build_state_patch(
        PERSONA_INSTANCE_ENTITY, instance.id, PATCH_OP_UPSERT, row, True
    )
    lossless = payload.get("op") == PATCH_OP_UPSERT and not any(
        _is_oversize_marker(value)
        for value in (payload.get("changed") or {}).values()
    )
    if not lossless:
        logger.warning(
            "persona-instance create patch degraded to refresh: the projected "
            "row for %s does not fit the %d-byte payload cap losslessly — the "
            "batch takes a full core (D3, plan §10.3)",
            instance.id,
            EVENT_PAYLOAD_LIMIT_BYTES,
        )
        return emit_state_patch(
            event_log,
            entity=PERSONA_INSTANCE_ENTITY,
            entity_id=instance.id,
            op=PATCH_OP_REFRESH,
            persona_id=getattr(instance, "persona_id", None),
            config=config,
        )
    return emit_state_patch(
        event_log,
        entity=PERSONA_INSTANCE_ENTITY,
        entity_id=instance.id,
        op=PATCH_OP_UPSERT,
        changed=row,
        created=True,
        task_id=getattr(instance, "current_task_id", None),
        run_id=getattr(instance, "active_run_id", None),
        persona_id=getattr(instance, "persona_id", None),
        config=config,
    )


def emit_persona_instance_remove(
    event_log: EventLog,
    instance: Any,
    *,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit a persona-instance ``remove`` (the instance left the active frame —
    a close / task-terminal fan-out). Dark unless the flag is on.

    Takes no ``reason``: ``emit_state_patch`` has no field to carry one, so the
    kwarg this used to accept was computed by its caller and discarded. The
    WHY travels on the paired domain event, which does have a place for it."""

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=PERSONA_INSTANCE_ENTITY,
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
#   20-second read cache does not write either. EG-3.1 did NOT change this: the
#   persisted read-model core serve now writes and reads lives somewhere else
#   entirely (under ``<store_root>/serve_read_model/``, in a generation directory
#   that ``agent_runtime.core_cache`` publishes through a pointer file — MCF-21.
#   The filenames are deliberately not respelled here: ``core_cache.core_path``
#   and ``sidecar_path`` are their one authority, and a second copy of a layout
#   is a thing that goes stale), deliberately not this one — ``snapshot.json``
#   has a consumer of its own in the launcher's cold-paint lane, and one file
#   with two writers of different provenance could not say which produced it.
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
    either half: ``serde.safe_id`` and ``paths.safe_path_token`` both
    keep only ``alnum`` plus ``_.:-`` and rewrite everything else to ``_``. Note
    ``:`` survives that filter and so could not have been used. Split on the
    FIRST ``/`` — single authority, mirrored by the launcher's fold.
    """

    return f"{workspace_id}/{actor_key}"


def emit_office_actor_patch(
    event_log: EventLog,
    actor: Any,
    *,
    created: bool = False,
    correlation_id: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit an office-actor ``upsert`` carrying the actor's COMPLETE wire row.

    Complete, not a changed-field subset, and that is deliberate: the store has
    no per-field office write — ``upsert_actor`` rewrites the whole actor file
    from a whole payload — so a subset would be an invention of the patch lane,
    and the whole row measures 663–764 bytes against the live canvas (four
    actors, 2026-08-14), an order of magnitude inside the 3584-byte per-value
    budget. It is also what makes the fold a plain row replace — and, since the
    2026-08-16 fold-promotion plan, what makes INSERT-on-absent safe: a client
    that does not hold this actor can materialize it from the patch alone,
    because there is nothing about the row the patch omits.

    ``created=True`` marks a write whose row was ABSENT before it (a first
    placement, or a re-add resurrecting an archived key — absent from the
    client's list either way). It changes nothing about the fold; it is the
    coverage gate's input, so the widened op is promoted only at a client that
    declared ``office_actor_lifecycle``.

    **What still is NOT expressible here.** The office core section is
    workspace-keyed with a nested actor list whose parent row carries derived
    state — ``actor_count``, ``actors_truncated``, ``archived_actor_keys``,
    ``folders``, the SURFACE's own ``revision``/``updated_at``. The
    2026-08-16 validation (§V1) established that under the two LIFECYCLE ops the
    first three are exactly derivable by a client from the rows it folds, so they
    no longer need a wire row of their own. ``folders`` and the surface
    ``revision`` are moved only by ``update_surface``, which has carried its own
    :func:`emit_office_surface_patch` row since 2026-08-16 (WV-H3) — so those
    two are no longer un-expressible either, they are simply somebody else's
    row; ``updated_at`` drift under an ACTOR write is still accepted and
    documented, because no actor write moves the surface's copy of it.
    The one case that remains genuinely inexpressible is TRUNCATION — past
    ``MAX_OFFICE_ACTORS_PROJECTED`` the projected list is a cut the client cannot
    reproduce — and that is what :func:`emit_office_actor_refresh` is now for,
    and all it is for.
    """

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=OFFICE_ACTOR_ENTITY,
        entity_id=office_actor_patch_id(actor.workspace_id, actor.actor_key),
        op=PATCH_OP_UPSERT,
        changed=project_office_actor_wire_row(actor),
        created=True if created else None,
        correlation_id=correlation_id,
        persona_id=getattr(actor, "persona_id", None),
        config=config,
    )


def emit_office_actor_remove(
    event_log: EventLog,
    workspace_id: Any,
    actor_key: Any,
    *,
    correlation_id: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit an office-actor ``remove`` — the archive half of the lifecycle pair.

    Carries no ``changed`` by contract, so it is always tiny and can never hit
    the oversize ladder. The fold splices the actor out by key, recomputes the
    derived container counts and appends the key to its mirrored
    ``archived_actor_keys`` ledger — the client-side derivation §V1 established,
    which is why archiving finally has a foldable op at all.

    Idempotent by construction on the client side (remove-if-present), which is
    what lets the same row replay across the stream lane and the office push lane
    without the two colliding.

    Best-effort at its call site like every sibling emitter: a patch-lane fault
    is a missing PROMOTION, never a failed archive.
    """

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=OFFICE_ACTOR_ENTITY,
        entity_id=office_actor_patch_id(workspace_id, actor_key),
        op=PATCH_OP_REMOVE,
        correlation_id=correlation_id,
        config=config,
    )


OFFICE_SURFACE_ENTITY = "office_surface"


def office_patch_scope(patch: Any) -> str | None:
    """The office workspace one ``state.patched`` row belongs to, or ``None``.

    THE SCOPE AUTHORITY for the office push lane, and it lives here — beside the
    id BUILDERS — because a scope parser is the id scheme read backwards, and the
    two had drifted (operator task #57).

    What the drift cost. ``serve_office_subscriptions.office_patch_sink`` carried
    a private restatement of this rule that knew only ``office_actor`` and its
    slash-prefixed id. When WV-H3 (2026-08-16) widened what may PROMOTE to
    include ``office_surface`` — whose id is the BARE workspace id, no slash — a
    folder-only batch became coverable, was fanned to the sink, and failed both
    conjuncts of that private predicate: no patch, no resync, the change dropped
    on a lane whose own docstring says "a resync is recoverable; a dropped change
    is not". It survived only because a mixed batch (any actor row) admits the
    whole frame under forward-whole, and because the argv ``harness stream`` child
    still folded the same batch for the launcher.

    So both readers on that lane now call THIS, and a batch the coverage authority
    promotes is by construction either forwarded or resynced.

    The two id shapes, and why one function can hold both:

    * ``office_actor`` — ``"<workspace_id>/<actor_key>"``. Split on the FIRST
      ``/`` exactly as :func:`office_actor_patch_id` joins on it (that separator
      is the one character neither half can contain, so the split is total). An id
      with no separator names no workspace and answers ``None`` rather than
      guessing; this is also what keeps ``ws_pilot_2`` out of ``ws_pilot``'s
      scope, where a naive ``startswith`` on the bare id would have leaked it.
    * ``office_surface`` — the bare workspace id
      (:func:`emit_office_surface_patch`), so the id IS the scope.

    ``None`` for every other entity, for a malformed row, and for an office row
    whose id cannot be placed. ``None`` is NOT "every workspace": a
    ``persona_instance`` row is real state at its watermark but it moves nothing a
    one-workspace office projection holds, which is why the office lane forwards
    such a row inside an in-scope frame and never lets it put a frame in scope.
    """

    if not isinstance(patch, dict):
        return None
    entity = patch.get("entity")
    entity_id = patch.get("id")
    if not isinstance(entity_id, str) or not entity_id:
        return None
    if entity == OFFICE_ACTOR_ENTITY:
        workspace_id, separator, _actor_key = entity_id.partition("/")
        if not separator or not workspace_id:
            return None
        return workspace_id
    if entity == OFFICE_SURFACE_ENTITY:
        return entity_id
    return None


#: EXACTLY the office-row fields ``update_surface`` moves — the whole content of
#: an ``office_surface`` patch, and the client's merge allowlist.
#:
#: Named here rather than written inline so the producer and the derivability
#: argument in :func:`emit_office_surface_patch` cannot drift apart, and so a
#: future field addition is a visible edit to a constant rather than a quiet
#: extra key in a dict literal. The launcher mirrors this set and RESYNCS on
#: anything outside it, which is what makes widening it a cross-stack change
#: needing its own capability token.
OFFICE_SURFACE_PATCH_FIELDS: tuple[str, ...] = ("folders", "revision", "updated_at")


def emit_office_surface_patch(
    event_log: EventLog,
    surface: Any,
    *,
    correlation_id: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Emit an ``office_surface`` ``upsert`` — the folder-taxonomy write's row.

    A SUBSET merge, like ``persona_instance``'s and unlike its ``office_actor``
    sibling's complete-row replace. The reason is the shape of what it patches:
    ``office_actor`` addresses a row the store rewrites whole, so a replace is
    faithful; this addresses the office row itself, which also carries the
    actor list, the derived ``actor_count``/``actors_truncated``, and the
    ``conflict_actor_keys``/``archived_actor_keys`` ledgers. Those belong to the
    ACTOR lifecycle folds and to the client-side derivation §V1 established, and
    a complete-row patch from here would clobber every one of them on a folder
    rename.

    So this carries :data:`OFFICE_SURFACE_PATCH_FIELDS` and nothing else, and
    the launcher merges exactly those three keys.

    Why this write can be covered at all
    ------------------------------------
    The §V1 derivability standard: an event is coverable only when nothing is
    left that ONLY the demoted full core could say. ``update_surface`` moves
    three things — ``folders``, the surface's own ``revision``, and
    ``updated_at`` — and this row carries all three verbatim. It moves no actor
    row, no count, and neither key ledger (``_normalize_folders`` and the
    revision bump are the entire mutation; ``office_store.update_surface``
    touches nothing else). Nothing is dropped because nothing is left over.

    Tiny by construction, so the oversize ladder is unreachable in practice:
    ``folders`` is at most ``MAX_FOLDERS`` (64) names of at most 80 characters
    (``office_store._safe_folder``), an order of magnitude inside the 3584-byte
    per-value budget. If it ever were not, :func:`build_state_patch`'s existing
    accounting degrades the whole patch to ``refresh`` and the batch demotes as
    it does today — an honest re-fetch, never a partial merge onto an office row
    whose folder list the client would then hold half of.

    NOT emitted from ``ensure_surface``. A create authors a surface the client
    has never held, and this subset is not a whole office row — a fold would
    answer it ``patch_without_target`` and re-hydrate, which is strictly worse
    than the full core a create already takes. ``office.surface.created``
    therefore stays uncovered, deliberately.

    Best-effort at its call site like every sibling emitter: a patch-lane fault
    is a missing PROMOTION, never a failed folder write.
    """

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=OFFICE_SURFACE_ENTITY,
        entity_id=str(getattr(surface, "workspace_id", "") or ""),
        op=PATCH_OP_UPSERT,
        changed={
            "folders": list(getattr(surface, "folders", []) or []),
            "revision": getattr(surface, "revision", None),
            "updated_at": to_jsonable(getattr(surface, "updated_at", None)),
        },
        correlation_id=correlation_id,
        config=config,
    )


def emit_office_surface_refresh(
    event_log: EventLog,
    workspace_id: Any,
    *,
    correlation_id: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """The accounted degrade for an office SURFACE write no fold can express:
    the whole surface left (``OfficeStore.archive_orphaned_surface``, EG-0.1).

    Same instrument, same reason as :func:`emit_office_actor_refresh` one entity
    up. Archiving an orphaned surface removes the ``offices`` row AND every
    ``office_actor`` row under it in one move; ``OFFICE_SURFACE_PATCH_FIELDS`` is
    a three-key folder subset and there is no remove-a-surface op on this wire,
    so nothing here is expressible as a fold. ``refresh``'s documented meaning
    applies unchanged: *this row is not expressible, re-fetch it*.

    WHY THIS AND NOT A NEW DOMAIN EVENT TYPE. The archive rides the existing
    ``office.surface.updated`` (``change="archived"``), which is a COVERED domain
    event — it promises a folding client that an equivalent ``office_surface``
    patch rides the same batch. Left alone that promise is a silent data loss:
    ``batch_is_patch_coverable`` is an ``all(...)`` with no "at least one patch"
    requirement, so a batch whose only entry is that covered event ships a patch
    frame with an EMPTY ``patches`` list — the client advances its watermark
    having folded nothing and keeps the archived surface, and its
    ``orphaned_office`` chip, forever. That is verbatim the failure
    :func:`emit_office_actor_refresh` documents for the actor lane.

    Registering a NEW uncovered event type would also have worked, and was
    rejected: it moves ``decision_contract_hash``, which is baked into the
    committed producer-derived golden fixtures, making a tests-only stage-zero
    landing a cross-stack fixture regeneration. This entity/op pair already
    exists on the wire and needs no client negotiation — a client that declares
    ``office_surface`` demotes on the ``refresh``, and one that does not demotes
    on the domain event. Both land on the full core, which is the refetch.
    """

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=OFFICE_SURFACE_ENTITY,
        entity_id=str(workspace_id or ""),
        op=PATCH_OP_REFRESH,
        correlation_id=correlation_id,
        config=config,
    )


def emit_office_actor_refresh(
    event_log: EventLog,
    workspace_id: Any,
    actor_key: Any,
    *,
    correlation_id: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """The accounted degrade for an office write the client cannot place: the
    actor list is TRUNCATED.

    **This is the only remaining meaning.** It used to carry a second, unrelated
    one — "a SECOND row changed and this lane has no vocabulary for it" (a
    create moved ``actor_count``, a re-add rewrote the resurrection ledger) —
    and that conflation is retired by the 2026-08-16 fold-promotion plan (§V3):
    those writes now emit real ``upsert``/``remove`` ops and the container state
    is derived client-side. What survives is ``refresh``'s documented meaning
    everywhere else in this module: *this row is not expressible as a fold,
    re-fetch it*.

    The surviving producer is ``OfficeStore._emit_actor_patch``'s
    ``> MAX_OFFICE_ACTORS_PROJECTED`` guard. Past that bound the snapshot
    projects a CUT of the actor list, and which actors survive the cut is not
    client-decidable — so neither the row's presence nor the derived counts can
    be folded, and a full core is the honest answer.

    ``refresh`` is not in :data:`FOLDABLE_PATCH_OPS`, so the batch carrying it
    demotes to a full core — which IS the refetch. Emitting this rather than
    simply staying silent is what makes the degrade visible in the log: a silent
    skip would leave the paired (covered) ``office.actor.upserted`` as the only
    entry in an otherwise-coverable batch, which would ship a patch frame with an
    EMPTY ``patches`` list — the launcher would advance its watermark having
    folded nothing and keep the pre-write row forever.
    """

    if not delta_patches_enabled(config):
        return False
    return emit_state_patch(
        event_log,
        entity=OFFICE_ACTOR_ENTITY,
        entity_id=office_actor_patch_id(workspace_id, actor_key),
        op=PATCH_OP_REFRESH,
        correlation_id=correlation_id,
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

