from agent_runtime.models import AgentPersona
from agent_runtime.personas import (
    AgentRole,
    blocked_tool_names,
    effective_toolsets,
)
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility


def _persona(pid):
    return next(persona for persona in sample_personas() if persona.id == pid)


def _explicit_pm():
    return AgentPersona(
        id="pm",
        display_name="PM",
        role=AgentRole.PM.value,
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "terminal", "todo"],
        system_prompt_path="personas/pm/system.md",
    )


def _tool_names(toolsets, blocked):
    from model_tools import get_tool_definitions

    return {
        tool["function"]["name"]
        for tool in get_tool_definitions(
            enabled_toolsets=toolsets,
            blocked_tool_names=list(blocked),
            quiet_mode=True,
        )
    }


def test_pm_actual_tool_schema_follows_its_configured_toolsets():
    pm = _explicit_pm()

    names = _tool_names(effective_toolsets(pm), blocked_tool_names(pm))

    assert "terminal" in names
    assert "write_file" in names
    assert "patch" in names


def test_qa_actual_tool_schema_follows_explicit_test_data():
    qa = _persona("qa")

    names = _tool_names(effective_toolsets(qa), blocked_tool_names(qa))

    assert "write_file" in names
    assert "patch" in names
    assert "terminal" in names


def test_sample_personas_keep_persona_blocklists():
    for persona in sample_personas():
        assert "delegate_task" in blocked_tool_names(persona)
        assert "memory" in blocked_tool_names(persona)
    assert "write_file" not in blocked_tool_names(_persona("qa"))
    assert "patch" not in blocked_tool_names(_persona("qa"))
    assert "send_message" in blocked_tool_names(_persona("dev"))


def test_unbounded_permission_mode_exposes_available_unbounded_tools(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task_from_outer_worker")

    required_model_tools = {
        "clarify",
        "delegate_task",
        "memory",
        "terminal",
        "write_file",
        "patch",
    }

    for persona in sample_personas():
        visibility = resolve_tool_visibility(
            persona,
            ToolVisibilityOptions(permission_mode="unbounded", permission_source="test"),
        )
        assert visibility["blocked_tool_names"] == []
        assert required_model_tools.issubset(set(visibility["final_model_tools"]))
