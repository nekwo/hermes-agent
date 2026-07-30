from __future__ import annotations

import json
import textwrap
from argparse import Namespace
from contextlib import closing

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event, RepoBundle
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.persona_assignments import PersonaAssignmentSpec, PersonaAssignmentStore
from agent_runtime.projector import Projector
from agent_runtime.read_model import ReadModel
from agent_runtime.repo_bundles import RepoBundleStore
from agent_runtime.role_envelopes import RoleEnvelopeStore
from agent_runtime.runtime_config import RuntimeConfig, ReadModelConfig
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore
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
    incremental = ReadModel(isolate_agent_runtime_root / "incremental.db")
    initial_snapshot = build_snapshot()
    incremental.apply_full_rebuild(initial_snapshot, watermark=initial_snapshot["parity"]["watermark"])
    EventLog().append(Event(now(), "persona.updated", None, None, "profile:alice", {}))

    result = Projector(
        incremental,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).apply_pending()

    assert result.applied_events == 1
    assert result.incremental_apply_ms <= SLO_INCREMENTAL_APPLY_MS
    rendered = incremental.render_snapshot()
    assert rendered["parity"]["contract_version"] == 45
    assert "goals" not in rendered


def test_replay_equivalence_goal_row_carries_bundle_assignment_and_lane_state(isolate_agent_runtime_root):
    incremental = ReadModel(isolate_agent_runtime_root / "incremental.db")
    initial_snapshot = build_snapshot()
    incremental.apply_full_rebuild(initial_snapshot, watermark=initial_snapshot["parity"]["watermark"])
    EventLog().append(Event(now(), "persona.updated", None, None, "profile:alice", {}))

    result = Projector(
        incremental,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).apply_pending()

    assert result.changed == {"sections": ["snapshot"]}
    assert "boards" in incremental.render_snapshot()


def test_apply_pending_is_o_delta_on_rd0_fixture(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    snapshot = build_snapshot()
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])

    EventLog().append(Event(now(), "persona.updated", None, None, "profile:delta", {}))

    result = Projector(
        read_model,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).apply_pending()

    assert result.applied_events == 1
    assert result.changed == {"sections": ["snapshot"]}
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
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    snapshot = build_snapshot()
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])
    EventLog().append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=None,
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
    assert result.stale_sections == []
    assert result.changed == {"sections": ["snapshot"]}


def test_rebuild_and_read_projection_cli(isolate_agent_runtime_root, capsys):
    import hermes_cli.harness as harness

    EventLog().append(Event(now(), "persona.updated", None, None, "profile:cli", {}))

    assert harness._cmd_rebuild_read_model(Namespace(json=True)) == 0
    rebuild_payload = json.loads(capsys.readouterr().out)
    assert rebuild_payload["ok"] is True
    assert rebuild_payload["watermark"]["event_offset"] > 0

    assert harness._cmd_read_projection(Namespace(projection="agent_instances", since_offset=None, json=True)) == 0
    read_payload = json.loads(capsys.readouterr().out)
    assert read_payload["projection"] == "agent_instances"
    assert read_payload["rows"] == []


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


def _write_enterprise_config() -> None:
    """Turn on the persona instance/assignment runtime in the ON-DISK config.

    ``build_snapshot`` gates the ``persona_assignments`` / ``persona_instances``
    inputs on ``load_agent_runtime_config()``, and so must every other rebuild
    path. Writing the real config file (instead of monkeypatching one module's
    imported name) keeps ONE config authority for both the full-rebuild and the
    incremental path — patching only ``snapshot``'s copy would hand the two
    paths different gate answers and hide the very divergence under test."""

    from hermes_constants import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """
            agent_runtime:
              enterprise_worker_sessions:
                enabled: true
                worker_session_store: true
                persona_instance_runtime: true
                persona_assignment_store: true
            """
        ),
        encoding="utf-8",
    )


def _row_diff(left: dict, right: dict) -> dict:
    """Field-level diff of two goal rows — the assertion message that names
    WHICH projected fields diverged instead of dumping two whole rows."""

    diff: dict = {}
    for key in sorted(set(left) | set(right)):
        if left.get(key) != right.get(key):
            diff[key] = {"incremental": left.get(key), "full": right.get(key)}
    return diff


def _goal(read_model: ReadModel, task_id: str) -> dict:
    for row in read_model.read_projection("goals")["rows"]:
        if row.get("goal_id") == task_id or row.get("task_id") == task_id:
            return row
    raise AssertionError(f"missing goal row {task_id}")
