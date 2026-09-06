"""Windows Git Bash resolution — the WSL-stub-avoidance contract.

The bug these tests lock down: with Git Bash unprovisioned, ``_find_bash`` used
to fall through to ``shutil.which("bash")`` which resolves to
``C:\\Windows\\System32\\bash.exe`` — the WSL launcher — so every agent terminal
call invoked ``wsl`` and failed on a normal Windows host.

All tests run on POSIX CI: they build real fake directory trees under
``tmp_path`` and point the Windows env vars at them, mocking only
``shutil.which`` for the ``git`` / ``bash`` lookups.
"""

import os
from unittest.mock import patch

import pytest

from tools.environments import local
from tools.environments.local import (
    _WINDOWS_PATH_SEP,
    _augment_windows_system_path,
    _bash_from_git,
    _find_bash,
    _find_windows_git_bash,
    _is_windows_system_shim,
    _windows_system_path_dirs,
)


@pytest.fixture
def clean_win_env(tmp_path):
    """A sandboxed Windows-ish environment with no Git Bash present anywhere."""
    env = {
        "SystemRoot": str(tmp_path / "Windows"),
        "LOCALAPPDATA": str(tmp_path / "AppData" / "Local"),
        "ProgramFiles": str(tmp_path / "Program Files"),
        "ProgramFiles(x86)": str(tmp_path / "Program Files (x86)"),
    }
    for v in env.values():
        os.makedirs(v, exist_ok=True)
    # Drop any real HERMES_GIT_BASH_PATH the CI box might carry.
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("HERMES_GIT_BASH_PATH", None)
        yield tmp_path


def _make(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("")
    return path


class TestIsWindowsSystemShim:
    def test_system32_bash_is_stub(self):
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            assert _is_windows_system_shim(r"C:\Windows\System32\bash.exe")
            assert _is_windows_system_shim("C:/Windows/System32/bash.exe")
            assert _is_windows_system_shim(r"c:\windows\system32\bash.exe")
            assert _is_windows_system_shim(r"C:\Windows\SysWOW64\bash.exe")

    def test_real_git_bash_is_not_stub(self):
        with patch.dict(os.environ, {"SystemRoot": r"C:\Windows"}):
            assert not _is_windows_system_shim(r"C:\Program Files\Git\bin\bash.exe")
            assert not _is_windows_system_shim("")


class TestFindWindowsGitBash:
    def test_wsl_stub_only_returns_none(self, clean_win_env):
        """Only System32\\bash.exe on PATH → resolver returns None (stub rejected)."""
        stub = _make(str(clean_win_env / "Windows" / "System32" / "bash.exe"))

        def which(name):
            return {"bash": stub, "git": None}.get(name)

        with patch.object(local.shutil, "which", side_effect=which):
            assert _find_windows_git_bash() is None

    def test_find_bash_raises_on_wsl_stub_only(self, clean_win_env):
        stub = _make(str(clean_win_env / "Windows" / "System32" / "bash.exe"))

        def which(name):
            return {"bash": stub, "git": None}.get(name)

        with patch.object(local, "_IS_WINDOWS", True), \
                patch.object(local.shutil, "which", side_effect=which):
            with pytest.raises(RuntimeError, match="Git Bash not found"):
                _find_bash()

    @pytest.mark.parametrize(
        "git_rel,bash_rel",
        [
            (("Git", "cmd", "git.exe"), ("Git", "bin", "bash.exe")),
            (("Git", "bin", "git.exe"), ("Git", "usr", "bin", "bash.exe")),
            (("Git", "mingw64", "bin", "git.exe"), ("Git", "bin", "bash.exe")),
        ],
    )
    def test_derives_bash_from_git(self, clean_win_env, git_rel, bash_rel):
        root = clean_win_env / "Program Files"
        git = _make(str(root.joinpath(*git_rel)))
        bash = _make(str(root.joinpath(*bash_rel)))

        def which(name):
            return {"git": git, "bash": None}.get(name)

        with patch.object(local.shutil, "which", side_effect=which):
            assert _find_windows_git_bash() == bash
            assert _bash_from_git() == bash

    def test_hermes_git_bash_path_takes_precedence(self, clean_win_env):
        override = _make(str(clean_win_env / "custom" / "bash.exe"))
        # Also plant a git-derived bash that must be ignored in favour of the override.
        _make(str(clean_win_env / "Program Files" / "Git" / "cmd" / "git.exe"))
        _make(str(clean_win_env / "Program Files" / "Git" / "bin" / "bash.exe"))
        with patch.dict(os.environ, {"HERMES_GIT_BASH_PATH": override}):
            with patch.object(local.shutil, "which", return_value=None):
                assert _find_windows_git_bash() == override

    def test_portable_git_precedes_git_derived(self, clean_win_env):
        portable = _make(
            str(clean_win_env / "AppData" / "Local" / "hermes" / "git" / "bin" / "bash.exe")
        )
        git = _make(str(clean_win_env / "Program Files" / "Git" / "cmd" / "git.exe"))
        _make(str(clean_win_env / "Program Files" / "Git" / "bin" / "bash.exe"))

        def which(name):
            return {"git": git, "bash": None}.get(name)

        with patch.object(local.shutil, "which", side_effect=which):
            assert _find_windows_git_bash() == portable

    def test_standard_location_fallback(self, clean_win_env):
        """No git on PATH, no portable — standard Program Files install is found."""
        bash = _make(str(clean_win_env / "Program Files" / "Git" / "bin" / "bash.exe"))
        with patch.object(local.shutil, "which", return_value=None):
            assert _find_windows_git_bash() == bash


class TestWindowsSystemPathAugmentation:
    """The agent must be able to reach powershell.exe / cmd.exe / pwsh from its
    bash terminal even under a minimal gateway PATH.

    These drive the WINDOWS branch of ``_augment_windows_system_path`` (which
    is a documented no-op off Windows), so the fixture pins ``_IS_WINDOWS``.
    They split on ``_WINDOWS_PATH_SEP`` and never on ``os.pathsep``: on POSIX
    CI ``os.pathsep`` is ``:``, which cuts ``C:\\some\\proj\\bin`` in half at
    the drive letter and reduces the first assertion to ``'C'``.
    """

    @pytest.fixture
    def fake_windows(self, tmp_path, monkeypatch):
        win = tmp_path / "Windows"
        system32 = win / "System32"
        psdir = system32 / "WindowsPowerShell" / "v1.0"
        pwsh7 = tmp_path / "Program Files" / "PowerShell" / "7"
        for d in (system32, psdir, pwsh7, system32 / "Wbem", system32 / "OpenSSH"):
            d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(local, "_IS_WINDOWS", True)
        monkeypatch.setenv("SystemRoot", str(win))
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        return {"system32": str(system32), "psdir": str(psdir), "pwsh7": str(pwsh7)}

    def test_appends_missing_system_dirs(self, fake_windows):
        result = _augment_windows_system_path(r"C:\some\proj\bin")
        entries = result.split(_WINDOWS_PATH_SEP)
        # Caller entry preserved and still first (precedence untouched).
        assert entries[0] == r"C:\some\proj\bin"
        # PowerShell 5.1, cmd (System32), and pwsh 7 all reachable now.
        assert fake_windows["system32"] in entries
        assert fake_windows["psdir"] in entries
        assert fake_windows["pwsh7"] in entries

    def test_does_not_duplicate_present_dirs(self, fake_windows):
        existing = _WINDOWS_PATH_SEP.join([fake_windows["system32"], r"C:\proj"])
        result = _augment_windows_system_path(existing)
        # System32 already present (case/if slash-variant) is not re-appended.
        occurrences = [
            e for e in result.split(_WINDOWS_PATH_SEP)
            if os.path.normcase(e.rstrip("\\/")) == os.path.normcase(fake_windows["system32"])
        ]
        assert len(occurrences) == 1

    def test_only_existing_dirs_are_added(self, tmp_path, monkeypatch):
        # SystemRoot points at a dir with no System32 → nothing bogus appended.
        monkeypatch.setattr(local, "_IS_WINDOWS", True)
        monkeypatch.setenv("SystemRoot", str(tmp_path / "empty"))
        monkeypatch.setenv("ProgramFiles", str(tmp_path / "none"))
        monkeypatch.delenv("ProgramW6432", raising=False)
        monkeypatch.delenv("ProgramFiles(x86)", raising=False)
        assert _windows_system_path_dirs() == []
        assert _augment_windows_system_path(r"C:\proj") == r"C:\proj"

    def test_off_windows_it_is_a_no_op(self, fake_windows, monkeypatch):
        """The guard stated on its own: the caller PATH comes back byte-identical
        even though every fake system dir exists and would otherwise be added."""
        monkeypatch.setattr(local, "_IS_WINDOWS", False)
        assert _augment_windows_system_path(r"C:\some\proj\bin") == r"C:\some\proj\bin"
