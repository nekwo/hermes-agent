from __future__ import annotations

from collections import deque
from typing import Any

from hermes_time import now

from .events import EventLog
from .models import Event, PersonaInstance, Task
from .persona_assignments import PersonaInstanceStore, safe_assignment_text
from .states import TaskState
from .steering import DEFAULT_MAX_CHILDREN_PER_STEERER, DEFAULT_MAX_STEER_DEPTH
from .store import ProofStore


def recursive_supervision_enabled(config) -> bool:
    supervision = getattr(config, "supervision", None)
    return bool(getattr(supervision, "recursive_enabled", False))


def direct_children(parent_node_id: str, *, goal_id: str | None = None, store: PersonaInstanceStore | None = None) -> list[PersonaInstance]:
    store = store or PersonaInstanceStore()
    parent = safe_assignment_text(parent_node_id, limit=160)
    goal = safe_assignment_text(goal_id, limit=160) if goal_id is not None else None
    children = [instance for instance in store.list_all() if safe_assignment_text(instance.spawned_by, limit=160) == parent]
    if goal:
        children = [instance for instance in children if instance.goal_id == goal or instance.current_task_id == goal]
    return children


def distilled_child_context(parent_node_id: str, *, task_id: str, event_log: EventLog | None = None, store: PersonaInstanceStore | None = None) -> list[dict[str, Any]]:
    event_log = event_log or EventLog()
    children = {child.id: child for child in direct_children(parent_node_id, goal_id=task_id, store=store)}
    if not children:
        return []
    latest_by_child: dict[str, dict[str, Any]] = {}
    for event in event_log.iter_all():
        if event.task_id != task_id or event.type not in {"child.progress", "child.blocked", "child.returned"}:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        child_id = str(payload.get("child_node_id") or "")
        if child_id not in children:
            continue
        latest_by_child[child_id] = {
            "child_node_id": child_id,
            "persona_id": children[child_id].persona_id,
            "event_type": event.type,
            "status": safe_assignment_text(payload.get("status") or payload.get("reason") or event.type, limit=120),
            "summary": safe_assignment_text(payload.get("summary") or payload.get("reason") or event.type, limit=240),
            "proof_ids": _safe_list(payload.get("proof_ids")),
            "artifact_refs": _safe_list(payload.get("artifact_refs")),
        }
    return [latest_by_child[child.id] for child in children.values() if child.id in latest_by_child]


def enforce_supervision_caps(parent_node_id: str, *, goal_id: str, store: PersonaInstanceStore | None = None, event_log: EventLog | None = None) -> dict[str, Any]:
    store = store or PersonaInstanceStore()
    event_log = event_log or EventLog()
    children = direct_children(parent_node_id, goal_id=goal_id, store=store)
    hits: list[dict[str, Any]] = []
    if len(children) > DEFAULT_MAX_CHILDREN_PER_STEERER:
        hits.append({"cap": "max_children_per_steerer", "limit": DEFAULT_MAX_CHILDREN_PER_STEERER, "actual": len(children)})
    depth = _max_descendant_depth(parent_node_id, goal_id=goal_id, store=store)
    if depth > DEFAULT_MAX_STEER_DEPTH:
        hits.append({"cap": "max_depth", "limit": DEFAULT_MAX_STEER_DEPTH, "actual": depth})
    for hit in hits:
        event_log.append(
            Event(
                ts=now(),
                type="steer.cap_hit",
                task_id=goal_id,
                run_id=None,
                persona_id=None,
                payload={"cap": hit["cap"], "source_instance_id": safe_assignment_text(parent_node_id, limit=160)},
            )
        )
    return {"ok": not hits, "hits": hits}


def child_return_gate_passed(event: Event, *, proof_store: ProofStore | None = None) -> tuple[bool, str]:
    """Gate a child return against harness-owned proof records.

    This is intentionally fail-closed for recursive supervision: a returned child
    event must cite at least one passed proof id unless the event is a block.
    """

    if event.type != "child.returned":
        return True, "not_returned"
    proof_store = proof_store or ProofStore()
    payload = event.payload if isinstance(event.payload, dict) else {}
    proof_ids = _safe_list(payload.get("proof_ids"))
    if not proof_ids:
        return False, "missing_child_proof"
    expected_stage_id = safe_assignment_text(payload.get("stage_id"), limit=160)
    for proof_id in proof_ids:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            return False, "missing_child_proof"
        metadata = proof.metadata if isinstance(getattr(proof, "metadata", None), dict) else {}
        status = str(metadata.get("status") or "").lower()
        if status != "passed":
            return False, "child_proof_not_passed"
        if getattr(proof, "redaction_status", None) != "safe":
            return False, "child_proof_not_redaction_safe"
        if str(getattr(proof, "created_by", "") or "") != "harness":
            return False, "child_proof_not_harness_owned"
        if metadata.get("source") == "agent_tool_trace" or metadata.get("authoritative") is False:
            return False, "child_proof_not_authoritative"
        proof_stage = safe_assignment_text(getattr(proof, "stage_id", None) or metadata.get("stage_id"), limit=160)
        if expected_stage_id and proof_stage and proof_stage != expected_stage_id:
            return False, "child_proof_wrong_stage"
    return True, "passed"


def block_task_for_failed_child_gate(task: Task, *, reason: str) -> None:
    if task.state not in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
        task.state = TaskState.BLOCKED
        task.harness_self_heal = {**(task.harness_self_heal or {}), "child_gate_failed": safe_assignment_text(reason, limit=160)}
        task.updated_at = now()


def _max_descendant_depth(parent_node_id: str, *, goal_id: str, store: PersonaInstanceStore) -> int:
    max_depth = 0
    queue: deque[tuple[str, int]] = deque((child.id, 1) for child in direct_children(parent_node_id, goal_id=goal_id, store=store))
    while queue:
        node_id, depth = queue.popleft()
        max_depth = max(max_depth, depth)
        for child in direct_children(node_id, goal_id=goal_id, store=store):
            queue.append((child.id, depth + 1))
    return max_depth


def _safe_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        safe = safe_assignment_text(item, limit=120)
        if safe and safe not in result:
            result.append(safe)
    return result[:8]
