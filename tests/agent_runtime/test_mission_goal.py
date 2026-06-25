import json

from agent_runtime.mission_goal import create_mission_goal
from agent_runtime.store import TaskStore
from tools.mission_goal_tool import mission_goal_create


def test_create_mission_goal_creates_real_task_without_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    data = create_mission_goal(
        title="Add reply quoting to broadcast composer",
        description="Wire reply targeting through the broadcast composer. Proof: flutter analyze + widget test. Non-goal: server-side changes.",
        requested_by="test-operator",
        start_daemon_mode=False,
    )

    task_id = data["task_id"]
    assert task_id.startswith("task_")
    # The task is real and persisted in the live store, not a temp smoke graph.
    stored = TaskStore().get(task_id)
    assert stored.title == "Add reply quoting to broadcast composer"
    assert data["daemon_start"]["attempted"] is False
    # New-goal hygiene ran (same payload shape the CLI emits).
    assert "new_goal_hygiene" in data
    assert "foreground_runtime" in data


def test_mission_goal_create_tool_returns_real_task_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    raw = mission_goal_create(
        title="Investigate snapshot refresh stalls",
        description="Find why the operator channel snapshot does not refresh after a chat turn. No edits; report findings.",
        start_daemon=False,
    )
    payload = json.loads(raw)

    assert payload["ok"] is True
    assert payload["task_id"].startswith("task_")
    assert TaskStore().get(payload["task_id"]).title == "Investigate snapshot refresh stalls"


def test_mission_goal_create_tool_rejects_blank_input(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    assert json.loads(mission_goal_create(title="", description="x"))["ok"] is False
    assert json.loads(mission_goal_create(title="x", description="  "))["ok"] is False


def test_mission_goal_create_is_available_and_unblocked_for_supervisor():
    from agent_runtime.personas import blocked_tool_names, default_personas, effective_toolsets

    neko = next(persona for persona in default_personas() if persona.id == "neko_supervisor")
    assert "mission_goal" in effective_toolsets(neko)
    assert "mission_goal_create" not in blocked_tool_names(neko)


def test_supervisor_chat_toolset_gains_mission_goal_even_if_persona_list_omits_it(tmp_path, monkeypatch):
    # The live operator persona (profile:alice) carries a persisted/config toolset
    # list that predates mission_goal. The chat resolver must still grant it so a
    # real goal can be triggered from the operator channel.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.models import AgentPersona
    from agent_runtime.persona_runtime import _enabled_toolsets_for_chat

    supervisor = AgentPersona(
        id="profile:alice",
        display_name="Alice Agent",
        role="alice_supervisor",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal", "code_execution"],
        system_prompt_path="",
    )
    assert "mission_goal" in _enabled_toolsets_for_chat(supervisor, session_id="sess_x")

    # A non-supervisor role is not granted the supervisor-only capability.
    dev = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal"],
        system_prompt_path="",
    )
    assert "mission_goal" not in _enabled_toolsets_for_chat(dev, session_id="sess_x")
