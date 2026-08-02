import os
import time
from pathlib import Path

import pytest

import json

from agent_runtime.profile_runner import (
    AgentRunRequest,
    ProfileAgentRunner,
    ProfileRunnerError,
    RunBudgetExceeded,
    _agent_chat_target_label,
    _progress_adapter,
    _tool_finished_payload,
    _tool_started_payload,
    _todo_items_from,
    _todo_state_payload,
    _TODO_STATE_MAX_CONTENT,
    _TODO_STATE_MAX_ITEMS,
)


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
    # T6c: the requested blocks are preserved AND the fork registry-hygiene set
    # (kanban + feishu) is unioned in at agent construction, so no lane can resolve
    # those toolsets. delegate_task / memory are deliberately NOT force-blocked
    # here (operator ruling: keep them registered).
    from agent_runtime.personas import REGISTRY_HYGIENE_BLOCKED_TOOLS

    passed_blocked = FakeAgent.last_kwargs["blocked_tool_names"]
    assert "send_message" in passed_blocked
    assert REGISTRY_HYGIENE_BLOCKED_TOOLS.issubset(set(passed_blocked))
    assert "delegate_task" not in passed_blocked
    assert "memory" not in passed_blocked
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


def test_runner_binds_and_resets_skill_runtime_surface():
    from agent.skill_utils import current_skill_runtime_context

    seen = []

    class ContextAgent(FakeAgent):
        def __init__(self, **kwargs):
            seen.append(current_skill_runtime_context())
            super().__init__(**kwargs)

        def run_conversation(self, user_message, system_message=None, task_id=None):
            seen.append(current_skill_runtime_context())
            return super().run_conversation(
                user_message,
                system_message=system_message,
                task_id=task_id,
            )

    result = ProfileAgentRunner(agent_factory=ContextAgent).run(
        AgentRunRequest(
            profile=None,
            user_message="route skills",
            skill_surface="mission_chat",
            skill_root_node_mode=False,
        )
    )

    assert result.final_response == "ok"
    assert seen == [("mission_chat", False), ("mission_chat", False)]
    assert current_skill_runtime_context() == (None, False)


def test_persona_chat_runner_forces_native_compression_tip_rotation():
    class CompressionAgent(FakeAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.compression_in_place = True

        def run_conversation(self, user_message, system_message=None, task_id=None):
            assert self.compression_in_place is False
            assert self._persona_chat_root_session_id == "chat_root"
            return super().run_conversation(
                user_message,
                system_message=system_message,
                task_id=task_id,
            )

    result = ProfileAgentRunner(agent_factory=CompressionAgent).run(
        AgentRunRequest(
            profile=None,
            user_message="compress safely",
            session_id="chat_tip",
            root_chat_session_id="chat_root",
        )
    )

    assert result.final_response == "ok"


def test_persona_chat_runner_applies_one_turn_compression_proof_overrides():
    class Compressor:
        context_length = 400_000
        threshold_percent = 0.5
        threshold_tokens = 200_000
        protect_first_n = 3
        protect_last_n = 12

    class CompressionAgent(FakeAgent):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.compression_in_place = True
            self.context_compressor = Compressor()

        def run_conversation(self, user_message, system_message=None, task_id=None):
            assert self.context_compressor.threshold_tokens == 500
            assert self.context_compressor.threshold_percent == 0.00125
            assert self.context_compressor.protect_first_n == 0
            assert self.context_compressor.protect_last_n == 1
            return super().run_conversation(
                user_message,
                system_message=system_message,
                task_id=task_id,
            )

    result = ProfileAgentRunner(agent_factory=CompressionAgent).run(
        AgentRunRequest(
            profile=None,
            user_message="compress safely",
            session_id="chat_tip",
            root_chat_session_id="chat_root",
            compression_threshold_tokens_override=500,
            compression_protect_first_n_override=0,
            compression_protect_last_n_override=1,
        )
    )

    assert result.final_response == "ok"


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


def test_system_prompt_section_receipts_address_exact_three_tiers(monkeypatch):
    from types import SimpleNamespace

    from agent_runtime.profile_runner import _system_prompt_section_receipts
    import agent.system_prompt as system_prompt_module

    parts = {
        "stable": "stable foundation",
        "context": "mission context\nwith two lines",
        "volatile": "volatile profile",
    }
    monkeypatch.setattr(
        system_prompt_module,
        "build_system_prompt_parts",
        lambda agent, system_message=None: parts,
    )
    joined = "\n\n".join(parts.values())

    sections = _system_prompt_section_receipts(
        agent=SimpleNamespace(),
        system_message="mission context",
        system_prompt=joined,
        captured_content=joined,
    )

    assert [section["kind"] for section in sections] == [
        "stable",
        "context",
        "volatile",
    ]
    assert [
        joined[section["start_char"] : section["end_char"]]
        for section in sections
    ] == list(parts.values())
    assert all(section["truncated"] is False for section in sections)


def test_system_prompt_section_receipts_fail_closed_on_cached_prompt_drift(
    monkeypatch,
):
    from types import SimpleNamespace

    from agent_runtime.profile_runner import _system_prompt_section_receipts
    import agent.system_prompt as system_prompt_module

    monkeypatch.setattr(
        system_prompt_module,
        "build_system_prompt_parts",
        lambda agent, system_message=None: {
            "stable": "new stable",
            "context": "new context",
            "volatile": "new volatile",
        },
    )

    assert (
        _system_prompt_section_receipts(
            agent=SimpleNamespace(),
            system_message="new context",
            system_prompt="cached prompt from an older turn",
            captured_content="cached prompt from an older turn",
        )
        == []
    )


def test_runner_attaches_agent_owned_final_cache_routing_observability(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider",
        lambda requested, target_model: {
            "provider": requested,
            "model": target_model,
            "api_mode": "codex_responses",
        },
    )

    class CacheRoutingAgent(FakeAgent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            self._last_cache_routing_observability = {
                "schema_version": 1,
                "backend": "openai_codex",
                "prompt_cache_key_present": True,
                "prompt_cache_key_source": "static_prefix",
                "prompt_cache_key_fingerprint": f"sha256:{'a' * 64}",
                "cache_scope_source": "cache_scope_id",
                "session_header_present": True,
                "session_header_fingerprint": f"sha256:{'b' * 64}",
                "client_request_header_present": True,
                "client_request_header_fingerprint": f"sha256:{'b' * 64}",
                "scope_headers_match": True,
                "raw_values_omitted": True,
            }
            return super().run_conversation(
                user_message,
                system_message=system_message,
                task_id=task_id,
            )

    result = ProfileAgentRunner(agent_factory=CacheRoutingAgent).run(
        AgentRunRequest(profile=None, user_message="hello")
    )

    routing = result.raw["model_input_observability"]["cache_routing"]
    assert routing["backend"] == "openai_codex"
    assert routing["prompt_cache_key_fingerprint"] == f"sha256:{'a' * 64}"
    assert routing["scope_headers_match"] is True


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


def test_progress_adapter_agent_chat_send_carries_structured_dispatch_fields():
    events = []
    cb = _progress_adapter(events.append, "run.tool.started")

    order = (
        "From Neko Mission Lead: run a bounded backend health check.\n"
        "Keep it lightweight; no repo commits.\n"
        "Report the one-line result back."
    )
    invocation = {"persona_id": "backend_dev", "message": order}
    cb("call_1", "agent_chat_send", invocation)

    payload = events[0]
    # G2 structured fields: the target chip + the FULL order, newlines preserved
    # (NOT whitespace-collapsed like the prose target_label).
    assert payload["dispatch_target"] == "backend_dev"
    assert payload["dispatch_order"] == order
    assert "\n" in payload["dispatch_order"]
    # Backward-compat: the prose target_label + summary are byte-identical to
    # what the (unchanged) label helper produces — the new keys are additive.
    expected_label = _agent_chat_target_label("agent_chat_send", invocation)
    assert payload["target_label"] == expected_label
    assert payload["summary"] == f"Started tool agent_chat_send: {expected_label}"
    # The prose label is still a single, 90-char-excerpted line.
    assert "\n" not in payload["target_label"]
    assert len(payload["target_label"]) <= len("→ backend_dev: ") + 90


def test_agent_chat_dispatch_order_drops_secret_lines_and_target_is_capped():
    events = []
    cb = _progress_adapter(events.append, "run.tool.started")

    order = "Line one is fine.\napi_key=SUPERSECRET must be dropped\nLine three is fine."
    cb("call_1", "agent_chat_send", {"persona_id": "q" * 200, "message": order})

    payload = events[0]
    # Secret-bearing line dropped whole; the surrounding lines survive in order.
    assert payload["dispatch_order"] == "Line one is fine.\nLine three is fine."
    assert "SUPERSECRET" not in payload["dispatch_order"]
    # Target persona capped at 120.
    assert len(payload["dispatch_target"]) == 120


def test_agent_chat_dispatch_order_caps_at_1500_with_ellipsis():
    events = []
    cb = _progress_adapter(events.append, "run.tool.started")

    cb("call_1", "agent_chat_send", {"persona_id": "dev", "message": "x" * 4000})

    order = events[0]["dispatch_order"]
    assert len(order) == 1500
    assert order.endswith("…")
    assert order[:1499] == "x" * 1499


def test_non_dispatch_tool_has_no_dispatch_fields():
    events = []
    cb = _progress_adapter(events.append, "run.tool.started")

    cb("call_1", "read_file", {"path": "lib/features/library/petdex_menu.dart"})

    assert "dispatch_target" not in events[0]
    assert "dispatch_order" not in events[0]


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
            # A string lifecycle result is the error itself — real signal for
            # the console's Result dropdown (scrubbed + bounded like all IO).
            "tool_result": "ERROR: command failed",
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
            # No output tail came back; the envelope remainder (minus the
            # exit_code echo) is the honest result record for the dropdown.
            "tool_result": "timed_out: true",
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


# ── T7: todo checklist state on the finished event ───────────────────────────


def _todo_result(items):
    """The JSON-string result shape `todo_tool` returns."""
    return json.dumps({"todos": items, "summary": {"total": len(items)}})


def test_todo_state_payload_from_json_string_result():
    items = [
        {"id": "1", "content": "Verify the data lane", "status": "completed"},
        {"id": "2", "content": "Ship the checklist panel", "status": "in_progress"},
        {"id": "3", "content": "Land it", "status": "pending"},
    ]
    payload = _todo_state_payload("todo", _todo_result(items), invocation={"todos": items})
    assert payload == items


def test_todo_state_payload_from_dict_result():
    items = [{"id": "a", "content": "one", "status": "pending"}]
    payload = _todo_state_payload("todo", {"todos": items}, invocation=None)
    assert payload == items


def test_todo_state_payload_falls_back_to_invocation_when_result_unparseable():
    items = [{"id": "x", "content": "draft", "status": "pending"}]
    payload = _todo_state_payload("todo", "not-json{", invocation={"todos": items})
    assert payload == items


def test_todo_state_payload_returns_none_for_non_todo_tool():
    assert _todo_state_payload("terminal", _todo_result([{"id": "1", "content": "c", "status": "pending"}]), None) is None


def test_todo_state_payload_returns_none_when_unrecoverable():
    # Absence (None) is reserved for a non-todo tool or an unrecoverable payload:
    # neither the result nor the invocation yields a list. A cleared list is a
    # DIFFERENT case (see test_todo_state_payload_emits_explicit_empty_on_clear).
    assert _todo_state_payload("todo", "just a string", invocation=None) is None
    assert _todo_state_payload("todo", {"summary": {}}, invocation=None) is None
    assert _todo_state_payload("todo", "not-json{", invocation="also bad") is None


def test_todo_state_payload_emits_explicit_empty_on_clear():
    # T9d: a todo WRITE whose resulting list is empty emits an explicit `[]` — the
    # cleared-checklist signal the launcher resolver uses to hide the panel —
    # distinct from absence (None). Both the JSON-string and bare-list shapes of
    # a cleared result recover an empty list.
    assert _todo_state_payload("todo", _todo_result([]), invocation=None) == []
    assert _todo_state_payload("todo", {"todos": []}, invocation=None) == []
    assert _todo_state_payload("todo", "[]", invocation=None) == []
    # A cleared result is NOT masked by a stale invocation fallback: the result's
    # empty list wins (the fallback only triggers on an unparseable result).
    assert _todo_state_payload(
        "todo", _todo_result([]), invocation={"todos": [{"id": "1", "content": "stale", "status": "pending"}]}
    ) == []
    # But a non-todo tool with an empty result is still absent, never `[]`.
    assert _todo_state_payload("terminal", _todo_result([]), invocation=None) is None


def test_todo_state_payload_normalizes_status_and_caps_content():
    long_content = "x" * (_TODO_STATE_MAX_CONTENT + 50)
    items = [
        {"id": "1", "content": long_content, "status": "bogus"},
        {"id": "", "content": "", "status": "completed"},
    ]
    payload = _todo_state_payload("todo", _todo_result(items), None)
    assert payload is not None
    assert payload[0]["status"] == "pending"  # unknown → pending
    assert len(payload[0]["content"]) == _TODO_STATE_MAX_CONTENT
    assert payload[0]["content"].endswith("…")
    assert payload[1]["id"] == "?"  # empty id → placeholder
    assert payload[1]["content"] == "(no description)"


def test_todo_state_payload_caps_item_count():
    items = [{"id": str(i), "content": f"item {i}", "status": "pending"} for i in range(_TODO_STATE_MAX_ITEMS + 20)]
    payload = _todo_state_payload("todo", _todo_result(items), None)
    assert len(payload) == _TODO_STATE_MAX_ITEMS


def test_todo_state_payload_collapses_whitespace_matching_persist_lane():
    # T9c: multi-line / multi-space todo content must be byte-identical on the
    # live `tool.finished` lane (the producer output) and the reloaded turn-store
    # lane (which re-bounds via `mission_chat_turns._safe_todo_state`). The
    # producer now collapses whitespace to the persisted `safe_assignment_text`
    # shape, so the persist re-run is a no-op and the two lanes match.
    from agent_runtime.mission_chat_turns import _safe_todo_state

    items = [
        {"id": "1", "content": "line one\nline two\n\n  trailing", "status": "in_progress"},
        {"id": "2", "content": "\ttabbed   and   spaced   ", "status": "pending"},
    ]
    live = _todo_state_payload("todo", _todo_result(items), None)
    assert live is not None
    # Whitespace collapsed (no raw newlines/tabs/double-spaces survive).
    assert live[0]["content"] == "line one line two trailing"
    assert live[1]["content"] == "tabbed and spaced"
    # The reloaded/persisted lane is byte-identical to the live lane.
    reloaded = _safe_todo_state(live)
    assert reloaded == live


def test_todo_state_payload_ellipsis_survives_persist_rerun():
    # The over-cap `…` marker must also be lane-stable: the persist re-bound
    # runs safe_assignment_text over the producer's already-collapsed,
    # ellipsis-terminated content and leaves it byte-identical.
    from agent_runtime.mission_chat_turns import _safe_todo_state

    items = [{"id": "1", "content": "word " * 100, "status": "pending"}]
    live = _todo_state_payload("todo", _todo_result(items), None)
    assert live[0]["content"].endswith("…")
    assert len(live[0]["content"]) == _TODO_STATE_MAX_CONTENT
    assert _safe_todo_state(live) == live


def test_todo_items_from_accepts_bare_list():
    items = [{"id": "1", "content": "c", "status": "pending"}]
    assert _todo_items_from(items) == items
    assert _todo_items_from(None) is None
    assert _todo_items_from(42) is None


def test_tool_finished_payload_carries_todo_state():
    items = [{"id": "1", "content": "do it", "status": "in_progress"}]
    payload = _tool_finished_payload(
        "run.tool.finished",
        "todo",
        duration=None,
        is_error=False,
        result=_todo_result(items),
        invocation={"todos": items},
    )
    assert payload["todo_state"] == items


def test_tool_finished_payload_omits_todo_state_for_other_tools():
    payload = _tool_finished_payload(
        "run.tool.finished",
        "skill_view",
        duration=None,
        is_error=False,
        result={"ok": True},
        invocation={"skill": "x"},
    )
    assert "todo_state" not in payload


# Generic tool input/result record (2026-07-23): every tool call gets a bounded
# key-per-line rendering of its raw invocation/result when no dedicated field
# (command_full / output / dispatch_order) captured it — the fix for the
# console's "No input or result detail was emitted for this tool call" rows.


def test_tool_started_payload_records_generic_input():
    payload = _tool_started_payload(
        "run.tool.started",
        "agent_chat_open",
        invocation={"persona_id": "dev", "instance_id": "abc"},
    )
    assert payload["tool_input"] == 'persona_id: "dev"\ninstance_id: "abc"'
    assert "command_full" not in payload


def test_tool_finished_payload_records_generic_input_and_result():
    payload = _tool_finished_payload(
        "run.tool.finished",
        "agent_chat_threads",
        duration=None,
        is_error=False,
        result={"ok": True, "threads": [{"id": "t1"}], "exit_code": 0},
        invocation={"limit": 5},
    )
    assert payload["tool_input"] == "limit: 5"
    # exit_code is echoed by its dedicated payload field, not the record.
    assert payload["tool_result"] == 'ok: true\nthreads: [{"id": "t1"}]'
    assert payload["exit_code"] == 0
    assert "output" not in payload


def test_tool_io_defers_to_dedicated_terminal_fields():
    payload = _tool_finished_payload(
        "run.tool.finished",
        "terminal",
        duration=None,
        is_error=False,
        result={"output": "42 tests passed", "exit_code": 0},
        invocation={"command": "pytest -q"},
    )
    # command_full IS the input record; output IS the result record.
    assert payload["command_full"] == "pytest -q"
    assert payload["output"] == "42 tests passed"
    assert "tool_input" not in payload
    assert "tool_result" not in payload


def test_tool_input_redacts_secret_pairs_line_by_line():
    payload = _tool_started_payload(
        "run.tool.started",
        "web_fetch",
        invocation={"url": "https://example.com", "api_key": "sk-12345"},
    )
    lines = payload["tool_input"].split("\n")
    assert lines[0] == 'url: "https://example.com"'
    assert lines[1] == "[redacted line — contained a secret]"
    assert "sk-12345" not in payload["tool_input"]


def test_agent_chat_send_finished_keeps_result_but_not_duplicate_input():
    payload = _tool_finished_payload(
        "run.tool.finished",
        "agent_chat_send",
        duration=None,
        is_error=False,
        result={"ok": True, "delivered": True},
        invocation={"persona_id": "dev", "message": "run the check"},
    )
    # The order already rides dispatch_order on the started event.
    assert "tool_input" not in payload
    assert payload["tool_result"] == "ok: true\ndelivered: true"


def test_dev_work_finished_payload_never_records_raw_tool_io():
    # Dev-work policy: changed_paths/changed_files ARE the record; the raw
    # invocation (diff body, machine-absolute paths) never rides the payload.
    payload = _tool_finished_payload(
        "run.tool.finished",
        "patch",
        duration=None,
        is_error=False,
        result={"ok": True, "diff": "raw diff body"},
        invocation={"patch": "*** Update File: lib/a.dart\n+x"},
    )
    assert payload["phase"] == "dev_work"
    assert "tool_input" not in payload
    assert "tool_result" not in payload


def test_tool_input_dropped_when_every_line_is_secret():
    payload = _tool_started_payload(
        "run.tool.started",
        "web_fetch",
        invocation={"api_key": "sk-12345"},
    )
    # A record of only redaction markers carries zero operator signal.
    assert "tool_input" not in payload


def test_tool_io_newline_in_key_cannot_split_a_secret_marker():
    # Review finding (2026-07-23): a hostile/foreign dict KEY carrying a raw
    # newline used to split the marker word across two rendered lines
    # ("pass" / "word: <secret>"), defeating every per-line scrub layer.
    # Keys are now newline-STRIPPED (removed, not spaced) so the marker
    # reconstitutes on one line and the pair redacts.
    payload = _tool_finished_payload(
        "run.tool.finished",
        "mcp__srv__do_thing",
        duration=None,
        is_error=False,
        result={"pass\nword": "hunter2-Xy9", "ok": True},
        invocation={"limit": 1},
    )
    assert "hunter2-Xy9" not in payload.get("tool_result", "")
    assert "[redacted line — contained a secret]" in payload["tool_result"]
    assert "ok: true" in payload["tool_result"]


def test_tool_io_pathological_result_never_kills_the_tool_event():
    # Review finding (2026-07-23): a result json.dumps AND str() both choke on
    # (deeply nested containers → RecursionError) must lose only the IO
    # record, never the whole run.tool.finished event.
    deep: list = []
    tail = deep
    for _ in range(4000):
        nested: list = []
        tail.append(nested)
        tail = nested
    payload = _tool_finished_payload(
        "run.tool.finished",
        "parser",
        duration=None,
        is_error=False,
        result=deep,
        invocation={"path": "x"},
    )
    assert payload["tool_name"] == "parser"
    assert payload["status"] == "passed"
    assert "tool_result" not in payload


def test_tool_result_bounded_head_with_marker():
    payload = _tool_finished_payload(
        "run.tool.finished",
        "read_file",
        duration=None,
        is_error=False,
        result={"content": "z" * 5000},
        invocation={"path": "lib/a.dart"},
    )
    assert payload["tool_result"].endswith("…(rest truncated)…")
    assert len(payload["tool_result"]) <= 1600 + len("\n…(rest truncated)…")


# T8 (2026-07-18): the rendered skills-index chars captured on final_model_input.


def test_rendered_skills_prompt_chars_guards_and_measures(monkeypatch):
    from types import SimpleNamespace
    from agent_runtime.profile_runner import _rendered_skills_prompt_chars
    import run_agent

    # No tool set at all -> None (never fabricated).
    assert _rendered_skills_prompt_chars(SimpleNamespace(valid_tool_names=None)) is None
    # A lane with no skills tools -> None (the index does not render).
    assert (
        _rendered_skills_prompt_chars(
            SimpleNamespace(valid_tool_names={"web_search", "terminal"}, platform="cli")
        )
        is None
    )

    # A lane that ships skill_view renders -> the length of the rendered index,
    # measured against the agent's OWN resolved tool set (mirrors
    # agent/system_prompt.py; a guaranteed in-process cache hit at runtime).
    rendered_text = "## Skills (mandatory)\n" + "x" * 9000
    monkeypatch.setattr(run_agent, "get_toolset_for_tool", lambda name: "skills")
    monkeypatch.setattr(
        run_agent, "build_skills_system_prompt", lambda **kwargs: rendered_text
    )
    agent = SimpleNamespace(
        valid_tool_names={"skill_view", "skills_list", "web_search"}, platform="cli"
    )
    assert _rendered_skills_prompt_chars(agent) == len(rendered_text)


def test_rendered_skills_prompt_chars_swallows_render_failure(monkeypatch):
    from types import SimpleNamespace
    from agent_runtime.profile_runner import _rendered_skills_prompt_chars
    import run_agent

    monkeypatch.setattr(run_agent, "get_toolset_for_tool", lambda name: "skills")

    def _boom(**kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(run_agent, "build_skills_system_prompt", _boom)
    agent = SimpleNamespace(valid_tool_names={"skill_view"}, platform="cli")
    assert _rendered_skills_prompt_chars(agent) is None


# ── T10c: cache_scope_id threading (request → factory → agent) ───────────────

def test_runner_threads_cache_scope_id_to_agent_factory(monkeypatch):
    """AgentRunRequest.cache_scope_id reaches the agent factory as a distinct
    kwarg while session_id is left None (persona-chat shape). It must not be
    conflated with session_id — the transcript-load key stays untouched."""
    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider",
        lambda requested, target_model: {"provider": requested, "model": target_model, "api_mode": "codex_responses"},
    )
    runner = ProfileAgentRunner(agent_factory=FakeAgent)

    runner.run(
        AgentRunRequest(
            profile=None,
            provider="openai-codex",
            model="gpt-5.6-luna",
            api_mode="codex_responses",
            session_id=None,               # persona-chat: no transcript reload
            cache_scope_id="chat-persona-abc",
            user_message="hello",
            system_message="system",
            task_id="run_scope",
        )
    )

    assert FakeAgent.last_kwargs["cache_scope_id"] == "chat-persona-abc"
    # session_id (the transcript/session-load key) is independent and stays None.
    assert FakeAgent.last_kwargs["session_id"] is None


def test_runner_defaults_cache_scope_id_none_for_worker_lanes(monkeypatch):
    """Lanes that don't set cache_scope_id thread None — the codex transport
    then falls back to session_id, so worker/mission-run behavior is unchanged."""
    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider",
        lambda requested, target_model: {"provider": requested, "model": target_model, "api_mode": "codex_responses"},
    )
    runner = ProfileAgentRunner(agent_factory=FakeAgent)

    runner.run(
        AgentRunRequest(
            profile=None,
            provider="openai-codex",
            model="gpt-5.6-luna",
            session_id="worker-session-1",
            user_message="hello",
            task_id="run_worker",
        )
    )

    assert FakeAgent.last_kwargs["cache_scope_id"] is None
    assert FakeAgent.last_kwargs["session_id"] == "worker-session-1"


def test_default_agent_factory_applies_cache_scope_without_ctor_kwarg(monkeypatch):
    """_default_agent_factory pops cache_scope_id and sets it on the constructed
    agent — it is NEVER forwarded to the (upstream) AIAgent constructor, and it
    never touches the session_id the agent loads its transcript from."""
    from agent_runtime import profile_runner as pr

    seen_kwargs = {}

    class _RecordingAgent:
        def __init__(self, **kwargs):
            seen_kwargs.update(kwargs)
            self.session_id = kwargs.get("session_id")

    monkeypatch.setattr("run_agent.AIAgent", _RecordingAgent, raising=False)

    agent = pr._default_agent_factory(session_id=None, cache_scope_id="chat-persona-xyz")

    # Applied as an attribute the codex seam reads…
    assert agent.cache_scope_id == "chat-persona-xyz"
    # …but NOT passed into the upstream constructor, and session_id untouched.
    assert "cache_scope_id" not in seen_kwargs
    assert seen_kwargs.get("session_id") is None

    # Unset scope leaves no attribute (getattr default None at the seam).
    seen_kwargs.clear()
    agent2 = pr._default_agent_factory(session_id="worker-1", cache_scope_id=None)
    assert getattr(agent2, "cache_scope_id", None) is None
    assert "cache_scope_id" not in seen_kwargs


# --------------------------------------------------------------------------- #
# Status honesty: the ok:false harness envelope must project as failed.
# --------------------------------------------------------------------------- #
def test_tool_finished_status_fails_on_ok_false_envelope_dict():
    from agent_runtime.profile_runner import _is_error_result

    assert _is_error_result({"ok": False, "target_persona": "@personainst_dev"})
    assert not _is_error_result({"ok": True, "reply": "done"})


def test_tool_finished_status_fails_on_serialized_ok_false_envelope():
    # Regression (2026-07-23): agent_chat_send failures return a serialized
    # {"ok": false, ...} envelope. _is_error_result missed it, so the turn
    # store recorded status="passed" and the operator console rendered green
    # OK chips for dispatches that never reached their target.
    from agent_runtime.profile_runner import _is_error_result

    failed_send = json.dumps(
        {"ok": False, "target_persona": "@personainst_dev", "reply": ""}
    )
    assert _is_error_result(failed_send)

    payload = _tool_finished_payload(
        "tool.finished", "agent_chat_send", duration=1.2,
        is_error=_is_error_result(failed_send), result=failed_send,
    )
    assert payload["status"] == "failed"


def test_tool_result_merely_containing_ok_false_text_is_not_a_failure():
    # A read_file of a JSON fixture may CONTAIN '"ok": false' — only a
    # top-level envelope counts, parse-confirmed.
    from agent_runtime.profile_runner import _is_error_result

    assert not _is_error_result('The fixture body was: {"data": {"ok": false}} etc.')
    assert not _is_error_result(json.dumps({"content": '{"ok": false}', "path": "x.json"}))
