"""Ledger guard: every environment-gap skip row still describes something real.

The per-directory fences (``tests/agent``, ``tests/gateway``,
``tests/hermes_cli``, ``tests/tools``) register known-unrunnable tests. The
original design deliberately did NOT skip them, so the gap would stay visible.
That backfired: it made ``main`` permanently red, everyone learned to read reds
as scenery, and the red turned out to be excellent camouflage. Two consecutive
audits (2026-08-09 on ``tests/tools``, 2026-08-10 on the other three) found the
overwhelming majority of rows were stale TESTS rather than gaps, plus two real
production defects hiding underneath and two host rows that had silently gone
stale — the disk they described was no longer full, and nothing failed to say
so.

So rows now carry a live probe and become real skips (see
``tests/_env_gap_fence``). This file is the other half of that contract: it
fails when a probe stops reporting a gap, which is the enforcement the
print-only stale tracker never had. A row that has stopped being true is a
failing test here, not a silent non-fence.

Two things are checked per row:

  * the probe still reports the gap on this host — otherwise the environment
    (or the code) moved and the row must be deleted;
  * the node id still exists in the file — an orphaned row marks nothing while
    still reading like a fence, which is exactly how 163 rows rotted through
    the 2026-07-31 upstream prune.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from tests._env_gap_fence import EnvGapSkipRegistry, stale_skip_rows

TESTS_ROOT = Path(__file__).resolve().parent

# Per-directory conftests that may carry a probe-backed registry.
_FENCED_DIRS = ("agent", "gateway", "hermes_cli", "tools")


def _load_registry(directory: str) -> tuple[EnvGapSkipRegistry, Path] | None:
    """Import ``tests/<directory>/conftest.py`` and return its skip registry."""
    conftest = TESTS_ROOT / directory / "conftest.py"
    if not conftest.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        f"_env_gap_probe_{directory}", conftest
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    registry = getattr(module, "_ENV_GAP_SKIPS", None)
    if not registry:
        return None
    return registry, conftest


@pytest.mark.parametrize("directory", _FENCED_DIRS)
def test_every_skip_row_still_describes_a_real_gap(directory: str) -> None:
    """A probe that no longer reports a gap means the row is dead — delete it."""
    loaded = _load_registry(directory)
    if loaded is None:
        pytest.skip(f"tests/{directory} carries no probe-backed registry")
    registry, conftest = loaded

    stale = stale_skip_rows(registry)
    assert not stale, (
        f"These rows in _ENV_GAP_SKIPS ({conftest.relative_to(TESTS_ROOT.parent)}) "
        "have a probe that no longer reports a gap on this host. The environment "
        "or the code moved — delete the row and let the test run:\n  "
        + "\n  ".join(stale)
    )


@pytest.mark.parametrize("directory", _FENCED_DIRS)
def test_no_directory_still_uses_the_mark_only_registry(directory: str) -> None:
    """Both checks above are blind to ``_ENV_GAPS``, so it must stay empty.

    ``_load_registry`` returns None when a conftest carries no
    ``_ENV_GAP_SKIPS``, and both preceding tests then SKIP. That is exactly
    what happened to ``tests/tools`` while it still held 109 mark-only rows:
    this gate reported green while auditing that directory not at all, and two
    of its rows had already gone orphaned — ``test_extract_relevant_content_
    guarded`` and ``test_browser_vision_guarded``, both deleted when their file
    was rewritten — with nothing failing to say so.

    A mark-only row is unguarded by construction: the only thing watching it is
    a terminal-summary print, which cannot fail a run. So the gate has to
    assert the mark-only lane is EMPTY rather than merely preferring probes.
    """
    conftest = TESTS_ROOT / directory / "conftest.py"
    if not conftest.is_file():
        pytest.skip(f"tests/{directory} has no conftest")
    spec = importlib.util.spec_from_file_location(
        f"_env_gap_marks_{directory}", conftest
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    rows = {
        f"{file_name}::{node_id}"
        for file_name, groups in (getattr(module, "_ENV_GAPS", None) or {}).items()
        for _mark, _reason, node_ids in groups
        for node_id in node_ids
    }
    assert not rows, (
        f"tests/{directory}/conftest.py still registers rows in the mark-only "
        "_ENV_GAPS registry, which neither the staleness nor the orphan check "
        "above can see. Move them to _ENV_GAP_SKIPS with a live probe, or "
        "delete them:\n  " + "\n  ".join(sorted(rows))
    )


def _defined_node_ids(path: Path) -> set[str]:
    """Return ``test_x`` and ``TestC::test_x`` ids defined in ``path``.

    Parsed, not grepped: a substring search over the source would match the
    name in a comment or a docstring and call an orphan row healthy.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.add(node.name)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    found.add(f"{node.name}::{sub.name}")
    return found


@pytest.mark.parametrize("directory", _FENCED_DIRS)
def test_no_skip_row_points_at_a_deleted_test(directory: str) -> None:
    """An orphaned row fences nothing while still reading like a fence."""
    loaded = _load_registry(directory)
    if loaded is None:
        pytest.skip(f"tests/{directory} carries no probe-backed registry")
    registry, conftest = loaded

    orphans: list[str] = []
    for file_name, groups in registry.items():
        matches = sorted((TESTS_ROOT / directory).rglob(file_name))
        if not matches:
            orphans.append(f"{file_name} (file no longer exists)")
            continue
        defined: set[str] = set()
        for match in matches:
            defined |= _defined_node_ids(match)
        for _probe, _reason, node_ids in groups:
            for node_id in sorted(node_ids):
                # Strip pytest parametrisation before matching the definition.
                bare = node_id.split("[", 1)[0]
                if bare not in defined:
                    orphans.append(f"{file_name}::{node_id}")

    assert not orphans, (
        f"These rows in _ENV_GAP_SKIPS ({conftest.relative_to(TESTS_ROOT.parent)}) "
        "name tests that no longer exist:\n  " + "\n  ".join(orphans)
    )


# ── ownership: a directory's registry may only reach that directory ─────────


class _FakeItem:
    """The two attributes ``apply_skips``/``apply_marks`` read off an item, plus
    a marker sink. Not a pytest item: what is under test is which items the
    registry TOUCHES, and a real item would need a whole collection to exist."""

    def __init__(self, path: Path, name: str) -> None:
        self.path = path
        self.nodeid = f"{path.as_posix()}::{name}"
        self.markers: list[object] = []

    def add_marker(self, marker) -> None:
        self.markers.append(marker)


def test_a_directorys_registry_cannot_reach_another_directorys_file():
    """The cross-directory conftest interaction, closed at the mechanism.

    Every registry is keyed by file BASENAME, and every conftest that owns one
    registers ``pytest_collection_modifyitems`` — a GLOBAL pytest hook, handed
    every item in the session once that conftest is loaded, not just its own
    directory's. In a single-directory run the two facts never meet. In a
    combined run they compose: ``tests/gateway``'s row for
    ``test_update_command.py`` also matches ``tests/cli/test_update_command.py``,
    and a row that fires there SKIPS a test nobody registered — silently, because
    a skip is not a failure.

    Measured live at this HEAD: that one basename really is shared across the two
    directories, and it is the only pair today. "Only one today" is the state a
    landmine is in before it goes off, and the fix is at the mechanism rather
    than at the row: ownership is a required parameter, and an item outside the
    owning directory is not the registry's to touch.

    Both halves, because a scoping check that refuses everything would pass the
    first: the OWNED item must still be skipped.
    """

    from tests._env_gap_fence import apply_skips

    owner = TESTS_ROOT / "gateway"
    registry: EnvGapSkipRegistry = {
        "test_update_command.py": [(lambda: True, "the gap is present", {"test_a"})],
    }
    mine = _FakeItem(owner / "test_update_command.py", "test_a")
    theirs = _FakeItem(TESTS_ROOT / "cli" / "test_update_command.py", "test_a")

    apply_skips([mine, theirs], registry, owner_dir=owner)

    assert mine.markers, "the registry stopped skipping its OWN directory's row"
    assert not theirs.markers, (
        "tests/gateway's registry skipped a same-named test in tests/cli — a "
        "combined run is silently deselecting another directory's coverage"
    )


def test_the_stale_row_tracker_does_not_claim_another_directorys_pass():
    """The same interaction on the reporting half.

    ``pytest_runtest_logreport`` is global too, so in a combined run the tracker
    sees every other directory's reports. A pass one directory over, in a
    same-named file, would be printed as a stale row of OURS — and the operator's
    instruction for a stale row is to DELETE it, which would retire a fence that
    was never stale.
    """

    from tests._env_gap_fence import StaleEntryTracker

    class _Report:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid
            self.when = "call"
            self.outcome = "passed"

    registry = {
        "test_update_command.py": [("windows_env_gap", "reason", {"test_a"})],
    }
    tracker = StaleEntryTracker(registry, "tests/gateway/conftest.py")
    tracker.record(_Report("tests/cli/test_update_command.py::test_a"))
    assert not tracker._passed, (
        "tests/gateway's tracker claimed a tests/cli pass as its own stale row"
    )
    tracker.record(_Report("tests/gateway/test_update_command.py::test_a"))
    assert tracker._passed == ["tests/gateway/test_update_command.py::test_a"], (
        "the tracker stopped seeing its own directory's rows"
    )
