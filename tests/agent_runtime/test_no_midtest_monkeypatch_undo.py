"""THE structural gate: no test under ``tests/agent_runtime/`` calls ``.undo()``.

=============================================================================
WHY THIS FILE EXISTS
=============================================================================

``monkeypatch`` is ONE ``MonkeyPatch`` instance per test function, shared by
every fixture that requests it and by the test body. Its ``undo()`` takes no
argument and unwinds the ENTIRE stack — it cannot drop "the patch I made".

This package's autouse ``isolate_agent_runtime_root``
(``tests/agent_runtime/conftest.py``) pins ``HERMES_AGENT_RUNTIME_ROOT`` and
the two worktree bases through that same instance. So a body that calls
``monkeypatch.undo()`` to drop its own stub silently unpins the sandbox, and
everything it does afterwards runs against the OPERATOR's live runtime root.

That is not hypothetical. Three sites did it, and on 2026-08-17 the damage was
found in the live tree (EG-0.1 / HC-H1):

* ``test_office_state_patches.py:751`` dropped a projection cap and three lines
  later wrote the live store — the leaked actor ``ws_office_patch_test`` sat at
  revision 67 in ``X:/Eternia/.hermes`` and climbed once per suite run;
* ``test_persona_chat_continuity.py:156`` dropped an unlock stub and then took a
  persona-chat root lease out there (the lease file is the physical evidence);
* ``test_mcp_admission_r2.py:327`` dropped a deregister stub in a ``finally``.

All three had a real reason to drop a patch mid-test. The correct instrument is
a SCOPED context, which unwinds precisely one block's patches:

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(...)
        ...                     # the patched half
    ...                         # the unpatched half; only your patch is gone

=============================================================================
TWO WITNESSES, DIFFERENT MECHANISMS
=============================================================================

1. The **behavioural** one: ``isolate_agent_runtime_root``'s teardown tripwire.
   It watches a sentinel the fixture minted per test, so an unwind reddens the
   exact test that performed it, whatever spelling it used — including spellings
   this file's walker cannot see (``undo`` reached through an alias, a callback,
   ``getattr``).
2. The **structural** one: this file. It reddens in review, before the suite is
   ever run against a live root, and it names the file and line.

Neither subsumes the other, which is why both ship.

=============================================================================
THE SHAPE, AND WHY THERE IS NO ALLOWLIST
=============================================================================

AST-based, per the repo rule for structural gates
(``test_s55_registered_events_have_emitters``,
``test_snapshot_contract_version_authority``): a substring scan would flag this
file's own prose and its synthetic fixtures below, which is exactly how a gate
earns a self-exemption and then a second one.

The detector flags **every** ``<expr>.undo()`` call, not only receivers spelled
``monkeypatch``. Deliberately:

* the fixture is routinely aliased (``mp``, ``m``, ``mpatch``, a
  ``request.getfixturevalue`` handle), so a receiver-name net would be the fuzzy
  half of the gate — and the one site a future author reaches for is whichever
  spelling the net missed;
* there is not one legitimate ``.undo()`` under ``tests/agent_runtime/`` today
  (verified: the three sites above were the complete set), so the net costs
  nothing to carry;
* consequently there is NO allowlist. If some future object genuinely needs an
  ``.undo()`` here, the gate gets widened by hand, in review, with the reason
  written down — a decision a human should make once, rather than one an
  exemption dict absorbs one entry at a time. Note that a scoped
  ``MonkeyPatch.context()`` needs no ``undo()`` call at all, so the pressure to
  widen is close to zero by construction.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest


#: The tree this gate polices. Scoped to this package because the autouse
#: root-isolation fixture that makes the defect dangerous is this package's.
#: Other packages call ``undo()`` too (``tests/cron``, ``tests/hermes_cli``,
#: ``tests/plugins``, ``tests/tools``) and are out of scope on purpose — they
#: have no runtime-root pin to unwind, and widening the gate to them without
#: reading each site would be a claim this file has not checked.
SCANNED_ROOT = "tests/agent_runtime"

#: Cheap prefilter token. SOUND by construction, not by luck: the detector only
#: ever reports an ``ast.Attribute`` whose ``attr`` is exactly ``"undo"``, and
#: every source spelling of such an attribute contains these four bytes. So a
#: file the prefilter skips cannot contain a finding.
#: :func:`test_the_prefilter_cannot_hide_a_finding` drives that claim.
PREFILTER_TOKEN = b"undo"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def undo_calls(tree: ast.Module) -> list[tuple[int, str]]:
    """Every ``<expr>.undo(...)`` CALL in ``tree``, as ``[(lineno, spelling)]``.

    Calls only. A bare reference — ``request.addfinalizer(mp.undo)`` — schedules
    an unwind for teardown, which is when the fixture's own pins come down
    anyway, so it is not the defect and is not flagged.

    Pure and AST-only so the vacuity cases below can drive it on synthetic
    source instead of on the tree.
    """

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "undo":
            found.append((node.lineno, f"{_receiver(func.value)}.undo()"))
    return sorted(set(found))


def _receiver(node: ast.expr) -> str:
    """A readable spelling of the call's receiver, for the failure message."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_receiver(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return f"{_receiver(node.func)}(...)"
    return "<expr>"


@functools.lru_cache(maxsize=1)
def _scanned_cached() -> tuple[tuple[str, ast.Module | None], ...]:
    return tuple(_scan().items())


def _scanned() -> dict[str, ast.Module | None]:
    """``{path: tree or None}`` for every module under :data:`SCANNED_ROOT`.

    ``None`` means the prefilter skipped it — recorded rather than dropped so
    :func:`test_the_gate_scanned_a_real_tree` can count what was LOOKED AT, not
    just what was parsed (a walker that silently stopped finding files would
    otherwise pass this gate forever).
    """

    return dict(_scanned_cached())


def _scan() -> dict[str, ast.Module | None]:
    directory = _repo_root() / SCANNED_ROOT
    trees: dict[str, ast.Module | None] = {}
    for path in sorted(directory.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_bytes()
        if PREFILTER_TOKEN not in source:
            trees[str(path)] = None
            continue
        try:
            trees[str(path)] = ast.parse(source.decode("utf-8", errors="replace"))
        except SyntaxError:
            trees[str(path)] = None
    return trees


# --------------------------------------------------------------------------- #
# The scan is paid HERE, at import
# --------------------------------------------------------------------------- #
# Measured on the tree this landed against: 321 files read, 8 of them past the
# prefilter and parsed, ~50 ms total — nowhere near the 30 s per-test cap. The
# warm is still taken at module scope, for the reason
# test_snapshot_contract_version_authority.py records the hard way: three tests
# share this walk, WHICH one pays is decided by collection order, and
# `--timeout-method=thread` KILLS the process, so a walk that ever grew past the
# cap would take every test after it down. Collection is not clocked by
# pytest-timeout, so paying it here removes the failure mode instead of leaving
# it a headroom argument.
_scanned_cached()

_SCAN_CACHE_SIZE_AT_IMPORT = _scanned_cached.cache_info().currsize


def test_the_shared_scan_is_paid_at_import():
    """REGRESSION GUARD pinning the cause, not a clock (a wall-time assertion
    here would be a flake generator and would assert the symptom)."""

    assert _SCAN_CACHE_SIZE_AT_IMPORT == 1, (
        "the scan was NOT warmed at module import (cache size at import: "
        f"{_SCAN_CACHE_SIZE_AT_IMPORT}). Restore the module-scope warm."
    )
    assert _scanned_cached.cache_info().misses == 1, (
        f"the scan ran more than once; a caller is going around the cache "
        f"({_scanned_cached.cache_info()})"
    )


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #
def test_no_test_in_this_package_unwinds_the_shared_monkeypatch():
    """THE GATE. See the module docstring for the incident it fences."""

    offenders: list[str] = []
    for path, tree in _scanned().items():
        if tree is None:
            continue
        for lineno, spelling in undo_calls(tree):
            offenders.append(f"{path}:{lineno}: {spelling}")

    assert offenders == [], (
        "these unwind the SHARED per-test MonkeyPatch instance:\n  "
        + "\n  ".join(offenders)
        + "\n\n`monkeypatch` is one instance per test, shared with every fixture, "
        "and `undo()` takes no argument — it drops EVERYTHING, including this "
        "package's autouse `isolate_agent_runtime_root` pins. Whatever the test "
        "does after that line runs against the OPERATOR's live runtime root: "
        "that is the 2026-08-17 leak (EG-0.1) that left `ws_office_patch_test` at "
        "revision 67 in X:/Eternia/.hermes.\n\n"
        "Use a scoped context, which drops exactly one block's patches:\n"
        "    with pytest.MonkeyPatch.context() as patched:\n"
        "        patched.setattr(...)\n"
        "        ...            # the patched half\n"
        "    ...                # the unpatched half"
    )


# --------------------------------------------------------------------------- #
# Anti-vacuity
# --------------------------------------------------------------------------- #
def test_the_gate_scanned_a_real_tree():
    """A walker that found nothing passes the gate above forever."""

    scanned = _scanned()
    assert len(scanned) > 250, (
        f"only {len(scanned)} modules under {SCANNED_ROOT} were looked at — the "
        "walker is misrooted"
    )
    names = {Path(p).name for p in scanned}
    # The three files that HELD the defect. Each is also the file a regression
    # would land in, so the gate must be able to see all three.
    for expected in (
        "test_office_state_patches.py",
        "test_persona_chat_continuity.py",
        "test_mcp_admission_r2.py",
    ):
        assert expected in names, f"{expected} is not in the scanned set"


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        # --- the shapes the detector MUST catch, one per spelling ------------
        ("monkeypatch.undo()", True),
        # Aliased fixture handles — the reason this is not a name-matched net.
        ("mp.undo()", True),
        ("m.undo()", True),
        ("request.getfixturevalue('monkeypatch').undo()", True),
        ("self.monkeypatch.undo()", True),
        ("pytest.MonkeyPatch().undo()", True),
        # Indented / nested, i.e. the `finally:` shape site 3 used.
        ("def t(monkeypatch):\n    try:\n        pass\n    finally:\n        monkeypatch.undo()", True),
        # --- shapes it must NOT catch ---------------------------------------
        # The correct instrument. Carries no undo() call at all.
        ("with pytest.MonkeyPatch.context() as p:\n    p.setattr(x, 'y', 1)", False),
        # Scheduled for teardown, which is when the pins come down anyway.
        ("request.addfinalizer(mp.undo)", False),
        # A different verb, and a same-named FUNCTION rather than a method.
        ("buffer.redo()", False),
        ("undo(buffer)", False),
        # Prose and assertions about the string are why this is AST-based. Both
        # of these appear verbatim in this very file, which is what lets the
        # gate scan itself without an exemption.
        ("SRC = 'monkeypatch.undo()'\nassert SRC not in src", False),
        ('"""Never call monkeypatch.undo() in a body."""', False),
    ],
)
def test_the_detector_is_not_vacuous(source: str, flagged: bool):
    """Driven both directions on synthetic source.

    Without this, a detector that resolved nothing would make the gate above
    pass forever — the failure mode the contract-version authority file records
    for ``test_response_contract_fixture``.
    """

    hits = undo_calls(ast.parse(source))
    assert bool(hits) is flagged, f"detector returned {hits} for: {source!r}"


def test_the_gate_does_not_need_to_exempt_itself():
    """THE PROOF that the gate is structural and not textual.

    This file mentions ``monkeypatch.undo()`` a dozen times — in the docstring,
    in the failure message, and as parametrized synthetic source above. A
    substring gate would have to exempt its own file, and a gate with a
    self-exemption is one edit away from having a second one. So the gate scans
    this file like any other and finds nothing.
    """

    own_path = str(Path(__file__).resolve())
    scanned = _scanned()
    assert own_path in scanned, (
        f"the gate is not scanning its own file ({own_path}); the self-exemption "
        "claim below would be untested"
    )
    tree = scanned[own_path]
    assert tree is not None, "this file must reach the parser (it contains 'undo')"
    assert undo_calls(tree) == []


def test_the_prefilter_cannot_hide_a_finding():
    """The prefilter's soundness, driven rather than argued.

    Every case the detector flags must also survive the byte prefilter — a fast
    path that could skip a file containing a finding would be a hole that grows
    quietly as the suite grows.
    """

    flaggable = [
        "monkeypatch.undo()",
        "mp.undo()",
        "pytest.MonkeyPatch().undo()",
        "self.monkeypatch.undo()",
    ]
    for source in flaggable:
        assert undo_calls(ast.parse(source)), f"detector no longer flags {source!r}"
        assert PREFILTER_TOKEN in source.encode("utf-8"), (
            f"the prefilter would SKIP a file whose only content is {source!r}, "
            "which the detector flags. The token and the detector have drifted."
        )


def test_the_teardown_tripwire_is_still_installed():
    """The second witness must still exist, or this gate is alone.

    The two are a pair by design (module docstring): this file catches the
    spellings the walker can see, the fixture catches the ones it cannot. If the
    tripwire is deleted, that trade stops being true and the deletion should
    cost a red test rather than nothing.
    """

    from tests.agent_runtime import conftest as package_conftest

    assert hasattr(package_conftest, "_ISOLATION_PIN_WITNESS"), (
        "isolate_agent_runtime_root's teardown tripwire (EG-0.1) is gone. It is "
        "the behavioural half of this fence — it catches an unwind reached "
        "through an alias, a callback or getattr, which this file's AST walker "
        "cannot see. Restore it, or state why the structural gate is now enough."
    )
    source = Path(package_conftest.__file__).read_text(encoding="utf-8")
    assert "_ISOLATION_PIN_WITNESS.token is token" in source, (
        "the tripwire's assertion no longer compares the per-test witness token; "
        "a tripwire that stopped probing is worse than none, because the fence "
        "still looks staffed"
    )
