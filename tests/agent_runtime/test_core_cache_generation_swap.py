"""THE GATE on the write-back being ONE unit (MCF-21 / H3).

WHY THIS FILE EXISTS
====================

``write_back`` landed three files — the core, the sidecar bound to its bytes,
and the stat set that makes a later miss diffable — through three independent
``os.replace`` calls. Each was atomic ALONE and the trio was not, so "these
three describe one build" was held up by two ad-hoc binding guards rather than
by one rule. MCF-21's judgement was that the guards were correct and that a
FOURTH file would make a third guard, so the moment to decide was before that,
not after.

The decision: the unit is the GENERATION. All three files are written into a
fresh directory nothing points at, and the write-back lands when one small
pointer file naming it is atomically replaced. This file drives the property
that follows — **each file failing ALONE must be unable to publish anything** —
and it drives each impossibility as its own case with its own killing mutation,
because "the trio is atomic" is three claims and a single drive would prove
whichever one the fixture happened to reach first.

WHAT MAKES THESE NON-VACUOUS
============================

The mutant here is "publish anyway", and its tell is that a consult would then
see the NEW generation — so no case asserts merely that ``write_back`` returned
``False``. Every one of them drives TWO distinguishable generations through the
lane and asserts which one a reader is served, by a value the fixture chose:

* the digest, which the previous write-back provably persisted, and
* the core's own ``event_offset``, which the previous write-back provably
  carried,

so a mutant that published a partial generation, or that resolved reads into the
staging directory, is serving the other value and cannot satisfy both.

Fault injection goes through the module's ONE writer seam
(``core_cache.atomic_json_write``, which the module calls by that global name),
selected by FILENAME rather than by full path: a staging directory's name is
minted inside the call under test and is not knowable to the fixture, which is
itself part of the property — nothing outside the write-back can name a
generation before it is published.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

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

    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:mcf21:clean")


def _key(entries: list[tuple[str, int, int]]) -> core_cache.CoreFingerprint:
    """A stat set the TEST owns, with a digest computed by the TEST's formula."""

    ordered = tuple(
        sorted(core_cache.FingerprintEntry(path, mtime, size) for path, mtime, size in entries)
    )
    return core_cache.CoreFingerprint(
        ordered, hashlib.sha256(repr(ordered).encode("utf-8")).hexdigest()
    )


def _core(offset: int) -> dict:
    return {"parity": {"watermark": {"event_offset": offset}}}


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


def _generation_dirs() -> list[str]:
    cache_dir = core_cache._cache_dir()
    if not cache_dir.exists():
        return []
    return sorted(
        entry.name
        for entry in cache_dir.iterdir()
        if entry.is_dir() and core_cache._is_generation_name(entry.name)
    )


def _refuse_writes_named(filename: str, monkeypatch):
    """Make the module's one writer raise for exactly one file of the trio.

    Selected by NAME because the staging directory is minted inside the call
    under test. Returns nothing; the patch is undone with the test.
    """

    real_write = atomic_json_write

    def refuse(path, payload, **kwargs):
        if Path(path).name == filename:
            raise OSError(f"the disk refused exactly {filename}")
        return real_write(path, payload, **kwargs)

    monkeypatch.setattr(core_cache, "atomic_json_write", refuse)


def _publish_first_generation(root) -> core_cache.CoreFingerprint:
    """A landed generation whose digest and offset the cases can name."""

    first = _key([(str(root / "workspaces" / "ws_first.json"), 11, 12)])
    assert core_cache.write_back(_core(0), fingerprint=first) is True
    return first


def _second_key(root) -> core_cache.CoreFingerprint:
    second = _key([(str(root / "workspaces" / "ws_second.json"), 21, 22)])
    return second


def _served(key: core_cache.CoreFingerprint) -> core_cache.CacheRead:
    return core_cache.read_persisted_core(fingerprint=key)


# --------------------------------------------------------------------------- #
# 0. The layout under test is the one on disk
# --------------------------------------------------------------------------- #
def test_a_write_back_publishes_a_generation_through_one_pointer(
    isolate_agent_runtime_root,
):
    """Anti-vacuity for every case below: prove the shape before asserting about it.

    Each gate below asserts that a FAILED write-back published nothing. If the
    successful path did not publish through a pointed-to generation either, all
    of them would pass against a module that had never changed. So the successful
    shape is pinned first, and pinned by structure rather than by existence: one
    generation directory, the trio inside it, the pointer naming that directory
    by name, and NOTHING flat beside it.

    *Kill:* keep writing the flat trio (``_cache_dir() / CORE_FILENAME``). The
    pointer is absent, the generation list is empty, and this reds three ways.
    """

    root = isolate_agent_runtime_root
    key = _publish_first_generation(root)

    generations = _generation_dirs()
    assert len(generations) == 1, f"a write-back did not mint exactly one generation: {generations}"

    pointer = json.loads(core_cache.pointer_path().read_text(encoding="utf-8"))
    assert pointer["generation"] == generations[0], (
        f"the pointer names {pointer['generation']!r} and the generation on disk "
        f"is {generations[0]!r} — the published trio is not the one that landed"
    )

    live = core_cache._cache_dir() / generations[0]
    for filename in (
        core_cache.CORE_FILENAME,
        core_cache.SIDECAR_FILENAME,
        core_cache.ENTRIES_FILENAME,
    ):
        assert (live / filename).exists(), f"{filename} is not inside the published generation"
        assert not (core_cache._cache_dir() / filename).exists(), (
            f"{filename} was also written flat, so two layouts are live at once "
            "and a lost pointer would resurrect the other one"
        )

    assert _served(key).matched is True, (
        "the generation this file's other cases must be served instead of a "
        "partial one is not itself servable, so their assertions prove nothing"
    )


# --------------------------------------------------------------------------- #
# 1-3. Each file failing ALONE publishes NOTHING
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "filename",
    [core_cache.CORE_FILENAME, core_cache.SIDECAR_FILENAME, core_cache.ENTRIES_FILENAME],
)
def test_one_file_of_the_trio_failing_publishes_nothing(
    isolate_agent_runtime_root, monkeypatch, caplog, filename
):
    """The torn-write property, once per file, each its own assertion.

    A generation is published; then a SECOND write-back is driven with exactly
    one of its three writes refused. The reader must still be served the first
    generation whole — proven by both of the values the first write-back chose,
    its digest and its core's ``event_offset``.

    Parametrised rather than folded into one drive because the three failures are
    three independent claims: a mutant that published the pair without the entries
    (the pre-MCF-21 behaviour) satisfies the core and sidecar arms and dies on the
    third, and only a case per file makes that visible as one red rather than a
    fixture that happened to stop early.

    *Kill A:* replace the pointer BEFORE the trio is written (move the pointer
    write to the top of the ``try``). Every arm reds: the pointer names a
    generation whose files are missing, so the reader is served nothing at all
    instead of the previous generation.
    *Kill B (the entries arm specifically):* restore the retired design — wrap the
    entries write in its own ``except`` that logs and continues. That arm reds on
    the return value and on the served offset; the other two stay green, which is
    what makes it a proof about the entries file rather than about the trio.
    """

    root = isolate_agent_runtime_root
    first = _publish_first_generation(root)
    second = _second_key(root)

    _refuse_writes_named(filename, monkeypatch)
    with caplog.at_level(logging.INFO, logger="agent_runtime.core_cache"):
        landed = core_cache.write_back(_core(1), fingerprint=second)

    assert landed is False, (
        f"the write-back reported success with {filename} unwritten, so the "
        "caller believes a generation landed that cannot exist"
    )

    read = _served(first)
    assert read.matched is True, (
        f"a refused {filename} cost the reader the generation that HAD landed: "
        f"reason={read.reason!r}. Nothing about a failed write may retract a "
        "published one"
    )
    assert read.sidecar["fingerprint"] == first.digest, read.sidecar["fingerprint"]
    assert read.core is not None
    assert read.core["parity"]["watermark"]["event_offset"] == 0, (
        "the reader was served the core from the write-back that FAILED, so a "
        "partial generation was published"
    )

    assert _served(second).matched is False, (
        "the generation whose write failed is being served as authoritative"
    )

    refusals = [line for line in _lines(caplog) if "snapshot_core_cache_write ok=false" in line]
    assert len(refusals) == 1, f"the failed landing left no countable receipt: {_lines(caplog)}"
    assert "reason=io" in refusals[0], refusals[0]
    assert not [line for line in _lines(caplog) if "entries=false" in line], (
        "the retired partial-landing receipt is being emitted again — a published "
        "pair with no entries file is the state MCF-21 made unrepresentable"
    )


# --------------------------------------------------------------------------- #
# 4. A consult during staging reads the OLD generation, never a mix
# --------------------------------------------------------------------------- #
def test_a_fully_staged_generation_serves_nobody_until_the_pointer_moves(
    isolate_agent_runtime_root, monkeypatch
):
    """The window the swap exists to close, driven at its widest point.

    Cases 1-3 fail before the generation is complete. This one COMPLETES it — all
    three files written, byte-for-byte the generation that would have been served
    — and fails only at the pointer. That is the moment a reader could see a mix,
    and the whole design is that it cannot: publication is the pointer and nothing
    else.

    **The consult happens INSIDE the window, not after it.** The first draft
    reconstructed the window after ``write_back`` returned and could not: a failed
    landing discards its staging directory, correctly, so there was nothing left to
    observe and the fixture reddened on its own anti-vacuity check. Reaching in
    through the writer seam is not a workaround for that — it is the case the gate
    is actually about, which is a reader arriving while a complete generation sits
    on disk unpublished. So the trio's completeness and the reader's answer are
    both measured at that instant.

    *Kill:* resolve the read paths into the newest generation directory rather
    than through the pointer (``_live_generation_dir`` returning
    ``max(gen dirs, key=mtime)``). The staged trio is the newest, it is served, the
    offset is 1, and this reds.
    """

    root = isolate_agent_runtime_root
    first = _publish_first_generation(root)
    published = _generation_dirs()
    second = _second_key(root)

    real_write = atomic_json_write
    window: dict = {}

    def observe_then_refuse(path, payload, **kwargs):
        if Path(path).name != core_cache.POINTER_FILENAME:
            return real_write(path, payload, **kwargs)
        staged = [name for name in _generation_dirs() if name not in published]
        window["staged"] = staged
        if len(staged) == 1:
            staged_dir = core_cache._cache_dir() / staged[0]
            window["files"] = sorted(entry.name for entry in staged_dir.iterdir())
            window["staged_digest"] = json.loads(
                (staged_dir / core_cache.SIDECAR_FILENAME).read_text(encoding="utf-8")
            )["fingerprint"]
        window["read"] = _served(first)
        window["pointer"] = json.loads(
            core_cache.pointer_path().read_text(encoding="utf-8")
        )["generation"]
        raise OSError("the pointer never moved")

    monkeypatch.setattr(core_cache, "atomic_json_write", observe_then_refuse)
    assert core_cache.write_back(_core(1), fingerprint=second) is False

    # The window was a COMPLETE unpublished generation. Without this the case
    # would pass against a write-back that gave up before writing anything, which
    # is the vacuous shape: "the old generation is served" is only evidence when a
    # whole new one was sitting on disk unpublished.
    assert len(window["staged"]) == 1, (
        f"the fixture did not stage exactly one new generation: {window['staged']}"
    )
    assert window["files"] == sorted(
        (
            core_cache.CORE_FILENAME,
            core_cache.SIDECAR_FILENAME,
            core_cache.ENTRIES_FILENAME,
        )
    ), f"the staged generation was not complete at the pointer write: {window['files']}"
    assert window["staged_digest"] == second.digest

    read = window["read"]
    assert read.matched is True, read.reason
    assert read.core is not None
    assert read.core["parity"]["watermark"]["event_offset"] == 0, (
        "a complete but UNPUBLISHED generation was served, so the pointer is not "
        "the thing that publishes"
    )
    assert read.sidecar["fingerprint"] == first.digest
    assert window["pointer"] == published[0], (
        "the pointer moved before the landing it is supposed to BE"
    )

    # And the failed landing left no residue: the staging directory the pointer
    # never named is discarded rather than accumulating until the next reap.
    assert _generation_dirs() == published, _generation_dirs()


# --------------------------------------------------------------------------- #
# 5. Crash residue is inert, and it is reaped
# --------------------------------------------------------------------------- #
def test_an_orphaned_generation_neither_serves_nor_survives(
    isolate_agent_runtime_root,
):
    """Two claims about a directory the pointer never named, so two assertions.

    A crash between staging and publishing leaves a complete, internally VALID
    generation on disk. It must serve nobody — the pointer is the only authority,
    not recency — and it must not accumulate.

    The orphan is deliberately the newest by BOTH orderings a plausible mutant
    would reach for: it is written last (newest mtime) and named to sort last
    (``gen-ffff…``). A consult that preferred either would serve it.

    *Kill A:* have the consult prefer the newest generation directory by mtime (or
    by name) over the pointer. The first half reds on the offset.
    *Kill B:* drop the reap. The second half reds with the orphan still present
    after a successful write-back.
    """

    root = isolate_agent_runtime_root
    first = _publish_first_generation(root)
    live = _generation_dirs()[0]

    orphan_name = "gen-ffffffffffffffff-ffffffff"
    assert core_cache._is_generation_name(orphan_name)
    assert orphan_name > live, "the orphan must sort AFTER the live generation to be a discriminator"
    orphan = core_cache._cache_dir() / orphan_name
    for filename in (
        core_cache.CORE_FILENAME,
        core_cache.SIDECAR_FILENAME,
        core_cache.ENTRIES_FILENAME,
    ):
        atomic_json_write(orphan / filename, {"orphan": True, "which": filename}, indent=None)

    read = _served(first)
    assert read.matched is True, read.reason
    assert read.core is not None
    assert read.core["parity"]["watermark"]["event_offset"] == 0, (
        "the newest directory on disk was served instead of the one the pointer "
        "names, so a crash's residue is live data"
    )

    third = _key([(str(root / "workspaces" / "ws_third.json"), 31, 32)])
    assert core_cache.write_back(_core(2), fingerprint=third) is True

    surviving = _generation_dirs()
    assert orphan_name not in surviving, (
        f"the orphaned staging directory outlived a successful write-back: {surviving}"
    )
    assert live not in surviving, f"the superseded generation was not reaped: {surviving}"
    assert len(surviving) == 1, surviving
    assert _served(third).matched is True


# --------------------------------------------------------------------------- #
# 6. The legacy flat trio is refused, and reaped — the arm this stage chose
# --------------------------------------------------------------------------- #
def test_the_pre_generation_flat_trio_is_never_adopted_and_is_reaped(
    isolate_agent_runtime_root,
):
    """The legacy arm, named and driven: ONE benign demote, never a silent adoption.

    Every store that held a cache before MCF-21 has the flat trio in
    ``_cache_dir()`` and no pointer. Adopting it as generation zero under the full
    judgement was the alternative on offer and it is refused, because it keeps a
    second resolution alive forever: a pointer that is ever LOST then falls back
    to whatever flat trio is on disk and can serve an arbitrarily old core as
    authoritative — the missed-input direction, reached through the one path
    nobody exercises.

    The fixture makes the refusal a DECISION rather than an accident of an invalid
    fixture: the flat trio here is one this module published seconds earlier and
    that ``read_persisted_core`` provably MATCHED, demoted to flat by moving the
    files. So it is not refused for being torn, stale, or unbound. It is refused
    for having no pointer.

    *Kill:* fall back to the flat layout when no generation resolves
    (``_live_generation_dir`` returning ``_cache_dir()``). The legacy trio matches
    again and this reds on the first assertion.
    """

    root = isolate_agent_runtime_root
    key = _publish_first_generation(root)
    assert _served(key).matched is True, "the fixture never had a matching pair to demote"

    # Demote the published generation to the pre-MCF-21 layout, byte-for-byte.
    live = core_cache._cache_dir() / _generation_dirs()[0]
    for filename in (
        core_cache.CORE_FILENAME,
        core_cache.SIDECAR_FILENAME,
        core_cache.ENTRIES_FILENAME,
    ):
        (core_cache._cache_dir() / filename).write_bytes((live / filename).read_bytes())
        (live / filename).unlink()
    live.rmdir()
    core_cache.pointer_path().unlink()

    read = _served(key)
    assert read.matched is False and read.reason == core_cache.DEMOTE_ABSENT, (
        "a flat trio with no pointer was adopted as the live generation, so a "
        f"lost pointer can resurrect an arbitrarily old core: {read.reason!r}"
    )
    assert read.core is None, "an unpointed core reached a caller that could paint it stale"

    # And the one-time cleanup: the first successful write-back removes it.
    second = _second_key(root)
    assert core_cache.write_back(_core(1), fingerprint=second) is True
    for filename in (
        core_cache.CORE_FILENAME,
        core_cache.SIDECAR_FILENAME,
        core_cache.ENTRIES_FILENAME,
    ):
        assert not (core_cache._cache_dir() / filename).exists(), (
            f"the legacy flat {filename} survived a write-back, so every store "
            "carries a second unreadable layout forever"
        )
    assert _served(second).matched is True


# --------------------------------------------------------------------------- #
# 7. A pointer that cannot be trusted resolves nothing OUTSIDE the cache
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "generation",
    [
        "../../../../etc",
        "gen-../escape",
        "",
        "snapshot",
        "gen-Not Hex",
    ],
)
def test_a_pointer_naming_something_this_module_never_minted_is_refused(
    isolate_agent_runtime_root, generation
):
    """The pointer comes off disk, and it is joined onto a path.

    ``_live_generation_dir`` resolves whatever the pointer says, so a corrupt or
    hostile pointer would otherwise send the judgement at bytes anywhere on the
    filesystem — and the judgement's whole job is to decide whether bytes may be
    served as authoritative. The charset admits exactly what
    ``_new_generation_name`` mints, so escaping is unrepresentable rather than
    filtered.

    *Kill:* accept any non-empty string as a generation name. The traversal arms
    resolve outside ``_cache_dir()`` and this reds.
    """

    atomic_json_write(core_cache.pointer_path(), {"generation": generation}, indent=None)

    assert core_cache._live_generation_name() is None, generation
    resolved = core_cache.core_path().resolve()
    assert core_cache._cache_dir().resolve() in resolved.parents, (
        f"a pointer naming {generation!r} resolved the live core to {resolved}, "
        "outside the directory this cache owns"
    )
    assert core_cache.read_persisted_core(fingerprint=_key([("x", 1, 1)])).reason == (
        core_cache.DEMOTE_ABSENT
    )
