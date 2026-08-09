from agent_runtime.models import AgentPersona
from agent_runtime.personas import (
    REGISTRY_HYGIENE_BLOCKED_TOOLS,
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

    names = _tool_names(effective_toolsets(pm), blocked_tool_names())

    assert "terminal" in names
    assert "write_file" in names
    assert "patch" in names


def test_qa_actual_tool_schema_follows_explicit_test_data():
    qa = _persona("qa")

    names = _tool_names(effective_toolsets(qa), blocked_tool_names())

    assert "write_file" in names
    assert "patch" in names
    assert "terminal" in names


def test_the_chat_blocklist_is_runtime_wide_not_per_persona():
    """S66 renamed this from ``test_sample_personas_keep_persona_blocklists``.

    The old name and its per-persona loop promised a contrast the runtime has
    not drawn since the per-role deny tables retired (s11's
    ``PER_ROLE_TOOL_DENIES``): ``blocked_tool_names`` returned the same constant
    for every argument, so the loop asserted one fact N times. The parameter is
    gone; the fact it actually pins — WHICH tools the runtime-wide chat
    blocklist holds, and which it deliberately does not — is asserted once, and
    the invariance is now asserted directly instead of implied.
    """

    blocked = blocked_tool_names()
    assert "delegate_task" in blocked
    assert "memory" in blocked
    assert "send_message" in blocked
    # Implementation tools are deliberately NOT blocked at this layer.
    assert "write_file" not in blocked
    assert "patch" not in blocked
    # It is a single runtime-wide frozenset, identical for every persona.
    assert all(blocked_tool_names() is blocked for _ in sample_personas())


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
        # Registry hygiene never yields to a permission mode (plan §3.4): the 17
        # kanban/feishu names stay blocked on every lane, unbounded included,
        # because deregistering upstream junk is not a permission tier.
        assert visibility["blocked_tool_names"] == sorted(REGISTRY_HYGIENE_BLOCKED_TOOLS)
        assert required_model_tools.issubset(set(visibility["final_model_tools"]))
