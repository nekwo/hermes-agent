"""S3 of the canvas replication plan — the pull, adopt-or-hold, whole document.

Three-way merge on node positions has no natural resolution, so the granularity
is the whole drawing: adopt it, keep the local one, or HOLD loudly. Never merge,
never last-write-wins, and never edit the operator's drawing on the way in —
which is what the dangling-binding test below is really about.

The boundary a reviewer should check first: this pull does NOT run
``reconcile_flow_graph_steering``. Steering already travels on ``steered_by``,
and running ingest here would let a pulled drawing rewrite a peer's instance
records.
"""

from __future__ import annotations

import json

import yaml

from agent_runtime import paths
from agent_runtime.flow_graph import FlowGraphStore, parse_flow_graph_doc
from agent_runtime.flow_graph_sync import (
    FLOW_GRAPH_PROJECTION_RELATIVE_PATH,
    PROJECTION_KIND,
    PROJECTION_SCHEMA_VERSION,
    apply_flow_graph_pull,
    flow_graph_baseline_key,
    flow_graph_def_hash,
    read_flow_graph_baseline,
    write_flow_graph_baseline,
)

OWNER = "personainst_dev_agent_9682caf4"
GRAPH_ID = f"runtime_{OWNER}"


def _body(*, x: int = 10, agent: str | None = OWNER, extra_node: str | None = None) -> dict:
    nodes = [{"agent": agent, "id": "n_owner", "x": x, "y": 20}]
    if extra_node is not None:
        nodes.append({"agent": extra_node, "id": "n_child", "x": 200, "y": 40})
    edges = [{"from": "n_owner", "to": "n_child"}] if extra_node is not None else []
    return {"edges": edges, "graph_id": GRAPH_ID, "nodes": nodes}


def _publish(tmp_path, *bodies: dict):
    subtree = tmp_path / "subtree"
    (subtree / "store").mkdir(parents=True, exist_ok=True)
    document = {
        "graphs": {body["graph_id"]: body for body in bodies},
        "kind": PROJECTION_KIND,
        "schema_version": PROJECTION_SCHEMA_VERSION,
    }
    (subtree / "store" / "flow_graphs.yaml").write_text(
        yaml.safe_dump(document, sort_keys=True), encoding="utf-8"
    )
    return subtree


def _store_local(body: dict):
    return FlowGraphStore().set_doc(parse_flow_graph_doc(body), requested_by="operator")


def _local_doc():
    stored = FlowGraphStore().get(GRAPH_ID)
    return stored["doc"] if stored else None


def _baseline(realm_id: str, body: dict):
    write_flow_graph_baseline(realm_id, {flow_graph_baseline_key(GRAPH_ID): flow_graph_def_hash(body)})


# ── the adopt arm ─────────────────────────────────────────────────────────


def test_a_canvas_this_machine_never_had_is_adopted_whole(tmp_path):
    subtree = _publish(tmp_path, _body(extra_node=None))

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER})

    assert summary.adopted == [GRAPH_ID]
    assert summary.source == "projection"
    assert _local_doc()["nodes"][0]["x"] == 10
    assert read_flow_graph_baseline("realm_a")[flow_graph_baseline_key(GRAPH_ID)] == (
        flow_graph_def_hash(_body())
    )


def test_an_unchanged_local_canvas_takes_the_realms_edit(tmp_path):
    _store_local(_body(x=10))
    _baseline("realm_a", _body(x=10))
    subtree = _publish(tmp_path, _body(x=999))

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER})

    assert summary.adopted == [GRAPH_ID]
    assert _local_doc()["nodes"][0]["x"] == 999


def test_a_locally_edited_canvas_the_realm_did_not_touch_stays(tmp_path):
    _store_local(_body(x=77))
    _baseline("realm_a", _body(x=10))
    subtree = _publish(tmp_path, _body(x=10))

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER})

    assert summary.kept_local == [GRAPH_ID]
    assert _local_doc()["nodes"][0]["x"] == 77


# ── the hold ──────────────────────────────────────────────────────────────


def test_two_diverged_canvases_hold_rather_than_merge(tmp_path):
    """Two operators, two canvases, one graph id. A drawing has no natural
    three-way resolution, so the local one is untouched and the remote body is
    parked where the operator can read it."""
    _store_local(_body(x=77))
    _baseline("realm_a", _body(x=10))
    subtree = _publish(tmp_path, _body(x=999))

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER})

    assert summary.held == [GRAPH_ID]
    assert summary.adopted == []
    assert _local_doc()["nodes"][0]["x"] == 77
    sidecar = json.loads(paths.flow_graph_conflict_path("realm_a", GRAPH_ID).read_text("utf-8"))
    assert sidecar["remote_body"]["nodes"][0]["x"] == 999
    assert sidecar["graph_id"] == GRAPH_ID


def test_a_canvas_the_realm_stopped_publishing_is_never_deleted(tmp_path):
    """Absence is short-answer-shaped, and a canvas is the only record of a map
    somebody drew by hand. The reap lane archives on OWNER liveness; nothing in
    this pull removes a drawing."""
    _store_local(_body(x=77))
    _baseline("realm_a", _body(x=77))
    subtree = _publish(tmp_path)

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER})

    assert summary.upstream_absent == [GRAPH_ID]
    assert _local_doc()["nodes"][0]["x"] == 77
    assert read_flow_graph_baseline("realm_a") != {}


# ── dangling bindings ─────────────────────────────────────────────────────


def test_a_pulled_canvas_with_an_absent_agent_is_written_whole_and_reported(tmp_path):
    """A node whose agent has not arrived yet is an AUTHORED node with a binding
    this machine cannot resolve YET — the instance may land on the next pull.
    Dropping it would silently edit the operator's drawing, which is the exact
    failure this whole lane exists to close."""
    subtree = _publish(tmp_path, _body(extra_node="personainst_ghost_agent_1"))

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER})

    assert summary.adopted == [GRAPH_ID]
    assert [node["id"] for node in _local_doc()["nodes"]] == ["n_owner", "n_child"]
    assert summary.unbound_node_agents == [
        {"graph_id": GRAPH_ID, "node": "n_child", "agent": "personainst_ghost_agent_1"}
    ]


# ── refusals and version skew ─────────────────────────────────────────────


def test_a_remote_body_the_parser_rejects_is_refused_not_written(tmp_path):
    """Every stored canvas has passed ``parse_flow_graph_doc``; a pulled one
    does not get to skip that door."""
    broken = {"graph_id": GRAPH_ID, "nodes": [{"id": "a"}], "edges": [{"from": "a", "to": "nope"}]}
    subtree = _publish(tmp_path, broken)

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER})

    assert summary.adopted == []
    assert summary.refused[0]["key"] == GRAPH_ID
    assert FlowGraphStore().get(GRAPH_ID) is None


def test_an_older_publisher_is_absence_not_an_empty_realm(tmp_path):
    subtree = tmp_path / "subtree"
    subtree.mkdir(parents=True, exist_ok=True)

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids=set())

    assert summary.source is None
    assert summary.as_dict()["source"] is None
    assert summary.changed is False


def test_a_projection_that_will_not_decode_is_a_refusal_not_absence(tmp_path):
    subtree = tmp_path / "subtree"
    (subtree / "store").mkdir(parents=True, exist_ok=True)
    (subtree / "store" / "flow_graphs.yaml").write_text("{ not: [yaml", encoding="utf-8")

    summary = apply_flow_graph_pull("realm_a", subtree, live_instance_ids=set())

    assert summary.source == "unreadable"
    assert summary.refused[0]["key"] == FLOW_GRAPH_PROJECTION_RELATIVE_PATH


def test_the_pull_never_rewrites_steering(monkeypatch, tmp_path):
    """THE boundary of this stage. Steering travels on ``steered_by`` in the
    instance family; a pulled drawing must not reach a peer's instance records."""
    from agent_runtime import flow_graph as flow_graph_mod

    def _explode(*_args, **_kwargs):
        raise AssertionError("the canvas pull must not run steering ingest")

    monkeypatch.setattr(flow_graph_mod, "reconcile_flow_graph_steering", _explode)
    subtree = _publish(tmp_path, _body(extra_node="personainst_other_agent_2"))

    assert apply_flow_graph_pull("realm_a", subtree, live_instance_ids={OWNER}).adopted == [GRAPH_ID]
