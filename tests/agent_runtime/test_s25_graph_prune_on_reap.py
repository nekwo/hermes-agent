"""S25 — graph-prune-on-reap: the reconciler's fifth phase.

A runtime flow graph IS one persona instance's blueprint: its id is
``runtime:<owner>`` (:func:`owner_instance_id_of`), so a graph whose owner no
longer resolves is an operator canvas addressed to an agent the runtime already
archived. Live evidence 2026-07-30 (home ``X:\\Eternia\\.hermes``, root
``agent-runtime``): seven such docs had already been moved into
``flow_graphs_stale/`` BY HAND — two of them (``runtime_dev``, ``runtime_neko``)
owned by persona ids that were never instance ids at all. This phase automates
that sweep under the phase-2 contract: archive (never delete), typed event,
held/pruned accounting, and a dry run that writes nothing.

CRITICAL KEEP RULE pinned here: the launcher auto-creates a single self-node,
zero-edge graph (``requested_by: launcher``) the moment an operator opens an
agent's canvas. Emptiness is NEVER a prune signal — owner liveness is the whole
rule.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime import paths
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, EventLog
from agent_runtime.flow_graph import (
    GRAPH_HELD_REASON_OWNER_ALIASED,
    GRAPH_HELD_REASON_OWNER_LIVE,
    GRAPH_PRUNE_REASON_OWNER_NOT_LIVE,
    FlowGraphStore,
    classify_graph_owner_liveness,
    parse_flow_graph_doc,
)
from agent_runtime.models import PersonaInstance
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.persona_instance_identity import reconcile_persona_instances
from agent_runtime.serde import to_jsonable
from agent_runtime.states import WorkerSessionState

_STALE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)  # ~6 weeks old


def _templates(monkeypatch) -> None:
    """An AUTHORITATIVE profile catalog: the orphan lane only engages when the
    template list was positively enumerated."""

    monkeypatch.setattr(
        "agent_runtime.persona_instance_identity._profile_template_names",
        lambda: ["alice", "base", "backend-dev"],
    )


def _seed_row(
    instance_id: str,
    *,
    persona_id: str,
    mode: str = "chat",
    profile_id: str | None = None,
    steered_by: list[str] | None = None,
    updated_at=None,
) -> PersonaInstance:
    instance = PersonaInstance(
        id=instance_id,
        persona_id=persona_id,
        role="profile" if persona_id.startswith("profile:") else persona_id,
        display_name=instance_id,
        profile_id=profile_id,
        steered_by=list(steered_by or []),
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
        mode=mode,
        updated_at=updated_at or now(),
    )
    path = paths.persona_instance_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(instance), indent=2, sort_keys=True), encoding="utf-8")
    return instance


def _seed_graph(owner: str, *, children: tuple[str, ...] = (), requested_by: str = "launcher") -> str:
    """Write a graph through the real store path. With no children this is
    EXACTLY the launcher's auto-created canvas: one self node, zero edges."""

    nodes = [{"id": "self", "agent": owner, "x": 80.0, "y": 80.0}]
    edges = []
    for index, child in enumerate(children, start=1):
        nodes.append({"id": f"n{index}", "agent": child, "x": 240.0, "y": 80.0 * index})
        edges.append({"from": "self", "to": f"n{index}"})
    doc = parse_flow_graph_doc({"graph_id": f"runtime:{owner}", "nodes": nodes, "edges": edges})
    FlowGraphStore().set_doc(doc, requested_by=requested_by)
    return doc.graph_id


def _pruned_events() -> list[dict]:
    return [
        event.payload
        for event in EventLog().tail(200)
        if getattr(event, "type", None) == "flow_graph.pruned"
    ]


# ----------------------------------------------------------- pure classifier


def test_owner_liveness_is_the_only_prune_signal():
    classified = classify_graph_owner_liveness(
        ["runtime_personainst_live", "runtime_personainst_gone", "personainst_live"],
        live_instance_ids={"personainst_live"},
    )

    assert classified["stale"] == [
        {
            "graph_id": "runtime_personainst_gone",
            "owner_instance_id": "personainst_gone",
            "reason": GRAPH_PRUNE_REASON_OWNER_NOT_LIVE,
        }
    ]
    # The un-prefixed id is the live root's ``personainst_neko_supervisor.json``
    # graph: owner_instance_id_of treats a prefix-less id as the owner verbatim.
    assert {item["graph_id"] for item in classified["held"]} == {
        "runtime_personainst_live",
        "personainst_live",
    }
    assert {item["reason"] for item in classified["held"]} == {GRAPH_HELD_REASON_OWNER_LIVE}


# ------------------------------------------------------------- the KEEP rule


def test_the_launcher_auto_created_empty_graph_is_never_pruned(monkeypatch):
    _templates(monkeypatch)
    _seed_row("personainst_dev", persona_id="dev")
    graph_id = _seed_graph("personainst_dev")  # self node only, zero edges

    report = reconcile_persona_instances(apply=True, event_log=EventLog())

    assert report["graphs_pruned"] == []
    assert report["graphs_pruned_count"] == 0
    assert [item["reason"] for item in report["graphs_held"]] == [GRAPH_HELD_REASON_OWNER_LIVE]
    assert FlowGraphStore().list_ids() == [graph_id]
    assert _pruned_events() == []


def test_a_graph_addressed_by_a_drifted_spelling_of_a_live_owner_is_held(monkeypatch):
    _templates(monkeypatch)
    _seed_row("personainst_neko_supervisor", persona_id="neko_supervisor")
    # Actor-token drift: the canvas was saved under ``persona_personainst_*``.
    graph_id = _seed_graph("persona_personainst_neko_supervisor")

    report = reconcile_persona_instances(apply=True, event_log=EventLog())

    assert report["graphs_pruned"] == []
    assert [item["reason"] for item in report["graphs_held"]] == [GRAPH_HELD_REASON_OWNER_ALIASED]
    assert FlowGraphStore().list_ids() == [graph_id]


# ------------------------------------------------------------ the prune lane


def test_dry_run_reports_the_stale_graph_and_writes_nothing(monkeypatch):
    _templates(monkeypatch)
    _seed_row("personainst_dev", persona_id="dev")
    live_graph = _seed_graph("personainst_dev")
    stale_graph = _seed_graph("personainst_reaped_last_week")

    report = reconcile_persona_instances(apply=False, event_log=EventLog())

    assert report["graphs_pruned_count"] == 1
    assert report["graphs_pruned"][0]["graph_id"] == stale_graph
    assert report["graphs_pruned"][0]["owner_instance_id"] == "personainst_reaped_last_week"
    assert report["graphs_pruned"][0]["reason"] == GRAPH_PRUNE_REASON_OWNER_NOT_LIVE
    assert report["graph_prune_archive_dir"] is None
    # Nothing moved, nothing archived, nothing emitted.
    assert set(FlowGraphStore().list_ids()) == {live_graph, stale_graph}
    assert not FlowGraphStore().stale_dir().exists()
    assert _pruned_events() == []


def test_apply_archives_the_stale_graph_emits_the_typed_event_and_is_idempotent(monkeypatch):
    _templates(monkeypatch)
    _seed_row("personainst_dev", persona_id="dev")
    live_graph = _seed_graph("personainst_dev")
    stale_graph = _seed_graph("personainst_reaped_last_week")

    report = reconcile_persona_instances(apply=True, event_log=EventLog())

    assert report["graphs_pruned_count"] == 1
    assert FlowGraphStore().list_ids() == [live_graph]
    # Archived, never deleted — under a timestamped subdir of the sibling dir.
    archive_dir = FlowGraphStore().stale_dir()
    archived = {path.name for path in archive_dir.rglob("*.json")}
    assert archived == {f"{stale_graph}.json"}
    assert report["graph_prune_archive_dir"].startswith(str(archive_dir))
    assert report["graphs_pruned"][0]["archived_to"].endswith(f"{stale_graph}.json")

    payloads = _pruned_events()
    assert len(payloads) == 1
    assert payloads[0]["graph_id"] == stale_graph
    assert payloads[0]["owner_instance_id"] == "personainst_reaped_last_week"
    assert payloads[0]["reason"] == GRAPH_PRUNE_REASON_OWNER_NOT_LIVE

    again = reconcile_persona_instances(apply=True, event_log=EventLog())
    assert again["graphs_pruned_count"] == 0
    assert len(_pruned_events()) == 1


def test_the_graph_of_an_instance_this_reconciler_prunes_goes_with_it(monkeypatch):
    """Phase 2 archives the orphan owner; phase 5 reaps the canvas it owned, and
    the children it drew keep every OTHER parent."""

    _templates(monkeypatch)
    _seed_row("personainst_base", persona_id="base", profile_id="base")
    _seed_row(
        "personainst_dev",
        persona_id="dev",
        steered_by=["personainst_profile_ghost_owner", "personainst_base"],
    )
    _seed_row(
        "personainst_profile_ghost_owner",
        persona_id="profile:ghost_owner",
        mode="configured",
        profile_id="ghost_owner",
        updated_at=_STALE,
    )
    ghost_graph = _seed_graph("personainst_profile_ghost_owner", children=("personainst_dev",))
    dev_graph = _seed_graph("personainst_dev")

    report = reconcile_persona_instances(apply=True, event_log=EventLog())

    assert report["pruned_count"] == 1  # phase 2 archived the orphan owner
    assert report["graphs_pruned_count"] == 1
    assert report["graphs_pruned"][0]["graph_id"] == ghost_graph
    assert FlowGraphStore().list_ids() == [dev_graph]
    # The drawn child survives and keeps the parent the reaped map never owned.
    assert PersonaInstanceStore().get("personainst_dev").steered_by == ["personainst_base"]
    # Departure settlement is accounted per drawn child, never silent.
    assert [item["persona_instance_id"] for item in report["graph_departed_steering"]] == [
        "personainst_dev"
    ]
    assert report["graph_departed_steering"][0]["graph_id"] == ghost_graph


# --------------------------------------------------------- contract registry


def test_flow_graph_pruned_is_registered_with_the_payload_the_phase_emits():
    assert "flow_graph.pruned" in ALLOWED_EVENT_TYPES
    entry = event_catalog()["flow_graph.pruned"]
    assert entry["summary_fields"] == ["graph_id", "owner_instance_id", "reason"]


def test_the_emitted_payload_satisfies_its_contract_under_strict_validation(monkeypatch):
    """Strict mode raises on a missing summary field — the phase must never emit
    a shape its own contract rejects."""

    monkeypatch.setenv("HERMES_EVENT_CONTRACT_STRICT", "1")
    _templates(monkeypatch)
    _seed_graph("personainst_reaped_last_week")

    report = reconcile_persona_instances(apply=True, event_log=EventLog())

    assert report["graphs_pruned_count"] == 1
    assert len(_pruned_events()) == 1
