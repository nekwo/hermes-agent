"""Regression tests for terminal guardrails that prevent stuck gateway turns."""

import json

import tools.terminal_tool as terminal_tool


def test_interactive_hermes_tools_command_is_rejected_before_execution(monkeypatch):
    def _fail_start_cleanup_thread():
        raise AssertionError("interactive command should be rejected before environment setup")

    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", _fail_start_cleanup_thread)

    result = json.loads(
        terminal_tool.terminal_tool("hermes -p alice tools 2>&1 | head -220")
    )

    assert result["status"] == "error"
    assert result["exit_code"] == -1
    assert "interactive Hermes menu" in result["error"]


def test_interactive_hermes_setup_command_is_rejected_before_execution(monkeypatch):
    def _fail_start_cleanup_thread():
        raise AssertionError("interactive command should be rejected before environment setup")

    monkeypatch.setattr(terminal_tool, "_start_cleanup_thread", _fail_start_cleanup_thread)

    result = json.loads(terminal_tool.terminal_tool("X:/Eternia/.hermes/venvs/hermes-agent/Scripts/hermes.exe setup"))

    assert result["status"] == "error"
    assert "interactive Hermes menu" in result["error"]


def test_hermes_version_is_not_blocked(monkeypatch):
    # Unit-test the classifier rather than executing the real command.
    assert terminal_tool._interactive_cli_guidance("hermes --version") is None
