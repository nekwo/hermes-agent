from __future__ import annotations

import os
import json
import sqlite3
import subprocess
import sys
import time
from argparse import Namespace

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.read_model import ReadModel
from agent_runtime.runtime_config import ReadModelConfig
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import build_snapshot, write_snapshot

from test_read_model_slo import SLO_FULL_BUILD_MS, _seed_synthetic_runtime


def test_apply_full_rebuild_then_render_is_equivalent(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    snapshot = build_snapshot()

    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])

    rendered = read_model.render_snapshot()
    assert to_jsonable(rendered) == to_jsonable(snapshot)
    assert read_model.projection_watermark("snapshot")["event_offset"] == snapshot["parity"]["watermark"]["event_offset"]
    assert read_model.read_projection("agent_instances")["rows"] == []
    # B4: sections are read as slices of the one stored frame, not from
    # duplicate per-section rows.
    assert read_model.read_projection("parity")["payload"] == to_jsonable(snapshot["parity"])
    assert read_model.read_projection("snapshot")["payload"] == to_jsonable(snapshot)


def test_wal_crash_mid_transaction_leaves_db_consistent(isolate_agent_runtime_root, tmp_path):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    snapshot = build_snapshot()
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])
    old_watermark = read_model.projection_watermark("snapshot")
    marker = tmp_path / "writer-started.txt"

    script = (
        "import sqlite3, time\n"
        f"conn = sqlite3.connect({str(read_model.db_path)!r})\n"
        "conn.execute('PRAGMA journal_mode=WAL')\n"
        "conn.execute('BEGIN IMMEDIATE')\n"
        "conn.execute(\"UPDATE projection_watermarks SET event_offset = 999999 WHERE projection = 'snapshot'\")\n"
        "conn.execute('DELETE FROM agent_instances')\n"
        f"open({str(marker)!r}, 'w', encoding='utf-8').write('started')\n"
        "time.sleep(30)\n"
    )
    env = dict(os.environ)
    env["HERMES_AGENT_RUNTIME_ROOT"] = str(isolate_agent_runtime_root)
    proc = subprocess.Popen([sys.executable, "-c", script], env=env)
    try:
        deadline = time.time() + 5
        while not marker.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert marker.exists()
    finally:
        proc.kill()
        proc.wait(timeout=5)

    reopened = ReadModel(isolate_agent_runtime_root / "read_model.db")
    assert reopened.render_snapshot() is not None
    assert reopened.projection_watermark("snapshot") == old_watermark
    assert reopened.read_projection("agent_instances")["rows"] == []
    with sqlite3.connect(reopened.db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_flag_off_is_inert(isolate_agent_runtime_root, monkeypatch):
    import agent_runtime.snapshot as snapshot_mod

    cfg = AgentRuntimeConfig(read_model=ReadModelConfig(enabled=False))
    # write_snapshot reads read_model.enabled from the ROOT config (pinned).
    monkeypatch.setattr(snapshot_mod, "load_root_runtime_config", lambda: cfg)

    snapshot = write_snapshot(build_snapshot())

    assert snapshot["schema_version"] == 2
    assert (isolate_agent_runtime_root / "snapshot.json").exists()
    assert not (isolate_agent_runtime_root / "read_model.db").exists()


def test_flag_on_dual_writes_read_model(isolate_agent_runtime_root, monkeypatch):
    import agent_runtime.snapshot as snapshot_mod

    cfg = AgentRuntimeConfig(read_model=ReadModelConfig(enabled=True))
    # write_snapshot reads read_model.enabled from the ROOT config (pinned).
    monkeypatch.setattr(snapshot_mod, "load_root_runtime_config", lambda: cfg)

    snapshot = write_snapshot(build_snapshot())

    assert (isolate_agent_runtime_root / "read_model.db").exists()
    assert ReadModel(isolate_agent_runtime_root / "read_model.db").render_snapshot()["generated_at"] == to_jsonable(snapshot)["generated_at"]


def test_harness_snapshot_serves_from_read_model_when_enabled(isolate_agent_runtime_root, monkeypatch, capsys):
    import agent_runtime.snapshot as snapshot_mod
    import hermes_cli.harness as harness

    cfg = AgentRuntimeConfig(read_model=ReadModelConfig(enabled=True, serve_snapshot_from_db=True))
    # Both read_model policy reads are pinned to the ROOT config: write_snapshot
    # binds snapshot_mod.load_root_runtime_config at import; _cmd_snapshot does a
    # function-local `from agent_runtime.config import load_root_runtime_config`
    # resolved from the config module at call time.
    monkeypatch.setattr(snapshot_mod, "load_root_runtime_config", lambda: cfg)
    monkeypatch.setattr("agent_runtime.config.load_root_runtime_config", lambda: cfg)

    assert harness._cmd_snapshot(Namespace(json=True)) == 0

    printed = json.loads(capsys.readouterr().out)
    rendered = ReadModel(isolate_agent_runtime_root / "read_model.db").render_snapshot()
    # The serve stamps its own provenance onto the frame it hands back; the rest
    # of the frame is the cached one, byte for byte.
    assert printed["parity"].pop("frame_source") == "cache"
    assert to_jsonable(rendered) == printed


def test_render_budget(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    snapshot = build_snapshot()
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    read_model.apply_full_rebuild(snapshot, watermark=snapshot["parity"]["watermark"])

    started = time.perf_counter()
    rendered = read_model.render_snapshot()
    render_ms = int((time.perf_counter() - started) * 1000)

    assert rendered["parity"]["watermark"]["event_offset"] == snapshot["parity"]["watermark"]["event_offset"]
    assert render_ms <= SLO_FULL_BUILD_MS


# ── an unknown watermark must not persist or read as byte 0 ───────────────
#
# ``int(watermark.get("event_offset") or 0)`` folded "position unknown" into
# "position 0" at three sites: the derivation fallback, the stored projection
# watermark, and the caught-up test every cursor is answered from.


def test_snapshot_watermark_does_not_invent_offset_zero():
    from agent_runtime.read_model import snapshot_watermark

    resolved = snapshot_watermark(
        {"parity": {"watermark": {"last_event_ts": "2026-08-09T00:00:00Z"}}},
        override={"last_event_ts": "2026-08-09T00:00:00Z"},
    )

    assert resolved["event_offset"] is None


def test_unknown_watermark_is_not_stored_as_a_position(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    snapshot = build_snapshot()
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")

    read_model.apply_full_rebuild(snapshot, watermark={"event_offset": 4096})
    assert read_model.projection_watermark("snapshot")["event_offset"] == 4096

    read_model.apply_full_rebuild(snapshot, watermark={"event_offset": None})

    # The absent row IS this table's typed unknown; a 0 here would be a cursor
    # at the head of the log that every reader would believe.
    assert read_model.projection_watermark("snapshot") is None
    # The frame itself still serves — losing the position is not losing the data.
    assert read_model.render_snapshot() is not None


def test_unknown_watermark_never_answers_caught_up(isolate_agent_runtime_root):
    _seed_synthetic_runtime(isolate_agent_runtime_root)
    snapshot = build_snapshot()
    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    read_model.apply_full_rebuild(snapshot, watermark={"event_offset": None})

    result = read_model.read_projection("snapshot", since_offset=999999)

    # "<= since_offset" is a claim to be CAUGHT UP; it may not be made from a
    # position that was never recorded.
    assert result["payload"] is not None
    assert result["watermark"] is None
