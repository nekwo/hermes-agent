from __future__ import annotations

from hermes_time import now

from agent_runtime import paths
from types import SimpleNamespace

Task = SimpleNamespace
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
    snapshot = build_snapshot()
    assert snapshot["parity"]["contract_version"] == 49
    assert "goals" not in snapshot
    assert "boards" in snapshot


def test_snapshot_stage_projections_are_empty_after_graph_removal(isolate_agent_runtime_root) -> None:
    snapshot = build_snapshot()
    for key in ("goals", "stage_verification", "runs", "proofs", "incidents"):
        assert key not in snapshot


def test_write_snapshot_remains_importable_and_persists(isolate_agent_runtime_root) -> None:
    result = write_snapshot(build_snapshot())

    assert result["parity"]["contract_version"] == 49
    assert paths.snapshot_path().exists()
