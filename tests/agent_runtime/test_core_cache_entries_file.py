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
* a stale generation planted on disk whose paths must NOT come back, which a
  reader that trusts position over provenance cannot avoid returning (test 2);
* the pair's own presence on disk after the diagnostic's writer was made to
  raise, which a landing that wrapped all three writes together cannot show
  (test 3);
* the entries path's ABSENCE from the fingerprint it is written beside, which a
  cache that fingerprints its own diagnostic cannot produce (test 4).
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
# 2. A stale generation refuses to be read, in its own words
# --------------------------------------------------------------------------- #
def test_entries_from_another_write_back_refuse_to_be_diffed(
    isolate_agent_runtime_root,
):
    """The failure this binding exists for, driven: an entries file left behind.

    It is not hypothetical. The entries write is best effort (test 3), so a
    failed one leaves the PREVIOUS generation's entries beside a NEW sidecar —
    exactly this shape. A reader that trusted position would then diff the
    current store against a store from some earlier write-back and name paths
    from a generation nobody asked about, which reads to an operator as "these
    files moved" when they did not.

    The stale path is asserted ABSENT rather than only the reason asserted
    present: a mutant that returned the wrong generation AND a typed reason
    would satisfy a reason-only probe.

    *Kill:* drop the digest comparison in ``_persisted_entries``. The stale
    triples come back, the reason is empty, and both halves of this red.
    """

    root = isolate_agent_runtime_root
    stale_path = str(root / "workspaces" / "ws_from_the_previous_generation.json")
    current = _key([(str(root / "workspaces" / "ws_current.json"), 31, 32)])
    assert core_cache.write_back(_core(0), fingerprint=current) is True

    # A generation that is NOT the one the sidecar describes, written by hand
    # through the same atomic writer the lane uses.
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
    assert ok == "" and rebound == current.entries
    assert stale_path not in {entry.path for entry in rebound or ()}


# --------------------------------------------------------------------------- #
# 3. The diagnostic never takes the cache down with it
# --------------------------------------------------------------------------- #
def test_a_failed_entries_write_leaves_the_cache_landed_and_says_so(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """``write_back``'s contract: a failed write changes NOTHING about the build.

    The entries file is a diagnostic that explains a miss. Letting its failure
    retract a cache pair that landed would trade the thing this module exists for
    against the thing that describes it — and it would do so silently, because
    the caller only sees ``False``.

    Two independent claims, so two kills. *Kill A:* move the entries write inside
    the pair's ``try`` (or drop its ``except``) — the exception propagates, the
    write-back is not ``True``, and the return assertion reds. *Kill B:* swallow
    the failure without logging — the pair still lands, and the receipt assertion
    reds on an empty list.
    """

    root = isolate_agent_runtime_root
    key = _key([(str(root / "workspaces" / "ws_alpha.json"), 11, 12)])
    real_write = atomic_json_write

    def refuse_only_the_entries(path, payload, **kwargs):
        if str(path) == str(core_cache.entries_path()):
            raise OSError("the disk refused exactly the diagnostic")
        return real_write(path, payload, **kwargs)

    monkeypatch.setattr(core_cache, "atomic_json_write", refuse_only_the_entries)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        landed = core_cache.write_back(_core(0), fingerprint=key)

    assert landed is True, (
        "a failed entries write retracted a cache pair that landed — the "
        "diagnostic is now able to disable the lane it exists to explain"
    )
    assert core_cache.core_path().exists() and core_cache.sidecar_path().exists()
    assert _sidecar()["fingerprint"] == key.digest
    assert not core_cache.entries_path().exists(), "the fixture did not reach the entries write"

    receipts = [line for line in _lines(caplog) if "entries=false" in line]
    assert len(receipts) == 1, (
        f"the failed entries write left no countable receipt: {_lines(caplog)}"
    )
    assert "reason=entries_io" in receipts[0], receipts[0]
    assert "ok=false" not in receipts[0], (
        "the failure was worded as a WRITE REFUSAL, but the pair landed and the "
        "write-back returned True — a census keyed on ok=false would now count a "
        f"successful cache write as a failed one: {receipts[0]}"
    )


# --------------------------------------------------------------------------- #
# 4. The cache's new file does not flip the cache's own key
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
