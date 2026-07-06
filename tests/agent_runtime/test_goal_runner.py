from __future__ import annotations

from hermes_time import now
from agent_runtime.locks import HarnessLockUnavailable

from agent_runtime.actions import HarnessActionType
from agent_runtime.goal_runner import GoalRunOptions, MissionRuntimeController
from agent_runtime.models import Proof
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, ProofStore, RunStore, TaskStore
from agent_runtime.ticker import RunUntilSettledResult
from agent_runtime.worklog import persona_worklog_for_task


class DoneEngine:
    def __init__(self, *, task_store, proof_store, **_kwargs):
        self.task_store = task_store
        self.proof_store = proof_store

    def run_until_settled(self, *, task_id, max_actions, max_seconds):
        task = self.task_store.get(task_id)
        task.state = TaskState.DONE
        task.updated_at = now()
        self.task_store.update(task, actor="harness", reason="test completion")
        self.proof_store.attach(
            Proof(
                id="proof_done",
                task_id=task_id,
                stage_id=None,
                type=ProofType.TEST_RUN,
                title="unit proof",
                path_or_value="pytest",
                created_by="dev",
                created_at=now(),
                metadata={"status": "passed"},
            )
        )
        return RunUntilSettledResult(
            settle_id="settle_test",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            ticks=2,
            actions_taken=[object(), object()],
            stop_reason="task_terminal",
            final_task_state="done",
            open_incidents=0,
            max_actions=max_actions,
            max_seconds=max_seconds,
        )


class MaxActionsEngine:
    def __init__(self, **_kwargs):
        pass

    def run_until_settled(self, *, task_id, max_actions, max_seconds):
        return RunUntilSettledResult(
            settle_id="settle_test",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            ticks=1,
            actions_taken=[],
            stop_reason="max_actions",
            final_task_state="created",
            open_incidents=0,
            max_actions=max_actions,
            max_seconds=max_seconds,
        )


class RecoverableBlockedMaxActionsEngine:
    def __init__(self, *, task_store, **_kwargs):
        self.task_store = task_store

    def run_until_settled(self, *, task_id, max_actions, max_seconds):
        task = self.task_store.get(task_id)
        task.state = TaskState.BLOCKED
        task.updated_at = now()
        self.task_store.update(task, actor="harness", reason="recoverable escalation")
        return RunUntilSettledResult(
            settle_id="settle_test",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            ticks=1,
            actions_taken=[],
            stop_reason="max_actions",
            final_task_state="blocked",
            open_incidents=0,
            max_actions=max_actions,
            max_seconds=max_seconds,
        )


class BlockedWithQaVerdictEngine:
    def __init__(self, *, task_store, proof_store, **_kwargs):
        self.task_store = task_store
        self.proof_store = proof_store

    def run_until_settled(self, *, task_id, max_actions, max_seconds):
        task = self.task_store.get(task_id)
        task.state = TaskState.BLOCKED
        task.updated_at = now()
        self.task_store.update(task, actor="qa", reason="missing proof")
        self.proof_store.attach(
            Proof(
                id="proof_qa_blocked",
                task_id=task_id,
                stage_id="qa_release",
                type=ProofType.QA_VERDICT,
                title="QA verdict: blocked",
                path_or_value="blocked",
                created_by="qa",
                created_at=now(),
                metadata={
                    "verdict": "blocked",
                    "findings": [{"severity": "blocker", "summary": "Contract proof is missing."}],
                    "proof_ids": ["proof_command"],
                },
            )
        )
        return RunUntilSettledResult(
            settle_id="settle_test",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            ticks=1,
            actions_taken=[object()],
            stop_reason="task_escalated",
            final_task_state="blocked",
            open_incidents=0,
            max_actions=max_actions,
            max_seconds=max_seconds,
        )


def test_goal_runner_creates_goal_runs_until_done_and_records_worklog(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=DoneEngine,
    ).run_goal(GoalRunOptions(title="T", description="D", acceptance_criteria=["done"], affected_repos=["harness"]))

    assert result.ok is True
    assert result.exit_code == 0
    assert result.stop_reason == "task_done"
    assert result.final_task_state == "done"
    assert result.proof_ids == ["proof_done"]
    assert result.hygiene["preserved_evidence"] is True
    assert [evt.payload["kind"] for evt in persona_worklog_for_task(result.task_id, persona_id="harness")] == [
        "goal_started",
        "goal_finished",
    ]
    task = TaskStore().get(result.task_id)
    assert task.acceptance_criteria == ["done"]
    assert task.affected_repos == ["harness"]


def test_goal_runner_creates_graph_routed_task_from_birth(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()

    result = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=task_store,
        engine_factory=MaxActionsEngine,
    ).run_goal(
        GoalRunOptions(
            title="T",
            description="D",
            acceptance_criteria=["done"],
            bindings={"builder": "persona:backend_dev"},
            max_actions=1,
        )
    )

    task = task_store.get(result.task_id)
    assert task.mission_plan is not None
    assert task.mission_plan.enabled
    assert task.mission_plan.stages
    assert task.mission_plan.blueprint_id == "neko_two_dev_default"
    assert task.mission_plan.bindings["builder"] == "backend_dev"
    assert task.mission_plan.bindings["lead"] == "neko_supervisor"
    assert task.current_stage_id == "scope"
    action = MissionStateMachine().next_action(task)
    assert action.type == HarnessActionType.RUN_SLOT
    assert action.slot_id == "lead"


def test_goal_runner_filters_default_bindings_for_explicit_blueprint(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()

    result = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=task_store,
        engine_factory=MaxActionsEngine,
    ).run_goal(
        GoalRunOptions(
            title="Visual proof",
            description="Capture and verify visual proof.",
            blueprint_id="visual_ui_qa",
            bindings={"builder": "persona:dev", "verifier": "persona:qa"},
            max_actions=1,
        )
    )

    task = task_store.get(result.task_id)
    assert task.mission_plan is not None
    assert task.mission_plan.blueprint_id == "visual_ui_qa"
    assert task.mission_plan.bindings == {"builder": "dev", "verifier": "qa"}
    assert task.current_stage_id == "implement_ui"


def test_goal_runner_explicit_no_edit_cross_stack_blueprint_uses_recipe_gates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()

    result = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=task_store,
        engine_factory=MaxActionsEngine,
    ).run_goal(
        GoalRunOptions(
            title="No-edit cross-stack proof",
            description="No-edit cross-stack proof for Backend and Launcher; do not modify product files.",
            acceptance_criteria=["Backend and Launcher no-product-edit proofs pass."],
            affected_repos=["EterniaBackend", "EterniaLauncher"],
            blueprint_id="neko_two_dev_default",
            max_actions=1,
        )
    )

    task = task_store.get(result.task_id)
    backend = next(stage for stage in task.mission_plan.stages if stage.id == "backend_implementation")
    launcher = next(stage for stage in task.mission_plan.stages if stage.id == "implement")

    assert backend.kind == "proof_only"
    assert backend.proof_recipe_id == "backend_contract_smoke"
    assert backend.proof_gate["proof_recipe_id"] == "backend_contract_smoke"
    assert launcher.kind == "proof_only"
    assert launcher.proof_recipe_id == "launcher_contract_smoke"
    assert launcher.proof_gate["proof_recipe_id"] == "launcher_contract_smoke"


def test_goal_runner_does_not_create_duplicate_task_when_tick_lock_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    class BusyLock:
        def __enter__(self):
            raise HarnessLockUnavailable("busy")

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("agent_runtime.goal_runner.tick_lock", lambda: BusyLock())

    result = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=DoneEngine,
    ).run_goal(GoalRunOptions(title="T", description="D"))

    assert result.ok is False
    assert result.task_id == ""
    assert result.final_task_state == "not_created"
    assert result.stop_reason == "tick_lock_unavailable"
    assert result.blocker_summary["kind"] == "active_tick_lock"
    assert TaskStore().list_all() == []


def test_goal_runner_archive_on_done_preserves_result_proof_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=DoneEngine,
    ).run_goal(GoalRunOptions(title="T", description="D", archive_on_done=True))

    assert result.ok is True
    assert result.proof_ids == ["proof_done"]
    assert result.proof_summary["command_proof_count"] == 1
    assert result.final_summary["proof_count"] == 1
    assert result.archive_result["archived_task_ids"] == [result.task_id]
    assert result.archive_result["archived_tasks"][0]["proof_ids"] == ["proof_done"]


def test_goal_runner_proof_summary_preserves_original_command_and_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=DoneEngine,
    ).run_goal(GoalRunOptions(title="T", description="D", acceptance_criteria=["done"], affected_repos=["harness"]))

    proof = result.proof_summary["command_proofs"][0]
    assert proof["original_command"] is None
    assert proof["command_adapter"] is None

    store = ProofStore()
    stored = store.get("proof_done")
    stored.metadata.update(
        {
            "command": "python -m pytest -o addopts='' -p no:timeout tests/agent_runtime/test_stage52_role_envelopes.py -q",
            "original_command": "python -m pytest tests/agent_runtime/test_stage52_role_envelopes.py -q",
            "command_adapter": "windows_pytest_timeout_disabled",
            "exit_code": 0,
            "status": "passed",
        }
    )
    store.attach(stored)

    refreshed = MissionRuntimeController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=MaxActionsEngine,
    )._build_result(
        task=TaskStore().get(result.task_id),
        settled=RunUntilSettledResult(
            settle_id="settle_test",
            started_at=now(),
            finished_at=now(),
            task_id=result.task_id,
            ticks=1,
            actions_taken=[],
            stop_reason="task_terminal",
            final_task_state="done",
            open_incidents=0,
            max_actions=1,
            max_seconds=None,
        ),
        hygiene={},
        normalized_stop="task_done",
        exit_code=0,
        elapsed_seconds=0.0,
        archive_result=None,
        next_actions=[],
    )

    proof = refreshed.proof_summary["command_proofs"][0]
    assert proof["command"].startswith("python -m pytest -o addopts='' -p no:timeout")
    assert proof["original_command"] == "python -m pytest tests/agent_runtime/test_stage52_role_envelopes.py -q"
    assert proof["command_adapter"] == "windows_pytest_timeout_disabled"


def test_goal_runner_returns_nonzero_on_max_actions_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = MissionRuntimeController(config=RuntimeConfig(), engine_factory=MaxActionsEngine).run_goal(
        GoalRunOptions(title="T", description="D", max_actions=1)
    )

    assert result.ok is False
    assert result.exit_code == 3
    assert result.stop_reason == "max_actions"
    assert result.next_actions == ["Increase the bound only if events show forward progress."]


def test_goal_runner_does_not_turn_recoverable_blocked_state_into_terminal_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = MissionRuntimeController(config=RuntimeConfig(), engine_factory=RecoverableBlockedMaxActionsEngine).run_goal(
        GoalRunOptions(title="T", description="D", max_actions=1)
    )

    assert result.ok is False
    assert result.exit_code == 3
    assert result.final_task_state == "blocked"
    assert result.stop_reason == "max_actions"
    assert result.final_summary["blocker_kind"] == "state_escalated"


def test_goal_runner_blocked_without_incident_surfaces_qa_blocker_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = MissionRuntimeController(config=RuntimeConfig(), engine_factory=BlockedWithQaVerdictEngine).run_goal(
        GoalRunOptions(title="T", description="D")
    )

    assert result.ok is False
    assert result.stop_reason == "task_escalated"
    assert result.open_incident_ids == []
    assert result.final_summary["blocker_kind"] == "qa_verdict_blocker"
    assert result.proof_summary["latest_qa_verdict"]["proof_id"] == "proof_qa_blocked"
    assert result.blocker_summary["findings"][0]["summary"] == "Contract proof is missing."
    assert result.next_actions == [
        "Inspect blocker_summary.latest QA findings, attach missing proof or route a bounded fix to the responsible persona."
    ]
