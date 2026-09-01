"""H4 — drift, revert, and retire-follows-the-desk for replicated agents.

Three things this stage has to make true, and the first is the one that makes
the other two safe:

* **A replica is not drift.** That is the mint's baseline-alignment property
  doing its job, not a special case in the walk — and if it ever stops being
  true, the revert lane that landed the same day will offer to archive correct
  state.
* **A locally-edited replica IS drift**, and is addressable by
  ``FAMILY:CONTAINER:KEY`` so the operator has an exit that is not Publish.
* **Retirement follows the DESK, never the absence.** An archived actor retires
  its agent; an instance merely missing from the projection does not.

Autouse conftest fixtures isolate the runtime root.
"""

from __future__ import annotations

import yaml

from agent_runtime import paths
from agent_runtime.events import EventLog
from agent_runtime.models import PersonaInstance
from agent_runtime.office_store import OfficeStore
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.persona_instance_sync import (
    PROJECTION_KIND,
    PROJECTION_RELATIVE_PATH,
    apply_persona_instance_pull,
    instance_baseline_key,
    read_persona_instance_baseline,
)
from agent_runtime.realm_revert import (
    OUTCOME_ARCHIVED_LOCAL_ONLY,
    OUTCOME_REVERTED,
    FAMILIES,
    classify_revert,
    revert_realm_sync,
)
from agent_runtime.realm_sync import (
    DRIFT_FAMILY_PERSONA_INSTANCE,
    DRIFT_KIND_ADDED,
    DRIFT_KIND_CHANGED,
    DRIFT_KIND_REMOVED,
    _persona_instance_store_drift_items,
    store_drift_items,
)
from agent_runtime.states import WorkerSessionState
from agent_runtime.store import RealmStore, WorkspaceStore

INSTANCE_ID = "personainst_dev_agent_9682caf4"


def _realm_workspace(sync_repo=None) -> tuple[str, str]:
    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    if sync_repo is not None:
        # A LOCAL path ref, so ``_sync_repo_path`` resolves here and no clone,
        # fetch or credential is ever in play — the revert lane is local-only.
        realm.sync_manifest_ref = str(sync_repo)
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    return realm.id, ws.id


def _subtree(realm_id: str, tmp_path):
    path = tmp_path / "sync_repo" / "realms" / paths.safe_path_token(realm_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _body(ws: str, instance_id: str = INSTANCE_ID, **overrides) -> dict:
    body = {
        "id": instance_id,
        "persona_id": "dev",
        "display_name": "Neko",
        "mode": "configured",
        "workspace_id": ws,
    }
    body.update(overrides)
    return body


def _write_remote(subtree, *bodies: dict) -> None:
    path = subtree.joinpath(*PROJECTION_RELATIVE_PATH.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "instances": {b["id"]: b for b in bodies},
                "kind": PROJECTION_KIND,
                "schema_version": 1,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _local(ws: str, instance_id: str = INSTANCE_ID, **overrides) -> PersonaInstance:
    base = dict(
        id=instance_id,
        persona_id="dev",
        role="developer",
        display_name="Neko",
        profile_id="alice",
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
        mode="configured",
        workspace_id=ws,
    )
    base.update(overrides)
    instance = PersonaInstance(**base)
    PersonaInstanceStore()._write(instance)
    return instance


def _drift(realm_id: str, ws: str):
    return _persona_instance_store_drift_items(realm_id, [WorkspaceStore().get(ws)])


def _events(event_type: str | None = None) -> list[dict]:
    return [
        {"type": e.type, "payload": dict(e.payload or {})}
        for _, e in EventLog().iter_from_offset(0)
        if event_type is None or e.type == event_type
    ]


# ── the drift family ──────────────────────────────────────────────────────


def test_a_replicated_row_is_not_reported_as_added_drift(tmp_path):
    """THE property. The mint records the baseline at the REMOTE hash, so a
    fresh replica's projection already equals its baseline entry and the walk
    produces no row for it — no special case, just the alignment holding.

    If this ever fails, `realm sync revert --all` archives correct state."""

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert summary.replicated == [INSTANCE_ID]

    assert _drift(realm_id, ws) == []
    assert [i for i in store_drift_items(realm_id, [WorkspaceStore().get(ws)])
            if i.family == DRIFT_FAMILY_PERSONA_INSTANCE] == []


def test_a_locally_edited_replica_is_reported_as_changed_drift(tmp_path):
    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    instance = PersonaInstanceStore().get(INSTANCE_ID)
    instance.display_name = "Locally renamed"
    PersonaInstanceStore()._write(instance)

    items = _drift(realm_id, ws)
    assert len(items) == 1
    assert items[0].family == DRIFT_FAMILY_PERSONA_INSTANCE
    assert items[0].kind == DRIFT_KIND_CHANGED
    assert items[0].item_key == INSTANCE_ID
    assert items[0].container == ws
    assert items[0].spec == f"{DRIFT_FAMILY_PERSONA_INSTANCE}:{ws}:{INSTANCE_ID}"
    assert items[0].baseline_key() == instance_baseline_key(INSTANCE_ID)


def test_a_locally_authored_agent_nobody_published_is_added_drift():
    realm_id, ws = _realm_workspace()
    _local(ws)
    items = _drift(realm_id, ws)
    assert [(i.kind, i.item_key) for i in items] == [(DRIFT_KIND_ADDED, INSTANCE_ID)]


def test_a_baselined_agent_that_is_gone_locally_is_removed_drift(tmp_path):
    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)
    paths.persona_instance_path(INSTANCE_ID).unlink()

    items = _drift(realm_id, ws)
    assert [(i.kind, i.item_key) for i in items] == [(DRIFT_KIND_REMOVED, INSTANCE_ID)]


def test_a_canonical_channel_is_never_reported_as_drift():
    """Every member derives its own, so offering the operator a publish for one
    would be offering a write the pull door on the other end refuses by design."""

    realm_id, ws = _realm_workspace()
    _local(ws, "personainst_dev")
    assert _drift(realm_id, ws) == []


def test_an_agent_outside_this_realms_workspaces_is_not_this_realms_drift():
    realm_id, ws = _realm_workspace()
    _local("ws_somewhere_else")
    assert _drift(realm_id, ws) == []


def test_an_unreadable_store_contributes_no_accounting_rather_than_removals():
    """The office family's guard, for the office family's reason: spending only
    the rows would hand back a short list, and the baseline diff would then
    report N removals for agents whose files merely would not open here."""

    realm_id, ws = _realm_workspace()
    _local(ws)
    assert len(_drift(realm_id, ws)) == 1
    paths.persona_instance_path("personainst_broken_agent_1").write_text("{oops", encoding="utf-8")
    assert _drift(realm_id, ws) == []


def test_the_counts_are_derived_from_the_rows(tmp_path):
    from agent_runtime.realm_sync import _PERSONA_INSTANCE_DRIFT_COUNTS, _drift_counts

    realm_id, ws = _realm_workspace()
    _local(ws)
    _local(ws, "personainst_qa_agent_11112222")
    counts = _drift_counts(_drift(realm_id, ws), _PERSONA_INSTANCE_DRIFT_COUNTS)
    assert counts == {"instances_changed": 0, "instances_added": 2, "instances_removed": 0}


# ── the revert transition rows ────────────────────────────────────────────


def test_the_revert_table_is_total_over_the_new_family():
    """``classify_revert`` is already total over family × kind; the family needs
    its transition rows, not a new table."""

    assert DRIFT_FAMILY_PERSONA_INSTANCE in FAMILIES
    for kind, present, expected in (
        (DRIFT_KIND_CHANGED, True, OUTCOME_REVERTED),
        (DRIFT_KIND_ADDED, True, OUTCOME_REVERTED),
        # A local-only agent upstream genuinely lacks: archived, never deleted,
        # and never with a tombstone — this family has no realm-visible ledger.
        (DRIFT_KIND_ADDED, False, OUTCOME_ARCHIVED_LOCAL_ONLY),
        (DRIFT_KIND_REMOVED, True, "restored_from_upstream"),
        (DRIFT_KIND_REMOVED, False, "baseline_entry_dropped"),
        (DRIFT_KIND_CHANGED, False, "baseline_entry_dropped"),
    ):
        decision = classify_revert(
            family=DRIFT_FAMILY_PERSONA_INSTANCE, kind=kind, upstream_present=present
        )
        assert decision.outcome == expected, (kind, present)


def test_reverting_an_edited_replica_restores_the_upstream_body(tmp_path):
    realm_id, ws = _realm_workspace(tmp_path / "sync_repo")
    subtree = _subtree(realm_id, tmp_path)
    _write_remote(subtree, _body(ws))
    apply_persona_instance_pull(realm_id, subtree)

    instance = PersonaInstanceStore().get(INSTANCE_ID)
    instance.display_name = "Locally renamed"
    PersonaInstanceStore()._write(instance)
    assert len(_drift(realm_id, ws)) == 1

    result = revert_realm_sync(realm_id, item_specs=[f"{DRIFT_FAMILY_PERSONA_INSTANCE}:{ws}:{INSTANCE_ID}"])
    assert [row["outcome"] for row in result["items"]] == [OUTCOME_REVERTED]
    assert PersonaInstanceStore().get(INSTANCE_ID).display_name == "Neko"
    # ... and the row stops counting, because the baseline was realigned from
    # the store's own post-write content.
    assert _drift(realm_id, ws) == []


def test_reverting_a_local_only_agent_archives_it_and_mints_no_tombstone(tmp_path):
    """The §AX7 ruling, for this family: a revert is DIAGNOSTIC intent. The row
    is archived (archive-never-delete) and nothing realm-visible is written."""

    realm_id, ws = _realm_workspace(tmp_path / "sync_repo")
    subtree = _subtree(realm_id, tmp_path)
    _write_remote(subtree)  # upstream has nothing
    _local(ws)
    OfficeStore().upsert_actor(
        ws,
        {
            "persona_id": "dev",
            "persona_instance_id": INSTANCE_ID,
            "items": [{"item_id": "dev", "persona_id": "dev", "kind": "agent", "position": [1.0, 2.0]}],
        },
    )
    surface_before = list(OfficeStore().get_surface(ws).archived_actor_keys)

    result = revert_realm_sync(realm_id, item_specs=[f"{DRIFT_FAMILY_PERSONA_INSTANCE}:{ws}:{INSTANCE_ID}"])
    assert [row["outcome"] for row in result["items"]] == [OUTCOME_ARCHIVED_LOCAL_ONLY]
    assert not paths.persona_instance_path(INSTANCE_ID).exists()
    # Archive-never-delete: the row MOVED.
    assert list(paths.persona_instances_archive_dir().rglob(f"{INSTANCE_ID}.json"))
    # No realm-visible ledger entry: the only place this lane could have minted
    # one is the office half, and ``retire_replica`` does not run it.
    assert OfficeStore().get_surface(ws).archived_actor_keys == surface_before
    assert OfficeStore().actor_exists(ws, INSTANCE_ID)


# ── §5.2 retire-follows-the-desk ──────────────────────────────────────────


def test_the_replica_follows_the_desk(tmp_path):
    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)
    assert paths.persona_instance_path(INSTANCE_ID).exists()

    # The office lane archived the actor as ``remote_removed`` in this pull.
    summary = apply_persona_instance_pull(realm_id, tmp_path, desks_removed=[INSTANCE_ID])
    assert summary.retired == [INSTANCE_ID]
    assert not paths.persona_instance_path(INSTANCE_ID).exists()
    assert list(paths.persona_instances_archive_dir().rglob(f"{INSTANCE_ID}.json"))
    assert instance_baseline_key(INSTANCE_ID) not in read_persona_instance_baseline(realm_id)
    # Archived, never tombstoned — nothing in this lane ever mints one.
    retired = _events("persona_instance.retired")
    assert retired and retired[-1]["payload"]["source"] == "realm_sync"
    assert retired[-1]["payload"]["reason"] == "remote_removed"


def test_an_instance_missing_while_its_desk_stands_is_upstream_absent_not_a_delete(tmp_path):
    """The distinction §5.2 rests on: a desk's removal is a decision a peer
    MADE; a row's absence is equally consistent with the publisher's own office
    scan having come back short."""

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    _write_remote(tmp_path)  # projection empty, but no desk was removed
    summary = apply_persona_instance_pull(realm_id, tmp_path, desks_removed=[])
    assert summary.upstream_absent == [INSTANCE_ID]
    assert summary.retired == []
    assert paths.persona_instance_path(INSTANCE_ID).exists()


def test_a_live_replica_is_never_archived_and_says_so(tmp_path, monkeypatch):
    """`_has_live_binding` → typed ``instance_active``. The pull accounts it as
    held and re-decides next pull — the 'keep the baseline, retry' repair."""

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    monkeypatch.setattr(PersonaInstanceStore, "_has_live_binding", lambda self, inst: True)
    summary = apply_persona_instance_pull(realm_id, tmp_path, desks_removed=[INSTANCE_ID])

    assert summary.retired == []
    assert summary.retire_held == [{"key": INSTANCE_ID, "code": "instance_active"}]
    assert paths.persona_instance_path(INSTANCE_ID).exists()
    assert instance_baseline_key(INSTANCE_ID) in read_persona_instance_baseline(realm_id)

    # The baseline entry STAYS, and the witness has to be the case where phase
    # one CANNOT put it back. With a projection present a dropped entry is
    # silently re-recorded by the converged arm two blocks later, so a sabotage
    # there is invisible — with the projection ABSENT (an older peer, or one
    # whose publish is mid-flight) nothing re-records it, and a row with no
    # baseline reads as a local ADD: the failed archive comes back as something
    # to publish, which is the C2 lesson one lane over.
    (tmp_path / "store" / "persona_instances.yaml").unlink()
    again = apply_persona_instance_pull(realm_id, tmp_path, desks_removed=[INSTANCE_ID])
    assert again.source is None
    assert again.retire_held == [{"key": INSTANCE_ID, "code": "instance_active"}]
    assert instance_baseline_key(INSTANCE_ID) in read_persona_instance_baseline(realm_id)
    assert _drift(realm_id, ws) == [], "a held retire turned a live agent into unpublished drift"


def test_a_held_retire_keeps_its_baseline_entry(tmp_path, monkeypatch):
    """The C2 lesson one lane over: dropping the baseline entry for a row that
    is STILL LIVE re-classifies it on the next pull as a local ADD, so a failed
    archive comes back as something to publish.

    Finding a witness for this took three tries and the reason is worth writing
    down, because it is exactly how a guarantee ends up believed and untested.
    Dropping the entry is INVISIBLE in the two obvious scenarios: with the
    projection absent the baseline file is never written at all, and with the
    projection present-and-matching, phase one's ``converged`` arm silently
    re-records the entry two blocks later. The only shape where the drop
    survives to disk is a locally-edited replica whose remote ALSO moved — the
    held arm, which writes the baseline and re-records nothing. So that is the
    shape this pins."""

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    local = PersonaInstanceStore().get(INSTANCE_ID)
    local.display_name = "Locally renamed"
    PersonaInstanceStore()._write(local)
    _write_remote(tmp_path, _body(ws, display_name="Remotely renamed"))

    monkeypatch.setattr(PersonaInstanceStore, "_has_live_binding", lambda self, inst: True)
    summary = apply_persona_instance_pull(realm_id, tmp_path, desks_removed=[INSTANCE_ID])

    assert summary.retire_held == [{"key": INSTANCE_ID, "code": "instance_active"}]
    assert summary.held == [INSTANCE_ID]
    assert instance_baseline_key(INSTANCE_ID) in read_persona_instance_baseline(realm_id)
    # CHANGED, not ADDED: the row is a known agent this machine has edited, not
    # a brand-new local addition the operator is being offered a publish for.
    assert [i.kind for i in _drift(realm_id, ws)] == [DRIFT_KIND_CHANGED]


def test_a_desk_removal_retires_even_when_the_peer_publishes_no_projection(tmp_path):
    """The trigger is the office lane's archive, not this document. A peer can
    retire a desk in the same pull where their projection is absent."""

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    (tmp_path / "store" / "persona_instances.yaml").unlink()
    summary = apply_persona_instance_pull(realm_id, tmp_path, desks_removed=[INSTANCE_ID])
    assert summary.source is None
    assert summary.retired == [INSTANCE_ID]


def test_an_archived_desk_blocks_the_next_pull_from_reviving_its_agent(tmp_path):
    """The resurrection guard, and it is the OFFICE family's own ledger rather
    than a second one: the actor key IS the instance id. Without it a
    retire-follows-the-desk taken in one pull is undone by the very next pull,
    because the retired row reads as locally absent while the realm still
    publishes it."""

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    store = OfficeStore()
    store.upsert_actor(
        ws,
        {
            "persona_id": "dev",
            "persona_instance_id": INSTANCE_ID,
            "items": [{"item_id": "dev", "persona_id": "dev", "kind": "agent", "position": [1.0, 2.0]}],
        },
    )
    store.remove_actor(ws, INSTANCE_ID, reason="remote_removed", updated_by="realm_sync")
    assert INSTANCE_ID in store.get_surface(ws).archived_actor_keys

    apply_persona_instance_pull(realm_id, tmp_path, desks_removed=[INSTANCE_ID])
    assert not paths.persona_instance_path(INSTANCE_ID).exists()

    # The realm STILL publishes the agent. The next pull must not bring it back.
    again = apply_persona_instance_pull(realm_id, tmp_path)
    assert again.replicated == []
    assert again.desk_archived == [INSTANCE_ID]
    assert not paths.persona_instance_path(INSTANCE_ID).exists()


def test_a_desk_removal_for_a_persona_keyed_actor_retires_nothing(tmp_path):
    """A persona-keyed desk's actor key is a persona id, not an instance id, so
    there is simply nothing to retire — and nothing is guessed at."""

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    summary = apply_persona_instance_pull(realm_id, tmp_path, desks_removed=["dev"])
    assert summary.retired == []
    assert summary.retire_held == []
    assert paths.persona_instance_path(INSTANCE_ID).exists()
