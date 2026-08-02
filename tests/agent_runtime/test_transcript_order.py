"""C8 — one transcript ordering authority (finding F17).

Operator ruling, verbatim: "we need to make sure there is only one authority."

The turn-scoped key (anchor = client_message_id / turn_id token, position =
turn_seq: operator 0 < emitter element seq < content band < terminal) is
stamped hermes-side and governs every transcript-visible representation:
the history projection, the operator-conversation projection, the turn store,
and (mirrored in Dart) the launcher fold. These tests pin:

* the pure ordering algorithm (grouping, band, terminal, shuffle-proofness);
* the F17 repro — the PRE-C8 wall-clock merge reorders an adversarial turn
  (same-second SessionDB stamps + trace clock skew); the key-sorted merge
  holds stable;
* the projections stamping the key;
* the turn store's write-ahead `started_at` anchor + start-ordered replay;
* the conversation contract agreeing with the history projection.
"""

from __future__ import annotations

import random

from agent_runtime.mission_chat_turns import (
    mission_chat_turn_record,
    mission_chat_turn_records,
    persist_mission_chat_turn,
)
from agent_runtime.operator_channels import _conversation_contract
from agent_runtime.persona_chat_history import (
    _terminal_turn_marker_rows,
    _ordered_message_rows,
    _safe_recent_messages,
)
from agent_runtime.transcript_order import (
    TURN_SEQ_CONTENT,
    TURN_SEQ_OPERATOR,
    TURN_SEQ_TERMINAL,
    order_transcript_rows,
    pre_trace_ack_text,
)


def _order(rows):
    return order_transcript_rows(
        rows,
        anchor=lambda row: row.get("anchor"),
        turn_seq=lambda row: row.get("turn_seq"),
        fallback_key=lambda row, _i: (str(row.get("ts") or ""), str(row.get("id") or "")),
    )


def _ids(rows):
    return [row["id"] for row in rows]


# --------------------------------------------------------------------------- #
# Pure algorithm                                                              #
# --------------------------------------------------------------------------- #


def test_intra_turn_key_beats_wall_clock():
    # Adversarial clocks: the reply's stamp is EARLIER than the tool rows'
    # (SessionDB second-granularity vs trace clock). Pre-C8 that painted the
    # reply above the tools; the key pins operator < elements < terminal.
    rows = [
        {"id": "reply", "anchor": "t1", "turn_seq": TURN_SEQ_TERMINAL, "ts": "2026-07-17T10:00:01Z"},
        {"id": "tool", "anchor": "t1", "turn_seq": 2, "ts": "2026-07-17T10:00:05Z"},
        {"id": "seg", "anchor": "t1", "turn_seq": 1, "ts": "2026-07-17T10:00:04Z"},
        {"id": "op", "anchor": "t1", "turn_seq": TURN_SEQ_OPERATOR, "ts": "2026-07-17T10:00:03Z"},
    ]
    assert _ids(_order(rows)) == ["op", "seg", "tool", "reply"]


def test_content_band_sits_between_operator_and_terminal():
    rows = [
        {"id": "band-b", "anchor": "t1", "turn_seq": TURN_SEQ_CONTENT, "ts": "2026-07-17T10:00:09Z"},
        {"id": "reply", "anchor": "t1", "turn_seq": TURN_SEQ_TERMINAL, "ts": "2026-07-17T10:00:02Z"},
        {"id": "band-a", "anchor": "t1", "turn_seq": TURN_SEQ_CONTENT, "ts": "2026-07-17T10:00:08Z"},
        {"id": "op", "anchor": "t1", "turn_seq": TURN_SEQ_OPERATOR, "ts": "2026-07-17T10:00:07Z"},
    ]
    # Band peers keep their relative fallback order; the terminal still closes.
    assert _ids(_order(rows)) == ["op", "band-a", "band-b", "reply"]


def test_unkeyed_rows_keep_fallback_order():
    rows = [
        {"id": "b", "ts": "2026-07-17T10:00:02Z"},
        {"id": "a", "ts": "2026-07-17T10:00:01Z"},
        {"id": "c", "ts": "2026-07-17T10:00:03Z"},
    ]
    assert _ids(_order(rows)) == ["a", "b", "c"]


def test_turns_keep_their_transcript_position_among_unkeyed_rows():
    rows = [
        {"id": "old-1", "ts": "2026-07-17T09:00:00Z"},
        {"id": "t1-op", "anchor": "t1", "turn_seq": TURN_SEQ_OPERATOR, "ts": "2026-07-17T10:00:00Z"},
        {"id": "t1-reply", "anchor": "t1", "turn_seq": TURN_SEQ_TERMINAL, "ts": "2026-07-17T10:00:01Z"},
        {"id": "old-2", "ts": "2026-07-17T11:00:00Z"},
        {"id": "t2-op", "anchor": "t2", "turn_seq": TURN_SEQ_OPERATOR, "ts": "2026-07-17T12:00:00Z"},
        {"id": "t2-reply", "anchor": "t2", "turn_seq": TURN_SEQ_TERMINAL, "ts": "2026-07-17T12:00:05Z"},
    ]
    assert _ids(_order(rows)) == ["old-1", "t1-op", "t1-reply", "old-2", "t2-op", "t2-reply"]


def test_shuffle_sabotage_output_is_input_order_independent():
    # The capstone shuffle check: feed the same rows in ANY arrival order and
    # the rendered order is identical — the key sorts it, not arrival. Note
    # every keyed row of turn t1 shares ONE second-granularity timestamp (the
    # live worst case): only the key can order them.
    rows = [
        {"id": "t1-op", "anchor": "t1", "turn_seq": TURN_SEQ_OPERATOR, "ts": "2026-07-17T10:00:00Z"},
        {"id": "t1-seg", "anchor": "t1", "turn_seq": 1, "ts": "2026-07-17T10:00:00Z"},
        {"id": "t1-tool", "anchor": "t1", "turn_seq": 2, "ts": "2026-07-17T10:00:00Z"},
        {"id": "t1-reply", "anchor": "t1", "turn_seq": TURN_SEQ_TERMINAL, "ts": "2026-07-17T10:00:00Z"},
        {"id": "legacy", "ts": "2026-07-17T09:59:00Z"},
        {"id": "t2-op", "anchor": "t2", "turn_seq": TURN_SEQ_OPERATOR, "ts": "2026-07-17T10:05:00Z"},
        {"id": "t2-reply", "anchor": "t2", "turn_seq": TURN_SEQ_TERMINAL, "ts": "2026-07-17T10:05:01Z"},
    ]
    expected = _ids(_order(rows))
    assert expected == [
        "legacy",
        "t1-op",
        "t1-seg",
        "t1-tool",
        "t1-reply",
        "t2-op",
        "t2-reply",
    ]
    rng = random.Random(0xC8)
    for _ in range(25):
        shuffled = list(rows)
        rng.shuffle(shuffled)
        assert _ids(_order(shuffled)) == expected


def test_f17_repro_old_wall_clock_merge_reorders_new_key_holds():
    """The F17 repro, hermes half.

    One turn whose rows carry the two-clock skew observed live: tool rows on
    the trace clock land AFTER the reply's SessionDB stamp. The pre-C8 merge —
    plain (timestamp, index) sort, exactly `_ordered_message_rows`' old body —
    paints the reply BETWEEN the tools it followed. The key-sorted merge holds
    the emitted order; the un-keyed pre-C8 ack residue keeps its fallback slot.
    """

    rows = [
        {"id": "op", "anchor": "t1", "turn_seq": TURN_SEQ_OPERATOR, "ts": "2026-07-17T10:00:00Z"},
        {"id": "ack", "ts": "2026-07-17T10:00:00Z"},  # pre-C8 residue: no key
        {"id": "tool-1", "anchor": "t1", "turn_seq": 1, "ts": "2026-07-17T10:00:04Z"},
        {"id": "tool-2", "anchor": "t1", "turn_seq": 2, "ts": "2026-07-17T10:00:06Z"},
        {"id": "reply", "anchor": "t1", "turn_seq": TURN_SEQ_TERMINAL, "ts": "2026-07-17T10:00:05Z"},
    ]

    def _old_merge(items):
        # Pre-C8 `_ordered_message_rows`: (timestamp, original index).
        keyed = [(str(r.get("ts") or ""), i, r) for i, r in enumerate(items)]
        keyed.sort(key=lambda item: (item[0], item[1]))
        return [r for _, _, r in keyed]

    def _new_merge(items):
        # The new `_ordered_message_rows`: the SAME projection fallback
        # (timestamp, original index) under the key-grouped ordering.
        return order_transcript_rows(
            items,
            anchor=lambda row: row.get("anchor"),
            turn_seq=lambda row: row.get("turn_seq"),
            fallback_key=lambda row, index: (str(row.get("ts") or ""), index),
        )

    # RED (the bug): the old merge interleaves the reply BEFORE tool-2.
    assert _ids(_old_merge(rows)) == ["op", "ack", "tool-1", "reply", "tool-2"]
    # GREEN (the fix): the key pins the emitted order and the turn renders as
    # ONE contiguous block. The un-keyed pre-C8 ack residue that fell inside
    # the turn's clock span follows the block (the launcher's persisted-residue
    # collapse hides mid-turn acks regardless — see _isPersistedThinkingAck).
    assert _ids(_new_merge(rows)) == ["op", "tool-1", "tool-2", "reply", "ack"]


def test_pre_trace_ack_text_matches_persisted_copy():
    # The presentation copy moved from persona_commands (exec'd) into this
    # importable module; pin the tool-family variants.
    assert pre_trace_ack_text({"tool_name": "skill_view"}).startswith(
        "I'll load the relevant guidance"
    )
    assert "`git status`" in pre_trace_ack_text(
        {"tool_name": "terminal", "command_label": "git status"}
    )
    assert pre_trace_ack_text({}) == "I'll check that now and report back with what I find."


# --------------------------------------------------------------------------- #
# History projection stamps the key                                           #
# --------------------------------------------------------------------------- #


class _FakeSessionDB:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self, session_id, include_inactive=False):
        return list(self._messages)


def test_history_rows_carry_turn_anchor_and_intra_turn_seq():
    db = _FakeSessionDB(
        [
            {
                "role": "user",
                "content": "run the checks",
                "platform_message_id": "turn-a",
                "created_at": 1752741600.0,
            },
            {
                "role": "assistant",
                "content": "All checks passed.",
                "platform_message_id": "turn-a",
                "created_at": 1752741610.0,
            },
        ]
    )
    rows, _status = _safe_recent_messages(db, session_id="s1")
    assert [r["role"] for r in rows] == ["operator", "agent"]
    assert rows[0]["client_message_id"] == "turn-a"
    assert rows[0]["turn_seq"] == TURN_SEQ_OPERATOR
    assert rows[1]["turn_seq"] == TURN_SEQ_TERMINAL


def test_pre_c8_ack_rows_stay_unkeyed_residue():
    db = _FakeSessionDB(
        [
            {
                "role": "assistant",
                "content": "I'll check that now and report back with what I find.",
                "finish_reason": "pre_trace_ack",
                "created_at": 1752741600.0,
            },
        ]
    )
    rows, _status = _safe_recent_messages(db, session_id="s1")
    assert rows[0]["kind"] == "pre_trace_ack"
    assert "turn_seq" not in rows[0]
    assert "client_message_id" not in rows[0]


def test_history_projection_orders_reply_after_late_stamped_operator_row():
    # Reply stamped EARLIER than its own operator row (clock skew) — the key
    # still renders the operator message first.
    db = _FakeSessionDB(
        [
            {
                "role": "assistant",
                "content": "Done.",
                "platform_message_id": "turn-a:assistant:1",
                "created_at": 1752741599.0,
            },
            {
                "role": "user",
                "content": "go",
                "platform_message_id": "turn-a",
                "created_at": 1752741600.0,
            },
        ]
    )
    rows, _status = _safe_recent_messages(db, session_id="s1")
    assert [r["role"] for r in rows] == ["operator", "agent"]
    assert [r["turn_id"] for r in rows] == ["turn-a", "turn-a"]
    assert rows[1]["client_message_id"] == "turn-a:assistant:1"


def test_history_projection_joins_turn_elements_through_assistant_persistence_id(
    isolate_agent_runtime_root,
):
    turn_id = "agent-chat-send-1784795889013735"
    persist_mission_chat_turn(
        session_id="s-assistant-id",
        client_message_id=turn_id,
        turn_id=turn_id,
        elements=[
            {
                "kind": "segment",
                "id": f"{turn_id}_seg_1",
                "turn_id": turn_id,
                "seq": 1,
                "text": "Hi Tony — Neko here.",
            }
        ],
        state="completed",
    )
    db = _FakeSessionDB(
        [
            {
                "role": "assistant",
                "content": "Hi Tony — Neko here.",
                "platform_message_id": f"{turn_id}:assistant:1",
                "created_at": 1784795889.0,
            }
        ]
    )

    rows, _status = _safe_recent_messages(db, session_id="s-assistant-id")

    assert rows[0]["turn_id"] == turn_id
    assert rows[0]["turn_elements"][0]["turn_id"] == turn_id


def test_interrupted_marker_is_the_turns_terminal_row(isolate_agent_runtime_root):
    persist_mission_chat_turn(
        session_id="s-int",
        client_message_id="turn-x",
        turn_id="turn-x",
        elements=[],
        state="running",
        write_ahead=True,
    )
    persist_mission_chat_turn(
        session_id="s-int",
        client_message_id="turn-x",
        turn_id="turn-x",
        elements=[],
        state="interrupted",
    )
    rows = _terminal_turn_marker_rows(
        session_id="s-int", assistant_client_message_ids=set()
    )
    assert len(rows) == 1
    assert rows[0]["kind"] == "turn_interrupted"
    assert rows[0]["turn_seq"] == TURN_SEQ_TERMINAL


def test_ordered_message_rows_uses_key_over_timestamp():
    rows = [
        {
            "id": "reply",
            "role": "agent",
            "text": "done",
            "timestamp": "2026-07-17T10:00:01Z",
            "client_message_id": "turn-a:assistant:1",
            "turn_id": "turn-a",
            "turn_seq": TURN_SEQ_TERMINAL,
        },
        {
            "id": "interrupt-other",
            "role": "system",
            "text": "interrupted",
            "timestamp": "2026-07-17T10:00:03Z",
            "client_message_id": "turn-b",
            "turn_seq": TURN_SEQ_TERMINAL,
        },
        {
            "id": "op",
            "role": "operator",
            "text": "go",
            "timestamp": "2026-07-17T10:00:02Z",
            "client_message_id": "turn-a",
            "turn_seq": TURN_SEQ_OPERATOR,
        },
    ]
    ordered = _ordered_message_rows(rows)
    assert [r["id"] for r in ordered] == ["op", "reply", "interrupt-other"]


# --------------------------------------------------------------------------- #
# Turn store: write-ahead start anchor + start-ordered replay                 #
# --------------------------------------------------------------------------- #


def test_started_at_stamped_at_write_ahead_and_preserved(isolate_agent_runtime_root):
    persist_mission_chat_turn(
        session_id="s-start",
        client_message_id="m1",
        turn_id="m1",
        elements=[],
        state="running",
        write_ahead=True,
    )
    record = mission_chat_turn_record(session_id="s-start", client_message_id="m1")
    started = record.get("started_at")
    assert started, "write-ahead must stamp the turn-start anchor"
    persist_mission_chat_turn(
        session_id="s-start",
        client_message_id="m1",
        turn_id="m1",
        elements=[
            {"kind": "segment", "id": "m1_seg_1", "turn_id": "m1", "seq": 1, "text": "hi"}
        ],
        state="running",
    )
    persist_mission_chat_turn(
        session_id="s-start",
        client_message_id="m1",
        turn_id="m1",
        elements=[
            {"kind": "segment", "id": "m1_seg_1", "turn_id": "m1", "seq": 1, "text": "hi"}
        ],
        state="completed",
    )
    record = mission_chat_turn_record(session_id="s-start", client_message_id="m1")
    assert record["state"] == "completed"
    assert record["started_at"] == started, (
        "flush/terminal persists must carry the write-ahead start anchor unchanged"
    )


def test_replay_orders_by_turn_start_not_settle_time(isolate_agent_runtime_root):
    # Turn A starts first but settles LAST (long turn); turn B starts second
    # and settles first. Pre-C8 replay ordered by settle time → B, A. The
    # start anchor orders replay A, B — what the operator actually did.
    persist_mission_chat_turn(
        session_id="s-replay",
        client_message_id="turn-a",
        turn_id="turn-a",
        elements=[],
        state="running",
        write_ahead=True,
    )
    persist_mission_chat_turn(
        session_id="s-replay",
        client_message_id="turn-b",
        turn_id="turn-b",
        elements=[],
        state="running",
        write_ahead=True,
    )
    persist_mission_chat_turn(
        session_id="s-replay",
        client_message_id="turn-b",
        turn_id="turn-b",
        elements=[],
        state="completed",
    )
    persist_mission_chat_turn(
        session_id="s-replay",
        client_message_id="turn-a",
        turn_id="turn-a",
        elements=[],
        state="completed",
    )
    records = mission_chat_turn_records(session_id="s-replay")
    assert [r["client_message_id"] for r in records] == ["turn-a", "turn-b"]
    # Sanity: settle order really was B-then-A (the old key would have flipped).
    assert records[0]["updated_at"] >= records[1]["updated_at"]


def test_pre_c8_records_without_start_fall_back_to_settle_time(
    isolate_agent_runtime_root,
):
    # A record persisted WITHOUT write-ahead (legacy shape) never gains a
    # started_at; replay falls back to its settle time — no fabricated keys.
    persist_mission_chat_turn(
        session_id="s-legacy",
        client_message_id="old-turn",
        turn_id="old-turn",
        elements=[],
        state="completed",
    )
    record = mission_chat_turn_record(session_id="s-legacy", client_message_id="old-turn")
    assert "started_at" not in record
    records = mission_chat_turn_records(session_id="s-legacy")
    assert [r["client_message_id"] for r in records] == ["old-turn"]


# --------------------------------------------------------------------------- #
# Conversation projection: key-aware contract order                           #
# --------------------------------------------------------------------------- #


def _history_for_conversation():
    return {
        "messages": [
            {
                "id": "h-op",
                "role": "operator",
                "text": "run the release checks",
                "timestamp": "2026-07-17T10:00:00Z",
                "client_message_id": "turn-a",
                "turn_seq": TURN_SEQ_OPERATOR,
            },
            {
                "id": "h-reply",
                # Stamped EARLIER than the tool trace ts below — the live F17
                # skew (SessionDB stamp vs trace clock).
                "role": "agent",
                "text": "Checks passed.",
                "timestamp": "2026-07-17T10:00:03Z",
                "client_message_id": "turn-a:assistant:1",
                "turn_seq": TURN_SEQ_TERMINAL,
            },
        ]
    }


def _trace_for_conversation():
    return {
        "entries": [
            {
                "kind": "harness_trace",
                "event": "tool_finished",
                "tool_name": "terminal",
                "summary": "Finished tool terminal: passed",
                "status": "passed",
                "ts": "2026-07-17T10:00:05Z",
                "turn_id": "turn-a",
                "run_id": "run_1",
            },
        ]
    }


def test_conversation_contract_orders_tool_call_before_its_reply():
    contract = _conversation_contract(
        channel_id="session:s1",
        persona_id="dev",
        persona_instance_id="personainst_dev",
        session_id="s1",
        task_id=None,
        goal_id=None,
        title="Dev chat",
        state="open",
        history=_history_for_conversation(),
        trace=_trace_for_conversation(),
    )
    kinds = [message["kind"] for message in contract["messages"]]
    assert kinds == ["operator_message", "tool_call", "reply"], kinds
    # The wire carries the key: intra positions + the projection-order seq.
    by_kind = {message["kind"]: message for message in contract["messages"]}
    assert by_kind["operator_message"]["turn_seq"] == TURN_SEQ_OPERATOR
    assert by_kind["tool_call"]["turn_seq"] == TURN_SEQ_CONTENT
    assert by_kind["reply"]["turn_seq"] == TURN_SEQ_TERMINAL
    assert [message["seq"] for message in contract["messages"]] == [1, 2, 3]


def test_conversation_contract_order_is_stable_under_source_shuffle():
    baseline = None
    rng = random.Random(0xF17)
    for _ in range(10):
        history = _history_for_conversation()
        rng.shuffle(history["messages"])
        contract = _conversation_contract(
            channel_id="session:s1",
            persona_id="dev",
            persona_instance_id="personainst_dev",
            session_id="s1",
            task_id=None,
            goal_id=None,
            title="Dev chat",
            state="open",
                history=history,
            trace=_trace_for_conversation(),
        )
        ids = [message["id"] for message in contract["messages"]]
        if baseline is None:
            baseline = ids
        assert ids == baseline
