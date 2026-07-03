from __future__ import annotations

from datetime import datetime
from typing import Any

from hermes_time import now

from .actions import HarnessAction, HarnessActionType
from .events import EventLog
from .models import Event, PersonaInstance, Task
from .persona_assignments import PersonaInstanceStore, safe_assignment_text
from .runtime_config import RuntimeConfig

CHILD_PROGRESS_MIN_INTERVAL_SECONDS = 30
_RETURN_EVENT_TYPES = {"child.returned", "child.blocked", "child.deploy_failed"}
_last_progress_emit_by_child: dict[str, datetime] = {}


def child_events_enabled(config: RuntimeConfig | None) -> bool:
    supervision = getattr(config, "supervision", None)
    return bool(getattr(supervision, "child_events_enabled", False))


def emit_child_progress(
    *,
    run,
    payload: dict[str, Any],
    config: RuntimeConfig | None,
    event_log: EventLog | None = None,
    persona_store: PersonaInstanceStore | None = None,
) -> bool:
    if not child_events_enabled(config):
        return False
    event_log = event_log or EventLog()
    persona_store = persona_store or PersonaInstanceStore(event_log=event_log)
    child = _child_instance_for_run(run, persona_store=persona_store)
    if child is None or not child.spawned_by:
        return False
    interval = max(1, int(getattr(config, "child_progress_min_interval_seconds", CHILD_PROGRESS_MIN_INTERVAL_SECONDS) or CHILD_PROGRESS_MIN_INTERVAL_SECONDS))
    if not _progress_due(child.id, interval):
        return False
    parent_node_id = safe_assignment_text(child.spawned_by, limit=160)
    child_node_id = safe_assignment_text(child.id, limit=160)
    safe_summary = _safe_text(payload.get("summary") or "Child progress update")
    event_log.append(
        Event(
            ts=now(),
            type="child.progress",
            task_id=run.task_id,
            run_id=run.id,
            persona_id=run.persona_id,
            payload={
                "parent_node_id": parent_node_id,
                "child_node_id": child_node_id,
                "phase": _safe_text(payload.get("phase") or "progress"),
                "step": _safe_text(payload.get("step") or "progress"),
                "status": _safe_text(payload.get("status") or "running"),
                "summary": safe_summary,
                "run_id": run.id,
                "stage_id": safe_assignment_text(getattr(run, "stage_id", None), limit=160),
            },
        )
    )
    return True


def emit_child_blocked(
    *,
    child_instance_id: str,
    reason: str,
    task_id: str | None = None,
    run_id: str | None = None,
    stage_id: str | None = None,
    summary: str | None = None,
    event_log: EventLog | None = None,
    persona_store: PersonaInstanceStore | None = None,
) -> bool:
    event_log = event_log or EventLog()
    persona_store = persona_store or PersonaInstanceStore(event_log=event_log)
    try:
        child = persona_store.get(safe_assignment_text(child_instance_id, limit=160))
    except Exception:
        return False
    if not child.spawned_by:
        return False
    event_log.append(
        Event(
            ts=now(),
            type="child.blocked",
            task_id=safe_assignment_text(task_id or child.current_task_id, limit=160),
            run_id=safe_assignment_text(run_id, limit=160),
            persona_id=child.persona_id,
            payload={
                "parent_node_id": safe_assignment_text(child.spawned_by, limit=160),
                "child_node_id": child.id,
                "reason": _safe_text(reason),
                "summary": _safe_text(summary or reason),
                "stage_id": safe_assignment_text(stage_id or getattr(child, "current_stage_id", None), limit=160),
                "run_id": safe_assignment_text(run_id, limit=160),
            },
        )
    )
    return True


def emit_child_deploy_failed(
    *,
    child_instance_id: str,
    reason: str,
    task_id: str | None = None,
    assignment_id: str | None = None,
    stage_id: str | None = None,
    persona_id: str | None = None,
    retryable: bool = False,
    summary: str | None = None,
    event_log: EventLog | None = None,
    persona_store: PersonaInstanceStore | None = None,
) -> bool:
    event_log = event_log or EventLog()
    persona_store = persona_store or PersonaInstanceStore(event_log=event_log)
    try:
        child = persona_store.get(safe_assignment_text(child_instance_id, limit=160))
    except Exception:
        return False
    parent_node_id = safe_assignment_text(child.spawned_by or "root", limit=160)
    event_log.append(
        Event(
            ts=now(),
            type="child.deploy_failed",
            task_id=safe_assignment_text(task_id or child.current_task_id, limit=160),
            run_id=None,
            persona_id=safe_assignment_text(persona_id or child.persona_id, limit=160),
            payload={
                "parent_node_id": parent_node_id,
                "child_node_id": safe_assignment_text(child.id, limit=160),
                "reason": _safe_text(reason),
                "summary": _safe_text(summary or reason),
                "assignment_id": safe_assignment_text(assignment_id, limit=160),
                "stage_id": safe_assignment_text(stage_id or child.current_stage_id, limit=160),
                "persona_id": safe_assignment_text(persona_id or child.persona_id, limit=160),
                "retryable": bool(retryable),
            },
        )
    )
    return True


def emit_child_returned(
    *,
    child: PersonaInstance,
    summary: str,
    proof_ids: list[str],
    artifact_refs: list[str],
    task_id: str | None,
    stage_id: str | None,
    event_log: EventLog | None = None,
) -> bool:
    if not child.spawned_by:
        return False
    log = event_log or EventLog()
    log.append(
        Event(
            ts=now(),
            type="child.returned",
            task_id=safe_assignment_text(task_id or child.current_task_id, limit=160),
            run_id=None,
            persona_id=child.persona_id,
            payload={
                "parent_node_id": safe_assignment_text(child.spawned_by, limit=160),
                "child_node_id": safe_assignment_text(child.id, limit=160),
                "summary": _safe_text(summary, limit=1200),
                "proof_ids": _bounded_refs(proof_ids),
                "artifact_refs": _bounded_refs(artifact_refs),
                "stage_id": safe_assignment_text(stage_id or getattr(child, "current_stage_id", None), limit=160),
                "persona_instance_id": safe_assignment_text(child.id, limit=160),
            },
        )
    )
    return True


def parent_child_event_wake_action(mission: Task, *, config: RuntimeConfig | None, event_log: EventLog | None = None, persona_store: PersonaInstanceStore | None = None) -> HarnessAction | None:
    if not child_events_enabled(config):
        return None
    event_log = event_log or EventLog()
    persona_store = persona_store or PersonaInstanceStore(event_log=event_log)
    goal_id = str(getattr(mission, "goal_id", None) or mission.id)
    for parent in persona_store.list_all():
        if str(getattr(parent, "goal_id", "") or "") != goal_id and str(getattr(parent, "current_task_id", "") or "") != mission.id:
            continue
        offset = max(0, int(getattr(parent, "child_events_offset", 0) or 0))
        pending, latest_offset = pending_child_events(parent.id, mission.id, offset=offset, event_log=event_log)
        if not pending:
            continue
        reason = "child status event requires parent supervision turn"
        if getattr(getattr(config, "supervision", None), "recursive_enabled", False):
            from .supervision import child_return_gate_passed

            for event in pending:
                ok, gate_reason = child_return_gate_passed(event)
                if not ok:
                    emit_child_blocked(
                        child_instance_id=str((event.payload or {}).get("child_node_id") or ""),
                        reason=f"recursive gate failed: {gate_reason}",
                        task_id=mission.id,
                        event_log=event_log,
                        persona_store=persona_store,
                    )
                    reason = "child return failed recursive gate; parent supervision turn required"
                    break
        return HarnessAction(
            HarnessActionType.RUN_SLOT,
            mission.id,
            reason=reason,
            slot_id=parent.persona_id,
            parent_node_id=parent.id,
            child_events_offset=latest_offset,
        )
    return None


def pending_child_events(parent_node_id: str, task_id: str, *, offset: int, event_log: EventLog | None = None) -> tuple[list[Event], int]:
    log = event_log or EventLog()
    pending: list[Event] = []
    latest = max(0, int(offset or 0))
    for new_offset, event in log.iter_from_offset(offset) or ():
        latest = max(latest, int(new_offset or latest))
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type in _RETURN_EVENT_TYPES and event.task_id == task_id and payload.get("parent_node_id") == parent_node_id:
            pending.append(event)
    return pending, latest


def _child_instance_for_run(run, *, persona_store: PersonaInstanceStore) -> PersonaInstance | None:
    progress = run.progress if isinstance(getattr(run, "progress", None), dict) else {}
    instance_id = safe_assignment_text(progress.get("persona_instance_id"), limit=160)
    if instance_id:
        try:
            return persona_store.get(instance_id)
        except Exception:
            pass
    candidates = [
        instance
        for instance in persona_store.list_all()
        if instance.persona_id == run.persona_id
        and (instance.active_run_id == run.id or instance.current_task_id == run.task_id or instance.goal_id == run.task_id)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda instance: instance.updated_at or instance.last_heartbeat_at or now())


def _progress_due(child_id: str, interval_seconds: int) -> bool:
    current = now()
    previous = _last_progress_emit_by_child.get(child_id)
    if previous is not None and (current - previous).total_seconds() < interval_seconds:
        return False
    _last_progress_emit_by_child[child_id] = current
    return True


def _advance_parent_offset(store: PersonaInstanceStore, parent: PersonaInstance, offset: int) -> None:
    parent.child_events_offset = max(int(getattr(parent, "child_events_offset", 0) or 0), int(offset or 0))
    store.update(parent)


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
