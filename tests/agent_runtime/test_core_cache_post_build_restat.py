"""IC-2 — the write-back is keyed on POST-build reality, for the audited set only.

WHY THIS FILE EXISTS
====================

``write_back`` persisted the CONSULT-time key. The build is not a pure reader —
the 2026-08-22 audit proved five writes it makes while reading (persona-instance
rows, the build's own SessionDB close draining tokens and running a TRUNCATE WAL
checkpoint, the ``state.reconciled`` append a DB open triggers) — so the
persisted key disagreed with the next consult's stat BY CONSTRUCTION, on every
build. That is the ``never_converged`` receipt the operator's log emitted ten
times in two days: the lane paid a megabyte write per build and no later process
could ever be served.

IC-2 re-stats the closure at write-back time and lets ONLY the audited
self-perturbation set adopt fresh triples.

WHAT MAKES THESE NON-VACUOUS
============================

The mutant is "re-stat everything", which converges beautifully and is exactly
the failure this module calls the worst one: an input a CONCURRENT writer moved
while the build ran would be recorded as current, and the next process would
serve a core missing that write as authoritative. So the file drives BOTH
directions against the same mechanism, in the same shape:

* a self-perturbed input moved during the build -> the persisted key carries the
  POST-build triple, and the next consult HITS;
* a foreign input moved during the build -> the persisted key carries the
  PRE-build triple, and the next consult DEMOTES.

A mutant that adopts everything reds the second; a mutant that adopts nothing
(the pre-IC-2 behaviour) reds the first. Neither can pass both.
"""

from __future__ import annotations

import json
import logging

import pytest

from agent_runtime import core_cache, paths
from utils import atomic_json_write


@pytest.fixture(autouse=True)
def fresh_cache_lane():
    core_cache.reset_process_state()
    yield
    core_cache.reset_process_state()


@pytest.fixture(autouse=True)
def measurable_build_stamp(monkeypatch):
    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:ic2:clean")


def _core() -> dict:
    return {"parity": {"watermark": {"event_offset": 0}}}


def _entries_file() -> dict[str, list]:
    payload = json.loads(core_cache.entries_path().read_text(encoding="utf-8"))
    return {str(row[0]): row for row in payload["entries"]}


def _write_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("snapshot_core_cache_write ok=true")
    ]


def _seed_store(root):
    """A store with the two shapes the diff has to tell apart, both populated.

    A persona-instance row (the self-perturbed TREE) and a workspace row (an
    ordinary projection input) — created BEFORE the pre-build key is taken, so
    each case moves an input the key already recorded rather than adding one.
    """

    (root / "workspaces").mkdir(parents=True, exist_ok=True)
    atomic_json_write(root / "workspaces" / "ws_alpha.json", {"id": "ws_alpha"})
    instances = paths.persona_instances_dir()
    instances.mkdir(parents=True, exist_ok=True)
    atomic_json_write(instances / "inst_probe.json", {"id": "inst_probe", "rev": 1})
    return instances / "inst_probe.json", root / "workspaces" / "ws_alpha.json"


# --------------------------------------------------------------------------- #
# 1. The build's OWN write is absorbed — the never_converged mechanism, ended
# --------------------------------------------------------------------------- #
def test_an_input_the_build_itself_moved_is_re_stat_into_the_persisted_key(
    isolate_agent_runtime_root, caplog
):
    """A persona-instance row rewritten between key and write-back keys FRESH.

    This is the whole of IC-2's converging half. ``ensure_for_personas`` rewrites
    rows on every build, so a key taken before it ran describes a store that no
    longer exists by the time the core is persisted — and the next process
    demotes, rebuilds, and writes another key that disagrees again.

    *Kill:* persist ``fingerprint`` unchanged (the pre-IC-2 behaviour). The
    persisted triple is then the stale one and this reds.
    """

    root = isolate_agent_runtime_root
    row, _workspace = _seed_store(root)
    key = core_cache.build_input_fingerprint()
    assert key is not None
    before = {entry.path: entry for entry in key.entries}[str(row)]

    atomic_json_write(row, {"id": "inst_probe", "rev": 2})
    fresh = core_cache._stat_entry(row)
    assert fresh != before, "the fixture rewrote the row without moving its triple"

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        assert core_cache.write_back(_core(), fingerprint=key) is True

    persisted = _entries_file()[str(row)]
    assert persisted[1:] == [fresh.mtime_ns, fresh.size], (
        "the write-back persisted the PRE-build triple for a row the build "
        f"itself rewrote ({persisted[1:]} vs the post-build {[fresh.mtime_ns, fresh.size]}). "
        "The next consult stats the store, disagrees with the key this build "
        "just wrote, and demotes — for a write this build made. That is the "
        "never_converged mechanism, unrepaired."
    )
    line = _write_lines(caplog)[-1]
    assert "self_perturbed_refreshed=1" in line, line
    assert "foreign_moved=0" in line, line
    assert "restat=refreshed" in line, line


def test_a_row_the_build_MINTED_is_added_to_the_persisted_key(
    isolate_agent_runtime_root
):
    """The cold-store shape: a row that did not exist when the key was taken.

    ``ensure_for_personas`` materializes a missing instance row, so on a virgin
    store the pre-build key has no triple for it at all. An appearing member of a
    self-perturbed TREE is the one addition IC-2 makes — and it is why the class
    is a directory prefix rather than a list of names.

    *Kill:* handle only paths present in BOTH stat sets. The minted row is then
    absent from the persisted key, the next consult's walk finds it, and the
    cold store takes a second build to converge again.
    """

    root = isolate_agent_runtime_root
    _seed_store(root)
    key = core_cache.build_input_fingerprint()
    assert key is not None

    minted = paths.persona_instances_dir() / "inst_minted.json"
    assert str(minted) not in {entry.path for entry in key.entries}
    atomic_json_write(minted, {"id": "inst_minted"})

    assert core_cache.write_back(_core(), fingerprint=key) is True
    assert str(minted) in _entries_file(), (
        "a persona-instance row the build minted is missing from the persisted "
        "stat set, so the next process's walk finds an input the key never "
        "described and demotes"
    )


# --------------------------------------------------------------------------- #
# 2. THE SAFETY PIN — a foreign write is NOT absorbed
# --------------------------------------------------------------------------- #
def test_an_input_a_CONCURRENT_writer_moved_keeps_its_pre_build_triple(
    isolate_agent_runtime_root, caplog
):
    """THE case IC-2 has to survive, and the one the obvious mutant fails.

    Between the build finishing and the write-back re-stat, another process may
    have moved an input too. The core was built from the OLDER state, so a key
    that adopted the fresh triple would describe a store the persisted core does
    not reflect — "a missed input serves unlabeled stale as authoritative", which
    the module header calls the failure class this whole lane exists to end.

    A workspace row is an ordinary projection input and is in no
    self-perturbation class, so its pre-build triple must survive the re-stat
    whatever the store looks like now.

    *Kill:* return the re-stat's key wholesale
    (``return _RestatOutcome(fresh, ...)``), or drop the
    ``perturbed.covers(path)`` guard so every moved entry adopts. Both converge
    faster and both red here.
    """

    root = isolate_agent_runtime_root
    _row, workspace = _seed_store(root)
    key = core_cache.build_input_fingerprint()
    assert key is not None
    before = {entry.path: entry for entry in key.entries}[str(workspace)]

    atomic_json_write(workspace, {"id": "ws_alpha", "name": "moved-by-somebody-else"})
    fresh = core_cache._stat_entry(workspace)
    assert fresh != before, "the fixture rewrote the workspace without moving its triple"

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        assert core_cache.write_back(_core(), fingerprint=key) is True

    persisted = _entries_file()[str(workspace)]
    assert persisted[1:] == [before.mtime_ns, before.size], (
        "the write-back adopted a fresh triple for an input NOTHING in the build "
        "writes. The persisted core was built before that write; the key now "
        "says the store is unchanged; the next process will serve a core missing "
        "that write as AUTHORITATIVE. The re-stat may only speak for the audited "
        "self-perturbation set — core_cache._self_perturbed_inputs."
    )
    line = _write_lines(caplog)[-1]
    assert "foreign_moved=1" in line, line
    assert "self_perturbed_refreshed=0" in line, line


def test_a_foreign_write_still_demotes_the_next_consult(
    isolate_agent_runtime_root
):
    """The same claim one layer out: the DEMOTE still happens, end to end.

    The case above reads the persisted triple; this one reads the consequence,
    because "the key kept the old triple" is only worth anything if the judgement
    that consumes it still refuses. A mutant that kept the triple and then
    compared keys loosely would pass the first and fail here.

    *Kill:* adopt fresh triples for every moved entry. The consult then MATCHES a
    core built before the workspace moved, and this reds.
    """

    root = isolate_agent_runtime_root
    _row, workspace = _seed_store(root)
    key = core_cache.build_input_fingerprint()
    assert key is not None
    atomic_json_write(workspace, {"id": "ws_alpha", "name": "moved-by-somebody-else"})
    assert core_cache.write_back(_core(), fingerprint=key) is True

    core_cache.reset_process_state()
    read = core_cache.read_persisted_core()
    assert read.matched is False, (
        "the persisted pair was judged CURRENT over a store whose workspace row "
        "moved while the build ran, so a core that does not contain that write "
        "would be served as authoritative"
    )
    assert read.reason == core_cache.DEMOTE_FINGERPRINT_MISMATCH, read.reason


def test_a_settled_store_writes_a_key_that_matches_it(
    isolate_agent_runtime_root
):
    """Nothing moved at all: the key must still describe the store, and match.

    The anti-vacuity case for the two above. A mechanism that refused to key
    anything, or that mangled the stat set while re-assembling it, would satisfy
    "a foreign write demotes" trivially — by demoting always.

    *Kill:* have ``_fingerprint_over`` order or dedupe differently from
    ``build_input_fingerprint``. The digest stops comparing equal to a fresh walk
    of the same untouched store and this reds while the demote cases stay green.
    """

    root = isolate_agent_runtime_root
    _seed_store(root)
    key = core_cache.build_input_fingerprint()
    assert key is not None
    assert core_cache.write_back(_core(), fingerprint=key) is True

    core_cache.reset_process_state()
    read = core_cache.read_persisted_core()
    assert read.matched is True, (
        "a write-back over a store nothing touched produced a key that does not "
        f"describe it: {read.demote_reason}"
    )


# --------------------------------------------------------------------------- #
# 3. The audited set is resolved, not spelled
# --------------------------------------------------------------------------- #
def test_the_self_perturbation_set_resolves_through_the_builds_own_authorities(
    isolate_agent_runtime_root
):
    """The set and the closure ask the SAME authorities, or the set is a copy.

    Every member is checked against the authority that produces it rather than
    against a restated path, for the reason the closure itself is resolved that
    way: a hand-written second list of store paths drifts, and a drifted member
    here would adopt a fresh triple for something the build does not write.

    *Kill:* re-spell any member as a literal under the store root. The literal
    stops tracking its owner and this reds on the first rename.
    """

    from agent_runtime import event_rotation
    from agent_runtime.chat_session_scope import chat_session_db_path

    perturbed = core_cache._self_perturbed_inputs()
    assert perturbed is not None, "the set could not be resolved at all"

    with core_cache._pinned_to_fingerprint_home():
        chat_db = chat_session_db_path()
    for suffix in core_cache._DB_SIBLINGS:
        assert perturbed.covers(f"{chat_db}{suffix}"), (
            f"the chat SessionDB sibling {suffix!r} is not in the set, though the "
            "build's own close drains token deltas and runs a TRUNCATE WAL "
            "checkpoint against it"
        )
    assert perturbed.covers(str(event_rotation.live_path())), (
        "the live events slice is not in the set, though a DB opened during the "
        "build appends state.reconciled to it"
    )
    assert perturbed.under_tree(str(paths.persona_instances_dir() / "any_row.json")), (
        "the persona-instances tree is not in the set, though ensure_for_personas "
        "rewrites rows in it on every build"
    )
    assert set(core_cache.BUILD_SELF_PERTURBED_CLASSES) == {
        core_cache.SELF_PERTURBED_SESSION_DB,
        core_cache.SELF_PERTURBED_PERSONA_INSTANCES,
        core_cache.SELF_PERTURBED_LIVE_EVENTS,
    }, (
        "a class was added to or removed from the enumeration without this case "
        "being told, and each member is a CLAIM that the build moves that input "
        "on every pass"
    )


def test_the_running_work_checkpoint_is_not_in_the_self_perturbation_set(
    isolate_agent_runtime_root
):
    """``processes.json`` is a FOREIGN write and must keep demoting.

    It sits beside a database that IS in the set (``running_work_store_paths``
    returns both), which makes it the nearest miss there is: a background process
    starting or exiting rewrites it with no event, and the whole reason it is
    fingerprinted is that the HUD would otherwise claim three processes are
    running twenty seconds after they exited.

    *Kill:* put every path ``running_work_store_paths()`` returns into the set
    instead of narrowing to the SQLite databases among them.
    """

    from agent_runtime.running_work import _CHECKPOINT_FILENAME, running_work_store_paths

    perturbed = core_cache._self_perturbed_inputs()
    assert perturbed is not None
    with core_cache._pinned_to_fingerprint_home():
        store_paths = running_work_store_paths()
    checkpoints = [path for path in store_paths if path.name == _CHECKPOINT_FILENAME]
    assert checkpoints, "the running_work authority stopped returning a checkpoint"
    for path in checkpoints:
        assert not perturbed.covers(str(path)), (
            f"{path} is in the self-perturbation set, so a background process "
            "starting or exiting during a build would be absorbed into the key "
            "and the next process would serve a HUD that never saw it"
        )


def test_an_unresolvable_set_refuses_to_refresh_rather_than_refreshing_nothing(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """"I could not look" is never "nothing moved" — the module's standing rule.

    An empty set would read as "the build perturbs nothing" and silently restore
    the pre-IC-2 behaviour with no receipt saying so. The refusal is typed on the
    write line instead, so a census can see an install whose keys are knowingly
    stale.

    *Kill:* return ``_SelfPerturbedInputs(frozenset(), ())`` from the except arm.
    The re-stat then reports ``restat=clean``, which claims a measurement that
    was never taken.
    """

    root = isolate_agent_runtime_root
    _row, workspace = _seed_store(root)
    key = core_cache.build_input_fingerprint()
    assert key is not None
    monkeypatch.setattr(core_cache, "_self_perturbed_inputs", lambda: None)

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        assert core_cache.write_back(_core(), fingerprint=key) is True

    line = _write_lines(caplog)[-1]
    assert "restat=unavailable" in line, line
    assert _entries_file().keys() == {entry.path for entry in key.entries}, (
        "the pre-build key was not persisted unchanged after the set refused"
    )


def test_a_caller_with_no_pre_build_key_pays_no_second_walk(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """``write_back(core)`` already walked AFTER the build — do not walk twice.

    The ``None`` default computes its key at write-back time, which is post-build
    by construction. Re-stat'ing it would be a second full walk of the store for
    an answer already in hand.

    *Kill:* run the re-stat unconditionally. The walk count doubles on every
    caller that holds no build.
    """

    _seed_store(isolate_agent_runtime_root)
    walks: list[str] = []
    real = core_cache.build_input_fingerprint

    def counted():
        walks.append("walk")
        return real()

    monkeypatch.setattr(core_cache, "build_input_fingerprint", counted)
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        assert core_cache.write_back(_core()) is True

    assert len(walks) == 1, f"the keyless write-back walked the store {len(walks)} times"
    assert "restat=skipped" in _write_lines(caplog)[-1]
