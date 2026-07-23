from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
from .config import get_persisted_persona, load_agent_runtime_config
from .events import EventLog
from .models import Event, MissionIntent, MissionPlan, Task
from .node_tools import ROOT_NODE_BLOCKED_MARKER, ROOT_NODE_DONE_MARKER
from .profile_context import resolve_persona_profile
from .profile_runner import AgentRunRequest, ProfileAgentRunner, RunBudgetExceeded
from .progress import RunProgressSink
from .states import RunState, TaskState
from .store import ProofStore, RunStore, TaskStore
from .worker_sessions import WorkerSessionStore


@dataclass(slots=True)
class RootNodeSettledResult:
    settle_id: str
    started_at: Any
    finished_at: Any
    ticks: int
    actions_taken: list[Any] = field(default_factory=list)
    stop_reason: str = "no_eligible_action"
    task_id: str | None = None


class RootNodeEngine:
    """Daemon-compatible facade for the root-node execution path."""

    def __init__(
        self,
        *,
        task_store: TaskStore | None = None,
        run_store: RunStore | None = None,
        proof_store: ProofStore | None = None,
        worker_sessions: WorkerSessionStore | None = None,
        event_log: EventLog | None = None,
        runner: ProfileAgentRunner | None = None,
        config: Any | None = None,
    ) -> None:
        self.task_store = task_store or TaskStore()
        self.run_store = run_store or RunStore()
        self.proof_store = proof_store or ProofStore()
        self.worker_sessions = worker_sessions or WorkerSessionStore()
        self.event_log = event_log or EventLog()
        self.runner = runner or ProfileAgentRunner()
        self.config = config or load_agent_runtime_config()

    def run_until_settled(
        self,
        *,
        task_id: str | None = None,
        max_actions: int = 10,
        max_seconds: float | None = None,
    ) -> RootNodeSettledResult:
        started = now()
        settle_id = f"rootsettle_{uuid.uuid4().hex[:8]}"
        actions: list[dict[str, Any]] = []
        if not getattr(self.config, "root_node_mode", False):
            return RootNodeSettledResult(settle_id, started, now(), 0, [], "root_node_mode_disabled", task_id)
        if not task_id:
            return RootNodeSettledResult(settle_id, started, now(), 0, [], "missing_task_id", task_id)
        task = self.task_store.get(task_id)
        if task.state in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
            return RootNodeSettledResult(settle_id, started, now(), 0, [], "task_terminal", task.id)
        _ensure_root_plan(task)
        task.harness_self_heal["root_node_mode"] = True
        if task.state == TaskState.CREATED:
            task.state = TaskState.RUNNING
            task.updated_at = now()
        self.task_store.update(task, actor="root_node", reason="root node started")
        root = get_persisted_persona("neko_supervisor", self.config)
        session_id = self.worker_sessions.reusable_session_id(task_id=task.id, persona_id=root.id)
        run = self.run_store.open_run(
            root.id,
            task.id,
            "root",
            iteration_budget=int(getattr(root, "iteration_budget", None) or self.config.live_run_iteration_budget),
            max_wall_seconds=_root_wall_seconds(root, self.config),
            max_api_calls=getattr(root, "max_api_calls", None) or self.config.live_run_max_api_calls,
            max_total_tokens=getattr(root, "max_total_tokens", None) or self.config.live_run_max_total_tokens,
            session_id=session_id,
            tick_id=settle_id,
        )
        worker = self.worker_sessions.open_or_resume(task_id=task.id, persona=root, stage_id="root", session_id=session_id, assignment_id="root")
        self.worker_sessions.assign_run(worker.id, run)
        try:
            result = self._run_root(task, root, run)
            run.session_id = result.session_id or run.session_id
            run.llm = {
                "provider": result.provider,
                "model": result.model,
                "session_id": result.session_id,
                "api_calls": result.api_calls,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "total_tokens": result.total_tokens,
                "latency_ms": result.latency_ms,
                "validation_status": "valid",
                "timing": dict(result.profile_timing or {}),
            }
            self.run_store.update(run)
            closed = self.run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "root_turn", "summary": _safe_summary(result.final_response)})
            self.worker_sessions.update_after_run(worker.id, closed)
            actions.append({"run_id": closed.id, "persona_id": root.id})
            final_response = str(result.final_response or "")
            if ROOT_NODE_DONE_MARKER in final_response:
                task = self.task_store.get(task.id)
                task.state = TaskState.DONE
                task.updated_at = now()
                self.task_store.update(task, actor="root_node", reason="root declared done")
                stop_reason = "task_terminal"
            elif ROOT_NODE_BLOCKED_MARKER in final_response:
                task = self.task_store.get(task.id)
                task.state = TaskState.BLOCKED
                task.updated_at = now()
                self.task_store.update(task, actor="root_node", reason="root declared blocked")
                stop_reason = "task_terminal"
            else:
                stop_reason = "max_actions" if len(actions) >= max(1, int(max_actions or 1)) else "root_waiting"
            return RootNodeSettledResult(settle_id, started, now(), 1, actions, stop_reason, task.id)
        except RunBudgetExceeded as exc:
            run.session_id = exc.session_id or run.session_id
            run.error = {"type": "run_budget_exceeded", "summary": _safe_summary(str(exc))}
            self.run_store.update(run)
            closed = self.run_store.close_run(run.id, state=RunState.FAILED, error=run.error)
            self.worker_sessions.update_after_run(worker.id, closed, close_reason="root_budget_exceeded")
            return RootNodeSettledResult(settle_id, started, now(), 1, [{"run_id": closed.id, "persona_id": root.id}], "root_budget_exceeded", task.id)
        except Exception as exc:
            run.error = {"type": type(exc).__name__, "summary": "Root node run failed."}
            self.run_store.update(run)
            closed = self.run_store.close_run(run.id, state=RunState.FAILED, error=run.error)
            self.worker_sessions.update_after_run(worker.id, closed, close_reason="root_run_failed")
            return RootNodeSettledResult(settle_id, started, now(), 1, [{"run_id": closed.id, "persona_id": root.id}], "root_run_failed", task.id)

    def _run_root(self, task: Task, persona, run):
        binding = resolve_persona_profile(persona)
        if binding.readiness == "missing_profile":
            raise ValueError(binding.summary)
        progress_sink = RunProgressSink(run_store=self.run_store, event_log=self.event_log, run_id=run.id)
        return self.runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=persona.provider or getattr(self.config, "default_provider", None),
                model=persona.model or getattr(self.config, "default_model", None) or "",
                api_mode=persona.api_mode or getattr(self.config, "default_api_mode", "codex_responses"),
                enabled_toolsets=_root_toolsets(persona),
                blocked_tool_names=[],
                quiet_mode=True,
                skip_context_files=not bool(getattr(persona, "include_core_context_files", False)),
                skip_memory=not bool(getattr(persona, "include_profile_memory", False)),
                platform="agent_runtime_root",
                skill_surface="mission_worker",
                skill_root_node_mode=True,
                session_id=run.session_id,
                max_iterations=run.iteration_budget,
                max_wall_seconds=run.max_wall_seconds,
                max_api_calls=run.max_api_calls,
                max_total_tokens=run.max_total_tokens,
                user_message=_root_user_message(task),
                system_message=_root_system_prompt(),
                task_id=task.id,
                progress_callback=lambda payload: progress_sink.emit(str(payload.get("type", "run.progress")), payload),
                runtime_root=paths.store_root(),
            )
        )


def _ensure_root_plan(task: Task) -> None:
    if task.mission_plan is not None:
        return
    task.mission_plan = MissionPlan(
        enabled=True,
        mission_intent=MissionIntent(
            title=task.title,
            objective=task.description,
            acceptance_criteria=list(task.acceptance_criteria),
            non_goals=list(task.non_goals),
            source_task_id=task.id,
        ),
        stages=[],
        current_stage_id="root",
        blueprint_id="neko_default_script",
        blueprint_version=1,
        slots={"root": {"role": "neko"}, "dev": {"role": "dev"}, "qa": {"role": "qa"}},
        bindings={"root": "persona:neko_supervisor", "dev": "persona:dev", "qa": "persona:qa"},
        agent_topology={
            "root": "root",
            "edges": [
                {"source": "root", "target": "dev", "kind": "steers"},
                {"source": "root", "target": "qa", "kind": "steers"},
            ],
        },
    )
    task.current_stage_id = "root"


def _root_toolsets(persona) -> list[str]:
    toolsets = list(getattr(persona, "toolsets", []) or [])
    if "node_control" not in toolsets:
        toolsets.append("node_control")
    return toolsets


def _root_wall_seconds(persona, config) -> float:
    configured = getattr(persona, "max_wall_seconds", None) or getattr(config, "live_run_max_wall_seconds", 300.0)
    child_budget = float(getattr(config, "live_run_max_wall_seconds", 300.0) or 300.0)
    return max(float(configured or 0.0), child_budget * 3.0, 900.0)


def _root_system_prompt() -> str:
    prompt = (
        "You are the Root Node, Neko Mission Lead, running a Harness goal. "
        "Author the stages needed for this specific goal, then call run_node for each child stage. "
        "Judge only from the child summary and harness-returned evidence. If the child needs another turn, call steer_node with the same session_id. "
        "Do not emit AgentDecision JSON. Do not create completion contracts, gates, incidents, or hidden criteria. "
        f"When the whole goal is complete, end your final answer with {ROOT_NODE_DONE_MARKER}. "
        f"When it is genuinely stuck, end with {ROOT_NODE_BLOCKED_MARKER} and a compact reason. "
        "Keep all persisted text redaction-safe: no absolute paths, secrets, raw credentials, or copied long logs."
    )
    skill = _root_skill_text()
    return f"{prompt}\n\n{skill}" if skill else prompt


def _root_user_message(task: Task) -> str:
    acceptance = "\n".join(f"- {item}" for item in task.acceptance_criteria[:10])
    non_goals = "\n".join(f"- {item}" for item in task.non_goals[:10])
    repos = ", ".join(task.affected_repos or []) or "derive from the goal"
    return (
        f"Goal: {task.title}\n\n{task.description}\n\n"
        f"Repo scope: {repos}\n\n"
        f"Acceptance criteria:\n{acceptance or '- Satisfy the user request with child-node proof.'}\n\n"
        f"Non-goals:\n{non_goals or '- None supplied.'}\n\n"
        "Use run_node and steer_node to drive child nodes. Persist authored stages by passing stage objects to run_node."
    )


def _safe_summary(value: Any) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "password", "credential", "authorization", "bearer ")):
        return "Summary redacted."
    return text[:2000] or "No summary returned."


def _root_skill_text() -> str:
    path = Path(__file__).resolve().parents[1] / "docs" / "agent-runtime-harness" / "harness-skills" / "harness-mission-lead" / "SKILL.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
