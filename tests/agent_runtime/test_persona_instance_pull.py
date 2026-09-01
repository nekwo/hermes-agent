"""H3 — the mint door: a pulled desk that has no agent here gets one.

Every row of the plan's §3.3 decision table as a unit, plus the four properties
the stage exists to guarantee:

* **idempotence** — a second pull of an unchanged realm is all ``converged`` and
  writes nothing;
* **baseline alignment** — a fresh replica reads ZERO drift immediately, or the
  just-landed revert lane offers to archive correct state;
* **HOLD never clobbers** — a locally-edited replica survives a moved remote;
* **the patch is emitted** — asserted on the EVENT LOG, never on the file,
  because the defect this forbids (``apply_board_pull``'s event-less adopt) is
  invisible to a file assertion by construction.

Autouse conftest fixtures isolate the runtime root.
"""

from __future__ import annotations

import json

import pytest
import yaml

from agent_runtime import paths
from agent_runtime.events import EventLog
from agent_runtime.models import PersonaInstance
from agent_runtime.persona_assignments import PersonaInstanceStore
from agent_runtime.persona_instance_sync import (
    PROJECTION_KIND,
    PROJECTION_RELATIVE_PATH,
    apply_persona_instance_pull,
    instance_baseline_key,
    instance_conflict_path,
    persona_instance_def_hash,
    read_persona_instance_baseline,
    write_persona_instance_baseline,
)
from agent_runtime.states import WorkerSessionState
from agent_runtime.store import RealmStore, WorkspaceStore

INSTANCE_ID = "personainst_dev_agent_9682caf4"


def _realm_workspace() -> tuple[str, str]:
    realm = RealmStore().create(name="Realm")
    ws = WorkspaceStore().create(name="WS", realm_id=realm.id)
    realm = RealmStore().get(realm.id)
    realm.workspace_ids.append(ws.id)
    RealmStore().save(realm)
    WorkspaceStore().set_active(ws.id)
    return realm.id, ws.id


def _body(instance_id: str = INSTANCE_ID, **overrides) -> dict:
    body = {
        "id": instance_id,
        "persona_id": "dev",
        "display_name": "Neko",
        "mode": "configured",
        "realm_id": "realm_home",
        "workspace_id": "ws_testv4_afb811",
    }
    body.update(overrides)
    return body


def _write_remote(subtree, *bodies: dict) -> None:
    path = subtree.joinpath(*PROJECTION_RELATIVE_PATH.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "instances": {body["id"]: body for body in bodies},
        "kind": PROJECTION_KIND,
        "schema_version": 1,
    }
    path.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")


def _local(instance_id: str = INSTANCE_ID, **overrides) -> PersonaInstance:
    base = dict(
        id=instance_id,
        persona_id="dev",
        role="developer",
        display_name="Neko",
        profile_id="alice",
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
        mode="configured",
        realm_id="realm_home",
        workspace_id="ws_testv4_afb811",
    )
    base.update(overrides)
    instance = PersonaInstance(**base)
    PersonaInstanceStore()._write(instance)
    return instance


def _seed_baseline(realm_id: str, body: dict) -> None:
    write_persona_instance_baseline(
        realm_id, {instance_baseline_key(body["id"]): persona_instance_def_hash(body)}
    )


def _events(event_type: str | None = None) -> list[dict]:
    return [
        {"type": event.type, "payload": dict(event.payload or {}), "persona_id": event.persona_id}
        for _, event in EventLog().iter_from_offset(0)
        if event_type is None or event.type == event_type
    ]


def _row_mtimes() -> dict[str, float]:
    directory = paths.persona_instances_dir()
    if not directory.exists():
        return {}
    return {p.name: p.stat().st_mtime_ns for p in directory.glob("*.json")}


# ── §3.3, row by row ──────────────────────────────────────────────────────


def test_absent_local_absent_baseline_present_remote_mints(tmp_path):
    """Row 1 — THE ruling. The desk arrived; the agent did not exist here."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())

    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert summary.replicated == [INSTANCE_ID]
    assert summary.adopted == summary.held == summary.refused == []

    minted = PersonaInstanceStore().get(INSTANCE_ID)
    assert minted.persona_id == "dev"
    assert minted.display_name == "Neko"
    assert minted.realm_id == "realm_home"
    assert minted.workspace_id == "ws_testv4_afb811"
    # §1.3: derived HERE, never carried.
    assert minted.runtime_root == str(paths.store_root())
    assert minted.state == WorkerSessionState.IDLE
    assert minted.default_chat_session_id
    assert minted.default_chat_session_id.startswith(f"persona_chat_{INSTANCE_ID}_")


def test_unchanged_local_moved_remote_adopts_only_travelling_fields(tmp_path):
    """Row 2. The realm moved the authored surface forward; every §1.2 field on
    the local row survives it — which is the whole record split, enforced by the
    door rather than remembered by the applier."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)

    # Local runtime state accrues after the mint: a live run binding, a chat
    # head, a goal the operator typed into THIS machine's conversation.
    local = PersonaInstanceStore().get(INSTANCE_ID)
    local.active_run_id = "run_local_only"
    local.chat_head_home = "alice"
    local.current_chat_goal = "ship the thing"
    local.token_budget_used = 4242
    chat_root = local.default_chat_session_id
    PersonaInstanceStore()._write(local)

    _write_remote(tmp_path, _body(display_name="Neko II", model="claude-x"))
    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert summary.adopted == [INSTANCE_ID]
    assert summary.replicated == []

    after = PersonaInstanceStore().get(INSTANCE_ID)
    assert after.display_name == "Neko II"
    assert after.model == "claude-x"
    # Never touched:
    assert after.active_run_id == "run_local_only"
    assert after.chat_head_home == "alice"
    assert after.current_chat_goal == "ship the thing"
    assert after.token_budget_used == 4242
    assert after.default_chat_session_id == chat_root
    assert after.runtime_root == str(paths.store_root())


def test_local_equals_baseline_equals_remote_converges_without_writing(tmp_path):
    """Row 3, and the IDEMPOTENCE property: a second pull of an unchanged realm
    takes the converged arm for every row and writes NOTHING."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)

    before = _row_mtimes()
    events_before = len(_events())
    summary = apply_persona_instance_pull(realm_id, tmp_path)

    assert summary.converged == [INSTANCE_ID]
    assert summary.replicated == summary.adopted == []
    assert summary.changed is False
    assert _row_mtimes() == before, "a converged pull rewrote the row"
    assert len(_events()) == events_before, "a converged pull emitted an event"


def test_a_locally_edited_replica_is_held_and_never_clobbered(tmp_path):
    """Row 4. Both sides moved. The local row is left exactly as it was and the
    body this pull refused to adopt is parked where an operator can find it."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)

    local = PersonaInstanceStore().get(INSTANCE_ID)
    local.display_name = "Locally renamed"
    PersonaInstanceStore()._write(local)

    _write_remote(tmp_path, _body(display_name="Remotely renamed"))
    summary = apply_persona_instance_pull(realm_id, tmp_path)

    assert summary.held == [INSTANCE_ID]
    assert PersonaInstanceStore().get(INSTANCE_ID).display_name == "Locally renamed"
    sidecar = instance_conflict_path(realm_id, INSTANCE_ID)
    assert sidecar.exists()
    parked = json.loads(sidecar.read_text(encoding="utf-8"))
    assert parked["remote_body"]["display_name"] == "Remotely renamed"
    assert parked["kind"] == "both_changed"


def test_a_row_the_realm_stopped_publishing_is_not_a_delete(tmp_path):
    """Row 5, and §5.2's argument. Absence is short-answer-shaped: the
    publisher's own office scan refuses a workspace it could not fully read, so
    a missing row is exactly as consistent with 'their scan came back short' as
    with 'the operator deleted it'. Only the DESK's removal is authored intent.
    """

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)

    _write_remote(tmp_path)  # the realm now publishes nothing
    summary = apply_persona_instance_pull(realm_id, tmp_path)

    assert summary.upstream_absent == [INSTANCE_ID]
    assert paths.persona_instance_path(INSTANCE_ID).exists()
    # The baseline is KEPT, so a repaired publish still converges instead of
    # re-reading the surviving replica as a brand-new local addition.
    assert instance_baseline_key(INSTANCE_ID) in read_persona_instance_baseline(realm_id)


def test_a_refused_row_leaves_the_store_untouched_and_the_pull_continues(tmp_path):
    """Row 6 + per-entity isolation. One hostile row must never cost the realm
    its other agents."""

    realm_id, _ = _realm_workspace()
    good = _body("personainst_qa_agent_11112222", persona_id="qa", display_name="QA")
    hostile = _body(runtime_root=r"X:\Eternia\.hermes")
    _write_remote(tmp_path, good, hostile)

    summary = apply_persona_instance_pull(realm_id, tmp_path)

    assert summary.replicated == ["personainst_qa_agent_11112222"]
    assert [row["key"] for row in summary.refused] == [INSTANCE_ID]
    assert summary.refused[0]["code"] == "nonportable_path"
    assert not paths.persona_instance_path(INSTANCE_ID).exists()
    assert instance_baseline_key(INSTANCE_ID) not in read_persona_instance_baseline(realm_id)


def test_a_locally_edited_row_the_realm_did_not_move_stays_local(tmp_path):
    """The row the plan's five-row table folds into 'not held'. Named because
    the H4 drift lane reports exactly these, and because silently adopting over
    an unpublished local edit is the clobber this whole lane exists to end."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)

    local = PersonaInstanceStore().get(INSTANCE_ID)
    local.display_name = "Locally renamed"
    PersonaInstanceStore()._write(local)

    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert summary.kept_local == [INSTANCE_ID]
    assert PersonaInstanceStore().get(INSTANCE_ID).display_name == "Locally renamed"


# ── the properties ────────────────────────────────────────────────────────


def test_a_fresh_replica_reads_zero_drift_immediately(tmp_path):
    """THE baseline-alignment property (plan §3.3, §6 non-negotiable).

    The baseline is keyed off the REMOTE hash, never re-derived from the local
    write. Without it the very next ``realm sync status`` reports the replica as
    an unpublished local addition — and the revert lane that landed the same day
    would offer to ARCHIVE correct state.
    """

    realm_id, ws = _realm_workspace()
    body = _body(workspace_id=ws)
    _write_remote(tmp_path, body)
    apply_persona_instance_pull(realm_id, tmp_path)

    baseline = read_persona_instance_baseline(realm_id)
    assert baseline[instance_baseline_key(INSTANCE_ID)] == persona_instance_def_hash(body)

    # The local projection of what was actually minted hashes to the SAME value,
    # which is what "zero drift" means for this family.
    from agent_runtime.persona_instance_sync import project_persona_instance

    minted = PersonaInstanceStore().get(INSTANCE_ID)
    assert persona_instance_def_hash(project_persona_instance(minted)) == baseline[
        instance_baseline_key(INSTANCE_ID)
    ]


def test_the_mint_emits_a_delta_patch_on_the_event_log(tmp_path):
    """Asserted on the EVENT LOG, not on the file, because the defect this
    forbids is invisible to a file assertion: ``apply_board_pull`` writes cards
    with raw atomic writes, so a pull that GIVES you a card reaches no live
    consumer while one that ARCHIVES a card emits (open queue row). A replicated
    instance that needs an app restart to appear has not been replicated."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)

    types = [row["type"] for row in _events()]
    assert "state.patched" in types, "the mint reached no live consumer"
    assert "persona_instance.replicated" in types
    # NOT the authored-create event: that means "this machine authored an agent"
    # to every consumer that reads it, and one pull would look like N creates.
    assert "persona_instance.created" not in types

    replicated = [row for row in _events("persona_instance.replicated")]
    assert replicated[0]["payload"]["source"] == "realm_sync"
    assert replicated[0]["payload"]["realm_id"] == realm_id
    assert replicated[0]["payload"]["persona_instance_id"] == INSTANCE_ID
    assert replicated[0]["payload"]["action"] == "replicated"

    patch = [row for row in _events("state.patched")][0]
    assert patch["payload"]["entity"] == "persona_instance"
    assert patch["payload"]["id"] == INSTANCE_ID
    # A complete-row insert, not a merge onto a target the client does not hold:
    # a replicated agent is a row nobody downstream has ever seen.
    assert patch["payload"]["created"] is True
    assert patch["payload"]["changed"]["display_name"] == "Neko"


def test_replication_never_mints_a_tombstone(tmp_path):
    """A non-negotiable: nothing is being deleted, on any arm of this lane."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)
    _write_remote(tmp_path)  # remote drops it — the upstream_absent arm
    apply_persona_instance_pull(realm_id, tmp_path)

    types = [row["type"] for row in _events()]
    assert "persona_instance.retired" not in types
    assert "persona_instance.pruned" not in types
    assert not list(paths.persona_instances_archive_dir().glob("*")) if paths.persona_instances_archive_dir().exists() else True


def test_the_mint_writes_no_office_row(tmp_path):
    """The desk arrives through its own lane. Writing it again here would be a
    second authority over one row."""

    from agent_runtime.office_store import OfficeStore

    realm_id, ws = _realm_workspace()
    _write_remote(tmp_path, _body(workspace_id=ws))
    apply_persona_instance_pull(realm_id, tmp_path)

    assert OfficeStore().list_workspaces() == [] or not OfficeStore().actor_exists(ws, INSTANCE_ID)


def test_the_store_door_cannot_express_importing_a_live_binding():
    """The door's OWN contract, asserted at the door rather than through the
    applier.

    Reached through ``apply_persona_instance_pull`` this is unreachable: the
    admission door refuses any body carrying a key outside the allowlist
    (``unexpected_key``), so a body like this never gets here. That makes the
    door's allowlist look redundant — and it is not, because
    ``replicate_instance`` is a PUBLIC store verb and the next caller may not be
    the pull. The arm writes only allowlisted names onto a copy of the existing
    record, so "adopt a peer's run binding" is a thing this door cannot say even
    when handed one.
    """

    _realm_workspace()
    local = _local(active_run_id="run_local_only", chat_head_home="alice", token_budget_used=7)
    store = PersonaInstanceStore()

    store.replicate_instance(
        {
            **_body(),
            "display_name": "Adopted",
            # Every §1.2 field a hostile or buggy caller might hand over:
            "active_run_id": "run_from_a_peer",
            "chat_head_home": "somebody_elses_profile",
            "default_chat_session_id": "persona_chat_elsewhere_000000000000",
            "runtime_root": "/somebody/elses/box",
            "token_budget_used": 999999,
        },
        realm_id="realm_home",
        adopt_existing=local,
    )

    after = store.get(INSTANCE_ID)
    assert after.display_name == "Adopted", "the travelling surface did not land"
    assert after.active_run_id == "run_local_only"
    assert after.chat_head_home == "alice"
    assert after.token_budget_used == 7
    assert after.runtime_root == str(paths.store_root())
    assert after.default_chat_session_id == local.default_chat_session_id


# ── §3.4 the ordering hazard ──────────────────────────────────────────────


def test_a_steering_edge_naming_a_row_minted_later_in_the_same_pass_still_lands(tmp_path):
    """Phase two exists for exactly this. ``personainst_a…`` sorts BEFORE
    ``personainst_z…``, so a single-phase applier would reach the child first,
    find its parent absent, and drop an edge whose target this same pull was
    about to write. The outcome would depend on the alphabetical order of ids.
    """

    realm_id, _ = _realm_workspace()
    child = _body("personainst_aaa_agent_1", steered_by=["personainst_zzz_agent_9"])
    parent = _body("personainst_zzz_agent_9")
    _write_remote(tmp_path, child, parent)

    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert sorted(summary.replicated) == ["personainst_aaa_agent_1", "personainst_zzz_agent_9"]
    assert summary.steering_dropped == []
    assert PersonaInstanceStore().get("personainst_aaa_agent_1").steered_by == [
        "personainst_zzz_agent_9"
    ]


def test_an_edge_whose_parent_never_arrives_is_dropped_with_a_row(tmp_path):
    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body(steered_by=["personainst_ghost_agent_7"]))

    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert summary.replicated == [INSTANCE_ID]
    assert summary.steering_dropped == [
        {"key": INSTANCE_ID, "parent": "personainst_ghost_agent_7", "reason": "parent_absent"}
    ]
    assert PersonaInstanceStore().get(INSTANCE_ID).steered_by == []


def test_a_remote_graph_carrying_a_cycle_cannot_install_one(tmp_path):
    """Every edge in a hostile graph passes the CONTENT door — they are all
    well-formed instance ids. ``_validate_no_steering_cycle`` is what stops the
    graph, and it runs on phase two so it sees the rows this pass wrote."""

    realm_id, _ = _realm_workspace()
    a = _body("personainst_aa_agent_1", steered_by=["personainst_bb_agent_2"])
    b = _body("personainst_bb_agent_2", steered_by=["personainst_aa_agent_1"])
    _write_remote(tmp_path, a, b)

    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert len(summary.replicated) == 2
    assert [row["reason"] for row in summary.steering_dropped] == ["ValueError"]

    store = PersonaInstanceStore()
    edges = {
        i: store.get(i).steered_by
        for i in ("personainst_aa_agent_1", "personainst_bb_agent_2")
    }
    assert [] in edges.values(), f"a cycle was installed: {edges}"


def test_a_self_edge_is_dropped(tmp_path):
    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body(steered_by=[INSTANCE_ID]))
    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert [row["reason"] for row in summary.steering_dropped] == ["self_edge"]
    assert PersonaInstanceStore().get(INSTANCE_ID).steered_by == []


# ── version skew + read failures ──────────────────────────────────────────


def test_an_older_publisher_reports_a_null_source_and_touches_nothing(tmp_path):
    """The version-skew contract the launcher keys its badge demotion on. An
    absent projection is NOT an empty one, and it must never strand this
    machine's replicas."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)
    before = read_persona_instance_baseline(realm_id)

    (tmp_path / "store" / "persona_instances.yaml").unlink()
    summary = apply_persona_instance_pull(realm_id, tmp_path)

    assert summary.source is None
    assert summary.upstream_absent == []
    assert summary.as_dict()["source"] is None
    assert read_persona_instance_baseline(realm_id) == before
    assert paths.persona_instance_path(INSTANCE_ID).exists()


def test_a_projection_that_will_not_decode_is_refused_never_read_as_absence(tmp_path):
    """Absence drives ``upstream_absent`` for every baselined row, so reading a
    parse error as absence would be a delete-shaped decision taken on a read
    failure — the ``RemoteOffice.unreadable`` argument."""

    realm_id, _ = _realm_workspace()
    _write_remote(tmp_path, _body())
    apply_persona_instance_pull(realm_id, tmp_path)

    (tmp_path / "store" / "persona_instances.yaml").write_text("{ not: [yaml", encoding="utf-8")
    summary = apply_persona_instance_pull(realm_id, tmp_path)

    assert summary.source == "unreadable"
    assert summary.upstream_absent == []
    assert [row["code"] for row in summary.refused] == ["unreadable_projection"]


def test_a_local_row_that_will_not_decode_is_refused_never_minted_over(tmp_path):
    """Absent is what drives the MINT arm, so folding a parse error into it
    would overwrite a row that might carry a live run binding."""

    realm_id, _ = _realm_workspace()
    _local()
    paths.persona_instance_path(INSTANCE_ID).write_text("{truncated", encoding="utf-8")
    _write_remote(tmp_path, _body())

    summary = apply_persona_instance_pull(realm_id, tmp_path)
    assert [row["code"] for row in summary.refused] == ["local_row_unreadable"]
    assert summary.replicated == []
    assert paths.persona_instance_path(INSTANCE_ID).read_text(encoding="utf-8") == "{truncated"


def test_the_pull_ack_carries_the_summary_under_the_contract_key():
    """The ONE contract seam the launcher pins to (plan §6)."""

    from agent_runtime.persona_instance_sync import PersonaInstancePullSummary

    shape = PersonaInstancePullSummary().as_dict()
    assert sorted(shape) == [
        "adopted",
        "converged",
        "held",
        "kept_local",
        "refused",
        "replicated",
        "source",
        "steering_dropped",
        "upstream_absent",
    ]
    assert shape["source"] is None
    assert all(shape[key] == [] for key in shape if key != "source")
