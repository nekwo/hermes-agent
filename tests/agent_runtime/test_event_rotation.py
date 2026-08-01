"""Stage C6a — size-gated, offset-safe rotation of the append-only event log.

The load-bearing invariant: logical (cross-slice, monotonic) offsets are
preserved, so every byte-offset tailer (``iter_from_offset``) and the checkpoint
``event_offset`` watermark keep resolving unchanged across a rotation boundary.
"""

import json
import os

import pytest

from hermes_time import now

from agent_runtime import event_rotation, paths
from agent_runtime.config import _event_log_config
from agent_runtime.events import (
    CachedEventLog,
    EventLog,
    event_log_health,
)
from agent_runtime.models import Event
from agent_runtime.parity import events_watermark

_CAP_ENV = "HERMES_EVENT_LOG_ROTATION_CAP_BYTES"


def _evt(i: int, *, task_id: str = "t", session_id: str | None = None) -> Event:
    return Event(
        ts=now(),
        type="persona_instance.created",
        task_id=task_id,
        run_id=f"r{i}",
        persona_id="dev",
        payload={"n": i, "persona_instance_id": f"pi_{i}", "persona_id": "dev"},
        session_id=session_id,
    )


def _run_ids(events) -> list[str]:
    return [event.run_id for event in events]


# ── config plumbing ──────────────────────────────────────────────────────────


def test_event_log_config_defaults_and_clamps():
    assert _event_log_config({}).rotation_cap_bytes == 16 * 1024 * 1024
    assert _event_log_config({"rotation_cap_bytes": 1234}).rotation_cap_bytes == 1234
    # 0 = explicit "never rotate"; negative clamps to 0; junk → default.
    assert _event_log_config({"rotation_cap_bytes": 0}).rotation_cap_bytes == 0
    assert _event_log_config({"rotation_cap_bytes": -5}).rotation_cap_bytes == 0
    assert _event_log_config({"rotation_cap_bytes": "nope"}).rotation_cap_bytes == 16 * 1024 * 1024


# ── pristine (no rotation) is byte-identical to the old world ────────────────


def test_pristine_no_rotation_preserves_legacy_offset_semantics(isolate_agent_runtime_root):
    # No cap env → 16 MiB default → tiny data never rotates.
    log = EventLog()
    for i in range(3):
        log.append(_evt(i))

    assert not event_rotation.manifest_path().exists()
    assert event_rotation.slice_count() == 1

    events_file = isolate_agent_runtime_root / "events.jsonl"
    assert len(events_file.read_text(encoding="utf-8").splitlines()) == 3

    # Logical tail == the raw byte size; resuming from it yields nothing.
    assert event_rotation.log_end_offset() == os.path.getsize(events_file)
    assert events_watermark()["event_offset"] == os.path.getsize(events_file)
    assert list(log.iter_from_offset(event_rotation.log_end_offset())) == []


def test_rotation_disabled_when_cap_zero(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "0")
    log = EventLog()
    for i in range(8):
        log.append(_evt(i))
    assert event_rotation.slice_count() == 1
    assert not event_rotation.manifest_path().exists()


# ── rotation seals in place + records a manifest ─────────────────────────────


def test_rotation_seals_base_slice_and_writes_manifest(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")  # rotate on every append after the first
    log = EventLog()
    for i in range(3):
        log.append(_evt(i))

    # 2 sealed slices (r0, r1) + live (r2).
    assert event_rotation.slice_count() == 3
    manifest = json.loads(event_rotation.manifest_path().read_text(encoding="utf-8"))
    assert manifest["version"] == event_rotation.MANIFEST_VERSION

    # The base-0 slice stays as events.jsonl, sealed in place (never renamed).
    base_slice = isolate_agent_runtime_root / "events.jsonl"
    assert base_slice.exists()
    assert len(base_slice.read_text(encoding="utf-8").splitlines()) == 1  # only r0

    # The live file moved off events.jsonl into events_archive/.
    live = event_rotation.live_path()
    assert live.parent == event_rotation.archive_dir()

    # Logical tail == sum of every slice's bytes.
    total = sum(sl.path.stat().st_size for sl in event_rotation.slices())
    assert event_rotation.log_end_offset() == total


# ── the core invariant: cross-slice logical offsets ──────────────────────────


def test_iter_from_offset_spans_slices_monotonic(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")
    log = EventLog()
    for i in range(10):
        log.append(_evt(i))

    pairs = list(log.iter_from_offset(0))
    assert _run_ids([e for _o, e in pairs]) == [f"r{i}" for i in range(10)]
    offsets = [o for o, _e in pairs]
    assert offsets == sorted(offsets) and len(set(offsets)) == len(offsets)  # strictly increasing
    assert offsets[-1] == event_rotation.log_end_offset()

    # Resuming from any mid-stream offset continues with no gap and no dup.
    resume_at = offsets[3]
    assert _run_ids([e for _o, e in log.iter_from_offset(resume_at)]) == [f"r{i}" for i in range(4, 10)]


def test_watermark_is_logical_tail_after_rotation(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")
    log = EventLog()
    for i in range(6):
        log.append(_evt(i))

    watermark = events_watermark()
    assert watermark["event_offset"] == event_rotation.log_end_offset()
    # Proves the watermark is the LOGICAL tail, not the (small) live-slice size —
    # a naive rotate would have reset it below the live file's own bytes.
    assert watermark["event_offset"] > event_rotation.live_path().stat().st_size
    assert list(log.iter_from_offset(watermark["event_offset"])) == []


def test_tailer_resumes_across_a_forced_rotation_without_gap_or_dup(isolate_agent_runtime_root, monkeypatch):
    # Requirement 2: a tailer resumed across a rotation boundary reads
    # continuously — never silence, never duplicated events.
    monkeypatch.setenv(_CAP_ENV, "1")
    log = EventLog()
    for i in range(3):
        log.append(_evt(i))

    seen: list[str] = []
    resume_offset = 0
    generator = log.iter_from_offset(0)
    for _ in range(2):
        resume_offset, event = next(generator)
        seen.append(event.run_id)
    generator.close()

    # Force MORE rotations between the tailer's reads.
    for i in range(3, 8):
        log.append(_evt(i))

    for _offset, event in log.iter_from_offset(resume_offset):
        seen.append(event.run_id)

    assert seen == [f"r{i}" for i in range(8)]  # every event exactly once, in order


def test_open_reader_survives_concurrent_rotation_windows_safe(isolate_agent_runtime_root, monkeypatch):
    # Windows-safety proof: rotation seals in place (new file + manifest rewrite),
    # never renaming/replacing the live file. So appends that trigger a rotation
    # do NOT raise even while a reader holds the soon-sealed live file open — a
    # rename-based rotation would raise PermissionError (WinError 32) here.
    monkeypatch.setenv(_CAP_ENV, "800")
    log = EventLog()
    for i in range(4):
        log.append(_evt(i))

    generator = log.iter_from_offset(0)
    _first_offset, first = next(generator)  # opens + holds events.jsonl open
    assert first.run_id == "r0"

    # These appends grow events.jsonl past the cap and then rotate it — must not
    # raise while the generator still holds events.jsonl open.
    for i in range(4, 13):
        log.append(_evt(i))
    generator.close()

    assert event_rotation.slice_count() > 1  # a rotation really happened
    # Full continuity after the rotation, from a fresh cursor.
    assert _run_ids([e for _o, e in log.iter_from_offset(0)]) == [f"r{i}" for i in range(13)]


# ── every whole-log reader spans slices ──────────────────────────────────────


def test_for_task_and_for_session_span_slices(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")
    log = EventLog()
    # Interleave task "t", task "other", and a chat session across many slices.
    for i in range(6):
        log.append(_evt(i, task_id="t"))
        log.append(_evt(100 + i, task_id="other"))
        log.append(Event(ts=now(), type="run.tool.started", task_id=None, run_id=None,
                          persona_id="base", payload={"tool_name": "x"}, session_id="chat"))

    assert _run_ids(log.for_task("t")) == [f"r{i}" for i in range(6)]
    assert _run_ids(log.for_task("t", limit=2)) == ["r4", "r5"]  # newest-N across slices
    session_rows = log.for_session("chat")
    assert len(session_rows) == 6
    assert all(row.session_id == "chat" for row in session_rows)


def test_tail_spans_slices(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")  # 1 event per sealed slice → live has 1 line
    log = EventLog()
    for i in range(12):
        log.append(_evt(i))
    assert _run_ids(log.tail(5)) == [f"r{i}" for i in range(7, 12)]
    assert _run_ids(log.tail(1)) == ["r11"]


def test_iter_all_and_iter_since_span_slices(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")
    log = EventLog()
    first = _evt(0)
    log.append(first)
    for i in range(1, 6):
        log.append(_evt(i))
    assert _run_ids(list(log.iter_all())) == [f"r{i}" for i in range(6)]
    assert len(list(log.iter_since(first.ts))) == 6


def test_cached_event_log_matches_base_across_rotation(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")
    base = EventLog()
    for i in range(8):
        base.append(_evt(i, task_id="t"))
        base.append(Event(ts=now(), type="run.tool.started", task_id=None, run_id=None,
                          persona_id="base", payload={"tool_name": "x"}, session_id="chat"))

    cached = CachedEventLog()
    assert _run_ids(cached.for_task("t")) == _run_ids(base.for_task("t"))
    assert [e.type for e in cached.for_session("chat")] == [e.type for e in base.for_session("chat")]
    assert _run_ids(cached.tail(3)) == _run_ids(base.tail(3))
    assert len(list(cached.iter_all())) == len(list(base.iter_all()))
    # Logical offsets from the flat cache match the seeking base reader.
    assert [o for o, _e in cached.iter_from_offset(0)] == [o for o, _e in base.iter_from_offset(0)]


# ── health + compaction ──────────────────────────────────────────────────────


def test_event_log_health_accounts_rotation(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv(_CAP_ENV, "1")
    log = EventLog()
    for i in range(6):
        log.append(_evt(i))
    health = event_log_health()
    assert health["rotated_slice_count"] == 5
    assert health["line_count"] == 6  # whole-log total across slices
    assert health["live_slice_lines"] == 1
    assert health["log_end_offset"] == event_rotation.log_end_offset()
    assert health["exists"] is True


