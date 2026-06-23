from __future__ import annotations

from dataclasses import dataclass, field

from hermes_time import now

from .actions import HarnessAction, HarnessActionType
from .blueprints.routing import apply_decision_outcome, apply_stage_outcome, is_blueprint_plan
from .blueprints.schema import StageOutcome
from .default_plan import ensure_default_mission_plan
from .context_requests import has_unresolved_context_request
from .decision_schema import AgentDecision, DecisionType
from .dev_discipline import needs_supervisor_slicing
from .models import Event, Task, TaskStage
from .mission_plan import (
    current_plan_stage,
    has_typed_plan,
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


def _run_slot(mission: Task, slot_id: str, reason: str) -> HarnessAction:
    return HarnessAction(HarnessActionType.RUN_SLOT, mission.id, reason=reason, slot_id=slot_id)


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
        ensure_default_mission_plan(mission)
        typed = self._blueprint_next_action(mission, state=state)
        if typed is not None:
            return typed
        return HarnessAction(HarnessActionType.NOOP, mission.id, reason="no eligible mission action")

    def _blueprint_next_action(self, mission: Task, *, state: TaskState) -> HarnessAction | None:
        plan = getattr(mission, "mission_plan", None)
        if plan is None or not getattr(plan, "blueprint_id", None):
            return None
        if state in {TaskState.DONE, TaskState.CANCELLED}:
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason="blueprint mission is terminal")
        if getattr(mission, "open_incident_ids", None):
            if state == TaskState.BLOCKED and block_recovery_attempted_for_current_signal(mission):
                return HarnessAction(HarnessActionType.NOOP, mission.id, reason="blueprint mission has open incidents waiting on intervention")
            return _run_slot(mission, "neko_supervisor", "blueprint mission has open incidents; Neko must adjudicate")
        if _has_failed_current_stage_test_proof(mission, proof_store=self.proof_store) and _same_stage_retry_blocked(mission):
            return _run_slot(mission, "neko_supervisor", "blueprint failed proof retry needs Neko self-heal before another same-stage run")
        if _has_blocked_qa_verdict(mission, proof_store=self.proof_store):
            implement = next((stage for stage in plan.stages if stage.id == "implement"), None)
            if implement is not None:
                plan.current_stage_id = implement.id
                mission.current_stage_id = implement.id
                if implement.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.BLOCKED, StageStatus.REWORK}:
                    implement.status = StageStatus.IMPLEMENTING
            return _run_slot(mission, "dev", "blueprint needs delivery recovery from QA blocked verdict")
        if state == TaskState.BLOCKED:
            if block_recovery_attempted_for_current_signal(mission):
                return HarnessAction(HarnessActionType.NOOP, mission.id, reason="blueprint mission blocked waiting on intervention")
            return _run_slot(mission, "neko_supervisor", "blueprint intervention needs Neko goal-owner adjudication")
        current = current_plan_stage(mission)
        if current is None:
            if getattr(mission, "requires_visual_proof", False) and not _has_visual_proof(mission, proof_store=self.proof_store):
                return _run_slot(mission, "dev", "blueprint mission requires visual proof before terminal close")
            return HarnessAction(HarnessActionType.COMPLETE_TASK, mission.id, reason="blueprint has no remaining stages")
        reconciliation = reconcile_task(mission)
        if reconciliation.needs_supervisor:
            return _run_slot(mission, "neko_supervisor", f"blueprint needs Neko transition reconciliation: {reconciliation.findings[0].kind}")
        if needs_pm_triage_before_dev(mission):
            return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead issue discovery triage")
        if has_unresolved_context_request(mission):
            return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead to resolve context request")
        if needs_supervisor_slicing(mission):
            return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead to slice broad specialist mission before delivery")
        if state != TaskState.REWORK_REQUESTED and current.status in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
            apply_stage_outcome(mission, current.id, StageOutcome.PASSED, reason=f"blueprint stage {current.id} already ready")
            current = current_plan_stage(mission)
            if current is None:
                if getattr(mission, "requires_visual_proof", False) and not _has_visual_proof(mission, proof_store=self.proof_store):
                    return _run_slot(mission, "dev", "blueprint mission requires visual proof before terminal close")
                return HarnessAction(HarnessActionType.COMPLETE_TASK, mission.id, reason="blueprint has no remaining stages")
        if plan.current_stage_id != current.id:
            release_next_stage(mission, current.id)
        slot_id = current.owner_slot or current.owner
        if not slot_id:
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"blueprint stage {current.id} has no owner slot")
        if plan.slots and slot_id not in plan.slots:
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"blueprint stage {current.id} owner slot {slot_id} is not declared")
        if plan.bindings and not plan.bindings.get(slot_id):
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"blueprint slot {slot_id} has no resolved binding")
        return HarnessAction(HarnessActionType.RUN_SLOT, mission.id, reason=f"blueprint stage {current.id} needs slot {slot_id}", slot_id=slot_id)

    def apply_decision(self, mission: Task, decision: AgentDecision, *, actor: str, task_store=None, incident_store=None, proof_store=None, run_id: str | None = None, normal_worker_flow: bool = False, mission_plan_flow: bool | None = None) -> StateMachineResult:
        before = mission.state if isinstance(mission.state, TaskState) else TaskState(mission.state)
        blueprint_owned = is_blueprint_plan(getattr(mission, "mission_plan", None))
        has_open_incident = bool(getattr(mission, "open_incident_ids", None))
        if not has_open_incident and incident_store is not None:
            try:
                has_open_incident = any(getattr(incident, "task_id", None) == mission.id for incident in incident_store.list_open())
            except Exception:
                has_open_incident = False
        incident_resolution_acceptance = (
            blueprint_owned
            and actor == "neko_supervisor"
            and decision.type == DecisionType.PROPOSE_ACCEPTANCE
            and has_open_incident
        )
        if mission_plan_flow is None:
            mission_plan_flow = has_typed_plan(mission)
        mission_plan_flow = bool(mission_plan_flow or blueprint_owned)
        apply_planning_decision(mission, decision, actor=actor, task_store=task_store, incident_store=incident_store, proof_store=proof_store, run_id=run_id, normal_worker_flow=normal_worker_flow, mission_plan_flow=mission_plan_flow)
        record_decision_packets(mission, decision, actor=actor, run_id=run_id, stage_id=getattr(mission, "current_stage_id", None))
        if blueprint_owned and decision.type != DecisionType.REQUEST_TEST_RUN and not incident_resolution_acceptance:
            proofs = proof_store.list_for_task(mission.id) if proof_store is not None else None
            apply_decision_outcome(mission, decision, proofs=proofs, reason=decision.summary)
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
