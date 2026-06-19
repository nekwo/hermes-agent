from __future__ import annotations

from dataclasses import dataclass, field

from .models import Task
from .plan_review import PlanReviewVerdict


@dataclass(slots=True)
class GateResult:
    allowed: bool
    missing: list[str]
    warnings: list[str] = field(default_factory=list)


def can_enter_dev_implementing(task: Task) -> GateResult:
    if task.waiver and task.waiver.get("gate") == "qa_plan_review":
        return GateResult(True, [], [f"waived by {task.waiver.get('actor', 'unknown')}"])
    missing: list[str] = []
    if not task.stages:
        missing.append("missing stage plan")
    for stage in task.stages:
        if not stage.acceptance_criteria:
            missing.append(f"stage {stage.id} missing acceptance criteria")
        if not stage.test_plan:
            missing.append(f"stage {stage.id} missing test plan")
    review = getattr(task, "plan_review", None)
    if review is None or getattr(review, "verdict", None) != PlanReviewVerdict.APPROVED:
        missing.append("missing approved QA plan review")
    else:
        reviewed = set(review.reviewed_stage_ids)
        for stage in task.stages:
            if stage.id not in reviewed:
                missing.append(f"stage {stage.id} not reviewed by QA")
        if not review.test_plan_confirmed:
            missing.append("QA did not confirm test plan")
        if not review.proof_requirements_confirmed:
            missing.append("QA did not confirm proof requirements")
    return GateResult(not missing, missing)
