from agent_runtime.models import AgentPersona
from agent_runtime.personas import default_personas
from agent_runtime.tool_visibility import (
    ToolVisibilityOptions,
    agent_hud_state_for_persona,
    permission_state_for_persona,
    resolve_tool_visibility,
    turn_tool_context_for_persona,
)
from agent_runtime.tool_permissions import ChatToolPermissionStore, permission_options_for_chat
from agent_runtime.tool_turn_history import persist_tool_turn_actual


def _persona(persona_id: str):
    return {persona.id: persona for persona in default_personas()}[persona_id]


def test_neko_supervisor_visibility_has_dev_parity_by_default():
    # The mission-lead role was brought to dev-grade tool parity (operator
    # decision 2026-06-25): terminal + file mutation are available by default,
    # while the global persona blocks (delegate_task/kanban/send_message) stay.
    visibility = resolve_tool_visibility(_persona("neko_supervisor"))

    final_tools = set(visibility["final_model_tools"])

    assert "read_file" in final_tools
    assert "search_files" in final_tools
    assert "write_file" in final_tools
    assert "patch" in final_tools
    assert "terminal" in final_tools
    assert "delegate_task" in visibility["blocked_tool_names"]
    assert visibility["mutation_boundary"]["can_mutate_files"] is True
    assert visibility["mutation_boundary"]["can_run_terminal"] is True


def test_dev_visibility_can_mutate_but_keeps_default_persona_blocks():
    visibility = resolve_tool_visibility(_persona("dev"))

    final_tools = set(visibility["final_model_tools"])

    assert "write_file" in final_tools
    assert "patch" in final_tools
    assert "terminal" in final_tools
    assert "send_message" in visibility["blocked_tool_names"]
    assert "delegate_task" in visibility["blocked_tool_names"]
    assert visibility["mutation_boundary"]["can_mutate_files"] is True
    assert visibility["mutation_boundary"]["can_run_terminal"] is True


def test_qa_visibility_is_unbounded_and_can_mutate():
    visibility = resolve_tool_visibility(_persona("qa"), ToolVisibilityOptions(permission_mode="unbounded"))

    final_tools = set(visibility["final_model_tools"])

    assert "terminal" in final_tools
    assert "write_file" in final_tools
    assert "patch" in final_tools
    assert visibility["blocked_tool_names"] == []
    assert visibility["mutation_boundary"]["can_mutate_files"] is True
    assert visibility["mutation_boundary"]["can_run_terminal"] is True


def test_unbounded_permission_mode_expands_neko_visibility():
    visibility = resolve_tool_visibility(
        _persona("neko_supervisor"),
        ToolVisibilityOptions(permission_mode="unbounded", permission_source="test"),
    )

    final_tools = set(visibility["final_model_tools"])

    assert "write_file" in final_tools
    assert "patch" in final_tools
    assert "terminal" in final_tools
    assert visibility["blocked_tool_names"] == []
    assert visibility["permission_mode"] == "unbounded"


def test_turn_context_permission_state_and_hud_share_the_same_resolution():
    persona = _persona("dev")
    options = ToolVisibilityOptions(
        permission_mode="operator_one_turn",
        permission_source="test",
        session_id="session_35",
        task_id="task_35",
        goal_id="goal_35",
        blocked_tool_names=["terminal"],
        turns_remaining=1,
    )

    turn_context = turn_tool_context_for_persona(persona, options)
    permission_state = permission_state_for_persona(persona, options)
    hud_state = agent_hud_state_for_persona(persona, options)

    assert turn_context["preview"]["permission_mode"] == "operator_one_turn"
    assert turn_context["preview"]["session_id"] == "session_35"
    assert "terminal" not in turn_context["preview"]["final_model_tools"]
    assert turn_context["preview"]["model_tool_tokens"] > 0
    assert permission_state["mode"] == "operator_one_turn"
    assert permission_state["turns_remaining"] == 1
    assert permission_state["can_run_terminal"] is False
    assert hud_state["tool_count"] == len(turn_context["preview"]["final_model_tools"])
    assert hud_state["model_tool_tokens"] == turn_context["preview"]["model_tool_tokens"]


def test_chat_permission_store_can_narrow_dev_chat_to_read_only(tmp_path):
    persona = _persona("dev")
    store = ChatToolPermissionStore(path=tmp_path / "tool_permissions.json")

    store.set(
        persona_id=persona.id,
        session_id="session_read_only",
        mode="read_only",
        reason="operator requested inspection-only turn",
    )

    options = permission_options_for_chat(
        persona,
        session_id="session_read_only",
        store=store,
    )
    visibility = resolve_tool_visibility(persona, options)

    assert visibility["permission_mode"] == "read_only"
    assert "write_file" not in visibility["final_model_tools"]
    assert "patch" not in visibility["final_model_tools"]
    assert "terminal" not in visibility["final_model_tools"]


def test_chat_permission_store_can_expand_chat_to_unbounded(tmp_path):
    persona = _persona("qa")
    store = ChatToolPermissionStore(path=tmp_path / "tool_permissions.json")

    store.set(
        persona_id=persona.id,
        session_id="session_unbounded",
        mode="unbounded",
        reason="operator enabled full tools for this chat",
    )

    options = permission_options_for_chat(
        persona,
        session_id="session_unbounded",
        store=store,
    )
    visibility = resolve_tool_visibility(persona, options)

    assert visibility["permission_mode"] == "unbounded"
    assert visibility["blocked_tool_names"] == []
    assert "write_file" in visibility["final_model_tools"]
    assert "patch" in visibility["final_model_tools"]
    assert "terminal" in visibility["final_model_tools"]


def test_profile_chat_keeps_persona_safety_blocks():
    persona = AgentPersona(
        id="profile:alice",
        display_name="Alice",
        role="profile",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal", "todo", "mission_goal"],
        system_prompt_path="",
    )

    visibility = resolve_tool_visibility(persona)

    assert "delegate_task" in visibility["blocked_tool_names"]
    assert "send_message" in visibility["blocked_tool_names"]
    assert "kanban_create" in visibility["blocked_tool_names"]
    assert "delegate_task" not in visibility["final_model_tools"]
    assert "send_message" not in visibility["final_model_tools"]


def test_expired_unbounded_permission_falls_back_to_profile_default(tmp_path):
    persona = _persona("neko_supervisor")
    store = ChatToolPermissionStore(path=tmp_path / "tool_permissions.json")

    store.set(
        persona_id=persona.id,
        session_id="session_expired",
        mode="unbounded",
        reason="operator enabled full tools briefly",
        expires_at="2000-01-01T00:00:00Z",
    )

    options = permission_options_for_chat(
        persona,
        session_id="session_expired",
        store=store,
    )
    state = permission_state_for_persona(persona, options)

    assert state["mode"] == "profile_default"
    assert state["expired"] is True


def test_turn_tool_context_loads_last_actual_tool_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    persona = _persona("dev")

    persist_tool_turn_actual(
        persona_id=persona.id,
        session_id="session_actual",
        task_id="task_actual",
        goal_id="goal_actual",
        turn_id="turn_actual",
        model_input={
            "enabled_toolsets": ["file"],
            "blocked_tool_names": [],
            "tool_schema": {
                "schema_version": 1,
                "kind": "actual_model_tools",
                "final_model_tools": ["read_file", "write_file"],
                "tool_count": 2,
            },
        },
    )

    context = turn_tool_context_for_persona(
        persona,
        ToolVisibilityOptions(session_id="session_actual"),
    )

    assert context["last_actual"]["turn_id"] == "turn_actual"
    assert context["last_actual"]["final_model_tools"] == ["read_file", "write_file"]
    assert context["history"][0]["tool_count"] == 2


def test_turn_tool_context_does_not_persist_requested_blocked_tool_names(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    persona = _persona("dev")

    persist_tool_turn_actual(
        persona_id=persona.id,
        session_id="session_blocked_names",
        turn_id="turn_blocked_names",
        model_input={
            "enabled_toolsets": ["file"],
            "blocked_tool_names": ["kanban_complete", "terminal"],
            "tool_schema": {
                "schema_version": 1,
                "kind": "actual_model_tools",
                "final_model_tools": ["read_file"],
                "tool_count": 1,
                "blocked_tool_names": ["kanban_complete", "terminal"],
            },
        },
    )

    context = turn_tool_context_for_persona(
        persona,
        ToolVisibilityOptions(session_id="session_blocked_names"),
    )

    assert "blocked_tool_names" not in context["last_actual"]
    assert "blocked_tool_names" not in context["last_actual"]["tool_schema"]
