# Mission Board CLI tier: `hermes harness board …`.
#
# This module is exec'd into hermes_cli/harness.py's globals (see
# _load_command_parts), so it reuses the shared Stage-42 envelope/printer/error
# helpers (_print_stage42, _list_envelope, _object_envelope, emit_harness_error,
# _load_request_json, _sort_rows) defined there. Skinny rows by default (≤7
# keys); --full drills into card bodies. All writes go through the BoardStore
# chokepoint — the same one the launcher capability lane and agent tools use.
#
# Board resolution rule (shared with agent tools, spec §10): explicit --board >
# the active workspace's default board. A card verb resolves the owning board
# from the card id itself.


def _board_store():
    from agent_runtime.board_store import BoardStore

    return BoardStore()


def _board_active_card_count(store, board) -> int:
    return len([c for c in store.list_cards(board.board_id) if _column_kind(board, c.column_id) != "done"])


def _column_kind(board, column_id: str) -> str:
    for column in board.columns:
        if column.column_id == column_id:
            return column.kind
    return "custom"


def _board_row(store, board, *, full: bool = False) -> dict:
    row = {
        "id": board.board_id,
        "workspace_id": board.workspace_id,
        "title": board.title,
        "columns": len(board.columns),
        "active_cards": _board_active_card_count(store, board),
        "revision": board.revision,
        "updated_at": board.updated_at,
    }
    if full:
        row["column_defs"] = [
            {"column_id": c.column_id, "title": c.title, "kind": c.kind, "wip_limit": c.wip_limit}
            for c in board.columns
        ]
        row["cards"] = [_card_row(card) for card in store.list_cards(board.board_id)]
        row["archived_card_ids"] = list(board.archived_card_ids)
    return row


def _card_row(card, *, full: bool = False) -> dict:
    row = {
        "id": card.card_id,
        "column_id": card.column_id,
        "title": card.title,
        "priority": card.priority,
        "state": card.state,
        "updated_at": card.updated_at,
    }
    if full:
        row.update(
            {
                "board_id": card.board_id,
                "description": card.description,
                "labels": list(card.labels),
                "assignee": card.assignee,
                "checklist": list(card.checklist),
                "order_key": card.order_key,
                "created_by": card.created_by,
                "created_at": card.created_at,
                "revision": card.revision,
            }
        )
    return row


def _resolve_board_id_for_read(store, args) -> str | None:
    board_id = getattr(args, "board", None) or getattr(args, "board_id", None)
    if board_id:
        return board_id
    workspace = getattr(args, "workspace", None) or WorkspaceStore().active_id()
    if not workspace:
        return None
    from agent_runtime import board_models

    return board_models.default_board_id(workspace)


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
