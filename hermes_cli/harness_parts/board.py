# Mission Board CLI tier: `hermes harness board …`.
#
# This module is exec'd into hermes_cli/harness.py's globals (see
# _load_command_parts) and shares the Stage-42 envelope/printer/error helpers
# with every other tier — imported from hermes_cli.harness_support below, not
# inherited. Skinny rows by default (≤7 keys); --full drills into card bodies.
# All writes go through the BoardStore chokepoint — the same one the launcher
# capability lane and agent tools use.
#
# Board resolution rule (shared with agent tools, spec §10): explicit --board >
# the active workspace's default board. A card verb resolves the owning board
# from the card id itself.

# Explicit import header. Still exec'd into harness.py's globals by
# _load_command_parts — that mechanism is unchanged — but no longer dependent
# on it: these names used to arrive implicitly from whatever harness.py
# imported, so a wrong one surfaced as a NameError only when an operator ran
# the one verb that touched it. Re-importing a name harness.py also imports
# rebinds it to the identical object; both halves are checked by
# tests/hermes_cli/test_harness_parts_namespace.py.
#
# Snapshot row builders (``board_summary_row`` / ``_board_card_row``) are
# imported FUNCTION-LOCALLY rather than here on purpose: a module-level import
# in an exec'd part binds the name into harness.py's shared globals for every
# other tier, which is the shadowing surface the namespace guard exists to
# police. Same convention ``_board_store`` already follows.

from __future__ import annotations

from agent_runtime.store import WorkspaceStore
from hermes_cli.harness_support import (
    _list_envelope,
    _load_request_json,
    _object_envelope,
    _print_stage42,
    _sort_rows,
    emit_harness_error,
)
from hermes_time import now


def _board_store():
    from agent_runtime.board_store import BoardStore

    return BoardStore()


def _board_active_card_count(board, cards) -> int:
    """Cards NOT parked in a ``done`` column.

    Deliberately NOT the snapshot row's ``active_card_count`` — that one counts
    every non-archived card. Two different questions with confusingly similar
    names; the CLI column keeps its own meaning rather than silently changing
    the number an operator reads. Recorded as a residual on ledger item 4.
    """

    return len([c for c in cards if _column_kind(board, c.column_id) != "done"])


def _column_kind(board, column_id: str) -> str:
    for column in board.columns:
        if column.column_id == column_id:
            return column.kind
    return "custom"


def _board_row(store, board, *, full: bool = False) -> dict:
    """`board list|show|create|update` row — a RE-KEY of the snapshot's own
    ``board_summary_row`` (S48, ledger item 4).

    Before this, ``--full`` printed EVERY active card through an unmasked,
    uncapped ``_card_row``; the wire had been capping at
    ``MAX_BOARD_CARDS_PROJECTED`` and masking card prose the whole time. The
    cap is now the builder's and is ACCOUNTED, never silent: ``cards_truncated``
    rides the full row whenever cards were cut.

    Imported inside the function: this module is exec'd into ``harness.py``'s
    globals, and a module-level import here would bind the builder into that
    shared namespace for every other tier. ``store``/``board`` stay in the
    signature because the CLI's own ``active_cards`` column (a different
    question — see ``_board_active_card_count``) is not on the builder's row.
    """

    from agent_runtime.snapshot import board_summary_row

    # ``scan_cards``, not ``list_cards``: the CLI row states the same
    # completeness the wire row does, so the two cannot disagree about how much
    # of the board they just rendered.
    scan = store.scan_cards(board.board_id)
    cards = scan.cards
    summary = board_summary_row(board, cards, cards_unreadable=scan.unreadable)
    row = {
        "id": summary["board_id"],
        "workspace_id": summary["workspace_id"],
        "title": summary["title"],
        "columns": len(summary["columns"]),
        "active_cards": _board_active_card_count(board, cards),
        "revision": summary["revision"],
        "updated_at": board.updated_at,
    }
    if full:
        row["column_defs"] = summary["columns"]
        by_id = {card.card_id: card for card in cards}
        row["cards"] = [_card_row(by_id[projected["card_id"]], summary=projected) for projected in summary["cards"]]
        row["cards_truncated"] = summary["cards_truncated"]
        row["archived_card_ids"] = summary["archived_card_ids"]
    return row


def _card_row(card, *, full: bool = False, summary: dict | None = None) -> dict:
    """One card row — a RE-KEY of the snapshot's own ``_board_card_row``.

    Card prose is the one genuinely SENSITIVE payload in this tier, and the CLI
    used to print ``card.title`` / ``card.description`` / ``card.checklist``
    raw while the wire masked all three. Masking is now inherited, so it is
    value-level and IN PLACE — ``"Rotate api_key: sk-live-…"`` renders
    ``"Rotate api_key: [redacted]"``, never a blanked field. Description
    truncation likewise carries ``description_truncated`` on the full row: the
    store accepts 4,000 characters and the projection bound is 2,048, so the
    cut is real and is named.

    ``summary`` lets ``_board_row`` pass the row the board builder ALREADY
    projected for this card (it owns the per-board cap), instead of projecting
    it a second time.
    """

    if summary is None:
        from agent_runtime.snapshot import _board_card_row

        summary = _board_card_row(card)
    row = {
        "id": summary["card_id"],
        "column_id": summary["column_id"],
        "title": summary["title"],
        "priority": summary["priority"],
        "state": summary["state"],
        "updated_at": card.updated_at,
    }
    if full:
        row.update(
            {
                key: summary[key]
                for key in (
                    "board_id",
                    "description",
                    "description_truncated",
                    "labels",
                    "assignee",
                    "checklist",
                    "order_key",
                    "created_by",
                    "revision",
                )
            }
        )
        row["created_at"] = card.created_at
    return row


def _cmd_board_list(args) -> int:
    store = _board_store()
    workspace = getattr(args, "workspace", None)
    boards = store.list_for_workspace(workspace) if workspace else store.list_all()
    rows = [_board_row(store, board) for board in boards]
    _print_stage42(_list_envelope("board", _sort_rows(rows, getattr(args, "sort", None))), args=args)
    return 0


def _cmd_board_show(args) -> int:
    store = _board_store()
    full = bool(getattr(args, "full", False))
    board = store.get(args.board_id)
    _print_stage42(_object_envelope("board", _board_row(store, board, full=full)), args=args)
    return 0


def _cmd_board_create(args) -> int:
    store = _board_store()
    if getattr(args, "dry_run", False):
        from agent_runtime import board_models

        row = {"id": f"board_dry_{args.workspace}", "workspace_id": args.workspace, "title": args.title or board_models.DEFAULT_BOARD_TITLE, "columns": 4, "active_cards": 0, "updated_at": now()}
        _print_stage42(_object_envelope("board", row), args=args, default_output="json")
        return 0
    board = store.create(workspace_id=args.workspace, title=getattr(args, "title", None))
    _print_stage42(_object_envelope("board", _board_row(store, board)), args=args, default_output="json")
    return 0


def _cmd_board_update(args) -> int:
    store = _board_store()
    columns = None
    if getattr(args, "columns_json", None):
        from agent_runtime.models import BoardColumn
        from agent_runtime import board_models

        raw = _load_request_json(args.columns_json)
        if not isinstance(raw, list):
            return emit_harness_error(ValueError("--columns-json must be a JSON array of columns"), args=args, code="invalid_request")
        columns = [
            BoardColumn(
                column_id=str(item.get("column_id") or f"col_{i}"),
                title=str(item.get("title") or "Column"),
                kind=board_models.normalize_kind(item.get("kind")),
                wip_limit=item.get("wip_limit") if isinstance(item.get("wip_limit"), int) else None,
            )
            for i, item in enumerate(raw)
            if isinstance(item, dict)
        ]
    board = store.update_board(
        args.board_id,
        title=getattr(args, "title", None),
        columns=columns,
        expect_revision=getattr(args, "expect_revision", None),
    )
    _print_stage42(_object_envelope("board", _board_row(store, board, full=True)), args=args, default_output="json")
    return 0


def _cmd_board_card_add(args) -> int:
    store = _board_store()
    if getattr(args, "dry_run", False):
        row = {"id": "card_dry", "column_id": getattr(args, "column", None) or "col_queued", "title": args.title, "priority": getattr(args, "priority", None) or "p2", "state": "active", "updated_at": now()}
        _print_stage42(_object_envelope("card", row), args=args, default_output="json")
        return 0
    labels = [part.strip() for part in (getattr(args, "labels", None) or "").split(",") if part.strip()] or None
    card = store.add_card(
        board_id=getattr(args, "board", None),
        workspace_id=getattr(args, "workspace", None) or WorkspaceStore().active_id(),
        title=args.title,
        description=getattr(args, "description", "") or "",
        column=getattr(args, "column", None),
        priority=getattr(args, "priority", None),
        labels=labels,
        assignee=getattr(args, "assignee", None),
        created_by=getattr(args, "created_by", None) or "operator",
        idempotency_key=getattr(args, "idempotency_key", None),
    )
    _print_stage42(_object_envelope("card", _card_row(card, full=True)), args=args, default_output="json")
    return 0


def _cmd_board_card_edit(args) -> int:
    store = _board_store()
    labels = None
    if getattr(args, "labels", None) is not None:
        labels = [part.strip() for part in args.labels.split(",") if part.strip()]
    card = store.edit_card(
        args.card_id,
        title=getattr(args, "title", None),
        description=getattr(args, "description", None),
        priority=getattr(args, "priority", None),
        labels=labels,
        assignee=getattr(args, "assignee", None),
        clear_assignee=bool(getattr(args, "clear_assignee", False)),
        expect_revision=getattr(args, "expect_revision", None),
        idempotency_key=getattr(args, "idempotency_key", None),
    )
    _print_stage42(_object_envelope("card", _card_row(card, full=True)), args=args, default_output="json")
    return 0


def _cmd_board_card_move(args) -> int:
    store = _board_store()
    card = store.move_card(
        args.card_id,
        column_id=args.column,
        before=getattr(args, "before", None),
        after=getattr(args, "after", None),
        expect_revision=getattr(args, "expect_revision", None),
        idempotency_key=getattr(args, "idempotency_key", None),
    )
    _print_stage42(_object_envelope("card", _card_row(card, full=True)), args=args, default_output="json")
    return 0


def _cmd_board_card_archive(args) -> int:
    store = _board_store()
    card = store.archive_card(args.card_id, reason="operator")
    _print_stage42(_object_envelope("card", _card_row(card, full=True)), args=args, default_output="json")
    return 0


def _cmd_board_card_restore(args) -> int:
    store = _board_store()
    card = store.restore_card(args.card_id)
    _print_stage42(_object_envelope("card", _card_row(card, full=True)), args=args, default_output="json")
    return 0


def _cmd_board_resolve_conflict(args) -> int:
    store = _board_store()
    card = store.resolve_conflict(args.card_id, take=args.take)
    row = _card_row(card, full=True) if card is not None else {"id": args.card_id, "state": "archived", "take": args.take}
    _print_stage42(_object_envelope("card", row), args=args, default_output="json")
    return 0
