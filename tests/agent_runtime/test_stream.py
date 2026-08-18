from __future__ import annotations

import json
from argparse import Namespace

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.stream import delta_frame, hydrate_frame, stream_frames
from tests.agent_runtime.stream_liveness_helpers import drain_boot_liveness


def test_hydrate_frame_carries_snapshot_contract(isolate_agent_runtime_root):
    frame = hydrate_frame()

    assert frame["type"] == "hydrate"
    assert frame["schema_version"] == 1
    assert "watermark" in frame
    assert "core" in frame
    assert frame["core"]["schema_version"] == 2
    assert "completeness" in frame
    assert "parity_warnings" in frame


def test_stream_emits_delta_after_hydrate_offset(isolate_agent_runtime_root):
    frames = stream_frames(heartbeat_interval_seconds=60, max_frames=2)
    first = next(frames)
    assert first["type"] == "hydrate"

    EventLog().append(
        Event(
            ts=now(),
            type="persona_assignment.created",
            task_id="task_streamed",
            run_id=None,
            persona_id="neko_supervisor",
            payload={"summary": "created by stream test", "assignment_id": "pa_streamed"},
        )
    )

    second = next(frames)
    assert second["type"] == "delta"
    # The op still comes from ``_delta_op``'s prefix routing — S15 de-registered
    # the ``task.*`` family, so the routed prefix under test is the surviving
    # ``persona_assignment.*`` one.
    assert second["op"] == "instance.upserted"
    assert second["seq"] == second["watermark"]["event_offset"]
    assert second["entity"]["event"]["task_id"] == "task_streamed"


def test_heartbeat_frame_carries_no_daemon_block(isolate_agent_runtime_root):
    # The Mission Daemon was retired; heartbeats are pure watermark liveness
    # (no daemon status rider, no core).
    frames = stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=0.01, max_frames=2
    )
    assert drain_boot_liveness(frames)["type"] == "hydrate"

    second = next(frames)
    assert second["type"] == "heartbeat"
    assert "daemon" not in second
    assert "core" not in second


def test_delta_frame_masks_secret_assignments():
    frame = delta_frame(
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id="task_secret",
            run_id="run_secret",
            persona_id="dev",
            payload={"output": "API_TOKEN=super-secret\nall good"},
        ),
        offset=123,
    )

    assert frame["op"] == "chat.trace.appended"
    assert frame["entity"]["event"]["payload"]["output"] == "API_TOKEN=[redacted]\nall good"


def test_harness_stream_command_outputs_ndjson(isolate_agent_runtime_root, capsys):
    import hermes_cli.harness as harness

    assert (
        harness._cmd_stream(
            Namespace(poll_interval=0.01, heartbeat_interval=0.01, max_frames=1)
        )
        == 0
    )

    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["type"] == "hydrate"
    assert payload["schema_version"] == 1


# ── an unknown resume position must not tail from the head of the log ──────
#
# ``offset = int(watermark.get("event_offset") or 0)`` folded both a missing and
# an explicitly-unknown watermark into 0, then tailed from there — replaying the
# whole event log as fresh activity at the root of every Mission Control
# surface.


def _seed(count: int) -> None:
    log = EventLog()
    for index in range(count):
        log.append(
            Event(
                ts=now(),
                type="persona_assignment.created",
                task_id=f"task_pre_{index}",
                run_id=None,
                persona_id="neko_supervisor",
                payload={"summary": "pre-existing", "assignment_id": f"pa_{index}"},
            )
        )


def test_unknown_watermark_does_not_replay_the_log_as_fresh_activity(
    isolate_agent_runtime_root, monkeypatch
):
    _seed(3)

    import agent_runtime.parity as parity_mod

    def _boom():
        raise OSError(32, "share violation")

    monkeypatch.setattr(parity_mod.event_rotation, "log_end_offset", _boom)

    frames = stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=0.01, max_frames=3
    )
    first = drain_boot_liveness(frames)
    assert first["type"] == "hydrate"
    assert first["watermark"]["event_offset"] is None

    # No delta may carry the pre-existing rows: the position is unknown, so the
    # tailer has nothing to resume FROM and must not start at byte 0.
    for _ in range(2):
        frame = next(frames)
        assert frame["type"] == "heartbeat", frame["type"]
        # Liveness without a position — not a cursor at the head of the log.
        assert frame["watermark"]["event_offset"] is None
        # The UNREADABLE-TAIL heartbeat, not the boot build's: this case is
        # about the tailer refusing to invent a cursor, and the boot lane's
        # liveness carries an activity block that would make it a different
        # claim wearing the same shape.
        assert "activity" not in frame, frame["activity"]


def test_recovered_watermark_re_baselines_instead_of_replaying_or_gapping(
    isolate_agent_runtime_root, monkeypatch
):
    """Learning the tail again must re-baseline, not resume into a gap.

    Resuming from the freshly measured tail would silently drop whatever landed
    while the log was unreadable; resuming from 0 would re-render the whole log
    as new. The honest answer is the explicit resync this lane already has.
    """

    _seed(3)

    import agent_runtime.parity as parity_mod

    real = parity_mod.event_rotation.log_end_offset
    calls = {"n": 0}

    def _flaky():
        calls["n"] += 1
        # Unreadable for the hydrate's measurement, readable afterwards.
        if calls["n"] <= 1:
            raise OSError(32, "share violation")
        return real()

    monkeypatch.setattr(parity_mod.event_rotation, "log_end_offset", _flaky)

    frames = stream_frames(
        poll_interval_seconds=0.01, heartbeat_interval_seconds=60, max_frames=3
    )
    first = next(frames)
    assert first["type"] == "hydrate"
    assert first["watermark"]["event_offset"] is None

    EventLog().append(
        Event(
            ts=now(),
            type="persona_assignment.created",
            task_id="task_during_outage",
            run_id=None,
            persona_id="neko_supervisor",
            payload={"summary": "landed while unreadable", "assignment_id": "pa_outage"},
        )
    )

    second = next(frames)
    # A full re-baseline carrying a REAL position, not a delta replayed from 0.
    assert second["type"] == "hydrate"
    assert isinstance(second["watermark"]["event_offset"], int)
    assert second["watermark"]["event_offset"] > 0

    EventLog().append(
        Event(
            ts=now(),
            type="persona_assignment.created",
            task_id="task_after_recovery",
            run_id=None,
            persona_id="neko_supervisor",
            payload={"summary": "after recovery", "assignment_id": "pa_after"},
        )
    )

    third = next(frames)
    # Tailed from the re-baseline's own offset: only the newest event, and none
    # of the four rows the re-baseline's core already covers.
    assert third["type"] == "delta"
    assert third["entity"]["event"]["task_id"] == "task_after_recovery"
