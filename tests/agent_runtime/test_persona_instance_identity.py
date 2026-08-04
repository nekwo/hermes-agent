"""Persona-instance identity: canonical-id chokepoint + store reconciliation.

Golden corpus = the live 2026-07-10 store in which one logical channel
persisted under four id schemes (neko_supervisor had three rows, profile:alice
two) and Mission Control rendered duplicate agent cards.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime import paths
from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.events import EventLog
from agent_runtime.models import PersonaInstance
from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    canonical_persona_instance_id,
    persona_instance_id_for,
)
from agent_runtime.persona_instance_identity import (
    backed_persona_identity,
    classify_orphan_persona_instances,
    duplicate_persona_instance_groups,
    identity_aliases_for_rows,
    load_persona_instance_aliases,
    reconcile_persona_instances,
)
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import WorkerSessionState


def _runtime_config() -> AgentRuntimeConfig:
    # S56: the persona-instance runtime / assignment store are unconditional now;
    # the enterprise_worker_sessions gate block was deleted.
    return AgentRuntimeConfig()


def _seed_row(
    instance_id: str,
    *,
    persona_id: str,
    display_name: str,
    mode: str = "chat",
    session_id: str | None = None,
    profile_id: str | None = None,
    spawned_by: str | None = None,
    steered_by: list[str] | None = None,
    updated_at=None,
) -> PersonaInstance:
    """Write a store row verbatim — the lane legacy drifted rows arrived by,
    bypassing today's creation-path canonicalization."""
    instance = PersonaInstance(
        id=instance_id,
        persona_id=persona_id,
        role="profile" if persona_id.startswith("profile:") else persona_id,
        display_name=display_name,
        profile_id=profile_id,
        spawned_by=spawned_by,
        steered_by=list(steered_by or []),
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
        mode=mode,
        session_id=session_id,
        updated_at=updated_at or now(),
    )
    path = paths.persona_instance_path(instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(instance), indent=2, sort_keys=True), encoding="utf-8")
    return instance


def _live_row_ids() -> set[str]:
    return {path.stem for path in paths.persona_instances_dir().glob("*.json")}


def test_canonical_persona_instance_id_table():
    # Actor-token drift strips to the instance id.
    assert (
        canonical_persona_instance_id("persona_personainst_neko_supervisor")
        == "personainst_neko_supervisor"
    )
    # Launcher 'persona:<persona_id>' selector tokens resolve to the persona's
    # operator channel when the persona is known.
    assert (
        canonical_persona_instance_id("persona:neko_supervisor", persona_id="neko_supervisor")
        == "personainst_neko_supervisor"
    )
    # Already-canonical and placement ids pass through untouched.
    assert canonical_persona_instance_id("personainst_profile_alice") == "personainst_profile_alice"
    assert canonical_persona_instance_id("personainst_alice_agent_2") == "personainst_alice_agent_2"
    # Unknown persona_ prefixes without the instance marker are not drift.
    assert canonical_persona_instance_id("persona_dev") == "persona_dev"
    assert canonical_persona_instance_id("") is None
    assert canonical_persona_instance_id(None) is None


def test_open_chat_cannot_mint_actor_token_drift_rows():
    store = PersonaInstanceStore()
    instance = store.open_chat(
        persona_id="neko_supervisor",
        session_id="persona_chat_test_session",
        persona_instance_id="persona:personainst_neko_supervisor",
    )
    assert instance.id == "personainst_neko_supervisor"
    assert "persona_personainst_neko_supervisor" not in _live_row_ids()


def test_reconcile_folds_the_live_four_scheme_corpus():
    older = datetime(2026, 7, 8, 9, 29, tzinfo=timezone.utc)
    newer = datetime(2026, 7, 10, 19, 24, tzinfo=timezone.utc)
    # Canonical rows (newest wins on the neko channel).
    _seed_row(
        "personainst_neko_supervisor",
        persona_id="neko_supervisor",
        display_name="Neko Supervisor",
        session_id="persona_chat_current",
        updated_at=newer,
    )
    _seed_row(
        "personainst_profile_alice",
        persona_id="profile:alice",
        display_name="Alice Agent",
        profile_id="alice",
        updated_at=newer,
    )
    # Legacy rows: actor-token drift + two retired operator-hash channels.
    _seed_row(
        "persona_personainst_neko_supervisor",
        persona_id="neko_supervisor",
        display_name="Neko Mission Lead",
        session_id="persona_chat_stale",
        updated_at=older,
    )
    _seed_row(
        "personainst_operator_c2728fe710acb898",
        persona_id="neko_supervisor",
        display_name="Neko Mission Lead",
        updated_at=older,
    )
    _seed_row(
        "personainst_operator_2c1f1de674e74942",
        persona_id="profile:alice",
        display_name="Alice Agent",
        profile_id="alice",
        updated_at=older,
    )
    # A deliberate extra instance (add_instance) must never be folded.
    _seed_row(
        "personainst_alice_agent_2",
        persona_id="profile:alice",
        display_name="Alice Agent 2",
        profile_id="alice",
        updated_at=newer,
    )

    report = reconcile_persona_instances(event_log=EventLog())

    assert report["applied"] is True
    assert report["merged_count"] == 3
    assert report["renamed_count"] == 0
    assert report["skipped_count"] == 0
    assert sorted(report["remaining_instance_ids"]) == [
        "personainst_alice_agent_2",
        "personainst_neko_supervisor",
        "personainst_profile_alice",
    ]
    assert _live_row_ids() == {
        "personainst_alice_agent_2",
        "personainst_neko_supervisor",
        "personainst_profile_alice",
    }
    # Legacy files are archived, never deleted.
    archived = list(paths.persona_instances_archive_dir().rglob("*.json"))
    assert {path.stem for path in archived} == {
        "persona_personainst_neko_supervisor",
        "personainst_operator_c2728fe710acb898",
        "personainst_operator_2c1f1de674e74942",
    }
    # The alias registry resolves every retired id.
    aliases = load_persona_instance_aliases()
    assert aliases["persona_personainst_neko_supervisor"] == "personainst_neko_supervisor"
    assert aliases["personainst_operator_c2728fe710acb898"] == "personainst_neko_supervisor"
    assert aliases["personainst_operator_2c1f1de674e74942"] == "personainst_profile_alice"
    # The canonical row kept its live session (legacy rows were older).
    survivor = PersonaInstanceStore().get("personainst_neko_supervisor")
    assert survivor.session_id == "persona_chat_current"

    # Idempotence: a second run does nothing and emits nothing.
    second = reconcile_persona_instances(event_log=EventLog())
    assert second["actions"] == []
    assert second["merged_count"] == 0
    reconciled_events = [
        event for event in EventLog().tail(50) if _event_type(event) == "persona_instance.reconciled"
    ]
    assert len(reconciled_events) == 3


def test_reconcile_renames_when_no_canonical_row_exists():
    _seed_row(
        "persona_personainst_backend_dev",
        persona_id="backend_dev",
        display_name="Backend Dev Agent",
        session_id="persona_chat_backend",
    )

    report = reconcile_persona_instances(event_log=EventLog())

    assert report["renamed_count"] == 1
    assert _live_row_ids() == {"personainst_backend_dev"}
    survivor = PersonaInstanceStore().get("personainst_backend_dev")
    assert survivor.session_id == "persona_chat_backend"
    assert survivor.display_name == "Backend Dev Agent"


def test_reconcile_never_folds_task_bound_or_cross_persona_rows():
    _seed_row(
        "personainst_task_a_dev",
        persona_id="dev",
        display_name="Launcher Dev Agent",
        mode="task_bound",
    )
    # Operator-hash id whose canonical slot is owned by ANOTHER persona.
    _seed_row(
        "personainst_dev",
        persona_id="dev",
        display_name="Launcher Dev Agent",
    )
    _seed_row(
        "personainst_operator_ffffffffffffffff",
        persona_id="qa",
        display_name="QA Agent",
    )
    _seed_row(
        "personainst_qa",
        persona_id="dev",  # deliberately conflicting owner
        display_name="Launcher Dev Agent",
    )

    report = reconcile_persona_instances(event_log=EventLog())

    assert report["skipped_count"] == 1
    assert "personainst_task_a_dev" in report["remaining_instance_ids"]
    assert "personainst_operator_ffffffffffffffff" in report["remaining_instance_ids"]


def test_snapshot_emits_identity_map_and_duplicate_warning(monkeypatch):
    cfg = _runtime_config()
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    _seed_row(
        "personainst_neko_supervisor",
        persona_id="neko_supervisor",
        display_name="Neko Supervisor",
    )
    _seed_row(
        "persona_personainst_neko_supervisor",
        persona_id="neko_supervisor",
        display_name="Neko Mission Lead",
    )

    snapshot = build_snapshot()

    assert (
        snapshot["identity_map"]["persona_personainst_neko_supervisor"]
        == "personainst_neko_supervisor"
    )
    duplicate_warnings = [
        warning
        for warning in snapshot["parity"]["warnings"]
        if warning.get("code") == "duplicate_persona_instance"
    ]
    assert len(duplicate_warnings) == 1
    assert duplicate_warnings[0]["entity_id"] == "personainst_neko_supervisor"
    assert sorted(duplicate_warnings[0]["instance_ids"]) == [
        "persona_personainst_neko_supervisor",
        "personainst_neko_supervisor",
    ]

    # After reconciliation the warning clears but the alias survives in the
    # registry so archived history still resolves.
    reconcile_persona_instances(event_log=EventLog())
    healed = build_snapshot()
    assert not [
        warning
        for warning in healed["parity"]["warnings"]
        if warning.get("code") == "duplicate_persona_instance"
    ]
    assert (
        healed["identity_map"]["persona_personainst_neko_supervisor"]
        == "personainst_neko_supervisor"
    )


def test_snapshot_reports_shape_valid_missing_steering_foreign_keys(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.snapshot.load_agent_runtime_config",
        lambda: _runtime_config(),
    )
    missing = "personainst_neko_supervisor_agent_gone"
    _seed_row(
        "personainst_dev_agent_live",
        persona_id="dev",
        display_name="Dev",
        spawned_by=missing,
        steered_by=[missing],
    )

    warnings = [
        warning
        for warning in build_snapshot()["parity"]["warnings"]
        if warning.get("code") == "fk_miss"
        and warning.get("from_entity") == "persona_instance"
    ]

    assert {(warning["fk_field"], warning["target_id"]) for warning in warnings} == {
        ("spawned_by", missing),
        ("steered_by", missing),
    }


def test_reconcile_repairs_missing_steering_foreign_keys_and_is_idempotent():
    missing = "personainst_neko_supervisor_agent_gone"
    child = _seed_row(
        "personainst_dev_agent_live",
        persona_id="dev",
        display_name="Dev",
        mode="free_floating",
        spawned_by=missing,
        steered_by=[missing],
    )

    report = reconcile_persona_instances(event_log=EventLog())
    repaired = PersonaInstanceStore().get(child.id)

    assert report["steering_repaired_count"] == 1
    assert report["steering_repairs"][0]["missing_parent_ids"] == [missing]
    assert repaired.steered_by == []
    assert repaired.spawned_by is None
    assert repaired.mode == "free_floating"

    again = reconcile_persona_instances(event_log=EventLog())
    assert again["steering_repaired_count"] == 0


def test_identity_alias_helpers_are_pure_over_rows():
    rows = [
        {"persona_instance_id": "personainst_profile_alice", "persona_id": "profile:alice", "mode": "chat"},
        {"persona_instance_id": "personainst_operator_2c1f1de674e74942", "persona_id": "profile:alice", "mode": "chat"},
        {"persona_instance_id": "personainst_task_x_dev", "persona_id": "dev", "mode": "task_bound"},
    ]
    aliases = identity_aliases_for_rows(rows)
    assert aliases["personainst_operator_2c1f1de674e74942"] == "personainst_profile_alice"
    assert "personainst_task_x_dev" not in aliases
    groups = duplicate_persona_instance_groups(rows)
    assert groups == [
        {
            "canonical_id": "personainst_profile_alice",
            "instance_ids": [
                "personainst_operator_2c1f1de674e74942",
                "personainst_profile_alice",
            ],
        }
    ]


def _event_type(event) -> str:
    if isinstance(event, dict):
        return str(event.get("type") or "")
    return str(getattr(event, "type", "") or "")


def test_persona_instance_id_for_matches_canonical_of_selector():
    # The two derivation functions must agree: canonicalizing the launcher's
    # selector for a persona yields exactly persona_instance_id_for.
    for persona_id in ("dev", "backend_dev", "qa", "neko_supervisor", "profile:alice"):
        assert (
            canonical_persona_instance_id(f"persona:{persona_id}", persona_id=persona_id)
            == persona_instance_id_for(persona_id)
        )


# ── Orphan / legacy-role prune ──────────────────────────────────────────────

_FIXED_NOW = datetime(2026, 7, 12, 20, 0, 0, tzinfo=timezone.utc)
_STALE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)  # ~6 weeks old
_FRESH = datetime(2026, 7, 12, 19, 30, 0, tzinfo=timezone.utc)  # 30 min old


def _backed_universe():
    from tests.agent_runtime.persona_samples import sample_personas

    return backed_persona_identity(
        agents=sample_personas(), profile_names=["alice", "base", "backend-dev"]
    )


def _orphan_row(instance_id, **over):
    row = {
        "persona_instance_id": instance_id,
        "persona_id": "profile:ghost",
        "role": "profile",
        "profile_id": "ghost",
        "mode": "configured",
        "updated_at": _STALE.isoformat(),
        "last_heartbeat_at": None,
        "active_worker_session_id": None,
        "active_run_id": None,
        "current_assignment_id": None,
        "current_task_id": None,
    }
    row.update(over)
    return row


def test_classify_orphan_truth_table():
    backed_ids, backed_profiles = _backed_universe()
    rows = [
        # Prunable orphans.
        _orphan_row("codex", persona_id="profile:codex_create_probe", profile_id="codex_create_probe"),
        _orphan_row("pm", persona_id="pm", role="pm", profile_id="pm"),
        # Real product agents — never orphaned, not even held.
        _orphan_row("dev", persona_id="dev", role="dev", profile_id=None),
        _orphan_row("qa", persona_id="qa", role="qa", profile_id=None),
        _orphan_row("neko", persona_id="neko_supervisor", role="neko_supervisor", profile_id=None),
        _orphan_row("bdev", persona_id="backend_dev", role="dev", profile_id=None),
        _orphan_row("base", persona_id="base", role="profile", profile_id="base"),
        _orphan_row("alice", persona_id="profile:alice", role="profile", profile_id="alice"),
        # S56: the fixture still SENDS active_worker_session_id (a stale producer
        # can), but the reader no longer honors it — the row is a plain orphan now.
        _orphan_row("stale_worker_session_field", active_worker_session_id="ws_1"),
        # Orphan-shaped but PROTECTED → held, not pruned.
        _orphan_row("held_run", active_run_id="run_1"),
        _orphan_row("held_assign", current_assignment_id="a_1"),
        _orphan_row("held_taskbound", mode="task_bound"),
        _orphan_row("held_heartbeat", last_heartbeat_at=_FRESH.isoformat()),
        _orphan_row("held_recent", updated_at=_FRESH.isoformat()),
    ]
    res = classify_orphan_persona_instances(
        rows, backed_persona_ids=backed_ids, backed_profile_names=backed_profiles, now=_FIXED_NOW
    )

    prunable = {e["persona_instance_id"]: e["reason"] for e in res["prunable"]}
    held = {e["persona_instance_id"]: e["reason"] for e in res["held"]}

    assert prunable == {
        "codex": "orphan-no-profile",
        "pm": "legacy-role",
        # S56: a dead active_worker_session_id no longer protects a row.
        "stale_worker_session_field": "orphan-no-profile",
    }
    # No real agent appears anywhere.
    for real in ("dev", "qa", "neko", "bdev", "base", "alice"):
        assert real not in prunable and real not in held
    assert held == {
        "held_run": "active-binding",
        "held_assign": "active-binding",
        "held_taskbound": "task-bound",
        "held_heartbeat": "fresh-heartbeat",
        "held_recent": "recently-updated",
    }


def test_classify_legacy_role_still_seeded_is_held():
    # If the mothballed persona is still present in the backed universe, pruning would
    # flap (it gets re-ensured), so it is held, not pruned.
    backed_ids, backed_profiles = _backed_universe()
    backed_ids = set(backed_ids) | {"pm"}
    res = classify_orphan_persona_instances(
        [_orphan_row("pm", persona_id="pm", role="pm", profile_id="pm")],
        backed_persona_ids=backed_ids,
        backed_profile_names=backed_profiles,
        now=_FIXED_NOW,
    )
    assert res["prunable"] == []
    assert res["held"] == [
        {
            "persona_instance_id": "pm",
            "persona_id": "pm",
            "role": "pm",
            "profile_id": "pm",
            "updated_at": _STALE.isoformat(),
            "reason": "legacy-role-still-seeded",
        }
    ]


def test_reconcile_prunes_orphans_holds_and_is_idempotent(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.persona_instance_identity._profile_template_names",
        lambda: ["alice", "base", "backend-dev"],
    )
    # Two true orphans (weeks stale), one held orphan (fresh), and real agents kept.
    _seed_row(
        "personainst_profile_codex_create_probe",
        persona_id="profile:codex_create_probe",
        display_name="Codex Create Probe",
        mode="configured",
        profile_id="codex_create_probe",
        updated_at=_STALE,
    )
    _seed_row("personainst_pm", persona_id="pm", display_name="Pm", mode="configured", profile_id="pm", updated_at=_STALE)
    _seed_row("personainst_profile_ghost", persona_id="profile:ghost", display_name="Ghost", mode="configured", profile_id="ghost", updated_at=now())
    _seed_row("personainst_dev", persona_id="dev", display_name="Dev", updated_at=_STALE)
    _seed_row("personainst_base", persona_id="base", display_name="Base", profile_id="base", updated_at=_STALE)

    before_ids = _live_row_ids()

    # Dry-run writes nothing.
    dry = reconcile_persona_instances(apply=False, event_log=EventLog())
    assert dry["pruned_count"] == 2
    assert {p["persona_instance_id"]: p["reason"] for p in dry["pruned"]} == {
        "personainst_profile_codex_create_probe": "orphan-no-profile",
        "personainst_pm": "legacy-role",
    }
    assert {h["persona_instance_id"] for h in dry["held"]} == {"personainst_profile_ghost"}
    assert dry["held"][0]["reason"] == "recently-updated"
    assert _live_row_ids() == before_ids  # nothing moved

    # Apply archives exactly the two orphans; real agents + held ghost survive.
    applied = reconcile_persona_instances(apply=True, event_log=EventLog())
    assert applied["pruned_count"] == 2
    assert _live_row_ids() == {
        "personainst_dev",
        "personainst_base",
        "personainst_profile_ghost",
    }
    prune_dir = paths.persona_instances_archive_dir()
    archived = {p.name for p in prune_dir.rglob("*.json")}
    assert "personainst_profile_codex_create_probe.json" in archived
    assert "personainst_pm.json" in archived

    events = [_event_type(e) for e in EventLog().tail(50)]
    assert events.count("persona_instance.pruned") == 2

    # Second apply is a no-op.
    again = reconcile_persona_instances(apply=True, event_log=EventLog())
    assert again["pruned_count"] == 0


class _FakeTemplate:
    def __init__(self, name):
        self.name = name
        self.description = ""


def test_snapshot_emits_orphan_and_no_warning_for_real_agent(monkeypatch):
    cfg = _runtime_config()
    monkeypatch.setattr("agent_runtime.snapshot.load_agent_runtime_config", lambda: cfg)
    # Provide an authoritative (non-empty) profile catalog so the profile:* orphan lane
    # engages; ``codex_create_probe`` is absent from it and must be flagged.
    monkeypatch.setattr(
        "agent_runtime.snapshot.available_profile_templates",
        lambda: [_FakeTemplate("alice"), _FakeTemplate("base")],
    )
    # The reconcile prune lane reads the catalog through _profile_template_names; keep it
    # authoritative there too so the heal step actually archives the flagged orphan.
    monkeypatch.setattr(
        "agent_runtime.persona_instance_identity._profile_template_names",
        lambda: ["alice", "base"],
    )
    _seed_row(
        "personainst_profile_codex_create_probe",
        persona_id="profile:codex_create_probe",
        display_name="Codex Create Probe",
        mode="configured",
        profile_id="codex_create_probe",
        updated_at=_STALE,
    )
    _seed_row("personainst_dev", persona_id="dev", display_name="Dev", updated_at=_STALE)

    snapshot = build_snapshot()
    orphan_warnings = [
        w for w in snapshot["parity"]["warnings"] if w.get("code") == "orphaned_persona_instance"
    ]
    entity_ids = {w["entity_id"] for w in orphan_warnings}
    assert "personainst_profile_codex_create_probe" in entity_ids
    assert "personainst_dev" not in entity_ids  # real agent never flagged
    codex_warning = next(w for w in orphan_warnings if w["entity_id"] == "personainst_profile_codex_create_probe")
    assert codex_warning["reason"] == "orphan-no-profile"

    # After reconcile the orphan is archived and the warning clears.
    reconcile_persona_instances(event_log=EventLog())
    healed = build_snapshot()
    assert not [
        w for w in healed["parity"]["warnings"] if w.get("code") == "orphaned_persona_instance"
    ]


# --- Phase 4: stale chat-session bindings (the ``session_not_in_db`` class) ----
#
# Live evidence 2026-07-25 (home X:\Eternia\.hermes, profile alice): the
# persona_chat_history projection accounted 10 permanent ``session_not_in_db``
# drops — instances still pointing at chat sessions SessionDB no longer had.
# The projection is READ-ONLY and can only hide those rows, so each orphan was
# one amber unit on Mission Control's parity pill forever. These cover the
# write-path repair.


class _FakeSessionDB:
    """Minimal SessionDB stand-in: knows a set of live session ids."""

    def __init__(self, session_ids=(), *, get_raises: bool = False, list_raises: bool = False):
        self.session_ids = list(session_ids)
        self.get_raises = get_raises
        self.list_raises = list_raises

    def list_sessions_rich(self, **kwargs):
        if self.list_raises:
            raise RuntimeError("session db unreadable")
        limit = int(kwargs.get("limit") or 20)
        return [{"id": session_id} for session_id in self.session_ids][:limit]

    def get_session(self, session_id):
        if self.get_raises:
            raise RuntimeError("session db unreadable")
        return {"id": session_id} if session_id in self.session_ids else None


def _binding_cleared_events() -> list:
    return [
        event
        for event in EventLog().tail(200)
        if _event_type(event) == "persona_instance.chat_binding_cleared"
    ]


def _bind(instance_id: str, *, persona_id: str, session_id: str, mode: str = "chat"):
    instance = _seed_row(
        instance_id,
        persona_id=persona_id,
        display_name=instance_id,
        mode=mode,
        session_id=session_id,
    )
    stored = PersonaInstanceStore().get(instance.id)
    stored.default_chat_session_id = session_id
    PersonaInstanceStore().update(stored)
    return stored


def test_repair_missing_chat_session_bindings_clears_only_the_dangling_pointer():
    _bind("personainst_gone", persona_id="dev", session_id="persona_chat_gone")
    _bind("personainst_live", persona_id="backend_dev", session_id="persona_chat_live")
    store = PersonaInstanceStore()

    report = store.repair_missing_chat_session_bindings(
        session_db=_FakeSessionDB(["persona_chat_live"])
    )

    assert report["applied"] is True
    assert report["repaired_count"] == 1
    assert report["repaired"][0]["persona_instance_id"] == "personainst_gone"
    assert report["repaired"][0]["session_id"] == "persona_chat_gone"
    assert sorted(report["repaired"][0]["cleared_fields"]) == [
        "default_chat_session_id",
        "session_id",
    ]

    healed = PersonaInstanceStore().get("personainst_gone")
    assert healed.default_chat_session_id is None
    assert healed.session_id is None
    assert healed.mode == "configured"  # demoted once it holds no chat
    untouched = PersonaInstanceStore().get("personainst_live")
    assert untouched.default_chat_session_id == "persona_chat_live"
    assert untouched.mode == "chat"

    # Store mutations always emit an event.
    events = _binding_cleared_events()
    assert len(events) == 1
    payload = events[0].payload if hasattr(events[0], "payload") else events[0]["payload"]
    assert payload["session_id"] == "persona_chat_gone"
    assert payload["reason"] == "session_missing_from_session_db"


def test_repair_missing_chat_session_bindings_dry_run_writes_nothing():
    _bind("personainst_gone", persona_id="dev", session_id="persona_chat_gone")
    store = PersonaInstanceStore()

    report = store.repair_missing_chat_session_bindings(
        apply=False,
        session_db=_FakeSessionDB(["persona_chat_other"]),
    )

    assert report["applied"] is False
    assert report["dry_run"] is True
    assert report["repaired_count"] == 1
    assert report["repaired"][0]["persona_instance_id"] == "personainst_gone"
    # Reported, not repaired: the row and the event log are untouched.
    still_bound = PersonaInstanceStore().get("personainst_gone")
    assert still_bound.default_chat_session_id == "persona_chat_gone"
    assert still_bound.session_id == "persona_chat_gone"
    assert still_bound.mode == "chat"
    assert _binding_cleared_events() == []


def test_repair_missing_chat_session_bindings_ignores_blank_legacy_pointers():
    instance = _bind("personainst_blank", persona_id="dev", session_id="")
    store = PersonaInstanceStore()

    dry = store.repair_missing_chat_session_bindings(
        apply=False,
        session_db=_FakeSessionDB(["persona_chat_live"]),
    )
    applied = store.repair_missing_chat_session_bindings(
        session_db=_FakeSessionDB(["persona_chat_live"]),
    )

    assert dry["repaired"] == []
    assert dry["repaired_count"] == 0
    assert applied["repaired"] == []
    assert applied["repaired_count"] == 0
    unchanged = store.get(instance.id)
    assert unchanged.default_chat_session_id == ""
    assert unchanged.session_id == ""
    assert unchanged.mode == "chat"
    assert _binding_cleared_events() == []


def test_repair_missing_chat_session_bindings_skips_task_bound_mission_sessions():
    instance = _bind(
        "personainst_worker",
        persona_id="dev",
        session_id="mission_run_session",
        mode="task_bound",
    )
    stored = PersonaInstanceStore().get(instance.id)
    stored.current_task_id = "task_live"
    PersonaInstanceStore().update(stored)

    report = PersonaInstanceStore().repair_missing_chat_session_bindings(
        session_db=_FakeSessionDB(["persona_chat_other"])
    )

    # A mission turn runs in a session that lives in the run/event stream, not
    # the operator SessionDB — absent there is normal, not stale.
    assert report["repaired_count"] == 0
    assert PersonaInstanceStore().get("personainst_worker").session_id == "mission_run_session"
    assert _binding_cleared_events() == []


def test_repair_missing_chat_session_bindings_refuses_on_blind_database():
    _bind("personainst_gone", persona_id="dev", session_id="persona_chat_gone")
    store = PersonaInstanceStore()

    empty = store.repair_missing_chat_session_bindings(session_db=_FakeSessionDB([]))
    assert empty["skipped"] == "session_db_empty"
    assert empty["repaired_count"] == 0

    unreadable = store.repair_missing_chat_session_bindings(
        session_db=_FakeSessionDB([], list_raises=True)
    )
    assert unreadable["skipped"] == "session_db_unavailable"
    assert unreadable["repaired_count"] == 0

    # Enumerates, but every row probe raises: "unknown" is never "absent".
    blind_probe = store.repair_missing_chat_session_bindings(
        session_db=_FakeSessionDB(["persona_chat_other"], get_raises=True)
    )
    assert blind_probe["repaired_count"] == 0

    assert PersonaInstanceStore().get("personainst_gone").session_id == "persona_chat_gone"
    assert _binding_cleared_events() == []


def test_repair_skips_when_head_home_is_not_authoritative(monkeypatch):
    """The self-resolved SessionDB is only trustworthy under an explicit head
    authority. Without HERMES_HEAD_HOME, a maintenance verb run under a
    profile home probes that profile's (populated!) database and reads every
    operator chat as absent — the live 2026-07-25 reconcile cleared 10 live
    bindings exactly this way. Fail closed with a typed skip instead."""

    from agent_runtime import persona_chat_history

    _bind("personainst_gone", persona_id="dev", session_id="persona_chat_gone")
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    monkeypatch.setattr(
        persona_chat_history,
        "_default_session_db",
        lambda: (_ for _ in ()).throw(AssertionError("guard must refuse before resolving the DB")),
    )

    report = PersonaInstanceStore().repair_missing_chat_session_bindings()

    assert report["skipped"] == "head_home_not_authoritative"
    assert report["repaired_count"] == 0
    assert PersonaInstanceStore().get("personainst_gone").session_id == "persona_chat_gone"
    assert _binding_cleared_events() == []


def test_reconcile_repairs_stale_chat_bindings_and_dry_run_is_inert(monkeypatch):
    import os

    from agent_runtime import persona_chat_history

    _bind("personainst_gone", persona_id="dev", session_id="persona_chat_gone")
    # The presence probe fails closed without an explicit head authority; the
    # repair path under test assumes correctly-routed maintenance.
    monkeypatch.setenv("HERMES_HEAD_HOME", os.environ.get("HERMES_HOME", ""))
    monkeypatch.setattr(
        persona_chat_history,
        "_default_session_db",
        lambda: _FakeSessionDB(["persona_chat_live"]),
    )

    dry = reconcile_persona_instances(apply=False, event_log=EventLog())
    assert dry["session_binding_repaired_count"] == 1
    assert PersonaInstanceStore().get("personainst_gone").default_chat_session_id == (
        "persona_chat_gone"
    )
    assert _binding_cleared_events() == []

    applied = reconcile_persona_instances(event_log=EventLog())
    assert applied["session_binding_repaired_count"] == 1
    assert PersonaInstanceStore().get("personainst_gone").default_chat_session_id is None
    assert len(_binding_cleared_events()) == 1

    # Idempotent: the second pass has nothing left to repair.
    again = reconcile_persona_instances(event_log=EventLog())
    assert again["session_binding_repaired_count"] == 0
