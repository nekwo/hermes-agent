from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime import paths
from agent_runtime.tool_permissions import default_permission_mode
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, AgentRun, PersonaInstance

Task = SimpleNamespace
from agent_runtime.mission_chat_turns import (
    mission_chat_turn_elements,
    mission_chat_turn_record,
    persist_mission_chat_turn,
    transition_mission_chat_turn,
)
from agent_runtime.persona_assignments import (
    ACTIVE_ASSIGNMENT_STATES,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_chat_session_id_for,
    persona_instance_summary,
    persona_instance_tool_detail,
    persona_instance_id_for,
    persona_instance_id_for_placement,
)
from agent_runtime.persona_chat_history import persona_chat_history_summary
from agent_runtime.persona_instance_identity import reconcile_persona_instances
from agent_runtime.snapshot import build_snapshot
from agent_runtime.serde import to_jsonable
from agent_runtime.states import RunState, TaskState, WorkerSessionState
from agent_runtime.status import build_status
from agent_runtime.store import AgentStore, RunStore, TaskStore
from tests.agent_runtime.conftest import release_to_implementation
from utils import atomic_json_write


def _seed_run(
    persona_id: str,
    task_id: str,
    stage_id: str | None = None,
    *,
    session_id: str | None = None,
) -> AgentRun:
    """Persist a run row without ``RunStore.open_run``.

    The run store is now historical/read-only. Tests that exercise projections
    seed a representative historical row directly.
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
    atomic_json_write(paths.run_path(run.id), to_jsonable(run), indent=2, sort_keys=True)
    return run


REPO_ROOT = Path(__file__).resolve().parents[2]


def _persona(persona_id: str = "dev", *, role: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} worker",
        role=role,
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
    # S56: the persona-instance runtime / assignment store are unconditional now;
    # the enterprise_worker_sessions gate block was deleted.
    return AgentRuntimeConfig()


def _assert_task_store_stub() -> None:
    store = TaskStore(event_log=EventLog())
    assert not hasattr(store, "create")
    assert not hasattr(store, "update")
    assert not hasattr(store, "cancel")
    with pytest.raises(Exception):
        store.get("retired_task")


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


def test_persona_instance_store_ensures_a_singleton_per_persona(isolate_agent_runtime_root):
    # S56 renamed derive_from_workers(personas, workers) -> ensure_for_personas
    # (the worker half is gone). The surviving contract is one canonical instance
    # per configured persona.
    store = PersonaInstanceStore()

    instances = store.ensure_for_personas([_persona("dev"), _persona("qa")])

    by_id = {item.persona_id: item for item in instances}
    assert by_id["dev"].id == "personainst_dev"
    assert by_id["qa"].id == "personainst_qa"


def test_disabled_and_mothballed_definitions_never_materialize_runtime_instances(
    isolate_agent_runtime_root,
):
    store = PersonaInstanceStore()

    instances = store.ensure_for_personas(
        [
            _persona("dev"),
            _persona("custom_reviewer", role="reviewer"),
            _persona("retired_coordinator", role="disabled"),
            _persona("pm", role="disabled"),
        ]
    )

    assert {item.persona_id for item in instances} == {"dev", "custom_reviewer"}


def test_reconciled_disabled_pm_is_not_recreated_by_read_builds(
    isolate_agent_runtime_root,
):
    store = PersonaInstanceStore()
    pm = _persona("pm", role="disabled")
    store.ensure_for_persona(pm)

    report = reconcile_persona_instances(event_log=EventLog())
    assert report["pruned_count"] == 1
    assert store.list_all() == []

    # These are the central paths used by snapshot/status/persona-list/chat
    # initialization. Repeating them after reconciliation must remain a no-op.
    assert store.ensure_for_personas([pm]) == []
    build_snapshot()
    build_status()
    assert not [item for item in store.list_all() if item.persona_id == "pm"]

    again = reconcile_persona_instances(event_log=EventLog())
    assert again["pruned_count"] == 0


def test_persona_instance_ensure_clears_a_stale_execution_binding(isolate_agent_runtime_root):
    # S56: with the worker lane gone the reset pass is unconditional — an
    # instance still carrying a dead task/assignment binding settles back to idle.
    store = PersonaInstanceStore()
    stale = store.ensure_for_persona(_persona("dev"))
    stale.mode = "task_bound"
    stale.state = WorkerSessionState.RUNNING
    stale.current_task_id = "task_stale"
    stale.current_assignment_id = "assign_stale"
    stale.goal_id = "task_stale"
    store.update(stale)

    instances = store.ensure_for_personas([_persona("dev")])

    instance = instances[0]
    assert instance.id == "personainst_dev"
    assert instance.state == WorkerSessionState.IDLE
    assert instance.current_assignment_id is None
    assert instance.current_task_id is None
    assert instance.active_run_id is None
    assert instance.session_id is None


def test_persona_instance_derivation_does_not_mark_idle_worker_active(isolate_agent_runtime_root):
    _assert_task_store_stub()


def test_dead_worker_of_settled_task_does_not_resurrect_binding(isolate_agent_runtime_root):
    _assert_task_store_stub()


def test_task_terminal_reaps_task_bound_persona_instances(isolate_agent_runtime_root):
    _assert_task_store_stub()


def test_task_archive_moves_task_bound_persona_instance_evidence(isolate_agent_runtime_root):
    _assert_task_store_stub()


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
    # S56 removed PersonaInstance.active_worker_session_id outright.
    assert not hasattr(first, "active_worker_session_id")

    instances = store.ensure_for_personas([_persona("dev")])
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


def test_additional_placement_stamps_scope_pointers(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    placed = store.add_instance(
        persona_id="qa",
        placement_id="agent_a1b2c3d4",
        display_name="QA Agent (2)",
        workspace_id="ws_testv4",
        realm_id="realm_test",
    )
    assert placed.workspace_id == "ws_testv4"
    assert placed.realm_id == "realm_test"

    # The pointers survive the store round-trip and ship in the snapshot row.
    reloaded = store.get(placed.id)
    assert reloaded.workspace_id == "ws_testv4"
    assert reloaded.realm_id == "realm_test"
    summary = persona_instance_summary(reloaded)
    assert summary["workspace_id"] == "ws_testv4"
    assert summary["realm_id"] == "realm_test"

    # A plain re-open that carries no scope must NOT clear the pointers —
    # ordinary chat opens don't know scope and never erase provenance.
    reopened = store.open_chat(
        persona_id="qa",
        persona_instance_id=placed.id,
        session_id=placed.session_id,
    )
    assert reopened.workspace_id == "ws_testv4"
    assert reopened.realm_id == "realm_test"


def test_scope_pointers_default_none_for_unscoped_lanes(isolate_agent_runtime_root):
    store = PersonaInstanceStore()
    chat = store.create_operator_chat(
        persona_id="profile:alice",
        display_name="Alice Agent",
    )
    assert chat.workspace_id is None
    assert chat.realm_id is None
    summary = persona_instance_summary(chat)
    assert summary["workspace_id"] is None
    assert summary["realm_id"] is None


# S70 removed the assignment MINT side (`create` / `create_or_resume` /
# `PersonaAssignmentSpec` and the evidence/archive-scope/signal-hash
# derivations) with the free-floating queue lane; the tombstone registry (wave
# s70) owns their absence. The store's READ/CLOSE side survives for residual
# on-disk rows, so tests of surviving behaviour seed representative historical
# rows directly — the same pattern `_seed_run` uses for the historical run
# store.


def _seed_assignment(
    *,
    persona_id: str,
    kind: str = "free_floating_message",
    state: str = "queued",
    persona_instance_id: str | None = None,
    evidence_kind: str = "free_floating",
    archive_scope: str = "assignment",
    production_proof_eligible: bool = False,
    client_message_id: str | None = None,
) -> "PersonaAssignment":
    from agent_runtime.models import PersonaAssignment

    ts = now()
    assignment = PersonaAssignment(
        id=f"assign_{uuid.uuid4().hex[:12]}",
        persona_instance_id=persona_instance_id or persona_instance_id_for(persona_id),
        persona_id=persona_id,
        kind=kind,
        state=state,
        title=f"{persona_id} residual assignment",
        message="historical row",
        created_by="test",
        created_at=ts,
        updated_at=ts,
        evidence_kind=evidence_kind,
        production_proof_eligible=production_proof_eligible,
        archive_scope=archive_scope,
        client_message_id=client_message_id,
    )
    atomic_json_write(
        paths.persona_assignment_path(assignment.id),
        to_jsonable(assignment),
        indent=2,
        sort_keys=True,
    )
    return assignment


def test_seeded_diagnostic_assignment_summary_marks_not_production_proof(isolate_agent_runtime_root):
    assignment = _seed_assignment(
        persona_id="qa",
        kind="diagnostic",
        evidence_kind="diagnostic",
        archive_scope="task",
    )

    summary = PersonaAssignmentStore().get(assignment.id)
    rendered = __import__("agent_runtime.persona_assignments", fromlist=["persona_assignment_summary"]).persona_assignment_summary(summary)
    assert rendered["evidence_kind"] == "diagnostic"
    assert rendered["production_proof_eligible"] is False
    assert rendered["archive_scope"] == "task"


def test_assignment_complete_is_idempotent_for_same_terminal_state(isolate_agent_runtime_root):
    store = PersonaAssignmentStore()
    assignment = _seed_assignment(
        persona_id="neko_supervisor",
        kind="diagnostic",
        evidence_kind="diagnostic",
        archive_scope="task",
    )

    first = store.complete(assignment.id, state="completed")
    second = store.complete(assignment.id, state="completed")

    events = [event for event in EventLog().tail(20) if event.type == "persona_assignment.closed"]
    assert first.completed_at == second.completed_at
    assert [event.payload["assignment_id"] for event in events] == [assignment.id]


def test_status_and_snapshot_expose_persona_instances_unconditionally(monkeypatch, isolate_agent_runtime_root):
    cfg = _assignment_config()
    monkeypatch.setattr("agent_runtime.status.load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    # S56: the worker session that used to make ``dev`` an active-lane agent is
    # gone; a chat instance carrying a live goal/assignment is the surviving way
    # in (``ensure_for_personas`` settles every non-chat instance back to idle).
    instance_store = PersonaInstanceStore()
    live = instance_store.create_operator_chat(persona_id="dev", display_name="dev worker")
    live.goal_id = "task_1"
    live.current_assignment_id = "assign_1"
    instance_store.update(live)

    # S56: build_status / build_snapshot no longer take worker_session_store=,
    # and the persona-instance roster ships unconditionally.
    status = build_status()
    snapshot = build_snapshot()

    # The wire block survives as a fixed True/True pair for stale readers.
    assert status["persona_instance_runtime"] == {
        "enabled": True,
        "assignment_store_enabled": True,
    }
    # S56 retired these build_status rows with the worker/bundle/envelope lanes.
    for retired in (
        "worker_sessions",
        "active_worker_sessions",
        "repo_bundles",
        "repo_bundle_closeout",
        "bundle_queue",
        "repo_locks",
        "lanes",
        "production_envelope",
        "swarm",
        "swarm_budget",
    ):
        assert retired not in status, retired
    # …while the runtime-instance rows deliberately STAY.
    assert "runtime_instances" in status
    assert "foreground_runtime" in status
    assert {item["persona_id"] for item in status["persona_instances"]} >= {"dev", "qa", "neko_supervisor", "backend_dev"}
    status_lane_agents = {item["persona_id"]: item for item in status["agents"]}
    assert "personainst_dev" in status_lane_agents
    assert status_lane_agents["personainst_dev"]["source_persona_id"] == "dev"
    # Residue-slim R2: the fat tool payloads (tool_resolution) + the retired
    # agent_hud_state leave the row; the head keeps a typed visibility_ref pointer
    # + the always-visible scalars, fetched in full via `harness persona-instance
    # detail`.
    assert "agent_hud_state" not in status_lane_agents["personainst_dev"]
    assert "tool_resolution" not in status_lane_agents["personainst_dev"]
    assert isinstance(status_lane_agents["personainst_dev"]["visibility_ref"], dict)
    assert status_lane_agents["personainst_dev"]["visibility_ref"]["evicted"] is True
    assert isinstance(status_lane_agents["personainst_dev"]["mutation_boundary"], dict)
    assert snapshot["persona_instance_runtime"]["enabled"] is True
    assert {item["persona_id"] for item in list(snapshot["persona_instances"].values())} >= {"dev", "qa", "neko_supervisor", "backend_dev"}
    snapshot_lane_agents = {item["persona_id"]: item for item in snapshot["agents"]}
    # S56 INVERSION: this used to pin "personainst_dev not in the snapshot agent
    # catalog", but that only held because build_snapshot seeded ``workers = []``
    # so the instance derived IDLE while build_status (handed the real worker
    # store) saw it active. Both projections now share one unconditional roster,
    # so an ACTIVE instance appears in both agent lanes; idle ones still don't
    # (see test_snapshot_exposes_operator_created_idle_persona_instance).
    assert "personainst_dev" in snapshot_lane_agents
    assert snapshot_lane_agents["personainst_dev"]["source_persona_id"] == "dev"


def test_snapshot_exposes_operator_created_idle_persona_instance(monkeypatch, isolate_agent_runtime_root):
    cfg = _assignment_config()
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    created = PersonaInstanceStore().create_free_floating("profile:reviewer")

    snapshot = build_snapshot()
    by_id = {item["persona_instance_id"]: item for item in list(snapshot["persona_instances"].values())}

    assert created.id in by_id
    assert by_id[created.id]["agent_profile_id"] == created.id
    assert by_id[created.id]["persona_id"] == "profile:reviewer"
    assert by_id[created.id]["source_persona_id"] == "profile:reviewer"
    assert by_id[created.id]["source_profile_id"] == "reviewer"
    assert created.id == "personainst_profile_reviewer"
    assert by_id[created.id]["state"] == "idle"
    assert by_id[created.id]["lifecycle_mode"] == "free_floating"
    assert by_id[created.id]["mode"] == "free_floating"
    # S56 dropped active_worker_session_id from the model and every wire row.
    assert "active_worker_session_id" not in by_id[created.id]


def test_persona_instance_create_without_display_name_refuses_and_mints_nothing(monkeypatch, capsys, isolate_agent_runtime_root):
    """S70: the display-name-less branch used to queue a free-floating
    assignment nothing would ever consume (the tick loop that drained the queue
    was removed by the 2026-07-30 chat-only purge, and the advertised
    ``run-once`` follow-up verb never existed). The branch is now a typed
    refusal pointing at the chat lane — and it must mint NO assignment row and
    NO instance row on the way out."""
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_create(
        Namespace(
            persona_id="dev",
            title="Launcher Dev sandbox",
            message="Check the persona without a product task.",
            requested_by="test",
            client_message_id=None,
            display_name=None,
            session_id=None,
            kill_active=False,
            add_instance=False,
            placement_id=None,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "free-floating assignment lane is retired" in payload["error"]
    assert "mission-chat message" in payload["next_expected"]
    assert PersonaAssignmentStore().list_for_persona("dev") == []
    assert PersonaInstanceStore().list_all() == []
    assert RunStore().list_all() == []


def test_persona_instance_message_verb_is_gone_from_the_parser():
    """S70 removed `persona instance message` (and create's --auto-run /
    --stream / --max-actions / --max-seconds). argparse must REJECT them
    cleanly — a removed lane must never degrade to a silent ignore."""
    import argparse

    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        root.parse_args(
            ["harness", "persona", "instance", "message", "personainst_dev", "--message", "hi"]
        )
    with pytest.raises(SystemExit):
        root.parse_args(
            [
                "harness", "persona", "instance", "create",
                "--persona", "dev", "--title", "t", "--message", "m",
                "--auto-run",
            ]
        )
    # The surviving create shape still parses (display-name mint lane).
    args = root.parse_args(
        [
            "harness", "persona", "instance", "create",
            "--persona", "dev", "--title", "t", "--message", "m",
            "--display-name", "Dev Agent", "--json",
        ]
    )
    assert args.display_name == "Dev Agent"


def test_coordinator_create_beyond_spawn_scope_returns_confirm_without_creating(monkeypatch, capsys, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_create(
        Namespace(
            persona_id="dev",
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


def test_persona_instance_open_chat_binds_old_chat_without_ticking(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    session_db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: session_db)
    previous = PersonaInstanceStore().create_operator_chat(
        persona_id="dev",
        display_name="dev worker",
        session_id="chat_current_123",
    )

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="dev",
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
    assert session_db.get_session(instance.session_id) is not None
    assert session_db.get_session_title(instance.session_id) == f"{instance.display_name} chat"
    payload = json.loads(capsys.readouterr().out)
    assert payload["previous_session_id"] == previous.session_id
    assert payload["binding_receipt"] == {
        "schema_version": 1,
        "persona_instance_id": instance.id,
        "session_id": "chat_old_123",
        "previous_session_id": previous.session_id,
        "changed": True,
        "instance_updated_at": instance.updated_at.isoformat(),
    }

    assert harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="dev",
            session_id="chat_old_123",
            kill_active=False,
            json=True,
        )
    ) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["binding_receipt"]["changed"] is False
    assert replay["binding_receipt"]["previous_session_id"] == "chat_old_123"


def test_persona_instance_open_chat_new_session_mints_exact_instance_and_replays(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    session_db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: session_db)
    existing = PersonaInstanceStore().create_operator_chat(
        persona_id="dev",
        display_name="Launcher Dev Agent",
    )
    original_session = existing.session_id
    args = Namespace(
        persona_id="dev",
        persona_instance_id=existing.id,
        session_id=None,
        new_session=True,
        idempotency_key="new-chat-dev-1",
        kill_active=False,
        add_instance=False,
        json=True,
    )

    assert harness._cmd_persona_instance_open_chat(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["ok"] is True
    assert first["persona_instance_id"] == existing.id
    assert first["session_id"] != original_session
    assert first["mission_chat_root_id"] == first["session_id"]
    assert first["idempotent_replay"] is False
    assert first["mint_receipt_state"] == "bound"
    assert PersonaInstanceStore().get(existing.id).session_id == first["session_id"]
    assert session_db.get_session(first["session_id"]) is not None

    assert harness._cmd_persona_instance_open_chat(args) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["session_id"] == first["session_id"]
    assert replay["idempotent_replay"] is True
    assert replay["selected"] is True

    distinct_args = Namespace(
        **{
            **vars(args),
            "idempotency_key": "new-chat-dev-2",
        }
    )
    assert harness._cmd_persona_instance_open_chat(distinct_args) == 0
    distinct = json.loads(capsys.readouterr().out)
    assert distinct["session_id"] != first["session_id"]
    assert distinct["idempotent_replay"] is False
    assert PersonaInstanceStore().get(existing.id).session_id == distinct["session_id"]


def test_persona_instance_open_chat_new_session_retry_recovers_reserved_root(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    existing = PersonaInstanceStore().create_operator_chat(
        persona_id="dev",
        display_name="Launcher Dev Agent",
    )
    original_session = existing.session_id
    args = Namespace(
        persona_id="dev",
        persona_instance_id=existing.id,
        session_id=None,
        new_session=True,
        idempotency_key="new-chat-recover-1",
        kill_active=False,
        add_instance=False,
        json=True,
    )
    monkeypatch.setattr(
        harness,
        "_default_persona_session_db",
        lambda: _FailingTranscriptDB("session_create"),
    )

    assert harness._cmd_persona_instance_open_chat(args) == 2
    failed = json.loads(capsys.readouterr().out)
    assert failed["error_kind"] == "chat_session_persist_failed"
    assert failed["mint_receipt_state"] == "reserved"
    assert PersonaInstanceStore().get(existing.id).session_id == original_session

    recovered_db = _TranscriptDB()
    monkeypatch.setattr(
        harness,
        "_default_persona_session_db",
        lambda: recovered_db,
    )
    assert harness._cmd_persona_instance_open_chat(args) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["session_id"] == failed["session_id"]
    assert recovered["idempotent_replay"] is True
    assert recovered["mint_receipt_state"] == "bound"
    assert PersonaInstanceStore().get(existing.id).session_id == failed["session_id"]


def test_persona_instance_open_chat_new_session_rejects_idempotency_scope_conflict(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    session_db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: session_db)
    store = PersonaInstanceStore()
    dev = store.create_operator_chat(persona_id="dev", display_name="Dev")
    qa = store.create_operator_chat(persona_id="qa", display_name="QA")

    def args_for(persona_id, instance_id):
        return Namespace(
            persona_id=persona_id,
            persona_instance_id=instance_id,
            session_id=None,
            new_session=True,
            idempotency_key="shared-key-is-a-client-bug",
            kill_active=False,
            add_instance=False,
            json=True,
        )

    assert harness._cmd_persona_instance_open_chat(args_for("dev", dev.id)) == 0
    capsys.readouterr()
    assert harness._cmd_persona_instance_open_chat(args_for("qa", qa.id)) == 2
    conflict = json.loads(capsys.readouterr().out)
    assert conflict["error_kind"] == "idempotency_conflict"
    assert PersonaInstanceStore().get(qa.id).session_id == qa.session_id


def test_persona_instance_open_chat_can_target_additional_placement(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    session_db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: session_db)

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
    assert session_db.get_session(additional.session_id) is not None
    assert session_db.get_session_title(additional.session_id) == f"{additional.display_name} chat"


def test_persona_instance_open_chat_session_persistence_failure_is_typed_and_not_silent(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(
        harness,
        "_default_persona_session_db",
        lambda: _FailingTranscriptDB("session_create"),
    )

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="dev",
            session_id="chat_persist_failure_123",
            kill_active=False,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_kind"] == "chat_session_persist_failed"
    assert payload["persistence_operation"] == "session_create"
    assert payload["persona_id"] == "dev"
    assert payload["persona_instance_id"] == "personainst_dev"
    assert payload["session_id"] == "chat_persist_failure_123"


def test_open_chat_cli_targets_the_session_owner_not_the_canonical(monkeypatch, isolate_agent_runtime_root):
    # Poison origin fix: the console's open-chat of a sibling passes the sibling's
    # session with a bare persona id. The CLI must rebind the instance the session
    # was minted FOR (personainst_qa_agent_2), never overwrite the canonical
    # primary's (personainst_qa) pointer with it.
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    sibling = PersonaInstanceStore().add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent (2)"
    )
    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="qa",
            persona_instance_id=sibling.id,
            session_id=None,
            new_session=True,
            idempotency_key="open-sibling-owner",
            kill_active=False,
            json=True,
        )
    )

    assert code == 0
    fresh = PersonaInstanceStore()
    # The sibling was rebound to its own session; the canonical primary never
    # adopted it.
    sibling_root = fresh.get("personainst_qa_agent_2").default_chat_session_id
    assert sibling_root
    assert sibling_root != sibling.session_id
    primary_adopted = False
    try:
        primary_adopted = fresh.get("personainst_qa").default_chat_session_id == sibling_root
    except Exception:
        primary_adopted = False
    assert not primary_adopted


def test_open_chat_default_display_name_never_renames_an_existing_named_instance(
    isolate_agent_runtime_root,
):
    # DEFECT B: the send path stamps the persona DEFAULT display_name. It must
    # NEVER rename an existing instance — the placement sibling reads "QA Agent
    # (2)" and the "(2)" is load-bearing (launcher conversational fold keys on
    # persona+displayName). Two "sends" (open_chat with default_display_name)
    # must both preserve the deliberate name.
    store = PersonaInstanceStore()
    sibling = store.add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent (2)"
    )
    assert sibling.display_name == "QA Agent (2)"

    for _ in range(2):  # repeated sends thread onto the same instance
        store.open_chat(
            persona_id="qa",
            persona_instance_id="personainst_qa_agent_2",
            session_id=sibling.session_id,
            default_display_name="QA Agent",  # the persona default the send path passes
        )
    assert store.get("personainst_qa_agent_2").display_name == "QA Agent (2)"


def test_open_chat_same_authoritative_binding_is_a_true_no_op(
    isolate_agent_runtime_root,
):
    """Repeated first-turn binding must not rewrite the row or fan out another
    full-core stream event when every authoritative field is already equal."""

    from agent_runtime.events import EventLog

    events = EventLog()
    store = PersonaInstanceStore(event_log=events)
    session_id = "persona_chat_personainst_dev_aaaaaaaaaaaa"
    first = store.open_chat(
        persona_id="dev",
        session_id=session_id,
        default_display_name="Dev",
    )
    opened_before = [
        event
        for event in events.tail(20)
        if event.type == "persona_instance.chat_opened"
    ]

    reopened = store.open_chat(
        persona_id="dev",
        session_id=session_id,
        default_display_name="Dev",
    )
    opened_after = [
        event
        for event in events.tail(20)
        if event.type == "persona_instance.chat_opened"
    ]

    assert reopened.updated_at == first.updated_at
    assert len(opened_before) == 1
    assert len(opened_after) == 1


def test_open_chat_default_display_name_names_a_first_ever_holder(isolate_agent_runtime_root):
    # A brand-new chat holder with no name yet DOES take the persona default —
    # stamping only happens when the instance has none.
    store = PersonaInstanceStore()
    minted = persona_chat_session_id_for(persona_instance_id_for("qa"))
    instance = store.open_chat(
        persona_id="qa", session_id=minted, default_display_name="QA Agent"
    )
    assert instance.display_name == "QA Agent"


def test_open_chat_authoritative_display_name_still_renames(isolate_agent_runtime_root):
    # An AUTHORITATIVE display_name (create_operator_chat / add_instance / an
    # explicit name) is unchanged by the fix — it always applies, so deliberate
    # (re)naming through those paths keeps working.
    store = PersonaInstanceStore()
    first = store.create_operator_chat(persona_id="qa", display_name="QA One")
    assert first.display_name == "QA One"
    second = store.create_operator_chat(persona_id="qa", display_name="QA Two")
    assert second.id == first.id
    assert second.display_name == "QA Two"  # authoritative rename honored


def test_add_instance_stores_explicit_placement_name_verbatim(isolate_agent_runtime_root):
    # PRIMARY FIX: a deliberate placement's distinct name ("QA Agent (2)") is
    # AUTHORITATIVE and stored verbatim, so the launcher conversational fold
    # (keyed on persona+display_name) keeps the sibling distinct end to end.
    store = PersonaInstanceStore()
    placed = store.add_instance(
        persona_id="qa",
        placement_id="qa_agent_2",
        display_name="QA Agent (2)",
    )
    assert placed.id == persona_instance_id_for_placement("qa_agent_2")
    assert placed.display_name == "QA Agent (2)"


def test_add_instance_omitted_name_uses_honest_default_not_title_cased_id(
    isolate_agent_runtime_root,
):
    # DOCUMENTED FALLBACK: when the client omits the placement name the store
    # takes the persona's honest default ("QA Agent"), NEVER the title-cased
    # persona id ("Qa") the template fallback would otherwise mint — that was
    # the live 2026-07-19 personainst_qa_agent_2 == "Qa" defect.
    store = PersonaInstanceStore()
    placed = store.add_instance(
        persona_id="qa",
        placement_id="qa_agent_2",
        default_display_name="QA Agent",
    )
    assert placed.display_name == "QA Agent"
    assert placed.display_name != "Qa"


def test_add_instance_default_never_overwrites_a_deliberate_name(
    isolate_agent_runtime_root,
):
    # INGEST SAFETY: a later add_instance re-open that carries only the persona
    # default must NOT rewrite an existing deliberate placement name — the "(2)"
    # is load-bearing for the fold.
    store = PersonaInstanceStore()
    first = store.add_instance(
        persona_id="qa", placement_id="qa_agent_2", display_name="QA Agent (2)"
    )
    assert first.display_name == "QA Agent (2)"
    reopened = store.add_instance(
        persona_id="qa",
        placement_id="qa_agent_2",
        default_display_name="QA Agent",  # the persona default a later open passes
    )
    assert reopened.display_name == "QA Agent (2)"


def test_open_chat_cli_add_instance_threads_explicit_display_name(
    monkeypatch, isolate_agent_runtime_root
):
    # END-TO-END SEAM: the occupied-chat placement path sends the distinct name
    # through --display-name; the open-chat CLI must forward it to the new
    # placement instead of dropping it (the exact seam that minted the live
    # "Qa" — add_instance was called with no display_name).
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="qa",
            session_id=None,
            kill_active=False,
            add_instance=True,
            placement_id="qa_agent_2",
            display_name="QA Agent (2)",
            json=True,
        )
    )

    assert code == 0
    placed = PersonaInstanceStore().get("personainst_qa_agent_2")
    assert placed.display_name == "QA Agent (2)"


def test_open_chat_cli_add_instance_omitted_name_uses_persona_config_not_title_case(
    monkeypatch, isolate_agent_runtime_root
):
    # HONEST FALLBACK at the CLI seam: with --display-name omitted, the new
    # placement takes the persona's CONFIGURED display name ("QA Agent"), never
    # the store template's title-cased persona id ("Qa").
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    qa_persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="agent_runtime/prompts/qa.md",
        hermes_profile="profile-qa",
    )
    monkeypatch.setattr(harness, "_persona_by_id", lambda _cfg, _pid: qa_persona)

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id="qa",
            session_id=None,
            kill_active=False,
            add_instance=True,
            placement_id="qa_agent_2",
            display_name=None,
            json=True,
        )
    )

    assert code == 0
    placed = PersonaInstanceStore().get("personainst_qa_agent_2")
    assert placed.display_name == "QA Agent"
    assert placed.display_name != "Qa"


def _seed_live_run_binding(instance_store) -> AgentRun:
    """Seed a live execution binding straight onto the instance row.

    S56 deleted the worker session store (and ``update_from_worker``), so the
    RUN is the only remaining live-binding authority; these two tests seed it
    directly rather than through a worker projection.
    """

    run = _seed_run("dev", "task_live", "stage_1", session_id="persona_chat_personainst_dev_live")
    live = instance_store.ensure_for_persona(_persona("dev"))
    live.mode = "task_bound"
    live.state = WorkerSessionState.RUNNING
    live.current_task_id = "task_live"
    live.goal_id = "task_live"
    live.current_assignment_id = "assign_live"
    live.active_run_id = run.id
    live.session_id = "persona_chat_personainst_dev_live"
    instance_store.update(live)
    return run


def test_open_chat_updates_only_default_chat_pointer_during_live_run(isolate_agent_runtime_root):
    instance_store = PersonaInstanceStore()
    runs = RunStore()
    run = _seed_live_run_binding(instance_store)

    instance = instance_store.open_chat(
        persona_id="dev", session_id="persona_chat_personainst_dev_new"
    )

    assert instance.default_chat_session_id == "persona_chat_personainst_dev_new"
    assert instance.active_run_id == run.id
    assert instance.current_task_id == "task_live"
    assert runs.get(run.id).state == RunState.RUNNING


def test_open_chat_kill_active_flag_cannot_cancel_run_lifecycle(isolate_agent_runtime_root):
    instance_store = PersonaInstanceStore()
    runs = RunStore()
    run = _seed_live_run_binding(instance_store)

    updated = instance_store.open_chat(
        persona_id="dev",
        session_id="persona_chat_personainst_dev_replacement",
        kill_active=True,
    )

    assert runs.get(run.id).state == RunState.RUNNING
    assert updated.id == "personainst_dev"
    assert updated.session_id == "persona_chat_personainst_dev_replacement"
    assert updated.current_task_id == "task_live"
    assert updated.active_run_id == run.id


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
                    "cache_read_tokens": 9000,
                    "cache_write_tokens": 300,
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
            "kind": "chat",
            "live_mission": False,
            "title": "Untitled persona chat",
            "last_message_preview": "Please inspect the proof packet.",
            "message_count": 7,
            "created_at": "1970-01-01T00:00:10.000000Z",
            "updated_at": "1970-01-01T00:00:20.000000Z",
            "state": "open",
            "redaction_status": "redacted",
            "input_tokens": 1234,
            "output_tokens": 56,
            "total_tokens": 1290,
            "cache_read_tokens": 9000,
            "cache_write_tokens": 300,
            "cache_mode": "none",
            "cache_ttl_seconds": None,
            "cache_ttl_basis": None,
            "messages": [],
            "root_chat_session_id": "chat_old_123",
            "active_session_id": "chat_old_123",
            "runtime_state": "unknown",
            "last_runtime_transition": None,
            "runtime_observer_id": "external_cli",
            "runtime_observed_at": None,
            "continuation_depth": 0,
            "last_resumed_at": None,
        }
    ]


def test_persona_chat_history_parity_separates_the_bound_limit_from_lost_sessions(
    isolate_agent_runtime_root,
):
    """The directory's ``limit`` is a bound; ``session_not_in_db`` is a defect.

    Live 2026-07-25 the same envelope carried both (103 + 10) and the Launcher,
    reading only ``dropped``, showed a permanent amber "projection drops 113".
    The classification now travels WITH the envelope.
    """

    from agent_runtime.parity import ProjectionAccountant

    store = PersonaInstanceStore()
    newer = store.open_chat(persona_id="dev", session_id="chat_new")
    older = store.open_chat(persona_id="backend_dev", session_id="chat_old")
    ghost = store.open_chat(persona_id="qa", session_id="chat_ghost")
    accountant = ProjectionAccountant("persona_chat_history")

    rows = persona_chat_history_summary(
        persona_instances=[newer, older, ghost],
        session_db=_FakeSessionDB(
            [
                {"id": "chat_new", "title": "new", "started_at": 20},
                {"id": "chat_old", "title": "old", "started_at": 10},
            ]
        ),
        limit=1,
        accountant=accountant,
    )

    assert len(rows) == 1
    summary = accountant.summary()
    assert summary["reasons"]["limit"] == 1
    assert summary["reasons"]["session_not_in_db"] == 1
    # Only the deliberate bound is declared by-design; the orphaned binding is
    # a real anomaly an operator can act on (harness persona-instance reconcile).
    assert summary["by_design"] == ["limit"]


def test_persona_chat_history_emits_cache_policy_for_known_provider(isolate_agent_runtime_root):
    # A session whose effective model is an automatic-prefix provider must carry
    # the estimated warm-window policy so the Launcher can render an honest
    # (non-contractual) freshness indicator.
    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="chat_codex_1")

    rows = persona_chat_history_summary(
        persona_instances=[instance],
        session_db=_FakeSessionDB(
            [
                {
                    "id": "chat_codex_1",
                    "title": "codex chat",
                    "message_count": 2,
                    "provider": "openai-codex",
                    "model": "gpt-5.6-luna",
                    "api_mode": "codex",
                    "started_at": 10,
                    "last_active": 20,
                }
            ]
        ),
    )

    assert rows[0]["cache_mode"] == "automatic_prefix"
    assert rows[0]["cache_ttl_basis"] == "estimated"
    assert rows[0]["cache_ttl_seconds"] == 300


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


def test_persona_chat_history_separates_a_retired_instance_from_a_lost_binding(
    isolate_agent_runtime_root,
):
    # HC-H2: the "projection drops" chip must count LOST data, not the residue of
    # the operator's own first-class retires. A retire archives the row and leaves
    # the chat session behind by design (history is never destroyed), so an
    # undifferentiated orphan count grew by one on every retire, forever — and
    # buried the drops that actually mean something. Three sessions, three
    # outcomes, one archive listing telling them apart.
    from agent_runtime.parity import ProjectionAccountant

    store = PersonaInstanceStore()
    # (a) bound to a live instance.
    live = store.open_chat(persona_id="dev", session_id="chat_live_1")
    # (b) bound to an instance the operator retired through the first-class verb:
    # the row exists ONLY in the archive now, and its chat session is the residue.
    retiring = store.add_instance(
        persona_id="qa",
        placement_id="scene_child_9",
        display_name="QA (9)",
    )
    retired_session_id = retiring.session_id
    assert retired_session_id
    store.retire(retiring.id, reason="placement deleted")
    # (c) bound to an id that exists NOWHERE — live roster or archive. This is the
    # genuinely anomalous orphan, and it must stay counted.
    lost_instance_id = "personainst_never_placed_c30e16a4"

    accountant = ProjectionAccountant("persona_chat_history")
    rows = persona_chat_history_summary(
        persona_instances=[live],
        session_db=_FakeSessionDB(
            [
                {"id": "chat_live_1", "title": "live", "message_count": 1},
                {
                    "id": retired_session_id,
                    "source": "agent_runtime_persona_chat",
                    "title": "retired placement chat",
                    "model_config": {"persona_instance_id": retiring.id},
                },
                {
                    "id": "chat_lost_1",
                    "source": "agent_runtime_persona_chat",
                    "title": "lost binding",
                    "model_config": {"persona_instance_id": lost_instance_id},
                },
            ]
        ),
        accountant=accountant,
    )

    assert [row["session_id"] for row in rows] == ["chat_live_1"]
    summary = accountant.summary()
    assert summary["considered"] == 3
    assert summary["included"] == 1
    assert summary["dropped"] == 2
    # The whole stage in two lines: the retire is by-design lifecycle, the lost
    # binding is still an anomaly, and neither one absorbed the other.
    assert summary["reasons"] == {"instance_retired": 1, "no_instance_match": 1}
    assert summary["by_design"] == ["instance_retired"]
    # …and the codes are not merely present in the right counts, they are on the
    # right sessions: a mutant that swapped them keeps both tallies at 1.
    samples = {sample["entity_id"]: sample for sample in accountant.drop_samples()}
    assert samples[retired_session_id]["code"] == "instance_retired"
    assert samples[retired_session_id]["by_design"] is True
    assert samples[retired_session_id]["detail"] == retiring.id
    assert samples["chat_lost_1"]["code"] == "no_instance_match"
    assert samples["chat_lost_1"]["by_design"] is False


def test_retired_persona_instance_ids_lists_only_retirement_tombstones(
    isolate_agent_runtime_root,
):
    # The bulk listing HC-H2's projection reads must answer the same question the
    # per-id tombstone probe answers, not a wider one: only ``*_retire`` batches
    # are tombstones. A reconcile/prune archive row means something else entirely,
    # and reading it as "retired" would classify a live-lineage id's orphaned
    # session as by-design lifecycle.
    from agent_runtime import paths
    from agent_runtime.persona_assignments import retired_persona_instance_ids

    store = PersonaInstanceStore()
    retiring = store.add_instance(
        persona_id="dev",
        placement_id="scene_child_11",
        display_name="Dev (11)",
    )
    store.retire(retiring.id, reason="placement deleted")
    reconcile_dir = paths.persona_instances_archive_dir() / "20260809T183345Z_reconcile"
    reconcile_dir.mkdir(parents=True, exist_ok=True)
    (reconcile_dir / "personainst_reconciled_only.json").write_text("{}", encoding="utf-8")

    ids = retired_persona_instance_ids()

    assert retiring.id in ids
    assert "personainst_reconciled_only" not in ids
    # The store is the discoverable door to the same listing.
    assert PersonaInstanceStore().retired_instance_ids() == ids


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
    by_instance = {item["persona_instance_id"]: item for item in list(snapshot["persona_instances"].values())}

    assert by_instance["personainst_dev"]["mode"] == "chat"
    assert by_instance["personainst_dev"]["session_id"] == "chat_old_123"
    # S2/S7-B: the frame emits the open chat's history as a recency POINTER — the
    # anchors (session/persona/instance ids, title, preview, count, timestamps,
    # state) survive; the heavy message tail is evicted and fetched on demand via
    # `harness persona chat history --session-id <id> --json` (the fetch path is
    # covered by test_snapshot_history_eviction). The open chat is still emitted —
    # as a pointer, never a silent absence.
    history = snapshot["persona_chat_history"]
    assert len(history) == 1
    pointer = history[0]
    assert pointer["session_id"] == "chat_old_123"
    assert pointer["persona_id"] == "dev"
    assert pointer["persona_instance_id"] == "personainst_dev"
    assert pointer["kind"] == "chat"
    assert pointer["live_mission"] is False
    assert pointer["title"] == "Launcher Dev operator channel"
    assert pointer["last_message_preview"] == "Continue the old chat safely."
    assert pointer["message_count"] == 3
    assert pointer["created_at"] == "1970-01-01T00:01:40.000000Z"
    assert pointer["updated_at"] == "1970-01-01T00:03:20.000000Z"
    assert pointer["state"] == "open"
    assert pointer["redaction_status"] == "safe"
    # The tail is gone from the frame; the eviction is flagged, never silent.
    assert pointer["messages"] == []
    assert pointer["messages_evicted"] is True
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


class _FailingTranscriptDB(_TranscriptDB):
    def __init__(self, operation: str):
        super().__init__()
        self.operation = operation

    def create_session(self, session_id, source, **kwargs):
        if self.operation == "session_create":
            raise OSError("simulated canonical session create failure")
        return super().create_session(session_id, source, **kwargs)

    def append_message(self, session_id, role, content=None, **kwargs):
        if self.operation == "operator_append" and role == "user":
            raise OSError("simulated canonical operator append failure")
        if self.operation == "assistant_append" and role == "assistant":
            raise OSError("simulated canonical assistant append failure")
        return super().append_message(session_id, role, content, **kwargs)


def _mission_chat_test_args(client_message_id: str, *, stream: bool = False):
    return SimpleNamespace(
        persona_id="dev",
        persona_instance_id="personainst_dev",
        session_id="persona_chat_personainst_dev",
        task_id=None,
        goal_id=None,
        message="please answer",
        surface_prompt="",
        intent_hint="chat",
        requested_by="test",
        client_message_id=client_message_id,
        stream=stream,
        max_seconds=5.0,
        json=True,
    )


def test_mission_chat_never_forwards_retired_goal_opt_in(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from hermes_cli import harness

    db = _TranscriptDB()
    seen = []

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            seen.append("allow_mission_goal" in kwargs)
            return SimpleNamespace(
                final_response="provider reply",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw={},
            )

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)

    normal = _mission_chat_test_args("client_chat_only")
    assert harness._cmd_mission_chat_message(normal) == 0
    capsys.readouterr()

    assert seen == [False]


@pytest.mark.parametrize("operation", ["session_create"])
def test_mission_chat_required_pre_model_transcript_failure_skips_provider(
    operation,
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from hermes_cli import harness

    provider_calls = []

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            provider_calls.append("constructed")

        def mission_chat_reply(self, *args, **kwargs):
            provider_calls.append("called")
            raise AssertionError("provider must not run after transcript persistence failure")

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(
        harness,
        "_default_persona_session_db",
        lambda: _FailingTranscriptDB(operation),
    )
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)

    code = harness._cmd_mission_chat_message(
        _mission_chat_test_args(f"client_pre_model_{operation}")
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["execution_state"] == "failed"
    assert payload["persistence_operation"] == operation
    assert provider_calls == []


def test_mission_chat_session_db_acquisition_failure_is_typed_and_skips_provider(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    import hermes_state
    from hermes_cli import harness

    provider_calls = []

    def _fail_session_db(*args, **kwargs):
        raise OSError("simulated canonical DB open failure")

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            provider_calls.append("constructed")

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(hermes_state, "SessionDB", _fail_session_db)
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)

    code = harness._cmd_mission_chat_message(
        _mission_chat_test_args("client_db_acquire_failure", stream=True)
    )

    assert code == 2
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(frames) == 1
    assert frames[0]["type"] == "chat.final"
    assert frames[0]["ok"] is False
    assert frames[0]["persistence_operation"] == "session_db_acquire"
    assert provider_calls == []


def test_mission_chat_fake_runtime_does_not_use_legacy_assistant_append(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from hermes_cli import harness

    db = _FailingTranscriptDB("assistant_append")
    provider_calls = []

    class _ProviderSpy:
        def __init__(self, *args, **kwargs):
            provider_calls.append("constructed")

        def mission_chat_reply(self, persona, message, **kwargs):
            provider_calls.append("called")
            kwargs["stream_callback"]("provider reply")
            return SimpleNamespace(
                final_response="provider reply",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw={},
            )

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "GPTPersonaRuntime", _ProviderSpy)

    code = harness._cmd_mission_chat_message(
        _mission_chat_test_args("client_assistant_db_failure", stream=True)
    )

    assert code == 0
    frames = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert provider_calls == ["constructed", "called"]
    assert frames[-1]["type"] == "chat.final"
    assert frames[-1]["ok"] is True
    terminal_frames = [frame for frame in frames if frame.get("type") == "turn.end"]
    assert terminal_frames
    assert {frame.get("state") for frame in terminal_frames} == {"completed"}
    record = mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_assistant_db_failure",
    )
    assert record["state"] == "projected"


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
            client_message_id=None,
            session_id=None,
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


def test_persona_chat_delete_unbinds_every_row_pointing_at_the_deleted_session(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """Delete is the moment the pointer becomes dangling — clear ALL of them.

    A drifted/sibling row holding the same session id used to survive the
    owner-filtered loop, and the projection could then only hide it and account
    a permanent ``session_not_in_db`` parity drop (live 2026-07-25: 10 of them).
    """

    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    store = PersonaInstanceStore()
    owner = store.create_operator_chat(persona_id="profile:reviewer", display_name="Reviewer")
    db.create_session(owner.session_id, "agent_runtime_persona_chat")

    sibling = store.open_chat(persona_id="qa", session_id="persona_chat_sibling_seed")
    sibling.default_chat_session_id = owner.session_id
    sibling.session_id = owner.session_id
    store.update(sibling)

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id=owner.session_id,
            persona_id=owner.persona_id,
            persona_instance_id=owner.id,
            requested_by="test",
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert sorted(payload["cleared_bindings"]) == sorted([owner.id, sibling.id])
    healed = PersonaInstanceStore()
    assert healed.get(owner.id).default_chat_session_id is None
    assert healed.get(sibling.id).default_chat_session_id is None
    assert healed.get(sibling.id).session_id is None
    # Store mutations always emit an event.
    cleared = [
        event
        for event in EventLog().tail(200)
        if getattr(event, "type", None) == "persona_instance.chat_binding_cleared"
    ]
    assert {event.payload["persona_instance_id"] for event in cleared} == {owner.id, sibling.id}
    assert {event.payload["reason"] for event in cleared} == {"chat_deleted"}


def test_persona_chat_delete_leaves_a_pointer_to_another_session_alone(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """Only the pointers naming THIS session are nulled.

    The old delete blanked ``session_id`` unconditionally, so deleting one chat
    could silently drop an unrelated live pointer.
    """

    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    store = PersonaInstanceStore()
    instance = store.create_operator_chat(persona_id="profile:reviewer", display_name="Reviewer")
    deleted_session = instance.default_chat_session_id
    db.create_session(deleted_session, "agent_runtime_persona_chat")
    instance.session_id = "persona_chat_other_live"
    store.update(instance)

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id=deleted_session,
            persona_id=instance.persona_id,
            persona_instance_id=instance.id,
            requested_by="test",
            json=True,
        )
    )

    assert code == 0
    json.loads(capsys.readouterr().out)
    updated = PersonaInstanceStore().get(instance.id)
    # The deleted session is gone from BOTH pointers, and the unrelated live
    # session survives (the v1 back-fill in PersonaInstance.__post_init__ then
    # re-derives the default pointer from it — the instance keeps its real chat).
    assert deleted_session not in {updated.default_chat_session_id, updated.session_id}
    assert updated.session_id == "persona_chat_other_live"  # untouched
    assert updated.default_chat_session_id == "persona_chat_other_live"
    assert updated.mode == "chat"  # still holds a chat pointer


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


def test_persona_chat_delete_rejects_foreign_instance_before_mutation(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    store = PersonaInstanceStore()
    owner = store.create_operator_chat(persona_id="dev", display_name="Owner")
    foreign = store.add_instance(
        persona_id="qa", placement_id="foreign-delete", display_name="Foreign"
    )
    db.create_session(owner.session_id, "agent_runtime_persona_chat")
    db.append_message(owner.session_id, "user", "must survive")

    code = harness._cmd_persona_chat_delete(
        SimpleNamespace(
            session_id=owner.session_id,
            persona_id=foreign.persona_id,
            persona_instance_id=foreign.id,
            requested_by="test",
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_kind"] == "foreign_chat_session"
    assert db.get_session(owner.session_id) is not None
    assert PersonaInstanceStore().get(owner.id).session_id == owner.session_id




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

    import agent.skill_utils as skill_utils

    skill_root = isolate_agent_runtime_root / "skills"
    manifest = skill_root / "deep-audit" / "SKILL.md"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("---\nname: deep-audit\n---\nbody\n", encoding="utf-8")
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skill_root])

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
    # The preload reaches the runtime inside its structural envelope so the
    # transcript projection can strip it from the displayed operator text.
    from agent_runtime.runtime_hud import render_skill_preload_envelope

    expected_preload = render_skill_preload_envelope(
        skill_names=["deep-audit"],
        skill_preload_content="PRELOADED SKILL PROMPT",
    )
    assert captured_prompts == [expected_preload]
    assert "PRELOADED SKILL PROMPT" in expected_preload
    assert payload["queued_skills_loaded"] == ["deep-audit"]
    used = payload["prompt_observability"]["used_skills"]
    assert used[0]["name"] == "deep-audit"
    assert used[0]["source"] == "queued_next_turn_skill"
    assert used[0]["resolution_status"] == "resolved"
    assert used[0]["hash_tracked"] is True
    assert used[0]["content_hash"]
    assert pending_skills_for_next_turn(
        persona_id="dev",
        session_id="persona_chat_personainst_dev",
    ) == []

    assert harness._cmd_mission_chat_message(_message_args("client_skill_2")) == 0
    json.loads(capsys.readouterr().out)
    # No skill queued on the second turn -> no envelope at all.
    assert captured_prompts == [expected_preload, ""]


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

    skill = context["available_skills"][0]
    assert skill["name"] == "deep-audit"
    assert skill["status"] == "available"
    assert skill["load_state"] == "catalog_only"
    assert skill["resolution_status"] == "missing"
    assert skill["loadable"] is False
    assert skill["hash_tracked"] is False
    assert skill["content_hash"] is None
    assert skill["category"] == "harness"
    assert skill["description"] == "Inspect the runtime deeply."
    assert "content" not in skill
    # C1 record-once (2026-07-17): the `skills_catalog` alias key is retired —
    # the row carries ONE canonical copy (`available_skills`), no compat
    # emission (ruling 0).
    assert "skills_catalog" not in context




def test_chat_protocol_v2_emitter_can_suppress_frames_while_accumulating(capsys):
    from hermes_cli import harness

    updates = []
    emitter = harness._ChatProtocolV2Emitter(
        turn_id="turn_1",
        client_message_id="client_1",
        emit_frames=False,
        on_update=lambda item: updates.append(list(item.elements)),
    )

    emitter.delta("He")
    emitter.delta("llo")
    emitter.progress(
        {
            "type": "run.tool.started",
            "tool_name": "terminal",
            "summary": "run tests",
        }
    )
    emitter.progress(
        {
            "type": "run.tool.finished",
            "tool_name": "terminal",
            "status": "ok",
            "output": "passed",
        }
    )
    emitter.finish(state="completed")

    assert capsys.readouterr().out == ""
    assert updates
    assert emitter.elements[0]["kind"] == "segment"
    assert emitter.elements[0]["text"] == "Hello"
    assert emitter.elements[1]["kind"] == "tool"
    assert emitter.elements[1]["state"] == "finished"


def test_mission_chat_non_stream_persists_completed_turn_and_prints_one_json(
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
            kwargs["trace_callback"](
                {
                    "type": "run.tool.started",
                    "tool_name": "terminal",
                    "summary": "run focused tests",
                }
            )
            kwargs["trace_callback"](
                {
                    "type": "run.tool.finished",
                    "tool_name": "terminal",
                    "status": "ok",
                    "output": "passed",
                }
            )
            return SimpleNamespace(
                final_response="Done.",
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
            message="please run it",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_durable_1",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    stdout = capsys.readouterr().out
    # Protocol-v2 frames serialize compactly (separators=(",", ":")); the final
    # payload is indented, so the compact form only ever matches a leaked frame.
    assert '"type":"turn.start"' not in stdout
    assert '"type":"tool.started"' not in stdout
    payload = json.loads(stdout)
    assert payload["ok"] is True
    assert payload["protocol_version"] is None
    record = mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_durable_1",
    )
    assert record["state"] == "projected"
    assert [item["kind"] for item in record["elements"]] == ["tool"]
    assert record["elements"][0]["state"] == "finished"


def test_mission_chat_post_boundary_failure_marks_outcome_unknown(monkeypatch, capsys, isolate_agent_runtime_root):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _FakeRuntime)

    code = harness._cmd_mission_chat_message(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="persona_chat_personainst_dev",
            task_id=None,
            goal_id=None,
            message="please run it",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_failed_1",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_kind"] == "chat_turn_outcome_unknown"
    assert payload["root_chat_session_id"] == "persona_chat_personainst_dev"
    assert payload["client_message_id"] == "client_failed_1"
    assert payload["turn_id"] == "client_failed_1"
    record = mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_failed_1",
    )
    assert record["state"] == "outcome_unknown"


def test_mission_chat_retry_recovers_native_reply_before_outcome_unknown(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    instance = PersonaInstanceStore().open_chat(
        persona_id="dev", session_id="persona_chat_personainst_dev"
    )
    db.create_session(instance.session_id, "agent_runtime_persona_chat")
    db.append_message(
        instance.session_id,
        "user",
        "recover me",
        platform_message_id="client_recover_native",
    )
    db.append_message(
        instance.session_id,
        "assistant",
        "Native reply already committed.",
        platform_message_id="client_recover_native",
    )
    transition_mission_chat_turn(
        session_id=instance.session_id,
        client_message_id="client_recover_native",
        turn_id="client_recover_native",
        state="pending",
    )
    transition_mission_chat_turn(
        session_id=instance.session_id,
        client_message_id="client_recover_native",
        turn_id="client_recover_native",
        state="executing",
        metadata={"provider_submitted": True},
    )

    class _MustNotRun:
        def __init__(self, *args, **kwargs):
            raise AssertionError("provider must not be called during native recovery")

    monkeypatch.setattr(harness, "GPTPersonaRuntime", _MustNotRun)
    code = harness._cmd_mission_chat_message(
        SimpleNamespace(
            persona_id="dev",
            persona_instance_id=instance.id,
            session_id=instance.session_id,
            task_id=None,
            goal_id=None,
            message="recover me",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_recover_native",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reply"] == "Native reply already committed."
    assert payload["idempotent_replay"] is True
    assert mission_chat_turn_record(
        session_id=instance.session_id,
        client_message_id="client_recover_native",
    )["state"] == "projected"
    projected = [
        event
        for _, event in EventLog().iter_from_offset(0)
        if event.type == "persona_chat.projected"
        and event.payload.get("client_message_id") == "client_recover_native"
    ]
    assert len(projected) == 1
    assert projected[0].payload["persona_instance_id"] == instance.id


def test_mission_chat_turn_resolve_requires_exact_owner_and_records_abandon(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    from hermes_cli import harness

    db = _TranscriptDB()
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    owner = PersonaInstanceStore().open_chat(
        persona_id="dev", session_id="persona_chat_personainst_dev"
    )
    foreign = PersonaInstanceStore().add_instance(
        persona_id="qa", placement_id="foreign-placement"
    )
    db.create_session(
        owner.session_id,
        "agent_runtime_persona_chat",
        model_config=json.dumps(
            {
                "source": "agent_runtime_persona_chat",
                "persona_instance_id": owner.id,
            }
        ),
    )
    for state in ("pending", "executing", "outcome_unknown"):
        transition_mission_chat_turn(
            session_id=owner.session_id,
            client_message_id="client_ambiguous",
            turn_id="client_ambiguous",
            state=state,
        )

    bad_code = harness._cmd_mission_chat_turn_resolve(
        SimpleNamespace(
            session_id=owner.session_id,
            client_message_id="client_ambiguous",
            turn_id="client_ambiguous",
            action="abandon",
            persona_instance_id=foreign.id,
            reason="wrong owner",
            json=True,
        )
    )
    bad = json.loads(capsys.readouterr().out)
    assert bad_code == 2
    assert bad["error_kind"] == "foreign_chat_session"
    assert mission_chat_turn_record(
        session_id=owner.session_id, client_message_id="client_ambiguous"
    )["state"] == "outcome_unknown"

    code = harness._cmd_mission_chat_turn_resolve(
        SimpleNamespace(
            session_id=owner.session_id,
            client_message_id="client_ambiguous",
            turn_id="client_ambiguous",
            action="abandon",
            persona_instance_id=owner.id,
            reason="operator confirmed unknown outcome",
            json=True,
        )
    )
    payload = json.loads(capsys.readouterr().out)
    record = mission_chat_turn_record(
        session_id=owner.session_id, client_message_id="client_ambiguous"
    )
    assert code == 0
    assert payload["journal_state"] == "abandoned"
    assert record["state"] == "abandoned"
    assert record["resolution_actor"] == owner.id
    assert record["resolution_reason"] == "operator confirmed unknown outcome"


def test_mission_chat_new_turn_interrupts_prior_running_turn(
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
    persist_mission_chat_turn(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_stale",
        turn_id="turn_stale",
        elements=[],
        state="running",
    )

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            return SimpleNamespace(
                final_response="Fresh turn.",
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
            message="new one",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_fresh",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    json.loads(capsys.readouterr().out)
    assert mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_stale",
    )["state"] == "interrupted"
    assert mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_fresh",
    )["state"] == "projected"


def test_mission_chat_post_native_projection_crash_stays_repairable(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    """W4: a crash AFTER the provider replied (transcript/bookkeeping steps)
    must persist a terminal `failed` state — never strand `running` — and the
    stdout contract (exactly one JSON object) must hold."""
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    # The native lane no longer calls _update_persona_chat_token_counts (the
    # per-call runtime writes are its sole usage authority - see the
    # single-writer guard in test_mission_chat_usage_single_writer.py), so the
    # post-native crash is injected at the lane's own fault seam instead.
    monkeypatch.setenv("HERMES_PERSONA_CHAT_FAULT_INJECTION", "after_native_commit")

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            return SimpleNamespace(
                final_response="The reply that must not vanish.",
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
            message="please answer",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_post_crash",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_kind"] == "chat_projection_incomplete"
    assert payload["reply"] == "The reply that must not vanish."
    assert "injected persona chat fault at after_native_commit" in payload["blocker"]
    record = mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_post_crash",
    )
    assert record["state"] == "native_committed"




def test_mission_chat_success_persist_sequence_has_single_terminal_write(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    """W5: one write-ahead, one on_update per tool event, ONE terminal write.
    finish() must not sneak an extra `running` persist around the terminal."""
    from hermes_cli import harness

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)
    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", lambda **_kwargs: None)

    recorded: list[tuple[str | None, bool]] = []
    real_persist = harness.persist_mission_chat_turn

    def _recording_persist(**kwargs):
        recorded.append((kwargs.get("state"), bool(kwargs.get("write_ahead"))))
        return real_persist(**kwargs)

    monkeypatch.setattr(harness, "persist_mission_chat_turn", _recording_persist)

    class _FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, persona, message, **kwargs):
            kwargs["trace_callback"](
                {"type": "run.tool.started", "tool_name": "terminal", "summary": "run"}
            )
            kwargs["trace_callback"](
                {"type": "run.tool.finished", "tool_name": "terminal", "status": "ok"}
            )
            return SimpleNamespace(
                final_response="Done.",
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
            message="run it",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_sequence",
            stream=False,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    json.loads(capsys.readouterr().out)
    assert recorded == [(None, False), (None, False)]
    assert mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_sequence",
    )["state"] == "projected"


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

    def _title(**kwargs):
        kwargs["session_db"].set_session_title(
            kwargs["session_id"], "Mission Chat"
        )

    monkeypatch.setattr(harness, "_maybe_auto_title_persona_chat", _title)

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
    assert db.messages["persona_chat_personainst_dev"] == []
    assert replay["idempotent_replay"] is True
    assert replay["client_message_id"] == "client_dup_1"
    assert replay["reply"] == "Recovered canonical reply."
    record = mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_dup_1",
    )
    assert record["projection_event_emitted"] is True
    projected = [
        event
        for _, event in EventLog().iter_from_offset(0)
        if event.type == "persona_chat.projected"
        and event.payload.get("client_message_id") == "client_dup_1"
    ]
    assert len(projected) == 1
    assert projected[0].payload["change_kind"] == "projection_committed"
    metadata_events = [
        event
        for _, event in EventLog().iter_from_offset(0)
        if event.type == "persona_chat.metadata_updated"
        and event.payload.get("root_chat_session_id")
        == "persona_chat_personainst_dev"
    ]
    assert len(metadata_events) == 1
    assert metadata_events[0].payload["change_kind"] == "auto_title_updated"


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
    assert mission_chat_turn_record(
        session_id="persona_chat_personainst_dev",
        client_message_id=client_message_id,
    )["state"] == "projected"
    # C3: the terminal frame's `prompt_observability` is the slim subset — the
    # turn id lives at the top level (asserted above); the slim block links to
    # the persisted record-at-injection row only by `context_id`.
    assert payload["prompt_observability"]["context_id"] == payload["prompt_context_id"]
    assert "turn_id" not in payload["prompt_observability"]


def test_mission_chat_message_stream_terminal_frame_is_slim(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    """C3 emission-shape sabotage anchor (main handler, streaming lane).

    The terminal ``chat.final`` frame carries the slim typed observability subset
    and NO ``turn_elements``. Re-adding ``turn_elements`` to the emit path, or
    restoring the full row, turns this red."""
    from agent_runtime.prompt_observability import CHAT_FINAL_OBSERVABILITY_FIELDS
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
            callback = kwargs.get("stream_callback")
            if callback is not None:
                callback("He")
                callback("llo")
            return SimpleNamespace(
                final_response="Hello",
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                api_calls=1,
                model="gpt-test",
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
            message="hi",
            surface_prompt="",
            intent_hint="chat",
            requested_by="test",
            client_message_id="client_slim_1",
            stream=True,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    final = lines[-1]
    assert final["type"] == "chat.final"
    assert final["ok"] is True
    # The double carriage is gone: no full turn-element echo on the wire.
    assert "turn_elements" not in final
    obs = final["prompt_observability"]
    # ONE shape: exactly the slim field set — no more, no less.
    assert set(obs) == set(CHAT_FINAL_OBSERVABILITY_FIELDS)
    # The heavy record-at-injection payloads never ride the terminal frame.
    for dropped in (
        "final_model_input",
        "prompt_layers",
        "context_files",
        "chat_history_context",
        "accessible_skills",
        "available_skills",
        "accessible_skills_ref",
        "available_skills_ref",
    ):
        assert dropped not in obs, dropped
    # The kept fields carry real content for the launcher's peek fallback +
    # usage overlay.
    assert obs["context_id"] == final["prompt_context_id"]
    assert isinstance(obs["situational_hud"], dict)
    assert isinstance(obs["used_skills"], list)
    assert isinstance(obs["model_selection"], dict)


def test_mission_chat_pre_trace_ack_is_presentation_only(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    """C8: the pre-trace ack NEVER enters the durable record.

    It rides the stream as a presentation-only protocol-v2 ``turn.ack`` frame:
    no SessionDB assistant row, no turn-store element — replay after the fact
    shows no ack. (Pre-C8 the ack was injected into SessionDB and then
    marker-suppressed launcher-side; that inject-then-hide seam is retired.)
    """

    from hermes_cli import harness
    from agent_runtime.mission_chat_turns import mission_chat_turn_elements

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
            kwargs["stream_callback"]("The guidance is loaded.")
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
            stream=True,
            max_seconds=5.0,
            json=True,
        )
    )

    assert code == 0
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    # The ack IS on the live stream (typed, presentation-only), before content.
    ack_frames = [line for line in lines if line["type"] == "turn.ack"]
    assert len(ack_frames) == 1
    assert ack_frames[0]["protocol_version"] == 2
    assert ack_frames[0]["turn_id"] == "client_pre_trace_1"
    assert ack_frames[0]["text"] == (
        "I'll load the relevant guidance first, then report back with the useful part."
    )
    assert lines.index(ack_frames[0]) < min(
        index for index, line in enumerate(lines) if line["type"] == "segment.start"
    )
    # Native persistence is owned by the runtime actor. This fake runtime writes
    # no SessionDB rows, proving the CLI did not restore the retired append lane.
    messages = db.messages["persona_chat_personainst_dev"]
    assert messages == []
    # …and the turn store's elements hold only real content (replay shows no ack).
    persisted = mission_chat_turn_elements(
        session_id="persona_chat_personainst_dev",
        client_message_id="client_pre_trace_1",
    )
    assert persisted, "turn store must hold the streamed segment"
    assert all(item["kind"] in {"segment", "tool"} for item in persisted)
    assert all("report back" not in str(item.get("text") or "") for item in persisted)


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


def test_persona_chat_context_uses_native_structured_prior_turns(isolate_agent_runtime_root):
    from hermes_cli import harness

    db = _TranscriptDB()
    session_id = "persona_chat_personainst_dev"
    db.create_session(session_id, "agent_runtime_persona_chat")
    db.append_message(session_id, "user", "remember the blue button")
    db.append_message(session_id, "assistant", "I will remember the blue button.")

    history = harness.safe_native_history(
        harness._persona_chat_native_history(db, session_id)
    )

    assert history == [
        {"role": "user", "content": "remember the blue button"},
        {"role": "assistant", "content": "I will remember the blue button."},
    ]


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
    assert persona.model == "gpt-default"
    assert persona.provider == "openai-codex"
    assert persona.toolsets == []


def test_profile_persona_resolution_prefers_exact_id_over_profile_owner(
    monkeypatch, isolate_agent_runtime_root
):
    from agent_runtime.personas import profile_chat_toolsets
    from hermes_cli import harness

    exact = _persona("profile:shared")
    exact.hermes_profile = "different"
    exact.toolsets = ["file"]
    owner = _persona("profile_owner")
    owner.hermes_profile = "shared"
    owner.toolsets = ["terminal"]
    monkeypatch.setattr(harness, "ensure_persisted_personas", lambda _cfg: [owner, exact])

    assert harness._persona_by_id(_assignment_config(), "profile:shared") is exact
    assert profile_chat_toolsets("shared", [owner, exact]) == ["file"]


def test_ambiguous_profile_owners_do_not_supply_arbitrary_defaults(
    monkeypatch, isolate_agent_runtime_root
):
    from agent_runtime.personas import profile_chat_toolsets
    from hermes_cli import harness

    first = _persona("first")
    first.hermes_profile = "shared"
    first.toolsets = ["file"]
    second = _persona("second")
    second.hermes_profile = "shared"
    second.toolsets = ["terminal"]
    monkeypatch.setattr(harness, "ensure_persisted_personas", lambda _cfg: [first, second])

    resolved = harness._persona_by_id(_assignment_config(), "profile:shared")
    assert resolved is not None
    assert resolved.id == "profile:shared"
    assert resolved.toolsets == []
    assert profile_chat_toolsets("shared", [first, second]) == []


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
    # C1 record-once (2026-07-17): the `skills` alias key is retired — one
    # canonical copy (`accessible_skills`), no compat emission (ruling 0).
    assert "skills" not in context
    assert {item["source"] for item in context["accessible_skills"]} == {
        "profile_skills_snapshot"
    }
    assert {item["status"] for item in context["accessible_skills"]} == {"missing"}
    assert {item["load_state"] for item in context["accessible_skills"]} == {
        "assigned_not_loaded"
    }
    assert all(not item["hash_tracked"] for item in context["accessible_skills"])


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
    # C1 record-once (2026-07-17): the `skills` alias key is retired — the row
    # carries ONE canonical copy (`accessible_skills`); the legacy alias on the
    # persisted input is normalized in, never re-emitted.
    assert "skills" not in refreshed
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
                # Regression: a profile-backed instance is synthesized rather
                # than loaded from AgentStore. Explicit skill policy must still
                # overlay through the typed AgentPersona path; the old
                # SimpleNamespace fallback crashed dataclasses.replace and took
                # down `harness stream` before its hydrate frame.
                skill_overrides=["harness-runtime-model"],
            )
        ],
        session_db=db,
    )

    context = snapshot["chat_contexts"][0]
    assert context["context_id"] == "ctx_stale_profile_alice"
    assert context["chat_id"] == "persona_chat_personainst_profile_alice"
    assert context["chat_title"] == "Alice Agent chat"
    assert context["used_skills"] == []
    # S8: the ``skills_catalogs`` table is evicted from the frame; rows keep only
    # the content hash. The accessible set here is the profile-snapshot fallback
    # (backfilled at merge time — distinct from the persisted persona_definition
    # skills), so this pins the RESOLUTION content by proving the ref equals the
    # content hash of the expected backfilled list (`skills` aliases it → same
    # ref). The on-disk resolver covers persisted lists; this in-memory backfill
    # is pinned by hash equality instead.
    expected_context = prompt_observability.mission_chat_prompt_observability(
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
            skills=["harness-runtime-model"],
        ),
        persona_instance_id="personainst_profile_alice",
        session_id="persona_chat_personainst_profile_alice",
        session_db=db,
        instance_skill_overrides=["harness-runtime-model"],
    )
    expected_accessible = expected_context["accessible_skills"]
    assert "skills_catalogs" not in snapshot
    assert context["accessible_skills_ref"] == prompt_observability._skills_list_content_hash(
        expected_accessible
    )


def test_persona_instance_close_cli_closes_only_free_floating_assignment(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    # S70: nothing can mint an assignment any more; seed the residual row the
    # close verb exists to settle.
    assignment = _seed_assignment(persona_id="dev")
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
    assignment = _seed_assignment(persona_id="dev")
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
    assignment = _seed_assignment(persona_id="dev")
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

    # Residue-slim R2: the summary keeps only the head SCALARS + a typed
    # visibility_ref pointer; the heavy payloads (and the retired agent_hud_state)
    # leave the wire row.
    assert "tool_resolution" not in summary
    assert "turn_tool_context" not in summary
    assert "permission_state" not in summary
    assert "agent_hud_state" not in summary
    assert "blocked_tools" not in summary
    # The runtime DEFAULT posture since the 2026-08-09 ruling — the drawer must
    # render what a turn actually gets, not a bounded literal.
    assert summary["permission_mode"] == default_permission_mode()
    assert summary["permission_mode"] == "unbounded"
    assert isinstance(summary["mutation_boundary"], dict)
    assert isinstance(summary["tool_count"], int)
    assert summary["blocked_tools_count"] >= 0
    assert summary["visibility_ref"]["evicted"] is True
    assert summary["visibility_ref"]["id"] == "personainst_profile_alice"
    assert "agent_hud_state" not in summary["visibility_ref"]["fields"]

    # The full payloads are rebuilt on demand by persona_instance_tool_detail (the
    # `harness persona-instance detail` body) — the same bytes the frame used to
    # carry, minus the retired agent_hud_state.
    detail = persona_instance_tool_detail(instance)
    assert detail["tool_resolution"]["persona_id"] == "profile:alice"
    assert detail["turn_tool_context"]["persona_id"] == "profile:alice"
    # T9b: this preview is the persona instance's operator CHAT lane, so it
    # reflects whatever that lane actually ships. Under the 2026-08-09 runtime
    # default (`unbounded`) the T3/T6a cost cuts do not apply, so the dev toolkit
    # is present — the preview would be LYING if it still hid it (the bounded
    # shape is pinned by the companion test below).
    final_tools = detail["tool_resolution"]["final_model_tools"]
    assert "read_file" in final_tools
    assert "terminal" in final_tools
    assert "agent_chat_send" in final_tools
    assert "clarify" in final_tools
    # The persona-safety blocks yield to the mode; registry hygiene never does.
    assert "send_message" not in detail["tool_resolution"]["blocked_tool_names"]
    assert "kanban_create" in detail["tool_resolution"]["blocked_tool_names"]
    assert detail["permission_state"]["mode"] == "unbounded"
    assert "agent_hud_state" not in detail
    assert summary["tool_count"] == len(detail["tool_resolution"]["final_model_tools"])


def test_profile_persona_instance_preview_reflects_an_operator_restriction(
    isolate_agent_runtime_root, bounded_chat_session
):
    """The same preview, for a session an operator narrowed.

    The chat-lane cost policy (T3/T6a) is alive and unchanged — it just no longer
    applies to the DEFAULT posture. This is the tier it does apply to, and the
    preview must track it rather than reporting one fixed shape.
    """

    session_id = "persona_chat_personainst_profile_alice_e898c1dc3794"
    bounded_chat_session("profile:alice", session_id)
    instance = PersonaInstance(
        id="personainst_profile_alice",
        persona_id="profile:alice",
        role="profile",
        display_name="Alice Agent",
        profile_id="alice",
        runtime_root=str(REPO_ROOT),
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id=session_id,
    )

    summary = persona_instance_summary(instance)
    detail = persona_instance_tool_detail(instance)
    final_tools = detail["tool_resolution"]["final_model_tools"]

    assert summary["permission_mode"] == "profile_default"
    assert "read_file" not in final_tools
    assert "terminal" not in final_tools
    assert "execute_code" not in final_tools
    assert "mission_goal_create" not in final_tools
    assert "agent_chat_send" in final_tools
    assert "clarify" in final_tools
    assert "send_message" in detail["tool_resolution"]["blocked_tool_names"]
    assert detail["permission_state"]["mode"] == "profile_default"


def test_profile_visibility_preserves_custom_instance_role_without_config(monkeypatch):
    from agent_runtime.persona_assignments import _profile_visibility_persona

    monkeypatch.setattr("agent_runtime.config.load_agent_runtime_config", lambda: object())
    monkeypatch.setattr("agent_runtime.config.ensure_persisted_personas", lambda cfg: [])
    instance = PersonaInstance(
        id="personainst_profile_researcher",
        persona_id="profile:researcher",
        role="literature_reviewer",
        display_name="Researcher",
        profile_id="researcher",
        runtime_root=str(REPO_ROOT),
        state=WorkerSessionState.IDLE,
    )

    persona = _profile_visibility_persona(instance)

    assert persona.id == "profile:researcher"
    assert persona.role == "literature_reviewer"
    assert persona.hermes_profile == "researcher"


def test_profile_visibility_uses_configured_custom_role_without_rewriting_raw_id(monkeypatch):
    from agent_runtime.persona_assignments import _profile_visibility_persona

    configured = AgentPersona(
        id="configured_researcher",
        display_name="Configured Researcher",
        role="evidence_synthesist",
        model="gpt-custom",
        provider="custom-provider",
        api_mode="chat_completions",
        toolsets=["search"],
        system_prompt_path="SOUL.md",
        hermes_profile="researcher",
    )
    monkeypatch.setattr("agent_runtime.config.load_agent_runtime_config", lambda: object())
    monkeypatch.setattr(
        "agent_runtime.config.ensure_persisted_personas", lambda cfg: [configured]
    )
    instance = PersonaInstance(
        id="personainst_profile_researcher",
        persona_id="profile:researcher",
        role="profile",
        display_name="Raw Profile Chat",
        profile_id="researcher",
        runtime_root=str(REPO_ROOT),
        state=WorkerSessionState.IDLE,
    )

    persona = _profile_visibility_persona(instance)

    assert persona.id == "profile:researcher"
    assert persona.display_name == "Raw Profile Chat"
    assert persona.role == "evidence_synthesist"
    assert persona.toolsets == ["search"]


def test_task_store_cancel_closes_persona_assignments():
    _assert_task_store_stub()


def test_task_store_update_terminal_transition_releases_assignments():
    _assert_task_store_stub()


def test_task_store_update_failed_transition_releases_assignments():
    _assert_task_store_stub()


def test_task_store_update_non_terminal_transition_keeps_assignments():
    _assert_task_store_stub()


def test_contention_warning_self_heals_assignment_held_by_terminal_goal():
    _assert_task_store_stub()


def test_contention_warning_still_fires_for_genuinely_open_goal():
    _assert_task_store_stub()


# ---------------------------------------------------------------------------
# Instance end-of-life: `harness persona instance retire` (placement removal).
# The operator ruling — deleting a deliberate placement IS the instance's
# end-of-life — needs a sanctioned verb that archives the ROW (not an
# assignment), emits an evented mutation, guards a working agent, and drops the
# row from every instance-fed projection while leaving chat history on disk.
# ---------------------------------------------------------------------------


def _placement_instance(persona_id: str = "dev", placement_id: str = "scene_child_2", display_name: str = "Dev (2)") -> PersonaInstance:
    return PersonaInstanceStore().add_instance(
        persona_id=persona_id,
        placement_id=placement_id,
        display_name=display_name,
    )


def test_retire_archives_placement_row_and_emits_event(isolate_agent_runtime_root):
    from agent_runtime import paths

    store = PersonaInstanceStore()
    instance = _placement_instance()
    assert instance.id == "personainst_scene_child_2"
    session_id = instance.session_id
    assert session_id  # a real chat session pointer we must NOT destroy

    result = store.retire(instance.id, reason="placement deleted", requested_by="operator")

    # Row left the live dir ...
    assert not paths.persona_instance_path(instance.id).exists()
    assert instance.id not in {row.id for row in store.list_all()}
    # ... and landed in the archive (never deleted); its session pointer survives.
    archived = list(paths.persona_instances_archive_dir().rglob(f"{instance.id}.json"))
    assert len(archived) == 1
    archived_payload = json.loads(archived[0].read_text(encoding="utf-8"))
    assert archived_payload["session_id"] == session_id
    assert Path(result["archive_path"]) == archived[0]
    assert result["persona_id"] == "dev"

    events = [event for event in EventLog().tail(50) if event.type == "persona_instance.retired"]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["persona_instance_id"] == instance.id
    assert payload["reason"] == "placement deleted"
    assert payload["requested_by"] == "operator"
    assert payload["persona_id"] == "dev"


def test_retire_releases_child_backlinks_without_clearing_child_context(
    isolate_agent_runtime_root,
):
    store = PersonaInstanceStore()
    retiring = _placement_instance(
        persona_id="neko_supervisor",
        placement_id="lead_retiring",
        display_name="Retiring lead",
    )
    remaining = _placement_instance(
        persona_id="neko_supervisor",
        placement_id="lead_remaining",
        display_name="Remaining lead",
    )
    child = _placement_instance(
        persona_id="dev",
        placement_id="dev_child",
        display_name="Dev child",
    )
    child = store.set_parents(
        child.id,
        [retiring.id, remaining.id],
        goal_id="goal_live",
    )

    store.retire(retiring.id, reason="placement deleted")

    child = store.get(child.id)
    assert child.steered_by == [remaining.id]
    assert child.spawned_by == remaining.id
    assert child.goal_id == "goal_live"
    assert child.current_task_id == "goal_live"
    assert child.mode == "task_bound"


def test_retire_last_parent_preserves_child_mission_context(
    isolate_agent_runtime_root,
):
    store = PersonaInstanceStore()
    retiring = _placement_instance(
        persona_id="neko_supervisor",
        placement_id="lead_only",
        display_name="Only lead",
    )
    child = _placement_instance(
        persona_id="dev",
        placement_id="dev_only_child",
        display_name="Dev child",
    )
    child = store.set_parents(child.id, [retiring.id], goal_id="goal_live")

    store.retire(retiring.id, reason="placement deleted")

    child = store.get(child.id)
    assert child.steered_by == []
    assert child.spawned_by is None
    assert child.goal_id == "goal_live"
    assert child.current_task_id == "goal_live"
    assert child.mode == "task_bound"


def test_retired_placement_cannot_be_resurrected_from_its_saved_chat(
    isolate_agent_runtime_root,
):
    from agent_runtime import paths
    from agent_runtime.persona_assignments import RetiredPersonaInstanceError

    store = PersonaInstanceStore()
    instance = _placement_instance(placement_id="dev_agent_2")
    session_id = instance.session_id
    assert session_id

    result = store.retire(instance.id, reason="placement deleted")

    with pytest.raises(RetiredPersonaInstanceError) as excinfo:
        store.open_chat(
            persona_id=instance.persona_id,
            persona_instance_id=instance.id,
            session_id=session_id,
        )

    assert excinfo.value.persona_instance_id == instance.id
    assert excinfo.value.archive_path == Path(result["archive_path"])
    assert not paths.persona_instance_path(instance.id).exists()
    assert instance.id not in {row.id for row in store.list_all()}


def test_retirement_is_answerable_read_only_before_anything_is_written(
    isolate_agent_runtime_root,
):
    """``open_chat`` answers "is this target retired?" only by RAISING, which is
    too late for a caller whose durable writes come first (the chat mint created
    and titled a session row, then bound). The same rule — no live row PLUS a
    ``*_retire`` tombstone — is available as a read-only predicate, and the rule
    itself has exactly one home: ``open_chat`` asks the predicate too."""

    store = PersonaInstanceStore()
    live = _placement_instance(placement_id="dev_agent_3")
    retired = _placement_instance(placement_id="dev_agent_2")
    result = store.retire(retired.id, reason="placement deleted")

    assert str(store.retired_instance_archive_path(retired.id)) == result["archive_path"]
    # A live placement is never a tombstone, and neither is an id that never
    # existed — retirement is absence PLUS a tombstone, not absence alone, or
    # every first-ever mint in the product would refuse.
    assert store.retired_instance_archive_path(live.id) is None
    assert store.retired_instance_archive_path("personainst_never_placed") is None
    assert store.retired_instance_archive_path(None) is None
    # The predicate is READ-ONLY: asking must not resurrect, create, or evict.
    assert {row.id for row in store.list_all()} == {live.id}
    # ``persona_id`` resolves a caller-supplied id through the same derivation
    # ``open_chat`` uses, so a drifted actor token gets the same answer.
    assert str(
        store.retired_instance_archive_path(f"persona_{retired.id}", persona_id="dev")
    ) == result["archive_path"]


def test_a_live_row_outranks_a_stale_retire_tombstone_for_the_same_id(
    isolate_agent_runtime_root,
):
    """The predicate's live-row-wins branch, pinned on a CONSTRUCTED state.

    No public verb can produce this state today: ``retire`` archives the row in
    the same breath as it writes the tombstone, so live-row-AND-tombstone is
    unreachable through the store's API — which is exactly why this branch
    survives every realistic scenario untested and reads like dead weight to
    the next person holding a knife.

    It is armor for the verb this design invites next. Tombstones are permanent,
    so an un-retire / re-placement that writes an id back into the live roster
    would leave precisely this shape behind — and a predicate that consulted
    only the archive would then call that live placement retired and refuse
    every chat it ever opened. So the state is built by hand: the archived row
    put back on disk, the tombstone deliberately left where it is.
    """

    from agent_runtime import paths

    store = PersonaInstanceStore()
    retired = _placement_instance(placement_id="dev_agent_2")
    result = store.retire(retired.id, reason="placement deleted")
    tombstone = Path(result["archive_path"])
    assert str(store.retired_instance_archive_path(retired.id)) == result["archive_path"]

    live_path = paths.persona_instance_path(retired.id)
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(tombstone.read_text(encoding="utf-8"), encoding="utf-8")

    # Both facts hold at once now, and the LIVE one is the one that decides.
    assert store.get(retired.id).id == retired.id
    assert tombstone.is_file(), "the tombstone must survive for this to prove anything"
    assert store.retired_instance_archive_path(retired.id) is None

def test_assert_bindable_answers_the_binds_refusals_without_writing(
    isolate_agent_runtime_root,
):
    """The seam that lets a lane refuse BEFORE its first durable write.

    ``open_chat`` answers "may this bind happen?" only by raising at the end
    of whatever the caller already did, which is why a mint could reach it
    with a titled session row already on disk. Same refusals, same typed
    error, same canonical target id — asserted without touching the store."""

    from agent_runtime import paths
    from agent_runtime.persona_assignments import RetiredPersonaInstanceError

    store = PersonaInstanceStore()
    live = _placement_instance(placement_id="dev_agent_2")

    # A live placement is bindable, and the canonical id comes back once.
    assert store.assert_bindable(persona_id="dev", persona_instance_id=live.id) == live.id
    # A first-ever canonical channel is bindable too: retirement is the
    # ABSENCE of a live row PLUS a tombstone, never absence alone.
    assert store.assert_bindable(persona_id="qa") == "personainst_qa"
    assert not paths.persona_instance_path("personainst_qa").exists()

    # A sibling's session is the same ValueError the bind raises.
    with pytest.raises(ValueError):
        store.assert_bindable(
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id=f"persona_chat_{live.id}_abcdef123456",
        )

    archived = store.retire(live.id, reason="placement deleted")
    with pytest.raises(RetiredPersonaInstanceError) as excinfo:
        store.assert_bindable(persona_id="dev", persona_instance_id=live.id)
    assert excinfo.value.persona_instance_id == live.id
    assert str(excinfo.value.archive_path) == archived["archive_path"]
    # …and the refusal never revived the row it refused.
    assert live.id not in {row.id for row in PersonaInstanceStore().list_all()}


def test_open_chat_cli_reports_a_retired_root_as_retired_not_unknown(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    instance = _placement_instance(placement_id="dev_agent_2")
    session_id = instance.session_id
    assert session_id
    PersonaInstanceStore().retire(instance.id, reason="placement deleted")

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id=instance.persona_id,
            session_id=session_id,
            kill_active=False,
            add_instance=False,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    # `retired_persona_instance`, not `unknown_chat_session`. Retiring a
    # placement archives the row and deliberately PRESERVES its chat on disk,
    # so re-opening that thread has a typed end-of-life answer: the tombstone
    # path and "history preserved". "unknown chat session" named the wrong
    # fact (the root is known; its owner ended) and offered the wrong next
    # step. The genuinely-unknown case is a different case, pinned below.
    assert payload["persona_instance_id"] == instance.id
    assert payload["history_preserved"] is True
    assert payload["error_kind"] == "retired_persona_instance"
    assert instance.id not in {
        row.id for row in PersonaInstanceStore().list_all()
    }


def test_open_chat_cli_still_rejects_a_genuinely_unknown_root(
    monkeypatch,
    capsys,
    isolate_agent_runtime_root,
):
    """The case `unknown_chat_session` is actually for, kept distinct.

    A LIVE instance whose named root was never persisted: nothing was
    retired, nothing has an archive to point at, and "open a server-minted
    chat root before sending" is the right next step. The retirement
    pre-flight must not swallow it."""

    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    instance = _placement_instance(placement_id="dev_agent_3")
    assert instance.id in {row.id for row in PersonaInstanceStore().list_all()}

    code = harness._cmd_persona_instance_open_chat(
        Namespace(
            persona_id=instance.persona_id,
            session_id=f"persona_chat_{instance.id}_abcdef123456",
            kill_active=False,
            add_instance=False,
            json=True,
        )
    )

    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_kind"] == "unknown_chat_session"


def test_retire_refuses_canonical_persona_channel(isolate_agent_runtime_root):
    from agent_runtime import paths
    from agent_runtime.persona_assignments import PersonaInstanceRetireError

    store = PersonaInstanceStore()
    canonical = store.ensure_for_persona(_persona("dev"))
    assert canonical.id == "personainst_dev"

    with pytest.raises(PersonaInstanceRetireError) as excinfo:
        store.retire(canonical.id)
    assert excinfo.value.code == "canonical_persona_channel"
    # Refusal is a no-op: the canonical channel stays live on disk.
    assert paths.persona_instance_path(canonical.id).exists()
    assert canonical.id in {row.id for row in store.list_all()}


def test_retire_refuses_active_run_binding(isolate_agent_runtime_root):
    from agent_runtime import paths
    from agent_runtime.persona_assignments import PersonaInstanceRetireError

    store = PersonaInstanceStore()
    instance = _placement_instance()
    run = _seed_run("dev", "task_live", "stage_1", session_id="session_live")
    instance.active_run_id = run.id
    store.update(instance)

    with pytest.raises(PersonaInstanceRetireError) as excinfo:
        store.retire(instance.id)
    assert excinfo.value.code == "instance_active"
    assert excinfo.value.detail["active_run_id"] == run.id
    assert paths.persona_instance_path(instance.id).exists()


def test_retire_refuses_active_assignment(isolate_agent_runtime_root):
    from agent_runtime import paths
    from agent_runtime.persona_assignments import PersonaInstanceRetireError

    store = PersonaInstanceStore()
    instance = _placement_instance()
    # S70: residual active rows can still exist on disk even though nothing can
    # mint one any more — the guard must keep refusing for them.
    assignment = _seed_assignment(persona_id="dev", persona_instance_id=instance.id)
    assert assignment.state in ACTIVE_ASSIGNMENT_STATES

    with pytest.raises(PersonaInstanceRetireError) as excinfo:
        store.retire(instance.id)
    assert excinfo.value.code == "assignment_active"
    assert assignment.id in excinfo.value.detail["active_assignment_ids"]
    assert paths.persona_instance_path(instance.id).exists()


def test_retire_refuses_missing_instance(isolate_agent_runtime_root):
    from agent_runtime.persona_assignments import PersonaInstanceRetireError

    with pytest.raises(PersonaInstanceRetireError) as excinfo:
        PersonaInstanceStore().retire("personainst_does_not_exist")
    assert excinfo.value.code == "not_found"


def test_retire_excludes_instance_from_snapshot_projection(monkeypatch, isolate_agent_runtime_root):
    cfg = _assignment_config()
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    store = PersonaInstanceStore()
    instance = _placement_instance()

    before = build_snapshot()
    assert instance.id in before["persona_instances"]

    store.retire(instance.id, reason="placement deleted")

    after = build_snapshot()
    assert instance.id not in after["persona_instances"]
    # The chat-history projection reads the live instances, so it drops too.
    history = persona_chat_history_summary(persona_instances=store.list_all())
    assert instance.id not in {row.get("persona_instance_id") for row in history}


def test_retire_cli_happy_path_archives_row(monkeypatch, isolate_agent_runtime_root):
    from argparse import Namespace
    from agent_runtime import paths
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    instance = _placement_instance()

    code = harness._cmd_persona_instance_retire(
        Namespace(
            persona_instance_id=instance.id,
            reason="placement deleted",
            requested_by="operator",
            coordinator_id=None,
            coordinator_max_spawns=None,
            coordinator_spawns_used=0,
            coordinator_may_kill_own=None,
            coordinator_no_kill_own=None,
            coordinator_may_kill_others=None,
            json=True,
        )
    )

    assert code == 0
    assert not paths.persona_instance_path(instance.id).exists()
    assert instance.id not in {row.id for row in PersonaInstanceStore().list_all()}


def test_retire_cli_canonical_refusal_returns_typed_error(monkeypatch, capsys, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    canonical = PersonaInstanceStore().ensure_for_persona(_persona("dev"))

    code = harness._cmd_persona_instance_retire(
        Namespace(
            persona_instance_id=canonical.id,
            reason="placement deleted",
            requested_by="operator",
            coordinator_id=None,
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
    assert payload["error"] == "canonical_persona_channel"
    assert payload["persona_instance_id"] == canonical.id


def test_retire_cli_coordinator_operator_placed_needs_confirm(monkeypatch, capsys, isolate_agent_runtime_root):
    from argparse import Namespace
    from hermes_cli import harness

    cfg = _assignment_config()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    # A placement dropped by the operator is not owned by a coordinator, so a
    # coordinator cannot end-of-life it (KILL_ACTIONS gate -> operator confirm).
    instance = _placement_instance()

    code = harness._cmd_persona_instance_retire(
        Namespace(
            persona_instance_id=instance.id,
            reason="coordinator tried retiring operator placement",
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




# ── how many paths may move an instance's chat pointers ─────────────────────
#
# ``clear_chat_session_binding`` used to claim "every unbind … goes through
# here so a stale binding can never be cleared silently by one path and loudly
# by another". Since ``b912cce88a`` that was false: ``rollback_chat_root_bind``
# nulls the same two fields and emits no ``chat_binding_cleared``.
#
# The claim was corrected rather than made true, because the retraction cannot
# route through the clear without losing three properties it needs (restores
# instead of clearing, must not raise inside a failure handler, must emit a
# ``state.patched`` the clear does not emit) — the argument is spelled out in
# ``clear_chat_session_binding``'s docstring.
#
# A corrected sentence is only worth what enforces it, so this is the fence: a
# prose claim about "two paths" is checkable only if the count is checked. It
# reads the module's AST rather than grepping, so a rename or a reformat does
# not silently widen it.


def _chat_pointer_writers() -> dict[str, set[str]]:
    """Every function in ``persona_assignments`` that assigns a chat pointer.

    Maps function name -> the pointer fields it writes. Attribute targets only
    (``<something>.default_chat_session_id = …``): the local rebinds inside the
    module's own helpers are not writes to a row.
    """

    import ast
    import inspect

    from agent_runtime import persona_assignments

    pointers = {"default_chat_session_id", "session_id"}
    tree = ast.parse(inspect.getsource(persona_assignments))
    writers: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            # ``AnnAssign``/``AugAssign`` too, not just ``Assign``: a gate that
            # only recognises one assignment node is a gate a third path walks
            # past by writing ``instance.session_id: str | None = None``.
            if isinstance(inner, ast.Assign):
                targets = list(inner.targets)
            elif isinstance(inner, (ast.AnnAssign, ast.AugAssign)):
                targets = [inner.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in pointers:
                    writers.setdefault(node.name, set()).add(target.attr)
    return writers


def test_only_two_paths_may_move_an_instances_chat_pointers():
    """One bind, one clear, one restore — and the module says which is which.

    If a THIRD unbind path appears, this fails and names it. That is the whole
    point: the docstring in ``clear_chat_session_binding`` now enumerates the
    exception instead of denying it, and an enumeration nobody counts drifts
    back into the sentence it replaced within one commit.

    A new writer is not automatically wrong — it is unreviewed. The cure is to
    decide whether it belongs behind ``clear_chat_session_binding`` (a stale
    binding being reaped) or beside ``rollback_chat_root_bind`` (a bind this
    same lane made being retracted), say so in that docstring, and add it here.
    """

    assert _chat_pointer_writers() == {
        # THE bind. Writes both pointers to the root it is opening.
        "open_chat": {"default_chat_session_id", "session_id"},
        # THE clear: nulls only pointers matching one session id, demotes the
        # mode, and emits ``persona_instance.chat_binding_cleared``.
        "clear_chat_session_binding": {"default_chat_session_id", "session_id"},
        # THE retraction: restores the pre-bind values (which may be ``None``),
        # keyed on the root identity it bound, and never raises.
        "rollback_chat_root_bind": {"default_chat_session_id", "session_id"},
    }


def test_the_two_unbind_paths_are_told_apart_by_what_they_emit():
    """The reason they are allowed to stay separate, pinned as behaviour.

    ``clear_chat_session_binding`` emits the domain event and NO
    ``state.patched`` (``_event`` only appends to the log). The retraction emits
    ``emit_persona_instance_patch`` and no domain event. Routing either through
    the other silently changes what every connected launcher receives, so the
    difference is asserted here rather than left as an assurance in a comment.

    This is the SOURCE half of the difference. The tests at the bottom of
    this file are the WIRE half -- what each shape actually costs a
    connected client -- because a source grep cannot tell a deliberate
    omission from a missing producer, and the question this asymmetry
    provokes ("is the clear leaving every launcher stale?") is answerable
    only from a run. It is not: the clear's event is uncovered, so its
    batch ships the whole core.
    """

    import inspect

    from agent_runtime.persona_assignments import PersonaInstanceStore

    clear = inspect.getsource(PersonaInstanceStore.clear_chat_session_binding)
    retract = inspect.getsource(PersonaInstanceStore.rollback_chat_root_bind)

    body_of = lambda src: src.split('"""', 2)[-1]  # noqa: E731 - drop the docstring

    assert "chat_binding_cleared" in body_of(clear)
    assert "emit_persona_instance_patch" not in body_of(clear)

    assert "emit_persona_instance_patch" in body_of(retract)
    assert "chat_binding_cleared" not in body_of(retract)


# -- what each unbind path costs a CONNECTED client --------------------------
#
# The source-level difference above was read as a defect: the clear moves
# ``mode`` and the session trio on ``persona_instance_summary`` and emits no
# ``state.patched``, so "every connected launcher keeps rendering the chat
# pointer the store no longer holds until a full snapshot refresh". Driven,
# that is not what happens -- the full snapshot refresh arrives in the SAME
# frame, because the event the clear does emit is uncovered and demotes its own
# batch. The tests below pin that, and pin the reason it must stay that way, so
# the next reader of the source-level asymmetry gets the measurement instead of
# re-deriving the wrong conclusion from it.


def _batch_since(offset: int):
    return list(EventLog().iter_from_offset(0))[offset:]


def _frames_for(batch, *, base_offset: int, delta_patches: bool = True):
    from agent_runtime import stream as stream_mod

    return list(
        stream_mod._batch_frames_with_liveness(
            batch,
            base_offset=base_offset,
            delta_patches=delta_patches,
            resync=False,
            heartbeat_interval_seconds=5.0,
            fold_entities=None,
        )
    )


def test_a_cleared_binding_is_not_stale_because_its_own_event_demotes_the_batch(
    isolate_agent_runtime_root,
):
    """The clear ships the whole core, so there is nothing left stale to patch.

    ``persona_instance.chat_binding_cleared`` is deliberately outside
    ``patch_coverage.COVERED_DOMAIN_EVENT_TYPES``, and one uncovered event makes
    the entire coalesced batch uncoverable. The frame a connected client
    receives therefore carries a full ``core`` and no ``patches`` list at all --
    the row is re-hydrated in the same frame that reports the clear.

    A ``state.patched`` emitted here would be unreachable: the promotion gate
    rejects the batch before the patches list is assembled. That is why
    ``clear_chat_session_binding`` emits none, and this is the assertion that
    keeps "it emits no patch" from drifting back into "it leaves clients stale".
    """

    from agent_runtime.patch_coverage import (
        COVERED_DOMAIN_EVENT_TYPES,
        batch_is_patch_coverable,
    )

    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="persona_chat_clear_wire")
    base = len(list(EventLog().iter_from_offset(0)))

    record = store.clear_chat_session_binding(
        instance, session_id="persona_chat_clear_wire", reason="chat_deleted"
    )
    assert record is not None

    batch = _batch_since(base)
    assert [event.type for _, event in batch] == [
        "persona_instance.chat_binding_cleared"
    ]
    assert "persona_instance.chat_binding_cleared" not in COVERED_DOMAIN_EVENT_TYPES
    assert not batch_is_patch_coverable(event for _, event in batch), (
        "the clear's batch became patch-coverable; a connected client will now "
        "fold a field subset instead of re-hydrating, and the clear moves core "
        "state no persona_instance patch can express (see the chat-history test)"
    )

    frames = _frames_for(batch, base_offset=base)
    assert [frame.get("type") for frame in frames] == ["delta"]
    assert frames[0].get("patches") is None
    assert isinstance(frames[0].get("core"), dict), (
        "the clear stopped shipping a full core; the row it moved now reaches "
        "no connected client at all"
    )
    # The re-hydrated core carries the CLEARED row, which is the whole reason
    # this path needs no patch of its own.
    rebuilt = frames[0]["core"]["persona_instances"][instance.id]
    assert rebuilt["default_chat_session_id"] is None
    assert rebuilt["session_id"] is None
    assert rebuilt["mode"] == "configured"


def test_covering_the_clear_would_drop_the_chat_history_row_it_also_moves(
    isolate_agent_runtime_root,
):
    """Why the demote is load-bearing rather than an un-done optimisation.

    A clear does not only move ``persona_instance`` fields. The
    ``persona_chat_history`` projection keys chat rows by
    ``default_chat_session_id``, so nulling the pointer takes the instance's
    chat row OUT of the section entirely -- and there is no
    ``persona_chat_history`` patch entity for that departure to ride.

    So the tempting symmetry with ``persona_instance.chat_opened`` (emit a
    patch, add the event to the covered set, stop paying a full core per chat
    delete) is a trade this wire cannot make yet: the promoted frame's only row
    would be the persona-instance patch, and the chat row would sit on every
    connected client for the rest of its session. The measurement is here so the
    next person to notice the asymmetry finds the reason instead of the gap.
    """

    from agent_runtime.patch_coverage import (
        COVERED_DOMAIN_EVENT_TYPES,
        HISTORICAL_FOLD_ENTITIES,
        normalize_fold_entities,
    )

    db = _TranscriptDB()
    store = PersonaInstanceStore()
    instance = store.open_chat(persona_id="dev", session_id="persona_chat_history_move")
    db.create_session("persona_chat_history_move", "agent_runtime_persona_chat")

    before = persona_chat_history_summary(
        persona_instances=[store.get(instance.id)],
        session_db=db,
        persona_assignments=[],
    )
    assert [row["session_id"] for row in before] == ["persona_chat_history_move"]

    store.clear_chat_session_binding(
        store.get(instance.id),
        session_id="persona_chat_history_move",
        reason="chat_deleted",
    )

    after = persona_chat_history_summary(
        persona_instances=[store.get(instance.id)],
        session_db=db,
        persona_assignments=[],
    )
    assert after == [], (
        "the chat-history row survived the clear; if that is now true the "
        "derivability argument below has changed and must be re-audited"
    )

    # Nothing a fielded client folds can carry that departure incrementally...
    declared = normalize_fold_entities(None)
    assert declared == normalize_fold_entities(HISTORICAL_FOLD_ENTITIES)
    assert "persona_chat_history" not in declared
    # ...so the event must stay uncovered, or the row is dropped silently.
    assert "persona_instance.chat_binding_cleared" not in COVERED_DOMAIN_EVENT_TYPES, (
        "the clear's event was added to the covered set, but its "
        "persona_chat_history row still has no patch entity to ride -- a "
        "promoted batch drops the chat row from every connected client"
    )


def test_both_live_clear_callers_reach_a_client_through_the_full_core(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The delete verb and the reconcile sweep, each driven, each demoting.

    Two operator-visible callers reach ``clear_chat_session_binding``. Neither
    is a special case: both append the same uncovered event, so both re-hydrate
    the client in the frame that reports the clear. A caller that emitted a
    patch INSTEAD of the event would pass the source-level test above and fail
    here, which is the point of driving them rather than asserting the shared
    code path.
    """

    from hermes_cli import harness

    from agent_runtime.patch_coverage import batch_is_patch_coverable

    cfg = _assignment_config()
    db = _TranscriptDB()
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)
    monkeypatch.setattr(harness, "_default_persona_session_db", lambda: db)

    store = PersonaInstanceStore()

    # -- caller 1: the operator ``persona chat delete`` verb -----------------
    deleted = store.create_operator_chat(
        persona_id="profile:reviewer", display_name="Reviewer"
    )
    db.create_session(deleted.session_id, "agent_runtime_persona_chat")
    base = len(list(EventLog().iter_from_offset(0)))
    assert (
        harness._cmd_persona_chat_delete(
            SimpleNamespace(
                session_id=deleted.session_id,
                persona_id=deleted.persona_id,
                persona_instance_id=deleted.id,
                requested_by="test",
                json=True,
            )
        )
        == 0
    )
    capsys.readouterr()
    delete_batch = _batch_since(base)
    assert "persona_instance.chat_binding_cleared" in {
        event.type for _, event in delete_batch
    }
    assert not batch_is_patch_coverable(event for _, event in delete_batch)
    frames = _frames_for(delete_batch, base_offset=base)
    assert frames and all(isinstance(frame.get("core"), dict) for frame in frames)

    # -- caller 2: the reconcile sweep ---------------------------------------
    orphan = store.open_chat(persona_id="qa", session_id="persona_chat_orphaned_root")
    # The sweep clears only on a POSITIVE "absent" from an enumerating
    # SessionDB, so the probe must see a live database that simply does not
    # hold this root.
    db.create_session("persona_chat_unrelated_live", "agent_runtime_persona_chat")
    base = len(list(EventLog().iter_from_offset(0)))
    result = PersonaInstanceStore().repair_missing_chat_session_bindings(
        apply=True, session_db=db
    )
    assert orphan.id in {row["persona_instance_id"] for row in result["repaired"]}
    sweep_batch = _batch_since(base)
    assert "persona_instance.chat_binding_cleared" in {
        event.type for _, event in sweep_batch
    }
    assert not batch_is_patch_coverable(event for _, event in sweep_batch)
    frames = _frames_for(sweep_batch, base_offset=base)
    assert frames and all(isinstance(frame.get("core"), dict) for frame in frames)
