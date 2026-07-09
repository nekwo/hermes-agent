import json
import os
import subprocess
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
from agent_runtime.personas import all_registered_toolsets, default_personas, effective_toolsets
from agent_runtime.tool_permissions import ChatToolPermissionStore
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
        state=TaskState.RUNNING,
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
    assert fake.kwargs["enabled_toolsets"] == effective_toolsets(dev)
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


def test_chat_permission_unbounded_reaches_actual_agent_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_id = "session_unbounded_actual"
    qa = next(persona for persona in default_personas() if persona.id == "qa")
    ChatToolPermissionStore().set(
        persona_id=qa.id,
        session_id=session_id,
        mode="unbounded",
        reason="operator enabled full tools for this chat",
    )
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    runtime.chat_reply(qa, "can you write now?", session_id=session_id)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["enabled_toolsets"] == all_registered_toolsets()
    assert fake.kwargs["blocked_tool_names"] == []


def test_chat_permission_unbounded_one_turn_expires_after_success(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_id = "session_unbounded_once"
    neko = next(persona for persona in default_personas() if persona.id == "neko_supervisor")
    store = ChatToolPermissionStore()
    store.set(
        persona_id=neko.id,
        session_id=session_id,
        mode="unbounded",
        reason="operator enabled one turn",
        turns_remaining=1,
    )
    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=FakeAIAgent)

    runtime.chat_reply(neko, "run the command", session_id=session_id)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["enabled_toolsets"] == all_registered_toolsets()
    assert fake.kwargs["blocked_tool_names"] == []
    record = store.get(persona_id=neko.id, session_id=session_id)
    assert record is not None
    assert record.mode == "profile_default"
    assert record.turns_remaining == 0


def test_chat_reply_can_disable_internal_session_persistence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_db = object()
    neko = next(persona for persona in default_personas() if persona.id == "neko_supervisor")
    runtime = GPTPersonaRuntime(
        default_provider="openai-codex",
        default_model="gpt-5.5",
        agent_factory=FakeAIAgent,
        session_db=session_db,
        persist_agent_session=False,
    )

    runtime.chat_reply(neko, "hi", session_id=None)

    fake = FakeAIAgent.instances[0]
    assert fake.kwargs["session_db"] is None


def test_chat_reply_routes_tool_calls_into_session_keyed_trace(tmp_path, monkeypatch):
    # End-to-end through the REAL ProfileAgentRunner: a chat turn that invokes a
    # tool must land redaction-safe run.tool.* events keyed on the chat session,
    # so the snapshot trace projection can surface them in the operator channel.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    session_id = "session_chat_tool_trace"
    neko = next(persona for persona in default_personas() if persona.id == "neko_supervisor")

    class ToolCallingAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            # Drive the runner's tool callbacks exactly as the real provider does.
            self.kwargs["tool_start_callback"]("tool.started", "terminal", "echo PARITY_OK_2026")
            self.kwargs["tool_complete_callback"](
                "tool.completed", "terminal", "echo PARITY_OK_2026", {"exit_code": 0}
            )
            return super().run_conversation(
                user_message=user_message, system_message=system_message, task_id=task_id
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_factory=ToolCallingAgent
    )

    pre_trace = []
    runtime.chat_reply(
        neko,
        "run echo PARITY_OK_2026 and paste the output",
        session_id=session_id,
        pre_trace_callback=pre_trace.append,
    )

    events = EventLog().for_session(session_id)
    assert len(pre_trace) == 1
    assert pre_trace[0]["type"] == "run.tool.started"
    assert pre_trace[0]["tool_name"] == "terminal"
    assert [event.type for event in events] == ["run.tool.started", "run.tool.finished"]
    assert all(event.session_id == session_id for event in events)
    assert all(event.task_id is None for event in events)
    assert all(event.persona_id == "neko_supervisor" for event in events)
    assert events[0].payload.get("tool_name") == "terminal"
    assert events[1].payload.get("status") == "passed"


def test_reasoning_summary_does_not_fire_pre_trace_ack(tmp_path, monkeypatch):
    # gpt-5.5 (codex) emits a reasoning-summary run.progress event on EVERY
    # turn, including tool-less "Hi" turns. It belongs in the Trace lane but
    # must NOT latch before_first_trace: doing so persisted a canned
    # "I'll check that now…" acknowledgment row on every conversational turn
    # (a phantom transcript row with no client_message_id that reordered the
    # Agent Console at snapshot reconcile). The ack hook fires on the first
    # run.tool.started only.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.progress import ChatProgressSink

    fired = []
    sink = ChatProgressSink(
        session_id="session_reasoning_no_ack",
        persona_id="neko_supervisor",
        before_first_trace=fired.append,
    )

    sink.emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "thinking_process",
            "step": "reasoning_summary",
            "status": "running",
            "reasoning_summary": "Hi Master — I'm here.",
        },
    )
    assert fired == []  # reasoning trace recorded, ack hook untouched

    sink.emit(
        "run.tool.started",
        {"type": "run.tool.started", "tool_name": "terminal", "status": "running"},
    )
    assert len(fired) == 1
    assert fired[0]["type"] == "run.tool.started"

    # Latched: a second tool start must not re-fire the ack.
    sink.emit(
        "run.tool.started",
        {"type": "run.tool.started", "tool_name": "terminal", "status": "running"},
    )
    assert len(fired) == 1


def test_persona_chat_prompt_allows_real_tools_and_forbids_fabrication():
    from agent_runtime.persona_runtime import _persona_chat_system_prompt

    neko = next(persona for persona in default_personas() if persona.id == "neko_supervisor")
    prompt = _persona_chat_system_prompt(neko)

    # Full tool access: actually use tools, permission-gated, no escalation nudge.
    assert "actually use your tools" in prompt
    assert "permission grant is the only gate" in prompt
    assert "hand it off as real work" not in prompt
    assert "task pipeline" not in prompt
    # Hard anti-fabrication invariant.
    assert "Never fabricate" in prompt
    assert "inventing output" in prompt


def test_mission_chat_surface_message_always_carries_operative_rules():
    from agent_runtime.persona_runtime import _mission_chat_operative_rules, _mission_chat_surface_message

    # Blank operator surface still injects the operative rules (so the
    # anti-fabrication invariant holds on the default operator channel).
    assert _mission_chat_surface_message("") == _mission_chat_operative_rules()
    assert _mission_chat_surface_message(None) == _mission_chat_operative_rules()
    assert "Never fabricate" in _mission_chat_surface_message("")

    # An operator-supplied surface prompt is layered after the rules, not instead.
    composed = _mission_chat_surface_message("Focus on the auth refresh path.")
    assert "Never fabricate" in composed
    assert composed.endswith("Focus on the auth refresh path.")


def test_mission_chat_reply_injects_operative_rules_into_system_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    neko = next(persona for persona in default_personas() if persona.id == "neko_supervisor")
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="ok",
                session_id="session_mission_chat",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    def agent_ready(_agent):
        return None

    runtime.mission_chat_reply(
        neko,
        "run echo PARITY_OK_2026",
        permission_session_id="session_mission_chat",
        agent_ready_callback=agent_ready,
    )

    system_message = captured["request"].system_message
    assert "Never fabricate" in system_message
    assert "actually use your tools" in system_message
    # And the chat-trace callback is wired on the canonical operator path too.
    assert captured["request"].progress_callback is not None
    assert captured["request"].agent_ready_callback is agent_ready


def test_profile_role_sentinel_resolves_to_supervisor_capabilities():
    # A synthetic operator-channel persona built from a raw Hermes profile carries
    # the "profile" role sentinel. It must resolve (not raise 'profile' is not a
    # valid AgentRole) and intersect down to its own configured toolsets.
    from agent_runtime.personas import (
        AgentRole,
        coerce_agent_role,
        effective_toolsets,
        role_from_persona,
    )
    from agent_runtime.models import AgentPersona

    assert coerce_agent_role("profile") == AgentRole.ALICE_SUPERVISOR

    profile = AgentPersona(
        id="profile:alice",
        display_name="Alice Agent",
        role="profile",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "session_search", "todo", "skills"],
        system_prompt_path="",
        hermes_profile="alice",
    )

    assert role_from_persona(profile) == AgentRole.ALICE_SUPERVISOR
    # The profile's own toolsets survive the supervisor-ceiling intersection.
    assert effective_toolsets(profile) == ["file", "search", "session_search", "todo", "skills"]


def test_mission_chat_reply_runs_for_profile_persona(tmp_path, monkeypatch):
    # Regression: the operator chat path (mission_chat_reply -> toolset/blocked
    # resolution -> role_from_persona) used to raise "'profile' is not a valid
    # AgentRole" for every profile persona, killing the whole turn before the model.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.models import AgentPersona

    profile = AgentPersona(
        id="profile:alice",
        display_name="Alice Agent",
        role="profile",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["file", "search", "session_search", "todo", "skills"],
        system_prompt_path="",
        # None -> binding "inherits active profile" so the test does not depend on a
        # profile on disk; the role="profile" sentinel still drives toolset resolution.
        hermes_profile=None,
    )
    captured = {}

    class CapturingRunner:
        def run(self, request):
            captured["request"] = request
            return AgentRunResult(
                final_response="hi from alice",
                session_id="session_profile_chat",
                provider="openai-codex",
                model="gpt-5.5",
                base_url=None,
                messages=[],
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
    )

    result = runtime.mission_chat_reply(
        profile, "say hi", permission_session_id="session_profile_chat"
    )

    assert result.final_response == "hi from alice"
    request = captured["request"]
    # Toolsets resolved through the supervisor ceiling; mission_goal augmentation
    # (the operator-channel goal capability) is available to the profile chat.
    assert "mission_goal" in request.enabled_toolsets
    assert set(["file", "search", "session_search", "todo", "skills"]).issubset(
        set(request.enabled_toolsets)
    )


def test_mission_chat_reply_honors_core_context_file_opt_in(tmp_path, monkeypatch):
    # Regression: the operator chat path used to hardcode skip_context_files=False,
    # forcing the process-cwd repo project docs (e.g. the 72KB hermes-agent
    # AGENTS.md, truncated to ~65K chars) into EVERY conversational turn — ~20K
    # tokens of fixed overhead regardless of persona. Operator chat must honor the
    # persona's include_core_context_files opt-in exactly like the mission-run
    # (skip_context_files) and free-chat paths. Isolated personas (the default)
    # stay lean; only an explicit opt-in loads repo docs.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    from agent_runtime.models import AgentPersona

    def _capture_skip(include_core: bool) -> bool:
        persona = AgentPersona(
            id="profile:alice",
            display_name="Alice Agent",
            role="profile",
            model="gpt-5.5",
            provider="openai-codex",
            api_mode="codex_responses",
            toolsets=["file", "search", "skills"],
            system_prompt_path="",
            hermes_profile=None,
            include_core_context_files=include_core,
        )
        captured = {}

        class CapturingRunner:
            def run(self, request):
                captured["request"] = request
                return AgentRunResult(
                    final_response="ok",
                    session_id="s",
                    provider="openai-codex",
                    model="gpt-5.5",
                    base_url=None,
                    messages=[],
                )

        runtime = GPTPersonaRuntime(
            default_provider="openai-codex", default_model="gpt-5.5", agent_runner=CapturingRunner()
        )
        runtime.mission_chat_reply(persona, "hi", permission_session_id="s")
        return captured["request"].skip_context_files

    # Default isolated persona -> context files skipped (lean turn).
    assert _capture_skip(False) is True
    # Explicit opt-in -> context files loaded (parity with the mission-run path).
    assert _capture_skip(True) is False


def test_chat_reply_without_session_records_no_trace(tmp_path, monkeypatch):
    # A sandbox chat turn with no durable session must not synthesize trace.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    FakeAIAgent.instances.clear()
    neko = next(persona for persona in default_personas() if persona.id == "neko_supervisor")

    class ToolCallingAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            # The runner still wraps a no-op budget callback; driving it must not
            # synthesize any trace because there is no session to key on.
            self.kwargs["tool_start_callback"]("tool.started", "terminal", "echo hi")
            return super().run_conversation(
                user_message=user_message, system_message=system_message, task_id=task_id
            )

    runtime = GPTPersonaRuntime(
        default_provider="openai-codex", default_model="gpt-5.5", agent_factory=ToolCallingAgent
    )

    runtime.chat_reply(neko, "hi", session_id=None)

    assert EventLog().tail(10) == []


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


def test_dev_grounds_in_current_stage_repo_not_lagging_affected_repos(monkeypatch):
    # Regression: task.affected_repos lags the active stage in a graph blueprint (it
    # holds the previous stage's repo), so grounding MUST use the current mission-plan
    # stage's repo. Here affected_repos points at the "wrong" repo; grounding must
    # ignore it and use the stage repo (hermes-agent always resolves to the harness
    # root, so this stays platform-independent).
    from agent_runtime import persona_runtime as pr

    task, run = make_task_and_run()
    task.affected_repos = ["EterniaBackend"]  # stale/lagging value from the previous stage
    dev = next(persona for persona in default_personas() if persona.id == "dev")
    dev.repo_scope = None  # no scope -> stage repo is used directly
    ctx = build_context(task, run)
    monkeypatch.setattr(pr, "current_plan_stage", lambda _task: type("S", (), {"repo": "hermes-agent"})())

    repo_ctx = pr._repo_context_for_persona(dev, ctx)

    harness_root = Path(pr.__file__).resolve().parents[1]
    assert repo_ctx is not None
    assert repo_ctx.workdir == harness_root  # stage repo won, not affected_repos[0]


def test_dev_persona_run_is_grounded_in_resolved_repo_and_loads_project_context(tmp_path, monkeypatch):
    FakeAIAgent.instances.clear()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("# Agent instructions\nUse repo-local tests.\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("# Claude instructions\nStay scoped.\n", encoding="utf-8")
    (repo / ".hermes.md").write_text("# Hermes project rules\nUse all repo guidance.\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
    assert seen["cwd"].parent == runtime_root / "wt" or seen["cwd"].parent.name == "hermes-agent-wt"
    assert seen["TERMINAL_CWD"] == str(seen["cwd"])
    assert seen["cwd"] != repo
    assert subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=seen["cwd"], text=True, stdout=subprocess.PIPE, check=True).stdout.strip() == "HEAD"
    assert "Repo-Grounded Execution" in seen["user_message"]
    assert "repo_label: repo_" in seen["user_message"]
    assert "context_loaded: .hermes.md, AGENTS.md, CLAUDE.md" in seen["user_message"]
    assert "Repo Context Excerpts (Harness-Controlled)" in seen["user_message"]
    assert "Use repo-local tests." in seen["user_message"]
    assert "Stay scoped." in seen["user_message"]


def test_backend_dev_persona_run_is_isolated_in_detached_worktree(tmp_path, monkeypatch):
    FakeAIAgent.instances.clear()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    repo = tmp_path / "backend"
    repo.mkdir()
    (repo / "manage.py").write_text("print('ok')\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (repo / "live-only.txt").write_text("pre-existing live dirt\n", encoding="utf-8")
    task, run = make_task_and_run()
    task.affected_repos = [str(repo)]
    backend_dev = next(persona for persona in default_personas() if persona.id == "backend_dev")
    backend_dev.hermes_profile = None
    backend_dev.repo_scope = None
    ctx = build_context(task, run)
    seen = {}

    class CwdAwareAgent(FakeAIAgent):
        def run_conversation(self, *, user_message, system_message, task_id):
            seen["cwd"] = Path.cwd()
            seen["user_message"] = user_message
            return super().run_conversation(user_message=user_message, system_message=system_message, task_id=task_id)

    runtime = GPTPersonaRuntime(default_provider="openai-codex", default_model="gpt-5.5", agent_factory=CwdAwareAgent)

    runtime.run_tick(backend_dev, ctx, run=run)

    assert seen["cwd"].parent == runtime_root / "wt" or seen["cwd"].parent.name == "hermes-agent-wt"
    assert seen["cwd"] != repo
    assert not (seen["cwd"] / "live-only.txt").exists()
    assert subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=seen["cwd"], text=True, stdout=subprocess.PIPE, check=True).stdout.strip() == "HEAD"
    assert run.progress["repo_execution"]["isolated"] is True
    assert run.progress["repo_execution"]["detached_head"] is True
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
    subprocess.run(["git", "init"], cwd=harness_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=harness_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=harness_repo, check=True)
    (harness_repo / "README.md").write_text("harness\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=harness_repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=harness_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "init"], cwd=backend_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=backend_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=backend_repo, check=True)
    (backend_repo / "README.md").write_text("backend\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=backend_repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=backend_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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

    assert seen["cwd"] != backend_repo
    assert seen["cwd"].parent == runtime_root / "wt" or seen["cwd"].parent.name == "hermes-agent-wt"
    assert "repo_label: backend_" in seen["user_message"]


def test_dev_persona_repo_scope_does_not_override_mismatched_task_repo(tmp_path, monkeypatch):
    FakeAIAgent.instances.clear()
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(runtime_root))
    launcher_repo = tmp_path / "launcher"
    harness_repo = tmp_path / "hermes-agent"
    launcher_repo.mkdir()
    harness_repo.mkdir()
    subprocess.run(["git", "init"], cwd=launcher_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=launcher_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=launcher_repo, check=True)
    (launcher_repo / "README.md").write_text("launcher\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=launcher_repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=launcher_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "init"], cwd=harness_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=harness_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=harness_repo, check=True)
    (harness_repo / "README.md").write_text("harness\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=harness_repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=harness_repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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

    assert seen["cwd"] != harness_repo
    assert seen["cwd"].parent == runtime_root / "wt" or seen["cwd"].parent.name == "hermes-agent-wt"
    assert "repo_label: hermes-agent_" in seen["user_message"]


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

    assert seen["cwd"] != Path(__file__).resolve().parents[2]
    assert (seen["cwd"] / ".git").exists()
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


def test_dev_grounding_overrides_default_blueprint_placeholder_repo(isolate_agent_runtime_root):
    """Live regression 2026-07-03 (task_8e1e0832): on the default two-dev
    blueprint a hermes-agent goal's backend_dev grounded in the
    backend_implementation placeholder repo (EterniaBackend), so the
    goal-named gate command ran file-not-found in the wrong tree."""

    from hermes_time import now as _now

    from agent_runtime.default_plan import ensure_default_mission_plan
    from agent_runtime.models import AgentPersona, Task
    from agent_runtime.persona_runtime import _stage_repo_scope_for_persona
    from agent_runtime.states import TaskState

    ts = _now()
    task = Task(
        id="task_ground",
        title="Audit",
        description="In the hermes-agent repo, audit and report.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        affected_repos=["hermes-agent"],
    )
    plan = ensure_default_mission_plan(task)
    plan.current_stage_id = "backend_implementation"
    task.current_stage_id = "backend_implementation"
    persona = AgentPersona(
        id="backend_dev",
        display_name="Backend Dev",
        role="backend_dev",
        model="stub",
        provider="stub",
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    ctx = type("Ctx", (), {"task": task, "current_stage": None})()

    assert _stage_repo_scope_for_persona(persona, ctx) == "hermes-agent"
