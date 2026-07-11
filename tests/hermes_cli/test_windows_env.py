"""Registry-safe User-env helpers — runs on any platform via a fake winreg."""

from unittest.mock import patch

import pytest

from hermes_cli import windows_env


# --- Fake winreg -----------------------------------------------------------
REG_SZ = 1
REG_EXPAND_SZ = 2
HKEY_CURRENT_USER = object()
KEY_READ = 1
KEY_WRITE = 2


class _FakeKey:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    """Minimal in-memory winreg emulation for the Environment key."""

    REG_SZ = REG_SZ
    REG_EXPAND_SZ = REG_EXPAND_SZ
    HKEY_CURRENT_USER = HKEY_CURRENT_USER
    KEY_READ = KEY_READ
    KEY_WRITE = KEY_WRITE

    def __init__(self, initial=None):
        # name -> (value, type)
        self.values = dict(initial or {})

    def OpenKey(self, root, path, reserved, access):
        return _FakeKey(self.values)

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name]

    def SetValueEx(self, key, name, reserved, value_type, value):
        key.store[name] = (value, value_type)


@pytest.fixture
def fake_reg():
    reg = FakeWinreg()
    with patch.object(windows_env, "winreg", reg):
        yield reg


def test_set_user_env_creates_reg_sz(fake_reg):
    assert windows_env.set_user_env("HERMES_GIT_BASH_PATH", r"C:\Git\bin\bash.exe")
    assert fake_reg.values["HERMES_GIT_BASH_PATH"] == (r"C:\Git\bin\bash.exe", REG_SZ)


def test_set_user_env_preserves_existing_type(fake_reg):
    fake_reg.values["HERMES_HOME"] = (r"%LOCALAPPDATA%\hermes", REG_EXPAND_SZ)
    assert windows_env.set_user_env("HERMES_HOME", r"C:\Users\x\.hermes")
    assert fake_reg.values["HERMES_HOME"][1] == REG_EXPAND_SZ


def test_add_user_path_prepends_when_absent(fake_reg):
    fake_reg.values["Path"] = (r"C:\Windows;C:\Windows\System32", REG_EXPAND_SZ)
    assert windows_env.add_user_path_entry(r"C:\Users\x\AppData\Local\hermes\bin")
    value, vtype = fake_reg.values["Path"]
    assert value.startswith(r"C:\Users\x\AppData\Local\hermes\bin;")
    assert vtype == REG_EXPAND_SZ  # type preserved, %VARS% not flattened


def test_add_user_path_is_idempotent_case_and_slash_insensitive(fake_reg):
    fake_reg.values["Path"] = (r"C:\Users\x\AppData\Local\hermes\bin\;C:\Windows", REG_SZ)
    assert windows_env.add_user_path_entry(r"c:\users\x\appdata\local\hermes\BIN")
    # No duplicate added — still the original two segments.
    assert fake_reg.values["Path"][0].count("hermes") == 1


def test_add_user_path_creates_when_missing(fake_reg):
    assert windows_env.add_user_path_entry(r"C:\hermes\bin")
    value, vtype = fake_reg.values["Path"]
    assert value == r"C:\hermes\bin"
    assert vtype == REG_EXPAND_SZ


def test_helpers_noop_without_winreg():
    with patch.object(windows_env, "winreg", None):
        assert windows_env.set_user_env("X", "y") is False
        assert windows_env.add_user_path_entry(r"C:\x") is False
        assert windows_env.is_windows() is False
        # broadcast must not raise
        windows_env.broadcast_environment_change()
