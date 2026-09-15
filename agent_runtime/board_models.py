"""Pure helpers for the Mission Board domain: the deterministic default-board
factory, the semantic content hash used by realm-sync change detection, and the
small vocabulary constants. No I/O — kept import-light so both the store and the
realm-sync classifier can depend on it.
"""

from __future__ import annotations

import hashlib
import json

from .models import Board, BoardCard, BoardColumn
from .serde import to_jsonable

# Fixed default-board column ids + deterministic content. Two machines lazily
# creating the same default board must land on byte-identical semantic content
# (see ``board_content_hash``) so the first realm sync converges instead of
# conflicting on timestamps.
COLUMN_KINDS = ("queued", "active", "review", "done", "custom")
CARD_PRIORITIES = ("p0", "p1", "p2", "p3")
DEFAULT_PRIORITY = "p2"

DEFAULT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("col_queued", "Queued", "queued"),
    ("col_active", "In Progress", "active"),
    ("col_review", "Review", "review"),
    ("col_done", "Complete", "done"),
)

DEFAULT_BOARD_TITLE = "Board"

# Fields excluded from the semantic content hash: revision + timestamps +
# updated_by. Timestamp-only differences are never sync conflicts, and the
# excluded revision lets a converged card settle without a spurious diff.
_HASH_EXCLUDE = frozenset({"revision", "created_at", "updated_at", "updated_by"})


def default_board_id(workspace_id: str) -> str:
    return f"board_default_{workspace_id}"


def default_board_columns() -> list[BoardColumn]:
    return [BoardColumn(column_id=cid, title=title, kind=kind, wip_limit=None) for cid, title, kind in DEFAULT_COLUMNS]


def default_board(workspace_id: str, *, created_at, updated_by: str = "operator") -> Board:
    """Deterministic default board for a workspace.

    Everything that feeds the semantic content hash (id, workspace, title,
    columns, empty ledger) is fixed; only the excluded timestamp/updated_by
    fields vary per machine, so ``board_content_hash`` is identical everywhere.
    """

    return Board(
        board_id=default_board_id(workspace_id),
        workspace_id=workspace_id,
        title=DEFAULT_BOARD_TITLE,
        columns=default_board_columns(),
        archived_card_ids=[],
        revision=1,
        created_at=created_at,
        updated_at=created_at,
        updated_by=updated_by,
    )


def normalize_priority(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in CARD_PRIORITIES else DEFAULT_PRIORITY


def normalize_kind(value: str | None) -> str:
    text = str(value or "").strip().lower()
    return text if text in COLUMN_KINDS else "custom"


def first_column_of_kind(board: Board, kind: str) -> BoardColumn | None:
    for column in board.columns:
        if column.kind == kind:
            return column
    return None


def first_queued_column(board: Board) -> BoardColumn | None:
    """The landing column for a new card: first ``queued``-kind column by order,
    falling back to the first column so a board with no queued lane still
    accepts cards."""

    return first_column_of_kind(board, "queued") or (board.columns[0] if board.columns else None)


def resolve_column_id(board: Board, column: str | None) -> str | None:
    """Resolve an operator-supplied column token (explicit id OR a kind) to a
    concrete column id on this board. ``None`` when nothing matches."""

    if not column:
        landing = first_queued_column(board)
        return landing.column_id if landing else None
    token = str(column).strip()
    for col in board.columns:
        if col.column_id == token:
            return col.column_id
    # allow addressing by kind (e.g. "done")
    by_kind = first_column_of_kind(board, normalize_kind(token))
    return by_kind.column_id if by_kind else None


def board_content_hash(entity: Board | BoardCard) -> str:
    """Semantic content hash H(entity): a stable hash over every field EXCEPT
    revision + timestamps + updated_by. Drives realm-sync change detection so
    that timestamp-only diffs are never conflicts and the deterministic default
    board converges."""

    payload = to_jsonable(entity)
    if isinstance(payload, dict):
        payload = {key: value for key, value in payload.items() if key not in _HASH_EXCLUDE}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
