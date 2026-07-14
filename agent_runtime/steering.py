from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from hermes_time import now

from .config import ensure_persisted_personas, load_agent_runtime_config
from .events import EventLog
from .models import AgentPersona, Event, Incident, Task
from .persona_assignments import (
    PersonaAssignmentSpec,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    safe_assignment_text,
    safe_assignment_token,
)
from .states import StageStatus, TaskState
from .store import IncidentStore, TaskStore

STEER_VERBS = frozenset({"route", "spawn", "re-scope", "resolve", "verdict-back"})
STEER_ACTION_CAP = 80
STEER_EDGE_CAP = 50
DEFAULT_MAX_CHILDREN_PER_STEERER = 3
DEFAULT_MAX_STEER_DEPTH = 2


@dataclass(frozen=True, slots=True)
class SteerActionRef:
    action_id: str
    verb: str
    source_node_id: str
    target_node_id: str


def build_steer_actions(
    nodes_by_id: dict[str, dict],
    edges: list[dict],
    *,
    control_node_id: str | None,
    task: Task,
) -> tuple[list[dict], list[dict]]:
    actions: list[dict] = []
    drops: list[dict] = []
    open_incident_ids = [str(item) for item in (getattr(task, "open_incident_ids", None) or []) if str(item).strip()]
    task_blocked = getattr(task, "state", None) == TaskState.BLOCKED

    for edge_index, edge in enumerate(edges):
        if edge_index >= STEER_EDGE_CAP:
            drops.append(
                {
                    "field": "steer_actions",
                    "kept": STEER_EDGE_CAP,
                    "dropped": len(edges) - STEER_EDGE_CAP,
                    "reason": "steer_edge_cap",
                }
            )
            break
        source = str(edge.get("source_node_id") or "").strip()
        target = str(edge.get("target_node_id") or "").strip()
        if source not in nodes_by_id or target not in nodes_by_id:
            continue
        available_now = bool(control_node_id and source == control_node_id)
        verbs = _verbs_for_edge(nodes_by_id[source], nodes_by_id[target], has_open_incidents=bool(open_incident_ids), task_blocked=task_blocked)
        for verb in verbs:
            action = _action_payload(
                verb=verb,
                source=source,
                target=target,
                source_node=nodes_by_id[source],
                target_node=nodes_by_id[target],
                available_now=available_now,
                task=task,
            )
            actions.append(action)
    if len(actions) > STEER_ACTION_CAP:
        drops.append(
            {
                "field": "steer_actions",
                "kept": STEER_ACTION_CAP,
                "dropped": len(actions) - STEER_ACTION_CAP,
                "reason": "steer_action_cap",
            }
        )
        actions = actions[:STEER_ACTION_CAP]
    _mark_recommended(actions)
    return actions, drops


def execute_steer_action(
    task_id: str,
    *,
    action_id: str | None = None,
    verb: str | None = None,
    source_node_id: str | None = None,
    target_node_id: str | None = None,
    requested_by: str = "operator",
    reason: str = "operator steer",
) -> dict[str, Any]:
    from .snapshot import build_snapshot

    task_store = TaskStore()
    event_log = EventLog()
    try:
        task = task_store.get_goal(task_id)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_kind": "task_not_found"}

    ref = _resolve_action_ref(
        action_id=action_id,
        verb=verb,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
    )
    if ref is None:
        return _steer_failed(task, None, "invalid_request", "A steer action id or verb/source/target is required.", event_log=event_log)

    snap = build_snapshot()
    task_row = next((item for item in snap.get("tasks", []) if item.get("task_id") == task.id), None)
    topology = ((task_row or {}).get("mission_level_state") or {}).get("agent_topology") or {}
    action = _matching_available_action(topology.get("steer_actions") or [], ref)
    if action is None:
        return _steer_failed(task, ref, "action_unavailable", "Steer action is not available in the current topology/state.", event_log=event_log)

    payload = _base_event_payload(ref, requested_by=requested_by, reason=reason)
    event_log.append(Event(ts=now(), type="steer.requested", task_id=task.id, run_id=None, persona_id=None, payload=payload))
    event_log.append(Event(ts=now(), type="steer.started", task_id=task.id, run_id=None, persona_id=None, payload=payload))

    try:
        result = _execute_available_action(task_store.get(task.id), action, requested_by=requested_by, reason=reason, event_log=event_log)
    except Exception as exc:
        return _steer_failed(task_store.get(task.id), ref, "execution_failed", str(exc), event_log=event_log)

    returned_payload = {**payload, "result": result.get("result"), "stage_id": result.get("stage_id"), "persona_instance_id": result.get("persona_instance_id")}
    event_log.append(Event(ts=now(), type="steer.returned", task_id=task.id, run_id=None, persona_id=None, payload=returned_payload))
    return {"ok": True, "capability_id": "goal.steer", "task_id": task.id, "goal_id": getattr(task, "goal_id", None) or task.id, "action": action, **result}


def _verbs_for_edge(source_node: dict, target_node: dict, *, has_open_incidents: bool, task_blocked: bool) -> list[str]:
    verbs = ["route", "spawn"]
    if has_open_incidents:
        verbs.append("resolve")
    if has_open_incidents or task_blocked:
        verbs.append("re-scope")
    roles = {str(source_node.get("role") or ""), str(target_node.get("role") or ""), str(source_node.get("persona_id") or ""), str(target_node.get("persona_id") or "")}
    if any("qa" in role or "verifier" in role for role in roles):
        verbs.append("verdict-back")
    return verbs


def _action_payload(
    *,
    verb: str,
    source: str,
    target: str,
    source_node: dict,
    target_node: dict,
    available_now: bool,
    task: Task,
) -> dict:
    capability_args = {"task_id": task.id, "verb": verb, "source_node_id": source, "target_node_id": target}
    action_id = f"steer:{source}:{target}:{verb}"
    return {
        "action_id": action_id,
        "verb": verb,
        "source_node_id": source,
        "target_node_id": target,
        "source_persona_id": source_node.get("persona_id"),
        "target_persona_id": target_node.get("persona_id"),
        "target_stage_id": target_node.get("current_stage_id"),
        "available_now": available_now,
        "disabled_reason": None if available_now else "source node does not currently hold control",
        "capability_id": "goal.steer",
        "capability_args": capability_args,
        "execution_semantics": "control_state_change",
        "fanout_limits": {
            "max_children_per_steerer": DEFAULT_MAX_CHILDREN_PER_STEERER,
            "max_depth": DEFAULT_MAX_STEER_DEPTH,
        },
    }


def _mark_recommended(actions: list[dict]) -> None:
    available = [item for item in actions if item.get("available_now")]
    preferred = next((item for item in available if item.get("verb") == "resolve"), None)
    preferred = preferred or next((item for item in available if item.get("verb") == "route"), None)
    preferred = preferred or (available[0] if available else None)
    for item in actions:
        item["recommended_steer"] = bool(preferred and item.get("action_id") == preferred.get("action_id"))


def _resolve_action_ref(*, action_id: str | None, verb: str | None, source_node_id: str | None, target_node_id: str | None) -> SteerActionRef | None:
    if action_id:
        parts = str(action_id).split(":")
        if len(parts) == 4 and parts[0] == "steer" and parts[3] in STEER_VERBS:
            return SteerActionRef(action_id=action_id, verb=parts[3], source_node_id=parts[1], target_node_id=parts[2])
        return None
    clean_verb = str(verb or "").strip()
    source = str(source_node_id or "").strip()
    target = str(target_node_id or "").strip()
    if clean_verb not in STEER_VERBS or not source or not target:
        return None
    return SteerActionRef(action_id=f"steer:{source}:{target}:{clean_verb}", verb=clean_verb, source_node_id=source, target_node_id=target)


def _matching_available_action(actions: list[dict], ref: SteerActionRef) -> dict | None:
    for action in actions:
        if (
            action.get("action_id") == ref.action_id
            and action.get("verb") == ref.verb
            and action.get("source_node_id") == ref.source_node_id
            and action.get("target_node_id") == ref.target_node_id
            and action.get("available_now") is True
        ):
            return action
    return None


def _execute_available_action(task: Task, action: dict, *, requested_by: str, reason: str, event_log: EventLog) -> dict:
    verb = str(action["verb"])
    if verb == "route":
        return _route_to_target_stage(task, action, actor=requested_by, reason=reason, event_log=event_log)
    if verb == "spawn":
        return _spawn_target_helper(task, action, actor=requested_by, reason=reason, event_log=event_log)
    if verb == "re-scope":
        return _rescope_to_source(task, action, actor=requested_by, reason=reason, event_log=event_log)
    if verb == "resolve":
        return _resolve_task_incidents(task, action, actor=requested_by)
    if verb == "verdict-back":
        return _verdict_back(task, action, actor=requested_by, reason=reason, event_log=event_log)
    raise ValueError(f"unsupported steer verb: {verb}")


def _route_to_target_stage(task: Task, action: dict, *, actor: str, reason: str, event_log: EventLog) -> dict:
    target_stage_id = str(action.get("target_stage_id") or "").strip()
    if not target_stage_id:
        raise ValueError("target has no current stage")
    return _activate_stage(task, target_stage_id, action, actor=actor, reason=reason, event_log=event_log, assignment_kind="steer_route")


def _rescope_to_source(task: Task, action: dict, *, actor: str, reason: str, event_log: EventLog) -> dict:
    source_stage_id = _node_stage_id(task, str(action.get("source_node_id") or ""), str(action.get("source_persona_id") or ""))
    if not source_stage_id:
        raise ValueError("source has no rescope stage")
    return _activate_stage(task, source_stage_id, action, actor=actor, reason=reason, event_log=event_log, assignment_kind="steer_rescope")


def _verdict_back(task: Task, action: dict, *, actor: str, reason: str, event_log: EventLog) -> dict:
    target_stage_id = str(action.get("target_stage_id") or "").strip()
    if not target_stage_id:
        target_stage_id = _node_stage_id(task, str(action.get("target_node_id") or ""), str(action.get("target_persona_id") or ""))
    if not target_stage_id:
        raise ValueError("verdict target has no stage")
    return _activate_stage(task, target_stage_id, action, actor=actor, reason=reason, event_log=event_log, assignment_kind="steer_verdict_back")


def _activate_stage(task: Task, stage_id: str, action: dict, *, actor: str, reason: str, event_log: EventLog, assignment_kind: str) -> dict:
    plan = getattr(task, "mission_plan", None)
    if plan is None:
        raise ValueError("task has no mission plan")
    stage = next((item for item in plan.stages if item.id == stage_id), None)
    if stage is None:
        raise ValueError(f"stage not found: {stage_id}")
    plan.current_stage_id = stage.id
    plan.revision = int(getattr(plan, "revision", 0) or 0) + 1
    if stage.status in {StageStatus.DRAFT, StageStatus.AUDITED, StageStatus.READY, StageStatus.REWORK, StageStatus.BLOCKED}:
        stage.status = StageStatus.IMPLEMENTING
    stage.updated_at = now()
    task.current_stage_id = stage.id
    if task.state in {TaskState.CREATED, TaskState.BLOCKED}:
        task.state = TaskState.RUNNING
    task.updated_at = now()
    TaskStore(event_log=event_log).update(task, actor=actor, reason=safe_assignment_text(reason, limit=300) or assignment_kind)
    assignment = PersonaAssignmentStore(event_log=event_log).create_or_resume(
        PersonaAssignmentSpec(
            persona_id=str(stage.owner),
            persona_instance_id=_existing_or_task_instance(str(stage.owner), task, parent_id=str(action.get("source_node_id") or "")),
            kind=assignment_kind,
            title=f"{stage.title} ({action.get('verb')})",
            message=safe_assignment_text(reason, limit=4000) or f"Steer {stage.owner} to stage {stage.id}.",
            created_by=actor,
            state="queued",
            task_id=task.id,
            goal_id=getattr(task, "goal_id", None) or task.id,
            stage_id=stage.id,
            repo=stage.repo,
            affected_paths=list(getattr(stage, "affected_paths", []) or []),
            acceptance=list(getattr(stage, "acceptance_criteria", []) or []),
            proof_targets=_proof_targets_for_stage(stage),
            allowed_decisions=["hand_off", "block", "escalate"],
        )
    )
    return {"result": "stage_routed", "stage_id": stage.id, "assignment_id": assignment.id}


def _spawn_target_helper(task: Task, action: dict, *, actor: str, reason: str, event_log: EventLog) -> dict:
    source_instance_id = _ensure_node_instance(task, str(action.get("source_persona_id") or ""), str(action.get("source_node_id") or ""), parent_id=None)
    if not source_instance_id:
        raise ValueError("source instance is required for spawn")
    depth = _steer_depth(source_instance_id)
    if depth >= DEFAULT_MAX_STEER_DEPTH:
        _log_cap(task, "max_depth", source_instance_id, event_log=event_log)
        raise ValueError("spawn depth cap reached")
    store = PersonaInstanceStore(event_log=event_log)
    children = [item for item in store.list_all() if source_instance_id in _parent_ids(item) and item.goal_id == task.id]
    if len(children) >= DEFAULT_MAX_CHILDREN_PER_STEERER:
        _log_cap(task, "max_children_per_steerer", source_instance_id, event_log=event_log)
        raise ValueError("spawn fan-out cap reached")
    target_persona = str(action.get("target_persona_id") or "").strip()
    persona = _persona_by_id(target_persona)
    placement = f"{task.id}:{target_persona}:spawn_{len(children) + 1}"
    child = store.ensure_for_goal(persona, goal_id=task.id, spawned_by=source_instance_id, placement_id=placement)
    target_stage_id = str(action.get("target_stage_id") or "").strip() or None
    stage = _stage_by_id(task, target_stage_id) if target_stage_id else None
    assignment = PersonaAssignmentStore(event_log=event_log).create_or_resume(
        PersonaAssignmentSpec(
            persona_id=target_persona,
            persona_instance_id=child.id,
            kind="steer_spawn",
            title=f"Spawned helper: {target_persona}",
            message=safe_assignment_text(reason, limit=4000) or "Spawned helper investigation; return a bounded summary and refs.",
            created_by=actor,
            state="queued",
            task_id=task.id,
            goal_id=getattr(task, "goal_id", None) or task.id,
            stage_id=target_stage_id,
            repo=getattr(stage, "repo", None),
            affected_paths=list(getattr(stage, "affected_paths", []) or []) if stage else [],
            acceptance=list(getattr(stage, "acceptance_criteria", []) or []) if stage else [],
            proof_targets=_proof_targets_for_stage(stage) if stage else [],
            allowed_decisions=["hand_off", "block", "escalate"],
        )
    )
    return {"result": "helper_spawned", "persona_instance_id": child.id, "assignment_id": assignment.id, "stage_id": target_stage_id}


def _proof_targets_for_stage(stage) -> list[str]:
    if _stage_has_visual_gate(stage):
        return ["launcher_qa screenshot proof"]
    return list(getattr(stage, "test_plan", []) or [])


def _stage_has_visual_gate(stage) -> bool:
    gate = getattr(stage, "proof_gate", {}) or {}
    required = {str(item).strip().lower() for item in (gate.get("required_proof_types") or []) if str(item).strip()}
    return bool(
        getattr(stage, "requires_product_edit", None) is not True
        and (getattr(stage, "requires_visual_proof", False) or gate.get("visual_required") is True or required & {"screenshot", "video"})
    )


def _resolve_task_incidents(task: Task, action: dict, *, actor: str) -> dict:
    store = IncidentStore()
    ids = [item for item in (getattr(task, "open_incident_ids", None) or []) if str(item).strip()]
    closed: list[str] = []
    for incident_id in ids:
        try:
            store.close(incident_id, reason=f"steer resolve by {actor}")
            closed.append(incident_id)
        except Exception:
            continue
    return {"result": "incidents_resolved", "closed_incident_ids": closed, "stage_id": getattr(getattr(task, "mission_plan", None), "current_stage_id", None)}


def _existing_or_task_instance(persona_id: str, task: Task, *, parent_id: str | None) -> str:
    return _ensure_node_instance(task, persona_id, persona_id, parent_id=parent_id) or ""


def _ensure_node_instance(task: Task, persona_id: str, node_id: str, *, parent_id: str | None) -> str | None:
    persona_id = str(persona_id or "").strip()
    if not persona_id:
        return None
    store = PersonaInstanceStore()
    try:
        existing = store.get(node_id)
        if existing.persona_id == persona_id:
            return existing.id
    except Exception:
        pass
    persona = _persona_by_id(persona_id)
    instance = store.ensure_for_goal(persona, goal_id=task.id, spawned_by=parent_id, placement_id=f"{task.id}:{persona_id}")
    return instance.id


def _persona_by_id(persona_id: str) -> AgentPersona:
    persona_id = str(persona_id or "").strip()
    for persona in ensure_persisted_personas(load_agent_runtime_config()):
        if persona.id == persona_id:
            return persona
    raise ValueError(f"persona not found: {persona_id}")


def _node_stage_id(task: Task, node_id: str, persona_id: str) -> str | None:
    plan = getattr(task, "mission_plan", None)
    if plan is None:
        return None
    slot = node_id[5:] if node_id.startswith("slot_") else None
    for stage in plan.stages:
        if slot and getattr(stage, "owner_slot", None) == slot:
            return stage.id
        if persona_id and getattr(stage, "owner", None) == persona_id:
            return stage.id
    return None


def _stage_by_id(task: Task, stage_id: str | None):
    plan = getattr(task, "mission_plan", None)
    if plan is None or not stage_id:
        return None
    return next((item for item in plan.stages if item.id == stage_id), None)


def _parent_ids(instance) -> list[str]:
    """Steering-parent ids of an instance (multi-parent fan-in aware): the
    authoritative ``steered_by`` set, falling back to the legacy scalar
    ``spawned_by`` for un-migrated records."""
    parents = list(getattr(instance, "steered_by", []) or [])
    if not parents:
        scalar = getattr(instance, "spawned_by", None)
        if scalar:
            parents = [scalar]
    return parents


def _steer_depth(instance_id: str) -> int:
    # Longest parent chain from this node upward, over the multi-parent steering
    # DAG (max across every parent), so the spawn depth cap still holds when a
    # child fans in from several parents.
    store = PersonaInstanceStore()

    def depth_of(node_id: str, seen: frozenset[str]) -> int:
        if not node_id or node_id in seen:
            return 0
        seen = seen | {node_id}
        try:
            instance = store.get(node_id)
        except Exception:
            return 0
        parents = _parent_ids(instance)
        if not parents:
            return 0
        return 1 + max(depth_of(parent, seen) for parent in parents)

    return depth_of(instance_id, frozenset())


def _log_cap(task: Task, cap: str, source_instance_id: str, *, event_log: EventLog) -> None:
    event_log.append(
        Event(
            ts=now(),
            type="steer.cap_hit",
            task_id=task.id,
            run_id=None,
            persona_id=None,
            payload={"cap": cap, "source_instance_id": source_instance_id},
        )
    )


def _base_event_payload(ref: SteerActionRef, *, requested_by: str, reason: str) -> dict[str, Any]:
    return {
        "action_id": ref.action_id,
        "verb": ref.verb,
        "source_node_id": ref.source_node_id,
        "target_node_id": ref.target_node_id,
        "requested_by": safe_assignment_token(requested_by) or "operator",
        "reason": safe_assignment_text(reason, limit=300),
    }


def _steer_failed(task: Task, ref: SteerActionRef | None, error_kind: str, error: str, *, event_log: EventLog) -> dict[str, Any]:
    payload = _base_event_payload(ref, requested_by="operator", reason=error) if ref is not None else {"reason": safe_assignment_text(error, limit=300)}
    payload["error_kind"] = error_kind
    event_log.append(Event(ts=now(), type="steer.failed", task_id=task.id, run_id=None, persona_id=None, payload=payload))
    incident = Incident(
        id=f"inc_{uuid.uuid4().hex[:8]}",
        task_id=task.id,
        run_id=None,
        kind="steer_failed",
        summary=safe_assignment_text(error, limit=240) or "Steer action failed",
        detail_path=None,
        opened_at=now(),
        metadata={"error_kind": error_kind, **{k: v for k, v in payload.items() if isinstance(v, str)}},
    )
    IncidentStore(event_log=event_log).open(incident)
    return {
        "ok": False,
        "capability_id": "goal.steer",
        "task_id": task.id,
        "error_kind": error_kind,
        "error": error,
        "incident_id": incident.id,
    }
