from __future__ import annotations

import json
import time

from hermes_time import now

from agent_runtime.models import Event
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import build_snapshot

SLO_FULL_BUILD_MS = 2000
# S46: ``SLO_INCREMENTAL_APPLY_MS = 150`` stood here. Its only two assertions
# were in ``test_synthetic_incremental_apply_within_rd3_slo`` (below) and
# ``test_apply_pending_is_o_delta_on_rd0_fixture`` — both timing
# ``Projector.apply_pending``, a lane with no production caller. The lane was
# ruled RETIRE (ledger item 9), so the budget went with the thing it budgeted.
SLO_CONSUMER_VISIBLE_LAG_MS = 1500

SYNTHETIC_EVENT_COUNT = 10_000


def test_synthetic_snapshot_full_build_within_rd0_slo(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)

    started = time.perf_counter()
    snapshot = build_snapshot()
    build_ms = int((time.perf_counter() - started) * 1000)

    assert "goals" not in snapshot
    assert snapshot["parity"]["snapshot_bytes"] > 0
    assert snapshot["parity"]["event_log_bytes"] > 0
    assert snapshot["parity"]["projection_age_ms"] is not None
    assert build_ms <= SLO_FULL_BUILD_MS


# S46: ``test_synthetic_incremental_apply_within_rd3_slo`` stood here, timing
# ``Projector.apply_pending`` against the retired incremental lane. Absence is
# asserted by ``test_s46_incremental_projection_lane_removal.py`` rather than
# left to a reader noticing the gap.


def _seed_synthetic_runtime(root) -> None:
    root.mkdir(parents=True, exist_ok=True)
    ts = now()
    lines = []
    for index in range(SYNTHETIC_EVENT_COUNT):
        event = Event(
            ts=ts,
            type="persona.updated",
            task_id=None,
            run_id=None,
            persona_id=f"profile:synthetic-{index % 50:03d}",
            payload={"summary": f"synthetic event {index}"},
        )
        lines.append(json.dumps(to_jsonable(event), ensure_ascii=False, separators=(",", ":")))
    with open(root / "events.jsonl", "a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
        if lines:
            handle.write("\n")
