"""A byte-identical rewrite of a profile config must not cost a full rebuild.

THE MEASUREMENT THIS FILE EXISTS FOR (operator's runtime, 2026-08-21)
====================================================================

Every Mission Control boot demoted the persisted core with::

    reason=fingerprint_mismatch inputs=2225 changed=1
    diff=X:\\Eternia\\.hermes\\profiles\\alice\\config.yaml

and paid a full rebuild — 6,467 ms on the 13:17 boot, ``agents_readiness``
3,266 ms of it. The named file was, at the moment of the miss, **byte-for-byte
identical to a copy taken two days earlier** (same 23,255 bytes, same SHA-256)
while its mtime and its NTFS creation time had both moved. Something outside the
snapshot build re-serialises that document and lands the same bytes; the
``(path, mtime_ns, size)`` triple reported a change that did not exist, and the
core the previous boot persisted was thrown away for it on every single boot.

WHAT EACH GATE IS FOR, AND WHAT KILLS IT
========================================

1. the fingerprint does not move for a content-identical atomic rewrite —
   killed by dropping the mask from class 4;
2. the persisted core SURVIVES a simulated restart across that rewrite. This is
   the operator's symptom, and it is the gate the unit-level "we did not move
   the key" assertion cannot stand in for: a cache can hold a stable key and
   still rebuild. Its own anti-vacuity witness is a counter on the build's store
   reads, because ``core_source=cache`` is a field a rebuilding mutant can stamp;
3. **a genuine change still invalidates** — the gate that proves the trigger was
   narrowed rather than the invalidation disabled. Driven with a SAME-LENGTH
   edit so it cannot pass on ``size`` alone, and again with the file's original
   mtime restored so it cannot pass on the timestamp either. That second half is
   a signal the old mtime key did not have: a restore-in-place that preserves
   mtime was a FALSE HIT before this change and is a miss after it;
4. appearance and disappearance still count. The mask is allowed to say "these
   bytes did not change"; it is not allowed to say "this file arriving is not an
   event". Same distinction ``_wal_without_frames_is_content_free`` refuses to
   generalise away, asserted here for the config class;
5. the read is BOUNDED. Over the ceiling an entry keeps its ordinary stat
   triple, so the boot path can never be made to read an unbounded file to
   decide a cache key.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from agent_runtime import core_cache
from agent_runtime.snapshot import BUILD_ROLE_CACHE, BUILD_ROLE_LED, build_snapshot
from agent_runtime.store import WorkspaceStore


WORKSPACE_ID = "ws_config_content_key_probe"

#: A config body in the shape the mask is about: hand-authored YAML, small.
BODY = (
    "_config_version: 23\n"
    "model:\n"
    "  default: deepseek-chat\n"
    "  provider: deepseek\n"
    "mcp_servers:\n"
    "  launcher_qa:\n"
    "    command: launcher-qa\n"
)

#: Same length as ``BODY``, different bytes. Same length ON PURPOSE: a gate that
#: only passes because the file grew is a gate on ``size``, not on content.
BODY_SAME_LENGTH = BODY.replace("deepseek-chat", "deepseek-chaT")


@pytest.fixture(autouse=True)
def fresh_cache_lane():
    """Every case starts and ends with a process that has built nothing."""

    core_cache.reset_process_state()
    yield
    core_cache.reset_process_state()


def _profiles_root() -> Path:
    from hermes_cli.profiles import _get_profiles_root

    return Path(_get_profiles_root())


def _seed_profile_config(name: str, body: str = BODY) -> Path:
    """A profile home carrying a ``config.yaml``, as class 4 enumerates them."""

    home = _profiles_root() / name
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.yaml"
    # write_bytes, not write_text: on Windows the text writer translates ``\n``
    # to ``\r\n`` and ``_atomic_rewrite`` (which mirrors ``atomic_yaml_write``)
    # does not, so a "content-identical" rewrite would silently change the file.
    path.write_bytes(body.encode("utf-8"))
    return path


def _atomic_rewrite(path: Path, body: str) -> None:
    """Replace ``path`` the way ``utils.atomic_yaml_write`` does.

    Temp file in the same directory, then ``os.replace``. Spelled out rather
    than imported so the case reproduces the SHAPE of the field writer (a full
    atomic replacement, fresh mtime, fresh creation time) without depending on
    which of this repo's ~200 config writers happened to be the one caught.
    """

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cfgprobe_", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    os.replace(tmp, path)


def _entry_for(fingerprint, path: Path):
    text = str(path)
    for item in fingerprint.entries:
        if item.path == text:
            return item
    raise AssertionError(f"{text} is not in the fingerprint at all")


def _seed_workspace(name: str) -> None:
    WorkspaceStore().create(name=name, workspace_id=WORKSPACE_ID)


def _persisted_core() -> dict:
    return json.loads(core_cache.core_path().read_text(encoding="utf-8"))


def _converge_persisted_core(*, limit: int = 4) -> int:
    """Build until the persisted key describes the SETTLED store.

    Same helper (and same bound, for the same reason) as
    ``test_core_fingerprint_cache.converge_persisted_core``: the build is not a
    pure reader, so a virgin store normally needs two passes. A store that never
    converges means a build perturbs its own inputs forever, which must fail
    loudly here rather than let every case below pass by rebuilding.
    """

    for attempt in range(1, limit + 1):
        core_cache.core_path().unlink(missing_ok=True)
        core_cache.sidecar_path().unlink(missing_ok=True)
        core_cache.reset_process_state()
        build_snapshot()
        if core_cache.read_persisted_core().matched:
            core_cache.reset_process_state()
            return attempt
    raise AssertionError(
        "the persisted core's fingerprint never converged: after "
        f"{limit} builds the key still does not describe the settled store."
    )


class _CountedStoreReads:
    """Counts the build's own store reads — the rebuilding mutant's tell.

    ``parity.core_source`` is a string a mutant that rebuilds can still stamp
    ``cache``. These counters live in the test, wrapped around readers a real
    build cannot avoid and a cache hit cannot reach.
    """

    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        from agent_runtime import events as events_mod
        from agent_runtime import snapshot as snapshot_mod
        from agent_runtime import store as store_mod

        real_list_all = store_mod.AgentStore.list_all
        real_tail = events_mod.CachedEventLog.tail

        def counted_list_all(inner_self, *args, **kwargs):
            self.calls.append("agent_store.list_all")
            return real_list_all(inner_self, *args, **kwargs)

        def counted_tail(inner_self, *args, **kwargs):
            self.calls.append("event_log.tail")
            return real_tail(inner_self, *args, **kwargs)

        monkeypatch.setattr(store_mod.AgentStore, "list_all", counted_list_all)
        monkeypatch.setattr(events_mod.CachedEventLog, "tail", counted_tail)
        monkeypatch.setattr(snapshot_mod, "AgentStore", store_mod.AgentStore)


# --------------------------------------------------------------------------- #
# 1. A content-identical rewrite does not move the key
# --------------------------------------------------------------------------- #
def test_a_content_identical_rewrite_does_not_move_the_fingerprint(
    isolate_agent_runtime_root,
):
    """The field shape, reduced to one file and one assertion.

    *Killing mutation:* key class 4's ``config.yaml`` with ``_stat_entry``
    again — the entry's second field becomes the fresh ``mtime_ns`` and both
    assertions below red.
    """

    config = _seed_profile_config("probe-profile")
    before = core_cache.build_input_fingerprint()
    assert before is not None
    mtime_before = config.stat().st_mtime_ns

    _atomic_rewrite(config, BODY)

    # Anti-vacuity FIRST: a rewrite that did not move the timestamp would make
    # everything below true for the wrong reason.
    assert config.stat().st_mtime_ns != mtime_before, (
        "the probe's own rewrite did not move mtime, so this case is not "
        "measuring what it claims"
    )
    assert config.read_bytes() == BODY.encode("utf-8")

    after = core_cache.build_input_fingerprint()
    assert after is not None
    assert _entry_for(after, config) == _entry_for(before, config), (
        "the profile config's fingerprint entry moved for a rewrite that "
        "changed no byte — the persisted core is invalidated by a document "
        "that did not change"
    )
    assert after.digest == before.digest, (
        "the whole input fingerprint moved for a content-identical config "
        "rewrite, which is the every-boot rebuild the operator measured"
    )


# --------------------------------------------------------------------------- #
# 2. The persisted core survives a simulated restart across that rewrite
# --------------------------------------------------------------------------- #
def test_the_persisted_core_survives_a_restart_after_an_identical_rewrite(
    isolate_agent_runtime_root, monkeypatch
):
    """The operator's symptom, end to end: restart, and the cache HITS.

    *Killing mutation:* the same one as gate 1. Without the mask the second
    ``build_snapshot`` demotes ``fingerprint_mismatch`` and rebuilds, which is
    exactly the 6.5-7.6 s every boot was paying.
    """

    _seed_workspace("alpha-one")
    config = _seed_profile_config("probe-profile")
    _converge_persisted_core()

    mtime_before = config.stat().st_mtime_ns
    _atomic_rewrite(config, BODY)
    assert config.stat().st_mtime_ns != mtime_before, (
        "the probe's own rewrite did not move mtime; the pre-fix code would "
        "have hit anyway and this case would prove nothing"
    )

    counted = _CountedStoreReads(monkeypatch)
    core_cache.reset_process_state()
    info: dict = {"caller": "probe"}
    core = build_snapshot(build_info=info)

    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_CACHE, (
        "a restart after a content-identical config rewrite still demoted the "
        "persisted core and rebuilt — the cache can never survive a boot"
    )
    assert info["role"] == BUILD_ROLE_CACHE
    # ...and the witness the receipt field cannot forge.
    assert counted.calls == [], (
        "the build read the stores, so it RECONSTRUCTED the core and merely "
        f"labelled it a cache hit (reads={counted.calls})"
    )


# --------------------------------------------------------------------------- #
# 3. A genuine change still invalidates — the gate that matters most
# --------------------------------------------------------------------------- #
def test_a_genuine_config_change_still_invalidates(
    isolate_agent_runtime_root,
):
    """Narrowing the trigger, not disabling invalidation.

    *Killing mutation:* the trivially-passing wrong fix — drop
    ``config.yaml`` out of the fingerprint entirely (or return a constant from
    the mask). Gates 1 and 2 both go green under it; this one reds.

    The edit is the SAME LENGTH as the original, so a fix that merely fell back
    to ``size`` cannot pass it either.
    """

    _seed_workspace("alpha-one")
    config = _seed_profile_config("probe-profile")
    _converge_persisted_core()

    assert len(BODY_SAME_LENGTH) == len(BODY), "the probe's own edit changed size"
    _atomic_rewrite(config, BODY_SAME_LENGTH)

    core_cache.reset_process_state()
    info: dict = {"caller": "probe"}
    core = build_snapshot(build_info=info)

    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT, (
        "a REAL config edit was served from cache: the fix did not narrow the "
        "invalidation trigger, it removed the input from the closure"
    )
    assert info["role"] == BUILD_ROLE_LED


def test_a_genuine_change_that_preserves_mtime_still_invalidates(
    isolate_agent_runtime_root,
):
    """A restore-in-place was a FALSE HIT under the mtime key. Now it is a miss.

    Not a regression pin for the old behaviour — a signal the old key did not
    have. ``os.utime`` puts the original timestamp back after the edit, so the
    ``(path, mtime_ns, size)`` triple is identical to the persisted one while
    the bytes are not.

    *Killing mutation:* the same never-invalidate sabotage as above, and also a
    "content hash only when the mtime moved" half-fix, which this case reds and
    gate 3 does not.
    """

    _seed_workspace("alpha-one")
    config = _seed_profile_config("probe-profile")
    _converge_persisted_core()

    stamp = config.stat()
    _atomic_rewrite(config, BODY_SAME_LENGTH)
    os.utime(config, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))

    assert config.stat().st_mtime_ns == stamp.st_mtime_ns, (
        "the probe could not restore the original mtime, so this case is not "
        "measuring a timestamp-preserving edit"
    )
    assert config.stat().st_size == stamp.st_size

    core_cache.reset_process_state()
    core = build_snapshot(build_info={"caller": "probe"})
    assert core["parity"]["core_source"] == core_cache.CORE_SOURCE_REBUILT, (
        "an edit that preserved both mtime and size was served from cache — "
        "the key is still reading the timestamp, not the bytes"
    )


# --------------------------------------------------------------------------- #
# 4. Appearance and disappearance are still content events
# --------------------------------------------------------------------------- #
def test_a_config_that_appears_still_moves_the_key(isolate_agent_runtime_root):
    """A profile gaining a ``config.yaml`` is a change, mask or no mask.

    *Killing mutation:* collapse absent into a constant the way the WAL mask
    collapses absent-or-empty. That collapse is argued at ``-wal`` precisely
    because a WAL appears EMPTY; a config appears with content.
    """

    home = _profiles_root() / "probe-profile"
    home.mkdir(parents=True, exist_ok=True)
    before = core_cache.build_input_fingerprint()
    assert before is not None

    (home / "config.yaml").write_bytes(BODY.encode("utf-8"))

    after = core_cache.build_input_fingerprint()
    assert after is not None
    assert after.digest != before.digest, (
        "a config file APPEARING did not move the key — the mask swallowed an "
        "event it is only allowed to swallow for identical bytes"
    )


def test_a_config_that_disappears_still_moves_the_key(isolate_agent_runtime_root):
    """...and the same in the other direction.

    *Killing mutation:* return a constant for an unreadable path instead of
    falling back to the stat triple's ``-1/-1``.
    """

    config = _seed_profile_config("probe-profile")
    before = core_cache.build_input_fingerprint()
    assert before is not None

    config.unlink()

    after = core_cache.build_input_fingerprint()
    assert after is not None
    assert after.digest != before.digest, (
        "a config file DISAPPEARING did not move the key"
    )


# --------------------------------------------------------------------------- #
# 5. The read is bounded
# --------------------------------------------------------------------------- #
def test_an_oversized_config_keeps_its_stat_triple(isolate_agent_runtime_root):
    """Over the ceiling the entry is stat'd, never read.

    The boot path must not be reachable into an unbounded read by a file it does
    not control the size of. Asserted through the OBSERVABLE consequence — an
    oversized file falls back to mtime keying, so a content-identical rewrite of
    it DOES move the key — rather than by counting reads, so the case survives
    any refactor of how the ceiling is enforced.

    *Killing mutation:* delete the ``_CONFIG_CONTENT_MAX_BYTES`` guard.
    """

    body = "# pad\n" + ("x" * (core_cache._CONFIG_CONTENT_MAX_BYTES + 16))
    config = _seed_profile_config("probe-profile", body)
    before = core_cache.build_input_fingerprint()
    assert before is not None
    assert _entry_for(before, config).size > core_cache._CONFIG_CONTENT_MAX_BYTES

    _atomic_rewrite(config, body)

    after = core_cache.build_input_fingerprint()
    assert after is not None
    assert _entry_for(after, config) != _entry_for(before, config), (
        "an oversized config was content-hashed anyway: the boot path can be "
        "made to read a file of unbounded size to decide a cache key"
    )
