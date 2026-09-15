"""S28: `persona-instance reconcile` renders its graph-prune phase to humans.

The graph-prune phase (``6c5040ed2``) archives owner-less runtime graphs, hands
their drawn children to the owner-scoped departure settlement, and appends a
``flow_graph.pruned`` event. It reported all of that through ``--json`` only:
the human render still printed the four original counters, so an operator
watching the default output saw a phase that moves files and appends events
happen silently.

The returned dict already carried ``graphs_pruned`` / ``graphs_held`` /
``graphs_pruned_count`` / ``graphs_held_count`` / ``graph_departed_steering*``
/ ``graph_prune_archive_dir``. This is a render fix, not a contract change --
the JSON wire is byte-identical before and after.
"""

from __future__ import annotations

import argparse

from hermes_cli.harness import build_parser


def _parser():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser


def _reconcile_report() -> dict:
    return {
        "applied": False,
        "actions": [],
        "merged_count": 0,
        "renamed_count": 0,
        "skipped_count": 0,
        "pruned": [],
        "held": [],
        "pruned_count": 0,
        "held_count": 0,
        "steering_repairs": [],
        "steering_repaired_count": 0,
        "session_binding_repairs": [],
        "session_binding_repaired_count": 0,
        "session_binding_held": [],
        "session_binding_skipped": None,
        "graphs_pruned": [
            {
                "graph_id": "runtime_personainst_gone",
                "owner_instance_id": "personainst_gone",
                "reason": "graph-owner-not-live",
                "drawn_agent_count": 2,
            }
        ],
        "graphs_held": [
            {
                "graph_id": "runtime_personainst_dev",
                "owner_instance_id": "personainst_dev",
                "reason": "graph-owner-live",
            }
        ],
        "graphs_pruned_count": 1,
        "graphs_held_count": 1,
        "graph_departed_steering": [
            {
                "persona_instance_id": "personainst_child",
                "owner": "personainst_gone",
                "graph_id": "runtime_personainst_gone",
                "changed": True,
                "steered_by": [],
            }
        ],
        "graph_departed_steering_count": 1,
        "alias_count": 0,
        "archive_dir": None,
        "prune_archive_dir": None,
        "graph_prune_archive_dir": None,
        "remaining_instance_ids": [],
    }


def test_reconcile_human_render_carries_the_graph_prune_phase(monkeypatch, capsys):
    monkeypatch.setattr(
        "agent_runtime.persona_instance_identity.reconcile_persona_instances",
        lambda **kwargs: _reconcile_report(),
    )
    args = _parser().parse_args(["harness", "persona-instance", "reconcile", "--dry-run"])

    assert args.func(args) == 0

    out = capsys.readouterr().out
    header = out.splitlines()[0]
    assert "graphs_pruned=1" in header
    assert "graphs_held=1" in header
    assert "graph_steering_settled=1" in header
    assert "  - graph pruned (graph-owner-not-live): runtime_personainst_gone (drew 2 agent(s))" in out
    assert "  - graph held (graph-owner-live): runtime_personainst_dev" in out
    assert (
        "  - graph steering settled: personainst_child -> removed personainst_gone "
        "(runtime_personainst_gone)" in out
    )


def test_reconcile_human_render_stays_quiet_when_the_graph_phase_did_nothing(monkeypatch, capsys):
    report = _reconcile_report()
    report.update(
        graphs_pruned=[],
        graphs_held=[],
        graphs_pruned_count=0,
        graphs_held_count=0,
        graph_departed_steering=[],
        graph_departed_steering_count=0,
    )
    monkeypatch.setattr(
        "agent_runtime.persona_instance_identity.reconcile_persona_instances",
        lambda **kwargs: report,
    )
    args = _parser().parse_args(["harness", "persona-instance", "reconcile", "--dry-run"])

    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "graphs_pruned=0 graphs_held=0 graph_steering_settled=0" in out.splitlines()[0]
    assert "  - graph " not in out


def test_a_departure_that_changed_nothing_is_not_reported_as_a_repair(monkeypatch, capsys):
    """Phase 3's liveness repair normally strips a departed owner's edge before
    the graph phase gets there, so most ``graph_departed_steering`` entries come
    back ``changed: False``. Those are the phases agreeing — the counter and the
    human line both report only real changes, never the agreement."""

    report = _reconcile_report()
    report["graph_departed_steering"] = [
        {
            "persona_instance_id": "personainst_child",
            "owner": "personainst_gone",
            "graph_id": "runtime_personainst_gone",
            "changed": False,
            "steered_by": ["personainst_other"],
        }
    ]
    report["graph_departed_steering_count"] = 0
    monkeypatch.setattr(
        "agent_runtime.persona_instance_identity.reconcile_persona_instances",
        lambda **kwargs: report,
    )
    args = _parser().parse_args(["harness", "persona-instance", "reconcile", "--dry-run"])

    assert args.func(args) == 0

    out = capsys.readouterr().out
    assert "graph_steering_settled=0" in out.splitlines()[0]
    assert "graph steering settled:" not in out
