import json
from types import SimpleNamespace

from hermes_time import now

from agent_runtime.context_builder import build_context
from agent_runtime.models import AgentPersona, AgentRun, MissionIntent, MissionPlan, MissionPlanStage, Task
from agent_runtime.persona_runtime import GPTPersonaRuntime, build_system_prompt
from agent_runtime.profile_runner import AgentRunResult
from agent_runtime.states import RunState, StageStatus, TaskState


class CapturingRunner:
    def __init__(self):
        self.requests = []

    def run(self, request):
        self.requests.append(request)
        return AgentRunResult(
            final_response=json.dumps(
                {
                    "type": "qa_verdict",
                    "summary": "QA approved from proof packet.",
                    "rationale": "Proof IDs were present and sufficient for this code-only acceptance criterion.",
                    "payload": {
                        "review_scope": "implementation",
                        "verdict": "approved",
                        "proof_ids": ["proof_1"],
                        "findings": [],
                    },
                }
            ),
            session_id="session_qa",
            provider="openai-codex",
            model="gpt-5.5",
            base_url="https://chatgpt.com/backend-api/codex/responses",
            messages=[{"role": "assistant", "content": "decision"}],
            api_calls=1,
            total_tokens=10,
            raw={},
        )


def _task():
    return Task(
        id="task_qa",
        title="QA proof-first smoke",
        description="Verify QA stops at proof-backed verdict.",
        state=TaskState.RUNNING,
        created_at=now(),
        updated_at=now(),
        requested_by="test",
        proof_ids=["proof_1"],
        acceptance_criteria=["QA reviews attached proof before searching broadly."],
    )


def _run(persona_id="qa"):
    return AgentRun(
        id="run_qa",
        persona_id=persona_id,
        task_id="task_qa",
        stage_id=None,
        state=RunState.RUNNING,
        started_at=now(),
        last_heartbeat_at=now(),
    )


def _persona(role):
    return AgentPersona(
        id=role,
        display_name=f"{role} persona",
        role=role,
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal", "skills"],
        system_prompt_path=f"personas/{role}/system.md",
    )


def test_qa_prompt_is_proof_first_and_verdict_bounded():
    prompt = build_system_prompt(_persona("qa"))

    assert "Proof-first QA protocol" in prompt
    assert "Inspect the supplied proof IDs/artifacts/log summaries first" in prompt
    assert "not a second implementation investigator" in prompt
    assert "Repeated `search_files`, `read_file`, `session_search`, or `browser_snapshot` means you are looping" in prompt
    assert "return `qa_verdict`" in prompt
    assert "do not demand screenshots just to avoid making a decision" in prompt


def test_qa_runs_use_repeated_read_search_hard_stop(monkeypatch):
    runner = CapturingRunner()
    monkeypatch.setattr(
        "agent_runtime.persona_runtime.resolve_persona_profile",
        lambda persona: SimpleNamespace(readiness="ready", hermes_profile=None, summary="ready"),
    )
    runtime = GPTPersonaRuntime(agent_runner=runner)
    run = _run("qa")

    decision = runtime.run_tick(_persona("qa"), build_context(_task(), run), run=run)

    assert decision.type.value == "qa_verdict"
    assert runner.requests[-1].stop_on_repeated_read_search is True


def test_dev_runs_still_use_repeated_read_search_hard_stop(monkeypatch):
    runner = CapturingRunner()
    monkeypatch.setattr(
        "agent_runtime.persona_runtime.resolve_persona_profile",
        lambda persona: SimpleNamespace(readiness="ready", hermes_profile=None, summary="ready"),
    )
    runtime = GPTPersonaRuntime(agent_runner=runner)
    run = _run("dev")
    task = _task()
    task.affected_repos = ["EterniaLauncher"]

    # Avoid repo workdir resolution in this unit test; the flag under test is role-derived.
    monkeypatch.setattr("agent_runtime.persona_runtime._repo_context_for_persona", lambda persona, ctx: None)

    try:
        runtime.run_tick(_persona("dev"), build_context(task, run), run=run)
    except Exception:
        pass

    assert runner.requests[-1].stop_on_repeated_read_search is True


def test_no_edit_context_dev_run_blocks_repo_search_tools(monkeypatch):
    runner = CapturingRunner()
    monkeypatch.setattr(
        "agent_runtime.persona_runtime.resolve_persona_profile",
        lambda persona: SimpleNamespace(readiness="ready", hermes_profile=None, summary="ready"),
    )
    runtime = GPTPersonaRuntime(agent_runner=runner)
    run = _run("backend_dev")
    run.stage_id = "backend_investigation"
    task = _task()
    task.state = TaskState.RUNNING
    task.affected_repos = ["EterniaBackend"]
    task.current_stage_id = "backend_investigation"
    task.mission_plan = MissionPlan(
        mission_intent=MissionIntent(title=task.title, objective=task.description),
        current_stage_id="backend_investigation",
        stages=[
            MissionPlanStage(
                id="backend_investigation",
                title="Backend Investigation",
                objective="No-product-edit investigation of backend moderation paths.",
                owner="backend_dev",
                repo="EterniaBackend",
                kind="context",
                requires_product_edit=False,
                status=StageStatus.IMPLEMENTING,
            )
        ],
    )

    monkeypatch.setattr("agent_runtime.persona_runtime._repo_context_for_persona", lambda persona, ctx: None)

    persona = _persona("dev")
    persona.id = "backend_dev"

    try:
        runtime.run_tick(persona, build_context(task, run), run=run)
    except Exception:
        pass

    blocked = set(runner.requests[-1].blocked_tool_names)
    assert {"read_file", "search_files", "session_search", "browser_snapshot"} <= blocked


def test_neko_supervisor_does_not_use_repeated_read_search_hard_stop(monkeypatch):
    runner = CapturingRunner()
    monkeypatch.setattr(
        "agent_runtime.persona_runtime.resolve_persona_profile",
        lambda persona: SimpleNamespace(readiness="ready", hermes_profile=None, summary="ready"),
    )
    runtime = GPTPersonaRuntime(agent_runner=runner)
    persona = AgentPersona(
        id="neko_supervisor",
        display_name="Neko Mission Lead",
        role="alice_supervisor",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "skills"],
        system_prompt_path="personas/neko_supervisor/system.md",
    )
    run = _run("neko_supervisor")

    # Neko gets a QA-valid final response only to exercise request construction; role validation will fail after capture.
    try:
        runtime.run_tick(persona, build_context(_task(), run), run=run)
    except Exception:
        pass

    assert runner.requests[-1].stop_on_repeated_read_search is False
