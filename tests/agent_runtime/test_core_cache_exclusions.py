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
    """``exclude_top`` is top-level by contract, and the comment must not overclaim.

    The argument written at the constant is about the ONE graveyard at the store
    root — the only place ``paths.deleted_archive_dir()`` can put it. A directory
    that merely shares the name, nested inside a real store subtree, is somebody
    else's data and is fingerprinted in full.

    *Kill:* implement the exclusion as a name filter inside the recursive walk
    (skip any entry whose ``name`` is in the exclusion set, at every depth)
    rather than as ``_walk_tree``'s top-level ``exclude_top``. The nested tree
    then vanishes from the closure too and this reds.
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

    ``_write_entries`` persists ``key.entries``, i.e. the fingerprint's own stat
    set, so today this property holds BY CONSTRUCTION rather than by a second
    decision. That is stated plainly instead of dressed up: the gate's job is not
    to discover a bug, it is to PIN the join, because the alternative shape — a
    diagnostic writer that re-walks the store for itself — is a plausible
    refactor that would silently restore ~81 % of the write-back MCF-20 measured
    at ~3.4 MiB per led build.

    *Kill:* have ``_write_entries`` persist a freshly re-walked store instead of
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
