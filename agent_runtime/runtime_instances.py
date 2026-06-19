from __future__ import annotations

import uuid
from dataclasses import replace
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .events import EventLog
from .models import Event, GoalRuntimeInstance
from .serde import from_jsonable, to_jsonable

FOREGROUND_LANE = "foreground"
BACKGROUND_LANE = "background"
ACTIVE_STATE = "active"
PARKED_STATE = "parked"
TERMINAL_STATE = "terminal"
WAITING_STATE = "waiting"
LANE_STATES = frozenset(
    {
        "queued",
        "activating",
        "running",
        "parked_by_budget",
        "parked_by_repo_lock",
        "parked_by_operator",
        "blocked",
        "done",
        "archiving",
        "archived",
        "failed_runtime",
        ACTIVE_STATE,
        PARKED_STATE,
        TERMINAL_STATE,
        WAITING_STATE,
    }
)
_ALLOWED_TRANSITIONS = {
    "queued": {"activating", "parked_by_operator", "failed_runtime"},
    "activating": {"running", "parked_by_repo_lock", "parked_by_budget", "blocked", "failed_runtime"},
    "running": {"parked_by_budget", "parked_by_repo_lock", "parked_by_operator", "blocked", "done", "failed_runtime"},
    "parked_by_budget": {"running", "failed_runtime", "archived"},
    "parked_by_repo_lock": {"running", "failed_runtime", "archived"},
    "parked_by_operator": {"running", "archived", "failed_runtime"},
    "blocked": {"running", "failed_runtime", "archived"},
    "done": {"archiving"},
    "archiving": {"archived"},
    ACTIVE_STATE: {"running", PARKED_STATE, TERMINAL_STATE, "parked_by_operator", "done", "failed_runtime"},
    PARKED_STATE: {"running", ACTIVE_STATE, TERMINAL_STATE, "parked_by_operator", "archived"},
    WAITING_STATE: {"running", ACTIVE_STATE, PARKED_STATE, TERMINAL_STATE, "blocked"},
    TERMINAL_STATE: {"archived"},
}


class GoalRuntimeInstanceStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def create_foreground(self, *, task_id: str, started_by: str = "cli") -> GoalRuntimeInstance:
        self.park_foreground_except(task_id, reason="new foreground goal activated")
        ts = now()
        existing = self.active_for_task(task_id)
        if existing:
            instance = replace(existing, lane=FOREGROUND_LANE, state=ACTIVE_STATE, updated_at=ts, parked_reason=None)
            self.save(instance, event_type="foreground_runtime.activated", reason="reactivated existing runtime instance")
            return instance
        instance = GoalRuntimeInstance(
            id=f"goalrt_{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            lane=FOREGROUND_LANE,
            state=ACTIVE_STATE,
            created_at=ts,
            updated_at=ts,
            started_by=_safe_token(started_by),
        )
        self.save(instance, event_type="foreground_runtime.activated", reason="created foreground runtime instance")
        return instance

    def create_lane(
        self,
        *,
        task_id: str,
        started_by: str = "cli",
        lane_kind: str = "production",
        priority: int = 5,
        state: str = "queued",
    ) -> GoalRuntimeInstance:
        if lane_kind not in {"production", "playground"}:
            raise ValueError("lane_kind must be production or playground")
        if state not in LANE_STATES:
            raise ValueError(f"unsupported lane state: {state}")
        ts = now()
        instance = GoalRuntimeInstance(
            id=f"goalrt_{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            lane=FOREGROUND_LANE,
            state=state,
            created_at=ts,
            updated_at=ts,
            started_by=_safe_token(started_by),
            lane_kind=lane_kind,
            priority=max(0, int(priority or 0)),
            state_reason="lane created",
        )
        self.save(instance, event_type="lane.created", reason="lane created")
        return instance

    def transition(self, instance_id: str, state: str, *, reason: str, **updates: Any) -> GoalRuntimeInstance:
        if state not in LANE_STATES:
            raise ValueError(f"unsupported lane state: {state}")
        instance = self.get(instance_id)
        allowed = _ALLOWED_TRANSITIONS.get(instance.state, set())
        if allowed and state not in allowed and state != instance.state:
            self.event_log.append(
                Event(
                    ts=now(),
                    type="lane.transition_rejected",
                    task_id=instance.task_id,
                    run_id=None,
                    persona_id=None,
                    payload={"runtime_instance_id": instance.id, "from": instance.state, "to": state, "reason": _safe_reason(reason)},
                )
            )
            raise ValueError(f"invalid lane transition: {instance.state} -> {state}")
        payload = {"state": state, "updated_at": now(), "state_reason": _safe_reason(reason), "parked_reason": _safe_reason(reason) if state.startswith("parked_") else None}
        payload.update({key: value for key, value in updates.items() if hasattr(instance, key)})
        updated = replace(instance, **payload)
        return self.save(updated, event_type="lane.transitioned", reason=reason)

    def park_lane(self, instance_id: str, *, reason: str, state: str = "parked_by_operator") -> GoalRuntimeInstance:
        return self.transition(instance_id, state, reason=reason, active_run_ids=[])

    def resume_lane(self, instance_id: str, *, reason: str = "lane resumed") -> GoalRuntimeInstance:
        return self.transition(instance_id, "running", reason=reason, parked_reason=None)

    def park_open_task(self, task_id: str, *, reason: str) -> GoalRuntimeInstance:
        ts = now()
        existing = self.active_for_task(task_id) or self.latest_for_task(task_id)
        if existing:
            instance = replace(
                existing,
                lane=BACKGROUND_LANE,
                state=PARKED_STATE,
                updated_at=ts,
                parked_reason=_safe_reason(reason),
                active_run_ids=[],
            )
        else:
            instance = GoalRuntimeInstance(
                id=f"goalrt_{uuid.uuid4().hex[:12]}",
                task_id=task_id,
                lane=BACKGROUND_LANE,
                state=PARKED_STATE,
                created_at=ts,
                updated_at=ts,
                started_by="harness",
                parked_reason=_safe_reason(reason),
            )
        self.save(instance, event_type="foreground_runtime.parked_task", reason=reason)
        return instance

    def mark_terminal_for_task(self, task_id: str, *, reason: str) -> list[str]:
        closed: list[str] = []
        for instance in self.list_for_task(task_id):
            if instance.state == TERMINAL_STATE:
                continue
            updated = replace(instance, state=TERMINAL_STATE, updated_at=now(), parked_reason=_safe_reason(reason), active_run_ids=[])
            self.save(updated, event_type="foreground_runtime.closed", reason=reason)
            closed.append(updated.id)
        return closed

    def park_foreground_except(self, task_id: str, *, reason: str) -> list[str]:
        parked: list[str] = []
        for instance in self.list_all():
            if instance.task_id == task_id:
                continue
            if instance.lane == FOREGROUND_LANE and instance.state in {ACTIVE_STATE, WAITING_STATE}:
                self.park_open_task(instance.task_id, reason=reason)
                parked.append(instance.id)
        return parked

    def save(self, instance: GoalRuntimeInstance, *, event_type: str | None = None, reason: str = "") -> GoalRuntimeInstance:
        path = paths.runtime_instance_path(instance.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, to_jsonable(instance), indent=2, sort_keys=True)
        if event_type:
            self.event_log.append(
                Event(
                    ts=now(),
                    type=event_type,
                    task_id=instance.task_id,
                    run_id=None,
                    persona_id=None,
                    payload={
                        "runtime_instance_id": instance.id,
                        "task_id": instance.task_id,
                        "lane": instance.lane,
                        "state": instance.state,
                        "reason": _safe_reason(reason),
                    },
                )
            )
        return instance

    def get(self, instance_id: str) -> GoalRuntimeInstance:
        return from_jsonable(GoalRuntimeInstance, _read_json(paths.runtime_instance_path(instance_id)))

    def list_all(self) -> list[GoalRuntimeInstance]:
        directory = paths.runtime_instances_dir()
        if not directory.exists():
            return []
        items: list[GoalRuntimeInstance] = []
        for path in directory.glob("*.json"):
            try:
                items.append(from_jsonable(GoalRuntimeInstance, _read_json(path)))
            except Exception:
                continue
        return sorted(items, key=lambda item: (item.updated_at, item.id))

    def list_for_task(self, task_id: str) -> list[GoalRuntimeInstance]:
        return [item for item in self.list_all() if item.task_id == task_id]

    def latest_for_task(self, task_id: str) -> GoalRuntimeInstance | None:
        items = self.list_for_task(task_id)
        return items[-1] if items else None

    def active_for_task(self, task_id: str) -> GoalRuntimeInstance | None:
        for item in reversed(self.list_for_task(task_id)):
            if item.state in {ACTIVE_STATE, WAITING_STATE, PARKED_STATE}:
                return item
        return None

    def active_foreground(self) -> GoalRuntimeInstance | None:
        for item in reversed(self.list_all()):
            if item.lane == FOREGROUND_LANE and item.state in {ACTIVE_STATE, WAITING_STATE}:
                return item
        return None


def runtime_instance_summary(instance: GoalRuntimeInstance) -> dict[str, Any]:
    return {
        "id": instance.id,
        "lane_id": instance.id,
        "task_id": instance.task_id,
        "lane": instance.lane,
        "lane_kind": getattr(instance, "lane_kind", "production"),
        "state": instance.state,
        "state_reason": getattr(instance, "state_reason", None),
        "updated_at": instance.updated_at,
        "parked_reason": instance.parked_reason,
        "run_generation": instance.run_generation,
        "active_run_ids": list(instance.active_run_ids or []),
        "priority": getattr(instance, "priority", 5),
        "current_stage_id": getattr(instance, "current_stage_id", None),
        "current_owner": getattr(instance, "current_owner", None),
        "persona_instance_ids": list(getattr(instance, "persona_instance_ids", []) or []),
        "repo_bundle_locks": list(getattr(instance, "repo_bundle_locks", []) or []),
        "daemon_lease_id": getattr(instance, "daemon_lease_id", None),
        "budget_counters": dict(getattr(instance, "budget_counters", {}) or {}),
        "last_decision_type": getattr(instance, "last_decision_type", None),
        "last_progress_at": getattr(instance, "last_progress_at", None),
        "open_incident_ids": list(getattr(instance, "open_incident_ids", []) or []),
        "latest_proof_ids": list(getattr(instance, "latest_proof_ids", []) or []),
    }


def runtime_instances_summary(instances: list[GoalRuntimeInstance]) -> dict[str, Any]:
    active_foreground = [
        item for item in instances if item.lane == FOREGROUND_LANE and item.state in {ACTIVE_STATE, WAITING_STATE}
    ]
    background = [item for item in instances if item.lane == BACKGROUND_LANE and item.state == PARKED_STATE]
    return {
        "foreground": runtime_instance_summary(active_foreground[-1]) if active_foreground else None,
        "foreground_active_count": len(active_foreground),
        "background_parked_count": len(background),
        "background_task_ids": [item.task_id for item in background[:25]],
        "instances": [runtime_instance_summary(item) for item in instances],
        "lanes": [runtime_instance_summary(item) for item in instances],
    }


def _read_json(path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _safe_reason(reason: str) -> str:
    text = " ".join(str(reason or "").split())
    return text[:160] or "runtime state updated"


def _safe_token(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in str(value or "").strip())
    return text[:80] or "harness"
