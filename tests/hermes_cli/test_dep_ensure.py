from unittest.mock import patch




def test_find_install_script_from_checkout(tmp_path):
    """_find_install_script finds scripts/install.sh in a git checkout."""
    from hermes_cli.dep_ensure import _find_install_script
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "install.sh").write_text("#!/bin/bash", encoding="utf-8")
    with patch("hermes_cli.dep_ensure._IS_WINDOWS", False):
        path, shell = _find_install_script(package_dir=tmp_path / "hermes_cli", repo_root=tmp_path)
    assert path is not None
    assert path.name == "install.sh"
    assert shell == "bash"








def test_ensure_dependency_uses_powershell_on_windows(tmp_path):
    from hermes_cli.dep_ensure import ensure_dependency
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "install.ps1").write_text("# fake")
    with patch("hermes_cli.dep_ensure._IS_WINDOWS", True), \
         patch("hermes_cli.dep_ensure._DEP_CHECKS", {"node": lambda: False}), \
         patch("hermes_cli.dep_ensure._find_install_script", return_value=(scripts_dir / "install.ps1", "powershell")), \
         patch("hermes_cli.dep_ensure.shutil") as mock_shutil, \
         patch("hermes_constants.get_hermes_home", return_value=tmp_path / "fakehome"), \
         patch("subprocess.run") as mock_run, \
         patch("sys.stdin") as mock_stdin:
        mock_shutil.which.side_effect = lambda name: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" if name == "powershell" else None
        mock_stdin.isatty.return_value = False
        mock_run.return_value = type("R", (), {"returncode": 0})()
        ensure_dependency("node", interactive=False)
        cmd = mock_run.call_args[0][0]
        assert "powershell" in cmd[0].lower()
        assert "-Ensure" in cmd
        assert cmd[cmd.index("-Ensure") + 1] == "node"
        assert "-HermesHome" in cmd
        assert str(tmp_path / "fakehome") in cmd


# --- ensure_git_bash -------------------------------------------------------

def test_ensure_git_bash_posix_is_noop_returning_bash():
    from hermes_cli.dep_ensure import ensure_git_bash
    with patch("hermes_cli.dep_ensure._IS_WINDOWS", False), \
         patch("hermes_cli.dep_ensure.shutil") as mock_shutil:
        mock_shutil.which.return_value = "/usr/bin/bash"
        assert ensure_git_bash(interactive=False) == "/usr/bin/bash"


def test_ensure_git_bash_windows_resolves_and_persists():
    from hermes_cli.dep_ensure import ensure_git_bash
    bash = r"C:\Program Files\Git\bin\bash.exe"
    persisted = {}
    with patch("hermes_cli.dep_ensure._IS_WINDOWS", True), \
         patch("tools.environments.local._find_windows_git_bash", return_value=bash), \
         patch("hermes_cli.windows_env.set_user_env",
               side_effect=lambda name, value: persisted.setdefault(name, value) or True), \
         patch("hermes_cli.windows_env.broadcast_environment_change") as mock_bcast:
        result = ensure_git_bash(interactive=False)
    assert result == bash
    assert persisted["HERMES_GIT_BASH_PATH"] == bash
    assert mock_bcast.called


def test_ensure_git_bash_windows_falls_back_to_install_then_reresolves():
    from hermes_cli import dep_ensure
    bash = r"C:\Users\x\AppData\Local\hermes\git\bin\bash.exe"
    resolves = iter([None, bash])  # first miss, then present after install
    install_calls = []
    with patch("hermes_cli.dep_ensure._IS_WINDOWS", True), \
         patch("tools.environments.local._find_windows_git_bash",
               side_effect=lambda: next(resolves)), \
         patch.object(dep_ensure, "ensure_dependency",
                      side_effect=lambda dep, interactive=True: install_calls.append(dep) or True), \
         patch("hermes_cli.windows_env.set_user_env", return_value=True), \
         patch("hermes_cli.windows_env.broadcast_environment_change"):
        result = dep_ensure.ensure_git_bash(interactive=False)
    assert result == bash
    assert install_calls == ["git"]


def test_ensure_git_bash_windows_returns_none_when_unprovisionable():
    from hermes_cli import dep_ensure
    with patch("hermes_cli.dep_ensure._IS_WINDOWS", True), \
         patch("tools.environments.local._find_windows_git_bash", return_value=None), \
         patch.object(dep_ensure, "ensure_dependency", return_value=False), \
         patch("hermes_cli.windows_env.set_user_env") as mock_set:
        result = dep_ensure.ensure_git_bash(interactive=False)
    assert result is None
    assert not mock_set.called  # nothing persisted on failure
