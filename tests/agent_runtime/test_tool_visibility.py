from agent_runtime.models import AgentPersona
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.tool_visibility import (
    ToolVisibilityOptions,
    permission_state_for_persona,
    resolve_tool_visibility,
    turn_tool_context_for_persona,
)
from agent_runtime.tool_permissions import ChatToolPermissionStore, permission_options_for_chat
from agent_runtime.tool_turn_history import persist_tool_turn_actual


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


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


def test_turn_context_and_permission_state_share_the_same_resolution():
    # ``agent_hud_state_for_persona`` was RETIRED (residue-slim R2, 2026-07-17);
    # the turn-context and permission-state lanes plus the head-scalar count now
    # all derive from the ONE ``resolve_tool_visibility`` resolution.
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
    resolution = resolve_tool_visibility(persona, options)

    assert turn_context["preview"]["permission_mode"] == "operator_one_turn"
    assert turn_context["preview"]["session_id"] == "session_35"
    assert "terminal" not in turn_context["preview"]["final_model_tools"]
    assert turn_context["preview"]["model_tool_tokens"] > 0
    assert permission_state["mode"] == "operator_one_turn"
    assert permission_state["turns_remaining"] == 1
    assert permission_state["can_run_terminal"] is False
    # The head-scalar ``tool_count`` (resolution["final_tool_count"]) the agents
    # drawer now renders equals the resolved model-callable tool set — the same
    # fact the retired hud_state["tool_count"] carried.
    assert resolution["final_tool_count"] == len(turn_context["preview"]["final_model_tools"])
    assert resolution["model_tool_tokens"] == turn_context["preview"]["model_tool_tokens"]


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


def test_turn_tool_context_never_labels_another_session_as_last_actual(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    persona = _persona("dev")

    persist_tool_turn_actual(
        persona_id=persona.id,
        session_id="session_other",
        turn_id="turn_other",
        model_input={
            "tool_schema": {
                "schema_version": 1,
                "kind": "actual_model_tools",
                "final_model_tools": ["read_file"],
                "tool_count": 1,
            },
        },
    )

    context = turn_tool_context_for_persona(
        persona,
        ToolVisibilityOptions(session_id="session_current"),
    )

    assert context["last_actual"] is None
    assert context["last_actual_session_match"] is False
    assert context["latest_persona_actual"]["session_id"] == "session_other"


def test_turn_tool_context_keeps_exact_session_and_latest_persona_provenance_separate(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    persona = _persona("dev")
    for session_id, turn_id in (
        ("session_current", "turn_current"),
        ("session_newer", "turn_newer"),
    ):
        persist_tool_turn_actual(
            persona_id=persona.id,
            session_id=session_id,
            turn_id=turn_id,
            model_input={"tool_schema": {"final_model_tools": ["read_file"]}},
        )

    context = turn_tool_context_for_persona(
        persona,
        ToolVisibilityOptions(session_id="session_current"),
    )

    assert context["last_actual"]["session_id"] == "session_current"
    assert context["last_actual_session_match"] is True
    assert context["latest_persona_actual"]["session_id"] == "session_newer"


def test_scoped_context_partitions_session_withheld_policy_and_excluded_toolsets():
    persona = _persona("neko_supervisor")
    preview = _scoped_preview(persona)

    callable_names = set(preview["final_model_tools"])
    withheld_names = {item["name"] for item in preview["withheld_tools"]}
    policy_names = {item["name"] for item in preview["policy_tools"]}
    candidate_names = set(preview["persona_candidate_tools"])

    assert callable_names.isdisjoint(withheld_names)
    assert withheld_names == candidate_names - callable_names
    assert policy_names.isdisjoint(candidate_names)
    assert preview["availability_counts"] == {
        "configured_toolsets": len(preview["configured_toolsets"]),
        "effective_toolsets": len(preview["effective_toolsets"]),
        "callable": len(callable_names),
        "withheld": len(withheld_names),
        "policy": len(policy_names),
        "excluded_toolsets": len(preview["excluded_toolsets"]),
    }
    assert preview["model_tool_token_estimate"]["exact"] is False
    assert preview["resolution_id"].startswith("toolres_")
    assert preview["resolved_at"].endswith("Z")


def test_registry_hygiene_is_not_mislabeled_as_persona_safety_policy():
    preview = _scoped_preview(_persona("neko_supervisor"))
    blocked = {item["name"]: item["reason"] for item in preview["blocked_tools"]}

    assert blocked["kanban_create"] == "registry_hygiene"
    assert blocked["feishu_doc_read"] == "registry_hygiene"
    assert blocked["skill_manage"] == "session_tool_policy"


# --- T9b: chat-lane preview parity ------------------------------------------
# The permission-preview resolver historically showed ``effective_toolsets``
# (the persona's raw configured set), so it omitted BOTH the operator-chat
# augmentation (mission_goal/agent_chat/board/clarify) AND the T3/T6a chat-lane
# cost scoping. ``apply_chat_lane_tool_scope`` threads the REAL chat-lane
# resolution onto the preview options so the preview reflects the schema the
# chat lane actually ships. These tests pin preview == actual lane.


def _actual_chat_lane(persona):
    """The tools + toolsets the operator chat lane actually ships for ``persona``,
    computed straight from the chat-lane chokepoint (the authority the preview
    must mirror)."""
    from agent_runtime.persona_runtime import (
        _blocked_tool_names_for_chat,
        _enabled_toolsets_for_chat,
    )
    from agent_runtime.profile_runner import _blocked_tool_names_with_registry_hygiene
    from agent_runtime.tool_visibility import _tool_names_for_toolsets

    enabled = _enabled_toolsets_for_chat(persona, session_id=None)
    blocked = _blocked_tool_names_with_registry_hygiene(
        _blocked_tool_names_for_chat(persona, session_id=None)
    )
    tools = set(_tool_names_for_toolsets(enabled, blocked_tool_names=sorted(set(blocked))))
    return sorted(enabled), tools


def _scoped_preview(persona):
    from agent_runtime.persona_runtime import apply_chat_lane_tool_scope

    options = permission_options_for_chat(persona, session_id=None)
    apply_chat_lane_tool_scope(persona, options, session_id=None)
    return resolve_tool_visibility(persona, options)


def test_chat_lane_preview_matches_actual_lane_default_scoped():
    persona = _persona("neko_supervisor")
    enabled, actual_tools = _actual_chat_lane(persona)
    preview = _scoped_preview(persona)

    # Toolsets + the resolved model-tool schema are byte-identical to the lane.
    assert sorted(preview["effective_toolsets"]) == enabled
    assert set(preview["final_model_tools"]) == actual_tools

    final = set(preview["final_model_tools"])
    # The chat-lane cost scoping (T3/T6a) is reflected: browser/vision/file/
    # terminal/code_execution and skill_manage are gone from the preview...
    assert "browser_navigate" not in final
    assert "vision_analyze" not in final
    assert "read_file" not in final
    assert "write_file" not in final
    assert "terminal" not in final
    assert "execute_code" not in final
    assert "skill_manage" not in final
    # ...while direct teammate messaging stays present and unfinished goal
    # creation is absent from standard Neko chat.
    assert "mission_goal_create" not in final
    assert "agent_chat_send" in final
    # ...including clarify, which PERSONA_BLOCKED_TOOLS blocks on autonomous
    # runs but the chat bridge unblocks — the old preview wrongly hid it.
    assert "clarify" in final
    # Read-only skill recall survives the skill_manage cut.
    assert "skill_view" in final


def test_chat_lane_preview_matches_actual_lane_with_restore_config(monkeypatch):
    # neko with the live config shape `chat_lane_restore_toolsets: [file]`
    # (constructed in-test — never read the operator's real config.yaml). The
    # restore un-excludes the `file` toolset on the bounded chat lane; the
    # preview must show `file` back AND stay byte-identical to the actual lane.
    import dataclasses

    import agent_runtime.config as cfgmod

    base = cfgmod.load_agent_runtime_config()
    fake = dataclasses.replace(
        base,
        personas={
            **(base.personas or {}),
            "neko_supervisor": {"chat_lane_restore_toolsets": ["file"]},
        },
    )
    monkeypatch.setattr(cfgmod, "load_agent_runtime_config", lambda *a, **k: fake)

    persona = _persona("neko_supervisor")
    enabled, actual_tools = _actual_chat_lane(persona)
    preview = _scoped_preview(persona)

    assert "file" in enabled
    assert sorted(preview["effective_toolsets"]) == enabled
    assert set(preview["final_model_tools"]) == actual_tools

    final = set(preview["final_model_tools"])
    # `file` toolset restored -> its tools reappear in the preview...
    assert "read_file" in final
    assert "write_file" in final
    # ...but the other cost cuts (terminal, skill_manage) stay off, and the
    # augmentation/clarify still ride.
    assert "terminal" not in final
    assert "skill_manage" not in final
    assert "clarify" in final
