"""Issue-discovery payload validation.

S27 removed this module's mutation half: ``record_issue_discovery``,
``apply_issue_triage`` and the fork-guard chain behind it
(``child_mission_depth`` / ``direct_child_count`` /
``should_report_gap_instead_of_forking`` / ``mark_discovery_for_final_report`` /
``mark_bounded_test_fix_pass``), plus the unread counters and predicates. They
wrote issue discoveries onto a ``Task`` record deleted in S8 --
``apply_issue_triage`` even constructed ``Task(...)`` without importing it, a
latent ``NameError`` that only survived because no caller was left to reach it.

What remains is validation + lookup, which is all the live importers use:
``decision_contracts`` (payload validation), ``observability`` (the untriaged
list), and ``harness`` (the discovery lookup).
"""

from __future__ import annotations

from typing import Any

from .decision_schema import DecisionPayloadInvalid

CLASSIFICATIONS = frozenset({"blocks_current", "same_scope", "fork_child", "defer", "escalate"})
RELATIONSHIP_HINTS = CLASSIFICATIONS | {"unknown"}
SEVERITIES = frozenset({"low", "medium", "high", "critical"})


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


def untriaged_issue_discoveries(task: Any) -> list[dict[str, Any]]:
    return [item for item in getattr(task, "issue_discoveries", []) or [] if item.get("triage_status") == "untriaged"]
def find_discovery_task(task_store, discovery_id: str) -> tuple[Any, dict[str, Any]]:
    for task in task_store.list_all():
        for item in getattr(task, "issue_discoveries", []) or []:
            if item.get("id") == discovery_id:
                return task, item
    raise DecisionPayloadInvalid(f"unknown discovery_id: {discovery_id}")
