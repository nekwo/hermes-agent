"""The flow-doc ingest lane: one authored chart document in, steering relations
set on EXISTING instances, per-agent report out.

Contract pins (2026-07-16 operator-decided design):
  * ingest NEVER creates an instance — unknown references are report entries;
  * ingest NEVER touches goal membership — drawn-standalone uses the
    goal-preserving clear_parents, not detach_parents (which strips goal_id);
  * re-ingesting an unchanged doc is a no-op (no writes, changed=False);
  * one agent's failure never aborts the pass.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.flow_graph import (
    FlowGraphDocError,
    FlowGraphStore,
    desired_parents_by_agent,
    ingest_flow_graph,
    parse_flow_graph_doc,
    reconcile_flow_graph_steering,
)
from agent_runtime.persona_assignments import PersonaInstanceStore


def _doc(nodes, edges, graph_id="personainst_lead"):
    return {"graph_id": graph_id, "nodes": nodes, "edges": edges}


def _node(node_id, agent=None, **extra):
    return {"id": node_id, "agent": agent, "x": 10.0, "y": 20.0, **extra}


# ---------------------------------------------------------------- parse


def test_parse_accepts_launcher_shape_and_ignores_layout_keys():
    doc = parse_flow_graph_doc(
        _doc(
            [_node("n1", "personainst_a"), _node("n2", None)],
            [{"from": "n1", "to": "n2"}],
        )
    )
    assert doc.graph_id == "personainst_lead"
    assert doc.node_bindings == {"n1": "personainst_a", "n2": None}
    assert doc.edges == [("n1", "n2")]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ("not a dict", "JSON object"),
        (_doc([_node("n1")], [], graph_id=""), "graph_id"),
        ({"graph_id": "g", "nodes": "nope"}, "nodes must be a list"),
        (_doc([_node("n1"), _node("n1")], []), "duplicate node id"),
        (_doc([_node("n1")], [{"from": "n1", "to": "ghost"}]), "unknown node"),
        (_doc([_node("n1")], [{"from": "n1", "to": "n1"}]), "cannot steer itself"),
        (
            _doc([_node("n1", "personainst_a"), _node("n2", "personainst_a")], []),
            "bound to two nodes",
        ),
    ],
)
def test_parse_rejects_invalid_docs(payload, fragment):
    with pytest.raises(FlowGraphDocError, match=fragment):
        parse_flow_graph_doc(payload)


# ------------------------------------------------- desired parents (pure)


def test_desired_parents_fan_in_preserves_edge_order_and_skips_unbound():
    doc = parse_flow_graph_doc(
        _doc(
            [
                _node("lead", "personainst_lead"),
                _node("dev", "personainst_dev"),
                _node("qa", "personainst_qa"),
                _node("ghost", None),  # authored, unbound: no runtime identity
            ],
            [
                {"from": "lead", "to": "dev"},
                {"from": "dev", "to": "qa"},
                {"from": "lead", "to": "qa"},
                {"from": "ghost", "to": "qa"},  # unbound parent: skipped
                {"from": "lead", "to": "ghost"},  # edge INTO unbound: no entry
            ],
        )
    )
    desired = desired_parents_by_agent(doc)
    assert desired == {
        "personainst_lead": [],  # drawn standalone (root)
        "personainst_dev": ["personainst_lead"],
        "personainst_qa": ["personainst_dev", "personainst_lead"],
    }


# ---------------------------------------------------------- reconcile


def _three_instances():
    store = PersonaInstanceStore()
    lead = store.create_free_floating("profile:lead")
    dev = store.create_free_floating("profile:dev")
    qa = store.create_free_floating("profile:qa")
    return store, lead, dev, qa


def _wired_doc(lead_id, dev_id, qa_id):
    return parse_flow_graph_doc(
        _doc(
            [
                _node("n_lead", lead_id),
                _node("n_dev", dev_id),
                _node("n_qa", qa_id),
            ],
            [
                {"from": "n_lead", "to": "n_dev"},
                {"from": "n_lead", "to": "n_qa"},
                {"from": "n_dev", "to": "n_qa"},
            ],
        )
    )


def test_reconcile_sets_parents_from_the_drawing(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    doc = _wired_doc(lead.id, dev.id, qa.id)

    results = {r["persona_instance_id"]: r for r in reconcile_flow_graph_steering(doc, store=store)}

    assert results[dev.id]["ok"] and results[dev.id]["changed"]
    assert store.get(dev.id).steered_by == [lead.id]
    # Fan-in lands whole, edge order preserved (primary parent first).
    assert store.get(qa.id).steered_by == [lead.id, dev.id]
    # The root drew no parents and had none: honest no-op, not a detach.
    assert results[lead.id]["ok"] and results[lead.id]["changed"] is False


def test_reconcile_is_idempotent_second_pass_writes_nothing(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    doc = _wired_doc(lead.id, dev.id, qa.id)
    reconcile_flow_graph_steering(doc, store=store)

    second = reconcile_flow_graph_steering(doc, store=store)

    assert all(entry["ok"] for entry in second)
    assert all(entry["changed"] is False for entry in second)


def test_reconcile_drawn_standalone_clears_parents_but_keeps_goal(isolate_agent_runtime_root):
    store, lead, dev, _ = _three_instances()
    # dev is steered AND bound to a goal (the mode the root agent lives in).
    store.set_parents(dev.id, [lead.id], goal_id="goal_live")
    assert store.get(dev.id).goal_id == "goal_live"

    # The operator redraws dev standalone. The chart states steering, not goal
    # membership: parents clear, the goal binding MUST survive (detach_parents
    # here would strip goal_id — the trap this lane exists to avoid).
    doc = parse_flow_graph_doc(
        _doc([_node("n_lead", lead.id), _node("n_dev", dev.id)], [])
    )
    results = {r["persona_instance_id"]: r for r in reconcile_flow_graph_steering(doc, store=store)}

    assert results[dev.id]["ok"] and results[dev.id]["changed"]
    after = store.get(dev.id)
    assert after.steered_by == []
    assert after.goal_id == "goal_live"
    assert after.current_task_id == "goal_live"


def test_reconcile_unknown_instance_is_reported_never_created(isolate_agent_runtime_root):
    store, lead, _, _ = _three_instances()
    doc = parse_flow_graph_doc(
        _doc(
            [_node("n_lead", lead.id), _node("n_ghost", "personainst_never_made")],
            [{"from": "n_lead", "to": "n_ghost"}],
        )
    )

    results = {r["persona_instance_id"]: r for r in reconcile_flow_graph_steering(doc, store=store)}

    ghost = results["personainst_never_made"]
    assert ghost["ok"] is False
    assert "never creates" in ghost["error"]
    # And it truly was not created.
    with pytest.raises(Exception):
        store.get("personainst_never_made")


def test_reconcile_cycle_fails_that_agent_and_continues(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    # A drawing with a 2-cycle: lead→dev and dev→lead. Whichever write lands
    # second trips the store's cycle guard; the third agent still reconciles.
    doc = parse_flow_graph_doc(
        _doc(
            [_node("n_lead", lead.id), _node("n_dev", dev.id), _node("n_qa", qa.id)],
            [
                {"from": "n_lead", "to": "n_dev"},
                {"from": "n_dev", "to": "n_lead"},
                {"from": "n_lead", "to": "n_qa"},
            ],
        )
    )

    results = {r["persona_instance_id"]: r for r in reconcile_flow_graph_steering(doc, store=store)}

    outcomes = sorted(results[i]["ok"] for i in (lead.id, dev.id))
    assert outcomes == [False, True], "exactly one side of the cycle must fail"
    assert results[qa.id]["ok"] is True
    assert store.get(qa.id).steered_by == [lead.id]


# ------------------------------------------------------------- ingest


def test_ingest_stores_doc_and_reports(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    payload = _doc(
        [_node("n_lead", lead.id), _node("n_dev", dev.id), _node("n_qa", qa.id)],
        [
            {"from": "n_lead", "to": "n_dev"},
            {"from": "n_lead", "to": "n_qa"},
            {"from": "n_dev", "to": "n_qa"},
        ],
    )

    report = ingest_flow_graph(json.dumps(payload), requested_by="launcher")

    assert report["ok"] is True
    assert report["stored"] is True
    assert report["bound_agent_count"] == 3
    assert report["failed_count"] == 0
    assert report["changed_count"] == 2  # lead is a no-op root
    stored = FlowGraphStore().get("personainst_lead")
    assert stored is not None
    assert stored["requested_by"] == "launcher"
    assert stored["doc"]["nodes"][0]["x"] == 10.0  # layout preserved verbatim
    assert (isolate_agent_runtime_root / "flow_graphs" / "personainst_lead.json").exists()


def test_ingest_removed_agent_sheds_the_maps_wiring(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    first = _doc(
        [_node("n_lead", lead.id), _node("n_dev", dev.id), _node("n_qa", qa.id)],
        [
            {"from": "n_lead", "to": "n_dev"},
            {"from": "n_dev", "to": "n_qa"},
        ],
    )
    ingest_flow_graph(json.dumps(first))
    assert store.get(dev.id).steered_by == [lead.id]
    assert store.get(qa.id).steered_by == [dev.id]

    # The operator deletes dev's node and wires lead -> qa directly.
    second = _doc(
        [_node("n_lead", lead.id), _node("n_qa", qa.id)],
        [{"from": "n_lead", "to": "n_qa"}],
    )
    report = ingest_flow_graph(json.dumps(second))

    assert report["removed_from_map_count"] == 1
    departed = next(e for e in report["reconciled"] if e.get("removed_from_map"))
    assert departed["persona_instance_id"] == dev.id
    assert departed["ok"] and departed["changed"]
    # dev lost the map's edge; qa follows the new drawing.
    assert store.get(dev.id).steered_by == []
    assert store.get(qa.id).steered_by == [lead.id]


def test_ingest_departure_preserves_foreign_parents_and_goal(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    foreign = store.create_free_floating("profile:foreign")
    ingest_flow_graph(
        json.dumps(
            _doc(
                [_node("n_lead", lead.id), _node("n_dev", dev.id)],
                [{"from": "n_lead", "to": "n_dev"}],
            )
        )
    )
    # A parent set OUTSIDE the map (goal engine / another surface) + a goal.
    store.set_parents(dev.id, [lead.id, foreign.id], goal_id="goal_live")

    ingest_flow_graph(json.dumps(_doc([_node("n_lead", lead.id)], [])))

    after = store.get(dev.id)
    # Map member (lead) stripped; foreign parent survives; goal untouched.
    assert after.steered_by == [foreign.id]
    assert after.goal_id == "goal_live"


def test_ingest_first_doc_has_no_departures(isolate_agent_runtime_root):
    store, lead, dev, _ = _three_instances()
    report = ingest_flow_graph(
        json.dumps(
            _doc(
                [_node("n_lead", lead.id), _node("n_dev", dev.id)],
                [{"from": "n_lead", "to": "n_dev"}],
            )
        )
    )
    assert report["removed_from_map_count"] == 0
    assert not any(e.get("removed_from_map") for e in report["reconciled"])


def test_ingest_departed_agent_gone_from_runtime_is_tolerated(isolate_agent_runtime_root):
    store, lead, _, _ = _three_instances()
    ghost = "personainst_soon_gone"
    # Prior doc claims a binding to an agent that no longer exists at the next
    # ingest — simulate by storing a doc that binds a never-created id.
    first = _doc(
        [_node("n_lead", lead.id), _node("n_ghost", ghost)],
        [{"from": "n_lead", "to": "n_ghost"}],
    )
    ingest_flow_graph(json.dumps(first))

    report = ingest_flow_graph(json.dumps(_doc([_node("n_lead", lead.id)], [])))

    departed = next(e for e in report["reconciled"] if e.get("removed_from_map"))
    assert departed["persona_instance_id"] == ghost
    assert departed["ok"] is True and departed["changed"] is False


def test_ingest_rejects_invalid_json_and_oversize(isolate_agent_runtime_root):
    with pytest.raises(FlowGraphDocError, match="not valid JSON"):
        ingest_flow_graph("{nope")
    with pytest.raises(FlowGraphDocError, match="too large"):
        ingest_flow_graph('{"pad": "' + "x" * (256 * 1024) + '"}')
    # Nothing stored on either failure.
    assert FlowGraphStore().list_ids() == []
