from __future__ import annotations

import importlib.util

import pytest

from agent_runtime.persona_runtime import GPTPersonaRuntime


@pytest.mark.parametrize(
    "module_name",
    [
        "agent_runtime.autonomy",
        "agent_runtime.goal_runner",
        "agent_runtime.liveness",
        "agent_runtime.no_freeze_monitor",
        "agent_runtime.node_tools",
        "agent_runtime.planning",
        "agent_runtime.reconciler",
        "agent_runtime.recovery",
        "agent_runtime.root_node_engine",
        "agent_runtime.supervision",
        "agent_runtime.ticker",
        "agent_runtime.worker_actions",
    ],
)
def test_dispatch_loop_module_is_retired(module_name: str) -> None:
    assert importlib.util.find_spec(module_name) is None


def test_persona_runtime_no_longer_exposes_tick_execution() -> None:
    assert not hasattr(GPTPersonaRuntime, "run_tick")
    assert not hasattr(GPTPersonaRuntime, "_invoke_agent")
