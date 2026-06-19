from agent_runtime.models import AgentPersona
from agent_runtime.personas import (
    AgentRole,
    blocked_tool_names,
    default_personas,
    effective_toolsets,
)


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


def test_qa_actual_tool_schema_excludes_write_patch_but_keeps_verification_tools():
    qa = _persona("qa")

    names = _tool_names(effective_toolsets(qa), blocked_tool_names(qa))

    assert "write_file" not in names
    assert "patch" not in names
    assert "terminal" in names


def test_all_personas_exclude_side_effect_orchestration_tools():
    forbidden = {"delegate_task", "clarify", "memory", "send_message", "cronjob"}

    for persona in default_personas():
        names = _tool_names(effective_toolsets(persona), blocked_tool_names(persona))
        assert names.isdisjoint(forbidden)
        assert not any(name.startswith("kanban_") for name in names)


def test_harness_personas_exclude_kanban_tools_even_when_worker_env_set(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task_from_outer_worker")

    for persona in default_personas():
        names = _tool_names(effective_toolsets(persona), blocked_tool_names(persona))
        assert not any(name.startswith("kanban_") for name in names)
