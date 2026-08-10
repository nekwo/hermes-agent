"""Shared fixtures for tests/tools/ web-provider tests.

Per-file subprocess isolation means each test file gets a fresh interpreter,
so module-level state (like the web-search-provider registry) is empty when
a file starts.  The ``web_registry_populated`` fixture registers all bundled
providers before each test and resets the registry afterwards — tests that
depend on the registry being populated should use it explicitly or via
``@pytest.mark.usefixtures("web_registry_populated")``.
"""

from unittest.mock import patch

import pytest

from tests._env_gap_fence import (
    HOST_DEPENDENCY_GAP as _HOST,
    WINDOWS_ENV_GAP as _WINDOWS,
    EnvGapRegistry,
    StaleEntryTracker,
    apply_marks,
    register_marks,
)


def register_all_web_providers():
    """Register all bundled web-search providers into the global registry.

    This is the single source of truth for the provider list used by
    test classes that need the registry populated for dispatch checks.
    """
    from agent.web_search_registry import register_provider, _reset_for_tests
    from plugins.web.brave_free.provider import BraveFreeWebSearchProvider
    from plugins.web.ddgs.provider import DDGSWebSearchProvider
    from plugins.web.exa.provider import ExaWebSearchProvider
    from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider
    from plugins.web.parallel.provider import ParallelWebSearchProvider
    from plugins.web.searxng.provider import SearXNGWebSearchProvider
    from plugins.web.tavily.provider import TavilyWebSearchProvider
    from plugins.web.xai.provider import XAIWebSearchProvider

    _reset_for_tests()
    for cls in (
        BraveFreeWebSearchProvider,
        DDGSWebSearchProvider,
        ExaWebSearchProvider,
        FirecrawlWebSearchProvider,
        ParallelWebSearchProvider,
        SearXNGWebSearchProvider,
        TavilyWebSearchProvider,
        XAIWebSearchProvider,
    ):
        register_provider(cls())


@pytest.fixture
def web_registry_populated():
    """Populate the web-search-provider registry for one test, then reset."""
    register_all_web_providers()
    yield
    from agent.web_search_registry import _reset_for_tests
    _reset_for_tests()


@pytest.fixture
def disable_lazy_stt_install():
    """Disarm the runtime lazy-install probe so static ``_HAS_FASTER_WHISPER``
    patches accurately simulate 'faster-whisper not installed'.

    Without this, ``_try_lazy_install_stt()`` calls
    ``importlib.util.find_spec("faster_whisper")``, which returns truthy
    whenever the package is installed in the dev / CI environment —
    defeating the test's ``_HAS_FASTER_WHISPER=False`` patch.

    Opt in at module scope with
    ``pytestmark = pytest.mark.usefixtures("disable_lazy_stt_install")``.
    """
    with patch("tools.transcription_tools._try_lazy_install_stt", return_value=False):
        yield


# ── Environment-gap fence — migrated to probe-backed skips (2026-08-10) ─────
#
# This registry held 109 mark-only rows across 47 files, every one of them a
# STANDING RED on a plain `pytest tests/tools`. That was the point of the
# original design: a gap should stay visible rather than be quietly skipped.
# It backfired exactly as it did in the three sibling registries. A permanently
# red directory trains every reader to treat reds as scenery, and scenery is
# the best camouflage a defect can have.
#
# All 109 were re-run individually and audited. NINETY-FOUR were not gaps:
#
#   * NINE were REAL DEFECTS in our own code, which rule 3 of the fence
#     contract forbids registering at all. Every one of them was a Windows-only
#     silent failure — none raised, so none was ever noticed:
#       - tools/file_tools.py `_check_sensitive_path` compared only the
#         os.path.normpath form, which rewrites "/etc/hosts" to "\etc\hosts",
#         so every sensitive-path prefix stopped matching and the guard
#         returned None — ALLOW — for exactly the paths it exists to refuse.
#         The container backends (docker, modal, daytona, singularity,
#         vercel_sandbox) hand this function real Linux paths from this host.
#         The identical bug had already been found and fixed 300 lines above
#         for `_BLOCKED_DEVICE_PATHS` and left open-coded, so the second
#         instance survived the first fix; both now share `_posix_match_forms`.
#       - tools/environments/file_sync.py held a NamedTemporaryFile handle open
#         and passed its path to a downloader that opens it again (refused on
#         Windows), and built `remote_path` with native separators so nothing
#         ever matched `_pushed_hashes`. sync_back therefore applied NOTHING,
#         three times per attempt, swallowed by the retry loop's warning.
#       - tools/process_registry.py sent "\n" to a Windows ConPTY (which
#         commits lines on CR) and called pywinpty's sendeof(), which writes
#         the POSIX Ctrl-D rather than Windows' Ctrl-Z — then returned
#         {"status": "ok", "message": "EOF sent"} both times. Interactive PTY
#         stdin was wholly non-functional while reporting success.
#       - tools/credential_files.py and tools/environments/daytona.py built
#         SANDBOX-side (always-Linux) paths with the HOST separator, so nested
#         skill files uploaded under one flat backslash-name and daytona's
#         mkdir created a literal "\root\.hermes\skills" directory.
#       - tools/skills_guard.py sorted `Path` objects (platform separator, and
#         case-insensitively on Windows) where tools/skills_hub.py sorted
#         relative POSIX strings, and skills_hub wrote bundles with
#         Path.write_text and no newline="" so every "\n" landed as "\r\n".
#         Together those made content_hash and bundle_content_hash disagree,
#         reporting every freshly installed skill as drifted and binding the
#         scan attestation to a digest the installed bytes never had.
#       - tools/checkpoint_manager.py used plain shutil.rmtree over a git object
#         store. Git writes loose objects read-only, and Windows refuses to
#         unlink a read-only file, so every checkpoint deletion failed and each
#         call site only logged a warning: "clear" silently freed nothing.
#       - scripts/check_subprocess_stdin.py derived its KNOWN_SAFE key with the
#         native separator, so the allowlist matched nothing on Windows and the
#         guard reported known-safe files as violations.
#
#   * the rest were STALE TESTS — POSIX spellings, fixtures that were never
#     ported, and under-mocked branches. Note how many of their recorded
#     REASONS were wrong, not just their verdicts: rows blaming "_SEP" turned
#     out to be an _IS_WINDOWS branch, a cp1252 decode, or a read-only unlink.
#     A blanket reason ("the code builds it with os.sep, so the separators do
#     not match") reads identically whether the CODE or the TEST is at fault —
#     and for any path crossing into a Linux sandbox, os.sep IS the bug. That
#     ambiguity is what let nine defects sit here.
#
#   * VACUOUS GREENS were found in passing, and they are NOT bounded by this
#     registry: seven were never fenced at all. TestBrowserSourceLinesAreGuarded
#     asserted a source line ("return response.choices[0].message.content")
#     that the module has never contained, because that site assigns rather
#     than returns — deleting the production guard left it GREEN. Five in
#     test_tirith_security.py passed only because the unsupported-platform
#     short-circuit happened to return the expected answer.
#
# What is left below is genuine and probe-backed. Each probe MEASURES its
# mechanism — a chmod round trip, a real spawn — rather than testing the
# platform name, so the row becomes a real skip here and runs normally
# anywhere the mechanism is absent. tests/test_env_gap_registry.py fails the
# run if a probe stops reporting a gap, if a row names a deleted test, or if
# anything is added back to the mark-only lane.
#
# ── NOT registered, deliberately, and therefore still RED ───────────────────
#
# Rule 3: a failure caused by a defect in our own code gets FIXED, not fenced.
# This one needs a design decision that is not an auditor's to make, so it is
# left red and announced (see _KNOWN_DEFECTS below) rather than re-filed as an
# environment gap the next time someone sees it.
#
#   * test_search_error_guard.py — searching for a literal backslash is
#     silently corrupted on Windows. ShellFileOperations routes the search
#     PATTERN (arbitrary user text) through an MSYS shell to a NATIVE ripgrep,
#     and Git Bash rebuilds the child command line with MSVCRT escaping,
#     collapsing a doubled backslash to one even inside single quotes. This is
#     the THIRD distinct incident in that same seam — `_bash_safe_path` vs
#     MSYS_NO_PATHCONV, then `_shell_arg_safe_path`, now this — so per the
#     recurrence rule the SHELL HOP is the bug, not the escaping. The fix is to
#     spawn rg with a direct argv list and do the `| head` truncation in Python
#     instead of relying on `set -o pipefail`. That is a real change to the
#     search execution path and is filed rather than smuggled into an audit.

import importlib
import os
import socket
import stat
import subprocess
import tempfile

from tests._env_gap_fence import (
    EnvGapRegistry,
    EnvGapSkipRegistry,
    StaleEntryTracker,
    apply_marks,
    apply_skips,
    register_marks,
)


def _cached(fn):
    """Memoize a probe: apply_skips calls it once per collected item."""
    cache = []

    def _probe() -> bool:
        if not cache:
            cache.append(fn())
        return cache[0]

    _probe.__doc__ = fn.__doc__
    return _probe


@_cached
def _no_posix_file_modes() -> bool:
    """True where chmod cannot express a POSIX file mode.

    Measured, not assumed: NTFS records only FILE_ATTRIBUTE_READONLY, so
    chmod(0o600) reads back as 0o666. Performs the real round trip.
    """
    with tempfile.TemporaryDirectory() as tmp:
        probe = os.path.join(tmp, "mode_probe")
        with open(probe, "w", encoding="utf-8"):
            pass
        os.chmod(probe, 0o600)
        return stat.S_IMODE(os.stat(probe).st_mode) != 0o600


@_cached
def _no_posix_exec_bit() -> bool:
    """True where a file cannot carry an executable bit.

    Windows decides executability by extension, not by mode, so a git tree
    built from the filesystem reports every blob as "file" and never "exec".
    """
    with tempfile.TemporaryDirectory() as tmp:
        probe = os.path.join(tmp, "exec_probe")
        with open(probe, "w", encoding="utf-8"):
            pass
        os.chmod(probe, 0o755)
        return not os.stat(probe).st_mode & stat.S_IXUSR


@_cached
def _no_unwritable_dir_via_chmod() -> bool:
    """True where chmod cannot make a directory unwritable.

    Windows ignores the read-only attribute on directories for the purpose of
    creating children, so a test that chmods a target 0o555 and expects the
    write to be REFUSED gets a successful write and no error to assert on.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "ro_dir")
        os.mkdir(target)
        os.chmod(target, 0o555)
        try:
            with open(os.path.join(target, "probe"), "w", encoding="utf-8"):
                pass
        except OSError:
            return False
        finally:
            os.chmod(target, 0o755)
        return True


@_cached
def _no_shebang_exec() -> bool:
    """True where the OS cannot execute a ``#!`` script as a program image.

    CreateProcess requires a PE image and rejects a shebang script with
    WinError 193 ("not a valid Win32 application"); execve honours the
    interpreter line. Measured by actually spawning one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        script = os.path.join(tmp, "shebang_probe")
        with open(script, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(script, 0o755)
        try:
            subprocess.run([script], capture_output=True, timeout=30)
        except OSError:
            return True
        return False


def _no_af_unix() -> bool:
    """True where socket.AF_UNIX is absent, so no unix socket can be bound."""
    return not hasattr(socket, "AF_UNIX")


def _no_process_groups() -> bool:
    """True where os.getpgid is absent — no process groups, no killpg."""
    return not hasattr(os, "getpgid")


# The mark-only lane is retired. tests/test_env_gap_registry.py asserts this
# stays empty: neither its staleness check nor its orphan check can see a
# mark-only row, so anything left here would be unguarded by construction.
_ENV_GAPS: EnvGapRegistry = {}


_ENV_GAP_SKIPS: EnvGapSkipRegistry = {
    # ── NTFS records no POSIX mode bits ────────────────────────────────────
    'test_base_environment.py': [
        (
            _no_posix_file_modes,
            'asserts the snapshot and cwd files are mode 0o600; this filesystem '
            'records only a read-only bit, so chmod(0o600) reads back as 0o666. '
            'The ATOMICITY guarantee of the same writer is pinned separately by '
            'TestAtomicSnapshotConcurrencyBehavioral, which runs here',
            {
                'TestSnapshotFileModes::test_snapshot_and_cwd_files_are_0600',
            },
        ),
    ],
    'test_file_operations.py': [
        (
            _no_posix_file_modes,
            'asserts the umask-derived mode of a newly created file; umask and '
            'chmod "=rw" are inert on this filesystem, so the mode under test '
            'can never appear',
            {
                'TestAtomicWriteNewFilePermissions::test_new_file_gets_umask_default_permissions[2]',
                'TestAtomicWriteNewFilePermissions::test_new_file_gets_umask_default_permissions[18]',
                'TestAtomicWriteNewFilePermissions::test_new_file_gets_umask_default_permissions[63]',
                'TestAtomicWriteNewFilePermissions::test_overwrite_still_preserves_existing_mode',
            },
        ),
    ],
    'test_file_write_safety.py': [
        (
            _no_posix_file_modes,
            'asserts atomic_write preserves a POSIX mode across the replace; '
            'the mode cannot be set in the first place on this filesystem. NB '
            'the SENSITIVE-PATH rows that used to sit beside this one were not '
            'gaps at all — they were the _check_sensitive_path defect above, '
            'and they now pass',
            {
                'TestAtomicWrite::test_patch_routes_through_atomic_write',
            },
        ),
    ],
    'test_skills_sync_client.py': [
        (
            _no_posix_exec_bit,
            'expects a git blob mode of "exec"; this filesystem carries no '
            'executable bit (Windows decides executability by extension), so '
            'the tree builder correctly reports "file"',
            {
                'TestObjectBuilding::test_build_tree_blob_and_exec',
            },
        ),
    ],
    # ── chmod cannot revoke write on a directory ───────────────────────────
    'test_lazy_deps_durable_target.py': [
        (
            _no_unwritable_dir_via_chmod,
            'chmods the ABI-stamp target 0o555 and asserts the write reports an '
            'error; this platform ignores the read-only attribute on '
            'directories when creating children, so the write SUCCEEDS and '
            'there is no error to assert on',
            {
                'TestAbiStamp::test_readonly_target_reports_error',
            },
        ),
    ],
    # ── the OS cannot run a shebang script as a program image ──────────────
    'test_execution_flag_detection.py': [
        (
            _no_shebang_exec,
            'the payload these cases try to get executed is a `#!`-shebang '
            'script, which CreateProcess rejects (WinError 193) because it is '
            'not a PE image — so the marker file the assertion reads is never '
            'written, whatever the option-ownership logic decided. The sibling '
            'parametrisations that do NOT depend on executing a shebang were '
            'registered here too and now pass',
            {
                'test_real_binaries_execute_leading_dash_program_payload[rg-args0-None-False]',
                'test_real_binaries_execute_leading_dash_program_payload[rg-args1-None-False]',
                'test_real_binaries_execute_leading_dash_program_payload[sort-args2-{bulk}-False]',
            },
        ),
    ],
    # ── POSIX-only syscalls: the mechanism is a missing attribute ──────────
    'test_voice_mode.py': [
        (
            _no_af_unix,
            'socket.AF_UNIX does not exist on this platform, so the PulseAudio '
            'unix-socket reachability path cannot be exercised at all. NB the '
            'three WSL/PowerShell rows that used to sit beside these were NOT '
            'gaps — the test simply under-mocked and found the real '
            'powershell.exe; they now pass',
            {
                'TestPulseSocketReachable::test_stale_socket_file_not_reachable',
                'TestPulseSocketReachable::test_listening_socket_reachable_via_xdg_runtime',
            },
        ),
    ],
    'test_local_interrupt_cleanup.py': [
        (
            _no_process_groups,
            'os.getpgid does not exist on this platform; the KeyboardInterrupt '
            'path under test kills the child by PROCESS GROUP, which has no '
            'analogue here. Its sibling row (the cached-pgid case) was a stale '
            'test, not a gap, and now passes',
            {
                'test_wait_for_process_kills_subprocess_on_keyboardinterrupt',
            },
        ),
    ],
}


_STALE = StaleEntryTracker(_ENV_GAPS, "tests/tools/conftest.py")


# ── Known defects that are deliberately NOT fenced ─────────────────────────
#
# Left RED on purpose. Without this banner the next person to run the suite
# sees a red on Windows and files it back into the registry as an environment
# gap — which is how nine defects stayed invisible here in the first place.
_KNOWN_DEFECTS: dict[str, str] = {
    "test_search_error_guard.py": (
        "KNOWN DEFECT — NOT an environment gap. Content search for a literal\n"
        "  backslash is silently CORRUPTED on Windows. ShellFileOperations\n"
        "  routes the search PATTERN — arbitrary user text — through an MSYS\n"
        "  shell to a NATIVE ripgrep, and Git Bash rebuilds the child command\n"
        "  line with MSVCRT escaping, collapsing a doubled backslash to one\n"
        "  even inside single quotes (measured: 16 chars in, 15 out).\n"
        "  This is the THIRD distinct incident in this same seam — _bash_safe_\n"
        "  path vs MSYS_NO_PATHCONV, then _shell_arg_safe_path, now this — so\n"
        "  the SHELL HOP is the bug, not the escaping. Fix: spawn rg with a\n"
        "  direct argv list and do the `| head` truncation in Python rather\n"
        "  than relying on `set -o pipefail`. That is a real change to the\n"
        "  search execution path, so it is filed, not smuggled into an audit.\n"
        "  Do NOT re-file this as windows_env_gap or host_dependency_gap."
    ),
}

_KNOWN_DEFECT_FAILURES: list[str] = []


def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register the environment-gap marks."""
    register_marks(config)


def pytest_collection_modifyitems(items):  # noqa: D401 — pytest hook
    """Skip every registered node whose probe reports its gap on this host."""
    apply_marks(items, _ENV_GAPS)
    apply_skips(items, _ENV_GAP_SKIPS)


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record stale mark-only passes, and failures of the known-defect tests."""
    if report.when == "call":
        file_name = report.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
        if report.outcome == "failed" and file_name in _KNOWN_DEFECTS:
            _KNOWN_DEFECT_FAILURES.append(report.nodeid)
            return
    _STALE.record(report)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Explain the deliberate reds, then surface any stale mark-only rows."""
    if _KNOWN_DEFECT_FAILURES:
        terminalreporter.write_sep("=", "KNOWN DEFECTS — deliberately not fenced")
        seen: set[str] = set()
        for nodeid in sorted(set(_KNOWN_DEFECT_FAILURES)):
            file_name = nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
            terminalreporter.write_line(f"  {nodeid}")
            if file_name not in seen:
                seen.add(file_name)
                terminalreporter.write_line(f"  {_KNOWN_DEFECTS[file_name]}")
                terminalreporter.write_line("")
    _STALE.report(terminalreporter)
