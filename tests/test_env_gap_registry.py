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

  * the probe still reports the gap on a host that HAS the gap — otherwise the
    environment (or the code) moved and the row must be deleted;
  * the node id still exists in the file — an orphaned row marks nothing while
    still reading like a fence, which is exactly how 163 rows rotted through
    the 2026-07-31 upstream prune.

The first of those said "on this host" until 2026-09-06, and it was a
single-host reading of a two-host ledger. Every row in all four registries
describes a gap measured on the Windows dev box; on the Linux CI runner every
probe correctly answers "no gap here", and the gate read all 52 of them as
rotted and instructed their deletion — which would have dropped the fence for
the host that has the gap. CI run 33969282189 slice 3 is where that finally
became visible, four parametrisations red. The verdict is now scoped to a host
the registry describes, and the host where it does NOT apply gets the
complementary assertion instead — see ``firing_skip_rows``.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

from tests._env_gap_fence import EnvGapSkipRegistry, firing_skip_rows, stale_skip_rows

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


def _this_host_carries_the_gaps() -> bool:
    """Does ANY row, in ANY of the four registries, report its gap here?

    The scope test for the stale verdict, and deliberately asked across all
    four registries rather than per-directory. Per-directory would leave a
    one-row registry — ``tests/agent`` is one today — unjudgeable the moment
    its single row went stale, because the registry would then look like one
    that simply does not describe this host. The four are populated from one
    developer host and describe one host class between them, so "is this that
    host" is a question about the fleet, not about a directory.
    """

    for directory in _FENCED_DIRS:
        loaded = _load_registry(directory)
        if loaded is not None and firing_skip_rows(loaded[0]):
            return True
    return False


@pytest.mark.parametrize("directory", _FENCED_DIRS)
def test_every_skip_row_still_describes_a_real_gap(directory: str) -> None:
    """On a host with the gaps: a probe gone quiet means the row is dead.

    On a host WITHOUT them: the registry fenced nothing here, so every one of
    its tests ran. Both branches assert; neither skips. Which branch applies is
    read off the registries rather than off a platform name.
    """
    loaded = _load_registry(directory)
    if loaded is None:
        pytest.skip(f"tests/{directory} carries no probe-backed registry")
    registry, conftest = loaded
    location = conftest.relative_to(TESTS_ROOT.parent)

    if not _this_host_carries_the_gaps():
        # None of these registries describes this host, so the stale verdict
        # has no standing: acting on it would delete a fence the host that DOES
        # have the gap still needs. What IS assertable here is that the ledger
        # is well formed — every probe answered a real ``bool``, on a host
        # whose imports and syscalls differ from the one it was written on.
        # Deliberately the weak half: the strong half is a claim about a host
        # this is not, and there is no honest way to make it from here.
        malformed = [
            f"{file_name}::{probe.__name__} -> {type(verdict).__name__}"
            for file_name, groups in registry.items()
            for probe, _reason, _node_ids in groups
            if not isinstance(verdict := probe(), bool)
        ]
        assert not malformed, (
            f"These probes in _ENV_GAP_SKIPS ({location}) did not answer a "
            "bool on this host. A probe is read for truthiness at collection, "
            "so a non-bool is a row that fences by accident:\n  "
            + "\n  ".join(sorted(malformed))
        )
        return

    stale = stale_skip_rows(registry)
    assert not stale, (
        f"These rows in _ENV_GAP_SKIPS ({location}) "
        "have a probe that no longer reports a gap on this host, which DOES "
        "carry the rest of this registry's gaps. The environment or the code "
        "moved — delete the row and let the test run:\n  "
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
