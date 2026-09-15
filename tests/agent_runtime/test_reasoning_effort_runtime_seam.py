"""Per-instance reasoning effort actually reaches the model call.

The fork-owned seam: a per-agent-instance reasoning override rides
``AgentRunRequest.reasoning_effort`` into ``ProfileAgentRunner`` and is
translated to the agent's ``reasoning_config`` (which the upstream transport
reads as ``params["reasoning_config"]``). When unset, no reasoning_config is
passed so the transport keeps its existing global-config behavior.
"""

from __future__ import annotations

import contextlib

import agent_runtime.profile_runner as pr


class _FakeAgent:
    def __init__(self, **kwargs):
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = None
        self.session_id = None

    def run_conversation(self, **_kwargs):
        return {"final_response": "ok", "messages": []}


def _make_runner(monkeypatch, captured: dict):
    def fake_factory(**kwargs):
        captured.clear()
        captured.update(kwargs)
        return _FakeAgent(**kwargs)

    class _Binding:
        readiness = "ready"
        hermes_profile = "p"
        summary = ""

    monkeypatch.setattr(pr, "_binding_for_profile", lambda profile: _Binding())
    monkeypatch.setattr(pr, "_resolve_request_runtime", lambda request: {})
    monkeypatch.setattr(pr, "persona_profile_context", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(pr, "_agent_workdir", lambda *a, **k: contextlib.nullcontext())
    return pr.ProfileAgentRunner(agent_factory=fake_factory)


def test_reasoning_effort_becomes_reasoning_config(monkeypatch):
    captured: dict = {}
    runner = _make_runner(monkeypatch, captured)
    runner.run(
        pr.AgentRunRequest(
            profile="p",
            provider="openai-codex",
            model="gpt-5.6-luna",
            reasoning_effort="high",
            user_message="hi",
        )
    )
    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}


def test_reasoning_none_disables_thinking(monkeypatch):
    captured: dict = {}
    runner = _make_runner(monkeypatch, captured)
    runner.run(
        pr.AgentRunRequest(
            profile="p",
            provider="openai-codex",
            model="gpt-5.6-luna",
            reasoning_effort="none",
            user_message="hi",
        )
    )
    assert captured["reasoning_config"] == {"enabled": False}


def test_unset_reasoning_leaves_config_untouched(monkeypatch):
    captured: dict = {}
    runner = _make_runner(monkeypatch, captured)
    runner.run(
        pr.AgentRunRequest(
            profile="p",
            provider="openai-codex",
            model="gpt-5.6-luna",
            user_message="hi",
        )
    )
    assert "reasoning_config" not in captured
