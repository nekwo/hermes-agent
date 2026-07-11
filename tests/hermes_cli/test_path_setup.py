"""hermes command shim + PATH registration."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import path_setup


class TestRenderShims:
    def test_windows_shim_bakes_conditional_home_and_abs_exe(self):
        out = path_setup.render_windows_shim(
            r"C:\Users\x\.hermes", r"C:\venv\Scripts\hermes.exe"
        )
        assert "if not defined HERMES_HOME" in out
        assert r"C:\Users\x\.hermes" in out
        assert '"C:\\venv\\Scripts\\hermes.exe" %*' in out

    def test_posix_shim_exports_conditional_home_and_execs(self):
        out = path_setup.render_posix_shim("/home/x/.hermes", "/venv/bin/hermes")
        assert out.startswith("#!/bin/sh\n")
        assert 'export HERMES_HOME="${HERMES_HOME:-/home/x/.hermes}"' in out
        assert 'exec "/venv/bin/hermes" "$@"' in out


class TestRegisterPosix:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX shim layout")
    def test_writes_local_bin_shim(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(path_setup.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(
            path_setup, "_resolve_hermes_exe", lambda: "/venv/bin/hermes"
        )
        # ~/.local/bin not on PATH → path_registered False + guidance note.
        monkeypatch.setenv("PATH", "/usr/bin")

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        shim = home / ".local" / "bin" / "hermes"
        assert Path(result.shim_path) == shim
        assert shim.is_file()
        assert os.access(shim, os.X_OK)
        assert result.path_registered is False
        assert ".local/bin" in (result.note or "") or ".local\\bin" in (result.note or "")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX shim layout")
    def test_path_registered_true_when_on_path(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".local" / "bin").mkdir(parents=True)
        monkeypatch.setattr(path_setup.Path, "home", staticmethod(lambda: home))
        monkeypatch.setattr(
            path_setup, "_resolve_hermes_exe", lambda: "/venv/bin/hermes"
        )
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(home / ".local" / "bin")]))

        result = path_setup.register_hermes_command(tmp_path / ".hermes")
        assert result.path_registered is True
        assert result.note is None


class TestRegisterWindows:
    @pytest.mark.skipif(os.name != "nt", reason="Windows shim layout")
    def test_writes_localappdata_shim_and_registers_path(self, tmp_path, monkeypatch):
        local_appdata = tmp_path / "AppData" / "Local"
        local_appdata.mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
        monkeypatch.setattr(
            path_setup, "_resolve_hermes_exe",
            lambda: str(tmp_path / "venv" / "Scripts" / "hermes.exe"),
        )
        # Stub the registry helpers so no real PATH mutation happens.
        from hermes_cli import windows_env
        monkeypatch.setattr(windows_env, "add_user_path_entry", lambda entry: True)
        monkeypatch.setattr(windows_env, "broadcast_environment_change", lambda: None)

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        shim = local_appdata / "hermes" / "bin" / "hermes.cmd"
        assert Path(result.shim_path) == shim
        assert shim.is_file()
        assert result.path_registered is True
