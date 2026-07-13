from __future__ import annotations

from types import SimpleNamespace

from hermes_time import now

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.mission_goal import create_mission_goal
from agent_runtime.models import MissionPlan, Task
from agent_runtime.node_tools import NodeToolService
from agent_runtime.root_node_engine import _root_system_prompt
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore


class FakeRunner:
    def run(self, request):
        return SimpleNamespace(
            final_response="implemented with focused proof",
            session_id="sess_author",
            provider="test",
            model="fake",
            base_url=None,
            messages=[],
            api_calls=1,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=1,
            profile_timing={},
            raw={},
        )


def _ready_binding(*_args, **_kwargs):
    return SimpleNamespace(readiness="ready", hermes_profile="base", summary="ready")


def test_root_mode_create_goal_uses_script_blueprint_instead_of_default_plan(isolate_agent_runtime_root):
    data = create_mission_goal(
        title="Gap one trap",
        description="Please fix the regression in hermes-agent and run `python -m pytest tests/agent_runtime/test_root_authoring.py -q`.",
        requested_by="test",
        start_daemon_mode=False,
        config=AgentRuntimeConfig(root_node_mode=True),
    )

    task = TaskStore().get(data["task_id"])
    assert task.harness_self_heal["root_node_mode"] is True
    assert task.mission_plan.blueprint_id == "neko_default_script"
    assert task.mission_plan.stages == []
    assert task.current_stage_id is None


def test_root_authored_single_repo_stage_persists_for_snapshot(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.node_tools as node_tools

    monkeypatch.setattr(node_tools, "resolve_persona_profile", _ready_binding)
    monkeypatch.setattr(node_tools, "isolated_repo_context_for_run", lambda repo_ctx, **_kwargs: repo_ctx)
    ts = now()
    task = TaskStore().create(
        Task(
            id="task_author",
            title="Author one stage",
            description="Run focused proof in hermes-agent only.",
            state=TaskState.CREATED,
            created_at=ts,
            updated_at=ts,
            requested_by="test",
            mission_plan=MissionPlan(enabled=True, stages=[], blueprint_id="neko_default_script", blueprint_version=1),
            harness_self_heal={"root_node_mode": True},
        )
    )
    service = NodeToolService(config=AgentRuntimeConfig(root_node_mode=True), runner=FakeRunner())

    result = service.run_node(
        task_id=task.id,
        stage={
            "id": "focused_test",
            "title": "Focused test",
            "objective": "Run the focused root authoring test.",
            "owner": "dev",
            "repo": "hermes-agent",
            "test_plan": ["python -m pytest tests/agent_runtime/test_root_authoring.py -q"],
        },
    )

    from agent_runtime.snapshot import build_snapshot

    stored = TaskStore().get(task.id)
    snapshot = build_snapshot(task_store=TaskStore())
    snap_task = next(item for item in snapshot["tasks"] if item["task_id"] == task.id)
    assert result.ok is True
    assert [stage.repo for stage in stored.mission_plan.stages] == ["hermes-agent"]
    assert snap_task["mission_plan"]["stages"][0]["repo"] == "hermes-agent"
    assert snap_task["mission_plan"]["stages"][0]["test_plan"] == ["python -m pytest tests/agent_runtime/test_root_authoring.py -q"]


def test_root_system_prompt_loads_root_skill_contract():
    prompt = _root_system_prompt()

    assert "Root Node Contract" in prompt
    assert "run_node" in prompt
    assert "steer_node" in prompt
    assert "ROOT_NODE_DONE" in prompt
    assert "Do not emit AgentDecision JSON" in prompt
