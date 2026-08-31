"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys

import pytest

from tests._env_gap_fence import EnvGapSkipRegistry, apply_skips
from tests.hermes_cli import _gateway_fence

# L2/L3 of the gateway fence: process-wide, installed at conftest IMPORT — the
# earliest moment this directory owns — rather than inside a fixture, because
# the measured escape ran from an ``atexit`` handler, a window no fixture can
# cover. See tests/hermes_cli/_gateway_fence.py for the full reproduction.
_gateway_fence.install()


@pytest.fixture(autouse=True)
def _sys_modules_identity_is_restored():
    """A test may IMPORT modules; it may not REPLACE or DROP one.

    The largest cross-test pollution class in this directory, measured
    2026-08-31. ``test_skills_subparser.py`` deletes ``hermes_cli.main`` from
    ``sys.modules`` and re-imports it to prove the parser still builds -- and
    never puts the original back. Python then holds TWO ``hermes_cli.main``
    module objects with two separate namespaces:

      * every test file that did ``from hermes_cli.main import _build_web_ui``
        at COLLECTION time holds a function whose ``__globals__`` is the FIRST
        namespace, now orphaned;
      * ``sys.modules["hermes_cli.main"]`` is the SECOND, which is what
        ``patch("hermes_cli.main._run_with_idle_timeout")`` and
        ``monkeypatch.setattr(cli_main, ...)`` reach.

    So the patch lands in a namespace the function under test never reads, and
    the test runs production for real. Measured: ``test_web_ui_build`` shelled
    out to a genuine ``npm run build`` (the ``npm error code EJSONPARSE`` in
    the baseline output is that build, not a mock), and the ``_cmd_update_impl``
    helper stubs in ``test_update_venv_health`` silently did nothing. It is
    also why these reds are green in isolation yet perfectly deterministic in a
    full run: they depend on running after ONE file, not on timing.
    Alphabetical order does the rest -- one file, 16 reds.

    Restoring identity fixes the class rather than the caller, and keeps
    working when the next test reaches for the same trick. Note the asymmetry:
    newly imported modules are LEFT alone (lazy imports are normal, and
    un-importing them would be its own pollution). Only a module the session
    already had, and which the test replaced or removed, is put back.

    ``importlib.reload`` is deliberately NOT covered: it mutates the existing
    module object in place, so identity -- and therefore every binding --
    survives. Reload is a different question, and this guard would answer it
    dishonestly by appearing to.
    """
    before = sys.modules.copy()
    try:
        yield
    finally:
        for name, module in before.items():
            if sys.modules.get(name) is not module:
                sys.modules[name] = module


@pytest.fixture(autouse=True)
def _no_windows_gateway_pause_token(request, monkeypatch):
    """L1 of the gateway fence: no test drives the REAL Windows gateway pause.

    ``_cmd_update_impl`` opens with
    ``_pause_windows_gateways_for_update()``. On a real Windows host that
    function walks this machine's live gateway process table and, finding
    nothing running, asks ``gateway_windows.is_installed()`` — a ``schtasks
    /Query`` against the operator's registered ``Hermes_Gateway_alice`` task.
    When that answers yes it returns a ``cold_start_if_installed`` token, and
    ``_cmd_update_impl`` parks the resume on ``atexit``. The handler then fires
    at INTERPRETER EXIT, after every monkeypatch has been undone, and starts a
    real gateway against the operator's real profile. Measured 2026-08-31;
    ``test_update_autostash.py`` alone reproduces it.

    Five files here call ``cmd_update`` / ``_cmd_update_impl`` and only
    ``test_update_venv_health.py`` patches this seam, so the default belongs in
    the directory's conftest — the same shape, and the same reasoning, as
    ``_suppress_concurrent_hermes_gate`` above: a Windows-only production guard
    that reads the developer's live machine has no defined answer in a test.

    ``None`` is production's own "nothing to pause" answer, so the code under
    test takes its normal path; nothing is registered and nothing is resumed.
    Tests that are ABOUT the pause/resume path opt out with
    ``@pytest.mark.real_windows_gateway_pause`` and bring their own mocks —
    L2/L3 still stand behind them.
    """
    if request.node.get_closest_marker(_gateway_fence.REAL_PAUSE_MARK):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(
        _cli_main,
        "_pause_windows_gateways_for_update",
        lambda *_a, **_k: None,
        raising=False,
    )


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )


class _EmptyProcessTable:
    """A process-table lister that reports a machine running nothing.

    The hermetic default for this directory. It answers the same shape the
    production lister answers, so the code under test takes its normal path
    and simply finds no candidates — as opposed to ``None``, which is the
    typed "no inspector available at all" arm and would exercise a different
    branch.
    """

    def __init__(self) -> None:
        self.reads = 0

    def read(self):
        from hermes_cli import profiles

        self.reads += 1
        return profiles._ProcessTable(
            self_pid=os.getpid(),
            ancestor_pids=frozenset(),
            current_username=None,
            processes=(),
        )


@pytest.fixture(autouse=True)
def _no_live_process_table(monkeypatch):
    """No test in this directory reads this machine's real process table.

    ``hermes profile delete`` scans for backends bound to the profile being
    deleted, and the production lister walks every process on the box (and,
    for candidates, reads their environment). Measured on this workstation
    2026-08-18: 448 processes, ~4.2s per scan, three scans in
    ``test_profiles.py`` alone — which is what made ``tests/hermes_cli`` time
    out as a directory (ledger row F1). The live table is also not a fact any
    test can drive: what it holds depends on what the developer happens to be
    running, so a test that reads it is asking a question with no defined
    answer.

    The desktop build-lock sweep (``hermes_cli.main._DESKTOP_PROCESS_LISTER``,
    reached from ``cmd_gui``) is the SECOND consumer of the same seam and is
    defaulted here too rather than in a fixture of its own — one place that
    answers "does any test in this directory touch the live process table",
    because two places is how one of them silently stops covering a call site
    (measured: ``test_gui_command.py`` walked the real table once per run,
    ledger row B20(vi)).

    Tests that are ABOUT a scan install their own lister on top of this one
    (``monkeypatch.setattr(profiles, "_PROCESS_LISTER", ...)``) and drive the
    rows they mean to filter.

    ``raising`` is left at its default of True deliberately: if either seam is
    ever renamed, this fixture must fail loudly rather than silently stop
    guarding — a guard that can quietly become a no-op is how the hole
    reopens.
    """
    try:
        from hermes_cli import profiles
    except Exception:
        return
    monkeypatch.setattr(profiles, "_PROCESS_LISTER", _EmptyProcessTable())
    try:
        from hermes_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(_cli_main, "_DESKTOP_PROCESS_LISTER", _EmptyProcessTable())


# ── Pre-existing environment-gap fence (2026-07-30) ─────────────────────────
#
# `python -m pytest tests/hermes_cli` on this Windows 10 workstation reports
# 234 failures across 57 files. Every one of them was triaged against the
# mission-lane removal (commits f47e6d278..25e2651ac) and **none is caused by
# it** — the removal touched `hermes_cli/harness*.py`,
# `hermes_cli/harness_parts/`, `hermes_cli/profiles.py`,
# `hermes_cli/web_server.py` and `tools/`, and the tests covering those are
# green (`test_harness_cli.py`, `test_web_server_blueprints.py`, the
# `test_mission_chat_*` set, `test_flow_commands.py`, `test_persona_chat_session.py`).
#
# SUPERSEDED 2026-08-10 — read the audited block further down before this one.
# The claim above ("234 failures ... pre-existing gaps between this host and
# the Linux CI") did not survive being checked: of the 82 rows that reached
# this audit, fifty were stale tests or defects in our own code. The no-skip
# design that left them failing is retired; genuine rows now carry a live probe
# and become real skips (`_ENV_GAP_SKIPS`), and `tests/test_env_gap_registry.py`
# fails when a probe stops describing anything. The paragraphs below are kept
# because their toolchain/hang notes are still accurate and still load-bearing.
#
# Two marks, by cause:
#
#   windows_env_gap      The assertion encodes POSIX-only semantics that
#                        Windows cannot satisfy — 0o600 file modes (NTFS
#                        reports 0o666), `os.chown` / `os.getpgid` /
#                        `signal.SIGKILL` / `signal.pause` / `_curses`,
#                        `/usr/bin/...` literals, POSIX path separators,
#                        `bash -n <C:\...>`, cp1252 console decoding, and
#                        the `git -c windows.appendAtomically=false` prefix
#                        that `cmd_update` adds only on win32.
#
#   host_dependency_gap  A host package or capability is absent rather than a
#                        platform property: `croniter`, `pywinpty`, `pathspec`
#                        are not installed (all three are declared project
#                        dependencies, so this is an install gap on THIS box,
#                        not an optional extra); the installed `rich` does not
#                        emit OSC-8 panel-title hyperlinks; upstream has since
#                        added a standalone `tool_describe` bridge tool to the
#                        minimal toolset; and the host's git 2.31.1 does not
#                        honour `--ignore-cr-at-eol` for `--name-only` output.
#
#                        `fire` and `psutil` USED to be in that list. They were
#                        installed on the ambient interpreter on 2026-08-01
#                        (ledger item 7, RULED EXECUTE) and every row naming
#                        them retired — three groups here, and more in the
#                        gateway/tools registries.
#
# ── 2026-07-31 upstream-sync sweep ─────────────────────────────────────────
#
# The `upstream/main` merge (b9721809e) replayed this registry against a suite
# that upstream had pruned hard (prune waves 1-2: 46,820 -> 19,757 test
# functions). Two things were done in one pass:
#
#   * every registry row whose node id no longer exists was DELETED (163 rows
#     across 40 files, plus the whole `test_uv_tool_update.py` file, which
#     upstream removed). An orphaned row marks nothing, so it silently stops
#     being a fence while still reading like one;
#   * four rows the stale detector reported as PASSING were deleted too
#     (test_dashboard_unified_launch, test_gateway_wsl, test_install_cua_driver,
#     test_kanban_db).
#
# The rows ADDED in that same pass are all merge-delta failures that were
# triaged individually to a real host/platform cause — never to hide a
# regression. The one genuine merge-resolution defect found in this area
# (`hermes_cli/dep_ensure.py` lost `import os` when the merge took upstream's
# import block over the fork's `ensure_git_bash` body) was FIXED in the code,
# not fenced.
#
# Two failure modes cannot be handled by a mark, because they HANG instead of
# failing and a hang kills the whole pytest process before any deselection can
# matter. Those get real prerequisite probes further down (`node --version`
# against the Vite 8 engine floor, and a TCP probe of 127.0.0.1:11434). Both
# are inert on a host that satisfies the prerequisite, so nothing is masked.
#
# Toolchain floor worth stating explicitly, because it is the one gap with a
# concrete version number: `hermes_cli.main._build_web_ui` runs
# `npm run build -w web`, and `web/package.json` pins `vite ^8`, which requires
# Node `^20.19.0 || >=22.12.0`. This box has Node v20.17.0, so that build
# cannot succeed here. `test_web_ui_build.py` fails on it, and
# `test_cmd_update.py` *executes* it for real — it stubs `subprocess.run` but
# not the `subprocess.Popen` that `_run_with_idle_timeout` uses, so a plain run
# of that file performs a real npm install/build (and a real bundled-skill
# sync) against the checkout. Treat that file as side-effecting until the
# upstream mocking is tightened.
#
# Keeping the registry honest: any registered node id that PASSES is printed in
# a "stale environment-gap registry entries" section at the end of the run (see
# pytest_terminal_summary below), so a fixed environment — or a fixed test —
# forces the row to be deleted rather than quietly masking a regression.

_WINDOWS = "windows_env_gap"
_HOST = "host_dependency_gap"


# ── Prerequisite guard: files that execute the REAL web-UI build ───────────
#
# The two files below call `cmd_update` / `_cmd_update_impl` with only
# `subprocess.run` stubbed. Production's `_build_web_ui` reaches
# `_run_with_idle_timeout`, which uses `subprocess.Popen` — unstubbed — so the
# test performs a genuine `npm install` + `npm run build -w web` against the
# checkout. `web/package.json` pins `vite ^8`, whose engine floor is
# Node `^20.19.0 || >=22.12.0`. Below that floor the build cannot complete, the
# call blocks past the 30s per-test cap, and pytest-timeout's thread method
# kills the WHOLE process — taking every other result in the run with it. A
# marker cannot help: the hang is in a test the registry does not (and should
# not) list as a failure.
#
# This is an environment PREREQUISITE check, not a loosened assertion: on a
# host that meets the floor the guard is inert and both files run in full,
# executing every original assertion.
_WEB_BUILD_PREREQ_FILES = frozenset({
    "test_cmd_update.py",
    "test_update_yes_flag.py",
})

_VITE8_NODE_FLOOR = "^20.19.0 || >=22.12.0"


def _node_version() -> str | None:
    """Return the host `node --version` string, or None when unavailable."""
    import subprocess as _sp

    try:
        out = _sp.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip() or None


def _web_build_prereq_failure() -> str | None:
    """Return a skip reason when the host cannot build the web UI, else None."""
    raw = _node_version()
    if raw is None:
        return (
            "web-UI build prerequisite: node is not runnable on this host, so "
            f"`npm run build -w web` (vite ^8, requires Node {_VITE8_NODE_FLOOR}) "
            "cannot complete; this file executes that build for real."
        )
    try:
        major, minor, patch = (int(part) for part in raw.lstrip("v").split(".")[:3])
    except ValueError:
        return None  # unparseable: assume capable rather than guess
    ok = (
        (major == 20 and (minor, patch) >= (19, 0))
        or (major == 21)
        or (major == 22 and (minor, patch) >= (12, 0))
        or major > 22
    )
    if ok:
        return None
    return (
        f"web-UI build prerequisite: Node {_VITE8_NODE_FLOOR} required for "
        f"`npm run build -w web` (web/package.json pins vite ^8); found {raw}. "
        "This file drives the real build through an unstubbed subprocess.Popen, "
        "so it would block past the per-test timeout and kill the whole run."
    )


_WEB_BUILD_PREREQ_REASON = _web_build_prereq_failure()


# ── Prerequisite guard: tests that construct AIAgent against a local model ──
#
# These two build a real `AIAgent` with `base_url=http://127.0.0.1:11434/v1`.
# `agent_init` probes that endpoint (`detect_local_server_type` /
# `query_ollama_num_ctx`) several times over httpx. On a host where the port
# refuses immediately — nothing listening, or a real ollama answering — the
# probes cost milliseconds and the tests run normally. On a host where the port
# is BLACKHOLED (a firewall or a WSL/Docker port proxy that drops SYN instead
# of sending RST) every probe burns its full timeout, the test blows the 30s
# per-test cap, and pytest-timeout's thread method kills the whole process.
#
# The probe below distinguishes those cases directly: a refusal or a successful
# connect means the prerequisite holds and the guard stays inert.
_LOCAL_MODEL_PROBE_NODE_IDS = {
    "test_timeouts.py": frozenset({
        "test_default_non_stream_stale_timeout_auto_disables_for_local_endpoints",
        "test_explicit_non_stream_stale_timeout_is_honored_for_local_endpoints",
    }),
}


def _local_model_probe_failure() -> str | None:
    """Return a skip reason when 127.0.0.1:11434 neither answers nor refuses."""
    import socket as _socket

    try:
        conn = _socket.create_connection(("127.0.0.1", 11434), timeout=2.0)
    except (ConnectionRefusedError, OSError) as exc:
        if isinstance(exc, ConnectionRefusedError):
            return None  # fast refusal — the probes will fail fast too
        if not isinstance(exc, (TimeoutError, _socket.timeout)):
            return None  # some other immediate error — still fast
        return (
            "local-endpoint prerequisite: 127.0.0.1:11434 accepted neither a "
            "connection nor a refusal within 2s (the SYN is being dropped, not "
            "reset). AIAgent construction probes that endpoint over httpx, so "
            "this test would block past the per-test timeout and kill the "
            "whole run. Free the port or let it refuse to re-enable."
        )
    conn.close()
    return None


_LOCAL_MODEL_PROBE_REASON = _local_model_probe_failure()

# ── Environment-gap registry (audited 2026-08-10) ──────────────────────────
#
# 82 node ids across 44 files were registered here as pre-existing Windows/host
# gaps. Every one was reproduced individually. FIFTY were not gaps:
#
#   * three were REAL DEFECTS in our own code, which rule 3 of the fence
#     contract forbids registering at all:
#       - utils.atomic_replace only retried EXDEV/EBUSY, so concurrent
#         os.replace onto one target lost ~3 writes in 10 on Windows. It now
#         retries Win32 rename contention (fixed).
#       - hermes_cli/uninstall.py compared os.readlink()'s extended-length
#         (\\?\) target against an unprefixed root, so uninstall silently left
#         its own node/npm/npx symlinks behind — via a bare `continue`, with no
#         log. The candidate dirs include ~/.local/bin on EVERY platform
#         (fixed).
#       - hermes_cli/main.py swallowed the web-UI stamp failure at DEBUG, so a
#         missing pathspec meant a full npm install + Vite build on every boot
#         with nothing above DEBUG to say why; and the READER called the same
#         hash unguarded, so `hermes web` would have died on an unhandled
#         ModuleNotFoundError the moment a stamp existed. It never crashed only
#         because the swallowing writer guaranteed no stamp ever existed. Both
#         halves fixed, and pinned by portable tests that mock the import
#         failure so they run with or without pathspec.
#   * the rest were stale TESTS: a dozen read a UTF-8 file with no encoding=;
#     several asserted a POSIX path SPELLING that os.path.abspath
#     drive-qualifies; several pinned a sys.platform-selected POSIX branch
#     without pinning the branch, where a sibling test in the SAME FILE already
#     showed the convention; two leaked an open sqlite handle (test_kanban_boards
#     used `with kb.connect(...)`, which commits but never closes, though
#     kb.connect_closing exists for exactly this); one asserted a hardcoded
#     toolset list that upstream had since extended.
#
#   * THREE VACUOUS GREENS were caught in passing — tests that passed while
#     proving nothing. The worst: test_setup_matrix_e2ee's guard used
#     ast.walk(), which descends into function bodies, so it matched a DEFERRED
#     `import shutil`. Deleting the module-level import left it GREEN, which is
#     precisely the NameError it exists to prevent. It now walks tree.body.
#
# What is left below is genuine and probe-backed.
#
# ── 2026-08-10: the dependency rows were never environment gaps ────────────
#
# 17 of the surviving rows were DEPENDENCY-bound rather than platform-bound.
# croniter==6.0.0, pathspec==1.1.1 and pywinpty>=2.0.0 are all DECLARED in
# pyproject.toml, and all three were already present in the managed runtime
# venv (X:\Eternia\.hermes\venvs\hermes-agent) — they were missing only from
# the ambient C:\Python312 the tests run under. That is a BROKEN LOCAL INSTALL,
# not a property of this host, and the fence should never have described it as
# one. Installing them on the ambient interpreter retired 12 rows outright
# (croniter 2, pathspec 3, pywinpty 7), matching the fire/psutil precedent.
#
# The 13th pywinpty row did NOT pass, and what it was hiding is the reason this
# distinction matters. test_win_pty_bridge's test_cwd_is_respected matched the
# PTY's RAW bytes for a tmp_path longer than the terminal's 80-column width, so
# the ConPTY's hard line wrap split the path mid-token and the test pinned
# terminal geometry rather than the cwd. Fixed with a wrap-aware unwrap.
#
# Under that row, in tests/tools, sat a REAL DEFECT — see the commit message.
# tools/process_registry.py's Windows PTY stdin path was entirely non-functional
# while reporting {"status": "ok"}, and the ONLY test that would have caught it
# was gated on the pywinpty that was never installed.
#
# windows-curses is the one that stays: it is deliberately NOT declared, and
# curses_ui.py falls back to a numbered text menu, so it is a real optional
# extra rather than a broken install.
#
# NOT registered, deliberately, and therefore still RED — these are defects,
# and rule 3 says a defect gets fixed, not fenced:
#
#   * test_hooks_cli.py (3 nodes) — FIXED 2026-08-10, owner ruled. The cause
#     was agent/shell_hooks.py running shlex.split(spec.command) in POSIX mode
#     at :452/:817/:904, which ate every backslash in a Windows hook path, so
#     script_mtime_iso returned None and the "script modified since approval"
#     TAMPER CHECK at hermes_cli/hooks.py:369-376 could never fire. All three
#     sites now route through shell_hooks._split_command; the tamper-check node
#     is green. The other two nodes had a SECOND cause underneath, unrelated to
#     tokenization — a bare shebang script cannot be exec'd by this loader —
#     and are now probe-registered in _ENV_GAP_SKIPS below.
#   * test_commands.py::TestSlackNativeSlashes::test_telegram_parity —
#     slack_native_slashes() drops entries at _SLACK_MAX_SLASH_COMMANDS
#     (commands.py:1335) in registration order with NO accounting, so which
#     commands survive is a function of how many plugins are installed. The old
#     row also named the wrong casualty ('version', which is in
#     _SLACK_VIA_HERMES_ONLY); the command actually clamped off is 'platform'.
#     Which commands get pinned is product curation: owner call.
_ENV_GAPS: dict[str, list[tuple[str, str, set[str]]]] = {}

_POSIX_MODE_BITS_PROBE = None
_GIT_EOL_PROBE = None


def _no_module(name: str):
    """Return a probe that is True while ``name`` cannot be imported.

    It really imports rather than asking ``find_spec``. ``find_spec("curses")``
    answers yes on Windows — the pure-Python package ships with CPython, and it
    is the ``_curses`` extension underneath that is missing, which only
    executing the module body discovers. The registry ledger caught exactly
    that mistake in this file on its first run, which is the point of having a
    ledger rather than a printed warning.
    """
    cached: list[bool] = []

    def _probe() -> bool:
        if not cached:
            try:
                importlib.import_module(name)
            except Exception:
                cached.append(True)
            else:
                cached.append(False)
        return cached[0]

    return _probe


def _no_posix_mode_bits() -> bool:
    """True where os.chmod cannot express an owner-only file mode.

    Measured, not assumed: NTFS records only FILE_ATTRIBUTE_READONLY, so
    chmod(0o600) reads back as 0o666. The probe performs the actual round trip
    rather than testing the platform name.
    """
    global _POSIX_MODE_BITS_PROBE
    if _POSIX_MODE_BITS_PROBE is None:
        import stat as _stat
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as _tmp:
            _probe_file = os.path.join(_tmp, "mode_probe")
            with open(_probe_file, "w", encoding="utf-8"):
                pass
            os.chmod(_probe_file, 0o600)
            _POSIX_MODE_BITS_PROBE = (
                _stat.S_IMODE(os.stat(_probe_file).st_mode) != 0o600
            )
    return _POSIX_MODE_BITS_PROBE


def _no_os_chown() -> bool:
    """True where os.chown is absent — the POSIX service-manager seam."""
    return not hasattr(os, "chown")


def _no_posix_wait_status() -> bool:
    """True where a raw wait status cannot be decoded.

    kanban_db._classify_worker_exit uses os.WIFEXITED / WEXITSTATUS /
    WIFSIGNALED, and the exit registry it reads is populated only by
    reap_worker_zombies, itself gated ``os.name != "nt"``.
    """
    return not hasattr(os, "WIFEXITED")


def _no_posix_privilege_api() -> bool:
    """True where os.geteuid is absent, so root/sudo branches are unreachable."""
    return not hasattr(os, "geteuid")


def _posix_only_branch() -> bool:
    """True where the code under test selects its non-POSIX arm.

    Used ONLY where the production code itself branches on ``sys.platform`` and
    the assertion pins the arm this host never takes — i.e. where the platform
    genuinely is the mechanism rather than a stand-in for one.
    """
    return sys.platform == "win32"


def _git_name_only_ignores_cr_at_eol() -> bool:
    """True where `git diff --name-only --ignore-cr-at-eol` is not honoured.

    _normalize_managed_eol() derives its EOL-only set as
    ``dirty - dirty(--ignore-cr-at-eol)``. Below git 2.32 the --name-only
    output is decided before the content-level ignore rules run, so that set is
    always empty and the function pins core.autocrlf without restoring
    anything. A toolchain version, not a platform.
    """
    global _GIT_EOL_PROBE
    if _GIT_EOL_PROBE is None:
        import re as _re
        import subprocess as _sp

        try:
            raw = _sp.run(
                ["git", "--version"], capture_output=True, text=True, timeout=15
            ).stdout
            match = _re.search(r"(\d+)\.(\d+)", raw or "")
            _GIT_EOL_PROBE = (
                True if match is None
                else (int(match.group(1)), int(match.group(2))) < (2, 32)
            )
        except Exception:
            _GIT_EOL_PROBE = True
    return _GIT_EOL_PROBE


_SHEBANG_EXEC_PROBE = None


def _no_shebang_script_execution() -> bool:
    """True where the OS cannot spawn a ``#!``-prefixed script directly.

    The shebang is honoured by the kernel's exec, not by the file: Windows'
    CreateProcess has no equivalent, so ``subprocess.run([r"C:\\...\\hook.sh"])``
    raises ``OSError: [WinError 193] %1 is not a valid Win32 application`` no
    matter how the path is spelled. Interpreter-prefixed hook commands
    (``python C:\\...\\hook.py``) are unaffected and still run here — it is the
    bare-script spawn shape alone that is unavailable.

    Probed by performing the spawn, because there is no attribute to test for:
    the answer is a property of the loader, not of the standard library.
    """
    global _SHEBANG_EXEC_PROBE
    if _SHEBANG_EXEC_PROBE is None:
        import subprocess as _sp
        import tempfile as _tempfile

        with _tempfile.TemporaryDirectory() as _tmp:
            _script = os.path.join(_tmp, "shebang_probe.sh")
            with open(_script, "w", encoding="utf-8", newline="\n") as _fh:
                _fh.write("#!/bin/sh\nexit 0\n")
            os.chmod(_script, 0o755)
            try:
                _sp.run([_script], capture_output=True, timeout=15)
            except OSError:
                _SHEBANG_EXEC_PROBE = True
            else:
                _SHEBANG_EXEC_PROBE = False
    return _SHEBANG_EXEC_PROBE


_ENV_GAP_SKIPS: EnvGapSkipRegistry = {
    # ── Spawn shapes the loader does not support ──────────────────────────
    #
    # Registered 2026-08-10, when the shlex path-spelling defect that used to
    # be the FIRST cause of these two failures was fixed (agent/shell_hooks.py
    # now tokenizes through _split_command). Underneath it sat this second,
    # independent cause, which the fix does not touch: both tests write a bare
    # `#!/usr/bin/env bash` script and require `hermes hooks test` to actually
    # execute it. Their sibling TestHooksDoctor::test_flags_mtime_drift — the
    # node that pinned the tamper check — is NOT registered and now passes.
    'test_hooks_cli.py': [
        (
            _no_shebang_script_execution,
            'these two spawn a bare `#!/usr/bin/env bash` hook script through '
            'shell_hooks._spawn (shell=False, argv[0] = the .sh itself), and '
            'this loader cannot exec a shebang script — WinError 193. The hook '
            'machinery itself is platform-neutral: an interpreter-prefixed '
            'command (`python <path>.py`) runs here',
            {
                'TestHooksTest::test_fires_real_subprocess_and_parses_block',
                'TestHooksTest::test_synthetic_payload_matches_production_shape',
            },
        ),
    ],
    # ── POSIX-only syscalls: the mechanism is a missing attribute ──────────
    'test_container_boot.py': [
        (
            _no_os_chown,
            's6 container supervision is Linux-only; service_manager._mkdir_owned '
            'calls os.chown, which does not exist on this platform',
            {
                'test_profiles_default_subdir_is_skipped_with_warning',
                'test_register_service_overwrites_existing_slot',
                'test_registered_profile_has_finish_script',
                'test_running_profile_is_registered_and_autostarted',
            },
        ),
    ],
    'test_ensure_hermes_home_uid_34107.py': [
        (
            _no_os_chown,
            'os.chown does not exist on this platform, and config.py:713 returns '
            '(None, None) from _resolve_hermes_uid_gid on win32 by design — the '
            'same guard the sibling TestSecureDirChown in this file already '
            'carries',
            {
                'TestChownToHermesUid::test_attributeerror_swallowed_for_windows_compat',
                'TestChownToHermesUid::test_calls_os_chown_when_both_set',
                'TestChownToHermesUid::test_eperm_is_silently_swallowed',
                'TestResolveHermesUidGid::test_returns_parsed_values_when_both_set',
            },
        ),
    ],
    'test_service_manager.py': [
        (
            _no_os_chown,
            'os.chown does not exist on this platform and systemd units cannot '
            'be written; service_manager._mkdir_owned is the POSIX seam',
            {
                'test_s6_log_run_creates_leaf_as_hermes_without_chown',
                'test_seed_supervise_skeleton_creates_expected_layout',
            },
        ),
    ],
    'test_ensure_acp_launcher.py': [
        (
            _no_posix_privilege_api,
            'os.geteuid does not exist on this platform; _ensure_acp_launcher '
            'also returns early on win32 (update_cmd.py:2198), and chmod(0o555) '
            'cannot make an NTFS directory unwritable',
            {
                'test_unwritable_bin_dir_is_skipped',
            },
        ),
    ],
    'test_apply_profile_override.py': [
        (
            _no_module("pwd"),
            'sudo profile resolution reads the POSIX account database '
            '(main.py:546 _resolve_sudo_user_profile_env imports pwd behind an '
            'os.geteuid()==0 gate); the pwd module does not exist here',
            {
                'TestApplyProfileOverrideHermesHomeGuard::test_sudo_explicit_profile_resolves_invoking_users_profile',
            },
        ),
    ],
    'test_kanban_core_functionality.py': [
        (
            _no_posix_wait_status,
            '_classify_worker_exit decodes a raw POSIX wait status '
            '(os.WIFEXITED / WEXITSTATUS / WIFSIGNALED); the exit registry it '
            'reads is populated only by reap_worker_zombies, gated '
            'os.name != "nt"',
            {
                'test_protocol_violation_budget_not_consumed_by_other_failures',
            },
        ),
    ],
    'test_kanban_db.py': [
        (
            _no_posix_wait_status,
            'same _classify_worker_exit POSIX wait-status decode. NB the old '
            'row claimed this asserts "POSIX signal delivery to synthetic PIDs" '
            '— it does not; _pid_alive is stubbed and no signal is ever sent',
            {
                'test_rate_limit_exit_requeues_without_counting_failure',
            },
        ),
    ],
    # ── NTFS cannot express a POSIX file mode ──────────────────────────────
    'test_profiles.py': [
        (
            _no_posix_mode_bits,
            'asserts a 0o600 .env mode; this filesystem records only a '
            'read-only bit, so os.chmod(0o600) reads back as 0o666. SEE THE '
            'ESCALATION FILED WITH THIS CHANGE: profile .env files hold API '
            'keys and are genuinely NOT owner-restricted on Windows, which '
            'wants an ACL path rather than a skipped test',
            {
                'TestBackfillProfileEnvs::test_copies_default_env_into_envless_profiles',
                'TestCreateProfile::test_seeds_placeholder_env_file',
            },
        ),
    ],
    'test_web_server_oauth_write.py': [
        (
            _no_posix_mode_bits,
            'asserts a 0o600 file mode; this filesystem records only a '
            'read-only bit. The guarantee is not lost — the sibling '
            'test_dashboard_oauth_write_uses_atomic_json_write_with_owner_only_mode '
            'pins atomic_json_write(mode=0o600) portably and runs here',
            {
                'test_dashboard_oauth_write_uses_owner_only_permissions',
            },
        ),
    ],
    # ── absent modules ─────────────────────────────────────────────────────
    #
    # The croniter / pathspec / pywinpty rows that stood here were retired on
    # 2026-08-10 by INSTALLING those packages on this interpreter — see the
    # "broken install, not an environment gap" note in the audit block above.
    # Only the two genuinely-absent modules are left.
    'test_session_browse.py': [
        (
            _no_module("curses"),
            "the '_curses' extension is unavailable. Genuinely OPTIONAL, unlike "
            'the three retired dependency rows: windows-curses is deliberately '
            'not declared in pyproject.toml, and curses_ui.py:623-624 catches '
            'the ImportError and returns the numbered text fallback, so the '
            'pickers degrade by design rather than break. These two tests pin '
            'the curses branch specifically, which this host cannot select',
            {
                'TestCursesBrowse::test_escape_cancels',
                'TestCursesBrowse::test_type_to_filter_then_enter',
            },
        ),
    ],
    'test_web_ui_build.py': [
        (
            _no_module("fcntl"),
            'the flock test imports fcntl; main.py:5602-5605 explicitly falls '
            'through on ImportError ("Windows: no flock"), so the branch under '
            'test is unreachable here',
            {
                'TestBuildWebUIFlock::test_contended_lock_without_dist_waits_then_skips_fresh_build',
            },
        ),
    ],
    # ── toolchain version ──────────────────────────────────────────────────
    'test_update_eol_churn.py': [
        (
            _git_name_only_ignores_cr_at_eol,
            'git toolchain floor: below 2.32 `git diff --name-only '
            '--ignore-cr-at-eol` still lists a file whose full '
            '--ignore-cr-at-eol diff is empty, so _normalize_managed_eol() '
            'derives an always-empty EOL-only set and pins core.autocrlf '
            'without restoring anything',
            {
                'test_churn_across_more_files_than_fit_in_one_argv',
                'test_churn_invisible_under_autocrlf_true_is_still_found',
                'test_churn_is_cleared_and_the_pin_is_persisted',
                'test_real_edits_survive_even_when_line_endings_also_flipped',
            },
        ),
    ],
    # ── the production code itself branches on sys.platform ────────────────
    'test_cmd_update.py': [
        (
            _posix_only_branch,
            'cmd_update prepends `git -c windows.appendAtomically=false` on '
            'win32 and resolves npm as npm.CMD; the mocks assert the POSIX '
            'argv. WARNING: this file only stubs subprocess.run, so '
            '_build_web_ui still runs a REAL npm install + vite build against '
            'the checkout — treat it as side-effecting',
            {
                'TestCmdUpdateBranchFallback::test_update_on_fork_checks_upstream_when_origin_up_to_date',
            },
        ),
    ],
}

def pytest_configure(config):  # noqa: D401 — pytest hook
    """Register the environment-gap marks (see the block comment above)."""
    config.addinivalue_line(
        "markers",
        f"{_gateway_fence.REAL_PAUSE_MARK}: let this test drive the REAL "
        "_pause_windows_gateways_for_update (it reads this machine's live "
        "gateway table and Scheduled Task). The test must mock the spawn "
        "itself; the process-wide gateway fence still stands behind it.",
    )
    config.addinivalue_line(
        "markers",
        f"{_WINDOWS}: pre-existing failure caused by POSIX-only test "
        "expectations that Windows cannot satisfy. Not a fork regression; "
        "deselect with -m 'not windows_env_gap'.",
    )
    config.addinivalue_line(
        "markers",
        f"{_HOST}: pre-existing failure caused by a missing host package or "
        "toolchain (croniter / pywinpty / pathspec, Node "
        ">=20.19 for the Vite 8 web build, git >=2.31 semantics for "
        "`--name-only --ignore-cr-at-eol`, outbound HTTP). Not a fork "
        "regression; deselect with -m 'not host_dependency_gap'.",
    )


def pytest_collection_modifyitems(items):  # noqa: D401 — pytest hook
    """Attach the environment-gap mark to every registered node id."""
    for item in items:
        if (
            _WEB_BUILD_PREREQ_REASON is not None
            and item.path.name in _WEB_BUILD_PREREQ_FILES
        ):
            item.add_marker(pytest.mark.skip(reason=_WEB_BUILD_PREREQ_REASON))
        if _LOCAL_MODEL_PROBE_REASON is not None:
            probe_ids = _LOCAL_MODEL_PROBE_NODE_IDS.get(item.path.name)
            _, _, probe_name = item.nodeid.partition("::")
            if probe_ids is not None and probe_name in probe_ids:
                item.add_marker(pytest.mark.skip(reason=_LOCAL_MODEL_PROBE_REASON))
        groups = _ENV_GAPS.get(item.path.name)
        if groups is None:
            continue
        _, _, within_file = item.nodeid.partition("::")
        for mark, reason, node_ids in groups:
            if within_file in node_ids:
                item.add_marker(getattr(pytest.mark, mark)(reason=reason))
    apply_skips(items, _ENV_GAP_SKIPS)


_STALE_ENV_GAP_ENTRIES: list[str] = []


# ── Known defects that are deliberately NOT fenced ─────────────────────────
#
# The 2026-08-10 registry audit left these RED on purpose. Rule 3 of the fence
# contract says a failure caused by a defect in our own code gets FIXED, not
# registered — and both of these need an owner decision, so neither could be
# fixed inside that audit. Without this banner the next person to run the suite
# sees a red on Windows and files it back into the registry as an environment
# gap, which is precisely how the tamper-check hole below stayed invisible.
# The test_hooks_cli.py entry was REMOVED on 2026-08-10: the defect it named is
# fixed. agent/shell_hooks.py no longer parses hook commands with POSIX-mode
# shlex — _split_command keeps the backslashes, script_mtime_iso resolves, and
# the "script modified since approval" tamper check fires again (pinned by
# TestHooksDoctor::test_flags_mtime_drift, which is green). A banner announcing
# a defect that no longer exists is the same stale claim in the other
# direction, so it does not outlive the fix. The two remaining reds in that
# file have a different, independent cause and are registered in
# _ENV_GAP_SKIPS above with a live probe.
#: The one-line reason the ``xfail`` mark on ``test_telegram_parity`` carries.
#: The mark IMPORTS this name (``tests/hermes_cli/test_commands.py``) rather
#: than restating it, so the fence and the report cannot drift apart into two
#: accounts of one defect — the register-rot shape C25 is about.
TELEGRAM_PARITY_DEFECT_REASON = (
    "KNOWN DEFECT (owner call, not an environment gap): Slack's 50-slash app "
    "cap drops '/platform', a canonical gateway command with no native Slack "
    "slot, so Telegram/Slack parity cannot hold until an owner either pins it "
    "a slot (something else loses one) or declares it _SLACK_VIA_HERMES_ONLY. "
    "strict=True: the day parity holds, this XPASSes and reds — delete the "
    "mark and this row. Full account: _KNOWN_DEFECTS in "
    "tests/hermes_cli/conftest.py."
)

_KNOWN_DEFECTS: dict[str, str] = {
    "test_commands.py": (
        "KNOWN DEFECT — NOT an environment gap. Slack allows an app only 50\n"
        "  slash commands, and the registry no longer fits: 'platform' is a\n"
        "  gateway command on Telegram/Discord/CLI with NO native Slack slash,\n"
        "  so test_telegram_parity cannot pass. It is the only CANONICAL\n"
        "  casualty: every other name the cap drops is an alias whose\n"
        "  canonical spelling either still holds a native slot or is already a\n"
        "  deliberate _SLACK_VIA_HERMES_ONLY entry. (The exact casualty set\n"
        "  depends on which plugins are installed — the WARNING names them.)\n"
        "  The silence half of this finding is FIXED: the clamp is accounted\n"
        "  for at the one branch that performs it, slack_native_slashes() logs\n"
        "  every dropped name at WARNING, `hermes slack manifest` prints them\n"
        "  to stderr, and slack_clamped_slashes() returns the same list\n"
        "  (hermes_cli/commands.py). Visibility is not parity, though — naming\n"
        "  the casualty does not give /platform a slot, so the DEFECT STAYS.\n"
        "  It is now fenced as xfail(strict=True) rather than left as a\n"
        "  permanent red (ML-16 / B20(iv)): a file that can never be green has\n"
        "  no red left to spend on a REGRESSION, and the canonical per-file\n"
        "  runner's red definition could never be all-green while it stood.\n"
        "  strict is what keeps the fence honest — the day parity actually\n"
        "  holds, the test XPASSes and reds, and someone must come delete the\n"
        "  mark and this row. Fenced is not fixed.\n"
        "  Closing it means either pinning 'platform' a native slot (something\n"
        "  else then loses one) or declaring it Slack-via-/hermes in\n"
        "  _SLACK_VIA_HERMES_ONLY. Which commands get a native slot is product\n"
        "  curation, so it is an owner decision. The 50 is SLACK'S limit, not\n"
        "  ours — do not 'fix' this by raising it. Do NOT re-file this as\n"
        "  host_dependency_gap."
    ),
}

_KNOWN_DEFECT_FAILURES: list[str] = []


def pytest_runtest_logreport(report):  # noqa: D401 — pytest hook
    """Record stale env-gap passes, and the known-defect tests' outcomes.

    The known-defect test is ``xfail(strict=True)``, so its ordinary outcome is
    ``skipped`` with ``wasxfail`` set — NOT ``failed``. Matching on ``failed``
    alone would have silenced this banner the moment the mark landed, which is
    exactly the hazard fencing a defect creates: the fence must not also
    retire the report. The ``failed`` arm still earns its place, because a
    strict XPASS arrives as ``failed`` with no ``wasxfail`` — and that is the
    day someone must read the row and delete it.
    """
    if report.when != "call":
        return
    file_name = report.nodeid.split("::", 1)[0].rsplit("/", 1)[-1]

    if file_name in _KNOWN_DEFECTS and (
        report.outcome == "failed" or hasattr(report, "wasxfail")
    ):
        _KNOWN_DEFECT_FAILURES.append(report.nodeid)
        return

    if report.outcome != "passed":
        return
    groups = _ENV_GAPS.get(file_name)
    if groups is None:
        return
    _, _, within_file = report.nodeid.partition("::")
    if any(within_file in node_ids for _, _, node_ids in groups):
        _STALE_ENV_GAP_ENTRIES.append(report.nodeid)


def pytest_terminal_summary(terminalreporter):  # noqa: D401 — pytest hook
    """Surface stale registry rows, and explain the deliberate reds."""
    if _KNOWN_DEFECT_FAILURES:
        terminalreporter.write_sep(
            "=", "KNOWN DEFECTS — fenced xfail(strict), still open"
        )
        seen: set[str] = set()
        for nodeid in sorted(set(_KNOWN_DEFECT_FAILURES)):
            file_name = nodeid.split("::", 1)[0].rsplit("/", 1)[-1]
            terminalreporter.write_line(f"  {nodeid}")
            if file_name not in seen:
                seen.add(file_name)
                terminalreporter.write_line(f"  {_KNOWN_DEFECTS[file_name]}")
                terminalreporter.write_line("")

    if not _STALE_ENV_GAP_ENTRIES:
        return
    terminalreporter.write_sep("=", "stale environment-gap registry entries")
    terminalreporter.write_line(
        "These node ids are registered in _ENV_GAPS (tests/hermes_cli/conftest.py) "
        "but PASSED. Delete their rows — a stale row hides a future regression."
    )
    for nodeid in sorted(set(_STALE_ENV_GAP_ENTRIES)):
        terminalreporter.write_line(f"  {nodeid}")
