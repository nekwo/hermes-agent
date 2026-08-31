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


def _backend_subcommand(tokens: list[str]) -> str | None:
    """Which backend this argv would START, or ``None``."""
    entry = None
    for index, token in enumerate(tokens):
        normalized = str(token).replace("\\", "/").lower()
        if normalized.rsplit("/", 1)[-1] in _HERMES_ENTRYPOINT_BASENAMES:
            entry = index
            break
        if normalized == "hermes_cli.main" or normalized.endswith("hermes_cli/main.py"):
            entry = index
            break
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

    if _names_real_root(text, env):
        return (
            f"it resolves the operator's REAL hermes store ({_REAL_ROOT}). "
            "Tests run against the hermetic home that tests/conftest.py's "
            "_hermetic_environment fixture provides"
        )

    return None


def _refuse(primitive: str, cmd, reason: str):
    raise GatewayFenceViolation(
        f"tests/hermes_cli gateway fence: blocked {primitive}({cmd!r}) — "
        f"{reason}.\n"
        "This fence is process-wide and is NOT undone at test teardown, so it "
        "also covers collection, background threads and atexit handlers — the "
        "window the measured escape actually used (see "
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
        reason = classify(command)
        if reason:
            _refuse("os.system", command, reason)
        return real_system(command)

    def _fenced_os_popen(cmd, *args, **kwargs):
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

    def _fenced_spawn_detached(script_path=None):
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
    "classify",
    "install",
    "is_installed",
    "real_root",
]
