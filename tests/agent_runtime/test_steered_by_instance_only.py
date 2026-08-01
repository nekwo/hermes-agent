"""Steering fields hold persona-INSTANCE ids only — never a principal.

Regression coverage for the "STEERED BY: operator" defect: the add-instance /
occupied-chat mint seeded ``steered_by=["operator"]`` (and mirrored the operator
principal into ``spawned_by``), which the launcher HUD rendered as a phantom
"steered by operator" relationship. This locks in:

  * the WRITER — ``ensure_for_goal`` seeds ``steered_by`` only from an
    instance-shaped spawn parent (a principal stays in ``spawned_by`` as
    provenance, never in the steering set);
  * the CHOKEPOINT — ``set_parents`` / ``_apply_steer_edges`` reject a
    non-instance-shaped parent loudly with the reason;
  * the REPAIR verb — strips non-instance entries out of the steering fields,
    honoring dry-run (byte-identical store + no event) vs real (mutates + emits);
  * the HUD — a phantom principal is dropped from the steering block while a
    resolved persona/role ref and an instance-shaped "off level" ref are kept.

Autouse conftest fixtures isolate the runtime root.
"""
from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace

import pytest

from agent_runtime import paths
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, PersonaInstance, looks_like_persona_instance_id
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.runtime_hud import resolve_situational_hud
from agent_runtime.states import WorkerSessionState


def _persona(persona_id: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} worker",
        role="dev",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file"],
        system_prompt_path="agent_runtime/prompts/dev.md",
        hermes_profile=f"profile-{persona_id}",
    )


def _assignment_config() -> AgentRuntimeConfig:
    # S56: the persona-instance runtime / assignment store are unconditional now;
    # the enterprise_worker_sessions gate block was deleted.
    return AgentRuntimeConfig()


def _event_count() -> int:
    return sum(1 for _ in EventLog().iter_all())


def _write_corrupt_row(store: PersonaInstanceStore, instance_id: str, *, steered_by, spawned_by) -> None:
    """Persist a legacy corrupt row directly (the fixed mint can no longer make one)."""
    inst = PersonaInstance(
        id=instance_id,
        persona_id="neko_supervisor",
        role="supervisor",
        display_name="Neko Mission Lead (2)",
        profile_id="profile-neko",
        runtime_root="r",
        state=WorkerSessionState.IDLE,
        mode="chat",
        steered_by=list(steered_by),
        spawned_by=spawned_by,
    )
    store._write(inst)  # noqa: SLF001 — test fabricates a pre-fix on-disk row


# ── predicate ──────────────────────────────────────────────────────────────


def test_predicate_accepts_instance_ids_rejects_principals():
    assert looks_like_persona_instance_id("personainst_neko_supervisor_agent_2")
    assert not looks_like_persona_instance_id("operator")
    assert not looks_like_persona_instance_id("neko_supervisor")  # bare persona id
    assert not looks_like_persona_instance_id(None)
    assert not looks_like_persona_instance_id("")


# ── writer: mint never seeds a principal into steered_by ─────────────────────


def test_ensure_for_goal_does_not_seed_steered_by_with_operator_principal():
    store = PersonaInstanceStore()
    inst = store.ensure_for_goal(_persona("neko_supervisor"), goal_id="g1", spawned_by="operator")
    # The exact defect: the add-instance mint used spawned_by="operator".
    assert inst.steered_by == []
    # Provenance is preserved in the scalar, just never treated as steering.
    assert inst.spawned_by == "operator"
    # Survives a re-read (no read-time backfill of the principal either).
    assert store.get(inst.id).steered_by == []


def test_ensure_for_goal_seeds_steered_by_from_instance_shaped_spawn_parent():
    store = PersonaInstanceStore()
    inst = store.ensure_for_goal(_persona("dev"), goal_id="g1", spawned_by="personainst_neko")
    assert inst.steered_by == ["personainst_neko"]
    assert inst.spawned_by == "personainst_neko"


# ── chokepoint: set_parents rejects a non-instance principal ─────────────────


def test_set_parents_rejects_a_non_instance_principal_with_the_reason():
    store = PersonaInstanceStore()
    child = store.ensure_for_goal(_persona("qa"), goal_id="g1", spawned_by=None)
    with pytest.raises(ValueError, match="persona-instance id"):
        store.set_parents(child.id, ["operator"])
    # A bare persona id is also not a resolved steer parent.
    with pytest.raises(ValueError, match="non-instance principal"):
        store.set_parents(child.id, ["neko_supervisor"])
    # The store row is untouched by the rejected write.
    assert store.get(child.id).steered_by == []


def test_set_parents_still_accepts_a_real_instance_parent():
    store = PersonaInstanceStore()
    child = store.ensure_for_goal(_persona("qa"), goal_id="g1", spawned_by=None)
    parent = store.ensure_for_goal(_persona("dev"), goal_id="g1", spawned_by=None)
    result = store.set_parents(child.id, [parent.id])
    assert result.steered_by == [parent.id]
    assert result.spawned_by == parent.id  # mirror restored to a real instance


# ── repair verb: dry-run vs real ─────────────────────────────────────────────


def test_repair_dry_run_is_byte_identical_and_eventless():
    store = PersonaInstanceStore()
    iid = "personainst_neko_supervisor_agent_2"
    _write_corrupt_row(store, iid, steered_by=["operator"], spawned_by="operator")
    path = paths.persona_instance_path(iid)
    before_bytes = path.read_bytes()
    before_events = _event_count()

    result = store.repair_non_instance_steering(iid, apply=False)

    assert result["dry_run"] is True
    assert result["repaired_count"] == 1
    rec = result["repaired"][0]
    assert rec["persona_instance_id"] == iid
    assert rec["steered_by_before"] == ["operator"]
    assert rec["steered_by_after"] == []
    assert rec["removed_steered_by"] == ["operator"]
    # Dry-run mutates nothing and emits nothing.
    assert path.read_bytes() == before_bytes
    assert _event_count() == before_events


def test_repair_real_strips_principal_and_emits_event():
    store = PersonaInstanceStore()
    iid = "personainst_neko_supervisor_agent_2"
    _write_corrupt_row(store, iid, steered_by=["operator"], spawned_by="operator")
    before_events = _event_count()

    result = store.repair_non_instance_steering(iid, apply=True)

    assert result["applied"] is True
    assert result["repaired_count"] == 1
    repaired = store.get(iid)
    assert repaired.steered_by == []
    assert repaired.spawned_by is None  # bogus mirror cleared → clean standalone
    # Chat mode / other fields are NOT changed — this is a steering repair.
    assert repaired.mode == "chat"
    types = [evt.type for evt in EventLog().iter_all()]
    assert "persona_instance.steered" in types
    assert _event_count() > before_events


def test_repair_keeps_instance_parents_and_repoints_mirror():
    store = PersonaInstanceStore()
    parent = store.ensure_for_goal(_persona("dev"), goal_id="g1", spawned_by=None)
    iid = "personainst_neko_supervisor_agent_2"
    _write_corrupt_row(store, iid, steered_by=[parent.id, "operator"], spawned_by="operator")

    result = store.repair_non_instance_steering(iid, apply=True)

    assert result["repaired_count"] == 1
    repaired = store.get(iid)
    assert repaired.steered_by == [parent.id]  # real parent kept
    assert repaired.spawned_by == parent.id  # mirror re-pointed at surviving primary


def test_repair_scan_all_finds_and_skips_clean_rows():
    store = PersonaInstanceStore()
    store.ensure_for_goal(_persona("qa"), goal_id="g1", spawned_by=None)  # clean
    _write_corrupt_row(store, "personainst_neko_supervisor_agent_2", steered_by=["operator"], spawned_by="operator")

    dry = store.repair_non_instance_steering(None, apply=False)
    ids = {rec["persona_instance_id"] for rec in dry["repaired"]}
    assert ids == {"personainst_neko_supervisor_agent_2"}  # only the corrupt row


# ── HUD: phantom principal dropped, real refs kept ───────────────────────────


def _hud_instance(**overrides):
    base = dict(
        id="personainst_neko",
        persona_id="neko_supervisor",
        role="supervisor",
        display_name="Neko Mission Lead",
        goal_id=None,
        current_task_id=None,
        state="idle",
        mode="configured",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_hud_drops_phantom_operator_from_steering_block():
    child = _hud_instance(
        id="personainst_neko_supervisor_agent_2",
        display_name="Neko (2)",
        steered_by=["operator"],
        spawned_by="operator",
    )
    hud = resolve_situational_hud(child, roster=[child])
    # No fake steerer — the lane reads as standalone.
    assert hud["steering"]["steered_by"] == []


def test_hud_keeps_resolved_persona_ref_and_offlevel_instance_ref():
    lead = _hud_instance(id="personainst_neko", persona_id="neko_supervisor")
    child = _hud_instance(
        id="personainst_dev",
        display_name="Dev",
        # persona-id ref (resolves), instance-shaped departed ref (off level),
        # and a phantom principal (dropped).
        steered_by=["neko_supervisor", "personainst_gone", "operator"],
    )
    hud = resolve_situational_hud(child, roster=[lead, child])
    refs = hud["steering"]["steered_by"]
    assert refs[0]["persona_instance_id"] == "personainst_neko"  # resolved
    assert refs[1] == {"ref": "personainst_gone"}  # off level, kept
    assert len(refs) == 2  # "operator" dropped


# ── CLI verb: argparse wiring + dry-run/real through the handler ─────────────


def _repair_args(persona_instance_id=None, *, scan_all=False, dry_run=False):
    return Namespace(persona_instance_id=persona_instance_id, all=scan_all, dry_run=dry_run, json=True)


def test_cli_repair_steering_dry_run_then_real(monkeypatch, capsys):
    import json as _json

    from hermes_cli import harness

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    store = PersonaInstanceStore()
    iid = "personainst_neko_supervisor_agent_2"
    _write_corrupt_row(store, iid, steered_by=["operator"], spawned_by="operator")

    code = harness._cmd_persona_instance_repair_steering(_repair_args(iid, dry_run=True))
    out = capsys.readouterr().out
    data = _json.loads(out[out.index("{"): out.rindex("}") + 1])
    assert code == 0 and data["dry_run"] is True
    assert store.get(iid).steered_by == ["operator"]  # dry-run mutated nothing

    code = harness._cmd_persona_instance_repair_steering(_repair_args(iid))
    assert code == 0
    assert store.get(iid).steered_by == []
    assert store.get(iid).spawned_by is None


def test_cli_repair_steering_requires_a_target_or_all(monkeypatch, capsys):
    from hermes_cli import harness

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    code = harness._cmd_persona_instance_repair_steering(_repair_args(None))
    assert code == 2
