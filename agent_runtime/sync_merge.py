"""Shared 3-way pull-merge classifier for realm-synced artifact families.

Lifted VERBATIM from ``board_sync.classify_board_pull`` on 2026-07-17, when the
Mission Office family became the classifier's second consumer
(``docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md`` §5 —
launcher repo). ``board_sync`` re-exports the board-named aliases so its
exhaustive decision-table tests keep passing unmodified, which is the proof the
lift changed nothing.

The model: change is detected against a NEVER-synced per-realm baseline sidecar
(semantic content hash ``H``, timestamps excluded). Divergence on both sides is
a **loud conflict** — no clock, no last-writer-wins, no CRDT. The decision
table is exhaustive over (local-state × remote-state × archived-ledger).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class PullAction(str, Enum):
    NOOP = "noop"
    WRITE_REMOTE = "write_remote"  # adopt/take-remote/converge → write remote + baseline
    KEEP_LOCAL = "keep_local"  # local changed vs unchanged remote → stays unpublished
    ARCHIVE_LOCAL = "archive_local"  # remote removed the entity → archive (never delete)
    CONFLICT = "conflict"  # both diverged / edit-vs-remove / archive-vs-edit


@dataclass(frozen=True, slots=True)
class PullDecision:
    action: PullAction
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


def classify_three_way_pull(
    local_hash: str | None,
    remote_hash: str | None,
    baseline_hash: str | None,
    *,
    locally_archived: bool = False,
) -> PullDecision:
    """Pure per-entity pull decision. ``*_hash`` are semantic content hashes
    (timestamp/revision-excluded); ``None`` means the entity is absent on that
    side. ``locally_archived`` is the resurrection-guard ledger membership.
    """

    ls = _local_state(local_hash, baseline_hash, locally_archived)
    rs = _remote_state(remote_hash, baseline_hash)

    if ls == "archived":
        # Ledger blocks resurrection: a pulled remote copy never re-creates a
        # locally archived entity. A remote EDIT of it is a loud conflict.
        if rs in ("absent", "unchanged"):
            return PullDecision(PullAction.NOOP, "archived_local")
        return PullDecision(PullAction.CONFLICT, "archive_vs_edit")

    if ls == "unchanged":
        if rs == "unchanged":
            return PullDecision(PullAction.NOOP, "unchanged")
        if rs == "changed":
            return PullDecision(PullAction.WRITE_REMOTE, "take_remote")
        if rs == "absent":
            return PullDecision(PullAction.ARCHIVE_LOCAL, "remote_removed")
        return PullDecision(PullAction.WRITE_REMOTE, "take_remote")

    if ls == "changed":
        if rs == "unchanged":
            return PullDecision(PullAction.KEEP_LOCAL, "unpublished")
        if rs == "changed":
            if local_hash == remote_hash:
                return PullDecision(PullAction.WRITE_REMOTE, "converged")
            return PullDecision(PullAction.CONFLICT, "both_changed")
        if rs == "absent":
            return PullDecision(PullAction.CONFLICT, "edit_vs_remove")
        return PullDecision(PullAction.CONFLICT, "both_changed")

    if ls == "new":  # local present, no baseline
        if rs == "absent":
            return PullDecision(PullAction.KEEP_LOCAL, "new_local")
        if rs == "new":
            if local_hash == remote_hash:
                return PullDecision(PullAction.WRITE_REMOTE, "converged")
            return PullDecision(PullAction.CONFLICT, "new_both")
        return PullDecision(PullAction.CONFLICT, "new_both")

    # ls == "absent"
    if rs == "absent":
        return PullDecision(PullAction.NOOP, "absent_both")
    return PullDecision(PullAction.WRITE_REMOTE, "adopt_remote")
