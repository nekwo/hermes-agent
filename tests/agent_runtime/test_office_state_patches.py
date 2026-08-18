"""S7-A office leg: the PUSH half of the office pilot.

The pilot's three legs are ``runtime.office.get`` (read), ``runtime.office.upsert``
(write), and this one — making a drag arrive as a ~700-byte patch instead of the
~842 KB full core an office write costs today.

What these tests are actually defending, in the order the design decisions were
made:

**Granularity.** ``office_actor`` keyed by ``workspace_id/actor_key``, not the
surface and not the item. Measured on a copy of the live canvas (four actors,
2026-08-14): the whole office row is 3158 bytes against a 3584-byte per-value
budget — so surface granularity would blow the 4 KB cap and degrade to
``refresh`` at roughly the seventh desk, on a surface bounded at 200. One actor
row is 663–764 bytes. ``test_office_actor_patch_is_small_against_the_office_row``
pins the ratio rather than the prose.

**Monotonicity.** The stream watermark is the EventLog offset and was already
monotonic; the ENTITY watermark is ``actor.revision``, which only agrees with
the offset order because the emit happens inside ``office_lock`` from the object
just written. ``test_concurrent_writers_never_invert_revision_against_offset``
runs real concurrent writers through the real cross-process lock and asserts the
two orders agree.

**The surface-row fence.** A patch describes ONE actor row. Every office write
that also moves the parent row — a create (``actor_count``), a re-add of an
archived key (``archived_actor_keys`` + surface ``updated_at``), an archive, a
restore — must NOT ship as a foldable patch. Three tests hold that line from
both sides: the degrade fires, and the batch carrying it demotes.

**Fidelity.** ``changed`` is asserted byte-equal to the row a full
``build_snapshot()`` rebuild produces, so the fold and the rebuild cannot
disagree. ``unpublished`` gets its own test because it is the one field the
snapshot's row builder does not compute and the one a drag actually flips.

Anti-vacuity discipline, inherited from ``test_patch_fold_negotiation.py``:
every fixture seeds TWO actors at DIFFERENT revisions, because a single-entity
fixture cannot catch a hoisted constant.
"""

from __future__ import annotations

import json
import threading

import pytest

from agent_runtime import state_patches as sp
from agent_runtime.config import load_agent_runtime_config
from agent_runtime.events import EVENT_PAYLOAD_LIMIT_BYTES, EventLog
from agent_runtime.office_store import OfficeStore
from agent_runtime.patch_coverage import (
    HISTORICAL_FOLD_ENTITIES,
    LIVE_COVERED_DOMAIN_EVENT_TYPES,
    OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
    OFFICE_SURFACE_FOLD_CAPABILITY,
    PERSONA_INSTANCE_CREATE_CAPABILITY,
    batch_is_patch_coverable,
    event_is_patch_coverable,
)
from agent_runtime.serde import to_jsonable
from agent_runtime.state_patches import (
    OFFICE_ACTOR_ENTITY,
    OFFICE_SURFACE_ENTITY,
    OFFICE_SURFACE_PATCH_FIELDS,
    PATCH_OP_REFRESH,
    PATCH_OP_UPSERT,
    STATE_PATCHED_EVENT_TYPE,
    office_actor_patch_id,
)
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_office_patch_test"


@pytest.fixture
def set_delta_patches(monkeypatch):
    """Flip ``read_model.delta_patches`` for the producer chokepoints (which
    read the ROOT config through ``load_root_runtime_config``)."""

    def _apply(enabled: bool):
        def _loader(*args, **kwargs):
            cfg = load_agent_runtime_config(*args, **kwargs)
            cfg.read_model.delta_patches = enabled
            return cfg

        monkeypatch.setattr(sp, "load_root_runtime_config", _loader)

    return _apply


def _actor_payload(persona_id: str, *, x: float, y: float, instance: str | None = None) -> dict:
    """One actor: an agent item plus its coupled desk — the live canvas's shape."""

    key = instance or f"personainst_{persona_id}_agent_0001"
    return {
        "persona_id": persona_id,
        "persona_instance_id": key,
        "items": [
            {
                "item_id": key,
                "kind": "agent",
                "persona_id": persona_id,
                "position": [x, y],
                "folder": "Agents",
                "display_name": f"{persona_id} agent",
            },
            {
                "item_id": f"{persona_id}_desk",
                "kind": "desk",
                "persona_id": persona_id,
                "position": [x, y + 2.5],
                "folder": "Desks",
            },
        ],
    }


@pytest.fixture
def seeded_office(isolate_agent_runtime_root):
    """TWO actors at DIFFERENT revisions.

    Two, because this codebase has twice shipped a green mutation that a
    single-entity fixture could not catch: with one row in the store, a producer
    that hoisted a constant — the same actor key, the same revision, the same
    projected row for every patch — is indistinguishable from a correct one.
    Different revisions, because equal ones make an off-by-one or a
    read-the-other-actor bug invisible too.

    ``qa`` is written twice (revision 2), ``dev`` once (revision 1).
    """

    store = OfficeStore()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(WORKSPACE, _actor_payload("qa", x=-8.0, y=-2.0))
    store.upsert_actor(WORKSPACE, _actor_payload("qa", x=-7.0, y=-2.0))
    store.upsert_actor(WORKSPACE, _actor_payload("dev", x=3.0, y=1.0))
    assert store.get_actor(WORKSPACE, "personainst_qa_agent_0001").revision == 2
    assert store.get_actor(WORKSPACE, "personainst_dev_agent_0001").revision == 1
    return store


def _drain() -> list:
    return [e for _, e in EventLog().iter_from_offset(0)]


def _log_end() -> int:
    return max((o for o, _ in EventLog().iter_from_offset(0)), default=0)


# --------------------------------------------------------------------------- #
# Identity
# --------------------------------------------------------------------------- #
def test_patch_id_is_workspace_scoped_because_actor_keys_are_not_unique():
    """An ``actor_key`` alone cannot address an actor.

    Actor files live at ``office/<workspace_id>/actors/<token>.json``, so the
    SAME key legitimately exists in two workspaces. A bare key on the wire would
    let a drag in one office move a desk in another.
    """

    a = office_actor_patch_id("ws_alpha", "personainst_qa_agent_0001")
    b = office_actor_patch_id("ws_beta", "personainst_qa_agent_0001")
    assert a != b
    assert a == "ws_alpha/personainst_qa_agent_0001"
    # Split on the FIRST separator, and the separator must be absent from both
    # halves — ``:`` survives the store's id filter and so could not be used.
    workspace, _, actor_key = a.partition("/")
    assert (workspace, actor_key) == ("ws_alpha", "personainst_qa_agent_0001")


def test_patch_id_survives_a_colon_bearing_actor_key():
    """``_safe_id`` keeps ``:``, so a colon separator would have been ambiguous
    and ``/`` (which it rewrites to ``_``) is the only safe one."""

    patch_id = office_actor_patch_id("ws_a", "profile:dev:instance")
    workspace, _, actor_key = patch_id.partition("/")
    assert (workspace, actor_key) == ("ws_a", "profile:dev:instance")


# --------------------------------------------------------------------------- #
# The hot path: a drag is a small, foldable, complete patch
# --------------------------------------------------------------------------- #
def test_drag_emits_one_office_actor_upsert_for_the_dragged_actor_only(
    seeded_office, set_delta_patches
):
    """Moving ``qa`` patches ``qa`` — not ``dev``, and not both."""

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))

    new = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ]
    assert len(new) == 1
    patch = new[0]
    assert patch["entity"] == OFFICE_ACTOR_ENTITY
    assert patch["op"] == PATCH_OP_UPSERT
    # The DRAGGED actor, at ITS OWN identity — a hoisted key would name `dev`
    # or a constant here, which is why the fixture holds two actors.
    assert patch["id"] == f"{WORKSPACE}/personainst_qa_agent_0001"
    # And the moved position actually crossed the wire.
    agent_item = next(i for i in patch["changed"]["items"] if i["kind"] == "agent")
    assert agent_item["position"] == [1.5, 9.25]


def test_patch_carries_the_post_write_revision_not_the_pre_write_one(
    seeded_office, set_delta_patches
):
    """``revision`` is the ENTITY watermark. Off by one and every subsequent
    ``expect_revision`` a client derives from a folded core is stale."""

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))
    stored = seeded_office.get_actor(WORKSPACE, "personainst_qa_agent_0001")
    assert stored.revision == 3  # seeded at 2

    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert patch["changed"]["revision"] == 3


def test_each_actor_patch_carries_its_own_revision(seeded_office, set_delta_patches):
    """Two actors, two DIFFERENT revisions, in one drain.

    This is the anti-hoist assertion: a producer that computed the revision once
    (or read it off the wrong actor) passes every single-actor test and dies
    here.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.0, y=1.0))  # → 3
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("dev", x=2.0, y=2.0))  # → 2

    patches = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ]
    by_id = {p["id"]: p["changed"]["revision"] for p in patches}
    assert by_id == {
        f"{WORKSPACE}/personainst_qa_agent_0001": 3,
        f"{WORKSPACE}/personainst_dev_agent_0001": 2,
    }


def test_office_actor_patch_is_small_against_the_office_row(seeded_office, set_delta_patches):
    """The measurement the granularity decision was made on, pinned.

    Surface granularity (``changed`` = the whole office row) would put the
    ``actors`` list in one value against a 3584-byte per-value budget; the live
    canvas already spends 2845 of it on four actors. Actor granularity spends
    ~700 regardless of how many desks the office holds. If this ratio ever
    collapses, the entity choice needs re-deciding — it does not silently
    degrade, it silently stops being worth doing.
    """

    from agent_runtime.snapshot import office_summary_row

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))
    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]

    def size(value) -> int:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))

    surface = seeded_office.get_surface(WORKSPACE)
    whole_office_row = office_summary_row(
        surface, seeded_office.list_actors(WORKSPACE), actors_unreadable=0
    )

    assert size(patch) < EVENT_PAYLOAD_LIMIT_BYTES
    # The actor row is a fraction of the office row it sits inside, at TWO
    # actors — and the office row grows with every desk while this does not.
    assert size(patch["changed"]) * 2 < size(whole_office_row)


# --------------------------------------------------------------------------- #
# Fidelity: the fold and a full rebuild must not disagree
# --------------------------------------------------------------------------- #
def test_changed_is_byte_equal_to_the_snapshot_rebuild_row(seeded_office, set_delta_patches):
    """``changed`` IS the row a full ``build_snapshot()`` produces.

    The launcher replaces the actor row with ``changed`` verbatim, so any field
    the snapshot carries and the patch omits is a field that goes stale for the
    rest of the session — and any field the patch ADDS is a row shape the full
    rebuild does not have, which is the ``attached_task_id`` failure the
    persona-instance lane already paid for once.
    """

    from agent_runtime.snapshot import build_snapshot

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))
    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]

    core = build_snapshot()
    rebuilt = next(
        a
        for a in core["offices"][WORKSPACE]["actors"]
        if a["actor_key"] == "personainst_qa_agent_0001"
    )
    assert json.dumps(patch["changed"], sort_keys=True, default=str) == json.dumps(
        rebuilt, sort_keys=True, default=str
    )


def test_unpublished_is_recomputed_and_omitted_without_a_realm(
    seeded_office, set_delta_patches, monkeypatch
):
    """``unpublished`` is the one derived field the row builder does not compute.

    It is also the one a drag flips: the content hash changes, so a realm-bound
    actor goes False→True on the same write. Two facts are pinned here because
    they are different facts and the launcher renders them differently: a
    workspace with no realm OMITS the key (not ``False``), and a realm-bound one
    carries a real boolean recomputed against the baseline.
    """

    set_delta_patches(True)

    # No realm behind the workspace (nothing was created) → key absent.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))
    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert "unpublished" not in patch["changed"]

    # A realm-bound workspace whose baseline does not know this actor → True.
    monkeypatch.setattr(
        sp, "_office_actor_unpublished", lambda actor: True, raising=True
    )
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=2.5, y=9.25))
    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert patch["changed"]["unpublished"] is True


def test_unpublished_derivation_reads_the_real_baseline(seeded_office, isolate_agent_runtime_root):
    """The derivation itself, against real stores — not the monkeypatched shim.

    ``unpublished`` never crosses the wire in the no-realm fixture above, so
    without this the whole realm branch is unexercised and a derivation that
    always returned ``None`` would stay green.
    """

    from agent_runtime.office_models import office_content_hash
    from agent_runtime.office_sync import write_office_baseline
    from agent_runtime.store import RealmStore, WorkspaceStore

    realm = RealmStore().create(name="patch-realm")
    workspace = WorkspaceStore().create(name="patch-ws", realm_id=realm.id)
    store = OfficeStore()
    store.ensure_surface(workspace.id, created_by="seed")
    store.upsert_actor(workspace.id, _actor_payload("qa", x=0.0, y=0.0))
    actor = store.get_actor(workspace.id, "personainst_qa_agent_0001")

    # Nothing published yet → unpublished.
    assert sp._office_actor_unpublished(actor) is True
    # Publish exactly this content → published.
    write_office_baseline(
        realm.id, {f"{workspace.id}:actor:{actor.actor_key}": office_content_hash(actor)}
    )
    assert sp._office_actor_unpublished(actor) is False
    # A drag changes the content hash → unpublished again, which is the flip the
    # patch must carry.
    store.upsert_actor(workspace.id, _actor_payload("qa", x=9.0, y=9.0))
    assert sp._office_actor_unpublished(store.get_actor(workspace.id, "personainst_qa_agent_0001")) is True


# --------------------------------------------------------------------------- #
# The surface-row fence
# --------------------------------------------------------------------------- #
def test_create_emits_a_complete_row_upsert_stamped_created(
    seeded_office, set_delta_patches
):
    """SPEC INVERSION (office fold-promotion plan §V2/§V3, 2026-08-16).

    This test asserted ``op: refresh`` until 2026-08-16, on the reasoning that a
    create bumps the office row's ``actor_count``/``actors_truncated`` and an
    actor-row patch cannot express the parent row. That reasoning was correct
    FOR THE WIRE AS IT STOOD; what changed is the derivability analysis, not the
    honesty rule. §V1's field-by-field audit established that both of those
    fields are DERIVED at projection (``len(actors)`` and ``max(0, n-200)``), so
    a client that folds the row can recompute them — the same rule upstream's
    project tree uses for derived container fields, which are never wire-synced.

    So the create ships a real ``upsert``, complete-row by construction, stamped
    ``created: true``. The stamp is not for the fold (which inserts on absent
    regardless) — it is the input to the capability gate, which is what keeps an
    un-updated client from being promoted a row it cannot fold. The gate has its
    own paired test below; without it this change would be the regression §V4
    names.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=5.0, y=5.0))

    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert patch["op"] == PATCH_OP_UPSERT, patch
    assert patch["id"] == f"{WORKSPACE}/personainst_backend_dev_agent_0001"
    assert patch["created"] is True, patch
    # COMPLETE-row, which is what makes insert-on-absent safe: a client that
    # holds no such actor can materialize it from ``changed`` alone.
    from agent_runtime.snapshot import build_snapshot

    rebuilt = next(
        a
        for a in build_snapshot()["offices"][WORKSPACE]["actors"]
        if a["actor_key"] == "personainst_backend_dev_agent_0001"
    )
    assert json.dumps(patch["changed"], sort_keys=True, default=str) == json.dumps(
        rebuilt, sort_keys=True, default=str
    )
    # Anti-vacuity for the stamp: a plain DRAG of the same actor carries no
    # ``created`` key at all, so a producer that stamped unconditionally (and
    # thereby gated every drag behind the token) dies here.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=6.0, y=6.0))
    drag = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert drag["op"] == PATCH_OP_UPSERT
    assert "created" not in drag, drag


def test_readd_of_an_archived_key_is_a_created_upsert(seeded_office, set_delta_patches):
    """SPEC INVERSION (same plan, §V1's ``archived_actor_keys`` row).

    Asserted ``refresh`` until 2026-08-16 because a re-add clears the
    resurrection ledger and rewrites the surface's ``updated_at``. §V1 resolved
    both: the ledger's delta under the two lifecycle ops is EXACTLY determined
    (archive appends the key, re-add removes it), so the client mirrors it
    during the fold; the surface ``updated_at`` has no launcher reader at all and
    its drift is accepted and documented until the next full core.

    ``created`` is TRUE here even though the key is one the client has seen
    before, and that is the point: ``created`` asks whether the ROW is absent
    from the client's list, which is the only question insert-on-absent needs
    answered. ``remove_actor`` unlinked the live file, so it is.
    """

    set_delta_patches(True)
    seeded_office.remove_actor(WORKSPACE, "personainst_qa_agent_0001")
    assert "personainst_qa_agent_0001" in seeded_office.get_surface(WORKSPACE).archived_actor_keys

    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=4.0, y=4.0))
    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert patch["op"] == PATCH_OP_UPSERT, patch
    assert patch["created"] is True, patch
    # And the ledger really did move — otherwise this test passes for the wrong
    # reason (it would just be re-testing the plain create branch).
    assert "personainst_qa_agent_0001" not in seeded_office.get_surface(WORKSPACE).archived_actor_keys


def test_a_live_actor_still_in_the_ledger_is_a_plain_upsert_not_a_created_one(
    seeded_office, set_delta_patches
):
    """SPEC INVERSION, and the one with a residual worth stating.

    This asserted ``refresh`` for the ``surface_rewritten`` arm — a live actor
    whose key the local surface still lists as archived, reachable only through
    the realm-sync pull applier's edit-vs-remove convergence (constructed
    directly here, because the store's own verbs cannot produce it). That arm is
    GONE: ``_emit_actor_patch`` now decides on row absence alone, exactly as the
    plan specifies (``created = (existing is None)``), so this write is a plain
    move upsert.

    **The residual, named rather than discovered later.** A plain upsert is NOT
    gated by ``office_actor_lifecycle``, so this write is promoted at an
    un-updated launcher — which replaces the actor row and does not touch its
    mirrored ``archived_actor_keys``. That client therefore keeps a ledger entry
    the server just cleared, until its next full core. It is the same accepted
    class as the surface ``updated_at`` drift (§V1): ``archived_actor_keys`` is
    parsed by the launcher's snapshot model and read by NO widget — its
    consumers are server-side guards. No cost regression (the client pays one
    small patch where it used to pay an 822 KB core, and takes no re-hydrate),
    and a NEW launcher clears the mirror on any upsert per O-L1, so the drift
    exists only for the old-client × sync-convergence pair.

    The mutation that this test still kills: making the emitter stamp
    ``created`` off the LEDGER rather than off row absence — which would gate a
    write whose row the client already holds, and would make ``created`` mean
    two different things on the wire the launcher is being built against.
    """

    set_delta_patches(True)
    actor_key = "personainst_qa_agent_0001"
    surface = seeded_office.get_surface(WORKSPACE)
    assert seeded_office.actor_exists(WORKSPACE, actor_key), "the actor must stay LIVE"
    surface.archived_actor_keys = [*surface.archived_actor_keys, actor_key]

    from agent_runtime.office_store import _write_surface

    _write_surface(surface)

    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=4.0, y=4.0))
    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert patch["op"] == PATCH_OP_UPSERT, patch
    assert "created" not in patch, patch
    # The state really was the ledger arm: the actor existed AND the surface was
    # rewritten by this write.
    assert actor_key not in seeded_office.get_surface(WORKSPACE).archived_actor_keys


def test_a_create_batch_is_not_coverable_for_a_legacy_declaration_but_a_drag_is(
    seeded_office, set_delta_patches
):
    """Anti-vacuity: the demotion is paired with the SAME batch promoting.

    A demotion test that passes because nothing was promotable proves nothing,
    so both halves run against a declaration that names ``office_actor``.

    What changed on 2026-08-16 is WHY the create half demotes. It used to be the
    op — a create emitted ``refresh``, which is not foldable. It is now the
    CAPABILITY GATE: the create emits a real ``upsert``, and this declaration
    (``office_actor`` without ``office_actor_lifecycle``) is exactly a fielded
    launcher's, which must keep getting the full core it gets today. The drag
    half is unchanged in both spelling and reason.
    """

    declared = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}
    set_delta_patches(True)

    # A create → created-upsert → NOT coverable for a client without the token.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=5.0, y=5.0))
    create_batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert not batch_is_patch_coverable(create_batch, fold_entities=declared)

    # A drag of the SAME actor → plain upsert → coverable, token or not.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=6.0, y=6.0))
    drag_batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert batch_is_patch_coverable(drag_batch, fold_entities=declared)


def test_restore_stays_uncovered_and_remove_no_longer_does(
    seeded_office, set_delta_patches
):
    """SPEC SPLIT (office fold-promotion plan O-H3, 2026-08-16).

    This asserted that BOTH ``office.actor.removed`` and ``.restored`` stay out
    of the covered set. Half of that survives and half of it was the point of
    the whole workstream, so the test splits rather than being deleted or
    quietly relaxed — the surviving half is what stops the widening from
    sliding one event further than it was argued for.

    ``office.actor.removed`` is covered: ``_archive_actor_locked`` now emits the
    paired ``office_actor`` remove, and the surface state the archive moves
    (``archived_actor_keys``, the derived counts) is exactly reproducible by a
    client folding that row.

    ``office.actor.restored`` stays uncovered, and the reason is specific rather
    than residual caution: a restore un-archives a row from a COPY the client
    never held, so there is nothing on the wire for it to insert. It rides the
    full core.

    SECOND SPLIT (office write-verbs plan WV-H3, 2026-08-16). This also
    asserted ``office.surface.updated`` stays out, on the reasoning that no
    ACTOR-row patch derives ``folders`` or the surface ``revision``. That is
    still true and is no longer the question: ``update_surface`` now emits an
    ``office_surface`` row that carries both. So the assertion moves to the two
    that remain — ``.restored`` and ``.conflict_resolved`` — plus
    ``office.surface.created``, which stays uncovered because a create authors a
    surface the client has never held. The token gate on the surviving
    surface event is pinned in its own tests below.
    """

    declared = (
        HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY, OFFICE_ACTOR_LIFECYCLE_CAPABILITY}
    )
    set_delta_patches(True)

    before = _log_end()
    seeded_office.remove_actor(WORKSPACE, "personainst_qa_agent_0001")
    assert batch_is_patch_coverable(
        [e for _, e in EventLog().iter_from_offset(before)], fold_entities=declared
    )

    before = _log_end()
    seeded_office.restore_actor(WORKSPACE, "personainst_qa_agent_0001")
    assert not batch_is_patch_coverable(
        [e for _, e in EventLog().iter_from_offset(before)], fold_entities=declared
    )

    assert "office.actor.removed" in LIVE_COVERED_DOMAIN_EVENT_TYPES
    assert "office.actor.restored" not in LIVE_COVERED_DOMAIN_EVENT_TYPES
    assert "office.actor.conflict_resolved" not in LIVE_COVERED_DOMAIN_EVENT_TYPES
    assert "office.surface.created" not in LIVE_COVERED_DOMAIN_EVENT_TYPES


# --------------------------------------------------------------------------- #
# O-H1: the lifecycle producer (archive remove, truncation guard, the gate)
# --------------------------------------------------------------------------- #
def test_archive_emits_a_remove_patch_inside_the_lock_before_the_domain_event(
    seeded_office, set_delta_patches
):
    """The archive half of the lifecycle pair.

    Before 2026-08-16 an archive emitted NO patch at all — only the domain
    event — which is why ``office.actor.removed`` could not be covered without
    shipping promoted frames whose office rows never arrive.

    Two facts, and the ORDER is one of them. The patch must precede its domain
    event in the log because that is the ordering ``upsert_actor`` already uses
    and the one the ride-along rule assumes; more importantly both must land
    INSIDE ``office_lock``, which is what makes EventLog order agree with
    revision order (see ``_emit_actor_patch``'s docstring). Emitting after the
    lock releases is the inversion
    ``test_concurrent_writers_never_invert_revision_against_offset`` exists to
    catch, and this test pins the placement that prevents it.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.remove_actor(WORKSPACE, "personainst_qa_agent_0001")

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    types = [e.type for e in batch]
    assert types == [STATE_PATCHED_EVENT_TYPE, "office.actor.removed"], types
    patch = batch[0].payload
    assert patch == {
        "entity": OFFICE_ACTOR_ENTITY,
        "id": f"{WORKSPACE}/personainst_qa_agent_0001",
        "op": "remove",
    }
    # A remove carries no ``changed`` by contract, so it can never reach the
    # oversize ladder — asserted as the shape above rather than as prose.
    # The OTHER actor is untouched: a hoisted key would name it here.
    assert seeded_office.actor_exists(WORKSPACE, "personainst_dev_agent_0001")


def test_a_conflict_resolution_archive_still_emits_the_remove_even_with_emit_false(
    seeded_office, set_delta_patches
):
    """``resolve_conflict``'s edit-vs-remove branch suppresses the DOMAIN event
    (``emit=False``) but the row really did leave the office.

    A client told nothing would render a desk the store no longer has, for the
    rest of its session. That batch demotes anyway on its own uncovered
    ``office.actor.conflict_resolved``, so the patch costs nothing here and is
    honest everywhere — which is why the emit sits in ``_archive_actor_locked``
    rather than beside the domain event above it.
    """

    import json as _json

    from agent_runtime import paths

    set_delta_patches(True)
    actor_key = "personainst_qa_agent_0001"
    sidecar = paths.office_conflict_path(WORKSPACE, actor_key)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    # A remote REMOVAL tombstone: no ``remote_actor``, so the local copy is
    # archived rather than replaced.
    sidecar.write_text(_json.dumps({"actor_key": actor_key}), encoding="utf-8")

    before = _log_end()
    seeded_office.resolve_conflict(WORKSPACE, actor_key, take="remote")

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    removes = [
        e.payload
        for e in batch
        if e.type == STATE_PATCHED_EVENT_TYPE and e.payload.get("op") == "remove"
    ]
    assert removes == [
        {"entity": OFFICE_ACTOR_ENTITY, "id": f"{WORKSPACE}/{actor_key}", "op": "remove"}
    ], batch
    # The domain event really was suppressed — otherwise this passes for the
    # wrong reason (it would just be re-testing the plain archive path).
    assert "office.actor.removed" not in [e.type for e in batch]
    assert not seeded_office.actor_exists(WORKSPACE, actor_key)


def test_a_workspace_over_the_projection_cap_keeps_the_honest_refresh(
    seeded_office, set_delta_patches
):
    """The ONE meaning ``refresh`` keeps: the projected actor list is a CUT.

    Past ``MAX_OFFICE_ACTORS_PROJECTED`` the snapshot ships only the first N
    actors, and which ones survive is not client-decidable — so neither the
    row's presence nor the derived ``actor_count``/``actors_truncated`` can be
    folded. This is a genuine "not expressible per-row" case, unlike the two the
    2026-08-16 plan retired, and it degrades to the full core exactly as before.

    The cap is monkeypatched rather than seeded with 201 actors: the guard is
    the comparison, and 201 real store writes would spend the whole per-test
    budget proving a constant.

    THE CAP PATCH IS SCOPED (EG-0.1). This test used to drop it with
    ``monkeypatch.undo()``, which unwinds the SHARED per-test instance — so the
    package's ``isolate_agent_runtime_root`` pins went with it and the second
    half of this test wrote the operator's live store (``ws_office_patch_test``
    at revision 67 in ``X:/Eternia/.hermes``). A scoped context drops the cap
    and nothing else; ``set_delta_patches``'s pin now survives the block, so it
    is not re-applied below.
    """

    set_delta_patches(True)
    # Two live actors from the fixture; a cap of 1 puts the store over it.
    with pytest.MonkeyPatch.context() as capped:
        capped.setattr(
            "agent_runtime.snapshot.MAX_OFFICE_ACTORS_PROJECTED", 1, raising=True
        )

        before = _log_end()
        seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=4.0, y=4.0))
        patch = [
            e.payload
            for _, e in EventLog().iter_from_offset(before)
            if e.type == STATE_PATCHED_EVENT_TYPE
        ][0]
        assert patch["op"] == PATCH_OP_REFRESH, patch
        assert "changed" not in patch
        assert "created" not in patch

    # Anti-vacuity: under the real cap the SAME write is a foldable upsert, so
    # this cannot be green because the lane went dark.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=5.0, y=5.0))
    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert patch["op"] == PATCH_OP_UPSERT, patch


def test_an_undeclared_client_is_never_promoted_a_lifecycle_row_and_a_declared_one_is(
    seeded_office, set_delta_patches
):
    """THE anti-vacuity check for the whole O-H1 stage (plan §V4).

    The producer half of this stage widens the OPS of an entity every fielded
    launcher already declares. Without the capability gate those widened rows
    would be PROMOTED at clients whose fold answers a create with
    ``patch_without_target`` and a remove with ``patch_unsupported_op`` — each a
    full re-hydrate, so the client pays the patch AND the core: strictly worse
    than the full core it replaced.

    Both directions, on the SAME batches, because a gate test that only shows
    the refusal is green against a gate that refuses everything.
    """

    legacy = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}
    widened = legacy | {OFFICE_ACTOR_LIFECYCLE_CAPABILITY}
    set_delta_patches(True)

    # CREATE: the ``created: true`` upsert.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=5.0, y=5.0))
    create = [
        e
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ]
    assert len(create) == 1
    assert not event_is_patch_coverable(create[0], fold_entities=legacy)
    assert event_is_patch_coverable(create[0], fold_entities=widened)

    # ARCHIVE: the ``remove``.
    before = _log_end()
    seeded_office.remove_actor(WORKSPACE, "personainst_backend_dev_agent_0001")
    remove = [
        e
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ]
    assert len(remove) == 1
    assert not event_is_patch_coverable(remove[0], fold_entities=legacy)
    assert event_is_patch_coverable(remove[0], fold_entities=widened)

    # A plain MOVE is coverable under the bare entity, token or not — the gate
    # is on the two widened ops, never on the entity. Without this the gate
    # could be "refuse every office_actor unless the token is present", which
    # would silently retire the drag promotion that already ships.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=6.0, y=6.0))
    drag = [
        e
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ]
    assert len(drag) == 1
    assert event_is_patch_coverable(drag[0], fold_entities=legacy)
    assert event_is_patch_coverable(drag[0], fold_entities=widened)


def test_the_capability_token_is_inert_as_an_entity_name(seeded_office, set_delta_patches):
    """The token rides the ENTITY declaration channel and must not act like one.

    ``normalize_fold_entities`` interprets no string, so the token is just a set
    member — which is exactly what makes an OLD runtime ignore it (V4's
    ``old runtime + new launcher`` cell: today's wire, byte-identical). The
    property that has to hold HERE is the mirror: declaring the token must not
    make some other entity foldable, and must not be required for the entities
    that were already declared.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.0, y=1.0))
    drag = [
        e
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]

    # The token ALONE (no ``office_actor``) does not promote an office row.
    assert not event_is_patch_coverable(
        drag, fold_entities=HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_LIFECYCLE_CAPABILITY}
    )
    # And it is not an entity anyone emits under.
    assert OFFICE_ACTOR_LIFECYCLE_CAPABILITY != OFFICE_ACTOR_ENTITY


# --------------------------------------------------------------------------- #
# O-H3, THE MILESTONE: a real operator gesture ships as a patch frame
# --------------------------------------------------------------------------- #
#: The declaration a launcher carrying O-L1..O-L3 sends. Spelled out rather than
#: composed from production constants: this is the wire contract the launcher is
#: being built against in the other repo, and an assertion written as ``x == x``
#: cannot catch either side renaming half of it.
_WIDENED_DECLARATION = frozenset(
    {
        "persona_instance",
        "incident",
        "office_actor",
        "office_actor_lifecycle",
        # D3 (2026-08-16): the SECOND capability token — the complete-row
        # ``persona_instance`` create-upsert that replaced ``open_chat``'s
        # ``refresh``. Same reason as its sibling: a widened OP on an entity
        # every fielded launcher already declares.
        "persona_instance_create",
    }
)
#: What every launcher in the field sends today.
_FIELDED_DECLARATION = frozenset({"persona_instance", "incident", "office_actor"})


def test_the_widened_declaration_is_exactly_the_launchers(
):
    """The cross-repo spelling, pinned on the hermes side.

    O-L1 puts the token behind a single Dart constant precisely so the two lanes
    cannot drift; this is the same fence one repo over. A misspelling here is
    indistinguishable at runtime from a client that never declared — gestures
    silently keep demoting, which reads as "the feature does not work" with
    nothing in any log to say why.
    """

    assert OFFICE_ACTOR_LIFECYCLE_CAPABILITY == "office_actor_lifecycle"
    assert OFFICE_ACTOR_LIFECYCLE_CAPABILITY in _WIDENED_DECLARATION
    assert PERSONA_INSTANCE_CREATE_CAPABILITY == "persona_instance_create"
    assert PERSONA_INSTANCE_CREATE_CAPABILITY in _WIDENED_DECLARATION
    assert _FIELDED_DECLARATION < _WIDENED_DECLARATION


def test_a_delete_gesture_batch_promotes_for_a_lifecycle_declared_client(
    seeded_office, set_delta_patches
):
    """THE MILESTONE, delete half.

    The live-decoded batch, reproduced through the real chokepoints:
    ``persona_instance.retired`` → the persona ``remove`` → the office
    ``remove`` → ``office.actor.removed``. Every one of those four was, until
    this stage, either uncovered or unpaired, so the whole batch demoted — and
    each demotion cost TWO ~822 KB core builds, one per producer, plus a
    re-subscribe whose hydrate the office sink then discarded at its own
    baseline gate.

    Both directions are asserted on the SAME batch. The demotion half is not
    politeness: it is the mixed-pair guarantee (§V4) — a fielded launcher must
    keep getting exactly today's wire — and it is also the anti-vacuity check,
    because a coverage test that only shows promotion is green against a
    classifier that promotes everything.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore

    set_delta_patches(True)
    store = PersonaInstanceStore()
    # A PLACEMENT-backed instance, which is what the gesture actually deletes:
    # ``retire`` refuses the canonical persona channel by design (the queued
    # global-singleton redesign), so a canonical row could never reproduce this
    # batch at all.
    instance = store.add_instance(
        persona_id="qa", placement_id="scene_child_2", display_name="QA Agent (2)"
    )
    # Bind the placement to the instance so the retire's office fan-out finds it.
    seeded_office.upsert_actor(
        WORKSPACE,
        _actor_payload("qa", x=1.0, y=1.0, instance=instance.id),
    )

    before = _log_end()
    store.retire(instance.id, reason="placement removed from Mission Office")
    batch = [e for _, e in EventLog().iter_from_offset(before)]

    types = [e.type for e in batch]
    assert "persona_instance.retired" in types, types
    assert "office.actor.removed" in types, types
    assert batch_is_patch_coverable(batch, fold_entities=_WIDENED_DECLARATION), types
    # A fielded launcher keeps today's wire: the office remove is gated.
    assert not batch_is_patch_coverable(batch, fold_entities=_FIELDED_DECLARATION)

    # And the frame the widened client gets carries BOTH rows — the persona
    # departure and the desk departure — at one watermark. That pairing is what
    # O-H2's complete-batch forwarding exists to keep intact across the two
    # transports.
    rows = [
        e.payload
        for e in batch
        if e.type == STATE_PATCHED_EVENT_TYPE
    ]
    assert [(r["entity"], r["op"]) for r in rows] == [
        ("persona_instance", "remove"),
        (OFFICE_ACTOR_ENTITY, "remove"),
    ], rows


def test_an_add_gesture_create_and_reopen_batch_both_promote(
    seeded_office, set_delta_patches
):
    """THE MILESTONE, add half — with D3 the hole in it is closed.

    RE-OPEN promotes: ``chat_opened`` has its diffed persona ``upsert``, so the
    batch folds, and for a FIELDED client too (nothing in a re-open is a widened
    op).

    CREATE now promotes as well, for a client declaring
    ``persona_instance_create``. This test asserted the opposite until D3, on the
    stated grounds that "a brand-new roster row cannot be assumed to fit the 4 KB
    cap" — an assumption that predated R2's residue slimming and was measured
    false (worst live payload 3,133 of 4,096). SPEC INVERSION, flagged as such.

    Both directions on the SAME batch, because a promotion assertion alone is
    green against a classifier that promotes everything — and the demotion half
    is the mixed-pair guarantee: a fielded launcher must keep getting today's
    full core for a create, since its generic fold answers a create-upsert with
    ``patch_without_target`` and re-hydrates.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore

    set_delta_patches(True)
    store = PersonaInstanceStore()

    # CREATE: a complete-row upsert → promotes for a token-declaring client,
    # demotes for every launcher in the field.
    before = _log_end()
    created = store.open_chat(
        persona_id="profile:qa",
        session_id="persona_chat_add_gesture",
        display_name="QA Agent",
    )
    create_batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert "persona_instance.chat_opened" in [e.type for e in create_batch]
    assert batch_is_patch_coverable(create_batch, fold_entities=_WIDENED_DECLARATION)
    assert not batch_is_patch_coverable(create_batch, fold_entities=_FIELDED_DECLARATION)

    # RE-OPEN: a diffed upsert → promotes.
    before = _log_end()
    store.open_chat(
        persona_id="profile:qa",
        persona_instance_id=created.id,
        session_id="persona_chat_add_gesture_two",
        display_name="QA Agent",
        workspace_id=WORKSPACE,
    )
    reopen_batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert "persona_instance.chat_opened" in [e.type for e in reopen_batch]
    assert batch_is_patch_coverable(reopen_batch, fold_entities=_WIDENED_DECLARATION)
    # It promotes for a FIELDED client too: nothing in a re-open is a widened
    # office op, so no token is needed. That is the plan's "the office half of
    # an add can fold one stage earlier" property, stated as an assertion.
    assert batch_is_patch_coverable(reopen_batch, fold_entities=_FIELDED_DECLARATION)


def test_the_office_half_of_an_add_promotes_in_its_own_batch(
    seeded_office, set_delta_patches
):
    """The live-proven split, and why the add gesture is worth anything at all.

    The operator's own log shows ``chat_opened`` and the office write landing
    ~1s apart — outside the 200ms debounce — so the add gesture usually arrives
    as TWO batches, and the office one has no roster row in it to demote on.
    Asserted here as the batch a create's office half actually is.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=5.0, y=5.0))
    batch = [e for _, e in EventLog().iter_from_offset(before)]

    assert [e.type for e in batch] == [STATE_PATCHED_EVENT_TYPE, "office.actor.upserted"]
    assert batch_is_patch_coverable(batch, fold_entities=_WIDENED_DECLARATION)
    assert not batch_is_patch_coverable(batch, fold_entities=_FIELDED_DECLARATION)


def test_surface_creation_batch_is_uncovered(isolate_agent_runtime_root, set_delta_patches):
    """The very first actor in a brand-new office emits
    ``office.surface.created`` beside it — uncovered, so the launcher gets the
    full core that gives it an ``offices[ws]`` row to fold into later."""

    declared = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}
    set_delta_patches(True)
    store = OfficeStore()
    # The workspace RECORD, and only the record: the office must NOT exist,
    # because authoring it is the event this case is about. Since MC-8/P10 a
    # write cannot author an office for an id no record resolves, so the
    # record is now the precondition of the authoring path rather than
    # something the write invents on the way past.
    seed_workspace_record("ws_brand_new")
    before = _log_end()
    store.upsert_actor("ws_brand_new", _actor_payload("qa", x=0.0, y=0.0))
    batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert "office.surface.created" in [e.type for e in batch]
    assert not batch_is_patch_coverable(batch, fold_entities=declared)


# --------------------------------------------------------------------------- #
# Negotiation: an un-updated client must get a FULL CORE, never an office patch
# --------------------------------------------------------------------------- #
def _drag_frames(fold_entities, store):
    """Run the REAL stream generator across a REAL drag and return its frames.

    Not the classifier — the generator. The classifier agreeing is necessary but
    not sufficient: the promotion decision is made in ``stream_frames``, and the
    thing an un-updated launcher actually receives is a FRAME.
    """

    import threading
    import time

    from agent_runtime import stream as st

    gen = st.stream_frames(
        fold_entities=fold_entities,
        poll_interval_seconds=0.05,
        delta_debounce_seconds=0.05,
        heartbeat_interval_seconds=60,
        max_frames=4,
    )
    hydrate = next(gen)

    def _drag():
        time.sleep(0.3)
        OfficeStore().upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))

    thread = threading.Thread(target=_drag, daemon=True)
    thread.start()
    frames = [hydrate]
    for frame in gen:
        frames.append(frame)
        if frame.get("type") in {"patch", "delta"}:
            break
    thread.join(timeout=10)
    return frames


def test_an_undeclared_client_gets_a_full_core_not_an_unfoldable_patch(
    seeded_office, set_delta_patches
):
    """Design question 3, VERIFIED rather than trusted.

    ``fold_entities=None`` is what every launcher in the field sends — no build
    declares anything. The intersection rule says such a client must keep
    getting full cores; the failure it prevents is strictly worse than the
    status quo, because an unfoldable patch costs the client the patch AND the
    re-hydrate it triggers (``mission_read_model._applyOnePatch`` returns
    ``patch_unknown_entity:office_actor`` → ``needsResync`` → the bridge runs a
    fresh one-shot ``harness stream --max-frames 1``).

    Paired with the SAME drag promoting under a declaration that names
    ``office_actor``, so this cannot pass by nothing being promotable.
    """

    set_delta_patches(True)

    frames = _drag_frames(None, seeded_office)
    hydrate = frames[0]
    # The echo tells an un-updated client what it was actually honoured for.
    assert hydrate["fold_entities"] == sorted(HISTORICAL_FOLD_ENTITIES)
    assert OFFICE_ACTOR_ENTITY not in hydrate["fold_entities"]
    assert frames[-1]["type"] == "delta", frames[-1]["type"]
    # A full core, which is exactly the wire this client gets today.
    assert isinstance(frames[-1].get("core"), dict)


def test_the_same_drag_promotes_for_a_client_that_declares_office_actor(
    seeded_office, set_delta_patches
):
    """The anti-vacuity half of the test above, and the whole point of the leg."""

    set_delta_patches(True)
    declared = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}

    frames = _drag_frames(declared, seeded_office)
    assert frames[0]["fold_entities"] == sorted(declared)
    patch_frame = frames[-1]
    assert patch_frame["type"] == "patch", patch_frame["type"]
    # No core on this lane — that IS the saving.
    assert "core" not in patch_frame
    entry = patch_frame["patches"][0]
    assert entry["entity"] == OFFICE_ACTOR_ENTITY
    assert entry["op"] == PATCH_OP_UPSERT
    assert entry["id"] == f"{WORKSPACE}/personainst_qa_agent_0001"
    # The saving, stated as the invariant rather than as a ratio. An isolated
    # test root's core is ~8 KB (nearly-empty stores), so a ratio pinned here
    # would be pinning the FIXTURE's emptiness — the live core measured 842 KB
    # on 2026-08-14 and grows with every store. What is actually true on both is
    # that the patch is bounded by the payload cap and the core is not bounded
    # at all: the patch is O(one actor), the frame it replaces is O(everything).
    hydrate_core_bytes = len(
        json.dumps(frames[0]["core"], default=str).encode("utf-8")
    )
    patch_bytes = len(json.dumps(patch_frame, default=str).encode("utf-8"))
    assert patch_bytes < hydrate_core_bytes, (patch_bytes, hydrate_core_bytes)
    assert len(json.dumps(entry, default=str).encode("utf-8")) < EVENT_PAYLOAD_LIMIT_BYTES


def test_a_shared_producer_demotes_office_for_everyone_if_one_client_cannot_fold(
    seeded_office, set_delta_patches
):
    """The INTERSECTION rule, at the office entity.

    The socket lane runs ONE producer for N subscribers, so a room containing a
    single un-updated launcher must not be sent office patches — not even for
    the subscribers that declared them. Union here would aim the exact bug this
    negotiation exists to prevent at the wrong client.
    """

    from agent_runtime.patch_coverage import accepted_fold_entities

    set_delta_patches(True)
    updated = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}

    # A room of updated clients: office is accepted.
    assert OFFICE_ACTOR_ENTITY in accepted_fold_entities([updated, updated])
    # One un-updated client joins (declares nothing) → office is dropped for all.
    accepted = accepted_fold_entities([updated, None])
    assert OFFICE_ACTOR_ENTITY not in accepted
    assert accepted == HISTORICAL_FOLD_ENTITIES

    # And that narrowed set really demotes the drag batch.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))
    batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert not batch_is_patch_coverable(batch, fold_entities=accepted)
    assert batch_is_patch_coverable(batch, fold_entities=updated)


# --------------------------------------------------------------------------- #
# Monotonicity
# --------------------------------------------------------------------------- #
def test_concurrent_writers_never_invert_revision_against_offset(
    seeded_office, set_delta_patches
):
    """The early-warning property this whole pilot exists to test at 2 KB.

    Real threads, real ``office_lock``, one actor. The EventLog offset order and
    the ``revision`` order must agree: the fold applies patches in offset order,
    so an inversion leaves the launcher's core holding an OLDER revision than
    disk, with no gap for ``base_offset`` to catch and nothing that would ever
    correct it short of a full core.

    Emitting outside the lock (or re-reading the actor instead of using the
    written object) produces exactly that inversion under this load.
    """

    set_delta_patches(True)
    before = _log_end()
    barrier = threading.Barrier(4)
    errors: list[BaseException] = []

    def write(n: int) -> None:
        try:
            barrier.wait(timeout=10)
            OfficeStore().upsert_actor(WORKSPACE, _actor_payload("qa", x=float(n), y=0.0))
        except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors

    revisions = [
        e.payload["changed"]["revision"]
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
        and e.payload.get("op") == PATCH_OP_UPSERT
        and e.payload.get("entity") == OFFICE_ACTOR_ENTITY
    ]
    assert len(revisions) == 4, revisions
    # Strictly increasing in offset order — and each write took its own
    # revision, so a duplicate is a lost update, not just a reordering.
    assert revisions == sorted(revisions), revisions
    assert len(set(revisions)) == len(revisions), revisions
    assert revisions[-1] == seeded_office.get_actor(WORKSPACE, "personainst_qa_agent_0001").revision


# --------------------------------------------------------------------------- #
# Inertness
# --------------------------------------------------------------------------- #
def test_flag_off_emits_no_office_patch_and_leaves_the_event_stream_intact(
    seeded_office, set_delta_patches
):
    """Flag off → provably inert: no ``state.patched``, and the domain events an
    office write already emitted are byte-identical to before this lane."""

    set_delta_patches(False)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))

    # Scoped to THIS write's span: the fixture seeds under the shipped-ON
    # default, so the log already holds patches from the seeding.
    new = [e for _, e in EventLog().iter_from_offset(before)]
    assert [e.type for e in new] == ["office.actor.upserted"]
    assert not [e for e in new if e.type == STATE_PATCHED_EVENT_TYPE]


def test_flag_off_does_not_even_run_the_projection(
    seeded_office, set_delta_patches, monkeypatch
):
    """Inertness is COST as well as silence — and the cost half needs its own
    assertion.

    FOUND BY MUTATION. Deleting the flag check from ``emit_office_actor_patch``
    stayed GREEN, because ``emit_state_patch`` checks the flag again and no
    entry was appended either way. But the office projection is an ARGUMENT to
    that call, so it runs first: dropping the early return means every office
    write with the lane OFF pays a ``WorkspaceStore`` read plus a realm-baseline
    file read for a patch that is then discarded. The redundant-looking guard is
    the one that makes "provably inert" true about work, not just about output.
    """

    set_delta_patches(False)
    calls: list[object] = []

    def _tracked(actor):
        calls.append(actor)
        raise AssertionError("the projection must not run with the lane off")

    monkeypatch.setattr(sp, "project_office_actor_wire_row", _tracked)
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.5, y=9.25))
    assert calls == []


def test_a_patch_emit_failure_never_takes_the_office_write_down(
    seeded_office, set_delta_patches, monkeypatch
):
    """A patch-lane fault is a missing PROMOTION, not a failed drag.

    Without the guard the operator's canvas write raises and the desk snaps
    back — a read-model optimisation breaking the write it observes.
    """

    set_delta_patches(True)

    def _boom(*args, **kwargs):
        raise RuntimeError("projection exploded")

    monkeypatch.setattr(sp, "project_office_actor_wire_row", _boom)
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=7.5, y=7.5))
    # The write landed...
    stored = seeded_office.get_actor(WORKSPACE, "personainst_qa_agent_0001")
    assert stored.revision == 3
    agent_item = next(i for i in stored.items if i.kind == "agent")
    assert list(agent_item.position) == [7.5, 7.5]
    # ...and the domain event still rides, so the batch falls back to a full core.
    assert "office.actor.upserted" in [e.type for e in _drain()]


# --------------------------------------------------------------------------- #
# WV-H3: the SURFACE producer, its token gate, and the two writes it refuses
# --------------------------------------------------------------------------- #
def _surface_patches(events) -> list[dict]:
    return [
        e.payload
        for e in events
        if e.type == STATE_PATCHED_EVENT_TYPE
        and e.payload.get("entity") == OFFICE_SURFACE_ENTITY
    ]


def test_a_folder_write_emits_a_subset_patch_before_its_domain_event(
    seeded_office, set_delta_patches
):
    """The producer, and the ORDER, and the SUBSET — three facts in one batch.

    ``changed`` is asserted as a whole dict rather than key-by-key, because the
    thing that would be wrong is an EXTRA key: the office row also carries
    ``actors``, the derived counts and both key ledgers, all owned by the actor
    folds and by client-side derivation, and a producer that shipped any of them
    here would have two writers for one field with no receipt anywhere.

    The order assertion (patch strictly before the domain event) is the
    ride-along rule the coverage classifier assumes, and the placement inside
    ``office_lock`` is what makes EventLog order agree with revision order — see
    ``_emit_surface_patch``'s docstring for why appending after the lock is the
    inversion nothing downstream would notice.
    """

    set_delta_patches(True)
    before = _log_end()
    surface = seeded_office.update_surface(WORKSPACE, folders=["Ops", "Design"])

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    types = [e.type for e in batch]
    assert types == [STATE_PATCHED_EVENT_TYPE, "office.surface.updated"], types

    payload = _surface_patches(batch)[0]
    assert payload["entity"] == OFFICE_SURFACE_ENTITY
    assert payload["id"] == WORKSPACE
    assert payload["op"] == PATCH_OP_UPSERT
    assert payload["changed"] == {
        "folders": ["Agents", "Desks", "Ops", "Design"],
        "revision": surface.revision,
        "updated_at": to_jsonable(surface.updated_at),
    }
    # …and the folder list is the STORE's normalization, not the caller's input.
    assert payload["changed"]["folders"] == list(
        seeded_office.get_surface(WORKSPACE).folders
    )


def test_the_surface_patch_carries_exactly_what_a_full_rebuild_would(
    seeded_office, set_delta_patches
):
    """FIDELITY, against the snapshot's own row builder rather than a literal.

    The fold merges these three keys onto the office row the client holds, and a
    rebuild recomputes the whole row. If the patch's values ever disagreed with
    ``office_summary_row``'s, a folded core and a rebuilt one would render
    different folders for the same surface and only one of them would be right.
    """

    from agent_runtime.snapshot import office_summary_row

    set_delta_patches(True)
    before = _log_end()
    seeded_office.update_surface(WORKSPACE, folders=["Ops"])

    payload = _surface_patches([e for _, e in EventLog().iter_from_offset(before)])[0]
    rebuilt = office_summary_row(
        seeded_office.get_surface(WORKSPACE),
        seeded_office.list_actors(WORKSPACE),
        actors_unreadable=0,
    )
    for field in OFFICE_SURFACE_PATCH_FIELDS:
        assert payload["changed"][field] == rebuilt[field], field


def test_a_folder_write_that_AUTHORED_the_office_emits_no_patch(
    isolate_agent_runtime_root, set_delta_patches
):
    """Creates stay full-core, and this is the arm that decides it.

    ``update_surface`` calls ``ensure_surface``, so on an unauthored workspace it
    is a CREATE wearing an update's name — and the patch is a three-field
    subset, which a client that has never held this workspace answers with
    ``patch_without_target`` and a re-hydrate: the patch AND the core, strictly
    worse than the core alone. Not hypothetical: ``workspace_template`` clones an
    office by calling ``ensure_surface`` and then this, and a template clone is
    exactly the case where no client holds the row.

    The witness is the patch's ABSENCE beside a domain event that did fire, so a
    producer that emitted unconditionally cannot pass by also writing whatever
    is probed.
    """

    set_delta_patches(True)
    store = OfficeStore()
    # Record only -- the office is still unauthored, which is the whole
    # subject. See the sibling case above for why the record is needed now.
    seed_workspace_record("ws_never_authored")
    before = _log_end()
    store.update_surface("ws_never_authored", folders=["Ops"])

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    types = [e.type for e in batch]
    assert "office.surface.created" in types, types
    assert "office.surface.updated" in types, types
    assert _surface_patches(batch) == [], "a create must not ship a subset merge"


def test_a_dry_run_folder_write_emits_nothing_at_all(
    seeded_office, set_delta_patches
):
    """The preview path writes no file, so it must log no row either.

    A patch for a revision the store does not hold is the one shape the fold
    cannot recover from on its own: the client would move to a revision disk
    never reached and stay there until a full core corrected it.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.update_surface(WORKSPACE, folders=["Ops"], dry_run=True)

    assert [e for _, e in EventLog().iter_from_offset(before)] == []


def test_an_actor_write_emits_no_surface_patch(seeded_office, set_delta_patches):
    """The other half of the ownership split, from the actor side.

    A drag moves no folder and no surface revision, so a surface patch beside it
    would be a second writer for state nothing changed — and it would drag
    ``updated_at`` with it, which the actor fold documents as accepted drift
    precisely because no actor write moves the surface's copy.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("qa", x=1.0, y=1.0))

    assert _surface_patches([e for _, e in EventLog().iter_from_offset(before)]) == []


def test_the_flag_off_emits_no_surface_patch(seeded_office, set_delta_patches):
    """``read_model.delta_patches`` off is provably inert on this leg too."""

    set_delta_patches(False)
    before = _log_end()
    seeded_office.update_surface(WORKSPACE, folders=["Ops"])

    batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert [e.type for e in batch] == ["office.surface.updated"]


def test_a_folder_batch_promotes_for_a_declared_client_and_demotes_for_a_legacy_one(
    seeded_office, set_delta_patches
):
    """THE PAIRED GATE CHECK, both directions on the SAME batch.

    A coverage assertion that only shows promotion is green against a classifier
    that promotes everything, so both readings of one batch are asserted here.

    The legacy declaration is the widest one any fielded launcher sends —
    ``office_actor`` plus BOTH existing tokens — and it still demotes, which is
    what makes the third token the thing that changed rather than "the office
    got covered".
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.update_surface(WORKSPACE, folders=["Ops"])
    batch = [e for _, e in EventLog().iter_from_offset(before)]

    fielded = HISTORICAL_FOLD_ENTITIES | {
        OFFICE_ACTOR_ENTITY,
        OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
        PERSONA_INSTANCE_CREATE_CAPABILITY,
    }
    assert not batch_is_patch_coverable(batch, fold_entities=fielded)

    declared = fielded | {OFFICE_SURFACE_ENTITY, OFFICE_SURFACE_FOLD_CAPABILITY}
    assert batch_is_patch_coverable(batch, fold_entities=declared)


def test_the_entity_alone_does_not_promote_the_batch(
    seeded_office, set_delta_patches
):
    """The token gates BOTH halves, and the domain event is why it has to.

    A covered domain event is not entity-gated anywhere else in this classifier
    — it carries no fold state, so the paired patch's gate is the one that
    matters. That reasoning breaks for a NEW entity: without the token,
    declaring ``office_surface`` alone would leave ``office.surface.updated``
    coverable for every client that has not, and a batch of just those two would
    ship a patch frame whose only row they answer with a re-hydrate.

    Asserted per EVENT rather than per batch, so the two halves are named
    separately instead of collapsing into one boolean.
    """

    set_delta_patches(True)
    before = _log_end()
    seeded_office.update_surface(WORKSPACE, folders=["Ops"])
    batch = [e for _, e in EventLog().iter_from_offset(before)]

    entity_only = HISTORICAL_FOLD_ENTITIES | {OFFICE_SURFACE_ENTITY}
    patch_event = next(e for e in batch if e.type == STATE_PATCHED_EVENT_TYPE)
    domain_event = next(e for e in batch if e.type == "office.surface.updated")

    assert not event_is_patch_coverable(patch_event, fold_entities=entity_only)
    assert not event_is_patch_coverable(domain_event, fold_entities=entity_only)

    with_token = entity_only | {OFFICE_SURFACE_FOLD_CAPABILITY}
    assert event_is_patch_coverable(patch_event, fold_entities=with_token)
    assert event_is_patch_coverable(domain_event, fold_entities=with_token)


def test_a_declaring_clients_OTHER_batches_are_unchanged_by_the_token(
    seeded_office, set_delta_patches
):
    """The token widens exactly one event and one entity, and nothing else.

    Declaring it must not make an uncovered office write coverable by accident —
    ``office.actor.restored`` is the nearest neighbour and the one a sloppy
    ``startswith("office.surface")`` or a widened family check would sweep in.
    """

    set_delta_patches(True)
    seeded_office.remove_actor(WORKSPACE, "personainst_qa_agent_0001")
    before = _log_end()
    seeded_office.restore_actor(WORKSPACE, "personainst_qa_agent_0001")

    declared = HISTORICAL_FOLD_ENTITIES | {
        OFFICE_ACTOR_ENTITY,
        OFFICE_ACTOR_LIFECYCLE_CAPABILITY,
        OFFICE_SURFACE_ENTITY,
        OFFICE_SURFACE_FOLD_CAPABILITY,
    }
    assert not batch_is_patch_coverable(
        [e for _, e in EventLog().iter_from_offset(before)], fold_entities=declared
    )
