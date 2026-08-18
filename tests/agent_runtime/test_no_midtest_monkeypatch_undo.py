"""THE structural gate: no test under ``tests/`` calls ``.undo()``.

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

And that pin is not this package's alone — see THE WIDENING below. The ROOT
``tests/conftest.py`` installs the same shape for EVERY test in the tree.

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

**But they no longer ship over the same tree.** Since the ML-4 widening below,
witness 2 covers all of ``tests/`` while witness 1 — ``_ISOLATION_PIN_WITNESS``
in ``tests/agent_runtime/conftest.py`` — remains this package's alone (verified:
that sentinel appears in exactly two files, that conftest and this one). So
OUTSIDE ``tests/agent_runtime`` the fence is structural-only, and the spellings
witness 1 exists to catch — ``undo`` reached through an alias, a callback,
``getattr`` — are uncaught there. That is a stated gap, not an oversight:
closing it means hoisting a tripwire of the same shape into the ROOT
``tests/conftest.py`` beside the pins listed under THE WIDENING, which is its
own change with its own blast radius (every test in the tree gains a teardown
assertion) and is NOT ML-4's. Recorded here so the trade is visible to whoever
reads this file next, instead of being inferred from a scope mismatch.

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

=============================================================================
THE WIDENING: tests/agent_runtime -> tests (2026-08-17, ML-4)
=============================================================================

This gate shipped scoped to ``tests/agent_runtime`` with this stated reason:

    Other packages call ``undo()`` too (``tests/cron``, ``tests/hermes_cli``,
    ``tests/plugins``, ``tests/tools``) and are out of scope on purpose — they
    have no runtime-root pin to unwind, and widening the gate to them without
    reading each site would be a claim this file has not checked.

The second half was right and is what this stage discharged: all five sites
were read, then converted. **The first half was wrong**, and reading is how
that surfaced. Those packages DO have a runtime-root pin to unwind — it is
just not this package's. The ROOT ``tests/conftest.py`` installs, autouse,
through the same shared per-test instance, for every test in the tree:

* ``_hermetic_environment`` (``:489``) — redirects ``HERMES_HOME`` to a
  per-test tempdir AND ``delenv``s every credential-shaped variable;
* ``_kanban_write_guard`` (``:682``) — a FAIL-CLOSED guard refusing kanban
  writes that resolve under the real ``~/.hermes``;
* ``_live_system_guard`` (``:1098``) — blocks real ``os.kill`` / systemctl /
  gateway-pid scans;
* ``_audio_playback_guard`` (``:1466``), ``_neutralize_webbrowser`` (``:596``),
  ``_neutralize_macos_keychain_creds`` (``:626``).

So a mid-test ``undo()`` ANYWHERE under ``tests/`` re-exposes the operator's
real ``~/.hermes`` and real credentials for the remainder of that test — the
EG-0.1 damage class exactly, with a wider blast radius than the one this file
was built for. The gate is scoped to the pin, and the pin is tree-wide.

Per-directory reasons, one line each, per the no-allowlist policy above. Each
names the site that was converted to get there (the conversions are the
admission price; the gate keeps them converted):

* ``tests/cron`` — ``test_cron_profile_isolation.py:66`` undid, then reloaded
  ``cron.jobs`` to re-anchor it. Converted to a context inside the ``try``,
  with the restoring reload left in the ``finally`` so it still runs when an
  assertion fails.
* ``tests/hermes_cli`` — ``test_plugins.py:215`` undid to drop a sweep stub and
  then RE-``setenv``'d ``HERMES_HOME`` on the next line, because the undo had
  taken that pin down too. That re-setenv is the collateral damage, visible in
  the source. Converted to a context around the stub; the repair line is gone.
* ``tests/plugins`` — two sites. ``memory/test_holographic_store.py:199`` undid
  so a sibling write could reach the REAL ``_rebuild_bank``;
  ``platforms/photon/test_sidecar_paths.py:137`` undid in a ``finally`` before
  restoring reloads. Both converted to contexts.
* ``tests/tools`` — ``test_lazy_deps_venv_barrier.py:142`` undid DELIBERATELY,
  to probe the real ``_venv_pip_install`` past an autouse stub of it. Converted
  at the fixture instead of in the body: the stub now honours
  ``@pytest.mark.real_venv_pip`` (the house idiom, cf. ``real_concurrent_gate``
  / ``real_agent_prewarm``), so the probe is never inside the stub's scope
  rather than escaping it mid-test. Its own ``subprocess.run`` failer keeps
  "no real pip" true for that test.
* **every other directory under ``tests/``** — zero sites at widening time
  (the AST detector, run over the whole tree, reported exactly the five above).
  They are in scope pre-emptively: the pins listed above are the root
  conftest's, so the hazard does not depend on which directory a future
  ``undo()`` lands in, and a gate that has to be widened again per directory
  is one that will be late every time.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest


#: The tree this gate polices: the WHOLE test tree, since 2026-08-17 (ML-4).
#: The pin that makes the defect dangerous is the root ``tests/conftest.py``'s,
#: not this package's, so the scope follows the pin. The five sites that lived
#: outside the old ``tests/agent_runtime`` scope were read and converted first;
#: THE WIDENING in the module docstring records each one's reason, which is the
#: no-allowlist policy's price of admission.
SCANNED_ROOT = "tests"

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
# Measured on the tree the ML-4 widening landed against: 2,981 files read, 47
# of them past the prefilter and parsed, ~0.6 s total. That is ~12x the scoped
# scan's ~50 ms (321 files, 8 parsed) and still two orders off the 30 s
# per-test cap — the widening's whole cost, paid once per session, stated
# rather than assumed. Note what the prefilter is buying: 47 of 2,981 files
# parse, so the walk is dominated by reading bytes, not by ast.parse. The
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
def test_no_test_in_the_tree_unwinds_the_shared_monkeypatch():
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
        "and `undo()` takes no argument — it drops EVERYTHING. Under "
        "tests/agent_runtime that includes the autouse `isolate_agent_runtime_root` "
        "pins; ANYWHERE under tests/ it includes the root conftest's autouse "
        "`_hermetic_environment` (HERMES_HOME redirected to a tempdir, every "
        "credential env var blanked) and `_kanban_write_guard`. Whatever the test "
        "does after that line runs against the OPERATOR's live root with its real "
        "credentials: that is the 2026-08-17 leak (EG-0.1) that left "
        "`ws_office_patch_test` at revision 67 in X:/Eternia/.hermes.\n\n"
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
    assert len(scanned) > 2000, (
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

    # THE WIDENING's own anti-misrooting witness, and the reason the count
    # above cannot carry this alone: a walker that silently fell back to
    # ``tests/agent_runtime`` would still find all three names above — all
    # three live there — and a bare count floor only ever proves "many files",
    # never "the right files". These are the five converted sites, one per
    # newly admitted directory, matched on RELATIVE PATH so a same-named file
    # somewhere else cannot stand in for one of them.
    root = _repo_root()
    relative = {Path(p).relative_to(root).as_posix() for p in scanned}
    for expected_path in (
        "tests/cron/test_cron_profile_isolation.py",
        "tests/hermes_cli/test_plugins.py",
        "tests/plugins/memory/test_holographic_store.py",
        "tests/plugins/platforms/photon/test_sidecar_paths.py",
        "tests/tools/test_lazy_deps_venv_barrier.py",
    ):
        assert expected_path in relative, (
            f"{expected_path} is not in the scanned set — the ML-4 widening to "
            f"{SCANNED_ROOT!r} is not actually reaching that directory"
        )


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
