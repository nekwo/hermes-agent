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
