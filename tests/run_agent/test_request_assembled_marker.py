"""The dispatch-start marker fires from the REAL loop, before the provider call.

The seam tests in ``tests/hermes_cli/test_mission_chat_turn_phases.py`` prove
the payload shape and the mapper; what they cannot prove is that
``_run_conversation`` actually CALLS ``_emit_request_assembled_marker`` at the
dispatch boundary. This file drives the real conversation loop with a mocked
client and asserts the ordering fact the split depends on: the marker payload
reaches ``status_callback`` before the provider client's ``create`` runs, and
after it there is nothing left of the turn's own work — so everything the
`request_assembled → provider_first_byte` span contains is client init +
network + provider.

Named sabotage for this stage: move the emission below the provider call (or
delete it). The ordering row must red with the marker missing or trailing.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import CONVERSATION_REQUEST_ASSEMBLED_STEP


@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_run_agent's fixture)."""

    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


def test_marker_fires_before_the_provider_call_on_a_live_loop(loop_agent):
    from tests.run_agent.test_run_agent import _mock_response

    order: list[str] = []

    def _observe(payload):
        if (
            isinstance(payload, dict)
            and payload.get("step") == CONVERSATION_REQUEST_ASSEMBLED_STEP
        ):
            order.append("marker")

    loop_agent.status_callback = _observe

    def _provider_call(*args, **kwargs):
        order.append("provider_call")
        return _mock_response(content="done.", finish_reason="stop")

    loop_agent.client.chat.completions.create.side_effect = _provider_call

    with (
        patch.object(loop_agent, "_persist_session"),
        patch.object(loop_agent, "_save_trajectory"),
        patch.object(loop_agent, "_cleanup_task_resources"),
    ):
        result = loop_agent.run_conversation("say done")

    assert result["final_response"], "the mocked turn must actually complete"
    assert "marker" in order, (
        "the loop never emitted the request-assembled marker; the whole "
        "run_conversation prologue is back inside the 'provider' span"
    )
    assert order.index("marker") < order.index("provider_call"), (
        f"marker must precede the provider call, got order={order}"
    )
