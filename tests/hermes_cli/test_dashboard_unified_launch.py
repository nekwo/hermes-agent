"""Tests for the unified profile→machine dashboard launch routing.

`<profile> dashboard` routes to ONE machine-level dashboard instead of
spawning a per-profile server: attach (open browser at ?profile=) when one
is already listening, else re-exec as the machine dashboard with the
launching profile preselected. `--isolated` opts out.
"""
import sys
import types
import pytest


@pytest.fixture
def main_mod():
    import hermes_cli.main as main_mod
    return main_mod


def _capture_reexec(main_mod, monkeypatch):
    """Stub BOTH platform branches of the machine-dashboard re-exec.

    ``cmd_dashboard`` re-execs through ``os.execvpe`` on POSIX but through
    ``subprocess.Popen`` + ``sys.exit(proc.wait())`` on Windows (``execvpe``
    does not truly replace the process there and can crash with
    STATUS_ACCESS_VIOLATION under Python 3.14+ — see the comment at the
    call site in ``hermes_cli/main.py``).

    A test that stubs only ``os.execvpe`` is therefore vacuous on Windows:
    the win32 branch spawns a REAL ``python -m hermes_cli.main ... dashboard``
    child, which runs ``npm install`` + ``vite build`` and then serves
    forever, while the parent blocks in ``proc.wait()``. That hangs the whole
    pytest process (pytest-timeout's thread method then kills the run, so a
    single test takes the entire file's results with it).

    Returning one ``calls`` list for both branches lets the assertions below
    describe the same re-exec on either platform — the recorded tuple is
    always ``(executable, argv, env)``.
    """
    calls: list[tuple[str, list[str], dict]] = []

    def fake_exec(exe, argv, env):
        calls.append((exe, list(argv), env))
        raise SystemExit(0)  # execvpe never returns

    class _FakePopen:
        def __init__(self, argv, *_a, env=None, **_kw):
            calls.append((argv[0], list(argv), env if env is not None else {}))

        def wait(self):
            return 0

    monkeypatch.setattr(main_mod.os, "execvpe", fake_exec)
    monkeypatch.setattr(main_mod.subprocess, "Popen", _FakePopen)
    return calls


def _args(**kw):
    defaults = dict(
        status=False, stop=False, host="127.0.0.1", port=9119,
        no_open=True, insecure=False, skip_build=False,
        isolated=False, open_profile="",
    )
    defaults.update(kw)
    return types.SimpleNamespace(**defaults)


class TestUnifiedDashboardRouting:
    # Fork-retained: the attach path exercises the Windows Popen branch of the
    # re-exec through _capture_reexec, so it stays even though upstream pruned it.
    def test_profile_launch_attaches_to_running_dashboard(self, main_mod, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: True)
        execs = _capture_reexec(main_mod, monkeypatch)

        with pytest.raises(SystemExit) as exc:
            main_mod.cmd_dashboard(_args())
        assert exc.value.code == 0
        assert execs == []  # attached, never re-exec'd (on either platform)

    def test_profile_launch_reexecs_machine_dashboard(self, main_mod, monkeypatch):
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)

        with pytest.raises(SystemExit):
            main_mod.cmd_dashboard(_args())

        assert len(execs) == 1
        exe, argv, env = execs[0]
        assert exe == sys.executable
        # Pinned to the default profile + launching profile preselected.
        assert "-p" in argv and argv[argv.index("-p") + 1] == "default"
        assert "--open-profile" in argv
        assert argv[argv.index("--open-profile") + 1] == "worker_x"
        # The child is pinned to the machine ROOT, not the launching profile's
        # HERMES_HOME.  For a standard install (HERMES_HOME unset) that root is
        # the platform-native default (~/.hermes), NOT dropped — see the Docker
        # test below for why we resolve explicitly instead of popping.
        from hermes_constants import get_default_hermes_root
        assert env.get("HERMES_HOME") == str(get_default_hermes_root())

    # Fork-retained: same _capture_reexec reason as above; upstream pruned it.
    def test_reexec_pins_docker_machine_root(self, main_mod, monkeypatch):
        """In the Docker layout (HERMES_HOME=/opt/data, profiles under
        /opt/data/profiles/<name>) the reroute must pin the child to the
        machine root /opt/data — NOT drop HERMES_HOME.

        Dropping it makes the child fall back to $HOME/.hermes
        (= /opt/data/.hermes), an empty auto-seeded home, so the dashboard
        shows only the default profile and the .install_method stamp is
        missing (which also misfires the Docker update-button guard).
        Regression test for the support report.
        """
        monkeypatch.setenv("HERMES_HOME", "/opt/data/profiles/oracle")
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "oracle"
        )
        monkeypatch.setattr(main_mod, "_dashboard_listening", lambda host, port: False)
        execs = _capture_reexec(main_mod, monkeypatch)

        with pytest.raises(SystemExit):
            main_mod.cmd_dashboard(_args())

        assert len(execs) == 1
        _exe, _argv, env = execs[0]
        # get_default_hermes_root() strips the trailing profiles/<name>, so the
        # child binds /opt/data — where the real default/oracle/saga profiles
        # and the .install_method stamp actually live. Rendered through Path so
        # the assertion also holds on native Windows (where the resolver returns
        # the same location spelled with backslashes).
        from pathlib import Path

        assert env.get("HERMES_HOME") == str(Path("/opt/data"))

    def test_desktop_profile_backend_skips_machine_dashboard_reroute(self, main_mod, monkeypatch):
        """A desktop-spawned named-profile backend (HERMES_DESKTOP=1) must NOT
        reroute into the machine dashboard. The reroute re-execs as the default
        profile and exits, so the desktop never sees a ready backend → boot
        loop. The guard keeps desktop pool backends per-profile."""
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        listening_calls = []
        monkeypatch.setattr(
            main_mod, "_dashboard_listening",
            lambda host, port: listening_calls.append(1) or False,
        )
        execs = _capture_reexec(main_mod, monkeypatch)
        monkeypatch.setitem(sys.modules, "fastapi", None)

        with pytest.raises((SystemExit, AttributeError, ImportError, TypeError)):
            main_mod.cmd_dashboard(_args())
        assert listening_calls == []
        assert execs == []


    # Fork-retained: same _capture_reexec reason as above; upstream pruned it.
    def test_reexec_child_does_not_reroute(self, main_mod, monkeypatch):
        """The re-exec'd child carries --open-profile; the guard must treat
        that as 'already routed' and never re-exec again (no exec loop)."""
        monkeypatch.setattr(
            "hermes_cli.profiles.get_active_profile_name", lambda: "worker_x"
        )
        execs = _capture_reexec(main_mod, monkeypatch)
        monkeypatch.setitem(sys.modules, "fastapi", None)

        with pytest.raises((SystemExit, AttributeError, ImportError, TypeError)):
            main_mod.cmd_dashboard(_args(open_profile="worker_x"))
        assert execs == []


