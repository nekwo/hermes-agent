"""Provider TTFB rides the ``API call #N`` line — and is ABSENT when unmeasured.

Mission-chat turns get their first-byte instant from the ``request_assembled``
/ ``provider_first_byte`` phase marks. Every other lane — gateway, cron, CLI —
has no ``phases`` block at all, so the one line this loop already logs per API
call is the only place their provider TTFB can survive. Stage 2 appends one
token to it.

What these rows pin is the honesty contract, not the plumbing:

* absent-never-zero — a call with no observed first byte prints NO ``ttfb=``
  token. ``ttfb=0.0s`` would claim an instantaneous provider, and no reader
  downstream could tell that apart from a real sub-100ms response.
* first-firing-wins — the wrapper records once and keeps invoking the original
  callback, because that callback is what stops the thinking spinner.

Named sabotages for this stage: default the recorder to ``0.0`` when
unobserved (the absent-token row must red), and drop the original callback
from the wrapper (the spinner row must red).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from agent.conversation_loop import _first_delta_recorder, _format_ttfb_token


# ---------------------------------------------------------------- formatter


def test_measured_value_renders_one_decimal_with_a_leading_space():
    assert _format_ttfb_token(1.234) == " ttfb=1.2s"


def test_unmeasured_renders_nothing_at_all():
    token = _format_ttfb_token(None)
    assert token == "", (
        "an unobserved first byte must vanish from the line; "
        f"got {token!r} — 'ttfb=0.0s' is the absent-as-zero lie"
    )


# ------------------------------------------------------------------ wrapper


def test_wrapper_records_on_first_firing_and_still_calls_the_original():
    cell: list[float | None] = [None]
    fired: list[str] = []

    wrapped = _first_delta_recorder(cell, 0.0, lambda: fired.append("spinner"))
    wrapped()

    assert cell[0] is not None, "the first delta went unmeasured"
    assert fired == ["spinner"], (
        "the wrapper swallowed the original callback; the thinking spinner "
        "would never stop"
    )


def test_second_firing_does_not_overwrite_the_first_instant():
    cell: list[float | None] = [None]
    fired: list[str] = []

    wrapped = _first_delta_recorder(cell, 0.0, lambda: fired.append("spinner"))
    wrapped()
    first = cell[0]
    wrapped()

    assert cell[0] == first, (
        "TTFB is the FIRST byte; a provider firing the delta callback per "
        "token must not push the measurement later"
    )
    assert fired == ["spinner", "spinner"], (
        "every firing must still reach the original callback"
    )


# ---------------------------------------------------------------- real loop


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


def test_non_streaming_call_logs_no_ttfb_token(loop_agent, caplog):
    """A Mock client disables streaming, so no first delta is ever observed."""

    from tests.run_agent.test_run_agent import _mock_response

    loop_agent.client.chat.completions.create.return_value = _mock_response(
        content="done.",
        finish_reason="stop",
        usage={
            "prompt_tokens": 11,
            "completion_tokens": 3,
            "total_tokens": 14,
        },
    )

    with (
        caplog.at_level(logging.INFO, logger="agent.conversation_loop"),
        patch.object(loop_agent, "_persist_session"),
        patch.object(loop_agent, "_save_trajectory"),
        patch.object(loop_agent, "_cleanup_task_resources"),
    ):
        result = loop_agent.run_conversation("say done")

    assert result["final_response"], "the mocked turn must actually complete"

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.name == "agent.conversation_loop"
        and record.getMessage().startswith("API call #")
    ]
    assert lines, "the loop logged no 'API call #' line to attach TTFB to"
    for line in lines:
        assert "ttfb=" not in line, (
            "a non-streaming call observed no first byte, so the token must "
            f"be absent entirely; got {line!r}"
        )
