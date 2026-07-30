"""Stage S5 — per-actor checkpoint fetch (the store IS the checkpoint).

These drive the REAL stores against the autouse isolated runtime root
(``tests/agent_runtime/conftest.py``): persona instances and a flow-graph doc
are persisted through their own store code, then bundled by
``build_checkpoint`` and asserted to appear keyed by actor id within their
entity class. Corrupt-file tolerance, class filtering, truncation accounting,
and watermark consistency with the append-only event log are each exercised.
"""

from __future__ import annotations

import json
import os

from agent_runtime import paths
from agent_runtime.checkpoint import (
    CHECKPOINT_VERSION,
    build_checkpoint,
    class_manifest,
    discover_classes,
)
from agent_runtime.events import EventLog
from agent_runtime.flow_graph import ingest_flow_graph
from agent_runtime.persona_assignments import PersonaInstanceStore


def _seed_actors():
    """Persist one of each tested class through the REAL store, return their ids."""

    store = PersonaInstanceStore()
    lead = store.create_free_floating("profile:lead")
    dev = store.create_free_floating("profile:dev")

    graph = {
        "graph_id": "chart_main",
        "nodes": [
            {"id": "n1", "agent": lead.id, "x": 0, "y": 0},
            {"id": "n2", "agent": dev.id, "x": 1, "y": 1},
        ],
        "edges": [{"from": "n1", "to": "n2"}],
    }
    ingest_flow_graph(json.dumps(graph))
    return lead, dev, "chart_main"


def test_checkpoint_bundles_actors_keyed_by_id():
    lead, dev, graph_id = _seed_actors()

    checkpoint = build_checkpoint()

    assert checkpoint["checkpoint_version"] == CHECKPOINT_VERSION
    classes = checkpoint["classes"]

    # persona_instances: keyed by instance id, rows read verbatim.
    assert lead.id in classes["persona_instances"]
    assert dev.id in classes["persona_instances"]
    assert classes["persona_instances"][lead.id]["id"] == lead.id

    # flow_graphs: the authored chart doc, keyed by graph id, stored verbatim.
    assert graph_id in classes["flow_graphs"]
    assert classes["flow_graphs"][graph_id]["graph_id"] == graph_id

    # counts mirror the bundled rows; discovery lists what was found.
    assert checkpoint["counts"]["persona_instances"] == 2
    for name in ("persona_instances", "flow_graphs"):
        assert name in discover_classes()
    assert checkpoint["bytes_estimate"] > 0


def test_checkpoint_class_filter_and_absent_accounting():
    _seed_actors()

    only_instances = build_checkpoint(classes=["persona_instances"])
    assert set(only_instances["classes"]) == {"persona_instances"}
    assert "flow_graphs" not in only_instances["classes"]

    # An unknown / absent requested class is accounted, never silently dropped.
    with_absent = build_checkpoint(classes=["persona_instances", "nonexistent_class"])
    assert "persona_instances" in with_absent["classes"]
    assert with_absent["requested_absent"] == ["nonexistent_class"]


def test_checkpoint_tolerates_corrupt_file():
    _seed_actors()
    # Drop a corrupt file into the graph store — it must become a typed
    # unreadable row, and the good rows must still bundle (no abort).
    graph_dir = paths.store_root() / "flow_graphs"
    (graph_dir / "graph_broken.json").write_text("{ not json", encoding="utf-8")

    checkpoint = build_checkpoint(classes=["flow_graphs"])
    graphs = checkpoint["classes"]["flow_graphs"]

    assert graphs["graph_broken"]["unreadable"] is True
    assert "error" in graphs["graph_broken"]
    # The healthy actor is untouched by its corrupt neighbour.
    assert "chart_main" in graphs
    assert graphs["chart_main"].get("unreadable") is None


def test_checkpoint_row_cap_truncation_is_accounted():
    store = PersonaInstanceStore()
    for template in ("profile:a", "profile:b", "profile:c"):
        store.create_free_floating(template)

    capped = build_checkpoint(classes=["persona_instances"], row_cap=2)

    assert capped["counts"]["persona_instances"] == 2
    assert len(capped["classes"]["persona_instances"]) == 2
    truncation = capped["truncations"]["persona_instances"]
    assert truncation == {"truncated": True, "total": 3, "returned": 2}


def test_checkpoint_watermark_is_consistent_with_event_log():
    _seed_actors()  # the store writes append events, advancing the log

    checkpoint = build_checkpoint()
    watermark = checkpoint["watermark"]

    # The watermark reuses the parity/snapshot authority: event_offset is the
    # append-only log's byte size — the exact cursor a tailer resumes from.
    assert watermark["event_offset"] == os.path.getsize(paths.events_path())
    assert watermark["last_event_ts"] is not None
    # Orderable against the log: nothing remains past the watermark offset.
    assert list(EventLog().iter_from_offset(watermark["event_offset"])) == []


def test_class_manifest_counts_without_reading_rows():
    lead, dev, _graph_id = _seed_actors()

    manifest = class_manifest()

    by_name = {entry["class"]: entry for entry in manifest["classes"]}
    assert by_name["persona_instances"]["count"] == 2
    assert by_name["persona_instances"]["bytes"] > 0
    assert "persona_instances" in manifest["discovered"]
    assert manifest["watermark"]["event_offset"] == os.path.getsize(paths.events_path())
