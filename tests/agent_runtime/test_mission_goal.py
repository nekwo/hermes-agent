import json

import agent_runtime.mission_goal as mission_goal_mod
from agent_runtime.mission_goal import create_mission_goal, create_mission_goal_from_request
from agent_runtime.store import TaskStore
from tools.mission_goal_tool import mission_goal_create


def _canonical_request(**overrides):
    request = {
        "schema_version": 1,
        "idempotency_key": "perm-key",
        "source_surface": "mission_control",
        "operator": {"operator_id": "tony", "session_id": "s"},
        "goal": {"title": "T", "description": "D"},
        "blueprint": {"requested_blueprint_id": "neko_dev_qa_basic", "selection_mode": "explicit"},
    }
    request.update(overrides)
    return request


def test_create_from_request_denies_unattributed_mission_control_goal(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    result = create_mission_goal_from_request(_canonical_request(operator={}))
    assert result["error"]["code"] == "permission_denied"
    assert result["error"]["retryable"] is False


def test_create_surfaces_runtime_unavailable_when_runtime_prep_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    def _boom(*args, **kwargs):
        raise RuntimeError("runtime down")

    monkeypatch.setattr(mission_goal_mod, "prepare_new_goal_runtime", _boom)
    result = create_mission_goal(title="T", description="D", start_daemon_mode=False)
    assert result["error"]["code"] == "runtime_unavailable"
    assert result["error"]["retryable"] is True


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


def test_create_mission_goal_accepts_canonical_stage38_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    data = create_mission_goal(
        title="Stage 38 contract",
        description="Create a graph-routed Neko mission.",
        requested_by="tony",
        start_daemon_mode=False,
        idempotency_key="stage38-key",
        source_surface="mission_control",
        operator={"operator_id": "tony", "session_id": "launcher-session"},
        acceptance_criteria=["Neko, Dev, and QA are visible."],
        proof_expectations=["snapshot projection"],
        requested_blueprint_id="neko_dev_qa_basic",
        blueprint_selection_mode="explicit",
        repo_scope=["hermes-agent"],
    )

    assert data["schema_version"] == 1
    assert data["state"] == "created"
    assert data["mission_id"] == data["task_id"]
    assert data["blueprint_id"] == "neko_dev_qa_basic"
    task = TaskStore().get(data["task_id"])
    assert task.mission_plan.blueprint_id == "neko_dev_qa_basic"
    assert [stage.owner for stage in task.mission_plan.stages] == [
        "neko_supervisor",
        "dev",
        "qa",
    ]

    duplicate = create_mission_goal(
        title="Stage 38 contract",
        description="Create a graph-routed Neko mission.",
        requested_by="tony",
        start_daemon_mode=False,
        idempotency_key="stage38-key",
        source_surface="mission_control",
        operator={"operator_id": "tony", "session_id": "launcher-session"},
        acceptance_criteria=["Neko, Dev, and QA are visible."],
        proof_expectations=["snapshot projection"],
        requested_blueprint_id="neko_dev_qa_basic",
        blueprint_selection_mode="explicit",
        repo_scope=["hermes-agent"],
    )

    assert duplicate["state"] == "already_created"
    assert duplicate["task_id"] == data["task_id"]


def test_create_mission_goal_explicit_no_edit_cross_stack_blueprint_uses_recipe_gates(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    data = create_mission_goal(
        title="No-edit cross-stack proof",
        description="No-edit cross-stack proof for Backend and Launcher; do not modify product files.",
        requested_by="tony",
        start_daemon_mode=False,
        requested_blueprint_id="neko_two_dev_default",
        blueprint_selection_mode="explicit",
        repo_scope=["EterniaBackend", "EterniaLauncher"],
        acceptance_criteria=["Backend and Launcher no-product-edit proofs pass."],
    )

    task = TaskStore().get(data["task_id"])
    backend = next(stage for stage in task.mission_plan.stages if stage.id == "backend_implementation")
    launcher = next(stage for stage in task.mission_plan.stages if stage.id == "implement")

    assert backend.kind == "proof_only"
    assert backend.proof_recipe_id == "backend_contract_smoke"
    assert backend.proof_gate["proof_recipe_id"] == "backend_contract_smoke"
    assert launcher.kind == "proof_only"
    assert launcher.proof_recipe_id == "launcher_contract_smoke"
    assert launcher.proof_gate["proof_recipe_id"] == "launcher_contract_smoke"


def test_create_mission_goal_rejects_idempotency_conflict(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    create_mission_goal(
        title="Stage 38 contract",
        description="Create a graph-routed Neko mission.",
        start_daemon_mode=False,
        idempotency_key="stage38-key",
    )
    conflict = create_mission_goal(
        title="Different contract",
        description="Same key, different content.",
        start_daemon_mode=False,
        idempotency_key="stage38-key",
    )

    assert conflict["error"]["code"] == "duplicate_conflict"


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
