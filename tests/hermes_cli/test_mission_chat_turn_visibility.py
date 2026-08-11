"""Every mission-chat payload that carries a reply carries whether it was SEEN.

`ok: True` has never meant "the operator saw an answer" — this handler returns
exactly that with an empty `reply` when the model produces no content, and on
2026-08-11 it did, three retries deep, on a background-completion delivery. The
completion notice landed in the operator's thread answered by nothing while the
drain, `last_delivery` and `harness status` all reported a clean delivery.

So the typed block travels WITH the reply, from every branch that emits one —
the live turn, both idempotent replays, and the projection-failure path. A
consumer must never have to know which internal branch produced its payload in
order to know whether anyone saw anything.

Driven through the real handler (the `_seed` pattern from
`test_mission_chat_budget_payload`): a source-shaped assertion on the dict
literal would pass just as happily against a lane that never reached the
payload.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_runtime.turn_visibility import TURN_VISIBILITY_KEY

from tests.hermes_cli.test_mission_chat_budget_payload import (  # noqa: F401
    _SESSION_ID,
    _args,
    _seed,
    isolate_agent_runtime_root,
)


def _provider(final_response: str, messages=None, raw=None):
    """A provider returning one real `AgentRunResult`, as the live lane does."""

    from agent_runtime.profile_runner import AgentRunResult

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, *args, **kwargs):
            return AgentRunResult(
                final_response=final_response,
                session_id=_SESSION_ID,
                provider="openai-codex",
                model="gpt-test",
                base_url=None,
                messages=list(messages or []),
                input_tokens=1,
                output_tokens=2,
                total_tokens=3,
                raw=dict(raw or {}),
            )

    return _Provider


def _drive(monkeypatch, capsys, *, final_response, messages=None, raw=None, cmid="vis_turn"):
    harness = _seed(monkeypatch, _provider(final_response, messages, raw))
    code = harness._cmd_mission_chat_message(_args(cmid))
    return code, json.loads(capsys.readouterr().out)


def test_a_replying_turn_reports_visible(monkeypatch, capsys, isolate_agent_runtime_root):
    code, payload = _drive(monkeypatch, capsys, final_response="3 failures in the panel")

    assert code == 0
    block = payload[TURN_VISIBILITY_KEY]
    assert block["state"] == "visible"
    assert block["reason"] == "content"
    assert block["reply_chars"] == len("3 failures in the panel")


def test_an_ok_turn_that_said_nothing_reports_silent(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """THE incident, as one row: `ok` true, `reply` empty, and now said so."""

    code, payload = _drive(monkeypatch, capsys, final_response="")

    assert code == 0
    assert payload["ok"] is True
    assert payload["reply"] == ""
    assert payload[TURN_VISIBILITY_KEY]["state"] == "silent"
    assert payload[TURN_VISIBILITY_KEY]["reason"] == "empty"


def test_the_finish_reason_reaches_the_payload_from_the_run_result(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The provider-derived typed value, carried end to end.

    This is the whole reason the block is computed HERE and not by each
    consumer: `messages` never leaves the handler, so nothing downstream can
    tell a truncation from a model that simply said nothing.
    """

    _, payload = _drive(
        monkeypatch,
        capsys,
        final_response="",
        messages=[
            {"role": "assistant", "content": "working", "finish_reason": "tool_calls"},
            {"role": "assistant", "content": "", "finish_reason": "incomplete"},
        ],
    )

    block = payload[TURN_VISIBILITY_KEY]
    assert block["state"] == "silent"
    assert block["reason"] == "truncated"
    assert block["finish_reason"] == "incomplete"


def test_an_idempotent_replay_carries_the_block_too(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """A retried delivery lands on the replay branch, not the live one.

    The drain derives its `client_message_id` from the dispatch id precisely so
    a retry converges on ONE turn — which means the replay payload is what a
    redelivery reads. A block that only the live branch emitted would go
    missing on exactly the path built for retries.
    """

    harness = _seed(monkeypatch, _provider(""))
    assert harness._cmd_mission_chat_message(_args("replayed_turn")) == 0
    capsys.readouterr()

    code = harness._cmd_mission_chat_message(_args("replayed_turn"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 0, payload
    assert payload.get("idempotent_replay") is True
    assert payload[TURN_VISIBILITY_KEY]["state"] == "silent"


def test_a_failing_provider_still_emits_a_payload_this_does_not_break(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """Totality where it counts: the block must never become the failure.

    One of the stamp sites is inside the handler's exception path, so a raise
    from the classifier would replace a real failure with its own and corrupt
    the one-JSON-object stdout contract on the way out.
    """

    class _Boom:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, *args, **kwargs):
            raise RuntimeError("provider exploded")

    harness = _seed(monkeypatch, _Boom)
    code = harness._cmd_mission_chat_message(_args("boom_turn"))
    payload = json.loads(capsys.readouterr().out)

    assert code != 0
    assert payload["ok"] is False
    # Whatever it says, it is one well-formed JSON object and it did not die
    # classifying visibility.
    assert isinstance(payload, dict)
