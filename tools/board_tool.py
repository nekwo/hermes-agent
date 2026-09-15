#!/usr/bin/env python3
"""Mission Board chat tools — let an agent track follow-up work as board cards.

The board is ADVISORY (nudged, never forced): nothing in the run loop requires
these tools; a mission runs identically with zero cards. They join the
mission-chat tool surface (toolset ``board``) so an agent MAY add/list cards when it notices work worth
tracking — exactly like a human teammate.

Board resolution (§10): explicit ``board_id`` arg > the active workspace's
default board; typed ``invalid_request`` when none resolves — never a silent
cross-workspace write. §10's middle rung — the lane's bound goal's workspace
default board — is GONE with the mission lane, so `task_id` no longer
participates: `agent_runtime.store` exports `TaskStore` as `TaskStoreStub`,
whose `.get()` always raises. Agent writes carry ``created_by=<persona_id>`` resolved
from the chat session, so operator- and agent-authored cards render distinctly.

Cards are planning state only and never start tracked work.
"""

import json
import logging

from tools.registry import registry

logger = logging.getLogger(__name__)


BOARD_CARD_ADD_SCHEMA = {
    "name": "board_card_add",
    "description": (
        "Add a planning CARD to the workspace Mission Board (Queued column, attributed to you) for follow-up work worth remembering. Advisory. A card is planning state only and never starts tracked work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short imperative card title."},
            "description": {"type": "string", "description": "Optional detail / context for the card."},
            "column": {"type": "string", "description": "Optional column id or kind (default: first queued column)."},
            "priority": {"type": "string", "enum": ["p0", "p1", "p2", "p3"], "description": "Optional priority (default p2)."},
            "board_id": {"type": "string", "description": "Optional explicit board id; defaults to the active workspace's board."},
        },
        "required": ["title"],
    },
}

BOARD_CARDS_SCHEMA = {
    "name": "board_cards",
    "description": (
        "List active cards on the workspace Mission Board (title, column, priority). Read-only -- check what is already tracked before adding a card."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "board_id": {"type": "string", "description": "Optional explicit board id; defaults to the active workspace's board."},
        },
        "required": [],
    },
}


def _persona_for_session(session_id):
    """Best-effort attribution: the persona bound to this chat session, or a
    generic ``agent`` label when it cannot be resolved (never spoofable by the
    model — resolved from the store, not from tool args)."""

    if not session_id:
        return "agent"
    try:
        from agent_runtime.persona_assignments import PersonaInstanceStore

        for instance in PersonaInstanceStore().list_all():
            if getattr(instance, "session_id", None) == session_id:
                return getattr(instance, "persona_id", None) or "agent"
    except Exception:  # noqa: BLE001 — attribution is best-effort
        logger.debug("board tool: could not resolve persona for session", exc_info=True)
    return "agent"


def _resolve_board_target(args, task_id):
    """Return ``(board_id, workspace_id, error)`` per the §10 resolution rule."""

    from agent_runtime import board_models
    from agent_runtime.board_store import BoardStore
    from agent_runtime.store import WorkspaceStore

    explicit = str(args.get("board_id") or "").strip()
    if explicit:
        if not BoardStore().exists(explicit):
            return None, None, "invalid_request: board_id not found"
        return explicit, None, None

    # The bound-goal rung is GONE, not merely unused: `agent_runtime.store`
    # exports `TaskStore` as `TaskStoreStub`, whose `.get()` is `-> NoReturn`
    # and always raises `NotFound`. The branch reading it could only ever fall
    # through to the line below, so `task_id` no longer participates in
    # resolution at all and the docstring above says two rungs, not three.
    active = WorkspaceStore().active_id()
    if active:
        return board_models.default_board_id(active), active, None

    return None, None, "invalid_request: no board_id or active workspace to resolve a board"


def board_card_add(args, **kwargs):
    title = str(args.get("title") or "").strip()
    if not title:
        return json.dumps({"ok": False, "error": "board_card_add requires a non-empty title."})
    task_id = kwargs.get("task_id")
    session_id = kwargs.get("session_id")
    board_id, workspace_id, error = _resolve_board_target(args, task_id)
    if error:
        return json.dumps({"ok": False, "error": error})
    created_by = _persona_for_session(session_id)
    try:
        from agent_runtime.board_store import BoardStore

        card = BoardStore().add_card(
            board_id=board_id if workspace_id is None else None,
            workspace_id=workspace_id,
            title=title,
            description=str(args.get("description") or ""),
            column=str(args.get("column") or "") or None,
            priority=str(args.get("priority") or "") or None,
            created_by=created_by,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced to the model
        logger.exception("board_card_add failed")
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return json.dumps(
        {
            "ok": True,
            "card_id": card.card_id,
            "board_id": card.board_id,
            "column_id": card.column_id,
            "title": card.title,
            "priority": card.priority,
            "created_by": card.created_by,
        },
        indent=2,
        default=str,
    )


def board_cards(args, **kwargs):
    task_id = kwargs.get("task_id")
    board_id, _workspace_id, error = _resolve_board_target(args, task_id)
    if error:
        return json.dumps({"ok": False, "error": error})
    try:
        from agent_runtime.board_store import BoardStore

        store = BoardStore()
        if not store.exists(board_id):
            return json.dumps({"ok": True, "board_id": board_id, "cards": []})
        board = store.get(board_id)
        column_titles = {c.column_id: c.title for c in board.columns}
        rows = [
            {
                "card_id": card.card_id,
                "column": column_titles.get(card.column_id, card.column_id),
                "title": card.title,
                "priority": card.priority,
                "created_by": card.created_by,
            }
            for card in store.list_cards(board_id)
        ]
    except Exception as exc:  # noqa: BLE001 — surfaced to the model
        logger.exception("board_cards failed")
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return json.dumps({"ok": True, "board_id": board_id, "cards": rows}, indent=2, default=str)


registry.register(
    name="board_card_add",
    toolset="board",
    schema=BOARD_CARD_ADD_SCHEMA,
    handler=lambda args, **kw: board_card_add(args, **kw),
    description="Add a planning card to the Mission Board (advisory; never mutates a goal).",
    emoji="📌",
)

registry.register(
    name="board_cards",
    toolset="board",
    schema=BOARD_CARDS_SCHEMA,
    handler=lambda args, **kw: board_cards(args, **kw),
    description="List active Mission Board cards for the current workspace.",
    emoji="📋",
)
