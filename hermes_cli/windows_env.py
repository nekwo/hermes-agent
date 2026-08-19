"""Registry-safe helpers for persisting User-scope environment on Windows.

The installer historically mutated ``HKCU\\Environment`` from PowerShell via
``[Environment]::SetEnvironmentVariable(..., "User")``.  When the *pip / Mission
Control* install channel runs ``hermes postinstall`` there is no PowerShell
installer in the loop, so this module gives the Python side the same registry
capabilities directly — no subprocess, works under Constrained Language Mode.

Everything here is a no-op (returning ``False`` / doing nothing) off Windows so
callers don't need their own platform guards.

The uninstall side of this contract lives in ``hermes_cli/uninstall.py``
(``remove_path_from_windows_registry`` / ``remove_hermes_env_vars_windows``).
That file is UPSTREAM-owned and this one is fork-added, so "keep the two in
sync" — which this docstring used to say — is an instruction the fork is not
allowed to follow: editing ``uninstall.py`` is a fork-boundary violation.
What the fork can do is route AROUND it. If the write side here ever grows a
key the upstream remover does not know about, the correct move is a fork-owned
removal path, not an edit to ``uninstall.py``.
"""

from __future__ import annotations

import os

try:  # pragma: no cover - platform gate
    import winreg  # type: ignore
except ImportError:  # not on Windows
    winreg = None  # type: ignore

try:
    from hermes_logging import log_warn  # type: ignore
except Exception:  # pragma: no cover - logging is best-effort here
    def log_warn(msg: str) -> None:  # type: ignore
        pass

_ENV_KEY = "Environment"


def is_windows() -> bool:
    return winreg is not None


def _normalize_segment(entry: str) -> str:
    """Casefold + strip trailing separators for PATH-segment comparison."""
    return os.path.normcase(entry.rstrip("\\/"))


def set_user_env(name: str, value: str) -> bool:
    """Write ``name=value`` into HKCU\\Environment (User scope).

    Preserves the existing registry value type (REG_EXPAND_SZ vs REG_SZ) when
    the value already exists so unexpanded ``%VARS%`` are not flattened.
    Returns True on success.
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _ENV_KEY, 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            value_type = winreg.REG_SZ
            try:
                _existing, value_type = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                value_type = winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, value_type, value)
        return True
    except OSError as e:
        log_warn(f"Could not set User env {name}: {e}")
        return False


def add_user_path_entry(entry: str) -> bool:
    """Prepend ``entry`` to the User-scope PATH if not already present.

    Uses segment-wise, case-insensitive, trailing-separator-tolerant matching
    (never substring ``-like`` matching, which mis-fires on prefix collisions).
    Preserves REG_EXPAND_SZ vs REG_SZ so a PATH containing ``%VARS%`` keeps its
    expandable type.  Returns True when the PATH now contains the entry (whether
    we added it or it was already there).
    """
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _ENV_KEY, 0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            try:
                current, value_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, value_type = "", winreg.REG_EXPAND_SZ
            segments = [s for s in current.split(";") if s]
            target = _normalize_segment(entry)
            if any(_normalize_segment(s) == target for s in segments):
                return True  # already on PATH
            new_value = ";".join([entry, *segments])
            winreg.SetValueEx(key, "Path", 0, value_type, new_value)
        return True
    except OSError as e:
        log_warn(f"Could not add {entry} to User PATH: {e}")
        return False


def broadcast_environment_change() -> None:
    """Broadcast WM_SETTINGCHANGE so newly-spawned shells pick up the change.

    Best-effort: swallows all failures (a hung top-level window must never
    stall postinstall — SMTO_ABORTIFHUNG + a short timeout guard against that).
    """
    if winreg is None:
        return
    try:  # pragma: no cover - ctypes/user32 not exercised in unit tests
        import ctypes
        from ctypes import wintypes

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002

        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
            wintypes.LPCWSTR, wintypes.UINT, wintypes.UINT,
            ctypes.POINTER(wintypes.DWORD),
        ]
        result = wintypes.DWORD()
        send(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 2000, ctypes.byref(result),
        )
    except Exception:
        pass
