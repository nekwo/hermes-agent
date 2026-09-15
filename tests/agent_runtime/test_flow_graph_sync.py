"""S1 of the flow-graph canvas replication plan — the projection and its accounting.

The operator's drawing travels: nodes with their layout, and every edge in the
order it was drawn. What deliberately does NOT travel is the point of these
tests: the viewport is a VIEW preference, the store envelope's ``updated_at`` /
``requested_by`` are local provenance, and an unknown key is an unreviewed field
that must never reach the wire. Each of those is DROPPED WITH ACCOUNTING — an
unaccounted drop is the same silence the replication row exists to close.

Plan: `docs/agent-runtime-harness/planned/w13-h2-flow-graph-canvas-replication.md`.
"""

import pytest

from agent_runtime import flow_graph_sync as fgs


def _stored(graph_id="runtime_alpha", *, doc=None, **envelope):
    body = {
        "graph_id": graph_id,
        "nodes": [{"id": "n1", "x": 10, "y": 20, "agent": "personainst_a"}],
        "edges": [],
    }
    if doc is not None:
        body = doc
    return {
        "graph_id": graph_id,
        "doc": body,
        "updated_at": "2026-09-04T00:00:00Z",
        "requested_by": "operator",
        **envelope,
    }


def test_the_viewport_is_dropped_and_named_on_the_row():
    """The launcher's own contract calls the viewport a view preference, never a
    steering fact. It must not travel, and the publish row must SAY it did not."""
    dropped: list[str] = []
    doc = {
        "graph_id": "runtime_alpha",
        "nodes": [],
        "edges": [],
        "viewport": {"x": 1.0, "y": 2.0, "zoom": 0.5},
    }

    body = fgs.project_flow_graph(_stored(doc=doc), dropped=dropped)

    assert "viewport" not in body
    assert "graphs.runtime_alpha.viewport" in dropped


def test_local_provenance_is_dropped_and_named():
    """``updated_at`` is the local write clock — carrying it would make every
    hash differ on every save — and ``requested_by`` is who asked HERE."""
    dropped: list[str] = []

    fgs.project_flow_graph(_stored(), dropped=dropped)

    assert "graphs.runtime_alpha.updated_at" in dropped
    assert "graphs.runtime_alpha.requested_by" in dropped


def test_node_layout_travels_and_unknown_node_keys_are_named():
    """x/y ARE the ruling's layout, so they travel. A key the allowlist does not
    know is an unreviewed field, dropped with its node named."""
    dropped: list[str] = []
    doc = {
        "graph_id": "runtime_alpha",
        "nodes": [{"id": "n1", "x": 4, "y": 5, "agent": "personainst_a", "collapsed": True}],
        "edges": [],
    }

    body = fgs.project_flow_graph(_stored(doc=doc), dropped=dropped)

    assert body["nodes"] == [{"agent": "personainst_a", "id": "n1", "x": 4, "y": 5}]
    assert "graphs.runtime_alpha.nodes.n1.collapsed" in dropped


def test_edge_order_is_preserved_and_non_owner_edges_travel():
    """Edge order is fan-in priority, so the list is never sorted. Non-owner
    edges are the operator's local context and travel by the ruling — ingest
    reports them and never applies them."""
    doc = {
        "graph_id": "runtime_alpha",
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [{"from": "b", "to": "c"}, {"from": "a", "to": "b"}],
    }

    body = fgs.project_flow_graph(_stored(doc=doc), dropped=[])

    assert body["edges"] == [{"from": "b", "to": "c"}, {"from": "a", "to": "b"}]


def test_the_hash_is_timestamp_free_and_key_order_independent():
    """The baseline and the three-way classifier key on this hash. A save that
    changed nothing but the clock must not read as a change."""
    first = fgs.project_flow_graph(_stored(), dropped=[])
    second = fgs.project_flow_graph(
        _stored(updated_at="2027-01-01T00:00:00Z", requested_by="launcher"), dropped=[]
    )

    assert fgs.flow_graph_def_hash(first) == fgs.flow_graph_def_hash(second)
    assert fgs.flow_graph_def_hash({"a": 1, "b": 2}) == fgs.flow_graph_def_hash({"b": 2, "a": 1})


def test_the_projection_is_scoped_to_the_owners_it_was_asked_for():
    """Exactly the desks this publish already ships — never a second walk of the
    store. An owner with no canvas is absent, not a refusal: most desks have
    never had one drawn."""
    projection = fgs.project_flow_graphs(
        ["alpha", "beta"],
        docs={"alpha": _stored(), "gamma": _stored("runtime_gamma")},
    )

    assert sorted(projection.graphs) == ["runtime_alpha"]
    assert projection.unreadable == []


def test_an_unreadable_document_is_accounted_not_raised():
    """A corrupt or hand-edited store file must not fail a whole publish."""
    projection = fgs.project_flow_graphs(
        ["alpha", "beta", "gamma"],
        docs={
            "alpha": "not-a-dict",
            "beta": {"graph_id": "runtime_beta", "doc": ["also", "not", "a", "dict"]},
            "gamma": {"doc": {"nodes": [], "edges": []}},
        },
    )

    assert projection.graphs == {}
    assert [row["key"] for row in projection.unreadable] == ["alpha", "beta", "gamma"]
    assert [row["code"] for row in projection.unreadable] == [
        fgs.REFUSAL_UNREADABLE_DOCUMENT,
        fgs.REFUSAL_UNREADABLE_DOCUMENT,
        fgs.REFUSAL_MISSING_GRAPH_ID,
    ]


def test_a_document_stored_under_another_owner_is_refused():
    """Graph identity IS the owner instance's id. A blob whose graph_id names a
    different owner than the desk it was resolved for is not this desk's canvas,
    and publishing it under this owner would replicate a mislabel."""
    projection = fgs.project_flow_graphs(["alpha"], docs={"alpha": _stored("runtime_beta")})

    assert projection.graphs == {}
    assert projection.unreadable[0]["code"] == fgs.REFUSAL_GRAPH_OWNER_MISMATCH


def test_the_bytes_are_deterministic_and_lf_only():
    """Republishing an unchanged projection must be a byte-for-byte no-op, or
    the publish change-detector lies."""
    projection = fgs.project_flow_graphs(["alpha"], docs={"alpha": _stored()})
    other = fgs.project_flow_graphs(["alpha"], docs={"alpha": _stored()})

    payload = projection.to_bytes()
    assert payload == other.to_bytes()
    assert b"\r" not in payload
    assert projection.document()["kind"] == fgs.PROJECTION_KIND


def test_the_projection_path_is_one_spelling():
    """An older member maps this path to ``None`` and SKIPS the artifact, which
    is the whole version-skew story: it degrades to no canvas replication."""
    assert fgs.FLOW_GRAPH_PROJECTION_RELATIVE_PATH == "store/flow_graphs.yaml"


@pytest.mark.parametrize("key", ["graph_id", "nodes", "edges"])
def test_the_allowlist_is_exactly_the_authored_document(key):
    assert key in fgs.FLOW_GRAPH_ALLOWED_KEYS
