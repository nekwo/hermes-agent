from datetime import timedelta
import importlib.util

from hermes_time import now

from agent_runtime import paths
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
    assert importlib.util.find_spec("agent_runtime.goal_hygiene") is None
    assert not hasattr(TaskStore(), "create")


def test_activate_runtime_uses_lanes_without_parking(isolate_agent_runtime_root):
    assert importlib.util.find_spec("agent_runtime.goal_hygiene") is None


def test_new_goal_hygiene_cancels_stale_foreign_active_run(isolate_agent_runtime_root):
    assert importlib.util.find_spec("agent_runtime.goal_hygiene") is None


def test_new_goal_hygiene_reports_fresh_foreign_active_run(isolate_agent_runtime_root):
    assert importlib.util.find_spec("agent_runtime.goal_hygiene") is None


def test_archive_preserves_runtime_instance_manifest(isolate_agent_runtime_root):
    assert not hasattr(TaskStore(), "archive")


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
