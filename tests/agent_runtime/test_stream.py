from __future__ import annotations

import json
from argparse import Namespace

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.stream import delta_frame, hydrate_frame, stream_frames


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
    first = next(frames)
    assert first["type"] == "hydrate"

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
