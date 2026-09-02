"""BoardStore — the single write chokepoint for the Mission Board domain.

Mirrors the ``WorkspaceStore`` / ``RealmStore`` pattern in ``store.py``: file-per
-card JSON under the runtime root, the shared store-lock discipline, atomic
writes, and a typed ``EventLog`` event on EVERY mutation (standing store rule —
an event-less write is invisible to the watermark-gated snapshot/serve pipeline).

Hard invariants this store upholds (see ``docs/mission_control/BOARD_DESIGN``):

- **Cards are planning state.** They do not carry or mutate mission records.
- **Archive-never-delete.** Archived cards move to ``archive/`` and their ids are
  recorded in the board's ``archived_card_ids`` resurrection-guard ledger.
- **Merge-friendly ordering.** Card position is a fractional ``order_key``; a move
  allocates the midpoint between neighbours (``board_order``), tiebreak by
  ``card_id``.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, NamedTuple

from hermes_time import now
from utils import atomic_json_write

from . import board_models, board_order, paths
from .errors import (
    AlreadyExists,
    CardsUnreadable,
    IdempotencyKeyVerbMismatch,
    IdempotentReplayUnresolved,
    NotFound,
    StaleRevision,
    SyncConflict,
)
from .events import EventLog
from .locks import board_lock
from .models import Board, BoardCard, BoardColumn, Event
from .serde import from_jsonable, safe_id, to_jsonable

#: The three card verbs that share one board's idempotency namespace. Recorded
#: ON the receipt (``_record_idempotency``) and checked on replay
#: (``_idempotent_replay``) so a key cannot cross from one to another.
VERB_ADD_CARD = "add_card"
VERB_EDIT_CARD = "edit_card"
VERB_MOVE_CARD = "move_card"

# Bounded projection / ledger caps (honest accounting, never silent).
ARCHIVED_LEDGER_CAP = 5000


class BoardScan(NamedTuple):
    """The boards a scan FOUND, beside how many board files it could not read.

    ``OfficeStore.ActorScan``'s law, applied to the board family: the two facts
    have to travel together, because any seam that carries only the list
    re-opens the hole at that seam. A board whose ``board.json`` will not decode
    is absent from ``list_all`` — and ``list_all`` is what realm publish walks
    to decide which boards travel and what the snapshot projects.
    """

    boards: list[Board]
    #: Board directories whose ``board.json`` existed and did not decode. NEVER
    #: folded into ``boards`` and never silently zero.
    unreadable: int


class CardScan(NamedTuple):
    """The cards a scan FOUND, beside how many card files it could not read.

    The count is load-bearing in two different ways, which is why it may not be
    dropped at either seam: a READER that renders the short list under-reports
    the column, and the order-key ALLOCATOR that reads it computes neighbour
    keys against rows it cannot see (see :class:`~agent_runtime.errors.
    CardsUnreadable`).
    """

    cards: list[BoardCard]
    unreadable: int


def _safe_text(value: Any, *, limit: int = 4000) -> str:
    return str(value or "").strip()[:limit]


def _safe_title(value: Any) -> str:
    return " ".join(str(value or "").split())[:280]


def _safe_actor(value: Any, *, fallback: str = "operator") -> str:
    return safe_id(value) or fallback


def _safe_labels(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for value in values:
        label = " ".join(str(value or "").split())[:60]
        if label and label not in out:
            out.append(label)
        if len(out) >= 24:
            break
    return out


def _safe_checklist(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for item in values:
        if isinstance(item, dict):
            text = _safe_text(item.get("text"), limit=280)
            done = bool(item.get("done"))
        else:
            text = _safe_text(item, limit=280)
            done = False
        if text:
            out.append({"text": text, "done": done})
        if len(out) >= 100:
            break
    return out


class BoardStore:
    def __init__(self, event_log: EventLog | None = None) -> None:
        self.event_log = event_log or EventLog()

    # --- event emission (single chokepoint) ------------------------------

    def _emit(self, event_type: str, **payload: Any) -> None:
        try:
            body = {key: value for key, value in payload.items() if value is not None}
            self.event_log.append(Event(now(), event_type, None, None, None, body))
        except Exception:
            import logging

            logging.getLogger(__name__).warning("board event append failed: %s", event_type, exc_info=True)

    # --- board-level reads ------------------------------------------------

    def get(self, board_id: str) -> Board:
        path = paths.board_def_path(board_id)
        if not path.exists():
            raise NotFound(f"board:{board_id}")
        return from_jsonable(Board, _read_json(path))

    def exists(self, board_id: str) -> bool:
        return paths.board_def_path(board_id).exists()

    def scan_all(self, *, include_archived: bool = True) -> BoardScan:
        """Every board this root HAS, plus how many board files did not decode.

        THE chokepoint. ``list_all`` is the thin list view over it, so the many
        callers that only want rows keep their signature while the ones that
        must not describe a short answer as complete — the snapshot projection
        and the realm-publish artifact set — can ask the fuller question.
        """

        root = paths.boards_root()
        boards: list[Board] = []
        unreadable = 0
        if not root.exists():
            return BoardScan([], 0)
        for child in sorted(root.iterdir()):
            def_path = child / "board.json"
            if not def_path.exists():
                continue
            try:
                boards.append(from_jsonable(Board, _read_json(def_path)))
            except Exception:
                unreadable += 1
                continue
        return BoardScan(sorted(boards, key=lambda b: b.board_id), unreadable)

    def list_all(self, *, include_archived: bool = True) -> list[Board]:
        return self.scan_all(include_archived=include_archived).boards

    def list_for_workspace(self, workspace_id: str, *, include_archived: bool = True) -> list[Board]:
        wanted = safe_id(workspace_id)
        return [b for b in self.list_all() if b.workspace_id == wanted]

    # --- board-level writes ----------------------------------------------

    def ensure_default_board(self, workspace_id: str, *, created_by: str = "operator") -> Board:
        """Lazily create the deterministic default board for a workspace.

        Idempotent: if the board already exists it is returned unchanged (no
        event). Two machines calling this converge on identical semantic content
        (fixed column ids + timestamp-excluded content hash)."""

        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        board_id = board_models.default_board_id(wsid)
        if self.exists(board_id):
            return self.get(board_id)
        with board_lock(board_id):
            if self.exists(board_id):
                return self.get(board_id)
            board = board_models.default_board(wsid, created_at=now(), updated_by=_safe_actor(created_by))
            _write_board(board)
            self._emit("board.created", board_id=board.board_id, workspace_id=board.workspace_id, title=board.title)
        return self.get(board_id)

    def create(
        self,
        *,
        workspace_id: str,
        title: str | None = None,
        board_id: str | None = None,
        columns: list[BoardColumn] | None = None,
        created_by: str = "operator",
    ) -> Board:
        wsid = safe_id(workspace_id)
        if not wsid:
            raise ValueError("invalid_request")
        resolved_id = safe_id(board_id) or f"board_{uuid.uuid4().hex[:10]}"
        if self.exists(resolved_id):
            raise AlreadyExists(resolved_id)
        with board_lock(resolved_id):
            if self.exists(resolved_id):
                raise AlreadyExists(resolved_id)
            ts = now()
            board = Board(
                board_id=resolved_id,
                workspace_id=wsid,
                title=_safe_title(title) or board_models.DEFAULT_BOARD_TITLE,
                columns=columns if columns else board_models.default_board_columns(),
                archived_card_ids=[],
                revision=1,
                created_at=ts,
                updated_at=ts,
                updated_by=_safe_actor(created_by),
            )
            _write_board(board)
            self._emit("board.created", board_id=board.board_id, workspace_id=board.workspace_id, title=board.title)
        return self.get(resolved_id)

    def update_board(
        self,
        board_id: str,
        *,
        title: str | None = None,
        columns: list[BoardColumn] | None = None,
        updated_by: str = "operator",
        expect_revision: int | None = None,
    ) -> Board:
        with board_lock(board_id):
            board = self.get(board_id)
            _check_revision(board.revision, expect_revision)
            change = []
            if title is not None:
                board.title = _safe_title(title) or board.title
                change.append("title")
            if columns is not None:
                board.columns = columns
                change.append("columns")
            board.revision += 1
            board.updated_at = now()
            board.updated_by = _safe_actor(updated_by)
            _write_board(board)
            self._emit(
                "board.updated",
                board_id=board.board_id,
                change=",".join(change) or "saved",
                title=board.title,
                revision=board.revision,
            )
        return self.get(board_id)

    # --- card reads -------------------------------------------------------

    def get_card(self, card_id: str, *, board_id: str | None = None) -> BoardCard:
        board_id, path = self._locate_card(card_id, board_id=board_id)
        if path is None or not path.exists():
            raise NotFound(f"card:{card_id}")
        return from_jsonable(BoardCard, _read_json(path))

    def scan_cards(self, board_id: str, *, include_archived: bool = False) -> CardScan:
        """Every card this board HAS, plus how many card files did not decode.

        THE read chokepoint; ``list_cards`` is the thin list view over it. The
        projection asks the fuller question so a rendered board cannot describe
        a column the platform partly ate as complete.
        """

        scan = self._scan_active_cards(board_id)
        cards = scan.cards
        unreadable = scan.unreadable
        if include_archived:
            archived = self._scan_archived_cards(board_id)
            cards = [*cards, *archived.cards]
            unreadable += archived.unreadable
        return CardScan(_sort_cards(cards), unreadable)

    def list_cards(self, board_id: str, *, include_archived: bool = False) -> list[BoardCard]:
        return self.scan_cards(board_id, include_archived=include_archived).cards

    # --- card writes ------------------------------------------------------

    def add_card(
        self,
        *,
        board_id: str | None = None,
        workspace_id: str | None = None,
        title: str,
        description: str = "",
        column: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
        checklist: list[Any] | None = None,
        created_by: str = "operator",
        idempotency_key: str | None = None,
    ) -> BoardCard:
        clean_title = _safe_title(title)
        if not clean_title:
            raise ValueError("invalid_request")
        board = self._resolve_board_for_write(board_id=board_id, workspace_id=workspace_id, created_by=created_by)
        with board_lock(board.board_id):
            replay = self._idempotent_replay(board.board_id, idempotency_key, verb=VERB_ADD_CARD)
            if replay is not None:
                return replay
            board = self.get(board.board_id)
            column_id = board_models.resolve_column_id(board, column)
            if column_id is None:
                raise ValueError("invalid_request")
            order_key = self._next_order_key(board.board_id, column_id)
            ts = now()
            actor = _safe_actor(created_by)
            card = BoardCard(
                card_id=f"card_{uuid.uuid4().hex}",
                board_id=board.board_id,
                column_id=column_id,
                title=clean_title,
                order_key=order_key,
                description=_safe_text(description),
                priority=board_models.normalize_priority(priority),
                labels=_safe_labels(labels),
                assignee=safe_id(assignee),
                checklist=_safe_checklist(checklist),
                state="active",
                created_by=actor,
                revision=1,
                created_at=ts,
                updated_at=ts,
                updated_by=actor,
            )
            _write_card(card)
            self._emit(
                "board.card.created",
                board_id=card.board_id,
                card_id=card.card_id,
                title=card.title,
                column_id=card.column_id,
                created_by=card.created_by,
            )
            self._record_idempotency(board.board_id, idempotency_key, card.card_id, verb=VERB_ADD_CARD)
        return self.get_card(card.card_id, board_id=board.board_id)

    def edit_card(
        self,
        card_id: str,
        *,
        board_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        priority: str | None = None,
        labels: list[str] | None = None,
        assignee: str | None = None,
        clear_assignee: bool = False,
        checklist: list[Any] | None = None,
        updated_by: str = "operator",
        expect_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> BoardCard:
        board_id, _ = self._locate_card(card_id, board_id=board_id)
        with board_lock(board_id):
            replay = self._idempotent_replay(board_id, idempotency_key, verb=VERB_EDIT_CARD)
            if replay is not None:
                return replay
            card = self.get_card(card_id, board_id=board_id)
            self._guard_no_conflict(card)
            _check_revision(card.revision, expect_revision)
            fields: list[str] = []
            if title is not None:
                card.title = _safe_title(title) or card.title
                fields.append("title")
            if description is not None:
                card.description = _safe_text(description)
                fields.append("description")
            if priority is not None:
                card.priority = board_models.normalize_priority(priority)
                fields.append("priority")
            if labels is not None:
                card.labels = _safe_labels(labels)
                fields.append("labels")
            if clear_assignee:
                card.assignee = None
                fields.append("assignee")
            elif assignee is not None:
                card.assignee = safe_id(assignee)
                fields.append("assignee")
            if checklist is not None:
                card.checklist = _safe_checklist(checklist)
                fields.append("checklist")
            card.revision += 1
            card.updated_at = now()
            card.updated_by = _safe_actor(updated_by)
            _write_card(card)
            self._emit(
                "board.card.edited",
                board_id=card.board_id,
                card_id=card.card_id,
                fields=",".join(fields) or "touched",
                revision=card.revision,
            )
            self._record_idempotency(board_id, idempotency_key, card.card_id, verb=VERB_EDIT_CARD)
        return self.get_card(card_id, board_id=board_id)

    def move_card(
        self,
        card_id: str,
        *,
        column_id: str,
        before: str | None = None,
        after: str | None = None,
        board_id: str | None = None,
        updated_by: str = "operator",
        expect_revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> BoardCard:
        board_id, _ = self._locate_card(card_id, board_id=board_id)
        with board_lock(board_id):
            replay = self._idempotent_replay(board_id, idempotency_key, verb=VERB_MOVE_CARD)
            if replay is not None:
                return replay
            board = self.get(board_id)
            card = self.get_card(card_id, board_id=board_id)
            self._guard_no_conflict(card)
            _check_revision(card.revision, expect_revision)
            target_column = board_models.resolve_column_id(board, column_id)
            if target_column is None:
                raise ValueError("invalid_request")
            from_column = card.column_id
            order_key = self._allocate_order_key(
                board_id, target_column, before=before, after=after, exclude_card_id=card_id
            )
            card.column_id = target_column
            card.order_key = order_key
            card.revision += 1
            card.updated_at = now()
            card.updated_by = _safe_actor(updated_by)
            _write_card(card)
            self._emit(
                "board.card.moved",
                board_id=card.board_id,
                card_id=card.card_id,
                column_id=card.column_id,
                from_column_id=from_column,
                order_key=card.order_key,
            )
            self._record_idempotency(board_id, idempotency_key, card.card_id, verb=VERB_MOVE_CARD)
        return self.get_card(card_id, board_id=board_id)

    def archive_card(
        self,
        card_id: str,
        *,
        board_id: str | None = None,
        reason: str = "operator",
        updated_by: str = "operator",
        record_tombstone: bool = True,
    ) -> BoardCard:
        """Archive one card — the board's delete.

        ``record_tombstone`` is the AUTHORED-vs-DIAGNOSTIC split, the office
        twin's parameter for the office twin's reason (``OfficeStore.
        remove_actor``, operator ruling 2026-08-30 / AX7). Defaulted true, which
        is every caller that carries an operator's intent to delete. ``False``
        is for a repair aimed only at THIS install's projection — the realm-sync
        REVERT lane's ``added`` arm, where the operator is saying "my
        unpublished local row is noise", not "delete this card realm-wide":
        ``archived_card_ids`` rides the PUBLISHED board def, so a tombstone
        minted from a local-only revert would archive the row on every machine
        in the realm. The archive copy is written either way — archive-never-
        delete is not what is being traded, and ``restore_card`` still works.
        """

        board_id, _ = self._locate_card(card_id, board_id=board_id)
        with board_lock(board_id):
            board = self.get(board_id)
            card = self.get_card(card_id, board_id=board_id)
            self._archive_card_locked(
                board, card, reason=reason, updated_by=updated_by, record_tombstone=record_tombstone
            )
            archived = from_jsonable(BoardCard, _read_json(paths.board_archived_card_path(board_id, card_id)))
        return archived

    def restore_card(self, card_id: str, *, board_id: str | None = None, updated_by: str = "operator") -> BoardCard:
        board_id = board_id or self._board_id_of_archived_card(card_id)
        if not board_id:
            raise NotFound(f"card:{card_id}")
        with board_lock(board_id):
            archive_path = paths.board_archived_card_path(board_id, card_id)
            if not archive_path.exists():
                raise NotFound(f"card:{card_id}")
            card = from_jsonable(BoardCard, _read_json(archive_path))
            board = self.get(board_id)
            # Re-home to a valid column (its old column may have been deleted).
            if not any(col.column_id == card.column_id for col in board.columns):
                landing = board_models.first_queued_column(board)
                card.column_id = landing.column_id if landing else card.column_id
            card.order_key = self._next_order_key(board_id, card.column_id)
            card.state = "active"
            card.revision += 1
            card.updated_at = now()
            card.updated_by = _safe_actor(updated_by)
            _write_card(card)
            archive_path.unlink(missing_ok=True)
            # Drop from the resurrection-guard ledger so it can sync again.
            if card.card_id in board.archived_card_ids:
                board.archived_card_ids = [cid for cid in board.archived_card_ids if cid != card.card_id]
                board.updated_at = now()
                _write_board(board)
            self._emit("board.card.restored", board_id=board_id, card_id=card.card_id, column_id=card.column_id)
        return self.get_card(card_id, board_id=board_id)

    def resolve_conflict(self, card_id: str, *, take: str, board_id: str | None = None, updated_by: str = "operator") -> BoardCard | None:
        """Resolve a realm-sync conflict sidecar for a card. ``take=local`` keeps
        the local card; ``take=remote`` adopts the sidecar's remote copy (or
        archives the local card for an edit-vs-remove tombstone). Always archives
        the sidecar and emits ``board.card.conflict_resolved``."""

        take = str(take or "").strip().lower()
        if take not in {"local", "remote"}:
            raise ValueError("invalid_request")
        board_id, _ = self._locate_card(card_id, board_id=board_id, allow_missing=True)
        if not board_id:
            board_id = self._board_id_of_conflict(card_id)
        if not board_id:
            raise NotFound(f"card:{card_id}")
        with board_lock(board_id):
            sidecar_path = paths.board_conflict_path(board_id, card_id)
            if not sidecar_path.exists():
                raise SyncConflict(f"no_conflict:{card_id}")
            sidecar = _read_json(sidecar_path)
            result_card: BoardCard | None = None
            if take == "local":
                # Keep the local card as-is; simply clear the conflict.
                if self._card_path_active(board_id, card_id):
                    result_card = self.get_card(card_id, board_id=board_id)
            else:  # take == remote
                remote = sidecar.get("remote_card")
                if isinstance(remote, dict):
                    card = from_jsonable(BoardCard, remote)
                    card.board_id = board_id
                    card.state = "active"
                    card.revision = max(int(card.revision or 1), 1) + 1
                    card.updated_at = now()
                    card.updated_by = _safe_actor(updated_by)
                    _write_card(card)
                    result_card = card
                else:
                    # Remote removed the card (edit-vs-remove) → archive local.
                    if self._card_path_active(board_id, card_id):
                        board = self.get(board_id)
                        card = self.get_card(card_id, board_id=board_id)
                        self._archive_card_locked(board, card, reason="remote_removed", updated_by=updated_by, emit=False)
            _archive_conflict_sidecar(board_id, card_id)
            self._emit(
                "board.card.conflict_resolved",
                board_id=board_id,
                card_id=card_id,
                take=take,
                revision=getattr(result_card, "revision", None),
            )
        return result_card

    # --- internal helpers -------------------------------------------------

    def _resolve_board_for_write(self, *, board_id: str | None, workspace_id: str | None, created_by: str) -> Board:
        if board_id:
            if not self.exists(board_id):
                raise NotFound(f"board:{board_id}")
            return self.get(board_id)
        if workspace_id:
            return self.ensure_default_board(workspace_id, created_by=created_by)
        raise ValueError("invalid_request")

    def _scan_card_dir(self, directory) -> CardScan:
        cards: list[BoardCard] = []
        unreadable = 0
        if not directory.exists():
            return CardScan([], 0)
        for path in directory.glob("*.json"):
            try:
                cards.append(from_jsonable(BoardCard, _read_json(path)))
            except Exception:
                unreadable += 1
                continue
        return CardScan(cards, unreadable)

    def _scan_active_cards(self, board_id: str) -> CardScan:
        return self._scan_card_dir(paths.board_cards_dir(board_id))

    def _scan_archived_cards(self, board_id: str) -> CardScan:
        return self._scan_card_dir(paths.board_archive_dir(board_id))

    def _ordering_cards(self, board_id: str) -> list[BoardCard]:
        """THE read every order-key decision goes through, and the one place
        that refuses.

        One chokepoint rather than a guard on each of the three allocators: two
        sufficient mechanisms answering the same question is how a fix survives
        its own sabotage and stops being a check. Every caller below allocates
        or rewrites keys, so none of them may proceed on a column it cannot
        fully see.
        """

        scan = self._scan_active_cards(board_id)
        if scan.unreadable:
            raise CardsUnreadable(
                f"cards_unreadable board={board_id} unreadable={scan.unreadable}"
            )
        return scan.cards

    def _archive_card_locked(
        self,
        board: Board,
        card: BoardCard,
        *,
        reason: str,
        updated_by: str,
        emit: bool = True,
        record_tombstone: bool = True,
    ) -> None:
        card.state = "archived"
        card.revision += 1
        card.updated_at = now()
        card.updated_by = _safe_actor(updated_by)
        atomic_json_write(paths.board_archived_card_path(board.board_id, card.card_id), to_jsonable(card), indent=2, sort_keys=True)
        active_path = paths.board_card_path(board.board_id, card.card_id)
        active_path.unlink(missing_ok=True)
        # The ONE line the diagnostic mode skips — see ``archive_card``'s
        # ``record_tombstone``. The archive copy above is written
        # unconditionally; only the realm-visible ledger entry is withheld,
        # because only that entry rides the published board def and is therefore
        # an assertion about the REALM rather than about this projection.
        if record_tombstone and card.card_id not in board.archived_card_ids:
            board.archived_card_ids = [*board.archived_card_ids, card.card_id][-ARCHIVED_LEDGER_CAP:]
            board.updated_at = now()
            _write_board(board)
        if emit:
            self._emit(
                "board.card.archived",
                board_id=board.board_id,
                card_id=card.card_id,
                reason=reason,
                column_id=card.column_id,
            )

    def _next_order_key(self, board_id: str, column_id: str) -> str:
        cards = _sort_cards([c for c in self._ordering_cards(board_id) if c.column_id == column_id])
        last = cards[-1].order_key if cards else None
        return board_order.allocate_between(last, None)

    def _allocate_order_key(self, board_id: str, column_id: str, *, before: str | None, after: str | None, exclude_card_id: str | None) -> str:
        column_cards = _sort_cards(
            [c for c in self._ordering_cards(board_id) if c.column_id == column_id and c.card_id != exclude_card_id]
        )
        keys = [c.order_key for c in column_cards]
        # ``after`` = the card the moved card should sit immediately AFTER (its
        # upper neighbour → lower key bound); ``before`` = the card it should sit
        # immediately BEFORE (its lower neighbour → upper key bound). Either may
        # be given alone (the missing bound is derived from the sorted column) or
        # both (an explicit bracket).
        lower_key = _order_key_of(column_cards, after)   # after → lower bound
        upper_key = _order_key_of(column_cards, before)  # before → upper bound
        if lower_key is None and upper_key is None:
            return board_order.allocate_between(keys[-1] if keys else None, None)
        if lower_key is not None and upper_key is None:
            idx = next((i for i, c in enumerate(column_cards) if c.card_id == after), None)
            if idx is not None:
                upper_key = column_cards[idx + 1].order_key if idx + 1 < len(column_cards) else None
        elif upper_key is not None and lower_key is None:
            idx = next((i for i, c in enumerate(column_cards) if c.card_id == before), None)
            if idx is not None:
                lower_key = column_cards[idx - 1].order_key if idx - 1 >= 0 else None
        try:
            return board_order.allocate_between(lower_key, upper_key)
        except ValueError:
            # Neighbours out of order or exhausted → rebalance the column, then
            # append at the end (one loud sweep, deterministic keys).
            self._rebalance_column(board_id, column_id, exclude_card_id=exclude_card_id)
            fresh = _sort_cards(
                [c for c in self._ordering_cards(board_id) if c.column_id == column_id and c.card_id != exclude_card_id]
            )
            return board_order.allocate_between(fresh[-1].order_key if fresh else None, None)

    def _rebalance_column(self, board_id: str, column_id: str, *, exclude_card_id: str | None = None) -> None:
        column_cards = _sort_cards([c for c in self._ordering_cards(board_id) if c.column_id == column_id])
        if not column_cards:
            return
        keys = board_order.rebalance(len(column_cards))
        for card, key in zip(column_cards, keys):
            if card.order_key == key:
                continue
            card.order_key = key
            card.updated_at = now()
            _write_card(card)
        self._emit("board.rebalanced", board_id=board_id, column_id=column_id, card_count=len(column_cards))

    def _locate_card(self, card_id: str, *, board_id: str | None = None, allow_missing: bool = False) -> tuple[str | None, Any]:
        if board_id:
            path = paths.board_card_path(board_id, card_id)
            if path.exists():
                return board_id, path
            if allow_missing:
                return board_id, None
            raise NotFound(f"card:{card_id}")
        root = paths.boards_root()
        if root.exists():
            for child in root.iterdir():
                candidate = child / "cards" / f"{paths.safe_path_token(card_id)}.json"
                if candidate.exists():
                    return child.name, candidate
        if allow_missing:
            return None, None
        raise NotFound(f"card:{card_id}")

    def _board_id_of_archived_card(self, card_id: str) -> str | None:
        root = paths.boards_root()
        if not root.exists():
            return None
        token = paths.safe_path_token(card_id)
        for child in root.iterdir():
            if (child / "archive" / f"{token}.json").exists():
                return child.name
        return None

    def _board_id_of_conflict(self, card_id: str) -> str | None:
        root = paths.boards_root()
        if not root.exists():
            return None
        token = paths.safe_path_token(card_id)
        for child in root.iterdir():
            if (child / "conflicts" / f"{token}.json").exists():
                return child.name
        return None

    def _card_path_active(self, board_id: str, card_id: str) -> bool:
        return paths.board_card_path(board_id, card_id).exists()

    def _guard_no_conflict(self, card: BoardCard) -> None:
        if paths.board_conflict_path(card.board_id, card.card_id).exists():
            raise SyncConflict(f"card_conflict:{card.card_id}")

    # --- idempotency ledger ----------------------------------------------

    def _idempotent_replay(self, board_id: str, key: str | None, *, verb: str) -> BoardCard | None:
        """The recorded card for this key, or ``None`` when there is no receipt.

        ``None`` means EXACTLY ONE thing to every caller: nothing has been
        recorded for this key, so go and do the write. It used to mean five
        things — no key, no receipt, an undecodable receipt, a receipt with no
        ``card_id``, and a ``card_id`` resolving to neither a live nor an
        archived card — and the caller ran the write for all five. The last
        three are "a receipt EXISTS and I cannot honour it", where re-running is
        the one thing that must not happen: ``add_card`` mints a new id per
        call, so the key meant to prevent a duplicate created one.

        Those three now refuse :class:`IdempotentReplayUnresolved`. This is the
        board's version of ``agent_create``'s ``actor_fresh: false`` — a replay
        that cannot re-read the row it describes says so rather than inventing
        an answer; the board's ack carries no field to degrade, so it refuses.

        ``verb`` closes the FOURTH way this used to answer wrongly. The receipt
        namespace is one directory per BOARD, and ``add_card``, ``edit_card``
        and ``move_card`` all read it, so a key reused across verbs replayed
        the other verb's card and the second write silently did not happen —
        an unmoved card returned from ``move_card`` with an ack no caller could
        tell from a real move. A receipt whose recorded verb is not this one
        refuses :class:`IdempotencyKeyVerbMismatch`, naming both verbs. That is
        a different class from the three above on purpose: this fault is in the
        REQUEST (retrying is futile; the cure is a distinct key), where those
        are damaged FILES (retrying works once the file is repaired).

        **Backward compatibility, decided here.** A receipt written before this
        change carries no ``verb`` field, and it is honoured EXACTLY as before —
        replayed for whichever verb presents the key. Refusing an unlabelled
        receipt would turn every in-flight key on every existing board into a
        hard failure at upgrade, to protect against a cross-verb reuse that
        already happened and cannot now be undone by refusing. The receipts are
        per-board scratch state that ages out with the board; the fence closes
        as they are rewritten. Only a receipt that STATES a different verb is
        refused.
        """

        safe = _safe_idempotency_key(key)
        if not safe:
            return None
        path = paths.board_idempotency_path(board_id, safe)
        if not path.exists():
            return None
        try:
            record = _read_json(path)
        except Exception as exc:
            raise IdempotentReplayUnresolved(
                f"idempotency receipt for {safe!r} on board {board_id} could not "
                f"be decoded ({type(exc).__name__}); the write it recorded is not "
                "safe to repeat"
            ) from exc
        recorded_verb = str(record.get("verb") or "").strip()
        if recorded_verb and recorded_verb != str(verb):
            raise IdempotencyKeyVerbMismatch(
                f"idempotency key {safe!r} on board {board_id} was already used by "
                f"{recorded_verb}; it cannot be reused by {verb}. One key names ONE "
                "gesture — replaying the other verb's card here would silently skip "
                "this write. Retry with a key of its own."
            )
        card_id = record.get("card_id")
        if not card_id:
            raise IdempotentReplayUnresolved(
                f"idempotency receipt for {safe!r} on board {board_id} names no "
                "card; the write it recorded is not safe to repeat"
            )
        try:
            return self.get_card(card_id, board_id=board_id)
        except NotFound:
            pass
        # Card archived since; surface the archived copy so replay is stable.
        archive_path = paths.board_archived_card_path(board_id, card_id)
        if not archive_path.exists():
            raise IdempotentReplayUnresolved(
                f"idempotency receipt for {safe!r} on board {board_id} names card "
                f"{card_id}, which is neither live nor archived; cards are never "
                "hard-deleted, so this is a truncated or corrupted write and the "
                "recorded write is not safe to repeat"
            )
        try:
            return from_jsonable(BoardCard, _read_json(archive_path))
        except Exception as exc:
            raise IdempotentReplayUnresolved(
                f"idempotency receipt for {safe!r} on board {board_id} names card "
                f"{card_id}, whose archived copy could not be decoded "
                f"({type(exc).__name__}); the recorded write is not safe to repeat"
            ) from exc

    def _record_idempotency(self, board_id: str, key: str | None, card_id: str, *, verb: str) -> None:
        """Write the receipt, naming the VERB that earned it.

        ``verb`` is what makes the namespace per-verb without moving the files:
        the directory stays one per board and the receipt states which gesture
        wrote it, so :meth:`_idempotent_replay` can refuse a key crossing verbs
        instead of replaying the wrong card. A receipt from before this field
        existed reads back with no ``verb`` and is honoured as before.
        """

        safe = _safe_idempotency_key(key)
        if not safe:
            return
        atomic_json_write(
            paths.board_idempotency_path(board_id, safe),
            {
                "idempotency_key": safe,
                "card_id": card_id,
                "verb": str(verb),
                "recorded_at": to_jsonable(now()),
            },
            indent=2,
            sort_keys=True,
        )


# --- module-level file + ordering helpers --------------------------------


def _read_json(path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def _write_board(board: Board) -> None:
    atomic_json_write(paths.board_def_path(board.board_id), to_jsonable(board), indent=2, sort_keys=True)


def _write_card(card: BoardCard) -> None:
    atomic_json_write(paths.board_card_path(card.board_id, card.card_id), to_jsonable(card), indent=2, sort_keys=True)


def _archive_conflict_sidecar(board_id: str, card_id: str) -> None:
    sidecar_path = paths.board_conflict_path(board_id, card_id)
    if not sidecar_path.exists():
        return
    try:
        payload = _read_json(sidecar_path)
    except Exception:
        payload = {"card_id": card_id}
    payload["resolved_at"] = to_jsonable(now())
    dest = paths.board_conflicts_dir(board_id) / f"{paths.safe_path_token(card_id)}.resolved.json"
    atomic_json_write(dest, payload, indent=2, sort_keys=True)
    sidecar_path.unlink(missing_ok=True)


def _sort_cards(cards: list[BoardCard]) -> list[BoardCard]:
    # Fractional order_key ascending; equal keys tiebreak by card_id ascending
    # (deterministic on every machine).
    return sorted(cards, key=lambda c: (c.order_key, c.card_id))


def _order_key_of(cards: list[BoardCard], card_id: str | None) -> str | None:
    if not card_id:
        return None
    for card in cards:
        if card.card_id == card_id:
            return card.order_key
    return None


def _check_revision(current: int, expected: int | None) -> None:
    if expected is None:
        return
    if int(current) != int(expected):
        raise StaleRevision(f"stale_revision: expected {expected}, have {current}")


def _safe_idempotency_key(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text if re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", text) else None
