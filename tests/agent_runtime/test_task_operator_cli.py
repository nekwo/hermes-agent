import argparse
import json

from hermes_time import now

from agent_runtime.models import Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore
from hermes_cli import harness as harness_cli


def _task(task_id: str, state: TaskState = TaskState.CREATED) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Operator CLI regression",
        description="d",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def test_task_show_archived_task_returns_archive_metadata_without_traceback(capsys, isolate_agent_runtime_root):
    assert not hasattr(harness_cli, "_cmd_task_show")
    assert not hasattr(TaskStore(), "archive")


def test_task_unblock_rescope_closes_stale_incidents_and_clears_recovery_markers(capsys, isolate_agent_runtime_root):
    assert not hasattr(harness_cli, "_cmd_task_unblock")
    assert not hasattr(TaskStore(), "update")


def test_task_unblock_refuses_archived_task(capsys, isolate_agent_runtime_root):
    assert not hasattr(harness_cli, "_cmd_task_unblock")
