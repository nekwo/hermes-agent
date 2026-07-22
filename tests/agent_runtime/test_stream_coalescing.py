"""Delta coalescing (transport plan W1, 2026-07-16).

The pre-W1 loop shipped one full ``build_snapshot()`` core PER EVENT — the
launcher then decoded ~9MB per append, serialized. These tests pin the new
contract: an event burst drains into ONE batch frame carrying ONE core, the
batch cap splits only genuinely long backlogs, and a single event keeps the
exact golden ``delta_batch`` shape (additive over ``delta``; the launcher
reads only type/watermark/identity_map/core).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import agent_runtime.stream as stream_mod
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.stream import stream_frames

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stream_frames"


def _append(log: EventLog, index: int) -> None:
    log.append(
        Event(
            ts=datetime(2026, 7, 16, 12, 0, index % 60, tzinfo=timezone.utc),
            type="state.reconciled",
            task_id=f"task_burst_{index}",
            run_id=None,
            persona_id="dev",
            payload={"fingerprint": f"burst-fp-{index}"},
        )
    )


class _SnapshotCallCounter:
    def __init__(self, monkeypatch):
        self.calls = 0
        real = stream_mod.build_snapshot

        def counting(*args, **kwargs):
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(stream_mod, "build_snapshot", counting)


def test_stream_coalesces_event_batch(isolate_agent_runtime_root, monkeypatch):
    counter = _SnapshotCallCounter(monkeypatch)
    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
    )
    assert next(frames)["type"] == "hydrate"
    calls_after_hydrate = counter.calls

    log = EventLog()
    for index in range(10):
        _append(log, index)

    frame = next(frames)
    assert frame["type"] == "delta"
    assert frame["coalesced_count"] == 10
    assert len(frame["events"]) == 10
    assert frame["entity"] == frame["events"][-1]
    # THE property W1 exists for: ten appends, ONE core rebuild.
    assert counter.calls == calls_after_hydrate + 1

    # Watermark sits at the batch's final offset so the launcher's `>`-only
    # gate applies the whole burst exactly once.
    final_offset = list(log.iter_from_offset(0))[-1][0]
    assert frame["watermark"]["event_offset"] == final_offset
    assert frame["seq"] == final_offset


def test_stream_batch_cap_splits_frames(isolate_agent_runtime_root, monkeypatch):
    counter = _SnapshotCallCounter(monkeypatch)
    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0,
        max_frames=3,
    )
    assert next(frames)["type"] == "hydrate"
    calls_after_hydrate = counter.calls

    log = EventLog()
    for index in range(300):
        _append(log, index)

    first = next(frames)
    second = next(frames)
    assert first["coalesced_count"] == 256
    assert second["coalesced_count"] == 44
    assert (
        second["watermark"]["event_offset"] > first["watermark"]["event_offset"]
    )
    # 300 events, exactly two rebuilds — one per emitted frame.
    assert counter.calls == calls_after_hydrate + 2


def test_stream_single_event_keeps_delta_batch_golden_shape(
    isolate_agent_runtime_root,
):
    """The live half the S1 golden test deferred: the stream's real output is
    now always batch-shaped, and one event must match the pinned
    ``delta_batch.json`` key-set exactly — additive over ``delta``, never a
    reshape."""

    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0,
        max_frames=2,
    )
    assert next(frames)["type"] == "hydrate"

    _append(EventLog(), 0)
    frame = next(frames)

    golden = json.loads(
        (FIXTURES / "delta_batch.json").read_text(encoding="utf-8")
    )
    assert set(frame) == set(golden)
    assert frame["schema_version"] == 1
    assert frame["coalesced_count"] == 1
    assert frame["entity"] == frame["events"][0]
    assert frame["seq"] == frame["watermark"]["event_offset"]
    assert isinstance(frame["core"], dict)
