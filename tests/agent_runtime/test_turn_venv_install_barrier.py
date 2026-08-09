"""The harness turn chokepoint arms the venv-mutation barrier (2026-08-09).

Proving it at ``ProfileAgentRunner.run`` matters because that is the ONLY point
that sees every harness lane — operator mission chat, mission-run workers, and
dispatch — and because the installs a turn triggers are indirect: the auxiliary
client resolving to Anthropic for a side-call, a model-invoked tool reaching an
unconfigured backend. The agent below observes the barrier from inside the run,
which is exactly where those installs fire.
"""

from __future__ import annotations

import threading

import pytest

from agent_runtime.profile_runner import AgentRunRequest, ProfileAgentRunner
from tools import lazy_deps


class _BarrierProbeAgent:
    """A fake agent that records the barrier state during its own turn."""

    observed: list[str | None] = []
    observed_on_thread: list[str | None] = []

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id") or "session_probe"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = "https://example.invalid/v1"
        self.tools = []

    def run_conversation(self, user_message, system_message=None, task_id=None):
        _BarrierProbeAgent.observed.append(lazy_deps.venv_install_denial())

        def _probe():
            _BarrierProbeAgent.observed_on_thread.append(lazy_deps.venv_install_denial())

        worker = threading.Thread(target=_probe)
        worker.start()
        worker.join(timeout=30)
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


@pytest.fixture
def probe_runner(monkeypatch):
    monkeypatch.setattr(
        "agent_runtime.profile_runner.resolve_runtime_provider",
        lambda requested, target_model: {
            "provider": requested,
            "model": target_model,
            "api_mode": "codex_responses",
        },
    )
    _BarrierProbeAgent.observed = []
    _BarrierProbeAgent.observed_on_thread = []
    return ProfileAgentRunner(agent_factory=_BarrierProbeAgent)


def _request(**overrides) -> AgentRunRequest:
    kwargs = dict(
        profile=None,
        provider="openai-codex",
        model="gpt-5.5",
        api_mode="codex_responses",
        session_id="session_barrier",
        user_message="hello",
        system_message="system",
        task_id="run_barrier",
    )
    kwargs.update(overrides)
    return AgentRunRequest(**kwargs)


def test_turn_runs_with_venv_installs_denied(probe_runner):
    assert lazy_deps.venv_install_denial() is None

    result = probe_runner.run(_request())

    assert result.final_response == "ok"
    assert len(_BarrierProbeAgent.observed) == 1
    denial = _BarrierProbeAgent.observed[0]
    assert denial is not None
    assert "an agent turn" in denial


def test_barrier_names_the_profile_so_the_diagnostic_is_attributable(probe_runner):
    probe_runner.run(_request(profile=None))

    assert _BarrierProbeAgent.observed[0] == "an agent turn (profile=None)"


def test_barrier_reaches_the_agents_worker_threads(probe_runner):
    probe_runner.run(_request())

    assert _BarrierProbeAgent.observed_on_thread == _BarrierProbeAgent.observed


def test_barrier_is_released_after_the_turn(probe_runner):
    probe_runner.run(_request())

    assert lazy_deps.venv_install_denial() is None


def test_barrier_is_released_when_the_turn_raises(monkeypatch, probe_runner):
    class _Exploding(_BarrierProbeAgent):
        def run_conversation(self, *a, **k):
            raise RuntimeError("turn blew up")

    monkeypatch.setattr(probe_runner, "_agent_factory", _Exploding)

    with pytest.raises(Exception):
        probe_runner.run(_request())

    assert lazy_deps.venv_install_denial() is None
