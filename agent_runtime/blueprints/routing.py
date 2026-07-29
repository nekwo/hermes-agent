from __future__ import annotations

from typing import Iterable

from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import MissionPlan, MissionPlanStage, Proof, Task
from agent_runtime.states import StageStatus, TaskState

from .schema import StageOutcome


OUTCOME_TO_STAGE_STATUS = {
    StageOutcome.READY: StageStatus.PASSED,
    StageOutcome.PASSED: StageStatus.PASSED,
    StageOutcome.FAILED: StageStatus.REWORK,
    StageOutcome.REWORK: StageStatus.REWORK,
    StageOutcome.BLOCKED: StageStatus.BLOCKED,
    StageOutcome.MISSING_INPUT: StageStatus.BLOCKED,
}

RETRY_OUTCOMES = {StageOutcome.FAILED, StageOutcome.REWORK, StageOutcome.BLOCKED, StageOutcome.MISSING_INPUT}
TERMINAL_TARGETS = {"done", "intervention"}


def is_blueprint_plan(plan: MissionPlan | None) -> bool:
    return bool(plan and plan.enabled and plan.blueprint_id and plan.stages)


def derive_stage_outcome(
    decision: AgentDecision,
    stage: MissionPlanStage,
    proofs: Iterable[Proof] | None = None,
) -> StageOutcome | None:
    if decision.type in {DecisionType.QA_VERDICT, DecisionType.REPORT_QA_VERDICT}:
        verdict = str(decision.payload.get("verdict") or "").strip().lower()
        if verdict in {"approved", "passed", "pass"}:
            return StageOutcome.PASSED
        if verdict in {"needs_fixes", "needs-fixes", "fixes"}:
            return StageOutcome.REWORK
        if verdict in {"failed", "fail", "rejected"}:
            return StageOutcome.FAILED
        if verdict in {"blocked", "block"}:
            return StageOutcome.BLOCKED
        if verdict in {"missing_input", "missing-input"}:
            return StageOutcome.MISSING_INPUT
        return StageOutcome.FAILED
    if decision.type == DecisionType.REQUEST_TEST_RUN:
        return _outcome_from_proofs(proofs or [])
    if decision.type in {DecisionType.SCOPE_ROUTE, DecisionType.PROPOSE_ACCEPTANCE, DecisionType.REQUEST_QA_REVIEW, DecisionType.COMPLETE, DecisionType.APPROVE}:
        if _scope_stage_ready_without_proof(stage):
            return StageOutcome.READY
        if decision.type in {DecisionType.SCOPE_ROUTE, DecisionType.PROPOSE_ACCEPTANCE}:
            # scope_route (or its legacy propose_acceptance alias) is a PLANNING/routing decision. Attributed to
            # anything but a proof-free scope stage (e.g. Neko's recovery
            # re-scope while the blocked dev stage is current), it must never
            # mark that stage passed — the typed-plan release inside
            # apply_planning_decision is what re-arms/advances stages. Deriving
            # PASSED here phantom-passed backend_implementation live
            # (2026-07-03: task_3e2ae539 turn 1; task_826869af looped
            # neko→implement 5× while the terminal proof gate clawed the
            # phantom pass back every cycle).
            return None
        return StageOutcome.PASSED
    if decision.type in {DecisionType.HAND_OFF, DecisionType.PROPOSE_PATCH}:
        proof_ids = decision.payload.get("proof_ids")
        if isinstance(proof_ids, list) and proof_ids:
            return StageOutcome.PASSED
        proof_outcome = _outcome_from_proofs(_stage_scoped_proofs(stage, proofs or []))
        if proof_outcome is not None:
            return proof_outcome
        if _scope_stage_ready_without_proof(stage):
            return StageOutcome.READY
        return None
    if decision.type == DecisionType.BLOCK:
        return StageOutcome.BLOCKED
    if decision.type == DecisionType.REQUEST_HUMAN:
        return StageOutcome.MISSING_INPUT
    return None


def next_target(plan: MissionPlan, stage_id: str, outcome: StageOutcome) -> str:
    desired = outcome.value if hasattr(outcome, "value") else str(outcome)
    for edge in plan.edges or []:
        if str(edge.get("source") or "") == stage_id and str(edge.get("outcome") or "") == desired:
            return str(edge.get("target") or "").strip()
    return str(plan.on_unhandled or "intervention").strip() or "intervention"


def increment_stage_attempt(plan: MissionPlan, stage_id: str) -> int:
    attempts = dict(plan.stage_attempts or {})
    count = int(attempts.get(stage_id, 0) or 0) + 1
    attempts[stage_id] = count
    plan.stage_attempts = attempts
    return count


def attempts_exceeded(plan: MissionPlan, stage_id: str) -> bool:
    limit = _positive_int((plan.limits or {}).get("max_attempts_per_stage"), default=1)
    count = int((plan.stage_attempts or {}).get(stage_id, 0) or 0)
    return count >= limit


def total_attempts_exceeded(plan: MissionPlan) -> bool:
    limit = _positive_int((plan.limits or {}).get("max_total_stages"), default=20)
    total = sum(max(0, int(value or 0)) for value in (plan.stage_attempts or {}).values())
    return total >= limit


def apply_stage_outcome(task: Task, stage_id: str, outcome: StageOutcome, *, reason: str) -> str:
    plan = getattr(task, "mission_plan", None)
    if not is_blueprint_plan(plan):
        return "not_blueprint"
    stage = _stage_by_id(plan, stage_id)
    if stage is None:
        return "missing_stage"

    increment_stage_attempt(plan, stage.id)
    stage.status = OUTCOME_TO_STAGE_STATUS[outcome]
    stage.updated_at = now()

    target = next_target(plan, stage.id, outcome)
    if _retry_bound_exceeded(plan, stage.id, outcome=outcome, target=target):
        return _route_intervention(task, plan, stage, reason=f"{reason}; blueprint retry limit exceeded")
    if target == "done":
        stage.status = StageStatus.PASSED
        unfinished = _first_unfinished_stage(plan, after_stage_id=stage.id)
        if unfinished is not None:
            plan.current_stage_id = unfinished.id
            task.current_stage_id = unfinished.id
            if unfinished.owner == "qa":
                task.state = TaskState.RUNNING
            elif unfinished.owner in {"dev", "backend_dev"}:
                task.state = TaskState.RUNNING
            if unfinished.status in {StageStatus.PASSED, StageStatus.READY_FOR_QA, StageStatus.BLOCKED, StageStatus.REWORK}:
                unfinished.status = StageStatus.READY
            elif unfinished.status == StageStatus.DRAFT:
                unfinished.status = StageStatus.READY
            if unfinished.owner in {"dev", "backend_dev"} and unfinished.status == StageStatus.READY:
                unfinished.status = StageStatus.IMPLEMENTING
            unfinished.updated_at = now()
            plan.revision = int(plan.revision or 0) + 1
            task.updated_at = now()
            return unfinished.id
        plan.current_stage_id = None
        task.current_stage_id = None
        if stage.owner == "qa":
            task.state = TaskState.RUNNING
        plan.revision = int(plan.revision or 0) + 1
        task.updated_at = now()
        from .runs import BlueprintRunStore

        BlueprintRunStore().record_task_terminal(task, result="passed", ended_at=task.updated_at)
        return "done"
    if target == "intervention":
        return _route_intervention(task, plan, stage, reason=reason)

    next_stage = _stage_by_id(plan, target)
    if next_stage is None:
        return _route_intervention(task, plan, stage, reason=f"{reason}; unknown blueprint edge target {target!r}")

    plan.current_stage_id = next_stage.id
    task.current_stage_id = next_stage.id
    if next_stage.owner == "qa":
        task.state = TaskState.RUNNING
    elif next_stage.owner in {"dev", "backend_dev"}:
        task.state = TaskState.RUNNING
    if next_stage.status in {StageStatus.PASSED, StageStatus.READY_FOR_QA, StageStatus.BLOCKED, StageStatus.REWORK}:
        next_stage.status = StageStatus.REWORK if outcome in RETRY_OUTCOMES else StageStatus.READY
    elif next_stage.status == StageStatus.DRAFT:
        next_stage.status = StageStatus.READY
    if next_stage.owner in {"dev", "backend_dev"} and next_stage.status == StageStatus.READY:
        next_stage.status = StageStatus.IMPLEMENTING
    next_stage.updated_at = now()
    plan.revision = int(plan.revision or 0) + 1
    task.updated_at = now()
    return next_stage.id


def _first_unfinished_stage(plan: MissionPlan, *, after_stage_id: str | None = None) -> MissionPlanStage | None:
    stages = list(getattr(plan, "stages", None) or [])
    if after_stage_id:
        index = next((idx for idx, candidate in enumerate(stages) if candidate.id == after_stage_id), -1)
        if index >= 0:
            stages = stages[index + 1 :] + stages[:index]
    for candidate in stages:
        if candidate.status != StageStatus.PASSED:
            return candidate
    return None


def apply_decision_outcome(
    task: Task,
    decision: AgentDecision,
    *,
    stage_id: str | None = None,
    proofs: Iterable[Proof] | None = None,
    reason: str | None = None,
) -> str | None:
    plan = getattr(task, "mission_plan", None)
    if not is_blueprint_plan(plan):
        return None
    stage = _stage_by_id(plan, stage_id or plan.current_stage_id or getattr(task, "current_stage_id", None))
    if stage is None:
        stage = _stage_by_id(plan, plan.current_stage_id or getattr(task, "current_stage_id", None))
    if stage is None:
        return None
    outcome = derive_stage_outcome(decision, stage, proofs=proofs)
    if outcome is None:
        return None
    return apply_stage_outcome(task, stage.id, outcome, reason=reason or decision.summary or decision.type.value)


def _outcome_from_proofs(proofs: Iterable[Proof]) -> StageOutcome | None:
    statuses = [_proof_status(proof) for proof in proofs]
    statuses = [status for status in statuses if status]
    if not statuses:
        return None
    if any(status in {"failed", "error", "blocked"} for status in statuses):
        return StageOutcome.FAILED
    if any(status in {"missing_input", "missing-input"} for status in statuses):
        return StageOutcome.MISSING_INPUT
    if all(status in {"passed", "approved", "safe"} for status in statuses):
        return StageOutcome.PASSED
    return StageOutcome.FAILED


def _stage_scoped_proofs(stage: MissionPlanStage, proofs: Iterable[Proof]) -> list[Proof]:
    stage_id = str(getattr(stage, "id", "") or "").strip()
    if not stage_id:
        return list(proofs)
    attached_ids = {str(proof_id) for proof_id in (getattr(stage, "proof_ids", None) or []) if str(proof_id)}
    return [
        proof
        for proof in proofs
        if not getattr(proof, "stage_id", None)
        or str(getattr(proof, "stage_id", "") or "") == stage_id
        or str(getattr(proof, "id", "") or "") in attached_ids
    ]


def _scope_stage_ready_without_proof(stage: MissionPlanStage) -> bool:
    return stage.kind in {"scope", "context", "investigation", "audit"} and not (
        stage.requires_product_edit or stage.requires_visual_proof or stage.proof_recipe_id
    )


def stage_declares_required_gate(stage: MissionPlanStage) -> bool:
    """Public alias: does this blueprint stage declare a required proof gate?"""

    return _stage_has_required_gate(stage)


def _stage_has_required_gate(stage: MissionPlanStage) -> bool:
    gate = getattr(stage, "proof_gate", {}) or {}
    return bool(
        gate.get("required")
        or gate.get("required_proof_types")
        or gate.get("proof_recipe_id")
        or gate.get("commands")
        or getattr(stage, "requires_visual_proof", False)
    )


def _proof_status(proof: Proof) -> str | None:
    metadata = getattr(proof, "metadata", None) or {}
    status = str(metadata.get("status") or metadata.get("verdict") or "").strip().lower()
    if status:
        return status
    if "exit_code" in metadata:
        try:
            return "passed" if int(metadata.get("exit_code")) == 0 else "failed"
        except (TypeError, ValueError):
            return "failed"
    return None


def _retry_bound_exceeded(plan: MissionPlan, stage_id: str, *, outcome: StageOutcome, target: str) -> bool:
    if target in TERMINAL_TARGETS:
        return False
    if outcome in RETRY_OUTCOMES:
        return attempts_exceeded(plan, stage_id) or total_attempts_exceeded(plan)
    return False


def _route_intervention(task: Task, plan: MissionPlan, stage: MissionPlanStage, *, reason: str) -> str:
    stage.status = StageStatus.BLOCKED
    stage.updated_at = now()
    plan.current_stage_id = stage.id
    task.current_stage_id = stage.id
    if task.state not in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
        task.state = TaskState.RUNNING
    _record_escalation_evidence(task, stage, reason=reason)
    note = f"blueprint intervention at {stage.id}: {reason}"
    if note not in task.operator_notes:
        task.operator_notes.append(note)
    plan.revision = int(plan.revision or 0) + 1
    task.updated_at = now()
    return "intervention"


def _record_proof_gate_evidence(task: Task, stage: MissionPlanStage, gate, *, reason: str) -> None:
    missing = [str(item) for item in (getattr(gate, "missing", None) or []) if str(item)]
    warnings = [str(item) for item in (getattr(gate, "warnings", None) or []) if str(item)]
    if not missing and not warnings:
        return
    _append_hud_evidence(
        task,
        {
            "kind": "proof_gate",
            "severity": "warning",
            "stage_id": stage.id,
            "summary": "Required proof is missing or stale; goal owner must adjudicate.",
            "missing": missing[:10],
            "warnings": warnings[:10],
            "recommended_owner": "neko_supervisor",
            "reason": str(reason or "")[:500],
        },
    )


def _record_escalation_evidence(task: Task, stage: MissionPlanStage, *, reason: str) -> None:
    _append_hud_evidence(
        task,
        {
            "kind": "blocked_escalation",
            "severity": "warning",
            "stage_id": stage.id,
            "summary": "Stage is blocked for goal-owner adjudication; this is recoverable.",
            "missing": [],
            "warnings": [str(reason or "")[:500]] if reason else [],
            "recommended_owner": "neko_supervisor",
            "reason": str(reason or "")[:500],
        },
    )


def _append_hud_evidence(task: Task, evidence: dict) -> None:
    root = task.harness_self_heal if isinstance(getattr(task, "harness_self_heal", None), dict) else {}
    existing = root.get("evidence_stack") if isinstance(root.get("evidence_stack"), list) else []
    key = (
        str(evidence.get("kind") or ""),
        str(evidence.get("stage_id") or ""),
        tuple(str(item) for item in evidence.get("missing") or []),
        tuple(str(item) for item in evidence.get("warnings") or []),
    )
    deduped = [
        item
        for item in existing
        if not (
            isinstance(item, dict)
            and (
                str(item.get("kind") or ""),
                str(item.get("stage_id") or ""),
                tuple(str(value) for value in item.get("missing") or []),
                tuple(str(value) for value in item.get("warnings") or []),
            )
            == key
        )
    ]
    safe = {name: value for name, value in evidence.items() if value not in (None, [], {})}
    safe["recorded_at"] = now().isoformat()
    root["evidence_stack"] = [*deduped, safe][-10:]
    task.harness_self_heal = root


def _stage_by_id(plan: MissionPlan, stage_id: str | None) -> MissionPlanStage | None:
    target = str(stage_id or "").strip()
    if not target:
        return None
    return next((stage for stage in plan.stages if stage.id == target), None)


def _positive_int(value, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default
