"""RO-7: the turn's own timing split, on the terminal payload the caller reads.

The numbers have been on the durable record since ``phases`` landed. On
2026-09-06 they answered the operator's "why is a local turn slower than the
Mac's" exactly once — through a hand-written script that joined the launcher's
``[MissionChatTiming]`` line to ``mission_chat_turns/*.json`` on the turn id,
and the answer (the extra seconds were spent BEFORE the provider, in context
assembly correlated with overlapping visibility-bundle builds) was the opposite
of what the launcher's line alone suggested. An operator cannot write that
script. So the terminal payload carries the same seven numbers.

**What these rows defend is the same thing the phase block's rows defend: the
ABSENCE of what was never measured.** A key here is copied or it is missing. A
zero would say "the provider answered instantly" about a turn that never
reached a provider, and that is the one reading no consumer can recover from.

Driven through the REAL handler for every fact about the payload (which keys a
live turn's frame carries, that they agree with the record written in the same
breath), and at the projection's unit for facts about the sanitizer, which is
where a scripted input can be silly on purpose.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import snapshot_build_ledger
from agent_runtime.mission_chat_phases import (
    TURN_PHASES_KEY,
    TURN_TIMING_KEY,
    TURN_TIMING_ORDER,
    turn_timing_block,
)
from agent_runtime.mission_chat_turns import TURN_PROFILE_TIMING_KEY

from tests.hermes_cli.test_mission_chat_turn_phases import (  # type: ignore
    _args,
    _never_reaches_the_provider,
    _record_on_disk,
    _streaming_provider,
    isolate_agent_runtime_root,  # noqa: F401  (re-exported fixture)
    scripted_marks,  # noqa: F401  (re-exported fixture)
)
from tests.hermes_cli.test_mission_chat_budget_payload import (  # type: ignore
    _seed,
)

#: A runner's dict as the codex lane fills it: the three durations RO-7 names,
#: plus the cold/warm receipt and a neighbour that must NOT reach the block.
_RUNNER_TIMING = {
    "profile_conversation_turn_context_ms": 1_233,
    "profile_provider_responses_create_ms": 889,
    "profile_provider_stream_consume_ms": 1_630,
    "resident_actor_reused": 1,
    "agent_construct_ms": 4_012,
}


def _drive_capturing(monkeypatch, capsys, provider, *, turn_id, stream=True):
    """The real handler, and the payload it actually EMITTED.

    ``tests/hermes_cli/test_mission_chat_turn_phases.py``'s ``_drive`` throws
    stdout away because the record is its subject; here the wire frame IS the
    subject, so it is parsed back out of the handler's own stdout — the same
    bytes a serve child turns into ``line`` frames under the request id.
    """

    harness = _seed(monkeypatch, provider)
    code = harness._cmd_mission_chat_message(_args(turn_id, stream=stream))
    captured = capsys.readouterr().out
    payload = None
    if stream:
        # A streamed turn writes one compact JSON object per line and ends on
        # ``chat.final`` — the frame a serve child forwards as the request's
        # last ``line``.
        for line in captured.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(frame, dict) and frame.get("type") == "chat.final":
                payload = frame
    else:
        # ``--json`` prints ONE object, indented across many lines.
        start = captured.find("{")
        if start >= 0:
            payload = json.loads(captured[start:])
    assert payload is not None, captured
    return code, payload


# --------------------------------------------------------------------------- #
# 1. A live turn's terminal payload carries the block                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
def timed_payload(monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks):  # noqa: F811
    # A snapshot build that OVERLAPS this turn, so ``builds_overlapped`` is a
    # measurement rather than the honest ``None`` a process which has never led
    # a build reports. The span is in the SCRIPTED clock's frame (the turn's
    # anchor is 0.0 under ``scripted_marks``), which is what makes the count
    # deterministic instead of a race with a real builder.
    snapshot_build_ledger.reset_for_tests()
    snapshot_build_ledger.record_build(started=0.5, ended=2.0)
    try:
        _code, payload = _drive_capturing(
            monkeypatch,
            capsys,
            _streaming_provider(profile_timing=_RUNNER_TIMING),
            turn_id="timing_block_ok",
        )
    finally:
        snapshot_build_ledger.reset_for_tests()
    return payload, _record_on_disk(isolate_agent_runtime_root, "timing_block_ok")


def test_the_terminal_payload_carries_the_seven_key_timing_block(timed_payload):
    """The frame the launcher folds, with the split it could not see before.

    *Killing mutation:* drop the ``MISSION_CHAT_TURN_TIMING_KEY`` entry from
    the terminal payload and this row goes red on the missing key.
    """

    payload, _record = timed_payload
    block = payload[TURN_TIMING_KEY]
    assert set(block) == set(TURN_TIMING_ORDER), block
    assert block["turn_context_ms"] == 1_233
    assert block["responses_create_ms"] == 889
    assert block["stream_consume_ms"] == 1_630
    assert block["resident_actor_reused"] is True
    # The two that come from the record's own marks rather than the runner's
    # namespace: elapsed ms off the turn's anchor, and the Stage-4 count.
    assert isinstance(block["request_assembled_ms"], int)
    assert isinstance(block["provider_first_byte_ms"], int)
    assert block["builds_overlapped"] == 1


def test_the_block_is_a_copy_of_the_ledger_record_and_not_a_second_reading(
    timed_payload,
):
    """"Copied at commit" stated as an assertion.

    The frame and the durable record are written in the same breath from the
    same two instruments, so a projection of the RECORD must reproduce the
    frame byte for byte. If it ever does not, one of them is measuring
    something the other is not — which is the whole class of defect this block
    exists to remove.

    *Killing mutation:* build the block from a fresh
    ``chat_result.profile_timing`` read taken elsewhere in the handler and the
    equality stops holding for the handler's own folded keys.
    """

    payload, record = timed_payload
    assert payload[TURN_TIMING_KEY] == turn_timing_block(
        phases=record[TURN_PHASES_KEY],
        profile_timing=record[TURN_PROFILE_TIMING_KEY],
    )


def test_the_block_carries_nothing_but_the_seven(timed_payload):
    """The runner's dict is an open namespace — ``agent_construct_ms`` and the
    admission receipts live in it. The block is a CLOSED set, so nothing new on
    that side can arrive on the wire without a decision."""

    payload, _record = timed_payload
    assert "agent_construct_ms" not in payload[TURN_TIMING_KEY]


def test_no_existing_key_moved_to_make_room_for_it(timed_payload):
    """Additive means additive: the payload's own timing keys stay put."""

    payload, _record = timed_payload
    assert payload["latency_ms"] == 4
    assert payload["profile_timing"]["agent_construct_ms"] == 4_012
    assert payload["resident_actor_reused"] is True


def test_the_non_streamed_json_payload_carries_it_too(
    monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks  # noqa: F811
):
    """The argv lane's exit payload. ``harness mission-chat message --json``
    prints ONE object and exits; a launcher reading that object gets the same
    seven numbers a socket client reads off the terminal frame."""

    _code, payload = _drive_capturing(
        monkeypatch,
        capsys,
        _streaming_provider(profile_timing=_RUNNER_TIMING),
        turn_id="timing_block_argv",
        stream=False,
    )

    assert payload["ok"] is True
    assert payload[TURN_TIMING_KEY]["turn_context_ms"] == 1_233


# --------------------------------------------------------------------------- #
# 2. A phase that never happened has no key                                    #
# --------------------------------------------------------------------------- #
def test_a_turn_whose_runner_reported_nothing_carries_only_what_was_marked(
    monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks  # noqa: F811
):
    """No runner durations at all: the three profile-sourced keys are ABSENT.

    Not zero. A ``responses_create_ms`` of ``0`` on a turn whose runner never
    reported one would read as a provider that answered before it was asked.

    *Killing mutation:* default the missing keys to ``0`` in
    ``turn_timing_block`` and this row goes red on the first ``not in``.
    """

    _code, payload = _drive_capturing(
        monkeypatch,
        capsys,
        _streaming_provider(profile_timing={}),
        turn_id="timing_block_bare",
    )

    block = payload[TURN_TIMING_KEY]
    for key in (
        "turn_context_ms",
        "responses_create_ms",
        "stream_consume_ms",
        "resident_actor_reused",
    ):
        assert key not in block, f"{key} materialized as {block.get(key)!r}"
    # The marks the turn DID pass through are still there — absence is only
    # honest when presence is real.
    assert block["provider_first_byte_ms"] >= 0


def test_a_turn_that_died_before_the_provider_carries_no_first_byte(
    monkeypatch, capsys, isolate_agent_runtime_root, scripted_marks  # noqa: F811
):
    """The failure lane's payload is a different dict and carries no block at
    all — there is no reply, no commit, and nothing to copy. What must never
    happen is a block with a zeroed ``provider_first_byte_ms``."""

    _code, payload = _drive_capturing(
        monkeypatch,
        capsys,
        _never_reaches_the_provider(),
        turn_id="timing_block_dead",
        stream=False,
    )

    assert payload["ok"] is False
    assert payload.get(TURN_TIMING_KEY, {}).get("provider_first_byte_ms") is None


# --------------------------------------------------------------------------- #
# 3. The projection itself                                                     #
# --------------------------------------------------------------------------- #
def test_the_projection_reads_both_instruments_and_renames_neither_wrongly():
    block = turn_timing_block(
        phases={
            "request_assembled": 1_762,
            "provider_first_byte": 7_800,
            "builds_overlapped": 3,
        },
        profile_timing={
            "profile_conversation_turn_context_ms": 4_730,
            "profile_provider_responses_create_ms": 1_542,
            "profile_provider_stream_consume_ms": 594,
            "resident_actor_reused": 0,
        },
    )

    assert block == {
        "turn_context_ms": 4_730,
        "request_assembled_ms": 1_762,
        "provider_first_byte_ms": 7_800,
        "responses_create_ms": 1_542,
        "stream_consume_ms": 594,
        "builds_overlapped": 3,
        "resident_actor_reused": False,
    }
    # The order a person reads a turn in, not the order the sources are in.
    assert list(block) == list(TURN_TIMING_ORDER)


def test_a_zero_that_was_MEASURED_survives():
    """The mirror of the absence rule. ``builds_overlapped: 0`` is a finding —
    builds happened in this process and none touched this turn — and dropping
    it as falsy would delete the answer to the question it was added for."""

    block = turn_timing_block(
        phases={"builds_overlapped": 0}, profile_timing={"resident_actor_reused": 0}
    )

    assert block == {"builds_overlapped": 0, "resident_actor_reused": False}


def test_nothing_measured_is_no_block_at_all():
    assert turn_timing_block(phases={}, profile_timing={}) is None
    assert turn_timing_block(phases=None, profile_timing="not a dict") is None


@pytest.mark.parametrize(
    "raw",
    ["1233", None, True, -1, 10**12, [1], {"ms": 1}],
)
def test_the_projection_drops_what_it_cannot_read(raw):
    """Sanitized on the way OUT, not only on the way in: this block is read
    straight off a wire frame by a consumer that will render it."""

    block = turn_timing_block(
        phases={"request_assembled": raw},
        profile_timing={"profile_conversation_turn_context_ms": raw},
    )

    assert block is None or "request_assembled_ms" not in block
    assert block is None or "turn_context_ms" not in block


def test_a_true_in_a_duration_slot_is_corruption_not_one_millisecond():
    assert turn_timing_block(
        phases={"provider_first_byte": True}, profile_timing={}
    ) is None
