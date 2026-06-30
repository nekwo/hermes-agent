from __future__ import annotations

from types import SimpleNamespace


def test_postinstall_yes_bootstraps_without_provider_setup(monkeypatch, capsys):
    import hermes_cli.config as config_mod
    import hermes_cli.dep_ensure as dep_ensure
    import hermes_cli.main as main_mod

    calls: list[tuple[str, bool]] = []
    setup_called = False

    monkeypatch.setattr(config_mod, "stamp_install_method", lambda _method: None)
    monkeypatch.setattr(dep_ensure, "ensure_dependency", lambda dep, interactive=True: calls.append((dep, interactive)) or True)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: False)

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

