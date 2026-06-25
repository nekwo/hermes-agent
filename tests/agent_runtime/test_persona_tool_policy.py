from agent_runtime.models import AgentPersona
from agent_runtime.personas import (
    AgentRole,
    blocked_tool_names,
    default_personas,
    effective_toolsets,
)
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility


def _persona(pid):
    return next(persona for persona in default_personas() if persona.id == pid)


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


def test_pm_actual_tool_schema_excludes_write_patch_terminal():
    pm = _explicit_pm()

    names = _tool_names(effective_toolsets(pm), blocked_tool_names(pm))

    assert "terminal" not in names
    assert "write_file" not in names
    assert "patch" not in names


def test_qa_actual_tool_schema_is_bounded_by_default_profile_policy():
    qa = _persona("qa")

    names = _tool_names(effective_toolsets(qa), blocked_tool_names(qa))

    assert "write_file" not in names
    assert "patch" not in names
    assert "terminal" in names


def test_default_personas_keep_role_and_persona_blocklists():
    for persona in default_personas():
        assert "delegate_task" in blocked_tool_names(persona)
        assert "memory" in blocked_tool_names(persona)
    assert "write_file" in blocked_tool_names(_persona("qa"))
    assert "patch" in blocked_tool_names(_persona("qa"))
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

    for persona in default_personas():
        visibility = resolve_tool_visibility(
            persona,
            ToolVisibilityOptions(permission_mode="unbounded", permission_source="test"),
        )
        assert visibility["blocked_tool_names"] == []
        assert required_model_tools.issubset(set(visibility["final_model_tools"]))
