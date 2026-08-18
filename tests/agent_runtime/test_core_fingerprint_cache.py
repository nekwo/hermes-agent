"""EG-3.1 — the persisted read-model core, validated by a stat fingerprint.

Nine behaviours from the plan's spec, plus HY-H1's ``model_tools`` recorder and
BW-H1's two adopted transport-shaped tests, plus the closure-enumeration gate
that makes Plan EG §6.1's audit surface a test instead of a paragraph.

WHAT MAKES THESE NON-VACUOUS
============================

The mutant this file is written against is "always rebuild but stamp
``core_source=cache``" — a receipt field alone can be forged by whatever writes
it. So every case probes TWO independent things: the envelope's own answer AND
something the mutant cannot set without doing the work. The second witness is a
different object per case: a store fake that COUNTS its own reads (1), a value
driven to two distinct states across the fixture so a constant matches at most
one (2, 5, 6), an entity that is absent from the persisted core BY CONSTRUCTION
(3), a ``sys.meta_path`` recorder the mutant's import must traverse (8).

Witnesses assert ``core_source``, counts, ordering and values — never elapsed
milliseconds. The seconds are read off EG-2.1's receipts (ruling #60).
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.abc
import json
import logging
import os
import sqlite3
import sys
import threading

import pytest

from agent_runtime import core_cache, paths
from agent_runtime.models import OfficeActor, OfficeItem, OfficeSurface
from agent_runtime.serde import to_jsonable
from agent_runtime.snapshot import BUILD_ROLE_CACHE, BUILD_ROLE_LED, build_snapshot
from agent_runtime.store import WorkspaceStore
from hermes_time import now
from utils import atomic_json_write


WORKSPACE_ID = "ws_fingerprint_probe"
OFFICE_WORKSPACE = "ws_office_added_by_hand"


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def fresh_cache_lane():
    """Every case starts and ends with a process that has built nothing.

    ``core_cache``'s lane state is a property of the PROCESS (it closes on the
    first completed build), so a case that left it closed would silently turn
    the next case's cache probe into an unconditional rebuild — passing for the
    wrong reason.
    """

    core_cache.reset_process_state()
    yield
    core_cache.reset_process_state()


@pytest.fixture(autouse=True)
def shadow_requests(monkeypatch):
    """Record the shadow-validation requests instead of running them.

    Two reasons, both concrete. (1) The shadow build is a REAL full build on a
    daemon thread; against a ``tmp_path`` root pytest is about to delete, a build
    that outlives its case is a teardown race, not a test. (2) The window's whole
    point is that a cache-hit boot ALSO walks the store — so a case measuring
    what the SERVING path touched has to separate the two, and the separation
    should be explicit rather than a hope about thread scheduling.

    The recorder still returns True and is asserted non-empty where it matters,
    so a landing that quietly removed the window reds instead of passing.
    """

    requests: list[str] = []

    def record(cached, *, caller, build, adopt=None):
        requests.append(caller)
        return True

    monkeypatch.setattr(core_cache, "maybe_start_shadow_validation", record)
    return requests


def _new_context() -> None:
    """What a fresh serve child sees: a lane that has built nothing yet."""

    core_cache.reset_process_state()


def _seed_workspace(name: str) -> str:
    """One durable, snapshot-visible row whose value the cases drive."""

    store = WorkspaceStore()
    item = store.create(name=name, workspace_id=WORKSPACE_ID)
    return item.id


def _rewrite_workspace_name(name: str) -> None:
    """Drive the row to a new value with NO EventLog event.

    Deliberately event-less. A store mutation that also appends an event would
    let a fingerprint that watches only ``events.jsonl`` pass every case here,
    and non-evented writers are exactly the class that made the event-offset key
    unsound (two shipped incidents; HY-H1 constraint 3).
    """

    path = paths.workspace_path(WORKSPACE_ID)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["name"] = name
    atomic_json_write(path, payload)


def _served_workspace_name(core: dict) -> str | None:
    for row in core.get("workspaces") or []:
        if isinstance(row, dict) and row.get("id") == WORKSPACE_ID:
            return row.get("name")
    return None


def _persisted_core() -> dict:
    return json.loads(core_cache.core_path().read_text(encoding="utf-8"))


def converge_persisted_core(*, limit: int = 4) -> int:
    """Build until the persisted key describes the SETTLED store; return builds.

    Two builds are normally needed on a virgin store and the reason is recorded
    in ``core_cache.write_back``: the build is not a pure reader (it materializes
    missing persona-instance rows and CREATES the chat SessionDB), and the key is
    taken pre-build on purpose, so the first write-back describes inputs the
    build then moved.

    The bound is the point of the helper. A fingerprint that never converges
    would mean a build perturbs its own inputs forever — the cache could never
    hit, and every case below would pass by rebuilding. That is a loud failure
    here rather than a silent one everywhere.
    """

    for attempt in range(1, limit + 1):
        # Each attempt starts from NO cache, so this helper only ever asks "does
        # a build of the settled store write a key that describes it?" — never
        # "does a build update an existing key?", which is the SUBJECT of the
        # write-back case below and must not be a precondition of its fixture.
        core_cache.core_path().unlink(missing_ok=True)
        core_cache.sidecar_path().unlink(missing_ok=True)
        _new_context()
        build_snapshot()
        if core_cache.read_persisted_core().matched:
            _new_context()
            return attempt
    raise AssertionError(
        "the persisted core's fingerprint never converged: after "
        f"{limit} builds the key still does not describe the settled store, so "
        "a build is perturbing one of its own inputs on every pass. Find the "
        "input (core_cache.build_input_fingerprint enumerates them) rather than "
        "raising this bound."
    )


@pytest.fixture
def seeded_cache():
    """A workspace at ``alpha-one``, with a matching persisted core in place."""

    _seed_workspace("alpha-one")
    converge_persisted_core()
    return WORKSPACE_ID


class _CountingStores:
    """Counts the build's own store reads. The mutant's tell.

    Three independent readers of three different stores, wrapped in place: a
    rebuilding mutant that stamps ``core_source=cache`` still has to walk them,
    and the counter lives HERE, in the test, where nothing in production can set
    it.
    """

    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        from agent_runtime import events as events_mod
        from agent_runtime import office_store as office_mod
        from agent_runtime import snapshot as snapshot_mod
        from agent_runtime import store as store_mod

        real_list_all = store_mod.AgentStore.list_all
        real_scan = office_mod.OfficeStore.scan_actors
        real_tail = events_mod.CachedEventLog.tail

        def counted_list_all(inner_self, *args, **kwargs):
            self.calls.append("agent_store.list_all")
            return real_list_all(inner_self, *args, **kwargs)

        def counted_scan(inner_self, *args, **kwargs):
            self.calls.append("office_store.scan_actors")
            return real_scan(inner_self, *args, **kwargs)

        def counted_tail(inner_self, *args, **kwargs):
            self.calls.append("event_log.tail")
            return real_tail(inner_self, *args, **kwargs)

        monkeypatch.setattr(store_mod.AgentStore, "list_all", counted_list_all)
        monkeypatch.setattr(office_mod.OfficeStore, "scan_actors", counted_scan)
        monkeypatch.setattr(events_mod.CachedEventLog, "tail", counted_tail)
        # The snapshot module holds its own bindings for two of the three.
        monkeypatch.setattr(snapshot_mod, "AgentStore", store_mod.AgentStore)
        monkeypatch.setattr(snapshot_mod, "OfficeStore", office_mod.OfficeStore)


# --------------------------------------------------------------------------- #
# 1. A fingerprint match serves the cache and reads no store
# --------------------------------------------------------------------------- #
def test_a_fingerprint_match_serves_the_cache_and_reads_no_store(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, shadow_requests
):
    """The stage's whole claim: validation instead of reconstruction.

    *Mutation:* always rebuild but stamp ``core_source=cache``.
    *Why it cannot pass:* the second probe is a counter that lives in this test's
    own store wrappers. A build that runs must call them; a cache hit cannot.

    The shadow-validation window is recorded rather than run (see the fixture),
    and asserted to have been REQUESTED — so "reads no store" is a statement
    about the path that answered the caller, not a claim that the window is off.
    """

    counters = _CountingStores(monkeypatch)
    _new_context()
    info: dict = {"caller": "probe"}
    core = build_snapshot(build_info=info)

    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE
    assert info["role"] == BUILD_ROLE_CACHE
    assert counters.calls == [], (
        "a fingerprint-match boot read the store: "
        f"{sorted(set(counters.calls))}. The persisted core is the answer; "
        "walking the store as well buys nothing and costs the whole 20s."
    )
    assert shadow_requests == ["probe"], shadow_requests


def test_a_cache_hit_emits_no_led_build_receipt(
    isolate_agent_runtime_root, seeded_cache, caplog
):
    """A cache hit is not a build, and the log must not claim one.

    EG-2.1 made a boot's build COUNT the count of ``role=led`` lines. A cache
    hit that printed one would put the log straight back into the state where a
    wait and a build are indistinguishable — the defect that made one build read
    as three on the 2026-08-17 boot.
    """

    _new_context()
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"), caplog.at_level(
        logging.INFO, logger="agent_runtime.snapshot"
    ):
        build_snapshot(build_info={"caller": "probe"})

    messages = [record.getMessage() for record in caplog.records]
    assert not [line for line in messages if "snapshot_build_core" in line], messages
    assert [
        line
        for line in messages
        if "snapshot_core_cache" in line and "core_source=cache" in line
    ], messages


# --------------------------------------------------------------------------- #
# 2. A changed input rebuilds and serves the NEW value
# --------------------------------------------------------------------------- #
def test_a_changed_input_rebuilds_and_serves_the_new_value(
    isolate_agent_runtime_root, seeded_cache
):
    """*Mutation:* serve the cache on mismatch.

    Two driven values, so a constant matches at most one: the persisted core
    provably holds ``alpha-one`` (it was written before the change) and the
    served core must hold ``alpha-two``.
    """

    assert _served_workspace_name(_persisted_core()) == "alpha-one"
    _rewrite_workspace_name("alpha-two")

    _new_context()
    info: dict = {"caller": "probe"}
    core = build_snapshot(build_info=info)

    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT
    assert info["role"] == BUILD_ROLE_LED
    assert _served_workspace_name(core) == "alpha-two"


# --------------------------------------------------------------------------- #
# 3. An ADDED file flips the fingerprint
# --------------------------------------------------------------------------- #
def _write_office_by_hand(workspace_id: str) -> None:
    """An office surface + one desk, written straight to disk. No event, no store.

    The office tree is the case a NAME LIST cannot see: it is absent from the
    serve read-cache's ``_FINGERPRINT_STORE_DIRS`` entirely, so a fingerprint
    that stats only previously-known paths reports "nothing changed" for a whole
    new office. Written without the store so ``events.jsonl`` — which IS on that
    name list — does not move and rescue the mutant.
    """

    stamp = now()
    surface = OfficeSurface(
        workspace_id=workspace_id,
        folders=["Ops"],
        archived_actor_keys=[],
        revision=1,
        created_at=stamp,
        updated_at=stamp,
        updated_by="probe",
    )
    atomic_json_write(paths.office_surface_path(workspace_id), to_jsonable(surface))
    actor = OfficeActor(
        workspace_id=workspace_id,
        actor_key="probe_desk",
        persona_id="probe",
        items=[
            OfficeItem(
                item_id="probe_desk_item",
                persona_id="probe",
                kind="desk",
                position=(1.0, 2.0),
                folder="Ops",
            )
        ],
        revision=1,
        created_at=stamp,
        updated_at=stamp,
        updated_by="probe",
    )
    atomic_json_write(
        paths.office_actors_dir(workspace_id) / "probe_desk.json", to_jsonable(actor)
    )


def test_an_added_file_flips_the_fingerprint(isolate_agent_runtime_root, seeded_cache):
    """*Mutation:* fingerprint only previously-known paths (the pre-EG-3.1 name
    list — ``serve._FINGERPRINT_ROOT_FILES`` + ``_FINGERPRINT_STORE_DIRS``).

    *Why it cannot pass:* the added entity is absent from the persisted core by
    construction, so serving the cache serves an office that does not exist.
    """

    assert OFFICE_WORKSPACE not in (_persisted_core().get("offices") or {})
    _write_office_by_hand(OFFICE_WORKSPACE)

    _new_context()
    core = build_snapshot(build_info={"caller": "probe"})

    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT
    assert OFFICE_WORKSPACE in (core.get("offices") or {}), sorted(core.get("offices") or {})


# --------------------------------------------------------------------------- #
# 4. A SessionDB-only mutation flips the fingerprint
# --------------------------------------------------------------------------- #
def test_a_sessiondb_only_mutation_flips_the_fingerprint(
    isolate_agent_runtime_root, seeded_cache
):
    """*Mutation:* skip the database files in the stat set.

    The chat SessionDB lives under the HERMES head home, not the store root, and
    a WAL commit that has not checkpointed leaves the main file's mtime
    untouched — so the ``-wal`` sibling is the load-bearing half. The mutant's
    fingerprint is unchanged by construction here and serves the cache,
    convicted by the ``core_source`` probe.
    """

    from agent_runtime.chat_session_scope import chat_session_db_path
    from hermes_state import SessionDB

    before = core_cache.build_input_fingerprint()
    SessionDB(db_path=chat_session_db_path()).create_session(
        "probe-fingerprint-session", "test"
    )
    after = core_cache.build_input_fingerprint()
    assert before is not None and after is not None
    assert before.digest != after.digest, (
        "a write through SessionDB's own writer did not move the fingerprint; "
        "the database files (or their WAL siblings) are not in the stat set"
    )

    _new_context()
    core = build_snapshot(build_info={"caller": "probe"})
    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT


# --------------------------------------------------------------------------- #
# 4b. Reading the SessionDB is not writing it (MC-1 / P2)
# --------------------------------------------------------------------------- #
def _db_key(db_path) -> tuple:
    """Exactly what ``build_input_fingerprint`` records for one database."""

    out: list = []
    core_cache._db_entries(db_path, out)
    return tuple(out)


def _write(path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _set_mtime_ns(path, mtime_ns: int) -> None:
    """Move an mtime EXACTLY, with no sleep and no clock-granularity hazard.

    NTFS timestamps advance in ~15.6 ms ticks here, so two writes inside one
    tick are indistinguishable — a fingerprint case that drives mtime by doing
    work fast enough would be measuring the tick, not the rule.
    """

    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_an_empty_wals_mtime_is_not_a_change_signal(isolate_agent_runtime_root, tmp_path):
    """A zero-length WAL holds no frames, so its clock says nothing about content.

    SQLite re-creates the WAL empty when a connection opens, stamping it with the
    open time. Keyed on that mtime, the build's own READ of a database becomes
    indistinguishable from somebody's WRITE to it — and since the key is taken
    pre-build by design, the build guaranteed the next process a mismatch.
    """

    db = tmp_path / "probe" / "state.db"
    _write(db, b"main")
    _write(db.parent / "state.db-wal", b"")
    before = _db_key(db)
    _set_mtime_ns(db.parent / "state.db-wal", 1_700_000_000_000_000_000)
    after = _db_key(db)
    assert after == before, (
        "an empty WAL's mtime moved the database's fingerprint entries, so every "
        f"open of this database invalidates the key it was read under: {before} "
        f"-> {after}"
    )


def test_a_non_empty_wal_still_carries_its_commit_signal(
    isolate_agent_runtime_root, tmp_path
):
    """The mask must not weaken the thing the siblings are stat'd FOR.

    An uncheckpointed commit leaves the main file's mtime untouched and lives
    only in the WAL. The instant the WAL is non-empty its mtime counts again in
    full — otherwise this change would trade a boot-time miss for a served core
    that is missing a committed write, which is the failure class inverted.
    """

    db = tmp_path / "probe" / "state.db"
    _write(db, b"main")
    wal = db.parent / "state.db-wal"
    _write(wal, b"one uncheckpointed frame")
    before = _db_key(db)
    _set_mtime_ns(wal, 1_700_000_000_000_000_000)
    after = _db_key(db)
    assert after != before, (
        "a NON-EMPTY WAL's mtime no longer moves the fingerprint — an "
        "uncheckpointed commit is now invisible to the key, and a core missing "
        "that write can be served as authoritative"
    )


def test_an_absent_wal_and_an_empty_one_stay_different_facts(
    isolate_agent_runtime_root, tmp_path
):
    """The mask moves the mtime and never the size, so absence stays visible.

    ``_stat_entry`` records a missing input as ``-1/-1`` precisely so an
    appearing file is not indistinguishable from an unchanged one. A mask that
    flattened both to zero would have re-opened that hole for the WAL.
    """

    db = tmp_path / "probe" / "state.db"
    _write(db, b"main")
    absent = _db_key(db)
    _write(db.parent / "state.db-wal", b"")
    empty = _db_key(db)
    assert absent != empty, (
        "a WAL that appeared did not move the fingerprint; the mask flattened "
        "the size as well as the mtime"
    )


def test_the_shm_sibling_is_outside_the_closure(isolate_agent_runtime_root, tmp_path):
    """``-shm`` is a reader's scratch index, not a record of anything.

    SQLite rebuilds it from the WAL on open and unlinks it when the last
    connection closes. Everything it could indicate is already carried by the
    WAL's frames or by the main file's checkpoint, and the sibling fingerprint
    next door (``stream._scope_fingerprint``) has always omitted it.
    """

    db = tmp_path / "probe" / "state.db"
    _write(db, b"main")
    before = _db_key(db)
    _write(db.parent / "state.db-shm", b"a rebuilt shared-memory index")
    appeared = _db_key(db)
    assert appeared == before, (
        f"the -shm sibling entered the fingerprint: {before} -> {appeared}"
    )
    _set_mtime_ns(db.parent / "state.db-shm", 1_700_000_000_000_000_000)
    assert _db_key(db) == before, "the -shm sibling's mtime entered the fingerprint"


@contextlib.contextmanager
def _sessiondb_in_the_live_shape():
    """The chat SessionDB as the LIVE root has it: on-disk WAL, connection held.

    Three steps, each load-bearing:

    * force ``journal_mode=WAL`` through a RAW connection. ``hermes_state``
      refuses to ENABLE WAL on this SQLite (3.45.3 carries the WAL-reset
      corruption bug, so it falls back to ``journal_mode=DELETE``) but
      deliberately does not downgrade a database that is already WAL on disk.
      Without this the case would be VACUOUS — a DELETE-mode database has no
      ``-wal`` at all, and a mask is trivially green when there is no file to
      mask;
    * hold the connection. The siblings exist only while a connection does — the
      last one to close checkpoints and unlinks them. That is why the live root
      has them (a serve holds the SessionDB for its life) and why a test that
      opens and closes never sees one;
    * one WARM-UP open, because the first open under the new journal mode also
      grows the main file with one-time schema work — a genuine input change,
      and not the one under test.

    Measured after warm-up: every further open moves ``-wal``'s mtime and
    nothing else, at size 0. That is exactly the self-perturbation P2 removes.
    """

    from agent_runtime.chat_session_scope import chat_session_db_path
    from hermes_state import SessionDB

    db_path = chat_session_db_path()
    SessionDB(db_path=db_path).close()
    holder = sqlite3.connect(str(db_path))
    try:
        mode = holder.execute("PRAGMA journal_mode=WAL").fetchone()
        assert mode and str(mode[0]).lower() == "wal", (
            f"could not put the fixture SessionDB into WAL mode (got {mode!r}); "
            "these cases cannot measure the mask without it"
        )
        holder.execute("CREATE TABLE IF NOT EXISTS mc1_wal_probe (id INTEGER)")
        holder.commit()
        SessionDB(db_path=db_path).close()
        assert os.path.exists(f"{db_path}-wal"), (
            "no -wal sibling exists while a connection is held, so this fixture "
            "is not reproducing the live shape and nothing below would be tested"
        )
        yield db_path
    finally:
        holder.close()


def _open_the_sessiondb(db_path) -> None:
    """What the build does to this database: open it, read, close."""

    from hermes_state import SessionDB

    handle = SessionDB(db_path=db_path)
    handle.list_gateway_sessions()
    handle.close()


def test_the_builds_own_sessiondb_open_does_not_move_the_key(
    isolate_agent_runtime_root,
):
    """The production shape, driven through SessionDB's own connection factory.

    The unit cases above pin the mask; this one pins that the mask covers what
    the RUNTIME actually does — the measured 2026-08-18 fact that ``-wal`` moved
    during the boot build that was reading it.
    """

    with _sessiondb_in_the_live_shape() as db_path:
        wal = f"{db_path}-wal"
        before_key = core_cache.build_input_fingerprint()
        assert before_key is not None
        before_wal = os.stat(wal).st_mtime_ns
        _open_the_sessiondb(db_path)
        after_wal = os.stat(wal)
        after_key = core_cache.build_input_fingerprint()
        assert after_key is not None

        # Non-vacuity FIRST: if the open did not disturb the sibling, the
        # equality below would be true for want of anything to be true about.
        assert after_wal.st_mtime_ns != before_wal, (
            "opening the SessionDB did not move -wal's mtime, so this case "
            "measured nothing — the fixture is not in the live shape"
        )
        assert after_wal.st_size == 0, (
            f"-wal is {after_wal.st_size} bytes after a read-only open; this "
            "case is about the EMPTY-WAL artefact and a non-empty one is "
            "legitimately keyed"
        )
        assert after_key.digest == before_key.digest, (
            "reading the SessionDB moved the read-model cache's key, so the "
            "build perturbs its own inputs and no process can ever be served "
            "the core the previous one persisted"
        )


def test_a_persisted_key_survives_the_next_process_opening_the_database(
    isolate_agent_runtime_root,
):
    """A2's cross-boot half: the key must outlive the next boot's own first read.

    This is the field failure in one line. Boot 1 settles and persists a key.
    Boot 2 starts, its build opens the chat SessionDB — and under the old
    keying that open moved ``-wal`` and invalidated boot 1's key before boot 2
    ever asked whether it matched. No boot could be served the core the previous
    boot had just written.

    Written as "settle, then open, then judge" rather than as a convergence
    COUNT because the count cannot see this: the fixture's ``build_snapshot``
    does not open the chat SessionDB at all (measured — a build leaves ``-wal``
    untouched here), so a convergence-bound assertion would be green with or
    without the mask. The open is therefore driven explicitly, through
    SessionDB's own factory, exactly as the live build reaches it.
    """

    with _sessiondb_in_the_live_shape() as db_path:
        _seed_workspace("alpha-one")
        converge_persisted_core()
        assert core_cache.read_persisted_core().matched, (
            "the fixture did not settle, so nothing below is about the open"
        )
        wal = f"{db_path}-wal"
        before = os.stat(wal).st_mtime_ns
        _new_context()
        _open_the_sessiondb(db_path)
        after = os.stat(wal).st_mtime_ns

        assert after != before, (
            "the SessionDB open did not disturb -wal, so this case measured "
            "nothing — the fixture is not in the live shape"
        )
        read = core_cache.read_persisted_core()
        assert read.matched, (
            "the persisted key stopped matching because the NEXT process opened "
            f"the database it was keyed over (demote reason: {read.reason!r}). "
            "The build perturbs its own inputs, so the cache can never hit."
        )


# --------------------------------------------------------------------------- #
# 5. A mismatch serves LABELED stale first, authoritative after
# --------------------------------------------------------------------------- #
def test_a_mismatch_serves_labeled_stale_first_then_authoritative(
    isolate_agent_runtime_root, seeded_cache, monkeypatch
):
    """Frame 1 while the build is GATED; frame 2 after it completes.

    *Why not settable:* a mutant that blocks until the rebuild finishes cannot
    deliver frame 1 while this test holds the gate, and a mutant that serves
    frame 1 unlabeled fails the marker probe. The two frames also carry the two
    driven values, so neither can be a constant.
    """

    from agent_runtime import snapshot as snapshot_mod
    from agent_runtime.stream import stream_frames

    _rewrite_workspace_name("alpha-two")
    _new_context()

    gate = threading.Event()
    real_build = snapshot_mod._build_snapshot_uncoalesced

    def gated_build(*args, **kwargs):
        assert gate.wait(20), "the gated build was never released"
        return real_build(*args, **kwargs)

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", gated_build)

    frames: list[dict] = []
    failure: list[BaseException] = []

    def drive() -> None:
        try:
            for frame in stream_frames(max_frames=2, poll_interval_seconds=0.01):
                frames.append(frame)
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)

    worker = threading.Thread(target=drive, name="probe-stream", daemon=True)
    worker.start()

    # Bounded wait on a condition, not a fixed sleep: the assertion below is
    # about WHAT arrived while the gate was held, so the wait must end as soon as
    # something has (or after 10s, well inside the 30s per-test cap).
    idle = threading.Event()
    for _ in range(200):
        if frames:
            break
        idle.wait(0.05)
    assert frames, "no frame arrived while the rebuild was gated — nothing was painted"

    stale = frames[0]
    stale_parity = stale["core"]["parity"]
    assert stale["type"] == "hydrate"
    assert stale_parity["core_source"] == core_cache.CORE_SOURCE_CACHE
    assert stale_parity["core_stale"] is True
    assert stale_parity["freshness"]["state"] == "stale", stale_parity["freshness"]
    assert _served_workspace_name(stale["core"]) == "alpha-one"

    gate.set()
    worker.join(20)
    assert not failure, failure
    assert not worker.is_alive()
    assert len(frames) == 2, [frame.get("type") for frame in frames]

    fresh_parity = frames[1]["core"]["parity"]
    assert "core_stale" not in fresh_parity
    assert fresh_parity["freshness"]["state"] == "fresh"
    assert _served_workspace_name(frames[1]["core"]) == "alpha-two"


def test_a_stale_labeled_core_is_never_live_to_the_launchers_own_predicate(
    isolate_agent_runtime_root, seeded_cache
):
    """The honesty half, stated as the consumer states it.

    The launcher's ``MissionSnapshotEnvelope.health()`` reads
    ``parity.freshness.state`` and returns ``stale`` on the literal ``"stale"``
    BEFORE it consults its own freshness window. A stale-served core therefore
    cannot read ``live`` on a launcher pinned at today's contract, which is what
    makes "a stale-labeled frame is never authoritative" a property of the
    existing consumer rather than a promise about a future one.
    """

    _rewrite_workspace_name("alpha-two")
    _new_context()
    stale = core_cache.take_stale_first_core(caller="probe")
    assert stale is not None
    assert stale["parity"]["freshness"]["state"] == "stale"
    assert stale["parity"]["core_stale"] is True
    # One-shot per process: a resubscribe cannot re-paint an old projection.
    assert core_cache.take_stale_first_core(caller="probe") is None


def test_a_matching_core_is_never_painted_stale(isolate_agent_runtime_root, seeded_cache):
    """The pessimistic lie is a lie too.

    A fingerprint MATCH must go out authoritative, not labeled stale-then-
    replaced: painting a validated projection as unvalidated would train the
    operator to ignore the banner, which is how a fence stops being a fence.
    """

    _new_context()
    assert core_cache.take_stale_first_core(caller="probe") is None


# --------------------------------------------------------------------------- #
# 6. Every build writes back
# --------------------------------------------------------------------------- #
def test_every_build_writes_back_not_only_the_boot_build(
    isolate_agent_runtime_root, seeded_cache
):
    """Three contexts: build, mutate+rebuild, then a hit against the SECOND build.

    *Mutation:* write back only on boot builds (e.g. refuse the write when a
    sidecar already exists). The third context then either rebuilds — reddening
    the ``core_source`` probe — or serves the FIRST build's value, reddening the
    row probe. Either way red.

    This is the half that made ``snapshot.json`` two days stale on the live
    store: a persisted projection nothing keeps current is a projection nobody
    can serve.
    """

    _rewrite_workspace_name("alpha-two")
    _new_context()
    second = build_snapshot(build_info={"caller": "probe"})
    assert second["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT
    assert _served_workspace_name(_persisted_core()) == "alpha-two", (
        "the rebuild did not write its core back, so the next process would pay "
        "for state this one already had in hand"
    )

    _new_context()
    third = build_snapshot(build_info={"caller": "probe"})
    assert third["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE
    assert _served_workspace_name(third) == "alpha-two"


# --------------------------------------------------------------------------- #
# 7. The equivalence golden — THE authority guard
# --------------------------------------------------------------------------- #
def test_the_cache_served_core_equals_the_rebuilt_core_field_for_field(
    isolate_agent_runtime_root, seeded_cache
):
    """THE authority guard, and the reason an input-closure gap reds HERE.

    For one fingerprint the two representations must be the same projection.
    Everything that is allowed to differ is named in
    ``core_cache._SHADOW_IGNORED_*`` and asserted below, so the exemption list
    cannot be widened quietly to make a real divergence pass: a section added to
    it is a section this golden stops guarding, and that has to be a visible
    edit.
    """

    from agent_runtime import snapshot as snapshot_mod

    _new_context()
    cached = build_snapshot(build_info={"caller": "probe"})
    assert cached["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE
    rebuilt = snapshot_mod._build_snapshot_uncoalesced()

    assert core_cache.compare_cores(cached, rebuilt) is None, (
        "the cache-served core and a rebuild of the same inputs disagree on "
        f"section {core_cache.compare_cores(cached, rebuilt)!r} — that is an "
        "input-closure gap, and the fix is widening the fingerprint's inputs, "
        "never trusting the cache harder"
    )

    left = to_jsonable(cached)
    right = to_jsonable(rebuilt)
    differing = sorted(
        key for key in set(left) | set(right) if left.get(key) != right.get(key)
    )
    assert differing == ["generated_at", "parity"], differing
    parity_differing = sorted(
        key
        for key in set(left["parity"]) | set(right["parity"])
        if left["parity"].get(key) != right["parity"].get(key)
    )
    assert set(parity_differing) <= (
        core_cache._SHADOW_IGNORED_PARITY_KEYS | {"watermark"}
    ), parity_differing
    # ``watermark`` is exempted only for the clock that says when it was READ;
    # ``event_offset`` — two cores at different log positions — stays guarded.
    assert left["parity"]["watermark"]["event_offset"] == (
        right["parity"]["watermark"]["event_offset"]
    )
    # The exemption lists themselves, pinned. Widening one is how this golden
    # would stop guarding a section, so it has to be a visible edit here too.
    assert core_cache._SHADOW_IGNORED_TOP_KEYS == {
        "generated_at",
        "parity",
        "runtime_paths_diagnostic",
    }
    assert core_cache._SHADOW_IGNORED_WATERMARK_KEYS == {"captured_at"}
    assert core_cache._SHADOW_IGNORED_PARITY_KEYS == {
        "build_ms",
        "sections_ms",
        "generated_at",
        "projection_age_ms",
        "core_source",
        "core_stale",
        "freshness",
        "snapshot_bytes",
    }


# --------------------------------------------------------------------------- #
# 8. A cache hit imports no model_tools (HY-H1 constraint 2)
# --------------------------------------------------------------------------- #
class _ImportRecorder(importlib.abc.MetaPathFinder):
    """Records every attempt to IMPORT the named module. Never resolves it."""

    def __init__(self, watched: str):
        self.watched = watched
        self.attempts: list[str] = []

    def find_spec(self, fullname, path=None, target=None):  # noqa: D102
        if fullname == self.watched or fullname.startswith(self.watched + "."):
            self.attempts.append(fullname)
        return None


def test_a_cache_hit_boot_imports_no_model_tools(
    isolate_agent_runtime_root, seeded_cache
):
    """The persisted core already carries the tool rows.

    A cache hit that still triggered the deferred ``model_tools`` import would
    re-buy ~1.3 s plus the ``check_fn`` discovery storm — the exact cost BW-H3
    took off the boot path.

    *Mutation:* import it anyway (an eager "just in case" warm).
    *Why not settable:* the recorder IS ``sys.meta_path``'s first finder, and the
    module is evicted from ``sys.modules`` below, so any import attempt must
    traverse it. Without the eviction this test would be vacuous the moment an
    earlier build in the same process had already imported the module — which is
    exactly what the seeding build does.
    """

    evicted = {
        name: module
        for name, module in list(sys.modules.items())
        if name == "model_tools" or name.startswith("model_tools.")
    }
    for name in evicted:
        del sys.modules[name]
    recorder = _ImportRecorder("model_tools")
    sys.meta_path.insert(0, recorder)
    try:
        _new_context()
        core = build_snapshot(build_info={"caller": "probe"})
    finally:
        sys.meta_path.remove(recorder)
        sys.modules.update(evicted)

    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE
    assert recorder.attempts == [], recorder.attempts


# --------------------------------------------------------------------------- #
# 9. Adopted from BW-H1 unchanged
# --------------------------------------------------------------------------- #
def test_a_build_stamp_mismatch_demotes(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, caplog
):
    """*Kill:* trust a stale install's core.

    Property 5: an upgrade must never be able to serve the old install's
    projection. The inputs are byte-identical here — the ONLY thing that moved
    is which code is running — so a cache hit would be a pure code-version lie.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "git:deadbeef:clean")
    _new_context()
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        core = build_snapshot(build_info={"caller": "probe"})

    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT
    assert [
        line
        for line in (record.getMessage() for record in caplog.records)
        if core_cache.DEMOTE_BUILD_STAMP_MISMATCH in line
    ], [record.getMessage() for record in caplog.records]


def test_an_unmeasurable_build_stamp_refuses_the_cache(
    isolate_agent_runtime_root, seeded_cache, monkeypatch
):
    """An install whose code cannot be identified does not get to be trusted.

    The tempting alternative — treat ``unknown`` as matching ``unknown`` — reads
    as harmless and is the whole failure: two different installs both answer
    "unknown" and the cache crosses between them silently.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: None)
    _new_context()
    core = build_snapshot(build_info={"caller": "probe"})
    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT


def test_a_failed_cache_write_leaves_the_build_path_byte_identical(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """*Kill:* raise.

    The cache is added to the path a boot waits on, so its failure mode is the
    part that has to be boring: a write that cannot land logs and changes
    nothing about the core that was just built, the receipt that was just
    emitted, or the value returned.
    """

    _seed_workspace("alpha-one")
    _new_context()
    good = build_snapshot(build_info={"caller": "probe"})

    def boom(*args, **kwargs):
        raise OSError("the cache directory is not writable")

    monkeypatch.setattr(core_cache, "atomic_json_write", boom)
    _new_context()
    with caplog.at_level(logging.INFO):
        info: dict = {"caller": "probe"}
        bad = build_snapshot(build_info=info)

    messages = [record.getMessage() for record in caplog.records]
    assert info["role"] == BUILD_ROLE_LED
    assert [line for line in messages if "snapshot_build_core role=led" in line], messages
    assert [
        line for line in messages if "snapshot_core_cache_write ok=false" in line
    ], messages
    assert core_cache.compare_cores(good, bad) is None, core_cache.compare_cores(good, bad)


# --------------------------------------------------------------------------- #
# The shadow-validation window (§6.1's third mitigation)
# --------------------------------------------------------------------------- #
def test_a_shadow_divergence_is_loud_named_and_adopted(
    isolate_agent_runtime_root, seeded_cache, caplog
):
    """A cache-hit boot that was WRONG says so, says where, and stops being wrong.

    The receipt names the differing SECTION on purpose: a boolean "the cache
    diverged" tells an operator to distrust the cache without telling them which
    input class to widen, and "widen the closure" is the only sanctioned
    response.

    *Kill:* log the divergence and keep serving the cache. Then the rebuilt core
    is not written back, the lane stays open, and nothing tells the lane that
    already painted to replace what it painted.
    """

    _new_context()
    cached = build_snapshot(build_info={"caller": "probe"})
    assert cached["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE

    divergent = json.loads(json.dumps(to_jsonable(cached)))
    divergent["workspaces"] = [
        {**row, "name": "alpha-divergent"} for row in divergent["workspaces"]
    ]
    adopted: list[dict] = []

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        section = core_cache.shadow_validate(
            cached,
            caller="probe",
            build=lambda: divergent,
            adopt=adopted.append,
        )

    assert section == "workspaces"
    assert [
        line
        for line in (record.getMessage() for record in caplog.records)
        if "snapshot_core_shadow_divergence" in line and "section=workspaces" in line
    ], [record.getMessage() for record in caplog.records]
    assert adopted and adopted[0] is divergent
    assert not core_cache.lane_armed(), (
        "the lane stayed open after a divergence, so this process would keep "
        "serving the core it just proved wrong"
    )
    assert _served_workspace_name(_persisted_core()) == "alpha-divergent"


def test_the_shadow_window_runs_at_most_once_per_process(
    isolate_agent_runtime_root, seeded_cache
):
    """A boot issues several builds; the validation is per PROCESS, not per hit.

    One full build behind every cache hit would cost the process more than the
    cache saved — four boot builds would buy four rebuilds.
    """

    _new_context()
    core = build_snapshot(build_info={"caller": "probe"})
    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE
    assert core_cache.claim_shadow_slot() is True
    assert core_cache.claim_shadow_slot() is False
    assert core_cache.claim_shadow_slot() is False
    _new_context()
    assert core_cache.claim_shadow_slot() is True


def test_a_shadow_build_does_not_close_the_cache_lane(
    isolate_agent_runtime_root, seeded_cache
):
    """The validation is not the process's answer.

    A shadow build that closed the lane would make the NEXT boot caller pay a
    full build for the privilege of having validated the one it just avoided —
    the hydrate, which is the caller the launcher is actually waiting on.
    """

    _new_context()
    with core_cache.shadow_build_scope():
        core_cache.note_full_build_completed()
    assert core_cache.lane_armed()
    core_cache.note_full_build_completed()
    assert not core_cache.lane_armed()


# --------------------------------------------------------------------------- #
# The input closure — Plan EG §6.1's audit surface, as a gate
# --------------------------------------------------------------------------- #
def test_the_fingerprint_covers_every_named_input_class(isolate_agent_runtime_root):
    """The closure is enumerable, and each class is named by its OWN authority.

    §6.1 calls this stage's input closure the plan's single biggest bet, and the
    first of its three mitigations is that the closure is derived from the
    build's own readers rather than restated. This asserts that each of those
    authorities actually contributes to the key — so deleting a class (or
    letting an authority's answer silently become empty) is red here rather than
    an unlabeled stale core in the field.
    """

    from agent.skill_utils import get_all_skills_dirs
    from agent_runtime.chat_session_scope import chat_session_db_path
    from agent_runtime.config import harness_root_config_path
    from agent_runtime.running_work import running_work_store_paths
    from hermes_cli.profiles import _get_profiles_root
    from hermes_constants import get_config_path

    _seed_workspace("alpha-one")
    _write_office_by_hand(OFFICE_WORKSPACE)
    fingerprint = core_cache.build_input_fingerprint()
    assert fingerprint is not None
    covered = set(core_cache.iter_fingerprint_paths(fingerprint))

    def assert_covered(label: str, path) -> None:
        assert str(path) in covered, (
            f"the {label} input class is NOT in the fingerprint: {path}. A build "
            "input that cannot flip the key is a core that can be served "
            "unlabeled-stale as authoritative."
        )

    # 1 — the store-root subtree, including a tree no name list ever covered.
    assert_covered("store root", paths.store_root())
    assert_covered("store subtree (workspaces)", paths.workspace_path(WORKSPACE_ID))
    assert_covered("store subtree (offices)", paths.office_surface_path(OFFICE_WORKSPACE))
    assert_covered("store subtree (event log)", paths.events_path())
    # 2 — the running_work stores, plus the WAL sibling of the one that is a DB.
    running = running_work_store_paths()
    assert running, "running_work_store_paths() resolved nothing to fingerprint"
    for path in running:
        assert_covered("running_work store", path)
    assert_covered("running_work WAL sibling", f"{running[-1]}-wal")
    # 3 — the chat SessionDB and its WAL siblings.
    assert_covered("chat SessionDB", chat_session_db_path())
    assert_covered("chat SessionDB WAL sibling", f"{chat_session_db_path()}-wal")
    # 4 — the profile inputs agents_readiness reads.
    assert_covered("profiles root", _get_profiles_root())
    # 5 — both config authorities (they are different files in production).
    assert_covered("ambient config", get_config_path())
    assert_covered("root config", harness_root_config_path())
    # 6 — every skill registry root the resolver would walk.
    for root in get_all_skills_dirs():
        assert_covered("skill registry root", root)
    # 7 — the event-rotation lane.
    assert_covered("event rotation manifest", paths.events_manifest_path())


def test_the_chat_sessiondb_class_is_resolved_through_its_own_authority(
    isolate_agent_runtime_root, monkeypatch
):
    """Class 3 is consulted independently, even when it usually COINCIDES with class 2.

    Worth its own case because the coverage gate above cannot see it: under the
    test harness (and on most installs) ``chat_session_db_path()`` and
    ``running_work_store_paths()[-1]`` resolve to the SAME ``state.db`` under the
    head home, so deleting the chat-scope class entirely left every other
    assertion green. They are not the same question, and the divergence between
    them is a shipped defect: a bare ``SessionDB()`` keyed the serve read cache on
    ``HERMES_HOME/state.db`` while every chat write went to the RESOLVED chat
    scope, freezing Chat History for the life of the serve process (defect D1,
    ``chat-session-presence-authority.md``).

    So the probe points the chat-scope authority somewhere unmistakably its own
    and asserts the fingerprint followed it.
    """

    from agent_runtime import chat_session_scope

    distinct = isolate_agent_runtime_root.parent / "chat-scope-elsewhere" / "state.db"
    monkeypatch.setattr(chat_session_scope, "chat_session_db_path", lambda: distinct)
    fingerprint = core_cache.build_input_fingerprint()
    assert fingerprint is not None
    covered = set(core_cache.iter_fingerprint_paths(fingerprint))
    assert str(distinct) in covered, (
        "the fingerprint did not follow chat_session_db_path(); it is watching "
        "whichever database ambient resolution hands it, which is defect D1"
    )
    assert f"{distinct}-wal" in covered


def test_the_cache_excludes_its_own_writes_from_the_fingerprint(
    isolate_agent_runtime_root
):
    """The cache must not invalidate the key it just persisted.

    Learned the hard way, and this is the exact shape that bit: on a store root
    the cache's own write CREATES, an entry recorded as "absent" before the write
    reads as "present" after it — so the write that persisted a core guaranteed
    the next process would miss it, on every single boot. The fix is that a
    directory contributes its path and never its existence-or-mtime, because a
    directory's timestamp also moves for the children this fingerprint
    deliberately excludes (``locks/``, the serve registry, staged temp files).

    Nothing is built here on purpose: the ONLY filesystem change between the two
    stat sets is the cache's own pair, so an inequality has exactly one cause.
    """

    assert not isolate_agent_runtime_root.exists(), (
        "this case needs a virgin store root — the cache's own directory must be "
        "the first thing written into it"
    )
    before = core_cache.build_input_fingerprint()
    assert before is not None
    assert core_cache.write_back({"parity": {"watermark": {"event_offset": 0}}}) is True
    assert core_cache.core_path().exists()
    after = core_cache.build_input_fingerprint()
    assert after is not None
    assert before.digest == after.digest, (
        "writing the cache changed the fingerprint of the inputs, so a cache "
        "write can never be validated by the process that reads it next"
    )
    # And a second, ordinary re-write of the existing pair is equally inert.
    assert core_cache.write_back({"parity": {"watermark": {"event_offset": 1}}}) is True
    again = core_cache.build_input_fingerprint()
    assert again is not None and again.digest == before.digest


def test_an_unresolvable_input_authority_refuses_the_cache(
    isolate_agent_runtime_root, seeded_cache, monkeypatch
):
    """A missing answer is a REFUSAL, never a quiet "nothing changed" (ruling #45).

    ``running_work_store_paths`` returns an empty tuple when it cannot resolve a
    home. Reading that as "there is nothing to watch" is how a HUD comes to claim
    three processes are running twenty seconds after they all exited.
    """

    from agent_runtime import running_work as running_work_mod

    monkeypatch.setattr(running_work_mod, "running_work_store_paths", lambda: ())
    assert core_cache.build_input_fingerprint() is None
    _new_context()
    core = build_snapshot(build_info={"caller": "probe"})
    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT


def test_a_core_whose_bytes_the_sidecar_does_not_describe_is_refused(
    isolate_agent_runtime_root, seeded_cache
):
    """The sidecar binds to the core's BYTES, not to its path.

    A half-replaced pair, a hand-edited core, or a rollback that restored an
    older ``core.json`` beside a newer sidecar would otherwise be
    indistinguishable from a valid pair — and this one is refused OUTRIGHT
    rather than served stale, because nothing here can say what an unbound core
    contains.
    """

    tampered = _persisted_core()
    tampered["workspaces"] = []
    atomic_json_write(core_cache.core_path(), tampered, indent=None)

    read = core_cache.read_persisted_core()
    assert read.matched is False
    assert read.reason == core_cache.DEMOTE_CORE_DIGEST_MISMATCH
    assert read.core is None
    _new_context()
    assert core_cache.take_stale_first_core(caller="probe") is None


# --------------------------------------------------------------------------- #
# The projection a persisted core must not bless (RD-H4's sibling defect)
# --------------------------------------------------------------------------- #
def test_the_persisted_core_reports_the_actor_files_it_could_not_read(
    isolate_agent_runtime_root
):
    """A persisted core must not fingerprint-bless a silently-shortened office.

    ``_read_actor_dir`` skips a file it cannot decode and returns the rest, so
    the snapshot's office row arrived already SHORTENED and computed its own
    truncation from the shortened length — answering 0. EG-1.5 fixed that one
    seam over in ``serve_rpc._office_projection``; this is the snapshot's copy of
    the same defect, and it matters more here because the row is now PERSISTED:
    a projection that under-reported its completeness would be written back as
    fingerprint-blessed truth and served to every later boot.

    *Mutation:* hand ``office_summary_row`` a bare ``list_actors`` list (or
    hardcode ``actors_unreadable=0``).
    """

    _seed_workspace("alpha-one")
    _write_office_by_hand(OFFICE_WORKSPACE)
    broken = paths.office_actors_dir(OFFICE_WORKSPACE) / "shredded.json"
    broken.write_text("{not json", encoding="utf-8")

    core = build_snapshot(build_info={"caller": "probe"})
    row = (core.get("offices") or {})[OFFICE_WORKSPACE]
    assert row["actors_unreadable"] == 1, row
    assert row["actor_count"] == 1, row

    persisted = (_persisted_core().get("offices") or {})[OFFICE_WORKSPACE]
    assert persisted["actors_unreadable"] == 1, (
        "the persisted core claims more completeness than the build had, so a "
        "cache-served boot would report a whole desk as absent rather than "
        "unreadable"
    )


# --------------------------------------------------------------------------- #
# ML-10 — the refusals and the non-convergence become RECEIPTS
# --------------------------------------------------------------------------- #
#
# The two ways this lane fails QUIETLY rather than wrongly. Neither changes a
# decision: a bound refusal still means never-cache, and a process whose keys
# never settle still demotes exactly as often as before. What changes is that
# both are now countable on the same channel the shadow lane reports divergence
# on, so "the cache is off for this install" and "the cache is buying nothing"
# stop being invisible to a census.
#
# WHAT MAKES THESE NON-VACUOUS. Every case drives a value the receipt must have
# COMPUTED — a store root, a skill root, an oscillating path — twice, with two
# distinct values, so a constant satisfies at most one of them; and each pairs
# that with a discriminator the mutant cannot also pass (the stable paths that
# must NOT be named; the exact build count at which the receipt appears; the
# converging store that must stay silent).


def _receipt_lines(caplog, token: str) -> list[str]:
    return [
        line
        for line in (record.getMessage() for record in caplog.records)
        if f"snapshot_core_cache {token}" in line
    ]


def _fake_key(entries: list[tuple[str, int, int]]) -> core_cache.CoreFingerprint:
    """A stat set the TEST owns, so a case can drive an input that oscillates.

    The digest is computed here rather than borrowed from
    ``build_input_fingerprint`` on purpose: the module only ever compares digests
    for equality, and a test that reused production's formula would pass just as
    happily if that formula stopped depending on the entries at all.
    """

    ordered = tuple(
        sorted(core_cache.FingerprintEntry(path, mtime, size) for path, mtime, size in entries)
    )
    digest = hashlib.sha256(repr(ordered).encode("utf-8")).hexdigest()
    return core_cache.CoreFingerprint(ordered, digest)


def test_an_entry_bound_refusal_is_receipted_not_just_warned(
    tmp_path, monkeypatch, caplog
):
    """A bound refusal turns the cache OFF for a whole install. It says which one.

    Reaching the walk bound is not a partial answer — the fingerprint becomes
    ``None`` and every caller must read that as never-cache — so an install that
    trips it pays the full ~20 s build on every boot forever. The only thing that
    reported it was a WARNING sentence carrying a NUMBER and no root, which an
    operator can read only if they are already reading, and which a census cannot
    count at all.

    Two distinct store roots are driven so a receipt that named a constant (or
    named the bound instead of the tree) satisfies at most one of them.

    *Kill:* restore the bare ``logger.warning`` — it never carried a root, so no
    receipt exists that carries the driven one.
    """

    monkeypatch.setattr(core_cache, "MAX_FINGERPRINT_ENTRIES", 3)
    receipts: list[str] = []
    for name in ("root-alpha", "root-beta"):
        root = tmp_path / name / "agent-runtime"
        (root / "workspaces").mkdir(parents=True)
        for index in range(6):
            (root / "workspaces" / f"ws_{index}.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
        assert str(paths.store_root()) == str(root)

        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
            refused = core_cache.build_input_fingerprint()

        assert refused is None, (
            "the refusal semantics moved: reaching the bound must still refuse "
            "the whole fingerprint, which is what makes 'never cache' safe"
        )
        lines = _receipt_lines(caplog, core_cache.RECEIPT_FINGERPRINT_REFUSED)
        assert len(lines) == 1, lines
        assert f"root={root}" in lines[0], lines[0]
        assert f"reason={core_cache.REFUSAL_ENTRIES_EXCEEDED}" in lines[0], lines[0]
        assert f"scope={core_cache.REFUSAL_SCOPE_STORE_ROOT}" in lines[0], lines[0]
        assert "bound=3" in lines[0], lines[0]
        receipts.append(lines[0])

    assert receipts[0] != receipts[1], (
        "both roots produced the same receipt, so the root on it is a constant "
        "and names nothing"
    )


def test_a_skill_root_bound_refusal_names_the_SKILL_root(
    isolate_agent_runtime_root, tmp_path, monkeypatch, caplog
):
    """The second bound is a different number over a different tree — and says so.

    A skill root that blows its per-root bound is not a skill registry, and the
    refusal is the same never-cache refusal. But an operator handed only "a bound
    was exceeded" cannot tell whether to go look at a store root of hundreds of
    thousands of files or at one mis-configured external skills directory. The
    scope and the root are the fix, and this case pins that the receipt names the
    tree that actually refused rather than the one the other arm names.
    """

    from agent import skill_utils

    skill_root = tmp_path / "not-a-skill-registry"
    skill_root.mkdir()
    for index in range(6):
        (skill_root / f"pack_{index}.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skill_root])
    monkeypatch.setattr(core_cache, "MAX_SKILL_ENTRIES_PER_ROOT", 3)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        assert core_cache.build_input_fingerprint() is None

    lines = _receipt_lines(caplog, core_cache.RECEIPT_FINGERPRINT_REFUSED)
    assert len(lines) == 1, lines
    assert f"scope={core_cache.REFUSAL_SCOPE_SKILL_ROOT}" in lines[0], lines[0]
    assert f"root={skill_root}" in lines[0], lines[0]
    assert "bound=3" in lines[0], lines[0]
    assert str(paths.store_root()) not in lines[0], (
        "the skill-root refusal named the STORE root, so the receipt's root is "
        "whatever the first arm happened to have rather than the tree that "
        "refused"
    )


@pytest.mark.parametrize(
    "oscillating_name",
    ["persona_instances/inst_probe.json", "chat_scope/state.db-wal"],
)
def test_an_input_that_oscillates_every_build_is_named(
    isolate_agent_runtime_root, monkeypatch, caplog, oscillating_name
):
    """A cache that can never converge is silent by construction. This ends that.

    The shape is the one ``write_back`` already names: the build is not a pure
    reader, so an input the build itself rewrites on every pass makes the
    pre-build key describe a store that no longer exists by the time the build
    ends — and the NEXT build's key disagrees again, forever. Every process then
    demotes, every process rebuilds, and the lane costs a write per build while
    buying nothing. Nothing above notices: each demote is individually
    legitimate.

    The oscillating path is driven with two distinct values (the parametrization)
    and the receipt must NAME it, which a receipt that only counts cannot do. The
    stable paths in the same stat set must NOT be named — a mutant that dumps the
    whole key instead of the diff passes the naming probe and fails this one.

    *Kill 1:* drop the diff-naming. The mutant cannot name a path it never
    computed. *Kill 2:* drop the emission. There is no receipt at all. *Kill 3:*
    emit on every mismatch (bound of 1) or never (bound past the drive) — the
    per-build count below pins the exact build the receipt appears on.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:ml10:clean")
    root = isolate_agent_runtime_root
    moving = str(root / oscillating_name)
    stable = [
        (str(root / "workspaces" / "ws_stable.json"), 11, 12),
        (str(root / "events.jsonl"), 21, 22),
    ]
    core = {"parity": {"watermark": {"event_offset": 0}}}

    counts: list[int] = []
    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        for pass_no in range(1, core_cache.NEVER_CONVERGED_BUILDS + 2):
            key = _fake_key([*stable, (moving, pass_no, pass_no)])
            assert core_cache.write_back(core, fingerprint=key) is True
            counts.append(len(_receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED)))

    assert counts == [0, 0, 0, 1], (
        "the receipt did not appear on exactly the build the bound names "
        f"(NEVER_CONVERGED_BUILDS={core_cache.NEVER_CONVERGED_BUILDS}); counts "
        f"per write-back were {counts}"
    )
    line = _receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED)[0]
    assert f"builds={core_cache.NEVER_CONVERGED_BUILDS}" in line, line
    assert f"diff_scope={core_cache.DIFF_SCOPE_EVERY_PASS}" in line, line
    assert "changed=1" in line, line
    assert moving in line, line
    for path, _mtime, _size in stable:
        assert path not in line, (
            "the receipt named an input that never moved, so it is reporting the "
            f"stat set rather than the diff: {line}"
        )


def test_a_settling_store_emits_no_never_converged_receipt(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """The no-change case: a cache that converges says NOTHING.

    This is the discriminator the always-emit mutant dies on, and it is also the
    reason the bound is the measured virgin-root convergence rather than 1: a
    cold store legitimately disagrees with itself ONCE (the build materializes
    persona-instance rows and creates the chat SessionDB, which the pre-build key
    could not have described), and a receipt that fired there would be crying
    wolf on every fresh install.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:ml10:clean")
    root = isolate_agent_runtime_root
    cold = _fake_key([(str(root / "workspaces" / "ws_stable.json"), 11, 12)])
    settled = _fake_key(
        [
            (str(root / "workspaces" / "ws_stable.json"), 11, 12),
            (str(root / "chat_scope" / "state.db"), 31, 32),
        ]
    )
    core = {"parity": {"watermark": {"event_offset": 0}}}

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        for key in (cold, settled, settled, settled, settled, settled):
            assert core_cache.write_back(core, fingerprint=key) is True

    assert _receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED) == [], (
        "a store that settled after the ONE disagreement a cold build owes was "
        "reported as never converging"
    )


def test_a_real_build_that_settles_emits_no_never_converged_receipt(
    isolate_agent_runtime_root, caplog
):
    """The same no-change case, driven by real builds instead of driven keys.

    The unit case above proves the counter; this proves the NUMBER against the
    thing it was measured on. Four consecutive real builds in one process: the
    virgin store disagrees with itself early (that is ``write_back``'s named
    consequence) and then settles, so the bound must not be reached. If this ever
    reds, the receipt is telling the truth and the finding is a real
    non-converging input — widen the closure; do not raise the bound.
    """

    _seed_workspace("alpha-one")
    _new_context()
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        for _ in range(core_cache.NEVER_CONVERGED_BUILDS + 1):
            build_snapshot(build_info={"caller": "probe"})

    # The silence is only evidence if the drive REACHED the counter: every one of
    # those builds has to have persisted a key for the streak to have had
    # anything to disagree about. A build that stopped writing back would
    # otherwise make this case pass by doing nothing.
    writes = [
        line
        for line in (record.getMessage() for record in caplog.records)
        if "snapshot_core_cache_write ok=true" in line
    ]
    assert len(writes) == core_cache.NEVER_CONVERGED_BUILDS + 1, writes
    assert _receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED) == []


def test_a_never_converged_receipt_that_cannot_diff_says_so_in_its_own_words(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """C16's lesson: the arm that cannot answer must not borrow another's sentence.

    A key whose entries are unavailable still disagrees — the digests differ —
    but nothing can be named from it. Reporting that as an empty diff, or reusing
    the oscillation wording with nothing after ``diff=``, would read to a census
    exactly like "we looked and nothing moved". It is typed instead.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:ml10:clean")
    core = {"parity": {"watermark": {"event_offset": 0}}}

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        for pass_no in range(1, core_cache.NEVER_CONVERGED_BUILDS + 2):
            entryless = core_cache.CoreFingerprint((), f"digest-without-entries-{pass_no}")
            assert core_cache.write_back(core, fingerprint=entryless) is True

    lines = _receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED)
    assert len(lines) == 1, lines
    assert f"diff={core_cache.DIFF_UNAVAILABLE}" in lines[0], lines[0]
    assert f"diff_reason={core_cache.DIFF_UNAVAILABLE_NO_ENTRIES}" in lines[0], lines[0]
    assert f"diff_scope={core_cache.DIFF_SCOPE_NONE}" in lines[0], lines[0]
    assert core_cache.DIFF_SCOPE_EVERY_PASS not in lines[0], lines[0]


# --------------------------------------------------------------------------- #
# The streak survives a process boundary (MC-3 / P4 arm 3, finding A2)
# --------------------------------------------------------------------------- #
# The receipt above fired on the FOURTH consecutive disagreeing write-back of ONE
# process. Measured: boots write back once (07:59, 08:04) or twice (05:33 — gen 1
# prewarm-led, gen 2 hub-led, streak 1). So it was unreachable on every boot
# shape there is, and the 05:33 pair — which IS the self-perturbation it was
# written to expose — could never be reported. A process boundary is not a
# convergence event.
#
# WHAT MAKES THESE NON-VACUOUS. The mutant is "fire more readily", so the two
# cases that must stay SILENT carry as much weight as the one that must speak: a
# legitimate non-agreement (an upgrade) must not seed, and a store that settles
# must both stay quiet and drop what it was holding. Each firing case drives the
# receipt to an exact BUILD COUNT and an exact PATH, neither of which a mutant
# that simply lowered the bound can produce.

_BOOT_CORE = {"parity": {"watermark": {"event_offset": 0}}}


def _boot_keys(root, *, passes: int) -> tuple[list, str, str]:
    """One key per simulated boot, with exactly ONE runtime-owned input moving."""

    stable = str(root / "workspaces" / "ws_stable.json")
    moving = str(root / "dispatch_delivery_drain.json")
    keys = [
        _fake_key([(stable, 11, 12), (moving, index, index)]) for index in range(passes)
    ]
    return keys, moving, stable


def _reboot() -> None:
    """What the next serve child sees: no memory, and the pair still on disk."""

    core_cache.reset_process_state()


def test_a_store_that_never_converges_across_boots_reaches_the_receipt(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """A2: the count continues across the boundary, so the boot shape can fire it.

    A baseline write-back establishes the pair, then each simulated boot
    disagrees with the one before it. With the threshold left at the measured
    virgin-root convergence — 3, deliberately NOT moved — the receipt appears on
    the third disagreeing boot instead of never.

    The per-boot count is the discriminator: a mutant that seeded eagerly, or
    that lowered the bound, fires on a different boot than this pins.

    *Kill:* restore the per-process early return. No boot ever has anything to
    disagree with, every streak is zero, and the receipt never appears.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:mc3:clean")
    keys, _moving, _stable = _boot_keys(isolate_agent_runtime_root, passes=4)

    counts: list[int] = []
    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        for key in keys:
            _reboot()
            assert core_cache.write_back(_BOOT_CORE, fingerprint=key) is True
            counts.append(len(_receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED)))

    assert counts == [0, 0, 0, 1], (
        "the receipt did not appear on exactly the boot the bound names "
        f"(NEVER_CONVERGED_BUILDS={core_cache.NEVER_CONVERGED_BUILDS}); the "
        f"baseline boot plus three disagreeing boots produced {counts}"
    )


def test_the_cross_boot_receipt_counts_the_streak_and_names_the_moving_input(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """Two claims, two kills: the count is right AND the paths are right.

    ``builds=`` is the fact that says the lane has been buying nothing since
    before this process started; the paths are the fact an operator can act on.
    A seed that carried only the digest would produce the first and not the
    second, which is precisely why they are asserted apart.

    The scope must read ``last_pair`` and NOT ``every_pass``: the streak began in
    an earlier process, so no intersection over its whole length was ever
    observed here, and C22(i) reserves ``every_pass`` for measured
    self-perturbation. Claiming it from one observed pass would inflate the arm
    an operator is meant to act on.

    *Kill A:* the per-process early return — no receipt at all. *Kill B:* seed
    the digest but not the entries — ``builds=3`` still lands and the diff
    collapses to ``diff_scope=none diff_reason=no_entries diff=diff_unavailable``,
    naming nothing.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:mc3:clean")
    keys, moving, stable = _boot_keys(isolate_agent_runtime_root, passes=4)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        for key in keys:
            _reboot()
            assert core_cache.write_back(_BOOT_CORE, fingerprint=key) is True

    lines = _receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED)
    assert len(lines) == 1, lines
    line = lines[0]
    assert f"builds={core_cache.NEVER_CONVERGED_BUILDS}" in line, line
    assert moving in line, (
        f"the cross-boot receipt named no path, so the streak carried a digest "
        f"and nothing an operator could go widen the closure over: {line}"
    )
    assert stable not in line, (
        f"the receipt named an input that never moved: {line}"
    )
    assert f"diff_scope={core_cache.DIFF_SCOPE_LAST_PAIR}" in line, line
    assert core_cache.DIFF_SCOPE_EVERY_PASS not in line, (
        "a streak that began in an earlier process claimed an intersection over "
        f"passes this one never observed: {line}"
    )


def test_a_legitimate_non_agreement_does_not_seed_the_streak(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """An upgrade is not an oscillation, and must not be reported as one.

    A ``build_stamp_mismatch`` says the operator upgraded; a ``contract_mismatch``
    says the schema moved. Neither is evidence about whether the store settles,
    and a receipt fired on them would be a WARNING at an operator with a
    perfectly healthy install — the expensive direction of error for a
    diagnostic whose whole value is that it speaks only when something is wrong.

    The second half of the case is the anti-vacuity proof: the IDENTICAL key
    sequence, run under one unchanging stamp, does fire. So the silence above is
    a decision about the reason, not a fixture that could never have reached the
    receipt.

    *Kill:* seed on any demote reason.
    """

    stamp = {"value": "probe:mc3:v0"}
    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: stamp["value"])
    keys, _moving, _stable = _boot_keys(isolate_agent_runtime_root, passes=4)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        for index, key in enumerate(keys):
            _reboot()
            # Every boot runs a DIFFERENT build than the pair it finds, so every
            # persisted pair fails on ``build_stamp`` before the digest is ever
            # compared.
            stamp["value"] = f"probe:mc3:v{index}"
            assert core_cache.write_back(_BOOT_CORE, fingerprint=key) is True

    assert _receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED) == [], (
        "a run of ordinary upgrades was reported as a store that never "
        "converges, which fires a warning at a healthy install"
    )

    caplog.clear()
    stamp["value"] = "probe:mc3:steady"
    core_cache.core_path().unlink(missing_ok=True)
    core_cache.sidecar_path().unlink(missing_ok=True)
    core_cache.entries_path().unlink(missing_ok=True)
    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        for key in keys:
            _reboot()
            assert core_cache.write_back(_BOOT_CORE, fingerprint=key) is True

    assert len(_receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED)) == 1, (
        "the same key sequence under one steady build stamp did NOT fire, so the "
        "silence above proves nothing about the reason the seed refused"
    )


def test_a_settled_lane_says_nothing_and_holds_no_stat_set(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """The retention rule survives the seed: a settled process holds nothing.

    An entry list on a live store is tens of thousands of triples. It is kept
    only while a streak is LIVE, and dropped the moment two write-backs agree —
    a seeded streak inherits that discipline rather than being an exception to
    it, which is the difference between a diagnostic and a leak.

    The drive reaches the agreement branch with something actually retained: a
    disagreeing write-back first (so the module IS holding a stat set), then the
    same key again. A case that agreed from the start would pass while holding
    nothing for the wrong reason.

    *Kill:* retain the entries unconditionally through the agreement branch.
    """

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:mc3:clean")
    keys, _moving, _stable = _boot_keys(isolate_agent_runtime_root, passes=2)

    _reboot()
    assert core_cache.write_back(_BOOT_CORE, fingerprint=keys[0]) is True

    _reboot()
    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        # Disagrees with the previous boot: the streak is live and the stat set
        # is being held.
        assert core_cache.write_back(_BOOT_CORE, fingerprint=keys[1]) is True
        assert core_cache._streak_entries, (
            "the fixture never reached a live streak, so the drop below would be "
            "true of a module that had nothing to drop"
        )
        # ...and now it settles.
        assert core_cache.write_back(_BOOT_CORE, fingerprint=keys[1]) is True

    assert core_cache._streak_entries == (), (
        "a settled lane is still holding the stat set of a streak that ended, so "
        "every healthy process now carries a second copy of the fingerprint for "
        "a diagnostic that is not going to fire"
    )
    assert _receipt_lines(caplog, core_cache.RECEIPT_NEVER_CONVERGED) == [], (
        "a lane that settled was reported as never converging"
    )


# --------------------------------------------------------------------------- #
# One consult per boot, not one per rider (MC-1 / P5)
# --------------------------------------------------------------------------- #
def _count_lane_work(monkeypatch) -> tuple[list[str], list[str]]:
    """Count the two expensive halves of a consult, from OUTSIDE production.

    The walk and the pair read are counted where nothing in ``core_cache`` can
    set the counter — the ``_CountingStores`` pattern at the top of this file. A
    landing that claimed to share a consult while still walking per rider cannot
    forge these numbers.
    """

    walks: list[str] = []
    reads: list[str] = []
    real_walk = core_cache.build_input_fingerprint
    real_read = core_cache._read_pair

    def counted_walk():
        walks.append("walk")
        return real_walk()

    def counted_read():
        reads.append("read")
        return real_read()

    monkeypatch.setattr(core_cache, "build_input_fingerprint", counted_walk)
    monkeypatch.setattr(core_cache, "_read_pair", counted_read)
    return walks, reads


def test_a_boots_riders_share_one_walk_and_one_core_read(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, shadow_requests
):
    """The boot asks one question, so it walks the store once.

    A serve boot's shape: the stream's stale-first read, then the prewarm, hub
    and cli riders' consults. Four askers, one moment, one answer — measured at
    ~300-355 ms per walk on the operator's drive, all of it on the boot's
    critical path and all of it identical.
    """

    walks, reads = _count_lane_work(monkeypatch)
    _new_context()

    # Frame 0: the stream lane's stale-first probe. The key MATCHES here, so it
    # declines to paint a stale copy — and pays for the walk the riders reuse.
    assert core_cache.take_stale_first_core(caller="hub") is None
    for caller in ("prewarm", "hub", "cli"):
        decision = core_cache.consult(caller=caller)
        assert decision.core is not None, (
            f"the {caller} rider was not served the cache, so this case is "
            "counting the wrong path"
        )

    assert len(walks) == 1, (
        f"a four-asker boot walked the store {len(walks)} times; the riders are "
        "each re-deciding a question the boot had already answered"
    )
    assert len(reads) == 1, (
        f"a four-asker boot read the persisted pair {len(reads)} times"
    )


def test_every_rider_still_emits_its_own_receipt(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, caplog, shadow_requests
):
    """Sharing the computation must not merge the ACCOUNT of it.

    EG-2.1's receipts are the instrument this lane's acceptance is measured
    with, and the census unit is per caller. A boot that walked once but logged
    once would make three riders indistinguishable from one.
    """

    _count_lane_work(monkeypatch)
    _new_context()
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        for caller in ("prewarm", "hub", "cli"):
            assert core_cache.consult(caller=caller).core is not None

    hits = [
        line
        for line in _receipt_lines(caplog, f"core_source={core_cache.CORE_SOURCE_CACHE}")
        if "stale=true" not in line
    ]
    assert len(hits) == 3, f"expected one hit receipt per rider, got: {hits}"
    for caller in ("prewarm", "hub", "cli"):
        assert any(f"caller={caller}" in line for line in hits), (
            f"no receipt names the {caller} rider: {hits}"
        )


def test_each_rider_is_handed_its_own_core(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, shadow_requests
):
    """Shared judgement, private payload.

    ``label_core`` stamps provenance IN PLACE. One shared dict would let the
    third rider's label — and its refreshed freshness anchor — land on the core
    the first rider had already handed to its frame. The build coalescer
    deep-copies its result for exactly this reason.
    """

    _count_lane_work(monkeypatch)
    _new_context()
    first = core_cache.consult(caller="prewarm").core
    second = core_cache.consult(caller="hub").core
    assert first is not None and second is not None
    assert first is not second, (
        "two riders were handed the SAME core object; one rider's in-place "
        "provenance stamp can now reach another rider's already-emitted frame"
    )
    first["workspaces"] = []
    assert second.get("workspaces") != [], (
        "mutating one rider's core changed another rider's core"
    )


def test_a_rewritten_persisted_pair_invalidates_the_shared_consult(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, shadow_requests
):
    """The window is bounded by the pair's own stat triples, not by hope.

    A write-back — this process's or another's — moves both files through
    ``atomic_json_write``, and a rename always moves mtime. The next asker must
    therefore see the NEW core, not an answer computed over the old one.
    """

    _count_lane_work(monkeypatch)
    _new_context()
    served = core_cache.consult(caller="prewarm").core
    assert served is not None
    assert _served_workspace_name(served) == "alpha-one"

    replacement = _persisted_core()
    for row in replacement.get("workspaces") or []:
        if isinstance(row, dict) and row.get("id") == WORKSPACE_ID:
            row["name"] = "alpha-two"
    assert core_cache.write_back(replacement) is True

    again = core_cache.consult(caller="hub").core
    assert again is not None
    assert _served_workspace_name(again) == "alpha-two", (
        "the second consult answered from a memo the persisted pair had already "
        "moved out from under; the window is not bounded by the pair's stats"
    )


def test_the_build_leader_reuses_the_consults_key_instead_of_rewalking(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, shadow_requests
):
    """The fifth walk of a boot was the leader restating its own consult.

    The pre-build key must stay PRE-build — ``write_back``'s direction argument
    — and the consult's key already is: it was taken before this build started.
    Reusing it can only make the key OLDER, which costs the next process a
    rebuild it did not strictly need, never a served core missing a write.
    """

    walks, _ = _count_lane_work(monkeypatch)
    _rewrite_workspace_name("alpha-two")
    _new_context()

    core = build_snapshot(build_info={"caller": "prewarm"})
    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT, (
        "this case needs the MISS path, where a leader actually builds"
    )
    assert len(walks) == 1, (
        f"the boot walked the store {len(walks)} times: the leader took its own "
        "stat set milliseconds after its consult had taken one over the same "
        "store"
    )


def test_a_disarmed_lane_takes_its_own_key(
    isolate_agent_runtime_root, seeded_cache, monkeypatch, shadow_requests
):
    """The window closes with the lane, and later builds are ordinary builds.

    A memo that outlived the boot would let the process's SECOND build persist a
    key describing the store as it was before its FIRST build ran — the unsafe
    direction, and the one thing this sharing must never do.
    """

    _new_context()
    assert core_cache.consult(caller="prewarm").core is not None
    core_cache.note_full_build_completed()
    assert core_cache.lane_armed() is False

    walks, _ = _count_lane_work(monkeypatch)
    key = core_cache.pre_build_fingerprint()
    assert key is not None
    assert len(walks) == 1, (
        "a build after the lane disarmed answered from the boot's memo instead "
        "of stat'ing the store it is about to describe"
    )
