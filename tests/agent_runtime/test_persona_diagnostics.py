from __future__ import annotations

import pytest

from hermes_time import now

from agent_runtime.actions import HarnessActionResult, HarnessActionType
from agent_runtime.persona_diagnostics import (
    PersonaDiagnosticController,
    PersonaDiagnosticOptions,
)
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.persona_assignments import PersonaAssignmentStore
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import RunState
from agent_runtime.store import IncidentStore, ProofStore, RunStore, TaskStore
from agent_runtime.ticker import RunUntilSettledResult


class OnePersonaEngine:
    def __init__(self, *, task_store, run_store, config, **_kwargs):
        self.task_store = task_store
        self.run_store = run_store
        self.config = config

    def run_until_settled(self, *, task_id, max_actions, max_seconds):
        task = self.task_store.get(task_id)
        action = MissionStateMachine(config=self.config).next_action(task)
        persona_id = _persona_for_action(action, task)
        run = self.run_store.open_run(persona_id, task.id, task.current_stage_id)
        run.progress = {"validation_status": "valid", "total_tokens": 123}
        self.run_store.update(run)
        self.run_store.close_run(
            run.id,
            state=RunState.COMPLETED,
            final_decision={"type": "diagnostic_ack", "payload": {"persona_id": persona_id}},
        )
        return RunUntilSettledResult(
            settle_id="settle_test",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            ticks=1,
            actions_taken=[HarnessActionResult(action, True, "diagnostic turn completed")],
            stop_reason="max_actions",
            final_task_state=task.state.value,
            open_incidents=0,
            max_actions=max_actions,
            max_seconds=max_seconds,
        )


class FailedPersonaEngine:
    def __init__(self, *, task_store, run_store, config, **_kwargs):
        self.task_store = task_store
        self.run_store = run_store
        self.config = config

    def run_until_settled(self, *, task_id, max_actions, max_seconds):
        task = self.task_store.get(task_id)
        action = MissionStateMachine(config=self.config).next_action(task)
        persona_id = _persona_for_action(action, task)
        run = self.run_store.open_run(persona_id, task.id, task.current_stage_id)
        run.progress = {"validation_status": "invalid", "total_tokens": 123}
        self.run_store.update(run)
        self.run_store.close_run(
            run.id,
            state=RunState.FAILED,
            error={"class": "AttributeError", "message": "'str' object has no attribute 'get'"},
        )
        return RunUntilSettledResult(
            settle_id="settle_failed",
            started_at=now(),
            finished_at=now(),
            task_id=task_id,
            ticks=1,
            actions_taken=[HarnessActionResult(action, False, "runtime failed", {"run_id": run.id})],
            stop_reason="action_failed",
            final_task_state=task.state.value,
            open_incidents=1,
            max_actions=max_actions,
            max_seconds=max_seconds,
        )


def test_persona_diagnostic_routes_neko_first(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = PersonaDiagnosticController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=OnePersonaEngine,
    ).diagnose(PersonaDiagnosticOptions(persona_id="neko", title="Neko diag", message="Scope this only."))

    assert result.ok is True
    assert result.operation_id.startswith("personaop_")
    assert result.operation_kind == "diagnostic"
    assert result.operation_mode == "standalone_task"
    assert result.persona_id == "neko_supervisor"
    assert result.expected_first_action == "run_slot"
    assert result.actual_actions == ["run_slot"]
    assert result.latest_decision_type == "diagnostic_ack"
    assert result.latest_validation_status == "valid"
    assert result.latest_total_tokens == 123
    assert result.stage_id == "scope"
    assert result.final_task_state == "done"


def test_persona_diagnostic_records_assignment_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    cfg = RuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        )
    )

    result = PersonaDiagnosticController(
        config=cfg,
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=OnePersonaEngine,
    ).diagnose(PersonaDiagnosticOptions(persona_id="neko", title="Neko diag", message="Scope this only."))

    assert result.ok is True
    assert result.assignment_id
    assert result.persona_instance_id == f"personainst_{result.task_id}_neko_supervisor"
    assignment = PersonaAssignmentStore().get(result.assignment_id)
    assert assignment.state == "completed"
    assert assignment.task_id == result.task_id
    assert assignment.run_ids == result.run_ids


def test_persona_diagnostic_auto_archives_task_unless_preserved(tmp_path, monkeypatch):
    # State-hygiene: a standalone diagnostic must not linger in the live runtime
    # (open/done-but-unarchived) where it accumulates and gates the scheduler.
    # preserve_open_task=False (the CLI default) auto-archives it; True keeps it.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()
    controller = PersonaDiagnosticController(
        config=RuntimeConfig(),
        task_store=task_store,
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=OnePersonaEngine,
    )

    kept = controller.diagnose(PersonaDiagnosticOptions(persona_id="dev", title="keep", message="m", preserve_open_task=True))
    assert task_store.get(kept.task_id).id == kept.task_id  # preserved in the live store

    archived = controller.diagnose(PersonaDiagnosticOptions(persona_id="dev", title="drop", message="m", preserve_open_task=False))
    with pytest.raises(Exception):
        task_store.get(archived.task_id)  # auto-archived out of the live store


def test_persona_diagnostic_records_repo_clean_baseline_from_hygiene(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    task_store = TaskStore()

    def hygiene_fn(**_kwargs):
        return {
            "dirty_state_after_cleanup": {
                "repos": [
                    {
                        "label": "EterniaBackend",
                        "dirty": True,
                        "dirty_count": 2,
                        "error": None,
                        "status_excerpt": ["M posts/models.py", "M media/services.py"],
                    }
                ]
            }
        }

    result = PersonaDiagnosticController(
        config=RuntimeConfig(),
        task_store=task_store,
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=OnePersonaEngine,
        hygiene_fn=hygiene_fn,
    ).diagnose(PersonaDiagnosticOptions(persona_id="backend-dev", title="Backend diag", message="Probe backend."))

    task = task_store.get(result.task_id)
    baseline = task.harness_self_heal["repo_clean_baseline"]
    assert baseline["repos"] == [
        {
            "label": "EterniaBackend",
            "dirty": True,
            "dirty_count": 2,
            "error": None,
            "status_excerpt": ["M posts/models.py", "M media/services.py"],
        }
    ]
    assert baseline["created_at"]


def test_persona_diagnostic_fails_when_matching_run_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = PersonaDiagnosticController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=FailedPersonaEngine,
    ).diagnose(PersonaDiagnosticOptions(persona_id="qa", title="QA diag", message="Run one bounded QA diagnostic."))

    assert result.ok is False
    assert result.exit_code == 3
    assert result.latest_validation_status == "invalid"
    assert result.stop_reason == "action_failed"
    assert any("ended in state" in item for item in result.next_actions)


def test_persona_diagnostic_routes_backend_dev_with_typed_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = PersonaDiagnosticController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=OnePersonaEngine,
    ).diagnose(
        PersonaDiagnosticOptions(
            persona_id="backend-dev",
            title="Backend diag",
            message="Explain the backend proof path.",
        )
    )

    assert result.ok is True
    assert result.persona_id == "backend_dev"
    assert result.expected_first_action == "run_slot"
    assert result.actual_actions == ["run_slot"]
    assert result.stage_id == "diagnostic_backend_dev"
    assert result.stage_owner == "backend_dev"
    assert result.stage_repo == "EterniaBackend"


@pytest.mark.parametrize(
    ("persona_id", "expected_persona", "expected_action", "expected_owner", "expected_repo"),
    [
        ("launcher-dev", "dev", "run_slot", "dev", "EterniaLauncher"),
        ("qa", "qa", "run_slot", "qa", "EterniaLauncher"),
    ],
)
def test_persona_diagnostic_routes_launcher_dev_and_qa(
    tmp_path,
    monkeypatch,
    persona_id,
    expected_persona,
    expected_action,
    expected_owner,
    expected_repo,
):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    result = PersonaDiagnosticController(
        config=RuntimeConfig(),
        task_store=TaskStore(),
        run_store=RunStore(),
        proof_store=ProofStore(),
        incident_store=IncidentStore(),
        engine_factory=OnePersonaEngine,
    ).diagnose(
        PersonaDiagnosticOptions(
            persona_id=persona_id,
            title="Persona diag",
            message="Explain the current operating contract.",
            operation_kind="contract_probe",
        )
    )

    assert result.ok is True
    assert result.persona_id == expected_persona
    assert result.operation_kind == "contract_probe"
    assert result.expected_first_action == expected_action
    assert result.actual_actions == [expected_action]
    assert result.stage_owner == expected_owner
    assert result.stage_repo == expected_repo


def test_persona_diagnostic_rejects_unknown_persona(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    with pytest.raises(ValueError, match="unsupported diagnostic persona"):
        PersonaDiagnosticController(config=RuntimeConfig()).diagnose(
            PersonaDiagnosticOptions(persona_id="designer", title="Bad", message="Bad")
        )


def _persona_for_action(action, task) -> str:
    if action.type != HarnessActionType.RUN_SLOT:
        raise AssertionError(f"unexpected action {action.type}")
    if action.slot_id in {"neko_supervisor", "qa", "backend_dev"}:
        return action.slot_id
    if action.slot_id == "dev":
        stage = task.mission_plan.stages[0] if task.mission_plan and task.mission_plan.stages else None
        return stage.owner if stage and stage.owner in {"dev", "backend_dev"} else "dev"
    raise AssertionError(f"unexpected slot {action.slot_id}")
