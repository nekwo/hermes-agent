"""Phase 2b — an instance whose only placement has been archived is a GHOST.

THE LIVE DEFECT, measured 2026-09-11. The operator's Default workspace dropdown
read "3 agents" while the office canvas drew two. The third was
``personainst_chara_a2_7b31d0e4`` ("Chara A2 – Tier1 authoring"): minted by
``persona instance create`` on 08-28, so its id carries no ``_agent_`` marker;
placed on the Default floor on 09-01; removed on 09-04. The launcher routed that
removal to ``runtime.office.remove`` — on the strength of the id's SPELLING —
which archives the actor and nothing else, so the ``persona_instances/`` row
stayed ``state: idle`` and ``harness workspace list`` kept counting it for a
week. Nothing on this side self-healed it either: ``persona-instance reconcile``
holds the row (it is backed by the seeded ``base`` persona, so not an orphan) and
had no classification for "scoped instance whose only placement is archived".

The launcher half is fixed in its own repo (the archive lane asks a FACT for
every actor). This is the hermes half, and it is a different guarantee: a row
that reaches this state by ANY route — an older launcher, a hand-run
``office actor-remove``, a realm pull — gets reported and reaped.

WHAT THESE TESTS PROBE, and why each is not "a dict comes back":

1. The state is reached through the REAL doors, and through the SAME PAIR that
   produced it live: ``PersonaInstanceStore.add_instance`` mints the unmarked
   instance, ``OfficeStore.upsert_actor`` puts it on the floor, and
   ``remove_actor`` archives the actor alone — which is exactly what the
   launcher's wrong lane did. No fixture hand-writes the half-state it is about
   to assert on. ``perform_agent_create`` is deliberately NOT that door: it
   refuses an unmarked placement id, which is pinned below and is the whole
   reason the launcher's spelling split looked safe.
2. The classification turns on EVIDENCE, never on an id shape. The two halves —
   "an archived actor in this workspace names it" and "no live actor anywhere
   does" — are moved one at a time, and each move alone changes the verdict.
3. The HELD rules are in force. A just-removed placement is held (the row was
   written minutes ago), and an instance still bound to live work is held.
4. The canonical operator channel is NEVER classified. Every agent gets a free
   profile row under the base-profile foundation, and those are chat channels,
   not placements.
5. An unreadable office store classifies NOTHING and says so. The central
   question is a NEGATIVE one, so a short read is a false positive that would
   archive a live agent's row.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime import paths
from agent_runtime.office_store import OfficeStore
from agent_runtime.persona_assignments import (
    PersonaInstanceStore,
    persona_instance_id_for,
    row_is_canonical_persona_channel,
)
from agent_runtime.persona_instance_identity import (
    PRUNE_REASON_UNPLACED,
    HELD_REASON_ACTIVE,
    HELD_REASON_RECENT,
    classify_unplaced_persona_instances,
    office_placement_evidence,
    reconcile_persona_instances,
)
from agent_runtime.serde import to_jsonable
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_unplaced_test"
OTHER_WORKSPACE = "ws_unplaced_other"

#: The 2026-09-11 id, verbatim in its shape: an instance minted by
#: ``persona instance create``, so no ``_agent_`` marker, and an office actor's
#: ``persona_instance_id`` all the same.
PLACEMENT_ID = "chara_a2_7b31d0e4"


@pytest.fixture
def qa_persona():
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


@pytest.fixture
def seeded_workspaces(qa_persona):
    for wsid in (WORKSPACE, OTHER_WORKSPACE):
        seed_workspace_record(wsid)
        OfficeStore().ensure_surface(wsid, created_by="seed")
    return OfficeStore()


def _place(*, workspace_id: str = WORKSPACE, placement_id: str = PLACEMENT_ID) -> dict:
    """Chara A2's ACTUAL route onto a floor, through both real doors.

    NOT ``perform_agent_create``. That verb refuses this placement id outright
    (``placement_id_not_discriminable``: "supply the <persona-token>_agent_<hex8>
    shape"), and the refusal is the whole reason the launcher's spelling
    assumption looked safe for a year — the placement verb really does mint
    marked ids. What it does not cover is the SECOND route: ``persona instance
    create`` mints the instance with any token, and the office write lane
    (``upsert_actor``, the same door the launcher's scene save goes through)
    puts it on a floor. That pairing produces an instance-backed actor whose id
    carries no marker, which is exactly what sat in ``ws_default``.

    Both halves are the production calls; only their ORDER is the fixture.
    """

    instance = PersonaInstanceStore().add_instance(
        persona_id="qa",
        placement_id=placement_id,
        display_name="Chara A2 - Tier1 authoring",
        workspace_id=workspace_id,
    )
    OfficeStore().upsert_actor(
        workspace_id,
        {
            "actor_key": instance.id,
            "persona_id": "qa",
            "persona_instance_id": instance.id,
            "items": [
                {
                    "item_id": instance.id,
                    "kind": "agent",
                    "position": [2.0, -3.0],
                    "folder": "Agents",
                }
            ],
        },
    )
    return {"persona_instance_id": instance.id, "actor_key": instance.id}


def test_the_placement_verb_really_does_refuse_this_id_shape(seeded_workspaces):
    """The premise the whole suite rests on, checked rather than assumed.

    If ``agent.create`` ever started accepting an unmarked placement id, the
    fixture above would stop being the only route to this state and the story
    these tests tell about WHY the launcher's spelling split looked safe would
    be wrong. Pinned so that change cannot be silent.
    """

    from agent_runtime.agent_create import perform_agent_create

    outcome = perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": WORKSPACE,
            "position": [0.0, 0.0],
            "idempotency_key": "unplaced-fixture-premise",
            "placement_id": PLACEMENT_ID,
        }
    )
    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "placement_id_not_discriminable"


def _age_row(instance_id: str, *, days: float) -> None:
    """Backdate a row's ``updated_at`` past the min-age grace.

    Written through the store file rather than through an update call on
    purpose: an update would stamp ``updated_at`` to now, which is the very
    field under test.
    """

    path = paths.persona_instance_path(instance_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = (now() - timedelta(days=days)).isoformat()
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _rows() -> list[dict]:
    return [to_jsonable(instance) for instance in PersonaInstanceStore().list_all()]


def _classify(store: OfficeStore, rows: list[dict] | None = None) -> dict:
    live, archived = office_placement_evidence(store)
    return classify_unplaced_persona_instances(
        rows if rows is not None else _rows(),
        live_placement_instance_ids=live or (),
        archived_placement_instance_ids_by_workspace=archived,
    )


def _ids(entries) -> set[str]:
    return {entry["persona_instance_id"] for entry in entries}


# ── 1 + 2: the evidence, moved one half at a time ───────────────────────────


def test_a_placed_instance_is_not_a_candidate(seeded_workspaces):
    """THE CONTROL. Before anything is removed the same row must be invisible to
    this classifier — otherwise every verdict below is just "it flags things"."""

    placed = _place()
    classified = _classify(seeded_workspaces)

    assert _ids(classified["prunable"]) == set()
    assert _ids(classified["held"]) == set()
    assert classified["skipped"] == []
    live, archived = office_placement_evidence(seeded_workspaces)
    assert placed["persona_instance_id"] in live, (
        "the fixture must really place the agent, or the control above passes "
        "for the wrong reason"
    )


def test_the_live_defect_archiving_the_actor_alone_leaves_a_ghost(seeded_workspaces):
    """Chara A2's exact sequence: place, then archive the ACTOR only."""

    placed = _place()
    instance_id = placed["persona_instance_id"]
    assert instance_id == f"personainst_{PLACEMENT_ID}", instance_id
    assert not row_is_canonical_persona_channel(instance_id, "qa"), (
        "the fixture's row must not BE the qa operator channel, or clause 2 of "
        "the classifier is what is being tested rather than the placement"
    )

    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    _age_row(instance_id, days=5)

    classified = _classify(seeded_workspaces)
    assert _ids(classified["prunable"]) == {instance_id}
    entry = classified["prunable"][0]
    assert entry["reason"] == PRUNE_REASON_UNPLACED
    assert entry["workspace_id"] == WORKSPACE


def test_a_live_placement_in_ANOTHER_workspace_spares_the_row(seeded_workspaces):
    """Clause 4, and the arm a "same workspace" reading would get wrong.

    The row's own workspace holds only the archived actor; a second workspace
    holds a live one for the same instance. The character is on a floor, and
    reaping it would delete something the operator can see.
    """

    placed = _place()
    instance_id = placed["persona_instance_id"]
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    _age_row(instance_id, days=5)

    # The same instance, placed on a second floor — written directly because
    # ``perform_agent_create`` would mint a SECOND instance, and the subject
    # here is one instance with two placements.
    seeded_workspaces.upsert_actor(
        OTHER_WORKSPACE,
        {
            "actor_key": instance_id,
            "persona_id": "qa",
            "persona_instance_id": instance_id,
            "items": [
                {
                    "item_id": instance_id,
                    "kind": "agent",
                    "position": [1.0, 1.0],
                    "folder": "Agents",
                }
            ],
        },
    )

    classified = _classify(seeded_workspaces)
    assert _ids(classified["prunable"]) == set()
    assert _ids(classified["held"]) == set()


def test_no_archived_placement_means_no_candidate(seeded_workspaces):
    """Clause 3. A workspace-scoped row that was NEVER placed is not a ghost —
    it is an instance somebody made and has not put anywhere. Without this
    clause the classifier would reap every freshly created scoped instance."""

    placed = _place()
    instance_id = placed["persona_instance_id"]
    _age_row(instance_id, days=5)

    # Remove the actor WITHOUT leaving an archive copy behind, which is what a
    # store that never held a placement for this instance looks like.
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    archive_dir = paths.office_archive_dir(WORKSPACE)
    for path in archive_dir.glob("*.json"):
        path.unlink()

    classified = _classify(seeded_workspaces)
    assert _ids(classified["prunable"]) == set()
    assert _ids(classified["held"]) == set()


# ── 3: the held rules ───────────────────────────────────────────────────────


def test_a_just_removed_placement_is_HELD_not_pruned(seeded_workspaces):
    """The min-age grace, in force. An operator who removes a desk and keeps
    chatting to the agent in the same minute gets the row held."""

    placed = _place()
    instance_id = placed["persona_instance_id"]
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    # No ``_age_row``: the row was written moments ago.

    classified = _classify(seeded_workspaces)
    assert _ids(classified["prunable"]) == set()
    assert _ids(classified["held"]) == {instance_id}
    assert classified["held"][0]["reason"] == HELD_REASON_RECENT


def test_an_instance_bound_to_live_work_is_HELD(seeded_workspaces):
    placed = _place()
    instance_id = placed["persona_instance_id"]
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    _age_row(instance_id, days=5)

    path = paths.persona_instance_path(instance_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["active_run_id"] = "run_still_going"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    classified = _classify(seeded_workspaces)
    assert _ids(classified["prunable"]) == set()
    assert classified["held"][0]["reason"] == HELD_REASON_ACTIVE


# ── 4: the canonical operator channel ───────────────────────────────────────


def test_the_canonical_persona_channel_is_never_classified(seeded_workspaces):
    """The base-profile foundation gives every agent a free PROFILE row. Those
    are operator chat channels, not placements, and the discriminator is the
    one ``agent retire`` refuses on — never an id shape."""

    canonical = persona_instance_id_for("qa")
    assert row_is_canonical_persona_channel(canonical, "qa")

    from agent_runtime.models import PersonaInstance
    from agent_runtime.states import WorkerSessionState

    instance = PersonaInstance(
        id=canonical,
        persona_id="qa",
        role="qa",
        display_name="QA Agent",
        profile_id=None,
        runtime_root=str(paths.store_root()),
        state=WorkerSessionState.IDLE,
        mode="chat",
        workspace_id=WORKSPACE,
        updated_at=now() - timedelta(days=5),
    )
    path = paths.persona_instance_path(canonical)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(instance), indent=2, sort_keys=True), encoding="utf-8")

    # Give it an ARCHIVED actor and no live one — i.e. every other clause holds.
    seeded_workspaces.upsert_actor(
        WORKSPACE,
        {
            "actor_key": canonical,
            "persona_id": "qa",
            "persona_instance_id": canonical,
            "items": [
                {
                    "item_id": canonical,
                    "kind": "agent",
                    "position": [3.0, 3.0],
                    "folder": "Agents",
                }
            ],
        },
    )
    seeded_workspaces.remove_actor(WORKSPACE, canonical)

    classified = _classify(seeded_workspaces)
    assert canonical not in _ids(classified["prunable"])
    assert canonical not in _ids(classified["held"])


# ── 5: an unreadable store classifies nothing ───────────────────────────────


def test_an_unreadable_office_store_classifies_NOTHING_and_says_so(seeded_workspaces):
    """A short read would answer "no live placement anywhere" for an instance
    that has one. Refusing the phase costs a repeat run; a partial answer costs
    an agent."""

    placed = _place()
    instance_id = placed["persona_instance_id"]
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    _age_row(instance_id, days=5)

    # POSITIVE CONTROL: the same store, readable, DOES flag this row. Without
    # it "classified nothing" is satisfied by a fixture that never had a
    # candidate.
    assert _ids(_classify(seeded_workspaces)["prunable"]) == {instance_id}

    (paths.office_actors_dir(WORKSPACE) / "unreadable.json").write_text(
        "{not json", encoding="utf-8"
    )

    live, archived = office_placement_evidence(seeded_workspaces)
    assert live is None and archived is None
    classified = classify_unplaced_persona_instances(
        _rows(),
        live_placement_instance_ids=(),
        archived_placement_instance_ids_by_workspace=archived,
    )
    assert classified["prunable"] == []
    assert classified["held"] == []
    assert classified["skipped"] == [{"reason": "office_store_unreadable"}]


# ── the reconciler, end to end ──────────────────────────────────────────────


def test_reconcile_dry_run_reports_the_ghost_and_writes_nothing(seeded_workspaces):
    placed = _place()
    instance_id = placed["persona_instance_id"]
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    _age_row(instance_id, days=5)

    report = reconcile_persona_instances(apply=False)
    assert _ids(report["unplaced_pruned"]) == {instance_id}
    assert report["unplaced_pruned_count"] == 1
    assert report["unplaced_skipped"] == []
    assert report["unplaced_archive_dir"] is None
    assert paths.persona_instance_path(instance_id).exists(), (
        "a dry run must not move the row"
    )


def test_reconcile_apply_archives_the_ghost_and_never_deletes_it(seeded_workspaces):
    placed = _place()
    instance_id = placed["persona_instance_id"]
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    _age_row(instance_id, days=5)

    report = reconcile_persona_instances(apply=True)
    assert _ids(report["unplaced_pruned"]) == {instance_id}
    assert not paths.persona_instance_path(instance_id).exists()

    archive_dir = report["unplaced_archive_dir"]
    assert archive_dir is not None
    archived_copies = list((paths.persona_instances_archive_dir()).rglob(f"{instance_id}.json"))
    assert archived_copies, "archive-never-delete: the row must still exist somewhere"

    # And the count the launcher prints is now honest.
    assert instance_id not in {row["id"] for row in _rows()}


def test_reconcile_is_idempotent_on_the_unplaced_lane(seeded_workspaces):
    placed = _place()
    seeded_workspaces.remove_actor(WORKSPACE, placed["actor_key"])
    _age_row(placed["persona_instance_id"], days=5)

    reconcile_persona_instances(apply=True)
    second = reconcile_persona_instances(apply=True)
    assert second["unplaced_pruned"] == []
    assert second["unplaced_held"] == []
