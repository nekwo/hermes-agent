from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from hermes_time import now


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class PlanReviewVerdict(StrEnum):
    APPROVED = "approved"
    NEEDS_CORRECTIONS = "needs_corrections"
    BLOCKED = "blocked"


@dataclass(slots=True)
class Finding:
    id: str
    severity: FindingSeverity
    summary: str
    affected_stage_id: str | None = None
    affected_paths: list[str] = field(default_factory=list)
    recommendation: str = ""
    created_by: str = "qa"
    created_at: datetime = field(default_factory=now)
    resolved_at: datetime | None = None
    schema_version: int = 1


@dataclass(slots=True)
class PlanReview:
    id: str
    task_id: str
    reviewer_agent_id: str
    verdict: PlanReviewVerdict
    findings: list[Finding] = field(default_factory=list)
    reviewed_stage_ids: list[str] = field(default_factory=list)
    proof_requirements_confirmed: bool = False
    test_plan_confirmed: bool = False
    created_at: datetime = field(default_factory=now)
    schema_version: int = 1


def finding_from_payload(raw: dict, *, created_by: str = "qa") -> Finding:
    return Finding(
        id=str(raw.get("id") or f"finding_{uuid.uuid4().hex[:8]}"),
        severity=FindingSeverity(raw.get("severity", FindingSeverity.WARNING.value)),
        summary=str(raw.get("summary", "")).strip(),
        affected_stage_id=raw.get("affected_stage_id") or raw.get("stage_id"),
        affected_paths=list(raw.get("affected_paths", [])),
        recommendation=str(raw.get("recommendation", "")),
        created_by=created_by,
    )
