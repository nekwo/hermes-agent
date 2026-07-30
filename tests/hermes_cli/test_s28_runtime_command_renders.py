"""S28: the CLI half of the S21 hollow-seam cut.

Two human-readable renders in ``hermes_cli/harness_parts/runtime_commands.py``
were out of step with what the runtime can actually report:

* ``_cmd_status`` opened its line with ``open_tasks=`` / ``running_runs=``, two
  fields ``build_status`` could only ever compute as ``0`` (S21 named both and
  had to leave them because this module was owned by another lane).
* ``_cmd_observe`` passed ``tasks=[]`` / ``proofs=[]`` / ``daemon_status=None``
  keywords into ``build_observability`` -- the literals that kept three dead
  parameters alive on the other side of the call.

The rule these share: the human line must mention exactly what the verb can
still measure -- no constant dressed as a count. The third render fixed in this
wave, ``_cmd_persona_instance_reconcile``'s silent graph-prune phase, is a
separate concern and lives in ``test_s28_reconcile_graph_render.py``.
"""

from __future__ import annotations

import argparse

from hermes_cli.harness import build_parser


def _parser():
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser


def _status_payload() -> dict:
    """A status dict shaped like the post-S28 payload: no constant task/run
    fields at all, so a render that still indexes them raises ``KeyError``."""

    return {
        "open_incidents": 3,
        "dirty_summary": "runtime=clean",
        "runtime_health": {"ok": True},
    }


def test_status_human_line_reports_only_measurable_fields(monkeypatch, capsys):
    monkeypatch.setattr("hermes_cli.harness.build_status", _status_payload)
    args = _parser().parse_args(["harness", "status"])

    assert args.func(args) == 0

    line = capsys.readouterr().out.strip()
    assert line == "open_incidents=3 dirty=runtime=clean runtime_health=True"
    assert "open_tasks=" not in line
    assert "running_runs=" not in line


def test_observe_passes_no_literal_fed_parameters(monkeypatch, capsys):
    observed: dict = {}

    def fake_build_observability(**kwargs):
        observed.update(kwargs)
        return {"health": {"status": "healthy"}, "interventions": []}

    monkeypatch.setattr("hermes_cli.harness.build_observability", fake_build_observability)
    args = _parser().parse_args(["harness", "observe"])

    assert args.func(args) == 0

    assert set(observed) == {"runs", "incidents", "events", "execution_mode", "worker_sessions"}
    assert capsys.readouterr().out.strip() == "observability=healthy interventions=0"

