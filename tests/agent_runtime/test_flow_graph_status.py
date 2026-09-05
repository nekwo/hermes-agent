"""S5 of the canvas replication plan — the accounting surface.

Every other sync family accounts for what it dropped and what it held; this one
now does too, so "the chart is empty because the drawing did not travel / is
held / would not read" is answerable from the status envelope.

The stage plan assumed a fourth ``store_drift`` family and S5 shipped a
top-level key instead, for a measured reason: ``store_drift`` rows are exactly
the set ``realm_revert`` addresses, and it sorts them through
``_PROCESS_ORDER[row.family]`` — a direct subscript that raises for a family
with no revert arm. A count without a revert arm is honest; a drift row
offering an exit that does not exist is not.

The revert arm landed 2026-09-05 (w17/hb) and the drift rows landed with it, so
the family is now BOTH — and the two answer different questions. This file owns
the top-level row: ``publishable`` (how many drawings this realm ships),
``held`` (conflict sidecars, which are not drift and must not be reverted away)
and ``unreadable`` (a canvas with no revertable row by construction). The drift
rows and their revert arm are ``tests/agent_runtime/test_realm_revert.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_runtime import paths
from agent_runtime.flow_graph import FlowGraphStore, parse_flow_graph_doc
from agent_runtime.flow_graph_sync import update_flow_graph_baseline_after_publish
from agent_runtime.office_store import OfficeStore
from agent_runtime.realm_sync import (
    _flow_graph_status_row,
    _resolve_artifacts_with_projection,
    realm_sync_status,
)
from agent_runtime.store import RealmStore, WorkspaceStore

INSTANCE_ID = "personainst_dev_agent_9682caf4"
GRAPH_ID = f"runtime_{INSTANCE_ID}"


def _realm_with_desk(tmp_path: Path):
    realm = RealmStore().create(name="Canvas Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "realm-sync-repo"],
        check=True,
        capture_output=True,
        text=True,
    )
    realm.sync_manifest_ref = str(tmp_path / "realm-sync-repo")
    realm = RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    OfficeStore().upsert_actor(
        ws.id,
        {
            "persona_id": "dev",
            "persona_instance_id": INSTANCE_ID,
            "items": [
                {
                    "item_id": "dev",
                    "persona_id": "dev",
                    "kind": "agent",
                    "position": [1.0, 2.0],
                    "folder": "Agents",
                }
            ],
        },
    )
    return realm, ws


def _store_canvas(x: int = 10):
    FlowGraphStore().set_doc(
        parse_flow_graph_doc(
            {
                "graph_id": f"runtime:{INSTANCE_ID}",
                "nodes": [{"id": "n_owner", "agent": INSTANCE_ID, "x": x, "y": 2}],
                "edges": [],
            }
        ),
        requested_by="operator",
    )


def _workspaces(realm):
    return [WorkspaceStore().get(ws_id) for ws_id in realm.workspace_ids]


def test_a_drawn_but_unpublished_canvas_is_counted(tmp_path):
    realm, _ = _realm_with_desk(tmp_path)
    _store_canvas()

    row = _flow_graph_status_row(realm.id, _workspaces(realm))

    assert row["publishable"] == 1
    assert row["unpublished"] == 1
    assert row["held"] == []


def test_a_published_canvas_stops_reading_as_unpublished(tmp_path):
    realm, _ = _realm_with_desk(tmp_path)
    _store_canvas()
    resolved = _resolve_artifacts_with_projection(realm.id)
    update_flow_graph_baseline_after_publish(realm.id, resolved.flow_graph_projection)

    row = _flow_graph_status_row(realm.id, _workspaces(realm))

    assert row["publishable"] == 1
    assert row["unpublished"] == 0


def test_a_held_canvas_is_named_by_its_conflict_sidecar(tmp_path):
    realm, _ = _realm_with_desk(tmp_path)
    _store_canvas()
    sidecar = paths.flow_graph_conflict_path(realm.id, GRAPH_ID)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text("{}", encoding="utf-8")

    assert _flow_graph_status_row(realm.id, _workspaces(realm))["held"] == [GRAPH_ID]


def test_the_status_envelope_carries_the_row(tmp_path):
    """End to end through the verb an operator runs, not a helper signature."""
    realm, _ = _realm_with_desk(tmp_path)
    _store_canvas()

    envelope = realm_sync_status(realm.id)

    assert envelope["flow_graphs"]["publishable"] == 1
    assert envelope["flow_graphs"]["unpublished"] == 1
    # Since the revert arm landed (w17/hb) the canvas IS a store_drift family,
    # so an unpublished drawing moves ``unpublished_changes`` — the flag whose
    # exits are Publish and Revert, and the canvas now has both.
    assert envelope["store_drift"]["flow_graphs"]["canvases_added"] == 1
    assert envelope["unpublished_changes"] is True
    # The two totals are not the same arithmetic and must not be conflated:
    # ``unpublished`` counts drawings the realm has not seen (added + changed),
    # while a reaped canvas is a drift ROW with nothing left to publish.
    assert envelope["flow_graphs"]["unpublished"] == (
        envelope["store_drift"]["flow_graphs"]["canvases_added"]
        + envelope["store_drift"]["flow_graphs"]["canvases_changed"]
    )
