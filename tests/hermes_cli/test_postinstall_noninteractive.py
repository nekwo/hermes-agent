from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import hermes_cli.path_setup as _path_setup

# The REAL install-dir seam, captured at import time — ``tests/conftest.py``'s
# autouse ``_isolate_hermes_shim_dir`` replaces the module attribute for every
# test, and the sandbox test below has to know which path it is PROTECTING.
_REAL_SHIM_INSTALL_DIR = _path_setup._shim_install_dir


def _patch_common(monkeypatch, calls, *, stub_shim: bool = True):
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
    if stub_shim:
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
    # The refusal code rides in the summary beside the prose note, so the
    # installer can branch on it. `None` when a shim was actually written.
    assert summary["error"] is None
    assert summary["shim_path"].endswith("hermes.cmd")
    assert isinstance(summary["deps"], dict)


def _snapshot(path: Path) -> object:
    """Existence + mtime + size, so a rewrite of an existing file is visible."""
    if not path.exists():
        return None
    st = path.stat()
    return (st.st_mtime_ns, st.st_size)


def test_postinstall_writes_its_shim_inside_the_sandbox_and_nowhere_else(
    tmp_path, monkeypatch, capsys, _isolate_hermes_shim_dir
):
    """The real `register_hermes_command`, run under the suite's own fixtures.

    `_hermetic_environment` redirects `HERMES_HOME` and deliberately NOT
    `HOME`, so this call used to write a genuine shim into the developer's real
    `~/.local/bin` (`%LOCALAPPDATA%/hermes/bin` on Windows) with the TEST's
    temp `HERMES_HOME` baked in as its default state root — a durable file on
    the operator's PATH pointing at a directory this test deletes on teardown.
    That is how an operator's Mac ended up with a `hermes` defaulting
    `HERMES_HOME` to a macOS temp path from an E2E run.

    Two assertions, and the second is the one that matters: the shim went
    INSIDE this test's tmp_path, and the real install dir was not touched.
    """
    calls: list[tuple[str, bool]] = []
    main_mod = _patch_common(monkeypatch, calls, stub_shim=False)

    # A resolvable target, so the run reaches the write instead of refusing.
    console_script = tmp_path / "venv" / (
        "Scripts" if os.name == "nt" else "bin"
    ) / ("hermes.exe" if os.name == "nt" else "hermes")
    console_script.parent.mkdir(parents=True)
    console_script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        _path_setup, "_resolve_hermes_exe", lambda: str(console_script)
    )
    if os.name == "nt":
        from hermes_cli import windows_env
        monkeypatch.setattr(windows_env, "add_user_path_entry", lambda entry: True)
        monkeypatch.setattr(windows_env, "broadcast_environment_change", lambda: None)

    real_dir = _REAL_SHIM_INSTALL_DIR()
    real_shim = Path(real_dir) / _path_setup._shim_file_name() if real_dir else None
    before = _snapshot(real_shim) if real_shim else None
    # The listing too, so a shim written under some OTHER name in the real
    # install dir is caught as well as a rewrite of the one we know about.
    listing_before = (
        sorted(p.name for p in Path(real_dir).iterdir())
        if real_dir and Path(real_dir).is_dir()
        else None
    )

    main_mod.cmd_postinstall(SimpleNamespace(yes=True, non_interactive=False))

    shim = Path(_isolate_hermes_shim_dir) / _path_setup._shim_file_name()

    assert shim.is_file(), "postinstall wrote no shim at all"
    assert shim.is_relative_to(tmp_path), f"{shim} escaped the test sandbox"
    # The temp home is baked in — correct for this run, and it never leaves the
    # sandbox precisely because the file holding it did not either.
    assert os.environ["HERMES_HOME"] in shim.read_text(encoding="utf-8")
    if real_shim is not None:
        assert _snapshot(real_shim) == before, f"{real_shim} was written by a test"
    if listing_before is not None:
        after = sorted(p.name for p in Path(real_dir).iterdir())
        assert after == listing_before, f"{real_dir} gained {set(after) - set(listing_before)}"


def test_postinstall_says_out_loud_when_it_refuses_to_write_the_shim(
    monkeypatch, capsys
):
    """A refusal an operator cannot see is a shim that silently is not there.

    The whole `PathSetupResult` used to be read only inside the `--json`
    branch, so a human running `hermes postinstall` was told nothing at all.
    """
    calls: list[tuple[str, bool]] = []
    main_mod = _patch_common(monkeypatch, calls)
    monkeypatch.setattr(
        _path_setup, "register_hermes_command",
        lambda home: _path_setup.PathSetupResult(
            error="shim_target_is_shim", note="Refusing to write /x/bin/hermes: ..."
        ),
    )

    main_mod.cmd_postinstall(SimpleNamespace(yes=True, non_interactive=False))

    out = capsys.readouterr().out
    assert "shim_target_is_shim" in out
    assert "Refusing to write /x/bin/hermes" in out
