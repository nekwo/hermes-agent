from __future__ import annotations

import re
import time
from collections.abc import Iterator
from typing import Any

from hermes_time import now

from .daemon import daemon_status_schema
from .events import EventLog
from .models import Event
from .serde import to_jsonable
from .snapshot import build_snapshot

STREAM_SCHEMA_VERSION = 1

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b((?:[A-Za-z0-9]+_)*(?:SECRET|TOKEN|PASSWORD|PASS|CREDENTIAL|API_?KEY|KEY)(?:_[A-Za-z0-9]+)*)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)


def hydrate_frame(snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the initial warm-stream hydrate frame.

    The hydrate carries the existing full snapshot as the read model payload so
    the stream is additive: the one-shot snapshot remains the canonical fallback
    and consumers can converge by applying this frame exactly like a fresh
    snapshot response.
    """

    snap = snapshot if snapshot is not None else build_snapshot()
    parity = snap.get("parity") if isinstance(snap.get("parity"), dict) else {}
    watermark = parity.get("watermark") if isinstance(parity.get("watermark"), dict) else {}
    return {
        "type": "hydrate",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": snap.get("generated_at") or now(),
        "watermark": dict(watermark or {}),
        "identity_map": _identity_map(snap),
        "core": snap,
        "completeness": parity.get("completeness") or {},
        "drops": parity.get("drops") or [],
        "parity_warnings": parity.get("warnings") or [],
    }


def heartbeat_frame(*, offset: int) -> dict[str, Any]:
    """Liveness frame; additionally carries the daemon status block.

    The daemon's per-loop status writes go to daemon_status.json, not the
    EventLog, so an idle daemon emits no deltas — without this block a
    stream consumer's runtime HUD freezes exactly while the daemon idles.
    The block is read-model telemetry: consumers merge it fire-and-forget
    and a dropped frame only ages the HUD, never runtime state.
    """

    frame = {
        "type": "heartbeat",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": now(),
        "watermark": {"event_offset": int(offset or 0), "captured_at": now()},
    }
    try:
        frame["daemon"] = to_jsonable(daemon_status_schema())
    except Exception:
        # Heartbeats are pure liveness; a corrupt status file must not
        # break the stream. Consumers treat a missing block as "no update".
        pass
    return frame


def delta_frame(event: Event, *, offset: int, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = _redaction_safe_json(event.payload)
    return {
        "type": "delta",
        "schema_version": STREAM_SCHEMA_VERSION,
        "generated_at": now(),
        "watermark": {
            "event_offset": int(offset or 0),
            "last_event_ts": event.ts,
            "captured_at": now(),
        },
        "seq": int(offset or 0),
        "op": _delta_op(event),
        "entity": {
            "event": {
                **to_jsonable(event),
                "payload": payload,
            },
            "task_id": event.task_id,
            "goal_id": event.task_id,
            "run_id": event.run_id,
            "persona_id": event.persona_id,
            "session_id": event.session_id,
            "correlation_id": payload.get("correlation_id") if isinstance(payload, dict) else None,
        },
        "core": snapshot if snapshot is not None else build_snapshot(),
    }


def stream_frames(
    *,
    event_log: EventLog | None = None,
    poll_interval_seconds: float = 0.25,
    heartbeat_interval_seconds: float = 5.0,
    max_frames: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield hydrate, delta, and heartbeat frames for ``hermes harness stream``."""

    log = event_log or EventLog()
    emitted = 0
    hydrate = hydrate_frame()
    offset = int(((hydrate.get("watermark") or {}).get("event_offset")) or 0)
    yield hydrate
    emitted += 1
    if max_frames is not None and emitted >= max_frames:
        return

    last_heartbeat = time.monotonic()
    while True:
        emitted_delta = False
        for next_offset, event in log.iter_from_offset(offset):
            offset = int(next_offset)
            yield delta_frame(event, offset=offset)
            emitted += 1
            emitted_delta = True
            last_heartbeat = time.monotonic()
            if max_frames is not None and emitted >= max_frames:
                return

        if not emitted_delta and time.monotonic() - last_heartbeat >= heartbeat_interval_seconds:
            yield heartbeat_frame(offset=offset)
            emitted += 1
            last_heartbeat = time.monotonic()
            if max_frames is not None and emitted >= max_frames:
                return

        time.sleep(max(0.01, float(poll_interval_seconds)))


def _delta_op(event: Event) -> str:
    event_type = str(event.type or "")
    if event_type.startswith("run.tool.") or event_type == "run.progress":
        return "chat.trace.appended"
    if event_type.startswith("task."):
        return "task.state_changed" if event_type in {"task.transition", "task.blocked", "task.unblocked", "task.cancelled", "task.archived"} else "task.upserted"
    if event_type == "proof.attached":
        return "proof.added"
    if event_type.startswith("incident."):
        return event_type
    if event_type.startswith("daemon."):
        return "daemon.status"
    if event_type.startswith("persona_assignment."):
        return "instance.upserted"
    return "event.appended"


def _identity_map(snapshot: dict[str, Any]) -> dict[str, str]:
    identity: dict[str, str] = {}
    for instance in snapshot.get("persona_instances") or []:
        if not isinstance(instance, dict):
            continue
        canonical = _first_text(instance, "persona_instance_id", "instance_id", "id")
        if not canonical:
            continue
        for key in ("persona_instance_id", "instance_id", "id", "agent_profile_id"):
            alias = _text(instance.get(key))
            if alias:
                identity[alias] = canonical
        persona_id = _text(instance.get("persona_id"))
        if persona_id and persona_id.startswith("profile:"):
            identity[persona_id.replace(":", "_")] = persona_id
    for channel in snapshot.get("operator_channels") or []:
        if not isinstance(channel, dict):
            continue
        canonical = _first_text(channel, "persona_instance_id", "channel_id", "id")
        if not canonical:
            continue
        for key in ("persona_instance_id", "channel_id", "id", "session_id"):
            alias = _text(channel.get(key))
            if alias:
                identity[alias] = canonical
    return identity


def _redaction_safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redaction_safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redaction_safe_json(item) for item in value[:200]]
    if isinstance(value, tuple):
        return [_redaction_safe_json(item) for item in value[:200]]
    if isinstance(value, str):
        return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    return to_jsonable(value)


def _first_text(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        text = _text(payload.get(key))
        if text:
            return text
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
