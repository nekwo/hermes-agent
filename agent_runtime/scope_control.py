from __future__ import annotations

import uuid
from typing import Any

from hermes_time import now

from .decision_schema import AgentDecision, DecisionPayloadInvalid
from .models import Incident
from .states import TaskState

CLASSIFICATIONS = frozenset({"blocks_current", "same_scope", "fork_child", "defer", "escalate"})
RELATIONSHIP_HINTS = CLASSIFICATIONS | {"unknown"}
SEVERITIES = frozenset({"low", "medium", "high", "critical"})
TERMINAL_TRIAGE_STATUSES = frozenset({"forked", "deferred", "escalated", "rejected", "same_scope", "blocked", "reported"})
BOUNDED_TEST_FIX_FLAG = "bounded_test_fix_pass_used"
FINAL_GAP_REPORT_FLAG_PREFIX = "final_gap_report:"


def normalize_severity(value: Any, default: str = "medium") -> str:
    text = str(value or default).strip().lower()
    if text not in SEVERITIES:
        raise DecisionPayloadInvalid(f"severity must be one of {sorted(SEVERITIES)}")
    return text


def normalize_relationship(value: Any, *, allow_unknown: bool = True) -> str:
    allowed = RELATIONSHIP_HINTS if allow_unknown else CLASSIFICATIONS
    text = str(value or ("unknown" if allow_unknown else "defer")).strip().lower()
    if text not in allowed:
        raise DecisionPayloadInvalid(f"relationship_hint/decision must be one of {sorted(allowed)}")
    return text


def list_of_strings(payload: dict[str, Any], key: str, *, required: bool = False) -> list[str]:
    if key not in payload:
        if required:
            raise DecisionPayloadInvalid(f"missing payload key: {key}")
        return []
    value = payload[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise DecisionPayloadInvalid(f"{key} must be a list of non-empty strings")
    return [item.strip() for item in value]


def validate_discovery_payload(payload: dict[str, Any]) -> None:
    if not str(payload.get("title", "")).strip():
        raise DecisionPayloadInvalid("discovery title is required")
    if not str(payload.get("summary", "")).strip():
        raise DecisionPayloadInvalid("discovery summary is required")
    normalize_severity(payload.get("severity", "medium"))
    normalize_relationship(payload.get("relationship_hint", "unknown"), allow_unknown=True)
    list_of_strings(payload, "evidence")
    list_of_strings(payload, "affected_paths")
    list_of_strings(payload, "suggested_acceptance_criteria")
    for key in ("suggested_child_title", "suggested_child_description"):
        if key in payload and not isinstance(payload[key], str):
            raise DecisionPayloadInvalid(f"{key} must be a string")


def validate_triage_payload(payload: dict[str, Any]) -> None:
    if not str(payload.get("discovery_id", "")).strip():
        raise DecisionPayloadInvalid("discovery_id is required")
    decision = normalize_relationship(payload.get("decision"), allow_unknown=False)
    if not str(payload.get("rationale", "")).strip():
        raise DecisionPayloadInvalid("triage rationale is required")
    if "priority" in payload:
        normalize_severity(payload.get("priority"))
    if decision == "fork_child":
        if not str(payload.get("child_title", "")).strip():
            raise DecisionPayloadInvalid("child_title is required for fork_child")
        if not str(payload.get("child_description", "")).strip():
            raise DecisionPayloadInvalid("child_description is required for fork_child")
        if not list_of_strings(payload, "child_acceptance_criteria", required=True):
            raise DecisionPayloadInvalid("child_acceptance_criteria must be non-empty for fork_child")


def record_issue_discovery(task: Any, decision: AgentDecision, *, actor: str, run_id: str | None = None) -> dict[str, Any]:
    validate_discovery_payload(decision.payload)
    if getattr(task, "issue_discoveries", None) is None:
        task.issue_discoveries = []
    ts = now()
    payload = decision.payload
    discovery = {
        "id": f"disc_{uuid.uuid4().hex[:8]}",
        "parent_task_id": task.id,
        "reported_by": actor,
        "reported_run_id": run_id,
        "title": str(payload["title"]).strip(),
        "summary": str(payload["summary"]).strip(),
        "evidence": list_of_strings(payload, "evidence"),
        "affected_paths": list_of_strings(payload, "affected_paths"),
        "severity": normalize_severity(payload.get("severity", "medium")),
        "relationship_hint": normalize_relationship(payload.get("relationship_hint", "unknown"), allow_unknown=True),
        "triage_status": "untriaged",
        "triage_decision": None,
        "triage_rationale": None,
        "child_task_id": None,
        "suggested_child_title": str(payload.get("suggested_child_title", "")).strip() or None,
        "suggested_child_description": str(payload.get("suggested_child_description", "")).strip() or None,
        "suggested_acceptance_criteria": list_of_strings(payload, "suggested_acceptance_criteria"),
        "created_at": ts.isoformat(),
        "updated_at": ts.isoformat(),
        "schema_version": 1,
    }
    task.issue_discoveries.append(discovery)
    task.updated_at = ts
    return discovery


def untriaged_issue_discoveries(task: Any) -> list[dict[str, Any]]:
    return [item for item in getattr(task, "issue_discoveries", []) or [] if item.get("triage_status") == "untriaged"]


def has_untriaged_issue_discovery(task: Any) -> bool:
    return bool(untriaged_issue_discoveries(task))


def needs_pm_triage_before_dev(task: Any) -> bool:
    for item in untriaged_issue_discoveries(task):
        if item.get("severity") in {"high", "critical"}:
            return True
        if item.get("relationship_hint") in {"blocks_current", "escalate"}:
            return True
    return False


def issue_discovery_counts(task: Any) -> dict[str, int]:
    counts = {"untriaged": 0, "forked": 0, "deferred": 0, "escalated": 0, "blocked": 0, "same_scope": 0, "rejected": 0, "reported": 0, "triaged": 0}
    for item in getattr(task, "issue_discoveries", []) or []:
        status = str(item.get("triage_status") or "untriaged")
        counts[status] = counts.get(status, 0) + 1
    return counts


def find_discovery(task: Any, discovery_id: str) -> dict[str, Any]:
    for item in getattr(task, "issue_discoveries", []) or []:
        if item.get("id") == discovery_id:
            return item
    raise DecisionPayloadInvalid(f"unknown discovery_id: {discovery_id}")


def find_discovery_task(task_store, discovery_id: str) -> tuple[Any, dict[str, Any]]:
    for task in task_store.list_all():
        for item in getattr(task, "issue_discoveries", []) or []:
            if item.get("id") == discovery_id:
                return task, item
    raise DecisionPayloadInvalid(f"unknown discovery_id: {discovery_id}")




def child_mission_depth(task: Any, task_store=None) -> int:
    """Return the task's parent-chain depth, bounded to safe local store traversal.

    Depth 0 means a top-level Tony mission. Depth 1 means a direct child mission.
    Unknown/missing parents stop traversal rather than failing a live tick.
    """
    depth = 0
    parent_id = getattr(task, "parent_task_id", None)
    seen = {task.id}
    while parent_id:
        depth += 1
        if task_store is None or parent_id in seen:
            break
        seen.add(parent_id)
        try:
            parent = task_store.get(parent_id)
        except Exception:
            break
        parent_id = getattr(parent, "parent_task_id", None)
    return depth


def direct_child_count(task: Any, task_store=None) -> int:
    if task_store is None:
        return 0
    try:
        return len([item for item in task_store.list_all() if getattr(item, "parent_task_id", None) == task.id])
    except Exception:
        return 0


def should_report_gap_instead_of_forking(task: Any, *, task_store=None) -> bool:
    """Tony guardrail: no recursive/deep issue trees and no many-sibling forests."""
    return child_mission_depth(task, task_store) >= 1 or direct_child_count(task, task_store) >= 1


def mark_discovery_for_final_report(discovery: dict[str, Any], *, reason: str, ts=None) -> dict[str, Any]:
    ts = ts or now()
    discovery["triage_status"] = "reported"
    discovery["final_report_reason"] = reason
    discovery["final_report"] = True
    discovery["updated_at"] = ts.isoformat()
    return discovery


def mark_bounded_test_fix_pass(task: Any, discovery: dict[str, Any], *, ts=None) -> bool:
    """Allow exactly one same-scope test/analyzer fixing pass per mission."""
    ts = ts or now()
    if BOUNDED_TEST_FIX_FLAG in task.risk_flags:
        mark_discovery_for_final_report(discovery, reason="bounded_test_fix_pass_already_used", ts=ts)
        return False
    task.risk_flags.append(BOUNDED_TEST_FIX_FLAG)
    discovery["bounded_test_fix_pass"] = "allowed_once"
    return True

def apply_issue_triage(task: Any, decision: AgentDecision, *, actor: str, task_store=None, incident_store=None) -> dict[str, Any]:
    validate_triage_payload(decision.payload)
    payload = decision.payload
    discovery = find_discovery(task, str(payload["discovery_id"]).strip())
    if discovery.get("triage_status") in TERMINAL_TRIAGE_STATUSES:
        return discovery
    triage = normalize_relationship(payload.get("decision"), allow_unknown=False)
    ts = now()
    discovery["triage_decision"] = triage
    discovery["triage_rationale"] = str(payload.get("rationale", "")).strip()
    discovery["updated_at"] = ts.isoformat()
    priority = normalize_severity(payload.get("priority", discovery.get("severity", "medium")))

    if triage == "fork_child":
        if task_store is None:
            raise DecisionPayloadInvalid("task_store is required for fork_child triage")
        if should_report_gap_instead_of_forking(task, task_store=task_store):
            mark_discovery_for_final_report(discovery, reason="child_mission_depth_or_sibling_limit_reached", ts=ts)
            flag = f"{FINAL_GAP_REPORT_FLAG_PREFIX}{discovery['id']}"
            if flag not in task.risk_flags:
                task.risk_flags.append(flag)
            task.updated_at = ts
            return discovery
        existing = discovery.get("child_task_id")
        if existing:
            discovery["triage_status"] = "forked"
            return discovery
        child = Task(
            id=f"task_{uuid.uuid4().hex[:8]}",
            title=str(payload["child_title"]).strip(),
            description=str(payload["child_description"]).strip(),
            state=TaskState.CREATED,
            created_at=ts,
            updated_at=ts,
            requested_by=f"harness:issue_discovery:{discovery['id']}",
            acceptance_criteria=list_of_strings(payload, "child_acceptance_criteria", required=True),
            non_goals=[
                "Do not modify parent mission scope unless PM re-triages.",
                "Do not fork additional child missions; report any new AAA/general gaps in the final PM/Neko report.",
                "At most one bounded same-scope test/analyzer fix pass is allowed before reporting remaining gaps.",
            ],
            parent_task_id=task.id,
            risk_flags=["forked_from_issue_discovery", "max_child_depth:1", f"priority:{priority}", f"severity:{discovery.get('severity', priority)}"],
        )
        task_store.create(child)
        discovery["child_task_id"] = child.id
        discovery["triage_status"] = "forked"
    elif triage == "defer":
        discovery["triage_status"] = "deferred"
    elif triage == "same_scope":
        if mark_bounded_test_fix_pass(task, discovery, ts=ts):
            discovery["triage_status"] = "same_scope"
            flag = f"same_scope_issue:{discovery['id']}"
            if flag not in task.risk_flags:
                task.risk_flags.append(flag)
        else:
            flag = f"{FINAL_GAP_REPORT_FLAG_PREFIX}{discovery['id']}"
            if flag not in task.risk_flags:
                task.risk_flags.append(flag)
    elif triage in {"blocks_current", "escalate"}:
        discovery["triage_status"] = "blocked" if triage == "blocks_current" else "escalated"
        task.state = TaskState.BLOCKED
        if incident_store is not None:
            incident_store.open(
                Incident(
                    id=f"inc_{uuid.uuid4().hex[:8]}",
                    task_id=task.id,
                    run_id=None,
                    kind="scope_blocker" if triage == "blocks_current" else "scope_intervention",
                    summary=f"Issue discovery {discovery['id']} requires {triage} triage action",
                    detail_path=None,
                    opened_at=ts,
                )
            )
    task.updated_at = ts
    return discovery
