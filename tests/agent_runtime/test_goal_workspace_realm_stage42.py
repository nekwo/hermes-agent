import json
import subprocess
import sys

from hermes_time import now

from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.persona_assignments import (
    PersonaAssignmentStore,
    PersonaInstanceStore,
)
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instances_summary
from tests.agent_runtime.test_s53_lane_write_lane_removal import seed_lane_row
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore, WorkspaceStore, RealmStore


def _task(task_id: str, *, goal_id: str | None = None, workspace_id: str | None = None, stage_id: str = "scope") -> Task:
    ts = now()
    return Task(
        id=task_id,
        goal_id=goal_id,
        workspace_id=workspace_id,
        title="Test goal",
        description="Test goal",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        current_stage_id=stage_id,
    )


def _run_harness(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_workspace_and_realm_round_trip(isolate_agent_runtime_root):
    realm = RealmStore().create(name="Runtime Realm", server_id="srv_1")
    workspace = WorkspaceStore().create(
        name="Runtime Workspace",
        realm_id=realm.id,
        agent_ids=["neko_supervisor", "dev"],
        default_blueprint_id="neko_two_dev_default",
    )

    assert RealmStore().get(realm.id).server_id == "srv_1"
    assert WorkspaceStore().get(workspace.id).realm_id == realm.id
    assert WorkspaceStore().active_id() is None
    WorkspaceStore().set_active(workspace.id)
    RealmStore().set_active(realm.id)
    assert WorkspaceStore().active_id() == workspace.id
    assert RealmStore().active_id() == realm.id


def test_realm_use_reconciles_active_workspace(isolate_agent_runtime_root):
    """Switching realms must not leave the active workspace pointing into
    another realm: fall to the new realm's first workspace or clear it,
    and emit activation events so stream consumers see the switch."""
    from agent_runtime.events import EventLog

    realm_a = RealmStore().create(name="Realm A")
    realm_b = RealmStore().create(name="Realm B")
    workspace_a = WorkspaceStore().create(name="A Workspace", realm_id=realm_a.id)
    RealmStore().set_active(realm_a.id)
    WorkspaceStore().set_active(workspace_a.id)

    result = _run_harness("realm", "use", realm_b.id, "--json")
    assert result.returncode == 0, result.stderr
    assert RealmStore().active_id() == realm_b.id
    # Realm B has no workspaces — the cross-realm workspace is cleared.
    assert WorkspaceStore().active_id() is None

    result = _run_harness("realm", "use", realm_a.id, "--json")
    assert result.returncode == 0, result.stderr
    assert WorkspaceStore().active_id() == workspace_a.id

    types = [event.type for event in EventLog().tail(6)]
    assert "realm.activated" in types
    assert "workspace.activated" in types


def test_realm_use_prefers_declared_default_workspace(isolate_agent_runtime_root):
    realm = RealmStore().create(
        name="Realm With Default",
        default_workspace_id="ws_realm_default",
        default_workspace_name="Custom Office",
    )
    WorkspaceStore().create(
        name="Alphabetically First", realm_id=realm.id, workspace_id="ws_alpha"
    )
    WorkspaceStore().create(
        name="Custom Office", realm_id=realm.id, workspace_id="ws_realm_default"
    )

    result = _run_harness("realm", "use", realm.id, "--json")

    assert result.returncode == 0, result.stderr
    assert WorkspaceStore().active_id() == "ws_realm_default"


def test_workspace_create_in_active_realm_auto_activates(isolate_agent_runtime_root):
    """Creating a workspace inside the active realm lands the operator in
    it — the view follows the creation (workspace.created +
    workspace.activated events keep stream consumers current)."""
    from agent_runtime.events import EventLog

    realm = RealmStore().create(name="Fresh Realm")
    RealmStore().set_active(realm.id)
    assert WorkspaceStore().active_id() is None

    result = _run_harness(
        "workspace", "create", "--name", "First Workspace", "--realm", realm.id, "--json"
    )
    assert result.returncode == 0, result.stderr
    created = json.loads(result.stdout)
    assert WorkspaceStore().active_id() == created["id"]
    assert WorkspaceStore().get(created["id"]).realm_id == realm.id

    types = [event.type for event in EventLog().tail(4)]
    assert "workspace.created" in types
    assert "workspace.activated" in types

    # A second create in the same active realm moves the active pointer:
    # the operator always lands in the workspace they just created.
    result = _run_harness(
        "workspace", "create", "--name", "Second Workspace", "--realm", realm.id, "--json"
    )
    assert result.returncode == 0, result.stderr
    second = json.loads(result.stdout)
    assert WorkspaceStore().active_id() == second["id"]


def test_goal_id_current_stage_and_assignment_grouping(
    isolate_agent_runtime_root, persisted_persona_samples
):
    store = TaskStore()
    assert not hasattr(store, "create")
    assert not hasattr(store, "get_goal")
    persona = next(item for item in ensure_persisted_personas(load_agent_runtime_config()) if item.id == "neko_supervisor")
    first = PersonaInstanceStore().ensure_for_persona(persona)
    second = PersonaInstanceStore().ensure_for_persona(persona)
    assert first.id == second.id
    assert first.mode in {"chat", "configured"}


def test_lanes_stay_a_flat_list_with_no_foreground_election(isolate_agent_runtime_root):
    """S53 retargeted this from ``test_lane_only_create_lane_does_not_park``.

    The original name described a WRITER guarantee (``create_lane`` does not
    park a sibling), and that writer is gone -- deleted with the lane write lane
    for want of a production caller. What it actually pinned is a READ-side
    finding and survives intact: lanes are a flat list, there is no foreground
    election, and every persisted row comes back out of ``list_all``. Rows are
    seeded on disk because nothing can mint one any more.
    """

    first = seed_lane_row("goalrt_one", task_id="task_one", state="running")
    second = seed_lane_row("goalrt_two", task_id="task_two", state="running")
    store = GoalRuntimeInstanceStore()

    assert first.lane == first.id
    assert second.lane == second.id
    assert store.get(first.id).state == "running"
    assert store.get(second.id).state == "running"
    # S21 removed `active_foreground()`: it walked lanes into `TaskStore.get`,
    # which the permanent `TaskStoreStub` always raises `NotFound` from, so it
    # returned `None` by construction and not because no lane was elected. The
    # honest pin is that lanes are a flat list with no foreground election.
    assert not hasattr(store, "active_foreground")
    lanes = runtime_instances_summary(store.list_all())["lanes"]
    assert {row["lane_id"] for row in lanes} == {first.id, second.id}
    assert "foreground" not in runtime_instances_summary(store.list_all())


def test_stage42_goal_list_and_error_envelopes(isolate_agent_runtime_root):
    ok = _run_harness("goal", "list", "--json")
    assert ok.returncode != 0
    assert "invalid choice" in ok.stderr

    missing = _run_harness("goal", "show", "missing_goal", "--json")
    assert missing.returncode != 0
    assert "invalid choice" in missing.stderr
