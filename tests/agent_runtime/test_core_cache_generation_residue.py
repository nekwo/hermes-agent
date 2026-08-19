"""MCF-54(ii), ruled by MCF-59: the reap may fail, but it may not fail SILENTLY.

``_reap_superseded_generations`` calls ``shutil.rmtree(..., ignore_errors=True)``
and its docstring argued that a failed removal is "not an event worth a line in a
log an operator reads". The first half of that is right and stays: a reader in
another process holding a generation open makes the removal fail on Windows, and
a write-back that has LANDED must never report failure because its housekeeping
did not. The second half did not follow. With no survivor count and no bound, a
store that keeps failing to reap accumulates whole cached cores with **nothing
counting them** — a silent drop with no accounting, which is the class this lane
refuses everywhere else.

The ruled shape, and one gate per clause of it:

* count the survivors (gate 2: ``present=`` / ``leftover=``);
* bound them (gate 1: at the bound the lane stays silent, so the receipt means
  something when it does fire);
* NAME the leftover directories (gate 2 again — the operator's refinement, and
  the point: a count says a problem exists, the names say WHICH directory to go
  unlock, and whether it is the same one every build);
* never turn housekeeping into an outage (gate 3: the write-back still returns
  ``True`` and the cache is still servable);
* never point the operator at the live generation (gate 4);
* stay bounded in the log too, without letting the cap understate the residue
  (gate 5).

The failure is simulated at the removal itself rather than by pre-planting
directories, because "the reap ran and did not manage to remove anything" is the
actual field condition — a pre-planted directory would also be reaped by a
mutant that removed everything indiscriminately, and would prove nothing.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path

import pytest

from agent_runtime import core_cache


@pytest.fixture(autouse=True)
def fresh_cache_lane():
    core_cache.reset_process_state()
    yield
    core_cache.reset_process_state()


@pytest.fixture(autouse=True)
def measurable_build_stamp(monkeypatch):
    monkeypatch.setattr(core_cache, "build_stamp_token", lambda: "probe:mcf54:clean")


def _key(path: str) -> core_cache.CoreFingerprint:
    ordered = (core_cache.FingerprintEntry(path, 11, 12),)
    return core_cache.CoreFingerprint(
        ordered, hashlib.sha256(repr(ordered).encode("utf-8")).hexdigest()
    )


def _core(offset: int) -> dict:
    return {"parity": {"watermark": {"event_offset": offset}}}


def _residue_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if core_cache.RECEIPT_GENERATION_RESIDUE in record.getMessage()
    ]


def _generation_dirs() -> list[str]:
    cache_dir = core_cache._cache_dir()
    if not cache_dir.exists():
        return []
    return sorted(
        entry.name
        for entry in cache_dir.iterdir()
        if entry.is_dir() and core_cache._is_generation_name(entry.name)
    )


def _hold_every_generation_open(monkeypatch) -> None:
    """Make every generation removal a no-op that reports nothing.

    That is EXACTLY what ``ignore_errors=True`` does when a reader holds the
    directory open: the call returns normally and the directory is still there.
    Anything not a generation still really goes, so the fixture cannot pass by
    disabling the reap wholesale.
    """

    real_rmtree = shutil.rmtree

    def refuse(path, *args, **kwargs):
        if core_cache._is_generation_name(Path(path).name):
            return
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(core_cache.shutil, "rmtree", refuse)


def _write_backs(root, count: int) -> None:
    """``count`` successful, distinct write-backs — each mints a generation."""

    for index in range(count):
        landed = core_cache.write_back(
            _core(index),
            fingerprint=_key(str(root / "workspaces" / f"ws_{index}.json")),
        )
        assert landed is True, f"write-back {index} did not land, so this case is not set up"


def _field(line: str, name: str) -> str:
    for token in line.split():
        if token.startswith(f"{name}="):
            return token[len(name) + 1 :]
    raise AssertionError(f"the receipt carries no {name}= field: {line}")


# --------------------------------------------------------------------------- #
# 1. Below the bound, the lane stays silent
# --------------------------------------------------------------------------- #
def test_a_store_at_the_bound_says_nothing(isolate_agent_runtime_root, monkeypatch, caplog):
    """A receipt that fires on a healthy store is a receipt nobody reads.

    Three generations present IS the bound, not past it, so this drives the
    boundary rather than a comfortable distance from it.

    *Kill:* make the guard ``len(leftover) + 1 < GENERATION_RESIDUE_BOUND`` (or
    drop the ``+ 1`` that counts the live generation) — the healthy store starts
    warning.
    """

    root = isolate_agent_runtime_root
    _hold_every_generation_open(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        _write_backs(root, core_cache.GENERATION_RESIDUE_BOUND)

    assert len(_generation_dirs()) == core_cache.GENERATION_RESIDUE_BOUND, (
        "the fixture did not actually leave the bound's worth of generations on "
        f"disk: {_generation_dirs()}"
    )
    assert _residue_lines(caplog) == [], (
        "a store AT the bound emitted the residue receipt, so the receipt cannot "
        "distinguish an accumulating store from a healthy one"
    )


# --------------------------------------------------------------------------- #
# 2. Past the bound, the survivors are counted AND named
# --------------------------------------------------------------------------- #
def test_a_reap_that_removed_nothing_counts_and_names_what_it_left(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """The whole row, in one line: a count, a bound, and the directory names.

    *Kill A (the count):* drop the ``_receipt_generation_residue`` call from
    ``write_back`` — the accumulation goes back to being invisible and this reds
    on the missing line.
    *Kill B (the names):* render the line with ``leftover=%d`` alone and no
    ``generations=`` tail — the count survives, the names do not, and the
    operator is back to hunting. This is the half MCF-59 corrected the parent's
    recommendation on, so it is asserted separately from the count.
    """

    root = isolate_agent_runtime_root
    _hold_every_generation_open(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        _write_backs(root, core_cache.GENERATION_RESIDUE_BOUND + 1)

    lines = _residue_lines(caplog)
    assert len(lines) == 1, (
        f"expected exactly one residue receipt for the one write-back that "
        f"crossed the bound, got {len(lines)}: {lines}"
    )
    line = lines[0]

    on_disk = _generation_dirs()
    assert _field(line, "present") == str(len(on_disk)), (
        f"the receipt's present= disagrees with the directory listing {on_disk}: {line}"
    )
    assert _field(line, "leftover") == str(len(on_disk) - 1), (
        f"leftover= must exclude the live generation: {line}"
    )

    named = _field(line, "generations").split(",")
    leftovers = [name for name in on_disk if name != _field(line, "live")]
    assert sorted(named) == sorted(leftovers), (
        "the receipt does not NAME the directories it left behind — a count "
        f"tells the operator a problem exists, the names tell them which: {line}"
    )


# --------------------------------------------------------------------------- #
# 3. Housekeeping never fails a landed write-back
# --------------------------------------------------------------------------- #
def test_the_write_back_still_succeeds_and_is_still_servable(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """Accounting, not enforcement — the accumulating store still works.

    *Kill:* raise from ``_receipt_generation_residue`` (or return ``False`` out
    of ``write_back`` when it fires) — a store with a stuck reap would then stop
    caching entirely, which is a far worse outcome than the residue.
    """

    root = isolate_agent_runtime_root
    _hold_every_generation_open(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        _write_backs(root, core_cache.GENERATION_RESIDUE_BOUND + 1)
        final = _key(str(root / "workspaces" / "ws_final.json"))
        assert core_cache.write_back(_core(99), fingerprint=final) is True, (
            "a write-back over an accumulating store reported failure: the "
            "residue receipt turned housekeeping into an outage"
        )

    assert core_cache.read_persisted_core(fingerprint=final).matched is True, (
        "the generation published over a stuck reap is not servable, so the "
        "residue path did more than count"
    )


# --------------------------------------------------------------------------- #
# 4. The live generation is never offered up for deletion
# --------------------------------------------------------------------------- #
def test_the_live_generation_is_named_as_live_and_never_as_a_leftover(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """The receipt asks an operator to remove directories. Not that one.

    *Kill:* build the leftover list without the ``name != live`` filter — the
    live generation appears in ``generations=``, and an operator following the
    receipt deletes the cache the pointer names.
    """

    root = isolate_agent_runtime_root
    _hold_every_generation_open(monkeypatch)

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        _write_backs(root, core_cache.GENERATION_RESIDUE_BOUND + 1)

    line = _residue_lines(caplog)[0]
    live = _field(line, "live")
    assert live in _generation_dirs(), f"live= does not name a directory on disk: {line}"
    assert live not in _field(line, "generations").split(","), (
        f"the receipt named the LIVE generation among the leftovers to remove: {line}"
    )


# --------------------------------------------------------------------------- #
# 5. The names are capped; the count is not
# --------------------------------------------------------------------------- #
def test_the_name_list_is_capped_without_understating_the_residue(
    isolate_agent_runtime_root, monkeypatch, caplog
):
    """A log line must stay bounded — and must not lie about how much it omitted.

    *Kill A:* drop the ``[:_GENERATION_RESIDUE_NAMES]`` slice — one line grows
    with the size of the residue, which on a badly stuck store is every
    generation the process ever wrote.
    *Kill B:* derive ``leftover=`` from the truncated list instead of the full
    one — the cap then makes a large residue read as a small one, which is the
    exact failure ``_NEVER_CONVERGED_DIFF_PATHS`` already documents next door.
    """

    root = isolate_agent_runtime_root
    _hold_every_generation_open(monkeypatch)
    total = core_cache._GENERATION_RESIDUE_NAMES + 4

    with caplog.at_level(logging.WARNING, logger="agent_runtime.core_cache"):
        _write_backs(root, total)

    line = _residue_lines(caplog)[-1]
    named = _field(line, "generations").split(",")
    assert len(named) == core_cache._GENERATION_RESIDUE_NAMES, (
        f"the name list is not capped at {core_cache._GENERATION_RESIDUE_NAMES}: {line}"
    )
    assert _field(line, "leftover") == str(total - 1), (
        "leftover= was derived from the truncated name list, so the cap makes a "
        f"large residue read as a small one: {line}"
    )
