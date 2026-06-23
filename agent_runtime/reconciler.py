from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .gates import can_enter_dev_implementing
from .models import Task
from .plan_review import PlanReviewVerdict
from .states import TaskState

UNSUPPORTED_CONTEXT_SUPERVISOR_THRESHOLD = 3


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    kind: str
    severity: str
    summary: str
    recommended_action: str
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    findings: list[ReconciliationFinding] = field(default_factory=list)

    @property
    def needs_supervisor(self) -> bool:
        return any(item.severity in {"medium", "high", "critical"} for item in self.findings)


def reconcile_task(task: Task) -> ReconciliationResult:
    """Detect persona handoff mismatches that need Neko/global steering.

    The reconciler is intentionally read-only: it records where PM/Dev/QA outputs
    disagree with task state or safety gates. Mutation remains in explicit
    decisions/transition application so Neko cannot silently invent proof.
    """
    findings: list[ReconciliationFinding] = []
    findings.extend(_qa_approved_stage_mismatch(task))
    findings.extend(_repeated_unsupported_context_requests(task))
    findings.extend(_approved_plan_still_in_design(task))
    return ReconciliationResult(findings=findings)


def _qa_approved_stage_mismatch(task: Task) -> list[ReconciliationFinding]:
    review = getattr(task, "plan_review", None)
    if review is None or review.verdict != PlanReviewVerdict.APPROVED:
        return []
    stage_ids = {stage.id for stage in task.stages}
    missing = [stage_id for stage_id in review.reviewed_stage_ids if stage_id not in stage_ids]
    if not missing:
        return []
    return [
        ReconciliationFinding(
            kind="qa_approved_missing_stage_records",
            severity="medium",
            summary="QA approved reviewed stage IDs that do not exist as TaskStage records.",
            recommended_action="Neko should recommend/materialize an auditable stage plan or request human authorization before advancing.",
            evidence=[f"missing_stage:{stage_id}" for stage_id in missing],
        )
    ]


def _repeated_unsupported_context_requests(task: Task) -> list[ReconciliationFinding]:
    counts: dict[str, int] = {}
    latest: dict[str, Any] | None = None
    for req in getattr(task, "context_requests", []) or []:
        status = req.get("status")
        if status not in {"unsupported", "superseded"}:
            continue
        actor = str(req.get("actor") or "unknown")
        counts[actor] = counts.get(actor, 0) + 1
        latest = req
    findings: list[ReconciliationFinding] = []
    for actor, count in counts.items():
        if count < UNSUPPORTED_CONTEXT_SUPERVISOR_THRESHOLD:
            continue
        evidence = [f"actor:{actor}", f"count:{count}"]
        if latest:
            evidence.append(f"latest_request:{latest.get('id')}")
            evidence.append(f"latest_failure:{latest.get('failure_reason')}")
        findings.append(
            ReconciliationFinding(
                kind="repeated_unsupported_context_requests",
                severity="high",
                summary="A persona repeatedly requested unavailable context instead of using available proof or reporting a final gap.",
                recommended_action="Route to Neko to force a verdict/block/final-gap report; do not schedule more context requests.",
                evidence=evidence,
            )
        )
    return findings


def _approved_plan_still_in_design(task: Task) -> list[ReconciliationFinding]:
    state = task.state if isinstance(task.state, TaskState) else TaskState(task.state)
    review = getattr(task, "plan_review", None)
    if state != TaskState.RUNNING or review is None or review.verdict != PlanReviewVerdict.APPROVED:
        return []
    gate = can_enter_dev_implementing(task)
    if gate.allowed:
        return [
            ReconciliationFinding(
                kind="approved_plan_not_advanced",
                severity="medium",
                summary="QA-approved plan satisfies the Dev implementation gate but task remains in dev_test_design.",
                recommended_action="Neko may authorize qa_review_plan -> dev_implementing or re-run the deterministic transition.",
                evidence=["gate_allowed:true"],
            )
        ]
    return [
        ReconciliationFinding(
            kind="approved_plan_gate_blocked",
            severity="medium",
            summary="QA approved the plan but the deterministic Dev implementation gate is still blocked.",
            recommended_action="Neko should request a concrete stage/test-plan repair or final gap report, not let Dev/QA loop.",
            evidence=[f"missing:{item}" for item in gate.missing],
        )
    ]
