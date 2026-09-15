"""S4 of the canvas replication plan — the reap ordering, re-measured.

The plan's stage 4 named this hazard: owner-liveness reaping archives a canvas
whose owner instance is gone, and a canvas pulled before its owner is minted
looks exactly like that, so it is reaped on arrival.

**Re-measured at this base, and half the premise is false.** The reap does NOT
run inside ``pull_realm_sync``: ``_prune_owner_less_flow_graphs`` is phase 5 of
``reconcile_persona_instances``, whose only caller is the ``harness runtime
reconcile`` verb. So no pull archives a canvas in its own pass, whatever order
its lanes run in. What IS order-sensitive is pinned below:

* the pull's own accounting. The canvas lane resolves the live instance set to
  decide ``unbound_node_agents``, so running it before the mint door would name
  every binding the same pull was about to satisfy.
* the hazard's real home — the liveness classifier — where a canvas whose owner
  exists is HELD however empty, and one whose owner does not is stale however
  richly drawn. That is the property the ordering has to leave true, and it is
  asserted here rather than assumed from the ordering.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agent_runtime import flow_graph_sync, persona_instance_sync
from agent_runtime.flow_graph import (
    FlowGraphStore,
    classify_graph_owner_liveness,
    parse_flow_graph_doc,
)
from agent_runtime.realm_sync import pull_realm_sync
from agent_runtime.store import RealmStore

OWNER = "personainst_dev_agent_9682caf4"


def _local_realm(tmp_path: Path, name: str = "Canvas Realm"):
    """A server-less realm whose sync repo is a local git repo (no remote)."""

    realm = RealmStore().create(name=name)
    repo = tmp_path / "realm-sync-repo"
    subprocess.run(
        ["git", "-C", str(tmp_path), "init", "realm-sync-repo"],
        check=True,
        capture_output=True,
        text=True,
    )
    realm.sync_manifest_ref = str(repo)
    realm = RealmStore().save(realm)
    return realm, repo


def test_the_canvas_lane_runs_after_the_mint_door(tmp_path, monkeypatch):
    """Pinned by a test rather than by a comment, because the comment is what
    the next refactor moves."""
    realm, _ = _local_realm(tmp_path)
    order: list[str] = []
    real_instances = persona_instance_sync.apply_persona_instance_pull
    real_canvas = flow_graph_sync.apply_flow_graph_pull

    def _instances(*args, **kwargs):
        order.append("instances")
        return real_instances(*args, **kwargs)

    def _canvas(*args, **kwargs):
        order.append("canvas")
        return real_canvas(*args, **kwargs)

    monkeypatch.setattr(persona_instance_sync, "apply_persona_instance_pull", _instances)
    monkeypatch.setattr(flow_graph_sync, "apply_flow_graph_pull", _canvas)

    result = pull_realm_sync(realm.id)

    assert order == ["instances", "canvas"]
    # And the ack carries the canvas row unconditionally, so a peer that
    # publishes no canvas is distinguishable from a hermes that has no canvas
    # family at all.
    assert result["flow_graph_sync"]["source"] is None


def test_a_canvas_whose_owner_the_pull_already_minted_survives_the_reap():
    """The hazard's real home. The reap asks ONE question of a stored canvas —
    does its owner still resolve — so the canvas surviving is exactly the owner
    existing by the time the reap runs."""
    FlowGraphStore().set_doc(
        parse_flow_graph_doc(
            {
                "graph_id": f"runtime:{OWNER}",
                "nodes": [{"id": "n_owner", "agent": OWNER, "x": 1, "y": 2}],
                "edges": [],
            }
        ),
        requested_by="realm-sync",
    )
    graph_ids = FlowGraphStore().list_ids()

    held = classify_graph_owner_liveness(graph_ids, live_instance_ids={OWNER})
    assert [row["graph_id"] for row in held["held"]] == [f"runtime_{OWNER}"]
    assert held["stale"] == []

    # The inverse is the whole reason the ordering matters: with the owner not
    # yet minted, the identical canvas classifies stale.
    orphaned = classify_graph_owner_liveness(graph_ids, live_instance_ids=set())
    assert [row["graph_id"] for row in orphaned["stale"]] == [f"runtime_{OWNER}"]
