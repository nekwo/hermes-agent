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
