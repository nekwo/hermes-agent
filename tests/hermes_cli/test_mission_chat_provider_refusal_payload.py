"""CLI contract for a mission-chat turn the PROVIDER refused.

Sibling of ``test_mission_chat_budget_payload`` and written the same way —
driving the real ``_cmd_mission_chat_message`` and reading the emitted envelope
plus the persisted journal record, never the source text of a file that is
``exec``'d and cannot be imported.

The 2026-09-11 incident: the operator's OpenAI Codex plan ran out, the provider
answered ``HTTP 429`` with ``error.type = usage_limit_reached``, and Mission
Control showed *"Hermes cannot prove whether turn … completed"* over an
**Abandon & resend** button. The operator resent, into the same wall. It is the
SECOND instance of the class the wall-budget row next door fixed: a KNOWN
terminal cause reported as an unknown one, freezing the console and sending an
operator at ``turn-resolve`` for a turn that never existed.

These rows pin the two halves that matter to that operator: the turn settles at
a TERMINAL journal state (never ``outcome_unknown``), and every string on the
envelope is free of resolve instructions.
"""

from __future__ import annotations

import json
import time

import pytest

from agent_runtime.mission_chat_outcome import ChatErrorKind, ExecutionState

from .test_mission_chat_budget_payload import (
    _NEGATIONS,
    _RESOLVE_INSTRUCTIONS,
    _RESOLVE_VERBS,
    _SESSION_ID,
    _args,
    _seed,
    isolate_agent_runtime_root,  # noqa: F401 — fixture re-export
)

#: What the live incident's transport produced: the ``_summarize_api_error``
#: one-liner (all that survives to the chat lane) plus the typed block
#: ``profile_runner`` now attaches beside it.
LIVE_SUMMARY = "HTTP 429: The usage limit has been reached"


def _refusing_provider(**provider_error):
    from agent_runtime.profile_runner import ProfileRunnerError

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

        def mission_chat_reply(self, *args, **kwargs):
            raise ProfileRunnerError(
                LIVE_SUMMARY, provider_error=provider_error or None
            )

    return _Provider


def _quota_error(**overrides):
    block = {
        "status_code": 429,
        "reason": "usage_limit_reached",
        "message": "The usage limit has been reached",
        "reset_at": time.time() + 10_800,
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "failure_reason": "rate_limit",
    }
    block.update(overrides)
    return block


@pytest.fixture
def refusal_envelope(monkeypatch, capsys, isolate_agent_runtime_root):
    """Drive one real provider refusal and hand back its emitted envelope."""

    harness = _seed(monkeypatch, _refusing_provider(**_quota_error()))
    code = harness._cmd_mission_chat_message(_args("refused_turn"))
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def refused_spent_envelope(monkeypatch, capsys, isolate_agent_runtime_root):
    """...then RESEND that same id, which finds the settled record."""

    harness = _seed(monkeypatch, _refusing_provider(**_quota_error()))
    assert harness._cmd_mission_chat_message(_args("refused_turn")) == 2
    capsys.readouterr()
    code = harness._cmd_mission_chat_message(_args("refused_turn"))
    return code, json.loads(capsys.readouterr().out)


def test_a_provider_refusal_emits_the_typed_pair(refusal_envelope):
    code, payload = refusal_envelope
    assert code == 2
    assert payload["ok"] is False
    assert payload["execution_state"] == ExecutionState.FAILED
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_PROVIDER_REFUSED
    # The incident's rendering, named so a regression to it is unmistakable.
    assert payload["error_kind"] != ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN


def test_the_frame_carries_the_typed_block_so_nobody_reads_the_prose(
    refusal_envelope,
):
    """``reason`` is the provider's OWN code. The launcher chooses its copy from
    that field; a consumer forced to match ``blocker`` English is one provider
    wording change away from showing the wrong sentence."""

    _, payload = refusal_envelope
    block = payload["provider_refusal"]
    assert block["status_code"] == 429
    assert block["reason"] == "usage_limit_reached"
    assert block["message"] == "The usage limit has been reached"
    assert block["provider"] == "openai-codex"
    assert block["model"] == "gpt-5.6-luna"
    assert 10_000 < block["resets_in_seconds"] <= 10_800
    assert payload["provider_refused"] is True
    assert payload["turn_resolution_required"] is False


def test_the_refusal_settles_a_terminal_journal_state_not_outcome_unknown(
    refusal_envelope,
):
    """The journal RECORD is the proof, and ``outcome_unknown`` here IS the
    incident: it is in-flight, so the console keeps a spinner, the repair sweep
    owns the row, and a resend is refused until an operator resolves it."""

    from agent_runtime.mission_chat_turns import (
        INFLIGHT_TURN_STATES,
        OPERATOR_RESOLVABLE_TURN_STATES,
        TERMINAL_TURN_STATES,
        mission_chat_turn_record,
    )

    record = mission_chat_turn_record(
        session_id=_SESSION_ID, client_message_id="refused_turn"
    )
    assert record is not None
    assert record["state"] == "provider_refused"
    assert record["state"] in TERMINAL_TURN_STATES
    assert record["state"] not in INFLIGHT_TURN_STATES
    assert record["state"] not in OPERATOR_RESOLVABLE_TURN_STATES
    assert record["provider_submitted"] is True
    assert record["provider_refusal"]["reason"] == "usage_limit_reached"


def test_a_resend_of_a_refused_id_reports_the_same_typed_pair(
    refused_spent_envelope,
):
    code, payload = refused_spent_envelope
    assert code == 2
    assert payload["execution_state"] == ExecutionState.FAILED
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_PROVIDER_REFUSED
    assert payload["journal_state"] == "provider_refused"
    assert payload["turn_resolution_required"] is False
    assert payload["provider_refusal"]["reason"] == "usage_limit_reached"


@pytest.mark.parametrize(
    "envelope", ["refusal_envelope", "refused_spent_envelope"]
)
def test_refusal_payloads_never_tell_the_operator_to_turn_resolve(envelope, request):
    """The sentence that cost the operator their second `hi`."""

    _, payload = request.getfixturevalue(envelope)
    texts = [value for value in payload.values() if isinstance(value, str)]
    assert texts
    for text in texts:
        lowered = text.lower()
        for phrase in _RESOLVE_INSTRUCTIONS:
            assert phrase not in lowered, (
                "a refused request never ran: it must never route the operator "
                f"to a resolution verb (found {phrase!r} in {text!r})"
            )
        if any(verb in lowered for verb in _RESOLVE_VERBS):
            assert any(negation in lowered for negation in _NEGATIONS), (
                "a refusal payload may name turn-resolve only to say it is NOT "
                f"needed (found an unnegated mention in {text!r})"
            )


def test_the_next_expected_names_the_wait(refusal_envelope):
    _, payload = refusal_envelope
    assert "resets in about 3 h" in payload["next_expected"]
    assert "NEW client_message_id" in payload["next_expected"]


def test_a_server_error_after_the_boundary_stays_genuinely_ambiguous(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The narrowing must not swallow the state it narrowed AWAY from.

    A 503 is not a verdict — the provider decided nothing and the request may
    well have run — so that turn still settles ``outcome_unknown`` and still
    routes the operator to ``turn-resolve``. Deleting this row is how the fix
    becomes the next incident.
    """

    from agent_runtime.mission_chat_turns import mission_chat_turn_record

    harness = _seed(
        monkeypatch, _refusing_provider(status_code=503, reason="overloaded")
    )
    code = harness._cmd_mission_chat_message(_args("overloaded_turn"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert payload["execution_state"] == ExecutionState.BLOCKED
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN
    assert "provider_refusal" not in payload
    assert "resolve the exact" in payload["next_expected"]
    record = mission_chat_turn_record(
        session_id=_SESSION_ID, client_message_id="overloaded_turn"
    )
    assert record["state"] == "outcome_unknown"


def test_a_failure_carrying_no_typed_block_stays_ambiguous(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The same one-liner with nothing attached — a dropped stream, a transport
    death — must NOT be read as a refusal. This is what makes the fix a typed
    verdict rather than a prose match on ``HTTP 429``."""

    from agent_runtime.mission_chat_turns import mission_chat_turn_record

    harness = _seed(monkeypatch, _refusing_provider())
    code = harness._cmd_mission_chat_message(_args("untyped_turn"))
    payload = json.loads(capsys.readouterr().out)

    assert code == 2
    assert LIVE_SUMMARY in payload["blocker"]
    assert payload["error_kind"] == ChatErrorKind.CHAT_TURN_OUTCOME_UNKNOWN
    record = mission_chat_turn_record(
        session_id=_SESSION_ID, client_message_id="untyped_turn"
    )
    assert record["state"] == "outcome_unknown"
