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
from pathlib import Path
from typing import Any, NamedTuple

from utils import atomic_json_write

from . import board_models, paths
from .board_store import BoardStore, _read_json
from .events import EventLog
from .models import Board, BoardCard
from .serde import from_jsonable, to_jsonable

# The classifier was lifted VERBATIM to ``sync_merge`` on 2026-07-17 when the
# Mission Office family became its second consumer. These aliases keep every
# existing import path (and the exhaustive decision-table tests) working
# unmodified — which is the proof the lift changed nothing.
from .sync_merge import (
    PullAction as BoardPullAction,
    PullDecision as BoardPullDecision,
    classify_three_way_pull as classify_board_pull,
)

__all__ = [
    "BoardPullAction",
    "BoardPullDecision",
    "BoardPullSummary",
    "apply_board_pull",
    "classify_board_pull",
    "read_board_baseline",
    "update_board_baseline_after_sync",
    "write_board_baseline",
]


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
    #: Entities the admission guard would not admit (secret-shaped content, or a
    #: machine-shaped WIRING value). Per-entity isolation: one bad card can never
    #: abort a pull. Prose (title / description / checklist) is never
    #: portability-scanned — see ``sync_admission``.
    refused: list[dict[str, str]] = None  # type: ignore[assignment]
    #: How many PULLED card files existed in the subtree and would not decode,
    #: across every board directory in this pull. Never folded into any other
    #: count and never silently zero: a remote row this side could not read is
    #: the one input indistinguishable from "the peer deleted it".
    unreadable_remote: int = 0
    #: The delete-shaped decisions this pull declined to take because the remote
    #: board they would have been derived from was not fully readable.
    delete_fenced: list[dict[str, Any]] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": self.adopted,
            "converged": self.converged,
            "kept_local": self.kept_local,
            "archived": self.archived,
            "conflicts": self.conflicts,
            "boards": list(self.boards or []),
            "refused": list(self.refused or []),
            "unreadable_remote": self.unreadable_remote,
            "delete_fenced": list(self.delete_fenced or []),
        }


class RemoteBoard(NamedTuple):
    """One pulled board directory as it could actually be READ.

    ``RemoteOffice``'s twin, same stake: the pull derives "the peer removed this
    card" from a card id being ABSENT from ``cards``, so a file that arrived
    intact and merely would not decode lands in exactly that absence — and a
    reader holding only the dict archives the member's own card on the strength
    of a parse error.
    """

    board: Board | None
    cards: dict[str, BoardCard]
    unreadable: int
    #: The board def existed and would not decode — distinct from "this
    #: directory has no board def", which is what ``board is None`` alone says.
    board_unreadable: bool = False


def _read_remote_board(board_dir: Path) -> RemoteBoard:
    def_path = board_dir / "board.json"
    board: Board | None = None
    board_unreadable = False
    if def_path.exists():
        try:
            board = from_jsonable(Board, _read_json(def_path))
        except Exception:
            board = None
            board_unreadable = True
    cards: dict[str, BoardCard] = {}
    unreadable = 0
    cards_dir = board_dir / "cards"
    if cards_dir.exists():
        for card_path in sorted(cards_dir.glob("*.json")):
            try:
                card = from_jsonable(BoardCard, _read_json(card_path))
            except Exception:
                # COUNTED, not dropped. See ``RemoteBoard``: this absence is the
                # pull's delete signal, so it may not be silent.
                unreadable += 1
                continue
            cards[card.card_id] = card
    return RemoteBoard(
        board=board, cards=cards, unreadable=unreadable, board_unreadable=board_unreadable
    )


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

    from .sync_admission import refuse_entity

    store = BoardStore(event_log=event_log)
    baseline = read_board_baseline(realm_id)
    summary = BoardPullSummary(boards=[], refused=[], delete_fenced=[])
    boards_root = subtree / "store" / "boards"
    if not boards_root.exists():
        return summary

    for board_dir in sorted(p for p in boards_root.iterdir() if p.is_dir()):
        remote = _read_remote_board(board_dir)
        remote_board, remote_cards = remote.board, remote.cards
        summary.unreadable_remote += remote.unreadable
        if remote_board is None:
            if remote.board_unreadable:
                # A directory whose board def will not decode names no board, so
                # nothing here can be attributed — but it is a real remote board
                # this pull did not apply, and the count says so.
                summary.unreadable_remote += 1
            continue
        board_id = remote_board.board_id
        # Whether the REMOTE half was fully readable decides one thing only, and
        # for this board alone: whether an absent card id may be read as the peer
        # having removed the card. Every other decision below is driven by rows
        # that DID decode.
        deletes_fenced = remote.unreadable > 0
        # Admission scan (defect (b), 2026-07-25): board files are excluded from
        # the generic pull loop, so ``_assert_no_secret_artifacts`` never saw
        # them. A board def that will not pass the door refuses WHOLE (its cards
        # have nowhere to live); a single bad card refuses alone.
        board_refusal = refuse_entity(_board_key(board_id), payload=to_jsonable(remote_board))
        if board_refusal is not None:
            summary.refused.append(board_refusal.as_dict())
            continue
        refused_card_ids: set[str] = set()
        for card_id in sorted(remote_cards):
            card_refusal = refuse_entity(_card_key(board_id, card_id), payload=to_jsonable(remote_cards[card_id]))
            if card_refusal is not None:
                summary.refused.append(card_refusal.as_dict())
                refused_card_ids.add(card_id)
                remote_cards.pop(card_id, None)
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
            # Through the STORE's adopt verb, not a raw ``atomic_json_write``:
            # the bytes are identical and the write now emits, so a pull that
            # gives this member a board (or a new column taxonomy) reaches the
            # live lane instead of landing silently on disk. See
            # ``BoardStore.adopt_remote_board``.
            store.adopt_remote_board(remote_board)
            baseline[board_key] = remote_board_hash

        # Cards: the union of local-active and remote card ids. A REFUSED card is
        # excluded outright — treating it as "absent remotely" would archive the
        # member's own copy, turning a hostile payload into a local deletion.
        for card_id in sorted((set(local_cards) | set(remote_cards) | archived_ids) - refused_card_ids):
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
                remote_card.state = "active"
                # Same swap as the board def above — the store's evented adopt
                # arm rather than a raw write, so an adopted or converged card
                # is visible to the snapshot/serve pipeline the moment it lands.
                store.adopt_remote_card(remote_card, board_id=board_id)
                baseline[key] = remote_hash or board_models.board_content_hash(remote_card)
                if decision.reason == "converged":
                    summary.converged += 1
                else:
                    summary.adopted += 1
                continue
            if decision.action == BoardPullAction.ARCHIVE_LOCAL:
                if deletes_fenced:
                    # THE delete-shaped decision, and the one this pull has not
                    # earned: "remote_removed" is inferred from the id being
                    # absent from a remote map we know to be short. Hold the
                    # card, name it, and leave the baseline alone so a repaired
                    # pull can still converge it.
                    summary.delete_fenced.append(
                        {
                            "board_id": board_id,
                            "card_id": card_id,
                            "reason": "unreadable_remote",
                            "unreadable_remote": remote.unreadable,
                        }
                    )
                    continue
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
