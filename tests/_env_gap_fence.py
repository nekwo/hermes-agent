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

PROBE-BACKED SKIPS — prefer these (2026-08-10)
----------------------------------------------
The no-skip design above was built so gaps would stay visible instead of being
quietly skipped. In practice it made ``main`` permanently red, which trained
every reader to treat reds as scenery — and a standing red is the best possible
camouflage. The 2026-08-09 audit of ``tests/tools`` found nine of ten rows were
stale TESTS rather than gaps, and that the red was hiding a real frozen-home
defect. The 2026-08-10 audit of the other three registries found the same shape
again: a 39-row block filed as "Windows home resolution" was a constructor-I/O
defect in ``plugins/platforms/feishu/adapter.py``, a two-row block was a regex
that had drifted out of sync with its own declared sibling, and two host rows
had silently gone stale (the disk they described was no longer full) with
nothing failing to say so.

So a genuine gap now registers in ``_ENV_GAP_SKIPS`` with a **live probe**
instead of ``_ENV_GAPS`` with a mark:

    ('test_foo.py', [(lambda: not hasattr(os, "chown"), "os.chown does not "
                      "exist on Windows", {'test_bar'})])

The probe is the honesty mechanism. It is evaluated at collection:

  * probe TRUE  -> the test is really skipped, with the mechanism as the reason,
    so a plain run is GREEN and the gap is visible as a named skip;
  * probe FALSE -> the row does not apply on this host, the test RUNS, and if
    it now passes ``tests/test_env_gap_registry.py`` fails the run and tells
    you to delete the row.

That inverts the failure mode. Under the mark-only design a row went stale
silently and a stale row fences nothing while still reading like a fence. Under
a probe, staleness is a failing test.

A probe must interrogate the MECHANISM, never the platform name.
``not hasattr(os, "chown")`` and ``importlib.util.find_spec("croniter") is
None`` are probes. ``sys.platform == "win32"`` is a probe only when the code
under test itself branches on ``sys.platform`` — i.e. when the platform IS the
mechanism, because the assertion pins a branch the host never selects.

Keeping it honest
-----------------
Any registered node id that PASSES is printed in a
"stale environment-gap registry entries" section at the end of the run, so a
fixed environment — or a fixed test — forces the row to be deleted rather than
quietly masking a future regression. Note this only catches rows that still
RUN; it is the weaker half of the contract, which is why probe-backed skips
above are preferred for anything new.

Rules for adding a row
----------------------
1. Reproduce the failure individually first, and read the traceback to a
   concrete host/platform cause. "It fails on Windows" is not a cause. Do not
   trust an existing row's stated reason either: the 2026-08-10 audit found one
   that blamed cmd.exe for a snippet the code runs under bash on every platform.
2. Prove it is not a regression — run the same node on the pre-merge / pre-change
   ref before registering it.
3. Never register a failure whose cause is a defect in our own code. Fix the
   code. A row here that hides a real bug is worse than a red test.
4. A hang is NOT registrable: the mark deselects at collection time, but a plain
   run still executes the test and the hang kills the whole pytest process,
   taking every other result with it. Hangs need a real prerequisite probe or a
   real fix.
5. A test that asserts a POSIX path SPELLING is not a gap. Windows reproduces
   the behaviour; only the separator, the drive letter or the default codec
   differs. Fix the assertion to pin the guarantee instead of the string.
6. ``monkeypatch.setenv("HOME", ...)`` is not a gap either. ``ntpath.expanduser``
   prefers ``USERPROFILE``, so the reflex silently expands ``~`` to the real
   profile and the test stops testing anything. Use
   ``tests._home_env.point_home_at``.
"""

from __future__ import annotations

from typing import Callable

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


# file basename -> [(probe, reason, {node ids within the file}), ...].
#
# ``probe`` is a zero-argument callable returning True when the gap is present
# on THIS host. See the "PROBE-BACKED SKIPS" section of the module docstring:
# the probe is what keeps the row honest, because a row whose probe has gone
# False lets its test run again and ``tests/test_env_gap_registry.py`` fails on
# it.
EnvGapSkipRegistry = dict[str, list[tuple["Callable[[], bool]", str, set[str]]]]


def apply_skips(items, registry: EnvGapSkipRegistry) -> None:
    """Skip every registered node whose probe reports the gap is present.

    A row whose probe is False is deliberately left alone: the test runs, and
    a stale row becomes a failing assertion in the registry ledger rather than
    a silent non-fence.
    """
    for item in items:
        groups = registry.get(item.path.name)
        if groups is None:
            continue
        _, _, within_file = item.nodeid.partition("::")
        for probe, reason, node_ids in groups:
            if within_file in node_ids and probe():
                item.add_marker(pytest.mark.skip(reason=reason))


def stale_skip_rows(registry: EnvGapSkipRegistry) -> list[str]:
    """Return ``file::node`` ids whose probe no longer reports a gap.

    Used by ``tests/test_env_gap_registry.py`` to fail the run on a row that
    has stopped describing anything — the enforcement the print-only stale
    tracker never had.
    """
    stale: list[str] = []
    for file_name, groups in registry.items():
        for probe, _reason, node_ids in groups:
            if probe():
                continue
            stale.extend(f"{file_name}::{node_id}" for node_id in sorted(node_ids))
    return stale


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
