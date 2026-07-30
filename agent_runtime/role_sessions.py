from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from .decision_schema import DecisionType
from .models import AgentRun
from .runtime_config import ContinuousRoleSessionConfig
from .states import RunState, TaskState
from .store import _safe_session_id


CONTINUE_SAME_RUN = "continue_same_run"
CLOSE_COMPLETED = "close_completed"
CLOSE_HANDOFF = "close_handoff"
CLOSE_BLOCKED = "close_blocked"
CLOSE_WATCHDOG = "close_watchdog"
CLOSE_INVALID = "close_invalid"

_BOUNDARY_DECISIONS = {
    DecisionType.BLOCK.value,
    DecisionType.REQUEST_HUMAN.value,
    DecisionType.QA_VERDICT.value,
    DecisionType.REPORT_QA_VERDICT.value,
    DecisionType.APPROVE.value,
    DecisionType.COMPLETE.value,
}


@dataclass(slots=True)
class RoleSessionEnvelope:
    task_id: str
    persona_id: str
    stage_id: str | None
    opened_run_id: str
    session_id: str | None = None
    envelope_id: str = field(default_factory=lambda: f"role_{uuid.uuid4().hex[:8]}")
    decision_count: int = 0
    model_invocation_count: int = 0
    proof_count: int = 0
    continuation_count: int = 0
    watchdog_warnings: int = 0
    api_calls_total: int = 0
    input_tokens_total: int = 0
    output_tokens_total: int = 0
    total_tokens_total: int = 0
    tool_turns_total: int = 0
    close_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ContinueDecision:
    action: str
    close_reason: str
    would_continue: bool = False
    summary: str = ""

    @property
    def should_continue(self) -> bool:
        return self.action == CONTINUE_SAME_RUN


def observe_enabled_config(config: ContinuousRoleSessionConfig) -> ContinuousRoleSessionConfig:
    """Evaluate continuation policy without enabling behavior."""

    return replace(config, enabled=True)


def should_continue_role_session(
    *,
    config: ContinuousRoleSessionConfig,
    before_task: Task,
    after_task: Task,
    persona_id: str,
    run: AgentRun,
    decision_type: str,
    envelope: RoleSessionEnvelope,
    next_action_type: str,
    next_persona_id: str | None,
    open_incident_count: int = 0,
    proof_ids_added: list[str] | None = None,
    proof_statuses: list[str] | None = None,
    deterministic_handoff_applied: bool = False,
    is_live_runtime: bool = False,
) -> ContinueDecision:
    if not config.enabled:
        return _close(CLOSE_COMPLETED, "config_disabled")

    proof_ids_added = proof_ids_added or []
    proof_statuses = [str(status or "").strip().lower() for status in (proof_statuses or [])]
    task_state = after_task.state if isinstance(after_task.state, TaskState) else TaskState(after_task.state)
    run_state = run.state if isinstance(run.state, RunState) else RunState(run.state)

    if after_task.id != before_task.id:
        return _close(CLOSE_INVALID, "task_changed")
    if task_state in {TaskState.DONE, TaskState.CANCELLED}:
        return _close(CLOSE_COMPLETED, f"task_{task_state.value}")
    if task_state == TaskState.BLOCKED:
        return _close(CLOSE_BLOCKED, "task_blocked")
    if run_state in {RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED}:
        return _close(CLOSE_INVALID, f"run_{run_state.value}")
    if config.close_on_open_incident and open_incident_count:
        return _close(CLOSE_BLOCKED, "open_incident")
    if decision_type in _BOUNDARY_DECISIONS:
        if decision_type in {DecisionType.QA_VERDICT.value, DecisionType.REPORT_QA_VERDICT.value}:
            return _close(CLOSE_COMPLETED, "qa_verdict")
        if decision_type in {DecisionType.BLOCK.value, DecisionType.REQUEST_HUMAN.value}:
            return _close(CLOSE_BLOCKED, decision_type)
        return _close(CLOSE_COMPLETED, decision_type)
    if deterministic_handoff_applied:
        return _close(CLOSE_HANDOFF, "deterministic_proof_handoff")
    if next_action_type in {"noop", "complete_task"}:
        return _close(CLOSE_COMPLETED, next_action_type)
    if config.close_on_state_owner_change and next_persona_id != persona_id:
        return _close(CLOSE_HANDOFF, "owner_change")
    if config.close_on_state_owner_change and after_task.current_stage_id != before_task.current_stage_id:
        return _close(CLOSE_HANDOFF, "stage_change")
    if proof_statuses:
        failed = [status for status in proof_statuses if status not in {"passed", "approved"}]
        if failed and not config.continue_after_failed_proof:
            return _close(CLOSE_BLOCKED, "proof_failed")
        if not failed and not config.continue_after_passing_proof:
            return _close(CLOSE_COMPLETED, "proof_collected")
    if envelope.decision_count >= config.max_decisions_per_envelope:
        return _close(CLOSE_WATCHDOG, "max_decisions_per_envelope")
    if envelope.proof_count > config.max_proofs_per_envelope:
        return _close(CLOSE_WATCHDOG, "max_proofs_per_envelope")
    if envelope.continuation_count >= config.max_continuations_per_stage:
        return _close(CLOSE_WATCHDOG, "max_continuations_per_stage")
    if is_live_runtime and not _safe_session_id(run.session_id):
        return _close(CLOSE_INVALID, "same_session_not_safe")
    if config.close_on_budget_warning and _has_budget_warning(run):
        return _close(CLOSE_WATCHDOG, "budget_warning")
    return ContinueDecision(CONTINUE_SAME_RUN, "same_owner_same_stage", would_continue=True, summary="same role owns refreshed task")


def update_envelope_after_invocation(envelope: RoleSessionEnvelope, run: AgentRun, *, proof_ids_added: list[str]) -> None:
    envelope.decision_count += 1
    envelope.model_invocation_count += 1
    envelope.proof_count += len(proof_ids_added or [])
    envelope.session_id = _safe_session_id(run.session_id)
    llm = run.llm if isinstance(run.llm, dict) else {}
    envelope.api_calls_total += _safe_int(llm.get("api_calls"))
    envelope.input_tokens_total += _safe_int(llm.get("input_tokens"))
    envelope.output_tokens_total += _safe_int(llm.get("output_tokens"))
    envelope.total_tokens_total += _safe_int(llm.get("total_tokens"))
    envelope.tool_turns_total += _safe_int(llm.get("tool_turns"))
    if _has_budget_warning(run):
        envelope.watchdog_warnings += 1


def role_session_payload(
    envelope: RoleSessionEnvelope,
    *,
    run: AgentRun,
    close_reason: str | None = None,
    next_action_before: str | None = None,
    next_action_after: str | None = None,
    proof_ids_added: list[str] | None = None,
    incident_ids_opened: list[str] | None = None,
    would_continue: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "envelope_id": envelope.envelope_id,
        "run_id": run.id,
        "task_id": envelope.task_id,
        "persona_id": envelope.persona_id,
        "stage_id": envelope.stage_id,
        "decision_count": envelope.decision_count,
        "model_invocation_count": envelope.model_invocation_count,
        "continuation_count": envelope.continuation_count,
        "proof_count": envelope.proof_count,
        "watchdog_warnings": envelope.watchdog_warnings,
        "session_id_present": bool(_safe_session_id(run.session_id)),
        "api_calls_total": envelope.api_calls_total,
        "input_tokens_total": envelope.input_tokens_total,
        "output_tokens_total": envelope.output_tokens_total,
        "total_tokens_total": envelope.total_tokens_total,
        "tool_turns_total": envelope.tool_turns_total,
    }
    if close_reason:
        payload["close_reason"] = close_reason
    if next_action_before:
        payload["next_action_before"] = next_action_before
    if next_action_after:
        payload["next_action_after"] = next_action_after
    if proof_ids_added is not None:
        payload["proof_ids_added_count"] = len(proof_ids_added)
    if incident_ids_opened is not None:
        payload["incident_ids_opened_count"] = len(incident_ids_opened)
    if would_continue is not None:
        payload["would_continue"] = bool(would_continue)
    return payload


def role_session_progress(envelope: RoleSessionEnvelope, *, close_reason: str | None = None) -> dict[str, Any]:
    data = {
        "envelope_id": envelope.envelope_id,
        "decision_count": envelope.decision_count,
        "model_invocation_count": envelope.model_invocation_count,
        "continuation_count": envelope.continuation_count,
        "proof_count": envelope.proof_count,
        "api_calls_total": envelope.api_calls_total,
        "input_tokens_total": envelope.input_tokens_total,
        "output_tokens_total": envelope.output_tokens_total,
        "total_tokens_total": envelope.total_tokens_total,
        "tool_turns_total": envelope.tool_turns_total,
        "watchdog_warnings": envelope.watchdog_warnings,
    }
    if close_reason:
        data["close_reason"] = close_reason
    return data


def _close(action: str, reason: str) -> ContinueDecision:
    return ContinueDecision(action, reason, would_continue=False, summary=reason.replace("_", " "))


def _has_budget_warning(run: AgentRun) -> bool:
    progress = run.progress if isinstance(run.progress, dict) else {}
    return bool(
        progress.get("phase") == "runaway_warning"
        or progress.get("severity") == "critical"
        or progress.get("loop_warning")
    )


def _safe_int(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, number)
