from __future__ import annotations

from types import SimpleNamespace

from hermes_time import now

from agent_runtime.config import AgentRuntimeConfig, load_agent_runtime_config
from agent_runtime.models import MissionPlan, Task
from agent_runtime.node_tools import NodeToolService
from agent_runtime.root_node_engine import ROOT_NODE_DONE_MARKER, RootNodeEngine
from agent_runtime.states import TaskState
from agent_runtime.store import ProofStore, RunStore, TaskStore


class FakeRunner:
    def __init__(self, response: str = "child complete", *, session_id: str = "sess_child") -> None:
        self.response = response
        self.session_id = session_id
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if request.progress_callback is not None:
            request.progress_callback(
                {
                    "type": "run.tool.finished",
                    "tool_name": "terminal",
                    "command": "python -m pytest tests/agent_runtime/test_root_node_mode.py -q",
                    "exit_code": 0,
                    "status": "completed",
                    "stdout": "1 passed",
                    "stderr": "",
                    "duration_ms": 12,
                    "stage_id": "implement",
                    "repo_label": "hermes-agent",
                }
            )
        return SimpleNamespace(
            final_response=self.response,
            session_id=self.session_id,
            provider="test",
            model="fake",
            base_url=None,
            messages=[],
            api_calls=1,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            latency_ms=3,
            profile_timing={},
            raw={},
        )


def _ready_binding(*_args, **_kwargs):
    return SimpleNamespace(readiness="ready", hermes_profile="base", summary="ready")


def _task(task_id: str = "task_rootnode") -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Root node test",
        description="Run a child node.",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        mission_plan=MissionPlan(enabled=True, stages=[], blueprint_id="neko_default_script", blueprint_version=1),
        harness_self_heal={"root_node_mode": True},
    )


def test_root_node_mode_defaults_off_and_loads_from_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agent_runtime:\n  root_node_mode: true\n", encoding="utf-8")

    assert AgentRuntimeConfig().root_node_mode is False
    assert load_agent_runtime_config(config_path).root_node_mode is True


def test_run_node_records_stage_run_summary_and_evidence(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.node_tools as node_tools

    monkeypatch.setattr(node_tools, "resolve_persona_profile", _ready_binding)
    monkeypatch.setattr(node_tools, "isolated_repo_context_for_run", lambda repo_ctx, **_kwargs: repo_ctx)
    task = TaskStore().create(_task())
    service = NodeToolService(config=AgentRuntimeConfig(root_node_mode=True), runner=FakeRunner())

    result = service.run_node(
        task_id=task.id,
        stage={
            "id": "implement",
            "title": "Run focused proof",
            "objective": "Run the focused test.",
            "owner": "dev",
            "repo": "hermes-agent",
            "test_plan": ["python -m pytest tests/agent_runtime/test_root_node_mode.py -q"],
        },
    )

    stored = TaskStore().get(task.id)
    assert result.ok is True
    assert result.session_id == "sess_child"
    assert stored.mission_plan.stages[0].repo == "hermes-agent"
    assert RunStore().get(result.run_id).session_id == "sess_child"
    assert any(item["kind"] == "self_test" and item["status"] == "passed" for item in result.evidence)
    assert any(proof.metadata.get("self_test_evidence_id") for proof in ProofStore().list_for_task(task.id))


def test_steer_node_reuses_child_session(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.node_tools as node_tools

    monkeypatch.setattr(node_tools, "resolve_persona_profile", _ready_binding)
    monkeypatch.setattr(node_tools, "isolated_repo_context_for_run", lambda repo_ctx, **_kwargs: repo_ctx)
    task = TaskStore().create(_task())
    service = NodeToolService(config=AgentRuntimeConfig(root_node_mode=True), runner=FakeRunner())
    first = service.run_node(task_id=task.id, stage={"id": "implement", "objective": "Do it.", "owner": "dev", "repo": "hermes-agent"})

    steer_runner = FakeRunner("fixed", session_id="sess_child")
    service.runner = steer_runner
    steered = service.steer_node(task_id=task.id, session_id=first.session_id, message="Please fix and rerun proof.", stage_id="implement")

    assert steered.ok is True
    assert steer_runner.requests[0].session_id == "sess_child"
    assert steered.session_id == "sess_child"


def test_run_node_rejects_unknown_repo_alias(isolate_agent_runtime_root):
    task = TaskStore().create(_task())
    service = NodeToolService(config=AgentRuntimeConfig(root_node_mode=True), runner=FakeRunner())

    try:
        service.run_node(task_id=task.id, stage={"id": "bad", "objective": "Nope", "owner": "dev", "repo": "definitely-not-a-repo"})
    except ValueError as exc:
        assert "unknown repo scope" in str(exc)
    else:
        raise AssertionError("unknown repo alias should be rejected")


def test_diff_weakens_tests_and_changed_files_helpers():
    from agent_runtime.repo_context import changed_files_from_diff, diff_weakens_tests

    weakening = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
        "@@\n-    assert result == 1\n+    pass\n"
    )
    assert diff_weakens_tests(weakening) is True
    assert changed_files_from_diff(weakening) == ["tests/test_x.py"]

    benign = "diff --git a/agent_runtime/foo.py b/agent_runtime/foo.py\n@@\n-    x = 1\n+    x = 2\n"
    assert diff_weakens_tests(benign) is False
    assert changed_files_from_diff(benign) == ["agent_runtime/foo.py"]
    assert diff_weakens_tests("") is False


def test_run_node_diff_evidence_flags_test_weakening(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.node_tools as node_tools

    monkeypatch.setattr(node_tools, "resolve_persona_profile", _ready_binding)
    monkeypatch.setattr(node_tools, "isolated_repo_context_for_run", lambda repo_ctx, **_kwargs: repo_ctx)
    weakening = "diff --git a/tests/test_x.py b/tests/test_x.py\n-    assert x\n+    pass\n"
    monkeypatch.setattr(
        node_tools,
        "git_diff_since_baseline",
        lambda *a, **k: {"diff": weakening, "truncated": False, "new_untracked_paths": []},
    )
    task = TaskStore().create(_task("task_weaken"))
    service = NodeToolService(config=AgentRuntimeConfig(root_node_mode=True), runner=FakeRunner())

    result = service.run_node(task_id=task.id, stage={"id": "impl", "objective": "x", "owner": "dev", "repo": "hermes-agent"})

    diff_ev = [item for item in result.evidence if item.get("kind") == "diff"]
    assert diff_ev, "diff evidence handle missing"
    assert diff_ev[0]["diff_weakens_tests"] is True
    assert "test_x.py" in diff_ev[0]["changed_files"]


def test_node_control_tool_is_root_only():
    # Defense-in-depth lock: run_node/steer_node live in the node_control toolset,
    # which must never be baked into a persona. The root gets it injected at dispatch
    # (_root_toolsets); no child persona may carry it, so a child node can never
    # invoke run_node/steer_node.
    from agent_runtime.personas import default_personas
    from agent_runtime.root_node_engine import _root_toolsets

    for persona in default_personas():
        assert "node_control" not in (persona.toolsets or []), persona.id

    root_like = SimpleNamespace(toolsets=["file", "skills"])
    assert "node_control" in _root_toolsets(root_like)


def test_root_node_engine_reports_task_terminal(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.root_node_engine as root_engine

    monkeypatch.setattr(root_engine, "resolve_persona_profile", _ready_binding)
    task = TaskStore().create(_task())
    runner = FakeRunner(f"All done. {ROOT_NODE_DONE_MARKER}", session_id="sess_root")
    engine = RootNodeEngine(config=AgentRuntimeConfig(root_node_mode=True), runner=runner)

    result = engine.run_until_settled(task_id=task.id, max_actions=3)

    assert result.stop_reason == "task_terminal"
    assert TaskStore().get(task.id).state == TaskState.DONE
    assert runner.requests[0].enabled_toolsets[-1] == "node_control"
    assert RunStore().list_for_task(task.id)[0].max_wall_seconds >= 900.0
