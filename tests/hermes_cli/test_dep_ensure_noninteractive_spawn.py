"""`ensure_dependency` must never hand a child an inherited stdin or a profile.

Regression test for a hang observed 2026-08-20. `hermes postinstall --yes
--json`, run as a non-interactive child by the Eternia Launcher, never
returned. `ensure_dependency` spawned `scripts/install.ps1` via
`powershell.exe` WITHOUT `-NoProfile` and without redirecting stdin; the
operator's PowerShell profile — which upgrades 5.1 sessions to pwsh 7 — ran
before `-File` did, replaced the script with an interactive shell, and that
shell blocked forever on a pipe the caller never closed. Because the profile
ended in `exit`, `install.ps1` had never actually run on that machine either.

The pre-existing `test_postinstall_noninteractive.py` cannot catch this: it
monkeypatches `ensure_dependency` itself, so the spawn is never built. These
tests drive the real function with a failing check and capture the argv.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hermes_cli import dep_ensure


@pytest.fixture
def captured_spawn(monkeypatch, tmp_path):
    """Force the install-script path and capture the subprocess.run call."""
    calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        calls.append({"cmd": list(cmd), **kwargs})
        return subprocess.CompletedProcess(cmd, 0)

    script = tmp_path / "install.ps1"
    script.write_text("# stub\n", encoding="utf-8")

    # The dependency is missing (so we reach the installer) and stays missing
    # (so the return value is False, not a second check's truth).
    monkeypatch.setitem(dep_ensure._DEP_CHECKS, "node", lambda: False)
    monkeypatch.setattr(dep_ensure, "subprocess", subprocess, raising=False)
    monkeypatch.setattr(dep_ensure.subprocess, "run", fake_run)
    return calls, script


def test_powershell_spawn_passes_noprofile_and_closes_stdin(
    monkeypatch, captured_spawn
):
    calls, script = captured_spawn
    monkeypatch.setattr(
        dep_ensure, "_find_install_script", lambda: (script, "powershell")
    )
    monkeypatch.setattr(dep_ensure.shutil, "which", lambda name: "powershell.exe")

    dep_ensure.ensure_dependency("node", interactive=False)

    assert calls, "the install script was never spawned"
    cmd = calls[0]["cmd"]

    # A user profile runs BEFORE -File. Without this the script is not what
    # executes, whatever the script contains.
    assert "-NoProfile" in cmd
    assert "-NonInteractive" in cmd
    # ...and the flags must precede -File, or PowerShell reads them as script
    # arguments instead of its own.
    assert cmd.index("-NoProfile") < cmd.index("-File")
    assert cmd.index("-NonInteractive") < cmd.index("-File")

    # An inherited pipe is what turned "prompted" into "hung forever". EOF is
    # an answer; a pipe that never closes is not.
    assert calls[0].get("stdin") is subprocess.DEVNULL


def test_shell_spawn_also_closes_stdin(monkeypatch, captured_spawn):
    """The bash branch has the same exposure through ~/.bashrc."""
    calls, script = captured_spawn
    monkeypatch.setattr(
        dep_ensure, "_find_install_script", lambda: (script, "bash")
    )

    dep_ensure.ensure_dependency("node", interactive=False)

    assert calls, "the install script was never spawned"
    assert calls[0]["cmd"][0] == "bash"
    assert calls[0].get("stdin") is subprocess.DEVNULL


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell argv is Windows-only")
def test_every_powershell_spawn_in_dep_ensure_is_profile_free(monkeypatch, captured_spawn):
    """No PowerShell invocation may leave this module without -NoProfile.

    Stated as a property rather than a single call site, because the defect was
    not "one wrong argument" — it was one spawn diverging from a convention the
    rest of the repo already followed.
    """
    calls, script = captured_spawn
    monkeypatch.setattr(
        dep_ensure, "_find_install_script", lambda: (script, "powershell")
    )
    monkeypatch.setattr(dep_ensure.shutil, "which", lambda name: "pwsh.exe")

    for dep in ("node", "browser", "ripgrep", "ffmpeg"):
        monkeypatch.setitem(dep_ensure._DEP_CHECKS, dep, lambda: False)
        dep_ensure.ensure_dependency(dep, interactive=False)

    assert len(calls) == 4
    for call in calls:
        assert "-NoProfile" in call["cmd"], call["cmd"]
        assert call.get("stdin") is subprocess.DEVNULL
