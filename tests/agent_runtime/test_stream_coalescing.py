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
import threading
from datetime import datetime, timezone
from pathlib import Path

import agent_runtime.stream as stream_mod
from tests.agent_runtime.stream_liveness_helpers import drain_boot_liveness
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
    # 300 events, ONE rebuild — it was two until W3-H2 (2026-08-22).
    #
    # The cap splits the FRAMES; it used to split the BUILD too. Every one of
    # the 300 appends landed before either frame drained, so the core built for
    # the first frame was stamped at the log's end offset — which is exactly the
    # offset the second frame's batch reaches. The demote lane now checks that
    # (``demote_core_reuse``: equality of ``parity.events_position``) and hands
    # the core back instead of rebuilding state it built milliseconds ago. Not a
    # freshness loss in either direction: the reused core reaches the offset its
    # frame is stamped with, which is what MCF-Q1 requires of it.
    assert counter.calls == calls_after_hydrate + 1
    # BO-1: the reuse is of the CORE, never a dedupe of emissions. Both frames
    # are still emitted, both still carry a core, and each carries its OWN dict.
    assert first["core"] is not second["core"]
    assert first["core"] == second["core"]


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


def test_slow_full_core_build_emits_applied_watermark_heartbeats(
    isolate_agent_runtime_root, monkeypatch
):
    """A healthy producer blocked in snapshot construction stays live without
    advertising the not-yet-applied event offset."""

    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.02,
        delta_debounce_seconds=0,
    )
    # Past the boot build's own liveness (MC-4 / P6): at this 0.02s cadence the
    # boot build heartbeats before its hydrate, and taking ``next(frames)`` as
    # the hydrate made this case ORDER-DEPENDENT — green alone on a warm build
    # that beat the interval, red in a batch run where it did not.
    hydrate = drain_boot_liveness(frames)
    assert hydrate["type"] == "hydrate", hydrate["type"]
    applied_offset = hydrate["watermark"]["event_offset"]

    real_build = stream_mod.build_snapshot
    release = threading.Event()

    def slow_build(**kwargs):
        # ``**kwargs`` because the batch build now threads ``build_info`` — the
        # attribution out-param (EG-2.1), which this fake neither fills nor
        # needs: the assertion below is about heartbeats, not about roles.
        assert release.wait(10)
        return real_build(**kwargs)

    monkeypatch.setattr(stream_mod, "build_snapshot", slow_build)
    _append(EventLog(), 0)

    heartbeat = next(frames)
    assert heartbeat["type"] == "heartbeat"
    assert heartbeat["watermark"]["event_offset"] == applied_offset
    assert heartbeat["activity"]["kind"] == "snapshot_build"
    assert heartbeat["activity"]["state"] == "busy"

    release.set()
    delta = next(frames)
    while delta["type"] == "heartbeat":
        delta = next(frames)
    assert delta["type"] == "delta"
    assert delta["watermark"]["event_offset"] > applied_offset
