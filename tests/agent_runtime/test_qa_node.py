from __future__ import annotations

import struct
from types import SimpleNamespace

from hermes_time import now

from agent_runtime.config import AgentRuntimeConfig
from agent_runtime.models import MissionPlan, Task
from agent_runtime.node_tools import NodeToolService, _child_system_prompt
from agent_runtime.personas import default_personas
from agent_runtime.proof_rules import ProofType
from agent_runtime.progress import RunProgressSink
from agent_runtime.states import TaskState
from agent_runtime.store import ProofStore, RunStore, TaskStore
from agent_runtime.tool_visibility import resolve_tool_visibility


def _png_bytes(width: int = 1366, height: int = 768) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00" + (b"0" * 160)


def _ready_binding(*_args, **_kwargs):
    return SimpleNamespace(readiness="ready", hermes_profile="base", summary="ready")


def _task(task_id: str = "task_qa_node") -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="QA node test",
        description="Run QA visual proof.",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        mission_plan=MissionPlan(enabled=True, stages=[], blueprint_id="neko_default_script", blueprint_version=1),
        harness_self_heal={"root_node_mode": True},
    )


class ScreenshotRunner:
    def __init__(self, screenshot_path, response: str = "qa satisfied") -> None:
        self.screenshot_path = screenshot_path
        self.response = response
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        if request.progress_callback is not None:
            request.progress_callback(
                {
                    "type": "run.tool.finished",
                    "tool_name": "mcp_launcher_qa_screenshot_window",
                    "status": "passed",
                    "label": "mission_control",
                    "screenshot": {
                        "path": str(self.screenshot_path),
                        "width": 1366,
                        "height": 768,
                    },
                    "redaction": {"safe": True},
                }
            )
        return SimpleNamespace(
            final_response=self.response,
            session_id="sess_qa",
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


def test_qa_persona_does_not_statically_carry_launcher_qa():
    # Flag-off zero change: launcher_qa is NOT baked into the static QA persona (that
    # would widen the legacy QA capability + readiness surface). It is injected at
    # dispatch for QA child nodes on the root-node path only (see below).
    qa = next(persona for persona in default_personas() if persona.id == "qa")
    visibility = resolve_tool_visibility(qa)

    assert "launcher_qa" not in qa.toolsets
    assert "launcher_qa" not in (qa.required_mcp_servers or [])
    assert "launcher_qa" not in visibility["effective_toolsets"]


def test_qa_child_gets_launcher_qa_injected_at_dispatch():
    from agent_runtime.node_tools import _child_enabled_toolsets

    qa = next(persona for persona in default_personas() if persona.id == "qa")
    dev = next(persona for persona in default_personas() if persona.id == "dev")

    assert "launcher_qa" in _child_enabled_toolsets(qa)
    assert "launcher_qa" not in _child_enabled_toolsets(dev)


def test_qa_child_prompt_names_rerun_and_launcher_qa_screenshot():
    qa = next(persona for persona in default_personas() if persona.id == "qa")
    prompt = _child_system_prompt(qa, SimpleNamespace(requires_visual_proof=True))

    assert "re-run the relevant tests yourself" in prompt
    assert "launcher_qa MCP screenshot" in prompt
    assert "concrete steer for the dev child" in prompt


def _enable_root_node_mode(monkeypatch):
    # The screenshot trace recorder is gated on root_node_mode (flag-off = zero
    # change); enable it for these root-node recorder tests.
    import agent_runtime.progress as progress

    # progress's root_node_mode gate is pinned to the ROOT config
    # (load_root_runtime_config); patch that symbol to enable the recorder.
    monkeypatch.setattr(progress, "load_root_runtime_config", lambda *a, **k: AgentRuntimeConfig(root_node_mode=True))


def test_launcher_qa_screenshot_trace_records_proof(monkeypatch, tmp_path, isolate_agent_runtime_root):
    _enable_root_node_mode(monkeypatch)
    image = tmp_path / "qa.png"
    image.write_bytes(_png_bytes())
    runs = RunStore()
    run = runs.open_run("qa", "task_visual_trace", stage_id="visual_qa")

    RunProgressSink(run_store=runs, run_id=run.id).emit(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "mcp_launcher_qa_screenshot_window",
            "status": "passed",
            "screenshot": {"path": str(image), "width": 1366, "height": 768},
            "redaction": {"safe": True},
        },
    )

    proofs = ProofStore().list_for_task("task_visual_trace")
    assert len(proofs) == 1
    proof = proofs[0]
    assert proof.type == ProofType.SCREENSHOT
    assert proof.created_by == "qa"
    assert proof.stage_id == "visual_qa"
    assert proof.metadata["source"] == "launcher_qa_tool_trace"
    assert proof.metadata["status"] == "passed"
    assert proof.metadata["nonblank"] is True
    assert proof.metadata["fullscreen"] is True
    assert proof.redaction_status == "safe"
    assert proof.path_or_value.startswith("proofs/task_visual_trace/artifacts/")


def test_launcher_qa_terminal_wrapper_screenshot_trace_records_proof(monkeypatch, tmp_path, isolate_agent_runtime_root):
    _enable_root_node_mode(monkeypatch)
    image = tmp_path / "qa_node_mission_control_20260703123623778.png"
    image.write_bytes(_png_bytes())
    runs = RunStore()
    run = runs.open_run("qa", "task_visual_wrapper", stage_id="visual_qa")

    RunProgressSink(run_store=runs, run_id=run.id).emit(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "terminal",
            "status": "passed",
            "command_label": (
                "powershell.exe -NoProfile -ExecutionPolicy Bypass "
                "-File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 "
                "-Tool mcp_launcher_qa_screenshot_window "
                f"-ArgsJson '{{\"label\":\"qa_node_mission_control\",\"out_dir\":\"{tmp_path.as_posix()}\"}}'"
            ),
        },
    )

    proofs = [proof for proof in ProofStore().list_for_task("task_visual_wrapper") if proof.type == ProofType.SCREENSHOT]
    assert len(proofs) == 1
    proof = proofs[0]
    assert proof.type == ProofType.SCREENSHOT
    assert proof.metadata["source"] == "launcher_qa_terminal_wrapper_trace"
    assert proof.metadata["tool_name"] == "mcp_launcher_qa_screenshot_window"
    assert proof.metadata["status"] == "passed"
    assert proof.metadata["artifact_exists"] is True
    assert proof.redaction_status == "safe"


def test_run_node_returns_qa_screenshot_evidence(monkeypatch, tmp_path, isolate_agent_runtime_root):
    import agent_runtime.node_tools as node_tools

    _enable_root_node_mode(monkeypatch)  # recorder reads the global flag (on in a live root-node goal)
    monkeypatch.setattr(node_tools, "resolve_persona_profile", _ready_binding)
    monkeypatch.setattr(node_tools, "isolated_repo_context_for_run", lambda repo_ctx, **_kwargs: repo_ctx)
    image = tmp_path / "qa-node.png"
    image.write_bytes(_png_bytes())
    task = TaskStore().create(_task("task_qa_run_node"))
    runner = ScreenshotRunner(image)
    service = NodeToolService(config=AgentRuntimeConfig(root_node_mode=True), runner=runner)

    result = service.run_node(
        task_id=task.id,
        stage={
            "id": "visual_qa",
            "title": "Visual QA",
            "objective": "Rerun proof and capture screenshot.",
            "owner": "qa",
            "repo": "hermes-agent",
            "requires_visual_proof": True,
        },
    )

    assert result.ok is True
    assert result.persona_id == "qa"
    assert "launcher_qa" in runner.requests[0].enabled_toolsets
    assert any(item.get("type") == "screenshot" and item.get("status") == "passed" for item in result.evidence)


def test_root_can_steer_qa_finding_to_dev_same_session(monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.node_tools as node_tools
    from tests.agent_runtime.test_root_node_mode import FakeRunner

    monkeypatch.setattr(node_tools, "resolve_persona_profile", _ready_binding)
    monkeypatch.setattr(node_tools, "isolated_repo_context_for_run", lambda repo_ctx, **_kwargs: repo_ctx)
    task = TaskStore().create(_task("task_qa_steer"))
    service = NodeToolService(config=AgentRuntimeConfig(root_node_mode=True), runner=FakeRunner(session_id="sess_dev"))
    first = service.run_node(task_id=task.id, stage={"id": "implement", "objective": "Implement it.", "owner": "dev", "repo": "hermes-agent"})

    steer_runner = FakeRunner("fixed after qa", session_id="sess_dev")
    service.runner = steer_runner
    result = service.steer_node(task_id=task.id, session_id=first.session_id, message="QA found a visual mismatch; fix and rerun proof.", stage_id="implement")

    assert result.ok is True
    assert steer_runner.requests[0].session_id == "sess_dev"
    assert "QA found a visual mismatch" in steer_runner.requests[0].user_message
