from __future__ import annotations

from dataclasses import dataclass, field

from hermes_time import now

from .actions import HarnessAction, HarnessActionType
from .context_requests import has_unresolved_context_request
from .decision_schema import AgentDecision
from .dev_discipline import needs_supervisor_slicing
from .models import Event, Task, TaskStage
from .mission_plan import (
    all_blocking_stages_passed,
    blocking_stages_ready_for_qa,
    current_plan_stage,
    has_typed_plan,
    mission_plan_routing_enabled,
    next_unblocked_stage,
    release_next_stage,
)
from .packets import record_decision_packets
from .packets import latest_packet
from .planning import LAUNCHER_RELEASED_BY_NEKO_FLAG, _all_stages_dev_complete, _all_stages_passed, _advance_to_next_dev_stage, _has_backend_contract_proof, _is_cross_stack_backend_first, _needs_cross_stack_launcher_completion, _stage_mentions_launcher, apply_planning_decision
from .reconciler import reconcile_task
from .recovery_flags import block_recovery_attempted_for_current_signal
from .scope_control import needs_pm_triage_before_dev
from .states import StageStatus, TaskState


QA_COORDINATION_RELEASED_FLAG = "neko_qa_coordination_released"


@dataclass(frozen=True, slots=True)
class StateMachineResult:
    from_state: TaskState
    to_state: TaskState
    events: list[Event] = field(default_factory=list)
    blocked_reason: str | None = None


class MissionStateMachine:
    """Deterministic mission progression authority.

    The persisted model is still named ``Task`` for schema compatibility, but new
    orchestration code should treat each record as a mission/goal.
    """

    def __init__(self, *, proof_store=None, config=None):
        self.proof_store = proof_store
        self.config = config

    def next_action(self, mission: Task) -> HarnessAction:
        state = mission.state if isinstance(mission.state, TaskState) else TaskState(mission.state)
        if getattr(mission, "open_incident_ids", None):
            if state == TaskState.BLOCKED and block_recovery_attempted_for_current_signal(mission):
                return HarnessAction(HarnessActionType.NOOP, mission.id, reason="blocked with open incidents waiting on intervention")
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="mission has open incidents; Neko must route recovery or block")
        if mission_plan_routing_enabled(self.config) and has_typed_plan(mission):
            typed = self._typed_next_action(mission, state=state)
            if typed is not None:
                return typed
        if state == TaskState.BLOCKED and not getattr(mission, "open_incident_ids", None):
            if _current_launcher_stage_waits_for_cross_stack_release(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="Launcher stage is waiting for Neko backend-proof join release")
            if _ensure_pending_dev_handoff_stage(mission):
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="resume deterministic Dev handoff from latest Neko packet")
            if _has_failed_current_stage_test_proof(mission, proof_store=self.proof_store):
                if _same_stage_retry_blocked(mission):
                    return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="failed proof retry needs Neko self-heal before another same-stage Dev run")
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="needs dev retry after failed current-stage command proof")
            if _has_resolved_incident_only_qa_block(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason="retry QA after resolved incident-only blocker")
            if _has_resolved_qa_output_incident(mission):
                return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason="retry QA after resolved QA output incident")
            if _has_blocked_qa_verdict(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="needs dev recovery from QA blocked verdict")
            if not block_recovery_attempted_for_current_signal(mission):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead blocked-state recovery")
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason="blocked waiting on intervention")
        reconciliation = reconcile_task(mission)
        if reconciliation.needs_supervisor:
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason=f"needs Neko transition reconciliation: {reconciliation.findings[0].kind}")
        if state in {TaskState.CREATED, TaskState.PM_TRIAGE}:
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead scoping")
        if state in {
            TaskState.PM_READY_FOR_DEV,
            TaskState.DEV_AUDIT,
            TaskState.DEV_STAGE_PLANNING,
            TaskState.DEV_TEST_DESIGN,
        }:
            if _current_launcher_stage_waits_for_cross_stack_release(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="Launcher stage is waiting for Neko backend-proof join release")
            if needs_pm_triage_before_dev(mission):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead issue discovery triage")
            if has_unresolved_context_request(mission):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead to resolve post-scoping context request")
            if needs_supervisor_slicing(mission):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead to slice broad specialist mission before Dev")
            return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="needs dev planning")
        if state == TaskState.QA_REVIEW_PLAN:
            return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason="needs QA plan review")
        if state in {TaskState.DEV_IMPLEMENTING, TaskState.QA_NEEDS_FIXES}:
            if _current_launcher_stage_waits_for_cross_stack_release(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="Launcher stage is waiting for Neko backend-proof join release")
            if _ensure_pending_dev_handoff_stage(mission):
                if _is_cross_stack_backend_first(mission) and _has_backend_contract_proof(mission, proof_store=self.proof_store):
                    mission.risk_flags = list(getattr(mission, "risk_flags", None) or [])
                    if LAUNCHER_RELEASED_BY_NEKO_FLAG not in mission.risk_flags:
                        mission.risk_flags.append(LAUNCHER_RELEASED_BY_NEKO_FLAG)
                if _current_launcher_stage_waits_for_cross_stack_release(mission, proof_store=self.proof_store):
                    return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="pending Launcher handoff is waiting for backend-proof join release")
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="resume deterministic Dev handoff from latest Neko packet")
            if _has_failed_current_stage_test_proof(mission, proof_store=self.proof_store) and _same_stage_retry_blocked(mission):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="failed proof retry needs Neko self-heal before another same-stage Dev run")
            return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="needs dev implementation/fix pass")
        if state == TaskState.DEV_READY_FOR_QA:
            if _needs_cross_stack_launcher_completion(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead to release Launcher side after backend proof")
            if not _all_stages_dev_complete(mission):
                if QA_COORDINATION_RELEASED_FLAG not in (getattr(mission, "risk_flags", None) or []):
                    return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead to release next specialist after proof-backed join gate")
                mission.risk_flags = [flag for flag in (getattr(mission, "risk_flags", None) or []) if flag != QA_COORDINATION_RELEASED_FLAG]
                _advance_to_next_dev_stage(mission)
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="needs remaining stages before QA")
            if QA_COORDINATION_RELEASED_FLAG not in (getattr(mission, "risk_flags", None) or []):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead to coordinate multi-Dev QA handoff")
            return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason="needs QA verification")
        if state == TaskState.QA_TESTING:
            if not _all_stages_dev_complete(mission):
                _advance_to_next_dev_stage(mission)
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="needs remaining stages before QA")
            return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason="needs QA verification")
        if state in {TaskState.QA_APPROVED, TaskState.PM_PROOF_REVIEW, TaskState.PM_READY_FOR_INTEGRATION} and getattr(mission, "proof_ids", None):
            if _needs_cross_stack_launcher_completion(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead to release Launcher side before terminal close")
            if getattr(mission, "requires_visual_proof", False) and not _has_visual_proof(mission, proof_store=self.proof_store):
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="requires Launcher implementation and visual proof before terminal close")
            if _all_stages_passed(mission):
                return HarnessAction(HarnessActionType.COMPLETE_TASK, mission.id, reason="all stages passed with proof-backed QA approval")
            _advance_to_next_dev_stage(mission)
            return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="needs remaining stages before terminal close")
        if state in {TaskState.QA_APPROVED, TaskState.PM_PROOF_REVIEW, TaskState.PM_READY_FOR_INTEGRATION}:
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko Mission Lead proof/integration review")
        if state == TaskState.BLOCKED and getattr(mission, "open_incident_ids", None):
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="needs Neko intervention steering")
        return HarnessAction(HarnessActionType.NOOP, mission.id, reason="no eligible mission action")

    def _typed_next_action(self, mission: Task, *, state: TaskState) -> HarnessAction | None:
        if state in {TaskState.DONE, TaskState.CANCELLED}:
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason="typed mission is terminal")
        if state == TaskState.BLOCKED and getattr(mission, "open_incident_ids", None):
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason="typed mission has open incidents; Neko must route recovery")
        if state == TaskState.QA_NEEDS_FIXES:
            return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason="typed mission QA requested fixes or missing proof; return to Dev")
        current = current_plan_stage(mission)
        next_stage = next_unblocked_stage(mission, include_qa=True)
        if next_stage is None:
            ready, missing = blocking_stages_ready_for_qa(mission, proof_store=self.proof_store)
            if ready and all_blocking_stages_passed(mission) and (state == TaskState.QA_APPROVED or _qa_release_stage_passed(mission)):
                return HarnessAction(HarnessActionType.COMPLETE_TASK, mission.id, reason="typed mission QA approved and all blocking stages passed")
            if not ready:
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason=f"typed mission missing QA blockers: {missing[:3]}")
            return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason="typed mission needs QA verdict")
        if current is not None and current.id != next_stage.id and current.status in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
            if next_stage.owner == "qa":
                ready, missing = blocking_stages_ready_for_qa(mission, proof_store=self.proof_store)
                if ready:
                    release_next_stage(mission, next_stage.id)
                    return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason=f"typed mission released {current.id} to QA")
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason=f"typed mission QA blocked by incomplete stages: {missing[:3]}")
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason=f"typed mission requires Neko release from {current.id} to {next_stage.id}")
        if mission.current_stage_id != next_stage.id:
            release_next_stage(mission, next_stage.id)
        if next_stage.owner == "neko_supervisor":
            return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason=f"typed mission stage {next_stage.id} needs Neko")
        if next_stage.owner in {"dev", "backend_dev"}:
            if _typed_stage_context_loop_needs_neko(mission, next_stage):
                return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason=f"typed mission stage {next_stage.id} has sufficient context; {next_stage.owner} must deliver findings or block without more file reads")
            return HarnessAction(HarnessActionType.RUN_DEV, mission.id, reason=f"typed mission stage {next_stage.id} needs {next_stage.owner}")
        if next_stage.owner == "qa":
            ready, missing = blocking_stages_ready_for_qa(mission, proof_store=self.proof_store)
            if not ready:
                return HarnessAction(HarnessActionType.RUN_NEKO_SUPERVISOR, mission.id, reason=f"typed mission QA blocked by incomplete stages: {missing[:3]}")
            return HarnessAction(HarnessActionType.RUN_QA, mission.id, reason="typed mission all blocking stages ready; QA may verify")
        if next_stage.owner == "harness":
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"typed mission stage {next_stage.id} is harness-owned")
        return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"typed mission waits on {next_stage.owner}")

    def apply_decision(self, mission: Task, decision: AgentDecision, *, actor: str, task_store=None, incident_store=None, proof_store=None, run_id: str | None = None, normal_worker_flow: bool = False, mission_plan_flow: bool | None = None) -> StateMachineResult:
        before = mission.state if isinstance(mission.state, TaskState) else TaskState(mission.state)
        if mission_plan_flow is None:
            mission_plan_flow = mission_plan_routing_enabled(self.config)
        apply_planning_decision(mission, decision, actor=actor, task_store=task_store, incident_store=incident_store, proof_store=proof_store, run_id=run_id, normal_worker_flow=normal_worker_flow, mission_plan_flow=mission_plan_flow)
        record_decision_packets(mission, decision, actor=actor, run_id=run_id, stage_id=getattr(mission, "current_stage_id", None))
        after = mission.state if isinstance(mission.state, TaskState) else TaskState(mission.state)
        events: list[Event] = []
        if before != after:
            events.append(
                Event(
                    ts=now(),
                    type="mission.transition",
                    task_id=mission.id,
                    run_id=None,
                    persona_id=actor,
                    payload={
                        "from": before.value,
                        "to": after.value,
                        "actor": actor,
                        "reason": decision.summary,
                    },
                )
            )
        return StateMachineResult(from_state=before, to_state=after, events=events)


def _has_visual_proof(mission: Task, *, proof_store=None) -> bool:
    if proof_store is None:
        return False
    for proof_id in list(getattr(mission, "proof_ids", []) or []):
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        if proof_type in {"screenshot", "video"} and getattr(proof, "redaction_status", "") == "safe" and getattr(proof, "path_or_value", None):
            return True
    return False


def _has_blocked_qa_verdict(mission: Task, *, proof_store=None) -> bool:
    if "qa_blocked_verdict_needs_dev_recovery" in (getattr(mission, "risk_flags", None) or []):
        return True
    if proof_store is None:
        return False
    for proof_id in list(getattr(mission, "proof_ids", []) or [])[-20:]:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        if proof_type == "qa_verdict" and str((proof.metadata or {}).get("verdict", "")).strip() == "blocked":
            return True
    return False


def _qa_release_stage_passed(mission: Task) -> bool:
    plan = getattr(mission, "mission_plan", None)
    if not plan:
        return False
    return any(
        stage.owner == "qa"
        and stage.kind == "qa_verdict"
        and stage.status == StageStatus.PASSED
        for stage in plan.stages
    )


def _typed_stage_context_loop_needs_neko(mission: Task, stage, *, threshold: int = 2) -> bool:
    if stage is None:
        return False
    owner = str(getattr(stage, "owner", "") or "")
    if owner not in {"dev", "backend_dev"}:
        return False
    kind = str(getattr(stage, "kind", "") or "").lower()
    objective = str(getattr(stage, "objective", "") or "").lower()
    title = str(getattr(stage, "title", "") or "").lower()
    is_no_edit_context = (
        kind in {"context", "investigation", "audit"}
        or "no-product-edit" in objective
        or "investigation" in objective
        or "investigation" in title
    )
    if not is_no_edit_context:
        return False
    stage_id = str(getattr(stage, "id", "") or "")
    relevant = []
    current_stage_id = str(getattr(mission, "current_stage_id", "") or "")
    for req in getattr(mission, "context_requests", []) or []:
        if not isinstance(req, dict):
            continue
        actor = str(req.get("actor") or "")
        status = str(req.get("status") or "")
        if actor not in {owner, "dev", "backend_dev"}:
            continue
        if status not in {"fulfilled", "fulfilled_partial", "superseded"}:
            continue
        req_stage_id = str(req.get("stage_id") or "")
        reason = str(req.get("reason") or "")
        if req_stage_id and stage_id and req_stage_id != stage_id:
            continue
        if stage_id and stage_id in reason:
            relevant.append(req)
        elif req_stage_id and req_stage_id == stage_id:
            relevant.append(req)
        elif not stage_id:
            relevant.append(req)
        elif current_stage_id == stage_id and not req_stage_id:
            # Legacy context requests did not persist stage_id. While a typed
            # stage is active, count same-owner fulfilled requests as current
            # stage evidence so a no-edit investigation cannot burn repeated
            # same-session context turns indefinitely.
            relevant.append(req)
        else:
            continue
    return len(relevant) >= threshold


def _has_resolved_incident_only_qa_block(mission: Task, *, proof_store=None) -> bool:
    if proof_store is None:
        return False
    for proof_id in list(getattr(mission, "proof_ids", []) or [])[-20:]:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        metadata = proof.metadata or {}
        if proof_type != "qa_verdict" or str(metadata.get("verdict", "")).strip() != "blocked":
            continue
        findings = metadata.get("findings") or []
        blocking = [item for item in findings if str(item.get("severity", "")).strip().lower() == "blocking"]
        if blocking and all(str(item.get("kind", "")).strip() == "open_incidents" for item in blocking):
            return True
    return False


def _has_resolved_qa_output_incident(mission: Task) -> bool:
    if QA_COORDINATION_RELEASED_FLAG not in (getattr(mission, "risk_flags", None) or []):
        return False
    if getattr(mission, "open_incident_ids", None):
        return False
    if not getattr(mission, "stages", None):
        return False
    if not _all_stages_dev_complete(mission):
        return False
    if not getattr(mission, "proof_ids", None):
        return False
    return True


def _has_failed_current_stage_test_proof(mission: Task, *, proof_store=None) -> bool:
    if proof_store is None or not getattr(mission, "current_stage_id", None):
        return False
    for proof_id in list(getattr(mission, "proof_ids", []) or [])[-20:]:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        metadata = proof.metadata or {}
        if (
            proof_type == "test_run"
            and proof.stage_id == mission.current_stage_id
            and str(metadata.get("status", "")).strip().lower() == "failed"
        ):
            return True
    return False


def _same_stage_retry_blocked(mission: Task) -> bool:
    state = _stage_self_heal_state(mission)
    counters = state.get("counters") if isinstance(state.get("counters"), dict) else {}
    retry_count = _safe_int(counters.get("same_stage_retry_count"))
    if retry_count < 1:
        return False
    if _environment_changed(state.get("environment_fingerprint_status")):
        return False
    self_heal = state.get("self_heal") if isinstance(state.get("self_heal"), dict) else {}
    if _safe_int(self_heal.get("attempt_number")) > 0 and _safe_int(self_heal.get("attempts_remaining")) >= 0:
        return False
    return True


def _current_launcher_stage_waits_for_cross_stack_release(mission: Task, *, proof_store=None) -> bool:
    if not _is_cross_stack_backend_first(mission):
        return False
    stage = _current_stage(mission)
    if stage is None or not _stage_mentions_launcher(stage):
        return False
    if not _has_backend_contract_proof(mission, proof_store=proof_store):
        return True
    if LAUNCHER_RELEASED_BY_NEKO_FLAG in (getattr(mission, "risk_flags", None) or []):
        return False
    return not _latest_handoff_targets_launcher(mission)


def _latest_handoff_targets_launcher(mission: Task) -> bool:
    body = _latest_handoff_body(mission)
    if not body:
        return False
    target_owner = str(body.get("target_dev_persona") or body.get("target_owner") or "").strip()
    target_repo = str(body.get("target_repo") or "").strip()
    phase = str(body.get("mission_phase") or "").strip().lower()
    kind = str(body.get("packet_kind") or "").strip().lower()
    return (
        target_owner in {"dev", "launcher_dev"}
        and target_repo == "EterniaLauncher"
        and (phase in {"launcher_handoff", "visual_proof_recovery"} or kind in {"contract_join", "recovery", "bounded_visual_proof_recovery"})
    )


def _current_stage(mission: Task) -> TaskStage | None:
    current_stage_id = str(getattr(mission, "current_stage_id", "") or "").strip()
    if not current_stage_id:
        return None
    return next((stage for stage in (getattr(mission, "stages", []) or []) if stage.id == current_stage_id), None)


def _ensure_pending_dev_handoff_stage(mission: Task) -> bool:
    body = _latest_handoff_body(mission)
    if not body:
        return False
    target_owner = str(body.get("target_dev_persona") or body.get("target_owner") or "").strip()
    target_repo = str(body.get("target_repo") or "").strip()
    if target_owner not in {"dev", "backend_dev", "launcher_dev"}:
        return False
    if target_repo == "EterniaLauncher":
        return _ensure_target_stage(
            mission,
            stage_id="launcher_contract_smoke",
            title="Launcher Contract Smoke",
            objective="Collect deterministic Launcher command proof for the Neko handoff.",
            repo="EterniaLauncher",
            stage_matcher=_stage_mentions_launcher,
        )
    if target_repo == "EterniaBackend":
        return _ensure_target_stage(
            mission,
            stage_id="backend_contract_smoke",
            title="Backend Contract Smoke",
            objective="Collect deterministic backend command proof for the Neko handoff.",
            repo="EterniaBackend",
            stage_matcher=_stage_mentions_backend,
        )
    return False


def _ensure_target_stage(mission: Task, *, stage_id: str, title: str, objective: str, repo: str, stage_matcher) -> bool:
    existing = next((stage for stage in getattr(mission, "stages", []) or [] if stage_matcher(stage)), None)
    if existing is not None:
        if existing.status in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
            return False
        mission.current_stage_id = existing.id
        if existing.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.BLOCKED}:
            existing.status = StageStatus.IMPLEMENTING
            existing.updated_at = now()
        mission.affected_repos = [repo]
        return True
    stage = TaskStage(
        id=stage_id,
        title=title,
        objective=objective,
        status=StageStatus.IMPLEMENTING,
        acceptance_criteria=list(getattr(mission, "acceptance_criteria", None) or ["Deterministic command proof is attached."]),
        test_plan=[],
        created_at=now(),
        updated_at=now(),
    )
    mission.stages.append(stage)
    mission.current_stage_id = stage.id
    mission.affected_repos = [repo]
    return True


def _latest_handoff_body(mission: Task) -> dict | None:
    try:
        packet = latest_packet(mission.id, "handoff_packet", stage_id=getattr(mission, "current_stage_id", None))
    except Exception:
        return None
    body = packet.get("body") if isinstance(packet, dict) else None
    return body if isinstance(body, dict) else None


def _stage_self_heal_state(mission: Task) -> dict:
    root = getattr(mission, "harness_self_heal", {}) or {}
    stages = root.get("stages") if isinstance(root, dict) else {}
    if not isinstance(stages, dict):
        return {}
    return stages.get(getattr(mission, "current_stage_id", None) or "_mission") or {}


def _safe_int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _environment_changed(value) -> bool:
    return str(value or "").strip().lower().startswith("changed")


def _stage_mentions_backend(stage: TaskStage) -> bool:
    haystack = " ".join(
        [
            str(stage.id),
            str(stage.title),
            str(stage.objective),
            " ".join(str(item) for item in (stage.affected_paths or [])),
            " ".join(str(item) for item in (stage.test_plan or [])),
        ]
    ).lower()
    return "backend" in haystack or "eterniabackend" in haystack
