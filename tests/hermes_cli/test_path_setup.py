"""hermes command shim + PATH registration."""

import os
from pathlib import Path

import pytest

from hermes_cli import path_setup

# The REAL seam, captured at import time. ``tests/conftest.py``'s autouse
# ``_isolate_hermes_shim_dir`` replaces the module attribute for every test in
# the tree, so a test that wants to assert the PRODUCTION derivation has to
# hold the original function rather than look it up through the module.
# Captured at collection, which happens before any fixture runs.
_REAL_SHIM_INSTALL_DIR = path_setup._shim_install_dir


def _exe_name() -> str:
    return "hermes.exe" if os.name == "nt" else "hermes"


def _make_exe(directory: Path, name: "str | None" = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    exe = directory / (name or _exe_name())
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


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


class TestShimInstallDir:
    """The write target, asserted without writing anything.

    Splitting the derivation out of ``register_hermes_command`` is what lets
    the suite pin ONE seam instead of redirecting ``HOME`` (which
    ``_hermetic_environment`` rules out), so the derivation needs its own
    assertions here, where no file is created.

    Both platform arms are driven by ``_IS_WINDOWS`` rather than skipped off
    the running OS: the subject is a two-line derivation, and the arm that is
    skipped is the arm that rots.
    """

    def test_posix_is_home_local_bin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(path_setup, "_IS_WINDOWS", False)
        monkeypatch.setenv("HOME", str(tmp_path))

        assert _REAL_SHIM_INSTALL_DIR() == str(tmp_path / ".local" / "bin")

    def test_posix_reads_home_from_the_env_not_the_password_database(
        self, tmp_path, monkeypatch
    ):
        """``$HOME`` is the sole authority, so a spawner can actually redirect it.

        ``Path.home()`` consults ``HOME`` first and the passwd entry after, so a
        subprocess that cleared its environment still resolved a real home. That
        is how a `hermes postinstall` spawned as a CHILD — by a test, or by the
        launcher's installer — wrote a genuine shim into an operator's
        `~/.local/bin` with the run's throwaway `HERMES_HOME` baked in as the
        default state root, measured on an operator's Mac.
        """
        monkeypatch.setattr(path_setup, "_IS_WINDOWS", False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            path_setup.Path, "home", staticmethod(lambda: Path("/not/this/one"))
        )

        assert _REAL_SHIM_INSTALL_DIR() == str(tmp_path / ".local" / "bin")

    def test_posix_without_home_resolves_to_nothing(self, monkeypatch):
        """The POSIX twin of the LOCALAPPDATA refusal below.

        No home stated is not "use whichever home this box's passwd file names".
        It is a directory this run does not have, and the shim is a durable
        artifact on somebody's PATH — so the answer is to skip with a receipt,
        which is what the Windows arm has always done.
        """
        monkeypatch.setattr(path_setup, "_IS_WINDOWS", False)
        monkeypatch.delenv("HOME", raising=False)

        assert _REAL_SHIM_INSTALL_DIR() is None

    def test_windows_is_localappdata_hermes_bin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(path_setup, "_IS_WINDOWS", True)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert _REAL_SHIM_INSTALL_DIR() == str(tmp_path / "hermes" / "bin")

    def test_windows_without_localappdata_resolves_to_nothing(self, monkeypatch):
        monkeypatch.setattr(path_setup, "_IS_WINDOWS", True)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        assert _REAL_SHIM_INSTALL_DIR() is None

    @pytest.mark.parametrize(
        ("is_windows", "variable", "expected"),
        [(False, "HOME", "HOME"), (True, "LOCALAPPDATA", "LOCALAPPDATA")],
    )
    def test_the_skip_names_the_variable_that_would_have_answered(
        self, monkeypatch, tmp_path, is_windows, variable, expected
    ):
        """A skipped shim is only actionable if it says what to set.

        The note was hard-coded to ``LOCALAPPDATA``, which on POSIX named a
        variable that platform does not have — and the POSIX arm could not
        reach it at all before this fence existed.
        """
        monkeypatch.setattr(path_setup, "_IS_WINDOWS", is_windows)
        monkeypatch.delenv(variable, raising=False)
        # The autouse `_isolate_hermes_shim_dir` fixture replaces this seam for
        # every test in the tree; this one is ABOUT the seam, so it hands the
        # production derivation back.
        monkeypatch.setattr(path_setup, "_shim_install_dir", _REAL_SHIM_INSTALL_DIR)
        monkeypatch.setattr(path_setup, "_resolve_hermes_exe", lambda: str(tmp_path / "hermes"))

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        assert result.error == "shim_dir_unresolved"
        assert result.shim_path is None
        assert expected in result.note


class TestResolveHermesExe:
    """Which `hermes` the shim is pointed AT — the self-exec defect's first half."""

    def test_the_venv_console_script_beats_whatever_path_answers(
        self, tmp_path, monkeypatch
    ):
        """`sys.executable`'s dir leads, and PATH comes last.

        A PATH-first order returns the SHIM on every run after the first: the
        shim is installed into a directory this module then puts on PATH, so
        `shutil.which("hermes")` finds it and the next postinstall writes a
        file whose exec target is its own path. Five stuck `/bin/sh` processes
        on an operator's Mac, the oldest 34 days old.
        """
        venv_bin = tmp_path / "venv" / ("Scripts" if os.name == "nt" else "bin")
        venv_exe = _make_exe(venv_bin)
        path_dir = tmp_path / "path-bin"
        _make_exe(path_dir)
        monkeypatch.setattr(path_setup.sys, "executable", str(venv_bin / "python"))
        monkeypatch.setattr(path_setup.sys, "argv", ["hermes"])
        monkeypatch.setenv("PATH", str(path_dir))
        # Off the repo root, so the PATH rung is genuinely reachable: Windows'
        # `shutil.which` searches the cwd FIRST, and this repo's root holds a
        # `hermes` script that would answer before PATH ever does.
        monkeypatch.chdir(tmp_path)

        assert path_setup._resolve_hermes_exe() == str(venv_exe)

    def test_path_still_answers_when_the_interpreter_dir_holds_no_console_script(
        self, tmp_path, monkeypatch
    ):
        """The fallback rung is intact: a system-python install still resolves."""
        interpreter_dir = tmp_path / "usr-bin"
        interpreter_dir.mkdir()
        path_dir = tmp_path / "path-bin"
        path_exe = _make_exe(path_dir)
        monkeypatch.setattr(
            path_setup.sys, "executable", str(interpreter_dir / "python3")
        )
        monkeypatch.setattr(path_setup.sys, "argv", ["hermes"])
        monkeypatch.setenv("PATH", str(path_dir))
        # Off the repo root: `shutil.which` searches the CURRENT directory
        # first on Windows, and this repo's root holds a `hermes` script.
        monkeypatch.chdir(tmp_path)

        assert path_setup._resolve_hermes_exe() == str(path_exe)

    def test_a_relative_hit_from_the_current_directory_is_not_a_target(
        self, tmp_path, monkeypatch
    ):
        """`shutil.which` answers relatively when it hits a relative rung.

        Windows searches the CURRENT directory before PATH, and a PATH entry
        of `.` does the same thing on either platform. Baking the answer into
        the shim makes the shim resolve against whatever directory the
        operator happens to be standing in — a different broken command every
        time. An absolute rung or no rung: here, no rung.
        """
        cwd = tmp_path / "cwd"
        _make_exe(cwd)
        # No interpreter rung: the dir does not exist, so `.` would otherwise
        # be the only candidate left and would become the answer.
        monkeypatch.setattr(
            path_setup.sys, "executable", str(tmp_path / "absent" / "python3")
        )
        monkeypatch.setattr(path_setup.sys, "argv", ["hermes"])
        monkeypatch.setenv("PATH", os.curdir)
        monkeypatch.chdir(cwd)

        resolved = path_setup._resolve_hermes_exe()

        assert resolved is None, f"a relative rung became the shim target: {resolved}"


class TestRegisterPosix:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX shim layout")
    def test_writes_the_shim_into_the_install_dir(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "home" / ".local" / "bin"
        monkeypatch.setattr(path_setup, "_shim_install_dir", lambda: str(bin_dir))
        monkeypatch.setattr(
            path_setup, "_resolve_hermes_exe", lambda: "/venv/bin/hermes"
        )
        # The install dir is not on PATH → path_registered False + guidance.
        monkeypatch.setenv("PATH", "/usr/bin")

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        shim = bin_dir / "hermes"
        assert Path(result.shim_path) == shim
        assert shim.is_file()
        assert os.access(shim, os.X_OK)
        assert result.error is None
        assert result.path_registered is False
        assert str(bin_dir) in (result.note or "")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX shim layout")
    def test_path_registered_true_when_on_path(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "home" / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        monkeypatch.setattr(path_setup, "_shim_install_dir", lambda: str(bin_dir))
        monkeypatch.setattr(
            path_setup, "_resolve_hermes_exe", lambda: "/venv/bin/hermes"
        )
        monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", str(bin_dir)]))

        result = path_setup.register_hermes_command(tmp_path / ".hermes")
        assert result.path_registered is True
        assert result.note is None
        assert result.error is None


class TestRegisterWindows:
    @pytest.mark.skipif(os.name != "nt", reason="Windows shim layout")
    def test_writes_the_shim_and_registers_path(self, tmp_path, monkeypatch):
        bin_dir = tmp_path / "AppData" / "Local" / "hermes" / "bin"
        monkeypatch.setattr(path_setup, "_shim_install_dir", lambda: str(bin_dir))
        monkeypatch.setattr(
            path_setup, "_resolve_hermes_exe",
            lambda: str(tmp_path / "venv" / "Scripts" / "hermes.exe"),
        )
        # Stub the registry helpers so no real PATH mutation happens.
        from hermes_cli import windows_env
        monkeypatch.setattr(windows_env, "add_user_path_entry", lambda entry: True)
        monkeypatch.setattr(windows_env, "broadcast_environment_change", lambda: None)

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        shim = bin_dir / "hermes.cmd"
        assert Path(result.shim_path) == shim
        assert shim.is_file()
        assert result.path_registered is True
        assert result.error is None


class TestRefusesToWriteAShimThatExecsItself:
    """The second half of the self-exec defect: a typed, loud, no-write refusal.

    Reachable without any resolution bug at all — a `pip install --user` puts
    the genuine console-script at `~/.local/bin/hermes`, which IS the POSIX
    shim path. Nothing stands behind the shim there, so the only honest answer
    is to refuse and say why.
    """

    def test_the_shim_is_not_written_and_the_refusal_is_typed(
        self, tmp_path, monkeypatch
    ):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / path_setup._shim_file_name()
        monkeypatch.setattr(path_setup, "_shim_install_dir", lambda: str(bin_dir))
        monkeypatch.setattr(path_setup, "_resolve_hermes_exe", lambda: str(shim))

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        assert not shim.exists()
        assert result.shim_path is None
        assert result.error == "shim_target_is_shim"
        assert str(shim) in (result.note or "")

    def test_an_existing_shim_is_left_exactly_as_it_was(self, tmp_path, monkeypatch):
        """No truncate-then-refuse: the operator's working file is untouched."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / path_setup._shim_file_name()
        shim.write_text("the operator's own wrapper\n", encoding="utf-8")
        before = shim.read_bytes()
        monkeypatch.setattr(path_setup, "_shim_install_dir", lambda: str(bin_dir))
        monkeypatch.setattr(path_setup, "_resolve_hermes_exe", lambda: str(shim))

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        assert shim.read_bytes() == before
        assert result.error == "shim_target_is_shim"

    def test_the_whole_resolution_chain_refuses_rather_than_looping(
        self, tmp_path, monkeypatch
    ):
        """End to end, with no stub on `_resolve_hermes_exe`.

        The operator's machine: the only `hermes` anywhere is the shim, and it
        is first on PATH. The pre-fix code resolved it through `PATH`, wrote
        `exec "<shim>"` into `<shim>`, and produced a file that execs itself.
        """
        bin_dir = tmp_path / "bin"
        shim = _make_exe(bin_dir, path_setup._shim_file_name())
        interpreter_dir = tmp_path / "usr-bin"
        interpreter_dir.mkdir()
        monkeypatch.setattr(path_setup, "_shim_install_dir", lambda: str(bin_dir))
        monkeypatch.setattr(
            path_setup.sys, "executable", str(interpreter_dir / "python3")
        )
        monkeypatch.setattr(path_setup.sys, "argv", ["hermes"])
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.chdir(interpreter_dir)  # off this repo's own `hermes`

        result = path_setup.register_hermes_command(tmp_path / ".hermes")

        assert result.error == "shim_target_is_shim"
        assert "exec" not in shim.read_text(encoding="utf-8")
