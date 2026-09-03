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


def merge_archived_ledgers(peer_keys, local_keys, *, cap: int) -> list[str]:
    """Union two resurrection-guard ledgers — peer order first, local tail.

    The classifier above reads ``locally_archived`` to refuse a resurrection,
    and this is the function that keeps that input alive across a pull. It
    lives here, beside the classifier, for the same reason the classifier does:
    the office family (``archived_actor_keys``) and the board family
    (``archived_card_ids``) are two spellings of one rule, and a rule with two
    copies is free to disagree with itself — which is exactly what happened.
    Written for the office family on 2026-08-30 (C1) and lifted here on
    2026-09-03 when the board family's ``adopt_remote_board`` took it too.

    The hole it closes: a ledger that one side can OVERWRITE guards nothing.
    Both families' adopt verbs wrote the peer's list verbatim, so a pull from a
    member that had never heard of a key this install archived silently erased
    that key's tombstone — a deletion of the exact evidence
    :func:`classify_three_way_pull` reads. Reachable, not theoretical: publish
    records the LOCAL hash as the baseline, so an install that archives and
    publishes is ``unchanged`` on its next pull, and one peer edit away from
    ``take_remote`` over its own ledger.

    Two properties, both load-bearing, neither an accident of the expression:

    * **the peer's list leads, in the peer's order.** When the local ledger is a
      subset of the peer's (the converged case, and the common one) the result is
      the peer's list byte-for-byte, so the family's content hash still matches
      the remote and a pull that changed nothing stays a no-op. Local-first
      ordering would re-hash every converged entity and hand the next pull a
      permanent "unpublished" local edit over identical content.
    * **local-only keys land at the TAIL**, which is the end the ``cap``
      truncation keeps. Under cap pressure the keys that survive are the ones
      THIS install archived — the ones whose resurrection this store is the only
      witness to.

    ``cap`` is a required argument rather than a constant here: each family owns
    its own ledger bound (``office_store.ARCHIVED_LEDGER_CAP``,
    ``board_store.ARCHIVED_LEDGER_CAP``), and a third copy in this module would
    be the same duplication this lift exists to remove.

    Deduplicated on first occurrence: neither family's local archive arm can
    produce a repeat, so a duplicate can only have arrived from a peer, and
    carrying it forward would spend cap budget on a key already guarded.

    THE PROPAGATION DIRECTION, stated because the union is what makes it total:
    a key that enters this ledger on ANY store travels — the union keeps it, the
    next publish carries it into the realm, and every member that pulls
    afterwards reads it as ``locally_archived`` and will archive that entity.
    **Writing a key into this ledger is therefore REALM-WIDE intent, not a local
    note**, and it is one-way: the ledger has no "un-tombstone" that syncs (the
    restore verbs drop the key locally, and the next pull from any peer that
    still holds it puts it back).

    The consequence neither store can currently see, recorded here rather than
    guessed at: an archive taken as a RECEIVER-SIDE REPAIR — evicting a desk or
    a card whose owner never existed on this machine, the realm-pulled orphan —
    is indistinguishable in this list from an AUTHORED delete, and the union
    exports both. Live on 2026-08-30: the Mac store's ledger holds
    ``personainst_neko_supervisor_agent_9682caf4`` from exactly such a repair
    while the actor is alive and correctly linked at its ORIGIN, a Windows store
    that has not yet pulled; the first pull after the Mac publishes will archive
    it there. That case matches the operator's intent, so the union is right as
    it stands and losing tombstones remains the worse failure. The TYPED SPLIT —
    an authored tombstone versus a local eviction that mints no realm-visible
    one — belongs with the delete lane that decides between the two verbs
    (Track A1's hermes half), not with this merge. The board family's
    ``archive_card(..., record_tombstone=False)`` is that split's near half,
    already shipped: a ledger entry withheld never reaches this union at all.
    """

    merged: list[str] = []
    seen: set[str] = set()
    for key in (*(peer_keys or ()), *(local_keys or ())):
        text = str(key)
        if text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return merged[-cap:]
