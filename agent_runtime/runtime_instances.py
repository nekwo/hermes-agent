from __future__ import annotations

from typing import Any

from . import paths
from .events import EventLog
from .models import GoalRuntimeInstance
from .serde import from_jsonable

class GoalRuntimeInstanceStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    # S53 (2026-08-01) removed this store's WRITE lane whole: ``create_lane``,
    # ``transition``, ``park_lane``, ``resume_lane``, ``park_open_task``,
    # ``mark_terminal_for_task``, and the single ``save`` chokepoint they all
    # funnelled through. Not one had a production caller.
    #
    # ``park_lane`` had exactly one non-test caller and it was
    # ``operator_control.py``, deleted at S49 in this same wave — so the cut
    # ORDER mattered: park_lane only became callerless once S49 landed.
    # ``resume_lane`` was not on the original cut list and is included because
    # its entire body is one ``transition`` call: keeping it would have left a
    # method that raises ``AttributeError`` on its first line. Zero production
    # callers either way.
    #
    # The four contracts they emitted (``lane.created``, ``lane.transitioned``,
    # ``lane.transition_rejected``, ``foreground_runtime.closed``) are
    # de-registered with them. The READ side below is LIVE — ``status.py``
    # projects lanes off ``list_all`` — so this is a write-lane cut, not a store
    # removal. See tests/agent_runtime/test_s53_lane_write_lane_removal.py.

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

    # S21: `active_foreground()` and `park_foreground_except()` were removed here.
    # `active_foreground` walked lanes into `TaskStore.get`, which the permanent
    # `TaskStoreStub` (ruling R-3) raises `NotFound` from unconditionally — every
    # iteration hit `continue`, so it returned `None` by construction rather than
    # because no lane was live. `park_foreground_except` was `return []`. Neither
    # had a production caller. The foreground/background split itself belonged to
    # the retired dispatch lane; lanes are now a flat list.


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
    lane_rows = [runtime_instance_summary(item) for item in instances]
    # S21: `foreground` / `foreground_active_count` / `background_parked_count` /
    # `background_task_ids` were literal `None` / `0` / `[]` here — a foreground
    # lane that no code could ever elect, reported in the shape of a live reading.
    return {
        "instances": lane_rows,
        "lanes": lane_rows,
    }


def _read_json(path):
    import json

    return json.loads(path.read_text(encoding="utf-8"))


# S53: ``_safe_reason`` and ``_safe_token`` went with the write lane. Both were
# write-time sanitisers -- reason text and started-by tokens on their way INTO a
# lane row -- and every caller was a deleted writer. A private helper outliving
# its only branch is the residue S25 named when it retired ``events._safe_int``.
