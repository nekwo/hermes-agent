"""One transcript ordering authority (console-chat C8, 2026-07-17).

Operator ruling (F17, verbatim): "we need to make sure there is only one
authority." Every transcript-visible item is ordered by ONE turn-scoped key
stamped here, hermes-side — the canonical conversation/history projections,
the turn store, the live v2 stream, and the Launcher fold all sort on it.
No consumer applies wall-clock judgment to keyed rows ever again.

The key:

* **turn anchor** — the turn's ``client_message_id`` (its token form is the
  ``turn_id`` the v2 emitter and the turn store already carry; launcher-minted
  ids are token-safe, so the two spellings are byte-equal in practice).
* **intra-turn position** (``turn_seq``) — where an item sits inside its turn:

  - ``TURN_SEQ_OPERATOR`` (0) — the operator message that opens the turn.
  - ``1..N`` — the v2 emitter's element ``seq`` (segments/tools; already
    stamped on stream frames, turn-store elements, and projected
    ``turn_elements``).
  - ``TURN_SEQ_CONTENT`` (500000) — turn-anchored rows that carry no emitter
    seq (trace-derived tool calls / thinking rows). They keep their relative
    fallback order among themselves but always land after the operator row
    and before the terminal row.
  - ``TURN_SEQ_TERMINAL`` (1000000) — the turn's terminal row: the recorded
    reply, or a terminal-turn marker (``turn_interrupted`` /
    ``budget_exhausted``; see ``persona_chat_history.TERMINAL_TURN_MARKERS``).
    A turn has one or the other, never both.

Rows that predate the key (pre-C8 persisted history) carry no anchor/seq and
order by the caller's existing fallback — honest fallback, never a fabricated
key (archive-never-delete).
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

TURN_SEQ_OPERATOR = 0
TURN_SEQ_CONTENT = 500_000
TURN_SEQ_TERMINAL = 1_000_000


def order_transcript_rows(
    rows: Sequence[dict[str, Any]],
    *,
    anchor: Callable[[dict[str, Any]], str | None],
    turn_seq: Callable[[dict[str, Any]], int | None],
    fallback_key: Callable[[dict[str, Any], int], Any],
) -> list[dict[str, Any]]:
    """Order transcript rows by the one turn-scoped key.

    Algorithm (deterministic; input-shuffle-proof for keyed rows whenever
    ``fallback_key`` itself is content-derived):

    1. Rank every row by ``fallback_key(row, index)`` — the caller's existing
       (pre-C8) ordering. This alone orders unkeyed rows and anchors where a
       turn sits among its neighbours.
    2. Rows carrying BOTH an anchor and an intra-turn ``turn_seq`` form turn
       groups. A group's position is its earliest member's fallback rank
       (turns keep their transcript position; the key never teleports a turn).
    3. Final order: ``(group position, intra-turn seq, fallback rank)`` —
       within a turn the stamped position wins over any clock; everything
       else keeps the fallback order.

    A keyed turn therefore renders as ONE contiguous block: an unkeyed row
    whose fallback rank fell inside a turn's clock span follows the block.
    Post-C8 writers never produce that mix (a live turn's rows are all keyed);
    the only real case is pre-C8 ack residue, which consumers collapse anyway.
    """

    indexed = list(enumerate(rows))
    ranked = sorted(indexed, key=lambda item: fallback_key(item[1], item[0]))
    rank_by_index = {index: rank for rank, (index, _row) in enumerate(ranked)}

    group_rank: dict[str, int] = {}
    for index, row in indexed:
        row_anchor = anchor(row)
        if not row_anchor or turn_seq(row) is None:
            continue
        rank = rank_by_index[index]
        current = group_rank.get(row_anchor)
        if current is None or rank < current:
            group_rank[row_anchor] = rank

    def _sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        index, row = item
        rank = rank_by_index[index]
        row_anchor = anchor(row)
        seq = turn_seq(row)
        if row_anchor and seq is not None and row_anchor in group_rank:
            return (group_rank[row_anchor], seq, rank)
        return (rank, TURN_SEQ_OPERATOR, rank)

    return [row for _index, row in sorted(indexed, key=_sort_key)]


def pre_trace_ack_text(trace_payload: dict[str, Any]) -> str:
    """Canned pre-trace acknowledgment copy, minted hermes-side.

    C8: the ack is a presentation-only protocol-v2 stream frame (``turn.ack``)
    — it never enters SessionDB or the turn store, renders live, and is
    superseded the moment real turn content arrives. Replay never shows it.
    """

    def _token(value: Any) -> str:
        text = "".join(
            ch if (ch.isalnum() or ch in {"_", "-", "."}) else "_"
            for ch in str(value or "").strip()
        )
        return text.strip("._-")[:120]

    def _text(value: Any, limit: int) -> str:
        text = " ".join(str(value or "").replace("\x00", " ").split())
        return text[:limit]

    tool_name = _token(trace_payload.get("tool_name") or trace_payload.get("tool"))
    command_label = _text(trace_payload.get("command_label"), 160)
    if tool_name in {"skill_view", "skills_list", "skill_search"}:
        return "I'll load the relevant guidance first, then report back with the useful part."
    if tool_name in {"terminal", "shell_command", "execute_code"}:
        if command_label:
            return f"I'll run `{command_label}` now, then report back with the result."
        return "I'll run the check now, then report back with the result."
    if tool_name in {"read_file", "search_files", "find_files", "session_search"}:
        return "I'll inspect the relevant context now, then report back with what I find."
    return "I'll check that now and report back with what I find."
