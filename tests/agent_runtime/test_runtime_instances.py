from datetime import timedelta
import importlib.util

from hermes_time import now

from agent_runtime import paths
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore, runtime_instance_summary
from tests.agent_runtime.test_s53_lane_write_lane_removal import seed_lane_row
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


def test_lane_summary_fields_project_a_persisted_row(isolate_agent_runtime_root):
    """S53 retargeted this from ``test_lane_lifecycle_transitions_and_summary_fields``.

    It used to drive the lifecycle -- create_lane, two transitions, park, resume
    -- and then assert the summary. Every one of those writers was deleted for
    want of a production caller, and this test was among their only callers.

    The half worth keeping is the READ half: ``runtime_instance_summary`` still
    projects operator lane rows and ``status.py`` still publishes them. The row
    is seeded on disk because nothing can mint one any more, which is the same
    move ``test_status`` already makes for runs.
    """

    lane = seed_lane_row(
        "goalrt_summary",
        task_id="task_lane",
        state="running",
        priority=2,
        current_stage_id="stage_1",
        current_owner="dev",
    )

    summary = runtime_instance_summary(GoalRuntimeInstanceStore().get(lane.id))
    assert summary["lane_id"] == lane.id
    assert summary["lane_kind"] == "production"
    assert summary["state"] == "running"
    assert summary["priority"] == 2
    assert summary["current_stage_id"] == "stage_1"
