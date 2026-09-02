"""Suite-wide fence: nothing under ``tests/hermes_cli`` starts a live gateway.

The hole this closes, measured on this Windows workstation 2026-08-31
(``tests/hermes_cli/test_update_autostash.py`` alone reproduces it in 7s)::

    test_update_autostash.py:187 test_cmd_update_skips_stash_restore_when_reset_fails
      -> hermes_cli/main.py:9482            cmd_update
      -> hermes_cli/update_cmd.py:3234      _cmd_update_impl
           atexit.register(_resume_windows_gateways_after_update,
                           {... "cold_start_if_installed": True})
    -- pytest process exit: EVERY fixture has torn down, every monkeypatch
       has been undone, HERMES_HOME is back to the operator's real value --
      -> update_cmd.py:2990 _refresh_windows_gateway_launchers
           schtasks /Query /TN Hermes_Gateway_alice        (the REAL task)
      -> update_cmd.py:2851 _cold_start_windows_gateway_after_update
      -> gateway_windows.py:990 _spawn_detached
           Popen: C:\\Python312\\python.exe -m hermes_cli.main
                  --profile alice gateway run              (LIVE, outlives us)

Three things make that escape invisible to the guards that already exist:

1. ``tests/conftest.py::_live_system_guard`` is an autouse **fixture**. Its
   subprocess wrappers are installed at test setup and removed at test
   teardown, so a callable parked on ``atexit`` runs in a window the fixture
   provably cannot cover. Every arm of that guard -- the backend-spawn
   classifier included -- is simply absent at interpreter exit.
2. ``_hermetic_environment`` redirects ``HERMES_HOME`` with ``monkeypatch``,
   which restores the *pre-test* value on teardown. On this host that value is
   the operator's real store (``X:\\Eternia\\.hermes``), and importing
   ``hermes_cli.main`` resolves it one step further to the real
   ``profiles/alice`` via the ``active_profile`` marker. So the exit-time
   spawn does not merely start a gateway -- it starts the operator's gateway.
3. Nothing in the chain fails. ``_cold_start_windows_gateway_after_update``
   is best-effort by design and swallows its own exceptions, so even a
   crashing spawn would have printed nothing a suite reader would read.

The fence therefore has three layers, and each one is placed at the level the
escape actually used:

* **L1 root cause** (``_no_windows_gateway_pause_token``, an autouse fixture):
  no test in this directory drives the REAL
  ``_pause_windows_gateways_for_update``. It returns ``None`` -- "no gateway
  to pause" -- so the atexit handler is never registered in the first place
  and no test ever reads the operator's live gateway process table or
  Scheduled Task. Tests that are *about* that path opt out with
  ``@pytest.mark.real_windows_gateway_pause`` and bring their own mocks.
* **L2 chokepoint** (``_install_process_fence``): ``gateway_windows
  ._spawn_detached`` -- the single function every Windows gateway start goes
  through -- is replaced for the whole session and never restored. Session
  scope, not fixture scope, is the entire point: it holds during collection,
  during teardown, on background threads, and at ``atexit``.
* **L3 backstop** (same installer): the subprocess spawn primitives are
  wrapped once, process-wide, and refuse a hermes-backend argv, the Windows
  gateway launcher scripts, a mutating ``schtasks`` verb, and any spawn whose
  argv or ``HERMES_HOME`` names the operator's real store root. This is the
  layer that catches a *future* path -- one that does not go through
  ``_spawn_detached`` and does not exist yet.

L3 deliberately duplicates part of the root conftest's classifier rather than
importing it: the root's version is built inside a fixture body and is not
callable from module scope, and the two answer different questions anyway
(the root guard asks "is this test misbehaving", this one asks "is this
PROCESS about to touch the operator's machine, whoever is on the stack").

Refusals raise ``GatewayFenceViolation``. That is a hard error on purpose --
the failure mode this replaces was silent.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

#: Marker letting a test drive the real Windows gateway pause/resume path.
REAL_PAUSE_MARK = "real_windows_gateway_pause"

#: Subcommands that BOOT a backend when they follow a hermes entry point.
_BACKEND_SUBCOMMANDS = ("gateway", "serve", "dashboard")
_HERMES_ENTRYPOINT_BASENAMES = ("hermes", "hermes.exe")

#: The persistence artifacts a Windows install launches the gateway through.
_LAUNCHER_BASENAMES = ("gateway.vbs", "gateway.cmd")

#: ``schtasks`` verbs that CHANGE the operator's registered task. ``/Query``
#: is read-only and stays allowed: it is how a test can still assert on the
#: install-detection branch, and reading a task cannot start one.
_SCHTASKS_MUTATING_VERBS = ("/create", "/delete", "/change", "/run", "/end")


class GatewayFenceViolation(RuntimeError):
    """A test (or something it left running) tried to touch the live gateway."""


def _real_hermes_root() -> Path | None:
    """The operator's real store root, captured before any test redirects it.

    Read from ``hermes_constants`` rather than ``os.environ`` so a suite
    started with ``HERMES_HOME`` already pointing somewhere hermetic still
    learns the path it must never spawn against.
    """
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root()).resolve()
    except Exception:
        return None


_REAL_ROOT = _real_hermes_root()


def _tokens(cmd) -> list[str]:
    """Flatten a command into tokens without eating Windows backslashes."""
    if isinstance(cmd, (list, tuple)):
        raw = [str(token) for token in cmd]
    elif isinstance(cmd, (bytes, bytearray)):
        raw = [bytes(cmd).decode(errors="replace")]
    else:
        text = "" if cmd is None else str(cmd)
        try:
            raw = shlex.split(text)
        except ValueError:
            raw = text.split()
    # A wrapper's argument is itself a whole command (``bash -c "hermes
    # gateway run"``), so split on whitespace too. shlex would eat the
    # backslashes in a Windows path; a plain split cannot invent an entry
    # point, because a path containing spaces still ends in its own basename.
    tokens: list[str] = []
    for token in raw:
        tokens.extend(str(token).split())
    return tokens


def _basename(token: str) -> str:
    return str(token).replace("\\", "/").rsplit("/", 1)[-1].lower()


def _hermes_entry_index(tokens: list[str]) -> int | None:
    """Position of the hermes entry point in this argv, or ``None``."""
    for index, token in enumerate(tokens):
        normalized = str(token).replace("\\", "/").lower()
        if normalized.rsplit("/", 1)[-1] in _HERMES_ENTRYPOINT_BASENAMES:
            return index
        if normalized == "hermes_cli.main" or normalized.endswith("hermes_cli/main.py"):
            return index
    return None


def _backend_subcommand(tokens: list[str]) -> str | None:
    """Which backend this argv would START, or ``None``."""
    entry = _hermes_entry_index(tokens)
    if entry is None:
        return None
    # Scan every non-flag token after the entry point: flag arity is unknowable
    # here and ``hermes --profile x gateway run`` puts the subcommand at 3.
    for token in tokens[entry + 1:]:
        if str(token).startswith("-"):
            continue
        if str(token).lower() in _BACKEND_SUBCOMMANDS:
            return str(token).lower()
    return None


def _names_real_root(text: str, env) -> bool:
    if _REAL_ROOT is None:
        return False
    needle = str(_REAL_ROOT).replace("\\", "/").lower()
    if needle in text.replace("\\", "/").lower():
        return True
    if not env:
        return False
    home = env.get("HERMES_HOME")
    if not home:
        return False
    try:
        candidate = Path(home).resolve()
    except Exception:
        return False
    return candidate == _REAL_ROOT or _REAL_ROOT in candidate.parents


def classify(cmd, env=None) -> str | None:
    """Return why this spawn is refused, or ``None`` when it is allowed."""
    tokens = _tokens(cmd)
    if not tokens:
        return None
    text = " ".join(tokens)

    backend = _backend_subcommand(tokens)
    if backend is not None:
        return (
            f"it would START a live hermes backend (`{backend}`). A real boot "
            "resolves its own runtime root, binds a port and OUTLIVES the "
            "test process"
        )

    for token in tokens:
        if _basename(token) in _LAUNCHER_BASENAMES:
            return (
                "it runs an installed Windows gateway launcher script "
                f"({_basename(token)}), which starts the real gateway"
            )

    if any(_basename(token) in ("schtasks", "schtasks.exe") for token in tokens):
        lowered = [str(token).lower() for token in tokens]
        for verb in _SCHTASKS_MUTATING_VERBS:
            if verb in lowered:
                return (
                    f"it would run `schtasks {verb}` against this machine's real "
                    "Task Scheduler"
                )

    # The real-store arm is scoped to argv that would boot OUR OWN runtime.
    # A blanket "names the real root" refusal was measured over-wide: it also
    # caught ``X:\...\.hermes\profiles\alice\node\agent-browser.CMD --version``,
    # a read-only capability probe reached from ``hermes doctor`` through an
    # import-time-resolved path -- 44 reds across 8 files in the 2026-08-31
    # measurement run, not one of them the hazard this fence is for. That probe
    # IS a real find (the suite resolves a tool out of the operator's live
    # profile) but it is a separate lane, and a ``--version`` call starts
    # nothing. What this arm must catch is a hermes process coming up on the
    # operator's store: the second half of the measured escape.
    #
    # 2026-09-01: the COUNT was cut, the exemption was not.
    # ``agent_browser_runnable`` now memoises its ``--version`` spawn per path
    # (``hermes_constants._AGENT_BROWSER_PROBE_CACHE``), so
    # ``tests/hermes_cli/test_doctor.py`` went from 29 spawns of that CMD to 1,
    # measured on this host. One is still one: the path is import-bound in
    # ``doctor``, so the suite still reaches the operator's live profile and
    # this arm must still let it through. Deleting the exemption needs the
    # OTHER half — resolving the browser path through the profile under test.
    if _hermes_entry_index(tokens) is not None and _names_real_root(text, env):
        return (
            f"it would run hermes against the operator's REAL store ({_REAL_ROOT}). "
            "Tests run against the hermetic home that tests/conftest.py's "
            "_hermetic_environment fixture provides"
        )

    return None


# ── Arming: the wrappers are permanent, the REFUSAL is scoped ──────────────
#
# The wrappers below are installed once and never removed — that is what makes
# the atexit window reachable at all, and it must not change. But "installed"
# and "refusing" have to be two different things, because this module is
# imported the moment pytest COLLECTS anything under tests/hermes_cli, and in a
# combined run that is the same process that later runs tests/agent_runtime.
# Those tests spawn real ``hermes_cli.main harness serve`` children ON PURPOSE
# (test_serve_socket_child_e2e, test_gateway_peer_cross_install_chat_e2e,
# test_gateway_peer_two_roots_e2e) against their own sandboxed roots, and a
# process-global refusal reds them: measured 2026-08-31, 6 failed / 5 errors in
# the wave-close run, every one of them green in isolation.
#
# So the refusal is ARMED:
#   * for the duration of each test that lives under tests/hermes_cli — the
#     autouse fixture in this directory's conftest is exactly that scope, since
#     a directory conftest's fixtures run for its own items and nothing else;
#   * permanently from ``pytest_sessionfinish`` onward, which is the window the
#     escape actually used. Arming is a FLAG, not an atexit handler, so it does
#     not have to win a LIFO race against the handler ``_cmd_update_impl``
#     registered mid-test: the wrapper reads the flag when the spawn is
#     attempted, and by then the latch is down.
#
# Everywhere else — collection, tests outside this directory, their fixtures
# and their children — the wrappers pass straight through.

_ARMED = False
_LATCHED = False


def arm() -> None:
    """Refuse gateway/backend spawns from now until :func:`disarm`."""
    global _ARMED
    _ARMED = True


def disarm() -> None:
    """Stop refusing — unless the session-finish latch is already down."""
    global _ARMED
    _ARMED = _LATCHED


def arm_permanently() -> None:
    """Latch the refusal on for the rest of the process (the atexit window)."""
    global _ARMED, _LATCHED
    _LATCHED = True
    _ARMED = True


def is_armed() -> bool:
    return _ARMED


def _refuse(primitive: str, cmd, reason: str):
    raise GatewayFenceViolation(
        f"tests/hermes_cli gateway fence: blocked {primitive}({cmd!r}) — "
        f"{reason}.\n"
        "The wrappers are process-wide and are never removed, so this fence "
        "still covers background threads and atexit handlers — the window the "
        "measured escape actually used. The REFUSAL is armed only for tests "
        "under tests/hermes_cli and from session finish onward, so it is not "
        "what you are seeing if you are outside that directory (see "
        "tests/hermes_cli/_gateway_fence.py).\n"
        "Drive the code in-process (build the parser, call the handler, fake "
        "the transport) instead of spawning, or — if the test is ABOUT the "
        "Windows gateway pause/resume path — mark it "
        f"@pytest.mark.{REAL_PAUSE_MARK} and mock the spawn yourself."
    )


_INSTALLED = False


def _install_spawn_wrappers() -> None:
    """Wrap the spawn primitives once, for the life of the process."""
    real_popen = subprocess.Popen

    class _FencedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, cmd, *args, **kwargs):
            if _ARMED:
                reason = classify(cmd, kwargs.get("env"))
                if reason:
                    _refuse("subprocess.Popen", cmd, reason)
            super().__init__(cmd, *args, **kwargs)

    _FencedPopen.__name__ = "Popen"
    _FencedPopen.__qualname__ = "Popen"
    subprocess.Popen = _FencedPopen

    for name in ("run", "call", "check_call", "check_output"):
        real = getattr(subprocess, name)

        def _make(primitive, func):
            def _fenced(cmd, *args, **kwargs):
                if _ARMED:
                    reason = classify(cmd, kwargs.get("env"))
                    if reason:
                        _refuse(f"subprocess.{primitive}", cmd, reason)
                return func(cmd, *args, **kwargs)

            _fenced.__name__ = f"_fenced_{primitive}"
            if hasattr(func, "__class_getitem__"):
                _fenced.__class_getitem__ = func.__class_getitem__
            return _fenced

        setattr(subprocess, name, _make(name, real))

    real_system = os.system
    real_os_popen = os.popen

    def _fenced_system(command):
        if _ARMED:
            reason = classify(command)
            if reason:
                _refuse("os.system", command, reason)
        return real_system(command)

    def _fenced_os_popen(cmd, *args, **kwargs):
        if _ARMED:
            reason = classify(cmd)
            if reason:
                _refuse("os.popen", cmd, reason)
        return real_os_popen(cmd, *args, **kwargs)

    os.system = _fenced_system
    os.popen = _fenced_os_popen


def _install_spawn_detached_stub() -> None:
    """Replace the Windows gateway's single spawn chokepoint for the session.

    Importing ``gateway_windows`` is safe on every platform (it only *raises*
    when a Windows-only function is called), so the stub is installed
    unconditionally: a POSIX run that ever reaches this call site should red
    the same way a Windows one does.
    """
    try:
        from hermes_cli import gateway_windows
    except Exception:
        return

    real_spawn_detached = gateway_windows._spawn_detached

    def _fenced_spawn_detached(script_path=None):
        if not _ARMED:
            # Outside this directory's tests the chokepoint is not ours to
            # close; hand the call back to production unchanged.
            return real_spawn_detached(script_path)
        _refuse(
            "gateway_windows._spawn_detached",
            script_path,
            "it is the single chokepoint every Windows gateway start goes "
            "through, and a spawned gateway outlives the suite",
        )

    gateway_windows._spawn_detached = _fenced_spawn_detached


def install() -> None:
    """Install every layer. Idempotent; never uninstalled."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    _install_spawn_wrappers()
    _install_spawn_detached_stub()


def is_installed() -> bool:
    return _INSTALLED


def real_root() -> Path | None:
    return _REAL_ROOT


__all__ = [
    "GatewayFenceViolation",
    "REAL_PAUSE_MARK",
    "arm",
    "arm_permanently",
    "classify",
    "disarm",
    "install",
    "is_armed",
    "is_installed",
    "real_root",
]
