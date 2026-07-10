"""Persona-instance identity: canonical-id chokepoint + store reconciliation.

Golden corpus = the live 2026-07-10 store in which one logical channel
persisted under four id schemes (neko_supervisor had three rows, profile:alice
two) and Mission Control rendered duplicate agent cards.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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
    duplicate_persona_instance_groups,
    identity_aliases_for_rows,
    load_persona_instance_aliases,
    reconcile_persona_instances,
)
from agent_runtime.runtime_config import EnterpriseWorkerSessionsConfig
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import WorkerSessionState


def _runtime_config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        enterprise_worker_sessions=EnterpriseWorkerSessionsConfig(
            enabled=True,
            worker_session_store=True,
            persona_instance_runtime=True,
            persona_assignment_store=True,
        )
    )


def _seed_row(
    instance_id: str,
    *,
    persona_id: str,
    display_name: str,
    mode: str = "chat",
    session_id: str | None = None,
    profile_id: str | None = None,
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
