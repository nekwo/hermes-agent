from datetime import timedelta

from hermes_time import now

from agent_runtime import paths
from agent_runtime.goal_hygiene import activate_foreground_runtime, prepare_new_goal_runtime
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instance_summary
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import RunStore, TaskStore


def _task(task_id: str, state=TaskState.CREATED) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title=task_id,
        description="test",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def test_new_goal_hygiene_preserves_open_tasks_without_parking(isolate_agent_runtime_root):
    task_store = TaskStore()
    task_store.create(_task("task_old"))

    report = prepare_new_goal_runtime(
        task_store=task_store,
        foreground_mode=True,
        park_open_tasks=True,
        exclude_task_ids={"task_new"},
    )

    assert paths.task_path("task_old").exists()
    assert not paths.deleted_archive_dir().exists()
    assert report["parked_open_task_ids"] == []
    assert GoalRuntimeInstanceStore().latest_for_task("task_old") is None


def test_activate_runtime_uses_lanes_without_parking(isolate_agent_runtime_root):
    store = GoalRuntimeInstanceStore()
    first = store.create_lane(task_id="task_old", started_by="test", state="running")

    activated = activate_foreground_runtime("task_new", started_by="test", runtime_store=store)

    assert activated["target_task_id"] == "task_new"
    assert activated["queue_mode"] == "lane"
    assert store.get(first.id).lane == first.id
    assert store.get(first.id).state == "running"
    assert store.active_foreground() is None


def test_new_goal_hygiene_cancels_stale_foreign_active_run(isolate_agent_runtime_root):
    task_store = TaskStore()
    run_store = RunStore()
    task_store.create(_task("task_old"))
    run = run_store.open_run("dev", "task_old")
    run.last_heartbeat_at = now() - timedelta(seconds=120)
    run_store.update(run)

    report = prepare_new_goal_runtime(
        task_store=task_store,
        run_store=run_store,
        foreground_mode=True,
        park_open_tasks=True,
        heartbeat_ttl_seconds=1,
        exclude_task_ids={"task_new"},
    )

    assert report["cancelled_run_ids"] == []
    assert report["stale_incident_ids"]
    assert run_store.get(run.id).state == RunState.STALE


def test_new_goal_hygiene_reports_fresh_foreign_active_run(isolate_agent_runtime_root):
    task_store = TaskStore()
    run_store = RunStore()
    task_store.create(_task("task_old"))
    run = run_store.open_run("dev", "task_old")

    report = prepare_new_goal_runtime(
        task_store=task_store,
        run_store=run_store,
        foreground_mode=True,
        park_open_tasks=True,
        heartbeat_ttl_seconds=3600,
        exclude_task_ids={"task_new"},
    )

    assert report["cancelled_run_ids"] == []
    assert report["blocking_active_run_ids"] == [run.id]
    assert run_store.get(run.id).state == RunState.RUNNING


def test_archive_preserves_runtime_instance_manifest(isolate_agent_runtime_root):
    task_store = TaskStore()
    task_store.create(_task("task_done", state=TaskState.DONE))
    instance = GoalRuntimeInstanceStore().create_lane(task_id="task_done", started_by="test", state="running")

    result = task_store.archive("task_done", actor="test", reason="test archive")

    assert result["archived_count"] == 1
    archived = result["archived_tasks"][0]
    assert archived["runtime_instance_ids"] == [instance.id]
    archive_dir = paths.deleted_archive_dir() / result["archive_batch"]
    assert (archive_dir / "runtime_instances" / f"{instance.id}.json").exists()
    assert not paths.runtime_instance_path(instance.id).exists()


def test_lane_lifecycle_transitions_and_summary_fields(isolate_agent_runtime_root):
    store = GoalRuntimeInstanceStore()
    lane = store.create_lane(task_id="task_lane", started_by="test", lane_kind="production", priority=2)

    assert lane.state == "queued"
    lane = store.transition(lane.id, "activating", reason="activate")
    lane = store.transition(lane.id, "running", reason="lease acquired", current_stage_id="stage_1", current_owner="dev")
    lane = store.park_lane(lane.id, reason="operator pause")
    lane = store.resume_lane(lane.id, reason="operator resume")

    summary = runtime_instance_summary(lane)
    assert summary["lane_id"] == lane.id
    assert summary["lane_kind"] == "production"
    assert summary["state"] == "running"
    assert summary["priority"] == 2
    assert summary["current_stage_id"] == "stage_1"
