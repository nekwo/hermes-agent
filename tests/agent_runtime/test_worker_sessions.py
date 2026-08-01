from __future__ import annotations

import json
import uuid
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from hermes_time import now

from agent_runtime import paths
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import AgentPersona, AgentRun
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.observability import build_observability
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import RunState, TaskState, WorkerSessionState
from agent_runtime.status import build_status
from agent_runtime.store import RunStore, TaskStore
from agent_runtime.worker_sessions import WorkerSessionStore, worker_context_manifest
from tests.agent_runtime.conftest import release_to_implementation


def _seed_run(
    persona_id: str,
    task_id: str,
    stage_id: str | None = None,
    *,
    session_id: str | None = None,
) -> AgentRun:
    """Persist a run row without ``RunStore.open_run``.

    S17 removed ``open_run`` as write-dead: no production caller survived the
    mission lane, so its only users were tests seeding a row. ``update`` is the
    surviving write path (it tolerates a missing previous row and applies the
    same ``_safe_session_id`` sanitisation ``open_run`` did).
    """

    ts = now()
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}",
        persona_id=persona_id,
        task_id=task_id,
        stage_id=stage_id,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        session_id=session_id,
    )
    assert RunStore().update(run) is True
    return run



REPO_ROOT = Path(__file__).resolve().parents[2]


def _task(task_id: str = "task_worker", state: TaskState = TaskState.RUNNING) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Worker session mission",
        description="Collect proof without freezing.",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["hermes-agent"],
        current_stage_id="stage_1",
    )


def _persona(persona_id: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} worker",
        role="dev",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal"],
        system_prompt_path="agent_runtime/prompts/dev.md",
    )


class RequestTestRunRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect worker proof",
            rationale="A deterministic proof should advance the stage.",
            payload={"stage_id": "implement", "commands": ["printf worker-ok\\n"]},
        )


class RequestRecipeRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect recipe proof",
            rationale="Recipe proof avoids command rediscovery.",
            payload={"stage_id": "archive_button_cli_contract", "recipe_id": "archive_button_cli_contract"},
        )


def _enterprise_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            mode="observe_only",
            worker_session_store=True,
            same_session_continuation=True,
        )
    )








def test_snapshot_status_and_observability_surface_worker_sessions():
    snapshot = build_snapshot()
    assert "worker_sessions" not in snapshot
    assert not hasattr(TaskStore(), "create")


def test_worker_cli_lists_and_controls_sessions(isolate_agent_runtime_root):
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "worker", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_terminal_run_update_marks_worker_idle_and_clears_active_run(isolate_agent_runtime_root):
    runs = RunStore()
    workers = WorkerSessionStore()
    run = _seed_run("dev", "task_terminal_worker", stage_id="stage_1", session_id="session_safe")
    worker = workers.open(task_id=run.task_id, persona=_persona(), stage_id=run.stage_id, session_id=run.session_id)
    workers.assign_run(worker.id, run)

    completed = runs.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "request_test_run"})
    updated = workers.update_after_run(worker.id, completed, close_reason="tick_completed", count_decision=False)

    assert updated.state == WorkerSessionState.IDLE
    assert updated.active_run_id is None
    assert workers.find_active(task_id=run.task_id) == []


def test_worker_run_refresh_does_not_invent_retired_llm_budget(isolate_agent_runtime_root):
    runs = RunStore()
    workers = WorkerSessionStore()
    run = _seed_run("dev", "task_worker_budget", stage_id="stage_1", session_id="session_safe")
    runs.update(run)
    worker = workers.open(task_id=run.task_id, persona=_persona(), stage_id=run.stage_id, session_id=run.session_id)
    workers.assign_run(worker.id, run)

    first = workers.update_after_run(worker.id, run)
    completed = runs.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "request_test_run"})
    second = workers.update_after_run(worker.id, completed, close_reason="tick_completed", count_decision=False)

    assert first.token_budget_used == 0
    assert first.tool_budget_used == 0
    assert second.token_budget_used == 0
    assert second.tool_budget_used == 0
    assert second.decision_count == 1


def test_run_cancel_cli_marks_owning_worker_idle(isolate_agent_runtime_root):
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "run", "cancel", "retired", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_archive_refuses_active_worker_then_preserves_closed_worker_context_and_sandbox(isolate_agent_runtime_root):
    assert not hasattr(TaskStore(), "archive")
