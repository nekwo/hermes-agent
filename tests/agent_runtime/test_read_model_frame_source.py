"""B4 — the snapshot serve path answers WHERE its frame came from.

Before this slice ``_cmd_snapshot`` replaced the freshly built frame with
``ReadModel().render_snapshot()`` whenever the read-model flags were on, with no
check that the read model held anything. A database that had never been
populated (first run after enabling the flag, or one just cleared by a schema
migration) therefore made ``harness snapshot --json`` print ``{}`` — an empty
runtime, indistinguishable from a real one with nothing in it, with no error and
exit code 0. These tests pin the three outcomes as typed, distinguishable
sources.
"""

from __future__ import annotations

import json
from argparse import Namespace

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.read_model import (
    FrameSource,
    ReadModel,
    resolve_snapshot_frame,
    snapshot_watermark,
)
from agent_runtime.runtime_config import ReadModelConfig
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import build_snapshot


def test_prefer_cache_off_serves_the_built_frame(isolate_agent_runtime_root):
    resolved = resolve_snapshot_frame(prefer_cache=False)

    assert resolved.source is FrameSource.BUILT
    assert resolved.frame["schema_version"] == 2
    assert resolved.frame["parity"]["frame_source"] == "built"
    # NamedTuple: the (frame, source) shape the spec names still unpacks.
    frame, source = resolved
    assert frame is resolved.frame and source is resolved.source


def test_empty_read_model_never_serves_an_empty_frame(isolate_agent_runtime_root):
    read_model = ReadModel(isolate_agent_runtime_root / "empty.db")
    assert read_model.render_snapshot() is None

    resolved = resolve_snapshot_frame(prefer_cache=True, read_model=read_model)

    assert resolved.source is FrameSource.CACHE_MISS_REBUILT
    assert resolved.frame["parity"]["frame_source"] == "cache_miss_rebuilt"
    # The degraded answer is the REAL runtime, not an empty object.
    assert resolved.frame["schema_version"] == 2
    assert resolved.frame["generated_at"]


def test_populated_read_model_serves_the_cache(isolate_agent_runtime_root):
    read_model = ReadModel(isolate_agent_runtime_root / "populated.db")
    seeded = build_snapshot()
    read_model.apply_full_rebuild(seeded)

    resolved = resolve_snapshot_frame(prefer_cache=True, read_model=read_model)

    assert resolved.source is FrameSource.CACHE
    assert resolved.frame["parity"]["frame_source"] == "cache"
    assert resolved.frame["generated_at"] == to_jsonable(seeded["generated_at"])


def test_cmd_snapshot_reports_a_cache_miss_instead_of_printing_nothing(
    isolate_agent_runtime_root, monkeypatch, capsys
):
    """The end-to-end regression: flags on, read model empty, non-empty output."""

    import hermes_cli.harness as harness
    from agent_runtime.read_model import ReadModel

    # One config, one scope: since RD-H6's resolver swap reached write_snapshot
    # (2026-08-17) the old construction — write-side OFF, serve-side ON via two
    # patched loaders — is impossible BY DESIGN; the config can no longer
    # disagree with itself. The un-migrated-database shape is still real, so it
    # is constructed honestly instead: flags ON at the one scope, and the
    # projector's apply is a no-op (a db that exists but was never populated —
    # corrupt, foreign-schema, or pre-migration).
    cfg = AgentRuntimeConfig(read_model=ReadModelConfig(enabled=True, serve_snapshot_from_db=True))
    monkeypatch.setattr("agent_runtime.config.load_root_runtime_config", lambda: cfg)
    monkeypatch.setattr(ReadModel, "apply_full_rebuild", lambda self, snapshot: None)

    assert harness._cmd_snapshot(Namespace(json=True)) == 0

    printed = json.loads(capsys.readouterr().out)
    assert printed != {}
    assert printed["parity"]["frame_source"] == "cache_miss_rebuilt"
    assert printed["schema_version"] == 2


def test_watermark_helper_is_the_only_fallback(isolate_agent_runtime_root):
    """Both former write-site fallbacks now resolve through one helper.

    ``write_snapshot`` fell back to ``{}`` and the projector to
    ``events_watermark()``, so a frame missing its parity watermark was recorded
    as caught-up-at-offset-0 by one writer and correctly by the other.
    """

    snapshot = build_snapshot()
    frame_watermark = snapshot["parity"]["watermark"]

    assert snapshot_watermark(snapshot)["event_offset"] == frame_watermark["event_offset"]

    # No parity block at all: a measured watermark, never a zeroed one.
    measured = snapshot_watermark({"generated_at": "x"})
    assert measured["event_offset"] == frame_watermark["event_offset"]
    assert measured["captured_at"]

    # An explicit override still wins, and still gets the required keys.
    override = snapshot_watermark(snapshot, override={"event_offset": 7})
    assert override["event_offset"] == 7
    assert "last_event_ts" in override and "captured_at" in override


def test_both_write_sites_record_the_same_watermark(isolate_agent_runtime_root):
    from agent_runtime.projector import Projector
    from agent_runtime.runtime_config import RuntimeConfig

    via_snapshot = ReadModel(isolate_agent_runtime_root / "via_snapshot.db")
    via_projector = ReadModel(isolate_agent_runtime_root / "via_projector.db")
    snapshot = build_snapshot()

    via_snapshot.apply_full_rebuild(snapshot)
    Projector(
        via_projector,
        config=RuntimeConfig(read_model=ReadModelConfig(enabled=True)),
    ).full_rebuild()

    left = via_snapshot.projection_watermark("snapshot")
    right = via_projector.projection_watermark("snapshot")
    assert left["event_offset"] == right["event_offset"]


def test_frame_is_stored_exactly_once(isolate_agent_runtime_root):
    """B4 duplication guard: one row for the frame, not one per section.

    ``apply_full_rebuild`` used to write the whole frame as the ``snapshot``
    blob and then again as one ``projections_misc`` row per top-level section —
    every byte stored twice, with the copies free to drift because only the blob
    was ever re-read as a whole.
    """

    read_model = ReadModel(isolate_agent_runtime_root / "read_model.db")
    snapshot = build_snapshot()
    read_model.apply_full_rebuild(snapshot)

    with read_model.connect() as conn:
        projections = [row[0] for row in conn.execute("SELECT projection FROM projections_misc")]
    assert projections == ["snapshot"]

    # Sections stay readable — sliced out of the one blob.
    for section in ("boards", "parity", "runtime_config"):
        assert read_model.read_projection(section)["payload"] is not None


def test_schema_bump_clears_a_database_written_by_the_old_layout(isolate_agent_runtime_root):
    """An old database rebuilds rather than serving unmaintained rows."""

    import sqlite3

    from agent_runtime.read_model import READ_MODEL_SCHEMA_VERSION

    db_path = isolate_agent_runtime_root / "old_layout.db"
    read_model = ReadModel(db_path)
    read_model.apply_full_rebuild(build_snapshot())

    # Rewind to the pre-B4 version and plant a duplicate per-section row of the
    # kind the old writer emitted.
    with sqlite3.connect(db_path) as raw:
        raw.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '2')")
        raw.execute("INSERT OR REPLACE INTO projections_misc(projection, payload) VALUES('boards', '{}')")
        raw.commit()

    reopened = ReadModel(db_path)
    assert reopened.render_snapshot() is None
    assert reopened.projection_watermark("snapshot") is None
    with reopened.connect() as conn:
        assert [row[0] for row in conn.execute("SELECT projection FROM projections_misc")] == []
        stored = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
    assert stored["schema_version"] == str(READ_MODEL_SCHEMA_VERSION)
    assert stored["schema_migrated_from"] == "2"
