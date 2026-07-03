from __future__ import annotations

import json
from argparse import Namespace
from contextlib import closing

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event, Task
from agent_runtime.projector import Projector
from agent_runtime.read_model import ReadModel
from agent_runtime.runtime_config import RuntimeConfig, ReadModelConfig
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore

from test_read_model_slo import SLO_INCREMENTAL_APPLY_MS, _seed_synthetic_runtime


def test_event_log_iter_from_offset_resumes_at_byte_boundary(isolate_agent_runtime_root):
    log = EventLog()
    log.append(Event(ts=now(), type="task.created", task_id="t1", run_id=None, persona_id=None))
    first_offset = isolate_agent_runtime_root.joinpath("events.jsonl").stat().st_size
    log.append(Event(ts=now(), type="task.created", task_id="t2", run_id=None, persona_id=None))

    events = list(log.iter_from_offset(first_offset))

    assert len(events) == 1
    assert events[0][0] == isolate_agent_runtime_root.joinpath("events.jsonl").stat().st_size
    assert events[0][1].task_id == "t2"


def test_replay_equivalence_full_vs_incremental_goal_row(isolate_agent_runtime_root):
    task = _seed_open_task("task_projector_equivalence")
    incremental = ReadModel(isolate_agent_runtime_root / "incremental.db")
    initial_snapshot = build_snapshot()
    incremental.apply_full_rebuild(initial_snapshot, watermark=initial_snapshot["parity"]["watermark"])

    task.state = TaskState.BLOCKED
    task.updated_at = now()
    TaskStore().update(task, actor="harness", reason="projector equivalence")

    result = Projector(
        incremental,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).apply_pending()

    final_snapshot = build_snapshot()
    full = ReadModel(isolate_agent_runtime_root / "full.db")
    full.apply_full_rebuild(final_snapshot, watermark=final_snapshot["parity"]["watermark"])

    assert result.applied_events == 1
    assert result.incremental_apply_ms <= SLO_INCREMENTAL_APPLY_MS
    assert _goal(incremental, task.id) == _goal(full, task.id)


def test_apply_pending_is_o_delta_on_rd0_fixture(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    snapshot = build_snapshot()
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])

    task = _seed_open_task("task_projector_delta")

    result = Projector(
        read_model,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).apply_pending()

    assert result.applied_events == 1
    assert result.changed["goals"] == [task.id]
    assert result.incremental_apply_ms <= SLO_INCREMENTAL_APPLY_MS


def test_lease_excludes_second_projector(isolate_agent_runtime_root):
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    with closing(read_model.connect()) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            (
                "projector_lease",
                json.dumps({"pid": 999999, "expires_at_monotonic": 999999999999}),
            ),
        )
        conn.commit()

    assert Projector(read_model, config=RuntimeConfig()).acquire_lease() is False


def test_unknown_event_kind_marks_section_stale_not_dropped(isolate_agent_runtime_root):
    task = _seed_open_task("task_projector_stale")
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    snapshot = build_snapshot()
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])
    EventLog().append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=task.id,
            run_id=None,
            persona_id="dev",
            payload={"packet_id": "packet_1", "packet_type": "handoff"},
        )
    )

    result = Projector(
        read_model,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).apply_pending()

    assert result.applied_events == 1
    assert result.stale_sections == ["snapshot"]
    assert read_model.read_projection("stale_sections")["payload"] == ["snapshot"]


def test_rebuild_and_read_projection_cli(isolate_agent_runtime_root, capsys):
    import hermes_cli.harness as harness

    _seed_open_task("task_projector_cli")

    assert harness._cmd_rebuild_read_model(Namespace(json=True)) == 0
    rebuild_payload = json.loads(capsys.readouterr().out)
    assert rebuild_payload["ok"] is True
    assert rebuild_payload["watermark"]["event_offset"] > 0

    assert harness._cmd_read_projection(Namespace(projection="goals", since_offset=None, json=True)) == 0
    read_payload = json.loads(capsys.readouterr().out)
    assert read_payload["projection"] == "goals"
    assert len(read_payload["rows"]) == 1


def _seed_open_task(task_id: str) -> Task:
    task = Task(
        id=task_id,
        title="Projector task",
        description="Synthetic projector task.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="rd3",
    )
    return TaskStore().create(task)


def _goal(read_model: ReadModel, task_id: str) -> dict:
    for row in read_model.read_projection("goals")["rows"]:
        if row.get("goal_id") == task_id or row.get("task_id") == task_id:
            return row
    raise AssertionError(f"missing goal row {task_id}")
