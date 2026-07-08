from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_time import now

from agent_runtime.actions import HarnessAction, HarnessActionType
from agent_runtime.blueprints import BlueprintStore, instantiate_blueprint
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, PersonaInstance, Proof, Task
from agent_runtime.mission_chat_turns import mission_chat_turn_elements
from agent_runtime.persona_assignments import (
    ACTIVE_ASSIGNMENT_STATES,
    ChatBusyError,
    PersonaAssignmentSpec,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_instance_summary,
    persona_instance_id_for,
    persona_instance_id_for_placement,
)
from agent_runtime.persona_chat_history import persona_chat_history_summary
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import RunState, TaskState, WorkerSessionState
from agent_runtime.status import build_status
from agent_runtime.store import AgentStore, ProofStore, RunStore, TaskStore
from agent_runtime.ticker import TickEngine
from agent_runtime.worker_sessions import WorkerSessionStore

REPO_ROOT = Path(__file__).resolve().parents[2]


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
        hermes_profile=f"profile-{persona_id}",
    )


def _task(task_id: str = "task_assign", state: TaskState = TaskState.RUNNING) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Persona assignment mission",
        description="Exercise assignment runtime.",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["hermes-agent"],
        current_stage_id="stage_1",
    )


def _assignment_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        )
    )


class RequestProofRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.REQUEST_TEST_RUN,
            summary="collect assignment proof",
            rationale="The proof should attach to the active assignment.",
            payload={"stage_id": "implement", "commands": ["python -c \"print('assignment-ok')\""]},
        )


class PassingProofRunner:
    def __init__(self, proof_store: ProofStore):
        self.proof_store = proof_store

    def run_commands(self, task, *, stage_id, run_id, actor, commands, **_kwargs):
        proof = Proof(
            id="proof_assignment_ok",
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title="assignment proof",
            path_or_value="assignment.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "run_id": run_id, "actor_requested": actor},
            redaction_status="safe",
        )
        return [self.proof_store.attach(proof)]


class NekoAcceptanceRuntime:
    def run_tick(self, persona, ctx, *, run):
        return AgentDecision(
            type=DecisionType.PROPOSE_ACCEPTANCE,
            summary="scoped assignment runtime smoke",
            rationale="The diagnostic assignment is already bounded and should stay attached to this Neko run.",
            payload={"objective": "verify assignment reuse", "acceptance_criteria": ["one assignment identity"]},
        )


class _FakeSessionDB:
    def __init__(self, messages_by_session: dict[str, list[dict]]):
        self.messages_by_session = messages_by_session

    def list_sessions_rich(self, source=None, limit=100, **_kwargs):
        return [
            {
                "id": session_id,
                "session_id": session_id,
                "source": "agent_runtime_persona_chat",
                "title": session_id,
            }
            for session_id in self.messages_by_session
        ][:limit]

    def get_messages(self, session_id, include_inactive=False):
        return self.messages_by_session.get(session_id, [])


def test_persona_instance_store_derives_singleton_from_worker_session(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    workers = WorkerSessionStore()
    worker = workers.open(task_id="task_1", persona=_persona("dev"), stage_id="stage_1", assignment_id="assign_1")

    instances = store.derive_from_workers([_persona("dev"), _persona("qa")], workers.list_all())

    by_id = {item.persona_id: item for item in instances}
    assert by_id["dev"].id == "personainst_dev"
    assert by_id["dev"].active_worker_session_id == worker.id
    assert by_id["dev"].current_assignment_id == "assign_1"
    assert by_id["qa"].id == "personainst_qa"


def test_worker_projection_replaces_stale_goal_id_with_assignment_goal(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    assignments = PersonaAssignmentStore()
    workers = WorkerSessionStore()
    stale = store.ensure_for_persona(_persona("dev"))
    stale.mode = "task_bound"
    stale.current_task_id = "task_stale"
    stale.goal_id = "task_stale"
    stale.spawned_by = "personainst_neko_supervisor"
    stale = store.update(stale)
    assignment = assignments.create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="task_stage",
            title="Implement",
            message="Run the active task.",
            task_id="task_live",
            goal_id="goal_live",
            stage_id="implement",
        )
    )
    worker = workers.open(
        task_id="task_live",
        persona=_persona("dev"),
        stage_id="implement",
        assignment_id=assignment.id,
    )

    updated = store.update_from_worker(worker)

    assert updated.id == stale.id == "personainst_dev"
    assert updated.current_task_id == "task_live"
    assert updated.goal_id == "goal_live"


def test_persona_instance_derivation_clears_stale_worker_projection(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    workers = WorkerSessionStore()
    worker = workers.open(task_id="task_1", persona=_persona("dev"), stage_id="stage_1", assignment_id="assign_1")
    workers.assign_run(worker.id, RunStore().open_run("dev", "task_1", "stage_1", session_id="session_safe"))
    store.derive_from_workers([_persona("dev")], workers.list_all())
    workers.close(worker.id, reason="archived")

    instances = store.derive_from_workers([_persona("dev")], workers.find_active())

    instance = instances[0]
    assert instance.state == WorkerSessionState.IDLE
    assert instance.current_assignment_id is None
    assert instance.current_task_id is None
    assert instance.active_worker_session_id is None
    assert instance.active_run_id is None
    assert instance.session_id is None


def test_persona_instance_derivation_does_not_mark_idle_worker_active(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    workers = WorkerSessionStore()
    runs = RunStore()
    worker = workers.open(
        task_id="task_1",
        persona=_persona("dev"),
        stage_id="stage_1",
        assignment_id="assign_1",
    )
    run = runs.open_run("dev", "task_1", "stage_1", session_id="session_safe")
    worker = workers.assign_run(worker.id, run)
    run = runs.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "done", "summary": "done"})
    workers.update_after_run(worker.id, run, close_reason="tick_completed")

    instances = store.derive_from_workers([_persona("dev")], workers.list_all())

    instance = instances[0]
    assert instance.state == WorkerSessionState.IDLE
    assert instance.current_task_id == "task_1"
    assert instance.active_worker_session_id is None
    assert instance.active_run_id is None


def test_task_terminal_reaps_task_bound_persona_instances(isolate_agent_runtime_root):
    tasks = TaskStore()
    store = PersonaInstanceStore()
    task = tasks.create(_task("task_terminal_reap", state=TaskState.RUNNING))
    instance = store.ensure_for_goal(
        _persona("dev"),
        goal_id=task.id,
        spawned_by="personainst_neko_supervisor",
    )

    task.state = TaskState.DONE
    tasks.update(task, actor="harness", reason="completed")

    assert instance.id not in {item.id for item in store.list_all()}


def test_persona_instance_sweep_reaps_orphans_but_preserves_live_workers(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    workers = WorkerSessionStore()
    orphan = store.ensure_for_goal(
        _persona("dev"),
        goal_id="task_archived",
        spawned_by="personainst_neko_supervisor",
    )
    live = store.ensure_for_goal(
        _persona("backend_dev"),
        goal_id="task_missing_but_worker_live",
        spawned_by="personainst_neko_supervisor",
    )
    worker = workers.open(
        task_id="task_missing_but_worker_live",
        persona=_persona("backend_dev"),
        stage_id="backend_implementation",
        assignment_id="assign_live",
    )
    live.active_worker_session_id = worker.id
    live.state = WorkerSessionState.RUNNING
    store.update(live)
    free = store.create_free_floating("profile:reviewer")

    report = store.sweep_orphaned_task_bound_instances(reason="test janitor")
    remaining = {item.id for item in store.list_all()}

    assert orphan.id not in remaining
    assert live.id in remaining
    assert free.id in remaining
    assert report["reaped_persona_instance_ids"] == [orphan.id]
    assert report["skipped_active_persona_instance_ids"] == [live.id]
    assert report["remaining_task_bound_persona_instance_ids"] == [live.id]


def test_task_archive_moves_task_bound_persona_instance_evidence(isolate_agent_runtime_root):
    tasks = TaskStore()
    store = PersonaInstanceStore()
    task = tasks.create(_task("task_archive_persona_instance", state=TaskState.DONE))
    instance = store.ensure_for_goal(
        _persona("qa"),
        goal_id=task.id,
        spawned_by="personainst_dev",
    )

    result = tasks.archive(task.id, actor="cli", reason="cleanup terminal task")

    archived = result["archived_tasks"][0]
    assert archived["persona_instance_ids"] == [instance.id]
    assert archived["persona_instances_archived"] is True
    assert instance.id not in {item.id for item in store.list_all()}


def test_create_free_floating_instance_reuses_canonical_idle_placement(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    first = store.create_free_floating("profile:reviewer")
    second = store.create_free_floating("profile:reviewer")

    assert first.id == second.id == persona_instance_id_for("profile:reviewer")
    assert first.persona_id == "profile:reviewer"
    assert first.role == "profile"
    assert first.profile_id == "reviewer"
    assert first.mode == "free_floating"
    assert first.state == WorkerSessionState.IDLE
    assert first.current_task_id is None
    assert first.active_worker_session_id is None

    instances = store.derive_from_workers([_persona("dev")], [])
    by_id = {item.id: item for item in instances}

    assert by_id[first.id].persona_id == "profile:reviewer"
    assert by_id[first.id].mode == "free_floating"
    assert by_id[first.id].state == WorkerSessionState.IDLE


def test_create_operator_chat_from_same_template_gets_distinct_sessions(isolate_agent_runtime_root):
    store = PersonaInstanceStore()

    first = store.create_operator_chat(
        persona_id="profile:reviewer",
        display_name="Reviewer One",
    )
    second = store.create_operator_chat(
        persona_id="profile:reviewer",
        display_name="Reviewer Two",
    )

    assert first.id == second.id == persona_instance_id_for("profile:reviewer")
    assert first.session_id.startswith(f"persona_chat_{first.id}_")
    assert second.session_id.startswith(f"persona_chat_{second.id}_")
    assert first.session_id != second.session_id
    assert first.mode == "chat"
    assert second.mode == "chat"
    assert first.persona_id == "profile:reviewer"
    assert first.profile_id == "reviewer"
    assert second.profile_id == "reviewer"
    assert first.display_name == "Reviewer One"
    assert second.display_name == "Reviewer Two"


def test_operator_chat_history_binds_by_unique_session(isolate_agent_runtime_root):
    from hermes_state import SessionDB

    store = PersonaInstanceStore()
    first = store.create_operator_chat(
        persona_id="profile:reviewer",
        display_name="Reviewer One",
    )
    second = store.create_operator_chat(
        persona_id="profile:reviewer",
        display_name="Reviewer Two",
    )
    db = SessionDB()
    for session_id, text in (
        (first.session_id, "first only"),
        (second.session_id, "second only"),
    ):
        db.create_session(
            session_id=session_id,
            source="agent_runtime_persona_chat",
            model=None,
            system_prompt="test persona chat",
        )
        db.append_message(session_id=session_id, role="assistant", content=text)

    rows = persona_chat_history_summary(
        persona_instances=store.list_all(),
        session_db=db,
    )

    messages_by_session = {
        row["session_id"]: [message["text"] for message in row["messages"]]
        for row in rows
        if row["messages"]
    }
    assert messages_by_session[first.session_id] == ["first only"]
    assert messages_by_session[second.session_id] == ["second only"]
    assert {row["persona_instance_id"] for row in rows if row["messages"]} == {first.id}


def test_additional_placement_does_not_reuse_another_instance_session(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    profile = store.create_operator_chat(
        persona_id="profile:alice",
        display_name="Alice Agent",
    )

    placed = store.add_instance(
        persona_id="profile:alice",
        placement_id="operator_2c1f1de674e74942",
        display_name="Alice Agent",
        session_id=profile.session_id,
    )

    assert placed.id == persona_instance_id_for_placement("operator_2c1f1de674e74942")
    assert placed.session_id != profile.session_id
    assert placed.session_id.startswith(f"persona_chat_{placed.id}_")


def test_assignment_store_create_or_resume_uses_signal_hash(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()
    spec = PersonaAssignmentSpec(
        persona_id="dev",
        kind="task_stage",
        title="Stage",
        message="Patch one file",
        task_id="task_1",
        stage_id="stage_1",
        affected_paths=["a.py"],
        proof_targets=["pytest tests/a.py"],
    )

    first = store.create_or_resume(spec)
    second = store.create_or_resume(spec)
    changed = store.create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="task_stage",
            title="Stage",
            message="Patch one file differently",
            task_id="task_1",
            stage_id="stage_1",
            affected_paths=["a.py"],
            proof_targets=["pytest tests/a.py"],
        )
    )

    assert second.id == first.id
    assert changed.id != first.id
    assert [item.id for item in store.find_active(persona_id="dev", task_id="task_1", stage_id="stage_1")] == [first.id, changed.id]


def test_free_floating_assignment_is_taskless_non_production_evidence(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()

    assignment = store.create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="free_floating_message",
            title="Launcher Dev sandbox",
            message="Test this persona without a product task.",
            created_by="launcher",
            task_id=None,
        )
    )

    summary = store.get(assignment.id)
    assert summary.task_id is None
    assert summary.kind == "free_floating_message"
    assert summary.evidence_kind == "free_floating"
    assert summary.production_proof_eligible is False
    assert summary.archive_scope == "assignment"


def test_free_floating_assignment_reuses_client_message_id(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()
    spec = PersonaAssignmentSpec(
        persona_id="dev",
        persona_instance_id="personainst_dev",
        kind="free_floating_message",
        title="Chat",
        message="hi",
        evidence_kind="free_floating",
        production_proof_eligible=False,
        archive_scope="assignment",
        client_message_id="client_msg_1",
    )

    first = store.create_or_resume(spec)
    store.complete(first.id)
    second = store.create_or_resume(spec)

    assert second.id == first.id
    assert second.client_message_id == "client_msg_1"
    assert len(store.list_all()) == 1


def test_diagnostic_assignment_summary_marks_not_production_proof(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()

    assignment = store.create_or_resume(
        PersonaAssignmentSpec(
            persona_id="qa",
            kind="diagnostic",
            title="QA diagnostic",
            message="Run a bounded QA diagnostic.",
            created_by="launcher",
            task_id="task_diag",
        )
    )

    summary = PersonaAssignmentStore().get(assignment.id)
    rendered = __import__("agent_runtime.persona_assignments", fromlist=["persona_assignment_summary"]).persona_assignment_summary(summary)
    assert rendered["evidence_kind"] == "diagnostic"
    assert rendered["production_proof_eligible"] is False
    assert rendered["archive_scope"] == "task"


def test_assignment_complete_is_idempotent_for_same_terminal_state(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()
    assignment = store.create_or_resume(
        PersonaAssignmentSpec(
            persona_id="neko_supervisor",
            kind="diagnostic",
            title="Neko diagnostic",
            message="Run one scoped diagnostic.",
            task_id="task_close_once",
        )
    )

    first = store.complete(assignment.id, state="completed")
    second = store.complete(assignment.id, state="completed")

    events = [event for event in EventLog().for_task("task_close_once", limit=20) if event.type == "persona_assignment.closed"]
    assert first.completed_at == second.completed_at
    assert [event.payload["assignment_id"] for event in events] == [assignment.id]


def test_attach_run_does_not_reopen_terminal_assignment(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()
    assignment = store.create_or_resume(
        PersonaAssignmentSpec(
            persona_id="neko_supervisor",
            kind="diagnostic",
            title="Neko diagnostic",
            message="Run one scoped diagnostic.",
            task_id="task_attach_after_close",
        )
    )
    closed = store.complete(assignment.id, state="completed")

    attached = store.attach_run(assignment.id, "run_after_close")
    second = store.complete(assignment.id, state="completed")

    events = [event for event in EventLog().for_task("task_attach_after_close", limit=20) if event.type == "persona_assignment.closed"]
    assert attached.state == "completed"
    assert attached.run_ids == ["run_after_close"]
    assert second.completed_at == closed.completed_at
    assert [event.payload["assignment_id"] for event in events] == [assignment.id]


def test_status_and_snapshot_expose_persona_instances_when_enabled(monkeypatch, isolate_agent_runtime_root):
    cfg = _assignment_config()
    monkeypatch.setattr("agent_runtime.status.load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    workers = WorkerSessionStore()
    workers.open(task_id="task_1", persona=_persona("dev"), stage_id="stage_1", assignment_id="assign_1")

    status = build_status(worker_session_store=workers)
    snapshot = build_snapshot(worker_session_store=workers)

    assert status["persona_instance_runtime"]["enabled"] is True
    assert {item["persona_id"] for item in status["persona_instances"]} >= {"dev", "qa", "neko_supervisor", "backend_dev"}
    status_lane_agents = {item["persona_id"]: item for item in status["agents"]}
    assert "personainst_dev" in status_lane_agents
    assert status_lane_agents["personainst_dev"]["source_persona_id"] == "dev"
    assert isinstance(status_lane_agents["personainst_dev"]["agent_hud_state"], dict)
    assert isinstance(status_lane_agents["personainst_dev"]["tool_resolution"], dict)
    assert snapshot["persona_instance_runtime"]["enabled"] is True
    assert {item["persona_id"] for item in snapshot["persona_instances"]} >= {"dev", "qa", "neko_supervisor", "backend_dev"}
    snapshot_lane_agents = {item["persona_id"]: item for item in snapshot["agents"]}
    assert "personainst_dev" in snapshot_lane_agents
    assert snapshot_lane_agents["personainst_dev"]["source_persona_id"] == "dev"
    assert isinstance(snapshot_lane_agents["personainst_dev"]["agent_hud_state"], dict)
    assert isinstance(snapshot_lane_agents["personainst_dev"]["tool_resolution"], dict)


def test_snapshot_exposes_operator_created_idle_persona_instance(monkeypatch, isolate_agent_runtime_root):
    cfg = _assignment_config()
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    created = PersonaInstanceStore().create_free_floating("profile:reviewer")

    snapshot = build_snapshot()
    by_id = {item["persona_instance_id"]: item for item in snapshot["persona_instances"]}

    assert created.id in by_id
    assert by_id[created.id]["agent_profile_id"] == created.id
    assert by_id[created.id]["persona_id"] == "profile:reviewer"
    assert by_id[created.id]["source_persona_id"] == "profile:reviewer"
    assert by_id[created.id]["source_profile_id"] == "reviewer"
    assert created.id == "personainst_profile_reviewer"
    assert by_id[created.id]["state"] == "idle"
    assert by_id[created.id]["lifecycle_mode"] == "free_floating"
    assert by_id[created.id]["mode"] == "free_floating"
    assert by_id[created.id]["active_worker_session_id"] is None


def test_persona_instance_create_cli_creates_free_floating_assignment_without_ticking(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_create(
        Namespace(
            persona_id="launcher-dev",
            title="Launcher Dev sandbox",
            message="Check the persona without a product task.",
            requested_by="test",
            client_message_id=None,
            display_name=None,
            session_id=None,
            kill_active=False,
            add_instance=False,
            placement_id=None,
            auto_run=False,
            max_actions=1,
            max_seconds=240.0,
            stream=False,
            json=True,
        )
    )

    assert code == 0
    assignments = PersonaAssignmentStore().list_for_persona("dev")
    assert len(assignments) == 1
    assert assignments[0].task_id is None
    assert assignments[0].kind == "free_floating_message"
    assert assignments[0].production_proof_eligible is False
    assert assignments[0].persona_instance_id == "personainst_dev"
    instance = PersonaInstanceStore().get(assignments[0].persona_instance_id)
    assert instance.mode == "free_floating"
    assert instance.current_assignment_id == assignments[0].id
    assert RunStore().list_all() == []


def test_coordinator_create_beyond_spawn_scope_returns_confirm_without_creating(monkeypatch, capsys, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_create(
        Namespace(
            persona_id="launcher-dev",
            title="Spawn Dev",
            message="Start a child.",
            requested_by="coordinator:neko_supervisor",
            coordinator_id="neko_supervisor",
            coordinator_max_spawns=0,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=None,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=None,
            client_message_id=None,
            display_name="Spawned Dev",
            session_id=None,
            kill_active=False,
            add_instance=True,
            placement_id="scene_child_1",
            auto_run=False,
            max_actions=1,
            max_seconds=240.0,
            stream=False,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "needs_operator_confirm"
    assert payload["reason"] == "spawn_scope_exhausted"
    assert PersonaInstanceStore().list_all() == []


def test_persona_instance_steer_cli_attaches_parent_and_goal(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    store = PersonaInstanceStore()
    store.ensure_for_persona(_persona("neko_supervisor"))
    store.ensure_for_persona(_persona("dev"))

    code = harness._cmd_persona_instance_steer(
        Namespace(
            persona_instance_id="personainst_dev",
            parent_instance_id="personainst_neko_supervisor",
            goal_id="task_77",
            detach=False,
            requested_by="operator",
            coordinator_id="neko_supervisor",
            coordinator_max_spawns=None,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=None,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=None,
            json=True,
        )
    )

    assert code == 0
    instance = PersonaInstanceStore().get("personainst_dev")
    assert instance.spawned_by == "personainst_neko_supervisor"
    assert instance.goal_id == "task_77"
    assert instance.current_task_id == "task_77"
    assert instance.mode == "task_bound"


def test_persona_instance_steer_cli_detaches_to_standalone(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    store = PersonaInstanceStore()
    store.ensure_for_persona(_persona("neko_supervisor"))
    store.ensure_for_persona(_persona("dev"))
    store.steer("personainst_dev", parent_instance_id="personainst_neko_supervisor", goal_id="task_77")

    code = harness._cmd_persona_instance_steer(
        Namespace(
            persona_instance_id="personainst_dev",
            parent_instance_id=None,
            goal_id=None,
            detach=True,
            requested_by="operator",
            coordinator_id="neko_supervisor",
            coordinator_max_spawns=None,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=None,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=None,
            json=True,
        )
    )

    assert code == 0
    instance = PersonaInstanceStore().get("personainst_dev")
    assert instance.spawned_by is None
    assert instance.goal_id is None
    assert instance.current_task_id is None
    assert instance.mode == "configured"


def test_persona_instance_steer_cli_rejects_self_steer(monkeypatch, capsys, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    PersonaInstanceStore().ensure_for_persona(_persona("dev"))

    code = harness._cmd_persona_instance_steer(
        Namespace(
            persona_instance_id="personainst_dev",
            parent_instance_id="personainst_dev",
            goal_id="task_77",
            detach=False,
            requested_by="operator",
            coordinator_id="neko_supervisor",
            coordinator_max_spawns=None,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=None,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=None,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "cannot steer itself" in payload["error"]


def test_persona_instance_steer_cli_rejects_missing_parent(monkeypatch, capsys, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    PersonaInstanceStore().ensure_for_persona(_persona("dev"))

    code = harness._cmd_persona_instance_steer(
        Namespace(
            persona_instance_id="personainst_dev",
            parent_instance_id="personainst_missing",
            goal_id="task_77",
            detach=False,
            requested_by="operator",
            coordinator_id="neko_supervisor",
            coordinator_max_spawns=None,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=None,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=None,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "parent persona instance not found" in payload["error"]


def test_persona_instance_steer_store_reroutes_without_minting_instances(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    store.ensure_for_persona(_persona("neko_supervisor"))
    store.ensure_for_persona(_persona("pm"))
    store.ensure_for_persona(_persona("dev"))
    before = {item.id for item in store.list_all()}

    store.steer("personainst_dev", parent_instance_id="personainst_neko_supervisor", goal_id="task_77")
    rewired = store.steer("personainst_dev", parent_instance_id="personainst_pm", goal_id="task_77")

    assert rewired.spawned_by == "personainst_pm"
    # Re-routing must mutate the existing instance, never spawn a new one.
    assert {item.id for item in store.list_all()} == before


def test_persona_instance_steer_store_rejects_cycles(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    store.ensure_for_persona(_persona("neko_supervisor"))
    store.ensure_for_persona(_persona("dev"))
    store.ensure_for_persona(_persona("qa"))
    store.steer("personainst_dev", parent_instance_id="personainst_neko_supervisor", goal_id="task_77")
    store.steer("personainst_qa", parent_instance_id="personainst_dev", goal_id="task_77")

    with pytest.raises(ValueError, match="cycle"):
        store.steer("personainst_dev", parent_instance_id="personainst_qa", goal_id="task_77")

    assert store.get("personainst_dev").spawned_by == "personainst_neko_supervisor"


def test_persona_instance_open_chat_binds_old_chat_without_ticking(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="launcher-dev",
            session_id="chat_old_123",
            kill_active=False,
            json=True,
        )
    )

    assert code == 0
    instance = PersonaInstanceStore().get("personainst_dev")
    assert instance.mode == "chat"
    assert instance.session_id == "chat_old_123"
    assert instance.current_task_id is None
    assert instance.active_run_id is None
    assert PersonaAssignmentStore().list_all() == []
    assert RunStore().list_all() == []


def test_persona_instance_open_chat_can_target_additional_placement(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="profile:reviewer",
            session_id="chat_old_123",
            kill_active=False,
            add_instance=True,
            placement_id="reviewer_agent_2",
            json=True,
        )
    )

    assert code == 0
    primary_missing = True
    try:
        PersonaInstanceStore().get("personainst_profile_reviewer")
        primary_missing = False
    except Exception:
        pass
    additional = PersonaInstanceStore().get("personainst_reviewer_agent_2")
    assert primary_missing is True
    assert additional.persona_id == "profile:reviewer"
    assert additional.session_id == "chat_old_123"
    assert additional.mode == "chat"


def test_open_chat_refuses_live_run_without_orphaning_fields(isolate_agent_runtime_root):
    instance_store = PersonaInstanceStore()
    workers = WorkerSessionStore()
    runs = RunStore()
    worker = workers.open(task_id="task_live", persona=_persona("dev"), stage_id="stage_1", assignment_id="assign_live")
    run = runs.open_run("dev", "task_live", "stage_1", session_id="persona_chat_personainst_dev_live")
    worker = workers.assign_run(worker.id, run)
    instance_store.update_from_worker(worker)

    with pytest.raises(ChatBusyError) as exc:
        instance_store.open_chat(persona_id="dev", session_id="persona_chat_personainst_dev_new")

    assert exc.value.active_run_id == run.id
    assert exc.value.active_worker_session_id == worker.id
    instance = instance_store.get("personainst_dev")
    assert instance.active_run_id == run.id
    assert instance.active_worker_session_id == worker.id
    assert instance.current_task_id == "task_live"
    assert runs.get(run.id).state == RunState.RUNNING
    assert workers.get(worker.id).state == WorkerSessionState.RUNNING


def test_open_chat_with_kill_active_terminates_run_and_worker_before_swap(isolate_agent_runtime_root):
    instance_store = PersonaInstanceStore()
    workers = WorkerSessionStore()
    runs = RunStore()
    worker = workers.open(task_id="task_live", persona=_persona("dev"), stage_id="stage_1", assignment_id="assign_live")
    run = runs.open_run("dev", "task_live", "stage_1", session_id="persona_chat_personainst_dev_live")
    worker = workers.assign_run(worker.id, run)
    instance_store.update_from_worker(worker)

    updated = instance_store.open_chat(
        persona_id="dev",
        session_id="persona_chat_personainst_dev_replacement",
        kill_active=True,
    )

    assert runs.get(run.id).state == RunState.CANCELLED
    assert workers.get(worker.id).state == WorkerSessionState.CLOSED
    assert updated.id == "personainst_dev"
    assert updated.session_id == "persona_chat_personainst_dev_replacement"
    assert updated.current_task_id is None
    assert updated.active_run_id is None
    assert updated.active_worker_session_id is None


def test_add_instance_mints_distinct_placement_backed_instance(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    primary = store.create_operator_chat(persona_id="profile:reviewer", display_name="Reviewer")
    additional = store.add_instance(
        persona_id="profile:reviewer",
        placement_id="reviewer_agent_2",
        display_name="Reviewer 2",
    )

    assert primary.id == "personainst_profile_reviewer"
    assert additional.id == "personainst_reviewer_agent_2"
    assert additional.persona_id == "profile:reviewer"
    assert additional.profile_id == "reviewer"
    assert additional.session_id.startswith("persona_chat_personainst_reviewer_agent_2_")
    assert store.get(primary.id).session_id == primary.session_id


class _FakeSessionDB:
    def __init__(self, sessions, messages=None):
        self.sessions = sessions
        self.messages = messages or {}

    def list_sessions_rich(self, **kwargs):
        return list(self.sessions)

    def get_session(self, session_id):
        for session in self.sessions:
            if session.get("id") == session_id:
                return dict(session)
        return None

    def get_messages(self, session_id):
        return list(self.messages.get(session_id, []))


def test_persona_chat_history_summary_projects_bound_sessions_redaction_safe(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="chat_old_123")

    rows = persona_chat_history_summary(
        persona_instances=[instance],
        session_db=_FakeSessionDB(
            [
                {
                    "id": "chat_old_123",
                    "title": "API_KEY=super-secret",
                    "preview": "Please inspect the proof packet.",
                    "message_count": 7,
                    "input_tokens": 1234,
                    "output_tokens": 56,
                    "started_at": 10,
                    "last_active": 20,
                    "archived": 0,
                },
                {"id": "unbound", "title": "Should not leak"},
            ]
        ),
    )

    assert rows == [
        {
            "session_id": "chat_old_123",
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev",
            "title": "Untitled persona chat",
            "last_message_preview": "Please inspect the proof packet.",
            "message_count": 7,
            "created_at": 10,
            "updated_at": 20,
            "state": "open",
            "redaction_status": "redacted",
            "input_tokens": 1234,
            "output_tokens": 56,
            "total_tokens": 1290,
            "messages": [],
        }
    ]


def test_persona_chat_history_accounting_ignores_unrelated_session_sources(isolate_agent_runtime_root):
    from agent_runtime.parity import ProjectionAccountant

    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="chat_bound_1")

    accountant = ProjectionAccountant("persona_chat_history")
    rows = persona_chat_history_summary(
        persona_instances=[instance],
        session_db=_FakeSessionDB(
            [
                {"id": "chat_bound_1", "title": "bound", "message_count": 1},
                # Unrelated SessionDB sources (cron/telegram/cli) can never
                # render as persona chat rows: out of scope, NOT drops.
                {"id": "cron_20260618", "source": "cron", "title": "cron run"},
                {"id": "tg_20260620", "source": "telegram", "title": "tg chat"},
                # A genuine persona-chat orphan stays a visible drop.
                {
                    "id": "chat_orphan_1",
                    "source": "agent_runtime_persona_chat",
                    "title": "orphan",
                },
            ]
        ),
        accountant=accountant,
    )

    assert [row["session_id"] for row in rows] == ["chat_bound_1"]
    summary = accountant.summary()
    assert summary["considered"] == 2
    assert summary["included"] == 1
    assert summary["dropped"] == 1
    assert summary["reasons"] == {"no_instance_match": 1}


def test_persona_chat_history_summary_empty_bound_chat_is_safe_placeholder(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="chat_empty_123")

    rows = persona_chat_history_summary(
        persona_instances=[instance],
        session_db=_FakeSessionDB(
            [
                {
                    "id": "chat_empty_123",
                    "source": "agent_runtime_persona_chat",
                    "system_prompt": "Mission Control persona chat for dev",
                    "title": None,
                    "preview": None,
                    "message_count": 0,
                    "started_at": 100,
                    "last_active": 100,
                    "archived": 0,
                }
            ]
        ),
    )

    assert rows[0]["session_id"] == "chat_empty_123"
    assert rows[0]["last_message_preview"] == "No messages yet."
    assert rows[0]["message_count"] == 0
    assert rows[0]["redaction_status"] == "safe"


def test_persona_chat_history_hides_bound_session_deleted_from_session_db(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="deleted_chat_123")

    rows = persona_chat_history_summary(
        persona_instances=[instance],
        session_db=_FakeSessionDB([]),
    )

    assert rows == []


def test_persona_instance_update_profile_persists_runtime_overrides_only(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    instance = store.create_operator_chat(
        persona_id="profile:alice",
        display_name="Alice Agent",
        session_id="chat_alice",
    )

    updated = store.update_profile(
        instance.id,
        display_name="Alice Mission Lead",
        current_chat_goal="Keep the operator channel warm.",
        goal_id="task_123",
        skills=["agent-runtime-harness", "technical-writing", "technical-writing"],
    )

    assert updated.id == instance.id
    assert updated.persona_id == "profile:alice"
    assert updated.profile_id == "alice"
    assert updated.display_name == "Alice Mission Lead"
    assert updated.current_chat_goal == "Keep the operator channel warm."
    assert updated.goal_id == "task_123"
    assert updated.current_task_id == "task_123"
    assert updated.skill_overrides == ["agent-runtime-harness", "technical-writing"]

    summary = persona_instance_summary(updated)
    assert summary["display_name"] == "Alice Mission Lead"
    assert summary["backing_profile"] == "alice"
    assert summary["current_chat_goal"] == "Keep the operator channel warm."
    assert summary["skills"] == ["agent-runtime-harness", "technical-writing"]
    assert summary["skill_overrides"] == ["agent-runtime-harness", "technical-writing"]


def test_persona_chat_history_summary_ignores_task_bound_worker_sessions(isolate_agent_runtime_root):
    from agent_runtime.models import PersonaInstance
    from agent_runtime.states import WorkerSessionState

    task_bound = PersonaInstance(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="Launcher Dev Agent",
        profile_id=None,
        runtime_root="runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        session_id="worker_session_123",
    )

    rows = persona_chat_history_summary(
        persona_instances=[task_bound],
        session_db=_FakeSessionDB(
            [
                {
                    "id": "worker_session_123",
                    "title": None,
                    "preview": "# Agent Runtime Tick Context ## Task",
                    "message_count": 2,
                    "started_at": 10,
                    "last_active": 20,
                }
            ]
        ),
    )

    assert rows == []


def test_persona_chat_history_summary_projects_builtin_chat_source_when_worker_overwrites_binding(
    isolate_agent_runtime_root,
):
    from agent_runtime.models import PersonaInstance
    from agent_runtime.states import WorkerSessionState

    task_bound = PersonaInstance(
        id="personainst_qa",
        persona_id="qa",
        role="qa",
        display_name="QA Agent",
        profile_id=None,
        runtime_root="runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        session_id="worker_session_qa",
    )

    rows = persona_chat_history_summary(
        persona_instances=[task_bound],
        session_db=_FakeSessionDB(
            [
                {
                    "id": "chat_qa_123",
                    "source": "agent_runtime_persona_chat",
                    "system_prompt": "Mission Control persona chat for qa",
                    "title": None,
                    "preview": "hi, what's your take on shipping fast?",
                    "message_count": 2,
                    "started_at": 100,
                    "last_active": 200,
                    "archived": 0,
                }
            ],
            messages={
                "chat_qa_123": [
                    {
                        "id": "msg_1",
                        "role": "user",
                        "content": "hi, what's your take on shipping fast?",
                    }
                ]
            },
        ),
    )

    assert rows[0]["session_id"] == "chat_qa_123"
    assert rows[0]["persona_id"] == "qa"
    assert rows[0]["persona_instance_id"] == "personainst_qa"
    assert rows[0]["title"] == "hi, what's your take on shipping fast?"
    assert rows[0]["messages"][0]["text"] == "hi, what's your take on shipping fast?"


def test_snapshot_preserves_open_chat_and_emits_history(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.persona_chat_history as history
    import agent_runtime.snapshot as snapshot_module

    cfg = _assignment_config()
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    db = _FakeSessionDB(
        [
            {
                "id": "chat_old_123",
                "title": "Launcher Dev operator channel",
                "preview": "Continue the old chat safely.",
                "message_count": 3,
                "started_at": 100,
                "last_active": 200,
            }
        ],
        messages={
            "chat_old_123": [
                {
                    "id": "msg_1",
                    "role": "user",
                    "content": "Can you keep working from the old chat?",
                    "created_at": "2026-06-19T10:00:00Z",
                },
                {
                    "id": "msg_2",
                    "role": "assistant",
                    "content": "Yes. I will continue from this persona session.",
                    "created_at": "2026-06-19T10:01:00Z",
                },
            ]
        },
    )
    monkeypatch.setattr(history, "_default_session_db", lambda: db)
    monkeypatch.setattr(snapshot_module, "_default_persona_session_db", lambda: db)
    PersonaInstanceStore().open_chat(persona_id="dev", session_id="chat_old_123")

    snapshot = build_snapshot()
    by_instance = {item["persona_instance_id"]: item for item in snapshot["persona_instances"]}

    assert by_instance["personainst_dev"]["mode"] == "chat"
    assert by_instance["personainst_dev"]["session_id"] == "chat_old_123"
    assert snapshot["persona_chat_history"] == [
        {
            "session_id": "chat_old_123",
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev",
            "title": "Launcher Dev operator channel",
            "last_message_preview": "Continue the old chat safely.",
            "message_count": 3,
            "created_at": 100,
            "updated_at": 200,
            "state": "open",
            "redaction_status": "safe",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "messages": [
                {
                    "id": "msg_1",
                    "role": "operator",
                    "text": "Can you keep working from the old chat?",
                    "timestamp": "2026-06-19T10:00:00Z",
                    "redaction_status": "safe",
                },
                {
                    "id": "msg_2",
                    "role": "agent",
                    "text": "Yes. I will continue from this persona session.",
                    "timestamp": "2026-06-19T10:01:00Z",
                    "redaction_status": "safe",
                },
            ],
        }
    ]
    assert PersonaAssignmentStore().list_all() == []
    assert RunStore().list_all() == []


class _TranscriptDB:
    def __init__(self):
        self.sessions = {}
        self.messages = {}
        self.titles = {}

    def create_session(self, session_id, source, **kwargs):
        self.sessions.setdefault(session_id, {"source": source, **kwargs})
        self.messages.setdefault(session_id, [])
        return session_id

    def list_sessions_rich(self, **kwargs):
        source = kwargs.get("source")
        exclude_sources = set(kwargs.get("exclude_sources") or [])
        rows = []
        for session_id, session in self.sessions.items():
            row_source = session.get("source")
            if source and row_source != source:
                continue
            if row_source in exclude_sources:
                continue
            rows.append(
                {
                    "id": session_id,
                    "source": row_source,
                    "system_prompt": session.get("system_prompt"),
                    "model": session.get("model"),
                    "model_config": session.get("model_config"),
                    "title": self.titles.get(session_id),
                    "preview": None,
                    "message_count": len(self.messages.get(session_id, [])),
                    "started_at": None,
                    "last_active": None,
                    "archived": 0,
                }
            )
        return rows

    def append_message(self, session_id, role, content=None, **kwargs):
        self.messages.setdefault(session_id, []).append(
            {"role": role, "content": content, **kwargs}
        )
        return len(self.messages[session_id])

    def get_messages(self, session_id, include_inactive=False):
        return list(self.messages.get(session_id, []))

    def get_session(self, session_id):
        session = self.sessions.get(session_id)
        if session is None:
            return None
        return {
            "id": session_id,
            "source": session.get("source"),
            "system_prompt": session.get("system_prompt"),
            "model": session.get("model"),
            "model_config": session.get("model_config"),
            "title": self.titles.get(session_id),
            "preview": None,
            "message_count": len(self.messages.get(session_id, [])),
            "started_at": None,
            "last_active": None,
            "archived": 0,
        }

    def get_session_title(self, session_id):
        return self.titles.get(session_id)

    def set_session_title(self, session_id, title):
        self.titles[session_id] = title

    def update_session_meta(self, session_id, model_config_json, model=None):
        session = self.sessions.setdefault(session_id, {})
        session["model_config"] = model_config_json
        if model is not None:
            session["model"] = model

    def delete_session(self, session_id, **kwargs):
        if session_id not in self.sessions:
            return False
        del self.sessions[session_id]
        self.messages.pop(session_id, None)
        self.titles.pop(session_id, None)
        return True


def test_persona_instance_create_persists_empty_operator_chat_history(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    def _args(display_name: str):
        return SimpleNamespace(
            persona_id="profile:reviewer",
            display_name=display_name,
            title=f"{display_name} chat",
            message="New operator chat opened. Wait for operator input.",
            requested_by="test",
            json=True,
            auto_run=False,
            max_actions=1,
            max_seconds=240,
            client_message_id=None,
            session_id=None,
            stream=False,
            kill_active=False,
            add_instance=False,
            placement_id=None,
        )

    assert harness._cmd_persona_instance_create(_args("Reviewer One")) == 0
    first_session_id = PersonaInstanceStore().get(
        persona_instance_id_for("profile:reviewer")
    ).session_id
    assert harness._cmd_persona_instance_create(_args("Reviewer Two")) == 0
    second_session_id = PersonaInstanceStore().get(
        persona_instance_id_for("profile:reviewer")
    ).session_id
    capsys.readouterr()

    assert first_session_id != second_session_id
    assert sorted(db.sessions) == sorted([first_session_id, second_session_id])

    rows = persona_chat_history_summary(
        persona_instances=PersonaInstanceStore().list_all(),
        session_db=db,
    )
    by_session = {row["session_id"]: row for row in rows}

    assert set(by_session) == {first_session_id, second_session_id}
    assert by_session[first_session_id]["persona_id"] == "profile:reviewer"
    assert by_session[first_session_id]["persona_instance_id"] == persona_instance_id_for(
        "profile:reviewer"
    )
    assert by_session[first_session_id]["last_message_preview"] == "No messages yet."
    assert by_session[first_session_id]["redaction_status"] == "safe"
    assert by_session[second_session_id]["last_message_preview"] == "No messages yet."
    assert by_session[second_session_id]["redaction_status"] == "safe"


def test_persona_chat_delete_removes_session_and_clears_binding(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    store = PersonaInstanceStore()
    instance = store.create_operator_chat(
        persona_id="profile:reviewer",
        display_name="Reviewer",
    )
    db.create_session(instance.session_id, "agent_runtime_persona_chat")
    db.append_message(instance.session_id, "user", "delete this")

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id=instance.session_id,
            persona_id=instance.persona_id,
            persona_instance_id=instance.id,
            requested_by="test",
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["deleted_session"] is True
    assert payload["cleared_bindings"] == [instance.id]
    assert instance.session_id not in db.sessions
    updated = PersonaInstanceStore().get(instance.id)
    assert updated.session_id is None
    assert updated.mode == "configured"
    assert persona_chat_history_summary(persona_instances=[updated], session_db=db) == []


def test_persona_chat_delete_clears_stale_binding_when_session_already_missing(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="deleted_chat_123")

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id="deleted_chat_123",
            persona_id="dev",
            persona_instance_id=instance.id,
            requested_by="test",
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["deleted_session"] is False
    assert payload["cleared_bindings"] == [instance.id]
    assert PersonaInstanceStore().get(instance.id).session_id is None


def test_persona_chat_delete_reports_missing_without_silent_success(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: _TranscriptDB())

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id="missing_chat_123",
            persona_id="dev",
            persona_instance_id="personainst_dev",
            requested_by="test",
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["status"] == "not_found"
    assert payload["error"] == "persona chat session not found: missing_chat_123"


def test_free_floating_chat_session_binding_reuses_resume_and_opens_fresh_chat(monkeypatch, isolate_agent_runtime_root):
    from hermes_cli import harness

    db = _TranscriptDB()
    instance_store = PersonaInstanceStore()
    first = harness._bind_free_floating_chat_session(
        instance_store=instance_store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        assignment_id="assign_1",
    )
    resumed = harness._bind_free_floating_chat_session(
        instance_store=instance_store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        assignment_id="assign_1",
    )
    second = harness._bind_free_floating_chat_session(
        instance_store=instance_store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        assignment_id="assign_2",
    )

    assert first.startswith("persona_chat_personainst_dev_")
    assert resumed == first
    assert second.startswith("persona_chat_personainst_dev_")
    assert second != first
    assert sorted(db.sessions) == sorted([first, second])
    instance = instance_store.get("personainst_dev")
    assert instance.session_id == second
    assert instance.current_assignment_id == "assign_2"
    assert instance.mode == "free_floating"


def test_persona_chat_transcript_records_operator_and_assistant_turn(isolate_agent_runtime_root):
    from hermes_cli import harness

    db = _TranscriptDB()
    session_id = "persona_chat_personainst_dev"
    db.create_session(session_id, "agent_runtime_persona_chat")
    harness._append_persona_operator_turn(
        session_db=db,
        session_id=session_id,
        message="hi",
    )
    harness._append_persona_assistant_text(
        session_db=db,
        session_id=session_id,
        text="Hey — what are we working on?\n\n- Scope\n- Proof",
    )

    assert [item["role"] for item in db.messages[session_id]] == [
        "user",
        "assistant",
    ]
    assert db.messages[session_id][0]["content"] == "hi"
    assert (
        db.messages[session_id][1]["content"]
        == "Hey — what are we working on?\n\n- Scope\n- Proof"
    )


def test_mission_chat_model_override_is_chat_scoped_and_does_not_mutate_persona(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    cfg.personas["dev"] = {
        "model": "gpt-default",
        "provider": "openai-codex",
        "api_mode": "codex_responses",
        "hermes_profile": "profile-dev",
    }
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", lambda **_kwargs: None)
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    # Base-profile foundation: only `base` is seeded into the store now, but this test
    # asserts the chat override does not mutate the *persisted* typed persona, so persist
    # `dev` explicitly (it stays resolvable via the dormant catalog either way).
    from agent_runtime.store import AgentStore as _AgentStore
    from agent_runtime.config import persona_records_from_config as _persona_records_from_config
    _AgentStore().save(next(p for p in _persona_records_from_config(cfg) if p.id == "dev"))
    captured: dict = {}

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            captured["runtime_kwargs"] = kwargs

        def mission_chat_reply(self, persona_arg, message, **kwargs):
            captured["persona_model"] = persona_arg.model
            captured["persona_provider"] = persona_arg.provider
            captured["reply_kwargs"] = kwargs
            return SimpleNamespace(
                final_response="override accepted",
                input_tokens=3,
                output_tokens=4,
                total_tokens=7,
                raw={
                    "model_input_observability": {
                        "kind": "redaction_safe_final_model_input",
                        "message_count": 1,
                        "messages": [],
                    }
                },
            )

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    code = harness._cmd_mission_chat_message(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            title="Operator message",
            message="use the override",
            provider="openrouter",
            model="anthropic/claude-sonnet-4",
            use_agent_default=False,
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_override_1",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["model_selection"]["default_provider"] == "openai-codex"
    assert payload["model_selection"]["default_model"] == "gpt-default"
    assert payload["model_selection"]["effective_provider"] == "openrouter"
    assert payload["model_selection"]["effective_model"] == "anthropic/claude-sonnet-4"
    assert payload["model_selection"]["model_is_default"] is False
    assert captured["reply_kwargs"]["provider_override"] == "openrouter"
    assert captured["reply_kwargs"]["model_override"] == "anthropic/claude-sonnet-4"
    assert captured["persona_provider"] == "openai-codex"
    assert captured["persona_model"] == "gpt-default"
    stored_persona = AgentStore().get("dev")
    assert stored_persona.provider == "openai-codex"
    assert stored_persona.model == "gpt-default"

    stored_config = json.loads(db.sessions["persona_chat_personainst_dev"]["model_config"])
    override = stored_config["mission_control_chat_model_override"]
    assert override["provider"] == "openrouter"
    assert override["model"] == "anthropic/claude-sonnet-4"
    assert override["scope"] == "mission_control_chat_session"

    instance = PersonaInstanceStore().get("personainst_dev")
    rows = persona_chat_history_summary(persona_instances=[instance], session_db=db)
    assert rows[0]["chat_provider"] == "openrouter"
    assert rows[0]["chat_model"] == "anthropic/claude-sonnet-4"
    assert rows[0]["effective_provider"] == "openrouter"
    assert rows[0]["effective_model"] == "anthropic/claude-sonnet-4"
    assert rows[0]["chat_model_is_default"] is False

    code = harness._cmd_mission_chat_message(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            title="Operator message",
            message="back to default",
            provider=None,
            model=None,
            use_agent_default=True,
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_default_1",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["model_selection"]["effective_provider"] == "openai-codex"
    assert payload["model_selection"]["effective_model"] == "gpt-default"
    assert payload["model_selection"]["model_is_default"] is True
    stored_config = json.loads(db.sessions["persona_chat_personainst_dev"]["model_config"])
    assert "mission_control_chat_model_override" not in stored_config


def test_mission_chat_model_override_rejects_bad_values_before_turn_is_written(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    code = harness._cmd_mission_chat_message(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            title="Operator message",
            message="bad model please",
            provider="openrouter",
            model="bad model with spaces",
            use_agent_default=False,
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_bad_model",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_kind"] == "invalid_chat_model_override"
    assert "Hermes profile defaults were not changed" in payload["next_expected"]
    assert db.messages["persona_chat_personainst_dev"] == []


def test_mission_chat_queues_skill_for_next_turn_once(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness
    from agent_runtime.queued_skills import pending_skills_for_next_turn

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", lambda **_kwargs: None)
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    import tools.skills_tool as skills_tool

    monkeypatch.setattr(
        skills_tool,
        "_find_all_skills",
        lambda: [{"name": "deep-audit", "identifier": "deep-audit"}],
    )

    code = harness._cmd_mission_chat_queue_skill(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            skill="deep-audit",
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["queued_skills"] == ["deep-audit"]
    assert pending_skills_for_next_turn(
        persona_id="dev",
        session_id="persona_chat_personainst_dev",
    ) == ["deep-audit"]

    import agent.skill_commands as skill_commands

    monkeypatch.setattr(
        skill_commands,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None: ("PRELOADED SKILL PROMPT", list(skills), []),
    )
    captured_prompts: list[str | None] = []

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona_arg, message, **kwargs):
            captured_prompts.append(kwargs.get("preloaded_skill_prompt"))
            return SimpleNamespace(
                final_response="skill queued",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw={
                    "model_input_observability": {
                        "kind": "redaction_safe_final_model_input",
                        "message_count": 1,
                        "messages": [],
                    }
                },
            )

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    def _message_args(client_id: str):
        return SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            title="Operator message",
            message=f"turn {client_id}",
            provider=None,
            model=None,
            use_agent_default=False,
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id=client_id,
            stream=False,
            max_seconds=5.0,
            json=True,
        )

    assert harness._cmd_mission_chat_message(_message_args("client_skill_1")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured_prompts == ["PRELOADED SKILL PROMPT"]
    assert payload["queued_skills_loaded"] == ["deep-audit"]
    assert payload["prompt_observability"]["used_skills"] == [
        {
            "name": "deep-audit",
            "kind": "skill",
            "status": "used",
            "hash_tracked": False,
            "source": "queued_next_turn_skill",
        }
    ]
    assert pending_skills_for_next_turn(
        persona_id="dev",
        session_id="persona_chat_personainst_dev",
    ) == []

    assert harness._cmd_mission_chat_message(_message_args("client_skill_2")) == 0
    json.loads(capsys.readouterr().out)
    assert captured_prompts == ["PRELOADED SKILL PROMPT", ""]


def test_queue_skill_rejects_missing_skill_without_pending_state(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness
    from agent_runtime.queued_skills import pending_skills_for_next_turn

    import tools.skills_tool as skills_tool

    monkeypatch.setattr(skills_tool, "_find_all_skills", lambda: [])

    code = harness._cmd_mission_chat_queue_skill(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            skill="missing-skill",
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert pending_skills_for_next_turn(
        persona_id="dev",
        session_id="persona_chat_personainst_dev",
    ) == []


def test_prompt_observability_reports_redaction_safe_available_skill_catalog(
    monkeypatch, isolate_agent_runtime_root
):
    from agent_runtime import prompt_observability

    import tools.skills_tool as skills_tool

    monkeypatch.setattr(
        skills_tool,
        "_find_all_skills",
        lambda: [
            {
                "name": "deep-audit",
                "description": "Inspect the runtime deeply.",
                "category": "harness",
                "identifier": "harness/deep-audit",
            }
        ],
    )
    context = prompt_observability.mission_chat_prompt_observability(
        persona=_persona("dev"),
        session_id="persona_chat_personainst_dev",
    )

    assert context["available_skills"] == [
        {
            "name": "deep-audit",
            "kind": "skill",
            "status": "available",
            "hash_tracked": False,
            "source": "installed_skill_catalog",
            "category": "harness",
            "description": "Inspect the runtime deeply.",
            "loadable": True,
        }
    ]
    assert "content" not in context["available_skills"][0]
    assert context["skills_catalog"] == context["available_skills"]


def test_free_floating_auto_run_chats_persists_reply_and_completes(monkeypatch, isolate_agent_runtime_root):
    """Chat-first wiring: auto-run uses chat_reply (no decision/task), persists the
    redacted operator + agent turns, wires SessionDB for recall, and completes
    the assignment with run_ids=[]/task_id=None."""
    from types import SimpleNamespace

    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    db = _TranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(
        "agent.title_generator.generate_title",
        lambda user_message, assistant_response, **kwargs: "Quick Persona Check",
    )

    captured: dict = {}

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            captured["runtime_kwargs"] = kwargs

        def chat_reply(self, persona, message, **kwargs):
            captured["chat_message"] = message
            return SimpleNamespace(final_response="Hey — doing great, what's up?")

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    code = harness._queue_free_floating_assignment(
        persona_id="launcher-dev",
        title="Launcher Dev chat",
        message="hey, how are you",
        requested_by="test",
        json_output=True,
        auto_run=True,
        max_seconds=5.0,
    )

    assert code == 0
    # The visible transcript is the only persisted operator-chat ledger; the
    # model run must not create a hidden scratch SessionDB row.
    assert captured["runtime_kwargs"].get("session_db") is db
    assert captured["runtime_kwargs"].get("persist_agent_session") is False
    # Chat-first path: no decision contract, the agent saw the raw operator text.
    assert "hey, how are you" in captured["chat_message"]

    assert len(db.messages) == 1
    session_id = next(iter(db.messages))
    roles = [item["role"] for item in db.messages.get(session_id, [])]
    assert roles == ["user", "assistant"]
    assert db.messages[session_id][0]["content"] == "hey, how are you"
    assert db.messages[session_id][1]["content"] == "Hey — doing great, what's up?"
    assert db.get_session_title(session_id) == "Quick Persona Check"

    assignments = PersonaAssignmentStore().list_for_persona("dev")
    assert len(assignments) == 1
    assert assignments[0].state == "completed"
    assert assignments[0].task_id is None
    assert RunStore().list_all() == []


def test_profile_backed_operator_chat_auto_run_resolves_profile_persona(monkeypatch, isolate_agent_runtime_root):
    from types import SimpleNamespace

    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    db = _TranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    captured: dict = {}

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            captured["runtime_kwargs"] = kwargs

        def chat_reply(self, persona, message, **kwargs):
            captured["persona"] = persona
            captured["chat_message"] = message
            return SimpleNamespace(final_response="Alice is online.")

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    instance = PersonaInstanceStore().create_operator_chat(
        persona_id="profile:alice",
        display_name="Alice Agent",
    )

    code = harness._cmd_persona_instance_message(
        SimpleNamespace(
            persona_instance_id=instance.id,
            title="Alice chat",
            message="hi alice",
            requested_by="test",
            json=True,
            auto_run=True,
            max_actions=1,
            max_seconds=5.0,
            client_message_id="client_alice_1",
            stream=False,
        )
    )

    assert code == 0
    assert captured["runtime_kwargs"].get("session_db") is db
    assert captured["runtime_kwargs"].get("persist_agent_session") is False
    assert captured["persona"].id == "profile:alice"
    assert captured["persona"].hermes_profile == "alice"
    assert captured["persona"].include_profile_memory is True
    assert "hi alice" in captured["chat_message"]

    assignments = PersonaAssignmentStore().list_for_persona("profile:alice")
    assert len(assignments) == 1
    assert assignments[0].state == "completed"
    assert assignments[0].persona_instance_id == instance.id

    updated = PersonaInstanceStore().get(instance.id)
    assert updated.display_name == "Alice Agent"
    assert updated.mode == "chat"
    assert updated.session_id == instance.session_id
    assert db.messages[updated.session_id][-1]["content"] == "Alice is online."


def test_free_floating_auto_run_streams_ndjson_and_final_payload(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from types import SimpleNamespace

    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    db = _TranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(
        "agent.title_generator.generate_title",
        lambda user_message, assistant_response, **kwargs: "Streaming Persona Chat",
    )

    captured: dict = {}

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            captured["runtime_kwargs"] = kwargs

        def chat_reply(self, persona, message, **kwargs):
            captured["stream_callback"] = kwargs.get("stream_callback")
            kwargs["stream_callback"]("He")
            kwargs["stream_callback"]("llo")
            return SimpleNamespace(final_response="Hello")

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    code = harness._queue_free_floating_assignment(
        persona_id="launcher-dev",
        title="Launcher Dev chat",
        message="hi",
        requested_by="test",
        json_output=True,
        auto_run=True,
        max_seconds=5.0,
        client_message_id="client_1",
        stream=True,
    )

    assert code == 0
    assert captured["stream_callback"] is not None
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert [line["type"] for line in lines] == [
        "turn.start",
        "chat.delta",
        "segment.start",
        "segment.delta",
        "chat.delta",
        "segment.delta",
        "segment.end",
        "turn.end",
        "chat.final",
    ]
    assert [line.get("text") for line in lines if line["type"] == "chat.delta"] == ["He", "llo"]
    assert [line.get("text") for line in lines if line["type"] == "segment.delta"] == ["He", "llo"]
    assert lines[0]["protocol_version"] == 2
    assert lines[2]["seq"] == 1
    assert lines[-1]["ok"] is True
    assert lines[-1]["protocol_version"] == 2
    assert lines[-1]["turn_elements"][0]["text"] == "Hello"
    assert lines[-1]["execution_state"] == "completed"
    assert lines[-1]["reply"] == "Hello"
    assert lines[-1]["run_ids"] == []
    assert lines[-1]["task_id"] is None
    assert lines[-1]["assignment_id"]
    assert lines[-1]["persona_instance_id"] == "personainst_dev"
    assert lines[-1]["client_message_id"] == "client_1"
    persisted = mission_chat_turn_elements(
        session_id=lines[-1]["session_id"],
        client_message_id="client_1",
    )
    assert [item["id"] for item in persisted] == [lines[-1]["turn_elements"][0]["id"]]
    assert persisted[0]["text"] == "Hello"


def test_mission_chat_message_replays_duplicate_client_message_id(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    calls = {"count": 0}
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(
        "agent.title_generator.generate_title",
        lambda user_message, assistant_response, **kwargs: "Mission Chat",
    )

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            calls["count"] += 1
            return SimpleNamespace(
                final_response="Recovered canonical reply.",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                api_calls=1,
                model="gpt-test",
                raw={},
            )

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    def _args():
        return SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            message="hi",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_dup_1",
            stream=False,
            max_seconds=5.0,
            json=True,
        )

    assert harness._cmd_mission_chat_message(_args()) == 0
    first = json.loads(capsys.readouterr().out)
    assert first.get("idempotent_replay") is not True

    assert harness._cmd_mission_chat_message(_args()) == 0
    replay = json.loads(capsys.readouterr().out)

    assert calls["count"] == 1
    assert [item["role"] for item in db.messages["persona_chat_personainst_dev"]] == [
        "user",
        "assistant",
    ]
    assert {
        item["platform_message_id"]
        for item in db.messages["persona_chat_personainst_dev"]
    } == {"client_dup_1"}
    assert replay["idempotent_replay"] is True
    assert replay["client_message_id"] == "client_dup_1"
    assert replay["reply"] == "Recovered canonical reply."


def test_mission_chat_message_generates_client_message_id_when_missing(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    captured = {}
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(
        "agent.title_generator.generate_title",
        lambda user_message, assistant_response, **kwargs: "Mission Chat",
    )

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            captured["turn_id"] = kwargs.get("turn_id")
            return SimpleNamespace(
                final_response="Generated id reply.",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                api_calls=1,
                model="gpt-test",
                raw={},
            )

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    code = harness._cmd_mission_chat_message(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            message="hi",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id=None,
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    client_message_id = payload["client_message_id"]
    assert client_message_id.startswith("agent-chat-send-")
    assert payload["turn_id"] == client_message_id
    assert captured["turn_id"] == client_message_id
    assert {
        item["platform_message_id"]
        for item in db.messages["persona_chat_personainst_dev"]
    } == {client_message_id}
    assert payload["prompt_observability"]["turn_id"] == client_message_id


def test_mission_chat_message_persists_pre_trace_ack_before_final_reply(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", lambda **_kwargs: None)

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            kwargs["pre_trace_callback"](
                {
                    "type": "run.tool.started",
                    "phase": "tool",
                    "step": "tool_started",
                    "status": "started",
                    "tool_name": "skill_view",
                }
            )
            return SimpleNamespace(
                final_response="The guidance is loaded; this is the right place.",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                api_calls=1,
                model="gpt-test",
                raw={},
            )

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    code = harness._cmd_mission_chat_message(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            message="is that a good place to do it",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_pre_trace_1",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    messages = db.messages["persona_chat_personainst_dev"]
    assert [item["role"] for item in messages] == [
        "user",
        "assistant",
        "assistant",
    ]
    assert messages[1]["content"] == (
        "I'll load the relevant guidance first, then report back with the useful part."
    )
    assert messages[2]["content"] == "The guidance is loaded; this is the right place."
    assert messages[1].get("platform_message_id") is None
    assert messages[2].get("platform_message_id") == "client_pre_trace_1"


def test_persona_chat_auto_title_waits_for_session_title_write(monkeypatch, isolate_agent_runtime_root):
    from hermes_cli import harness

    db = _TranscriptDB()
    session_id = "persona_chat_personainst_dev"
    db.create_session(session_id, "agent_runtime_persona_chat")
    called = []

    def fake_auto_title_session(session_db, sid, user_message, assistant_response, **kwargs):
        called.append((sid, user_message, assistant_response))
        session_db.set_session_title(sid, "Shipping Strategy Discussion")

    monkeypatch.setattr("agent.title_generator.auto_title_session", fake_auto_title_session)

    harness._maybe_auto_title_persona_chat(
        session_db=db,
        session_id=session_id,
        user_message="what's your take on shipping fast?",
        assistant_response="Ship the smallest coherent slice.",
    )

    assert called == [
        (
            session_id,
            "what's your take on shipping fast?",
            "Ship the smallest coherent slice.",
        )
    ]
    assert db.get_session_title(session_id) == "Shipping Strategy Discussion"


def test_persona_chat_context_includes_prior_turns(isolate_agent_runtime_root):
    from hermes_cli import harness

    db = _TranscriptDB()
    session_id = "persona_chat_personainst_dev"
    db.create_session(session_id, "agent_runtime_persona_chat")
    db.append_message(session_id, "user", "remember the blue button")
    db.append_message(session_id, "assistant", "I will remember the blue button.")

    enriched = harness._persona_chat_message_with_history(
        session_db=db,
        session_id=session_id,
        message="what did I mention?",
    )

    assert "Prior persona chat context" in enriched
    assert "Operator: remember the blue button" in enriched
    assert "Agent: I will remember the blue button." in enriched
    assert enriched.endswith("what did I mention?")


def test_profile_persona_resolution_does_not_borrow_role_skills(monkeypatch, isolate_agent_runtime_root):
    from hermes_cli import harness

    cfg = _assignment_config()
    cfg.default_provider = "openai-codex"
    cfg.default_model = "gpt-default"
    cfg.personas["neko_supervisor"] = {
        "display_name": "Neko Mission Lead",
        "hermes_profile": "alice",
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "api_mode": "codex_responses",
        "skills": ["harness-mission-lead", "systematic-debugging"],
        "toolsets": ["terminal", "code_execution", "browser", "mission_goal"],
    }

    persona = harness._persona_by_id(cfg, "profile:alice")

    assert persona is not None
    assert persona.id == "profile:alice"
    assert persona.role == "profile"
    assert persona.hermes_profile == "alice"
    assert persona.skills == []
    assert persona.model == "gpt-5.5"
    assert persona.provider == "openai-codex"
    assert "terminal" in persona.toolsets
    assert "code_execution" in persona.toolsets
    assert "mission_goal" in persona.toolsets


def test_profile_prompt_observability_uses_profile_skills_and_chat_title(
    monkeypatch,
    tmp_path,
    isolate_agent_runtime_root,
):
    from agent_runtime import prompt_observability

    profile_dir = tmp_path / "alice"
    profile_dir.mkdir()
    (profile_dir / ".skills_prompt_snapshot.json").write_text(
        json.dumps(
            {
                "skills": [
                    {"skill_name": "agent-runtime-harness"},
                    {"skill_name": "creative-ideation"},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_observability, "get_profile_dir", lambda profile: profile_dir)
    db = _TranscriptDB()
    db.create_session("persona_chat_alice", "agent_runtime_persona_chat")
    db.set_session_title("persona_chat_alice", "Alice Agent chat")

    context = prompt_observability.mission_chat_prompt_observability(
        persona=AgentPersona(
            id="profile:alice",
            display_name="Alice Agent",
            role="profile",
            model="gpt-test",
            provider="openai-codex",
            api_mode="codex_responses",
            toolsets=["file", "skills"],
            system_prompt_path="",
            hermes_profile="alice",
            skills=[],
        ),
        persona_instance_id="personainst_profile_alice",
        session_id="persona_chat_alice",
        session_db=db,
    )

    assert context["chat_id"] == "persona_chat_alice"
    assert context["chat_title"] == "Alice Agent chat"
    assert context["chat_name"] == "Alice Agent chat"
    assert context["chat"]["source"] == "agent_runtime_persona_chat"
    assert context["used_skills"] == []
    assert [item["name"] for item in context["accessible_skills"]] == [
        "agent-runtime-harness",
        "creative-ideation",
    ]
    assert context["skills"] == context["accessible_skills"]
    assert {item["source"] for item in context["accessible_skills"]} == {
        "profile_skills_snapshot"
    }
    assert {item["status"] for item in context["accessible_skills"]} == {
        "accessible"
    }


def test_prompt_observability_reports_used_skill_from_skill_view_trace(
    monkeypatch,
    tmp_path,
    isolate_agent_runtime_root,
):
    from agent_runtime import prompt_observability

    profile_dir = tmp_path / "alice"
    profile_dir.mkdir()
    (profile_dir / ".skills_prompt_snapshot.json").write_text(
        json.dumps({"skills": [{"skill_name": "agent-runtime-harness"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_observability, "get_profile_dir", lambda profile: profile_dir)

    context = prompt_observability.mission_chat_prompt_observability(
        persona=AgentPersona(
            id="profile:alice",
            display_name="Alice Agent",
            role="profile",
            model="gpt-test",
            provider="openai-codex",
            api_mode="codex_responses",
            toolsets=["file", "skills"],
            system_prompt_path="",
            hermes_profile="alice",
            skills=[],
        ),
        persona_instance_id="personainst_profile_alice",
        session_id="persona_chat_alice",
        trace_events=[
            {
                "tool_name": "skill_view",
                "step": "tool_finished",
                "status": "passed",
                "skill_name": "agent-runtime-harness",
            },
            {
                "tool_name": "skills_list",
                "step": "tool_finished",
                "status": "passed",
                "skill_name": "creative-ideation",
            },
        ],
    )

    assert [item["name"] for item in context["used_skills"]] == [
        "agent-runtime-harness"
    ]
    assert context["used_skills"][0]["source"] == "skill_view_trace"


def test_prompt_observability_refreshes_stale_derived_fields(
    monkeypatch,
    tmp_path,
    isolate_agent_runtime_root,
):
    from agent_runtime import prompt_observability

    profile_dir = tmp_path / "alice"
    profile_dir.mkdir()
    (profile_dir / ".skills_prompt_snapshot.json").write_text(
        json.dumps({"skills": [{"skill_name": "fresh-profile-skill"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_observability, "get_profile_dir", lambda profile: profile_dir)
    db = _TranscriptDB()
    db.create_session("persona_chat_alice", "agent_runtime_persona_chat")
    db.set_session_title("persona_chat_alice", "Alice Agent chat")
    stale = {
        "context_id": "ctx_stale",
        "persona_id": "profile:alice",
        "persona_instance_id": "personainst_profile_alice",
        "profile": "alice",
        "session_id": "persona_chat_alice",
        "skills": [
            {
                "name": "harness-mission-lead",
                "kind": "skill",
                "status": "loaded",
                "hash_tracked": True,
                "source": "persona_definition",
            }
        ],
        "used_skills": None,
        "model_selection": {
            "effective_provider": "openai-codex",
            "effective_model": "gpt-5.5",
        },
        "final_model_input": {
            "messages": [
                {"role": "system", "bytes": 40},
                {"role": "user", "bytes": 60},
            ]
        },
        "context_budget": {
            "model": "gpt-5.5",
            "provider": "openai-codex",
            "window_tokens": 272000,
            "compaction_ratio": 0.85,
            "compaction_tokens": 231200,
            "used_tokens": None,
            "used_estimated": True,
        },
    }
    prompt_observability.persist_prompt_observability_context(stale)
    built = prompt_observability.mission_chat_prompt_observability(
        persona=AgentPersona(
            id="profile:alice",
            display_name="Alice Agent",
            role="profile",
            model="gpt-test",
            provider="openai-codex",
            api_mode="codex_responses",
            toolsets=["file", "skills"],
            system_prompt_path="",
            hermes_profile="alice",
            skills=[],
        ),
        persona_instance_id="personainst_profile_alice",
        session_id="persona_chat_alice",
        session_db=db,
    )

    merged = prompt_observability._merge_latest_contexts([built])

    refreshed = merged[0]
    assert refreshed["context_id"] == "ctx_stale"
    assert refreshed["chat_title"] == "Alice Agent chat"
    assert refreshed["used_skills"] == []
    assert [item["name"] for item in refreshed["accessible_skills"]] == ["fresh-profile-skill"]
    assert refreshed["skills"] == refreshed["accessible_skills"]
    assert refreshed["accessible_skills"][0]["source"] == "profile_skills_snapshot"
    assert refreshed["context_budget"]["used_tokens"] == 25


def test_snapshot_prompt_observability_builds_profile_instance_context(
    monkeypatch,
    tmp_path,
    isolate_agent_runtime_root,
):
    from agent_runtime import prompt_observability
    from agent_runtime.models import PersonaInstance, WorkerSessionState

    profile_dir = tmp_path / "alice"
    profile_dir.mkdir()
    (profile_dir / ".skills_prompt_snapshot.json").write_text(
        json.dumps({"skills": [{"skill_name": "alice-profile-skill"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(prompt_observability, "get_profile_dir", lambda profile: profile_dir)
    db = _TranscriptDB()
    db.create_session("persona_chat_personainst_profile_alice", "agent_runtime_persona_chat")
    db.set_session_title("persona_chat_personainst_profile_alice", "Alice Agent chat")
    stale = {
        "context_id": "ctx_stale_profile_alice",
        "persona_id": "profile:alice",
        "persona_instance_id": "personainst_profile_alice",
        "profile": "alice",
        "session_id": "persona_chat_personainst_profile_alice",
        "skills": [
            {
                "name": "harness-mission-lead",
                "kind": "skill",
                "status": "loaded",
                "hash_tracked": True,
                "source": "persona_definition",
            }
        ],
    }
    prompt_observability.persist_prompt_observability_context(stale)

    snapshot = prompt_observability.snapshot_prompt_observability(
        personas=[],
        persona_instances=[
            PersonaInstance(
                id="personainst_profile_alice",
                persona_id="profile:alice",
                role="profile",
                display_name="Alice Agent",
                profile_id="alice",
                runtime_root=".",
                state=WorkerSessionState.IDLE,
                mode="chat",
                session_id="persona_chat_personainst_profile_alice",
            )
        ],
        session_db=db,
    )

    context = snapshot["chat_contexts"][0]
    assert context["context_id"] == "ctx_stale_profile_alice"
    assert context["chat_id"] == "persona_chat_personainst_profile_alice"
    assert context["chat_title"] == "Alice Agent chat"
    assert context["used_skills"] == []
    assert [item["name"] for item in context["accessible_skills"]] == ["alice-profile-skill"]
    assert context["skills"] == context["accessible_skills"]
    assert context["accessible_skills"][0]["source"] == "profile_skills_snapshot"


def test_persona_instance_close_cli_closes_only_free_floating_assignment(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    assignment = PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="free_floating_message",
            title="Sandbox",
            message="Close me.",
            task_id=None,
        )
    )
    instance_store = PersonaInstanceStore()
    instance = instance_store.ensure_for_persona(_persona("dev"))
    instance.mode = "free_floating"
    instance.current_assignment_id = assignment.id
    instance_store.update(instance)

    code = harness._cmd_persona_instance_close(
        Namespace(
            persona_instance_id=assignment.persona_instance_id,
            reason="operator closed sandbox",
            requested_by="test",
            json=True,
        )
    )

    assert code == 0
    closed = PersonaAssignmentStore().get(assignment.id)
    assert closed.state == "cancelled"
    assert closed.last_error == "operator closed sandbox"
    instance = PersonaInstanceStore().get(assignment.persona_instance_id)
    assert instance.mode == "configured"
    assert instance.current_assignment_id is None


def test_coordinator_close_own_spawned_instance_with_scope(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    assignment = PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="free_floating_message",
            title="Sandbox",
            message="Close me.",
            task_id=None,
        )
    )
    instance_store = PersonaInstanceStore()
    instance = instance_store.ensure_for_persona(_persona("dev"))
    instance.mode = "free_floating"
    instance.current_assignment_id = assignment.id
    instance.spawned_by = "neko_supervisor"
    instance_store.update(instance)

    code = harness._cmd_persona_instance_close(
        Namespace(
            persona_instance_id=assignment.persona_instance_id,
            reason="coordinator closed own child",
            requested_by="coordinator:neko_supervisor",
            coordinator_id="neko_supervisor",
            coordinator_max_spawns=0,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=True,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=None,
            json=True,
        )
    )

    assert code == 0
    assert PersonaAssignmentStore().get(assignment.id).state == "cancelled"


def test_coordinator_close_operator_placed_instance_needs_confirm(monkeypatch, capsys, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    assignment = PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id="dev",
            kind="free_floating_message",
            title="Sandbox",
            message="Do not close without operator.",
            task_id=None,
        )
    )
    instance_store = PersonaInstanceStore()
    instance = instance_store.ensure_for_persona(_persona("dev"))
    instance.mode = "free_floating"
    instance.current_assignment_id = assignment.id
    instance.spawned_by = "operator"
    instance_store.update(instance)

    code = harness._cmd_persona_instance_close(
        Namespace(
            persona_instance_id=assignment.persona_instance_id,
            reason="coordinator tried closing operator placement",
            requested_by="coordinator:neko_supervisor",
            coordinator_id="neko_supervisor",
            coordinator_max_spawns=0,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=True,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=True,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "needs_operator_confirm"
    assert payload["reason"] == "operator_placed_target"
    assert PersonaAssignmentStore().get(assignment.id).state == "queued"


def test_persona_message_cli_creates_assignment_without_ticking(monkeypatch, isolate_agent_runtime_root):
    # The subprocess reads config from disk, so force flags through environment-backed config is not available here.
    # Exercise the command handler behavior through module monkeypatching in-process instead.
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    task = _task("task_msg")
    TaskStore().create(task)

    code = harness._cmd_persona_message(
        Namespace(
            persona_id="launcher-dev",
            task_id=task.id,
            message="Check the current UI contract.",
            title="Operator steer",
            requested_by="test",
            json=True,
        )
    )

    assert code == 0
    assignments = PersonaAssignmentStore().list_for_task(task.id)
    assert len(assignments) == 1
    assert assignments[0].persona_id == "dev"
    assert RunStore().list_for_task(task.id) == []


def test_run_slot_spawns_attributed_persona_instance(isolate_agent_runtime_root):
    store = AgentStore()
    store.save(_persona("neko_supervisor"))
    store.save(_persona("backend_dev"))
    bp = BlueprintStore().get("neko_dev_qa_basic")
    plan = instantiate_blueprint(
        bp,
        goal="Build a graph-routed thing.",
        bindings={"lead": "persona:neko_supervisor", "builder": "persona:backend_dev", "verifier": "persona:qa"},
    )
    task = _task("task_run_slot_instance")
    plan.current_stage_id = "implement"
    task.mission_plan = plan
    task.current_stage_id = "implement"
    TaskStore().create(task)

    result = TickEngine(
        task_store=TaskStore(),
        proof_store=ProofStore(),
        persona_runtime=RequestProofRuntime(),
        proof_runner=PassingProofRunner(ProofStore()),
        config=_assignment_config(),
    )._execute_action(
        HarnessAction(HarnessActionType.RUN_SLOT, task_id=task.id, reason="run builder", slot_id="builder"),
        task,
    )

    assert result.ok is True
    instance = PersonaInstanceStore().get(persona_instance_id_for_placement(f"{task.id}:backend_dev"))
    assert instance.goal_id == task.id
    assert instance.spawned_by == "neko_supervisor"
    instance.returned_to = "parent_session_r3"
    summary = persona_instance_summary(instance)
    assert summary["goal_id"] == task.id
    assert summary["spawned_by"] == "neko_supervisor"
    assert summary["returned_to"] == "parent_session_r3"


def test_profile_persona_instance_summary_includes_tool_visibility(isolate_agent_runtime_root):
    instance = PersonaInstance(
        id="personainst_profile_alice",
        persona_id="profile:alice",
        role="profile",
        display_name="Alice Agent",
        profile_id="alice",
        runtime_root=str(REPO_ROOT),
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id="persona_chat_personainst_profile_alice_e898c1dc3794",
    )

    summary = persona_instance_summary(instance)

    assert summary["tool_resolution"]["persona_id"] == "profile:alice"
    assert summary["turn_tool_context"]["persona_id"] == "profile:alice"
    assert "read_file" in summary["tool_resolution"]["final_model_tools"]
    assert "terminal" in summary["tool_resolution"]["final_model_tools"]
    assert "execute_code" in summary["tool_resolution"]["final_model_tools"]
    assert "send_message" in summary["tool_resolution"]["blocked_tool_names"]
    assert summary["permission_state"]["mode"] == "profile_default"
    assert summary["agent_hud_state"]["tool_count"] == len(summary["tool_resolution"]["final_model_tools"])


def test_tick_observe_only_links_assignment_to_run_and_worker(monkeypatch, isolate_agent_runtime_root):
    cfg = _assignment_config()
    tasks = TaskStore()
    runs = RunStore()
    proofs = ProofStore()
    workers = WorkerSessionStore()
    task = _task("task_tick_assign")
    tasks.create(task)

    result = TickEngine(
        task_store=tasks,
        run_store=runs,
        worker_session_store=workers,
        proof_store=proofs,
        persona_runtime=RequestProofRuntime(),
        proof_runner=PassingProofRunner(proofs),
        config=cfg,
    ).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    assignment_id = result.actions_taken[0].payload["assignment_id"]
    assignment = PersonaAssignmentStore().get(assignment_id)
    run = runs.get(assignment.run_ids[0])
    worker = workers.list_for_task(task.id)[0]
    assert assignment.state == "completed"
    assert run.progress["assignment_id"] == assignment.id
    assert run.progress["persona_instance_id"] == persona_instance_id_for_placement(f"{task.id}:dev")
    assert worker.current_assignment_id == assignment.id


def test_tick_reuses_task_flagged_diagnostic_assignment(isolate_agent_runtime_root):
    cfg = _assignment_config()
    tasks = TaskStore()
    runs = RunStore()
    workers = WorkerSessionStore()
    agents = AgentStore()
    agents.save(_persona("neko_supervisor"))
    task = _task("task_diag_assign", state=TaskState.CREATED)
    task.current_stage_id = None
    assignment = PersonaAssignmentStore().create_or_resume(
        PersonaAssignmentSpec(
            persona_id="neko_supervisor",
            kind="diagnostic",
            title="Neko diagnostic",
            message="Run one scoped Neko diagnostic.",
            created_by="test",
            task_id=task.id,
        )
    )
    task.risk_flags = [f"persona_assignment_id:{assignment.id}"]
    tasks.create(task)

    result = TickEngine(
        task_store=tasks,
        run_store=runs,
        agent_store=agents,
        worker_session_store=workers,
        persona_runtime=NekoAcceptanceRuntime(),
        config=cfg,
    ).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    assert result.actions_taken[0].payload["assignment_id"] == assignment.id
    assignments = PersonaAssignmentStore().list_for_task(task.id)
    assert [item.id for item in assignments] == [assignment.id]
    updated = PersonaAssignmentStore().get(assignment.id)
    run = runs.get(updated.run_ids[0])
    worker = workers.list_for_task(task.id)[0]
    assert updated.state == "completed"
    assert run.progress["assignment_id"] == assignment.id
    assert run.progress["assignment_kind"] == "diagnostic"
    assert worker.current_assignment_id == assignment.id


def test_tick_assignment_tracks_command_proof_and_archive_preserves_it(isolate_agent_runtime_root):
    cfg = _assignment_config()
    tasks = TaskStore()
    proofs = ProofStore()
    workers = WorkerSessionStore()
    task = _task("task_archive_assignment")
    tasks.create(task)

    result = TickEngine(
        task_store=tasks,
        proof_store=proofs,
        worker_session_store=workers,
        persona_runtime=RequestProofRuntime(),
        proof_runner=PassingProofRunner(proofs),
        config=cfg,
    ).tick_once(task_id=task.id)

    assert result.actions_taken[0].ok
    assignment_id = result.actions_taken[0].payload["assignment_id"]
    assignment = PersonaAssignmentStore().get(assignment_id)
    assert assignment.proof_ids == ["proof_assignment_ok"]

    workers.close(workers.list_for_task(task.id)[0].id, reason="ready to archive")
    saved = tasks.get(task.id)
    saved.state = TaskState.DONE
    saved.updated_at = now()
    tasks.update(saved, actor="harness", reason="test terminal")
    archived = tasks.archive(task.id, actor="cli", reason="test archive")

    batch = Path(archived["archive_dir"])
    assert archived["archived_tasks"][0]["persona_assignment_ids"] == [assignment_id]
    assert (batch / "persona_assignments" / f"{assignment_id}.json").exists()
    snapshot = build_snapshot()
    archived_task = snapshot["archived_tasks"][0]
    assert archived_task["persona_assignment_ids"] == [assignment_id]
    assert archived_task["persona_streams"]["dev"]["assignment_ids"] == [assignment_id]


def test_close_for_task_releases_active_assignments_and_leaves_other_goals():
    store = PersonaAssignmentStore()
    a = store.create_or_resume(
        PersonaAssignmentSpec(persona_id="neko_supervisor", kind="scope", title="scope", message="m", task_id="task_g1", goal_id="task_g1")
    )
    b = store.create_or_resume(
        PersonaAssignmentSpec(persona_id="backend_dev", kind="task_stage", title="impl", message="m", task_id="task_g1", goal_id="task_g1", stage_id="s1")
    )
    store.create_or_resume(
        PersonaAssignmentSpec(persona_id="dev", kind="task_stage", title="impl", message="m", task_id="task_g2", goal_id="task_g2")
    )

    assert {"neko_supervisor", "backend_dev", "dev"} <= {x.persona_id for x in store.find_active()}

    closed = store.close_for_task("task_g1", state="cancelled", reason="graveyard cleanup")

    assert set(closed) == {a.id, b.id}
    active_after = {x.persona_id for x in store.find_active()}
    assert "neko_supervisor" not in active_after
    assert "backend_dev" not in active_after
    assert "dev" in active_after  # a different goal's assignment is untouched
    assert store.get(a.id).state == "cancelled"
    assert store.close_for_task("task_g1") == []  # idempotent


def test_task_store_cancel_closes_persona_assignments():
    task_store = TaskStore()
    task_store.create(_task("task_cancel", state=TaskState.RUNNING))
    assignment_store = PersonaAssignmentStore()
    assignment = assignment_store.create_or_resume(
        PersonaAssignmentSpec(persona_id="neko_supervisor", kind="scope", title="scope", message="m", task_id="task_cancel", goal_id="task_cancel")
    )
    assert assignment.id in {x.id for x in assignment_store.find_active(persona_id="neko_supervisor")}

    task_store.cancel("task_cancel", reason="operator cancel")

    assert assignment_store.find_active(persona_id="neko_supervisor") == []
    assert assignment_store.get(assignment.id).state == "cancelled"


def test_task_store_update_terminal_transition_releases_assignments():
    """Any writer that lands a task in a terminal state via TaskStore.update must
    release its persona assignments — the persona-diagnostics driver sets DONE
    directly (bypassing ticker COMPLETE_TASK and TaskStore.cancel), which left
    done-but-unarchived diag goals holding queued/needs_input slots live on
    2026-07-03 (task_008d575b / task_8bd8b4af / task_940caf52)."""
    task_store = TaskStore()
    task_store.create(_task("task_direct_done", state=TaskState.RUNNING))
    assignment_store = PersonaAssignmentStore()
    assignment = assignment_store.create_or_resume(
        PersonaAssignmentSpec(persona_id="backend_dev", kind="task_stage", title="impl", message="m", task_id="task_direct_done", goal_id="task_direct_done")
    )
    assert assignment_store.find_active(persona_id="backend_dev")

    saved = task_store.get("task_direct_done")
    saved.state = TaskState.DONE
    saved.updated_at = now()
    task_store.update(saved, actor="harness", reason="diagnostic finalize (direct DONE)")

    assert assignment_store.find_active(persona_id="backend_dev") == []
    assert assignment_store.get(assignment.id).state == "completed"


def test_task_store_update_failed_transition_releases_assignments():
    task_store = TaskStore()
    task_store.create(_task("task_direct_failed", state=TaskState.RUNNING))
    assignment_store = PersonaAssignmentStore()
    assignment = assignment_store.create_or_resume(
        PersonaAssignmentSpec(persona_id="dev", kind="task_stage", title="impl", message="m", task_id="task_direct_failed", goal_id="task_direct_failed")
    )

    saved = task_store.get("task_direct_failed")
    saved.state = TaskState.FAILED
    saved.updated_at = now()
    task_store.update(saved, actor="harness", reason="fatal")

    assert assignment_store.find_active(persona_id="dev") == []
    assert assignment_store.get(assignment.id).state == "cancelled"


def test_task_store_update_non_terminal_transition_keeps_assignments():
    task_store = TaskStore()
    task_store.create(_task("task_stays_open", state=TaskState.RUNNING))
    assignment_store = PersonaAssignmentStore()
    assignment_store.create_or_resume(
        PersonaAssignmentSpec(persona_id="qa", kind="task_stage", title="verdict", message="m", task_id="task_stays_open", goal_id="task_stays_open")
    )

    saved = task_store.get("task_stays_open")
    saved.state = TaskState.BLOCKED
    saved.updated_at = now()
    task_store.update(saved, actor="harness", reason="waiting on operator")

    assert assignment_store.find_active(persona_id="qa"), "non-terminal transitions must not release assignments"


def test_contention_warning_self_heals_assignment_held_by_terminal_goal():
    """A stale active assignment held by a done-but-unarchived goal is released
    (not warned about): the warning must stay an honest signal of genuinely
    concurrent goals."""
    task_store = TaskStore()
    task_store.create(_task("task_old_done", state=TaskState.DONE))
    assignment_store = PersonaAssignmentStore()
    stale = assignment_store.create_or_resume(
        PersonaAssignmentSpec(persona_id="backend_dev", kind="task_stage", title="impl", message="m", task_id="task_old_done", goal_id="task_old_done")
    )

    warnings = assignment_store.contention_warnings(persona_id="backend_dev", goal_id="task_new_goal")

    assert warnings == []
    assert assignment_store.get(stale.id).state == "completed"
    assert "owning goal is done" in str(assignment_store.get(stale.id).last_error or "")


def test_contention_warning_still_fires_for_genuinely_open_goal():
    task_store = TaskStore()
    task_store.create(_task("task_live_goal", state=TaskState.RUNNING))
    assignment_store = PersonaAssignmentStore()
    live = assignment_store.create_or_resume(
        PersonaAssignmentSpec(persona_id="backend_dev", kind="task_stage", title="impl", message="m", task_id="task_live_goal", goal_id="task_live_goal")
    )

    warnings = assignment_store.contention_warnings(persona_id="backend_dev", goal_id="task_new_goal")

    assert [w["code"] for w in warnings] == ["agent_already_assigned"]
    assert assignment_store.get(live.id).state in ACTIVE_ASSIGNMENT_STATES


def test_contention_warning_self_heals_assignment_for_archived_goal():
    """Owning task file gone from the live store (archived) → assignment is
    releasable, not contention."""
    assignment_store = PersonaAssignmentStore()
    stale = assignment_store.create_or_resume(
        PersonaAssignmentSpec(persona_id="dev", kind="task_stage", title="impl", message="m", task_id="task_gone_archived", goal_id="task_gone_archived")
    )

    warnings = assignment_store.contention_warnings(persona_id="dev", goal_id="task_new_goal")

    assert warnings == []
    assert assignment_store.get(stale.id).state == "completed"
