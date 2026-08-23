"""CLI wiring for the flow-doc lane: `harness flow set|show|list`.

The Launcher pushes the whole authored chart through `flow set --graph <json>`
— one spawn per sync instead of one `persona instance steer` spawn per agent.
These tests drive the REAL parser (build_parser) against an isolated runtime
root, so a registration slip (part not exec'd, flag renamed) fails here before
it fails in the Launcher's bridge.
"""

import argparse
import json

from hermes_cli.harness import build_parser


def parser():
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command")
    build_parser(subs)
    return p


def _run(capsys, *argv):
    args = parser().parse_args(["harness", "flow", *argv])
    exit_code = args.func(args)
    out = capsys.readouterr().out.strip()
    return exit_code, json.loads(out) if out.startswith("{") else out


def _instances(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from tests.agent_runtime.persona_instance_mint import mint_free_floating

    store = PersonaInstanceStore()
    lead = mint_free_floating("profile:lead", store=store)
    dev = mint_free_floating("profile:dev", store=store)
    return store, lead, dev


def test_flow_set_ingests_inline_graph(tmp_path, monkeypatch, capsys):
    store, lead, dev = _instances(monkeypatch, tmp_path)
    graph = {
        "graph_id": lead.id,
        "nodes": [
            {"id": "n1", "agent": lead.id, "x": 0, "y": 0},
            {"id": "n2", "agent": dev.id, "x": 1, "y": 1},
        ],
        "edges": [{"from": "n1", "to": "n2"}],
    }

    exit_code, data = _run(
        capsys, "set", "--graph", json.dumps(graph), "--requested-by", "launcher", "--json"
    )

    assert exit_code == 0
    assert data["ok"] is True
    assert data["failed_count"] == 0
    assert store.get(dev.id).steered_by == [lead.id]


def test_flow_set_requires_exactly_one_source(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    exit_code, data = _run(capsys, "set", "--json")
    assert exit_code == 2
    assert data["ok"] is False
    assert "exactly one" in data["error"]


def test_flow_set_invalid_doc_is_typed_rejection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    exit_code, data = _run(capsys, "set", "--graph", '{"nodes": []}', "--json")
    assert exit_code == 2
    assert data["ok"] is False
    assert "graph_id" in data["error"]


def test_flow_set_reads_graph_file_and_show_lists_round_trip(tmp_path, monkeypatch, capsys):
    store, lead, dev = _instances(monkeypatch, tmp_path)
    graph_file = tmp_path / "chart.json"
    graph_file.write_text(
        json.dumps(
            {
                "graph_id": lead.id,
                "nodes": [
                    {"id": "n1", "agent": lead.id, "x": 0, "y": 0},
                    {"id": "n2", "agent": dev.id, "x": 1, "y": 1},
                ],
                "edges": [{"from": "n1", "to": "n2"}],
            }
        ),
        encoding="utf-8",
    )

    exit_code, data = _run(capsys, "set", "--graph-file", str(graph_file), "--json")
    assert exit_code == 0 and data["ok"] is True

    exit_code, shown = _run(capsys, "show", lead.id, "--json")
    assert exit_code == 0
    assert shown["doc"]["edges"] == [{"from": "n1", "to": "n2"}]

    exit_code, listed = _run(capsys, "list", "--json")
    assert exit_code == 0
    assert listed["graph_ids"] == [lead.id]
