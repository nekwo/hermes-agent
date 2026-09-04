"""Row 87 (mission-control-queue.md): no uninstall path warns that the code
checkout's git history is deleted and not backed up by this tool. The
printed warnings previously named only ``$HERMES_HOME`` (configs, API keys,
sessions) as at risk.
"""
from types import SimpleNamespace

from hermes_cli import uninstall


def test_dry_run_warns_git_history_not_backed_up(monkeypatch, tmp_path, capsys):
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()

    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: False)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda: [])

    uninstall._print_uninstall_dry_run(
        project_root=project_root, hermes_home=hermes_home, full_uninstall=False
    )

    output = capsys.readouterr().out
    assert "git history" in output.lower()
    assert "not backed up" in output.lower()


def test_interactive_preamble_warns_git_history_not_backed_up(
    monkeypatch, tmp_path, capsys
):
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    hermes_home.mkdir()

    monkeypatch.setattr(uninstall, "get_project_root", lambda: project_root)
    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: False)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda: [])

    # Cancel at the very first prompt (option select) so nothing destructive
    # runs; we only care that the banner already printed the warning.
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "3")

    uninstall.run_uninstall(SimpleNamespace(dry_run=False, yes=False, full=False))

    output = capsys.readouterr().out
    assert "git history" in output.lower()
    assert "not backed up" in output.lower()
