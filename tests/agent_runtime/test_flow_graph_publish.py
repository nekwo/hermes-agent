"""S2 of the canvas replication plan — the publish arm.

The canvas travels with the desk it belongs to: resolved for exactly the
instance ids the office walk already produced, never a second enumeration of the
graph store. The two facts this stage has to make true, and the second is the
one that keeps every existing publish golden still: a realm with a stored canvas
carries ``store/flow_graphs.yaml``, and a realm with none publishes exactly what
it published before.

Autouse conftest fixtures isolate the runtime root; no test here touches a live
store.
"""

from __future__ import annotations

import yaml

from agent_runtime import paths
from agent_runtime.flow_graph import FlowGraphStore, parse_flow_graph_doc
from agent_runtime.flow_graph_sync import (
    FLOW_GRAPH_PROJECTION_RELATIVE_PATH,
    PROJECTION_KIND,
    flow_graph_baseline_key,
    flow_graph_def_hash,
    read_flow_graph_baseline,
    update_flow_graph_baseline_after_publish,
)
from agent_runtime.office_store import OfficeStore
from agent_runtime.realm_sync import (
    _destination_for_sync_path,
    _flow_graph_row,
    _kind_for_sync_path,
    _resolve_artifacts_with_projection,
    resolve_realm_sync_artifacts,
)
from agent_runtime.store import RealmStore, WorkspaceStore

INSTANCE_ID = "personainst_dev_agent_9682caf4"


def _make_realm_workspace() -> tuple[str, str]:
    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    return realm.id, ws.id


def _payload(persona_id: str = "dev", *, instance_id: str | None = INSTANCE_ID) -> dict:
    payload: dict = {
        "persona_id": persona_id,
        "items": [
            {
                "item_id": persona_id,
                "persona_id": persona_id,
                "kind": "agent",
                "position": [1.0, 2.0],
                "folder": "Agents",
            }
        ],
    }
    if instance_id is not None:
        payload["persona_instance_id"] = instance_id
    return payload


def _store_canvas(owner_instance_id: str = INSTANCE_ID, *, viewport=True) -> dict:
    """A canvas the way the launcher saves one: two nodes with layout, one edge,
    and the viewport the operator's window happened to sit at."""

    doc: dict = {
        "graph_id": f"runtime:{owner_instance_id}",
        "nodes": [
            {"id": "n_owner", "x": 10, "y": 20, "agent": owner_instance_id},
            {"id": "n_child", "x": 200, "y": 40, "agent": None},
        ],
        "edges": [{"from": "n_owner", "to": "n_child"}],
    }
    if viewport:
        doc["viewport"] = {"x": -12.5, "y": 3.0, "zoom": 0.75}
    return FlowGraphStore().set_doc(parse_flow_graph_doc(doc), requested_by="operator")


def _published_document(realm_id: str) -> dict | None:
    for artifact in resolve_realm_sync_artifacts(realm_id):
        if artifact.relative_path == FLOW_GRAPH_PROJECTION_RELATIVE_PATH:
            return yaml.safe_load(artifact.read_bytes().decode("utf-8"))
    return None


# ── the artifact ──────────────────────────────────────────────────────────


def test_a_desk_with_a_stored_canvas_publishes_it():
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _store_canvas()

    document = _published_document(realm_id)

    assert document is not None
    assert document["kind"] == PROJECTION_KIND
    graph = document["graphs"][f"runtime_{INSTANCE_ID}"]
    assert [node["id"] for node in graph["nodes"]] == ["n_owner", "n_child"]
    assert graph["nodes"][0]["x"] == 10
    assert graph["edges"] == [{"from": "n_owner", "to": "n_child"}]
    assert "viewport" not in graph


def test_a_realm_that_never_drew_a_canvas_publishes_exactly_what_it_did_before():
    """The artifact is appended only when the projection is non-empty, so every
    existing publish golden for a graph-less realm stays byte-identical."""
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))

    assert _published_document(realm_id) is None
    assert _resolve_artifacts_with_projection(realm_id).flow_graph_projection.graphs == {}


def test_a_canvas_owned_by_a_desk_outside_this_publish_does_not_travel():
    """Scope is exactly the office scan's instance ids. A canvas for an agent
    this realm does not place is not this realm's to ship."""
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _store_canvas("personainst_other_agent_deadbeef")

    assert _published_document(realm_id) is None


def test_the_publish_row_names_the_viewport_it_dropped():
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _store_canvas()

    resolved = _resolve_artifacts_with_projection(realm_id)
    row = _flow_graph_row(resolved.flow_graph_projection)

    assert row["graphs"] == [f"runtime_{INSTANCE_ID}"]
    assert any(key.endswith(".viewport") for key in row["dropped_keys"])
    assert row["unreadable"] == []


# ── path classification + baseline ────────────────────────────────────────


def test_the_projection_path_classifies_as_its_own_kind_and_no_older_member_writes_it():
    assert _kind_for_sync_path(FLOW_GRAPH_PROJECTION_RELATIVE_PATH) == "flow_graph_config"
    assert _kind_for_sync_path("store/persona_instances.yaml") == "persona_instance_config"
    assert _destination_for_sync_path(FLOW_GRAPH_PROJECTION_RELATIVE_PATH) is None


def test_the_publish_baseline_leaves_the_publisher_with_nothing_to_hold():
    """Without it, a member who publishes and then pulls reads the canvas they
    just shipped as locally-edited-and-remotely-changed — a hold on their own
    publish, which for a drawing means a conflict sidecar over nothing."""
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _store_canvas()

    resolved = _resolve_artifacts_with_projection(realm_id)
    assert read_flow_graph_baseline(realm_id) == {}
    update_flow_graph_baseline_after_publish(realm_id, resolved.flow_graph_projection)

    graph_id = f"runtime_{INSTANCE_ID}"
    baseline = read_flow_graph_baseline(realm_id)
    assert baseline[flow_graph_baseline_key(graph_id)] == flow_graph_def_hash(
        resolved.flow_graph_projection.graphs[graph_id]
    )


def test_the_baseline_sidecar_lives_outside_the_store_the_publish_walks():
    realm_id, _ = _make_realm_workspace()
    path = paths.flow_graph_baseline_path(realm_id)

    assert paths.realm_sync_root() in path.parents
    assert paths.store_root() / "flow_graphs" not in path.parents
