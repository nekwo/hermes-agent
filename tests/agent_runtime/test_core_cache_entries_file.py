"""MC-3 / P4 arm 1 — the sidecar keeps the stat set its digest summarised.

WHY THIS FILE EXISTS
====================

``CoreFingerprint.entries`` has always been kept in memory "so a divergence
investigation can diff two fingerprints and name the file that moved" — and it
was never persisted. The measured consequence (investigation finding A1-b,
2026-08-18): no process, not the serve and not a read-only investigation, could
name the file that moved between two boots at an unchanged events offset. The
operator saw ``reason=fingerprint_mismatch`` on every same-commit boot with
nothing to act on, and between two read-only walks fifteen minutes apart the
entry count moved 23,107 → 23,106 with WHICH entry left unrecoverable (C-8).

This file gates the file that makes the diff computable at all. The demote line
that CONSUMES it is arm 2's subject and is gated beside the receipts it joins.

WHAT MAKES THESE NON-VACUOUS
============================

The mutant here is "write something called entries.json" — a file's existence is
trivially forgeable, so no case asserts only that it exists. Each one drives a
value the file must have COMPUTED and pairs it with a discriminator the mutant
cannot also set:

* two DISTINCT keys through one write path, so a constant payload satisfies at
  most one of them (test 1);
* an entries file planted on disk whose paths must NOT come back, which a reader
  that trusts position over provenance cannot avoid returning (test 2);
* the entries path's ABSENCE from the fingerprint it is written beside, which a
  cache that fingerprints its own diagnostic cannot produce (test 3).

WHAT LEFT THIS FILE, AND WHERE IT WENT (MCF-21)
===============================================

A fourth case used to pin the arm this file was designed around: the entries
write was BEST EFFORT, so a failed one left the core and sidecar landed, the
write-back returning ``True``, and a receipt worded ``entries=false
reason=entries_io``. The generation swap retired that state — a published
generation missing any of its three files is unrepresentable — so the case was
not edited to agree with the new world, it was replaced by the opposite claim in
``test_core_cache_generation_swap.py``: an entries failure publishes NOTHING.
That inversion is the mutation which proves the design changed rather than the
docstrings.
"""

from __future__ import annotations

import hashlib
import json
import logging

import pytest

from agent_runtime import core_cache
from utils import atomic_json_write


@pytest.fixture(autouse=True)
def fresh_cache_lane():
    """Every case starts and ends with a process that has built nothing."""

    core_cache.reset_process_state()
    yield
    core_cache.reset_process_state()


@pytest.fixture(autouse=True)
def measurable_build_stamp(monkeypatch):
    """A stamp the write lane can record without asking git about this checkout."""

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:mc3:clean")


def _key(entries: list[tuple[str, int, int]]) -> core_cache.CoreFingerprint:
    """A stat set the TEST owns, with a digest computed by the TEST's formula.

    Not borrowed from ``build_input_fingerprint``: the module only ever compares
    digests for equality, so a case that reused production's formula would pass
    just as happily if that formula stopped depending on the entries at all.
    """

    ordered = tuple(
        sorted(core_cache.FingerprintEntry(path, mtime, size) for path, mtime, size in entries)
    )
    return core_cache.CoreFingerprint(
        ordered, hashlib.sha256(repr(ordered).encode("utf-8")).hexdigest()
    )


def _core(offset: int) -> dict:
    return {"parity": {"watermark": {"event_offset": offset}}}


def _sidecar() -> dict:
    return json.loads(core_cache.sidecar_path().read_text(encoding="utf-8"))


def _entries_file() -> dict:
    return json.loads(core_cache.entries_path().read_text(encoding="utf-8"))


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


# --------------------------------------------------------------------------- #
# 1. The file lands, and it is bound to the key it describes
# --------------------------------------------------------------------------- #
def test_the_entries_file_lands_bound_to_the_digest_it_describes(
    isolate_agent_runtime_root,
):
    """A stat set beside the sidecar is only usable if it says WHOSE it is.

    Three files in one directory written by three ``os.replace`` calls are three
    separate generations the moment any of them fails, so position beside the
    sidecar proves nothing. The payload carries the digest it belongs to, and
    that binding is what a reader checks.

    Two distinct keys are driven through the same write path: a writer that
    persisted a constant — or that persisted the entries of whichever key it saw
    first — satisfies at most one of them.

    *Kill:* write the entries without the ``fingerprint`` field. The file still
    lands, still carries every triple, and the binding assertion reds because
    nothing on disk says which write-back it came from.
    """

    root = isolate_agent_runtime_root
    first = _key([(str(root / "workspaces" / "ws_alpha.json"), 11, 12)])
    second = _key(
        [
            (str(root / "workspaces" / "ws_alpha.json"), 11, 12),
            (str(root / "workspaces" / "ws_beta.json"), 21, 22),
        ]
    )
    assert first.digest != second.digest, "the fixture drove one value twice"

    for index, key in enumerate((first, second)):
        assert core_cache.write_back(_core(index), fingerprint=key) is True

        payload = _entries_file()
        assert payload["fingerprint"] == key.digest, (
            "the entries file does not name the digest it describes, so a reader "
            "cannot tell this generation from the one before it"
        )
        assert payload["fingerprint"] == _sidecar()["fingerprint"], (
            "the entries file and the sidecar disagree about which write-back "
            "they belong to, which is the state the binding rule exists to catch"
        )
        assert [tuple(row) for row in payload["entries"]] == [
            tuple(entry) for entry in key.entries
        ], "the persisted stat set is not the one this write-back summarised"


# --------------------------------------------------------------------------- #
# 2. An entries file the sidecar does not describe refuses to be read
# --------------------------------------------------------------------------- #
def test_entries_from_another_write_back_refuse_to_be_diffed(
    isolate_agent_runtime_root,
):
    """The binding, RE-AIMED by MCF-21 and still convicting.

    What it used to guard is gone: the entries write was best effort, so a failed
    one left the PREVIOUS generation's entries beside a NEW sidecar, and a reader
    trusting position would diff the current store against some earlier one. The
    generation swap makes that shape unrepresentable — one directory, one pointer
    replace — and the case was NOT deleted with it.

    It is re-aimed at what a swap cannot prevent, and the fixture now drives that
    literally: the entries file inside the LIVE generation is overwritten with
    content bound to a digest nobody published. Position beside the sidecar is
    still not provenance, and the alternative to refusing is a receipt that NAMES
    FILES an operator will go and investigate, computed against a stat set that is
    not this pair's.

    The stale path is asserted ABSENT rather than only the reason asserted
    present: a mutant that returned the wrong stat set AND a typed reason would
    satisfy a reason-only probe.

    *Kill:* drop the digest comparison in ``_persisted_entries``. The planted
    triples come back, the reason is empty, and both halves of this red.
    """

    root = isolate_agent_runtime_root
    stale_path = str(root / "workspaces" / "ws_from_the_previous_generation.json")
    current = _key([(str(root / "workspaces" / "ws_current.json"), 31, 32)])
    assert core_cache.write_back(_core(0), fingerprint=current) is True

    # Content the published write-back did not put there, written into the LIVE
    # generation by hand through the same atomic writer the lane uses.
    atomic_json_write(
        core_cache.entries_path(),
        {"fingerprint": "a-digest-from-some-earlier-write-back", "entries": [[stale_path, 41, 42]]},
        indent=None,
        separators=(",", ":"),
        sort_keys=True,
    )

    entries, reason = core_cache._persisted_entries(expect_digest=_sidecar()["fingerprint"])

    assert entries is None, (
        "the reader accepted entries bound to another write-back; every path it "
        f"returned describes a store nobody asked about: {entries}"
    )
    assert reason == core_cache.DIFF_UNAVAILABLE_ENTRIES_UNBOUND, reason
    assert reason != core_cache.DIFF_UNAVAILABLE_NO_ENTRIES, (
        "an unbound generation was reported as a MISSING one — the two ask for "
        "opposite responses (wait for the next write-back vs the three files "
        "disagree about which generation they describe)"
    )

    # And the bound case is genuinely reachable through the same call, so the
    # refusal above is a decision rather than this reader never answering.
    assert core_cache.write_back(_core(1), fingerprint=current) is True
    rebound, ok = core_cache._persisted_entries(expect_digest=_sidecar()["fingerprint"])
    assert ok == "" and rebound is not None and rebound.entries == current.entries
    assert stale_path not in {entry.path for entry in rebound.entries}


# --------------------------------------------------------------------------- #
# 3. The cache's new file does not flip the cache's own key
# --------------------------------------------------------------------------- #
def test_the_entries_file_is_outside_the_key_it_is_written_beside(
    isolate_agent_runtime_root,
):
    """The one way this arm could silently make the cache WORSE.

    A cache whose own writes move the fingerprint invalidates itself on every
    boot — the exact shape that shipped and cost every same-commit boot a full
    rebuild. Adding a THIRD file to the cache directory re-opens that hole if it
    ever lands anywhere the walk can see, so the claim is pinned on the new file
    specifically: its path must be absent from the stat set taken after it was
    written, and the digest must not move.

    Nothing is built here on purpose — the only filesystem change between the two
    stat sets is the cache's own three files, so an inequality has exactly one
    cause.

    *Kill:* remove ``CORE_CACHE_DIRNAME`` from ``_EXCLUDED_STORE_ENTRIES``. The
    entries path appears in the walk and the digest moves.
    """

    assert not isolate_agent_runtime_root.exists(), (
        "this case needs a virgin store root — the cache's own directory must be "
        "the first thing written into it"
    )
    before = core_cache.build_input_fingerprint()
    assert before is not None

    assert core_cache.write_back(_core(0)) is True
    assert core_cache.entries_path().exists(), "the drive never wrote the file under test"

    after = core_cache.build_input_fingerprint()
    assert after is not None
    walked = set(core_cache.iter_fingerprint_paths(after))
    assert str(core_cache.entries_path()) not in walked, (
        "the cache fingerprints its own diagnostic, so every write-back "
        "guarantees the next process a miss"
    )
    assert before.digest == after.digest, (
        "writing the cache changed the fingerprint of the inputs, so a cache "
        "write can never be validated by the process that reads it next"
    )


# =========================================================================== #
# MC-3 / P4 arm 2 — the demote line names the paths, LAST, with its caveat
# =========================================================================== #
# ``_log_demote`` emitted ``core_source=rebuilt caller=… reason=… inputs=…`` and
# nothing else; only ``never_converged`` carried a ``diff=``, and it cannot fire
# on any boot shape (A2). So the one line the operator DOES see on every boot was
# the one that said least.
#
# WHAT MAKES THESE NON-VACUOUS. "A diff is present" and "the diff is RIGHT" are
# two claims, so they get two kills. Every case drives a value the tail must have
# COMPUTED — a path the fixture moved, a count that differs from the capped
# sample, an ordering that only holds if the field is genuinely last — and pairs
# it with a discriminator: the paths that did NOT move must not appear, and the
# demote reasons that are not "an input moved" must not grow a tail at all.


def _demote_lines(caplog) -> list[str]:
    return [line for line in _lines(caplog) if "core_source=rebuilt" in line]


def _plant_entries(digest: str, rows: list[tuple[str, int, int]]) -> dict:
    """A persisted generation the TEST owns, plus the sidecar that binds to it."""

    core_cache.entries_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(
        core_cache.entries_path(),
        {"fingerprint": digest, "entries": [[path, mtime, size] for path, mtime, size in rows]},
        indent=None,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {"fingerprint": digest}


def _diff_paths(line: str) -> list[str]:
    return line.split("diff=", 1)[1].split(",")


def test_a_fingerprint_miss_names_the_input_that_moved(
    isolate_agent_runtime_root, caplog
):
    """A1-b, end to end and through the real walk: the miss becomes actionable.

    A pair is persisted, EXACTLY ONE input is added, and a fresh process state
    consults — the boot shape, minus the process boundary. The line the operator
    reads must name that file.

    The directory is created BEFORE the first walk on purpose, so the single
    filesystem change between the two stat sets is the file itself: a directory
    contributes its path and never its mtime, so nothing else can move.

    *Kill A:* log without the diff tail — a receipt that carries no path cannot
    name one. *Kill B:* name a hard-coded path instead of the computed diff —
    the tail is present, ``changed=`` still counts, and the driven path is
    absent. Two kills, because a present diff and a CORRECT diff are two claims.
    """

    root = isolate_agent_runtime_root
    (root / "workspaces").mkdir(parents=True)
    stable = root / "workspaces" / "ws_that_never_moves.json"
    stable.write_text("{}", encoding="utf-8")

    before = core_cache.build_input_fingerprint()
    assert before is not None
    assert core_cache.write_back(_core(0), fingerprint=before) is True

    moved = root / "workspaces" / "ws_that_moved.json"
    moved.write_text("{}", encoding="utf-8")

    core_cache.reset_process_state()
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        decision = core_cache.consult(caller="probe")

    assert decision.core is None and decision.demoted, (
        "the fixture did not produce a demote, so nothing below is about a miss"
    )
    lines = _demote_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert f"reason={core_cache.DEMOTE_FINGERPRINT_MISMATCH}" in line, line
    assert f"diff_scope={core_cache.DIFF_SCOPE_LAST_PAIR}" in line, line
    assert str(moved) in line, (
        "the demote did not name the one input the fixture moved, which is the "
        f"whole point of the tail: {line}"
    )
    assert str(stable) not in line, (
        "the demote named an input that did not move, so it is reporting the "
        f"stat set rather than the diff: {line}"
    )
    assert "changed=1" in line, line


def test_changed_counts_the_drift_and_the_sample_is_capped_beneath_it(
    isolate_agent_runtime_root, caplog
):
    """``changed=`` is the COUNT; ``diff=`` is a bounded sample of it.

    The cap exists so one line cannot become a store dump, and the count exists
    so the cap can never make a large drift read as a small one. That is only
    true if they are computed from different things — which is exactly what a
    single drive cannot prove, so two drifts are driven: one beneath the cap and
    one above it.

    *Kill:* emit ``changed=len(sample)``. The under-cap arm still passes (3 is 3),
    and the over-cap arm reds — which is why both arms are here.
    """

    root = isolate_agent_runtime_root
    cap = core_cache._NEVER_CONVERGED_DIFF_PATHS
    base = [(str(root / "workspaces" / f"ws_{index}.json"), 10, 10) for index in range(12)]

    for moving in (3, cap + 2):
        sidecar = _plant_entries("persisted-digest", base)
        key = _key(
            [
                (path, mtime + 1, size) if index < moving else (path, mtime, size)
                for index, (path, mtime, size) in enumerate(base)
            ]
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
            core_cache._log_demote(
                caller="probe",
                reason=core_cache.DEMOTE_FINGERPRINT_MISMATCH,
                key=key,
                sidecar=sidecar,
            )

        line = _demote_lines(caplog)[0]
        assert f"changed={moving}" in line, (
            f"{moving} inputs moved and the line says otherwise: {line}"
        )
        assert len(_diff_paths(line)) == min(moving, cap), (
            f"the sample is not bounded by the cap ({cap}): {line}"
        )


def test_the_diff_is_the_last_field_on_the_line(isolate_agent_runtime_root, caplog):
    """Nothing may be field-parsed after ``diff=``, so nothing may follow it.

    A path can contain spaces and the list is variable-length, so a parser that
    met another ``key=value`` after the diff would read part of a filename as a
    field — the same rule ``_receipt_never_converged`` already follows, and the
    reason the tail could be approved as additive at all.

    The assertion is on the rendered STRING's tail rather than on field presence,
    because "present" is exactly what an ordering bug leaves intact.

    *Kill:* move ``inputs=`` after the tail. Every field is still on the line and
    the ending is no longer the diff.
    """

    root = isolate_agent_runtime_root
    base = [(str(root / "workspaces" / "ws_a.json"), 10, 10)]
    sidecar = _plant_entries("persisted-digest", base)
    key = _key([(str(root / "workspaces" / "ws_a.json"), 11, 10)])

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        core_cache._log_demote(
            caller="probe",
            reason=core_cache.DEMOTE_FINGERPRINT_MISMATCH,
            key=key,
            sidecar=sidecar,
        )

    line = _demote_lines(caplog)[0]
    assert line.endswith(f"diff={base[0][0]}"), (
        f"something is field-parseable after the variable-length diff list: {line}"
    )


@pytest.mark.parametrize(
    "reason",
    [
        core_cache.DEMOTE_BUILD_STAMP_MISMATCH,
        core_cache.DEMOTE_CONTRACT_MISMATCH,
        core_cache.DEMOTE_RUNTIME_ROOT_MISMATCH,
        core_cache.DEMOTE_HOME_MISMATCH,
    ],
)
def test_only_a_fingerprint_miss_grows_a_tail(
    isolate_agent_runtime_root, caplog, reason
):
    """A diff on the other reasons would be a measurement of the wrong thing.

    ``build_stamp_mismatch`` says the operator upgraded; ``contract_mismatch``
    says the schema moved; ``runtime_root_mismatch`` and ``home_mismatch`` say
    the pair answers a different question entirely. None of them is "an input
    moved", and a diff attached to one would name every file the upgrade touched
    and read to a census as store churn.

    The same fixture that produces NO tail here is proven to produce one — the
    last block drives ``fingerprint_mismatch`` over the identical inputs — so the
    silence is a decision rather than a fixture that could never have diffed.

    *Kill:* compute the diff for every demote reason.
    """

    root = isolate_agent_runtime_root
    base = [(str(root / "workspaces" / "ws_a.json"), 10, 10)]
    sidecar = _plant_entries("persisted-digest", base)
    key = _key([(str(root / "workspaces" / "ws_a.json"), 11, 10)])

    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        core_cache._log_demote(caller="probe", reason=reason, key=key, sidecar=sidecar)

    quiet = _demote_lines(caplog)[0]
    assert f"reason={reason}" in quiet, quiet
    assert "changed=" not in quiet, quiet
    assert "diff=" not in quiet, (
        f"a demote that is not about an input moving grew an input diff: {quiet}"
    )

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        core_cache._log_demote(
            caller="probe",
            reason=core_cache.DEMOTE_FINGERPRINT_MISMATCH,
            key=key,
            sidecar=sidecar,
        )
    assert "diff=" in _demote_lines(caplog)[0], (
        "the fixture cannot produce a tail at all, so the silence above proves "
        "nothing about which reason grows one"
    )


def test_a_matched_consult_reads_no_entries_at_all(
    isolate_agent_runtime_root, monkeypatch
):
    """The hit path — every boot the cache works on — must pay nothing for this.

    The entries file is 3.4 MiB on the operator's store. Reading it to decide
    that nothing needs explaining would make the diagnostic a tax on the very
    outcome it is not about.

    The counter lives HERE, wrapping the path helper, where nothing in
    ``core_cache`` can set it — the ``_CountingStores`` pattern. Both consult
    shapes are driven, because they reach the judgement by different routes (the
    shared armed-window read, and the handed-in-key read) and only one of them
    would catch an eager read planted in the other.

    *Kill:* read the entries eagerly in ``_judge_persisted_pair``. The counter
    reads 1 on both shapes.
    """

    key = core_cache.build_input_fingerprint()
    assert key is not None
    assert core_cache.write_back(_core(0), fingerprint=key) is True

    touches: list[str] = []
    real_entries_path = core_cache.entries_path

    def counted_entries_path():
        touches.append("entries")
        return real_entries_path()

    monkeypatch.setattr(core_cache, "entries_path", counted_entries_path)

    for handed_in in (key, None):
        core_cache.reset_process_state()
        touches.clear()
        decision = core_cache.consult(caller="probe", fingerprint=handed_in)
        assert decision.core is not None and not decision.demoted, (
            "the fixture did not produce a cache HIT, so a zero read count below "
            f"would only mean the path was never taken (handed_in={handed_in is not None})"
        )
        assert touches == [], (
            "a matched consult touched the entries file, so every hitting boot "
            f"now pays for a diagnostic about misses: {touches}"
        )
