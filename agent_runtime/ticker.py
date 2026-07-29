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
from .models import Event, Incident, MissionIntent, MissionPlan, MissionPlanStage, Task, apply_instance_model_overrides
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
from .profile_context import mcp_owner_profile_name
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
                            if not active_runs:
                                # An active run for this persona that carries NO
                                # stage is unattributed, not unrelated — it must
                                # still block a second dispatch. This only became
                                # reachable at Stage 15.3: the retired legacy
                                # orchestrator emitted stage-less actions, so the
                                # lookup below was the one that ran; every graph
                                # action carries a stage, which would otherwise
                                # make a stage-less live run invisible here and
                                # race a second run onto the same persona.
                                # Fail safe: unknown lane counts as occupied.
                                active_runs = [
                                    run
                                    for run in self.run_store.find_active(task_id=action_task.id, persona_id=persona_id)
                                    if not getattr(run, "stage_id", None)
                                ]
                        else:
                            active_runs = self.run_store.find_active(task_id=action_task.id, persona_id=persona_id)
                        if active_runs:
                            continue
                    # The budget-approval adjudication slot must stay runnable even
                    # when the mission-scoped budget is exhausted, otherwise the
                    # continuation lane deadlocks: Neko can never adjudicate the very
                    # incident that blocks the task.
                    budget_adjudication_slot = bool(budget_approval_incidents) and _action_targets(
                        action, "neko_supervisor"
                    )
                    if persona_id and not budget_adjudication_slot:
                        budget_block = _runtime_budget_block(
                            action_task,
                            persona_id=persona_id,
                            run_store=self.run_store,
                            config=self.config,
                        )
                        if budget_block is not None:
                            incident, newly_opened = self._open_runtime_budget_incident(action_task, budget_block)
                            if newly_opened:
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
                            elif action_task.id not in result.skipped:
                                result.skipped.append(action_task.id)
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

    def _open_runtime_budget_incident(self, task: Task, budget_block: dict[str, Any]) -> tuple[Incident, bool]:
        event_type = str(budget_block["event_type"])
        # One open incident per (task, kind): re-raising on every tick floods
        # the incident store and event log without adding operator signal.
        for existing in self.incident_store.list_open():
            if existing.task_id == task.id and existing.kind == event_type:
                return existing, False
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
        return incident, True

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
        # Fold the persona-instance model override tier into the persona used
        # for this attempt (WorkerSession stamp, run llm metadata, run_tick),
        # so two instances of one persona can run different models. Cascade:
        # instance override > persona default > cfg default. Failure-tolerant:
        # an unreadable instance record must never crash a tick.
        persona = _persona_with_instance_model_overrides(
            persona, child_instance=child_instance, assignment=assignment
        )
        max_attempts = 1
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
                        _pin_visual_request_to_mcp_owner_profile(decision)
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
                        public_decision = projection.public_decision
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
                            normalize_request_test_run_decision(before_task, final_gate_decision)
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
                        "decision_type": public_type or decision.type.value,
                        "public_decision_type": public_type or decision.type.value,
                        "execution_decision_type": execution_type or decision.type.value,
                        "decision_contract_mode": getattr(projection, "mode", "legacy"),
                        "validation_status": "valid",
                        "retry_attempt": attempt,
                        "retry_max_attempts": max_attempts,
                    }
                    if execution_type and public_type and execution_type != public_type:
                        run.llm["raw_decision_type"] = execution_type
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
    bundle_proof_targets = _repo_bundle_proof_targets(task.id, repo_bundle_id)
    goal_named = goal_named_gate_commands(task, stage_repo_for_gate(task, stage)) if stage is not None else []
    if stage is not None and _stage_has_visual_gate(stage):
        proof_targets.append("launcher_qa screenshot proof")
    elif bundle_proof_targets:
        proof_targets.extend(bundle_proof_targets)
    elif goal_named and goal_demands_exact_proof(task):
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
    assignment_message = _assignment_message_for_action(
        task,
        action,
        persona_id=persona_id,
        stage_id=getattr(getattr(task, "mission_plan", None), "current_stage_id", None) or task.current_stage_id,
        base_message=str(objective or action.reason),
    )
    return PersonaAssignmentSpec(
        persona_id=persona_id,
        kind="task_stage",
        title=str(title),
        message=assignment_message,
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


def _stage_has_visual_gate(stage) -> bool:
    gate = getattr(stage, "proof_gate", {}) or {}
    required = {str(item).strip().lower() for item in (gate.get("required_proof_types") or []) if str(item).strip()}
    return bool(
        getattr(stage, "requires_product_edit", None) is not True
        and (getattr(stage, "requires_visual_proof", False) or gate.get("visual_required") is True or required & {"screenshot", "video"})
    )


def _repo_bundle_proof_targets(task_id: str, repo_bundle_id: str | None) -> list[str]:
    if not repo_bundle_id:
        return []
    try:
        bundle = RepoBundleStore().get(task_id, repo_bundle_id)
    except Exception:
        return []
    return [str(item).strip() for item in (getattr(bundle, "proof_targets", None) or []) if str(item).strip()]


def _assignment_message_for_action(task: Task, action: HarnessAction, *, persona_id: str, stage_id: str | None, base_message: str) -> str:
    base = _safe_assignment_message_line(base_message) or _safe_assignment_message_line(action.reason) or "Run the assigned stage."
    steer = _latest_upstream_handoff_steer(task, target_persona_id=persona_id, target_stage_id=stage_id)
    if not steer:
        return base
    return _truncate_assignment_message(f"{base} {steer}")


def _latest_upstream_handoff_steer(task: Task, *, target_persona_id: str, target_stage_id: str | None) -> str | None:
    try:
        runs = RunStore().list_for_task(task.id)
    except Exception:
        return None
    target_stage = str(target_stage_id or "").strip()
    for run in sorted(runs, key=_run_finished_sort_key, reverse=True):
        decision = run.final_decision if isinstance(getattr(run, "final_decision", None), dict) else {}
        decision_type = str(decision.get("type") or "").strip()
        if decision_type not in {"hand_off", "scope_route", "qa_verdict"}:
            continue
        source_persona = str(getattr(run, "persona_id", "") or "").strip()
        source_stage = str(getattr(run, "stage_id", "") or "").strip()
        if source_persona == target_persona_id and source_stage == target_stage:
            continue
        summary = _safe_assignment_message_line(decision.get("summary") or decision.get("execution_summary") or "Upstream stage completed.")
        proof_refs = _handoff_proof_refs(task, source_stage=source_stage, decision=decision)
        next_instruction = _handoff_next_instruction(target_persona_id=target_persona_id, decision_type=decision_type, source_persona=source_persona)
        source = f"{source_persona or 'harness'}" + (f" / stage {source_stage}" if source_stage else "") + f" / decision {decision_type}"
        proof_text = ", ".join(proof_refs) if proof_refs else "(none attached yet; inspect Proof Records and Mission HUD)"
        return f"Upstream handoff steer: from: {source}; summary: {summary}; proof_refs: {proof_text}; next: {next_instruction}"
    return None


def _run_finished_sort_key(run) -> tuple[str, str]:
    finished = getattr(run, "finished_at", None) or getattr(run, "last_heartbeat_at", None) or getattr(run, "started_at", None)
    return (finished.isoformat() if hasattr(finished, "isoformat") else str(finished or ""), str(getattr(run, "id", "") or ""))


def _handoff_proof_refs(task: Task, *, source_stage: str, decision: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    payload = decision.get("payload") if isinstance(decision.get("payload"), dict) else {}
    refs.extend(str(item).strip() for item in (payload.get("proof_ids") or []) if str(item).strip())
    plan = getattr(task, "mission_plan", None)
    if source_stage and plan is not None:
        for stage in getattr(plan, "stages", None) or []:
            if str(getattr(stage, "id", "") or "") == source_stage:
                refs.extend(str(item).strip() for item in (getattr(stage, "proof_ids", None) or []) if str(item).strip())
                break
    if source_stage:
        try:
            refs.extend(
                str(proof.id).strip()
                for proof in ProofStore().list_for_task(task.id)
                if str(getattr(proof, "stage_id", "") or "") == source_stage and str(getattr(proof, "id", "") or "").strip()
            )
        except Exception:
            pass
    if not refs:
        refs.extend(str(item).strip() for item in (getattr(task, "proof_ids", None) or []) if str(item).strip())
    return _dedupe_strings(refs)[:8]


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _handoff_next_instruction(*, target_persona_id: str, decision_type: str, source_persona: str) -> str:
    if target_persona_id == "qa":
        return "Review Harness proof records for the current stage, then emit qa_verdict; request exactly one missing proof lane only if required proof is absent or stale."
    if target_persona_id in {"dev", "backend_dev"}:
        if decision_type == "scope_route":
            return "Execute the current assigned stage, run a focused in-session self-test when work changes, then hand_off."
        if source_persona in {"dev", "backend_dev"}:
            return "Consume the upstream stage proof/context, work only the current assigned stage, run a focused self-test, then hand_off."
        return "Work only the current assigned stage, use visible HUD actions, and hand_off when complete."
    if target_persona_id == "neko_supervisor":
        return "Adjudicate the upstream result and route the next bounded owner with scope_route, or block with exact evidence."
    return "Use the current Mission HUD action and preserve this upstream context in the next decision."


def _safe_assignment_message_line(value: Any, *, limit: int = 700) -> str:
    text = str(value or "").replace("\r", " ").strip()
    text = re.sub(r"[ \t]+", " ", text)
    return text[:limit]


def _truncate_assignment_message(value: str, *, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 32].rstrip() + "\n[assignment steer truncated]"


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
    if decision_type in {DecisionType.HAND_OFF, DecisionType.PROPOSE_PATCH, DecisionType.COMPLETE, DecisionType.REQUEST_QA_REVIEW}:
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
    if decision_type in {DecisionType.QA_VERDICT, DecisionType.REPORT_QA_VERDICT}:
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
        return ["scope_route", "request_context", "block", "escalate"]
    if _action_targets(action, "dev", "backend_dev"):
        return ["hand_off", "request_screenshot", "request_video", "block", "escalate"]
    if _action_targets(action, "qa"):
        return ["qa_verdict", "request_screenshot", "request_video", "block", "escalate"]
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
    if decision.type not in {DecisionType.HAND_OFF, DecisionType.PROPOSE_PATCH}:
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
    # Fire the observe-the-work lane on every handoff/gate-request signal so the
    # HUD diff+trace surface has parity across modern and legacy contracts.
    if decision.type not in {DecisionType.HAND_OFF, DecisionType.PROPOSE_PATCH, DecisionType.REQUEST_QA_REVIEW, DecisionType.REQUEST_TEST_RUN}:
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
    if decision.type not in {DecisionType.HAND_OFF, DecisionType.PROPOSE_PATCH, DecisionType.REQUEST_QA_REVIEW, DecisionType.REQUEST_TEST_RUN}:
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


def _pin_visual_request_to_mcp_owner_profile(decision: AgentDecision) -> None:
    """Make the launch pin follow the MCP server owner, not the caller."""

    if decision.type not in {DecisionType.REQUEST_SCREENSHOT, DecisionType.REQUEST_VIDEO}:
        return
    pins = decision.payload.get("required_launch_pins")
    if not isinstance(pins, dict):
        return
    pins["hermes_profile"] = mcp_owner_profile_name(str(decision.payload.get("mcp_server") or ""))


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
        "and return qa_verdict with verdict, findings, and proof_ids including that visual proof, "
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
            f"request_test_run recipe_id {recipe_id!r} cannot bypass incomplete product-edit stage {current_stage_id!r}; return hand_off/correct_stage for that stage or request focused Flutter/widget proof after edits."
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



def _load_ticker_parts() -> None:
    parts_dir = Path(__file__).with_name("ticker_parts")
    for filename in ("role_runtime_tail.py",):
        path = parts_dir / filename
        exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), globals())


_load_ticker_parts()
