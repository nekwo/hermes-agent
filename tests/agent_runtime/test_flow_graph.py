"""The flow-doc ingest lane: one authored chart document in, OWNER-scoped
steering set on EXISTING instances, per-agent report out.

Contract pins:
  * (2026-07-16) ingest NEVER creates an instance — unknown references are
    report entries; ingest NEVER touches goal membership (goal-preserving
    clear_parents, not detach_parents); one agent's failure never aborts the
    pass; re-ingesting an unchanged doc is a no-op.
  * (2026-07-18 per-instance blueprint ownership) graph identity IS the owner
    instance id (``runtime:<owner>``); a map asserts ONLY its owner's outbound
    edges — set/clear just the owner in each child's ``steered_by``, preserving
    every other parent; two leads' maps COMPOSE into fan-in on a shared child;
    departure strips only the owner edge; non-owner edges are reported
    (``ignored_non_owner_edges``), never applied.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime.flow_graph import (
    FlowGraphDocError,
    FlowGraphStore,
    bound_agent_ids,
    ignored_non_owner_edges,
    ingest_flow_graph,
    owner_instance_id_of,
    owner_scoped_children,
    parse_flow_graph_doc,
    reconcile_flow_graph_steering,
)
from agent_runtime.persona_assignments import PersonaInstanceStore
from tests.agent_runtime.persona_instance_mint import mint_free_floating


def _doc(nodes, edges, graph_id="runtime:personainst_lead"):
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
    # The launcher's `runtime:<owner>` id survives parse — the `:` is normalized
    # to `_` by safe_assignment_token, which owner_instance_id_of accounts for.
    assert doc.graph_id == "runtime_personainst_lead"
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


# ------------------------------------------------- owner derivation (pure)


def test_owner_instance_id_of_strips_either_prefix():
    # Raw launcher id (with `:`) and the parsed/normalized id (with `_`) both
    # resolve to the same owner; an id without either prefix is verbatim.
    assert owner_instance_id_of("runtime:personainst_neko") == "personainst_neko"
    assert owner_instance_id_of("runtime_personainst_neko") == "personainst_neko"
    assert owner_instance_id_of("personainst_neko") == "personainst_neko"
    assert owner_instance_id_of("") == ""


def test_owner_scoped_children_flags_only_owner_edges():
    # owner = personainst_lead; lead draws lead->dev but NOT lead->qa; the
    # dev->qa edge is non-owner, so qa is not flagged steered by the owner.
    doc = parse_flow_graph_doc(
        _doc(
            [
                _node("lead", "personainst_lead"),
                _node("dev", "personainst_dev"),
                _node("qa", "personainst_qa"),
                _node("ghost", None),
            ],
            [
                {"from": "lead", "to": "dev"},
                {"from": "dev", "to": "qa"},  # non-owner edge
            ],
            graph_id="runtime:personainst_lead",
        )
    )
    children = owner_scoped_children(doc, "personainst_lead")
    # The owner itself is never its own child; unbound nodes contribute nothing.
    assert children == {"personainst_dev": True, "personainst_qa": False}


# ------------------------------------------ desired parents (pure, whole-doc)


# ---------------------------------------------------- reconcile (owner-scoped)


def _three_instances():
    store = PersonaInstanceStore()
    lead = mint_free_floating("profile:lead", store=store)
    dev = mint_free_floating("profile:dev", store=store)
    qa = mint_free_floating("profile:qa", store=store)
    return store, lead, dev, qa


def _owned_doc(owner_id, nodes, edges):
    return parse_flow_graph_doc(
        {"graph_id": f"runtime:{owner_id}", "nodes": nodes, "edges": edges}
    )


def test_reconcile_applies_only_owner_edges(isolate_agent_runtime_root):
    # lead's map draws lead->dev, lead->qa, AND dev->qa. Only the two OWNER edges
    # apply; the dev->qa edge is non-owner and is NOT applied (qa is steered by
    # lead alone — the old whole-doc ingest would have made it [lead, dev]).
    store, lead, dev, qa = _three_instances()
    doc = _owned_doc(
        lead.id,
        [_node("n_lead", lead.id), _node("n_dev", dev.id), _node("n_qa", qa.id)],
        [
            {"from": "n_lead", "to": "n_dev"},
            {"from": "n_lead", "to": "n_qa"},
            {"from": "n_dev", "to": "n_qa"},  # non-owner: ignored
        ],
    )

    results = {r["persona_instance_id"]: r for r in reconcile_flow_graph_steering(doc, store=store)}

    # No entry for the owner — a map never steers its own owner.
    assert lead.id not in results
    assert store.get(dev.id).steered_by == [lead.id]
    assert store.get(qa.id).steered_by == [lead.id]
    assert results[dev.id]["ok"] and results[dev.id]["changed"]
    assert results[qa.id]["owner"] == lead.id and results[qa.id]["owner_steers"] is True


def test_reconcile_is_idempotent_second_pass_writes_nothing(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    doc = _owned_doc(
        lead.id,
        [_node("n_lead", lead.id), _node("n_dev", dev.id), _node("n_qa", qa.id)],
        [{"from": "n_lead", "to": "n_dev"}, {"from": "n_lead", "to": "n_qa"}],
    )
    reconcile_flow_graph_steering(doc, store=store)

    second = reconcile_flow_graph_steering(doc, store=store)

    assert all(entry["ok"] for entry in second)
    assert all(entry["changed"] is False for entry in second)


def test_reconcile_drawn_standalone_clears_owner_but_keeps_goal(isolate_agent_runtime_root):
    store, lead, dev, _ = _three_instances()
    store.set_parents(dev.id, [lead.id], goal_id="goal_live")
    assert store.get(dev.id).goal_id == "goal_live"

    # lead redraws dev standalone (dev still a node, no owner edge). The owner
    # edge clears; the goal binding MUST survive (clear_parents, not detach).
    doc = _owned_doc(lead.id, [_node("n_lead", lead.id), _node("n_dev", dev.id)], [])
    results = {r["persona_instance_id"]: r for r in reconcile_flow_graph_steering(doc, store=store)}

    assert results[dev.id]["ok"] and results[dev.id]["changed"]
    after = store.get(dev.id)
    assert after.steered_by == []
    assert after.goal_id == "goal_live"
    assert after.current_task_id == "goal_live"


def test_reconcile_unknown_instance_is_reported_never_created(isolate_agent_runtime_root):
    store, lead, _, _ = _three_instances()
    doc = _owned_doc(
        lead.id,
        [_node("n_lead", lead.id), _node("n_ghost", "personainst_never_made")],
        [{"from": "n_lead", "to": "n_ghost"}],
    )

    results = {r["persona_instance_id"]: r for r in reconcile_flow_graph_steering(doc, store=store)}

    ghost = results["personainst_never_made"]
    assert ghost["ok"] is False
    assert "never creates" in ghost["error"]
    with pytest.raises(Exception):
        store.get("personainst_never_made")


# ------------------------------------------ two maps compose (the headline)


def test_two_maps_compose_fan_in_on_one_child(isolate_agent_runtime_root):
    # A and B each own their OWN blueprint; both steer the same child C. Their
    # maps compose into fan-in [A, B] instead of clobbering each other — the fix
    # for "two Neko instances show the SAME blueprint".
    store = PersonaInstanceStore()
    a = mint_free_floating("profile:a", store=store)
    b = mint_free_floating("profile:b", store=store)
    c = mint_free_floating("profile:c", store=store)

    # A's map: A -> C.
    ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{a.id}",
                "nodes": [_node("n_a", a.id), _node("n_c", c.id)],
                "edges": [{"from": "n_a", "to": "n_c"}],
            }
        ),
        store=store,
    )
    assert store.get(c.id).steered_by == [a.id]

    # B's map: B -> C. A's edge must SURVIVE — C is now steered by both.
    ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{b.id}",
                "nodes": [_node("n_b", b.id), _node("n_c", c.id)],
                "edges": [{"from": "n_b", "to": "n_c"}],
            }
        ),
        store=store,
    )
    assert store.get(c.id).steered_by == [a.id, b.id]

    # Re-ingesting A's map is a no-op — it does not drop B's edge.
    report = ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{a.id}",
                "nodes": [_node("n_a", a.id), _node("n_c", c.id)],
                "edges": [{"from": "n_a", "to": "n_c"}],
            }
        ),
        store=store,
    )
    assert report["changed_count"] == 0
    assert store.get(c.id).steered_by == [a.id, b.id]

    # A un-draws its edge (C still a node on A's map). Only A is stripped; B stays.
    ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{a.id}",
                "nodes": [_node("n_a", a.id), _node("n_c", c.id)],
                "edges": [],
            }
        ),
        store=store,
    )
    assert store.get(c.id).steered_by == [b.id]


# ------------------------------------------ ignored non-owner edges


def test_ignored_non_owner_edges_reports_and_never_applies(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    # owner=lead. lead->dev is the owner edge; dev->qa is non-owner (a deeper
    # tree drawn on lead's canvas). The non-owner edge is reported, not applied.
    report = ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{lead.id}",
                "nodes": [
                    _node("n_lead", lead.id),
                    _node("n_dev", dev.id),
                    _node("n_qa", qa.id),
                ],
                "edges": [
                    {"from": "n_lead", "to": "n_dev"},
                    {"from": "n_dev", "to": "n_qa"},
                ],
            }
        ),
        store=store,
    )

    assert report["owner_instance_id"] == lead.id
    assert report["ignored_non_owner_edge_count"] == 1
    ignored = report["ignored_non_owner_edges"][0]
    assert ignored["from_node"] == "n_dev" and ignored["to_node"] == "n_qa"
    assert ignored["parent_agent"] == dev.id and ignored["child_agent"] == qa.id
    assert "non-owner edge" in ignored["reason"]
    # The owner edge applied; the non-owner edge did NOT (qa steered by nobody).
    assert store.get(dev.id).steered_by == [lead.id]
    assert store.get(qa.id).steered_by == []


def test_ignored_non_owner_edges_pure_helper():
    doc = parse_flow_graph_doc(
        _doc(
            [
                _node("n_lead", "personainst_lead"),
                _node("n_dev", "personainst_dev"),
                _node("n_ghost", None),
            ],
            [
                {"from": "n_lead", "to": "n_dev"},  # owner edge
                {"from": "n_dev", "to": "n_lead"},  # non-owner (bound source)
                {"from": "n_ghost", "to": "n_dev"},  # unbound source
            ],
            graph_id="runtime:personainst_lead",
        )
    )
    ignored = ignored_non_owner_edges(doc, "personainst_lead")
    reasons = {(e["from_node"], e["to_node"]): e["reason"] for e in ignored}
    assert set(reasons) == {("n_dev", "n_lead"), ("n_ghost", "n_dev")}
    assert "non-owner edge" in reasons[("n_dev", "n_lead")]
    assert "unbound" in reasons[("n_ghost", "n_dev")]


# ------------------------------------------------------------- ingest


def test_ingest_stores_doc_and_reports_owner(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    payload = {
        "graph_id": f"runtime:{lead.id}",
        "nodes": [_node("n_lead", lead.id), _node("n_dev", dev.id), _node("n_qa", qa.id)],
        "edges": [{"from": "n_lead", "to": "n_dev"}, {"from": "n_lead", "to": "n_qa"}],
    }

    report = ingest_flow_graph(json.dumps(payload), requested_by="launcher", store=store)

    assert report["ok"] is True and report["stored"] is True
    assert report["owner_instance_id"] == lead.id
    assert report["bound_agent_count"] == 3
    assert report["failed_count"] == 0
    assert report["changed_count"] == 2  # dev + qa; the owner has no entry
    assert report["ignored_non_owner_edge_count"] == 0
    # Stored under the NORMALIZED id (`:` -> `_`); layout preserved verbatim.
    stored = FlowGraphStore().get(f"runtime_{lead.id}")
    assert stored is not None
    assert stored["requested_by"] == "launcher"
    assert stored["doc"]["nodes"][0]["x"] == 10.0
    assert (isolate_agent_runtime_root / "flow_graphs" / f"runtime_{lead.id}.json").exists()


def test_ingest_removed_node_strips_only_owner_edge(isolate_agent_runtime_root):
    store, lead, dev, qa = _three_instances()
    # A foreign parent set outside the map + a goal on qa.
    foreign = mint_free_floating("profile:foreign", store=store)
    ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{lead.id}",
                "nodes": [_node("n_lead", lead.id), _node("n_qa", qa.id)],
                "edges": [{"from": "n_lead", "to": "n_qa"}],
            }
        ),
        store=store,
    )
    store.set_parents(qa.id, [lead.id, foreign.id], goal_id="goal_live")

    # lead deletes qa's node entirely. Departure strips ONLY the owner (lead);
    # the foreign parent AND the goal survive.
    report = ingest_flow_graph(
        json.dumps(
            {"graph_id": f"runtime:{lead.id}", "nodes": [_node("n_lead", lead.id)], "edges": []}
        ),
        store=store,
    )

    assert report["removed_from_map_count"] == 1
    departed = next(e for e in report["reconciled"] if e.get("removed_from_map"))
    assert departed["persona_instance_id"] == qa.id
    assert departed["owner"] == lead.id and departed["changed"]
    after = store.get(qa.id)
    assert after.steered_by == [foreign.id]
    assert after.goal_id == "goal_live"


def test_ingest_first_doc_has_no_departures(isolate_agent_runtime_root):
    store, lead, dev, _ = _three_instances()
    report = ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{lead.id}",
                "nodes": [_node("n_lead", lead.id), _node("n_dev", dev.id)],
                "edges": [{"from": "n_lead", "to": "n_dev"}],
            }
        ),
        store=store,
    )
    assert report["removed_from_map_count"] == 0
    assert not any(e.get("removed_from_map") for e in report["reconciled"])


def test_ingest_departed_agent_gone_from_runtime_is_tolerated(isolate_agent_runtime_root):
    store, lead, _, _ = _three_instances()
    ghost = "personainst_soon_gone"
    ingest_flow_graph(
        json.dumps(
            {
                "graph_id": f"runtime:{lead.id}",
                "nodes": [_node("n_lead", lead.id), _node("n_ghost", ghost)],
                "edges": [{"from": "n_lead", "to": "n_ghost"}],
            }
        ),
        store=store,
    )

    report = ingest_flow_graph(
        json.dumps(
            {"graph_id": f"runtime:{lead.id}", "nodes": [_node("n_lead", lead.id)], "edges": []}
        ),
        store=store,
    )

    departed = next(e for e in report["reconciled"] if e.get("removed_from_map"))
    assert departed["persona_instance_id"] == ghost
    assert departed["ok"] is True and departed["changed"] is False


def test_ingest_rejects_invalid_json_and_oversize(isolate_agent_runtime_root):
    with pytest.raises(FlowGraphDocError, match="not valid JSON"):
        ingest_flow_graph("{nope")
    with pytest.raises(FlowGraphDocError, match="too large"):
        ingest_flow_graph('{"pad": "' + "x" * (256 * 1024) + '"}')
    assert FlowGraphStore().list_ids() == []


def test_bound_agent_ids_excludes_unbound():
    doc = parse_flow_graph_doc(
        _doc([_node("n1", "personainst_a"), _node("n2", None), _node("n3", "personainst_b")], [])
    )
    assert bound_agent_ids(doc) == {"personainst_a", "personainst_b"}
