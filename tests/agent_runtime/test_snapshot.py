from __future__ import annotations

from hermes_time import now

from agent_runtime import paths
from agent_runtime.models import Task
from agent_runtime.snapshot import build_snapshot, write_snapshot
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore


def _task() -> Task:
    ts = now()
    return Task(
        id="task_snapshot",
        title="Snapshot without stage graph",
        description="The chat-first snapshot remains buildable.",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def test_snapshot_builds_without_stage_graph(isolate_agent_runtime_root) -> None:
    TaskStore().create(_task())

    snapshot = build_snapshot()
    row = next(item for item in snapshot["goals"].values() if item["task_id"] == "task_snapshot")

    assert "mission_plan" not in row
    assert "agent_topology" not in row.get("mission_level_state", {})
    assert "agent_topology" not in snapshot["parity"]["capabilities"]


def test_snapshot_stage_projections_are_empty_after_graph_removal(isolate_agent_runtime_root) -> None:
    TaskStore().create(_task())

    row = next(item for item in build_snapshot()["goals"].values() if item["task_id"] == "task_snapshot")

    assert row["mission_flow_timeline"]["items"] == []
    assert row["proof_gate_state"]["status"] == "not_applicable"
    assert "stage_streams" not in row


def test_write_snapshot_remains_importable_and_persists(isolate_agent_runtime_root) -> None:
    result = write_snapshot(build_snapshot())

    assert isinstance(result["goals"], dict)
    assert paths.snapshot_path().exists()
