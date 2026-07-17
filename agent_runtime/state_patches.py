"""S6 producer: state-carrying field-patch log entries (flagged, dark).

Read-model workstream Stage S6, producer half. When ``read_model.delta_patches``
is on, a store *chokepoint* that mutates a keyed entity emits a ``state.patched``
EventLog entry carrying a field-level patch::

    {"entity": "<class>", "id": "<actor_id>", "changed": {"<field>": <new_value>}}

The ``seq``/``ts`` come from the EventLog's own envelope (the stream assigns the
sequence; ``Event.ts`` is the timestamp) — the payload carries only the patch.
The launcher (a fold-only materialized view) applies each patch to its keyed
table ``Map<id, Entity>``. That consumer fold and the stream/wire changes are
OUT of this stage's scope: S6 producer is **log-only**.

Sizing. The payload must fit the hard ``EVENT_PAYLOAD_LIMIT_BYTES = 4096`` cap
``EventLog.append`` enforces. A ``changed`` value whose serialized size would
overflow is replaced by an **accounted** ``{"oversize": true, "bytes": N}``
marker — never dropped silently — so the consumer knows to fall back to a
checkpoint fetch for that actor. If many mid-sized fields together overflow, the
largest still-inline values are marked deterministically until the payload fits.

Inertness. With the flag off (default), :func:`emit_state_patch` returns before
any append and never mutates the log — the diff is provably inert. Config-load
failures also degrade to "off" (never take a store mutation down), mirroring the
observe-and-warn posture of the EventLog contract validator.
"""

from __future__ import annotations

import json
from typing import Any

from hermes_time import now

from .config import AgentRuntimeConfig, load_agent_runtime_config
from .events import EVENT_PAYLOAD_LIMIT_BYTES, EventLog
from .models import Event
from .serde import to_jsonable

STATE_PATCHED_EVENT_TYPE = "state.patched"

# Headroom reserved for the ``{entity, id, changed:{...}}`` scaffold plus the
# field keys, so a patch assembled from within-budget values still clears the
# hard 4096-byte payload cap. A single field value that serializes larger than
# this per-value budget is replaced by an accounted oversize marker.
PATCH_ENVELOPE_HEADROOM_BYTES = 512
PATCH_VALUE_BUDGET_BYTES = EVENT_PAYLOAD_LIMIT_BYTES - PATCH_ENVELOPE_HEADROOM_BYTES


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


def _assemble(entity: str, entity_id: str, changed: dict[str, Any]) -> dict[str, Any]:
    return {"entity": str(entity), "id": str(entity_id), "changed": changed}


def build_state_patch(entity: str, entity_id: str, changed: dict[str, Any]) -> dict[str, Any]:
    """Build a ``state.patched`` payload ``{entity, id, changed}``, sized to fit
    the 4 KB EventLog cap.

    A field value larger than :data:`PATCH_VALUE_BUDGET_BYTES` is replaced by an
    accounted ``{oversize: true, bytes: N}`` marker. If the assembled payload
    still exceeds :data:`EVENT_PAYLOAD_LIMIT_BYTES` (many mid-sized fields, none
    of which individually tripped the per-value budget), the largest still-inline
    values are marked until it fits — deterministically (largest-first, ties
    broken by field name) so the same input always yields the same patch.
    """

    safe_changed: dict[str, Any] = {}
    for field_name, value in changed.items():
        size = _value_bytes(value)
        safe_changed[str(field_name)] = _oversize_marker(size) if size > PATCH_VALUE_BUDGET_BYTES else value

    payload = _assemble(entity, entity_id, safe_changed)
    while _value_bytes(payload) > EVENT_PAYLOAD_LIMIT_BYTES:
        inline = [(name, val) for name, val in safe_changed.items() if not _is_oversize_marker(val)]
        if not inline:
            # Nothing left to shrink (entity/id alone overflow) — let append raise
            # honestly rather than ship a malformed patch. Not reachable for real
            # rows; the headroom budget keeps entity/id well under the cap.
            break
        name = max(inline, key=lambda item: (_value_bytes(item[1]), item[0]))[0]
        safe_changed[name] = _oversize_marker(_value_bytes(safe_changed[name]))
        payload = _assemble(entity, entity_id, safe_changed)
    return payload


def delta_patches_enabled(config: AgentRuntimeConfig | None = None) -> bool:
    """Whether the S6 producer lane is on (``read_model.delta_patches``).

    Default False — the producer is dark until the launcher fold exists. A
    config-load failure degrades to False so a broken config never takes a store
    mutation down (observe-and-warn posture)."""

    cfg = config
    if cfg is None:
        try:
            cfg = load_agent_runtime_config()
        except Exception:
            return False
    read_model = getattr(cfg, "read_model", None)
    return bool(getattr(read_model, "delta_patches", False))


def emit_state_patch(
    event_log: EventLog,
    *,
    entity: str,
    entity_id: str,
    changed: dict[str, Any],
    task_id: str | None = None,
    run_id: str | None = None,
    persona_id: str | None = None,
    config: AgentRuntimeConfig | None = None,
) -> bool:
    """Append one ``state.patched`` entry for a chokepoint mutation, gated by
    ``read_model.delta_patches`` (default off → no-op, provably inert).

    Returns True when an entry was appended, False when the flag is off or
    ``changed`` is empty. Log-only: this appends to the EventLog; the stream/wire
    fold is out of S6-producer scope.
    """

    if not changed:
        return False
    if not delta_patches_enabled(config):
        return False
    payload = build_state_patch(entity, entity_id, changed)
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
