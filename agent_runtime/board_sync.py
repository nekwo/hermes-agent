"""Realm-sync for the Mission Board: per-card LWW with loud conflicts, no CRDT.

The pure classifier ``classify_board_pull`` implements the exhaustive decision
table in ``docs/mission_control/BOARD_DESIGN_2026-07-13.md`` §8. Change is
detected against a NEVER-synced baseline sidecar (the semantic content hash H,
timestamps excluded — so the deterministic default board converges instead of
conflicting). Conflicts are never silent: a sidecar in ``conflicts/`` + a
snapshot parity warning + an explicit ``resolve-conflict`` verb.

Board card files are excluded from the generic realm-sync pull overwrite
(``_destination_for_sync_path`` returns None for ``store/boards/*``); this module
owns the board pull instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from utils import atomic_json_write

from . import board_models, paths
from .board_store import BoardStore, _read_json
from .events import EventLog
from .models import Board, BoardCard
from .serde import from_jsonable, to_jsonable


class BoardPullAction(str, Enum):
    NOOP = "noop"
    WRITE_REMOTE = "write_remote"  # adopt/take-remote/converge → write remote + baseline
    KEEP_LOCAL = "keep_local"  # local changed vs unchanged remote → stays unpublished
    ARCHIVE_LOCAL = "archive_local"  # remote removed the card → archive (never delete)
    CONFLICT = "conflict"  # both diverged / edit-vs-remove / archive-vs-edit


@dataclass(frozen=True, slots=True)
class BoardPullDecision:
    action: BoardPullAction
    reason: str


def _local_state(local_hash: str | None, baseline_hash: str | None, locally_archived: bool) -> str:
    if locally_archived:
        return "archived"
    if local_hash is None:
        return "absent"
    if baseline_hash is None:
        return "new"
    return "unchanged" if local_hash == baseline_hash else "changed"


def _remote_state(remote_hash: str | None, baseline_hash: str | None) -> str:
    if remote_hash is None:
        return "absent"
    if baseline_hash is None:
        return "new"
    return "unchanged" if remote_hash == baseline_hash else "changed"


def classify_board_pull(
    local_hash: str | None,
    remote_hash: str | None,
    baseline_hash: str | None,
    *,
    locally_archived: bool = False,
) -> BoardPullDecision:
    """Pure per-card pull decision (§8). ``*_hash`` are semantic content hashes
    (``board_models.board_content_hash``); ``None`` means the card is absent on
    that side. ``locally_archived`` is the resurrection-guard ledger membership.
    """

    ls = _local_state(local_hash, baseline_hash, locally_archived)
    rs = _remote_state(remote_hash, baseline_hash)

    if ls == "archived":
        # Ledger blocks resurrection: a pulled remote copy never re-creates a
        # locally archived card. A remote EDIT of it is a loud conflict.
        if rs in ("absent", "unchanged"):
            return BoardPullDecision(BoardPullAction.NOOP, "archived_local")
        return BoardPullDecision(BoardPullAction.CONFLICT, "archive_vs_edit")

    if ls == "unchanged":
        if rs == "unchanged":
            return BoardPullDecision(BoardPullAction.NOOP, "unchanged")
        if rs == "changed":
            return BoardPullDecision(BoardPullAction.WRITE_REMOTE, "take_remote")
        if rs == "absent":
            return BoardPullDecision(BoardPullAction.ARCHIVE_LOCAL, "remote_removed")
        return BoardPullDecision(BoardPullAction.WRITE_REMOTE, "take_remote")

    if ls == "changed":
        if rs == "unchanged":
            return BoardPullDecision(BoardPullAction.KEEP_LOCAL, "unpublished")
        if rs == "changed":
            if local_hash == remote_hash:
                return BoardPullDecision(BoardPullAction.WRITE_REMOTE, "converged")
            return BoardPullDecision(BoardPullAction.CONFLICT, "both_changed")
        if rs == "absent":
            return BoardPullDecision(BoardPullAction.CONFLICT, "edit_vs_remove")
        return BoardPullDecision(BoardPullAction.CONFLICT, "both_changed")

    if ls == "new":  # local present, no baseline
        if rs == "absent":
            return BoardPullDecision(BoardPullAction.KEEP_LOCAL, "new_local")
        if rs == "new":
            if local_hash == remote_hash:
                return BoardPullDecision(BoardPullAction.WRITE_REMOTE, "converged")
            return BoardPullDecision(BoardPullAction.CONFLICT, "new_both")
        return BoardPullDecision(BoardPullAction.CONFLICT, "new_both")

    # ls == "absent"
    if rs == "absent":
        return BoardPullDecision(BoardPullAction.NOOP, "absent_both")
    return BoardPullDecision(BoardPullAction.WRITE_REMOTE, "adopt_remote")


# --- baseline sidecar (never synced, never published) --------------------


def read_board_baseline(realm_id: str) -> dict[str, str]:
    path = paths.board_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_board_baseline(realm_id: str, entries: dict[str, str]) -> None:
    atomic_json_write(
        paths.board_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def _card_key(board_id: str, card_id: str) -> str:
    return f"{board_id}:card:{card_id}"


def _board_key(board_id: str) -> str:
    return f"{board_id}:board"


# --- publish baseline update ---------------------------------------------


def update_board_baseline_after_sync(realm_id: str, board_ids: list[str]) -> None:
    """Record H for every active board+card of the given boards as the new
    baseline (called after a successful publish OR pull)."""

    store = BoardStore()
    baseline = read_board_baseline(realm_id)
    # Drop stale entries for these boards, then re-record current truth.
    prefixes = tuple(f"{board_id}:" for board_id in board_ids)
    baseline = {k: v for k, v in baseline.items() if not k.startswith(prefixes)}
    for board_id in board_ids:
        if not store.exists(board_id):
            continue
        board = store.get(board_id)
        baseline[_board_key(board_id)] = board_models.board_content_hash(board)
        for card in store.list_cards(board_id):
            baseline[_card_key(board_id, card.card_id)] = board_models.board_content_hash(card)
    write_board_baseline(realm_id, baseline)


# --- pull application ------------------------------------------------------


@dataclass(slots=True)
class BoardPullSummary:
    adopted: int = 0
    converged: int = 0
    kept_local: int = 0
    archived: int = 0
    conflicts: int = 0
    boards: list[str] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": self.adopted,
            "converged": self.converged,
            "kept_local": self.kept_local,
            "archived": self.archived,
            "conflicts": self.conflicts,
            "boards": list(self.boards or []),
        }


def _read_remote_board(board_dir: Path) -> tuple[Board | None, dict[str, BoardCard]]:
    def_path = board_dir / "board.json"
    board: Board | None = None
    if def_path.exists():
        try:
            board = from_jsonable(Board, _read_json(def_path))
        except Exception:
            board = None
    cards: dict[str, BoardCard] = {}
    cards_dir = board_dir / "cards"
    if cards_dir.exists():
        for card_path in sorted(cards_dir.glob("*.json")):
            try:
                card = from_jsonable(BoardCard, _read_json(card_path))
            except Exception:
                continue
            cards[card.card_id] = card
    return board, cards


def _write_conflict_sidecar(board_id: str, card_id: str, *, kind: str, remote_card: BoardCard | None, local_hash: str | None, remote_hash: str | None) -> None:
    atomic_json_write(
        paths.board_conflict_path(board_id, card_id),
        {
            "schema_version": 1,
            "board_id": board_id,
            "card_id": card_id,
            "kind": kind,
            "local_hash": local_hash,
            "remote_hash": remote_hash,
            "remote_card": to_jsonable(remote_card) if remote_card is not None else None,
        },
        indent=2,
        sort_keys=True,
    )


def apply_board_pull(realm_id: str, subtree: Path, *, event_log: EventLog | None = None) -> BoardPullSummary:
    """Apply the per-card decision table across every board in the pulled realm
    subtree. Updates the baseline for converged/adopted cards, archives remote
    -removed cards (never deletes), and writes loud conflict sidecars. Never a
    silent overwrite of local changes.
    """

    store = BoardStore(event_log=event_log)
    baseline = read_board_baseline(realm_id)
    summary = BoardPullSummary(boards=[])
    boards_root = subtree / "store" / "boards"
    if not boards_root.exists():
        return summary

    for board_dir in sorted(p for p in boards_root.iterdir() if p.is_dir()):
        remote_board, remote_cards = _read_remote_board(board_dir)
        if remote_board is None:
            continue
        board_id = remote_board.board_id
        summary.boards.append(board_id)

        local_board = store.get(board_id) if store.exists(board_id) else None
        local_cards: dict[str, BoardCard] = {}
        archived_ids: set[str] = set()
        if local_board is not None:
            archived_ids = set(local_board.archived_card_ids)
            for card in store.list_cards(board_id):
                local_cards[card.card_id] = card

        # Board def: file-granular LWW (keep-local-wins on both-changed for v1;
        # a column deleted remotely surfaces as the launcher "Unassigned" lane).
        board_key = _board_key(board_id)
        local_board_hash = board_models.board_content_hash(local_board) if local_board is not None else None
        remote_board_hash = board_models.board_content_hash(remote_board)
        board_decision = classify_board_pull(local_board_hash, remote_board_hash, baseline.get(board_key))
        if board_decision.action == BoardPullAction.WRITE_REMOTE or local_board is None:
            atomic_json_write(paths.board_def_path(board_id), to_jsonable(remote_board), indent=2, sort_keys=True)
            baseline[board_key] = remote_board_hash

        # Cards: the union of local-active and remote card ids.
        for card_id in sorted(set(local_cards) | set(remote_cards) | archived_ids):
            local_card = local_cards.get(card_id)
            remote_card = remote_cards.get(card_id)
            local_hash = board_models.board_content_hash(local_card) if local_card is not None else None
            remote_hash = board_models.board_content_hash(remote_card) if remote_card is not None else None
            key = _card_key(board_id, card_id)
            decision = classify_board_pull(
                local_hash,
                remote_hash,
                baseline.get(key),
                locally_archived=card_id in archived_ids,
            )
            if decision.action == BoardPullAction.NOOP:
                continue
            if decision.action == BoardPullAction.KEEP_LOCAL:
                summary.kept_local += 1
                continue
            if decision.action == BoardPullAction.WRITE_REMOTE and remote_card is not None:
                remote_card.board_id = board_id
                remote_card.state = "active"
                atomic_json_write(paths.board_card_path(board_id, card_id), to_jsonable(remote_card), indent=2, sort_keys=True)
                baseline[key] = remote_hash or board_models.board_content_hash(remote_card)
                if decision.reason == "converged":
                    summary.converged += 1
                else:
                    summary.adopted += 1
                continue
            if decision.action == BoardPullAction.ARCHIVE_LOCAL:
                try:
                    store.archive_card(card_id, board_id=board_id, reason="remote_removed")
                    summary.archived += 1
                except Exception:
                    pass
                baseline.pop(key, None)
                continue
            if decision.action == BoardPullAction.CONFLICT:
                _write_conflict_sidecar(
                    board_id,
                    card_id,
                    kind=decision.reason,
                    remote_card=remote_card,
                    local_hash=local_hash,
                    remote_hash=remote_hash,
                )
                summary.conflicts += 1
                continue

    write_board_baseline(realm_id, baseline)
    return summary
