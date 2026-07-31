"""Shared environment-gap fence used by the per-directory test conftests.

`tests/hermes_cli/conftest.py` grew the first copy of this mechanism during the
2026-07-30 mission-lane triage. The 2026-07-31 `upstream/main` sync needed the
same fence in `tests/agent/`, `tests/gateway/` and `tests/tools/`, so the
mechanism lives here once instead of being pasted three more times.

What it is
----------
A registry that gives a NAME to a pre-existing host/platform failure. It does
**not** skip and does **not** xfail: every registered test still runs, still
executes its real assertions, and still fails loudly on a plain
``pytest tests/<dir>``. What the mark adds is the ability for a run that wants
only the fork-owned signal to deselect it:

    python -m pytest tests/agent -m "not windows_env_gap and not host_dependency_gap"

Two marks, by cause:

    windows_env_gap      The assertion encodes POSIX-only semantics that
                         Windows cannot satisfy — `signal.SIGKILL`,
                         `bash -c` / `setsid` spawn shapes, POSIX path
                         separators, cp1252 console/file encoding, CRLF
                         checkout normalization.

    host_dependency_gap  A host package or capability is absent rather than a
                         platform property — an uninstalled dependency, an
                         unreachable service, a missing toolchain version.

Keeping it honest
-----------------
Any registered node id that PASSES is printed in a
"stale environment-gap registry entries" section at the end of the run, so a
fixed environment — or a fixed test — forces the row to be deleted rather than
quietly masking a future regression.

Rules for adding a row
----------------------
1. Reproduce the failure individually first, and read the traceback to a
   concrete host/platform cause. "It fails on Windows" is not a cause.
2. Prove it is not a regression — run the same node on the pre-merge / pre-change
   ref before registering it.
3. Never register a failure whose cause is a defect in our own code. Fix the
   code. A row here that hides a real bug is worse than a red test.
4. A hang is NOT registrable: the mark deselects at collection time, but a plain
   run still executes the test and the hang kills the whole pytest process,
   taking every other result with it. Hangs need a real prerequisite probe or a
   real fix.
"""

from __future__ import annotations

import pytest

WINDOWS_ENV_GAP = "windows_env_gap"
HOST_DEPENDENCY_GAP = "host_dependency_gap"

# file basename -> [(mark, reason, {node ids within the file}), ...].
#
# A file can carry MORE THAN ONE group when its failures have more than one
# cause. Every group is applied independently, so each node id keeps the mark
# and the reason that actually explains it — a single-mark-per-file registry
# would have forced one of the causes to be recorded as a lie.
EnvGapRegistry = dict[str, list[tuple[str, str, set[str]]]]


def register_marks(config) -> None:
    """Register both env-gap marks on ``config`` (call from ``pytest_configure``)."""
    config.addinivalue_line(
        "markers",
        f"{WINDOWS_ENV_GAP}: pre-existing failure caused by POSIX-only test "
        "expectations that Windows cannot satisfy. Not a regression; deselect "
        f"with -m 'not {WINDOWS_ENV_GAP}'.",
    )
    config.addinivalue_line(
        "markers",
        f"{HOST_DEPENDENCY_GAP}: pre-existing failure caused by a missing host "
        "package, service or toolchain version. Not a regression; deselect with "
        f"-m 'not {HOST_DEPENDENCY_GAP}'.",
    )


def apply_marks(items, registry: EnvGapRegistry) -> None:
    """Attach the registered env-gap mark to every matching collected item."""
    for item in items:
        groups = registry.get(item.path.name)
        if groups is None:
            continue
        _, _, within_file = item.nodeid.partition("::")
        for mark, reason, node_ids in groups:
            if within_file in node_ids:
                item.add_marker(getattr(pytest.mark, mark)(reason=reason))


class StaleEntryTracker:
    """Collect registered node ids that actually PASSED, and report them.

    A registry row that no longer describes a real failure stops being a fence
    while still reading like one, so it has to be loud.
    """

    def __init__(self, registry: EnvGapRegistry, registry_location: str) -> None:
        self._registry = registry
        self._location = registry_location
        self._passed: list[str] = []

    def record(self, report) -> None:
        """Feed one report in (call from ``pytest_runtest_logreport``)."""
        if report.when != "call" or report.outcome != "passed":
            return
        file_name = report.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
        groups = self._registry.get(file_name)
        if groups is None:
            return
        _, _, within_file = report.nodeid.partition("::")
        if any(within_file in node_ids for _, _, node_ids in groups):
            self._passed.append(report.nodeid)

    def report(self, terminalreporter) -> None:
        """Emit the stale section (call from ``pytest_terminal_summary``)."""
        if not self._passed:
            return
        terminalreporter.write_sep("=", "stale environment-gap registry entries")
        terminalreporter.write_line(
            f"These node ids are registered in _ENV_GAPS ({self._location}) but "
            "PASSED. Delete their rows — a stale row hides a future regression."
        )
        for nodeid in sorted(set(self._passed)):
            terminalreporter.write_line(f"  {nodeid}")
