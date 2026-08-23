"""CLI wiring for the checkpoint lane: `harness checkpoint fetch|classes`.

Stage S5 — the read model's per-actor recovery/hydrate substrate. These drive
the REAL parser (build_parser) against an isolated runtime root, so a
registration slip (part not exec'd, flag renamed) fails here before it fails in
the Launcher's bridge.
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
    args = parser().parse_args(["harness", "checkpoint", *argv])
    exit_code = args.func(args)
    out = capsys.readouterr().out.strip()
    return exit_code, json.loads(out) if out.startswith("{") else out


def _seed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from tests.agent_runtime.persona_instance_mint import mint_free_floating

    store = PersonaInstanceStore()
    lead = mint_free_floating("profile:lead", store=store)
    dev = mint_free_floating("profile:dev", store=store)
    return store, lead, dev


def test_checkpoint_fetch_bundles_actors(tmp_path, monkeypatch, capsys):
    _store, lead, dev = _seed(monkeypatch, tmp_path)

    exit_code, data = _run(capsys, "fetch", "--json")

    assert exit_code == 0
    assert data["checkpoint_version"] == 1
    instances = data["classes"]["persona_instances"]
    assert lead.id in instances and dev.id in instances
    assert data["counts"]["persona_instances"] == 2
    assert data["watermark"]["event_offset"] >= 0


def test_checkpoint_fetch_class_filter(tmp_path, monkeypatch, capsys):
    _seed(monkeypatch, tmp_path)

    exit_code, data = _run(capsys, "fetch", "--classes", "persona_instances", "--json")

    assert exit_code == 0
    assert set(data["classes"]) == {"persona_instances"}


def test_checkpoint_fetch_row_cap_truncates_with_accounting(tmp_path, monkeypatch, capsys):
    _seed(monkeypatch, tmp_path)

    exit_code, data = _run(
        capsys, "fetch", "--classes", "persona_instances", "--max-rows", "1", "--json"
    )

    assert exit_code == 0
    assert data["counts"]["persona_instances"] == 1
    assert data["truncations"]["persona_instances"] == {
        "truncated": True,
        "total": 2,
        "returned": 1,
    }


def test_checkpoint_classes_lists_discovered_counts(tmp_path, monkeypatch, capsys):
    _seed(monkeypatch, tmp_path)

    exit_code, data = _run(capsys, "classes", "--json")

    assert exit_code == 0
    by_name = {entry["class"]: entry for entry in data["classes"]}
    assert by_name["persona_instances"]["count"] == 2
    assert "persona_instances" in data["discovered"]


def test_checkpoint_fetch_human_output_when_not_json(tmp_path, monkeypatch, capsys):
    _seed(monkeypatch, tmp_path)

    exit_code, out = _run(capsys, "fetch")

    assert exit_code == 0
    assert "checkpoint v1" in out
    assert "persona_instances: 2" in out
