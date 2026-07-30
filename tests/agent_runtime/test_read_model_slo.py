from __future__ import annotations

import json
import time

from hermes_time import now

from agent_runtime.models import Event
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.projector import Projector
from agent_runtime.read_model import ReadModel
from agent_runtime.runtime_config import ReadModelConfig, RuntimeConfig
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore

SLO_FULL_BUILD_MS = 2000
SLO_INCREMENTAL_APPLY_MS = 150
SLO_CONSUMER_VISIBLE_LAG_MS = 1500

SYNTHETIC_TASK_COUNT = 50
SYNTHETIC_EVENT_COUNT = 10_000


def test_synthetic_snapshot_full_build_within_rd0_slo(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)

    started = time.perf_counter()
    snapshot = build_snapshot()
    build_ms = int((time.perf_counter() - started) * 1000)

    assert len(snapshot["goals"]) == SYNTHETIC_TASK_COUNT
    assert snapshot["parity"]["snapshot_bytes"] > 0
    assert snapshot["parity"]["event_log_bytes"] > 0
    assert snapshot["parity"]["projection_age_ms"] is not None
    assert build_ms <= SLO_FULL_BUILD_MS


def test_synthetic_incremental_apply_within_rd3_slo(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    snapshot = build_snapshot()
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])
    task = Task(
        id="task_slo_delta",
        title="SLO delta task",
        description="Synthetic RD3 incremental task.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="rd3_slo",
    )
    TaskStore().create(task)

    result = Projector(
        read_model,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).apply_pending()

    assert result.applied_events == 1
    assert result.changed["goals"] == ["task_slo_delta"]
    assert result.incremental_apply_ms <= SLO_INCREMENTAL_APPLY_MS


def _seed_synthetic_runtime(root) -> None:
    task_store = TaskStore()
    ts = now()
    task_ids = []
    for index in range(SYNTHETIC_TASK_COUNT):
        task_id = f"task_slo_{index:03d}"
        task_ids.append(task_id)
        task_store.create(
            Task(
                id=task_id,
                title=f"SLO task {index}",
                description="Synthetic RD0 snapshot build task.",
                state=TaskState.DONE,
                created_at=ts,
                updated_at=ts,
                requested_by="rd0_slo",
            )
        )

    remaining = max(0, SYNTHETIC_EVENT_COUNT - SYNTHETIC_TASK_COUNT)
    lines = []
    for index in range(remaining):
        task_id = task_ids[index % len(task_ids)]
        event = Event(
            ts=ts,
            type="run.progress",
            task_id=task_id,
            run_id=f"run_slo_{index % 100:03d}",
            persona_id="dev",
            payload={"summary": f"synthetic event {index}", "stage_id": "implement"},
        )
        lines.append(json.dumps(to_jsonable(event), ensure_ascii=False, separators=(",", ":")))
    with open(root / "events.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        if lines:
            handle.write("\n")
