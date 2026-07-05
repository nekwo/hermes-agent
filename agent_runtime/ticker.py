from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import inspect
import json
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
import time
import traceback
from typing import Any

from hermes_time import now

from .actions import HarnessAction, HarnessActionResult, HarnessActionType
from .autonomy import record_autonomy_packet
from .blueprints.routing import apply_decision_outcome, apply_stage_outcome, is_blueprint_plan, stage_declares_required_gate
from .blueprints.schema import StageOutcome
from .budget_approval import budget_incident_needs_scope_recovery, eligible_budget_approval_incidents
from .child_events import emit_child_deploy_failed
from .config import ensure_persisted_personas, get_persisted_persona, load_agent_runtime_config
from .locks import HarnessLockUnavailable, tick_lock
from .liveness import LivenessProbe
from .events import EventLog
from .final_gate import (
    build_final_gate_decision,
    default_blueprint_placeholder_repo_override,
    filter_forbidden_gate_commands,
    goal_demands_exact_proof,
    goal_named_gate_commands,
    packet_forbidden_gate_commands,
    packet_named_gate_commands,
    stage_repo_for_gate,
)
from .models import Event, Incident, MissionIntent, MissionPlan, MissionPlanStage, Task
from .mission_plan import attach_proofs_to_plan_stage, current_plan_stage, _sync_task_stage_compat_from_plan
from .persona_assignments import (
    PersonaAssignmentSpec,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_assignment_store_enabled,
    persona_instance_id_for_placement,
)
from .planning import _advance_to_next_dev_stage, _all_stages_dev_complete, _has_backend_contract_delivery_packet, _needs_cross_stack_launcher_completion, _needs_sequential_specialist_join
from .incidents import MODEL_INVALID_OUTPUT, RUN_BUDGET_EXCEEDED, classify_exception
from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from .decision_contracts import validate_planning_decision
from .dev_discipline import validate_dev_progress_gate
from .proof_runner import CommandProofRunner
from .proof_command_policy import validate_request_test_run_policy
from .proof_recipes import normalize_request_test_run_decision, proof_recipe_metadata
from .preflight import open_preflight_blocker, record_preflight_pass, run_preflight
from .progress import RunProgressSink
from .proof_batches import ProofBatchStore
from .role_checklists import apply_decision_checklist_updates, sanitize_decision_checklist_payload, validate_decision_checklist_payload
from .role_envelopes import RoleEnvelopeStore
from .visual_proof import VisualProofRunner
from .repo_bundles import RepoBundleStore, find_best_bundle_for_action, qa_waiting_on
from .repo_context import RepoExecutionContext, command_workdir_for_task, diff_weakens_tests, git_diff_since_baseline, isolated_repo_context_for_run, safe_affected_repo_labels
from .packets import latest_packet, make_packet, record_packet
from .role_sessions import (
    RoleSessionEnvelope,
    observe_enabled_config,
    role_session_payload,
    role_session_progress,
    should_continue_role_session,
    update_envelope_after_invocation,
)

from .recovery import mark_stale_runs
from .runtime_config import RuntimeConfig
from .simplified_contract import project_decision_for_execution, simplified_contract_enabled
from .recovery_flags import mark_block_recovery_attempt
from .state_machine import MissionStateMachine
from .stage_intent import (
    first_incomplete_product_edit_stage,
    no_product_edit_recipe_id,
    stage_requires_product_edit,
)
from .states import RunState, StageStatus, TaskState
from .store import AgentStore, IncidentStore, ProofStore, RunStore, TaskStore, _safe_session_id
from .worker_sessions import WorkerSessionStore


@dataclass(slots=True)
class TickResult:
    tick_id: str
    started_at: object
    finished_at: object | None = None
    tasks_seen: int = 0
    actions_taken: list[HarnessActionResult] = field(default_factory=list)
    incidents_opened: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RunUntilSettledResult:
    settle_id: str
    started_at: object
    finished_at: object | None = None
    task_id: str | None = None
    ticks: int = 0
    actions_taken: list[HarnessActionResult] = field(default_factory=list)
    stop_reason: str = "unknown"
    final_task_state: str | None = None
    open_incidents: int = 0
    max_actions: int = 0
    max_seconds: float | None = None


class TickEngine:
    def __init__(self, *, task_store=None, run_store=None, incident_store=None, agent_store=None, proof_store=None, persona_runtime=None, config: RuntimeConfig | None = None, state_machine: MissionStateMachine | None = None, proof_runner=None, visual_proof_runner=None, command_workdir=None, worker_session_store=None):
        self.task_store = task_store or TaskStore()
        self.run_store = run_store or RunStore()
        self.incident_store = incident_store or IncidentStore()
        self.agent_store = agent_store or AgentStore()
        self.proof_store = proof_store or ProofStore()
        self.persona_runtime = persona_runtime
        self.config = config or RuntimeConfig()
        self.state_machine = state_machine or MissionStateMachine(proof_store=self.proof_store, config=self.config)
        self.proof_runner = proof_runner
        self.visual_proof_runner = visual_proof_runner
        self.command_workdir = command_workdir
        self.worker_session_store = worker_session_store or WorkerSessionStore()
        self.role_envelope_store = RoleEnvelopeStore()
        self.proof_batch_store = ProofBatchStore()
        self.persona_assignment_store = PersonaAssignmentStore()
        self.repo_bundle_store = RepoBundleStore()
        self.liveness_probe = LivenessProbe(
            config=self.config,
            run_store=self.run_store,
            incident_store=self.incident_store,
            task_store=self.task_store,
            event_log=EventLog(),
            worker_session_store=self.worker_session_store,
        )

    def tick_once(self, *, task_id: str | None = None) -> TickResult:
        with tick_lock():
            result = TickResult(tick_id=f"tick_{uuid.uuid4().hex[:8]}", started_at=now())
            self._active_tick_id = result.tick_id
            try:
                ensure_persisted_personas(self.config)
            except Exception:
                pass
            self._poll_liveness(result)
            result.incidents_opened.extend(mark_stale_runs(self.run_store, self.incident_store, heartbeat_ttl_seconds=self.config.heartbeat_ttl_seconds))
            closed_model_invalid_tasks = self._close_model_invalid_output_incidents(task_id=task_id)
            if task_id:
                task = self.task_store.get(task_id)
                tasks = [] if task.state in {TaskState.DONE, TaskState.CANCELLED} else [task]
            else:
                tasks = self.task_store.list_open()
            open_incidents_by_task: dict[str, list[Incident]] = {}
            for incident in self.incident_store.list_open():
                if incident.task_id:
                    open_incidents_by_task.setdefault(incident.task_id, []).append(incident)
            result.tasks_seen = len(tasks)
            for task in tasks:
                loaded_task = copy.deepcopy(task)
                if task.id in closed_model_invalid_tasks:
                    result.skipped.append(task.id)
                    continue
                task_incidents = open_incidents_by_task.get(task.id, [])
                if _hard_environment_blocker_incidents(task_incidents):
                    result.skipped.append(task.id)
                    continue
                budget_approval_incidents = _budget_approval_incidents_for_task(
                    task_incidents,
                    self.run_store,
                    cap=getattr(self.config, "neko_extension_cap", 2),
                )
                if budget_approval_incidents:
                    actions = [
                        HarnessAction(
                            HarnessActionType.RUN_SLOT,
                            task.id,
                            reason="needs Neko approval to continue budget-limited Dev run",
                            slot_id="neko_supervisor",
                        )
                    ]
                else:
                    actions = self.state_machine.next_actions(task) if _swarm_lane_concurrency_enabled(self.config) else [self.state_machine.next_action(task)]
                self.task_store.update(task, actor="harness", reason="sync mission plan routing state")
                task = self.task_store.get(task.id)
                if task.id in open_incidents_by_task and not (
                    any(_action_targets(action, "neko_supervisor") for action in actions)
                    and (task.state == TaskState.BLOCKED or budget_approval_incidents or getattr(task, "open_incident_ids", None))
                ):
                    result.skipped.append(task.id)
                    continue
                eligible: list[tuple[HarnessAction, Task]] = []
                for action in actions:
                    if len(result.actions_taken) + len(eligible) >= self.config.max_actions_per_tick:
                        break
                    action_task = _task_for_action(task, action)
                    if action.type == HarnessActionType.NOOP:
                        continue
                    persona_id = _persona_id_for_harness_action(action, task=action_task, config=self.config, run_store=self.run_store)
                    if persona_id:
                        if getattr(action, "stage_id", None):
                            active_runs = self.run_store.find_active(task_id=action_task.id, persona_id=persona_id, stage_id=action.stage_id)
                        else:
                            active_runs = self.run_store.find_active(task_id=action_task.id, persona_id=persona_id)
                        if active_runs:
                            continue
                    if persona_id:
                        budget_block = _runtime_budget_block(
                            action_task,
                            persona_id=persona_id,
                            run_store=self.run_store,
                            config=self.config,
                        )
                        if budget_block is not None:
                            incident = self._open_runtime_budget_incident(action_task, budget_block)
                            result.incidents_opened.append(incident.id)
                            result.actions_taken.append(
                                HarnessActionResult(
                                    action,
                                    False,
                                    budget_block["summary"],
                                    {
                                        "incident_id": incident.id,
                                        "budget_kind": budget_block["kind"],
                                        "total_tokens": budget_block["total_tokens"],
                                        "limit": budget_block["limit"],
                                    },
                                )
                            )
                            continue
                    if action.type == HarnessActionType.COMPLETE_TASK:
                        action_task.state = TaskState.DONE
                        action_task.updated_at = now()
                        self.task_store.update(action_task, actor="harness", reason=action.reason)
                        closed_worker_ids = self._close_terminal_task_workers(action_task.id, reason="task completed")
                        closed_assignment_ids = self.persona_assignment_store.close_for_task(action_task.id, state="completed", reason="task completed")
                        payload = {"state": TaskState.DONE.value, "proof_ids": len(action_task.proof_ids)}
                        if closed_worker_ids:
                            payload["closed_worker_session_ids"] = closed_worker_ids
                        if closed_assignment_ids:
                            payload["closed_persona_assignment_ids"] = closed_assignment_ids
                        result.actions_taken.append(HarnessActionResult(action, True, action.reason, payload))
                        continue
                    if (
                        _action_targets(action, "neko_supervisor")
                        and (
                            bool(getattr(action_task, "open_incident_ids", None))
                            or action_task.state == TaskState.BLOCKED
                            or action.reason == "needs Neko Mission Lead to resolve post-scoping context request"
                        )
                    ):
                        # Open-incident adjudication dispatches are ALSO one
                        # bounded pass per evidence signal: observed live, a
                        # supervisor that answers adjudication with `block`
                        # was re-dispatched every ~30-60s forever (the signal
                        # fingerprint re-arms on incident close / new proof /
                        # new packet, so real progress always re-enables Neko).
                        mark_block_recovery_attempt(action_task)
                        action_task.updated_at = now()
                        self.task_store.update(action_task, actor="harness", reason="route task to one bounded Neko recovery pass")
                    eligible.append((action, action_task))
                if not eligible:
                    result.skipped.append(task.id)
                    continue
                action_results = self._execute_eligible_actions(eligible, loaded_task=loaded_task)
                for action_result in action_results:
                    if action_result.ok:
                        _commit_child_event_offset(action_result.action)
                result.actions_taken.extend(action_results)
            result.finished_at = now()
            self._apply_read_model_pending()
            return result

    def run_until_settled(self, *, task_id: str | None = None, max_actions: int = 10, max_seconds: float | None = None) -> RunUntilSettledResult:
        """Run bounded ticks until a mission reaches a meaningful boundary."""
        started_monotonic = time.monotonic()
        max_actions = max(1, int(max_actions or 1))
        result = RunUntilSettledResult(
            settle_id=f"settle_{uuid.uuid4().hex[:8]}",
            started_at=now(),
            task_id=task_id,
            max_actions=max_actions,
            max_seconds=max_seconds,
        )

        self.liveness_probe.poll_once()
        mark_stale_runs(self.run_store, self.incident_store, heartbeat_ttl_seconds=self.config.heartbeat_ttl_seconds)
        boundary = self._settled_boundary(task_id=task_id)
        if boundary is not None:
            result.stop_reason = boundary
        while result.stop_reason == "unknown" and len(result.actions_taken) < max_actions:
            if max_seconds is not None and time.monotonic() - started_monotonic >= max_seconds:
                result.stop_reason = "max_seconds"
                break

            remaining_actions = max_actions - len(result.actions_taken)
            original_tick_limit = getattr(self.config, "max_actions_per_tick", 1)
            self.config.max_actions_per_tick = max(1, min(int(original_tick_limit or 1), remaining_actions))
            try:
                tick = self.tick_once(task_id=task_id)
            except HarnessLockUnavailable:
                result.stop_reason = "tick_lock_unavailable"
                break
            finally:
                self.config.max_actions_per_tick = original_tick_limit
            result.ticks += 1
            result.actions_taken.extend(tick.actions_taken)
            self.liveness_probe.poll_once()

            if not tick.actions_taken:
                result.stop_reason = "no_eligible_action"
                break
            if any(not action.ok for action in tick.actions_taken):
                if task_id and self._has_continuable_budget_incident(task_id):
                    continue
                result.stop_reason = "action_failed"
                break

            boundary = self._settled_boundary(task_id=task_id)
            if boundary is not None:
                result.stop_reason = boundary
                break

        if result.stop_reason == "unknown":
            result.stop_reason = "max_actions" if len(result.actions_taken) >= max_actions else "no_eligible_action"
        result.open_incidents = self._open_incident_count(task_id=task_id)
        result.final_task_state = self._task_state_value(task_id) if task_id else None
        result.finished_at = now()
        self._apply_read_model_pending()
        return result

    def _poll_liveness(self, result: TickResult | None = None) -> None:
        before = {incident.id for incident in self.incident_store.list_open()}
        self.liveness_probe.poll_once()
        if result is not None:
            after = {incident.id for incident in self.incident_store.list_open()}
            result.incidents_opened.extend(sorted(after - before))

    def _apply_read_model_pending(self) -> None:
        read_model_cfg = getattr(self.config, "read_model", None)
        if not bool(getattr(read_model_cfg, "enabled", False)):
            return
        try:
            from .projector import Projector
            from .read_model import ReadModel

            Projector(ReadModel(), config=self.config).apply_pending()
        except Exception:
            return

    def _execute_eligible_actions(self, actions: list[tuple[HarnessAction, Task]], *, loaded_task: Task) -> list[HarnessActionResult]:
        if len(actions) <= 1 or not _swarm_lane_concurrency_enabled(self.config):
            return [self._execute_action(action, task, loaded_task=copy.deepcopy(loaded_task)) for action, task in actions]
        max_workers = min(len(actions), _max_active_lanes(self.config))
        results: list[HarnessActionResult] = []
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="harness-lane") as executor:
            futures = [
                executor.submit(self._execute_action, action, task, loaded_task=copy.deepcopy(loaded_task))
                for action, task in actions
            ]
            for future in as_completed(futures):
                results.append(future.result())
        return results

    def _settled_boundary(self, *, task_id: str | None) -> str | None:
        open_incidents = self._open_incident_count(task_id=task_id)
        if open_incidents > 0:
            # An untargeted daemon (task_id=None) must apply the same
            # Neko-owns-recovery carve-out per incident-owning task; hard-stopping
            # on ANY open incident leaves every goal idle with no adjudication
            # dispatch (observed live: settle_stop_reason=incident_opened,
            # actions=0 across daemon loops while a running task waited on Neko).
            candidate_task_ids = (
                [task_id]
                if task_id
                else sorted({str(incident.task_id) for incident in self.incident_store.list_open() if incident.task_id})
            )
            for candidate_task_id in candidate_task_ids:
                if self._incident_recovery_can_proceed(candidate_task_id):
                    return None
            return "incident_opened"
        active_runs = self.run_store.find_active(task_id=task_id)
        if any(run.state == RunState.WAITING_ON_APPROVAL for run in active_runs):
            return "waiting_on_approval"
        if any(run.state in {RunState.QUEUED, RunState.STARTING, RunState.RUNNING, RunState.WAITING_ON_TOOL} for run in active_runs):
            return "active_run"
        if task_id:
            task = self.task_store.get(task_id)
            state = task.state
            if state in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
                return "task_terminal"
            if state == TaskState.BLOCKED:
                action = self.state_machine.next_action(task)
                if action.type != HarnessActionType.NOOP:
                    return None
                return "task_blocked"
        return None

    def _incident_recovery_can_proceed(self, task_id: str) -> bool:
        """True when this incident-owning task's next action is Neko recovery.

        Mirrors the long-standing targeted-daemon carve-out: hard environment
        blockers stop the tick; budget approval / scope-recovery incidents and a
        state-machine route to neko_supervisor let it proceed so the incident is
        adjudicated instead of idling.
        """

        try:
            task = self.task_store.get(task_id)
            incidents = [incident for incident in self.incident_store.list_open() if incident.task_id == task_id]
            if not incidents:
                return False
            if _hard_environment_blocker_incidents(incidents):
                return False
            budget_approval_incidents = _budget_approval_incidents_for_task(
                incidents,
                self.run_store,
                cap=getattr(self.config, "neko_extension_cap", 2),
            )
            budget_scope_recovery_incidents = _budget_scope_recovery_incidents_for_task(incidents, self.run_store)
            if budget_approval_incidents or budget_scope_recovery_incidents:
                return True
            action = self.state_machine.next_action(task)
            return _action_targets(action, "neko_supervisor") and (
                task.state == TaskState.BLOCKED or bool(getattr(task, "open_incident_ids", None))
            )
        except Exception:
            return False

    def _open_incident_count(self, *, task_id: str | None) -> int:
        incidents = self.incident_store.list_open()
        if task_id is None:
            return len(incidents)
        return sum(1 for incident in incidents if incident.task_id == task_id)

    def _close_model_invalid_output_incidents(self, *, task_id: str | None = None) -> set[str]:
        closed_task_ids: set[str] = set()
        for incident in list(self.incident_store.list_open()):
            if incident.kind != MODEL_INVALID_OUTPUT:
                continue
            if task_id is not None and incident.task_id != task_id:
                continue
            self.incident_store.close(incident.id, reason="contract_simplification_normalized_model_invalid_output")
            if incident.task_id:
                closed_task_ids.add(incident.task_id)
        return closed_task_ids

    def _apply_no_progress_guard(self, task: Task, run, persona_id: str, decision_type: str, envelope) -> None:
        threshold = max(1, int(getattr(getattr(self.config, "role_envelope", None), "max_no_progress_repeats", 1) or 1))
        if getattr(envelope, "no_progress_count", 0) < threshold:
            return
        stage_id = getattr(envelope, "mission_stage_id", None) or getattr(task, "current_stage_id", None)
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=getattr(run, "id", None),
                persona_id=persona_id,
                payload={
                    "phase": "self_heal",
                    "step": "no_progress_guard",
                    "status": "blocked" if persona_id == "neko_supervisor" else "escalated",
                    "severity": "warning",
                    "stage_id": stage_id,
                    "decision_type": decision_type,
                    "no_progress_count": getattr(envelope, "no_progress_count", 0),
                    "summary": "Repeated same-stage decision without new progress; Harness guard prevented another blind retry.",
                    "next_expected": "neko_recovery" if persona_id != "neko_supervisor" else "operator_or_new_evidence",
                },
            )
        )
        self.role_envelope_store.close(task.id, envelope.envelope_id, reason="no_progress_guard", run_id=getattr(run, "id", None))
        # The per-run role envelope resets its no_progress_count every dispatch, so
        # the soft guard above can fire repeatedly while the daemon keeps
        # re-dispatching the SAME stage with the SAME decision (a Neko re-judge/route
        # loop). Persist a cumulative (stage, decision) counter across runs and hard-
        # block once it exceeds a small cap so the loop stops in a few turns, not
        # dozens — instead of an incident that Neko adjudication can just reopen.
        heal = task.harness_self_heal if isinstance(task.harness_self_heal, dict) else {}
        loops = heal.setdefault("no_progress_loops", {})
        loop_key = f"{stage_id}:{decision_type}"
        loops[loop_key] = int(loops.get(loop_key, 0) or 0) + 1
        task.harness_self_heal = heal
        hard_cap = max(3, threshold * 3)
        if persona_id == "neko_supervisor":
            incident = Incident(
                id=f"inc_{uuid.uuid4().hex[:12]}",
                task_id=task.id,
                run_id=getattr(run, "id", None),
                kind="no_progress_loop",
                summary="Neko repeated the same recovery/scoping decision without new progress.",
                detail_path=None,
                opened_at=now(),
                metadata={"stage_id": stage_id, "decision_type": decision_type, "no_progress_count": getattr(envelope, "no_progress_count", 0), "cumulative_no_progress": loops[loop_key]},
            )
            self.incident_store.open(incident)
            task.open_incident_ids = _dedupe(list(task.open_incident_ids or []), [incident.id])
            if loops[loop_key] >= hard_cap:
                # Genuine loop: same stage, same decision, N times over. Block hard —
                # a re-dispatch will not produce new evidence. Requires operator
                # unblock or new evidence to resume (see `task unblock`).
                task.state = TaskState.BLOCKED
                task.risk_flags = _dedupe(list(task.risk_flags or []), ["no_progress_hard_block"])
            else:
                task.state = TaskState.RUNNING
        else:
            task.state = TaskState.RUNNING
            task.risk_flags = _dedupe(list(task.risk_flags or []), ["no_progress_escalated_to_neko"])

    def _has_continuable_budget_incident(self, task_id: str) -> bool:
        incidents = [incident for incident in self.incident_store.list_open() if incident.task_id == task_id]
        return bool(
            _budget_approval_incidents_for_task(
                incidents,
                self.run_store,
                cap=getattr(self.config, "neko_extension_cap", 2),
            )
        )

    def _open_runtime_budget_incident(self, task: Task, budget_block: dict[str, Any]) -> Incident:
        event_type = str(budget_block["event_type"])
        payload = {
            "task_id": task.id,
            "persona_id": budget_block.get("persona_id"),
            "stage_id": budget_block.get("stage_id") or "",
            "total_tokens": budget_block["total_tokens"],
            "limit": budget_block["limit"],
        }
        EventLog().append(
            Event(
                ts=now(),
                type=event_type,
                task_id=task.id,
                run_id=None,
                persona_id=budget_block.get("persona_id"),
                payload=payload,
            )
        )
        incident = Incident(
            id=f"inc_{uuid.uuid4().hex[:8]}",
            task_id=task.id,
            run_id=None,
            kind=event_type,
            summary=budget_block["summary"],
            detail_path=None,
            opened_at=now(),
            metadata={
                "budget_state": {
                    "kind": budget_block["kind"],
                    "event_type": event_type,
                    "total_tokens": budget_block["total_tokens"],
                    "limit": budget_block["limit"],
                    "persona_target": budget_block.get("persona_id") or "",
                    "stage_id": budget_block.get("stage_id") or "",
                },
                "stage_id": budget_block.get("stage_id") or "",
                "persona_target": budget_block.get("persona_id") or "",
            },
        )
        self.incident_store.open(incident)
        task.open_incident_ids = _dedupe(list(task.open_incident_ids or []), [incident.id])
        if task.state not in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
            task.state = TaskState.BLOCKED
        task.updated_at = now()
        self.task_store.update(task, actor="harness", reason=event_type)
        return incident

    def _record_preflight_started(self, task: Task, persona_id: str) -> None:
        try:
            self.task_store.event_log.append(
                Event(
                    ts=now(),
                    type="task.preflight",
                    task_id=task.id,
                    run_id=None,
                    persona_id=persona_id,
                    payload={
                        "status": "started",
                        "stage_id": task.current_stage_id,
                        "persona_target": persona_id,
                    },
                )
            )
        except Exception:
            return None

    def _task_state_value(self, task_id: str | None) -> str | None:
        if not task_id:
            return None
        try:
            state = self.task_store.get(task_id).state
        except Exception:
            return None
        return state.value if hasattr(state, "value") else str(state)

    def _close_terminal_task_workers(self, task_id: str, *, reason: str) -> list[str]:
        closed: list[str] = []
        try:
            for worker in self.worker_session_store.find_active(task_id=task_id):
                closed.append(self.worker_session_store.close(worker.id, reason=reason).id)
            if _role_envelope_enabled(self.config):
                self.role_envelope_store.close_for_task(task_id, reason=reason)
        except Exception:
            return closed
        return closed

    def _execute_action(self, action: HarnessAction, task: Task, *, loaded_task: Task | None = None) -> HarnessActionResult:
        if self.persona_runtime is None:
            return HarnessActionResult(action, False, "no persona runtime configured")
        if _action_targets(action, "dev", "backend_dev"):
            handoff_recovery = _recover_handoff_repair_with_existing_proof(task, proof_store=self.proof_store)
            if handoff_recovery:
                self.task_store.update(task, actor="harness", reason="deterministic handoff repair proof reuse")
                EventLog().append(
                    Event(
                        ts=now(),
                        type="task.transition",
                        task_id=task.id,
                        run_id=None,
                        persona_id="harness",
                        payload={
                            "source": "deterministic_handoff_repair_recovery",
                            "phase": "handoff",
                            "step": "existing_proof_reused",
                            "status": "ready_for_qa",
                            "stage_id": task.current_stage_id,
                            "proof_ids": handoff_recovery["proof_ids"],
                            "summary": "Handoff repair reused existing passed command proof and routed directly to QA.",
                            "next_expected": "qa_verification",
                            "to": task.state.value,
                        },
                    )
                )
                return HarnessActionResult(action, True, "handoff repair reused existing passed proof; routed to QA", handoff_recovery)
        persona_id = _persona_id_for_harness_action(action, task=task, config=self.config, run_store=self.run_store)
        persona = _get_persona(self.agent_store, persona_id, self.config)
        child_instance = None
        if action.type == HarnessActionType.RUN_SLOT:
            child_instance = PersonaInstanceStore().ensure_for_goal(
                persona,
                goal_id=getattr(task, "goal_id", None) or task.id,
                spawned_by=_spawned_by_for_harness_action(action, task=task),
                placement_id=f"{getattr(task, 'goal_id', None) or task.id}:{persona.id}",
            )
        worker_store = self.worker_session_store if _enterprise_worker_sessions_enabled(self.config) else None
        assignment = None
        if persona_assignment_store_enabled(self.config):
            assignment = _assignment_from_task_flag(self.persona_assignment_store, task, persona.id)
            repo_bundle = None
            if assignment is None:
                deploy_warning = _first_deploy_contention_warning(
                    self.persona_assignment_store,
                    persona_id=persona.id,
                    goal_id=getattr(task, "goal_id", None) or task.id,
                    enabled=_deploy_verification_enabled(self.config),
                )
                if deploy_warning is not None:
                    return _child_deploy_failed_result(
                        action,
                        task=task,
                        persona_id=persona.id,
                        child_instance_id=getattr(child_instance, "id", None),
                        reason=str(deploy_warning.get("message") or deploy_warning.get("code") or "agent deployment failed"),
                        assignment_id=str(deploy_warning.get("assignment_id") or "") or None,
                        retryable=bool(deploy_warning.get("retryable", False)),
                        event_log=EventLog(),
                    )
                repo_bundle = _repo_bundle_for_action(self.config, self.repo_bundle_store, action, task, persona_id=persona.id)
                assignment = self.persona_assignment_store.create_or_resume(
                    _assignment_spec_for_action(
                        action,
                        task,
                        persona_id=persona.id,
                        repo_bundle_id=repo_bundle.id if repo_bundle is not None else None,
                    )
                )
                if repo_bundle is not None:
                    self.repo_bundle_store.attach_assignment(repo_bundle, assignment.id)
            else:
                repo_bundle = self.repo_bundle_store.find_for_assignment(assignment)
            if _action_targets(action, "dev", "backend_dev") and repo_bundle is not None and repo_bundle.state == "queued_waiting_dependency":
                # Re-check the dependency at the scheduling gate. Bundles
                # re-projected after their dependency already delivered are born
                # queued, and the decision-time wake never fires again for them
                # (observed live: task_burn_2f4e59be launcher bundle livelocked
                # behind an already-delivered backend bundle for 48 actions).
                self.repo_bundle_store.wake_ready_dependencies(task.id)
                repo_bundle = self.repo_bundle_store.get(task.id, repo_bundle.id)
            if _action_targets(action, "dev", "backend_dev") and repo_bundle is not None and repo_bundle.state == "queued_waiting_dependency":
                return HarnessActionResult(
                    action,
                    True,
                    "repo bundle queued waiting for dependency; no Dev run launched",
                    {
                        "assignment_id": assignment.id,
                        "repo_bundle_id": repo_bundle.id,
                        "queue_reason": repo_bundle.queue_reason,
                        "wake_condition": repo_bundle.wake_condition,
                    },
                )
            if _action_targets(action, "qa"):
                waiting_on = qa_waiting_on(self.repo_bundle_store.list_for_task(task.id))
                if waiting_on:
                    return HarnessActionResult(
                        action,
                        True,
                        "QA queued waiting for repo bundles; no QA run launched",
                        {
                            "assignment_id": assignment.id if assignment is not None else None,
                            "qa_waiting_on": waiting_on,
                        },
                    )
        preflight = None
        if _action_targets(action, "dev", "backend_dev", "qa") and _is_live_persona_runtime(self.persona_runtime):
            self._record_preflight_started(task, persona.id)
            preflight = run_preflight(task, stage=_current_stage(task), persona_target=persona.id)
        if preflight is not None and not preflight.ok:
            incident = open_preflight_blocker(
                task,
                preflight,
                persona_target=persona.id,
                proof_store=self.proof_store,
                incident_store=self.incident_store,
                task_store=self.task_store,
                stage_id=task.current_stage_id,
            )
            payload = {"incident_id": incident.id} if incident else {}
            if assignment is not None:
                self.persona_assignment_store.complete(assignment.id, state="blocked", error="preflight environment blocker")
                payload["assignment_id"] = assignment.id
            if preflight.blocker:
                payload["check_id"] = preflight.blocker.get("check_id")
                payload["environment_fingerprint"] = preflight.environment_fingerprint
            return HarnessActionResult(action, False, "preflight environment blocker", payload)
        if preflight is not None and preflight.ok:
            record_preflight_pass(
                task,
                preflight,
                persona_target=persona.id,
                task_store=self.task_store,
                stage_id=task.current_stage_id,
            )
        max_attempts = max(1, min(int(getattr(self.config, "daemon_max_retries_per_state", 1) or 1), 3))
        last_run_id = None
        for attempt in range(1, max_attempts + 1):
            worker = None
            continuation_session_id = self.run_store.latest_session_id(task_id=task.id, persona_id=persona.id, stage_id=task.current_stage_id)
            if worker_store is not None:
                worker_session_id = worker_store.reusable_session_id(task_id=task.id, persona_id=persona.id)
                continuation_session_id = worker_session_id or continuation_session_id
                worker = worker_store.open_or_resume(
                    task_id=task.id,
                    persona=persona,
                    stage_id=task.current_stage_id,
                    session_id=continuation_session_id,
                    goal_epoch=task.id,
                    assignment_id=assignment.id if assignment is not None else None,
                )
                continuation_session_id = _safe_session_id(worker.session_id) or continuation_session_id
            repair_error = _latest_model_invalid_repair_error(
                self.run_store,
                task_id=task.id,
                persona_id=persona.id,
                stage_id=task.current_stage_id,
            )
            max_total_tokens = _persona_value(persona, "max_total_tokens", getattr(self.config, "live_run_max_total_tokens", None))
            continuation_approvals = _approved_continuation_count(task, self.run_store, persona.id)
            max_total_tokens = _continuation_token_budget(max_total_tokens, continuation_approvals)
            run = self.run_store.open_run(
                persona.id,
                task.id,
                task.current_stage_id,
                iteration_budget=_persona_int(persona, "iteration_budget", max(1, int(getattr(self.config, "live_run_iteration_budget", 60) or 60))),
                max_wall_seconds=_persona_value(persona, "max_wall_seconds", getattr(self.config, "live_run_max_wall_seconds", None)),
                max_api_calls=_persona_value(persona, "max_api_calls", getattr(self.config, "live_run_max_api_calls", None)),
                max_total_tokens=max_total_tokens,
                session_id=continuation_session_id,
                tick_id=getattr(self, "_active_tick_id", None),
            )
            if assignment is not None:
                run.progress = {
                    **(run.progress or {}),
                    "assignment_id": assignment.id,
                    "persona_instance_id": assignment.persona_instance_id,
                    "assignment_kind": assignment.kind,
                    "repo_bundle_id": assignment.repo_bundle_id,
                }
                self.run_store.update(run)
                assignment = self.persona_assignment_store.attach_run(assignment.id, run.id)
                _mark_repo_bundle_running_for_assignment(self.config, self.repo_bundle_store, assignment, run_id=run.id)
            prior_progress_flags = _prior_stage_run_progress_flags(task, self.run_store, persona.id, exclude_run_id=run.id)
            if prior_progress_flags:
                run.progress = {**(run.progress or {}), **prior_progress_flags}
                self.run_store.update(run)
            if worker_store is not None and worker is not None:
                worker_store.assign_run(worker.id, run)
            deploy_failure = _verify_child_deploy_started(
                config=self.config,
                run=run,
                worker=worker_store.get(worker.id) if worker_store is not None and worker is not None else worker,
                assignment=assignment,
                child_instance_id=getattr(child_instance, "id", None),
            )
            if deploy_failure is not None:
                if assignment is not None:
                    self.persona_assignment_store.complete(assignment.id, state="blocked", error=deploy_failure)
                if worker_store is not None and worker is not None:
                    worker_store.update_after_run(worker.id, run, close_reason="deploy_failed", count_decision=False)
                    worker_store.close(worker.id, reason="deploy_failed")
                self.run_store.cancel(run.id, reason=deploy_failure)
                return _child_deploy_failed_result(
                    action,
                    task=task,
                    persona_id=persona.id,
                    child_instance_id=getattr(child_instance, "id", None),
                    reason=deploy_failure,
                    assignment_id=getattr(assignment, "id", None),
                    run_id=run.id,
                    retryable=True,
                    event_log=EventLog(),
                )
            stage52_envelope = None
            if _role_envelope_enabled(self.config):
                stage52_envelope = self.role_envelope_store.open_or_resume(
                    task=task,
                    role_id=persona.id,
                    mission_stage_id=task.current_stage_id,
                    worker_session_id=worker.id if worker is not None else None,
                    run_id=run.id,
                    legacy_projection=False,
                )
            last_run_id = run.id
            envelope = None
            try:
                from .context_builder import build_context
                role_config = getattr(self.config, "continuous_role_sessions", None)
                role_metrics_enabled = bool(role_config and (role_config.enabled or role_config.observe_only))
                envelope = (
                    RoleSessionEnvelope(
                        task_id=task.id,
                        persona_id=persona.id,
                        stage_id=task.current_stage_id,
                        opened_run_id=run.id,
                        session_id=run.session_id,
                    )
                    if role_metrics_enabled
                    else None
                )
                next_action_before = action.type.value
                if envelope is not None:
                    EventLog().append(
                        Event(
                            now(),
                            "role_session.opened",
                            task.id,
                            run.id,
                            persona.id,
                            role_session_payload(envelope, run=run, next_action_before=next_action_before),
                        )
                    )
                run.llm = _initial_run_llm_metadata(
                    persona,
                    self.config,
                    retry_attempt=attempt,
                    retry_max_attempts=max_attempts,
                )
                self.run_store.update(run)
                decision_repair_attempts = 0
                while True:
                    pre_context_task = copy.deepcopy(loaded_task or task)
                    context_started = time.perf_counter()
                    _emit_timing_started(self.run_store, run.id, "context_build")
                    ctx = build_context(
                        task,
                        run,
                        proof_store=self.proof_store,
                        incident_store=self.incident_store,
                        requires_repair=bool(repair_error),
                        repair_error=repair_error,
                        config=self.config,
                    )
                    _record_timing_span(self.run_store, run.id, "context_build", context_started)
                    autonomy_started = time.perf_counter()
                    _emit_timing_started(self.run_store, run.id, "autonomy_packet")
                    autonomy_packet = record_autonomy_packet(persona, ctx, event_log=EventLog(), run_store=self.run_store)
                    _record_timing_span(self.run_store, run.id, "autonomy_packet", autonomy_started)
                    if worker_store is not None and worker is not None:
                        worker_store.record_context(worker.id, context_receipt_id=autonomy_packet.get("context_receipt_id"))
                    if assignment is not None:
                        self.persona_assignment_store.record_context(assignment.id, autonomy_packet.get("context_receipt_id"))
                    try:
                        runtime_started = time.perf_counter()
                        decision = self.persona_runtime.run_tick(
                            persona,
                            ctx,
                            run=run,
                        )
                        _record_timing_span(self.run_store, run.id, "persona_runtime", runtime_started)
                    except DecisionPayloadInvalid as exc:
                        _record_timing_span(self.run_store, run.id, "persona_runtime", runtime_started, status="invalid")
                        if _should_retry_invalid_decision(exc, repair_attempts=decision_repair_attempts):
                            decision_repair_attempts += 1
                            run, repair_error = _record_decision_repair_request(
                                self.run_store,
                                task,
                                run,
                                persona_id=persona.id,
                                exc=exc,
                                decision=None,
                                repair_attempt=decision_repair_attempts,
                                worker_store=worker_store,
                                worker=worker,
                            )
                            task = self.task_store.get(task.id)
                            continue
                        raise
                    repair_error = None
                    try:
                        decision_apply_started = time.perf_counter()
                        proof_workdir_task = copy.deepcopy(pre_context_task)
                        current_run = self.run_store.get(run.id)
                        _attach_stage_self_heal_to_run_progress(current_run, task)
                        validate_dev_progress_gate(persona, current_run, decision)
                        if _record_failed_proof_auto_attachment(current_run, task, decision, actor=persona.id):
                            self.run_store.update(current_run)
                        persona_role = persona.role.value if hasattr(persona.role, "value") else str(persona.role)
                        if persona_role == "dev":
                            _autocorrect_downstream_visual_block_to_current_stage_proof(task, decision)
                        validate_planning_decision(decision)
                        projection = project_decision_for_execution(
                            task,
                            decision,
                            config=self.config,
                            actor=persona.id,
                            run_id=run.id,
                            event_log=EventLog(),
                        )
                        if projection.blocked_reason:
                            raise DecisionPayloadInvalid(projection.blocked_reason)
                        public_decision = decision
                        decision = projection.execution_decision
                        if decision is not public_decision:
                            validate_planning_decision(decision)
                        sanitized_payload, ignored_checklist_updates = sanitize_decision_checklist_payload(
                            task,
                            role_id=persona.id,
                            mission_stage_id=task.current_stage_id,
                            payload=decision.payload,
                            config=self.config,
                        )
                        if ignored_checklist_updates:
                            decision.payload = sanitized_payload
                            EventLog().append(
                                Event(
                                    ts=now(),
                                    type="run.progress",
                                    task_id=task.id,
                                    run_id=run.id,
                                    persona_id=persona.id,
                                    payload={
                                        "type": "run.progress",
                                        "phase": "checklist_payload",
                                        "step": "unauthorized_checklist_update_ignored",
                                        "status": "ignored",
                                        "severity": "info",
                                        "ignored_count": len(ignored_checklist_updates),
                                        "ignored_updates": ignored_checklist_updates[:8],
                                        "summary": "Ignored checklist status update outside this role's authority; decision validation continued.",
                                        "next_expected": "Use one of the HUD checklist choices owned by this role or omit checklist_updates.",
                                    },
                                )
                            )
                        validate_decision_checklist_payload(task, role_id=persona.id, mission_stage_id=task.current_stage_id, payload=decision.payload, config=self.config)
                        if decision.type == DecisionType.REQUEST_TEST_RUN:
                            normalize_request_test_run_decision(task, decision)
                            _validate_request_test_run_targets_current_stage(task, decision)
                        validate_request_test_run_policy(task, decision)
                        current_task = self.task_store.get(task.id)
                        if decision.type in {DecisionType.REQUEST_SCREENSHOT, DecisionType.REQUEST_VIDEO}:
                            _validate_visual_request_not_redundant(current_task, decision, proof_store=self.proof_store)
                        if current_run.state in {RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED}:
                            if worker_store is not None and worker is not None:
                                worker_store.update_after_run(worker.id, current_run, close_reason=f"run_{current_run.state.value}", count_decision=False)
                            _close_role_session(envelope, run=current_run, close_reason=f"run_{current_run.state.value}", next_action_after=next_action_before)
                            return HarnessActionResult(action, False, "run reached terminal state before decision application", {"run_id": run.id, "state": current_run.state.value})
                        if current_task.state in {TaskState.DONE, TaskState.CANCELLED}:
                            self.run_store.cancel(run.id, reason="task reached terminal state before decision application")
                            cancelled_run = self.run_store.get(run.id)
                            if worker_store is not None and worker is not None:
                                worker_store.update_after_run(worker.id, cancelled_run, close_reason=f"task_{current_task.state.value}", count_decision=False)
                            _close_role_session(envelope, run=cancelled_run, close_reason=f"task_{current_task.state.value}", next_action_after=next_action_before)
                            return HarnessActionResult(action, False, "task reached terminal state before decision application", {"run_id": run.id, "task_state": current_task.state.value})
                        task = current_task
                        before_task = copy.deepcopy(task)
                        _validate_observed_trace_requirement(
                            task,
                            decision,
                            run=self.run_store.get(run.id),
                            proof_store=self.proof_store,
                        )
                        if _backend_first_burn_in_orchestration_plan(task, decision, persona_id=persona.id):
                            raise DecisionPayloadInvalid(
                                "Dev stage plan loop guard failed: the plan contained only Neko/Launcher/QA orchestration stages; Dev must provide an executable proof stage or request_test_run."
                            )
                        normal_flow_enabled = bool(getattr(getattr(self.config, "normal_worker_flow", None), "enabled", False)) or simplified_contract_enabled(self.config)
                        self.state_machine.apply_decision(
                            task,
                            decision,
                            actor=persona.id,
                            task_store=self.task_store,
                            incident_store=self.incident_store,
                            proof_store=self.proof_store,
                            run_id=run.id,
                            stage_id=str(decision.payload.get("stage_id") or run.stage_id or "").strip() or None,
                            normal_worker_flow=normal_flow_enabled,
                            mission_plan_flow=None,
                        )
                        _record_handoff_observation(
                            task,
                            decision,
                            run=self.run_store.get(run.id),
                            actor=persona.id,
                            proof_store=self.proof_store,
                            command_workdir=self.command_workdir,
                            task_store=self.task_store,
                        )
                        _record_failed_proof_block_after_reuse(task, decision, actor=persona.id, run_id=run.id)
                        if decision.type == DecisionType.REQUEST_TEST_RUN:
                            _ensure_legacy_command_proof_plan(task, decision, before_task=before_task)
                        _record_timing_span(self.run_store, run.id, "decision_apply", decision_apply_started)
                    except DecisionPayloadInvalid as exc:
                        _record_timing_span(self.run_store, run.id, "decision_apply", decision_apply_started, status="invalid")
                        if _should_retry_invalid_decision(exc, repair_attempts=decision_repair_attempts):
                            decision_repair_attempts += 1
                            run, repair_error = _record_decision_repair_request(
                                self.run_store,
                                task,
                                run,
                                persona_id=persona.id,
                                exc=exc,
                                decision=decision,
                                repair_attempt=decision_repair_attempts,
                                worker_store=worker_store,
                                worker=worker,
                            )
                            task = self.task_store.get(task.id)
                            continue
                        raise
                    proof_ids: list[str] = []
                    proof_statuses: list[str] = []
                    proof_batch_id = None
                    handoff_applied = False
                    if decision.type == DecisionType.REQUEST_TEST_RUN:
                        if worker_store is not None and worker is not None:
                            worker_store.update_after_run(worker.id, self.run_store.get(run.id), close_reason="waiting_on_proof", count_decision=False)
                        proof_ids = self._collect_command_proof(task, decision, actor=persona.id, run_id=run.id, workdir_task=proof_workdir_task)
                        proof_statuses = _proof_statuses(proof_ids, proof_store=self.proof_store)
                        proof_batch_id = _record_stage52_proof_batch(self.proof_batch_store, task, decision, proof_ids, proof_statuses, role_envelope_id=stage52_envelope.envelope_id if stage52_envelope is not None else None, run_id=run.id, persona_id=persona.id)
                        current_run = self.run_store.get(run.id)
                        current_task = self.task_store.get(task.id)
                        if current_run.state in {RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED}:
                            if worker_store is not None and worker is not None:
                                worker_store.update_after_run(worker.id, current_run, proof_ids_added=proof_ids, close_reason=f"run_{current_run.state.value}", count_decision=False)
                            if assignment is not None:
                                self.persona_assignment_store.complete(assignment.id, state="blocked", error=f"run_{current_run.state.value}")
                            _close_role_session(envelope, run=current_run, close_reason=f"run_{current_run.state.value}", next_action_after=next_action_before)
                            return HarnessActionResult(action, False, "run reached terminal state during proof collection", {"run_id": run.id, "state": current_run.state.value})
                        if current_task.state in {TaskState.DONE, TaskState.CANCELLED}:
                            self.run_store.cancel(run.id, reason="task reached terminal state during proof collection")
                            cancelled_run = self.run_store.get(run.id)
                            if worker_store is not None and worker is not None:
                                worker_store.update_after_run(worker.id, cancelled_run, proof_ids_added=proof_ids, close_reason=f"task_{current_task.state.value}", count_decision=False)
                            if assignment is not None:
                                self.persona_assignment_store.complete(assignment.id, state="blocked", error=f"task_{current_task.state.value}")
                            _close_role_session(envelope, run=cancelled_run, close_reason=f"task_{current_task.state.value}", next_action_after=next_action_before)
                            return HarnessActionResult(action, False, "task reached terminal state during proof collection", {"run_id": run.id, "task_state": current_task.state.value})
                        task.proof_ids = _dedupe(list(task.proof_ids or []), proof_ids)
                        attach_proofs_to_plan_stage(
                            task,
                            str(decision.payload.get("stage_id") or task.current_stage_id or "").strip() or None,
                            proof_ids,
                            proof_store=self.proof_store,
                        )
                        proof_records = []
                        for proof_id in proof_ids:
                            try:
                                proof_records.append(self.proof_store.get(proof_id))
                            except Exception:
                                continue
                        decision_stage_id = str(decision.payload.get("stage_id") or task.current_stage_id or "").strip()
                        blueprint_command_mismatch = False
                        if is_blueprint_plan(getattr(task, "mission_plan", None)):
                            acceptable_statuses = {"passed"}
                            if _is_red_stage(task, decision_stage_id):
                                acceptable_statuses.add("failed")
                            proof_status_values = {str((proof.metadata or {}).get("status", "")).strip() for proof in proof_records}
                            if proof_status_values and proof_status_values <= acceptable_statuses:
                                blueprint_command_mismatch = bool(
                                    _proof_repo_mismatch_labels(task, proof_records, actor=persona.id, stage_id=decision_stage_id)
                                    or _proof_command_stage_mismatch_labels(task, proof_records, stage_id=decision_stage_id)
                                )
                            if blueprint_command_mismatch:
                                _apply_deterministic_proof_handoff(
                                    task,
                                    proof_ids,
                                    decision,
                                    proof_store=self.proof_store,
                                    actor=persona.id,
                                    run_id=run.id,
                                )
                        if not blueprint_command_mismatch:
                            apply_decision_outcome(
                                task,
                                decision,
                                stage_id=decision_stage_id or None,
                                proofs=proof_records,
                                reason=decision.summary,
                            )
                            task.risk_flags = [
                                flag
                                for flag in (getattr(task, "risk_flags", None) or [])
                                if flag not in {"command_proof_stage_mismatch", "command_proof_repo_mismatch"}
                            ]
                        _record_command_proof_self_heal(
                            task,
                            proof_ids,
                            proof_store=self.proof_store,
                            stage_id=str(decision.payload.get("stage_id") or task.current_stage_id or "").strip() or None,
                            actor=persona.id,
                            run_id=run.id,
                        )
                        if not is_blueprint_plan(getattr(task, "mission_plan", None)):
                            handoff_applied = _apply_deterministic_proof_handoff(
                                task,
                                proof_ids,
                                decision,
                                proof_store=self.proof_store,
                                actor=persona.id,
                                run_id=run.id,
                            )
                        if handoff_applied:
                            task.proof_ids = _dedupe(task.proof_ids, proof_ids)
                        task.updated_at = now()
                        if assignment is not None:
                            for proof_id in proof_ids:
                                self.persona_assignment_store.attach_proof(assignment.id, proof_id)
                    elif _should_auto_run_final_gate(self.config, decision):
                        source_stage_id = str(before_task.current_stage_id or run.stage_id or "").strip()
                        source_stage = _stage_for_gate(before_task, source_stage_id)
                        handoff_packet = latest_packet(before_task.id, "handoff_packet", stage_id=source_stage_id) or latest_packet(before_task.id, "handoff_packet")
                        final_gate_decision = _build_authoritative_stage_gate_decision(
                            before_task,
                            source_stage,
                            delivery_packet=latest_packet(before_task.id, "delivery", stage_id=source_stage_id),
                            handoff_packet=handoff_packet,
                        )
                        if final_gate_decision is not None and final_gate_decision.type == DecisionType.REQUEST_TEST_RUN:
                            normalize_request_test_run_decision(task, final_gate_decision)
                        if final_gate_decision is not None:
                            if worker_store is not None and worker is not None:
                                worker_store.update_after_run(worker.id, self.run_store.get(run.id), close_reason="auto_final_gate_after_delivery", count_decision=False)
                            reused_existing_gate = False
                            proof_ids = _existing_passed_final_gate_proof_ids(task, source_stage_id, proof_store=self.proof_store)
                            if proof_ids:
                                reused_existing_gate = True
                            else:
                                proof_ids = self._collect_command_proof(task, final_gate_decision, actor=persona.id, run_id=run.id, workdir_task=before_task)
                            proof_statuses = _proof_statuses(proof_ids, proof_store=self.proof_store)
                            test_tampering = _handoff_diff_weakens_tests(task, source_stage_id or None)
                            if test_tampering:
                                proof_statuses = ["failed"]
                            proof_batch_id = _record_stage52_proof_batch(self.proof_batch_store, task, final_gate_decision, proof_ids, proof_statuses, role_envelope_id=stage52_envelope.envelope_id if stage52_envelope is not None else None, run_id=run.id, persona_id=persona.id)
                            _record_authoritative_gate_observation(
                                task,
                                stage_id=source_stage_id or None,
                                proof_ids=proof_ids,
                                proof_statuses=proof_statuses,
                                run_id=run.id,
                                actor=persona.id,
                                task_store=self.task_store,
                            )
                            task.proof_ids = _dedupe(list(task.proof_ids or []), proof_ids)
                            attach_proofs_to_plan_stage(task, source_stage_id or None, proof_ids, proof_store=self.proof_store)
                            proof_records = []
                            for proof_id in proof_ids:
                                try:
                                    proof_records.append(self.proof_store.get(proof_id))
                                except Exception:
                                    continue
                            if test_tampering:
                                task.risk_flags = _dedupe(list(task.risk_flags or []), ["test_tampering_detected"])
                                EventLog().append(
                                    Event(
                                        ts=now(),
                                        type="proof.gate_blocked",
                                        task_id=task.id,
                                        run_id=run.id,
                                        persona_id=persona.id,
                                        payload={
                                            "status": "failed",
                                            "gate_source": "auto_after_delivery",
                                            "stage_id": source_stage_id,
                                            "proof_ids": proof_ids,
                                            "summary": "Authoritative gate failed closed because the handoff diff weakens test assertions or skips.",
                                        },
                                    )
                                )
                            else:
                                apply_decision_outcome(
                                    task,
                                    final_gate_decision,
                                    stage_id=source_stage_id or None,
                                    proofs=proof_records,
                                    reason=final_gate_decision.summary,
                                )
                            _record_command_proof_self_heal(
                                task,
                                proof_ids,
                                proof_store=self.proof_store,
                                stage_id=source_stage_id or None,
                                actor=persona.id,
                                run_id=run.id,
                            )
                            if not is_blueprint_plan(getattr(task, "mission_plan", None)):
                                handoff_applied = _apply_deterministic_proof_handoff(
                                    task,
                                    proof_ids,
                                    final_gate_decision,
                                    proof_store=self.proof_store,
                                    actor=persona.id,
                                    run_id=run.id,
                                )
                            EventLog().append(
                                Event(
                                    ts=now(),
                                    type="proof.gate_checked",
                                    task_id=task.id,
                                    run_id=run.id,
                                    persona_id=persona.id,
                                    payload={
                                        "status": "passed" if proof_statuses and all(status == "passed" for status in proof_statuses) else "failed",
                                        "gate_source": "auto_after_delivery",
                                        "reused_existing_proof": reused_existing_gate,
                                        "stage_id": source_stage_id,
                                        "proof_ids": proof_ids,
                                        "summary": "Automatic final gate reused an existing passed proof after Dev delivery." if reused_existing_gate else "Automatic final gate ran after Dev delivery.",
                                    },
                                )
                            )
                            if handoff_applied:
                                task.proof_ids = _dedupe(task.proof_ids, proof_ids)
                            task.updated_at = now()
                            if assignment is not None:
                                for proof_id in proof_ids:
                                    self.persona_assignment_store.attach_proof(assignment.id, proof_id)
                        elif source_stage is not None and not stage_declares_required_gate(source_stage):
                            # The stage's own owner delivered (hand_off) and the
                            # blueprint declares NO required proof gate for this
                            # stage: the accepted delivery IS the completion
                            # signal. Without this, no path ever marks the stage
                            # passed and the owner is re-dispatched forever
                            # (observed live 2026-07-03: task_49f8ee3b looped
                            # backend_implementation 8x on 'no safe automatic
                            # final gate command was available').
                            apply_stage_outcome(
                                task,
                                source_stage_id,
                                StageOutcome.PASSED,
                                reason="delivery accepted; stage declares no required proof gate and no gate command was derivable",
                            )
                            task.updated_at = now()
                            EventLog().append(
                                Event(
                                    ts=now(),
                                    type="run.progress",
                                    task_id=task.id,
                                    run_id=run.id,
                                    persona_id=persona.id,
                                    payload={
                                        "type": "run.progress",
                                        "phase": "proof",
                                        "step": "auto_final_gate_not_required",
                                        "status": "passed",
                                        "stage_id": source_stage_id,
                                        "summary": "Delivery accepted; the stage declares no required proof gate, so the hand_off completes the stage.",
                                        "next_expected": "next_blueprint_stage",
                                    },
                                )
                            )
                        else:
                            EventLog().append(
                                Event(
                                    ts=now(),
                                    type="run.progress",
                                    task_id=task.id,
                                    run_id=run.id,
                                    persona_id=persona.id,
                                    payload={
                                        "type": "run.progress",
                                        "phase": "proof",
                                        "step": "auto_final_gate_missing",
                                        "status": "missing",
                                        "stage_id": source_stage_id,
                                        "summary": "Normal worker flow accepted delivery but no safe automatic final gate command was available.",
                                        "next_expected": "neko_or_qa_missing_gate_repair",
                                    },
                                )
                            )
                    elif decision.type in {DecisionType.REQUEST_SCREENSHOT, DecisionType.REQUEST_VIDEO}:
                        if worker_store is not None and worker is not None:
                            worker_store.update_after_run(worker.id, self.run_store.get(run.id), close_reason="waiting_on_visual_proof", count_decision=False)
                        proof_ids = self._collect_visual_proof(task, decision, actor=persona.id, run_id=run.id)
                        proof_statuses = _proof_statuses(proof_ids, proof_store=self.proof_store)
                        proof_batch_id = _record_stage52_proof_batch(self.proof_batch_store, task, decision, proof_ids, proof_statuses, role_envelope_id=stage52_envelope.envelope_id if stage52_envelope is not None else None, run_id=run.id, persona_id=persona.id)
                        task.proof_ids = _dedupe(list(task.proof_ids or []), proof_ids)
                        attach_proofs_to_plan_stage(
                            task,
                            str(decision.payload.get("stage_id") or task.current_stage_id or "").strip() or None,
                            proof_ids,
                            proof_store=self.proof_store,
                        )
                        task.updated_at = now()
                        if assignment is not None:
                            for proof_id in proof_ids:
                                self.persona_assignment_store.attach_proof(assignment.id, proof_id)
                    checklist_stage_id = str(run.stage_id or before_task.current_stage_id or task.current_stage_id or "").strip() or None
                    checklist = apply_decision_checklist_updates(task, role_id=persona.id, mission_stage_id=checklist_stage_id, payload=decision.payload, run_id=run.id, config=self.config)
                    checklist_revision = getattr(checklist, "revision", None) if checklist is not None else None
                    if stage52_envelope is not None:
                        stage52_envelope = self.role_envelope_store.record_progress(
                            stage52_envelope,
                            run_id=run.id,
                            decision_type=decision.type.value,
                            proof_ids=proof_ids,
                            checklist_revision=checklist_revision,
                            payload=decision.payload if isinstance(decision.payload, dict) else None,
                            status="continuing",
                            continuation_reason="progress recorded" if proof_ids or checklist_revision is not None else "decision recorded",
                            proof_batch_id=proof_batch_id,
                        )
                        self._apply_no_progress_guard(task, run, persona.id, decision.type.value, stage52_envelope)
                    self.task_store.update(task, actor=persona.id, reason=decision.summary)
                    run = _refresh_run_for_update(self.run_store, run)
                    public_type = getattr(getattr(projection, "public_type", None), "value", None)
                    execution_type = getattr(getattr(projection, "execution_type", None), "value", None)
                    run.llm = {
                        **(run.llm or {}),
                        "decision_type": decision.type.value,
                        "public_decision_type": public_type or decision.type.value,
                        "execution_decision_type": execution_type or decision.type.value,
                        "decision_contract_mode": getattr(projection, "mode", "legacy"),
                        "validation_status": "valid",
                        "retry_attempt": attempt,
                        "retry_max_attempts": max_attempts,
                    }
                    budget_warning = _emit_run_budget_warning(run, task_id=task.id, actor=persona.id)
                    if envelope is not None:
                        update_envelope_after_invocation(envelope, run, proof_ids_added=proof_ids)
                    if worker_store is not None and worker is not None:
                        worker_store.update_after_run(worker.id, run, proof_ids_added=proof_ids)
                    refreshed_task = self.task_store.get(task.id)
                    next_action = self.state_machine.next_action(refreshed_task)
                    next_persona_id = _persona_id_for_harness_action(next_action, task=refreshed_task, config=self.config, run_store=self.run_store)
                    open_incidents = [incident for incident in self.incident_store.list_open() if incident.task_id == task.id]
                    if envelope is not None:
                        eval_config = observe_enabled_config(role_config) if role_config.observe_only else role_config
                        if budget_warning:
                            run.progress = {**(run.progress or {}), "phase": "runaway_warning", "severity": "warning"}
                        policy = should_continue_role_session(
                            config=eval_config,
                            before_task=before_task,
                            after_task=refreshed_task,
                            persona_id=persona.id,
                            run=run,
                            decision_type=decision.type.value,
                            envelope=envelope,
                            next_action_type=next_action.type.value,
                            next_persona_id=next_persona_id,
                            open_incident_count=len(open_incidents),
                            proof_ids_added=proof_ids,
                            proof_statuses=proof_statuses,
                            deterministic_handoff_applied=handoff_applied,
                            is_live_runtime=_is_live_persona_runtime(self.persona_runtime),
                        )
                        actual_continue = policy.should_continue and role_config.enabled and not role_config.observe_only
                        close_reason = policy.close_reason if not policy.should_continue else "observe_only"
                        run.progress = {**(run.progress or {}), "role_session": role_session_progress(envelope, close_reason=None if actual_continue else close_reason)}
                        self.run_store.update(run)
                        if actual_continue:
                            envelope.continuation_count += 1
                            run.progress = {**(run.progress or {}), "role_session": role_session_progress(envelope)}
                            self.run_store.update(run)
                            EventLog().append(
                                Event(
                                    now(),
                                    "role_session.continued",
                                    task.id,
                                    run.id,
                                    persona.id,
                                    role_session_payload(
                                        envelope,
                                        run=run,
                                        next_action_before=next_action_before,
                                        next_action_after=next_action.type.value,
                                        proof_ids_added=proof_ids,
                                        incident_ids_opened=[incident.id for incident in open_incidents],
                                        would_continue=True,
                                    ),
                                )
                            )
                            task = refreshed_task
                            next_action_before = next_action.type.value
                            continue
                        _close_role_session(
                            envelope,
                            run=run,
                            close_reason=close_reason,
                            next_action_before=next_action_before,
                            next_action_after=next_action.type.value,
                            proof_ids_added=proof_ids,
                            incident_ids_opened=[incident.id for incident in open_incidents],
                            would_continue=policy.should_continue,
                        )
                    else:
                        self.run_store.update(run)
                    if assignment is not None:
                        run = self.run_store.get(run.id)
                        run.progress = {
                            **(run.progress or {}),
                            "assignment_id": assignment.id,
                            "persona_instance_id": assignment.persona_instance_id,
                            "assignment_kind": assignment.kind,
                            "repo_bundle_id": assignment.repo_bundle_id,
                        }
                        self.run_store.update(run)
                    _emit_decision_process_summary(self.run_store, run.id, decision)
                    if assignment is not None:
                        run = self.run_store.get(run.id)
                        run.progress = {
                            **(run.progress or {}),
                            "assignment_id": assignment.id,
                            "persona_instance_id": assignment.persona_instance_id,
                            "assignment_kind": assignment.kind,
                            "repo_bundle_id": assignment.repo_bundle_id,
                        }
                        self.run_store.update(run)
                    final_decision = {
                        "type": getattr(getattr(projection, "public_type", None), "value", None) or decision.type.value,
                        "summary": public_decision.summary,
                        "rationale": public_decision.rationale,
                    }
                    execution_type = getattr(getattr(projection, "execution_type", None), "value", None)
                    if execution_type and execution_type != final_decision["type"]:
                        final_decision["execution_type"] = execution_type
                        final_decision["execution_summary"] = decision.summary
                    self.run_store.close_run(run.id, state=RunState.COMPLETED, final_decision=final_decision)
                    if worker_store is not None and worker is not None:
                        closed_run = self.run_store.get(run.id)
                        worker_store.update_after_run(worker.id, closed_run, close_reason="tick_completed", count_decision=False)
                    if assignment is not None:
                        _mark_repo_bundle_after_decision(self.config, self.repo_bundle_store, assignment, decision, proof_ids=proof_ids, proof_statuses=proof_statuses)
                    if assignment is not None:
                        self.persona_assignment_store.complete(assignment.id, state="completed")
                    payload = {"run_id": run.id, "decision": decision.type.value}
                    if assignment is not None:
                        payload["assignment_id"] = assignment.id
                    if worker_store is not None and worker is not None:
                        payload["worker_session_id"] = worker.id
                    if envelope is not None:
                        payload["role_session"] = {
                            "envelope_id": envelope.envelope_id,
                            "decision_count": envelope.decision_count,
                            "continuation_count": envelope.continuation_count,
                        }
                    if attempt > 1:
                        payload["attempts"] = attempt
                    return HarnessActionResult(action, True, decision.summary, payload)
            except Exception as exc:
                current_run = self.run_store.get(run.id)
                current_task = self.task_store.get(task.id)
                recovered_proof_ids = _sync_run_proofs_to_task(
                    current_task,
                    run.id,
                    proof_store=self.proof_store,
                    task_store=self.task_store,
                    actor="harness",
                    reason="incremental command proof recovery during runtime error",
                )
                if _is_dev_stage_plan_loop_guard(exc):
                    current_task.state = TaskState.RUNNING
                    current_task.updated_at = now()
                    self.task_store.update(current_task, actor="harness", reason="dev orchestration-only stage plan ignored")
                    run = _refresh_run_for_update(self.run_store, run)
                    progress = {
                        "type": "run.progress",
                        "source": "dev_stage_plan_loop_guard",
                        "phase": "planning",
                        "step": "orchestration_only_plan_ignored",
                        "status": "ignored",
                        "summary": _safe_repair_error(str(exc)) or "Dev returned an orchestration-only stage plan; keeping the bounded owner lane active.",
                        "next_expected": "dev_executable_proof_or_bounded_retry",
                    }
                    run.progress = {**(run.progress or {}), **progress}
                    run.llm = {
                        **(run.llm or {}),
                        "validation_status": "guarded_no_progress",
                        "decision_type": getattr(getattr(decision, "type", None), "value", None) or str(getattr(decision, "type", "")),
                    }
                    self.run_store.update(run)
                    EventLog().append(Event(now(), "run.progress", task.id, run.id, persona.id, progress))
                    self.run_store.close_run(
                        run.id,
                        state=RunState.COMPLETED,
                        final_decision={
                            "type": getattr(getattr(decision, "type", None), "value", None) or "propose_stage_plan",
                            "summary": progress["summary"],
                        },
                    )
                    _close_role_session(envelope, run=self.run_store.get(run.id), close_reason="dev_stage_plan_loop_guard", next_action_after="run_slot")
                    if worker_store is not None and worker is not None:
                        worker_store.update_after_run(worker.id, self.run_store.get(run.id), close_reason="dev_stage_plan_loop_guard", count_decision=False)
                    if assignment is not None:
                        self.persona_assignment_store.complete(assignment.id, state="completed")
                    payload = {"run_id": run.id, "decision": getattr(getattr(decision, "type", None), "value", None) or "propose_stage_plan"}
                    if recovered_proof_ids:
                        payload["recovered_proof_ids"] = recovered_proof_ids
                    return HarnessActionResult(action, True, progress["summary"], payload)
                if current_run.state in {RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED}:
                    if worker_store is not None and worker is not None:
                        worker_store.update_after_run(worker.id, current_run, close_reason=f"run_{current_run.state.value}", count_decision=False)
                    if assignment is not None:
                        self.persona_assignment_store.complete(assignment.id, state="blocked", error=f"run_{current_run.state.value}")
                    _close_role_session(envelope, run=current_run, close_reason=f"run_{current_run.state.value}", next_action_after="runtime_error")
                    payload = {"run_id": run.id, "state": current_run.state.value}
                    if recovered_proof_ids:
                        payload["recovered_proof_ids"] = recovered_proof_ids
                    return HarnessActionResult(action, False, "run reached terminal state during runtime error", payload)
                if current_task.state in {TaskState.DONE, TaskState.CANCELLED}:
                    self.run_store.cancel(run.id, reason="task reached terminal state during runtime error")
                    cancelled_run = self.run_store.get(run.id)
                    if worker_store is not None and worker is not None:
                        worker_store.update_after_run(worker.id, cancelled_run, close_reason=f"task_{current_task.state.value}", count_decision=False)
                    if assignment is not None:
                        self.persona_assignment_store.complete(assignment.id, state="blocked", error=f"task_{current_task.state.value}")
                    _close_role_session(envelope, run=cancelled_run, close_reason=f"task_{current_task.state.value}", next_action_after="runtime_error")
                    return HarnessActionResult(action, False, "task reached terminal state during runtime error", {"run_id": run.id, "task_state": current_task.state.value})
                classification = classify_exception(exc)
                retryable = _is_retryable_provider_failure(classification.kind, exc) and attempt < max_attempts
                run = _refresh_run_for_update(self.run_store, run)
                run.llm = {**(run.llm or {}), "validation_status": "invalid", "retry_attempt": attempt, "retry_max_attempts": max_attempts, "retryable": retryable}
                self.run_store.update(run)
                error_payload = {"class": type(exc).__name__, "message": classification.summary, "retryable": retryable, "attempt": attempt, "max_attempts": max_attempts, "traceback": _safe_traceback_frames(exc)}
                if classification.kind == "run_budget_exceeded":
                    error_payload = {"type": classification.kind, "summary": classification.summary, "retryable": False, "attempt": attempt, "max_attempts": max_attempts}
                    safe_session_id = _safe_session_id(run.session_id)
                    run.session_id = safe_session_id
                    run.error = error_payload
                    run.progress = {
                        **(run.progress or {}),
                        "phase": "runaway_warning",
                        "severity": "critical",
                        "step": "budget_waiting_for_approval" if safe_session_id else "same_session_not_safe",
                        "status": "waiting_on_approval" if safe_session_id else "blocked",
                        "summary": "Budget limit reached; waiting for operator approval to continue same session." if safe_session_id else "Budget limit reached but same-session continuation is not safe: missing or unsafe session_id.",
                        "session_id": safe_session_id,
                        "next_expected": "approve_budget_continuation" if safe_session_id else "inspect_same_session_gap",
                    }
                    if safe_session_id:
                        run.state = RunState.WAITING_ON_APPROVAL
                        self.run_store.update(run)
                        EventLog().append(Event(now(), "run.approval_required", task.id, run.id, persona.id, run.progress))
                    else:
                        self.run_store.update(run)
                        self.run_store.close_run(run.id, state=RunState.FAILED, error={**error_payload, "type": "same_session_not_safe"})
                else:
                    self.run_store.close_run(
                        run.id,
                        state=RunState.FAILED,
                        error=error_payload,
                    )
                close_reason = "run_budget_exceeded" if classification.kind == RUN_BUDGET_EXCEEDED else "model_invalid_output" if classification.kind == MODEL_INVALID_OUTPUT else classification.kind
                _close_role_session(envelope, run=self.run_store.get(run.id), close_reason=close_reason, next_action_after="runtime_error")
                if worker_store is not None and worker is not None:
                    worker_store.update_after_run(worker.id, self.run_store.get(run.id), close_reason=close_reason, count_decision=False)
                if assignment is not None:
                    if retryable:
                        refreshed_assignment = self.persona_assignment_store.get(assignment.id)
                        refreshed_assignment.state = "assigned"
                        self.persona_assignment_store.update(refreshed_assignment)
                    elif classification.kind == RUN_BUDGET_EXCEEDED and self.run_store.get(run.id).state == RunState.WAITING_ON_APPROVAL:
                        refreshed_assignment = self.persona_assignment_store.get(assignment.id)
                        refreshed_assignment.state = "needs_input"
                        refreshed_assignment.last_error = classification.summary
                        self.persona_assignment_store.update(refreshed_assignment)
                    else:
                        self.persona_assignment_store.complete(assignment.id, state="blocked", error=classification.summary)
                if retryable:
                    continue
                inc = Incident(id=f"inc_{uuid.uuid4().hex[:8]}", task_id=task.id, run_id=run.id, kind=classification.kind, summary=classification.summary, detail_path=None, opened_at=now())
                self.incident_store.open(inc)
                if classification.kind == MODEL_INVALID_OUTPUT:
                    self.incident_store.close(inc.id, reason="contract_simplification_normalized_model_invalid_output")
                    current_task.state = TaskState.RUNNING
                    current_task.open_incident_ids = [item for item in (current_task.open_incident_ids or []) if item != inc.id]
                    current_task.updated_at = now()
                    self.task_store.update(current_task, actor="harness", reason="model invalid output normalized without Neko intervention loop")
                elif classification.kind == RUN_BUDGET_EXCEEDED:
                    current_task.open_incident_ids = _dedupe(list(current_task.open_incident_ids or []), [inc.id])
                    current_task.updated_at = now()
                    self.task_store.update(current_task, actor="harness", reason="budget incident routed to Neko continuation steering")
                payload = {"incident_id": inc.id, "run_id": run.id, "attempts": attempt}
                if last_run_id and last_run_id != run.id:
                    payload["last_run_id"] = last_run_id
                result_summary = classification.summary
                return HarnessActionResult(action, False, result_summary, payload)
        return HarnessActionResult(action, False, "persona runtime retry loop exhausted", {"run_id": last_run_id, "attempts": max_attempts})

    def _stage52_close_task_envelopes(self, task_id: str, *, reason: str, run_id: str | None = None) -> list[str]:
        if not _role_envelope_enabled(self.config):
            return []
        return self.role_envelope_store.close_for_task(task_id, reason=reason, run_id=run_id)

    def _collect_command_proof(self, task: Task, decision, *, actor: str, run_id: str, workdir_task: Task | None = None) -> list[str]:
        stage_id = str(decision.payload.get("stage_id") or task.current_stage_id or "")
        workdir_scope_task = workdir_task or task
        recipe_metadata = proof_recipe_metadata(decision.payload) if isinstance(decision.payload, dict) else None
        recipe_workdir = None
        run_record = self.run_store.get(run_id)
        isolated_workdir = _isolated_workdir_from_run_progress(run_record)
        if recipe_metadata and decision.payload.get("repo_scope") and isolated_workdir is None:
            recipe_workdir = _command_workdir_for_task(
                type("TaskCommandRecipeRepoScope", (), {"affected_repos": [decision.payload["repo_scope"]]})(),
                self.command_workdir,
                actor=actor,
                stage_id=stage_id,
            )
        proof_workdir = isolated_workdir or recipe_workdir or _command_workdir_for_task(workdir_scope_task, self.command_workdir, actor=actor, stage_id=stage_id)
        if isolated_workdir is None and self.proof_runner is None:
            proof_workdir = _isolate_command_proof_workdir_if_git(proof_workdir, task_id=task.id, run_id=run_id, actor=actor)
        runner = self.proof_runner or CommandProofRunner(
            proof_store=self.proof_store,
            workdir=proof_workdir,
            timeout_seconds=max(
                1,
                int(
                    (recipe_metadata or {}).get("timeout_seconds")
                    or getattr(self.config, "tool_wait_timeout_seconds", 300)
                    or 300
                ),
            ),
        )
        kwargs = {
            "stage_id": stage_id,
            "run_id": run_id,
            "actor": actor,
            "commands": list(decision.payload.get("commands") or []),
        }
        optional = {
            "proof_intent": _proof_intent_from_decision(decision),
            **_environment_fingerprint_payload(task, stage_id),
            "proof_recipe": recipe_metadata,
        }
        kwargs.update(_supported_runner_kwargs(runner.run_commands, optional))
        proofs = runner.run_commands(task, **kwargs)
        return [proof.id for proof in proofs]

    def _collect_visual_proof(self, task: Task, decision, *, actor: str, run_id: str) -> list[str]:
        runner = self.visual_proof_runner or VisualProofRunner(proof_store=self.proof_store)
        stage_id = str(decision.payload.get("stage_id") or task.current_stage_id or "").strip() or None
        kind = "video" if decision.type == DecisionType.REQUEST_VIDEO else "screenshot"
        result = runner.capture(task, stage_id=stage_id, run_id=run_id, actor=actor, request=decision.payload, kind=kind)
        if result.environment_blocker:
            inc = Incident(
                id=f"inc_{uuid.uuid4().hex[:8]}",
                task_id=task.id,
                run_id=run_id,
                kind="environment_blocker",
                summary=result.blocker_summary or "visual proof environment blocker",
                detail_path=None,
                opened_at=now(),
                metadata={
                    "proof_id": result.proof.id,
                    "check_id": "launcher_qa_mcp",
                    "blocking_event_id": f"visual_blocked_{uuid.uuid4().hex[:8]}",
                    "stage_id": stage_id or "",
                    "persona_target": actor,
                },
            )
            self.incident_store.open(inc)
            task.state = TaskState.RUNNING
            task.open_incident_ids = _dedupe(list(task.open_incident_ids or []), [inc.id])
        return [result.proof.id]




def _assignment_from_task_flag(store: PersonaAssignmentStore, task: Task, persona_id: str):
    """Reuse an operator/diagnostic assignment already stamped onto this task."""
    prefix = "persona_assignment_id:"
    for raw_flag in list(getattr(task, "risk_flags", None) or []):
        flag = str(raw_flag or "").strip()
        if not flag.startswith(prefix):
            continue
        assignment_id = flag[len(prefix) :].strip()
        if not assignment_id:
            continue
        try:
            assignment = store.get(assignment_id)
        except Exception:
            continue
        if assignment.task_id != task.id:
            continue
        if assignment.persona_id != persona_id:
            continue
        if assignment.state in {"completed", "blocked", "cancelled"}:
            continue
        return assignment
    return None


def _emit_timing_started(run_store: RunStore, run_id: str, timing_key: str) -> None:
    RunProgressSink(run_store=run_store, run_id=run_id).emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "timing",
            "step": timing_key,
            "status": "started",
            "summary": f"{timing_key.replace('_', ' ').title()} started.",
            "timing_key": f"{timing_key}_ms",
        },
    )


def _record_timing_span(run_store: RunStore, run_id: str, timing_key: str, started: float, *, status: str = "completed") -> None:
    duration_ms = max(0, int((time.perf_counter() - started) * 1000))
    try:
        run = run_store.get(run_id)
        timing = _safe_timing_map(run.llm)
        _record_timing_value(timing, f"{timing_key}_ms", duration_ms)
        run.llm = {**(run.llm or {}), "timing": timing}
        run_store.update(run)
    except Exception:
        pass
    RunProgressSink(run_store=run_store, run_id=run_id).emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "timing",
            "step": timing_key,
            "status": status,
            "summary": f"{timing_key.replace('_', ' ').title()} {status} in {duration_ms}ms.",
            "duration_ms": duration_ms,
            "timing_key": f"{timing_key}_ms",
        },
    )


def _safe_timing_map(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    raw = value.get("timing")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, int] = {}
    for key, item in raw.items():
        if not isinstance(key, str) or not (key.endswith("_ms") or key.endswith("_count")):
            continue
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            result[key[:64]] = parsed
    return result


def _record_timing_value(timing_map: dict[str, int], key: object, value: object) -> None:
    if not isinstance(key, str) or not key.endswith(("_ms", "_count")):
        return
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return
    if parsed < 0:
        return
    safe_key = key[:64]
    if safe_key.endswith("_count"):
        previous = timing_map.get(safe_key)
        timing_map[safe_key] = (previous if isinstance(previous, int) and previous >= 0 else 0) + parsed
        return
    previous = timing_map.get(safe_key)
    if isinstance(previous, int):
        base = safe_key[:-3]
        count_key = f"{base}_count"[:64]
        total_key = f"{base}_total_ms"[:64]
        max_key = f"{base}_max_ms"[:64]
        previous_count = timing_map.get(count_key)
        previous_total = timing_map.get(total_key)
        previous_max = timing_map.get(max_key)
        if not isinstance(previous_count, int) or previous_count < 1:
            previous_count = 1
        if not isinstance(previous_total, int) or previous_total < previous:
            previous_total = previous
        if not isinstance(previous_max, int):
            previous_max = previous
        timing_map[count_key] = previous_count + 1
        timing_map[total_key] = previous_total + parsed
        timing_map[max_key] = max(previous_max, parsed)
    timing_map[safe_key] = parsed


def _assignment_spec_for_action(action: HarnessAction, task: Task, *, persona_id: str, repo_bundle_id: str | None = None) -> PersonaAssignmentSpec:
    stage = current_plan_stage(task) or _current_stage(task)
    title = getattr(stage, "title", None) or str(action.type.value).replace("_", " ").title()
    objective = getattr(stage, "objective", None) or action.reason
    repo = getattr(stage, "repo", None) or _repo_for_task(task)
    affected_paths = list(getattr(stage, "affected_paths", None) or [])
    proof_targets = []
    goal_named = goal_named_gate_commands(task, stage_repo_for_gate(task, stage)) if stage is not None else []
    if goal_named and goal_demands_exact_proof(task):
        proof_targets.extend(goal_named)
    else:
        for item in list(getattr(stage, "test_plan", None) or []):
            text = str(item or "").strip()
            if text:
                proof_targets.append(text)
        if getattr(stage, "proof_recipe_id", None):
            proof_targets.append(f"proof_recipe:{stage.proof_recipe_id}")
        if not proof_targets and goal_named:
            proof_targets.extend(goal_named)
    return PersonaAssignmentSpec(
        persona_id=persona_id,
        kind="task_stage",
        title=str(title),
        message=str(objective or action.reason),
        created_by="harness",
        persona_instance_id=persona_instance_id_for_placement(f"{getattr(task, 'goal_id', None) or task.id}:{persona_id}"),
        task_id=task.id,
        goal_id=getattr(task, "goal_id", None) or task.id,
        stage_id=getattr(getattr(task, "mission_plan", None), "current_stage_id", None) or task.current_stage_id,
        repo_bundle_id=repo_bundle_id,
        repo=repo,
        affected_paths=affected_paths,
        proof_targets=proof_targets,
        acceptance=list(getattr(stage, "acceptance_criteria", None) or getattr(task, "acceptance_criteria", None) or []),
        non_goals=list(getattr(task, "non_goals", None) or []),
        allowed_decisions=_allowed_decisions_for_action(action),
    )


def _repo_for_task(task: Task) -> str | None:
    repos = list(getattr(task, "affected_repos", None) or [])
    return str(repos[0]) if repos else None


def _repo_bundle_routing_enabled(config: RuntimeConfig) -> bool:
    bundle_config = getattr(config, "repo_bundle_routing", None)
    contract_config = getattr(config, "simplified_agent_contract", None)
    return bool(getattr(bundle_config, "enabled", False) or getattr(contract_config, "enabled", False))


def _repo_bundle_for_action(config: RuntimeConfig, store: RepoBundleStore, action: HarnessAction, task: Task, *, persona_id: str):
    if not _repo_bundle_routing_enabled(config):
        return None
    if not _action_targets(action, "dev", "backend_dev", "qa"):
        if _action_targets(action, "neko_supervisor"):
            store.create_or_update_from_task(task)
        return None
    stage = _current_stage(task)
    repo = getattr(stage, "repo", None) or _repo_for_task(task)
    return find_best_bundle_for_action(
        task,
        persona_id=persona_id,
        stage_id=getattr(stage, "id", None) or task.current_stage_id,
        repo=repo,
        store=store,
    )


def _mark_repo_bundle_running_for_assignment(config: RuntimeConfig, store: RepoBundleStore, assignment, *, run_id: str | None) -> None:
    if not _repo_bundle_routing_enabled(config):
        return
    if getattr(assignment, "persona_id", None) not in {"dev", "backend_dev"}:
        return
    bundle = store.find_for_assignment(assignment)
    if bundle is None:
        return
    store.mark_running(bundle, run_id=run_id)


def _mark_repo_bundle_after_decision(config: RuntimeConfig, store: RepoBundleStore, assignment, decision, *, proof_ids: list[str], proof_statuses: list[str] | None = None) -> None:
    if not _repo_bundle_routing_enabled(config):
        return
    bundle = store.find_for_assignment(assignment)
    if bundle is None:
        return
    decision_type = getattr(decision, "type", None)
    if decision_type in {DecisionType.PROPOSE_PATCH, DecisionType.COMPLETE, DecisionType.REQUEST_QA_REVIEW}:
        store.mark_delivered(bundle, proof_ids=proof_ids)
        store.wake_ready_dependencies(bundle.task_id)
        return
    if decision_type == DecisionType.REQUEST_TEST_RUN and proof_ids:
        statuses = [str(status or "").strip().lower() for status in (proof_statuses or [])]
        if statuses and all(status in {"passed", "approved"} for status in statuses):
            store.mark_delivered(bundle, proof_ids=proof_ids)
            store.wake_ready_dependencies(bundle.task_id)
        elif statuses:
            store.mark_rejected(bundle, reason="Requested proof did not pass")
        return
    if decision_type == DecisionType.REPORT_QA_VERDICT:
        payload = getattr(decision, "payload", None) if isinstance(getattr(decision, "payload", None), dict) else {}
        verdict = str(payload.get("validation_status") or payload.get("verdict") or payload.get("status") or "").lower()
        if verdict in {"valid", "approved", "pass", "passed"}:
            for candidate in store.list_for_task(bundle.task_id):
                if candidate.state in {"delivered_waiting_for_qa", "delivered"}:
                    store.mark_verified(candidate, proof_ids=proof_ids)
        elif verdict in {"invalid", "rejected", "fail", "failed"}:
            store.mark_rejected(bundle, reason=getattr(decision, "summary", "") or "QA rejected bundle")
        return
    if decision_type == DecisionType.BLOCK:
        store.mark_rejected(bundle, reason=getattr(decision, "summary", "") or "Agent reported blocker")


def _allowed_decisions_for_action(action: HarnessAction) -> list[str]:
    if _action_targets(action, "neko_supervisor"):
        return ["propose_acceptance", "handoff_to_dev", "request_context", "block"]
    if _action_targets(action, "dev", "backend_dev"):
        return ["deliver", "request_test_run", "request_screenshot", "request_video", "report_blocker", "handoff"]
    if _action_targets(action, "qa"):
        return ["report_qa_verdict", "request_test_run", "request_screenshot", "request_video", "block"]
    return []


def _role_envelope_enabled(config: RuntimeConfig) -> bool:
    cfg = getattr(config, "role_envelope", None)
    return bool(getattr(cfg, "enabled", False))


def _record_stage52_proof_batch(store: ProofBatchStore, task: Task, decision, proof_ids: list[str], proof_statuses: list[str], *, role_envelope_id: str | None, run_id: str | None, persona_id: str | None) -> str | None:
    if role_envelope_id is None:
        return None
    if not proof_ids:
        return None
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    stage_id = str(payload.get("stage_id") or task.current_stage_id or "").strip() or None
    recipe_id = str(payload.get("recipe_id") or "").strip() or None
    normalized_statuses = [str(status or "").strip().lower() for status in proof_statuses]
    status = "passed" if normalized_statuses and all(status in {"passed", "approved"} for status in normalized_statuses) else "failed"
    batch = store.record_batch(
        task_id=task.id,
        mission_stage_id=stage_id,
        role_envelope_id=role_envelope_id,
        recipe_id=recipe_id,
        proof_ids=proof_ids,
        status=status,
        run_id=run_id,
        persona_id=persona_id,
    )
    return batch.proof_batch_id

def _sync_run_proofs_to_task(task: Task, run_id: str, *, proof_store: ProofStore, task_store: TaskStore, actor: str, reason: str) -> list[str]:
    """Persist proofs already attached for this run onto the task immediately.

    Command proofs are stored one command at a time. If a later command hangs,
    fails, or the model runtime errors before the decision returns, earlier proof
    records must not remain orphaned in ProofStore/event logs while task.proof_ids
    stays empty.
    """
    proof_ids: list[str] = []
    for proof in proof_store.list_for_task(task.id):
        metadata = proof.metadata or {}
        if metadata.get("run_id") == run_id:
            proof_ids.append(proof.id)
    if not proof_ids:
        return []
    merged = _dedupe(list(task.proof_ids or []), proof_ids)
    added = [proof_id for proof_id in merged if proof_id not in set(task.proof_ids or [])]
    if not added:
        return []
    task.proof_ids = merged
    task.updated_at = now()
    task_store.update(task, actor=actor, reason=reason)
    return added


def _should_auto_run_final_gate(config: RuntimeConfig, decision) -> bool:
    if decision.type != DecisionType.PROPOSE_PATCH:
        return False
    flow = getattr(config, "normal_worker_flow", None)
    if bool(getattr(flow, "enabled", False)) and bool(getattr(flow, "auto_final_gate_after_delivery", False)):
        return True
    return simplified_contract_enabled(config)


def _stage_for_gate(task: Task, stage_id: str | None):
    wanted = str(stage_id or "").strip()
    plan = getattr(task, "mission_plan", None)
    if plan is not None and wanted:
        stage = next((item for item in getattr(plan, "stages", []) or [] if str(getattr(item, "id", "") or "") == wanted), None)
        if stage is not None:
            return stage
    return next((stage for stage in getattr(task, "stages", []) or [] if str(getattr(stage, "id", "") or "") == wanted), None)


def _build_authoritative_stage_gate_decision(
    task: Task,
    stage,
    *,
    delivery_packet: dict[str, Any] | None = None,
    handoff_packet: dict[str, Any] | None = None,
):
    if stage is None:
        return None
    repo = stage_repo_for_gate(task, stage)
    packet_commands = packet_named_gate_commands(task, stage, repo, delivery_packet=delivery_packet, handoff_packet=handoff_packet)
    legacy_product_gate = build_final_gate_decision(task, stage, delivery_packet=delivery_packet, handoff_packet=handoff_packet)
    if legacy_product_gate is not None and (packet_commands or not str(getattr(stage, "proof_recipe_id", "") or "").strip()):
        return legacy_product_gate
    if packet_commands:
        return None
    stage_id = str(getattr(stage, "id", "") or getattr(task, "current_stage_id", "") or "").strip()
    forbidden = packet_forbidden_gate_commands(handoff_packet, delivery_packet)
    goal_named = filter_forbidden_gate_commands(goal_named_gate_commands(task, repo), forbidden)
    if goal_named:
        # A focused proof command literally named by the goal outranks generic
        # proof recipes at the authoritative gate: the Harness re-runs THAT
        # command; recipes/defaults remain the fallback when none is named.
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="Run goal-named focused proof command as the authoritative gate.",
            rationale="The goal names an exact runnable proof command; the Harness gate re-runs it instead of a generic recipe.",
            payload={
                "stage_id": stage_id,
                "commands": goal_named,
                "proof_intent": "authoritative_gate_after_hand_off",
            },
        )
    recipe_id = str(getattr(stage, "proof_recipe_id", "") or "").strip()
    if recipe_id:
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="Run authoritative Harness proof recipe after hand_off.",
            rationale="Collapsed hand_off gates on the Harness rerun, not the agent's observed command.",
            payload={
                "stage_id": stage_id,
                "recipe_id": recipe_id,
                "proof_intent": "authoritative_gate_after_hand_off",
            },
        )
    commands = filter_forbidden_gate_commands(_stage_gate_commands(stage), forbidden)
    if commands:
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="Run authoritative Harness command gate after hand_off.",
            rationale="Collapsed hand_off gates on the Harness rerun, not the agent's observed command.",
            payload={
                "stage_id": stage_id,
                "commands": commands,
                "proof_intent": "authoritative_gate_after_hand_off",
            },
        )
    return build_final_gate_decision(task, stage, delivery_packet=delivery_packet, handoff_packet=handoff_packet)


def _stage_gate_commands(stage) -> list[str]:
    commands: list[str] = []
    for item in list(getattr(stage, "test_plan", []) or []):
        text = str(item or "").strip()
        if not text or text.startswith("proof_recipe:"):
            continue
        commands.append(text)
    return commands[:3]


def _record_handoff_observation(
    task: Task,
    decision,
    *,
    run,
    actor: str,
    proof_store,
    command_workdir,
    task_store,
) -> None:
    # Fire the observe-the-work lane on every delivery/gate-request signal so the
    # HUD diff+trace surface has parity across the simplified and legacy contracts:
    # under the simplified flag a collapsed hand_off projects onto PROPOSE_PATCH,
    # but on the legacy/rollback path a no-edit dev delivers via REQUEST_TEST_RUN
    # (and REQUEST_QA_REVIEW), which previously produced no observed-handoff record.
    if decision.type not in {DecisionType.PROPOSE_PATCH, DecisionType.REQUEST_QA_REVIEW, DecisionType.REQUEST_TEST_RUN}:
        return
    stage_id = str((decision.payload or {}).get("stage_id") or task.current_stage_id or getattr(run, "stage_id", "") or "").strip() or None
    baseline = (getattr(run, "progress", None) or {}).get("repo_baseline") if run is not None else None
    try:
        workdir = _isolated_workdir_from_run_progress(run) or _command_workdir_for_task(task, command_workdir, actor=actor, stage_id=stage_id)
        diff = git_diff_since_baseline(workdir, baseline if isinstance(baseline, dict) else None)
    except Exception:
        diff = {"schema_version": 1, "diff": "", "diff_chars": 0, "error": "diff_capture_failed"}
    observed_proof_ids: list[str] = []
    try:
        for proof in proof_store.list_for_task(task.id):
            metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
            if metadata.get("source") != "agent_tool_trace":
                continue
            # Link observed self-tests to a stage by STAGE identity, not the exact
            # handoff turn's run: dev work is multi-turn, so the self-test command
            # often runs in an earlier turn (run A) than the handoff turn (run B).
            # Keying on run_id alone dropped every same-stage observed proof. Match
            # on stage when the proof carries one; fall back to run_id only for
            # proofs with no stage label so nothing cross-stage bleeds in.
            proof_stage = proof.stage_id or metadata.get("stage_id")
            if stage_id:
                if proof_stage:
                    if proof_stage != stage_id:
                        continue
                elif metadata.get("run_id") != getattr(run, "id", None):
                    continue
            elif metadata.get("run_id") != getattr(run, "id", None):
                continue
            observed_proof_ids.append(proof.id)
    except Exception:
        observed_proof_ids = []
    root = task.harness_self_heal if isinstance(task.harness_self_heal, dict) else {}
    observations = root.get("stage_observations") if isinstance(root.get("stage_observations"), dict) else {}
    key = stage_id or "_task"
    observations[key] = {
        "schema_version": 1,
        "captured_at": now().isoformat(),
        "source": "harness_observed_handoff",
        "actor": actor,
        "run_id": getattr(run, "id", None),
        "stage_id": stage_id,
        "repo_diff": diff,
        "observed_proof_ids": observed_proof_ids[:20],
        "authoritative_gate_proof_ids": [],
    }
    root["stage_observations"] = observations
    evidence = root.get("evidence_stack") if isinstance(root.get("evidence_stack"), list) else []
    evidence.append(
        {
            "kind": "harness_observed_handoff",
            "severity": "info",
            "stage_id": stage_id or "",
            "summary": f"Harness captured handoff diff ({diff.get('diff_chars', 0)} chars) and {len(observed_proof_ids)} observed proof record(s).",
            "warnings": ["pre-existing dirty paths excluded"] if diff.get("baseline_dirty_count") else [],
            "recorded_at": now().isoformat(),
            "recommended_owner": "harness",
        }
    )
    root["evidence_stack"] = evidence[-25:]
    task.harness_self_heal = root
    task_store.update(task, actor="harness", reason="record harness observed handoff diff/proof")


def _validate_observed_trace_requirement(task: Task, decision, *, run, proof_store) -> None:
    if decision.type not in {DecisionType.PROPOSE_PATCH, DecisionType.REQUEST_QA_REVIEW, DecisionType.REQUEST_TEST_RUN}:
        return
    stage_id = str((decision.payload or {}).get("stage_id") or task.current_stage_id or getattr(run, "stage_id", "") or "").strip() or None
    if not _stage_requires_observed_agent_trace(task, stage_id):
        return
    observed = _observed_agent_tool_trace_proof_ids(
        task.id,
        run_id=getattr(run, "id", None),
        stage_id=stage_id,
        proof_store=proof_store,
    )
    if observed:
        return
    raise DecisionPayloadInvalid(
        "observed agent_tool_trace proof required before hand_off; run a real focused terminal self-test "
        "(pytest, flutter test/analyze, or manage.py check) in this agent session first. "
        "Prose or ad hoc echo/print markers do not satisfy the observed lane."
    )


def _stage_requires_observed_agent_trace(task: Task, stage_id: str | None) -> bool:
    stage = _stage_for_gate(task, stage_id or "")
    gate = getattr(stage, "proof_gate", None) if stage is not None else None
    gate = gate if isinstance(gate, dict) else {}
    required = {str(value).strip().lower() for value in (gate.get("required_proof_types") or []) if str(value).strip()}
    expectation = " ".join(
        str(gate.get(key) or "")
        for key in ("observed_lane_expectation", "observed_lane_requirement")
    ).strip().lower()
    return bool(gate.get("observed_lane_required")) or "agent_tool_trace" in required or "observed_proof" in expectation or "agent_tool_trace" in expectation


def _observed_agent_tool_trace_proof_ids(task_id: str, *, run_id: str | None, stage_id: str | None, proof_store) -> list[str]:
    observed: list[str] = []
    try:
        for proof in proof_store.list_for_task(task_id):
            metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
            if metadata.get("source") != "agent_tool_trace":
                continue
            if run_id and metadata.get("run_id") != run_id:
                continue
            if stage_id and proof.stage_id and proof.stage_id != stage_id:
                continue
            observed.append(proof.id)
    except Exception:
        return []
    return observed


def _record_authoritative_gate_observation(
    task: Task,
    *,
    stage_id: str | None,
    proof_ids: list[str],
    proof_statuses: list[str],
    run_id: str,
    actor: str,
    task_store,
) -> None:
    root = task.harness_self_heal if isinstance(task.harness_self_heal, dict) else {}
    observations = root.get("stage_observations") if isinstance(root.get("stage_observations"), dict) else {}
    key = stage_id or "_task"
    current = observations.get(key) if isinstance(observations.get(key), dict) else {}
    current["authoritative_gate_proof_ids"] = proof_ids[:20]
    current["authoritative_gate_status"] = "passed" if proof_statuses and all(status == "passed" for status in proof_statuses) else "failed"
    current["authoritative_gate_recorded_at"] = now().isoformat()
    current["authoritative_gate_run_id"] = run_id
    observations[key] = current
    root["stage_observations"] = observations
    evidence = root.get("evidence_stack") if isinstance(root.get("evidence_stack"), list) else []
    evidence.append(
        {
            "kind": "harness_authoritative_gate",
            "severity": "info" if current["authoritative_gate_status"] == "passed" else "warning",
            "stage_id": stage_id or "",
            "summary": f"Harness authoritative gate {current['authoritative_gate_status']} with {len(proof_ids)} proof record(s).",
            "recorded_at": now().isoformat(),
            "recommended_owner": actor,
        }
    )
    root["evidence_stack"] = evidence[-25:]
    task.harness_self_heal = root
    task_store.update(task, actor="harness", reason="record authoritative gate proof lane")


def _handoff_diff_weakens_tests(task: Task, stage_id: str | None) -> bool:
    root = task.harness_self_heal if isinstance(task.harness_self_heal, dict) else {}
    observations = root.get("stage_observations") if isinstance(root.get("stage_observations"), dict) else {}
    item = observations.get(stage_id or "_task") if isinstance(observations, dict) else None
    diff = (item or {}).get("repo_diff", {}).get("diff") if isinstance(item, dict) else ""
    # Shared pure scanner (repo_context.diff_weakens_tests) so the legacy gate and
    # the root-node evidence handle never drift.
    return diff_weakens_tests(diff if isinstance(diff, str) else "")


def _emit_run_budget_warning(run, *, task_id: str, actor: str) -> bool:
    llm = run.llm or {}
    api_calls = _safe_int(llm.get("api_calls"))
    total_tokens = _safe_int(llm.get("total_tokens"))
    iterations_used = _safe_int(getattr(run, "iterations_used", None))
    iteration_budget = _safe_int(getattr(run, "iteration_budget", None))
    warnings: list[str] = []
    if api_calls is not None and api_calls >= 20:
        warnings.append(f"api_calls={api_calls}")
    if total_tokens is not None and total_tokens >= 750_000:
        warnings.append(f"total_tokens={total_tokens}")
    if iterations_used is not None and iteration_budget is not None and iteration_budget > 0 and iterations_used >= iteration_budget:
        warnings.append(f"iterations={iterations_used}/{iteration_budget}")
    if not warnings:
        return False
    payload = {
        "phase": "runaway_warning",
        "severity": "warning",
        "step": "run_budget_high",
        "status": "warning",
        "summary": "Run exceeded live budget warning threshold: " + ", ".join(warnings),
        "next_expected": "inspect_run_budget",
    }
    if api_calls is not None:
        payload["api_calls"] = api_calls
    if total_tokens is not None:
        payload["total_tokens"] = total_tokens
    if iterations_used is not None:
        payload["iteration"] = iterations_used
    if iteration_budget is not None:
        payload["max_iterations"] = iteration_budget
    EventLog().append(Event(now(), "run.progress", task_id, run.id, actor, payload))
    return True


def _proof_statuses(proof_ids: list[str], *, proof_store: ProofStore) -> list[str]:
    statuses: list[str] = []
    for proof_id in proof_ids:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            statuses.append("missing")
            continue
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        statuses.append(str(metadata.get("status") or metadata.get("verdict") or "missing").strip().lower())
    return statuses


def _existing_passed_final_gate_proof_ids(task: Task, stage_id: str | None, *, proof_store: ProofStore) -> list[str]:
    stage_proof_ids = [
        proof_id
        for stage in _runtime_stage_records(task)
        if not stage_id or stage.id == stage_id
        for proof_id in list(getattr(stage, "proof_ids", None) or [])
    ]
    candidates = _dedupe(list(getattr(task, "proof_ids", None) or []), stage_proof_ids)
    passed: list[str] = []
    for proof_id in candidates:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        if proof_type != "test_run":
            continue
        if stage_id and proof.stage_id != stage_id:
            continue
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if str(metadata.get("status") or "").strip().lower() != "passed":
            continue
        passed.append(proof.id)
    return passed[:1]


def _recover_handoff_repair_with_existing_proof(task: Task, *, proof_store: ProofStore) -> dict | None:
    if task.state != TaskState.RUNNING:
        return None
    stage_id = str(getattr(task, "current_stage_id", "") or "").strip()
    if not stage_id:
        return None
    text = " ".join(
        str(item or "").lower()
        for item in [
            getattr(task, "title", ""),
            getattr(task, "description", ""),
            *(getattr(task, "risk_flags", None) or []),
        ]
    )
    if not (
        ("handoff" in text or "delivery" in text)
        and ("repair" in text or "metadata" in text)
        and "existing passed proof" in text
    ):
        return None
    proof_ids = _existing_passed_final_gate_proof_ids(task, stage_id, proof_store=proof_store)
    if not proof_ids:
        return None
    _set_stage_status(task, stage_id, StageStatus.READY_FOR_QA)
    task.proof_ids = _dedupe(list(getattr(task, "proof_ids", None) or []), proof_ids)
    task.state = TaskState.RUNNING
    task.updated_at = now()
    return {"stage_id": stage_id, "proof_ids": proof_ids, "next_state": task.state.value}


def _runtime_stage_records(task: Task) -> list:
    plan = getattr(task, "mission_plan", None)
    if plan is not None and getattr(plan, "enabled", False):
        return list(getattr(plan, "stages", None) or [])
    return list(getattr(task, "stages", None) or [])


def _ensure_legacy_command_proof_plan(task: Task, decision, *, before_task: Task) -> bool:
    if getattr(task, "mission_plan", None) is not None:
        return False
    if getattr(before_task, "stages", None):
        return False
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    stage_id = str(payload.get("stage_id") or getattr(task, "current_stage_id", "") or "").strip()
    if stage_id != "implement":
        return False
    commands = [str(command).strip() for command in (payload.get("commands") or []) if str(command).strip()]
    repo = _legacy_command_proof_repo(task)
    stamp = now()
    task.mission_plan = MissionPlan(
        enabled=True,
        blueprint_id="legacy_command_proof",
        mission_intent=MissionIntent(
            title=task.title,
            objective=task.description or task.title,
            acceptance_criteria=list(getattr(task, "acceptance_criteria", None) or ["Deterministic command proof is attached."]),
            non_goals=list(getattr(task, "non_goals", None) or []),
            source_task_id=task.id,
        ),
        stages=[
            MissionPlanStage(
                id="implement",
                title="Implement",
                objective=f"Collect deterministic command proof for {task.title}.",
                owner="dev",
                owner_slot="dev",
                repo=repo,
                kind="implementation",
                status=StageStatus.READY,
                affected_paths=[],
                acceptance_criteria=list(getattr(task, "acceptance_criteria", None) or ["Deterministic command proof is attached."]),
                test_plan=commands,
                created_at=stamp,
                updated_at=stamp,
            ),
            MissionPlanStage(
                id="verify",
                title="Verify",
                objective="QA verifies the attached command proof and task acceptance.",
                owner="qa",
                owner_slot="qa",
                repo=repo,
                kind="qa_verdict",
                status=StageStatus.READY,
                depends_on=["implement"],
                blocks_qa_until=False,
                created_at=stamp,
                updated_at=stamp,
            ),
        ],
        current_stage_id="implement",
        revision=1,
        slots={
            "dev": {"role": "builder", "required": True},
            "qa": {"role": "qa", "required": True},
        },
        bindings={"dev": "dev", "qa": "qa"},
        binding_sources={"dev": "persona:dev", "qa": "persona:qa"},
        edges=[
            {"source": "implement", "outcome": "ready", "target": "verify"},
            {"source": "implement", "outcome": "passed", "target": "verify"},
            {"source": "implement", "outcome": "failed", "target": "implement"},
            {"source": "implement", "outcome": "needs_fixes", "target": "implement"},
            {"source": "implement", "outcome": "missing_input", "target": "implement"},
            {"source": "implement", "outcome": "blocked", "target": "intervention"},
            {"source": "verify", "outcome": "passed", "target": "done"},
            {"source": "verify", "outcome": "failed", "target": "implement"},
            {"source": "verify", "outcome": "needs_fixes", "target": "implement"},
            {"source": "verify", "outcome": "blocked", "target": "intervention"},
            {"source": "verify", "outcome": "missing_input", "target": "intervention"},
        ],
        limits={"max_attempts_per_stage": 2, "max_total_stages": 16},
    )
    task.current_stage_id = "implement"
    _sync_task_stage_compat_from_plan(task)
    return True


def _legacy_command_proof_repo(task: Task) -> str:
    labels = {str(label).strip() for label in safe_affected_repo_labels(list(getattr(task, "affected_repos", []) or []))}
    if "EterniaBackend" in labels:
        return "EterniaBackend"
    if "EterniaLauncher" in labels:
        return "EterniaLauncher"
    if "hermes-agent" in labels:
        return "hermes-agent"
    return "hermes-agent"


def _backend_first_burn_in_orchestration_plan(task: Task, decision, *, persona_id: str) -> bool:
    if persona_id != "backend_dev" or decision.type != DecisionType.PROPOSE_STAGE_PLAN:
        return False
    flags = {str(flag).strip().lower() for flag in (getattr(task, "risk_flags", None) or [])}
    if not {"backend_contract_first", "bounded_complex_burn_in"} <= flags:
        return False
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    stages = payload.get("stages") if isinstance(payload.get("stages"), list) else []
    if not stages:
        return False
    return not any(_raw_stage_is_backend_executable_proof(stage) for stage in stages if isinstance(stage, dict))


def _raw_stage_is_backend_executable_proof(stage: dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(stage.get("id") or ""),
            str(stage.get("title") or ""),
            str(stage.get("objective") or ""),
            " ".join(str(item) for item in (stage.get("affected_paths") or [])),
            " ".join(str(item) for item in (stage.get("test_plan") or [])),
        ]
    ).lower()
    has_test_plan = any(str(item).strip() for item in (stage.get("test_plan") or []))
    return has_test_plan and "backend" in text and not any(marker in text for marker in ("launcher", "qa", "neko", "join gate"))


def _set_current_stage_id(task: Task, stage_id: str | None) -> None:
    task.current_stage_id = stage_id
    plan = getattr(task, "mission_plan", None)
    if plan is not None and getattr(plan, "enabled", False):
        known = {stage.id for stage in list(getattr(plan, "stages", None) or [])}
        plan.current_stage_id = stage_id if stage_id in known else None
        plan.revision = int(getattr(plan, "revision", 0) or 0) + 1


def _set_stage_status(task: Task, stage_id: str, status: StageStatus) -> None:
    for stage in _runtime_stage_records(task):
        if stage.id == stage_id:
            stage.status = status
            stage.updated_at = now()
            break


def _validate_visual_request_not_redundant(task: Task, decision, *, proof_store: ProofStore) -> None:
    """Force QA to verdict from an already captured same-scope visual proof."""
    if decision.type not in {DecisionType.REQUEST_SCREENSHOT, DecisionType.REQUEST_VIDEO}:
        return
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    stage_id = str(payload.get("stage_id") or task.current_stage_id or "").strip()
    target = str(payload.get("target") or "").strip().lower()
    requested_type = "video" if decision.type == DecisionType.REQUEST_VIDEO else "screenshot"
    existing = _latest_matching_passed_visual_proof(
        task,
        proof_store=proof_store,
        stage_id=stage_id,
        target=target,
        proof_type=requested_type,
    )
    if existing is None:
        return
    latest_change = _latest_stage_change_proof(task, proof_store=proof_store, stage_id=stage_id)
    if latest_change is not None and latest_change.created_at > existing.created_at:
        return
    raise DecisionPayloadInvalid(
        "matching visual proof already exists for this stage/target: "
        f"{existing.id}. Do not request another {requested_type}; inspect the existing proof metadata/artifact "
        "and return report_qa_verdict with review_scope='implementation', verdict, proof_ids including that visual proof, "
        "and findings, or return block with the exact remaining gap."
    )


def _validate_request_test_run_targets_current_stage(task: Task, decision) -> None:
    if decision.type != DecisionType.REQUEST_TEST_RUN:
        return
    requested_stage_id = str(decision.payload.get("stage_id") or "").strip()
    current_stage_id = str(getattr(task, "current_stage_id", "") or "").strip()
    typed_current_stage_id = str(getattr(getattr(task, "mission_plan", None), "current_stage_id", "") or "").strip()
    recipe_id = str(decision.payload.get("recipe_id") or "").strip()
    requested_stage = next((stage for stage in _runtime_stage_records(task) if stage.id == requested_stage_id), None)
    typed_stage = current_plan_stage(task)
    if (
        typed_stage is not None
        and str(getattr(typed_stage, "id", "") or "") == (requested_stage_id or typed_current_stage_id or current_stage_id)
        and str(getattr(typed_stage, "kind", "") or "") == "context"
        and not stage_requires_product_edit(task, typed_stage)
        and not getattr(typed_stage, "proof_recipe_id", None)
        and not _unambiguous_stage_proof_commands(typed_stage)
    ):
        raise DecisionPayloadInvalid(
            "request_test_run is not valid for no-edit investigation context stages without a typed proof recipe or test_plan; "
            "request_file_reads for bounded repo context, then deliver findings or block with evidence."
        )
    if requested_stage is not None and no_product_edit_recipe_id(recipe_id) and stage_requires_product_edit(task, requested_stage):
        raise DecisionPayloadInvalid(
            f"request_test_run recipe_id {recipe_id!r} is no-product-edit proof and cannot satisfy product-edit stage {requested_stage_id!r}; patch first and request focused implementation proof."
        )
    if requested_stage_id and typed_current_stage_id and requested_stage_id == typed_current_stage_id:
        return
    if not requested_stage_id or not current_stage_id or requested_stage_id == current_stage_id:
        return
    current_stage = _current_stage(task)
    if current_stage is None:
        return
    if current_stage.status in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
        return
    if no_product_edit_recipe_id(recipe_id) and stage_requires_product_edit(task, current_stage):
        raise DecisionPayloadInvalid(
            f"request_test_run recipe_id {recipe_id!r} cannot bypass incomplete product-edit stage {current_stage_id!r}; return propose_patch/correct_stage for that stage or request focused Flutter/widget proof after edits."
        )
    current_commands = _unambiguous_stage_proof_commands(current_stage)
    if len(current_commands) == 1:
        requested_commands = [str(item).strip() for item in (decision.payload.get("commands") or []) if str(item).strip()]
        decision.payload["stage_id"] = current_stage_id
        decision.payload["commands"] = current_commands
        if isinstance(decision.payload.get("delivery"), dict):
            decision.payload["delivery"]["known_gaps"] = _dedupe(
                list(decision.payload["delivery"].get("known_gaps") or []),
                [f"request_test_run stage autocorrected from {requested_stage_id} to {current_stage_id}"],
            )
        state = task.harness_self_heal.setdefault("stages", {}).setdefault(current_stage_id, {})
        state["last_stage_autocorrect"] = {
            "from_stage_id": requested_stage_id,
            "to_stage_id": current_stage_id,
            "commands": current_commands,
        }
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=None,
                persona_id="harness",
                payload={
                    "type": "run.progress",
                    "source": "request_test_run_stage_autocorrect",
                    "phase": "self_heal",
                    "step": "proof_stage_autocorrected",
                    "status": "applied",
                    "from_stage_id": requested_stage_id,
                    "to_stage_id": current_stage_id,
                    "requested_commands": requested_commands,
                    "commands": current_commands,
                    "summary": "Rewrote later-stage proof request to the current stage's unambiguous proof command.",
                    "next_expected": "harness_command_proof",
                },
            )
        )
        return
    raise DecisionPayloadInvalid(
        "request_test_run cannot target a later or different stage while the current stage "
        f"{current_stage_id!r} is {current_stage.status.value}; request proof for the current stage first "
        "or return correct_stage/block with exact evidence."
    )


def _emit_decision_process_summary(run_store: RunStore, run_id: str, decision) -> None:
    summary = str(getattr(decision, "summary", "") or "").strip()
    rationale = str(getattr(decision, "rationale", "") or "").strip()
    if not summary and not rationale:
        return
    reasoning = _safe_decision_text(summary)
    safe_rationale = _safe_decision_text(rationale)
    if safe_rationale and safe_rationale != reasoning:
        reasoning = f"{reasoning} Rationale: {safe_rationale}" if reasoning else safe_rationale
    if not reasoning:
        return
    decision_type = getattr(getattr(decision, "type", None), "value", str(getattr(decision, "type", "")))
    existing_progress = run_store.get(run_id).progress or {}
    role_session_progress = existing_progress.get("role_session") if isinstance(existing_progress.get("role_session"), dict) else None
    RunProgressSink(run_store=run_store, run_id=run_id).emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "thinking_process",
            "step": "decision_summary",
            "status": "completed",
            "summary": "Agent decision process summarized",
            "decision_type": decision_type,
            "reasoning_summary": reasoning,
        },
    )
    if role_session_progress:
        run = run_store.get(run_id)
        run.progress = {**(run.progress or {}), "role_session": role_session_progress}
        run_store.update(run)


def _safe_decision_text(value: str) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "api_key", "apikey", "authorization", "bearer", "credential", "cookie", "private_key", "sk-")):
        return ""
    if ":/" in text or "\\" in text or text.startswith(("/", "~")):
        return ""
    return text[:500]


def _autocorrect_downstream_visual_block_to_current_stage_proof(task: Task, decision) -> None:
    if decision.type != DecisionType.BLOCK:
        return
    current_stage_id = str(getattr(task, "current_stage_id", "") or "").strip()
    current_stage = _current_stage(task)
    if not current_stage_id or current_stage is None:
        return
    if current_stage.status in {StageStatus.READY_FOR_QA, StageStatus.PASSED}:
        return
    current_commands = _unambiguous_stage_proof_commands(current_stage)
    if len(current_commands) != 1:
        return
    text = _decision_text(decision)
    if not _looks_like_downstream_visual_gap(text):
        return
    decision.type = DecisionType.REQUEST_TEST_RUN
    decision.summary = "Collect current-stage proof before downstream visual proof."
    decision.rationale = (
        "The worker blocked on downstream visual proof while the current stage still has one "
        "unambiguous command proof gate; Harness is collecting the current-stage proof first."
    )
    decision.payload = {
        "stage_id": current_stage_id,
        "commands": current_commands,
        "delivery": {
            "source_handoff_packet_id": "",
            "consumed_contract_packet_ids": [],
            "consumed_proof_ids": [],
            "known_gaps": ["downstream visual proof remains required after current-stage command proof"],
            "next_owner": "neko_supervisor",
        },
    }
    EventLog().append(
        Event(
            ts=now(),
            type="run.progress",
            task_id=task.id,
            run_id=None,
            persona_id="harness",
            payload={
                "type": "run.progress",
                "source": "downstream_visual_block_autocorrect",
                "phase": "self_heal",
                "step": "block_to_current_stage_proof",
                "status": "applied",
                "stage_id": current_stage_id,
                "commands": current_commands,
                "summary": "Rewrote downstream visual-proof block to the current stage's unambiguous command proof.",
                "next_expected": "harness_command_proof",
            },
        )
    )


def _decision_text(decision) -> str:
    parts = [getattr(decision, "summary", ""), getattr(decision, "rationale", "")]
    payload = getattr(decision, "payload", {}) if isinstance(getattr(decision, "payload", {}), dict) else {}
    parts.append(str(payload.get("reason") or ""))
    log_ref = payload.get("log_ref")
    if isinstance(log_ref, dict):
        parts.append(str(log_ref.get("summary") or ""))
    for key in ("known_gaps", "blockers", "missing_proof"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
    return " ".join(parts).lower()


def _looks_like_downstream_visual_gap(text: str) -> bool:
    if not text:
        return False
    mentions_visual = any(marker in text for marker in ("screenshot", "visual proof", "stage c", "mcp visual", "fullscreen"))
    mentions_missing = any(marker in text for marker in ("missing", "not yet", "required", "before qa", "hand off to qa", "handoff to qa"))
    if not (mentions_visual and mentions_missing):
        return False
    hard_blockers = (
        "environment",
        "crash",
        "failed",
        "failure",
        "cannot run",
        "can't run",
        "unable to run",
        "build error",
        "not installed",
        "not found",
        "permission",
        "timeout",
    )
    return not any(marker in text for marker in hard_blockers)


def _unambiguous_stage_proof_commands(stage) -> list[str]:
    commands = []
    for item in list(getattr(stage, "test_plan", None) or []):
        text = str(item).strip()
        if _looks_like_proof_command(text):
            commands.append(text)
    return commands if len(commands) == 1 else []


def _looks_like_proof_command(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    prefixes = (
        "flutter ",
        "dart ",
        "python ",
        "py ",
        "pytest",
        "powershell",
        "cmd ",
        "npm ",
        "pnpm ",
        "yarn ",
        "node ",
        ".\\",
        "./",
        ".eterniabackendvirtualenv/",
        ".eterniabackendvirtualenv\\",
    )
    return lowered.startswith(prefixes)


def _latest_matching_passed_visual_proof(task: Task, *, proof_store: ProofStore, stage_id: str, target: str, proof_type: str):
    candidates = []
    for proof_id in list(getattr(task, "proof_ids", []) or []):
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        current_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        if current_type != proof_type:
            continue
        if stage_id and str(proof.stage_id or "").strip() != stage_id:
            continue
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if target and str(metadata.get("target") or "").strip().lower() != target:
            continue
        if str(metadata.get("status") or "").strip().lower() != "passed":
            continue
        if getattr(proof, "redaction_status", "") != "safe" or not getattr(proof, "path_or_value", None):
            continue
        candidates.append(proof)
    if not candidates:
        return None
    return max(candidates, key=lambda proof: proof.created_at)


def _latest_stage_change_proof(task: Task, *, proof_store: ProofStore, stage_id: str):
    change_types = {"test_run", "diff", "diff_stat", "commit"}
    candidates = []
    for proof_id in list(getattr(task, "proof_ids", []) or []):
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        if proof_type not in change_types:
            continue
        if stage_id and str(proof.stage_id or "").strip() != stage_id:
            continue
        candidates.append(proof)
    if not candidates:
        return None
    return max(candidates, key=lambda proof: proof.created_at)


def _close_role_session(
    envelope: RoleSessionEnvelope | None,
    *,
    run,
    close_reason: str,
    next_action_before: str | None = None,
    next_action_after: str | None = None,
    proof_ids_added: list[str] | None = None,
    incident_ids_opened: list[str] | None = None,
    would_continue: bool | None = None,
) -> None:
    if envelope is None:
        return
    if envelope.close_reason:
        return
    envelope.close_reason = close_reason
    try:
        EventLog().append(
            Event(
                now(),
                "role_session.closed",
                envelope.task_id,
                run.id,
                envelope.persona_id,
                role_session_payload(
                    envelope,
                    run=run,
                    close_reason=close_reason,
                    next_action_before=next_action_before,
                    next_action_after=next_action_after,
                    proof_ids_added=proof_ids_added,
                    incident_ids_opened=incident_ids_opened,
                    would_continue=would_continue,
                ),
            )
        )
    except Exception:
        return


def _safe_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _runtime_budget_block(task: Task, *, persona_id: str, run_store: RunStore, config: RuntimeConfig) -> dict[str, Any] | None:
    mission_limit = _safe_int(getattr(config, "mission_max_total_tokens", None))
    mission_total = _run_token_total(run_store.list_for_task(task.id))
    stage_id = getattr(task, "current_stage_id", None)
    if mission_limit is not None and mission_limit > 0 and mission_total >= mission_limit:
        return {
            "kind": "mission",
            "event_type": "mission_budget_exceeded",
            "summary": f"Mission token budget exceeded: total_tokens={mission_total}/{mission_limit}",
            "task_id": task.id,
            "persona_id": persona_id,
            "stage_id": stage_id,
            "total_tokens": mission_total,
            "limit": mission_limit,
        }

    swarm = getattr(config, "swarm", None)
    supervision = getattr(config, "supervision", None)
    hierarchical_budget = bool(getattr(supervision, "hierarchical_budget_enabled", False))
    if not bool(getattr(swarm, "enabled", False)) and not hierarchical_budget:
        return None
    swarm_limit = _safe_int(getattr(swarm, "global_token_hard_limit", None))
    swarm_total = _run_token_total(run_store.list_all())
    if swarm_limit is not None and swarm_limit > 0 and swarm_total >= swarm_limit:
        return {
            "kind": "swarm",
            "event_type": "swarm_budget_exceeded",
            "summary": f"Swarm token budget exceeded: total_tokens={swarm_total}/{swarm_limit}",
            "task_id": task.id,
            "persona_id": persona_id,
            "stage_id": stage_id,
            "total_tokens": swarm_total,
            "limit": swarm_limit,
        }
    if hierarchical_budget:
        child_limit = _safe_int(getattr(swarm, "per_lane_token_limit", None)) or swarm_limit
        if child_limit is not None and child_limit > 0:
            child_total = _child_token_total(run_store.list_for_task(task.id), persona_id=persona_id)
            if child_total >= child_limit:
                return {
                    "kind": "swarm",
                    "event_type": "swarm_budget_exceeded",
                    "summary": f"Child token budget exceeded: total_tokens={child_total}/{child_limit}",
                    "task_id": task.id,
                    "persona_id": persona_id,
                    "stage_id": stage_id,
                    "total_tokens": child_total,
                    "limit": child_limit,
                }
    return None


def _deploy_verification_enabled(config: RuntimeConfig) -> bool:
    supervision = getattr(config, "supervision", None)
    return bool(getattr(supervision, "deploy_verification_enabled", False))


def _commit_child_event_offset(action: HarnessAction, *, persona_store: PersonaInstanceStore | None = None) -> bool:
    parent_node_id = str(getattr(action, "parent_node_id", "") or "").strip()
    if not parent_node_id:
        return False
    try:
        offset = int(getattr(action, "child_events_offset", None) or 0)
    except (TypeError, ValueError):
        return False
    if offset <= 0:
        return False
    store = persona_store or PersonaInstanceStore()
    try:
        parent = store.get(parent_node_id)
    except Exception:
        return False
    parent.child_events_offset = max(int(getattr(parent, "child_events_offset", 0) or 0), offset)
    store.update(parent)
    return True


def _first_deploy_contention_warning(store: PersonaAssignmentStore, *, persona_id: str, goal_id: str, enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None
    warnings = store.contention_warnings(persona_id=persona_id, goal_id=goal_id)
    return warnings[0] if warnings else None


def _verify_child_deploy_started(*, config: RuntimeConfig, run, worker, assignment, child_instance_id: str | None) -> str | None:
    if not _deploy_verification_enabled(config):
        return None
    if run is None or getattr(run, "state", None) != RunState.RUNNING:
        return "child run did not enter running state"
    if getattr(run, "last_heartbeat_at", None) is None:
        return "child run has no initial heartbeat"
    if assignment is not None and run.id not in list(getattr(assignment, "run_ids", []) or []):
        return "child assignment did not attach the opened run"
    if worker is not None:
        if getattr(worker, "active_run_id", None) != run.id:
            return "child worker did not attach the opened run"
        if getattr(worker, "last_heartbeat_at", None) is None:
            return "child worker has no initial heartbeat"
    if not child_instance_id:
        return "child persona instance was not created"
    return None


def _child_deploy_failed_result(
    action: HarnessAction,
    *,
    task: Task,
    persona_id: str,
    child_instance_id: str | None,
    reason: str,
    assignment_id: str | None = None,
    run_id: str | None = None,
    retryable: bool = False,
    event_log: EventLog | None = None,
) -> HarnessActionResult:
    if child_instance_id:
        emit_child_deploy_failed(
            child_instance_id=child_instance_id,
            reason=reason,
            task_id=task.id,
            assignment_id=assignment_id,
            stage_id=getattr(action, "stage_id", None) or getattr(task, "current_stage_id", None),
            persona_id=persona_id,
            retryable=retryable,
            summary=reason,
            event_log=event_log or EventLog(),
        )
    return HarnessActionResult(
        action,
        False,
        f"child deploy failed: {reason}",
        {
            "reason": reason,
            "assignment_id": assignment_id,
            "run_id": run_id,
            "persona_id": persona_id,
            "stage_id": getattr(action, "stage_id", None) or getattr(task, "current_stage_id", None),
            "retryable": bool(retryable),
        },
    )


def _task_for_action(task: Task, action: HarnessAction) -> Task:
    action_task = copy.deepcopy(task)
    stage_id = str(getattr(action, "stage_id", "") or "").strip()
    if not stage_id:
        return action_task
    action_task.current_stage_id = stage_id
    plan = getattr(action_task, "mission_plan", None)
    if plan is not None:
        known = {stage.id for stage in list(getattr(plan, "stages", None) or [])}
        if stage_id in known:
            plan.current_stage_id = stage_id
    return action_task


def _swarm_lane_concurrency_enabled(config: RuntimeConfig) -> bool:
    swarm = getattr(config, "swarm", None)
    if not bool(getattr(swarm, "enabled", False)):
        return False
    try:
        from .burn_in import swarm_certification_allows_production

        allowed, _summary = swarm_certification_allows_production(
            allow_uncertified_dev_swarm=bool(getattr(swarm, "allow_uncertified_dev_swarm", False)),
            requires_certification=bool(getattr(swarm, "requires_certification", True)),
        )
        return bool(allowed)
    except Exception:
        return not bool(getattr(swarm, "requires_certification", True))


def _max_active_lanes(config: RuntimeConfig) -> int:
    swarm = getattr(config, "swarm", None)
    try:
        return max(1, int(getattr(swarm, "max_active_lanes", 1) or 1))
    except (TypeError, ValueError):
        return 1


def _run_token_total(runs) -> int:
    total = 0
    for run in runs:
        llm = getattr(run, "llm", None)
        if not isinstance(llm, dict):
            continue
        tokens = _safe_int(llm.get("total_tokens"))
        if tokens is not None and tokens > 0:
            total += tokens
    return total


def _child_token_total(runs, *, persona_id: str) -> int:
    total = 0
    for run in runs:
        if getattr(run, "persona_id", None) != persona_id:
            continue
        llm = getattr(run, "llm", None)
        if not isinstance(llm, dict):
            continue
        tokens = _safe_int(llm.get("total_tokens"))
        if tokens is not None and tokens > 0:
            total += tokens
    return total


def _persona_value(persona, field: str, default):
    value = getattr(persona, field, None)
    return value if value is not None else default


def _persona_int(persona, field: str, default: int) -> int:
    value = _safe_int(getattr(persona, field, None))
    return max(1, value) if value is not None else default


def _apply_deterministic_proof_handoff(task: Task, proof_ids: list[str], decision, *, proof_store: ProofStore, actor: str, run_id: str) -> bool:
    if not proof_ids:
        return False
    proofs = []
    for proof_id in proof_ids:
        try:
            proofs.append(proof_store.get(proof_id))
        except Exception:
            return False
    if any(str(proof.type.value if hasattr(proof.type, "value") else proof.type) != "test_run" for proof in proofs):
        return False

    stage_id = str(decision.payload.get("stage_id") or task.current_stage_id or "").strip()
    acceptable_statuses = {"passed"}
    if _is_red_stage(task, stage_id):
        acceptable_statuses.add("failed")
    if any(str((proof.metadata or {}).get("status", "")).strip() not in acceptable_statuses for proof in proofs):
        return False

    mismatched_labels = _proof_repo_mismatch_labels(task, proofs, actor=actor, stage_id=stage_id)
    if mismatched_labels:
        task.proof_ids = _dedupe(list(getattr(task, "proof_ids", None) or []), proof_ids)
        if stage_id:
            _set_current_stage_id(task, stage_id)
            _set_stage_status(task, stage_id, StageStatus.BLOCKED)
        if "command_proof_repo_mismatch" not in task.risk_flags:
            task.risk_flags.append("command_proof_repo_mismatch")
        task.state = TaskState.RUNNING
        task.updated_at = now()
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "deterministic_proof_handoff",
                    "phase": "proof",
                    "step": "proof_repo_mismatch",
                    "stage_id": stage_id,
                    "proof_ids": proof_ids,
                    "workdir_labels": mismatched_labels,
                    "status": "blocked",
                    "summary": "Passing command proof came from a workdir that does not match the current stage repo intent.",
                    "next_expected": "neko_self_heal_or_corrected_dev_proof",
                },
            )
        )
        return False

    command_mismatches = _proof_command_stage_mismatch_labels(task, proofs, stage_id=stage_id)
    if command_mismatches:
        task.proof_ids = _dedupe(list(getattr(task, "proof_ids", None) or []), proof_ids)
        target_stage_id = _proof_command_stage_mismatch_target_stage_id(task, proofs, stage_id=stage_id) or stage_id
        if target_stage_id:
            _set_current_stage_id(task, target_stage_id)
            _set_stage_status(task, target_stage_id, StageStatus.IMPLEMENTING)
            if stage_id and stage_id != target_stage_id:
                stage = _stage_for_command_proof(task, stage_id)
                if stage is not None and stage.status == StageStatus.IMPLEMENTING:
                    _set_stage_status(task, stage_id, StageStatus.BLOCKED)
        if "command_proof_stage_mismatch" not in task.risk_flags:
            task.risk_flags.append("command_proof_stage_mismatch")
        task.state = TaskState.RUNNING
        task.updated_at = now()
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "deterministic_proof_handoff",
                    "phase": "proof",
                    "step": "proof_command_stage_mismatch",
                    "stage_id": stage_id,
                    "corrected_current_stage_id": target_stage_id,
                    "proof_ids": proof_ids,
                    "commands": command_mismatches,
                    "status": "needs_corrected_proof",
                    "summary": "Passing command proof did not satisfy the current stage proof contract.",
                    "next_expected": "corrected_stage_proof",
                },
            )
        )
        return False

    if stage_id:
        _set_current_stage_id(task, stage_id)
        _set_stage_status(task, stage_id, StageStatus.READY_FOR_QA)
    task.risk_flags = [
        flag
        for flag in (getattr(task, "risk_flags", None) or [])
        if flag
        not in {
            "command_proof_stage_mismatch",
            "command_proof_repo_mismatch",
        }
    ]
    if not _all_stages_dev_complete(task):
        if _needs_sequential_specialist_join(task):
            task.state = TaskState.RUNNING
        else:
            _advance_to_next_dev_stage(task)
            task.state = TaskState.RUNNING
    else:
        task.state = TaskState.RUNNING
    waits_for_launcher_join = task.state == TaskState.RUNNING and _needs_cross_stack_launcher_completion(task, proof_store=proof_store)
    contract_packet_id = None
    if waits_for_launcher_join:
        contract_packet_id = _ensure_backend_contract_packet_for_handoff(
            task,
            proof_ids,
            decision,
            actor=actor,
            run_id=run_id,
            stage_id=stage_id,
        )
        handoff_status = "backend_join_ready"
        handoff_summary = "Passing backend command proof attached; routed to Neko for Launcher join release without another Backend Dev tick."
        next_expected = "neko_cross_stack_launcher_release"
    elif task.state == TaskState.RUNNING:
        handoff_status = "ready_for_qa"
        handoff_summary = "Passing command proof attached; routed to QA without another Dev model tick."
        next_expected = "qa_verification"
    else:
        handoff_status = "next_stage_ready"
        handoff_summary = "Passing command proof attached; advanced to the next implementation stage."
        next_expected = "dev_next_stage"
    EventLog().append(
        Event(
            ts=now(),
            type="task.transition",
            task_id=task.id,
            run_id=run_id,
            persona_id=actor,
            payload={
                "source": "deterministic_proof_handoff",
                "phase": "handoff",
                "step": "deterministic_proof_handoff",
                "status": handoff_status,
                "summary": handoff_summary,
                "proof_count": len(proof_ids),
                "stage_id": stage_id,
                "next_expected": next_expected,
                "contract_packet_id": contract_packet_id,
                "to": task.state.value,
            },
        )
    )
    return True


def _ensure_backend_contract_packet_for_handoff(task: Task, proof_ids: list[str], decision, *, actor: str, run_id: str, stage_id: str) -> str | None:
    if actor != "backend_dev" or not proof_ids:
        return None
    if _has_backend_contract_delivery_packet(task):
        return None
    proof_id = str(proof_ids[0]).strip()
    if not proof_id:
        return None
    safe_stage = (stage_id or "backend_contract").replace(" ", "_")
    contract_packet_id = f"backend_contract_packet_{task.id}_{safe_stage}"
    source_packet = latest_packet(task.id, "handoff_packet")
    source_packet_id = str((source_packet or {}).get("packet_id") or "").strip()
    body = {
        "source_handoff_packet_id": source_packet_id,
        "consumed_contract_packet_ids": [],
        "consumed_proof_ids": [],
        "produced_contract_packet_id": contract_packet_id,
        "contract_packet": {
            "contract_packet_id": contract_packet_id,
            "surface": "Harness-owned backend_contract_smoke handoff",
            "contract_status": "tested",
            "request_shape": {
                "repo_scope": "EterniaBackend",
                "required_recipe_id": "backend_contract_smoke",
                "mode": "no_product_edit",
            },
            "response_shape": {
                "required_backend_proof_id": proof_id,
                "required_backend_proof_status": "passed",
                "next_handoff_packet_kind": "contract_join",
                "next_target_repo": "EterniaLauncher",
            },
            "error_shape": {
                "missing_backend_proof": "block Launcher release until backend_contract_smoke proof passes",
                "premature_qa": "block until backend and Launcher proof IDs are both attached",
            },
            "example_response": {
                "proof_id": proof_id,
                "recipe_id": "backend_contract_smoke",
                "status": "passed",
            },
        },
        "proof_ids": [proof_id],
        "proof_summary": "backend_contract_smoke passed; no product edits certified by Harness proof recipe",
        "command_summary": "Harness-owned backend_contract_smoke proof command passed",
        "known_gaps": [],
        "next_owner": "neko_supervisor",
        "operator_note": "Synthesized by Harness after backend proof to avoid a second Backend Dev packet-only turn.",
    }
    log = EventLog()
    packet = make_packet(task=task, decision=decision, packet_type="delivery", body=body, actor=actor, run_id=run_id, stage_id=stage_id)
    if record_packet(packet, event_log=log):
        log.append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "deterministic_proof_handoff",
                    "phase": "handoff",
                    "step": "backend_contract_packet_synthesized",
                    "status": "recorded",
                    "stage_id": stage_id,
                    "proof_id": proof_id,
                    "contract_packet_id": contract_packet_id,
                    "summary": "Harness synthesized backend delivery packet from passed backend_contract_smoke proof before Neko Launcher join.",
                    "next_expected": "neko_cross_stack_launcher_release",
                },
            )
        )
    return contract_packet_id


def _record_command_proof_self_heal(
    task: Task,
    proof_ids: list[str],
    *,
    proof_store: ProofStore,
    stage_id: str | None,
    actor: str,
    run_id: str,
) -> None:
    if not proof_ids:
        return
    failed_ids: list[str] = []
    passed_ids: list[str] = []
    environment_status: str | None = None
    for proof_id in proof_ids:
        try:
            proof = proof_store.get(proof_id)
        except Exception:
            continue
        metadata = proof.metadata or {}
        status = str(metadata.get("status") or "").strip().lower()
        if status == "failed":
            failed_ids.append(proof_id)
        elif status == "passed":
            passed_ids.append(proof_id)
        if metadata.get("environment_fingerprint_status"):
            environment_status = str(metadata.get("environment_fingerprint_status")).strip()[:80]
    if not failed_ids and not passed_ids:
        return

    root = dict(getattr(task, "harness_self_heal", {}) or {})
    stages = dict(root.get("stages") or {})
    key = stage_id or getattr(task, "current_stage_id", None) or "_mission"
    state = dict(stages.get(key) or {})
    counters = dict(state.get("counters") or {})

    if failed_ids:
        previous_failed = [str(item).strip() for item in (state.get("last_failed_proof_ids") or []) if str(item).strip()]
        new_failed = [proof_id for proof_id in failed_ids if proof_id not in previous_failed]
        if previous_failed and new_failed:
            counters["same_stage_retry_count"] = (_safe_int(counters.get("same_stage_retry_count")) or 0) + 1
        state["last_failed_proof_ids"] = _dedupe(previous_failed, failed_ids)[-20:]
        if environment_status:
            state["environment_fingerprint_status"] = environment_status
        if counters:
            state["counters"] = counters
        stages[key] = state
        current_key = getattr(task, "current_stage_id", None)
        if current_key and current_key != key:
            stages[current_key] = dict(state)
        root["stages"] = stages
        task.harness_self_heal = root
        EventLog().append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task.id,
                run_id=run_id,
                persona_id=actor,
                payload={
                    "type": "run.progress",
                    "source": "command_proof_self_heal",
                    "phase": "proof",
                    "step": "failed_proof_recorded",
                    "stage_id": key,
                    "status": "failed",
                    "last_failed_proof_ids": list(state["last_failed_proof_ids"]),
                    "same_stage_retry_count": counters.get("same_stage_retry_count", 0),
                    "summary": "Failed command proof recorded for bounded retry or Neko self-heal.",
                    "next_expected": "dev_bounded_retry" if not counters.get("same_stage_retry_count") else "neko_self_heal",
                },
            )
        )
        return

    if passed_ids:
        changed = False
        if state.get("last_failed_proof_ids"):
            state.pop("last_failed_proof_ids", None)
            changed = True
        if counters.get("same_stage_retry_count"):
            counters.pop("same_stage_retry_count", None)
            changed = True
        if environment_status:
            state["environment_fingerprint_status"] = environment_status
            changed = True
        if counters:
            state["counters"] = counters
        elif "counters" in state:
            state.pop("counters", None)
        if changed:
            stages[key] = state
            root["stages"] = stages
            task.harness_self_heal = root


def _record_failed_proof_block_after_reuse(task: Task, decision, *, actor: str, run_id: str) -> bool:
    if decision.type != DecisionType.BLOCK:
        return False
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    proof_ids = [str(item).strip() for item in (payload.get("failed_proof_ids") or []) if str(item).strip()] if isinstance(payload.get("failed_proof_ids"), list) else []
    if not proof_ids:
        root = getattr(task, "harness_self_heal", {}) or {}
        stages = root.get("stages") if isinstance(root, dict) else {}
        state = stages.get(getattr(task, "current_stage_id", None) or "_mission") if isinstance(stages, dict) else {}
        proof_ids = [str(item).strip() for item in (state.get("last_failed_proof_ids") or []) if str(item).strip()] if isinstance(state, dict) else []
    if not proof_ids:
        return False

    root = dict(getattr(task, "harness_self_heal", {}) or {})
    stages = dict(root.get("stages") or {})
    key = getattr(task, "current_stage_id", None) or "_mission"
    state = dict(stages.get(key) or {})
    counters = dict(state.get("counters") or {})
    counters["same_stage_retry_count"] = max(1, (_safe_int(counters.get("same_stage_retry_count")) or 0) + 1)
    state["last_failed_proof_ids"] = _dedupe([str(item).strip() for item in (state.get("last_failed_proof_ids") or []) if str(item).strip()], proof_ids)[-20:]
    if "environment_fingerprint_status" not in state:
        state["environment_fingerprint_status"] = "unchanged"
    state["counters"] = counters
    stages[key] = state
    root["stages"] = stages
    task.harness_self_heal = root
    EventLog().append(
        Event(
            ts=now(),
            type="run.progress",
            task_id=task.id,
            run_id=run_id,
            persona_id=actor,
            payload={
                "type": "run.progress",
                "source": "command_proof_self_heal",
                "phase": "self_heal",
                "step": "failed_proof_block_recorded",
                "stage_id": key,
                "status": "blocked",
                "last_failed_proof_ids": list(state["last_failed_proof_ids"]),
                "same_stage_retry_count": counters["same_stage_retry_count"],
                "summary": "Dev blocked after reusing a failed proof without an environment change; route Neko self-heal before another same-stage Dev run.",
                "next_expected": "neko_self_heal",
            },
        )
    )
    return True


def _proof_repo_mismatch_labels(task: Task, proofs: list, *, actor: str, stage_id: str) -> list[str]:
    intent = _command_proof_repo_intent(task, actor=actor, stage_id=stage_id)
    if intent is None:
        return []
    mismatches: list[str] = []
    for proof in proofs:
        metadata = proof.metadata or {}
        label = str(metadata.get("workdir_label") or "").strip()
        if not label:
            continue
        if _workdir_label_conflicts_intent(label, intent):
            mismatches.append(label[:120])
    return mismatches


def _workdir_label_conflicts_intent(label: str, intent: str) -> bool:
    if _repo_text_matches_intent(label, intent):
        return False
    return any(
        other != intent and _repo_text_matches_intent(label, other)
        for other in ("backend", "launcher", "harness")
    )


def _proof_command_stage_mismatch_labels(task: Task, proofs: list, *, stage_id: str) -> list[str]:
    incomplete_product_stage = first_incomplete_product_edit_stage(task, excluding_stage_id=stage_id)
    current_stage = _stage_for_command_proof(task, stage_id)
    if incomplete_product_stage is not None or (current_stage is not None and stage_requires_product_edit(task, current_stage)):
        mismatches = []
        for proof in proofs:
            if _proof_is_no_product_edit_smoke(proof):
                metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
                command = str(metadata.get("command") or "").strip()
                recipe = str(metadata.get("proof_recipe_recipe_id") or "").strip()
                mismatches.append(
                    f"{recipe or 'no_product_edit_smoke'}:{command[:220] or '<missing command>'}"
                )
        if mismatches:
            return mismatches
    if not _stage_requires_bridge_archive_regression(task, stage_id):
        return []
    mismatches: list[str] = []
    for proof in proofs:
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        command = str(metadata.get("command") or "").strip()
        normalized = command.lower().replace("\\", "/")
        if (
            "mission_control_bridge_test.dart" in normalized
            and "mission_control_snapshot_test.dart" in normalized
        ):
            continue
        mismatches.append(command[:240] or "<missing command>")
    return mismatches


def _proof_command_stage_mismatch_target_stage_id(task: Task, proofs: list, *, stage_id: str) -> str | None:
    if not any(_proof_is_no_product_edit_smoke(proof) for proof in proofs):
        return None
    target = first_incomplete_product_edit_stage(task, excluding_stage_id=stage_id)
    return target.id if target is not None else None


def _proof_is_no_product_edit_smoke(proof) -> bool:
    metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
    recipe_id = str(metadata.get("proof_recipe_recipe_id") or "").strip()
    if not recipe_id:
        recipe = metadata.get("proof_recipe")
        if isinstance(recipe, dict):
            recipe_id = str(recipe.get("recipe_id") or "").strip()
    if no_product_edit_recipe_id(recipe_id):
        return True
    command = str(metadata.get("command") or "").strip().lower()
    stdout = str(metadata.get("stdout_excerpt") or metadata.get("stdout") or "").strip().lower()
    return any(
        marker in f"{command}\n{stdout}"
        for marker in (
            "launcher_contract_smoke",
            "backend_contract_smoke",
            "archive_button_cli_contract",
            "harness_runtime_status_snapshot",
            "qa_release_verdict_smoke",
        )
    )


def _stage_requires_bridge_archive_regression(task: Task, stage_id: str) -> bool:
    stage = _stage_for_command_proof(task, stage_id)
    if stage is None:
        return False
    text = " ".join(
        [
            str(stage.id or ""),
            str(stage.title or ""),
            str(stage.objective or ""),
            " ".join(str(item) for item in (stage.acceptance_criteria or [])),
            " ".join(str(item) for item in (stage.test_plan or [])),
        ]
    ).lower().replace("_", "-")
    return (
        "mission control" in text
        and ("bridge" in text or "snapshot" in text or "archive" in text)
        and ("regression" in text or "test" in text or "coverage" in text)
    )


def _is_red_stage(task: Task, stage_id: str) -> bool:
    for stage in _runtime_stage_records(task):
        if stage.id == stage_id:
            text = " ".join(str(value or "") for value in (stage.id, stage.title, stage.objective)).lower()
            return any(marker in text for marker in ("red", "failing test", "prove tests fail"))
    return False


def _is_retryable_provider_failure(kind: str, exc: Exception) -> bool:
    text = str(exc).lower()
    if kind in {"provider_auth_failure", "runtime_dependency_missing", "model_invalid_output", "tool_policy_violation"}:
        return False
    if kind == "provider_rate_limit":
        return True
    if kind == "provider_failure":
        return any(marker in text for marker in ("ttfb", "first byte", "no bytes", "timeout", "timed out", "temporarily", "connection reset", "server error", "http 5"))
    return False


def _latest_model_invalid_repair_error(
    run_store: RunStore,
    *,
    task_id: str,
    persona_id: str,
    stage_id: str | None,
) -> str | None:
    try:
        runs = run_store.list_for_task(task_id)
    except Exception:
        return None
    candidates = [
        run
        for run in runs
        if run.persona_id == persona_id
        and run.stage_id == stage_id
        and run.state == RunState.FAILED
    ]
    if not candidates and stage_id:
        candidates = [
            run
            for run in runs
            if run.persona_id == persona_id
            and run.stage_id is None
            and run.state == RunState.FAILED
        ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda run: run.finished_at or run.last_heartbeat_at or run.started_at)
    llm = latest.llm if isinstance(latest.llm, dict) else {}
    if llm.get("validation_status") != "invalid":
        return None
    progress = latest.progress if isinstance(latest.progress, dict) else {}
    error = latest.error if isinstance(latest.error, dict) else {}
    if progress.get("approved_for_continuation") is True or error.get("approved_for_continuation") is True:
        return None
    message = str(error.get("message") or error.get("summary") or "").strip()
    if not message:
        return None
    return _safe_repair_error(message)


_DECISION_REPAIR_MAX_ATTEMPTS = 1


def _should_retry_invalid_decision(exc: DecisionPayloadInvalid, *, repair_attempts: int) -> bool:
    if repair_attempts >= _DECISION_REPAIR_MAX_ATTEMPTS:
        return False
    text = str(exc).strip().lower()
    if not text:
        return False
    if "dev stage plan loop guard failed" in text:
        return False
    return True


def _is_dev_stage_plan_loop_guard(exc: Exception) -> bool:
    if not isinstance(exc, DecisionPayloadInvalid):
        return False
    return "dev stage plan loop guard failed" in str(exc).strip().lower()


def _decision_repair_feedback(
    exc: DecisionPayloadInvalid,
    *,
    decision,
    repair_attempt: int,
) -> str:
    message = _safe_repair_error(str(exc)) or "decision failed Harness contract validation"
    payload: dict[str, Any] = {
        "message": message,
        "decision_type": getattr(getattr(decision, "type", None), "value", None) or str(getattr(decision, "type", "")),
        "repair_attempt": repair_attempt,
        "max_repair_attempts": _DECISION_REPAIR_MAX_ATTEMPTS,
    }
    invalid_field = _invalid_field_for_repair_message(message)
    if invalid_field:
        payload["invalid_field"] = invalid_field
        invalid_value = _extract_invalid_decision_value(decision, invalid_field)
        if invalid_value is not None:
            payload["invalid_value"] = _safe_value_preview(invalid_value)
    return json.dumps({key: value for key, value in payload.items() if value is not None}, sort_keys=True)


def _record_decision_repair_request(
    run_store: RunStore,
    task: Task,
    run,
    *,
    persona_id: str,
    exc: DecisionPayloadInvalid,
    decision,
    repair_attempt: int,
    worker_store=None,
    worker=None,
):
    repair_error = _decision_repair_feedback(exc, decision=decision, repair_attempt=repair_attempt)
    _capture_replay_scenario(task, run, persona_id=persona_id, exc=exc, decision=decision)
    run = _refresh_run_for_update(run_store, run)
    repair_payload = _decision_repair_progress_payload(repair_error, repair_attempt=repair_attempt)
    run.progress = {**(run.progress or {}), **repair_payload}
    run.llm = {
        **(run.llm or {}),
        "validation_status": "repair_requested",
        "last_validation_error": _safe_repair_error(str(exc)),
        "schema_repair_attempts": repair_attempt,
    }
    run_store.update(run)
    EventLog().append(Event(now(), "run.progress", task.id, run.id, persona_id, repair_payload))
    if worker_store is not None and worker is not None:
        worker_store.heartbeat(worker.id)
    return run, repair_error


def _capture_replay_scenario(task, run, *, persona_id: str, exc: DecisionPayloadInvalid, decision) -> None:
    """Auto-capture every live contract failure as a replay scenario candidate."""
    try:
        from .replay_scenarios import classify_failure_origin, record_scenario_candidate

        payload = getattr(decision, "payload", None) if decision is not None else None
        record_scenario_candidate(
            task_id=getattr(task, "id", ""),
            run_id=getattr(run, "id", None),
            persona_id=persona_id,
            decision_type=getattr(getattr(decision, "type", None), "value", None),
            payload=payload,
            error_class=type(exc).__name__,
            error_message=_safe_repair_error(str(exc)) or str(exc),
            failure_origin=classify_failure_origin(task=task, run=run, payload=payload, error_message=str(exc)),
        )
    except Exception:
        # Scenario capture is observability, never a reason to fail the repair path.
        pass


def _decision_repair_progress_payload(repair_error: str | None, *, repair_attempt: int) -> dict[str, Any]:
    try:
        parsed = json.loads(repair_error or "{}")
    except Exception:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    payload: dict[str, Any] = {
        "type": "run.progress",
        "source": "decision_contract_repair",
        "phase": "contract_repair",
        "step": "decision_validation_failed",
        "status": "repair_requested",
        "summary": str(parsed.get("message") or repair_error or "decision failed Harness contract validation")[:500],
        "repair_attempt": repair_attempt,
        "max_repair_attempts": _DECISION_REPAIR_MAX_ATTEMPTS,
        "next_expected": "corrected_agent_decision",
    }
    for key in ("decision_type", "invalid_field", "invalid_value"):
        if parsed.get(key) is not None:
            payload[key] = parsed[key]
    return payload


def _invalid_field_for_repair_message(message: str) -> str | None:
    text = str(message or "").lower()
    if "request_screenshot" in text or "request_video" in text or "mcp_server" in text or "required_launch_pins" in text:
        if "mcp_server" in text:
            return "payload.mcp_server"
        if "required_launch_pins.hermes_profile" in text or "hermes_profile" in text:
            return "payload.required_launch_pins.hermes_profile"
        if "required_launch_pins.runtime_root_id" in text or "runtime_root_id" in text:
            return "payload.required_launch_pins.runtime_root_id"
        if "required_launch_pins" in text:
            return "payload.required_launch_pins"
        if "proof_requirement" in text:
            return "payload.proof_requirement"
        if "target" in text and "target_repo" not in text:
            return "payload.target"
        if "stage_id" in text:
            return "payload.stage_id"
        return "payload"
    if "missing payload keys" in text and "target" in text and "target_repo" not in text:
        return "payload.target"
    if "delivery.next_owner" in text:
        return "payload.delivery.next_owner"
    if "delivery.work_status" in text:
        return "payload.delivery.work_status"
    if "qa_review.coverage" in text:
        return "payload.qa_review.coverage"
    if "qa_review.next_owner" in text:
        return "payload.qa_review.next_owner"
    if "handoff_packet." in text:
        for field in ("target_owner", "next_owner", "final_owner", "target_repo", "next_repo", "final_repo", "handoff_mode"):
            if f"handoff_packet.{field}" in text:
                return f"payload.handoff_packet.{field}"
        return "payload.handoff_packet"
    if "proof_ids" in text:
        return "payload.proof_ids"
    if "recipe_id" in text:
        return "payload.recipe_id"
    if "commands" in text or "proof command" in text or "proof policy" in text:
        return "payload.commands"
    return None


def _extract_invalid_decision_value(decision, invalid_field: str) -> Any:
    payload = getattr(decision, "payload", None)
    if not isinstance(payload, dict):
        return None
    path = invalid_field
    if path.startswith("payload."):
        path = path[len("payload.") :]
    current: Any = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _safe_value_preview(value: Any) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_value_preview(item) for item in value[:8]]
    if isinstance(value, dict):
        return {str(key)[:80]: _safe_value_preview(val) for key, val in list(value.items())[:8]}
    text = " ".join(str(value).split())
    if not text:
        return ""
    lowered = text.lower()
    if ":/" in text or "\\" in text or "token" in lowered or "secret" in lowered or "password" in lowered:
        return "<redacted>"
    return text[:160]


def _safe_repair_error(message: str) -> str | None:
    text = " ".join(message.split())
    if not text:
        return None
    if ":/" in text or "\\" in text:
        return "previous decision failed contract validation with path-like content; return a redaction-safe AgentDecision"
    return text[:500]


def _current_stage(task: Task):
    if not getattr(task, "current_stage_id", None):
        return None
    return next((stage for stage in _runtime_stage_records(task) if stage.id == task.current_stage_id), None)


def _is_live_persona_runtime(runtime) -> bool:
    return runtime is not None and runtime.__class__.__name__ == "GPTPersonaRuntime"


def _enterprise_worker_sessions_enabled(config: RuntimeConfig) -> bool:
    enterprise = getattr(config, "enterprise_worker_sessions", None)
    if enterprise is None:
        return False
    return bool(getattr(enterprise, "enabled", False) and getattr(enterprise, "worker_session_store", True))


def _supported_runner_kwargs(callable_obj, optional: dict) -> dict:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return {key: value for key, value in optional.items() if value is not None}
    parameters = signature.parameters
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values())
    if accepts_kwargs:
        return {key: value for key, value in optional.items() if value is not None}
    return {key: value for key, value in optional.items() if value is not None and key in parameters}


def _proof_intent_from_decision(decision) -> str:
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    explicit = payload.get("proof_intent") or payload.get("intent")
    command_count = len(payload.get("commands") or []) if isinstance(payload.get("commands"), list) else 0
    stage_id = str(payload.get("stage_id") or "").strip()
    if explicit:
        return _safe_metadata_text(explicit)
    summary = str(getattr(decision, "summary", "") or "collect deterministic command proof").strip()
    return _safe_metadata_text(f"{summary}; command_count={command_count}; stage_id={stage_id or 'current'}")


def _environment_fingerprint_payload(task: Task, stage_id: str | None) -> dict:
    state = _task_stage_self_heal_state(task, stage_id)
    fingerprint = state.get("last_environment_fingerprint") or state.get("environment_fingerprint")
    status = state.get("environment_fingerprint_status")
    safe_status = _safe_metadata_token(status) or ("recorded" if fingerprint else "unknown")
    return {
        "environment_fingerprint": _safe_metadata_text(fingerprint or safe_status),
        "environment_fingerprint_status": safe_status,
    }


def _task_stage_self_heal_state(task: Task, stage_id: str | None) -> dict:
    root = getattr(task, "harness_self_heal", None)
    if not isinstance(root, dict):
        return {}
    stages = root.get("stages") if isinstance(root.get("stages"), dict) else root
    if not isinstance(stages, dict):
        return {}
    state = stages.get(stage_id or getattr(task, "current_stage_id", None) or "_mission")
    return state if isinstance(state, dict) else {}


def _safe_metadata_text(value) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "unknown"
    if ":/" in text or "\\" in text or text.startswith(("/", "~")):
        return "redacted_path_like_value"
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret=", "token=", "password=", "api_key=", "apikey=", "bearer ")):
        return "redacted_sensitive_value"
    return text[:500]


def _safe_metadata_token(value) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"unknown", "recorded", "changed", "unchanged", "blocked", "missing"}:
        return text
    return None


def _refresh_run_for_update(run_store: RunStore, source) -> Any:
    try:
        run = run_store.get(source.id)
    except Exception:
        return source
    if isinstance(getattr(source, "llm", None), dict):
        timing = _safe_timing_map(run.llm)
        timing.update(_safe_timing_map(source.llm))
        run.llm = {**(run.llm or {}), **source.llm}
        if timing:
            run.llm["timing"] = timing
    safe_session_id = _safe_session_id(getattr(source, "session_id", None))
    if safe_session_id:
        run.session_id = safe_session_id
    return run


def _record_failed_proof_auto_attachment(run, task: Task, decision, *, actor: str) -> bool:
    payload = decision.payload if isinstance(getattr(decision, "payload", None), dict) else {}
    if payload.get("failed_proof_auto_attached") is not True:
        return False
    proof_ids = [str(item).strip() for item in (payload.get("failed_proof_ids") or []) if str(item).strip()] if isinstance(payload.get("failed_proof_ids"), list) else []
    if not proof_ids:
        return False
    progress = dict(getattr(run, "progress", None) or {})
    progress["failed_proof_reused"] = True
    progress["failed_proof_auto_attached"] = True
    progress["last_failed_proof_ids"] = proof_ids
    run.progress = progress
    EventLog().append(
        Event(
            ts=now(),
            type="run.progress",
            task_id=task.id,
            run_id=run.id,
            persona_id=actor,
            payload={
                "type": "run.progress",
                "source": "dev_progress_gate",
                "phase": "self_heal",
                "step": "failed_proof_auto_attached",
                "status": "ready",
                "stage_id": getattr(task, "current_stage_id", None) or getattr(run, "stage_id", None),
                "failed_proof_ids": proof_ids,
                "summary": "Attached known failed proof IDs to the Dev retry decision before proof execution.",
                "next_expected": "bounded_retry_or_neko_self_heal",
            },
        )
    )
    return True


def _attach_stage_self_heal_to_run_progress(run, task: Task) -> None:
    root = getattr(task, "harness_self_heal", {}) or {}
    stages = root.get("stages") if isinstance(root, dict) else {}
    if not isinstance(stages, dict):
        return
    state = stages.get(getattr(task, "current_stage_id", None) or "_mission")
    if not isinstance(state, dict):
        return
    progress = dict(run.progress or {})
    for key in ("last_failed_proof_ids", "environment_fingerprint_status"):
        if key in state:
            progress[key] = state[key]
    if isinstance(state.get("self_heal"), dict) and state["self_heal"].get("attempt_number"):
        progress["self_heal_applied"] = True
    run.progress = progress


def _budget_approval_incidents_for_task(incidents: list[Incident], run_store: RunStore, *, cap: int = 2) -> list[Incident]:
    """Return budget incidents that Neko can safely steer into same-session continuation."""
    return eligible_budget_approval_incidents(incidents, run_store, cap=cap)


def _budget_scope_recovery_incidents_for_task(incidents: list[Incident], run_store: RunStore) -> list[Incident]:
    """Return budget incidents where Neko should split scope instead of approving continuation."""
    return [incident for incident in incidents if budget_incident_needs_scope_recovery(incident, run_store)]


def _hard_environment_blocker_incidents(incidents: list[Incident]) -> list[Incident]:
    return [incident for incident in incidents if str(getattr(incident, "kind", "") or "") == "environment_blocker"]


def choose_next_action(task: Task) -> HarnessAction:
    return MissionStateMachine().next_action(task)


def _persona_id_for_harness_action(action: HarnessAction, *, task: Task | None = None, config: RuntimeConfig | None = None, run_store: RunStore | None = None) -> str | None:
    if action.type == HarnessActionType.RUN_SLOT:
        slot_id = str(action.slot_id or "").strip()
        plan = getattr(task, "mission_plan", None) if task is not None else None
        bindings = getattr(plan, "bindings", None) if plan is not None else None
        if isinstance(bindings, dict) and slot_id:
            resolved = str(bindings.get(slot_id) or "").strip()
            if resolved:
                return resolved
        if slot_id == "dev":
            return _dev_persona_id_for_task(task, config=config, run_store=run_store)
        return slot_id or None
    return None


def _spawned_by_for_harness_action(action: HarnessAction, *, task: Task | None = None) -> str | None:
    if action.type != HarnessActionType.RUN_SLOT:
        return None
    slot_id = str(action.slot_id or "").strip()
    plan = getattr(task, "mission_plan", None) if task is not None else None
    bindings = getattr(plan, "bindings", None) if plan is not None else None
    if slot_id in {"lead", "neko_supervisor"}:
        return "operator"
    if isinstance(bindings, dict):
        lead = str(bindings.get("lead") or bindings.get("coordinator") or "").strip()
        if lead:
            return lead
    return "neko_supervisor"


def _persona_id_for_action(action_type: HarnessActionType, *, task: Task | None = None, config: RuntimeConfig | None = None, run_store: RunStore | None = None) -> str | None:
    return None


def _action_targets(action: HarnessAction, *slot_ids: str) -> bool:
    return action.type == HarnessActionType.RUN_SLOT and str(action.slot_id or "").strip() in set(slot_ids)


def _dev_persona_id_for_task(task: Task | None, *, config: RuntimeConfig | None = None, run_store: RunStore | None = None) -> str:
    """Choose the narrowest configured Dev specialist for a task's repo scope."""
    if task is None:
        return "dev"
    continuation_persona_id = _approved_continuation_persona_id(task, run_store)
    if continuation_persona_id:
        return continuation_persona_id
    typed_stage = current_plan_stage(task)
    if typed_stage is not None and typed_stage.owner in {"dev", "backend_dev"}:
        return typed_stage.owner
    stage_persona_id = _dev_persona_id_from_current_stage(task)
    if stage_persona_id:
        return stage_persona_id
    handoff_persona_id = _dev_persona_id_from_latest_handoff(task)
    if handoff_persona_id:
        return handoff_persona_id
    labels = {str(label).strip().lower() for label in safe_affected_repo_labels(list(getattr(task, "affected_repos", []) or []))}
    raw_repos = {str(repo).strip().lower() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()}
    haystack = " ".join(sorted(labels | raw_repos))
    if "eterniabackend" in haystack or "eternia-backend" in haystack or "backend" in haystack:
        return "backend_dev"

    cfg = config if hasattr(config, "personas") else load_agent_runtime_config()
    try:
        personas = ensure_persisted_personas(cfg)
    except Exception:
        personas = []
    for persona in personas:
        if str(getattr(persona, "role", "")) != "dev" or persona.id == "dev":
            continue
        repo_label = str(getattr(persona, "repo_scope_label", "") or "").strip().lower()
        repo_scope = str(getattr(persona, "repo_scope", "") or "").strip().lower()
        if repo_label and repo_label in labels:
            return persona.id
        if repo_scope and any(repo_scope in raw or raw in repo_scope for raw in raw_repos):
            return persona.id
    return "dev"


def _dev_persona_id_from_current_stage(task: Task) -> str | None:
    stage_id = str(getattr(task, "current_stage_id", "") or "").strip().lower()
    stage = _current_stage(task)
    title = str(getattr(stage, "title", "") or "").strip().lower()
    objective = str(getattr(stage, "objective", "") or "").strip().lower()
    affected_paths = [str(item).lower() for item in (getattr(stage, "affected_paths", None) or [])]
    test_plan = [str(item).lower() for item in (getattr(stage, "test_plan", None) or [])]
    haystack = " ".join(
        [
            stage_id,
            title,
            objective,
            " ".join(affected_paths),
            " ".join(test_plan),
        ]
    )
    backend_identity = (
        "backend" in stage_id
        or title.startswith("backend")
        or "eterniabackend" in haystack
        or any("eterniabackend" in path or "eternia-backend" in path for path in affected_paths)
        or any("scripts/test.sh" in item or "manage.py" in item for item in test_plan)
    )
    if backend_identity:
        return "backend_dev"
    if any(marker in haystack for marker in ("launcher", "frontend", "front-end", "ui", "eternialauncher")):
        return "dev"
    return None


def _dev_persona_id_from_latest_handoff(task: Task) -> str | None:
    try:
        packet = latest_packet(task.id, "handoff_packet", stage_id=getattr(task, "current_stage_id", None))
    except Exception:
        return None
    body = packet.get("body") if isinstance(packet, dict) else None
    if not isinstance(body, dict):
        return None
    target_dev_persona = str(body.get("target_dev_persona") or "").strip()
    if target_dev_persona in {"dev", "backend_dev"}:
        return target_dev_persona
    target_owner = str(body.get("target_owner") or "").strip()
    target_repo = str(body.get("target_repo") or "").strip()
    if target_owner in {"dev", "backend_dev"}:
        if target_owner == "dev" and target_repo == "EterniaLauncher":
            return "dev"
        if target_owner == "backend_dev" or target_repo == "EterniaBackend":
            return "backend_dev"
    return None


def _approved_continuation_persona_id(task: Task, run_store: RunStore | None) -> str | None:
    if run_store is None:
        return None
    try:
        runs = run_store.list_for_task(task.id)
    except Exception:
        return None
    dev_runs = [
        run for run in runs
        if run.stage_id == task.current_stage_id
        and (run.persona_id == "dev" or str(run.persona_id).endswith("_dev"))
    ]
    dev_runs.sort(key=_run_order_key)
    for run in reversed(dev_runs):
        if (
            run.state == RunState.FAILED
            and isinstance(run.error, dict)
            and run.error.get("type") == RUN_BUDGET_EXCEEDED
            and run.error.get("approved_for_continuation")
            and _safe_session_id(run.session_id)
        ):
            if _has_later_persona_run(dev_runs, run):
                continue
            return run.persona_id
    return None


def _has_later_persona_run(runs: list, candidate) -> bool:
    candidate_key = _run_order_key(candidate)
    return any(
        run.persona_id == candidate.persona_id
        and _run_order_key(run) > candidate_key
        for run in runs
    )


def _run_order_key(run) -> tuple[str, str]:
    return (
        str(getattr(run, "started_at", "") or ""),
        str(getattr(run, "id", "") or ""),
    )


def _approved_continuation_count(task: Task, run_store: RunStore | None, persona_id: str) -> int:
    if run_store is None:
        return 0
    try:
        runs = run_store.list_for_task(task.id)
    except Exception:
        return 0
    return sum(
        1
        for run in runs
        if run.stage_id == task.current_stage_id
        and run.persona_id == persona_id
        and run.state == RunState.FAILED
        and isinstance(run.error, dict)
        and run.error.get("type") == RUN_BUDGET_EXCEEDED
        and run.error.get("approved_for_continuation")
        and _safe_session_id(run.session_id)
    )


def _continuation_token_budget(base_limit, approved_count: int):
    base = _safe_int(base_limit)
    if base is None or approved_count <= 0:
        return base_limit
    return base * (approved_count + 1)


def _prior_stage_run_progress_flags(task: Task, run_store: RunStore | None, persona_id: str, *, exclude_run_id: str | None = None) -> dict[str, bool]:
    if run_store is None:
        return {}
    try:
        runs = run_store.list_for_task(task.id)
    except Exception:
        return {}
    flags: dict[str, bool] = {}
    for run in runs:
        if exclude_run_id and run.id == exclude_run_id:
            continue
        if run.stage_id != task.current_stage_id or run.persona_id != persona_id:
            continue
        progress = run.progress if isinstance(run.progress, dict) else {}
        if progress.get("has_patch_progress") is True or (_safe_int(progress.get("patch_count")) or 0) > 0:
            flags["has_patch_progress"] = True
        if progress.get("has_test_progress") is True or (_safe_int(progress.get("test_count")) or 0) > 0:
            flags["has_test_progress"] = True
        if progress.get("has_proof_progress") is True or (_safe_int(progress.get("proof_count")) or 0) > 0:
            flags["has_proof_progress"] = True
    return flags


def _initial_run_llm_metadata(persona, config: RuntimeConfig, *, retry_attempt: int, retry_max_attempts: int) -> dict:
    metadata = {
        "provider": persona.provider or getattr(config, "default_provider", None),
        "model": persona.model or getattr(config, "default_model", None),
        "api_mode": persona.api_mode or getattr(config, "default_api_mode", None),
        "retry_attempt": retry_attempt,
        "retry_max_attempts": retry_max_attempts,
    }
    return {key: value for key, value in metadata.items() if value is not None}


def _safe_traceback_frames(exc: BaseException, *, limit: int = 8) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    for frame in traceback.extract_tb(exc.__traceback__)[-limit:]:
        path = Path(frame.filename)
        frames.append(
            {
                "file": path.name,
                "module_path_tail": "/".join(path.parts[-3:]),
                "line": frame.lineno,
                "function": frame.name,
            }
        )
    return frames


def _get_persona(agent_store: AgentStore, persona_id: str, config: RuntimeConfig | None = None):
    stored = {persona.id: persona for persona in agent_store.list_all()}
    if persona_id in stored:
        return stored[persona_id]
    cfg = config if hasattr(config, "personas") else load_agent_runtime_config()
    return get_persisted_persona(persona_id, cfg)


def _dedupe(existing: list[str], incoming: list[str]) -> list[str]:
    result = list(existing)
    seen = set(result)
    for item in incoming:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _command_workdir_for_task(task: Task, explicit_workdir=None, *, actor: str | None = None, stage_id: str | None = None) -> Path:
    try:
        if explicit_workdir is None:
            repo_scope = _command_proof_repo_scope(task, actor=actor, stage_id=stage_id)
            if repo_scope:
                scoped_task = type("TaskCommandProofRepoScope", (), {"affected_repos": [repo_scope]})()
                return command_workdir_for_task(scoped_task)
        return command_workdir_for_task(task, explicit_workdir=explicit_workdir)
    except ValueError as exc:
        safe_repos = safe_affected_repo_labels(list(getattr(task, "affected_repos", []) or []))
        raise ValueError(
            "request_test_run could not resolve a valid affected repo workdir; "
            f"affected_repos={safe_repos!r}"
        ) from exc


def _isolated_workdir_from_run_progress(run) -> Path | None:
    progress = getattr(run, "progress", None)
    if not isinstance(progress, dict):
        return None
    execution = progress.get("repo_execution")
    if not isinstance(execution, dict) or not execution.get("isolated"):
        return None
    raw_workdir = str(execution.get("workdir") or "").strip()
    if not raw_workdir:
        return None
    workdir = Path(raw_workdir).expanduser()
    try:
        resolved = workdir.resolve()
    except OSError:
        return None
    if not resolved.is_dir():
        return None
    parts = {part.lower() for part in resolved.parts}
    if "wt" not in parts and resolved.parent.name.lower() != "hermes-agent-wt":
        return None
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=resolved,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    if branch.returncode != 0 or (branch.stdout or "").strip() != "HEAD":
        return None
    return resolved


def _isolate_command_proof_workdir_if_git(workdir: Path, *, task_id: str, run_id: str, actor: str | None) -> Path:
    git_root = _git_root_for_command_workdir(workdir)
    if git_root is None:
        return workdir
    repo_ctx = RepoExecutionContext(workdir=git_root, repo_label=git_root.name, source=f"{actor or 'proof'}-proof")
    return isolated_repo_context_for_run(repo_ctx, task_id=task_id, run_id=f"{run_id}_proof").workdir


def _git_root_for_command_workdir(workdir: Path) -> Path | None:
    try:
        start = workdir.resolve()
    except OSError:
        return None
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _command_proof_repo_scope(task: Task, *, actor: str | None, stage_id: str | None) -> str | None:
    """Return the repo that must own a command proof for the current stage.

    Dev sessions can be correctly repo-scoped while command proof collection
    still sees the task's broader cross-stack repo list. For proof integrity, a
    stage-specific repo hint is authoritative over the first affected_repo.
    """

    intent = _command_proof_repo_intent(task, actor=actor, stage_id=stage_id)
    affected_repos = [str(repo).strip() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()]
    stage_repo = _command_proof_stage_repo(task, stage_id=stage_id)
    has_legacy_stage = _has_legacy_command_stage(task, stage_id=stage_id)
    if intent is not None and (stage_repo is None or has_legacy_stage):
        for repo in affected_repos:
            if _repo_text_matches_intent(repo, intent):
                return repo
    if stage_repo and not has_legacy_stage:
        return stage_repo
    if stage_repo and (intent is None or _repo_text_matches_intent(stage_repo, intent)):
        return stage_repo
    if intent is None:
        return None
    if affected_repos:
        return None
    return {
        "backend": "EterniaBackend",
        "launcher": "EterniaLauncher",
        "harness": "hermes-agent",
    }[intent]


def _command_proof_stage_repo(task: Task, *, stage_id: str | None) -> str | None:
    stage = _stage_for_command_proof(task, stage_id)
    repo = str(getattr(stage, "repo", "") or "").strip() if stage is not None else ""
    if repo in {"EterniaBackend", "EterniaLauncher", "hermes-agent"}:
        if _is_typed_plan_stage(task, stage_id=stage_id):
            return repo
        return default_blueprint_placeholder_repo_override(task, repo) or repo
    return None


def _is_typed_plan_stage(task: Task, *, stage_id: str | None) -> bool:
    target = str(stage_id or getattr(task, "current_stage_id", "") or "").strip()
    if not target:
        return False
    plan = getattr(task, "mission_plan", None)
    return any(str(getattr(stage, "id", "") or "") == target for stage in (getattr(plan, "stages", None) or []))


def _has_legacy_command_stage(task: Task, *, stage_id: str | None) -> bool:
    target = str(stage_id or getattr(task, "current_stage_id", "") or "").strip()
    if not target:
        return False
    return any(getattr(stage, "id", None) == target for stage in (getattr(task, "stages", None) or []))


def _command_proof_repo_intent(task: Task, *, actor: str | None, stage_id: str | None) -> str | None:
    stage = _stage_for_command_proof(task, stage_id)
    stage_scope_text = ""
    stage_objective_text = ""
    stage_command_text = ""
    if stage is not None:
        stage_scope_text = " ".join(
            [
                str(stage.id),
                str(stage.title),
                " ".join(str(item) for item in (stage.affected_paths or [])),
            ]
        ).lower()
        stage_objective_text = str(stage.objective or "").lower()
        stage_command_text = " ".join(
            [
                " ".join(str(item) for item in (stage.test_plan or [])),
            ]
        ).lower()
    if _text_mentions_launcher(stage_scope_text):
        return "launcher"
    if _text_mentions_backend(stage_scope_text):
        return "backend"
    if _text_mentions_harness(stage_scope_text):
        return "harness"
    if _text_mentions_launcher(stage_command_text):
        return "launcher"
    if _text_mentions_backend(stage_command_text):
        return "backend"
    if _text_mentions_harness(stage_command_text):
        return "harness"
    if _text_mentions_backend(stage_objective_text):
        return "backend"
    if _text_mentions_launcher(stage_objective_text):
        return "launcher"
    if _text_mentions_harness(stage_objective_text):
        return "harness"
    actor_id = str(actor or "").strip()
    if actor_id == "backend_dev":
        return "backend"
    return None


def _stage_for_command_proof(task: Task, stage_id: str | None):
    target = str(stage_id or getattr(task, "current_stage_id", "") or "").strip()
    if not target:
        return None
    stages = list(_runtime_stage_records(task))
    legacy_stages = list(getattr(task, "stages", None) or [])
    seen = {id(stage) for stage in stages}
    stages.extend(stage for stage in legacy_stages if id(stage) not in seen)
    for stage in stages:
        if stage.id == target:
            return stage
    return None


def _repo_text_matches_intent(repo: str, intent: str) -> bool:
    raw = str(repo or "").strip()
    if ":" in raw or "/" in raw or "\\" in raw:
        haystack = Path(raw).name.lower().replace("_", "-")
    else:
        name = Path(raw).name.lower().replace("_", "-")
        text = raw.lower().replace("_", "-")
        haystack = f"{text} {name}"
    if intent == "launcher":
        return _text_mentions_launcher(haystack)
    if intent == "backend":
        return _text_mentions_backend(haystack)
    if intent == "harness":
        return _text_mentions_harness(haystack)
    return False


def _text_mentions_launcher(text: str) -> bool:
    normalized = str(text or "").lower().replace("_", "-")
    return any(
        marker in normalized
        for marker in (
            "eternialauncher",
            "eternia-launcher",
            "launcher",
            "frontend",
            "front-end",
            "flutter ",
            "flutter-test",
            "flutter-analyze",
            "dart-test",
        )
    )


def _text_mentions_backend(text: str) -> bool:
    normalized = str(text or "").lower().replace("_", "-")
    return any(marker in normalized for marker in ("eterniabackend", "eternia-backend", "backend"))


def _text_mentions_harness(text: str) -> bool:
    normalized = str(text or "").lower().replace("_", "-")
    return any(marker in normalized for marker in ("hermes-agent", "agent-runtime-harness", "harness"))
