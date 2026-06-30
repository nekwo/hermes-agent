from __future__ import annotations

from typing import Any

from hermes_time import now

from .events import EventLog
from .models import Event


MAX_WORKLOG_TEXT = 1400


def append_persona_worklog(
    *,
    task_id: str,
    persona_id: str,
    message: str,
    event_log: EventLog | None = None,
    run_id: str | None = None,
    stage_id: str | None = None,
    source: str = "agent",
    kind: str = "progress",
    related_proof_ids: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Event:
    clean_message = " ".join(str(message or "").split())[:MAX_WORKLOG_TEXT]
    if not clean_message:
        clean_message = "No observable worklog message was emitted."
    payload: dict[str, Any] = {
        "kind": str(kind or "progress")[:64],
        "source": str(source or "agent")[:64],
        "message": clean_message,
        "stage_id": stage_id,
        "related_proof_ids": list(related_proof_ids or [])[:8],
    }
    if metadata:
        payload["metadata"] = {str(k)[:64]: _safe_value(v) for k, v in metadata.items()}
    evt = Event(ts=now(), type="persona.worklog", task_id=task_id, run_id=run_id, persona_id=persona_id, payload=payload)
    (event_log or EventLog()).append(evt)
    return evt


def persona_worklog_for_task(
    task_id: str,
    *,
    persona_id: str | None = None,
    limit: int = 50,
    event_log: EventLog | None = None,
) -> list[Event]:
    events = (event_log or EventLog()).for_task(task_id, limit=0)
    result = [evt for evt in events if evt.type == "persona.worklog" and (persona_id is None or evt.persona_id == persona_id)]
    if limit > 0:
        result = result[-limit:]
    return result


def _safe_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(k)[:64]: _safe_value(v) for k, v in list(value.items())[:20]}
    return str(value)

