from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from hermes_time import now

from agent_runtime import paths
from agent_runtime.autonomy import record_autonomy_packet
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.context_builder import build_context
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.models import AgentPersona, Proof, Task
from agent_runtime.observability import build_observability
from agent_runtime.proof_recipes import RECIPES
from agent_runtime.proof_rules import ProofType
from agent_runtime.proof_runner import CommandProofRunner
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import RunState, TaskState, WorkerSessionState
from agent_runtime.status import build_status
from agent_runtime.store import ProofStore, RunStore, TaskStore
from agent_runtime.ticker import TickEngine
from agent_runtime.worker_sessions import WorkerSessionStore, worker_context_manifest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _task(task_id: str = "task_worker", state: TaskState = TaskState.DEV_IMPLEMENTING) -> Task:
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
            payload={"stage_id": "stage_1", "commands": ["printf worker-ok\\n"]},
        )


class RequestRecipeRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect recipe proof",
            rationale="Recipe proof avoids command rediscovery.",
            payload={"stage_id": "stage_1", "recipe_id": "archive_button_cli_contract"},
        )


class PassingProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store
        self.calls = []

    def run_commands(self, task, *, stage_id, run_id, actor, commands, proof_recipe=None, **_kwargs):
        self.calls.append({"commands": list(commands), "proof_recipe": proof_recipe})
        proof = Proof(
            id=f"proof_{task.id}_{len(self.calls)}",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="passing proof",
            path_or_value="proof.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "run_id": run_id, "actor_requested": actor},
            redaction_status="safe",
        )
        return [self.proof_store.attach(proof)]


def _enterprise_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            mode="observe_only",
            worker_session_store=True,
            same_session_continuation=True,
        )
    )


def test_enterprise_worker_session_tracks_tick_context_and_proof(isolate_agent_runtime_root):
    tasks = TaskStore()
    proofs = ProofStore()
    workers = WorkerSessionStore()
    task = _task()
    tasks.create(task)
    runner = PassingProofRunner(proofs)

    result = TickEngine(
        task_store=tasks,
        proof_store=proofs,
        persona_runtime=RequestTestRunRuntime(),
        proof_runner=runner,
        worker_session_store=workers,
        config=_enterprise_config(),
    ).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    worker = workers.list_for_task(task.id)[0]
    assert result.actions_taken[0].payload["worker_session_id"] == worker.id
    assert worker.context_receipt_id.startswith("ctxr_")
    assert worker.decision_count == 1
    assert worker.proof_count == 1
    assert worker.prompt_contract_hash
    manifest = worker_context_manifest(task.id, "dev")
    assert "static_prompt_receipt.json" in manifest["files"]


def test_request_test_run_recipe_normalizes_commands_and_metadata():
    tasks = TaskStore()
    proofs = ProofStore()
    task = _task("task_recipe")
    tasks.create(task)
    runner = PassingProofRunner(proofs)

    result = TickEngine(
        task_store=tasks,
        proof_store=proofs,
        persona_runtime=RequestRecipeRuntime(),
        proof_runner=runner,
    ).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    assert runner.calls[0]["commands"] == list(RECIPES["archive_button_cli_contract"].commands)
    assert runner.calls[0]["proof_recipe"]["recipe_id"] == "archive_button_cli_contract"
    assert "proof_recipe" not in result.actions_taken[0].payload
    saved = tasks.get(task.id)
    assert len(saved.proof_ids) == 1


def test_autonomy_packet_advertises_relevant_proof_recipes():
    tasks = TaskStore()
    runs = RunStore()
    task = _task("task_recipe_hud")
    task.title = "Launcher contract smoke"
    tasks.create(task)
    run = runs.open_run("dev", task.id, stage_id="launcher_contract_smoke")
    ctx = build_context(task, run)

    packet = record_autonomy_packet(_persona("dev"), ctx, run_store=runs)

    recipe_ids = {item["recipe_id"] for item in packet["available_proof_recipes"]}
    assert "launcher_contract_smoke" in recipe_ids


def test_no_edit_recipe_fails_when_command_dirties_git_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    task = _task("task_dirty_recipe")
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=repo, timeout_seconds=10)

    proof = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=["python -c \"open('probe.txt','w',encoding='utf-8').write('dirty')\""],
        proof_recipe={
            "recipe_id": "no_edit_dirty_guard",
            "recipe_hash": "abc123",
            "mode": "no_product_edit",
            "writes_product_probe": False,
            "cleanup": "manifest_verified",
        },
    )[0]

    assert proof.metadata["exit_code"] == 0
    assert proof.metadata["status"] == "failed"
    assert proof.metadata["dirty_delta_status"] == "dirty_delta_blocked"
    assert proof.metadata["dirty_delta_count"] == 1
    assert (repo / "probe.txt").exists()
    sandbox_manifest = paths.store_root() / proof.metadata["proof_recipe_sandbox_manifest"]
    assert sandbox_manifest.exists()


def test_recipe_expected_markers_are_enforced(tmp_path):
    task = _task("task_marker_recipe")
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    proof = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=["python -c \"print('wrong-output')\""],
        proof_recipe={
            "recipe_id": "marker_guard",
            "recipe_hash": "def456",
            "mode": "no_product_edit",
            "expected_markers": ["required_marker"],
        },
    )[0]

    assert proof.metadata["exit_code"] == 0
    assert proof.metadata["status"] == "failed"
    assert proof.metadata["missing_expected_markers"] == ["required_marker"]


def test_recipe_expected_markers_can_be_command_specific(tmp_path):
    task = _task("task_marker_by_command_recipe")
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    proofs = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=[
            "python -c \"print('first_marker')\"",
            "python -c \"print('second_marker')\"",
        ],
        proof_recipe={
            "recipe_id": "marker_by_command_guard",
            "recipe_hash": "def789",
            "mode": "no_product_edit",
            "expected_markers": ["first_marker", "second_marker"],
            "expected_markers_by_command": [["first_marker"], ["second_marker"]],
        },
    )

    assert [proof.metadata["status"] for proof in proofs] == ["passed", "passed"]


def test_snapshot_status_and_observability_surface_worker_sessions():
    tasks = TaskStore()
    workers = WorkerSessionStore()
    task = _task("task_observe")
    tasks.create(task)
    worker = workers.open(task_id=task.id, persona=_persona(), stage_id="stage_1", session_id="session_safe")
    worker.last_heartbeat_at = now() - timedelta(seconds=1000)
    workers.update(worker)

    status = build_status(task_store=tasks, worker_session_store=workers)
    snapshot = build_snapshot(task_store=tasks, worker_session_store=workers)
    obs = build_observability(
        tasks=[task],
        runs=[],
        incidents=[],
        proofs=[],
        daemon_status={"state": "offline"},
        worker_sessions=workers.list_all(),
        reference_time=now(),
        run_stalled_after_seconds=10,
    )

    assert status["active_worker_sessions"] == 1
    assert snapshot["summary"]["active_worker_sessions"] == 1
    assert snapshot["tasks"][0]["active_worker_session_ids"] == [worker.id]
    assert obs["signals"]["stale_worker_sessions"] == 1
    assert obs["interventions"][0]["kind"] == "worker_stale_heartbeat"


def test_worker_cli_lists_and_controls_sessions(isolate_agent_runtime_root):
    worker = WorkerSessionStore().open(task_id="task_cli", persona=_persona(), stage_id="stage_1", session_id="session_safe")

    listed = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "worker", "list", "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    rows = json.loads(listed.stdout)
    assert rows[0]["worker_session_id"] == worker.id

    possessed = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "worker", "possess", worker.id, "--actor", "qa", "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(possessed.stdout)["possession_state"] == "possessed"

    released = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "worker", "release", worker.id, "--actor", "qa", "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert json.loads(released.stdout)["possession_state"] == "available"


def test_terminal_run_update_marks_worker_idle_and_clears_active_run(isolate_agent_runtime_root):
    runs = RunStore()
    workers = WorkerSessionStore()
    run = runs.open_run("dev", "task_terminal_worker", stage_id="stage_1", session_id="session_safe")
    worker = workers.open(task_id=run.task_id, persona=_persona(), stage_id=run.stage_id, session_id=run.session_id)
    workers.assign_run(worker.id, run)

    completed = runs.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "request_test_run"})
    updated = workers.update_after_run(worker.id, completed, close_reason="tick_completed", count_decision=False)

    assert updated.state == WorkerSessionState.IDLE
    assert updated.active_run_id is None
    assert workers.find_active(task_id=run.task_id) == []


def test_worker_run_refresh_does_not_double_count_llm_budget(isolate_agent_runtime_root):
    runs = RunStore()
    workers = WorkerSessionStore()
    run = runs.open_run("dev", "task_worker_budget", stage_id="stage_1", session_id="session_safe")
    run.llm = {"total_tokens": 120, "tool_turns": 3}
    runs.update(run)
    worker = workers.open(task_id=run.task_id, persona=_persona(), stage_id=run.stage_id, session_id=run.session_id)
    workers.assign_run(worker.id, run)

    first = workers.update_after_run(worker.id, run)
    completed = runs.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "request_test_run"})
    second = workers.update_after_run(worker.id, completed, close_reason="tick_completed", count_decision=False)

    assert first.token_budget_used == 120
    assert first.tool_budget_used == 3
    assert second.token_budget_used == 120
    assert second.tool_budget_used == 3
    assert second.decision_count == 1


def test_run_cancel_cli_marks_owning_worker_idle(isolate_agent_runtime_root):
    runs = RunStore()
    workers = WorkerSessionStore()
    run = runs.open_run("dev", "task_cancel_worker", stage_id="stage_1", session_id="session_safe")
    worker = workers.open(task_id=run.task_id, persona=_persona(), stage_id=run.stage_id, session_id=run.session_id)
    workers.assign_run(worker.id, run)

    cancelled = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "run", "cancel", run.id, "--reason", "test cancel", "--json"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(cancelled.stdout)
    assert payload["updated_worker_session_ids"] == [worker.id]
    updated = workers.get(worker.id)
    assert updated.state == WorkerSessionState.IDLE
    assert updated.active_run_id is None
    assert workers.find_active(task_id=run.task_id) == []


def test_archive_refuses_active_worker_then_preserves_closed_worker_context_and_sandbox(isolate_agent_runtime_root):
    tasks = TaskStore()
    task = _task("task_archive_worker", state=TaskState.DONE)
    tasks.create(task)
    workers = WorkerSessionStore()
    worker = workers.open(task_id=task.id, persona=_persona(), stage_id="stage_1", session_id="session_safe")
    sandbox = paths.proof_sandbox_dir(task.id, "recipe")
    sandbox.mkdir(parents=True)
    (sandbox / "manifest.json").write_text("{}", encoding="utf-8")

    refused = tasks.archive(task.id, actor="cli", reason="operator archive")
    assert refused["archived_count"] == 0
    assert refused["skipped_tasks"][0]["reason"] == "active_worker_sessions"

    workers.close(worker.id, reason="test complete")
    archived = tasks.archive(task.id, actor="cli", reason="operator archive")

    batch = paths.deleted_archive_dir() / archived["archive_batch"]
    assert archived["archived_count"] == 1
    assert archived["archived_tasks"][0]["worker_session_ids"] == [worker.id]
    assert archived["archived_tasks"][0]["worker_context_archived"] is True
    assert archived["archived_tasks"][0]["proof_sandbox_archived"] is True
    assert (batch / "worker_sessions" / f"{worker.id}.json").exists()
    assert (batch / "context" / task.id / "dev" / "static_prompt_receipt.json").exists()
    assert (batch / "proof_sandbox" / task.id / "recipe" / "manifest.json").exists()


def test_complete_task_action_closes_active_workers():
    tasks = TaskStore()
    workers = WorkerSessionStore()
    task = _task("task_complete_worker", state=TaskState.QA_APPROVED)
    task.proof_ids = ["proof_ok"]
    tasks.create(task)
    worker = workers.open(task_id=task.id, persona=_persona(), stage_id="stage_1", session_id="session_safe")

    result = TickEngine(task_store=tasks, worker_session_store=workers).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    assert result.actions_taken[0].payload["closed_worker_session_ids"] == [worker.id]
    assert workers.get(worker.id).state.value == "closed"
