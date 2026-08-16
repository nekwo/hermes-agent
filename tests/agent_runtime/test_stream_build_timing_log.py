"""``build_snapshot()``'s real cost, on the record (follow-on item 2, 2026-08-16).

The full-core rebuild is the dominant cost of every demoted gesture — measured
~6.3-6.6s of a 6.94s agent create on the operator's machine. Establishing that
number took two diagnostic passes: subtraction from ``events_archive/*.jsonl``
timestamps, then profiling an isolated probe copy of the runtime root. Nothing
logged it.

The stream's liveness heartbeat carries a ``snapshot_build`` activity block, but
that value is a MID-BUILD sample on the heartbeat cadence and is NOT the total
(see ``test_heartbeat_activity_is_a_partial_not_the_total``). These tests pin the
line that IS the total: emitted on frame completion, through the ordinary
``Logger`` family, anchored to the watermark the frame carries and labelled with
the lane that paid for it.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import pytest

import agent_runtime.stream as stream_mod
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.stream import stream_frames

_LINE = re.compile(
    r"snapshot_build reason=(?P<reason>\S+) elapsed_ms=(?P<elapsed>\d+) "
    r"offset=(?P<offset>\S+) events=(?P<events>\S+)"
)


def _append(log: EventLog, index: int) -> None:
    log.append(
        Event(
            ts=datetime(2026, 8, 16, 12, 0, index % 60, tzinfo=timezone.utc),
            type="state.reconciled",
            task_id=f"task_timing_{index}",
            run_id=None,
            persona_id="dev",
            payload={"fingerprint": f"timing-fp-{index}"},
        )
    )


def _build_lines(caplog) -> list[re.Match[str]]:
    matches = []
    for record in caplog.records:
        if record.name != "agent_runtime.stream":
            continue
        match = _LINE.fullmatch(record.getMessage())
        if match is not None:
            matches.append(match)
    return matches


@pytest.fixture
def capture_build_log(caplog):
    caplog.set_level(logging.INFO, logger="agent_runtime.stream")
    return caplog


def _slow_build(monkeypatch, seconds: float = 0.15):
    """Make ``build_snapshot`` take a known, non-trivial amount of time.

    The point is a MEASUREMENT assertion rather than a "was it called" one: a
    hard-coded zero, a constant, or a timer around the wrong span all pass an
    "elapsed_ms is present" check and all fail this.
    """

    real = stream_mod.build_snapshot

    def slow(*args, **kwargs):
        time.sleep(seconds)
        return real(*args, **kwargs)

    monkeypatch.setattr(stream_mod, "build_snapshot", slow)


def test_hydrate_logs_the_build_it_paid_for(
    isolate_agent_runtime_root, monkeypatch, capture_build_log
):
    _slow_build(monkeypatch)
    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=1,
    )
    hydrate = next(frames)
    assert hydrate["type"] == "hydrate"

    lines = _build_lines(capture_build_log)
    assert len(lines) == 1, [record.getMessage() for record in capture_build_log.records]
    assert lines[0]["reason"] == "hydrate"
    assert int(lines[0]["elapsed"]) >= 100
    assert lines[0]["offset"] == str(hydrate["watermark"]["event_offset"])


def test_full_core_batch_logs_elapsed_offset_and_event_count(
    isolate_agent_runtime_root, monkeypatch, capture_build_log
):
    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
    )
    assert next(frames)["type"] == "hydrate"
    capture_build_log.clear()

    # Slow only the BATCH build, so the hydrate above stays cheap and the
    # asserted duration can only have come from this frame's own build.
    _slow_build(monkeypatch)
    log = EventLog()
    for index in range(4):
        _append(log, index)

    frame = next(frames)
    assert frame["type"] == "delta"
    assert frame["coalesced_count"] == 4

    lines = _build_lines(capture_build_log)
    assert len(lines) == 1, [record.getMessage() for record in capture_build_log.records]
    match = lines[0]
    # The fold lane is on in this build, and `state.reconciled` is not
    # patch-coverable — so this is the expensive case by name.
    assert match["reason"] == "demote"
    assert int(match["elapsed"]) >= 100
    # The anchors that make the number usable: which watermark this build
    # produced, and how many events it was billed to.
    assert match["offset"] == str(frame["watermark"]["event_offset"])
    assert match["events"] == "4"


def test_lane_off_batch_is_labelled_full_core_not_demote(
    isolate_agent_runtime_root, monkeypatch, capture_build_log
):
    """With the fold lane OFF, a full core is the designed wire, not a demotion.

    ``full_core`` and ``demote`` cost identical seconds but mean opposite
    things: the first is the wire working as specified, the second is a
    foldable update that just paid for a whole snapshot. Collapsing them would
    make the grep that matters return every frame ever built.
    """

    monkeypatch.setattr(stream_mod, "delta_patches_enabled", lambda config=None: False)
    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
    )
    assert next(frames)["type"] == "hydrate"
    capture_build_log.clear()

    log = EventLog()
    _append(log, 0)

    frame = next(frames)
    assert frame["type"] == "delta"

    lines = _build_lines(capture_build_log)
    assert len(lines) == 1, [record.getMessage() for record in capture_build_log.records]
    assert lines[0]["reason"] == "full_core"


def test_resync_batch_is_labelled_resync(
    isolate_agent_runtime_root, monkeypatch, capture_build_log
):
    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
        resync=True,
    )
    assert next(frames)["type"] == "hydrate"
    capture_build_log.clear()

    log = EventLog()
    _append(log, 0)

    assert next(frames)["type"] == "delta"
    lines = _build_lines(capture_build_log)
    assert len(lines) == 1, [record.getMessage() for record in capture_build_log.records]
    assert lines[0]["reason"] == "resync"


def _drain_to_delta(frames) -> tuple[list[dict], dict | None]:
    activities: list[dict] = []
    for frame in frames:
        activity = frame.get("activity")
        if isinstance(activity, dict) and activity.get("kind") == "snapshot_build":
            activities.append(activity)
        if frame["type"] == "delta":
            return activities, frame
    return activities, None


def test_heartbeat_activity_is_sampled_on_the_cadence_not_the_build(
    isolate_agent_runtime_root, monkeypatch, capture_build_log
):
    """The wire's ``elapsed_ms`` is a cadence sample; the log line is the total.

    This is the claim the follow-on plan (§10.3 item 2) got wrong — it read the
    heartbeat as carrying the build's cost. It carries how long the build had
    been running when a heartbeat came DUE, so its values are quantised to the
    heartbeat interval, not to the build. At the 5s production cadence a 6.5s
    build yields exactly one sample near 5000.
    """

    interval = 0.1
    _slow_build(monkeypatch, seconds=0.55)
    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=interval,
        delta_debounce_seconds=0.01,
        max_frames=60,
    )
    assert next(frames)["type"] == "hydrate"
    capture_build_log.clear()

    log = EventLog()
    _append(log, 0)
    activities, delta = _drain_to_delta(frames)

    assert delta is not None
    assert activities, "expected at least one snapshot_build heartbeat"
    assert all(activity["state"] == "busy" for activity in activities)
    # Sample k lands at ~k intervals in, so the FIRST one reports roughly one
    # interval — nowhere near the ~550ms the build actually took. That is the
    # whole defect: the wire number answers "has it been going a while?", not
    # "how long did it take?".
    assert int(activities[0]["elapsed_ms"]) < 300

    lines = _build_lines(capture_build_log)
    assert lines, "the delta's build logged nothing"
    assert max(int(match["elapsed"]) for match in lines) >= 500


def test_a_build_shorter_than_one_heartbeat_puts_nothing_on_the_wire(
    isolate_agent_runtime_root, monkeypatch, capture_build_log
):
    """The warm-build case: no heartbeat is ever due, so the wire says nothing.

    Measured warm cost on the operator's drive is ~1.17s against a 5s cadence —
    i.e. the common case emits ZERO ``snapshot_build`` activity. Only the log
    line covers it.
    """

    frames = stream_frames(
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=60,
        delta_debounce_seconds=0.01,
        max_frames=2,
    )
    assert next(frames)["type"] == "hydrate"
    capture_build_log.clear()

    log = EventLog()
    _append(log, 0)
    activities, delta = _drain_to_delta(frames)

    assert delta is not None
    assert activities == []
    lines = _build_lines(capture_build_log)
    assert len(lines) == 1, [record.getMessage() for record in capture_build_log.records]
    assert int(lines[0]["elapsed"]) >= 0
