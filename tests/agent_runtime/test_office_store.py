"""OfficeStore tests (office plan W-H1): event-per-mutation, actor-key
canonicalization at the store boundary, filename truncation collision-proofing,
write-time secret rejection of display names, archive ledger + restore,
revision guard, the prune-lane hook, and the snapshot offices projection.
Autouse conftest fixtures isolate the runtime root.
"""

from __future__ import annotations

import pytest

from agent_runtime import office_models, paths
from agent_runtime.errors import NotFound, StaleRevision, SyncConflict
from agent_runtime.events import EventLog
from agent_runtime.office_store import OfficeStore
from agent_runtime.snapshot import build_snapshot
from agent_runtime.store import WorkspaceStore


def _event_types() -> list[str]:
    return [evt.type for evt in EventLog().iter_all()]


def _make_workspace(name: str = "Default") -> str:
    ws = WorkspaceStore().create(name=name)
    WorkspaceStore().set_active(ws.id)
    return ws.id


def _actor_payload(persona_id: str = "dev", **overrides) -> dict:
    payload = {
        "persona_id": persona_id,
        "items": [
            {"item_id": persona_id, "persona_id": persona_id, "kind": "agent", "position": [1.5, 2.0], "folder": "Agents"},
            {"item_id": f"desk-{persona_id}", "persona_id": persona_id, "kind": "desk", "position": [1.5, 3.6], "folder": "Desks"},
        ],
    }
    payload.update(overrides)
    return payload


# ── event per mutation + round trip ───────────────────────────────────────


def test_upsert_remove_restore_round_trip_emits_event_per_mutation():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("dev"))
    assert actor.actor_key == "dev"
    assert len(actor.items) == 2
    removed = store.remove_actor(ws, "dev")
    assert removed.state == "archived"
    restored = store.restore_actor(ws, "dev")
    assert restored.state == "active"
    store.update_surface(ws, folders=["West Wing"])

    types = _event_types()
    for expected in (
        "office.surface.created",
        "office.actor.upserted",
        "office.actor.removed",
        "office.actor.restored",
        "office.surface.updated",
    ):
        assert expected in types, (expected, types)


def test_surface_created_once_and_deterministic():
    ws = _make_workspace()
    store = OfficeStore()
    first = store.ensure_surface(ws)
    second = store.ensure_surface(ws)
    assert _event_types().count("office.surface.created") == 1
    assert office_models.office_content_hash(first) == office_models.office_content_hash(second)
    assert list(first.folders) == list(office_models.DEFAULT_FOLDERS)


# ── identity: canonicalization at the boundary (plan §4.3) ────────────────


def test_actor_key_canonicalizes_drifted_instance_id():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(
        ws,
        _actor_payload("dev", persona_instance_id="persona_personainst_goal1_dev"),
    )
    # persona_personainst_* actor-token drift collapses at the store boundary.
    assert actor.actor_key == "personainst_goal1_dev"
    assert actor.persona_instance_id == "personainst_goal1_dev"
    assert paths.office_actor_path(ws, "personainst_goal1_dev").exists()


def test_actor_key_falls_back_to_persona_id_and_normalizes_case():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("Backend_Dev"))
    assert actor.actor_key == "backend_dev"
    assert actor.persona_id == "backend_dev"


def test_long_actor_keys_truncate_without_colliding():
    shared_prefix = "p" * 70
    token_a = office_models.actor_file_token(shared_prefix + "alpha")
    token_b = office_models.actor_file_token(shared_prefix + "beta")
    assert token_a != token_b, "truncated filenames must stay collision-proof (hash suffix)"
    assert len(token_a) <= 64 + 11
    # Deterministic: same key, same token, every machine.
    assert token_a == office_models.actor_file_token(shared_prefix + "alpha")


# ── write-time secret rejection (plan §4.2) ───────────────────────────────


def test_secret_shaped_display_name_rejected_at_write():
    ws = _make_workspace()
    store = OfficeStore()
    payload = _actor_payload("dev")
    payload["items"][0]["display_name"] = "token: abcdefgh12345678"
    with pytest.raises(ValueError):
        store.upsert_actor(ws, payload)
    # Nothing was written; the surface may exist but no actor file does.
    assert not store.actor_exists(ws, "dev")


# ── validation + revision guard ────────────────────────────────────────────


def test_invalid_payloads_rejected():
    ws = _make_workspace()
    store = OfficeStore()
    with pytest.raises(ValueError):
        store.upsert_actor(ws, {"persona_id": "dev", "items": []})
    with pytest.raises(ValueError):
        store.upsert_actor(ws, _actor_payload("dev", items=[{"item_id": "dev", "position": ["nan", 0]}]))
    with pytest.raises(ValueError):
        store.upsert_actor(ws, {"items": [{"item_id": "x", "position": [0, 0]}]})


def test_stale_revision_guard():
    ws = _make_workspace()
    store = OfficeStore()
    actor = store.upsert_actor(ws, _actor_payload("dev"))
    with pytest.raises(StaleRevision):
        store.upsert_actor(ws, _actor_payload("dev"), expect_revision=actor.revision + 5)
    updated = store.upsert_actor(ws, _actor_payload("dev"), expect_revision=actor.revision)
    assert updated.revision == actor.revision + 1


def test_scale_clamped_defensively():
    ws = _make_workspace()
    store = OfficeStore()
    payload = _actor_payload("dev")
    payload["items"][0]["scale"] = 99.0
    actor = store.upsert_actor(ws, payload)
    assert actor.items[0].scale == office_models.SCALE_MAX


# ── archive ledger + restore + re-add ─────────────────────────────────────


def test_remove_records_ledger_and_blocks_nothing_else():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.remove_actor(ws, "dev", reason="operator")
    surface = store.get_surface(ws)
    assert "dev" in surface.archived_actor_keys
    assert not store.actor_exists(ws, "dev")
    assert paths.office_archived_actor_path(ws, "dev").exists()
    # Idempotent remove returns the archived copy.
    again = store.remove_actor(ws, "dev")
    assert again.state == "archived"


def test_upsert_after_archive_clears_ledger():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    store.remove_actor(ws, "dev")
    readded = store.upsert_actor(ws, _actor_payload("dev"))
    assert readded.state == "active"
    surface = store.get_surface(ws)
    assert "dev" not in surface.archived_actor_keys
    assert not paths.office_archived_actor_path(ws, "dev").exists()


def test_restore_missing_raises():
    ws = _make_workspace()
    store = OfficeStore()
    with pytest.raises(NotFound):
        store.restore_actor(ws, "ghost")


# ── conflict guard ─────────────────────────────────────────────────────────


def test_conflict_sidecar_blocks_upsert_until_resolved():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    sidecar = paths.office_conflict_path(ws, "dev")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"actor_key": "dev", "kind": "both_changed", "remote_actor": null}', encoding="utf-8")
    with pytest.raises(SyncConflict):
        store.upsert_actor(ws, _actor_payload("dev"))
    resolved = store.resolve_conflict(ws, "dev", take="local")
    assert resolved is not None and resolved.actor_key == "dev"
    assert "office.actor.conflict_resolved" in _event_types()
    # Resolution archives the sidecar; writes flow again.
    assert not sidecar.exists()
    store.upsert_actor(ws, _actor_payload("dev"))


# ── prune lane (plan §4.3) ─────────────────────────────────────────────────


def test_archive_actors_for_instance_archives_only_instance_bound():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))  # persona-keyed: survives
    store.upsert_actor(ws, _actor_payload("qa", persona_instance_id="personainst_goal9_qa"))
    count = store.archive_actors_for_instance("persona_personainst_goal9_qa")
    assert count == 1
    assert store.actor_exists(ws, "dev")
    assert not store.actor_exists(ws, "personainst_goal9_qa")
    surface = store.get_surface(ws)
    assert "personainst_goal9_qa" in surface.archived_actor_keys


# ── snapshot projection (W-H3) ─────────────────────────────────────────────


def test_snapshot_offices_section_and_conflict_parity_warning():
    ws = _make_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _actor_payload("dev"))
    sidecar = paths.office_conflict_path(ws, "dev")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text('{"actor_key": "dev", "kind": "both_changed", "remote_actor": null}', encoding="utf-8")

    snap = build_snapshot(event_log=EventLog())
    offices = snap["offices"]
    assert ws in offices
    row = offices[ws]
    assert row["actor_count"] == 1
    assert row["actors"][0]["actor_key"] == "dev"
    assert row["actors"][0]["items"][0]["position"] == [1.5, 2.0]
    assert row["conflict_actor_keys"] == ["dev"]
    assert row["orphaned"] is False
    codes = {w.get("code") for w in snap["parity"]["warnings"]}
    assert "office_actor_conflict" in codes
    assert snap["parity"]["contract_version"] == 43
