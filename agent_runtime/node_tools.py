from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from hermes_time import now

from . import paths
from .config import get_persisted_persona, load_agent_runtime_config
from .events import EventLog
from .models import AgentPersona, AgentRun, Event, MissionPlan, MissionPlanStage, Proof, Task
from .profile_context import resolve_persona_profile
from .profile_runner import AgentRunRequest, ProfileAgentRunner, RunBudgetExceeded
from .progress import RunProgressSink
from .repo_context import (
    RepoExecutionContext,
    canonical_repo_scope_label,
    changed_files_from_diff,
    diff_weakens_tests,
    git_diff_since_baseline,
    isolated_repo_context_for_run,
    known_repo_scope_labels,
    resolve_affected_repo_workdir,
)
from .self_test_evidence import SelfTestEvidenceStore, self_test_summary
from .states import RunState, StageStatus, TaskState
from .store import ProofStore, RunStore, TaskStore
from .worker_sessions import WorkerSessionStore

ROOT_NODE_DONE_MARKER = "ROOT_NODE_DONE"
ROOT_NODE_BLOCKED_MARKER = "ROOT_NODE_BLOCKED"


@dataclass(slots=True)
class NodeRunResult:
    ok: bool
    summary: str
    session_id: str | None
    run_id: str | None
    stage_id: str | None
    persona_id: str | None
    evidence: list[dict[str, Any]]
    error: str | None = None

    def jsonable(self) -> dict[str, Any]:
        data = {
            "ok": self.ok,
            "summary": self.summary,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "stage_id": self.stage_id,
            "persona_id": self.persona_id,
            "evidence": self.evidence,
        }
        if self.error:
            data["error"] = self.error
        return data


class NodeToolService:
    """Substrate for root-run service tools.

    This class deliberately does not judge child output. It resolves a safe repo
    workdir, runs one normal Hermes agent loop, captures server-side evidence,
    and returns compact handles for the root model to adjudicate.
    """

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

    def run_node(self, *, task_id: str | None, stage: dict[str, Any]) -> NodeRunResult:
        task = self._task_for_call(task_id)
        stage_record = self._stage_from_payload(task, stage)
        persona = self._persona_for_stage(stage_record)
        repo_ctx = self._repo_context_for_stage(stage_record)
        session_id = self.worker_sessions.reusable_session_id(task_id=task.id, persona_id=persona.id)
        run = self._open_run(persona, task.id, stage_record.id, session_id=session_id)
        worker = self.worker_sessions.open_or_resume(
            task_id=task.id,
            persona=persona,
            stage_id=stage_record.id,
            session_id=session_id,
            assignment_id=stage_record.id,
        )
        self.worker_sessions.assign_run(worker.id, run)
        try:
            result = self._run_persona(
                persona,
                run,
                task=task,
                stage=stage_record,
                repo_ctx=repo_ctx,
                user_message=_child_user_message(task, stage_record),
            )
            run.session_id = result.session_id or run.session_id
            run.llm = _llm_metadata(result)
            self.run_store.update(run)
            closed = self.run_store.close_run(
                run.id,
                state=RunState.COMPLETED,
                final_decision={"type": "node_returned", "summary": _safe_summary(result.final_response)},
            )
            evidence = self._evidence_for(task.id, stage_record.id, run.id, repo_ctx=repo_ctx)
            self.worker_sessions.update_after_run(worker.id, closed, proof_ids_added=_proof_ids(evidence))
            self._event("child.returned", task.id, run.id, persona.id, {"stage_id": stage_record.id, "evidence_count": len(evidence)})
            return NodeRunResult(True, _safe_summary(result.final_response), closed.session_id, closed.id, stage_record.id, persona.id, evidence)
        except RunBudgetExceeded as exc:
            run.session_id = exc.session_id or run.session_id
            run.error = {"type": "run_budget_exceeded", "summary": _safe_summary(str(exc))}
            self.run_store.update(run)
            closed = self.run_store.close_run(run.id, state=RunState.FAILED, error=run.error)
            self.worker_sessions.update_after_run(worker.id, closed, close_reason="node_budget_exceeded")
            return NodeRunResult(False, "Child node hit its run budget.", closed.session_id, closed.id, stage_record.id, persona.id, self._evidence_for(task.id, stage_record.id, run.id, repo_ctx=repo_ctx), error="run_budget_exceeded")
        except Exception as exc:
            run.error = {"type": type(exc).__name__, "summary": "Child node run failed."}
            self.run_store.update(run)
            closed = self.run_store.close_run(run.id, state=RunState.FAILED, error=run.error)
            self.worker_sessions.update_after_run(worker.id, closed, close_reason="node_run_failed")
            return NodeRunResult(False, "Child node run failed.", closed.session_id, closed.id, stage_record.id, persona.id, self._evidence_for(task.id, stage_record.id, run.id, repo_ctx=repo_ctx), error=_safe_summary(f"{type(exc).__name__}: {exc}"))

    def steer_node(
        self,
        *,
        task_id: str | None,
        session_id: str,
        message: str,
        stage_id: str | None = None,
        persona_id: str | None = None,
    ) -> NodeRunResult:
        task = self._task_for_call(task_id)
        clean_session_id = _safe_session(session_id)
        if not clean_session_id:
            raise ValueError("steer_node requires a redaction-safe session_id")
        message = _safe_prompt_text(message, limit=4000)
        if not message:
            raise ValueError("steer_node requires a non-empty message")
        persona = self._persona_for_steer(task, session_id=clean_session_id, persona_id=persona_id)
        stage = self._stage_by_id(task, stage_id) if stage_id else self._latest_stage_for_persona(task, persona.id)
        repo_ctx = self._repo_context_for_stage(stage) if stage is not None else None
        run = self._open_run(persona, task.id, getattr(stage, "id", None), session_id=clean_session_id)
        worker = self.worker_sessions.open_or_resume(
            task_id=task.id,
            persona=persona,
            stage_id=getattr(stage, "id", None),
            session_id=clean_session_id,
            assignment_id=getattr(stage, "id", None),
        )
        self.worker_sessions.assign_run(worker.id, run)
        try:
            result = self._run_persona(persona, run, task=task, stage=stage, repo_ctx=repo_ctx, user_message=message)
            run.session_id = result.session_id or run.session_id
            run.llm = _llm_metadata(result)
            self.run_store.update(run)
            closed = self.run_store.close_run(
                run.id,
                state=RunState.COMPLETED,
                final_decision={"type": "node_steered", "summary": _safe_summary(result.final_response)},
            )
            evidence = self._evidence_for(task.id, getattr(stage, "id", None), run.id, repo_ctx=repo_ctx)
            self.worker_sessions.update_after_run(worker.id, closed, proof_ids_added=_proof_ids(evidence))
            self._event("worker_session.steered", task.id, run.id, persona.id, {"stage_id": getattr(stage, "id", None), "session_id_present": bool(closed.session_id)})
            return NodeRunResult(True, _safe_summary(result.final_response), closed.session_id, closed.id, getattr(stage, "id", None), persona.id, evidence)
        except Exception as exc:
            run.error = {"type": type(exc).__name__, "summary": "Child node steering failed."}
            self.run_store.update(run)
            closed = self.run_store.close_run(run.id, state=RunState.FAILED, error=run.error)
            self.worker_sessions.update_after_run(worker.id, closed, close_reason="node_steer_failed")
            return NodeRunResult(False, "Child node steering failed.", closed.session_id, closed.id, getattr(stage, "id", None), persona.id, self._evidence_for(task.id, getattr(stage, "id", None), run.id, repo_ctx=repo_ctx), error=_safe_summary(f"{type(exc).__name__}: {exc}"))

    def _task_for_call(self, task_id: str | None) -> Task:
        if not getattr(self.config, "root_node_mode", False):
            raise PermissionError("root_node_mode is disabled")
        task = self.task_store.get(_safe_task_id(task_id))
        if not ((task.harness_self_heal or {}).get("root_node_mode") or getattr(task, "mission_plan", None)):
            raise PermissionError("node tools are available only inside root-node harness goals")
        return task

    def _open_run(self, persona: AgentPersona, task_id: str, stage_id: str | None, *, session_id: str | None) -> AgentRun:
        return self.run_store.open_run(
            persona.id,
            task_id,
            stage_id,
            iteration_budget=int(getattr(persona, "iteration_budget", None) or self.config.live_run_iteration_budget),
            max_wall_seconds=getattr(persona, "max_wall_seconds", None) or self.config.live_run_max_wall_seconds,
            max_api_calls=getattr(persona, "max_api_calls", None) or self.config.live_run_max_api_calls,
            max_total_tokens=getattr(persona, "max_total_tokens", None) or self.config.live_run_max_total_tokens,
            session_id=session_id,
        )

    def _stage_from_payload(self, task: Task, stage: dict[str, Any]) -> MissionPlanStage:
        if not isinstance(stage, dict):
            raise ValueError("run_node requires a stage object")
        stage_id = _safe_token(stage.get("id") or stage.get("stage_id") or f"stage_{len(_plan_stages(task)) + 1}")
        repo = _canonical_repo_or_raise(stage.get("repo") or stage.get("affected_repo"))
        owner = _safe_persona_id(stage.get("persona_id") or stage.get("owner") or stage.get("owner_slot") or "dev")
        existing = self._stage_by_id(task, stage_id, missing_ok=True)
        if existing is not None:
            return existing
        record = MissionPlanStage(
            id=stage_id,
            title=_safe_prompt_text(stage.get("title") or stage_id, limit=160),
            objective=_safe_prompt_text(stage.get("objective") or stage.get("description") or stage_id, limit=3000),
            owner=owner,
            owner_slot=_safe_token(stage.get("owner_slot") or owner),
            repo=repo,
            kind=_safe_token(stage.get("kind") or "implementation"),
            status=StageStatus.READY,
            requires_visual_proof=bool(stage.get("requires_visual_proof", False)),
            acceptance_criteria=_safe_text_list(stage.get("acceptance_criteria")),
            test_plan=_safe_text_list(stage.get("test_plan")),
            depends_on=[_safe_token(item) for item in stage.get("depends_on", []) or [] if _safe_token(item)],
            created_at=now(),
            updated_at=now(),
        )
        _append_stage(task, record)
        self.task_store.update(task, actor="root_node", reason="root authored stage")
        return record

    def _stage_by_id(self, task: Task, stage_id: str | None, *, missing_ok: bool = False) -> MissionPlanStage | None:
        wanted = _safe_token(stage_id)
        for stage in _plan_stages(task):
            if stage.id == wanted:
                return stage
        if missing_ok:
            return None
        raise ValueError("unknown stage_id")

    def _latest_stage_for_persona(self, task: Task, persona_id: str) -> MissionPlanStage | None:
        stages = [stage for stage in _plan_stages(task) if stage.owner == persona_id]
        return stages[-1] if stages else None

    def _persona_for_stage(self, stage: MissionPlanStage) -> AgentPersona:
        return get_persisted_persona(_safe_persona_id(stage.owner), self.config)

    def _persona_for_steer(self, task: Task, *, session_id: str, persona_id: str | None) -> AgentPersona:
        if persona_id:
            return get_persisted_persona(_safe_persona_id(persona_id), self.config)
        for worker in reversed(self.worker_sessions.list_for_task(task.id)):
            if worker.session_id == session_id:
                return get_persisted_persona(worker.persona_id, self.config)
        raise ValueError("could not resolve session_id to a child node")

    def _repo_context_for_stage(self, stage: MissionPlanStage | None) -> RepoExecutionContext | None:
        if stage is None:
            return None
        repo = _canonical_repo_or_raise(stage.repo)
        workdir = resolve_affected_repo_workdir(repo)
        if workdir is None:
            raise ValueError("repo scope did not resolve to a workdir")
        return RepoExecutionContext(workdir=workdir, repo_label=repo, source=repo)

    def _run_persona(
        self,
        persona: AgentPersona,
        run: AgentRun,
        *,
        task: Task,
        stage: MissionPlanStage | None,
        repo_ctx: RepoExecutionContext | None,
        user_message: str,
    ):
        binding = resolve_persona_profile(persona)
        if binding.readiness == "missing_profile":
            raise ValueError(binding.summary)
        progress_sink = RunProgressSink(run_store=self.run_store, event_log=self.event_log, run_id=run.id)
        isolated = None
        if repo_ctx is not None:
            isolated = isolated_repo_context_for_run(repo_ctx, task_id=task.id, run_id=run.id)
            run.progress = {**(run.progress or {}), "repo_execution": {"repo_label": repo_ctx.repo_label}}
            self.run_store.update(run)
        return self.runner.run(
            AgentRunRequest(
                profile=binding.hermes_profile,
                provider=persona.provider or getattr(self.config, "default_provider", None),
                model=persona.model or getattr(self.config, "default_model", None) or "",
                api_mode=persona.api_mode or getattr(self.config, "default_api_mode", "codex_responses"),
                enabled_toolsets=_child_enabled_toolsets(persona),
                blocked_tool_names=[],
                quiet_mode=True,
                skip_context_files=not bool(getattr(persona, "include_core_context_files", False)),
                skip_memory=not bool(getattr(persona, "include_profile_memory", False)),
                platform="agent_runtime",
                skill_surface="mission_worker",
                skill_root_node_mode=True,
                session_id=run.session_id,
                max_iterations=run.iteration_budget,
                max_wall_seconds=run.max_wall_seconds,
                max_api_calls=run.max_api_calls,
                max_total_tokens=run.max_total_tokens,
                user_message=user_message,
                system_message=_child_system_prompt(persona, stage),
                task_id=task.id,
                progress_callback=lambda payload: progress_sink.emit(str(payload.get("type", "run.progress")), payload),
                runtime_root=paths.store_root(),
                workdir=isolated.workdir if isolated is not None else None,
                stop_on_repeated_read_search=False,
            )
        )

    def _evidence_for(self, task_id: str, stage_id: str | None, run_id: str, *, repo_ctx: RepoExecutionContext | None) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        for item in SelfTestEvidenceStore(event_log=self.event_log).list_for_task(task_id):
            if item.run_id == run_id or (stage_id and item.stage_id == stage_id):
                evidence.append({"kind": "self_test", **self_test_summary(item)})
        for proof in self.proof_store.list_for_task(task_id):
            if proof.stage_id == stage_id or (proof.metadata or {}).get("run_id") == run_id:
                evidence.append(_proof_summary(proof))
        diff_summary = _diff_summary(repo_ctx)
        if diff_summary:
            evidence.append(diff_summary)
        return evidence[:20]

    def _event(self, event_type: str, task_id: str, run_id: str | None, persona_id: str | None, payload: dict[str, Any]) -> None:
        self.event_log.append(Event(now(), event_type, task_id, run_id, persona_id, payload))


def run_node_tool(args: dict[str, Any], *, task_id: str | None = None, **_kwargs) -> str:
    service = NodeToolService()
    result = service.run_node(task_id=task_id, stage=args.get("stage") if isinstance(args.get("stage"), dict) else args)
    return json.dumps(result.jsonable(), indent=2, sort_keys=True)


def steer_node_tool(args: dict[str, Any], *, task_id: str | None = None, **_kwargs) -> str:
    service = NodeToolService()
    result = service.steer_node(
        task_id=task_id,
        session_id=str(args.get("session_id") or ""),
        message=str(args.get("message") or ""),
        stage_id=args.get("stage_id"),
        persona_id=args.get("persona_id"),
    )
    return json.dumps(result.jsonable(), indent=2, sort_keys=True)


def node_tools_available() -> bool:
    try:
        return bool(getattr(load_agent_runtime_config(), "root_node_mode", False))
    except Exception:
        return False


def _child_enabled_toolsets(persona: AgentPersona) -> list[str]:
    """Toolsets for a child node run.

    ``launcher_qa`` is injected for QA nodes here — on the root-node path only —
    rather than baked into the static QA persona, so the flag-off legacy path keeps
    the pre-root-node QA capability set unchanged. Symmetric with how the root gets
    ``node_control`` (see ``root_node_engine._root_toolsets``).
    """

    toolsets = list(persona.toolsets or [])
    if str(getattr(persona, "role", "") or "") == "qa" and "launcher_qa" not in toolsets:
        toolsets.append("launcher_qa")
    return toolsets


def _child_system_prompt(persona: AgentPersona, stage: MissionPlanStage | None) -> str:
    role = str(getattr(persona, "role", "") or "node")
    visual = " If visual proof is required, capture it with your available QA tools." if getattr(stage, "requires_visual_proof", False) else ""
    qa = (
        " As QA, re-run the relevant tests yourself; when visual proof is requested, "
        "use launcher_qa MCP screenshot tools, then end with either satisfied or a concrete steer for the dev child."
        if role == "qa"
        else ""
    )
    return (
        "You are a self-looped Hermes child node running inside the Agent Runtime Harness. "
        "Use your normal tools to complete the assigned stage, run focused proof yourself, "
        "and finish with a concise summary naming changed files, commands run, proof status, "
        "remaining gaps, and whether the root should accept or steer another turn."
        f"{visual}{qa} You do not emit AgentDecision JSON on this root-node path."
        f" Node role: {role}."
    )


def _child_user_message(task: Task, stage: MissionPlanStage) -> str:
    acceptance = "\n".join(f"- {item}" for item in stage.acceptance_criteria[:8])
    tests = "\n".join(f"- {item}" for item in stage.test_plan[:8])
    return (
        f"Goal: {task.title}\n\n"
        f"Stage: {stage.title}\n"
        f"Objective: {stage.objective}\n"
        f"Repo: {stage.repo}\n"
        f"Acceptance:\n{acceptance or '- Complete the objective and summarize proof.'}\n\n"
        f"Suggested proof:\n{tests or '- Run the narrowest relevant focused self-test.'}\n\n"
        "Work until the stage is ready to judge, then summarize. Do not ask the human unless a human-only blocker is proven."
    )


def _llm_metadata(result) -> dict[str, Any]:
    data = {
        "provider": result.provider,
        "model": result.model,
        "base_url_host": _safe_host(result.base_url),
        "session_id": result.session_id,
        "api_calls": result.api_calls,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "latency_ms": result.latency_ms,
        "validation_status": "valid",
    }
    if isinstance(result.profile_timing, dict):
        data["timing"] = dict(result.profile_timing)
    return {key: value for key, value in data.items() if value is not None}


def _safe_host(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value)
    return text.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0][:120] or None


def _proof_summary(proof: Proof) -> dict[str, Any]:
    metadata = proof.metadata or {}
    return {
        "kind": "proof",
        "proof_id": proof.id,
        "type": str(proof.type),
        "stage_id": proof.stage_id,
        "status": metadata.get("status", "attached"),
        "created_by": proof.created_by,
        "redaction_status": proof.redaction_status,
    }


def _diff_summary(repo_ctx: RepoExecutionContext | None) -> dict[str, Any] | None:
    if repo_ctx is None:
        return None
    try:
        # Enough diff text to detect test-weakening; the root reads this handle and
        # judges — the harness never gates on it.
        diff = git_diff_since_baseline(repo_ctx.workdir, None, max_chars=20000)
    except Exception:
        return None
    diff_text = diff.get("diff") if isinstance(diff, dict) else ""
    changed = changed_files_from_diff(diff_text) or (diff.get("new_untracked_paths") if isinstance(diff, dict) else None) or []
    return {
        "kind": "diff",
        "repo_label": repo_ctx.repo_label,
        "changed_files": [str(item).replace("\\", "/").rsplit("/", 1)[-1] for item in changed[:20]],
        "diff_weakens_tests": diff_weakens_tests(diff_text if isinstance(diff_text, str) else ""),
        "diff_truncated": bool(diff.get("truncated")) if isinstance(diff, dict) else False,
    }


def _append_stage(task: Task, stage: MissionPlanStage) -> None:
    if task.mission_plan is None:
        task.mission_plan = MissionPlan(enabled=True, stages=[], current_stage_id=stage.id, blueprint_id="neko_default_script", blueprint_version=1)
    task.mission_plan.stages.append(stage)
    task.mission_plan.current_stage_id = stage.id
    task.mission_plan.revision += 1
    task.current_stage_id = stage.id
    task.affected_repos = _append_unique(task.affected_repos, stage.repo)
    if task.state == TaskState.CREATED:
        task.state = TaskState.RUNNING
    task.updated_at = now()


def _plan_stages(task: Task) -> list[MissionPlanStage]:
    plan = getattr(task, "mission_plan", None)
    return list(getattr(plan, "stages", []) or [])


def _canonical_repo_or_raise(value: Any) -> str:
    raw = str(value or "").strip()
    label = canonical_repo_scope_label(raw) or (raw if raw in known_repo_scope_labels() else None)
    if not label:
        raise ValueError(f"unknown repo scope: {raw[:80]}")
    return label


def _safe_task_id(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"task_[A-Za-z0-9_-]{1,64}", text):
        return text
    raise ValueError("invalid task_id")


def _safe_session(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", text) else None


def _safe_token(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip()).strip("._:-")
    return text[:80] or "stage"


def _safe_persona_id(value: Any) -> str:
    text = _safe_token(value)
    if text in {"lead", "root", "neko", "pm", "alice_supervisor"}:
        return "neko_supervisor"
    if text in {"builder", "launcher_dev"}:
        return "dev"
    if text in {"verifier", "reviewer"}:
        return "qa"
    return text


def _safe_prompt_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "password", "credential", "authorization", "bearer ")):
        return ""
    return text[:limit]


def _safe_summary(value: Any) -> str:
    return _safe_prompt_text(value, limit=2000) or "No summary returned."


def _safe_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := _safe_prompt_text(item, limit=240))][:12]


def _append_unique(values: list[str], item: str) -> list[str]:
    result = list(values or [])
    if item and item not in result:
        result.append(item)
    return result


def _proof_ids(evidence: list[dict[str, Any]]) -> list[str]:
    return [str(item["proof_id"]) for item in evidence if item.get("proof_id")]
