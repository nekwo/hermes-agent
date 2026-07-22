from __future__ import annotations

from dataclasses import dataclass, field

from hermes_time import now

from .actions import HarnessAction, HarnessActionType
from .blueprints.routing import apply_decision_outcome, apply_stage_outcome, is_blueprint_plan
from .blueprints.schema import StageOutcome
from .child_events import parent_child_event_wake_action
from .default_plan import ensure_default_mission_plan
from .context_requests import has_unresolved_context_request
from .decision_schema import AgentDecision, DecisionType
from .dev_discipline import needs_supervisor_slicing
from .models import Event, MissionPlanStage, Task, TaskStage
from .mission_plan import (
    current_plan_stage,
    is_mission_lead_actor,
    release_next_stage,
)
from .packets import record_decision_packets
from .packets import latest_packet
from .planning import _all_stages_dev_complete, _all_stages_passed, _advance_to_next_dev_stage, _has_backend_contract_proof, _is_cross_stack_backend_first, _needs_cross_stack_launcher_completion, _needs_sequential_specialist_join, _stage_mentions_launcher, apply_planning_decision
from .proof_gates import stage_proof_satisfied
from .reconciler import reconcile_task
from .recovery_flags import block_recovery_attempted_for_current_signal
from .scope_control import needs_pm_triage_before_dev
from .states import StageStatus, TaskState


def _run_slot(mission: Task, slot_id: str, reason: str, *, stage_id: str | None = None) -> HarnessAction:
    return HarnessAction(HarnessActionType.RUN_SLOT, mission.id, reason=reason, slot_id=slot_id, stage_id=stage_id)


def _prune_closed_incident_links(mission: Task) -> None:
    """Drop open_incident_ids entries whose incident is closed in the store.

    An in-flight engine turn can persist a stale in-memory task copy over an
    operator's incident-close unlink (observed live: a closed incident stayed
    linked and permanently routed the mission to Neko adjudication). The store
    is the authority on open incidents; the link list is a routing hint. The
    prune is in-memory — the row heals on the next engine persist.
    """

    linked = list(getattr(mission, "open_incident_ids", None) or [])
    if not linked:
        return
    try:
        from .store import IncidentStore

        store = IncidentStore()
    except Exception:
        return
    pruned: list[str] = []
    for item in linked:
        try:
            incident = store.get(str(item))
        except Exception:
            # Unknown id (no record readable) — keep the link, fail safe.
            pruned.append(item)
            continue
        if getattr(incident, "closed_at", None) is None:
            pruned.append(item)
    if pruned != linked:
        mission.open_incident_ids = pruned


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

    def __init__(self, *, proof_store=None, config=None, event_log=None):
        self.proof_store = proof_store
        self.config = config
        self.event_log = event_log

    def next_action(self, mission: Task) -> HarnessAction:
        state = mission.state if isinstance(mission.state, TaskState) else TaskState(mission.state)
        if _mission_plan_routing_enabled(mission, self.config):
            ensure_default_mission_plan(mission)
        _prune_closed_incident_links(mission)
        child_wake = parent_child_event_wake_action(mission, config=self.config)
        if child_wake is not None:
            return child_wake
        typed = self._blueprint_next_action(mission, state=state)
        if typed is not None:
            return typed
        return _legacy_next_action(
            mission,
            state=state,
            proof_store=self.proof_store,
            event_log=self.event_log,
        )

    def next_actions(self, mission: Task) -> list[HarnessAction]:
        state = mission.state if isinstance(mission.state, TaskState) else TaskState(mission.state)
        if _mission_plan_routing_enabled(mission, self.config):
            ensure_default_mission_plan(mission)
        _prune_closed_incident_links(mission)
        child_wake = parent_child_event_wake_action(mission, config=self.config)
        if child_wake is not None:
            return [child_wake]
        typed = self._blueprint_ready_actions(mission, state=state)
        if typed:
            return typed
        return [self.next_action(mission)]

    def _blueprint_ready_actions(self, mission: Task, *, state: TaskState) -> list[HarnessAction]:
        plan = getattr(mission, "mission_plan", None)
        if plan is None or not getattr(plan, "blueprint_id", None):
            return []
        if state in {TaskState.DONE, TaskState.CANCELLED, TaskState.BLOCKED}:
            return []
        if getattr(mission, "open_incident_ids", None):
            return []
        if needs_pm_triage_before_dev(mission) or has_unresolved_context_request(mission) or needs_supervisor_slicing(mission, event_log=self.event_log):
            return []
        stages = list(getattr(plan, "stages", None) or [])
        by_id = {stage.id: stage for stage in stages}
        limit = _ready_action_limit(self.config)
        actions: list[HarnessAction] = []
        for stage in stages:
            if stage.status not in {StageStatus.DRAFT, StageStatus.READY, StageStatus.REWORK, StageStatus.IMPLEMENTING}:
                continue
            if any((by_id.get(dep_id) is None or by_id[dep_id].status != StageStatus.PASSED) for dep_id in list(stage.depends_on or [])):
                continue
            slot_id = stage.owner_slot or stage.owner
            if not slot_id:
                continue
            if plan.slots and slot_id not in plan.slots:
                continue
            if plan.bindings and not plan.bindings.get(slot_id):
                continue
            if stage.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.REWORK}:
                stage.status = StageStatus.IMPLEMENTING
                stage.updated_at = now()
            actions.append(_run_slot(mission, slot_id, f"blueprint stage {stage.id} needs slot {slot_id}", stage_id=stage.id))
            if len(actions) >= limit:
                break
        if actions:
            first_stage_id = actions[0].stage_id
            plan.current_stage_id = first_stage_id
            mission.current_stage_id = first_stage_id
        return actions

    def _blueprint_next_action(self, mission: Task, *, state: TaskState) -> HarnessAction | None:
        plan = getattr(mission, "mission_plan", None)
        if plan is None or not getattr(plan, "blueprint_id", None):
            return None
        if state in {TaskState.DONE, TaskState.CANCELLED}:
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason="blueprint mission is terminal")
        if getattr(mission, "open_incident_ids", None):
            # One bounded adjudication pass per evidence signal in ANY state,
            # not just BLOCKED: a RUNNING mission whose supervisor answers
            # adjudication with `block` must settle to wait-on-intervention
            # instead of re-dispatching Neko forever. Closing an incident,
            # attaching proof, or recording a packet changes the signal and
            # re-arms recovery automatically.
            if block_recovery_attempted_for_current_signal(mission):
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
            return _blueprint_terminal_action(mission, proof_store=self.proof_store)
        reconciliation = reconcile_task(mission)
        if reconciliation.needs_supervisor:
            return _run_slot(mission, "neko_supervisor", f"blueprint needs Neko transition reconciliation: {reconciliation.findings[0].kind}")
        if needs_pm_triage_before_dev(mission):
            return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead issue discovery triage")
        if has_unresolved_context_request(mission):
            return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead to resolve context request")
        if needs_supervisor_slicing(mission, event_log=self.event_log):
            return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead to slice broad specialist mission before delivery")
        if current.status in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
            apply_stage_outcome(mission, current.id, StageOutcome.PASSED, reason=f"blueprint stage {current.id} already ready")
            current = current_plan_stage(mission)
            if current is None:
                return _blueprint_terminal_action(mission, proof_store=self.proof_store)
        if current.status == StageStatus.BLOCKED:
            return _run_slot(mission, "neko_supervisor", f"blueprint stage {current.id} needs goal-owner adjudication")
        dependency = _first_unpassed_blueprint_dependency(plan, current) if _strict_blueprint_dependency_dispatch(plan) else None
        if dependency is not None:
            plan.current_stage_id = dependency.id
            mission.current_stage_id = dependency.id
            if dependency.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.REWORK}:
                dependency.status = StageStatus.IMPLEMENTING
                dependency.updated_at = now()
            current = dependency
            if current.status in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
                apply_stage_outcome(mission, current.id, StageOutcome.PASSED, reason=f"blueprint dependency {current.id} already ready")
                current = current_plan_stage(mission)
                if current is None:
                    return _blueprint_terminal_action(mission, proof_store=self.proof_store)
            if current.status == StageStatus.BLOCKED:
                return _run_slot(mission, "neko_supervisor", f"blueprint dependency {current.id} needs goal-owner adjudication")
        if plan.current_stage_id != current.id:
            release_next_stage(mission, current.id)
        slot_id = current.owner_slot or current.owner
        if not slot_id:
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"blueprint stage {current.id} has no owner slot")
        if plan.slots and slot_id not in plan.slots:
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"blueprint stage {current.id} owner slot {slot_id} is not declared")
        if plan.bindings and not plan.bindings.get(slot_id):
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason=f"blueprint slot {slot_id} has no resolved binding")
        return _run_slot(mission, slot_id, f"blueprint stage {current.id} needs slot {slot_id}", stage_id=current.id)

    def apply_decision(self, mission: Task, decision: AgentDecision, *, actor: str, task_store=None, incident_store=None, proof_store=None, run_id: str | None = None, stage_id: str | None = None, normal_worker_flow: bool = False, mission_plan_flow: bool | None = None) -> StateMachineResult:
        if _mission_plan_routing_enabled(mission, self.config):
            ensure_default_mission_plan(mission)
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
            and is_mission_lead_actor(mission, actor)
            and decision.type == DecisionType.PROPOSE_ACCEPTANCE
            and has_open_incident
        )
        if mission_plan_flow is None:
            mission_plan_flow = bool(blueprint_owned)
        mission_plan_flow = bool(mission_plan_flow or blueprint_owned)
        apply_planning_decision(mission, decision, actor=actor, task_store=task_store, incident_store=incident_store, proof_store=proof_store, run_id=run_id, normal_worker_flow=normal_worker_flow, mission_plan_flow=mission_plan_flow)
        record_decision_packets(mission, decision, actor=actor, run_id=run_id, stage_id=getattr(mission, "current_stage_id", None))
        if blueprint_owned and decision.type != DecisionType.REQUEST_TEST_RUN and not incident_resolution_acceptance:
            proofs = proof_store.list_for_task(mission.id) if proof_store is not None else None
            # Attribute the outcome to the stage the deciding run actually ran.
            # apply_planning_decision above may have already advanced the plan's
            # current stage (e.g. Neko's scope release moves it to the first dev
            # stage), so falling back to current_stage_id here lands the outcome
            # on the WRONG downstream stage — live-observed as Neko's scope_route
            # marking backend_implementation PASSED with zero proof, which the
            # terminal proof gate then had to claw back at the cost of an extra
            # adjudication turn and re-dispatch.
            apply_decision_outcome(mission, decision, stage_id=stage_id, proofs=proofs, reason=decision.summary)
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


def _first_unpassed_blueprint_dependency(plan, stage: MissionPlanStage) -> MissionPlanStage | None:
    by_id = {item.id: item for item in list(getattr(plan, "stages", None) or [])}
    seen: set[str] = set()

    def visit(candidate: MissionPlanStage) -> MissionPlanStage | None:
        for dep_id in list(getattr(candidate, "depends_on", None) or []):
            dep = by_id.get(str(dep_id))
            if dep is None or dep.id in seen:
                continue
            seen.add(dep.id)
            upstream = visit(dep)
            if upstream is not None:
                return upstream
            if dep.status != StageStatus.PASSED:
                return dep
        return None

    return visit(stage)


def _strict_blueprint_dependency_dispatch(plan) -> bool:
    limits = getattr(plan, "limits", None) or {}
    return bool(limits.get("strict_depends_on_dispatch"))


def _ready_action_limit(config) -> int:
    swarm = getattr(config, "swarm", None)
    try:
        return max(1, int(getattr(swarm, "max_active_lanes", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _mission_plan_routing_enabled(mission: Task, config) -> bool:
    if getattr(mission, "mission_plan", None) is not None:
        return True
    if getattr(mission, "stages", None):
        return True
    mission_plan_config = getattr(config, "mission_plan", None)
    return bool(getattr(mission_plan_config, "enabled", False))


def _legacy_next_action(mission: Task, *, state: TaskState, proof_store=None, event_log=None) -> HarnessAction:
    if state in {TaskState.DONE, TaskState.CANCELLED}:
        return HarnessAction(HarnessActionType.NOOP, mission.id, reason="legacy task is terminal")
    if getattr(mission, "open_incident_ids", None):
        if block_recovery_attempted_for_current_signal(mission):
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason="legacy task has open incidents waiting on intervention")
        return _run_slot(mission, "neko_supervisor", "legacy task has open incidents; Neko must adjudicate")
    if state == TaskState.CREATED:
        return _run_slot(mission, "neko_supervisor", "blueprint stage scope needs slot neko_supervisor")
    if state == TaskState.BLOCKED:
        if _has_blocked_qa_verdict(mission, proof_store=proof_store):
            return _run_slot(mission, "dev", "blueprint needs delivery recovery from QA blocked verdict", stage_id=mission.current_stage_id)
        if _has_resolved_incident_only_qa_block(mission, proof_store=proof_store):
            return _run_slot(mission, "dev", "blueprint needs delivery recovery from QA blocked verdict")
        if _latest_handoff_body(mission):
            return _run_slot(mission, "neko_supervisor", "blueprint intervention needs Neko goal-owner adjudication", stage_id=mission.current_stage_id)
        if block_recovery_attempted_for_current_signal(mission):
            return HarnessAction(HarnessActionType.NOOP, mission.id, reason="legacy task blocked waiting on intervention")
        return _run_slot(mission, "neko_supervisor", "blueprint intervention needs Neko goal-owner adjudication")

    reconciliation = reconcile_task(mission)
    if reconciliation.needs_supervisor:
        return _run_slot(mission, "neko_supervisor", f"blueprint needs Neko transition reconciliation: {reconciliation.findings[0].kind}")
    if needs_pm_triage_before_dev(mission):
        return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead issue discovery triage")
    if has_unresolved_context_request(mission) or _has_unsupported_context_requests(mission):
        return _run_slot(mission, "neko_supervisor", "legacy task needs Neko Mission Lead to resolve context request")
    if needs_supervisor_slicing(mission, event_log=event_log):
        return _run_slot(mission, "neko_supervisor", "blueprint needs Neko Mission Lead to slice broad specialist mission before delivery")

    current = _current_stage(mission)
    if current is not None:
        if _current_launcher_stage_waits_for_cross_stack_release(mission, proof_store=proof_store):
            return _run_slot(mission, "neko_supervisor", "legacy Launcher stage waits for backend join release", stage_id=current.id)
        if current.status == StageStatus.BLOCKED:
            if _has_failed_current_stage_test_proof(mission, proof_store=proof_store):
                return _run_slot(mission, "dev", f"legacy stage {current.id} needs Dev retry after failed proof", stage_id=current.id)
            return _run_slot(mission, "neko_supervisor", f"legacy stage {current.id} is blocked", stage_id=current.id)
        if current.status == StageStatus.READY_FOR_QA:
            mission.current_stage_id = "verify"
            return _run_slot(mission, "qa", f"blueprint stage {current.id} needs slot qa", stage_id=current.id)
        if current.status in {StageStatus.READY, StageStatus.DRAFT, StageStatus.REWORK, StageStatus.IMPLEMENTING}:
            if current.status in {StageStatus.READY, StageStatus.DRAFT, StageStatus.REWORK}:
                current.status = StageStatus.IMPLEMENTING
                current.updated_at = now()
            return _run_slot(mission, _legacy_dev_slot_for_task(mission, current), f"legacy stage {current.id} needs Dev", stage_id=current.id)

    if _has_resolved_qa_output_incident(mission) or (_all_stages_dev_complete(mission) and getattr(mission, "proof_ids", None)):
        return _run_slot(mission, "qa", "blueprint stage qa_release needs slot qa")
    if _legacy_backend_first_burn_in(mission):
        return _run_slot(mission, "dev", "legacy backend-first burn-in needs Backend Dev proof lane")
    if _needs_sequential_specialist_join(mission) or _needs_cross_stack_launcher_completion(mission, proof_store=proof_store):
        return _run_slot(mission, "neko_supervisor", "legacy cross-stack handoff needs Neko join")
    if _all_stages_passed(mission):
        return HarnessAction(HarnessActionType.COMPLETE_TASK, mission.id, reason="legacy task stages passed")
    _advance_to_next_dev_stage(mission)
    next_stage = _current_stage(mission)
    if next_stage is not None:
        return _run_slot(mission, _legacy_dev_slot_for_task(mission, next_stage), f"legacy stage {next_stage.id} needs Dev", stage_id=next_stage.id)
    return _run_slot(mission, "dev", "blueprint stage implement needs slot dev")


def _legacy_dev_slot_for_task(mission: Task, stage) -> str:
    if _stage_mentions_backend(stage):
        return "backend_dev"
    if _stage_mentions_launcher(stage):
        return "dev"
    repos = " ".join(str(repo).lower() for repo in (getattr(mission, "affected_repos", None) or []))
    if "backend" in repos or "eterniabackend" in repos or "eternia-backend" in repos:
        return "dev"
    return "dev"


def _has_unsupported_context_requests(mission: Task) -> bool:
    requests = getattr(mission, "context_requests", None) or []
    if not isinstance(requests, list):
        return False
    unsupported = 0
    for item in requests:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        failure = str(item.get("failure_reason") or "").strip().lower()
        if status == "unsupported" or failure:
            unsupported += 1
    return unsupported >= 3


def _legacy_backend_first_burn_in(mission: Task) -> bool:
    flags = {str(flag).strip().lower() for flag in (getattr(mission, "risk_flags", None) or [])}
    if not {"backend_contract_first", "bounded_complex_burn_in"} <= flags:
        return False
    repos = " ".join(str(repo).strip().lower() for repo in (getattr(mission, "affected_repos", None) or []))
    return "backend" in repos or "eterniabackend" in repos


def _blueprint_terminal_action(mission: Task, *, proof_store=None) -> HarnessAction:
    plan = getattr(mission, "mission_plan", None)
    if plan is not None:
        blocker = _first_incomplete_or_unproven_blueprint_stage(mission, proof_store=proof_store)
        if blocker is not None:
            stage, reason, proof_blocked = blocker
            plan.current_stage_id = stage.id
            mission.current_stage_id = stage.id
            if proof_blocked:
                stage.status = StageStatus.BLOCKED
                stage.updated_at = now()
                return _run_slot(mission, "neko_supervisor", reason)
            if stage.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.REWORK}:
                stage.status = StageStatus.IMPLEMENTING
                stage.updated_at = now()
            slot_id = stage.owner_slot or stage.owner or "neko_supervisor"
            return _run_slot(mission, slot_id, reason)
    if getattr(mission, "requires_visual_proof", False) and not _has_visual_proof(mission, proof_store=proof_store):
        return _run_slot(mission, "dev", "blueprint mission requires visual proof before terminal close")
    return HarnessAction(HarnessActionType.COMPLETE_TASK, mission.id, reason="blueprint has no remaining stages")


def _first_incomplete_or_unproven_blueprint_stage(mission: Task, *, proof_store=None) -> tuple[MissionPlanStage, str, bool] | None:
    plan = getattr(mission, "mission_plan", None)
    if plan is None:
        return None
    proofs = None
    if proof_store is not None:
        try:
            proofs = list(proof_store.list_for_task(mission.id))
        except Exception:
            proofs = []
    for stage in list(getattr(plan, "stages", None) or []):
        if stage.status != StageStatus.PASSED:
            status = stage.status.value if hasattr(stage.status, "value") else str(stage.status)
            return stage, f"blueprint terminal close blocked: stage {stage.id} is {status}, not passed", False
        if proofs is not None:
            failed = _latest_failed_proof_for_stage(stage, proofs)
            if failed is not None:
                return stage, f"blueprint terminal close blocked: latest proof {failed.id} for stage {stage.id} failed", True
            gate = stage_proof_satisfied(stage, proofs)
            if not gate.allowed:
                missing = ", ".join(gate.missing or ["missing required proof"])
                return stage, f"blueprint terminal close blocked: stage {stage.id} proof gate unsatisfied ({missing})", True
    return None


def _latest_failed_proof_for_stage(stage: MissionPlanStage, proofs: list[Proof]) -> Proof | None:
    scoped = [proof for proof in list(proofs or []) if _proof_stage_id(proof) == stage.id]
    if not scoped:
        return None
    newest = max(scoped, key=lambda proof: getattr(proof, "created_at", None) or "")
    if _proof_status(newest) in {"failed", "error", "blocked"}:
        return newest
    return None


def _proof_stage_id(proof: Proof) -> str:
    direct = str(getattr(proof, "stage_id", "") or "").strip()
    if direct:
        return direct
    metadata = getattr(proof, "metadata", None) or {}
    if isinstance(metadata, dict):
        return str(metadata.get("stage_id") or "").strip()
    return ""


def _proof_status(proof: Proof) -> str:
    metadata = getattr(proof, "metadata", None) or {}
    if not isinstance(metadata, dict):
        return ""
    status = str(metadata.get("status") or metadata.get("verdict") or "").strip().lower()
    if status:
        return status
    if "exit_code" in metadata:
        try:
            return "passed" if int(metadata.get("exit_code")) == 0 else "failed"
        except (TypeError, ValueError, OverflowError):
            return "failed"
    return ""


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
