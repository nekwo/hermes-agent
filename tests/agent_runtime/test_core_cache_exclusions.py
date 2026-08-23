"""MC-1 / P1 — the core cache's exclusion set, proved against its actual writers.

WHAT THIS FILE EXISTS TO STOP
=============================

``core_cache._EXCLUDED_STORE_ENTRIES`` is a DENYLIST over the agent-runtime
store root: everything not named in it is fingerprinted. A name in it that no
writer produces is therefore not a harmless typo — it is a file left INSIDE the
key, and if that file moves on a cadence the cache can never hit.

That is not hypothetical. The set shipped with ``"drain_state.json"``, annotated
"per ``dispatch_delivery.DRAIN_STATE_FILENAME``", while the constant read
``dispatch_delivery_drain.json``. The exclusion named a file that has never
existed; the real drain mirror — rewritten every 60 s for the life of a serve —
stayed in the key. ``serve_socket.owner.json`` and ``serve_socket.lock`` were
absent from the set entirely, though ``serve_socket``'s own doctrine requires
both to be out of every freshness fingerprint. Between them, no boot's key could
describe the store the NEXT boot stat'd, and the lane demoted
``fingerprint_mismatch`` on every same-commit boot from the day it shipped.

WHAT MAKES THESE NON-VACUOUS
============================

The mutant this file is written against is a denylist that agrees with a
restated list rather than with the filesystem. So the load-bearing gate does not
compare the set against a second list of names — it DRIVES THE REAL WRITERS and
reads the store root back, so the names under test are produced by the code that
produces them in production. A writer that renames its file breaks this gate on
the rename, which is the whole point of naming a file by its owner's constant.

The key-stability gates are parametrized ONE WRITER PER CASE, so dropping any
single exclusion reds exactly the case whose writer produced it — a file-level
"something moved" would let one kill stand in for five claims.
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Iterator

import pytest

from agent_runtime import (
    core_cache,
    dispatch_delivery,
    locks,
    paths,
    serve_auth,
    serve_registry,
    serve_socket,
)
from agent_runtime.office_store import OfficeStore
from agent_runtime.store import WorkspaceStore


# --------------------------------------------------------------------------- #
# Drivers — the REAL writers, each left in its post-write state inside the block
# --------------------------------------------------------------------------- #
def _top_level_names() -> set[str]:
    try:
        return {entry.name for entry in os.scandir(paths.store_root())}
    except OSError:
        return set()


@contextlib.contextmanager
def _drain_mirror() -> Iterator[None]:
    """``dispatch_delivery``'s 60-second telemetry mirror, written twice.

    Twice on purpose: one write proves the name, and the second proves the
    REWRITE — which is the shape that actually costs the cache its hit, because
    ``atomic_json_write`` moves mtime even for byte-identical content.
    """

    path = dispatch_delivery._drain_state_path(paths.store_root())
    assert path is not None, "the drain mirror could not resolve a store root"
    assert dispatch_delivery._write_drain_state(path, live=True) is True
    assert dispatch_delivery._write_drain_state(path, live=True) is True
    assert path.exists(), (
        f"the drain mirror reported success but wrote nothing at {path} — this "
        "driver would then prove the exclusion of a file that is never written"
    )
    yield


@contextlib.contextmanager
def _socket_owner() -> Iterator[None]:
    """``serve_socket``'s ownership lock plus its identity sidecar.

    The sidecar is published TWICE and the lock is still held inside the block:
    ``release()`` drops the sidecar, so releasing before the probe would let an
    appear-then-vanish cancel itself out and the exclusion would never be tested.
    """

    lock = serve_socket.SocketOwnerLock(paths.store_root())
    result = lock.acquire()
    assert result.acquired, f"could not take the socket ownership lock: {result.outcome}"
    try:
        lock.publish_owner({"pid": os.getpid(), "port": 1})
        lock.publish_owner({"pid": os.getpid(), "port": 2})
        assert serve_socket.socket_lock_path(paths.store_root()).exists()
        assert serve_socket.socket_owner_path(paths.store_root()).exists(), (
            "publish_owner is best-effort and swallows its errors, so a sidecar "
            "that was never written would make this driver vacuous"
        )
        yield
    finally:
        lock.release()


@contextlib.contextmanager
def _serve_instances() -> Iterator[None]:
    """``serve_registry``'s per-pid announcement — the churn a boot/exit makes."""

    root = paths.store_root()
    registration = serve_registry.register_serve_instance(root, transport="stdio")
    try:
        assert registration.registered, "the serve registry announced nothing"
        assert serve_registry.serve_instance_path(root, os.getpid()).exists()
        yield
    finally:
        serve_registry.unregister_serve_instance(root)


@contextlib.contextmanager
def _serve_auth_token() -> Iterator[None]:
    """``serve_auth``'s per-root token, minted at first boot."""

    root = paths.store_root()
    status = serve_auth.ensure_token(root)
    assert not status.state.startswith("error:"), status.state
    assert serve_auth.serve_auth_token_path(root).exists()
    yield


@contextlib.contextmanager
def _harness_lock() -> Iterator[None]:
    """A real harness lock, held across the probe.

    Locks are created and removed INSIDE a build, so a lock in the stat set
    would make a build's own locking flip the key it had just written.
    """

    with locks.events_lock():
        assert paths.lock_dir().exists()
        yield


_DRIVERS = {
    "drain_mirror": _drain_mirror,
    "socket_owner": _socket_owner,
    "serve_instances": _serve_instances,
    "serve_auth_token": _serve_auth_token,
    "harness_lock": _harness_lock,
}


# --------------------------------------------------------------------------- #
# 1. Every top-level name a boot writer PRODUCES is excluded
# --------------------------------------------------------------------------- #
def test_every_store_root_name_a_boot_writer_produces_is_excluded(
    isolate_agent_runtime_root,
):
    """The names under test come from the writers, never from a restated list.

    This is the gate the shipped defect would have failed: it does not ask "is
    ``drain_state.json`` in the set", it asks "the drain mirror just wrote
    something into the store root — is THAT in the set". A denylist checked
    against a second copy of the same list can agree with itself forever while
    disagreeing with the filesystem.
    """

    paths.store_root().mkdir(parents=True, exist_ok=True)
    before = _top_level_names()
    with contextlib.ExitStack() as stack:
        for driver in _DRIVERS.values():
            stack.enter_context(driver())
        minted = _top_level_names() - before

    assert minted, (
        "no boot writer produced a store-root entry, so this gate proved "
        "nothing — the drivers are broken, not the exclusion set"
    )
    unexcluded = sorted(
        name for name in minted if name not in core_cache._EXCLUDED_STORE_ENTRIES
    )
    assert not unexcluded, (
        "a boot writer produced store-root entries that the core cache's "
        f"fingerprint still watches: {unexcluded}. Every one of them moves for "
        "reasons the read-model core does not depend on, so each is a guaranteed "
        "fingerprint_mismatch on the next boot. Add the writer's OWN constant to "
        "core_cache._EXCLUDED_STORE_ENTRIES — never a hand-spelled copy of it."
    )


def test_the_exclusion_set_agrees_with_the_constants_its_writers_own(
    isolate_agent_runtime_root,
):
    """The denylist and the writers' declared names are ONE vocabulary.

    The case above catches a drifted name once a driver exercises the writer.
    This one catches the drift directly and says WHICH constant moved, so a
    writer rename does not have to wait for someone to notice a slow cache.
    """

    owned = {
        "dispatch_delivery.DRAIN_STATE_FILENAME": dispatch_delivery.DRAIN_STATE_FILENAME,
        "serve_socket.SOCKET_LOCK_FILENAME": serve_socket.SOCKET_LOCK_FILENAME,
        "serve_socket.SOCKET_OWNER_FILENAME": serve_socket.SOCKET_OWNER_FILENAME,
        "serve_auth.SERVE_AUTH_TOKEN_FILENAME": serve_auth.SERVE_AUTH_TOKEN_FILENAME,
        "serve_registry.SERVE_INSTANCES_DIRNAME": serve_registry.SERVE_INSTANCES_DIRNAME,
        "paths.DELETED_ARCHIVE_DIRNAME": paths.DELETED_ARCHIVE_DIRNAME,
    }
    missing = sorted(
        f"{symbol} ({value!r})"
        for symbol, value in owned.items()
        if value not in core_cache._EXCLUDED_STORE_ENTRIES
    )
    assert not missing, (
        "core_cache._EXCLUDED_STORE_ENTRIES no longer names what these writers "
        f"call their files: {missing}. This is the exact shape of the shipped "
        "defect — the set said 'drain_state.json' and the writer said "
        "'dispatch_delivery_drain.json' for the life of the lane."
    )


# --------------------------------------------------------------------------- #
# 2. A boot's worth of writes does not move the key
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("driver_name", sorted(_DRIVERS))
def test_a_boot_writers_own_output_does_not_move_the_fingerprint(
    isolate_agent_runtime_root, driver_name
):
    """One writer per case, so one dropped exclusion reds exactly one claim.

    The fingerprint is taken before the writer runs and again while its output
    is on disk. An inequality means the next process cannot be served the core
    this one persisted — which is a cache that costs a megabyte write per build
    and buys nothing.
    """

    paths.store_root().mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None, "the fingerprint refused before the writer ran"
    with _DRIVERS[driver_name]():
        during = core_cache.build_input_fingerprint()
        assert during is not None, "the fingerprint refused while the writer's output was present"

    assert during.digest == before.digest, (
        f"the {driver_name} writer's own output changed the read-model cache's "
        "input fingerprint, so every boot invalidates the key the previous boot "
        f"persisted ({before.count} entries before, {during.count} during). The "
        "file is not a build input; exclude it by its writer's constant in "
        "core_cache._EXCLUDED_STORE_ENTRIES."
    )


def test_a_whole_boots_writes_together_do_not_move_the_fingerprint(
    isolate_agent_runtime_root,
):
    """The integration shape: what a real serve boot writes, all at once.

    Worth its own case beside the parametrized ones because the field failure
    was CUMULATIVE — the socket owner and the drain mirror each flipped the key
    on their own, and a fix that covered one of them would still have missed on
    every boot.
    """

    paths.store_root().mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None
    with contextlib.ExitStack() as stack:
        for driver in _DRIVERS.values():
            stack.enter_context(driver())
        during = core_cache.build_input_fingerprint()
        assert during is not None

    assert during.digest == before.digest, (
        "a boot's worth of runtime writes moved the fingerprint, so the cache "
        "cannot hit on any same-commit boot. This is the measured 2026-08-18 "
        "field defect, reproduced."
    )


# --------------------------------------------------------------------------- #
# 3. The drain mirror's STAGING file is skipped too
# --------------------------------------------------------------------------- #
def test_the_drain_mirrors_staging_file_is_invisible_to_the_walk(
    isolate_agent_runtime_root,
):
    """Excluding the target is not enough while the transient has another name.

    ``_walk_tree`` skips a staged temp file only in the ``.<stem>_*.tmp`` shape
    ``atomic_json_write`` produces. The drain mirror used to stage
    ``path.with_suffix(".tmp")`` — ``dispatch_delivery_drain.tmp``, which starts
    with no dot — so any walk that landed between the write and the rename saw a
    store-root entry that was about to vanish. A file whose mere PRESENCE flips
    the key, on a 60-second cadence.

    The probe runs INSIDE the rename, through ``os.replace``, which is the one
    call both the atomic writer and the hand-rolled predecessor pass through —
    so the killing mutation (staging by ``with_suffix``) is still observed.
    """

    path = dispatch_delivery._drain_state_path(paths.store_root())
    assert path is not None
    assert dispatch_delivery._write_drain_state(path, live=True) is True
    settled = core_cache.build_input_fingerprint()
    assert settled is not None

    staged_names: list[str] = []
    staged_present: list[bool] = []
    midwrite: list[core_cache.CoreFingerprint | None] = []
    real_replace = os.replace

    def replace_spy(src, dst, *args, **kwargs):
        staged_names.append(os.path.basename(str(src)))
        staged_present.append(os.path.exists(src))
        midwrite.append(core_cache.build_input_fingerprint())
        return real_replace(src, dst, *args, **kwargs)

    # A SCOPED patch: ``monkeypatch.undo()`` on the shared instance would unwind
    # the conftest's runtime-root pins as well (EG-0.1's leak), and this one has
    # to come off before the assertions run.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "replace", replace_spy)
        assert dispatch_delivery._write_drain_state(path, live=True) is True

    assert len(midwrite) == 1, (
        f"expected exactly one rename inside the mirror write, saw {len(midwrite)}"
    )
    assert staged_present == [True], (
        "the staged temp file was not on disk when the fingerprint was taken, so "
        "this probe measured nothing"
    )
    # The KEY claim is asserted first, so the killing mutation reds the
    # behaviour rather than the diagnostic restatement of it below.
    key = midwrite[0]
    assert key is not None, "the fingerprint refused mid-write"
    name = staged_names[0]
    assert key.digest == settled.digest, (
        f"the drain mirror's staging file ({name!r}) changed the fingerprint "
        f"while it was on disk ({settled.count} entries settled, {key.count} "
        "mid-write), so a walk that lands inside a 60-second heartbeat writes a "
        "key no later process can agree with."
    )
    assert name.startswith(".") and name.endswith(".tmp"), (
        f"the drain mirror staged {name!r}, which is not the ``.<stem>_*.tmp`` "
        "shape core_cache._walk_tree skips — route the write through "
        "utils.atomic_json_write rather than adding a second name to the "
        "exclusion set for a file that exists for microseconds. (The digest "
        "assertion above is the behaviour; this one names the cause, and both "
        "die to the same mutation.)"
    )


# --------------------------------------------------------------------------- #
# 4. MC-8 / P12 — the compaction graveyard is outside the closure
# --------------------------------------------------------------------------- #
#
# ``deleted_archive/`` is the ONE exclusion in the set that is not justified by
# "the runtime rewrites it". It is excluded because the PROJECTION has no reader
# for it — a claim about a reader set, which is exactly the kind of claim that
# rots — so the argument is written at the constant and these cases are what make
# the argument checkable. Measured on the live root before landing: 18,804 of
# 23,107 entries, ~81 % of every walk and of every ``entries.json`` write-back.
#
# NOTHING HERE ASSERTS A DURATION. P12's whole value is a time saving and a
# timing assertion over a synthetic tree of tens of files would measure the
# machine (the standing rule; a witness asserts counts, ordering or typed
# reasons). These cases assert the COUNT and the DIGEST, which is the fact that
# causes the saving and the only one a small fixture can prove.
#
# THE FIXTURE TAKES ITS BASELINE BEFORE THE GRAVEYARD EXISTS, deliberately. A
# baseline taken with ``deleted_archive/`` already on disk would survive the most
# plausible half-fix there is — skip RECURSION into the tree but keep recording
# the directory's own triple — because the directory entry is a constant and
# additions beneath it are the only thing left to see. Creating the whole tree
# after the baseline makes that mutant visible as an off-by-one.
def _graveyard_batch(root, *, batches: int = 3, per_batch: int = 4) -> int:
    """Write a realistic compaction graveyard; return how many files landed."""

    written = 0
    for batch in range(batches):
        batch_dir = root / paths.DELETED_ARCHIVE_DIRNAME / f"batch_{batch:03d}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        (batch_dir / "manifest.json").write_text(
            '{"archived_tasks":[]}', encoding="utf-8"
        )
        written += 1
        for item in range(per_batch):
            (batch_dir / f"task_{item}.jsonl").write_text(
                '{"type":"probe"}\n', encoding="utf-8"
            )
            written += 1
    return written


def test_the_compaction_graveyard_does_not_enter_the_fingerprint_count(
    isolate_agent_runtime_root,
):
    """Adding the graveyard moves the entry count by EXACTLY ZERO.

    The second half is what stops this reading as a win when it is not: a store
    root that contributes nothing at all would satisfy the first assertion
    perfectly. So the same fixture then writes ONE file outside the graveyard and
    requires the count to move by exactly one — the walk is still watching the
    store, it is just no longer watching the graveyard.

    *Kills, one per claim:*

    * drop ``DELETED_ARCHIVE_DIRNAME`` from ``_EXCLUDED_STORE_ENTRIES`` — the
      graveyard's 15 files plus its 4 directories re-enter and the first
      assertion reds on the count;
    * widen the exclusion to swallow a real store subtree (exclude
      ``"workspaces"`` as well) — the graveyard claim still passes and the
      second assertion reds, which is the over-broad-exclusion direction a
      count-of-zero cannot distinguish on its own.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    (root / "workspaces" / "ws_alpha.json").write_text("{}", encoding="utf-8")

    before = core_cache.build_input_fingerprint()
    assert before is not None, "the fingerprint refused before the graveyard was written"
    assert not (root / paths.DELETED_ARCHIVE_DIRNAME).exists(), (
        "the graveyard already existed when the baseline was taken, so a mutant "
        "that keeps the directory entry and skips only its contents would pass"
    )

    files = _graveyard_batch(root)
    assert files == 15, f"the fixture wrote {files} graveyard files, not the 15 it claims"
    with_graveyard = core_cache.build_input_fingerprint()
    assert with_graveyard is not None

    assert with_graveyard.count == before.count, (
        f"{files} files and 4 directories under "
        f"{paths.DELETED_ARCHIVE_DIRNAME}/ moved the fingerprint's entry count "
        f"({before.count} -> {with_graveyard.count}). On the operator's live root "
        "that tree is 18,804 of 23,107 entries — 81 % of every walk, paid four to "
        "five times per boot and again on every entries.json write-back — for a "
        "graveyard no projection reads and no current code writes."
    )

    (root / "workspaces" / "ws_beta.json").write_text("{}", encoding="utf-8")
    with_real_input = core_cache.build_input_fingerprint()
    assert with_real_input is not None
    assert with_real_input.count == before.count + 1, (
        "one added workspace file did not move the entry count by exactly one "
        f"({before.count} -> {with_real_input.count}), so the exclusion above is "
        "not 'the graveyard left the closure' — it is a walk that stopped seeing "
        "the store. An over-broad exclusion reads identically to a correct one "
        "from the graveyard's side alone, which is why this assertion exists."
    )


def test_rewriting_a_graveyard_file_does_not_move_the_digest(
    isolate_agent_runtime_root,
):
    """A change the COUNT can never see, which is the point of a second case.

    Rewriting a file in place changes its mtime and its size and leaves the entry
    count identical, so this is the half of the exclusion a count-shaped witness
    is structurally blind to: a walk that had stopped counting the graveyard but
    still stat'd it would pass the case above and fail here.

    *Kill:* drop ``DELETED_ARCHIVE_DIRNAME`` from ``_EXCLUDED_STORE_ENTRIES``.
    The count is unchanged across the rewrite either way; only the digest moves,
    so this reds while a count-only gate stays green.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    _graveyard_batch(root, batches=1, per_batch=2)
    victim = root / paths.DELETED_ARCHIVE_DIRNAME / "batch_000" / "task_0.jsonl"
    assert victim.exists()

    before = core_cache.build_input_fingerprint()
    assert before is not None

    # A longer body, so SIZE moves as well as mtime — a filesystem whose mtime
    # granularity is coarse would otherwise let this probe measure nothing.
    victim.write_text('{"type":"probe","rewritten":true,"padding":"xxxxxxxx"}\n', encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None

    assert after.count == before.count, (
        "the fixture added or removed an entry, so this case is no longer the "
        "count-blind probe it claims to be"
    )
    assert after.digest == before.digest, (
        "rewriting a file inside the compaction graveyard moved the read-model "
        "cache's input digest, so the graveyard is still being stat'd even though "
        "it is not being counted. Half an exclusion costs the full walk and still "
        "invalidates the key."
    )


def test_only_the_TOP_LEVEL_graveyard_is_excluded(
    isolate_agent_runtime_root,
):
    """``_EXCLUDED_STORE_ENTRIES`` is top-level by contract, and the comment must
    not overclaim.

    The argument written at the constant is about the ONE graveyard at the store
    root — the only place ``paths.deleted_archive_dir()`` can put it. A directory
    that merely shares the name, nested inside a real store subtree, is somebody
    else's data and is fingerprinted in full.

    **AMENDED BY IC-1.** ``_walk_tree`` now also has a NESTED mechanism
    (``exclude_nested`` / ``_EXCLUDED_NESTED_STORE_NAMES``), so "the walk is
    top-level only" stopped being true of the walk and is now a claim about THIS
    SET. The second half of the case says so as a fact: the graveyard names must
    not appear anywhere in the nested mapping either, because a nested rule that
    named one would reach exactly the tree the first half proves is in.

    *Kill:* implement the exclusion as a name filter inside the recursive walk
    (skip any entry whose ``name`` is in the exclusion set, at every depth)
    rather than as ``_walk_tree``'s top-level ``exclude_top``. The nested tree
    then vanishes from the closure too and this reds. *Second kill:* add
    ``paths.DELETED_ARCHIVE_DIRNAME`` to any value of
    ``_EXCLUDED_NESTED_STORE_NAMES``.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None

    nested = root / "workspaces" / paths.DELETED_ARCHIVE_DIRNAME
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "kept.json").write_text("{}", encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None

    assert after.count == before.count + 2, (
        "a directory named "
        f"{paths.DELETED_ARCHIVE_DIRNAME!r} NESTED under another store subtree "
        f"did not contribute its own entry plus its file ({before.count} -> "
        f"{after.count}). The exclusion is keyed to the store root's own top-level "
        "entries; implementing it as a name filter at every depth would silently "
        "drop unrelated data out of the closure, which is a missed input — the "
        "failure direction this module calls the worst one."
    )
    _assert_not_named_by_any_nested_rule(paths.DELETED_ARCHIVE_DIRNAME)


def test_the_excluded_name_is_the_one_the_path_helper_actually_produces(
    isolate_agent_runtime_root,
):
    """The set and the directory's producer are ONE vocabulary, not two copies.

    The neighbouring constants case proves the exclusion agrees with the declared
    constant. This one goes one step further out and asks the question that
    actually decides whether the walk skips anything: does the set contain the
    name of the directory ``paths.deleted_archive_dir()`` RESOLVES TO? A constant
    nobody's path helper uses would satisfy the other case and skip nothing here.

    *Kill:* re-spell the literal ``"deleted_archive"`` in ``core_cache``'s set
    beside the import instead of importing it, then change
    ``paths.DELETED_ARCHIVE_DIRNAME``'s VALUE. The hand-spelled copy stops
    tracking its owner and both this case and the constants case red — which is
    the whole reason a name with a cross-module reader is promoted to a constant
    rather than typed twice.
    """

    produced = paths.deleted_archive_dir().name
    assert produced in core_cache._EXCLUDED_STORE_ENTRIES, (
        f"paths.deleted_archive_dir() resolves to a directory named {produced!r} "
        "and core_cache._EXCLUDED_STORE_ENTRIES does not name it, so the walk "
        "still stats the whole compaction graveyard. This is the MCF-2 shape: an "
        "exclusion that agrees with a restated list rather than with the code "
        "that produces the file."
    )
    assert produced == paths.DELETED_ARCHIVE_DIRNAME, (
        "the path helper stopped using its own constant, so the constant and the "
        "directory can now drift apart while both look correct in isolation"
    )


def test_the_persisted_entries_shrink_with_the_closure(
    isolate_agent_runtime_root, monkeypatch
):
    """MC-3's ``entries.json`` carries no graveyard path — the two stay joined.

    ``_entries_payload`` persists ``key.entries``, i.e. the fingerprint's own stat
    set, so today this property holds BY CONSTRUCTION rather than by a second
    decision. That is stated plainly instead of dressed up: the gate's job is not
    to discover a bug, it is to PIN the join, because the alternative shape — a
    diagnostic writer that re-walks the store for itself — is a plausible
    refactor that would silently restore ~81 % of the write-back MCF-20 measured
    at ~3.4 MiB per led build.

    *Kill:* have ``_entries_payload`` persist a freshly re-walked store instead of
    the key it was handed (``_walk_tree(paths.store_root(), fresh, limit=...)``
    with no ``exclude_top``). Every triple the fingerprint excluded comes back
    into the file and this reds, while the fingerprint's own digest — and every
    other case in this file — stays green.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    (root / "workspaces" / "ws_alpha.json").write_text("{}", encoding="utf-8")
    _graveyard_batch(root)

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:mc8:clean")
    core_cache.reset_process_state()
    try:
        key = core_cache.build_input_fingerprint()
        assert key is not None
        assert core_cache.write_back({"parity": {}}, fingerprint=key) is True

        payload = json.loads(core_cache.entries_path().read_text(encoding="utf-8"))
    finally:
        core_cache.reset_process_state()

    persisted = [str(row[0]) for row in payload["entries"]]
    assert persisted, "the entries file carried no rows, so this gate proved nothing"
    assert any("ws_alpha.json" in path for path in persisted), (
        "the entries file does not contain the ordinary store file this fixture "
        "wrote, so its emptiness — not the exclusion — would satisfy the "
        "assertion below"
    )

    marker = f"{os.sep}{paths.DELETED_ARCHIVE_DIRNAME}{os.sep}"
    leaked = sorted(path for path in persisted if marker in path)
    assert not leaked, (
        f"{len(leaked)} compaction-graveyard paths reached the persisted stat "
        f"set, e.g. {leaked[0]!r}. The diagnostic is meant to describe the "
        "closure the digest was taken over; a file describing a WIDER set both "
        "costs the bytes P12 removed and would name paths on a demote that were "
        "never inputs."
    )


# --------------------------------------------------------------------------- #
# 5. H2 — the ORPHANED-SURFACE graveyard is outside the closure
# --------------------------------------------------------------------------- #
#
# ``office_archive/`` is the store-root sibling that
# ``OfficeStore.archive_orphaned_surface`` RENAMES a whole orphaned office
# surface into, driven by ``harness office archive-surface``. Its argument is the
# ordinary one and NOT the graveyard's above: the runtime WRITES this tree (B1
# wrote into it on the operator's live root on 2026-08-18) and the projection
# does not read it — ``OfficeStore.list_workspaces()`` enumerates
# ``office_root()``'s children, and this tree is deliberately a SIBLING of
# ``office_root()`` so that archiving actually stops the projection seeing the
# surface. It also grows without bound against ``MAX_FINGERPRINT_ENTRIES``: the
# destination helper appends ``-2``, ``-3`` … slots rather than refusing.
#
# THE NEAR NAME IS THE WHOLE RISK, so it gets its own case below.
# ``paths.office_archive_dir(ws)`` is ``office/<ws>/archive/`` — per-workspace,
# holding archived ACTOR placements, READ by the store on the actor-listing seam
# and by every archived-actor lookup. It is a projection INPUT and must stay in
# the walk. One underscore separates the two names and excluding the wrong one is
# a MISSED INPUT, the failure direction this module calls the worst.
#
# NOTHING HERE ASSERTS A DURATION (the standing rule): these cases assert
# membership, counts and the digest.
#
# WHAT THESE CASES DELIBERATELY DO NOT CLAIM: that archiving stops flipping the
# key. It does not, and it should not — the surface is MOVED OUT of
# ``office/<ws>/``, which is fingerprinted, so the gesture itself is a real input
# change and flips the key exactly once. What leaves the closure is the
# graveyard's CONTINUING contribution, which is what the two cases below split
# between them: its entries, and any churn inside it afterwards.
def _orphaned_surface(workspace_id: str = "ws_h2_probe") -> str:
    """Drive the REAL archive verb; return the workspace id it archived.

    Every step is the runtime's own: a real workspace, a real office surface with
    a real actor placement, the workspace record removed so the surface is
    genuinely orphaned (which is ``archive_orphaned_surface``'s precondition —
    it REFUSES a surface whose workspace still resolves), then the store's own
    move. No directory is created by hand.
    """

    store = WorkspaceStore()
    item = store.create(name="H2 archive probe", workspace_id=workspace_id)
    store.set_active(item.id)
    office = OfficeStore()
    office.ensure_surface(item.id)
    office.upsert_actor(
        item.id,
        {
            "persona_id": "dev",
            "items": [
                {
                    "item_id": "dev",
                    "persona_id": "dev",
                    "kind": "agent",
                    "position": [1.5, 2.0],
                    "folder": "Agents",
                }
            ],
        },
    )
    paths.workspace_path(item.id).unlink()
    assert not office.workspace_resolves(item.id), (
        "the fixture did not orphan the surface, so the archive verb below would "
        "refuse and this case would drive nothing"
    )
    office.archive_orphaned_surface(item.id)
    return item.id


def _archived_surface_root():
    """The one archived slot the fixture produced, asserted to be exactly one."""

    root = paths.office_surface_archive_root()
    slots = sorted(entry for entry in root.iterdir() if entry.is_dir())
    assert len(slots) == 1, (
        f"the fixture produced {len(slots)} archived surfaces, not the one it "
        "claims, so the assertions below are about an unknown tree"
    )
    return slots[0]


def test_the_real_archive_verbs_output_is_absent_from_the_stat_set(
    isolate_agent_runtime_root,
):
    """The writer runs, and NOTHING it wrote is in the fingerprint's stat set.

    Membership rather than a count, because the archive gesture legitimately
    moves entries OUT of ``office/<ws>/`` at the same time — a count taken across
    it would net two real changes against each other and could pass while the
    graveyard was still being stat'd.

    The second half is the non-vacuity guard: a walk that had stopped seeing the
    store entirely would satisfy the first assertion perfectly, so the same stat
    set must still contain the ordinary store file this fixture wrote.

    *Kill:* remove ``OFFICE_ARCHIVE_DIRNAME`` from ``_EXCLUDED_STORE_ENTRIES``.
    The archived ``office.json`` and the actor file re-enter and this reds.
    """

    root = isolate_agent_runtime_root
    _orphaned_surface()
    # Written AFTER the verb: it is a stub, not a decodable workspace record, and
    # ``WorkspaceStore`` reads the whole directory when the fixture above creates
    # its own. Its only job is to be an ordinary store file the walk must see.
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    (root / "workspaces" / "ws_alpha.json").write_text("{}", encoding="utf-8")

    archived = _archived_surface_root()
    assert (archived / "office.json").exists(), (
        "the archive verb did not land the surface file, so this case would "
        "assert the absence of something that was never written"
    )

    key = core_cache.build_input_fingerprint()
    assert key is not None, "the fingerprint refused, so nothing here is measurable"
    graveyard = str(paths.office_surface_archive_root())
    offenders = [entry.path for entry in key.entries if entry.path.startswith(graveyard)]
    assert offenders == [], (
        "the orphaned-surface graveyard is inside the read-model cache's input "
        f"closure ({len(offenders)} entries, e.g. {offenders[:2]}). The runtime "
        "writes this tree on every `harness office archive-surface`, it grows "
        "without bound against MAX_FINGERPRINT_ENTRIES on re-archives, and no "
        "projection reads it — list_workspaces() enumerates office_root(), whose "
        "SIBLING this deliberately is."
    )
    assert any("ws_alpha.json" in entry.path for entry in key.entries), (
        "the stat set does not contain the ordinary store file this fixture "
        "wrote, so its emptiness — not the exclusion — would satisfy the "
        "assertion above"
    )


def test_churn_inside_the_archived_surface_does_not_move_the_digest(
    isolate_agent_runtime_root,
):
    """The half a membership check is structurally blind to.

    A walk that had stopped LISTING the graveyard but still stat'd it would pass
    the case above and fail here. This is also the property that matters in the
    field: after the move settles, the tree must contribute nothing FURTHER — an
    operator poking at a recovered surface, or a second archive landing beside
    the first, must not disturb the key.

    *Kill:* the same removal of ``OFFICE_ARCHIVE_DIRNAME``. The count is
    unchanged across an in-place rewrite either way, so only the digest moves and
    a count-only gate would stay green.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    _orphaned_surface()
    victim = _archived_surface_root() / "office.json"
    assert victim.exists()

    before = core_cache.build_input_fingerprint()
    assert before is not None

    # Longer body so SIZE moves as well as mtime — a coarse mtime granularity
    # would otherwise let this probe measure nothing.
    victim.write_text(
        '{"probe":"rewritten","padding":"xxxxxxxxxxxxxxxx"}', encoding="utf-8"
    )
    (_archived_surface_root() / "recovered_note.json").write_text(
        "{}", encoding="utf-8"
    )
    after = core_cache.build_input_fingerprint()
    assert after is not None

    assert after.digest == before.digest, (
        "rewriting a file inside an archived office surface — and adding one "
        "beside it — moved the read-model cache's input digest, so the graveyard "
        "is still being stat'd. Half an exclusion costs the full walk and still "
        "invalidates the key."
    )


def test_the_per_workspace_actor_archive_is_STILL_in_the_closure(
    isolate_agent_runtime_root,
):
    """THE NEAR-NAME TRAP, pinned. One underscore, and the opposite answer.

    ``paths.office_archive_dir(ws)`` — ``office/<ws>/archive/`` — holds archived
    ACTOR placements and is READ by ``OfficeStore``: ``_read_actor_dir`` on the
    actor-listing seam (``scan_actors(include_archived=True)``), and
    ``office_archived_actor_path`` on the archived-actor lookups that
    ``upsert_actor`` / ``remove_actor`` / ``restore_actor`` and the class-key
    fence ride. It is a projection INPUT and must be fingerprinted.

    *Kill:* collapse the two trees — point ``paths.office_archive_dir`` at
    ``office_surface_archive_root() / ws`` instead of ``office_dir(ws)/archive``.
    That is the exact confusion this case exists for: the archived actor copies
    land inside the excluded tree, they stop being stat'd, and a change to a real
    projection input can then be served stale. This reds; every other case in
    this section stays green, which is why it cannot be stood in for.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None

    archive = paths.office_archive_dir("ws_near_name")
    assert not archive.exists(), "the fixture's near-name tree already existed"
    archive.mkdir(parents=True, exist_ok=True)
    (archive / "dev.json").write_text("{}", encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None

    added = after.count - before.count
    assert added >= 2, (
        "the per-workspace archived-actor tree "
        f"({archive}) added {added} entries rather than at least its own "
        "directory plus its file, so a tree the OfficeStore genuinely reads has "
        "left the fingerprint closure. That is a missed input — a change to an "
        "archived actor could then be served from a stale core — and it is the "
        "way to get this exclusion exactly backwards."
    )
    assert paths.office_archive_dir("ws_near_name").name not in (
        core_cache._EXCLUDED_STORE_ENTRIES
    ), (
        "the per-workspace archive directory's own name is in the exclusion set, "
        "so any store-root entry that happens to share it would silently leave "
        "the closure too"
    )


def test_only_the_TOP_LEVEL_office_archive_is_excluded(
    isolate_agent_runtime_root,
):
    """``_EXCLUDED_STORE_ENTRIES`` is top-level by contract, and the comment must
    not overclaim.

    The argument written at the constant is about the ONE graveyard at the store
    root — the only place ``paths.office_surface_archive_root()`` can put it. A
    directory that merely shares the name, nested inside a real store subtree, is
    somebody else's data and is fingerprinted in full.

    **AMENDED BY IC-1**, exactly as the ``deleted_archive`` case above: the walk
    now has a nested mechanism too, so the second half pins that this name is not
    in it.

    *Kill:* implement the exclusion as a name filter inside the recursive walk
    (skip any entry whose ``name`` is in the exclusion set, at every depth)
    rather than as ``_walk_tree``'s top-level ``exclude_top``. The nested tree
    then vanishes from the closure too and this reds. *Second kill:* add
    ``paths.OFFICE_ARCHIVE_DIRNAME`` to any value of
    ``_EXCLUDED_NESTED_STORE_NAMES``.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None

    nested = root / "workspaces" / paths.OFFICE_ARCHIVE_DIRNAME
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "kept.json").write_text("{}", encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None

    assert after.count == before.count + 2, (
        "a directory named "
        f"{paths.OFFICE_ARCHIVE_DIRNAME!r} NESTED under another store subtree "
        f"did not contribute its own entry plus its file ({before.count} -> "
        f"{after.count}). The exclusion is keyed to the store root's own "
        "top-level entries; implementing it as a name filter at every depth "
        "would silently drop unrelated data out of the closure, which is a "
        "missed input — the failure direction this module calls the worst one."
    )
    _assert_not_named_by_any_nested_rule(paths.OFFICE_ARCHIVE_DIRNAME)


def test_the_excluded_name_is_the_one_the_surface_archive_helper_produces(
    isolate_agent_runtime_root,
):
    """The set and the directory's producer are ONE vocabulary, not two copies.

    Same question the graveyard's constants case asks, aimed at the name that
    actually decides whether this walk skips anything: does the set contain the
    name ``paths.office_surface_archive_root()`` RESOLVES TO? A constant nobody's
    path helper uses would look correct and skip nothing.

    The third assertion is the near-name separation stated as a fact rather than
    left to the prose: the two helpers must resolve to different directories, so
    that excluding one can never mean excluding the other.

    *Kill:* re-spell the literal ``"office_archive"`` in ``core_cache``'s set
    beside the import instead of importing it, then change
    ``paths.OFFICE_ARCHIVE_DIRNAME``'s VALUE. The hand-spelled copy stops
    tracking its owner and this reds — which is the whole reason a name with a
    cross-module reader is promoted to a constant rather than typed twice.
    """

    produced = paths.office_surface_archive_root().name
    assert produced in core_cache._EXCLUDED_STORE_ENTRIES, (
        f"paths.office_surface_archive_root() resolves to a directory named "
        f"{produced!r} and core_cache._EXCLUDED_STORE_ENTRIES does not name it, "
        "so the walk still stats every archived office surface. This is the "
        "MCF-2 shape: an exclusion that agrees with a restated list rather than "
        "with the code that produces the directory."
    )
    assert produced == paths.OFFICE_ARCHIVE_DIRNAME, (
        "the path helper stopped using its own constant, so the constant and the "
        "directory can now drift apart while both look correct in isolation"
    )
    assert paths.office_archive_dir("ws_near_name") != (
        paths.office_surface_archive_root()
    ), (
        "the per-workspace actor archive and the orphaned-surface graveyard "
        "resolve to the SAME directory, so the exclusion above now covers a tree "
        "the OfficeStore reads"
    )


# --------------------------------------------------------------------------- #
# 4. IC-1 — the NESTED exclusion: ``realm_sync/**/.git`` only
# --------------------------------------------------------------------------- #
#
# The nested mechanism is a different shape from ``_EXCLUDED_STORE_ENTRIES`` and
# carries a different risk. The top-level set can only ever drop a store-root
# entry the author looked at; a nested rule reaches DOWN, so the cases below are
# written against two mutants at once:
#
#   * OVER-exclusion — the rule reaches a sibling it was never audited for. The
#     one that matters is ``realm_sync/<realm>/board_baseline.json`` and its
#     three sidecar siblings, which the projection genuinely READS. A
#     fingerprint that ignored a baseline change would serve a stale publication
#     verdict as authoritative, which is the failure class this whole lane
#     exists to end.
#   * UNDER-scoping — the rule is implemented as "skip ``.git`` anywhere", so a
#     ``.git`` outside ``realm_sync/`` silently leaves the closure too.
def _assert_not_named_by_any_nested_rule(name: str) -> None:
    """No nested rule may name ``name`` — the amendment's other half.

    Shared by the two top-level-only cases above, because "this name is not in
    the nested mapping either" is the same claim for both graveyards and a
    hand-copied assertion is how two claims drift into one being checked.
    """

    naming = sorted(
        f"{top!r} -> {sorted(skipped)}"
        for top, skipped in core_cache._EXCLUDED_NESTED_STORE_NAMES.items()
        if name in skipped
    )
    assert not naming, (
        f"{name!r} is skipped by a NESTED rule ({naming}), so the tree the case "
        "above just proved is inside the closure leaves it again one level down. "
        "The nested mapping is not a second home for top-level exclusions: every "
        "entry in it needs its own reader audit, written at "
        "core_cache._EXCLUDED_NESTED_STORE_NAMES."
    )


def _realm_sync_worktree(root, *, realm_id: str = "realm_alpha"):
    """A realm-sync subtree shaped like the live one: a git worktree AND baselines.

    Both halves on purpose. The worktree's ``.git`` is what the exclusion is for;
    the baseline sidecars beside it are what the exclusion must not touch, and a
    fixture that built only the first could not tell the two mutants apart.
    """

    realm_dir = root / paths.REALM_SYNC_DIRNAME / realm_id
    (realm_dir / ".git" / "objects" / "pack").mkdir(parents=True, exist_ok=True)
    (realm_dir / ".git" / "logs").mkdir(parents=True, exist_ok=True)
    (realm_dir / "realms").mkdir(parents=True, exist_ok=True)
    (realm_dir / "realms" / "manifest.json").write_text("{}", encoding="utf-8")
    return realm_dir


def _git_churn(realm_dir) -> None:
    """What a fetch/checkout leaves behind — the 60-entry class, in miniature."""

    (realm_dir / ".git" / "index").write_text("a", encoding="utf-8")
    (realm_dir / ".git" / "logs" / "HEAD").write_text("a", encoding="utf-8")
    (realm_dir / ".git" / "objects" / "pack" / "pack-a.pack").write_text("a", encoding="utf-8")


def test_a_synced_worktrees_git_bookkeeping_is_outside_the_closure(
    isolate_agent_runtime_root,
):
    """IC-1's whole point: git's own churn must not move the key.

    The 2026-08-20 18:21 ``never_converged`` firing named 60 changed entries and
    every one of them was under ``realm_sync/<realm>/.git/``. The projection is
    forbidden to read any of it (Decision 7 — ``build_snapshot`` "must never
    shell out to git"), so it is not an input and its churn is pure noise inside
    the key.

    *Kill:* drop ``REALM_SYNC_DIRNAME`` from
    ``core_cache._EXCLUDED_NESTED_STORE_NAMES``, or hand ``_walk_tree`` no
    ``exclude_nested`` at the store-root call site.
    """

    root = isolate_agent_runtime_root
    realm_dir = _realm_sync_worktree(root)
    _git_churn(realm_dir)
    before = core_cache.build_input_fingerprint()
    assert before is not None

    # A whole fetch's worth of git bookkeeping: rewritten files AND new ones.
    _git_churn(realm_dir)
    (realm_dir / ".git" / "objects" / "pack" / "pack-b.pack").write_text("b", encoding="utf-8")
    (realm_dir / ".git" / "ORIG_HEAD").write_text("b", encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None

    assert after.digest == before.digest, (
        "a synced worktree's git bookkeeping moved the read-model cache's input "
        f"digest ({before.count} entries -> {after.count}). Nothing in the "
        "projection reads it, so every fetch the sync verbs run costs the next "
        "process its cache hit — the largest oscillating class in the whole "
        "never_converged receipt series."
    )


@pytest.mark.parametrize(
    "helper",
    [
        "board_baseline_path",
        "office_baseline_path",
        "persona_config_baseline_path",
        "profile_artifact_baseline_path",
    ],
)
def test_a_realm_sync_baseline_stays_inside_the_closure(
    isolate_agent_runtime_root, helper
):
    """THE OVER-EXCLUSION GATE. A baseline change MUST move the key.

    ``realm_sync/<realm>/board_baseline.json`` and ``office_baseline.json`` are
    read by ``build_snapshot`` (through ``board_sync.read_board_baseline`` /
    ``office_sync.read_office_baseline``) to decide whether a card or an actor is
    published; the other two sidecars are their siblings by the same helper
    family. They sit ONE DIRECTORY UP from the ``.git`` the case above excludes,
    inside the same top-level subtree, which is exactly the blast radius a nested
    rule has and a top-level one does not.

    Parametrized one sidecar per case so a rule that reached one of them reds the
    case that names it, rather than a single "something moved" standing in for
    four claims.

    *Kill:* widen ``_EXCLUDED_NESTED_STORE_NAMES[REALM_SYNC_DIRNAME]`` to skip
    the baseline's own name, or implement the nested rule as a suffix/prefix
    match over the realm directory instead of the literal child name.
    """

    root = isolate_agent_runtime_root
    _realm_sync_worktree(root)
    path = getattr(paths, helper)("realm_alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"card_a": "sha_one"}', encoding="utf-8")

    before = core_cache.build_input_fingerprint()
    assert before is not None
    assert str(path) in {entry.path for entry in before.entries}, (
        f"{helper}() resolved to {path}, and the fingerprint does not stat it at "
        "all. The build reads this file; a core served across a change to it is "
        "unlabeled stale served as authoritative."
    )

    path.write_text('{"card_a": "sha_two"}', encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None
    assert after.digest != before.digest, (
        f"rewriting {path} did not move the read-model cache's input digest, so "
        "the realm-sync baseline has left the input closure. The publication "
        "verdict the projection computes from it can now be served from a core "
        "built before the change."
    )


def test_the_nested_rule_does_not_reach_a_dot_git_outside_realm_sync(
    isolate_agent_runtime_root,
):
    """UNDER-SCOPING. ``.git`` is excluded INSIDE one subtree, not everywhere.

    The audit that opened this exclusion covers exactly one tree: the worktree
    the realm-sync verbs create. A ``.git`` anywhere else in the store is
    somebody else's data, carries no audit, and must be fingerprinted in full —
    the same argument the two top-level-only cases make about the graveyards.

    *Kill:* implement the skip as ``if name == ".git": continue`` inside
    ``_walk_tree``, or consult ``exclude_nested`` at every depth instead of only
    over the store root's own entry names.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None

    stray = root / "workspaces" / ".git"
    stray.mkdir(parents=True, exist_ok=True)
    (stray / "index").write_text("a", encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None

    assert after.count == before.count + 2, (
        f"a '.git' directory NESTED under another store subtree ({stray}) did "
        f"not contribute its own entry plus its file ({before.count} -> "
        f"{after.count}). The nested exclusion is declared for the realm-sync "
        "subtree alone; applying it by name at every depth would drop unaudited "
        "data out of the closure, which is a missed input."
    )


def test_a_nested_rule_is_not_activated_by_a_deep_directory_of_the_same_name(
    isolate_agent_runtime_root,
):
    """The rule keys on a TOP-LEVEL entry name, not on any path segment.

    ``exclude_nested`` is consulted over the store root's own entries and nowhere
    else, so a directory that merely happens to be called ``realm_sync`` while
    nested inside another store subtree activates nothing. Without this, the
    mapping would silently grow into a claim about the name everywhere — the
    defect the top-level-only doctrine block was written against, arriving
    through the new door.

    *Kill:* look ``exclude_nested`` up by ``entry.name`` at every depth in the
    walk loop instead of once over ``root``'s own entries.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None

    impostor = root / "workspaces" / paths.REALM_SYNC_DIRNAME / "child"
    impostor.mkdir(parents=True, exist_ok=True)
    (impostor / ".git").mkdir(parents=True, exist_ok=True)
    (impostor / ".git" / "index").write_text("a", encoding="utf-8")
    after = core_cache.build_input_fingerprint()
    assert after is not None

    assert after.count == before.count + 4, (
        "a directory named "
        f"{paths.REALM_SYNC_DIRNAME!r} NESTED under another store subtree "
        f"activated the nested rule ({before.count} -> {after.count}; expected "
        "+4 for the impostor, its child, the .git directory and its file). The "
        "rule is declared for the STORE ROOT's own entry of that name."
    )


def test_the_nested_rule_names_the_directory_the_path_helpers_produce(
    isolate_agent_runtime_root,
):
    """The mapping's key and the realm-sync tree's producers are ONE vocabulary.

    Both producers are checked, because the two key their children differently
    and the whole reason the rule skips the literal name ``.git`` is that neither
    keying is usable as a skip: ``realm_sync._sync_repo_path`` keys a worktree by
    the realm's SERVER token, ``paths.board_baseline_path`` keys a sidecar by the
    REALM ID, and both land under the same top-level directory.

    *Kill:* re-spell the literal ``"realm_sync"`` in ``core_cache``'s mapping
    instead of importing ``paths.REALM_SYNC_DIRNAME``, then change the
    constant's VALUE.
    """

    from agent_runtime import realm_sync

    produced = paths.realm_sync_root().name
    assert produced in core_cache._EXCLUDED_NESTED_STORE_NAMES, (
        f"paths.realm_sync_root() resolves to a directory named {produced!r} and "
        "core_cache._EXCLUDED_NESTED_STORE_NAMES does not key on it, so the walk "
        "still stats every synced worktree's git internals. This is the MCF-2 "
        "shape: an exclusion that agrees with a restated list rather than with "
        "the code that produces the directory."
    )
    assert produced == paths.REALM_SYNC_DIRNAME
    assert paths.board_baseline_path("realm_alpha").parent.parent == (
        paths.realm_sync_root()
    ), (
        "the baseline sidecars no longer live under the directory the nested "
        "rule is keyed on, so the over-exclusion gate above is testing a tree "
        "the rule cannot reach"
    )

    class _Realm:
        sync_manifest_ref = ""
        server_id = "srv_token"

    assert realm_sync._sync_repo_path(_Realm()).parent == paths.realm_sync_root(), (
        "the synced worktree no longer lands under the directory the nested rule "
        "is keyed on, so the exclusion skips nothing on a live store"
    )
