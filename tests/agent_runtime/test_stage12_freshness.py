"""Stage 12 read-path freshness hardening tests.

Mission Control's read path is watermark-gated on the EventLog offset, so any
store mutation that changes client-visible snapshot state without appending an
event is invisible to every stream consumer (docs/agent-runtime-harness/
12-read-path-freshness-hardening.md). These tests pin the Stage 12 fixes:
event-coupled mutations at the store chokepoint, the blueprint-save event, the
stream fingerprint backstop, and contract-validated appends.
"""

from types import SimpleNamespace

import yaml

from agent_runtime.events import EventLog


def _write_valid_blueprint_spec(tmp_path):
    from agent_runtime.blueprints.store import BlueprintStore, blueprint_to_dict

    bp = BlueprintStore().list()[0]
    spec = tmp_path / "spec.yaml"
    spec.write_text(
        yaml.safe_dump(blueprint_to_dict(bp), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return bp, spec


def test_blueprint_save_appends_event(tmp_path, monkeypatch, capsys):
    """`blueprint save` mutates the client-visible catalog; it must advance the
    EventLog watermark or the launcher blueprint list freezes (Stage 12 slice A)."""

    import agent_runtime.blueprints.store as bp_store
    from hermes_cli.harness import _cmd_blueprint_save

    bp, spec = _write_valid_blueprint_spec(tmp_path)
    original_save = bp_store.save_blueprint
    monkeypatch.setattr(
        bp_store,
        "save_blueprint",
        lambda bp, *, root=None: original_save(bp, root=tmp_path / "catalog"),
    )

    assert _cmd_blueprint_save(SimpleNamespace(spec_file=str(spec), json=True)) == 0
    capsys.readouterr()

    events = EventLog().tail(1)
    assert events and events[0].type == "blueprint.saved"
    assert events[0].payload["blueprint_id"] == bp.id


def test_blueprint_save_failure_appends_no_event(tmp_path, monkeypatch, capsys):
    import agent_runtime.blueprints.store as bp_store
    from hermes_cli.harness import _cmd_blueprint_save

    # Defense in depth: even if the invalid spec somehow validated, the write
    # must land in tmp, never the repo catalog.
    original_save = bp_store.save_blueprint
    monkeypatch.setattr(
        bp_store,
        "save_blueprint",
        lambda bp, *, root=None: original_save(bp, root=tmp_path / "catalog"),
    )
    bad_spec = tmp_path / "bad.yaml"
    # An edge naming an unknown stage is rejected by blueprint_from_dict.
    bad_spec.write_text(
        "id: broken\nedges:\n  - source: missing\n    outcome: approved\n    target: also_missing\n",
        encoding="utf-8",
    )

    assert _cmd_blueprint_save(SimpleNamespace(spec_file=str(bad_spec), json=True)) == 2
    capsys.readouterr()
    assert EventLog().tail(1) == []
    assert not (tmp_path / "catalog").exists()
