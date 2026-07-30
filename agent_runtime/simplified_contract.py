from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_time import now

from .decision_schema import AgentDecision, DecisionType
from .events import EventLog
from .models import Event
from .runtime_config import RuntimeConfig


COLLAPSED_SIGNAL_TYPES = frozenset(
    {
        DecisionType.HAND_OFF,
        DecisionType.BLOCK,
        DecisionType.ESCALATE,
        DecisionType.SCOPE_ROUTE,
        DecisionType.RESOLVE_INCIDENT,
        DecisionType.TRIAGE_ISSUE_DISCOVERY,
        DecisionType.REQUEST_SCREENSHOT,
        DecisionType.REQUEST_VIDEO,
        DecisionType.QA_VERDICT,
    }
)


def public_decision_type(decision_type: DecisionType | str | None) -> DecisionType | None:
    if decision_type is None:
        return None
    try:
        resolved = decision_type if isinstance(decision_type, DecisionType) else DecisionType(str(decision_type))
    except Exception:
        return None
    return resolved


def public_decision_type_value(decision_type: DecisionType | str | None) -> str | None:
    resolved = public_decision_type(decision_type)
    return resolved.value if resolved is not None else None


@dataclass(frozen=True, slots=True)
class DecisionProjection:
    public_decision: AgentDecision
    execution_decision: AgentDecision
    public_type: DecisionType
    execution_type: DecisionType
    mode: str
    shimmed: bool = False
    blocked_reason: str | None = None
    source_type: DecisionType | None = None


def simplified_contract_enabled(config: RuntimeConfig | None) -> bool:
    simplified = getattr(config, "simplified_agent_contract", None)
    return bool(getattr(simplified, "enabled", False))


def expose_only_simplified_actions(config: RuntimeConfig | None) -> bool:
    simplified = getattr(config, "simplified_agent_contract", None)
    return simplified_contract_enabled(config) and bool(getattr(simplified, "expose_only_simplified_actions", False))


def keep_internal_state_machine(config: RuntimeConfig | None) -> bool:
    simplified = getattr(config, "simplified_agent_contract", None)
    return bool(getattr(simplified, "keep_internal_state_machine", True))


def collapsed_signal_for(decision_type: DecisionType) -> DecisionType:
    return decision_type


def project_decision_for_execution(task: Any, decision: AgentDecision, *, config: RuntimeConfig | None, actor: str, run_id: str | None, event_log: EventLog | None = None) -> DecisionProjection:
    if not simplified_contract_enabled(config):
        projection = DecisionProjection(
            public_decision=decision,
            execution_decision=decision,
            public_type=decision.type,
            execution_type=decision.type,
            mode="legacy",
            source_type=decision.type,
        )
        _record_parity(task, projection, actor=actor, run_id=run_id, event_log=event_log)
        return projection

    public_type = collapsed_signal_for(decision.type)
    if decision.type not in COLLAPSED_SIGNAL_TYPES:
        projection = DecisionProjection(
            public_decision=decision,
            execution_decision=decision,
            public_type=public_type,
            execution_type=decision.type,
            mode="simplified_rejected",
            blocked_reason=f"decision {decision.type.value} is not in the collapsed signal set",
            source_type=decision.type,
        )
        _record_parity(task, projection, actor=actor, run_id=run_id, event_log=event_log)
        return projection

    public_decision = decision
    execution = decision
    mode = "simplified"
    shimmed = False
    if keep_internal_state_machine(config):
        execution = _internal_execution_decision(task, decision)
        if execution.type != decision.type:
            mode = "simplified_internal_projection"
            shimmed = True

    projection = DecisionProjection(
        public_decision=public_decision,
        execution_decision=execution,
        public_type=public_type,
        execution_type=execution.type,
        mode=mode,
        shimmed=shimmed,
        source_type=decision.type,
    )
    _record_parity(task, projection, actor=actor, run_id=run_id, event_log=event_log)
    return projection


def _internal_execution_decision(task: Any, decision: AgentDecision) -> AgentDecision:
    if decision.type == DecisionType.HAND_OFF:
        return decision
    if decision.type == DecisionType.SCOPE_ROUTE:
        return decision
    if decision.type == DecisionType.QA_VERDICT:
        return decision
    if decision.type == DecisionType.ESCALATE:
        return decision
    return decision


def legacy_acceptance_decision_from_scope_route(task: Any, decision: AgentDecision) -> AgentDecision:
    if decision.type != DecisionType.SCOPE_ROUTE:
        return decision
    payload = dict(decision.payload or {})
    proof_gate = payload.get("proof_gate") if isinstance(payload.get("proof_gate"), dict) else {"required": False, "required_proof_types": [], "minimum_status": "passed", "visual_required": False}
    target_owner = str(payload.get("target_owner") or "dev").strip()
    target_repo = str(payload.get("target_repo") or "none").strip()
    handoff_packet = {
        "packet_kind": "fresh_scope",
        "mission_phase": "scope_route",
        "handoff_mode": "single_specialist",
        "target_owner": target_owner,
        "target_repo": target_repo,
        "proof_gate": proof_gate,
    }
    if target_owner in {"dev", "backend_dev", "qa"}:
        handoff_packet["join_gate"] = {"release_condition": "Target owner completes the current typed stage with Harness-verified proof."}
    execution_payload = {
        "objective": payload.get("objective") or decision.summary,
        "acceptance_criteria": payload.get("acceptance_criteria") or list(getattr(task, "acceptance_criteria", []) or ["Routed owner completes the stage."]),
        "non_goals": payload.get("non_goals", []),
        "affected_repos": [target_repo] if target_repo and target_repo != "none" else [],
        "suggested_roles": [target_owner] if target_owner else [],
        "requires_visual_proof": bool(payload.get("requires_visual_proof", False)),
        "risk_flags": payload.get("risk_flags", []),
        "release_stage_id": payload.get("release_stage_id"),
        "handoff_packet": handoff_packet,
    }
    if str(payload.get("scope_override_reason") or "").strip():
        execution_payload["scope_override_reason"] = str(payload.get("scope_override_reason")).strip()
    return AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary=decision.summary or "Collapsed scope_route signal.",
        rationale=decision.rationale or "Apply scope_route through the retained typed-plan routing mechanics.",
        payload=execution_payload,
        requires_approval=decision.requires_approval,
        schema_version=decision.schema_version,
    )


def legacy_qa_review_decision_from_qa_verdict(decision: AgentDecision) -> AgentDecision:
    if decision.type != DecisionType.QA_VERDICT:
        return decision
    payload = dict(decision.payload or {})
    qa_coverage = {
        "backend_contract": "not_required",
        "launcher_integration": "not_required",
        "visual_or_mcp": "not_required",
        "cross_stack_join": "not_required",
    }
    simplified_coverage = payload.get("coverage")
    if isinstance(simplified_coverage, dict):
        for key in qa_coverage:
            if key in simplified_coverage:
                qa_coverage[key] = simplified_coverage[key]
    return AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary=decision.summary or "Collapsed qa_verdict signal.",
        rationale=decision.rationale or "Apply qa_verdict through the retained QA review mechanics.",
        payload={
            "review_scope": "implementation",
            "verdict": payload.get("verdict") or "blocked",
            "proof_ids": payload.get("proof_ids", []),
            "findings": payload.get("findings", []),
            "qa_review": {
                "coverage": qa_coverage,
                "decision_basis": "harness_verified_proof",
                "remaining_gaps": payload.get("findings", []),
                "next_owner": "harness",
            },
        },
        requires_approval=decision.requires_approval,
        schema_version=decision.schema_version,
    )


def legacy_issue_decision_from_escalate(decision: AgentDecision) -> AgentDecision:
    if decision.type != DecisionType.ESCALATE:
        return decision
    payload = dict(decision.payload or {})
    return AgentDecision(
        type=DecisionType.REPORT_ISSUE_DISCOVERY,
        summary=decision.summary or "Collapsed escalate signal.",
        rationale=decision.rationale or "Apply escalate through the retained issue-discovery mechanics.",
        payload={
            "title": payload.get("title") or "Escalated issue",
            "summary": payload.get("summary") or decision.summary,
            "severity": payload.get("severity", "medium"),
            "relationship_hint": "escalate",
            "evidence": payload.get("evidence", []),
        },
        requires_approval=decision.requires_approval,
        schema_version=decision.schema_version,
    )


def _record_parity(task: Any, projection: DecisionProjection, *, actor: str, run_id: str | None, event_log: EventLog | None) -> None:
    log = event_log or EventLog()
    status = "rejected" if projection.blocked_reason else "mapped" if projection.shimmed else "native"
    log.append(
        Event(
            ts=now(),
            type="decision_contract.parity",
            task_id=task.id,
            run_id=run_id,
            persona_id=actor,
            payload={
                "mode": projection.mode,
                "status": status,
                "public_decision_type": projection.public_type.value,
                "execution_decision_type": projection.execution_type.value,
                "legacy_decision_type": (projection.source_type or projection.public_decision.type).value,
                "shimmed": projection.shimmed,
                "blocked_reason": projection.blocked_reason,
            },
        )
    )
