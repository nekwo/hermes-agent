from __future__ import annotations

import json
from pathlib import Path

from hermes_time import now

from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, Proof, Task
from agent_runtime.persona_assignments import (
    PersonaAssignmentSpec,
    PersonaAssignmentStore,
    PersonaInstanceStore,
)
from agent_runtime.persona_chat_history import persona_chat_history_summary
from agent_runtime.proof_rules import ProofType
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import TaskState, WorkerSessionState
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


def _task(task_id: str = "task_assign", state: TaskState = TaskState.DEV_IMPLEMENTING) -> Task:
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
            payload={"stage_id": "stage_1", "commands": ["python -c \"print('assignment-ok')\""]},
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
    assert snapshot["persona_instance_runtime"]["enabled"] is True
    assert {item["persona_id"] for item in snapshot["persona_instances"]} >= {"dev", "qa", "neko_supervisor", "backend_dev"}


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
            json=True,
        )
    )

    assert code == 0
    assignments = PersonaAssignmentStore().list_for_persona("dev")
    assert len(assignments) == 1
    assert assignments[0].task_id is None
    assert assignments[0].kind == "free_floating_message"
    assert assignments[0].production_proof_eligible is False
    instance = PersonaInstanceStore().get(assignments[0].persona_instance_id)
    assert instance.mode == "free_floating"
    assert instance.current_assignment_id == assignments[0].id
    assert RunStore().list_all() == []


def test_persona_instance_open_chat_binds_old_chat_without_ticking(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="launcher-dev",
            session_id="chat_old_123",
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


class _FakeSessionDB:
    def __init__(self, sessions, messages=None):
        self.sessions = sessions
        self.messages = messages or {}

    def list_sessions_rich(self, **kwargs):
        return list(self.sessions)

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
            "messages": [],
        }
    ]


def test_snapshot_preserves_open_chat_and_emits_history(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.persona_chat_history as history

    cfg = _assignment_config()
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(
        history,
        "_default_session_db",
        lambda: _FakeSessionDB(
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
        ),
    )
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
            "messages": [
                {
                    "id": "msg_1",
                    "role": "operator",
                    "safe_text": "Can you keep working from the old chat?",
                    "timestamp": "2026-06-19T10:00:00Z",
                    "redaction_status": "safe",
                },
                {
                    "id": "msg_2",
                    "role": "agent",
                    "safe_text": "Yes. I will continue from this persona session.",
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

    def append_message(self, session_id, role, content=None, **kwargs):
        self.messages.setdefault(session_id, []).append(
            {"role": role, "content": content, **kwargs}
        )
        return len(self.messages[session_id])

    def get_messages(self, session_id, include_inactive=False):
        return list(self.messages.get(session_id, []))

    def get_session_title(self, session_id):
        return self.titles.get(session_id)

    def set_session_title(self, session_id, title):
        self.titles[session_id] = title


def test_free_floating_chat_session_binding_reuses_instance_session(monkeypatch, isolate_agent_runtime_root):
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
    second = harness._bind_free_floating_chat_session(
        instance_store=instance_store,
        session_db=db,
        persona_id="dev",
        persona_instance_id="personainst_dev",
        assignment_id="assign_2",
    )

    assert first == "persona_chat_personainst_dev"
    assert second == first
    assert sorted(db.sessions) == [first]
    instance = instance_store.get("personainst_dev")
    assert instance.session_id == first
    assert instance.current_assignment_id == "assign_2"
    assert instance.mode == "free_floating"


def test_persona_chat_transcript_records_operator_and_decision_fallback(isolate_agent_runtime_root):
    from hermes_cli import harness

    db = _TranscriptDB()
    session_id = "persona_chat_personainst_dev"
    db.create_session(session_id, "agent_runtime_persona_chat")
    harness._append_persona_operator_turn(
        session_db=db,
        session_id=session_id,
        message="hi",
    )
    harness._append_persona_decision_reply(
        session_db=db,
        session_id=session_id,
        summary="Scope a safe diagnostic.",
        rationale="The operator sent a greeting.",
    )

    assert [item["role"] for item in db.messages[session_id]] == [
        "user",
        "assistant",
    ]
    assert db.messages[session_id][0]["content"] == "hi"
    assert db.messages[session_id][1]["content"] == (
        "Scope a safe diagnostic.\n\nThe operator sent a greeting."
    )


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
    assert run.progress["persona_instance_id"] == "personainst_dev"
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
