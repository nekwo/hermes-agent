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
    batch_is_patch_coverable,
)
from agent_runtime.state_patches import (
    OFFICE_ACTOR_ENTITY,
    PATCH_OP_REFRESH,
    PATCH_OP_UPSERT,
    STATE_PATCHED_EVENT_TYPE,
    office_actor_patch_id,
)

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
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(WORKSPACE, _actor_payload("qa", x=-8.0, y=-2.0))
    store.upsert_actor(WORKSPACE, _actor_payload("qa", x=-7.0, y=-2.0))
    store.upsert_actor(WORKSPACE, _actor_payload("dev", x=3.0, y=1.0))
    assert store.get_actor(WORKSPACE, "personainst_qa_agent_0001").revision == 2
    assert store.get_actor(WORKSPACE, "personainst_dev_agent_0001").revision == 1
    return store


def _patches() -> list:
    return [e for _, e in EventLog().iter_from_offset(0) if e.type == STATE_PATCHED_EVENT_TYPE]


def _office_patches() -> list[dict]:
    return [e.payload for e in _patches() if e.payload.get("entity") == OFFICE_ACTOR_ENTITY]


def _drain() -> list:
    return [e for _, e in EventLog().iter_from_offset(0)]


def _batch_from(offset: int) -> list[tuple[int, object]]:
    return list(EventLog().iter_from_offset(offset))


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
    whole_office_row = office_summary_row(surface, seeded_office.list_actors(WORKSPACE))

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
def test_create_degrades_to_refresh_because_actor_count_moves(
    seeded_office, set_delta_patches
):
    """A create bumps the office row's ``actor_count`` / ``actors_truncated``,
    which an actor-row patch cannot express. It must take the full core."""

    set_delta_patches(True)
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=5.0, y=5.0))

    patch = [
        e.payload
        for _, e in EventLog().iter_from_offset(before)
        if e.type == STATE_PATCHED_EVENT_TYPE
    ][0]
    assert patch["op"] == PATCH_OP_REFRESH
    assert patch["id"] == f"{WORKSPACE}/personainst_backend_dev_agent_0001"
    assert "changed" not in patch


def test_readd_of_an_archived_key_degrades_to_refresh(seeded_office, set_delta_patches):
    """Re-adding an archived actor clears the resurrection ledger and rewrites
    the surface's ``updated_at``. Both live on the office row, not the actor.

    NOTE this reaches the degrade through the CREATE arm, not the ledger arm:
    ``remove_actor`` unlinks the live actor file, so after it the re-add sees
    ``existing is None``. The ledger arm gets its own test below — found by
    mutation, see its docstring.
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
    assert patch["op"] == PATCH_OP_REFRESH
    # And the ledger really did move — otherwise this test passes for the wrong
    # reason (it would just be re-testing the create branch).
    assert "personainst_qa_agent_0001" not in seeded_office.get_surface(WORKSPACE).archived_actor_keys


def test_a_live_actor_still_in_the_ledger_degrades_to_refresh(
    seeded_office, set_delta_patches
):
    """The ``surface_rewritten`` arm, made load-bearing.

    FOUND BY MUTATION. Replacing ``replaced_existing and not surface_rewritten``
    with a bare ``replaced_existing`` stayed GREEN: every test that reached the
    ledger-clearing branch got there via ``remove_actor``, which unlinks the
    live actor file — so ``existing is None`` and the CREATE arm answered first.
    The ledger condition was never independently exercised, and would have been
    free to be wrong.

    The state it guards is reachable without going through ``remove_actor``: the
    realm-sync pull applier can land an actor file for a key the local surface
    still lists as archived (an edit-vs-remove convergence). Seeded directly
    here, because constructing it through the store's own verbs is exactly what
    is impossible. The write then rewrites the surface — so it must refresh, not
    ship an actor-row upsert that leaves the launcher's ``archived_actor_keys``
    holding a key the server just cleared.
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
    # Both conditions were live: the actor existed AND the surface was rewritten.
    assert patch["op"] == PATCH_OP_REFRESH, patch
    assert actor_key not in seeded_office.get_surface(WORKSPACE).archived_actor_keys


def test_a_refresh_batch_is_not_coverable_but_the_same_shape_upsert_is(
    seeded_office, set_delta_patches
):
    """Anti-vacuity: the demotion is paired with the SAME batch promoting.

    A demotion test that passes because nothing was promotable proves nothing,
    so both halves run against a declaration that names ``office_actor``.
    """

    declared = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}
    set_delta_patches(True)

    # A create → refresh → NOT coverable.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=5.0, y=5.0))
    create_batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert not batch_is_patch_coverable(create_batch, fold_entities=declared)

    # A drag of the SAME actor → upsert → coverable.
    before = _log_end()
    seeded_office.upsert_actor(WORKSPACE, _actor_payload("backend_dev", x=6.0, y=6.0))
    drag_batch = [e for _, e in EventLog().iter_from_offset(before)]
    assert batch_is_patch_coverable(drag_batch, fold_entities=declared)


def test_remove_and_restore_stay_uncovered(seeded_office, set_delta_patches):
    """``office.actor.removed`` / ``.restored`` rewrite the surface ledger and
    are deliberately NOT in the covered set. Their batches take the full core
    with no patch-lane code at all."""

    declared = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}
    set_delta_patches(True)

    before = _log_end()
    seeded_office.remove_actor(WORKSPACE, "personainst_qa_agent_0001")
    assert not batch_is_patch_coverable(
        [e for _, e in EventLog().iter_from_offset(before)], fold_entities=declared
    )

    before = _log_end()
    seeded_office.restore_actor(WORKSPACE, "personainst_qa_agent_0001")
    assert not batch_is_patch_coverable(
        [e for _, e in EventLog().iter_from_offset(before)], fold_entities=declared
    )

    assert "office.actor.removed" not in LIVE_COVERED_DOMAIN_EVENT_TYPES
    assert "office.actor.restored" not in LIVE_COVERED_DOMAIN_EVENT_TYPES
    assert "office.surface.updated" not in LIVE_COVERED_DOMAIN_EVENT_TYPES


def test_surface_creation_batch_is_uncovered(isolate_agent_runtime_root, set_delta_patches):
    """The very first actor in a brand-new office emits
    ``office.surface.created`` beside it — uncovered, so the launcher gets the
    full core that gives it an ``offices[ws]`` row to fold into later."""

    declared = HISTORICAL_FOLD_ENTITIES | {OFFICE_ACTOR_ENTITY}
    set_delta_patches(True)
    store = OfficeStore()
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
