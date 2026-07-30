from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from hermes_time import now

from .errors import LegacyOrchestratorRemoved
from .goal_hygiene import prepare_new_goal_runtime
from .persona_assignments import (
    PersonaAssignmentSpec,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_assignment_store_enabled,
    persona_instance_id_for,
    persona_instance_runtime_enabled,
)
from .runtime_config import RuntimeConfig
from .states import TaskState
from .store import IncidentStore, RunStore, TaskStore
from .worklog import append_persona_worklog


ALLOWED_DIAGNOSTIC_PERSONAS = frozenset({"neko_supervisor", "dev", "backend_dev", "qa"})


@dataclass(slots=True)
class PersonaDiagnosticOptions:
    persona_id: str
    title: str
    message: str
    requested_by: str = "cli"
    operation_kind: str = "diagnostic"
    operation_mode: str = "standalone_task"
    max_actions: int = 1
    max_seconds: float | None = 240.0
    affected_repos: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    non_goals: list[str] = field(default_factory=list)
    preserve_open_task: bool = True


@dataclass(slots=True)
class PersonaDiagnosticResult:
    ok: bool
    operation_id: str
    operation_kind: str
    operation_mode: str
    persona_id: str
    persona_instance_id: str | None
    assignment_id: str | None
    task_id: str
    title: str
    final_task_state: str
    stop_reason: str
    exit_code: int
    elapsed_seconds: float
    actions_taken: int
    ticks: int
    run_ids: list[str]
    latest_run_id: str | None
    latest_decision_type: str | None
    latest_validation_status: str | None
    latest_total_tokens: int | None
    expected_first_action: str
    actual_actions: list[str]
    stage_id: str | None
    stage_owner: str | None
    stage_repo: str | None
    hygiene: dict[str, Any]
    next_actions: list[str] = field(default_factory=list)


class PersonaDiagnosticController:
    """Retained typed boundary for the retired mission diagnostic runner."""

    def __init__(
        self,
        *,
        config: RuntimeConfig,
        task_store: TaskStore | None = None,
        run_store: RunStore | None = None,
        incident_store: IncidentStore | None = None,
        assignment_store: PersonaAssignmentStore | None = None,
        instance_store: PersonaInstanceStore | None = None,
        engine_factory: Callable[..., Any] | None = None,
        hygiene_fn: Callable[..., dict[str, Any]] = prepare_new_goal_runtime,
    ) -> None:
        self.config = config
        self.task_store = task_store or TaskStore()
        self.run_store = run_store or RunStore()
        self.incident_store = incident_store or IncidentStore()
        self.assignment_store = assignment_store or PersonaAssignmentStore()
        self.instance_store = instance_store or PersonaInstanceStore()
        self.engine_factory = engine_factory
        self.hygiene_fn = hygiene_fn

    def diagnose(self, options: PersonaDiagnosticOptions) -> PersonaDiagnosticResult:
        raise LegacyOrchestratorRemoved(
            "persona mission diagnostics are unavailable because the dispatch loop is retired",
            safe_details={"persona_id": options.persona_id, "operation_kind": options.operation_kind},
        )
        persona_id = _normalize_persona_id(options.persona_id)
        operation_id = f"personaop_{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        hygiene = self.hygiene_fn(
            task_store=self.task_store,
            run_store=self.run_store,
            incident_store=self.incident_store,
            cleanup_stage47_temp=False,
            cleanup_launcher_visual_processes=False,
            heartbeat_ttl_seconds=getattr(self.config, "heartbeat_ttl_seconds", 900),
        )
        task = self._create_task(options, persona_id=persona_id, operation_id=operation_id)
        task.harness_self_heal["repo_clean_baseline"] = _repo_clean_baseline_from_hygiene(hygiene)
        self.task_store.update(task, actor="harness", reason="record persona diagnostic repo-clean baseline")
        assignment = None
        if persona_instance_runtime_enabled(self.config) or persona_assignment_store_enabled(self.config):
            assignment = self.assignment_store.create_or_resume(
                PersonaAssignmentSpec(
                    persona_id=persona_id,
                    kind="diagnostic",
                    title=options.title,
                    message=options.message,
                    created_by=options.requested_by,
                    task_id=task.id,
                    stage_id=task.current_stage_id,
                    operation_id=operation_id,
                    repo=_stage_repo_for_persona(persona_id),
                    acceptance=list(options.acceptance_criteria or []),
                    non_goals=list(options.non_goals or []),
                )
            )
            task.risk_flags = _dedupe(
                list(task.risk_flags or []),
                [
                    f"persona_assignment_id:{assignment.id}",
                    f"persona_instance_id:{assignment.persona_instance_id}",
                ],
            )
            task.updated_at = now()
            self.task_store.update(task, actor="harness", reason="attach persona diagnostic assignment")
        append_persona_worklog(
            task_id=task.id,
            persona_id="harness",
            source="persona_diagnostic",
            kind="diagnostic_started",
            message=f"Persona diagnostic created {task.id} for {persona_id}.",
            metadata={
                "operation_id": operation_id,
                "operation_kind": _safe_operation_label(options.operation_kind),
                "operation_mode": _safe_operation_label(options.operation_mode),
                "assignment_id": assignment.id if assignment else None,
                "persona_instance_id": assignment.persona_instance_id if assignment else persona_instance_id_for(persona_id),
                "max_actions": options.max_actions,
                "max_seconds": options.max_seconds,
            },
        )
        engine = self.engine_factory(
            task_store=self.task_store,
            run_store=self.run_store,
            incident_store=self.incident_store,
            config=self.config,
        )
        settled = engine.run_until_settled(
            task_id=task.id,
            max_actions=max(1, int(options.max_actions or 1)),
            max_seconds=options.max_seconds,
        )
        final_task = self.task_store.get(task.id)
        if assignment is not None:
            runs = self.run_store.list_for_task(task.id)
            for run in runs:
                if run.persona_id == persona_id:
                    self.assignment_store.attach_run(assignment.id, run.id)
            terminal_state = "completed" if any(run.persona_id == persona_id for run in runs) else "blocked"
            self.assignment_store.complete(
                assignment.id,
                state=terminal_state,
                error=None if terminal_state == "completed" else f"no {persona_id} run recorded",
            )
        self._finalize_successful_diagnostic_task(task.id, persona_id=persona_id)
        final_task = self.task_store.get(task.id)
        result = self._build_result(
            task=final_task,
            persona_id=persona_id,
            operation_id=operation_id,
            operation_kind=_safe_operation_label(options.operation_kind),
            operation_mode=_safe_operation_label(options.operation_mode),
            persona_instance_id=assignment.persona_instance_id if assignment else persona_instance_id_for(persona_id),
            assignment_id=assignment.id if assignment else None,
            settled=settled,
            hygiene=hygiene,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
        append_persona_worklog(
            task_id=task.id,
            persona_id="harness",
            source="persona_diagnostic",
            kind="diagnostic_finished",
            message=f"Persona diagnostic stopped at {result.stop_reason}; latest decision is {result.latest_decision_type or 'none'}.",
            metadata={"operation_id": operation_id, "exit_code": result.exit_code, "run_ids": result.run_ids},
        )
        if not options.preserve_open_task:
            self._archive_diagnostic_task(task.id)
        return result

    def _archive_diagnostic_task(self, task_id: str) -> None:
        # A standalone diagnostic is a throwaway probe. Leaving its task open (or
        # done-but-unarchived) accumulates in the runtime and gates the scheduler
        # (open-task / graveyard pressure that stalls the next real goal). Cancel a
        # non-terminal probe, then archive so evidence is preserved without
        # polluting live state.
        try:
            task = self.task_store.get(task_id)
        except Exception:
            return
        try:
            state = task.state if isinstance(task.state, TaskState) else TaskState(task.state)
        except Exception:
            state = None
        try:
            if state not in {TaskState.DONE, TaskState.CANCELLED}:
                self.task_store.cancel(task_id, reason="persona diagnostic complete; auto-archiving standalone probe")
            self.task_store.archive(task_id, actor="harness", reason="persona diagnostic auto-archive")
        except Exception:
            return

    def _finalize_successful_diagnostic_task(self, task_id: str, *, persona_id: str) -> None:
        task = self.task_store.get(task_id)
        runs = self.run_store.list_for_task(task_id)
        if not _has_completed_valid_matching_run(runs, persona_id):
            return
        task.state = TaskState.DONE
        task.updated_at = now()
        self.task_store.update(task, actor="harness", reason=f"complete successful {persona_id} persona diagnostic")

    def _create_task(self, options: PersonaDiagnosticOptions, *, persona_id: str, operation_id: str) -> Task:
        ts = now()
        operation_kind = _safe_operation_label(options.operation_kind)
        operation_mode = _safe_operation_label(options.operation_mode)
        task = Task(
            id=f"task_{uuid.uuid4().hex[:8]}",
            title=options.title,
            description=options.message,
            state=_initial_state_for_persona(persona_id),
            created_at=ts,
            updated_at=ts,
            requested_by=options.requested_by,
            acceptance_criteria=list(options.acceptance_criteria),
            non_goals=list(options.non_goals),
            affected_repos=_affected_repos_for_persona(persona_id, list(options.affected_repos or [])),
            risk_flags=[
                "persona_operation",
                f"persona_operation_id:{operation_id}",
                f"persona_operation_kind:{operation_kind}",
                f"persona_operation_mode:{operation_mode}",
                f"diagnostic_persona:{persona_id}",
            ],
        )
        return self.task_store.create(task)

    def _build_result(
        self,
        *,
        task: Task,
        persona_id: str,
        operation_id: str,
        operation_kind: str,
        operation_mode: str,
        persona_instance_id: str | None,
        assignment_id: str | None,
        settled: RunUntilSettledResult,
        hygiene: dict[str, Any],
        elapsed_seconds: float,
    ) -> PersonaDiagnosticResult:
        runs = self.run_store.list_for_task(task.id)
        matching_runs = [run for run in runs if run.persona_id == persona_id]
        latest = matching_runs[-1] if matching_runs else (runs[-1] if runs else None)
        actions = [item.action.type.value for item in settled.actions_taken]
        expected_action = _expected_action_for_persona(persona_id)
        wrong_persona = bool(actions and expected_action not in actions)
        latest_state = str(getattr(latest, "state", "") or "")
        latest_completed = latest_state.endswith("completed") or latest_state == "RunState.COMPLETED"
        latest_valid = _validation_status(latest) == "valid"
        exit_code = 0 if matching_runs and not wrong_persona and latest_completed and latest_valid else 3
        next_actions: list[str] = []
        if not matching_runs:
            next_actions.append(f"No {persona_id} run was recorded; inspect next_action routing and task setup.")
        if wrong_persona:
            next_actions.append(f"Expected {expected_action}, got {actions}.")
        if latest is not None and not latest_completed:
            next_actions.append(f"Latest {persona_id} run ended in state {latest_state or 'unknown'}; inspect run error before treating the diagnostic as healthy.")
        if latest is not None and latest_completed and not latest_valid:
            next_actions.append(f"Latest {persona_id} run did not record validation_status=valid; inspect decision parsing and validation.")
        stage = None
        return PersonaDiagnosticResult(
            ok=exit_code == 0,
            operation_id=operation_id,
            operation_kind=operation_kind,
            operation_mode=operation_mode,
            persona_id=persona_id,
            persona_instance_id=persona_instance_id,
            assignment_id=assignment_id,
            task_id=task.id,
            title=task.title,
            final_task_state=task.state.value if hasattr(task.state, "value") else str(task.state),
            stop_reason=settled.stop_reason,
            exit_code=exit_code,
            elapsed_seconds=elapsed_seconds,
            actions_taken=len(settled.actions_taken),
            ticks=settled.ticks,
            run_ids=[run.id for run in runs],
            latest_run_id=latest.id if latest else None,
            latest_decision_type=_decision_type(latest.final_decision if latest else None),
            latest_validation_status=_validation_status(latest),
            latest_total_tokens=_latest_total_tokens(latest),
            expected_first_action=expected_action,
            actual_actions=actions,
            stage_id=stage.id if stage else None,
            stage_owner=stage.owner if stage else None,
            stage_repo=stage.repo if stage else None,
            hygiene=hygiene,
            next_actions=next_actions,
        )


def _normalize_persona_id(persona_id: str) -> str:
    value = str(persona_id or "").strip()
    aliases = {
        "neko": "neko_supervisor",
        "launcher_dev": "dev",
        "launcher-dev": "dev",
        "backend-dev": "backend_dev",
        "backend": "backend_dev",
    }
    value = aliases.get(value, value)
    if value not in ALLOWED_DIAGNOSTIC_PERSONAS:
        allowed = ", ".join(sorted(ALLOWED_DIAGNOSTIC_PERSONAS))
        raise ValueError(f"unsupported diagnostic persona {persona_id!r}; expected one of {allowed}")
    return value


def _safe_operation_label(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip().lower())
    return cleaned.strip("_")[:64] or "diagnostic"


def _repo_clean_baseline_from_hygiene(hygiene: dict[str, Any]) -> dict[str, Any]:
    dirty_state = hygiene.get("dirty_state_after_cleanup") if isinstance(hygiene, dict) else None
    repos = dirty_state.get("repos") if isinstance(dirty_state, dict) else None
    if not isinstance(repos, list):
        return {"repos": []}
    safe_repos: list[dict[str, Any]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        safe_repos.append(
            {
                "label": str(repo.get("label") or "")[:80],
                "dirty": bool(repo.get("dirty")),
                "dirty_count": int(repo.get("dirty_count") or 0),
                "error": str(repo.get("error") or "")[:80] or None,
                "status_excerpt": [str(item)[:180] for item in list(repo.get("status_excerpt") or [])[:20]],
            }
        )
    return {"created_at": now().isoformat(), "repos": safe_repos}


def _dedupe(existing: list[str], additions: list[str]) -> list[str]:
    seen = set()
    result: list[str] = []
    for item in [*existing, *additions]:
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _stage_repo_for_persona(persona_id: str) -> str | None:
    if persona_id == "backend_dev":
        return "EterniaBackend"
    if persona_id in {"dev", "qa"}:
        return "EterniaLauncher"
    return "hermes-agent"


def _initial_state_for_persona(persona_id: str) -> TaskState:
    if persona_id == "neko_supervisor":
        return TaskState.CREATED
    if persona_id in {"dev", "backend_dev"}:
        return TaskState.RUNNING
    return TaskState.RUNNING


def _expected_action_for_persona(persona_id: str) -> str:
    return "run_slot"


def _has_completed_valid_matching_run(runs: list[Any], persona_id: str) -> bool:
    for run in runs:
        if getattr(run, "persona_id", None) != persona_id:
            continue
        latest_state = str(getattr(run, "state", "") or "")
        latest_completed = latest_state.endswith("completed") or latest_state == "RunState.COMPLETED"
        if latest_completed and _validation_status(run) == "valid":
            return True
    return False


def _affected_repos_for_persona(persona_id: str, requested: list[str]) -> list[str]:
    if requested:
        return requested
    if persona_id == "backend_dev":
        return ["EterniaBackend"]
    if persona_id == "dev":
        return ["EterniaLauncher"]
    if persona_id == "qa":
        return ["EterniaLauncher"]
    return ["hermes-agent"]


def _decision_type(final_decision: Any) -> str | None:
    if not isinstance(final_decision, dict):
        return None
    value = final_decision.get("type") or final_decision.get("decision_type")
    return str(value) if value else None


def _progress_value(run, key: str) -> str | None:
    progress = getattr(run, "progress", None) if run is not None else None
    if not isinstance(progress, dict):
        return None
    value = progress.get(key)
    return str(value) if value is not None else None


def _validation_status(run) -> str | None:
    explicit = _progress_value(run, "validation_status")
    if explicit:
        return explicit
    if run is not None and str(getattr(run, "state", "") or "") == "completed" and getattr(run, "final_decision", None):
        return "valid"
    return None


def _latest_total_tokens(run) -> int | None:
    if run is None:
        return None
    progress = getattr(run, "progress", None)
    if isinstance(progress, dict) and isinstance(progress.get("total_tokens"), int):
        return progress["total_tokens"]
    llm = getattr(run, "llm", None)
    if isinstance(llm, dict) and isinstance(llm.get("total_tokens"), int):
        return llm["total_tokens"]
    return None
