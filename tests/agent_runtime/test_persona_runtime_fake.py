import json
import os
from pathlib import Path

import pytest

from hermes_time import now

from agent_runtime.context_builder import build_context
from agent_runtime.decision_schema import DecisionPayloadInvalid, DecisionType
from agent_runtime.events import EventLog
from agent_runtime.models import AgentRun, Event, Task
from agent_runtime.persona_runtime import GPTPersonaRuntime
from agent_runtime.persona_runtime import _apply_llm_metadata
from agent_runtime.profile_runner import AgentRunResult
from agent_runtime.personas import default_personas
from agent_runtime.states import RunState, TaskState


class FakeAIAgent:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        FakeAIAgent.instances.append(self)

    def run_conversation(self, *, user_message, system_message, task_id):
        self.calls.append({"user_message": user_message, "system_message": system_message, "task_id": task_id})
        return {
            "final_response": json.dumps(
                {
                    "type": "propose_stage_plan",
                    "summary": "Plan the safe implementation stages.",
                    "rationale": "The task needs staged execution before patching.",
                    "payload": {
                        "stages": [
                            {
                                "title": "Audit",
                                "objective": "Inspect repo",
                                "acceptance_criteria": ["Repo surfaces are identified."],
                                "affected_paths": ["agent_runtime/"],
                                "test_plan": ["Run targeted harness tests."],
                            }
                        ]
                    },
                    "requires_approval": False,
                    "schema_version": 1,
                }
            ),
            "session_id": "session_from_fake",
            "api_calls": 1,
            "model": "gpt-5.5",
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex/?token=SECRET",
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "turn_exit_reason": "text_response(finish_reason=stop)",
        }


def make_task_and_run():
    ts = now()
    task = Task(
        id="task_abc",
        title="Build harness",
        description="Make agent runtime reliable",
        state=TaskState.DEV_STAGE_PLANNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )
    run = AgentRun(
        id="run_abc",
        persona_id="dev",
        task_id=task.id,
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        iteration_budget=12,
        session_id="session_existing",
    )
    return task, run


def test_apply_llm_metadata_tolerates_non_dict_raw_result():
    _task, run = make_task_and_run()
    result = AgentRunResult(
        final_response='{"type":"report_qa_verdict","summary":"ok","rationale":"ok"}',
        session_id="session_from_fake",
        provider="openai-codex",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        messages=[],
        api_calls=1,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        latency_ms=123,
        raw="text_response(finish_reason=stop)",  # type: ignore[arg-type]
    )

    _apply_llm_metadata(run, result, timing={})

    assert run.llm["provider"] == "openai-codex"
    assert "finish_reason" not in run.llm
    assert run.llm["total_tokens"] == 15


def test_dev_persona_tick_returns_structured_decision_without_network():
    FakeAIAgent.instances.clear()
    task, run = make_task_and_run()
    task.affected_repos = [str(Path.cwd())]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    ctx = build_context(task, run)
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    decision = runtime.run_tick(dev, ctx, run=run)

    assert decision.type == DecisionType.PROPOSE_STAGE_PLAN
    assert decision.payload["stages"][0]["title"] == "Audit"
    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["provider"] == "openai-codex"
    assert fake.kwargs["model"] == "gpt-5.5"
    assert fake.kwargs["enabled_toolsets"] == ["file", "search", "terminal", "session_search", "code_execution", "skills"]
    assert fake.kwargs["skip_context_files"] is True
    assert fake.kwargs["skip_memory"] is True
    assert fake.kwargs["platform"] == "agent_runtime"
    assert fake.kwargs["session_id"] == "session_existing"
    assert fake.kwargs["max_iterations"] == 12
    assert fake.calls[0]["task_id"] == "run_abc"
    assert "AgentDecision" in fake.calls[0]["system_message"]
    assert "Build harness" in fake.calls[0]["user_message"]
    assert run.session_id == "session_from_fake"
    assert run.llm["session_id"] == "session_from_fake"
    assert run.llm["base_url_host"] == "chatgpt.com"
    assert run.llm["total_tokens"] == 120
    assert isinstance(run.llm["latency_ms"], int)
    assert run.llm["latency_ms"] >= 0
    assert run.llm["timing"]["prompt_render_ms"] >= 0
    assert run.llm["timing"]["provider_call_ms"] >= 0
    assert run.llm["timing"]["profile_runner_ms"] >= 0
    assert run.llm["timing"]["profile_runtime_resolve_ms"] >= 0
    assert run.llm["timing"]["profile_agent_construct_ms"] >= 0
    assert run.llm["timing"]["profile_conversation_call_ms"] >= 0
    assert run.llm["timing"]["profile_result_normalize_ms"] >= 0
    assert run.llm["timing"]["profile_budget_checks_ms"] >= 0
    assert "SECRET" not in json.dumps(run.llm)


def test_llm_timing_records_repeated_profile_attempts_without_losing_totals():
    _, run = make_task_and_run()
    first = AgentRunResult(
        final_response="{}",
        session_id="session_1",
        provider="openai-codex",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        messages=[],
        latency_ms=100,
        profile_timing={"conversation_call_ms": 90, "provider_stream_event_count": 4},
    )
    second = AgentRunResult(
        final_response="{}",
        session_id="session_1",
        provider="openai-codex",
        model="gpt-5.5",
        base_url="https://chatgpt.com/backend-api/codex",
        messages=[],
        latency_ms=60,
        profile_timing={"conversation_call_ms": 50, "provider_stream_event_count": 7},
    )

    _apply_llm_metadata(run, first, timing={"provider_call_ms": 110})
    _apply_llm_metadata(run, second, timing={"provider_call_ms": 70})

    timing = run.llm["timing"]
    assert timing["provider_call_ms"] == 70
    assert timing["provider_call_count"] == 2
    assert timing["provider_call_total_ms"] == 180
    assert timing["provider_call_max_ms"] == 110
    assert timing["profile_conversation_call_ms"] == 50
    assert timing["profile_conversation_call_count"] == 2
    assert timing["profile_conversation_call_total_ms"] == 140
    assert timing["profile_conversation_call_max_ms"] == 90
    assert timing["profile_provider_stream_event_count"] == 11


def test_dev_launcher_can_use_profile_memory_when_configured():
    FakeAIAgent.instances.clear()
    task, run = make_task_and_run()
    task.affected_repos = [str(Path.cwd())]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    dev.include_profile_memory = True
    ctx = build_context(task, run)
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    runtime.run_tick(dev, ctx, run=run)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["skip_memory"] is False


def test_free_floating_persona_chat_turn_uses_memory():
    FakeAIAgent.instances.clear()
    task, run = make_task_and_run()
    task.affected_repos = [str(Path.cwd())]
    task.risk_flags = ["persona_operation_kind:free_floating"]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    ctx = build_context(task, run)
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    runtime.run_tick(dev, ctx, run=run)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["skip_memory"] is False


def test_dev_core_context_files_require_explicit_opt_in():
    FakeAIAgent.instances.clear()
    task, run = make_task_and_run()
    task.affected_repos = [str(Path.cwd())]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    dev.include_core_context_files = True
    ctx = build_context(task, run)
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    runtime.run_tick(dev, ctx, run=run)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["skip_context_files"] is False


def test_dev_persona_run_is_grounded_in_resolved_repo_and_loads_project_context(tmp_path, monkeypatch):
    FakeAIAgent.instances.clear()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Agent instructions\nUse repo-local tests.\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("# Claude instructions\nStay scoped.\n", encoding="utf-8")
    (repo / ".hermes.md").write_text("# Hermes project rules\nUse all repo guidance.\n", encoding="utf-8")
    task, run = make_task_and_run()
    task.affected_repos = [str(repo)]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    ctx = build_context(task, run)
    seen = {}

    class CwdAwareAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            seen["cwd"] = Path.cwd()
            seen["TERMINAL_CWD"] = os.environ.get("TERMINAL_CWD")
            seen["user_message"] = user_message
            return super().run_conversation(user_message=user_message, system_message=system_message, task_id=task_id)

    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=CwdAwareAgent)

    runtime.run_tick(dev, ctx, run=run)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["skip_context_files"] is True
    assert seen["cwd"] == repo
    assert seen["TERMINAL_CWD"] == str(repo)
    assert "Repo-Grounded Execution" in seen["user_message"]
    assert "repo_label: repo" in seen["user_message"]
    assert "context_loaded: .hermes.md, AGENTS.md, CLAUDE.md" in seen["user_message"]
    assert "Repo Context Excerpts (Harness-Controlled)" in seen["user_message"]
    assert "Use repo-local tests." in seen["user_message"]
    assert "Stay scoped." in seen["user_message"]
    assert "Use all repo guidance." in seen["user_message"]
    assert str(repo) not in seen["user_message"]
    assert "Use session_search only when task context is insufficient" in seen["user_message"]


def test_backend_dev_uses_persona_repo_scope_instead_of_first_task_repo(tmp_path, monkeypatch):
    FakeAIAgent.instances.clear()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    harness_repo = tmp_path / "harness"
    backend_repo = tmp_path / "backend"
    harness_repo.mkdir()
    backend_repo.mkdir()
    task, run = make_task_and_run()
    task.affected_repos = [str(harness_repo), str(backend_repo)]
    backend_dev = next(persona for persona in default_personas() if persona.id == "backend_dev")
    backend_dev.hermes_profile = None
    backend_dev.repo_scope = str(backend_repo)
    ctx = build_context(task, run)
    seen = {}

    class CwdAwareAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            seen["cwd"] = Path.cwd()
            seen["user_message"] = user_message
            return super().run_conversation(user_message=user_message, system_message=system_message, task_id=task_id)

    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=CwdAwareAgent)

    runtime.run_tick(backend_dev, ctx, run=run)

    assert seen["cwd"] == backend_repo
    assert "repo_label: backend" in seen["user_message"]


def test_dev_persona_repo_scope_does_not_override_mismatched_task_repo(tmp_path, monkeypatch):
    FakeAIAgent.instances.clear()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    launcher_repo = tmp_path / "launcher"
    harness_repo = tmp_path / "hermes-agent"
    launcher_repo.mkdir()
    harness_repo.mkdir()
    task, run = make_task_and_run()
    task.affected_repos = [str(harness_repo)]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    dev.hermes_profile = None
    dev.repo_scope = str(launcher_repo)
    ctx = build_context(task, run)
    seen = {}

    class CwdAwareAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            seen["cwd"] = Path.cwd()
            seen["user_message"] = user_message
            return super().run_conversation(user_message=user_message, system_message=system_message, task_id=task_id)

    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=CwdAwareAgent)

    runtime.run_tick(dev, ctx, run=run)

    assert seen["cwd"] == harness_repo
    assert "repo_label: hermes-agent" in seen["user_message"]


def test_dev_persona_can_use_latest_handoff_repo_when_affected_repos_empty(monkeypatch, tmp_path):
    FakeAIAgent.instances.clear()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    task, run = make_task_and_run()
    task.affected_repos = []
    log = EventLog()
    log.append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=task.id,
            run_id="run_neko",
            persona_id="neko_supervisor",
            payload={
                "packet_id": "packet_handoff_harness",
                "packet_type": "handoff_packet",
                "body": {
                    "target_owner": "dev",
                    "target_repo": "hermes-agent",
                    "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed", "visual_required": False},
                },
            },
        )
    )
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    dev.hermes_profile = None
    ctx = build_context(task, run, event_log=log)
    seen = {}

    class CwdAwareAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            seen["cwd"] = Path.cwd()
            seen["user_message"] = user_message
            return super().run_conversation(user_message=user_message, system_message=system_message, task_id=task_id)

    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=CwdAwareAgent)

    runtime.run_tick(dev, ctx, run=run)

    assert seen["cwd"] == Path(__file__).resolve().parents[2]
    assert "repo_label: hermes-agent" in seen["user_message"]


def test_dev_persona_invalid_affected_repo_fails_closed_before_home_cwd_fallback(tmp_path):
    FakeAIAgent.instances.clear()
    task, run = make_task_and_run()
    task.affected_repos = [str(tmp_path / "missing_repo")]
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    with pytest.raises(DecisionPayloadInvalid, match="affected repo workdir"):
        runtime.run_tick(dev, build_context(task, run), run=run)

    assert FakeAIAgent.instances == []


def test_dev_persona_missing_affected_repo_fails_closed_before_home_cwd_fallback():
    FakeAIAgent.instances.clear()
    task, run = make_task_and_run()
    task.affected_repos = []
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    with pytest.raises(DecisionPayloadInvalid, match="requires at least one affected_repo"):
        runtime.run_tick(dev, build_context(task, run), run=run)

    assert FakeAIAgent.instances == []
