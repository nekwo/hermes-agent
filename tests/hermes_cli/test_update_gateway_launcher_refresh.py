"""Legacy pythonw launcher normalization + post-update launcher refresh.

Covers the two halves of the "legacy pythonw gateways survive updates
forever" gap:

1. ``gateway_windows._resolve_detached_python`` — normalizes a legacy
   ``pythonw.exe`` interpreter (pre-aa2ae36c3f launchers / argv snapshots)
   to the sibling console ``python.exe`` so respawns and regenerated
   launchers use the hidden-console design (#54220/#56747) and don't die
   with ``RuntimeError: sys.stderr is None`` (#71671).
2. ``hermes_cli.main._refresh_windows_gateway_launchers`` — ``hermes
   update`` regenerates the installed Scheduled Task / Startup launcher
   scripts instead of leaving install-time artifacts stale forever.

Windows-specific paths are exercised via ``_is_windows`` patching so they
run on any host (same approach as test_update_venv_health).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.main as cli_main


# ---------------------------------------------------------------------------
# _resolve_detached_python: legacy pythonw normalization
# ---------------------------------------------------------------------------


def _make_venv(tmp_path: Path, *, with_console_python: bool) -> tuple[Path, Path]:
    scripts = tmp_path / "venv" / "Scripts"
    scripts.mkdir(parents=True)
    pythonw = scripts / "pythonw.exe"
    pythonw.write_text("", encoding="utf-8")
    python = scripts / "python.exe"
    if with_console_python:
        python.write_text("", encoding="utf-8")
    return pythonw, python


def test_resolve_detached_python_swaps_legacy_pythonw_for_console_sibling(tmp_path):
    pythonw, python = _make_venv(tmp_path, with_console_python=True)

    exe, venv_dir, extra = gateway_windows._resolve_detached_python(str(pythonw))

    assert exe == str(python)
    assert venv_dir == tmp_path / "venv"
    assert extra == []




def test_restart_spec_normalizes_legacy_pythonw_argv(tmp_path):
    """A pre-rework Scheduled Task argv snapshot (leading pythonw.exe) must be
    respawned through the console python + hidden-console launch, with every
    argument after the interpreter preserved verbatim."""
    pythonw, python = _make_venv(tmp_path, with_console_python=True)

    # Pre-import so the function's lazy imports resolve from sys.modules
    # instead of re-importing under the win32 platform patch (see the
    # TestWindowlessGatewayRestartSpec comment in
    # tests/tools/test_windows_native_support.py).
    import hermes_cli.config  # noqa: F401
    import hermes_cli.gateway  # noqa: F401

    argv = [str(pythonw), "-m", "hermes_cli.main", "gateway", "run"]
    with mock.patch.object(gateway_windows.sys, "platform", "win32"), mock.patch.object(
        gateway_windows, "_stable_gateway_working_dir", return_value=str(tmp_path)
    ), mock.patch("hermes_cli.config.get_hermes_home", return_value=str(tmp_path)):
        new_argv, cwd, env = gateway_windows.windowless_gateway_restart_spec(list(argv))

    assert new_argv[0] == str(python)
    assert new_argv[1:] == argv[1:]
    assert cwd == str(tmp_path)
    assert env["VIRTUAL_ENV"] == str(tmp_path / "venv")


# ---------------------------------------------------------------------------
# _refresh_windows_gateway_launchers: hermes update regenerates launchers
# ---------------------------------------------------------------------------


_SCRIPT = Path(r"C:\hermes\gateway-service\Hermes_Gateway_alice.cmd")


def test_task_action_classifier_separates_legacy_cmd_from_console_less_vbs():
    """Rewriting the launcher FILES only retargets a task whose action already
    names the ``.vbs``. A pre-#45610 action names the ``.cmd`` and keeps a
    visible console window no matter how often the files are regenerated, so
    the two must not collapse into one answer — and an action naming neither
    must stay unknown rather than being guessed either way."""
    legacy = r"Task To Run: C:\hermes\gateway-service\Hermes_Gateway_alice.cmd"
    modern = r'Task To Run: wscript.exe //B //Nologo "C:\hermes\gateway-service\Hermes_Gateway_alice.vbs"'

    assert gateway_windows._task_action_is_console_less(legacy, _SCRIPT) is False
    assert gateway_windows._task_action_is_console_less(modern, _SCRIPT) is True
    assert gateway_windows._task_action_is_console_less("Task To Run: other.exe", _SCRIPT) is None


def test_task_action_classifier_reads_localized_output():
    """Windows translates the field LABELS but never the path inside them, so a
    localized ``schtasks`` dump must still classify."""
    localized = r"Aufgabe wird ausgeführt: C:\hermes\gateway-service\Hermes_Gateway_alice.cmd"

    assert gateway_windows._task_action_is_console_less(localized, _SCRIPT) is False


def _run_refresh(*, console_less: bool | None, capsys):
    with mock.patch.object(cli_main, "_is_windows", return_value=True), mock.patch.object(
        gateway_windows, "is_installed", return_value=True
    ), mock.patch.object(gateway_windows, "_write_task_script", return_value=_SCRIPT), mock.patch.object(
        gateway_windows, "task_action_is_console_less", return_value=console_less
    ), mock.patch.object(gateway_windows, "get_task_name", return_value="Hermes_Gateway_alice"):
        cli_main._refresh_windows_gateway_launchers()
    return capsys.readouterr().out


def test_refresh_reports_a_task_that_still_launches_a_visible_console(capsys):
    """The update path cannot re-register the action (that needs elevation), so
    the one thing it must not do is stay silent: a ``.cmd`` task means the live
    gateway dies whenever its console window closes, with no restart until the
    next login."""
    out = _run_refresh(console_less=False, capsys=capsys)

    assert "Hermes_Gateway_alice" in out
    assert "VISIBLE console window" in out
    assert "hermes gateway install" in out


def test_refresh_stays_quiet_for_a_console_less_task(capsys):
    """A modern registration is already correct — no scary warning on it."""
    out = _run_refresh(console_less=True, capsys=capsys)

    assert "VISIBLE console window" not in out


def test_refresh_stays_quiet_when_the_action_cannot_be_read(capsys):
    """Unknown must not be reported as broken."""
    out = _run_refresh(console_less=None, capsys=capsys)

    assert "VISIBLE console window" not in out


def _run_status(*, console_less: bool | None, capsys):
    with mock.patch.object(gateway_windows, "_assert_windows"), mock.patch.object(
        gateway_windows, "get_task_name", return_value="Hermes_Gateway_alice"
    ), mock.patch.object(gateway_windows, "is_task_registered", return_value=True), mock.patch.object(
        gateway_windows, "is_startup_entry_installed", return_value=False
    ), mock.patch.object(gateway_windows, "_gateway_pids", return_value=[]), mock.patch.object(
        gateway_windows, "query_task_status", return_value={"last run result": "-1073741510"}
    ), mock.patch.object(gateway_windows, "task_action_is_console_less", return_value=console_less):
        gateway_windows.status()
    return capsys.readouterr().out


def test_status_explains_a_dead_gateway_left_by_a_visible_console_task(capsys):
    """``hermes gateway status`` is what an operator reads when the gateway is
    missing. On a legacy ``.cmd`` registration it used to print a bare
    ``Last Run Result: -1073741510`` — the numeric form of
    STATUS_CONTROL_C_EXIT — and leave the cause unnamed."""
    out = _run_status(console_less=False, capsys=capsys)

    assert "VISIBLE console window" in out
    assert "hermes gateway install" in out


def test_status_stays_quiet_for_a_console_less_task(capsys):
    out = _run_status(console_less=True, capsys=capsys)

    assert "VISIBLE console window" not in out






