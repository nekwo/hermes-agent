"""The autostart launcher must name the interpreter Hermes is installed into.

The Windows gateway launcher is written once at install time and then outlives
every process that could notice it is wrong. ``get_python_path()`` ends in a
``sys.executable`` fallback — correct for ephemeral work in a dev checkout,
catastrophic once persisted: it stamps whichever interpreter happened to run
the install into an artifact nothing re-examines, and the gateway then boots
for months against a package set no update syncs.

That is not hypothetical. One install spent months pinned to a bare system
Python whose packages lived in a stray user-site directory, which produced two
incidents of the same class: a fatal ``concurrent_log_handler`` import death,
and a ``nemo_relay`` traceback that made a fully healthy boot look like a
crash.

Windows-only paths are exercised via ``sys.platform`` / ``_assert_windows``
patching so these run on any host (same approach as
test_update_gateway_launcher_refresh).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

import hermes_cli.doctor as doctor
import hermes_cli.gateway as gateway
import hermes_cli.gateway_windows as gateway_windows


def _make_venv(tmp_path: Path, name: str = "hermes-venv") -> Path:
    """A venv with an interpreter for BOTH layouts, so the pin is host-agnostic."""
    venv = tmp_path / name
    for bin_dir, exe in (("Scripts", "python.exe"), ("bin", "python")):
        d = venv / bin_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / exe).write_text("", encoding="utf-8")
    return venv


# ---------------------------------------------------------------------------
# resolve_managed_python: the accessor that refuses to guess
# ---------------------------------------------------------------------------


def test_resolve_managed_python_returns_the_venv_interpreter(tmp_path):
    venv = _make_venv(tmp_path)

    with mock.patch.object(gateway, "_detect_venv_dir", return_value=venv):
        assert gateway.resolve_managed_python() == str(gateway._venv_interpreter(venv))


def test_resolve_managed_python_refuses_to_guess_when_no_venv_is_found():
    """THE regression. get_python_path() answers sys.executable here, and that
    answer stamped into a launcher is how an install silently ends up running
    the gateway on an interpreter no update maintains."""
    with mock.patch.object(gateway, "_detect_venv_dir", return_value=None):
        with pytest.raises(gateway.ManagedPythonUnavailable) as excinfo:
            gateway.resolve_managed_python()

    reason = str(excinfo.value)
    assert "\n" not in reason, "the reason must stay a single readable line"
    assert "VIRTUAL_ENV" in reason and "sys.prefix" in reason


def test_resolve_managed_python_refuses_a_venv_with_no_interpreter(tmp_path):
    empty = tmp_path / "hollow"
    empty.mkdir()

    with mock.patch.object(gateway, "_detect_venv_dir", return_value=empty):
        with pytest.raises(gateway.ManagedPythonUnavailable) as excinfo:
            gateway.resolve_managed_python()

    assert str(empty) in str(excinfo.value)


def test_get_python_path_keeps_its_fallback_for_ephemeral_callers():
    """The fallback is not wrong everywhere — only in persisted artifacts. Its
    other callers must keep working from any interpreter."""
    with mock.patch.object(gateway, "_detect_venv_dir", return_value=None):
        assert gateway.get_python_path() == gateway.sys.executable


# ---------------------------------------------------------------------------
# Rendering: run the real body, then read the artifact back off disk
# ---------------------------------------------------------------------------


def _render(tmp_path, venv):
    """Drive the REAL _write_task_script end to end against a real venv."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    with mock.patch.object(gateway_windows.sys, "platform", "win32"), mock.patch.object(
        gateway_windows, "_assert_windows"
    ), mock.patch.object(gateway, "_detect_venv_dir", return_value=venv), mock.patch(
        "hermes_cli.config.get_hermes_home", return_value=str(home)
    ), mock.patch.object(gateway_windows, "_stable_gateway_working_dir", return_value=str(tmp_path)):
        return gateway_windows._write_task_script()


def test_rendered_launcher_names_the_managed_interpreter(tmp_path):
    """The rendering body, against realistic input, with the artifact read back
    off disk — not an assertion on a mocked accessor. Both launchers matter:
    the .vbs is what the Scheduled Task runs, the .cmd is what a manual run
    uses, and either one pinning the wrong interpreter is an outage."""
    venv = _make_venv(tmp_path)
    expected = str(gateway._venv_interpreter(venv))

    script_path = _render(tmp_path, venv)

    cmd_text = script_path.read_text(encoding="utf-8")
    assert gateway_windows.launcher_interpreter(cmd_text) == expected
    assert expected in script_path.with_suffix(".vbs").read_text(encoding="utf-8")


def test_write_task_script_refuses_rather_than_pinning_a_guess(tmp_path):
    """No managed interpreter means no launcher. Leaving the old one alone is
    recoverable; silently writing a guess is what rots for months."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    with mock.patch.object(gateway_windows.sys, "platform", "win32"), mock.patch.object(
        gateway_windows, "_assert_windows"
    ), mock.patch.object(gateway, "_detect_venv_dir", return_value=None), mock.patch(
        "hermes_cli.config.get_hermes_home", return_value=str(home)
    ), mock.patch.object(gateway_windows, "_stable_gateway_working_dir", return_value=str(tmp_path)):
        with pytest.raises(gateway.ManagedPythonUnavailable):
            gateway_windows._write_task_script()

    assert not list((home / "gateway-service").glob("*.cmd"))


def test_launcher_interpreter_reads_back_a_quoted_path():
    rendered = "\r\n".join(
        [
            "@echo off",
            'set "HERMES_HOME=C:\\hermes home"',
            '"C:\\Program Files\\hermes\\Scripts\\python.exe" -m hermes_cli.main --profile alice gateway run',
            "exit /b 0",
        ]
    )

    assert (
        gateway_windows.launcher_interpreter(rendered)
        == "C:\\Program Files\\hermes\\Scripts\\python.exe"
    )


def test_launcher_interpreter_is_none_without_an_invocation():
    assert gateway_windows.launcher_interpreter("@echo off\r\nexit /b 0\r\n") is None


# ---------------------------------------------------------------------------
# hermes doctor: surface the drift instead of waiting for a boot to fail
# ---------------------------------------------------------------------------


def _run_doctor_check(*, rendered: str | None, managed: str, issues: list[str], capsys):
    with mock.patch.object(gateway, "is_windows", return_value=True), mock.patch.object(
        gateway_windows, "installed_launcher_interpreter", return_value=rendered
    ), mock.patch.object(gateway, "resolve_managed_python", return_value=managed):
        doctor._check_gateway_launcher_interpreter(issues)
    return capsys.readouterr().out


def test_doctor_fails_when_the_launcher_interpreter_is_unmanaged(capsys):
    issues: list[str] = []

    out = _run_doctor_check(
        rendered=r"C:\Python312\python.exe",
        managed=r"C:\hermes\venv\Scripts\python.exe",
        issues=issues,
        capsys=capsys,
    )

    assert r"C:\Python312\python.exe" in out
    assert "hermes gateway install" in out
    assert issues and "not the Hermes interpreter" in issues[0]


def test_doctor_passes_when_the_launcher_matches(capsys):
    issues: list[str] = []
    managed = r"C:\hermes\venv\Scripts\python.exe"

    out = _run_doctor_check(rendered=managed, managed=managed, issues=issues, capsys=capsys)

    assert issues == []
    assert "unmanaged interpreter" not in out


def test_doctor_stays_quiet_when_no_launcher_is_installed(capsys):
    issues: list[str] = []

    out = _run_doctor_check(
        rendered=None, managed=r"C:\hermes\venv\Scripts\python.exe", issues=issues, capsys=capsys
    )

    assert issues == []
    assert out.strip() == ""
