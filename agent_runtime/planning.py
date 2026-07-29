from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hermes_time import now

from .decision_contracts import validate_planning_decision
from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from .budget_approval import budget_incident_can_continue, budget_incident_needs_scope_recovery
from .events import EventLog
from .gates import can_enter_dev_implementing
from .context_requests import add_context_request
from .incidents import RUN_BUDGET_EXCEEDED
from .scope_control import apply_issue_triage, record_issue_discovery
from .simplified_contract import (
    legacy_acceptance_decision_from_scope_route,
    legacy_issue_decision_from_escalate,
    legacy_qa_review_decision_from_qa_verdict,
)
from .models import Event, Task, TaskStage
from .mission_plan import (
    append_task_stage_record,
    attach_proofs_to_plan_stage,
    current_plan_stage,
    ensure_mission_plan,
    is_mission_lead_actor,
    mark_plan_stage_from_decision,
    release_next_stage,
    task_stage_records,
)
from .plan_review import PlanReview, PlanReviewVerdict, finding_from_payload
from .proof_recipes import resolve_proof_recipe
from .proof_rules import ProofType
from .qa_verdict import record_qa_verdict
from .promotion_gates import validate_product_promotion_gate
from .recovery_flags import mark_block_recovery_attempt
from .stage_intent import (
    extract_single_known_stage_reference,
    first_incomplete_product_edit_stage,
    no_product_edit_recipe_conflicts_with_stage,
    no_product_edit_recipe_id,
    stage_requires_product_edit,
)
from .states import StageStatus, TaskState
from .store import RunStore


DEV_COMPLETE_STAGE_STATUSES = frozenset({StageStatus.READY_FOR_QA, StageStatus.PASSED})
TERMINAL_STAGE_STATUSES = frozenset({StageStatus.PASSED})
CROSS_STACK_BACKEND_FIRST_FLAGS = frozenset(
    {
        "cross_stack_contract_handoff",
        "cross_stack_contract_join",
        "cross_stack_sequential_handoff",
        "cross_stack_sequential_join_required",
        "backend_contract_first",
        "frontend_backend_contract_handoff",
    }
)


def _soft_block_task(task: Task, *, reason: str, stage_id: str | None = None, recommended_owner: str = "neko_supervisor") -> None:
    if task.state not in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
        task.state = TaskState.RUNNING
    root = task.harness_self_heal if isinstance(getattr(task, "harness_self_heal", None), dict) else {}
    stack = root.get("evidence_stack") if isinstance(root.get("evidence_stack"), list) else []
    evidence = {
        "kind": "blocked_escalation",
        "severity": "warning",
        "stage_id": stage_id or getattr(task, "current_stage_id", None),
        "summary": "Blocked condition escalated to the goal owner; task remains runnable for adjudication.",
        "warnings": [str(reason or "")[:500]] if reason else [],
        "recommended_owner": recommended_owner,
        "reason": str(reason or "")[:500],
        "recorded_at": now().isoformat(),
    }
    key = (evidence["kind"], evidence.get("stage_id"), tuple(evidence.get("warnings") or []))
    stack = [
        item
        for item in stack
        if not (
            isinstance(item, dict)
            and (item.get("kind"), item.get("stage_id"), tuple(item.get("warnings") or [])) == key
        )
    ]
    root["evidence_stack"] = [*stack, {k: v for k, v in evidence.items() if v not in (None, [], {})}][-10:]
    task.harness_self_heal = root


def _dedupe_extend(existing: list[str], incoming: Iterable[str]) -> list[str]:
    seen = set(existing)
    for item in incoming:
        if item not in seen:
            existing.append(item)
            seen.add(item)
    return existing


def _coerce_neko_budget_acceptance_to_continuation(
    task: Task,
    decision: AgentDecision,
    *,
    actor: str,
    incident_store,
    log: EventLog,
    run_id: str | None,
) -> bool:
    """Treat Neko's bounded re-scope as approval for a waiting budget run.

    Live Neko may correctly shrink/scope the continuation using `scope_route`
    or its legacy `propose_acceptance` alias. For an open
    `run_budget_exceeded` incident linked to a waiting same-session Dev run,
    that otherwise loops Neko forever. The deterministic Harness owns the
    approval side effect: preserve Neko's tightened scope, approve the linked
    run, close the incident, and route back to Dev implementation.
    """
    if not is_mission_lead_actor(task, actor) or decision.type != DecisionType.PROPOSE_ACCEPTANCE or incident_store is None:
        return False
    incident_ids = list(getattr(task, "open_incident_ids", []) or [])
    if not incident_ids and hasattr(incident_store, "list_open"):
        incident_ids = [incident.id for incident in incident_store.list_open() if incident.task_id == task.id]
    for incident_id in incident_ids:
        try:
            incident = incident_store.get(incident_id)
        except Exception:
            continue
        if incident.kind != RUN_BUDGET_EXCEEDED or not incident.run_id:
            continue
        if budget_incident_needs_scope_recovery(incident, RunStore()):
            try:
                cancelled = RunStore().cancel(incident.run_id, reason="Neko scope recovery after Dev read/search loop without delivery")
            except Exception:
                cancelled = None
            _apply_acceptance(task, decision.payload, actor=actor)
            incident_store.close(
                incident_id,
                reason=str(decision.payload.get("summary") or decision.summary or "Neko narrowed scope after Dev read/search loop without delivery"),
            )
            task.open_incident_ids = [item for item in task.open_incident_ids if item != incident_id]
            task.state = TaskState.RUNNING
            task.updated_at = now()
            payload = {
                "incident_id": incident_id,
                "resolution": "Neko narrowed scope; Harness cancelled exhausted Dev run and routed a fresh Dev attempt",
                "next_state": TaskState.RUNNING.value,
                "approval_type": "scope_recovery",
                "next_expected": "dev_fresh_scope_retry",
                "coerced_from_decision": DecisionType.PROPOSE_ACCEPTANCE.value,
            }
            if cancelled is not None:
                payload["cancelled_run_id"] = cancelled.id
            log.append(Event(now(), "incident.resolved", task.id, run_id, actor, payload))
            return True
        if not budget_incident_can_continue(incident, RunStore()):
            raise DecisionPayloadInvalid("budget continuation cap reached for this Dev session; Neko must block or request human intervention instead of approving another continuation")
        try:
            approved = RunStore().approve_continuation(incident.run_id)
        except Exception:
            continue
        _apply_acceptance(task, decision.payload, actor=actor)
        incident_store.close(
            incident_id,
            reason=str(decision.payload.get("summary") or decision.summary or "Neko approved bounded same-session Dev continuation"),
        )
        task.open_incident_ids = [item for item in task.open_incident_ids if item != incident_id]
        task.state = TaskState.RUNNING
        task.updated_at = now()
        log.append(
            Event(
                now(),
                "incident.resolved",
                task.id,
                run_id,
                actor,
                {
                    "incident_id": incident_id,
                    "resolution": "Neko scoped bounded continuation; Harness approved same-session Dev continuation",
                    "next_state": TaskState.RUNNING.value,
                    "approved_run_id": approved.id,
                    "approval_type": "budget_continuation",
                    "next_expected": "dev_same_session_continuation",
                    "coerced_from_decision": DecisionType.PROPOSE_ACCEPTANCE.value,
                },
            )
        )
        return True
    return False


def apply_planning_decision(task: Task, decision: AgentDecision, *, actor: str, event_log: EventLog | None = None, task_store=None, incident_store=None, proof_store=None, run_id: str | None = None, normal_worker_flow: bool = False, mission_plan_flow: bool = False) -> Task:
    validate_planning_decision(decision)
    log = event_log or EventLog()
    source_decision = decision
    decision = legacy_acceptance_decision_from_scope_route(task, decision)
    decision = legacy_qa_review_decision_from_qa_verdict(decision)
    decision = legacy_issue_decision_from_escalate(decision)
    if decision is not source_decision:
        validate_planning_decision(decision)
    if decision.type == DecisionType.PROPOSE_ACCEPTANCE:
        _validate_affected_repo_scope(task, decision, actor=actor, log=log, run_id=run_id)
    if _coerce_neko_budget_acceptance_to_continuation(task, decision, actor=actor, incident_store=incident_store, log=log, run_id=run_id):
        return task
    if mission_plan_flow and is_mission_lead_actor(task, actor) and decision.type == DecisionType.PROPOSE_ACCEPTANCE:
        _apply_acceptance(task, decision.payload, actor=actor)
        ensure_mission_plan(task, decision.payload, actor=actor)
        target = release_next_stage(task, str(decision.payload.get("release_stage_id") or "").strip() or None)
        if target is not None:
            task.affected_repos = _release_stage_affected_repos(task, target.repo)
            if target.owner == "qa":
                task.state = TaskState.RUNNING
            elif target.owner in {"dev", "backend_dev"}:
                task.state = TaskState.RUNNING if task.state in {TaskState.CREATED, TaskState.RUNNING} else TaskState.RUNNING
            elif target.owner == "human":
                _soft_block_task(task, reason="typed plan routed to human owner", stage_id=target.id, recommended_owner="human")
            else:
                task.state = TaskState.RUNNING
        task.updated_at = now()
        log.append(
            Event(
                now(),
                "mission_plan.updated",
                task.id,
                run_id,
                actor,
                {
                    "current_stage_id": getattr(getattr(task, "mission_plan", None), "current_stage_id", None),
                    "stage_count": len(getattr(getattr(task, "mission_plan", None), "stages", []) or []),
                    "revision": getattr(getattr(task, "mission_plan", None), "revision", None),
                    "summary": "Typed mission plan set or released by Neko.",
                },
            )
        )
        return task
    if decision.type == DecisionType.PROPOSE_ACCEPTANCE:
        if is_mission_lead_actor(task, actor) and task.state == TaskState.RUNNING and _all_stages_dev_complete(task):
            if _needs_cross_stack_launcher_completion(task, proof_store=proof_store):
                if not _has_backend_contract_proof(task, proof_store=proof_store):
                    _block_launcher_release_until_backend_proof(task, log=log, actor=actor, run_id=run_id)
                    return task
                if _payload_targets_launcher(decision.payload):
                    task.affected_repos = ["EterniaLauncher"]
                    _ensure_scoped_dev_handoff_stage(task, decision.payload, actor=actor, log=log)
                    task.state = TaskState.RUNNING
                    task.updated_at = now()
                    log.append(
                        Event(
                            now(),
                            "cross_stack.launcher_released",
                            task.id,
                            run_id,
                            actor,
                            {
                                "source": "neko_cross_stack_join_gate",
                                "next_state": TaskState.RUNNING.value,
                                "next_expected": "launcher_dev_verification",
                                "proof_ids": len(task.proof_ids),
                            },
                        )
                    )
                    return task
                _soft_block_task(task, reason="cross-stack launcher release missing", stage_id=task.current_stage_id)
                if "cross_stack_launcher_release_missing" not in task.risk_flags:
                    task.risk_flags.append("cross_stack_launcher_release_missing")
                task.updated_at = now()
                log.append(
                    Event(
                        now(),
                        "cross_stack.launcher_release_missing",
                        task.id,
                        run_id,
                        actor,
                        {
                            "source": "neko_cross_stack_join_gate",
                            "next_expected": "Neko must release Launcher Dev with affected_repos including EterniaLauncher/frontend",
                            "proof_ids": len(task.proof_ids),
                        },
                    )
                )
                return task
            if _payload_is_launcher_handoff(decision.payload):
                _soft_block_task(task, reason="cross-stack QA coordination release missing", stage_id=task.current_stage_id)
                _dedupe_extend(task.risk_flags, ["cross_stack_qa_coordination_release_missing"])
                task.updated_at = now()
                log.append(
                    Event(
                        now(),
                        "cross_stack.qa_coordination_release_missing",
                        task.id,
                        run_id,
                        actor,
                        {
                            "source": "neko_cross_stack_join_gate",
                            "next_expected": "Neko must emit qa_coordination_release after backend and Launcher proofs are both attached; contract_join cannot release QA.",
                            "proof_ids": len(task.proof_ids),
                        },
                    )
                )
                return task
            next_repos = _canonical_affected_repos(decision.payload.get("affected_repos", []) or [])
            if next_repos:
                task.affected_repos = next_repos
            task.updated_at = now()
            log.append(
                Event(
                    now(),
                    "qa.coordination_released",
                    task.id,
                    run_id,
                    actor,
                    {
                        "source": "neko_qa_coordination",
                        "next_state": TaskState.RUNNING.value,
                        "next_expected": "qa_verification",
                        "proof_ids": len(task.proof_ids),
                    },
                )
            )
            return task
        if (
            task.state in {TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING}
            and task.proof_ids
            and _all_stages_passed(task)
            and not _needs_cross_stack_launcher_completion(task, proof_store=proof_store)
        ):
            task.state = TaskState.DONE
            task.updated_at = now()
            log.append(Event(now(), "task.transition", task.id, run_id, actor, {"source": "pm_post_qa_proof_guard", "to": TaskState.DONE.value, "proof_ids": len(task.proof_ids)}))
            return task
        if task.state in {TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING} and task.proof_ids:
            _advance_to_next_dev_stage(task)
            task.state = TaskState.RUNNING
            task.updated_at = now()
            log.append(Event(now(), "task.transition", task.id, run_id, actor, {"source": "pm_post_qa_remaining_stage_guard", "to": TaskState.RUNNING.value, "proof_ids": len(task.proof_ids)}))
            return task
        _apply_acceptance(task, decision.payload, actor=actor)
        if (
            is_mission_lead_actor(task, actor)
            and _payload_is_launcher_handoff(decision.payload)
            and not _payload_is_visual_recovery_handoff(decision.payload)
            and _is_cross_stack_backend_first(task)
            and not _has_backend_contract_proof(task, proof_store=proof_store)
        ):
            _block_launcher_release_until_backend_proof(task, log=log, actor=actor, run_id=run_id)
            return task
        if is_mission_lead_actor(task, actor) and _payload_is_launcher_handoff(decision.payload):
            _ensure_scoped_dev_handoff_stage(task, decision.payload, actor=actor, log=log)
            task.affected_repos = ["EterniaLauncher"]
            if _payload_is_visual_recovery_handoff(decision.payload):
                task.risk_flags = [
                    flag
                    for flag in task.risk_flags
                    if flag not in {"cross_stack_backend_proof_missing_before_launcher_release", "sequential_specialist_handoff"}
                ]
            if not _payload_is_visual_recovery_handoff(decision.payload):
                _dedupe_extend(task.risk_flags, ["sequential_specialist_handoff"])
        if is_mission_lead_actor(task, actor) and _payload_is_no_edit_proof_handoff(decision.payload):
            _ensure_scoped_dev_handoff_stage(task, decision.payload, actor=actor, log=log)
        if is_mission_lead_actor(task, actor) and _should_release_backend_first_slice(task):
            task.affected_repos = ["EterniaBackend"]
            _dedupe_extend(task.risk_flags, ["cross_stack_contract_handoff", "backend_contract_first"])
            log.append(
                Event(
                    now(),
                    "cross_stack.backend_first_released",
                    task.id,
                    run_id,
                    actor,
                    {
                        "source": "neko_scope_backend_first_normalization",
                        "next_expected": "backend_dev_verification",
                        "affected_repos": list(task.affected_repos),
                    },
                )
            )
        if is_mission_lead_actor(task, actor):
            _ensure_scoped_dev_handoff_stage(task, decision.payload, actor=actor, log=log)
        task.state = TaskState.RUNNING
        task.updated_at = now()
        log.append(Event(now(), "task.pm_fleshed", task.id, None, actor, {"criteria": len(task.acceptance_criteria)}))
    elif decision.type == DecisionType.REQUEST_FILE_READS:
        if _no_edit_context_stage_has_sufficient_context(task, actor=actor):
            raise DecisionPayloadInvalid(
                "request_file_reads is no longer valid for this no-edit investigation stage: "
                "two or more context bundles are already fulfilled; deliver findings from existing context or block with exact missing evidence."
            )
        req = add_context_request(task, actor=actor, payload=decision.payload)
        task.updated_at = now()
        log.append(
            Event(
                now(),
                "context.requested",
                task.id,
                None,
                actor,
                {
                    "reason": "request_file_reads",
                    "request_id": req.get("id"),
                    "paths": len(req.get("paths", [])),
                    "status": req.get("status"),
                    "failure_reason": req.get("failure_reason"),
                    "bundle_id": req.get("bundle_id"),
                    "summary": _context_request_summary(req),
                    "next_expected": _context_request_next_expected(req),
                    "path_results": (req.get("path_results") or [])[:10] if isinstance(req.get("path_results"), list) else [],
                },
            )
        )
    elif decision.type == DecisionType.REPORT_ISSUE_DISCOVERY:
        discovery = record_issue_discovery(task, decision, actor=actor, run_id=run_id)
        log.append(
            Event(
                now(),
                "issue.discovery_reported",
                task.id,
                run_id,
                actor,
                {
                    "discovery_id": discovery.get("id"),
                    "severity": discovery.get("severity"),
                    "relationship_hint": discovery.get("relationship_hint"),
                    "reported_by": actor,
                },
            )
        )
    elif decision.type == DecisionType.TRIAGE_ISSUE_DISCOVERY:
        discovery_id = str(decision.payload.get("discovery_id", "")).strip()
        before_child = None
        for item in getattr(task, "issue_discoveries", []) or []:
            if item.get("id") == discovery_id:
                before_child = item.get("child_task_id")
                break
        discovery = apply_issue_triage(task, decision, actor=actor, task_store=task_store, incident_store=incident_store)
        log.append(
            Event(
                now(),
                "issue.discovery_triaged",
                task.id,
                run_id,
                actor,
                {
                    "discovery_id": discovery.get("id"),
                    "decision": discovery.get("triage_decision"),
                    "triage_status": discovery.get("triage_status"),
                    "child_task_id": discovery.get("child_task_id"),
                },
            )
        )
        if discovery.get("child_task_id") and discovery.get("child_task_id") != before_child:
            log.append(Event(now(), "issue.child_mission_created", task.id, run_id, actor, {"discovery_id": discovery.get("id"), "child_task_id": discovery.get("child_task_id")}))
    elif decision.type == DecisionType.RESOLVE_INCIDENT:
        if incident_store is None:
            raise DecisionPayloadInvalid("incident_store is required for resolve_incident")
        incident_id = str(decision.payload.get("incident_id", "")).strip()
        incident = incident_store.get(incident_id)
        approved_run_id = None
        cancelled_run_id = None
        approval_type = None
        next_expected = None
        if incident.kind == RUN_BUDGET_EXCEEDED and incident.run_id:
            if budget_incident_needs_scope_recovery(incident, RunStore()):
                try:
                    cancelled = RunStore().cancel(incident.run_id, reason=str(decision.payload.get("resolution") or "Neko scope recovery after Dev read/search loop without delivery"))
                    cancelled_run_id = cancelled.id
                except Exception:
                    cancelled_run_id = None
                approval_type = "scope_recovery"
                next_expected = "dev_fresh_scope_retry"
            elif not budget_incident_can_continue(incident, RunStore()):
                raise DecisionPayloadInvalid("budget continuation cap reached for this Dev session; Neko must block or request human intervention instead of approving another continuation")
            else:
                approved = RunStore().approve_continuation(incident.run_id)
                approved_run_id = approved.id
                approval_type = "budget_continuation"
                next_expected = "dev_same_session_continuation"
        incident_store.close(incident_id, reason=str(decision.payload.get("resolution") or "resolved by Neko supervisor"))
        task.open_incident_ids = [item for item in task.open_incident_ids if item != incident_id]
        if decision.payload.get("next_state"):
            task.state = TaskState(str(decision.payload["next_state"]).strip())
        task.updated_at = now()
        payload = {"incident_id": incident_id, "resolution": decision.payload.get("resolution"), "next_state": str(task.state)}
        if approved_run_id:
            payload["approved_run_id"] = approved_run_id
        if cancelled_run_id:
            payload["cancelled_run_id"] = cancelled_run_id
        if approval_type:
            payload["approval_type"] = approval_type
        if next_expected:
            payload["next_expected"] = next_expected
        log.append(Event(now(), "incident.resolved", task.id, run_id, actor, payload))
    elif decision.type == DecisionType.PROPOSE_STAGE_PLAN:
        if _dev_replanned_materialized_proof_stage(task, actor=actor):
            raise DecisionPayloadInvalid(
                "Dev stage plan loop guard failed: this proof-oriented stage is already materialized; return request_test_run with focused commands, correct_stage, or block with exact evidence."
            )
        stats = _apply_stage_plan(task, decision.payload, actor=actor, log=log)
        if _dev_plan_materialized_no_executable_stage(task, actor=actor, stats=stats):
            raise DecisionPayloadInvalid(
                "Dev stage plan loop guard failed: the plan contained only Neko/Launcher/QA orchestration stages; Dev must provide an executable proof stage or request_test_run."
            )
        if task.state in {TaskState.RUNNING, TaskState.RUNNING}:
            task.state = TaskState.RUNNING
        if task_stage_records(task) and all(stage.test_plan for stage in task_stage_records(task)):
            task.state = TaskState.RUNNING if _dev_plan_can_enter_implementation(task, actor=actor) else TaskState.RUNNING
        task.updated_at = now()
    elif decision.type == DecisionType.REQUEST_TEST_RUN:
        _materialize_test_run_stage(task, decision.payload, actor=actor, log=log)
        if mission_plan_flow:
            mark_plan_stage_from_decision(task, decision, actor=actor, proof_store=proof_store)
        if task.state in {TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING}:
            task.state = TaskState.RUNNING
        elif task.state == TaskState.BLOCKED:
            task.state = TaskState.RUNNING
        task.updated_at = now()
    elif decision.type == DecisionType.CORRECT_STAGE:
        _apply_stage_correction(task, decision.payload, actor=actor, log=log)
        if task.state == TaskState.RUNNING:
            task.state = TaskState.RUNNING if all(stage.test_plan for stage in task_stage_records(task)) else TaskState.RUNNING
        task.updated_at = now()
    elif decision.type in {DecisionType.APPROVE, DecisionType.REPORT_QA_VERDICT}:
        if (
            decision.type == DecisionType.APPROVE
            and task.state in {TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING}
            and task.proof_ids
            and _all_stages_passed(task)
            and not _needs_cross_stack_launcher_completion(task, proof_store=proof_store)
        ):
            task.state = TaskState.DONE
            task.updated_at = now()
            log.append(Event(now(), "task.transition", task.id, run_id, actor, {"source": "pm_post_qa_approve_guard", "to": TaskState.DONE.value, "proof_ids": len(task.proof_ids)}))
            return task
        if decision.type == DecisionType.APPROVE and task.state in {TaskState.RUNNING, TaskState.RUNNING, TaskState.RUNNING} and task.proof_ids:
            _advance_to_next_dev_stage(task)
            task.state = TaskState.RUNNING
            task.updated_at = now()
            log.append(Event(now(), "task.transition", task.id, run_id, actor, {"source": "pm_post_qa_approve_remaining_stage_guard", "to": TaskState.RUNNING.value, "proof_ids": len(task.proof_ids)}))
            return task
        if decision.payload.get("review_scope", "plan") == "implementation":
            _apply_implementation_review(task, decision.payload, actor=actor, proof_store=proof_store)
            if mission_plan_flow:
                mark_plan_stage_from_decision(task, decision, actor=actor, proof_store=proof_store)
        else:
            _apply_plan_review(task, decision.payload, actor=actor, log=log)
            gate = can_enter_dev_implementing(task)
            if gate.allowed and task.state == TaskState.RUNNING:
                task.state = TaskState.RUNNING
            elif not gate.allowed and task.state == TaskState.RUNNING:
                task.state = TaskState.RUNNING
        task.updated_at = now()
    elif decision.type in {DecisionType.HAND_OFF, DecisionType.PROPOSE_PATCH}:
        if proof_store is not None and not decision.payload.get("proof_ids") and not normal_worker_flow:
            raise DecisionPayloadInvalid("proof_ids are required before handing implementation to QA")
        if proof_store is not None and decision.payload.get("proof_ids"):
            proof_ids = decision.payload.get("proof_ids", [])
            _validate_qa_handoff_proof_readiness(
                task,
                proof_ids,
                proof_store=proof_store,
                action_label="hand_off",
                stage_id=task.current_stage_id,
            )
            _validate_commit_deploy_gate(task, decision, proof_store=proof_store, stage_id=task.current_stage_id)
            _merge_existing_proof_ids(task, proof_ids, proof_store=proof_store)
        if normal_worker_flow and not decision.payload.get("proof_ids"):
            if mission_plan_flow:
                mark_plan_stage_from_decision(task, decision, actor=actor, proof_store=proof_store)
                stage = current_plan_stage(task)
                if stage is not None and stage.kind in {"context", "investigation", "audit"} and not stage_requires_product_edit(task, stage):
                    task.state = TaskState.RUNNING
                    task.updated_at = now()
                    log.append(_delivery_intent_event(task.id, actor, decision, mode="no_edit", no_edit=True, normal_worker_flow=True))
                    return task
            if task.current_stage_id:
                for stage in task_stage_records(task):
                    if stage.id == task.current_stage_id:
                        stage.status = StageStatus.IMPLEMENTING
                        stage.updated_at = now()
                        break
            task.state = TaskState.RUNNING
            task.updated_at = now()
            log.append(_delivery_intent_event(task.id, actor, decision, mode="proof_only", diff_chars=0, normal_worker_flow=True))
            return task
        if task.current_stage_id:
            for stage in task_stage_records(task):
                if stage.id == task.current_stage_id:
                    stage.status = StageStatus.READY_FOR_QA
                    stage.updated_at = now()
                    break
        if mission_plan_flow:
            mark_plan_stage_from_decision(task, decision, actor=actor, proof_store=proof_store)
        if not _all_stages_dev_complete(task):
            _advance_to_next_dev_stage(task)
        task.state = TaskState.RUNNING if _all_stages_dev_complete(task) else TaskState.RUNNING
        task.updated_at = now()
        log.append(_delivery_intent_event(task.id, actor, decision, mode="patch"))
    elif decision.type == DecisionType.REQUEST_QA_REVIEW:
        proof_ids = decision.payload.get("proof_ids", [])
        stage_id = str(decision.payload.get("stage_id") or task.current_stage_id or "").strip()
        if task.state in {TaskState.RUNNING, TaskState.RUNNING} or proof_ids:
            _validate_qa_handoff_proof_readiness(task, proof_ids, proof_store=proof_store, stage_id=stage_id)
            _validate_commit_deploy_gate(task, decision, proof_store=proof_store, stage_id=stage_id)
            _merge_existing_proof_ids(task, proof_ids, proof_store=proof_store)
            if mission_plan_flow:
                attach_proofs_to_plan_stage(task, stage_id, proof_ids, proof_store=proof_store)
            if stage_id:
                task.current_stage_id = stage_id
                for stage in task_stage_records(task):
                    if stage.id == stage_id:
                        stage.status = StageStatus.READY_FOR_QA
                        stage.updated_at = now()
                        break
            if not _all_stages_dev_complete(task):
                if _needs_sequential_specialist_join(task):
                    task.state = TaskState.RUNNING
                else:
                    _advance_to_next_dev_stage(task)
                    task.state = TaskState.RUNNING
            else:
                task.state = TaskState.RUNNING
        else:
            task.state = TaskState.RUNNING
        task.updated_at = now()
    elif decision.type in {DecisionType.NEEDS_CONTEXT, DecisionType.REQUEST_HUMAN}:
        if _coerce_neko_needs_context_to_handoff_continuation(task, decision, actor=actor, log=log, run_id=run_id, proof_store=proof_store):
            return task
        # Do not let daemon ticks repeatedly re-run the same persona after a
        # valid structured request for missing external context. Blocking keeps
        # the intervention visible through observability/context-request signals
        # without inventing proof or bypassing gates.
        _soft_block_task(task, reason="missing external context requested", stage_id=task.current_stage_id)
        if is_mission_lead_actor(task, actor):
            mark_block_recovery_attempt(task)
        task.updated_at = now()
    elif decision.type == DecisionType.BLOCK:
        if is_mission_lead_actor(task, actor):
            _reject_mission_lead_block_on_closable_incident(task, incident_store=incident_store)
        _soft_block_task(task, reason=str(decision.summary or "agent reported blocker"), stage_id=task.current_stage_id)
        if is_mission_lead_actor(task, actor):
            mark_block_recovery_attempt(task)
        task.updated_at = now()
    return task


def _reject_mission_lead_block_on_closable_incident(task: Task, *, incident_store=None) -> None:
    """Repair feedback when Neko blocks on an incident it can close itself.

    An open incident whose underlying run is already terminal (cancelled,
    failed, completed, stale) needs no external actor: the adjudication turn
    holds the resolve_incident capability, so answering ``block`` just parks
    the goal on an operator. Raising here routes the repair back to Neko.
    """

    from .states import RunState
    from .store import IncidentStore

    store = incident_store or IncidentStore()
    for incident_id in list(getattr(task, "open_incident_ids", []) or [])[:10]:
        try:
            incident = store.get(incident_id)
        except Exception:
            continue
        run_id = getattr(incident, "run_id", None)
        if not run_id:
            continue
        try:
            run = RunStore().get(str(run_id))
        except Exception:
            continue
        if run.state in {RunState.CANCELLED, RunState.FAILED, RunState.COMPLETED, RunState.STALE}:
            raise DecisionPayloadInvalid(
                f"block is not a valid adjudication for incident {incident_id}: its underlying run is already "
                f"terminal ({run.state.value}). Emit resolve_incident with this incident_id and a "
                "redaction-safe resolution instead of blocking on an incident you can close."
            )


def _coerce_neko_needs_context_to_handoff_continuation(task: Task, decision: AgentDecision, *, actor: str, log: EventLog, run_id: str | None, proof_store=None) -> bool:
    if not is_mission_lead_actor(task, actor) or decision.type != DecisionType.NEEDS_CONTEXT:
        return False
    if getattr(task, "open_incident_ids", None):
        return False
    typed_launcher_handoff = _payload_handoff_request_targets_launcher(decision.payload)
    legacy_launcher_handoff = _payload_is_launcher_handoff(decision.payload) or _summary_is_missing_launcher_proof(decision)
    if typed_launcher_handoff and legacy_launcher_handoff:
        log.append(
            Event(
                now(),
                "handoff_request.deprecated_heuristic_agreement",
                task.id,
                run_id,
                actor,
                {
                    "target_repo": "EterniaLauncher",
                    "source": "needs_context.handoff_request",
                    "legacy_heuristic": "launcher_handoff",
                    "summary": "Typed handoff_request agreed with legacy Launcher handoff prose heuristic.",
                },
            )
        )
    launcher_handoff_requested = typed_launcher_handoff or legacy_launcher_handoff
    blocked_recovery = task.state == TaskState.BLOCKED and (
        launcher_handoff_requested
    )
    proof_backed_join = (
        task.state == TaskState.RUNNING
        and _is_cross_stack_backend_first(task)
        and _has_backend_contract_proof(task, proof_store=proof_store)
        and _has_backend_contract_delivery_packet(task, event_log=log)
        and _needs_cross_stack_launcher_completion(task, proof_store=proof_store)
        and (launcher_handoff_requested or not _has_launcher_stage(task))
    )
    proof_backed_missing_packet = (
        task.state == TaskState.RUNNING
        and _is_cross_stack_backend_first(task)
        and _has_backend_contract_proof(task, proof_store=proof_store)
        and not _has_backend_contract_delivery_packet(task, event_log=log)
        and (launcher_handoff_requested or "contract packet" in f"{decision.summary} {decision.rationale}".lower())
    )
    if proof_backed_missing_packet:
        _route_backend_contract_packet_repair(task, actor=actor, log=log, run_id=run_id)
        return True
    if not blocked_recovery and not proof_backed_join:
        return False
    if not getattr(task, "proof_ids", None) and not task_stage_records(task):
        return False
    _ensure_scoped_dev_handoff_stage(
        task,
        {
            "objective": task.description,
            "acceptance_criteria": list(task.acceptance_criteria or []),
            "handoff_packet": {
                "packet_kind": "contract_join",
                "target_owner": "dev",
                "target_repo": "EterniaLauncher",
                "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed"},
            },
        },
        actor=actor,
        log=log,
    )
    task.affected_repos = ["EterniaLauncher"]
    _dedupe_extend(task.risk_flags, ["sequential_specialist_handoff", "post_scope_wait_coerced_to_handoff"])
    task.risk_flags = [flag for flag in task.risk_flags if flag != "cross_stack_launcher_release_missing"]
    task.state = TaskState.RUNNING
    task.updated_at = now()
    source = "proof_backed_neko_needs_context_launcher_handoff" if proof_backed_join else "post_scope_needs_context_handoff_continuation"
    log.append(
        Event(
            now(),
            "task.transition",
            task.id,
            run_id,
            actor,
            {
                "source": source,
                "from_decision": decision.type.value,
                "next_state": TaskState.RUNNING.value,
                "next_expected": "launcher_dev_verification",
                "summary": "Proof-backed Neko context wait converted to deterministic Launcher Dev handoff because required Launcher proof is the next missing artifact.",
            },
        )
    )
    return True


def _no_edit_context_stage_has_sufficient_context(task: Task, *, actor: str, threshold: int = 2) -> bool:
    if actor not in {"dev", "backend_dev"}:
        return False
    stage = current_plan_stage(task)
    if stage is None:
        return False
    if str(getattr(stage, "kind", "") or "") not in {"context", "investigation", "audit"}:
        return False
    if stage_requires_product_edit(task, stage):
        return False
    stage_id = str(getattr(stage, "id", "") or "")
    count = 0
    for req in getattr(task, "context_requests", []) or []:
        if not isinstance(req, dict):
            continue
        if req.get("status") not in {"fulfilled", "fulfilled_partial", "superseded"}:
            continue
        req_actor = str(req.get("actor") or "")
        if req_actor and req_actor not in {actor, "dev", "backend_dev"}:
            continue
        req_stage_id = str(req.get("stage_id") or "")
        reason = str(req.get("reason") or "")
        if stage_id and req_stage_id and req_stage_id != stage_id:
            continue
        if stage_id and not req_stage_id and stage_id not in reason and str(getattr(task, "current_stage_id", "") or "") != stage_id:
            continue
        count += 1
    return count >= threshold


def _route_backend_contract_packet_repair(task: Task, *, actor: str, log: EventLog, run_id: str | None) -> None:
    sid = "stage_47_backend_contract_packet"
    stage = next((item for item in task_stage_records(task) if item.id == sid), None)
    if stage is None:
        stage = TaskStage(
            id=sid,
            title="Stage 47 Backend Contract Packet",
            objective="Attach a compact backend-to-Launcher contract packet and preserve the existing backend proof handoff.",
            status=StageStatus.IMPLEMENTING,
            affected_paths=[],
            acceptance_criteria=[
                "Backend Dev emits delivery.contract_packet with endpoint/request/response/error/example or selected contract surface.",
                "Backend Dev includes produced_contract_packet_id and consumed_proof_ids for the passed backend proof.",
                "Neko must not release Launcher Dev until the backend contract packet is visible in context.",
            ],
            test_plan=[".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"],
            created_at=now(),
            updated_at=now(),
        )
        append_task_stage_record(task, stage)
        log.append(Event(now(), "task.stage_added", task.id, None, actor, {"stage_id": sid, "title": stage.title, "source": "backend_contract_packet_repair"}))
    else:
        stage.status = StageStatus.IMPLEMENTING
        stage.updated_at = now()
        if not stage.test_plan:
            stage.test_plan = [".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"]
        log.append(Event(now(), "task.stage_updated", task.id, None, actor, {"stage_id": sid, "source": "backend_contract_packet_repair"}))
    task.current_stage_id = sid
    task.affected_repos = ["EterniaBackend"]
    task.state = TaskState.RUNNING
    _dedupe_extend(task.risk_flags, ["backend_contract_packet_missing_repair"])
    task.risk_flags = [flag for flag in task.risk_flags if flag != "cross_stack_launcher_release_missing"]
    task.updated_at = now()
    log.append(
        Event(
            now(),
            "cross_stack.backend_contract_packet_missing",
            task.id,
            run_id,
            actor,
            {
                "source": "neko_cross_stack_join_gate",
                "next_state": TaskState.RUNNING.value,
                "next_expected": "backend_dev_contract_packet_repair",
                "summary": "Backend proof exists, but the backend-to-Launcher contract packet is missing; routed back to Backend Dev.",
                "proof_ids": len(getattr(task, "proof_ids", []) or []),
                "stage_id": sid,
            },
        )
    )


def _apply_acceptance(task: Task, payload: dict[str, Any], *, actor: str | None = None) -> None:
    task.routing_scope = _routing_scope_from_acceptance(payload, actor=actor)
    preserve_operator_goal = bool(
        actor
        and is_mission_lead_actor(task, actor)
        and _task_has_operator_goal_detail(task)
    )
    objective = str(payload.get("objective", "")).strip()
    if objective and not preserve_operator_goal:
        task.description = objective
    if not preserve_operator_goal:
        task.acceptance_criteria = list(payload.get("acceptance_criteria", []))
        task.non_goals = list(payload.get("non_goals", []))
    affected_repos = _canonical_affected_repos(payload.get("affected_repos", []) or [])
    fallback_repos = _affected_repos_from_handoff(payload) or _canonical_affected_repos(_declared_repo_scope(task))
    if affected_repos or fallback_repos:
        task.affected_repos = affected_repos or fallback_repos
    if not preserve_operator_goal:
        task.suggested_roles = list(payload.get("suggested_roles", []))
    task.requires_visual_proof = bool(payload.get("requires_visual_proof", task.requires_visual_proof))
    if not preserve_operator_goal:
        task.risk_flags = list(payload.get("risk_flags", []))


def _routing_scope_from_acceptance(payload: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
    scope = {
        "objective": str(payload.get("objective", "")).strip() or None,
        "acceptance_criteria": list(payload.get("acceptance_criteria", [])),
        "non_goals": list(payload.get("non_goals", [])),
        "suggested_roles": list(payload.get("suggested_roles", [])),
        "risk_flags": list(payload.get("risk_flags", [])),
        "affected_repos": _canonical_affected_repos(payload.get("affected_repos", []) or []),
        "handoff_target_repo": _routing_scope_handoff_target_repo(payload),
        "actor": actor,
        "updated_at": now(),
    }
    return {key: value for key, value in scope.items() if value not in (None, [], "")}


def _task_has_operator_goal_detail(task: Task) -> bool:
    """Did the OPERATOR state goal detail that Neko must not overwrite?

    Stage 15.3 note: this used to answer ``True`` for the mere presence of a
    ``mission_intent``, which was a fair proxy while typing was conditional — an
    intent existed only if something had deliberately authored one. Routing is
    now unconditional, so ``ensure_default_mission_plan`` synthesizes an intent
    for *every* mission from the task's own title/description. Left as-is the
    clause degenerates to "always true" and Neko's scope route silently stops
    updating acceptance criteria on every mission in the runtime.

    So an intent counts as operator detail only when it actually CARRIES detail
    — acceptance criteria or non-goals — rather than mirroring the task.
    ``MissionIntent.locked`` is deliberately NOT part of the test: it is ``True``
    on every one of the eight construction paths, either explicitly
    (`blueprints/instantiate.py`, `default_plan.py`, `mission_plan.py`
    ``_mission_intent_from_task``) or by the `models.py` field default
    (`persona_diagnostics.py`, `root_node_engine.py`, `ticker.py`
    ``legacy_command_proof``). The one path that could set it otherwise
    (`mission_plan.py` ``_plan_from_payload``) is gated on ``not intent.locked``
    and is therefore unreachable. ``locked`` says nothing about who authored the
    intent.
    """

    plan = getattr(task, "mission_plan", None)
    intent = getattr(plan, "mission_intent", None)
    if intent is not None and (
        getattr(intent, "acceptance_criteria", None) or getattr(intent, "non_goals", None)
    ):
        return True
    return bool(
        getattr(task, "acceptance_criteria", None)
        or getattr(task, "non_goals", None)
        or getattr(task, "suggested_roles", None)
        or getattr(task, "risk_flags", None)
    )


def _routing_scope_handoff_target_repo(payload: dict[str, Any]) -> str | None:
    handoff = payload.get("handoff_packet")
    if not isinstance(handoff, dict):
        return None
    repo = str(handoff.get("target_repo") or "").strip()
    return repo or None


def _routing_scope_text(task: Task) -> str:
    scope = getattr(task, "routing_scope", None)
    if not isinstance(scope, dict):
        return ""
    return " ".join(
        [
            str(scope.get("objective") or ""),
            " ".join(str(item) for item in (scope.get("acceptance_criteria") or [])),
            " ".join(str(item) for item in (scope.get("non_goals") or [])),
            " ".join(str(item) for item in (scope.get("risk_flags") or [])),
            " ".join(str(item) for item in (scope.get("affected_repos") or [])),
        ]
    )


def _routing_scope_flags(task: Task) -> set[str]:
    scope = getattr(task, "routing_scope", None)
    if not isinstance(scope, dict):
        return set()
    return {str(flag).strip().lower() for flag in (scope.get("risk_flags") or [])}


def _delivery_intent_event(
    task_id: str,
    actor: str | None,
    decision: AgentDecision,
    *,
    mode: str,
    no_edit: bool = False,
    diff_chars: int | None = None,
    normal_worker_flow: bool = False,
) -> Event:
    payload: dict[str, Any] = {
        "mode": mode,
        "requires_approval": decision.requires_approval,
        "normal_worker_flow": normal_worker_flow,
        "no_edit": no_edit,
        "summary": str(decision.summary or "Delivery handoff recorded.").strip()[:500],
    }
    if diff_chars is not None:
        payload["diff_chars"] = diff_chars
    changed_files = decision.payload.get("changed_files") or decision.payload.get("files_touched")
    if isinstance(changed_files, list):
        payload["changed_files"] = [str(item) for item in changed_files[:40]]
        payload["changed_file_count"] = len(changed_files)
    return Event(now(), "delivery.intent", task_id, None, actor, payload)


def _release_stage_affected_repos(task: Task, stage_repo: str | None) -> list[str]:
    """Repo scope to apply when a typed-plan stage is released.

    The task-level scope describes the whole mission, not just the first
    runnable stage. Keep explicit single-repo goals pinned, otherwise surface
    the typed mission graph's repo union so observability and fallback routing
    cannot forget a sibling Launcher/Backend stage while a fork is live.
    """

    pinned_repos = _canonical_affected_repos(_declared_repo_scope(task))
    if pinned_repos:
        return pinned_repos
    routing_scope = getattr(task, "routing_scope", None)
    routing_repos = _canonical_affected_repos(
        routing_scope.get("affected_repos", []) if isinstance(routing_scope, dict) else []
    )
    routing_target_repo = str(routing_scope.get("handoff_target_repo") or "").strip() if isinstance(routing_scope, dict) else ""
    if len(routing_repos) == 1 and routing_target_repo:
        return routing_repos
    plan_repos = _mission_plan_affected_repos(task)
    task_repos = _canonical_affected_repos(getattr(task, "affected_repos", []) or [])
    if len(task_repos) == 1:
        if len(plan_repos) > 1 and _mission_text_mentions_cross_stack(task):
            return plan_repos
        return task_repos
    if plan_repos:
        return plan_repos
    repo = str(stage_repo or "").strip()
    if not repo or repo == "none":
        return []
    from .final_gate import default_blueprint_placeholder_repo_override

    override = default_blueprint_placeholder_repo_override(task, repo)
    return [override or repo]


def _mission_plan_affected_repos(task: Task) -> list[str]:
    plan = getattr(task, "mission_plan", None)
    repos: list[str] = []
    for stage in getattr(plan, "stages", []) or []:
        repo = str(getattr(stage, "repo", "") or "").strip()
        if not repo or repo == "none":
            continue
        for canonical in _canonical_affected_repos([repo]):
            if canonical not in repos:
                repos.append(canonical)
    return repos


def _mission_text_mentions_cross_stack(task: Task) -> bool:
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "risk_flags", []) or [])),
        ]
    ).lower()
    mentions_backend = "backend" in text or "eterniabackend" in text
    mentions_launcher = "launcher" in text or "eternialauncher" in text
    if mentions_backend and mentions_launcher:
        return True
    return any(
        marker in text
        for marker in ("cross-stack", "cross_stack", "parallel", "fork-join", "fork join")
    )


def _canonical_affected_repos(repos: Iterable[str]) -> list[str]:
    from .repo_context import canonical_repo_scope_label

    result: list[str] = []
    for repo in repos:
        text = str(repo).strip()
        if not text:
            continue
        label = canonical_repo_scope_label(text) or text
        if label not in result:
            result.append(label)
    return result


def _declared_repo_scope(task: Task) -> list[str]:
    values = (getattr(task, "harness_self_heal", None) or {}).get("repo_scope_pinned") or []
    return [str(item).strip() for item in values if str(item).strip()]


def _validate_affected_repo_scope(task: Task, decision: AgentDecision, *, actor: str, log: EventLog, run_id: str | None) -> None:
    """Reject affected_repos that silently contradict the goal's named repo scope.

    A goal that literally names a repo in its title/description must not be
    scoped to a different repo without a recorded justification. An
    operator-pinned repo scope from task creation is stricter: it is an
    explicit runtime boundary and cannot be overridden by a Neko packet.
    """

    from .persona_assignments import safe_assignment_text
    from .repo_context import canonical_repo_scope_label, explicit_repo_mentions

    payload = decision.payload if isinstance(decision.payload, dict) else {}
    repos = [str(repo).strip() for repo in (payload.get("affected_repos") or []) if str(repo).strip()]
    if not repos:
        return
    canonical: list[str] = []
    for repo in repos:
        label = canonical_repo_scope_label(repo)
        if label is not None and label not in canonical:
            canonical.append(label)
    if not canonical:
        # Free-form scope (absolute repo paths, workspace-specific labels) is
        # outside the canonical-alias contract; only canonical scopes are
        # cross-checked against the goal's named repo.
        return
    pinned = _declared_repo_scope(task)
    mentions = list(explicit_repo_mentions(f"{task.title or ''} {task.description or ''}"))
    conflicts: list[str] = []
    if pinned and set(canonical) != set(pinned):
        raise DecisionPayloadInvalid(
            f"affected_repos/target_repo {canonical} contradicts the operator pinned repo scope "
            f"{pinned}. Operator-pinned scope cannot be overridden by Neko; create a new goal "
            "or use an operator rescope before widening the runtime boundary."
        )
    if mentions and not (set(canonical) & set(mentions)):
        conflicts.append(f"goal title/description literally names {mentions}")
    if not conflicts:
        return
    override = str(payload.get("scope_override_reason") or "").strip()
    if not override:
        raise DecisionPayloadInvalid(
            f"affected_repos/target_repo {canonical} contradicts the goal's named repo scope "
            f"({'; '.join(conflicts)}). Scope to the repo the goal names, or include "
            "scope_override_reason recording why the goal text is wrong."
        )
    log.append(
        Event(
            now(),
            "scope.override_recorded",
            task.id,
            run_id,
            actor,
            {
                "affected_repos": canonical,
                "named_repo_scope": pinned or mentions,
                "scope_override_reason": safe_assignment_text(override, limit=240),
                "summary": "Scope diverges from the repo named by the goal; justification recorded.",
            },
        )
    )


def _affected_repos_from_handoff(payload: dict[str, Any]) -> list[str]:
    handoff = payload.get("handoff_packet")
    if not isinstance(handoff, dict):
        return []
    target_repo = str(handoff.get("target_repo") or "").strip()
    if target_repo in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        return [target_repo]
    target_owner = str(handoff.get("target_dev_persona") or handoff.get("target_owner") or "").strip()
    if target_owner == "backend_dev":
        return ["EterniaBackend"]
    if target_owner in {"dev", "launcher_dev"}:
        return ["EterniaLauncher"]
    return []


def _apply_stage_plan(task: Task, payload: dict[str, Any], *, actor: str, log: EventLog) -> dict[str, int]:
    by_id = {stage.id: stage for stage in task_stage_records(task)}
    applied = 0
    skipped = 0
    for idx, raw in enumerate(payload["stages"], start=1):
        skip_reason = _skip_dev_orchestration_stage(raw, actor=actor, task=task)
        if skip_reason:
            skipped += 1
            log.append(
                Event(
                    now(),
                    "task.stage_updated",
                    task.id,
                    None,
                    actor,
                    {
                        "stage_id": str(raw.get("id") or f"stage_{idx}"),
                        "source": skip_reason,
                    },
                )
            )
            continue
        sid = str(raw.get("id") or f"stage_{idx}")
        if sid in by_id:
            stage = by_id[sid]
            stage.title = str(raw["title"]).strip()
            stage.objective = str(raw["objective"]).strip()
            stage.status = StageStatus.READY if raw.get("test_plan") else StageStatus.DRAFT
            stage.acceptance_criteria = list(raw.get("acceptance_criteria", []))
            stage.affected_paths = list(raw.get("affected_paths", []))
            stage.test_plan = list(raw.get("test_plan", []))
            stage.requires_visual_proof = raw.get("requires_visual_proof", stage.requires_visual_proof)
            stage.updated_at = now()
            event_type = "task.stage_updated"
        else:
            stage = TaskStage(
                id=sid,
                title=str(raw["title"]).strip(),
                objective=str(raw["objective"]).strip(),
                status=StageStatus.READY if raw.get("test_plan") else StageStatus.DRAFT,
                affected_paths=list(raw.get("affected_paths", [])),
                acceptance_criteria=list(raw.get("acceptance_criteria", [])),
                test_plan=list(raw.get("test_plan", [])),
                requires_visual_proof=raw.get("requires_visual_proof"),
                created_at=now(),
                updated_at=now(),
            )
            append_task_stage_record(task, stage)
            by_id[sid] = stage
            if task.current_stage_id is None:
                task.current_stage_id = sid
            event_type = "task.stage_added"
        applied += 1
        log.append(Event(now(), event_type, task.id, None, actor, {"stage_id": sid, "title": stage.title}))
    return {"applied": applied, "skipped": skipped}


def _skip_dev_orchestration_stage(raw: dict[str, Any], *, actor: str, task: Task | None = None) -> str | None:
    if not _is_dev_actor(actor):
        return None
    haystack = _raw_stage_haystack(raw)
    orchestration_scope = _raw_stage_identity(raw) if _raw_stage_has_executable_test_plan(raw) else haystack
    if (
        actor == "backend_dev"
        and task is not None
        and _is_cross_stack_backend_first(task)
        and _raw_stage_mentions_launcher(raw)
    ):
        if not _raw_stage_is_backend_owned_proof(raw):
            return "backend_dev_launcher_stage_skipped_until_backend_proof_join"
    if ("neko" in orchestration_scope or "scope freeze" in orchestration_scope or "join gate" in orchestration_scope) and not _raw_stage_is_backend_owned_proof(raw):
        return "dev_stage_plan_orchestration_stage_skipped"
    if "qa" in orchestration_scope and any(marker in orchestration_scope for marker in ("verify", "verifies", "verification", "approval", "verdict", "testing", "review")) and not _raw_stage_is_backend_owned_proof(raw):
        return "dev_stage_plan_orchestration_stage_skipped"
    return None


def _raw_stage_has_executable_test_plan(raw: dict[str, Any]) -> bool:
    return any(str(item).strip() for item in (raw.get("test_plan") or []))


def _raw_stage_identity(raw: dict[str, Any]) -> str:
    return " ".join(
        [
            str(raw.get("id", "")),
            str(raw.get("title", "")),
            str(raw.get("objective", "")),
        ]
    ).lower()


def _dev_replanned_materialized_proof_stage(task: Task, *, actor: str) -> bool:
    if not _is_dev_actor(actor):
        return False
    if not _task_is_bounded_proof_or_burnin(task):
        return False
    stage = _current_stage(task)
    if stage is None:
        return False
    state = task.state if isinstance(task.state, TaskState) else TaskState(task.state)
    if state not in {TaskState.RUNNING, TaskState.RUNNING}:
        return False
    return bool(getattr(stage, "test_plan", None))


def _dev_plan_materialized_no_executable_stage(task: Task, *, actor: str, stats: dict[str, int]) -> bool:
    if not _is_dev_actor(actor):
        return False
    if not _task_is_bounded_proof_or_burnin(task):
        return False
    return (stats.get("applied") or 0) == 0 and (stats.get("skipped") or 0) > 0 and not task_stage_records(task)


def _dev_plan_can_enter_implementation(task: Task, *, actor: str) -> bool:
    return _is_dev_actor(actor) or _task_is_bounded_proof_or_burnin(task)


def _is_dev_actor(actor: str) -> bool:
    return actor in {"dev", "backend_dev"} or str(actor).endswith("_dev")


def _task_is_bounded_proof_or_burnin(task: Task) -> bool:
    flags = {str(flag).strip().lower() for flag in (getattr(task, "risk_flags", []) or [])}
    if flags.intersection(
        {
            "bounded_complex_burn_in",
            "routing_burn_in_only",
            "no_product_edits",
            "no_edit_smoke",
            "proof_ids_required_before_qa",
        }
    ):
        return True
    if str(getattr(task, "requested_by", "") or "").strip() == "stage47_burn_in":
        return True
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
        ]
    ).lower()
    return "no-edit" in text or "no product edits" in text or "command proof" in text


def _raw_stage_is_backend_owned_proof(raw: dict[str, Any]) -> bool:
    haystack = _raw_stage_haystack(raw)
    identity = f"{raw.get('id', '')} {raw.get('title', '')}".lower()
    affected = " ".join(str(item) for item in raw.get("affected_paths", []) or []).lower()
    if any(marker in identity for marker in ("launcher", "frontend", "front-end", "qa", "neko", "join gate")):
        return False
    if "eternialauncher" in affected or "launcher" in affected or "frontend" in affected or "front-end" in affected:
        return False
    return _text_mentions_backend(haystack) and any(marker in haystack for marker in ("proof", "contract", "test", "smoke", "verify"))


def _raw_stage_haystack(raw: dict[str, Any]) -> str:
    return " ".join(
        [
            str(raw.get("id", "")),
            str(raw.get("title", "")),
            str(raw.get("objective", "")),
            " ".join(str(item) for item in raw.get("acceptance_criteria", []) or []),
            " ".join(str(item) for item in raw.get("affected_paths", []) or []),
            " ".join(str(item) for item in raw.get("test_plan", []) or []),
            "visual" if raw.get("requires_visual_proof") else "",
        ]
    ).lower()


def _materialize_test_run_stage(task: Task, payload: dict[str, Any], *, actor: str, log: EventLog) -> None:
    sid = str(payload.get("stage_id") or task.current_stage_id or "stage_1").strip() or "stage_1"
    commands = [str(command).strip() for command in payload.get("commands", []) if str(command).strip()]
    existing = next((stage for stage in task_stage_records(task) if stage.id == sid), None)
    _validate_no_edit_recipe_stage_target(task, requested_stage_id=sid, requested_stage=existing, payload=payload)
    if existing is None:
        stage = TaskStage(
            id=sid,
            title=sid.replace("_", " ").strip().title() or "Command Proof",
            objective=f"Collect deterministic command proof for {task.title}.",
            status=StageStatus.READY,
            affected_paths=[],
            acceptance_criteria=list(task.acceptance_criteria or ["Deterministic command proof is attached."]),
            test_plan=commands,
            created_at=now(),
            updated_at=now(),
        )
        append_task_stage_record(task, stage)
        log.append(Event(now(), "task.stage_added", task.id, None, actor, {"stage_id": sid, "title": stage.title, "source": "request_test_run"}))
    else:
        if commands:
            _dedupe_extend(existing.test_plan, commands)
        if not existing.acceptance_criteria:
            existing.acceptance_criteria = list(task.acceptance_criteria or ["Deterministic command proof is attached."])
        if existing.status == StageStatus.DRAFT and existing.test_plan:
            existing.status = StageStatus.READY
        existing.updated_at = now()
        log.append(Event(now(), "task.stage_updated", task.id, None, actor, {"stage_id": sid, "source": "request_test_run"}))
    task.current_stage_id = sid


def _apply_stage_correction(task: Task, payload: dict[str, Any], *, actor: str, log: EventLog) -> None:
    sid = str(payload["stage_id"])
    stage = next((stage for stage in task_stage_records(task) if stage.id == sid), None)
    if stage is None:
        raise DecisionPayloadInvalid(f"unknown stage_id: {sid}")
    _dedupe_extend(stage.corrections, [f"{actor}: {item}" for item in payload.get("corrections", [])])
    _dedupe_extend(stage.audit_notes, [f"{actor}: {item}" for item in payload.get("audit_notes", [])])
    _dedupe_extend(stage.affected_paths, payload.get("affected_paths", []))
    _dedupe_extend(stage.test_plan, payload.get("test_plan", []))
    stage.updated_at = now()
    log.append(Event(now(), "task.stage_corrected", task.id, None, actor, {"stage_id": sid, "corrections": len(payload.get("corrections", []))}))
    target_stage_id = _correct_stage_target_id(task, payload, source_stage_id=sid)
    if target_stage_id and target_stage_id != sid:
        target = next((stage for stage in task_stage_records(task) if stage.id == target_stage_id), None)
        if target is None:
            raise DecisionPayloadInvalid(f"unknown target_stage_id: {target_stage_id}")
        task.current_stage_id = target.id
        if target.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.BLOCKED}:
            target.status = StageStatus.IMPLEMENTING
            target.updated_at = now()
        if stage.status == StageStatus.IMPLEMENTING and stage_requires_product_edit(task, target):
            stage.status = StageStatus.BLOCKED
            stage.updated_at = now()
        log.append(
            Event(
                now(),
                "task.stage_updated",
                task.id,
                None,
                actor,
                {
                    "stage_id": target.id,
                    "status": target.status.value,
                    "reason": f"correct_stage rerouted from {sid}",
                    "from_stage_id": sid,
                },
            )
        )


def _validate_no_edit_recipe_stage_target(
    task: Task,
    *,
    requested_stage_id: str,
    requested_stage: TaskStage | None,
    payload: dict[str, Any],
) -> None:
    recipe_id = str(payload.get("recipe_id") or "").strip()
    if not no_product_edit_recipe_id(recipe_id):
        return
    if requested_stage is not None and no_product_edit_recipe_conflicts_with_stage(task, requested_stage, recipe_id):
        raise DecisionPayloadInvalid(
            f"request_test_run recipe_id {recipe_id!r} is no-product-edit smoke proof and cannot satisfy product-edit stage {requested_stage_id!r}; patch first and request focused implementation proof."
        )
    current_stage_id = str(getattr(task, "current_stage_id", "") or "").strip()
    current_stage = next((stage for stage in task_stage_records(task) if stage.id == current_stage_id), None)
    if (
        current_stage is not None
        and current_stage_id != requested_stage_id
        and current_stage.status not in DEV_COMPLETE_STAGE_STATUSES
        and stage_requires_product_edit(task, current_stage)
    ):
        raise DecisionPayloadInvalid(
            f"request_test_run recipe_id {recipe_id!r} cannot bypass incomplete product-edit stage {current_stage_id!r}; return hand_off/correct_stage for that stage or request focused Flutter/widget proof after edits."
        )
    incomplete_product_stage = first_incomplete_product_edit_stage(task, excluding_stage_id=requested_stage_id)
    if requested_stage is None and incomplete_product_stage is not None:
        raise DecisionPayloadInvalid(
            f"request_test_run cannot materialize no-product-edit helper stage {requested_stage_id!r} while product-edit stage {incomplete_product_stage.id!r} is incomplete."
        )


def _correct_stage_target_id(task: Task, payload: dict[str, Any], *, source_stage_id: str) -> str | None:
    explicit = str(payload.get("target_stage_id") or payload.get("set_current_stage_id") or "").strip()
    if explicit:
        return explicit
    text = " ".join(
        [
            " ".join(str(item) for item in payload.get("corrections", []) or []),
            " ".join(str(item) for item in payload.get("audit_notes", []) or []),
            " ".join(str(item) for item in payload.get("test_plan", []) or []),
        ]
    )
    return extract_single_known_stage_reference(task, source_stage_id=source_stage_id, text=text)


def _apply_plan_review(task: Task, payload: dict[str, Any], *, actor: str, log: EventLog) -> None:
    verdict_raw = payload.get("verdict", "approved" if payload.get("review_scope", "plan") == "plan" else "needs_corrections")
    if verdict_raw == "needs_fixes":
        verdict_raw = "needs_corrections"
    verdict = PlanReviewVerdict(verdict_raw)
    findings = [finding_from_payload(item, created_by=actor) for item in payload.get("findings", [])]
    task.plan_review = PlanReview(
        id=f"plan_review_{task.id}",
        task_id=task.id,
        reviewer_agent_id=actor,
        verdict=verdict,
        findings=findings,
        reviewed_stage_ids=list(payload["reviewed_stage_ids"]),
        proof_requirements_confirmed=payload.get("proof_requirements_confirmed") is True,
        test_plan_confirmed=payload.get("test_plan_confirmed") is True,
    )
    if verdict == PlanReviewVerdict.APPROVED and task.plan_review.test_plan_confirmed:
        _synthesize_missing_reviewed_stages(task, actor=actor, log=log)
    log.append(Event(now(), "plan.reviewed", task.id, None, actor, {"verdict": verdict.value, "findings": len(findings)}))


def _merge_existing_proof_ids(task: Task, proof_ids: Iterable[str], *, proof_store=None) -> list[str]:
    clean = [str(item).strip() for item in proof_ids if str(item).strip()]
    if proof_store is not None:
        missing: list[str] = []
        for proof_id in clean:
            try:
                proof = proof_store.get(proof_id)
            except Exception:
                missing.append(proof_id)
                continue
            if proof.task_id != task.id:
                missing.append(proof_id)
        if missing:
            raise DecisionPayloadInvalid(f"unknown proof_ids: {missing}")
    _dedupe_extend(task.proof_ids, clean)
    return clean


def _apply_implementation_review(task: Task, payload: dict[str, Any], *, actor: str, proof_store=None) -> None:
    verdict = str(payload.get("verdict", "approved")).strip()
    raw_proof_ids = payload.get("proof_ids", [])
    if verdict == "approved":
        _validate_qa_handoff_proof_readiness(task, raw_proof_ids, proof_store=proof_store, action_label="implementation approval")
        validate_product_promotion_gate(task, raw_proof_ids, proof_store=proof_store)
    proof_ids = _merge_existing_proof_ids(task, raw_proof_ids, proof_store=proof_store)
    findings = payload.get("findings", [])
    safe_findings = findings if isinstance(findings, list) else []
    if proof_store is not None:
        qa_proof = record_qa_verdict(task, verdict=verdict, proof_ids=proof_ids, findings=safe_findings, store=proof_store)
        _dedupe_extend(task.proof_ids, [qa_proof.id])
        attach_proofs_to_plan_stage(
            task,
            qa_proof.stage_id or task.current_stage_id,
            [qa_proof.id],
            proof_store=proof_store,
        )
    if verdict == "approved":
        if not _all_stages_dev_complete(task):
            task.state = TaskState.RUNNING
            _advance_to_next_dev_stage(task)
            return
        for stage in task_stage_records(task):
            if stage.status in DEV_COMPLETE_STAGE_STATUSES:
                stage.status = StageStatus.PASSED
                stage.updated_at = now()
        task.state = TaskState.RUNNING
    elif verdict == "needs_fixes":
        task.state = TaskState.RUNNING
    else:
        _soft_block_task(task, reason="QA verdict blocked; Dev recovery required", stage_id=task.current_stage_id)
        if "qa_blocked_verdict_needs_dev_recovery" not in task.risk_flags:
            task.risk_flags.append("qa_blocked_verdict_needs_dev_recovery")
        if task.current_stage_id:
            for stage in task_stage_records(task):
                if stage.id == task.current_stage_id:
                    stage.status = StageStatus.BLOCKED
                    stage.updated_at = now()
                    break


def _all_stages_dev_complete(task: Task) -> bool:
    dev_stages = [stage for stage in list(task_stage_records(task)) if not _is_qa_stage(stage)]
    if not dev_stages:
        return True
    return all(stage.status in DEV_COMPLETE_STAGE_STATUSES for stage in dev_stages)


_DEPLOY_CHECK_MARKERS: dict[str, tuple[str, ...]] = {
    "EterniaBackend": ("manage.py check",),
    "EterniaLauncher": ("flutter analyze", "flutter build", "flutter test"),
    "hermes-agent": ("pytest",),
}


def _validate_commit_deploy_gate(task: Task, decision, *, proof_store=None, stage_id: str | None = None) -> None:
    """Harness-owned delivery gate: product-edit stages cannot hand off to QA
    with uncommitted work or without a passed deploy-check proof.

    QA steering alone cannot enforce this (the model can claim anything); the
    gate rejects the handoff decision itself with exact repair guidance, which
    the repair retry and replay capture machinery then own.
    """
    if proof_store is None:
        return
    plan = getattr(task, "mission_plan", None)
    stage = None
    wanted = str(stage_id or "").strip()
    if plan is not None and wanted:
        stage = next((item for item in plan.stages if item.id == wanted), None)
    if stage is None:
        stage = current_plan_stage(task)
    if stage is None or not getattr(stage, "requires_product_edit", False):
        return
    from .packets import iter_packet_payloads

    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    deliveries = [packet for packet_type, packet in iter_packet_payloads(payload) if packet_type == "delivery"]
    if not deliveries:
        raise DecisionPayloadInvalid(
            "product-edit QA handoff requires a delivery packet with commit_refs and deploy_verification"
        )
    delivery = deliveries[-1]
    commit_refs = [str(item).strip() for item in (delivery.get("commit_refs") or []) if str(item).strip()]
    if not commit_refs:
        raise DecisionPayloadInvalid(
            "delivery.commit_refs is required for a product-edit QA handoff: commit exactly the changed paths "
            "(git add <changed_paths> && git commit -m <slice summary>) on the current branch, then reference the "
            "commit as <repo>@<branch>:<short_sha> in delivery.commit_refs"
        )
    markers = _DEPLOY_CHECK_MARKERS.get(str(getattr(stage, "repo", "") or ""))
    if not markers:
        return
    candidate_ids: list[str] = []
    candidate_ids.extend(str(item).strip() for item in (payload.get("proof_ids") or []) if str(item).strip())
    candidate_ids.extend(str(item).strip() for item in (delivery.get("proof_ids") or []) if str(item).strip())
    deploy = delivery.get("deploy_verification") if isinstance(delivery.get("deploy_verification"), dict) else {}
    if str(deploy.get("proof_id") or "").strip():
        candidate_ids.append(str(deploy["proof_id"]).strip())
    for proof_id in dict.fromkeys(candidate_ids):
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        command = str(metadata.get("command") or "")
        if str(metadata.get("status") or "").strip().lower() == "passed" and any(marker in command for marker in markers):
            return
    raise DecisionPayloadInvalid(
        f"product-edit QA handoff requires a passed deploy-check proof for {getattr(stage, 'repo', 'the stage repo')}: "
        f"request_test_run one of {list(markers)} and reference the passed proof id in delivery.deploy_verification.proof_id"
    )


def _validate_qa_handoff_proof_readiness(
    task: Task,
    proof_ids: Iterable[str],
    *,
    proof_store=None,
    action_label: str = "hand_off",
    stage_id: str | None = None,
) -> None:
    if proof_store is None:
        return
    clean = [str(item).strip() for item in proof_ids if str(item).strip()]
    if not clean:
        return
    proofs = []
    missing: list[str] = []
    for proof_id in clean:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            missing.append(proof_id)
            continue
        if proof.task_id != task.id:
            missing.append(proof_id)
            continue
        proofs.append(proof)
    if missing:
        raise DecisionPayloadInvalid(f"unknown proof_ids: {missing}")

    failed_command_proofs: list[str] = []
    recipe_groups: dict[tuple[str, str], dict[str, Any]] = {}
    for proof in proofs:
        if proof.type != ProofType.TEST_RUN:
            continue
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        status = str(metadata.get("status") or "").strip().lower()
        if status != "passed":
            failed_command_proofs.append(f"{proof.id}:{status or 'missing_status'}")
        recipe_id = _proof_recipe_id(metadata)
        if recipe_id:
            run_id = str(metadata.get("run_id") or "").strip()
            group = recipe_groups.setdefault(
                (recipe_id, run_id),
                {
                    "recipe_id": recipe_id,
                    "run_id": run_id,
                    "commands_requested": 0,
                    "command_indexes": set(),
                },
            )
            group["commands_requested"] = max(group["commands_requested"], _positive_int(metadata.get("commands_requested")))
            command_index = _nonnegative_int(metadata.get("command_index"))
            if command_index is not None:
                group["command_indexes"].add(command_index)
    if failed_command_proofs:
        raise DecisionPayloadInvalid(
            f"{action_label} requires passing command proof_ids; failed or incomplete command proof cannot be handed to QA: {failed_command_proofs[:5]}"
        )

    incomplete_recipes: list[str] = []
    for group in recipe_groups.values():
        requested = int(group.get("commands_requested") or 0)
        if requested <= 1:
            continue
        indexes = group["command_indexes"]
        missing_indexes = [index for index in range(requested) if index not in indexes]
        if missing_indexes:
            run_label = group["run_id"] or "unknown_run"
            incomplete_recipes.append(
                f"{group['recipe_id']}@{run_label}:{len(indexes)}/{requested}_commands_passed_missing_{missing_indexes[:5]}"
            )
    if incomplete_recipes:
        raise DecisionPayloadInvalid(
            f"{action_label} requires a complete passing proof recipe batch; incomplete recipe proof cannot be handed to QA: {incomplete_recipes[:5]}"
        )
    _validate_stage_specific_qa_proof_contract(task, proofs, stage_id=stage_id, action_label=action_label)


def _validate_stage_specific_qa_proof_contract(
    task: Task,
    proofs: list,
    *,
    stage_id: str | None,
    action_label: str,
) -> None:
    effective_stage_id = str(stage_id or task.current_stage_id or "").strip()
    if not effective_stage_id or not _stage_requires_bridge_archive_regression(task, effective_stage_id):
        return
    current_stage_proofs = [
        proof
        for proof in proofs
        if str(getattr(proof, "stage_id", None) or "").strip() == effective_stage_id
        and proof.type == ProofType.TEST_RUN
    ]
    if not current_stage_proofs:
        raise DecisionPayloadInvalid(
            f"{action_label} requires current-stage bridge/snapshot proof_ids for {effective_stage_id}; "
            "stale or previous-stage proofs cannot mark this stage ready_for_qa"
        )
    mismatches: list[str] = []
    for proof in current_stage_proofs:
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        command = str(metadata.get("command") or "").strip()
        normalized = command.lower().replace("\\", "/")
        if (
            "mission_control_bridge_test.dart" in normalized
            and "mission_control_snapshot_test.dart" in normalized
        ):
            continue
        mismatches.append(f"{proof.id}:{command[:180] or 'missing_command'}")
    if mismatches:
        raise DecisionPayloadInvalid(
            f"{action_label} requires Mission Control bridge and snapshot regression proof before QA handoff; "
            f"wrong current-stage proof command(s): {mismatches[:5]}"
        )


def _stage_requires_bridge_archive_regression(task: Task, stage_id: str) -> bool:
    stage = next((item for item in (task_stage_records(task) or []) if item.id == stage_id), None)
    if stage is None:
        return False
    identity_text = " ".join(
        [
            str(stage.id or ""),
            str(stage.title or ""),
            str(stage.objective or ""),
        ]
    ).lower().replace("_", "-")
    return (
        ("mission control" in identity_text or "mission-control" in identity_text)
        and ("bridge" in identity_text or "snapshot" in identity_text or "archive" in identity_text)
        and ("regression" in identity_text or "test" in identity_text or "coverage" in identity_text)
    )


def _proof_recipe_id(metadata: dict[str, Any]) -> str:
    direct = str(metadata.get("proof_recipe_recipe_id") or "").strip()
    if direct:
        return direct
    recipe = metadata.get("proof_recipe")
    if isinstance(recipe, dict):
        return str(recipe.get("recipe_id") or "").strip()
    return ""


def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


def _nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _needs_sequential_specialist_join(task: Task) -> bool:
    flags = {str(flag).strip().lower() for flag in (getattr(task, "risk_flags", []) or [])}
    return any(
        flag in flags
        for flag in {
            "sequential_specialists_required",
            "sequential_specialist_handoff",
            "cross_stack_contract_join",
            "cross_stack_sequential_join_required",
            "backend_contract_first",
            "launcher_visual_proof_required_after_join_gate",
        }
    )


def _needs_cross_stack_launcher_completion(task: Task, *, proof_store=None) -> bool:
    if not _is_cross_stack_backend_first(task):
        return False
    if not _has_backend_contract_proof(task, proof_store=proof_store):
        return True
    launcher_stage = _launcher_stage(task)
    if launcher_stage is None:
        return True
    return launcher_stage.status not in DEV_COMPLETE_STAGE_STATUSES


def _should_release_backend_first_slice(task: Task) -> bool:
    if getattr(task, "proof_ids", None):
        return False
    if task_stage_records(task):
        return False
    text = " ".join(
        [
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            " ".join(str(item) for item in (getattr(task, "risk_flags", []) or [])),
            _routing_scope_text(task),
        ]
    ).lower()
    has_backend_first = any(
        marker in text
        for marker in (
            "backend dev first",
            "backend dev runs first",
            "backend dev before launcher dev",
            "backend dev, then launcher dev",
            "backend dev observational proof first",
            "backend proof first",
            "backend dev proof before",
            "backend dev observational proof",
            "backend proof, gate launcher",
        )
    )
    has_launcher_later = any(
        marker in text
        for marker in (
            "launcher dev only after",
            "launcher dev runs only after",
            "launcher dev proof third",
            "launcher proof second",
            "release launcher dev only if",
            "backend proof before any launcher dev release",
        )
    )
    has_cross_stack_flag = _has_cross_stack_backend_first_flag(task)
    has_cross_stack_text = "cross-stack" in text or "cross stack" in text or ("mission control" in text and "backend" in text and "launcher" in text)
    return (has_backend_first and has_launcher_later and (has_cross_stack_flag or has_cross_stack_text)) or _text_implies_backend_first_cross_stack(task)


def _has_launcher_stage(task: Task) -> bool:
    return _launcher_stage(task) is not None


def _launcher_stage(task: Task) -> TaskStage | None:
    return next((stage for stage in task_stage_records(task) if _stage_mentions_launcher(stage)), None)


def _current_stage(task: Task) -> TaskStage | None:
    current_stage_id = str(getattr(task, "current_stage_id", "") or "").strip()
    if not current_stage_id:
        return None
    return next((stage for stage in task_stage_records(task) if stage.id == current_stage_id), None)


def _is_cross_stack_backend_first(task: Task) -> bool:
    return _has_cross_stack_backend_first_flag(task) or _text_implies_backend_first_cross_stack(task)


def _has_cross_stack_backend_first_flag(task: Task) -> bool:
    flags = {str(flag).strip().lower() for flag in (getattr(task, "risk_flags", []) or [])}
    flags.update(_routing_scope_flags(task))
    return bool(
        flags.intersection(
            {
                *CROSS_STACK_BACKEND_FIRST_FLAGS,
                "cross_stack_contract_ordering",
                "cross_stack_sequential_handoff",
                "sequential_specialist_handoff",
                "sequential_specialists_required",
            }
        )
    )


def _text_implies_backend_first_cross_stack(task: Task) -> bool:
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
            " ".join(str(item) for item in (getattr(task, "non_goals", []) or [])),
            " ".join(str(item) for item in (getattr(task, "risk_flags", []) or [])),
            _routing_scope_text(task),
            " ".join(
                " ".join(
                    [
                        str(getattr(stage, "id", "") or ""),
                        str(getattr(stage, "title", "") or ""),
                        str(getattr(stage, "objective", "") or ""),
                        " ".join(str(item) for item in (getattr(stage, "acceptance_criteria", []) or [])),
                    ]
                )
                for stage in task_stage_records(task)
            ),
        ]
    ).lower()
    if "backend" not in text or not any(marker in text for marker in ("launcher", "frontend", "ui/bridge", "ui bridge")):
        return False
    backend_first = any(
        marker in text
        for marker in (
            "backend dev before launcher dev",
            "backend dev, then launcher dev",
            "backend proof before",
            "backend proof artifacts being joined before launcher",
            "backend proof id before launcher",
            "backend proof ids exist",
            "backend_contract_smoke proof",
            "backend contract smoke proof",
            "backend proof to join",
        )
    )
    launcher_later = any(
        marker in text
        for marker in (
            "launcher dev only after",
            "launcher dev runs only after",
            "release launcher dev",
            "releases launcher dev",
            "before launcher dev ui",
            "before launcher dev starts",
            "launcher dev ui/bridge repair",
            "launcher ui/bridge repair",
        )
    )
    cross_stack = "cross-stack" in text or "cross stack" in text or "mission control" in text or "frontend" in text
    return backend_first and launcher_later and cross_stack


def _has_backend_contract_delivery_packet(task: Task, *, event_log: EventLog | None = None) -> bool:
    log = event_log or EventLog()
    try:
        events = log.for_task(task.id, limit=0)
    except Exception:
        return False
    for event in reversed(events):
        if event.type != "packet.recorded":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        if payload.get("packet_type") != "delivery":
            continue
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        actor = str(payload.get("actor") or event.persona_id or "").strip()
        if actor != "backend_dev" and not _text_mentions_backend(" ".join([actor, str(payload.get("stage_id") or ""), str(body.get("operator_note") or "")])):
            continue
        contract_packet = body.get("contract_packet")
        produced_id = str(body.get("produced_contract_packet_id") or "").strip().lower()
        pending_ids = {"", "pending", "pending_harness_contract_packet_record", "tbd", "todo", "none", "n/a"}
        if isinstance(contract_packet, dict) and contract_packet:
            return True
        if produced_id not in pending_ids:
            return True
    return False


def _has_backend_contract_proof(task: Task, *, proof_store=None) -> bool:
    proof_ids = [str(item).strip() for item in (getattr(task, "proof_ids", []) or []) if str(item).strip()]
    if not proof_ids:
        return False
    if proof_store is None:
        return any(_text_mentions_backend(proof_id) for proof_id in proof_ids) or not _has_launcher_stage(task)
    for proof_id in proof_ids[-20:]:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        if _proof_is_backend_contract_candidate(proof):
            return True
    return False


def _proof_is_backend_contract_candidate(proof) -> bool:
    proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
    if proof_type == "qa_verdict":
        return False
    metadata = proof.metadata or {}
    if str(metadata.get("kind", "")).strip().lower() == "preflight":
        return False
    status = str(metadata.get("status", "")).strip().lower()
    if status and status not in {"passed", "safe", "ok", "success"}:
        return False
    text = " ".join(
        [
            str(getattr(proof, "id", "") or ""),
            str(getattr(proof, "stage_id", "") or ""),
            str(getattr(proof, "title", "") or ""),
            str(getattr(proof, "path_or_value", "") or ""),
            str(getattr(proof, "created_by", "") or ""),
            str(metadata.get("actor_requested", "") or ""),
            str(metadata.get("proof_intent", "") or ""),
            str(metadata.get("workdir_label", "") or ""),
            str(metadata.get("command", "") or ""),
        ]
    ).lower()
    return _text_mentions_backend(text) or str(getattr(proof, "created_by", "") or "") == "backend_dev" or str(metadata.get("actor_requested", "") or "") == "backend_dev"


def _raw_stage_mentions_launcher(raw: dict[str, Any]) -> bool:
    return any(marker in _raw_stage_haystack(raw) for marker in ("launcher", "frontend", "front-end", "ui", "eternialauncher"))


def _text_mentions_backend(text: str) -> bool:
    haystack = str(text or "").lower()
    return any(marker in haystack for marker in ("backend", "eterniabackend", "eternia-backend", "manage.py", "scripts/test.sh"))


def _block_launcher_release_until_backend_proof(task: Task, *, log: EventLog, actor: str, run_id: str | None) -> None:
    _soft_block_task(task, reason="backend proof required before Launcher release", stage_id=task.current_stage_id)
    _dedupe_extend(task.risk_flags, ["cross_stack_backend_proof_missing_before_launcher_release"])
    task.updated_at = now()
    log.append(
        Event(
            now(),
            "cross_stack.backend_proof_missing",
            task.id,
            run_id,
            actor,
            {
                "source": "neko_cross_stack_join_gate",
                "next_expected": "Backend Dev must attach passed backend proof before Neko releases Launcher Dev.",
                "proof_ids": len(getattr(task, "proof_ids", []) or []),
            },
        )
    )


def _payload_targets_launcher(payload: dict[str, Any]) -> bool:
    if _payload_handoff_request_targets_launcher(payload) or _payload_is_launcher_handoff(payload):
        return True
    haystack = " ".join(
        [
            str(payload.get("objective", "")),
            " ".join(str(item) for item in payload.get("acceptance_criteria", []) or []),
            " ".join(str(item) for item in payload.get("affected_repos", []) or []),
            " ".join(str(item) for item in payload.get("suggested_roles", []) or []),
            " ".join(str(item) for item in payload.get("risk_flags", []) or []),
        ]
    ).lower()
    return any(marker in haystack for marker in ("launcher", "frontend", "front-end", "ui", "eternialauncher"))


def _payload_is_launcher_handoff(payload: dict[str, Any]) -> bool:
    handoff = payload.get("handoff_packet")
    if not isinstance(handoff, dict):
        return False
    target_owner = str(handoff.get("target_owner") or handoff.get("target_dev_persona") or "").strip()
    target_repo = str(handoff.get("target_repo") or "").strip()
    phase = str(handoff.get("mission_phase") or "").strip().lower()
    kind = str(handoff.get("packet_kind") or "").strip().lower()
    return (
        target_owner in {"dev", "launcher_dev"}
        and target_repo == "EterniaLauncher"
        and (phase in {"launcher_handoff", "visual_proof_recovery"} or kind in {"contract_join", "recovery", "bounded_visual_proof_recovery"})
    )


def _payload_handoff_request_targets_launcher(payload: dict[str, Any]) -> bool:
    handoff = payload.get("handoff_request")
    if not isinstance(handoff, dict):
        return False
    target_repo = str(handoff.get("target_repo") or "").strip()
    target_owner = str(handoff.get("target_owner") or "").strip()
    return target_repo == "EterniaLauncher" and target_owner in {"", "dev", "launcher_dev"}


def _payload_is_visual_recovery_handoff(payload: dict[str, Any]) -> bool:
    handoff = payload.get("handoff_packet")
    if not isinstance(handoff, dict):
        return False
    kind = str(handoff.get("packet_kind") or "").strip().lower()
    phase = str(handoff.get("mission_phase") or "").strip().lower()
    return kind == "bounded_visual_proof_recovery" or phase == "visual_proof_recovery"


def _payload_is_no_edit_proof_handoff(payload: dict[str, Any]) -> bool:
    handoff = payload.get("handoff_packet")
    if not isinstance(handoff, dict):
        return False
    proof_gate = handoff.get("proof_gate")
    if not isinstance(proof_gate, dict):
        return False
    recipe_id = _no_edit_handoff_recipe_id(payload)
    if not no_product_edit_recipe_id(recipe_id):
        return False
    mode = str(proof_gate.get("mode") or "no_product_edit").strip()
    if mode != "no_product_edit":
        return False
    target_owner = str(handoff.get("target_owner") or handoff.get("target_dev_persona") or "").strip()
    return target_owner in {"dev", "backend_dev", "launcher_dev"}


def _no_edit_handoff_recipe_id(payload: dict[str, Any]) -> str:
    handoff = payload.get("handoff_packet")
    handoff = handoff if isinstance(handoff, dict) else {}
    proof_gate = handoff.get("proof_gate")
    proof_gate = proof_gate if isinstance(proof_gate, dict) else {}
    explicit = str(proof_gate.get("recipe_id") or proof_gate.get("proof_recipe_id") or payload.get("recipe_id") or payload.get("proof_recipe_id") or "").strip()
    if explicit:
        return explicit
    target_repo = str(handoff.get("target_repo") or "").strip()
    text = " ".join(
        [
            str(payload.get("objective") or ""),
            " ".join(str(item) for item in (payload.get("acceptance_criteria") or [])),
            str(handoff),
        ]
    ).lower()
    if target_repo == "hermes-agent" and _is_harness_no_edit_recipe_text(text):
        return "harness_runtime_status_snapshot"
    return ""


def _is_harness_no_edit_recipe_text(text: str) -> bool:
    lowered = str(text or "").lower()
    if "harness" not in lowered:
        return False
    if any(marker in lowered for marker in ("implement", "add ", "update", "patch", "change", "code", "test cover", "focused tests")):
        return False
    return any(marker in lowered for marker in ("status", "snapshot", "log", "logs", "thinking", "observability", "smoke"))


def _ensure_scoped_dev_handoff_stage(task: Task, payload: dict[str, Any], *, actor: str, log: EventLog) -> bool:
    handoff = payload.get("handoff_packet")
    if not isinstance(handoff, dict):
        return False
    current = next((stage for stage in task_stage_records(task) if stage.id == task.current_stage_id), None) if task.current_stage_id else None
    if current is not None and current.status not in DEV_COMPLETE_STAGE_STATUSES:
        return False
    if any(stage.status not in DEV_COMPLETE_STAGE_STATUSES for stage in task_stage_records(task)):
        return False
    target_owner = str(handoff.get("target_owner") or handoff.get("target_dev_persona") or "").strip()
    if target_owner not in {"dev", "backend_dev", "launcher_dev"}:
        return False
    target_repo = _normalized_handoff_repo(handoff, target_owner)
    if target_repo not in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        return False
    stage_id = _scoped_handoff_stage_id(target_repo, handoff, payload)
    existing = next((stage for stage in task_stage_records(task) if stage.id == stage_id), None)
    if existing is not None:
        task.current_stage_id = existing.id
        if existing.status in {StageStatus.DRAFT, StageStatus.READY, StageStatus.BLOCKED}:
            existing.status = StageStatus.IMPLEMENTING
            existing.updated_at = now()
        return True
    proof_gate = handoff.get("proof_gate") if isinstance(handoff.get("proof_gate"), dict) else {}
    criteria = _scoped_handoff_stage_criteria(payload, target_repo)
    stage = TaskStage(
        id=stage_id,
        title=_scoped_handoff_stage_title(target_repo, handoff),
        objective=_scoped_handoff_stage_objective(payload, target_repo),
        status=StageStatus.IMPLEMENTING,
        affected_paths=[target_repo],
        acceptance_criteria=criteria,
        test_plan=[],
        requires_visual_proof=bool(proof_gate.get("visual_required", False)),
        created_at=now(),
        updated_at=now(),
    )
    append_task_stage_record(task, stage)
    task.current_stage_id = stage.id
    task.affected_repos = [target_repo]
    _dedupe_extend(task.risk_flags, ["neko_scoped_dev_handoff_stage"])
    log.append(
        Event(
            now(),
            "task.stage_added",
            task.id,
            None,
            actor,
            {
                "stage_id": stage.id,
                "title": stage.title,
                "source": "neko_scoped_dev_handoff",
                "target_owner": target_owner,
                "target_repo": target_repo,
                "packet_kind": str(handoff.get("packet_kind") or "").strip(),
            },
        )
    )
    return True


def _normalized_handoff_repo(handoff: dict[str, Any], target_owner: str) -> str:
    target_repo = str(handoff.get("target_repo") or "").strip()
    if target_repo in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        return target_repo
    if target_owner == "backend_dev":
        return "EterniaBackend"
    if target_owner in {"dev", "launcher_dev"}:
        return "EterniaLauncher"
    return target_repo


def _scoped_handoff_stage_id(target_repo: str, handoff: dict[str, Any], payload: dict[str, Any]) -> str:
    explicit = str(handoff.get("stage_id") or handoff.get("target_stage_id") or payload.get("stage_id") or "").strip()
    if explicit:
        return _safe_stage_identifier(explicit)
    packet_kind = str(handoff.get("packet_kind") or "handoff").strip() or "handoff"
    return _safe_stage_identifier(f"{target_repo}_{packet_kind}")


def _scoped_handoff_stage_title(target_repo: str, handoff: dict[str, Any]) -> str:
    packet_kind = str(handoff.get("packet_kind") or "handoff").strip().replace("_", " ").title()
    repo_title = {
        "EterniaBackend": "Backend Dev",
        "EterniaLauncher": "Launcher Dev",
        "hermes-agent": "Harness Dev",
    }.get(target_repo, target_repo or "Dev")
    return f"{repo_title} {packet_kind}".strip()


def _scoped_handoff_stage_objective(payload: dict[str, Any], target_repo: str) -> str:
    objective = str(payload.get("objective") or "").strip()
    lowered = objective.lower()
    if target_repo == "EterniaBackend" and any(marker in lowered for marker in ("launcher", "frontend", "qa agent", "mission control ui")):
        return "Complete the Backend Dev handoff with redaction-safe backend ownership evidence or a no-backend-surface proof."
    if target_repo == "EterniaLauncher" and "backend dev" in lowered and "launcher" in lowered:
        return "Complete the Launcher Dev implementation handoff using joined backend evidence and focused UI proof."
    return objective or "Complete the scoped specialist handoff."


def _scoped_handoff_stage_criteria(payload: dict[str, Any], target_repo: str) -> list[str]:
    raw = [str(item).strip() for item in (payload.get("acceptance_criteria") or []) if str(item).strip()]
    if not raw:
        return [
            "Complete the scoped specialist handoff with redaction-safe evidence.",
            "Attach focused proof or a precise blocker before releasing the next owner.",
        ]
    if target_repo == "EterniaBackend":
        selected = [
            item
            for item in raw
            if _criterion_mentions_any(item, ("backend", "eterniabackend", "no-backend", "no backend", "contract", "handoff"))
            and not _criterion_mentions_any(item, ("launcher dev consumes", "launcher ui", "launcher implementation", "qa verifies", "qa agent"))
        ]
        return selected or raw[:2]
    if target_repo == "EterniaLauncher":
        selected = [
            item
            for item in raw
            if _criterion_mentions_any(item, ("launcher", "frontend", "ui", "mission control", "visual"))
            and not _criterion_mentions_any(item, ("backend dev uses", "backend dev attaches", "qa verifies"))
        ]
        return selected or raw[:3]
    return raw


def _criterion_mentions_any(value: str, markers: tuple[str, ...]) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in markers)


def _safe_stage_identifier(value: str) -> str:
    safe = "".join(ch.lower() if ch.isalnum() else "_" for ch in str(value or "").strip())
    while "__" in safe:
        safe = safe.replace("__", "_")
    return safe.strip("_")[:80] or "scoped_dev_handoff"


def _summary_is_missing_launcher_proof(decision: AgentDecision) -> bool:
    text = " ".join(
        [
            str(decision.summary or ""),
            str(decision.rationale or ""),
            " ".join(str(value) for value in (decision.payload or {}).values() if isinstance(value, str)),
        ]
    ).lower()
    return "launcher" in text and "proof" in text and any(marker in text for marker in ("missing", "cannot proceed", "required"))


def _context_request_summary(req: dict[str, Any]) -> str:
    status = str(req.get("status") or "unknown")
    reason = str(req.get("failure_reason") or "").strip()
    if status == "fulfilled":
        return "Context request fulfilled; read the attached context bundle before asking again."
    if status == "fulfilled_partial":
        return "Context request partially fulfilled; read returned files and inspect per-path failures before asking again."
    if status == "superseded":
        return "Duplicate context request ignored; do not repeat the same paths."
    if status == "unsupported":
        return f"Context request unsupported: {reason or 'unknown'}."
    return "Context request recorded."


def _context_request_next_expected(req: dict[str, Any]) -> str:
    status = str(req.get("status") or "unknown")
    if status == "fulfilled":
        return "use_context_then_deliver_or_request_one_narrower_context"
    if status == "fulfilled_partial":
        return "use_partial_context_then_request_one_missing_path_or_block"
    if status == "unsupported":
        return "request_one_narrower_repo_relative_path_or_block_with_exact_feedback"
    if status == "superseded":
        return "choose_a_different_visible_hud_action_or_block_with_evidence"
    return "wait_for_context_feedback_or_block_if_stale"


def _stage_mentions_launcher(stage: TaskStage) -> bool:
    # A backend-first stage can mention Launcher/frontend in downstream join-gate
    # criteria. Only stage identity/scope/proof requirements should classify the
    # stage itself as the Launcher side of the handoff.
    identity = " ".join([str(stage.id), str(stage.title)]).lower()
    if any(marker in identity for marker in ("backend", "eterniabackend", "eternia-backend")) and not any(
        marker in identity for marker in ("launcher", "frontend", "front-end", "ui", "eternialauncher")
    ):
        return False
    haystack = " ".join(
        [
            str(stage.id),
            str(stage.title),
            " ".join(str(item) for item in (stage.affected_paths or [])),
            "visual" if getattr(stage, "requires_visual_proof", False) else "",
        ]
    ).lower()
    return any(marker in haystack for marker in ("launcher", "frontend", "front-end", "ui"))


def _all_stages_passed(task: Task) -> bool:
    if not task_stage_records(task):
        return bool(task.proof_ids)
    return all(stage.status in TERMINAL_STAGE_STATUSES for stage in task_stage_records(task))


def _advance_to_next_dev_stage(task: Task) -> None:
    for stage in task_stage_records(task):
        if _is_qa_stage(stage):
            continue
        if stage.status not in DEV_COMPLETE_STAGE_STATUSES:
            task.current_stage_id = stage.id
            if stage.status in {StageStatus.DRAFT, StageStatus.READY}:
                stage.status = StageStatus.IMPLEMENTING
                stage.updated_at = now()
            return


def _is_qa_stage(stage: TaskStage) -> bool:
    text = " ".join(
        str(item or "").lower()
        for item in [
            getattr(stage, "id", ""),
            getattr(stage, "title", ""),
            getattr(stage, "objective", ""),
        ]
    )
    return "qa_release" in text or "qa release" in text or "qa verdict" in text


def _synthesize_missing_reviewed_stages(task: Task, *, actor: str, log: EventLog) -> None:
    """Materialize QA-approved stage handles when PM/Dev emitted only names.

    This is a bounded reconciliation seam for recovery goals: QA may approve a
    named test plan before concrete TaskStage records exist. Rather than looping
    back forever, create minimal reviewed stages so the normal proof gate can
    advance, while keeping acceptance/test evidence explicit and auditable.
    """
    review = task.plan_review
    if review is None:
        return
    existing = {stage.id for stage in task_stage_records(task)}
    for stage_id in review.reviewed_stage_ids:
        sid = str(stage_id).strip()
        if not sid or sid in existing:
            continue
        title = sid.replace("_", " ").strip().title()
        criteria = list(task.acceptance_criteria or [f"Complete reviewed stage {sid}"])
        stage = TaskStage(
            id=sid,
            title=title,
            objective=f"Complete QA-reviewed stage {sid} for {task.title}.",
            status=StageStatus.READY,
            affected_paths=[],
            acceptance_criteria=criteria,
            test_plan=[
                f"Verify acceptance criteria for reviewed stage {sid}.",
                "Reuse existing safe proof IDs when applicable; report remaining AAA/general gaps at final handoff.",
            ],
            created_at=now(),
            updated_at=now(),
        )
        append_task_stage_record(task, stage)
        existing.add(sid)
        if task.current_stage_id is None:
            task.current_stage_id = sid
        log.append(Event(now(), "task.stage_added", task.id, None, actor, {"stage_id": sid, "title": title, "source": "qa_review_reconciliation"}))
