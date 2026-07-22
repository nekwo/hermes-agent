"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(cwd=None, skip_soul=False, context_length=None):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def _init_code_repo(path):
    """A git repo that actually holds code — the coding posture requires a source
    file (or manifest), not a bare ``.git`` (a prose/notes repo stays general)."""
    import subprocess

    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "main.py").write_text("print('hi')\n")


class TestCodingContextBlock:
    def test_injected_when_active(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        stable = _stable_prompt(agent)
        assert "coding agent" in stable
        assert "Workspace" in stable

    def test_absent_when_off(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        # Drive the real path: force the resolved mode to "off" via config.
        with patch("agent.coding_context._coding_mode", return_value="off"):
            stable = _stable_prompt(agent)
        assert "coding agent" not in stable

    def test_absent_without_tools(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=[], platform="cli")
        assert "coding agent" not in _stable_prompt(agent)


class TestT6bToolGuidance:
    """T6b: the tool schemas ship brief descriptions; the details-on-demand
    pointer and the policy moves ride the byte-stable, tool-gated system prompt.
    These extend the byte-stability coverage to the new lines (static, gated on
    the fixed-per-conversation tool set → no per-turn churn)."""

    def test_tool_describe_pointer_present_with_tools_absent_without(self):
        from agent.prompt_builder import TOOL_DESCRIBE_GUIDANCE

        with_tools = _stable_prompt(_make_agent(valid_tool_names=["read_file"]))
        assert TOOL_DESCRIBE_GUIDANCE in with_tools
        no_tools = _stable_prompt(_make_agent(valid_tool_names=[]))
        assert TOOL_DESCRIBE_GUIDANCE not in no_tools

    def test_shell_preference_gated_on_terminal(self):
        from agent.prompt_builder import SHELL_TOOL_PREFERENCE_GUIDANCE

        assert SHELL_TOOL_PREFERENCE_GUIDANCE in _stable_prompt(
            _make_agent(valid_tool_names=["terminal"])
        )
        assert SHELL_TOOL_PREFERENCE_GUIDANCE not in _stable_prompt(
            _make_agent(valid_tool_names=["clarify"])
        )

    def test_clarify_choices_gated_on_clarify(self):
        from agent.prompt_builder import CLARIFY_CHOICES_GUIDANCE

        assert CLARIFY_CHOICES_GUIDANCE in _stable_prompt(
            _make_agent(valid_tool_names=["clarify"])
        )
        assert CLARIFY_CHOICES_GUIDANCE not in _stable_prompt(
            _make_agent(valid_tool_names=["terminal"])
        )

    def test_browser_precondition_gated_on_browser(self):
        from agent.prompt_builder import BROWSER_PRECONDITION_GUIDANCE

        assert BROWSER_PRECONDITION_GUIDANCE in _stable_prompt(
            _make_agent(valid_tool_names=["browser_navigate"])
        )
        assert BROWSER_PRECONDITION_GUIDANCE not in _stable_prompt(
            _make_agent(valid_tool_names=["terminal"])
        )

    def test_skill_confirm_before_delete_gated_on_skill_manage(self):
        confirm = "Confirm with the user before creating or deleting a skill."
        assert confirm in _stable_prompt(_make_agent(valid_tool_names=["skill_manage"]))
        assert confirm not in _stable_prompt(_make_agent(valid_tool_names=["read_file"]))

    def test_guidance_is_byte_stable_across_builds(self):
        """Byte-stability guard (extends T5): the stable tier carrying the T6b
        lines is byte-identical across repeated builds — the additions are
        static and introduce no per-turn volatility."""
        tools = ["terminal", "clarify", "browser_navigate", "skill_manage", "read_file"]
        first = _stable_prompt(_make_agent(valid_tool_names=tools))
        second = _stable_prompt(_make_agent(valid_tool_names=tools))
        assert first == second
        # All four moves + the pointer are present in the one build.
        from agent.prompt_builder import (
            BROWSER_PRECONDITION_GUIDANCE,
            CLARIFY_CHOICES_GUIDANCE,
            SHELL_TOOL_PREFERENCE_GUIDANCE,
            TOOL_DESCRIBE_GUIDANCE,
        )
        for line in (
            TOOL_DESCRIBE_GUIDANCE,
            SHELL_TOOL_PREFERENCE_GUIDANCE,
            CLARIFY_CHOICES_GUIDANCE,
            BROWSER_PRECONDITION_GUIDANCE,
        ):
            assert line in first
