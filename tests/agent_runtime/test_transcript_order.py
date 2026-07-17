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
  holds stable (red on the old logic, green on the new — recorded inline);
* the projections stamping the key;
* the turn store's write-ahead `started_at` anchor + start-ordered replay;
* cross-representation agreement for one scripted turn.
"""

from __future__ import annotations

import random

import pytest

from agent_runtime.mission_chat_turns import (
    mission_chat_turn_record,
    mission_chat_turn_records,
    persist_mission_chat_turn,
)
from agent_runtime.operator_channels import _conversation_contract
from agent_runtime.persona_chat_history import (
    _interrupted_turn_rows,
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
