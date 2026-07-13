import os
import time
from pathlib import Path

import pytest

from agent_runtime.profile_runner import AgentRunRequest, ProfileAgentRunner, ProfileRunnerError, RunBudgetExceeded, _progress_adapter


class FakeAgent:
    last_kwargs = None
    response = None

    def __init__(self, **kwargs):
        FakeAgent.last_kwargs = kwargs
        self.session_id = kwargs.get("session_id") or "session_fake"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = "https://example.invalid/v1"
        self.tools = [
            {"type": "function", "function": {"name": tool_name}}
            for tool_name in (kwargs.get("enabled_toolsets") or [])
        ]

    def run_conversation(self, user_message, system_message=None, task_id=None):
        if FakeAgent.response is not None:
            return FakeAgent.response
        return {
            "final_response": "ok",
            "session_id": self.session_id,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "messages": [{"role": "assistant", "content": "ok"}],
            "api_calls": 1,
            "total_tokens": 3,
        }


class ProviderTimingAgent(FakeAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.status_callback = kwargs.get("status_callback")

    def run_conversation(self, user_message, system_message=None, task_id=None):
        self.status_callback(
            {
                "type": "run.progress",
                "phase": "timing",
                "step": "provider_stream_consume",
                "status": "completed",
                "duration_ms": 123,
                "timing_key": "provider_stream_consume_ms",
                "timing_values": {
                    "provider_stream_event_count": 7,
                    "provider_stream_text_delta_count": 3,
                    "provider_stream_terminal_event_ms": 122,
                    "ignored_secret": 999,
                },
            }
        )
        self.status_callback(
            {
                "type": "run.progress",
                "phase": "timing",
                "step": "conversation_request_build",
                "status": "completed",
                "duration_ms": 4,
                "timing_key": "conversation_request_build_ms",
            }
        )
        return super().run_conversation(user_message, system_message=system_message, task_id=task_id)


@pytest.fixture(autouse=True)
def reset_fake_agent_response():
    FakeAgent.response = None
    yield
    FakeAgent.response = None


def test_runner_passes_toolsets_and_blocked_tools_to_ai_agent(monkeypatch):
    monkeypatch.setattr("agent_runtime.profile_runner.resolve_runtime_provider", lambda requested, target_model: {"provider": requested, "model": target_model, "api_mode": "codex_responses"})
    runner = ProfileAgentRunner(agent_factory=FakeAgent)
    progress_events = []

    result = runner.run(
        AgentRunRequest(
            profile=None,
            provider="openai-codex",
            model="gpt-5.5",
            api_mode="codex_responses",
            enabled_toolsets=["terminal"],
            blocked_tool_names=["send_message"],
            session_id="session_1",
            user_message="hello",
            system_message="system",
            task_id="run_1",
            progress_callback=progress_events.append,
        )
    )

    assert result.final_response == "ok"
    assert result.session_id == "session_1"
    assert FakeAgent.last_kwargs["enabled_toolsets"] == ["terminal"]
    assert FakeAgent.last_kwargs["blocked_tool_names"] == ["send_message"]
    assert FakeAgent.last_kwargs["api_mode"] == "codex_responses"
    assert result.profile_timing["runtime_resolve_ms"] >= 0
    assert result.profile_timing["agent_construct_ms"] >= 0
    assert result.profile_timing["conversation_call_ms"] >= 0
    assert result.profile_timing["result_normalize_ms"] >= 0
    assert result.profile_timing["budget_checks_ms"] >= 0
    assert result.raw["profile_timing"] == result.profile_timing
    model_input = result.raw["model_input_observability"]
    assert model_input["enabled_toolsets"] == ["terminal"]
    assert model_input["tool_schema"]["final_model_tools"] == ["terminal"]
    assert model_input["tool_schema"]["tool_count"] == 1
    assert "blocked_tool_names" not in model_input
    assert "blocked_tool_names" not in model_input["tool_schema"]
    assert [
        event["timing_key"]
        for event in progress_events
        if event.get("phase") == "timing" and str(event.get("timing_key", "")).startswith("profile_")
    ] == [
        "profile_runtime_resolve_ms",
        "profile_agent_construct_ms",
        "profile_conversation_call_ms",
        "profile_result_normalize_ms",
        "profile_budget_checks_ms",
    ]


def test_runner_attaches_redaction_safe_model_input(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider",
        lambda requested, target_model: {
            "provider": requested,
            "model": target_model,
            "api_mode": "codex_responses",
        },
    )

    class PromptAgent(FakeAgent):
        def _build_system_prompt(self, system_message=None):
            return f"core prompt\napi_key: should-not-leak\n{system_message or ''}"

    result = ProfileAgentRunner(agent_factory=PromptAgent).run(
        AgentRunRequest(
            profile=None,
            provider="openai-codex",
            model="gpt-5.5",
            user_message="hello",
            system_message="surface",
        )
    )

    model_input = result.raw["model_input_observability"]
    assert model_input["skip_context_files"] is True
    assert model_input["skip_memory"] is True
    assert model_input["message_count"] == 2
    assert model_input["messages"][0]["role"] == "system"
    assert "api_key=<redacted>" in model_input["messages"][0]["content"]
    assert "should-not-leak" not in model_input["messages"][0]["content"]
    assert model_input["messages"][1]["role"] == "user"
    assert model_input["messages"][1]["content"] == "hello"


def test_runner_forwards_stream_callback_to_agent():
    captured = {}

    class StreamingAgent(FakeAgent):
        def run_conversation(
            self,
            user_message,
            system_message=None,
            task_id=None,
            stream_callback=None,
        ):
            captured["stream_callback"] = stream_callback
            if stream_callback is not None:
                stream_callback("He")
            return super().run_conversation(
                user_message,
                system_message=system_message,
                task_id=task_id,
            )

    deltas = []
    callback = deltas.append
    result = ProfileAgentRunner(agent_factory=StreamingAgent).run(
        AgentRunRequest(
            profile=None,
            user_message="hi",
            stream_callback=callback,
        )
    )

    assert result.final_response == "ok"
    assert captured["stream_callback"] is callback
    assert deltas == ["He"]


def test_runner_calls_agent_ready_callback_and_cleanup():
    events = []

    def agent_ready(agent):
        events.append(("ready", agent.session_id))
        return lambda: events.append(("cleanup", agent.session_id))

    result = ProfileAgentRunner(agent_factory=FakeAgent).run(
        AgentRunRequest(
            profile=None,
            user_message="hi",
            session_id="session_ready",
            agent_ready_callback=agent_ready,
        )
    )

    assert result.final_response == "ok"
    assert events == [("ready", "session_ready"), ("cleanup", "session_ready")]


def test_runner_persists_provider_conversation_timing_from_agent_status_callback():
    progress_events = []

    result = ProfileAgentRunner(agent_factory=ProviderTimingAgent).run(
        AgentRunRequest(
            profile=None,
            user_message="hi",
            progress_callback=progress_events.append,
        )
    )

    assert result.profile_timing["profile_provider_stream_consume_ms"] == 123
    assert result.profile_timing["profile_provider_stream_event_count"] == 7
    assert result.profile_timing["profile_provider_stream_text_delta_count"] == 3
    assert result.profile_timing["profile_provider_stream_terminal_event_ms"] == 122
    assert result.profile_timing["profile_conversation_request_build_ms"] == 4
    assert "profile_ignored_secret" not in result.profile_timing
    assert any(event.get("step") == "provider_stream_consume" for event in progress_events)


def test_runner_resolves_runtime_credentials_for_explicit_provider(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider",
        lambda requested, target_model: {
            "provider": requested,
            "model": target_model,
            "api_mode": "codex_responses",
            "api_key": "runtime-key",
            "base_url": "https://chatgpt.com/backend-api/codex/responses",
        },
    )
    runner = ProfileAgentRunner(agent_factory=FakeAgent)

    runner.run(AgentRunRequest(profile=None, provider="openai-codex", model="gpt-5.5", user_message="hello"))

    assert FakeAgent.last_kwargs["api_key"] == "runtime-key"
    assert FakeAgent.last_kwargs["base_url"] == "https://chatgpt.com/backend-api/codex/responses"
    assert FakeAgent.last_kwargs["api_mode"] == "codex_responses"


def test_runner_enters_profile_context_and_restores_environment(tmp_path, monkeypatch):
    profile_home = tmp_path / "profiles" / "pm"
    (profile_home / "home").mkdir(parents=True)
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HERMES_HOME", "before_home")
    monkeypatch.setenv("HOME", "before_user_home")
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", "before_runtime")
    monkeypatch.setattr("agent_runtime.profile_runner.profile_exists", lambda name: True)
    monkeypatch.setattr("agent_runtime.profile_runner.get_profile_dir", lambda name: profile_home)
    monkeypatch.setattr("agent_runtime.profile_runner.normalize_profile_name", lambda name: name)
    seen = {}

    class EnvAgent(FakeAgent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            seen["HERMES_HOME"] = os.environ.get("HERMES_HOME")
            seen["HOME"] = os.environ.get("HOME")
            seen["HERMES_AGENT_RUNTIME_ROOT"] = os.environ.get("HERMES_AGENT_RUNTIME_ROOT")
            return super().run_conversation(user_message, system_message=system_message, task_id=task_id)

    ProfileAgentRunner(agent_factory=EnvAgent).run(AgentRunRequest(profile="pm", runtime_root=runtime_root, user_message="hi"))

    assert seen["HERMES_HOME"] == str(profile_home)
    assert seen["HOME"] == str(profile_home / "home")
    assert seen["HERMES_AGENT_RUNTIME_ROOT"] == str(runtime_root)
    assert os.environ["HERMES_HOME"] == "before_home"
    assert os.environ["HOME"] == "before_user_home"
    assert os.environ["HERMES_AGENT_RUNTIME_ROOT"] == "before_runtime"


def test_runner_reports_missing_profile_before_agent_construction(monkeypatch):
    monkeypatch.setattr("agent_runtime.profile_runner.profile_exists", lambda name: False)
    constructed = False

    class ShouldNotConstruct(FakeAgent):
        def __init__(self, **kwargs):
            nonlocal constructed
            constructed = True
            super().__init__(**kwargs)

    runner = ProfileAgentRunner(agent_factory=ShouldNotConstruct)

    with pytest.raises(ProfileRunnerError):
        runner.run(AgentRunRequest(profile="missing", user_message="hi"))

    assert constructed is False


def test_runner_raises_failed_agent_results_before_decision_parsing(monkeypatch):
    monkeypatch.setattr("agent_runtime.profile_runner.resolve_runtime_provider", lambda requested, target_model: {"provider": requested, "model": target_model, "api_mode": "codex_responses"})
    FakeAgent.response = {
        "final_response": None,
        "messages": [],
        "provider": "openai-codex",
        "model": "gpt-5.3-codex-spark",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "completed": False,
        "failed": True,
        "error": "HTTP 401: Provided authentication token is expired. Please try signing in again.",
    }

    with pytest.raises(ProfileRunnerError, match="HTTP 401"):
        ProfileAgentRunner(agent_factory=FakeAgent).run(AgentRunRequest(profile=None, provider="openai-codex", user_message="hi"))


def test_progress_adapter_enriches_tool_progress_started_event():
    events = []
    cb = _progress_adapter(events.append, "run.progress")

    cb("tool.started", "terminal", "run tests", {"command": "pytest"})

    assert events == [
        {
            "type": "run.progress",
            "phase": "tool",
            "step": "tool_started",
            "tool_name": "terminal",
            "status": "started",
            "summary": "Started tool terminal: pytest",
            "command_label": "pytest",
            "command_full": "pytest",
        }
    ]


def test_progress_adapter_enriches_tool_lifecycle_started_event():
    events = []
    cb = _progress_adapter(events.append, "run.tool.started")

    cb("call_1", "terminal", {"command": "pytest"})

    assert events == [
        {
            "type": "run.tool.started",
            "phase": "tool",
            "step": "tool_started",
            "tool_name": "terminal",
            "status": "started",
            "summary": "Started tool terminal: pytest",
            "command_label": "pytest",
            "command_full": "pytest",
        }
    ]


def test_progress_adapter_enriches_tool_completed_event_with_duration_and_status():
    events = []
    cb = _progress_adapter(events.append, "run.progress")

    cb("tool.completed", "terminal", None, None, duration=1.25, is_error=False, result={"exit_code": 0})

    assert events == [
        {
            "type": "run.progress",
            "phase": "tool",
            "step": "tool_finished",
            "tool_name": "terminal",
            "status": "passed",
            "duration_ms": 1250,
            "exit_code": 0,
            "summary": "Finished tool terminal: passed in 1250ms",
        }
    ]


def test_progress_adapter_enriches_tool_lifecycle_finished_event():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    cb("call_1", "terminal", {"command": "pytest"}, {"exit_code": 0})

    assert events == [
        {
            "type": "run.tool.finished",
            "phase": "tool",
            "step": "tool_finished",
            "tool_name": "terminal",
            "status": "passed",
            "exit_code": 0,
            "summary": "Finished tool terminal: passed",
            "command_label": "pytest",
            "command_full": "pytest",
        }
    ]


def test_progress_adapter_surfaces_operator_command_and_scrubbed_output():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    cb(
        "call_1",
        "terminal",
        {"command": "rg --files /home/x/foo"},
        {"exit_code": 1, "output": "line1\napi_key=SECRET\n/home/x/foo/bar.dart"},
    )

    payload = events[0]
    # Operator command keeps the path (unlike the path-stripped command_label).
    assert payload["command_full"] == "rg --files /home/x/foo"
    assert "command_label" not in payload  # path-stripped variant drops it
    # Output is surfaced for terminal-class tools, with the secret LINE redacted
    # and the path line kept.
    assert "api_key=SECRET" not in payload["output"]
    assert "[redacted line" in payload["output"]
    assert "/home/x/foo/bar.dart" in payload["output"]
    assert payload["exit_code"] == 1


def test_progress_adapter_does_not_surface_output_for_non_terminal_tools():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    cb("call_1", "read_file", {"path": "mission.md"}, {"output": "file body here"})

    assert "output" not in events[0]


def test_progress_adapter_records_target_for_read_and_search_tools():
    events = []
    cb = _progress_adapter(events.append, "run.tool.started")

    cb("call_1", "read_file", {"path": "lib/features/library/petdex_menu.dart"})
    cb("call_2", "search_files", {"pattern": "PetdexTile", "path": "lib/features/library"})

    read_payload, search_payload = events
    assert read_payload["target_label"] == "lib/features/library/petdex_menu.dart"
    assert read_payload["summary"] == "Started tool read_file: lib/features/library/petdex_menu.dart"
    assert search_payload["target_label"] == "PetdexTile in lib/features/library"
    assert search_payload["summary"] == "Started tool search_files: PetdexTile in lib/features/library"


def test_progress_adapter_recovers_patch_files_from_diff_headers():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    patch_text = (
        "*** Begin Patch\n"
        "*** Update File: lib/features/library/petdex_menu.dart\n"
        "@@\n-old\n+new\n"
        "*** Add File: test/features/library/petdex_menu_test.dart\n"
        "+content\n"
        "*** End Patch\n"
    )
    # Result carries NO file list — the live failure shape ("changed-file list
    # unavailable"); the diff headers are the only record of the edit.
    cb("call_1", "patch", {"patch": patch_text}, {"success": True})

    payload = events[0]
    assert payload["changed_paths"] == [
        "lib/features/library/petdex_menu.dart",
        "test/features/library/petdex_menu_test.dart",
    ]
    assert payload["changed_files"] == ["petdex_menu.dart", "petdex_menu_test.dart"]
    assert payload["summary"].startswith("Patched 2 files:")


def test_progress_adapter_recovers_patch_files_from_unified_diff():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    diff_text = (
        "diff --git a/lib/a.dart b/lib/a.dart\n"
        "--- a/lib/a.dart\n"
        "+++ b/lib/a.dart\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    cb("call_1", "apply_patch", {"diff": diff_text}, {"success": True})

    assert events[0]["changed_paths"] == ["lib/a.dart"]
    assert events[0]["changed_files"] == ["a.dart"]


def test_progress_adapter_marks_string_error_lifecycle_result_failed():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    cb("call_1", "terminal", {"command": "pytest"}, "ERROR: command failed")

    assert events == [
        {
            "type": "run.tool.finished",
            "phase": "tool",
            "step": "tool_finished",
            "tool_name": "terminal",
            "status": "failed",
            "summary": "Finished tool terminal: failed",
            "command_label": "pytest",
            "command_full": "pytest",
        }
    ]


def test_progress_adapter_summarizes_patch_tool_result_without_raw_diff():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    cb(
        "call_1",
        "patch",
        {"path": "private/absolute/path.dart"},
        {
            "success": True,
            "files_modified": [
                "lib/features/mission_control/mission_control_page.dart",
                "test/features/mission_control/mission_control_page_test.dart",
            ],
            "diff": "SECRET raw diff should never be persisted",
        },
    )

    assert events == [
        {
            "type": "run.tool.finished",
            "phase": "dev_work",
            "step": "patch",
            "tool_name": "patch",
            "status": "passed",
            "summary": "Patched 2 files: mission_control_page.dart, mission_control_page_test.dart",
            "detail": "Changed files: mission_control_page.dart, mission_control_page_test.dart",
            "patch_summary": "Patched 2 files",
            "changed_files": ["mission_control_page.dart", "mission_control_page_test.dart"],
            # Operator lane keeps the repo-RELATIVE paths (absolute paths and
            # secret-looking names never make it in — see the write_file test).
            "changed_paths": [
                "lib/features/mission_control/mission_control_page.dart",
                "test/features/mission_control/mission_control_page_test.dart",
            ],
            "files_touched": 2,
        }
    ]
    encoded = repr(events)
    assert "SECRET raw diff" not in encoded
    assert "private/absolute/path.dart" not in encoded


def test_progress_adapter_summarizes_write_file_as_dev_work_without_absolute_path():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    cb(
        "call_2",
        "write_file",
        {"path": "C:/Users/beast/project/lib/private_token.dart"},
        {"success": True, "path": "C:/Users/beast/project/lib/customize_order.dart"},
    )

    assert events == [
        {
            "type": "run.tool.finished",
            "phase": "dev_work",
            "step": "write_file",
            "tool_name": "write_file",
            "status": "passed",
            "summary": "Wrote code file: customize_order.dart",
            "detail": "Changed files: customize_order.dart",
            "file_summary": "Wrote code file",
            "changed_files": ["customize_order.dart"],
            "files_touched": 1,
        }
    ]
    encoded = repr(events)
    assert "C:/Users" not in encoded
    assert "private_token" not in encoded


def test_progress_adapter_sanitizes_sensitive_tool_names_and_summaries():
    events = []
    cb = _progress_adapter(events.append, "run.progress")

    cb("tool.started", "C:/Users/example/secret_token.txt", "Authorization bearer token", {"path": "C:/Users/example/secret_token.txt"})

    assert events == [
        {
            "type": "run.progress",
            "phase": "tool",
            "step": "tool_started",
            "status": "started",
            "summary": "Started tool",
        }
    ]


def test_progress_adapter_emits_single_repeated_tool_warning():
    events = []
    cb = _progress_adapter(events.append, "run.progress")

    for _ in range(7):
        cb("tool.started", "terminal", "same", {})

    warnings = [event for event in events if event.get("phase") == "runaway_warning"]
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "warning"
    assert warnings[0]["step"] == "repeated_tool_event"
    assert warnings[0]["tool_name"] == "terminal"


def test_progress_adapter_flags_repeated_skill_view_as_skill_loading_fanout():
    events = []
    cb = _progress_adapter(events.append, "run.progress")

    for _ in range(4):
        cb("tool.started", "skill_view", "same", {})

    warnings = [event for event in events if event.get("phase") == "runaway_warning"]
    assert len(warnings) == 1
    assert warnings[0]["step"] == "skill_loading_fanout"
    assert warnings[0]["tool_name"] == "skill_view"
    assert "stop loading additional skills" in warnings[0]["summary"].lower()


def test_progress_adapter_can_stop_repeated_read_search_loops_when_enabled():
    events = []
    cb = _progress_adapter(events.append, "run.progress", stop_on_repeated_read_search=True)

    with pytest.raises(RunBudgetExceeded, match="repeated read/search loop"):
        for _ in range(6):
            cb("tool.started", "read_file", "same", {})

    warnings = [event for event in events if event.get("phase") == "runaway_warning"]
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "critical"
    assert warnings[0]["step"] == "repeated_read_search_loop"
    assert warnings[0]["next_expected"] == "bounded_verdict_proof_handoff_or_exact_blocker"
    assert "bounded verdict" in warnings[0]["summary"]


def test_progress_adapter_allows_repeated_read_search_after_patch_progress():
    events = []
    cb = _progress_adapter(events.append, "run.progress", stop_on_repeated_read_search=True)

    cb("tool.completed", "patch", {}, {"success": True})
    for _ in range(8):
        cb("tool.started", "read_file", "same", {})

    critical_warnings = [
        event
        for event in events
        if event.get("phase") == "runaway_warning"
        and event.get("step") == "repeated_read_search_loop"
        and event.get("severity") == "critical"
    ]
    assert critical_warnings == []


def test_progress_adapter_allows_repeated_read_search_with_prior_patch_progress():
    events = []
    cb = _progress_adapter(
        events.append,
        "run.progress",
        stop_on_repeated_read_search=True,
        tool_budget_limits={"has_patch_progress": True},
    )

    for _ in range(8):
        cb("tool.started", "read_file", "same", {})

    critical_warnings = [
        event
        for event in events
        if event.get("phase") == "runaway_warning"
        and event.get("step") == "repeated_read_search_loop"
        and event.get("severity") == "critical"
    ]
    assert critical_warnings == []


def test_progress_adapter_stops_mixed_read_search_budget_when_enabled():
    events = []
    cb = _progress_adapter(
        events.append,
        "run.tool.finished",
        stop_on_repeated_read_search=True,
        tool_budget_limits={"read_search_limit": 2},
    )

    cb("call_1", "search_files", {}, {"success": True})
    with pytest.raises(RunBudgetExceeded, match="aggregate read/search budget exceeded"):
        cb("call_2", "read_file", {}, {"success": True})

    warnings = [event for event in events if event.get("phase") == "runaway_warning"]
    assert len(warnings) == 1
    assert warnings[0]["step"] == "repeated_read_search_loop"
    assert warnings[0]["read_search_count"] == 2
    assert warnings[0]["read_search_limit"] == 2


def test_progress_adapter_summarizes_reasoning_progress_without_raw_private_text():
    events = []
    cb = _progress_adapter(events.append, "run.progress")

    cb(
        "reasoning.available",
        "Comparing proof gaps before QA handoff.",
        "C:/Users/beast/private_token.txt must not leak",
    )

    assert events == [
        {
            "type": "run.progress",
            "phase": "thinking_process",
            "step": "reasoning_summary",
            "status": "running",
            "summary": "Agent thinking process updated",
            "reasoning_summary": "Comparing proof gaps before QA handoff.",
        }
    ]


def test_progress_adapter_reads_reasoning_text_from_structured_callback_shape():
    """The conversation loop emits ("reasoning.available", "_thinking", text,
    None) — args[1] is the channel placeholder, args[2] is the reasoning. The
    adapter must record the text (paths included), never the placeholder, and
    must mask secret-bearing lines in place instead of dropping the summary."""

    events = []
    cb = _progress_adapter(events.append, "run.progress")

    cb(
        "reasoning.available",
        "_thinking",
        "Reviewing docs/scratch/goal_turn_probe.md before the echo proof.\nexport api_key=sk-live-12345",
        None,
    )

    assert len(events) == 1
    summary = events[0]["reasoning_summary"]
    assert "_thinking" not in summary
    assert "docs/scratch/goal_turn_probe.md" in summary
    assert "sk-live-12345" not in repr(events)
    assert "[redacted line — contained a secret]" in summary


class SlowInterruptibleAgent(FakeAgent):
    interrupted = False

    def interrupt(self, message=None):
        type(self).interrupted = True

    def run_conversation(self, user_message, system_message=None, task_id=None):
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if type(self).interrupted:
                return super().run_conversation(user_message, system_message=system_message, task_id=task_id)
            time.sleep(0.005)
        return super().run_conversation(user_message, system_message=system_message, task_id=task_id)


def test_runner_interrupts_agent_when_wall_clock_budget_exceeded():
    SlowInterruptibleAgent.interrupted = False
    runner = ProfileAgentRunner(agent_factory=SlowInterruptibleAgent)

    started = time.monotonic()
    with pytest.raises(RunBudgetExceeded, match="wall_seconds=0.01"):
        runner.run(AgentRunRequest(profile=None, user_message="hi", max_wall_seconds=0.01))

    assert time.monotonic() - started < 0.2
    assert SlowInterruptibleAgent.interrupted is True


class SwallowingCallbackLoopAgent(FakeAgent):
    interrupted = False
    interrupt_message = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_complete_callback = kwargs.get("tool_complete_callback")

    def interrupt(self, message=None):
        type(self).interrupted = True
        type(self).interrupt_message = message

    def run_conversation(self, user_message, system_message=None, task_id=None):
        for index, tool_name in enumerate(["search_files", "read_file", "search_files"], start=1):
            try:
                self.tool_complete_callback(f"call_{index}", tool_name, {}, {"success": True})
            except Exception:
                pass
            if type(self).interrupted:
                return {
                    "final_response": "Operation interrupted by Harness budget guard.",
                    "session_id": self.session_id,
                    "provider": self.provider,
                    "model": self.model,
                    "base_url": self.base_url,
                    "messages": [],
                    "api_calls": index,
                    "total_tokens": 100,
                    "interrupted": True,
                }
        return super().run_conversation(user_message, system_message=system_message, task_id=task_id)


def test_runner_interrupts_agent_when_mixed_read_search_budget_is_swallowed():
    SwallowingCallbackLoopAgent.interrupted = False
    SwallowingCallbackLoopAgent.interrupt_message = None

    with pytest.raises(RunBudgetExceeded, match="aggregate read/search budget exceeded"):
        ProfileAgentRunner(agent_factory=SwallowingCallbackLoopAgent).run(
            AgentRunRequest(
                profile=None,
                user_message="hi",
                stop_on_repeated_read_search=True,
                tool_budget_limits={"read_search_limit": 2},
            )
        )

    assert SwallowingCallbackLoopAgent.interrupted is True
    assert "aggregate read/search budget exceeded" in SwallowingCallbackLoopAgent.interrupt_message


def test_runner_rejects_result_that_exceeds_api_call_budget():
    FakeAgent.response = {
        "final_response": "ok",
        "messages": [],
        "api_calls": 6,
        "total_tokens": 10,
    }

    with pytest.raises(RunBudgetExceeded, match="api_calls=6/5"):
        ProfileAgentRunner(agent_factory=FakeAgent).run(
            AgentRunRequest(profile=None, user_message="hi", max_api_calls=5)
        )


def test_runner_rejects_result_that_exceeds_total_token_budget():
    FakeAgent.response = {
        "final_response": "ok",
        "messages": [],
        "api_calls": 1,
        "total_tokens": 101,
    }

    with pytest.raises(RunBudgetExceeded, match="total_tokens=101/100"):
        ProfileAgentRunner(agent_factory=FakeAgent).run(
            AgentRunRequest(profile=None, user_message="hi", max_total_tokens=100)
        )


def test_runner_emits_budget_pressure_warning_before_total_token_cap():
    events = []
    FakeAgent.response = {
        "final_response": "ok",
        "messages": [],
        "api_calls": 1,
        "total_tokens": 81,
        "session_id": "session_budget_pressure",
    }

    result = ProfileAgentRunner(agent_factory=FakeAgent).run(
        AgentRunRequest(
            profile=None,
            user_message="hi",
            max_total_tokens=100,
            progress_callback=events.append,
        )
    )

    assert result.final_response == "ok"
    warnings = [event for event in events if event.get("step") == "budget_pressure"]
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "warning"
    assert warnings[0]["next_expected"] == "proof_or_block_now"
    assert warnings[0]["budget_kind"] == "total_tokens"


def test_runner_allows_under_budget_result():
    FakeAgent.response = {
        "final_response": "ok",
        "messages": [],
        "api_calls": 5,
        "total_tokens": 100,
    }

    result = ProfileAgentRunner(agent_factory=FakeAgent).run(
        AgentRunRequest(profile=None, user_message="hi", max_api_calls=5, max_total_tokens=100, max_wall_seconds=1)
    )

    assert result.final_response == "ok"


def test_runner_executes_agent_inside_requested_workdir_and_restores_caller(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setenv("TERMINAL_CWD", "before_terminal_cwd")
    caller_cwd = Path.cwd()
    seen = {}

    class CwdAgent(FakeAgent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            seen["cwd"] = Path.cwd()
            seen["TERMINAL_CWD"] = os.environ.get("TERMINAL_CWD")
            return super().run_conversation(user_message, system_message=system_message, task_id=task_id)

    ProfileAgentRunner(agent_factory=CwdAgent).run(AgentRunRequest(profile=None, user_message="hi", workdir=repo))

    assert seen["cwd"] == repo
    assert seen["TERMINAL_CWD"] == str(repo)
    assert Path.cwd() == caller_cwd
    assert os.environ["TERMINAL_CWD"] == "before_terminal_cwd"


def test_runner_rejects_missing_workdir_before_agent_construction(tmp_path):
    constructed = False

    class ShouldNotConstruct(FakeAgent):
        def __init__(self, **kwargs):
            nonlocal constructed
            constructed = True
            super().__init__(**kwargs)

    with pytest.raises(ProfileRunnerError, match="workdir"):
        ProfileAgentRunner(agent_factory=ShouldNotConstruct).run(
            AgentRunRequest(profile=None, user_message="hi", workdir=tmp_path / "missing")
        )

    assert constructed is False


def test_tool_lifecycle_finished_marks_timeout_exit_code_as_failed():
    events = []
    cb = _progress_adapter(events.append, "run.tool.finished")

    cb("call_1", "terminal", {"command": "pytest"}, {"exit_code": 124, "timed_out": True})

    assert events == [
        {
            "type": "run.tool.finished",
            "phase": "tool",
            "step": "tool_finished",
            "tool_name": "terminal",
            "status": "failed",
            "exit_code": 124,
            "summary": "Finished tool terminal: failed",
            "command_label": "pytest",
            "command_full": "pytest",
        }
    ]


def test_normalize_result_carries_canonical_cache_and_reasoning():
    # finalize_turn emits the full canonical usage in the run result dict; the
    # normalizer must carry the cache/reasoning buckets through so downstream
    # accounting (persona-chat bound session, Launcher cache indicator) reads a
    # complete record rather than a lossy input/output-only subset.
    from agent_runtime.profile_runner import AgentRunResult, _normalize_result

    class _Agent:
        session_id = "scratch_1"
        provider = "openai-codex"
        model = "gpt-5.6-luna"
        base_url = "https://example.invalid/v1"

    result = _normalize_result(
        {
            "final_response": "hi",
            "messages": [],
            "api_calls": 2,
            "input_tokens": 25225,
            "output_tokens": 36,
            "total_tokens": 25261,
            "cache_read_tokens": 1432576,
            "cache_write_tokens": 300,
            "reasoning_tokens": 128,
        },
        agent=_Agent(),
    )

    assert isinstance(result, AgentRunResult)
    assert result.input_tokens == 25225  # uncached, full-price remainder
    assert result.cache_read_tokens == 1432576
    assert result.cache_write_tokens == 300
    assert result.reasoning_tokens == 128
