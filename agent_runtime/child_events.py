"""Chat-graph child return events retained after mission-task removal."""

from __future__ import annotations

from typing import Any

from hermes_time import now

from .events import EventLog
from .models import Event, PersonaInstance
from .persona_assignments import safe_assignment_text


def emit_child_returned(
    *,
    child: PersonaInstance,
    summary: str,
    proof_ids: list[str],
    artifact_refs: list[str],
    event_log: EventLog | None = None,
) -> bool:
    """Append a bounded return event to a child instance's steering parent."""

    if not child.spawned_by:
        return False
    log = event_log or EventLog()
    log.append(
        Event(
            ts=now(),
            type="child.returned",
            task_id=None,
            run_id=None,
            persona_id=child.persona_id,
            payload={
                "parent_node_id": safe_assignment_text(child.spawned_by, limit=160),
                "child_node_id": safe_assignment_text(child.id, limit=160),
                "summary": _safe_text(summary, limit=1200),
                "proof_ids": _bounded_refs(proof_ids),
                "artifact_refs": _bounded_refs(artifact_refs),
                "persona_instance_id": safe_assignment_text(child.id, limit=160),
            },
        )
    )
    return True


def _bounded_refs(values: list[str]) -> list[str]:
    refs: list[str] = []
    for value in values:
        safe = safe_assignment_text(value, limit=120)
        if safe and safe not in refs:
            refs.append(safe)
        if len(refs) >= 8:
            break
    return refs


def _safe_text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "api_key", "bearer")):
        return "[redacted]"
    if ":/" in text or "\\" in text:
        return "[redacted-path]"
    return text[:limit]
