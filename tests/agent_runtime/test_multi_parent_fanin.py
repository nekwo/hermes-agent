"""Stage 77 multi-parent fan-in: a persona instance may be steered by >=2 parents.

Covers the store set-operations, spawned_by mirror, legacy backfill, DAG cycle
guard, and the kept persona-instance steering CLI verbs.
"""
from __future__ import annotations

import json

import pytest

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.models import AgentPersona, PersonaInstance
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.serde import from_jsonable, to_jsonable
from agent_runtime.states import WorkerSessionState


def _persona(persona_id: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=f"{persona_id} worker",
        role="dev",
        model="gpt-test",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal"],
        system_prompt_path="agent_runtime/prompts/dev.md",
        hermes_profile=f"profile-{persona_id}",
    )


def _assignment_config() -> AgentRuntimeConfig:
    # S56: the persona-instance runtime / assignment store are unconditional now;
    # the enterprise_worker_sessions gate block was deleted.
    return AgentRuntimeConfig()


def _make(store: PersonaInstanceStore, persona_id: str, *, goal_id: str = "g1") -> PersonaInstance:
    return store.ensure_for_goal(_persona(persona_id), goal_id=goal_id, spawned_by=None)


# --- store: set-operations + primary mirror -------------------------------


def test_set_parents_replaces_and_mirrors_primary():
    store = PersonaInstanceStore()
    child = _make(store, "qa")
    p1 = _make(store, "dev")
    p2 = _make(store, "backend_dev")

    result = store.set_parents(child.id, [p1.id, p2.id])

    assert result.steered_by == [p1.id, p2.id]
    # spawned_by is the denormalized mirror of the primary (first) parent.
    assert result.spawned_by == p1.id
    # persisted, survives a re-read.
    assert store.get(child.id).steered_by == [p1.id, p2.id]


def test_add_parent_is_idempotent_union():
    store = PersonaInstanceStore()
    child = _make(store, "qa")
    p1 = _make(store, "dev")
    p2 = _make(store, "backend_dev")

    store.set_parents(child.id, [p1.id])
    # adding an existing parent is a no-op set-union.
    assert store.add_parent(child.id, p1.id).steered_by == [p1.id]
    # adding a new parent unions in.
    assert store.add_parent(child.id, p2.id).steered_by == [p1.id, p2.id]


def test_remove_parent_detaches_one_then_last():
    store = PersonaInstanceStore()
    child = _make(store, "qa")
    p1 = _make(store, "dev")
    p2 = _make(store, "backend_dev")
    store.set_parents(child.id, [p1.id, p2.id], goal_id="g1")

    after_one = store.remove_parent(child.id, p1.id)
    assert after_one.steered_by == [p2.id]
    assert after_one.spawned_by == p2.id  # mirror follows the new primary

    # removing the last parent detaches the child entirely.
    detached = store.remove_parent(child.id, p2.id)
    assert detached.steered_by == []
    assert detached.spawned_by is None
    assert detached.goal_id is None
    assert detached.mode == "configured"


def test_detach_parents_clears_all():
    store = PersonaInstanceStore()
    child = _make(store, "qa")
    p1 = _make(store, "dev")
    store.set_parents(child.id, [p1.id])

    detached = store.detach_parents(child.id)
    assert detached.steered_by == []
    assert detached.spawned_by is None


def test_back_compat_steer_replaces_and_detaches():
    store = PersonaInstanceStore()
    child = _make(store, "qa")
    p1 = _make(store, "dev")
    p2 = _make(store, "backend_dev")

    # --parent semantics preserved exactly: REPLACE the whole set with one.
    store.set_parents(child.id, [p1.id, p2.id])
    replaced = store.steer(child.id, parent_instance_id=p1.id)
    assert replaced.steered_by == [p1.id]

    detached = store.steer(child.id, parent_instance_id=None, detach=True)
    assert detached.steered_by == []


def test_cycle_guard_rejects_but_allows_diamond():
    store = PersonaInstanceStore()
    g = _make(store, "neko_supervisor")
    p1 = _make(store, "dev")
    p2 = _make(store, "backend_dev")
    c = _make(store, "qa")

    store.set_parents(p1.id, [g.id])
    store.set_parents(p2.id, [g.id])
    # Diamond: c steered by p1 AND p2, both sharing ancestor g — NOT a cycle.
    store.set_parents(c.id, [p1.id, p2.id])
    assert set(store.get(c.id).steered_by) == {p1.id, p2.id}

    # A real cycle: making g a child of c (c already reaches g via p1/p2).
    with pytest.raises(ValueError, match="cycle"):
        store.set_parents(g.id, [c.id])
    # self-steer is rejected too.
    with pytest.raises(ValueError, match="cannot steer itself"):
        store.set_parents(c.id, [c.id])


# --- store: id-scheme drift tolerance at the steer boundary ---------------
# Regression: the Launcher graph save shipped a node's raw ownerSlot to
# `persona instance steer`. The roster preserves raw ids, so a drifted
# actor-token id (`persona_personainst_x`) reached the store, `get()` did a
# literal filename read, and the steer failed with "parent persona instance not
# found" — the edit never persisted and closing the graph editor reverted it.


def test_get_resolves_actor_token_drift_to_the_real_row():
    from agent_runtime.persona_assignments import canonical_persona_instance_id

    store = PersonaInstanceStore()
    inst = _make(store, "backend_dev")
    drift = f"persona_{inst.id}"  # persona_personainst_..._backend_dev
    # Precondition: this IS the actor-token drift the identity authority strips.
    assert canonical_persona_instance_id(drift) == inst.id
    assert drift != inst.id

    # The literal file is missing, so get() resolves the drift to the real row.
    assert store.get(drift).id == inst.id


def test_get_still_raises_for_a_genuinely_missing_row():
    store = PersonaInstanceStore()
    with pytest.raises(FileNotFoundError):
        store.get("personainst_does_not_exist")


def test_set_parents_canonicalizes_drifted_parent_ids():
    store = PersonaInstanceStore()
    child = _make(store, "qa")
    p1 = _make(store, "dev")
    p2 = _make(store, "backend_dev")

    # The operator wired the graph from nodes carrying the drifted ids.
    result = store.set_parents(child.id, [f"persona_{p1.id}", f"persona_{p2.id}"])

    # The steer lands, and the PERSISTED set is canonical — never the drift —
    # so the next snapshot re-emits ids the Launcher graph can resolve.
    assert result.steered_by == [p1.id, p2.id]
    assert result.spawned_by == p1.id
    assert store.get(child.id).steered_by == [p1.id, p2.id]


def test_set_parents_dedupes_a_drift_and_canonical_twin_of_one_parent():
    store = PersonaInstanceStore()
    child = _make(store, "qa")
    p1 = _make(store, "dev")

    # The same parent named two ways collapses to a single canonical edge.
    result = store.set_parents(child.id, [p1.id, f"persona_{p1.id}"])
    assert result.steered_by == [p1.id]


# --- model: legacy backfill ----------------------------------------------


def test_legacy_record_backfills_steered_by_from_instance_shaped_spawned_by():
    # A constructed instance with only the scalar parent seeds the set — but ONLY
    # when that scalar is instance-shaped (a real steer parent), never a principal.
    inst = PersonaInstance(
        id="x",
        persona_id="dev",
        role="dev",
        display_name="d",
        profile_id=None,
        runtime_root="r",
        state=WorkerSessionState.IDLE,
        spawned_by="personainst_parent_1",
    )
    assert inst.steered_by == ["personainst_parent_1"]

    # A serialized legacy dict (no steered_by key) round-trips with the backfill.
    raw = to_jsonable(inst)
    raw.pop("steered_by", None)
    reloaded = from_jsonable(PersonaInstance, raw)
    assert reloaded.steered_by == ["personainst_parent_1"]
    assert reloaded.schema_version == 1  # not bumped (serde upgrade() hard-gates)


def test_backfill_does_not_mirror_a_non_instance_principal_into_steered_by():
    # Regression: ``spawned_by`` doubles as a provenance scalar and can hold a
    # principal (operator add-instance ⇒ spawned_by="operator"). Mirroring that
    # into steered_by is exactly the defect that made the HUD say "steered by
    # operator" — the set stays empty for a non-instance-shaped scalar.
    inst = PersonaInstance(
        id="x",
        persona_id="neko_supervisor",
        role="supervisor",
        display_name="d",
        profile_id=None,
        runtime_root="r",
        state=WorkerSessionState.IDLE,
        spawned_by="operator",
    )
    assert inst.steered_by == []
    assert inst.spawned_by == "operator"  # provenance preserved, just not steering

    raw = to_jsonable(inst)
    raw.pop("steered_by", None)
    reloaded = from_jsonable(PersonaInstance, raw)
    assert reloaded.steered_by == []


def _steer_args(persona_instance_id: str, **overrides):
    from argparse import Namespace

    base = dict(
        persona_instance_id=persona_instance_id,
        parent_instance_id=None,
        add_parent=None,
        remove_parent=None,
        set_parents=None,
        goal_id=None,
        detach=False,
        requested_by="operator",
        coordinator_id="neko_supervisor",
        coordinator_max_spawns=None,
        coordinator_spawns_used=0,
        coordinator_may_kill_own=None,
        coordinator_no_kill_own=None,
        coordinator_may_kill_others=None,
        json=True,
    )
    base.update(overrides)
    return Namespace(**base)


def _run_steer(harness, capsys, args):
    code = harness._cmd_persona_instance_steer(args)
    raw = capsys.readouterr().out
    # emit_json pretty-prints across lines; take the whole JSON object (tolerating
    # any provider log lines around it).
    return code, json.loads(raw[raw.index("{"): raw.rindex("}") + 1])


def test_cli_steer_verbs_and_json_shape(monkeypatch, capsys):
    from hermes_cli import harness

    monkeypatch.setattr(harness, "load_agent_runtime_config", _assignment_config)
    store = PersonaInstanceStore()
    store.ensure_for_persona(_persona("qa"))
    store.ensure_for_persona(_persona("dev"))
    store.ensure_for_persona(_persona("backend_dev"))
    child, p1, p2 = "personainst_qa", "personainst_dev", "personainst_backend_dev"

    # --set-parents: declarative replace (fan-in).
    code, data = _run_steer(harness, capsys, _steer_args(child, set_parents=[p1, p2]))
    assert code == 0 and data["ok"] is True
    assert data["steered_by"] == [p1, p2]
    assert set(data["added"]) == {p1, p2} and data["removed"] == []
    assert data["detached"] is False
    assert data["instance"]["steered_by"] == [p1, p2]
    assert data["instance"]["spawned_by"] == p1  # mirror = primary

    # --remove-parent: detach-one, reports the delta.
    code, data = _run_steer(harness, capsys, _steer_args(child, remove_parent=p1))
    assert code == 0
    assert data["steered_by"] == [p2]
    assert data["removed"] == [p1] and data["added"] == []

    # --add-parent: additive union.
    code, data = _run_steer(harness, capsys, _steer_args(child, add_parent=p1))
    assert code == 0
    assert set(data["steered_by"]) == {p1, p2}
    assert data["added"] == [p1]

    # --parent alias: replace-with-one (back-compat).
    code, data = _run_steer(harness, capsys, _steer_args(child, parent_instance_id=p2))
    assert code == 0
    assert data["steered_by"] == [p2]

    # --detach: clear all.
    code, data = _run_steer(harness, capsys, _steer_args(child, detach=True))
    assert code == 0
    assert data["steered_by"] == [] and data["detached"] is True

    # mutually-exclusive verbs are rejected.
    code, data = _run_steer(harness, capsys, _steer_args(child, add_parent=p1, remove_parent=p2))
    assert code == 2 and data["ok"] is False
    assert "mutually exclusive" in data["error"]
