"""H2 — the publish arm for replicated persona instances.

What this stage has to make true: the agents behind the desks a realm publishes
travel WITH those desks, resolved by the same walk that resolves the desks, and
nothing else travels at all. The office scan's own coherence argument
(``OfficePublishScan``'s docstring — one walk, one authority, because a second
glob is how the artifact list and the persona list came apart) now covers a
third fact, so it has to hold for that one too: a workspace refused by the gate
contributes no instance either.

Autouse conftest fixtures isolate the runtime root; no test here touches a live
store.
"""

from __future__ import annotations

import yaml

from agent_runtime import paths
from agent_runtime.office_store import OfficeStore
from agent_runtime.persona_instance_sync import (
    PROJECTION_KIND,
    PROJECTION_RELATIVE_PATH,
    instance_baseline_key,
    persona_instance_def_hash,
    read_persona_instance_baseline,
    update_persona_instance_baseline_after_publish,
)
from agent_runtime.realm_sync import (
    _kind_for_sync_path,
    _office_publish_scan,
    _resolve_artifacts_with_projection,
    resolve_realm_sync_artifacts,
)
from agent_runtime.store import RealmStore, WorkspaceStore

INSTANCE_ID = "personainst_dev_agent_9682caf4"


def _make_realm_workspace() -> tuple[str, str]:
    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    return realm.id, ws.id


def _payload(persona_id: str = "dev", *, instance_id: str | None = INSTANCE_ID) -> dict:
    payload: dict = {
        "persona_id": persona_id,
        "items": [
            {
                "item_id": persona_id,
                "persona_id": persona_id,
                "kind": "agent",
                "position": [1.0, 2.0],
                "folder": "Agents",
            }
        ],
    }
    if instance_id is not None:
        payload["persona_instance_id"] = instance_id
    return payload


def _mint_instance(instance_id: str = INSTANCE_ID, *, persona_id: str = "dev", **overrides):
    """A placement-backed instance row on disk, written the way the store writes
    one. Deliberately NOT through ``add_instance``: that lane binds a durable
    chat root through SessionDB, which is machinery this publish-side suite does
    not need and must not depend on."""

    from agent_runtime.models import PersonaInstance
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.states import WorkerSessionState

    base = dict(
        id=instance_id,
        persona_id=persona_id,
        role="developer",
        display_name="Neko",
        profile_id="alice",
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
    )
    base.update(overrides)
    instance = PersonaInstance(**base)
    PersonaInstanceStore()._write(instance)
    return instance


def _published_instance_document(realm_id: str) -> dict | None:
    for artifact in resolve_realm_sync_artifacts(realm_id):
        if artifact.relative_path == PROJECTION_RELATIVE_PATH:
            return yaml.safe_load(artifact.read_bytes().decode("utf-8"))
    return None


def _blind_one_actor(ws: str, actor_key: str):
    path = paths.office_actor_path(ws, actor_key)
    assert path.exists()
    path.write_text("{truncated", encoding="utf-8")
    return path


# ── the scan's fourth fact ────────────────────────────────────────────────


def test_the_publish_scan_yields_instance_ids_from_the_walk_it_already_takes():
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    scan = _office_publish_scan([WorkspaceStore().get(ws)])

    assert scan.instance_ids == [INSTANCE_ID]
    # The three facts move together — same walk, same gate.
    assert scan.persona_ids == ["dev"]
    assert scan.refused == []


def test_a_refused_workspace_contributes_no_instance_id():
    """The office scan refuses a workspace whose actor directory does not fully
    read, because publishing a partial office turns one quarantined file here
    into desk removals on every peer. The instance ids ride that same gate: an
    agent whose desk is not travelling must not travel either."""

    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _mint_instance()
    workspaces = [WorkspaceStore().get(ws)]
    assert _office_publish_scan(workspaces).instance_ids == [INSTANCE_ID]

    _blind_one_actor(ws, INSTANCE_ID)
    scan = _office_publish_scan(workspaces)
    assert [row["workspace_id"] for row in scan.refused] == [ws]
    assert scan.instance_ids == []
    assert scan.persona_ids == []
    # ... and nothing about that instance reaches the wire.
    assert _published_instance_document(realm_id) is None


def test_the_instance_ids_come_off_the_gated_scan_never_a_second_directory_walk(monkeypatch):
    """The C3 argument, extended to the fourth fact.

    A workspace REFUSAL is not enough to prove this: when the gate refuses
    because a file will not decode, a second glob would fail to decode it too
    and the two authorities would agree by accident. The only way they can
    disagree without the gate having refused first is a row the scan does not
    return while its file sits readable on disk — a create landing between the
    scan and the walk — so the divergence is injected rather than staged, the
    same way ``test_publish_ships_the_scan_it_gated_on_never_the_directory``
    does for the artifact list.

    What is pinned is the invariant: the agents that travel are the agents the
    gate cleared.
    """

    realm_id, ws = _make_realm_workspace()
    store = OfficeStore()
    store.upsert_actor(ws, _payload("dev"))
    store.upsert_actor(ws, _payload("latecomer", instance_id="personainst_latecomer_agent_2"))
    _mint_instance()
    _mint_instance("personainst_latecomer_agent_2", persona_id="latecomer")
    assert paths.office_actor_path(ws, "personainst_latecomer_agent_2").exists()

    real_scan = OfficeStore.scan_actors

    def _scan_without_the_latecomer(self, workspace_id, **kwargs):
        scan = real_scan(self, workspace_id, **kwargs)
        return type(scan)(
            [a for a in scan.actors if a.actor_key != "personainst_latecomer_agent_2"],
            # ``unreadable_files``, not the derived ``unreadable`` int: the field
            # became UnreadableActorFiles and a CONSTRUCTOR is not one of the
            # readers the property was kept for. Its twin in
            # tests/agent_runtime/test_office_sync.py was migrated; this one was
            # not, and reddened `main` until the push lane looked.
            scan.unreadable_files,
        )

    monkeypatch.setattr(OfficeStore, "scan_actors", _scan_without_the_latecomer)

    assert _office_publish_scan([WorkspaceStore().get(ws)]).instance_ids == [INSTANCE_ID]
    document = _published_instance_document(realm_id)
    assert sorted(document["instances"]) == [INSTANCE_ID], (
        "an agent the gated scan never admitted was replicated onto every peer"
    )


# ── the published artifact ────────────────────────────────────────────────


def test_the_projection_carries_exactly_the_instances_the_desks_reference():
    """Pruning is part of the contract. A local instance no published desk
    references is an agent this realm is not publishing, and shipping it would
    mint a replica on every peer for a desk they will never see."""

    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _mint_instance()
    _mint_instance("personainst_qa_agent_11112222", persona_id="qa")  # no desk

    document = _published_instance_document(realm_id)
    assert document is not None
    assert document["kind"] == PROJECTION_KIND
    assert sorted(document["instances"]) == [INSTANCE_ID]


def test_runtime_root_never_reaches_the_published_bytes():
    """The single most portability-hostile field on the record, asserted on the
    BYTES rather than on the projection object — the wire is what a peer reads,
    and ``_assert_portable_artifacts`` runs over these same bytes at publish."""

    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _mint_instance()

    artifact = [
        a for a in resolve_realm_sync_artifacts(realm_id)
        if a.relative_path == PROJECTION_RELATIVE_PATH
    ]
    assert len(artifact) == 1
    raw = artifact[0].read_bytes().decode("utf-8")
    assert "runtime_root" not in raw
    assert str(paths.store_root()) not in raw
    assert "default_chat_session_id" not in raw
    assert b"\r" not in artifact[0].read_bytes()


def test_republishing_an_unchanged_realm_is_byte_identical():
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _mint_instance()

    def _bytes():
        return next(
            a.read_bytes()
            for a in resolve_realm_sync_artifacts(realm_id)
            if a.relative_path == PROJECTION_RELATIVE_PATH
        )

    assert _bytes() == _bytes()


def test_a_realm_with_no_instance_backed_desks_publishes_no_instance_artifact():
    """Absence is a real state and must not be an empty document: an empty
    projection published to a peer says nothing about their replicas either way,
    but shipping the file at all would put a byte-churning artifact in every
    realm that has never placed an agent."""

    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev", instance_id=None))
    assert _published_instance_document(realm_id) is None


def test_a_canonical_row_is_never_published_even_when_a_desk_names_it():
    """Replication is scoped to placement-backed rows (plan §2). Canonical
    channels are derived locally on every machine from a persona id that already
    travels; publishing one would be this member asserting a row the receiver's
    own ``ensure_for_personas`` already owns."""

    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev", instance_id="personainst_dev"))
    _mint_instance("personainst_dev", persona_id="dev")

    resolved = _resolve_artifacts_with_projection(realm_id)
    assert resolved.instance_projection.skipped_canonical == ["personainst_dev"]
    assert resolved.instance_projection.instances == {}
    assert _published_instance_document(realm_id) is None


def test_a_desk_whose_instance_row_is_absent_is_reported_missing_not_invented():
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))  # no instance row minted

    resolved = _resolve_artifacts_with_projection(realm_id)
    assert resolved.instance_projection.missing == [INSTANCE_ID]
    assert resolved.instance_projection.instances == {}


def test_the_publish_row_carries_the_projections_accounting_and_the_store_shortfall():
    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _mint_instance()
    # A row that will not decode: the store's shortfall, reported beside the
    # projection's own accounting rather than folded into it.
    paths.persona_instance_path("personainst_broken_agent_1").write_text("{oops", encoding="utf-8")

    resolved = _resolve_artifacts_with_projection(realm_id)
    from agent_runtime.realm_sync import _persona_instance_row

    row = _persona_instance_row(resolved.instance_projection, resolved.instance_rows_unreadable)
    assert row["instances"] == [INSTANCE_ID]
    assert row["rows_unreadable"] == 1
    assert any(key.endswith(".runtime_root") for key in row["dropped_keys"])


# ── path classification + baseline ────────────────────────────────────────


def test_the_projection_path_classifies_as_its_own_kind():
    assert _kind_for_sync_path(PROJECTION_RELATIVE_PATH) == "persona_instance_config"
    # It must not be mistaken for the persona-DEFINITION family beside it: the
    # two documents have different shapes and different appliers.
    assert _kind_for_sync_path("store/personas.yaml") == "persona_config"


def test_the_publish_baseline_leaves_the_publisher_with_nothing_to_hold():
    """The ``_published_profile_file_hashes`` precedent. Without this a member
    who publishes and then pulls sees every row they just shipped as
    locally-edited-and-remotely-changed — a HOLD on their own publish."""

    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _mint_instance()

    resolved = _resolve_artifacts_with_projection(realm_id)
    assert read_persona_instance_baseline(realm_id) == {}
    update_persona_instance_baseline_after_publish(realm_id, resolved.instance_projection)

    baseline = read_persona_instance_baseline(realm_id)
    key = instance_baseline_key(INSTANCE_ID)
    assert baseline[key] == persona_instance_def_hash(
        resolved.instance_projection.instances[INSTANCE_ID]
    )


def test_the_baseline_sidecar_is_never_itself_published():
    """It is a never-synced sidecar by construction — it lives under the
    realm-sync root, not under the store the publish walks."""

    realm_id, ws = _make_realm_workspace()
    OfficeStore().upsert_actor(ws, _payload("dev"))
    _mint_instance()
    update_persona_instance_baseline_after_publish(
        realm_id, _resolve_artifacts_with_projection(realm_id).instance_projection
    )
    assert paths.persona_instance_baseline_path(realm_id).exists()
    published = [a.relative_path for a in resolve_realm_sync_artifacts(realm_id)]
    assert not any("persona_instance_baseline" in path for path in published)
