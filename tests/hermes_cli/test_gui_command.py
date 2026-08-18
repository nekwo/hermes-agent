"""Tests for ``hermes gui`` desktop launcher wiring."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import main as cli_main


def _ns(**kw):
    defaults = dict(
        skip_build=False,
        build_only=False,
        force_build=False,
        source=False,
        fake_boot=False,
        ignore_existing=False,
        hermes_root=None,
        cwd=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _make_desktop_tree(tmp_path: Path) -> Path:
    root = tmp_path / "hermes-agent"
    desktop_dir = root / "apps" / "desktop"
    desktop_dir.mkdir(parents=True)
    (desktop_dir / "package.json").write_text("{}", encoding="utf-8")
    return root


def _make_packaged_executable(root: Path, monkeypatch, platform: str = "darwin") -> Path:
    monkeypatch.setattr(cli_main.sys, "platform", platform)
    desktop_dir = root / "apps" / "desktop"
    if platform == "darwin":
        exe = desktop_dir / "release" / "mac-arm64" / "Hermes.app" / "Contents" / "MacOS" / "Hermes"
    elif platform == "win32":
        exe = desktop_dir / "release" / "win-unpacked" / "Hermes.exe"
    else:
        exe = desktop_dir / "release" / "linux-unpacked" / "hermes"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    return exe


def test_gui_installs_packages_and_launches_desktop_app(tmp_path, monkeypatch):
    root = _make_desktop_tree(tmp_path)
    desktop_dir = root / "apps" / "desktop"
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    packaged_exe = _make_packaged_executable(root, monkeypatch)

    install_ok = subprocess.CompletedProcess(["npm", "ci"], 0)
    pack_ok = subprocess.CompletedProcess(["npm", "run", "pack"], 0)
    launch_ok = subprocess.CompletedProcess([str(packaged_exe)], 0)

    with patch("hermes_cli.main.shutil.which", return_value="/usr/bin/npm"), \
         patch("hermes_cli.main._run_npm_install_deterministic", return_value=install_ok) as mock_install, \
         patch("hermes_cli.main._desktop_build_needed", return_value=True), \
         patch("hermes_cli.main._write_desktop_build_stamp"), \
         patch("hermes_cli.main._desktop_macos_relaunchable_fixup"), \
         patch("hermes_cli.main.subprocess.run", side_effect=[pack_ok, launch_ok]) as mock_run, \
         pytest.raises(SystemExit) as exc:
        cli_main.cmd_gui(_ns())

    assert exc.value.code == 0
    # The install now runs with a resolved env (managed-Node PATH), never a bare
    # ``env=None`` that would leave npm's child scripts unable to find ``node``.
    mock_install.assert_called_once()
    assert mock_install.call_args.args == ("/usr/bin/npm", root)
    assert mock_install.call_args.kwargs["capture_output"] is False
    install_env = mock_install.call_args.kwargs["env"]
    assert install_env is not None and "PATH" in install_env
    assert mock_run.call_args_list[0].args[0] == ["/usr/bin/npm", "run", "pack"]
    assert mock_run.call_args_list[0].kwargs["cwd"] == desktop_dir
    assert mock_run.call_args_list[1].args[0] == [str(packaged_exe)]
    assert mock_run.call_args_list[1].kwargs["cwd"] == desktop_dir


def test_gui_install_env_prepends_managed_node_on_bare_path(tmp_path, monkeypatch):
    """Regression: npm's child scripts (electron-winstaller's select-7z-arch.js)
    shell out to bare ``node``. When Desktop is launched from the updater chain
    the parent PATH is stripped, so the install env MUST carry the Hermes-managed
    Node ahead of that bare PATH or the install dies with ``node: not found``.
    """
    import os

    from hermes_constants import iter_hermes_node_dirs

    root = _make_desktop_tree(tmp_path)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    _make_packaged_executable(root, monkeypatch, platform="win32")

    # A managed Node tree on disk so with_hermes_node_path() actually prepends it.
    home = tmp_path / "hermes-home"
    (home / "node" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Simulate the stripped PATH the desktop updater chain hands us.
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))

    install_ok = subprocess.CompletedProcess(["npm", "ci"], 0)
    launch_ok = subprocess.CompletedProcess(["hermes"], 0)

    with patch("hermes_cli.main._resolve_node_runtime_npm", return_value="/usr/bin/npm"), \
         patch("hermes_cli.main._run_npm_install_deterministic", return_value=install_ok) as mock_install, \
         patch("hermes_cli.main._desktop_build_needed", return_value=True), \
         patch("hermes_cli.main._write_desktop_build_stamp"), \
         patch("hermes_cli.main.subprocess.run", side_effect=[subprocess.CompletedProcess([], 0), launch_ok]), \
         pytest.raises(SystemExit):
        cli_main.cmd_gui(_ns(skip_build=False))

    managed_dirs = [str(p) for p in iter_hermes_node_dirs() if p.is_dir()]
    assert managed_dirs, "managed node tree not discovered"
    install_env = mock_install.call_args.kwargs["env"]
    path_parts = install_env["PATH"].split(os.pathsep)
    assert path_parts[: len(managed_dirs)] == managed_dirs
    assert "/usr/bin" in path_parts  # the bare updater PATH is preserved, just after managed Node








# ── Content-hash stamp tests ──────────────────────────────────────────








# ── Electron build-cache recovery tests ───────────────────────────────


def _write_zip(path: Path) -> None:
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("electron", "fake binary payload")


def test_purge_electron_build_cache_clears_all_zips_and_unpacked_dir(tmp_path, monkeypatch):
    """Purge is unconditional: it removes every electron-*.zip (regardless of
    whether stdlib zipfile thinks it's corrupt) plus the half-written unpacked
    dir, because @electron/get's own SHASUM check on re-download is the real
    validator — not a self-rolled one."""
    cache = tmp_path / "electron-cache"
    # A "clean" zip and a prepended-junk zip — the latter is the real-world
    # corruption that zipfile.testzip() silently passes (it reads from the
    # end-of-central-directory backward), which is why we don't gate on it.
    clean = cache / "electron-v40.9.3-linux-x64.zip"
    prepended = cache / "hashdir" / "electron-v40.9.3-linux-x64.zip"
    _write_zip(clean)
    _write_zip(prepended)
    prepended.write_bytes(b"\x00" * 4096 + prepended.read_bytes())

    desktop_dir = tmp_path / "apps" / "desktop"
    unpacked = desktop_dir / "release" / "linux-unpacked"
    unpacked.mkdir(parents=True)
    (unpacked / "LICENSE.electron.txt").write_text("x", encoding="utf-8")
    (unpacked / "resources.pak").write_text("x", encoding="utf-8")

    monkeypatch.setattr(cli_main, "_electron_download_cache_dirs", lambda: [cache])

    removed = cli_main._purge_electron_build_cache(desktop_dir)

    assert clean in removed
    assert prepended in removed
    assert unpacked in removed
    assert not clean.exists()
    assert not prepended.exists()
    assert not unpacked.exists()




def test_gui_does_not_retry_after_packaged_executable_exists(tmp_path, monkeypatch, capsys):
    """A build that already produced a packaged executable did NOT fail from the
    Electron-download problem the cache purge + mirror retries exist to repair.

    Regression for #40187: a late failure such as macOS code signing leaves
    Hermes.app/Contents/MacOS/Hermes in place. Re-downloading Electron can't
    repair a signing failure, so the destructive purge + slow mirror retry must
    be skipped — we fail directly instead of grinding through an identical retry.
    """
    root = _make_desktop_tree(tmp_path)
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    # Executable EXISTS at failure time → late failure, not a corrupt download.
    _make_packaged_executable(root, monkeypatch, platform="darwin")
    monkeypatch.delenv("ELECTRON_MIRROR", raising=False)

    install_ok = subprocess.CompletedProcess(["npm", "ci"], 0)
    pack_fail = subprocess.CompletedProcess(["npm", "run", "pack"], 1)

    with patch("hermes_cli.main.shutil.which", return_value="/usr/bin/npm"), \
         patch("hermes_cli.main._run_npm_install_deterministic", return_value=install_ok), \
         patch("hermes_cli.main._desktop_macos_relaunchable_fixup"), \
         patch("hermes_cli.main._purge_electron_build_cache", return_value=[Path("/c/electron.zip")]) as mock_purge, \
         patch("hermes_cli.main._redownload_electron_dist", return_value=True) as mock_dl, \
         patch("hermes_cli.main.subprocess.run", return_value=pack_fail) as mock_run, \
         pytest.raises(SystemExit) as exc:
        cli_main.cmd_gui(_ns())

    assert exc.value.code == 1
    # Neither destructive recovery runs, and there is exactly ONE pack attempt.
    mock_purge.assert_not_called()
    mock_dl.assert_not_called()
    assert mock_run.call_count == 1
    assert "Desktop GUI build failed" in capsys.readouterr().out




# ── electronDist (re)download helper tests (#47266) ───────────────────


@pytest.mark.parametrize(
    "platform,rel",
    [
        ("linux", "dist/electron"),
        ("win32", "dist/electron.exe"),
        ("darwin", "dist/Electron.app/Contents/MacOS/Electron"),
    ],
)
def test_electron_dist_ok_per_platform(tmp_path, monkeypatch, platform, rel):
    monkeypatch.setattr(cli_main.sys, "platform", platform)
    electron = tmp_path / "node_modules" / "electron"
    # A dist dir that exists but lacks the binary is NOT ok (partial extraction).
    (electron / "dist").mkdir(parents=True)
    assert cli_main._electron_dist_ok(tmp_path) is False

    binp = electron / rel
    binp.parent.mkdir(parents=True, exist_ok=True)
    binp.write_text("", encoding="utf-8")
    assert cli_main._electron_dist_ok(tmp_path) is True










class _FakeProc:
    """Minimal psutil.Process stand-in for the lock-breaker tests.

    ``still_locked_after_terminate`` makes ``wait`` raise the way psutil's does
    when a terminated process outlives the timeout, which is the only way the
    kill escalation is reachable at all.
    """

    def __init__(self, pid: int, exe: str | None, *, still_locked_after_terminate=False):
        self.pid = pid
        self.info = {"pid": pid, "exe": exe}
        self.terminated = False
        self.killed = False
        self.waits: list[float] = []
        self._still_locked = still_locked_after_terminate

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self._still_locked:
            raise TimeoutError("still holding the lock")
        return 0


class _RefusingProcessIter:
    """Stands in for ``psutil.process_iter`` and refuses to enumerate.

    Counting alone would let a mutant walk the live table and still pass on a
    machine that happens to be running nothing from this build's release tree;
    raising makes the bypass fatal wherever it happens. Same instrument as
    ``tests/hermes_cli/test_profiles.py``'s ``_RealEnumeratorRecorder``.
    """

    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError(
            "the desktop build-lock sweep reached the live process table"
        )


@pytest.fixture(autouse=True)
def _no_live_process_iter(monkeypatch):
    """No test in THIS FILE may enumerate the machine's processes.

    ``cmd_gui`` -> ``_stop_desktop_processes_locking_build`` used to call
    ``psutil.process_iter`` once per run of this file (ledger B20(vi)). The
    directory conftest already defaults the seam to an empty table; this pins
    the stronger claim at the layer underneath it — nothing here reaches psutil
    at all, whichever way it tries.
    """
    psutil = pytest.importorskip("psutil")
    recorder = _RefusingProcessIter()
    monkeypatch.setattr(psutil, "process_iter", recorder)
    yield recorder
    assert recorder.calls == 0


def _driven_table(rows, *, self_pid=424242):
    from hermes_cli import profiles

    return profiles._ProcessTable(
        self_pid=self_pid,
        ancestor_pids=frozenset(),
        current_username=None,
        processes=tuple(rows),
    )


def _row(proc: _FakeProc):
    from hermes_cli import profiles

    return profiles._ProcessFacts(
        pid=proc.pid, exe=proc.info["exe"], inspector_handle=proc
    )


class _DrivenDesktopLister:
    def __init__(self, table):
        self._table = table
        self.reads = 0

    def read(self):
        self.reads += 1
        return self._table


@pytest.mark.skipif(sys.platform != "win32", reason="the sweep is win32-only")
class TestDesktopBuildLockSweepSeam:
    """The sweep reads the injected table, never the machine's."""

    def test_a_locking_process_is_terminated_and_reported(self, tmp_path, monkeypatch):
        desktop = tmp_path / "apps" / "desktop"
        release = desktop / "release" / "win-unpacked"
        release.mkdir(parents=True)
        locker = _FakeProc(4242, str(release / "Hermes.exe"))
        # Two rows the filter must reject, for the two different reasons:
        # an exe outside the release tree, and this very process.
        outsider = _FakeProc(4343, str(tmp_path / "elsewhere" / "Hermes.exe"))
        myself = _FakeProc(424242, str(release / "Hermes.exe"))

        lister = _DrivenDesktopLister(
            _driven_table([_row(locker), _row(outsider), _row(myself)])
        )
        monkeypatch.setattr(cli_main, "_DESKTOP_PROCESS_LISTER", lister)

        assert cli_main._stop_desktop_processes_locking_build(desktop) == [4242]
        assert lister.reads == 1
        assert locker.terminated is True
        assert outsider.terminated is False
        assert myself.terminated is False

    def test_a_process_that_keeps_the_lock_is_killed(self, tmp_path, monkeypatch):
        desktop = tmp_path / "apps" / "desktop"
        release = desktop / "release" / "win-unpacked"
        release.mkdir(parents=True)
        stubborn = _FakeProc(
            5151, str(release / "Hermes.exe"), still_locked_after_terminate=True
        )

        monkeypatch.setattr(
            cli_main,
            "_DESKTOP_PROCESS_LISTER",
            _DrivenDesktopLister(_driven_table([_row(stubborn)])),
        )

        assert cli_main._stop_desktop_processes_locking_build(desktop) == [5151]
        assert stubborn.terminated is True
        assert stubborn.killed is True
        assert len(stubborn.waits) == 1

    def test_no_inspector_stops_nothing(self, tmp_path, monkeypatch):
        desktop = tmp_path / "apps" / "desktop"
        (desktop / "release").mkdir(parents=True)

        class _NoInspector:
            def read(self):
                return None

        monkeypatch.setattr(cli_main, "_DESKTOP_PROCESS_LISTER", _NoInspector())
        assert cli_main._stop_desktop_processes_locking_build(desktop) == []












# --- macOS TCC-stable local signing (relaunch fixup) -----------------------


def _write_info_plist(bundle: Path, identifier: str) -> None:
    import plistlib

    info = bundle / "Contents" / "Info.plist"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_bytes(plistlib.dumps({"CFBundleIdentifier": identifier}))


def _make_signable_app(desktop_dir: Path) -> Path:
    """Build a fake packaged Hermes.app with the pieces the signer must find."""
    ent_dir = desktop_dir / "electron"
    ent_dir.mkdir(parents=True, exist_ok=True)
    (ent_dir / "entitlements.mac.plist").write_text("<plist/>", encoding="utf-8")
    (ent_dir / "entitlements.mac.inherit.plist").write_text("<plist/>", encoding="utf-8")

    app = desktop_dir / "release" / "mac-arm64" / "Hermes.app"
    _write_info_plist(app, "com.nousresearch.hermes")
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "MacOS" / "Hermes").write_text("", encoding="utf-8")

    helper = app / "Contents" / "Frameworks" / "Hermes Helper.app"
    _write_info_plist(helper, "com.nousresearch.hermes.helper")

    native_dir = app / "Contents" / "Resources" / "app.asar.unpacked" / "node_modules" / "pty"
    native_dir.mkdir(parents=True)
    (native_dir / "pty.node").write_text("", encoding="utf-8")
    (app / "Contents" / "Frameworks" / "chrome_crashpad_handler").write_text("", encoding="utf-8")
    return app


def _collect_codesign_calls(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(
        cli_main.shutil, "which", lambda name: "/usr/bin/codesign" if name == "codesign" else None
    )
    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)
    return calls


def test_desktop_macos_local_codesign_signs_native_binaries(tmp_path, monkeypatch):
    """The standalone Mach-O pass must actually find files inside the bundle.

    Regression: an absolute-path parts check always matches the outer
    Hermes.app component, silently skipping every .node/.dylib/crashpad
    binary — codesign then rejects the outer signature (nested code unsigned).
    """
    desktop_dir = tmp_path / "apps" / "desktop"
    app = _make_signable_app(desktop_dir)
    calls = _collect_codesign_calls(monkeypatch)

    assert cli_main._desktop_macos_local_codesign(app, desktop_dir=desktop_dir) is True

    signed = [c[-1] for c in calls if c[:3] == ["/usr/bin/codesign", "--force", "--sign"]]
    assert str(app / "Contents" / "Resources" / "app.asar.unpacked" / "node_modules" / "pty" / "pty.node") in signed
    assert str(app / "Contents" / "Frameworks" / "chrome_crashpad_handler") in signed




def test_relaunchable_fixup_falls_back_to_legacy_adhoc_on_failure(tmp_path, monkeypatch, capsys):
    """A failing stable sign must still leave a launchable (deep ad-hoc) bundle."""
    root = _make_desktop_tree(tmp_path)
    desktop_dir = root / "apps" / "desktop"
    monkeypatch.setattr(cli_main, "PROJECT_ROOT", root)
    monkeypatch.delenv("CSC_LINK", raising=False)
    monkeypatch.delenv("APPLE_SIGNING_IDENTITY", raising=False)
    exe = _make_packaged_executable(root, monkeypatch, platform="darwin")
    app = exe.parents[2]

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(
        cli_main.shutil, "which", lambda name: "/usr/bin/codesign" if name == "codesign" else None
    )
    monkeypatch.setattr(cli_main.subprocess, "run", fake_run)
    monkeypatch.setattr(cli_main, "_desktop_macos_has_valid_real_signature", lambda a: False)
    monkeypatch.setattr(cli_main, "_desktop_macos_local_signing_identity", lambda: None)

    def boom(*a, **kw):
        raise subprocess.CalledProcessError(1, ["codesign"])

    monkeypatch.setattr(cli_main, "_desktop_macos_local_codesign", boom)

    assert cli_main._desktop_macos_relaunchable_fixup(desktop_dir) is False
    assert ["xattr", "-cr", str(app)] in calls
    assert ["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app)] in calls


# --- desktop.* launch options (config.yaml) -------------------------------


