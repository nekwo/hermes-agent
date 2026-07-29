from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from hermes_time import now

from .blueprints import BlueprintStore, instantiate_blueprint
from .default_plan import DEFAULT_TASK_BLUEPRINT_BINDINGS, DEFAULT_TASK_BLUEPRINT_ID, specialize_default_plan_for_task
from .goal_hygiene import prepare_new_goal_runtime
from .locks import HarnessLockUnavailable, tick_lock
from .models import Task
from .runtime_config import RuntimeConfig
from .resolution import assert_pinned, resolve_runtime
from .states import TaskState
from .store import IncidentStore, ProofStore, RunStore, TaskStore
from .ticker import TickEngine, RunUntilSettledResult
from .worklog import append_persona_worklog


DEFAULT_GOAL_BLUEPRINT_ID = DEFAULT_TASK_BLUEPRINT_ID
DEFAULT_GOAL_BINDINGS = DEFAULT_TASK_BLUEPRINT_BINDINGS


def _default_goal_bindings_for_blueprint(bp: Any) -> dict[str, str]:
    slot_ids = {
        str(getattr(slot, "id", "") or "").strip()
        for slot in getattr(bp, "slots", []) or []
        if str(getattr(slot, "id", "") or "").strip()
    }
    return {
        slot_id: binding
        for slot_id, binding in DEFAULT_GOAL_BINDINGS.items()
        if slot_id in slot_ids
    }


@dataclass(slots=True)
class GoalRunOptions:
    title: str
    description: str
    requested_by: str = "cli"
    max_actions: int = 16
    max_seconds: float | None = None
    archive_on_done: bool = False
    requires_visual_proof: bool = False
    affected_repos: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    blueprint_id: str | None = DEFAULT_GOAL_BLUEPRINT_ID
    bindings: dict[str, str] = field(default_factory=dict)
    workspace_id: str | None = None
    runtime_root: str | None = None


@dataclass(slots=True)
class GoalRunResult:
    ok: bool
    task_id: str
    title: str
    final_task_state: str
    stop_reason: str
    tick_stop_reason: str
    exit_code: int
    elapsed_seconds: float
    actions_taken: int
    ticks: int
    run_ids: list[str]
    proof_ids: list[str]
    open_incident_ids: list[str]
    all_incident_ids: list[str]
    hygiene: dict[str, Any]
    archive_result: dict[str, Any] | None = None
    next_actions: list[str] = field(default_factory=list)
    final_summary: dict[str, Any] = field(default_factory=dict)
    proof_summary: dict[str, Any] = field(default_factory=dict)
    blocker_summary: dict[str, Any] = field(default_factory=dict)


class MissionRuntimeController:
    """Product-facing goal runner built on the existing bounded TickEngine."""

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        task_store: TaskStore | None = None,
        run_store: RunStore | None = None,
        proof_store: ProofStore | None = None,
        incident_store: IncidentStore | None = None,
        engine_factory: Callable[..., TickEngine] | None = None,
        hygiene_fn: Callable[..., dict[str, Any]] = prepare_new_goal_runtime,
    ) -> None:
        self.config = config
        self.task_store = task_store or TaskStore()
        self.run_store = run_store or RunStore()
        self.proof_store = proof_store or ProofStore()
        self.incident_store = incident_store or IncidentStore()
        self.engine_factory = engine_factory or TickEngine
        self.hygiene_fn = hygiene_fn

    def run_goal(self, options: GoalRunOptions) -> GoalRunResult:
        started = time.monotonic()
        if options.runtime_root:
            assert_pinned(resolve_runtime(), pinned_root=options.runtime_root)
        hygiene = self.hygiene_fn(
            task_store=self.task_store,
            run_store=self.run_store,
            incident_store=self.incident_store,
            cleanup_stage47_temp=False,
            cleanup_launcher_visual_processes=False,
            heartbeat_ttl_seconds=getattr(self.config, "heartbeat_ttl_seconds", 900),
        )
        try:
            with tick_lock():
                pass
        except HarnessLockUnavailable:
            return GoalRunResult(
                ok=False,
                task_id="",
                title=options.title,
                final_task_state="not_created",
                stop_reason="tick_lock_unavailable",
                tick_stop_reason="tick_lock_unavailable",
                exit_code=2,
                elapsed_seconds=round(time.monotonic() - started, 3),
                actions_taken=0,
                ticks=0,
                run_ids=[],
                proof_ids=[],
                open_incident_ids=[],
                all_incident_ids=[],
                hygiene=hygiene,
                next_actions=["Another tick owns the runtime lock; monitor the active run and retry after it exits."],
                final_summary={
                    "status": "not_passed",
                    "stop_reason": "tick_lock_unavailable",
                    "task_state": "not_created",
                    "proof_count": 0,
                    "open_incident_count": 0,
                    "blocker_kind": "active_tick_lock",
                },
                proof_summary={"command_proofs": [], "command_proof_count": 0, "passed_command_proof_count": 0, "qa_verdicts": [], "latest_qa_verdict": None},
                blocker_summary={"kind": "active_tick_lock", "summary": "Another tick owns the runtime lock; no duplicate task was created."},
            )
        task = self._create_task(options)
        append_persona_worklog(
            task_id=task.id,
            persona_id="harness",
            source="controller",
            kind="goal_started",
            message=f"Goal runner created {task.id} and is running bounded ticks in process.",
            metadata={"max_actions": options.max_actions, "max_seconds": options.max_seconds},
        )
        engine = self.engine_factory(
            task_store=self.task_store,
            run_store=self.run_store,
            incident_store=self.incident_store,
            proof_store=self.proof_store,
            config=self.config,
        )
        settled = engine.run_until_settled(
            task_id=task.id,
            max_actions=max(1, int(options.max_actions or 1)),
            max_seconds=options.max_seconds,
        )
        final_task = self.task_store.get(task.id)
        normalized_stop, exit_code, next_actions = _normalize_stop(final_task.state, settled)
        ok = exit_code == 0
        result = self._build_result(
            task=final_task,
            settled=settled,
            hygiene=hygiene,
            normalized_stop=normalized_stop,
            exit_code=exit_code,
            elapsed_seconds=round(time.monotonic() - started, 3),
            archive_result=None,
            next_actions=next_actions,
        )
        if ok and options.archive_on_done:
            result.archive_result = self.task_store.archive(task.id, actor="harness", reason="goal runner archive-on-done")
        append_persona_worklog(
            task_id=task.id,
            persona_id="harness",
            source="controller",
            kind="goal_finished" if result.ok else "goal_stopped",
            message=f"Goal runner stopped at {result.stop_reason}; final task state is {result.final_task_state}.",
            metadata={"exit_code": result.exit_code, "proof_ids": result.proof_ids, "open_incident_ids": result.open_incident_ids},
        )
        return result

    def _create_task(self, options: GoalRunOptions) -> Task:
        ts = now()
        # Local accumulator only — NOT a shape any created goal can keep. The
        # `else` branch below closes it (Stage 15.2: no goal is born plan-less).
        mission_plan = None
        blueprint_id = (options.blueprint_id or DEFAULT_GOAL_BLUEPRINT_ID).strip()
        if blueprint_id:
            bp = BlueprintStore().get(blueprint_id)
            bindings = {
                **_default_goal_bindings_for_blueprint(bp),
                **dict(options.bindings or {}),
            }
            mission_plan = instantiate_blueprint(bp, goal=options.description, bindings=bindings)
            if mission_plan.mission_intent is not None:
                mission_plan.mission_intent.title = options.title
                mission_plan.mission_intent.acceptance_criteria = list(options.acceptance_criteria)
                mission_plan.mission_intent.non_goals = list(options.non_goals)
        task = Task(
            id=f"task_{uuid.uuid4().hex[:8]}",
            goal_id=f"goal_{uuid.uuid4().hex[:8]}",
            title=options.title,
            description=options.description,
            state=TaskState.CREATED,
            created_at=ts,
            updated_at=ts,
            requested_by=options.requested_by,
            requires_visual_proof=bool(options.requires_visual_proof),
            acceptance_criteria=list(options.acceptance_criteria),
            non_goals=list(options.non_goals),
            affected_repos=list(options.affected_repos),
            mission_plan=mission_plan,
            current_stage_id=mission_plan.current_stage_id if mission_plan is not None else None,
            workspace_id=options.workspace_id,
        )
        if mission_plan is not None:
            specialize_default_plan_for_task(task, mission_plan)
            task.current_stage_id = mission_plan.current_stage_id
        else:
            # Stage 15.2: no goal may be born plan-less. ``blueprint_id`` above
            # only falls through when the caller passed a blank/whitespace id,
            # which used to hand the engine an un-typed task — the exact shape
            # that reached the legacy orchestrator. Fall back to the default
            # graph rather than leaving the window open.
            from .default_plan import ensure_default_mission_plan

            ensure_default_mission_plan(task)
        return self.task_store.create(task)

    def _build_result(
        self,
        *,
        task: Task,
        settled: RunUntilSettledResult,
        hygiene: dict[str, Any],
        normalized_stop: str,
        exit_code: int,
        elapsed_seconds: float,
        archive_result: dict[str, Any] | None,
        next_actions: list[str],
    ) -> GoalRunResult:
        runs = self.run_store.list_for_task(task.id)
        proofs = self.proof_store.list_for_task(task.id)
        incidents = [incident for incident in self.incident_store.list_all() if incident.task_id == task.id]
        open_incidents = [incident for incident in incidents if incident.closed_at is None]
        proof_summary = _proof_summary(proofs)
        blocker_summary = _blocker_summary(task, normalized_stop=normalized_stop, open_incidents=open_incidents, proof_summary=proof_summary)
        adjusted_next_actions = _next_actions_for_summary(next_actions, blocker_summary=blocker_summary)
        return GoalRunResult(
            ok=exit_code == 0,
            task_id=task.id,
            title=task.title,
            final_task_state=task.state.value,
            stop_reason=normalized_stop,
            tick_stop_reason=settled.stop_reason,
            exit_code=exit_code,
            elapsed_seconds=elapsed_seconds,
            actions_taken=len(settled.actions_taken),
            ticks=settled.ticks,
            run_ids=[run.id for run in runs],
            proof_ids=[proof.id for proof in proofs],
            open_incident_ids=[incident.id for incident in open_incidents],
            all_incident_ids=[incident.id for incident in incidents],
            hygiene=hygiene,
            archive_result=archive_result,
            next_actions=adjusted_next_actions,
            final_summary={
                "status": "passed" if exit_code == 0 else "not_passed",
                "stop_reason": normalized_stop,
                "task_state": task.state.value,
                "proof_count": len(proofs),
                "open_incident_count": len(open_incidents),
                "blocker_kind": blocker_summary.get("kind"),
            },
            proof_summary=proof_summary,
            blocker_summary=blocker_summary,
        )


def _normalize_stop(task_state: TaskState, settled: RunUntilSettledResult) -> tuple[str, int, list[str]]:
    raw = settled.stop_reason
    if task_state == TaskState.DONE:
        return "task_done", 0, []
    if task_state == TaskState.CANCELLED:
        return "task_cancelled", 1, ["Inspect cancellation reason and decide whether to create a replacement goal."]
    if task_state == TaskState.FAILED:
        return "task_failed", 1, ["Inspect run errors and attached incidents."]
    if raw == "task_blocked":
        return "task_escalated", 1, ["Let Neko adjudicate the advisory blocker, then continue the run loop."]
    if raw == "incident_opened":
        return "incident_opened", 1, ["Fix or close the open incident, then rerun the goal runner for this task."]
    if raw == "waiting_on_approval":
        return "waiting_on_approval", 1, ["Approve or cancel the waiting run."]
    if raw == "active_run":
        return "active_run_boundary", 1, ["Monitor the active run; rerun the controller after the run closes."]
    if raw == "tick_lock_unavailable":
        return raw, 2, ["Another tick owns the runtime lock; retry after it exits."]
    if raw in {"max_actions", "max_seconds"}:
        return raw, 3, ["Increase the bound only if events show forward progress."]
    if raw == "action_failed":
        return raw, 1, ["Inspect the failed action and any proof-backed blocker."]
    return raw or "no_eligible_action", 1, ["No eligible next action was found; inspect task state and plan metadata."]


def _proof_summary(proofs) -> dict[str, Any]:
    command_proofs: list[dict[str, Any]] = []
    qa_verdicts: list[dict[str, Any]] = []
    latest_qa_verdict: dict[str, Any] | None = None
    for proof in sorted(proofs, key=lambda item: item.created_at):
        proof_type = proof.type.value if hasattr(proof.type, "value") else str(proof.type)
        metadata = proof.metadata if isinstance(proof.metadata, dict) else {}
        if proof_type == "test_run":
            original_command = metadata.get("original_command")
            command_adapter = metadata.get("command_adapter")
            command_proofs.append(
                {
                    "proof_id": proof.id,
                    "stage_id": proof.stage_id,
                    "command": metadata.get("command"),
                    "original_command": original_command,
                    "command_adapter": command_adapter,
                    "status": metadata.get("status"),
                    "exit_code": metadata.get("exit_code"),
                    "timed_out": metadata.get("timed_out"),
                    "redaction_status": proof.redaction_status,
                }
            )
        if proof_type == "qa_verdict":
            verdict = {
                "proof_id": proof.id,
                "stage_id": proof.stage_id,
                "verdict": metadata.get("verdict") or proof.path_or_value,
                "findings": list(metadata.get("findings") or [])[:10],
                "proof_ids": list(metadata.get("proof_ids") or [])[:20],
                "redaction_status": proof.redaction_status,
            }
            qa_verdicts.append(verdict)
            latest_qa_verdict = verdict
    passed_commands = [item for item in command_proofs if item.get("status") == "passed" and item.get("exit_code") == 0]
    return {
        "command_proofs": command_proofs,
        "command_proof_count": len(command_proofs),
        "passed_command_proof_count": len(passed_commands),
        "qa_verdicts": qa_verdicts,
        "latest_qa_verdict": latest_qa_verdict,
    }


def _blocker_summary(task: Task, *, normalized_stop: str, open_incidents: list, proof_summary: dict[str, Any]) -> dict[str, Any]:
    if open_incidents:
        return {
            "kind": "open_incident",
            "summary": "Open incident(s) are blocking the goal.",
            "incidents": [
                {
                    "incident_id": incident.id,
                    "kind": incident.kind,
                    "summary": incident.summary,
                    "run_id": incident.run_id,
                }
                for incident in open_incidents
            ],
        }
    latest_qa = proof_summary.get("latest_qa_verdict")
    if isinstance(latest_qa, dict):
        blocker_findings = [
            finding
            for finding in (latest_qa.get("findings") or [])
            if isinstance(finding, dict) and str(finding.get("severity") or "").lower() in {"blocker", "fail", "failed", "error"}
        ]
        if blocker_findings:
            return {
                "kind": "qa_verdict_blocker",
                "summary": "Latest QA verdict contains blocker findings.",
                "qa_verdict_proof_id": latest_qa.get("proof_id"),
                "findings": blocker_findings[:10],
            }
    if normalized_stop in {"task_escalated", "action_failed"} or task.state == TaskState.BLOCKED:
        return {
            "kind": "state_escalated",
            "summary": "Task hit an advisory blocker; inspect proof_summary, HUD evidence, recent events, and task state.",
            "task_state": task.state.value,
        }
    return {}


def _next_actions_for_summary(next_actions: list[str], *, blocker_summary: dict[str, Any]) -> list[str]:
    if blocker_summary.get("kind") == "qa_verdict_blocker":
        return ["Inspect blocker_summary.latest QA findings, attach missing proof or route a bounded fix to the responsible persona."]
    if blocker_summary.get("kind") == "state_escalated":
        return ["Inspect blocker_summary, proof_summary, and HUD evidence; route Neko adjudication instead of treating BLOCKED as terminal."]
    return next_actions
