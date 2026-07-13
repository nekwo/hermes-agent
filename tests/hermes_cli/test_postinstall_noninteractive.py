from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace


def _patch_common(monkeypatch, calls):
    """Patch the side-effecting bits of cmd_postinstall for a hermetic run."""
    import hermes_cli.config as config_mod
    import hermes_cli.dep_ensure as dep_ensure
    import hermes_cli.main as main_mod
    import hermes_cli.path_setup as path_setup

    monkeypatch.setattr(config_mod, "stamp_install_method", lambda _method: None)
    monkeypatch.setattr(
        dep_ensure,
        "ensure_dependency",
        lambda dep, interactive=True: calls.append((dep, interactive)) or True,
    )
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: False)
    # Shell provisioning + PATH registration must not touch the real machine.
    monkeypatch.setattr(
        dep_ensure, "ensure_git_bash",
        lambda interactive=True: r"C:\Program Files\Git\bin\bash.exe",
    )
    monkeypatch.setattr(
        path_setup, "register_hermes_command",
        lambda home: path_setup.PathSetupResult(
            shim_path=str(Path(home) / "bin" / "hermes.cmd"),
            path_dir=str(Path(home) / "bin"),
            path_registered=True,
            note=None,
        ),
    )
    return main_mod


def test_postinstall_yes_bootstraps_without_provider_setup(monkeypatch, capsys):
    calls: list[tuple[str, bool]] = []
    main_mod = _patch_common(monkeypatch, calls)

    setup_called = False

    def fake_setup(_args):
        nonlocal setup_called
        setup_called = True

    monkeypatch.setattr(main_mod, "cmd_setup", fake_setup)

    main_mod.cmd_postinstall(SimpleNamespace(yes=True, non_interactive=False))

    assert calls == [
        ("node", False),
        ("browser", False),
        ("ripgrep", False),
        ("ffmpeg", False),
    ]
    assert setup_called is False
    assert "Provider setup skipped" in capsys.readouterr().out


def test_postinstall_json_emits_summary_as_final_line(monkeypatch, capsys):
    calls: list[tuple[str, bool]] = []
    main_mod = _patch_common(monkeypatch, calls)

    main_mod.cmd_postinstall(
        SimpleNamespace(yes=True, non_interactive=False, json=True)
    )

    out_lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    summary = json.loads(out_lines[-1])
    assert summary["schema"] == "hermes.postinstall/1"
    assert summary["git_bash_path"] == r"C:\Program Files\Git\bin\bash.exe"
    assert summary["path_registered"] is True
    assert summary["shim_path"].endswith("hermes.cmd")
    assert isinstance(summary["deps"], dict)
