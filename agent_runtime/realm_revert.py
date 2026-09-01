"""Per-item revert-to-upstream for realm store drift — the second exit from
"unpublished changes" (``hermes harness realm sync revert``).

Plan: ``docs/mission_control/planned/realm-sync-local-changes-resolution.md``
(launcher repo), Stage H. Until this module existed the ONLY exit the product
offered an operator holding local store drift was **Publish** — pull
deliberately never clobbers local state (correct), so an operator whose local
changes are noise had no exit at all. Measured 2026-08-31 on the operator's
store: office drift ``offices_changed 1, actors_removed 1``, the one item a
baseline actor the census had archived locally and nobody ever meant to
publish.

Three rulings shape every line below.

* **A revert is DIAGNOSTIC intent, not authored deletion** (the actor-lifecycle
  wave's authored-vs-diagnostic ruling, §AX7). The operator is saying "my local
  unpublished state is noise; upstream is truth" — NOT "delete this
  realm-wide". So the ``added`` arm archives through the
  ``record_tombstone=False`` lane on BOTH stores, and **no revert ever mints a
  realm-visible tombstone**. That is a guarantee, pinned by
  ``tests/agent_runtime/test_realm_revert.py``.
* **Local-only.** No git, no network, no credential, no remote. This
  reconciles the local store against the local sync-repo subtree — the
  LAST-PULLED upstream picture. A fresher upstream is what Pull is for, and the
  verb refuses ``sync_repo_missing`` rather than inventing one.
* **Archive-never-delete stands.** Reverting a locally-added row archives it;
  nothing is unlinked, and ``actor-restore`` / ``board card restore`` still
  reach it.

The write arms are the PULL lane's arms, not new ones: ``adopt_remote_actor`` /
``adopt_remote_surface`` / ``restore_actor`` / ``restore_card`` /
``archive_card`` / ``remove_actor``, and the same ``sync_admission`` door every
pulled payload passes. A revert writes nothing a pull could not have written.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from utils import atomic_json_write

from . import board_models, office_models, paths
from .realm_sync import (
    DRIFT_FAMILY_BOARD,
    DRIFT_FAMILY_BOARD_CARD,
    DRIFT_FAMILY_OFFICE_ACTOR,
    DRIFT_FAMILY_OFFICE_SURFACE,
    DRIFT_KIND_ADDED,
    DRIFT_KIND_CHANGED,
    DRIFT_KIND_REMOVED,
    RealmSyncError,
    StoreDriftItem,
    _append_realm_sync_event,
    _board_store_drift,
    _office_store_drift,
    _realm_subtree,
    _safe_display_path,
    _sync_repo_path,
    _workspaces_for_realm,
    store_drift_items,
)
from .serde import to_jsonable
from .store import RealmStore

#: The event a completed revert appends. It advances the EventLog watermark the
#: same way ``realm.sync.pulled`` / ``realm.sync.published`` do, because a
#: revert rewrites store state through the same door and a live office
#: subscriber that never heard would render rows the store no longer has.
REVERT_EVENT_TYPE = "realm.sync.reverted"

#: ``updated_by`` on every write this lane takes. The pull's arms stamp
#: ``realm_sync``; this one says which sync lane moved the row, so an operator
#: reading an actor's provenance can tell a peer's pull from their own revert.
REVERT_ACTOR_REF = "realm_sync_revert"

# --- outcomes (the typed vocabulary the launcher renders per row) ----------

#: A baseline row that was archived/absent locally is live again, from the
#: subtree artifact.
OUTCOME_RESTORED = "restored_from_upstream"
#: A locally-edited row's content was overwritten with the subtree artifact's.
OUTCOME_REVERTED = "reverted_to_upstream"
#: A local-only row was archived through the ``record_tombstone=False`` lane.
OUTCOME_ARCHIVED_LOCAL_ONLY = "archived_local_only"
#: The baseline claimed an artifact the subtree does not have. Accounted, never
#: silent: the stale entry is dropped so the item stops counting.
OUTCOME_BASELINE_DROPPED = "baseline_entry_dropped"
#: There is no upstream copy to revert TO and this family has no local-only
#: archive lane (a board def / office surface cannot be "archived locally").
REFUSED_NO_UPSTREAM = "refused_no_upstream"
#: The requested item is not in this realm's current drift set — a typo, or an
#: item some earlier pass already resolved.
REFUSED_UNKNOWN_ITEM = "refused_unknown_item"
#: The subtree artifact exists and would not decode. Never treated as absence:
#: absence is what drives the baseline-drop and archive arms, so a parse error
#: read as absence is a delete-shaped decision taken on a read failure.
REFUSED_UNREADABLE_UPSTREAM = "refused_unreadable_upstream"
#: The subtree payload would not pass ``sync_admission`` — the same door the
#: pull holds against secret-shaped and machine-shaped content.
REFUSED_ADMISSION = "refused_admission"
#: The store refused the write. The item is untouched and the pass continues.
REFUSED_STORE_ERROR = "refused_store_error"

APPLIED_OUTCOMES = frozenset(
    {OUTCOME_RESTORED, OUTCOME_REVERTED, OUTCOME_ARCHIVED_LOCAL_ONLY, OUTCOME_BASELINE_DROPPED}
)

#: Families whose row IS the container definition. They have no local-only
#: archive lane, which is the one place their transition table differs.
CONTAINER_FAMILIES = frozenset({DRIFT_FAMILY_BOARD, DRIFT_FAMILY_OFFICE_SURFACE})
FAMILIES = frozenset(
    {
        DRIFT_FAMILY_BOARD,
        DRIFT_FAMILY_BOARD_CARD,
        DRIFT_FAMILY_OFFICE_SURFACE,
        DRIFT_FAMILY_OFFICE_ACTOR,
    }
)

#: Rows before containers. A restored actor leaves its workspace's
#: resurrection-guard ledger, which CHANGES the surface hash — so the surface
#: arm (and the baseline realignment that follows it) must run after the rows it
#: is downstream of, or a ``--all`` pass realigns the surface against a picture
#: one write out of date.
_PROCESS_ORDER = {
    DRIFT_FAMILY_OFFICE_ACTOR: 0,
    DRIFT_FAMILY_BOARD_CARD: 0,
    DRIFT_FAMILY_OFFICE_SURFACE: 1,
    DRIFT_FAMILY_BOARD: 1,
}


class RevertAction(str, Enum):
    RESTORE = "restore"  # baseline row, gone locally → live again from upstream
    ADOPT = "adopt"  # local row overwritten with the upstream artifact
    ARCHIVE_LOCAL = "archive_local"  # local-only row → archived, NO tombstone
    DROP_BASELINE = "drop_baseline"  # stale baseline entry with no artifact behind it
    REFUSE = "refuse"  # nothing to revert to, and nothing safe to do


@dataclass(frozen=True, slots=True)
class RevertDecision:
    action: RevertAction
    outcome: str


def classify_revert(*, family: str, kind: str, upstream_present: bool) -> RevertDecision:
    """THE transition table, pure and total over (family × kind × upstream).

    ``upstream_present`` is "the last-pulled subtree holds a decodable artifact
    for this row" — the only fact about upstream this decision needs. The
    unreadable case never reaches here: a subtree artifact that exists and will
    not decode is refused by the caller rather than folded into ``False``,
    because ``False`` is what drives the two delete-shaped arms below.
    """

    if kind == DRIFT_KIND_REMOVED:
        # Baseline says the realm has this row; locally it is archived or gone.
        return (
            RevertDecision(RevertAction.RESTORE, OUTCOME_RESTORED)
            if upstream_present
            else RevertDecision(RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED)
        )
    if kind == DRIFT_KIND_CHANGED:
        return (
            RevertDecision(RevertAction.ADOPT, OUTCOME_REVERTED)
            if upstream_present
            else RevertDecision(RevertAction.DROP_BASELINE, OUTCOME_BASELINE_DROPPED)
        )
    if kind == DRIFT_KIND_ADDED:
        # No baseline entry — but "no baseline entry" is a statement about what
        # THIS install last published, not about what the realm holds. When the
        # subtree does have the row, reverting to upstream means adopting it,
        # not deleting it; only a row upstream genuinely lacks is local-only.
        if upstream_present:
            return RevertDecision(RevertAction.ADOPT, OUTCOME_REVERTED)
        if family in CONTAINER_FAMILIES:
            return RevertDecision(RevertAction.REFUSE, REFUSED_NO_UPSTREAM)
        return RevertDecision(RevertAction.ARCHIVE_LOCAL, OUTCOME_ARCHIVED_LOCAL_ONLY)
    raise ValueError("invalid_request")


@dataclass(slots=True)
class RevertRow:
    """One item's result. ``detail`` names the exception CLASS or the admission
    code — never a message, the same disclosure rule the rest of this runtime's
    receipts follow."""

    family: str
    container: str
    item_key: str
    kind: str | None
    outcome: str
    detail: str | None = None

    @property
    def applied(self) -> bool:
        return self.outcome in APPLIED_OUTCOMES

    def as_dict(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "container": self.container,
            "item_key": self.item_key,
            "kind": self.kind,
            "outcome": self.outcome,
            "detail": self.detail,
        }


def parse_item_spec(raw: str) -> tuple[str, str, str]:
    """``FAMILY:CONTAINER:KEY`` → the triple. Raises ``RealmSyncError
    invalid_request`` for anything else: an unparseable selector is a fault in
    the REQUEST, and guessing at one is how the wrong desk gets archived."""

    text = str(raw or "").strip()
    parts = text.split(":", 2)
    if len(parts) != 3 or not all(part.strip() for part in parts):
        raise RealmSyncError(
            "invalid_request",
            "--item takes FAMILY:CONTAINER:KEY (e.g. office_actor:ws_x:dev_agent_1234).",
            safe_details={"item": text},
        )
    family, container, item_key = (part.strip() for part in parts)
    if family not in FAMILIES:
        raise RealmSyncError(
            "invalid_request",
            f"Unknown drift family {family!r}; expected one of {sorted(FAMILIES)}.",
            safe_details={"item": text},
        )
    return family, container, item_key


class _Upstream:
    """The last-pulled subtree, read through the PULL lane's own readers.

    Per-container and cached, for the reason ``_read_remote_office`` exists at
    all: the payload is truth and the filename is routing, so an item is looked
    up by its decoded key rather than by recomputing a filename token — and the
    directory's ``unreadable`` count travels with the rows, so an artifact that
    exists and would not decode can be told apart from one that is absent.
    """

    def __init__(self, subtree: Path) -> None:
        self._subtree = subtree
        self._offices: dict[str, Any] = {}
        self._boards: dict[str, Any] = {}

    def _office(self, workspace_id: str):
        from .office_sync import _read_remote_office

        if workspace_id not in self._offices:
            office_dir = self._subtree / "store" / "office" / paths.safe_path_token(workspace_id)
            self._offices[workspace_id] = _read_remote_office(office_dir)
        return self._offices[workspace_id]

    def _board(self, board_id: str):
        from .board_sync import _read_remote_board

        if board_id not in self._boards:
            board_dir = self._subtree / "store" / "boards" / paths.safe_path_token(board_id)
            self._boards[board_id] = _read_remote_board(board_dir)
        return self._boards[board_id]

    def lookup(self, family: str, container: str, item_key: str) -> tuple[Any, bool]:
        """``(entity_or_None, unreadable)``. ``unreadable`` True means the
        artifact is not decodable HERE, which is never the same answer as
        absent."""

        if family == DRIFT_FAMILY_OFFICE_SURFACE:
            remote = self._office(container)
            return remote.surface, remote.surface_unreadable
        if family == DRIFT_FAMILY_OFFICE_ACTOR:
            remote = self._office(container)
            actor = remote.actors.get(item_key)
            return actor, actor is None and remote.unreadable > 0
        if family == DRIFT_FAMILY_BOARD:
            remote = self._board(container)
            return remote.board, remote.board_unreadable
        remote = self._board(container)
        card = remote.cards.get(item_key)
        return card, card is None and remote.unreadable > 0


def revert_realm_sync(
    realm_id: str,
    *,
    item_specs: list[str] | None = None,
    revert_all: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Revert the selected drifted store rows to the last-pulled upstream.

    Local-only and credential-free by construction: the only thing read from
    outside the store is the checked-out subtree already on disk.
    """

    from .board_store import BoardStore
    from .board_sync import read_board_baseline, write_board_baseline
    from .office_store import OfficeStore
    from .office_sync import read_office_baseline, write_office_baseline

    requested = [spec for spec in (item_specs or []) if str(spec).strip()]
    if revert_all and requested:
        raise RealmSyncError(
            "invalid_request", "Pass --all or --item, never both — the selection must be unambiguous."
        )
    if not revert_all and not requested:
        raise RealmSyncError(
            "invalid_request", "Nothing selected: pass --all, or at least one --item FAMILY:CONTAINER:KEY."
        )

    realm = RealmStore().get(realm_id)
    repo = _sync_repo_path(realm)
    subtree = _realm_subtree(repo, realm.id)
    # The refusal is deliberately BEFORE any selection work, and it covers the
    # missing SUBTREE as well as the missing clone. Without the subtree every
    # item would read as "upstream does not have this", and the ``added`` arm
    # would then archive the operator's whole local office on the strength of a
    # directory that was never cloned.
    if not repo.exists():
        raise RealmSyncError(
            "sync_repo_missing",
            "The realm sync repo is not present locally; pull before reverting.",
            safe_details={"missing": "sync_repo", "sync_repo": _safe_display_path(repo)},
        )
    if not subtree.exists():
        raise RealmSyncError(
            "sync_repo_missing",
            "This realm has no pulled subtree in the local sync repo; pull before reverting.",
            safe_details={"missing": "realm_subtree", "sync_repo": _safe_display_path(repo)},
        )

    workspaces = _workspaces_for_realm(realm)
    drift = store_drift_items(realm.id, workspaces)
    by_spec = {item.spec: item for item in drift}

    selected: list[StoreDriftItem] = []
    rows: list[RevertRow] = []
    if revert_all:
        selected = list(drift)
    else:
        for raw in requested:
            family, container, item_key = parse_item_spec(raw)
            item = by_spec.get(f"{family}:{container}:{item_key}")
            if item is None:
                rows.append(
                    RevertRow(
                        family=family,
                        container=container,
                        item_key=item_key,
                        kind=None,
                        outcome=REFUSED_UNKNOWN_ITEM,
                        detail="not_in_drift_set",
                    )
                )
                continue
            selected.append(item)

    upstream = _Upstream(subtree)
    office_store = OfficeStore()
    board_store = BoardStore()
    office_baseline = read_office_baseline(realm.id)
    board_baseline = read_board_baseline(realm.id)
    touched_office = touched_board = False

    for item in sorted(selected, key=lambda row: (_PROCESS_ORDER[row.family], row.family, row.container, row.item_key)):
        row = _revert_one(
            item,
            upstream=upstream,
            office_store=office_store,
            board_store=board_store,
            office_baseline=office_baseline,
            board_baseline=board_baseline,
            dry_run=dry_run,
        )
        rows.append(row)
        if row.applied:
            if item.family in (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_FAMILY_OFFICE_SURFACE):
                touched_office = True
            else:
                touched_board = True

    applied = [row for row in rows if row.applied]
    if not dry_run:
        # The baselines are realigned the way the publish/pull lanes maintain
        # them — PER ITEM and from the store's own post-write content, never by
        # re-recording the whole workspace. Re-recording would zero the drift of
        # every row the operator did NOT select, which is exactly the lie this
        # accounting exists to prevent.
        if touched_office:
            write_office_baseline(realm.id, office_baseline)
        if touched_board:
            write_board_baseline(realm.id, board_baseline)
        if applied:
            _append_realm_sync_event(
                REVERT_EVENT_TYPE, realm, changed=True, artifacts=len(applied)
            )

    return {
        "id": realm.id,
        "realm_id": realm.id,
        "dry_run": bool(dry_run),
        "selection": "all" if revert_all else "items",
        "count": len(rows),
        "reverted": len(applied),
        "refused": len(rows) - len(applied),
        "items": [row.as_dict() for row in rows],
        # Measured AFTER the pass. On ``--dry-run`` nothing was applied, so this
        # is the unchanged current drift — ``dry_run`` above is what says which.
        "store_drift_after": {
            "boards": _board_store_drift(realm.id, workspaces),
            "office": _office_store_drift(realm.id, workspaces),
        },
        "sync_repo": _safe_display_path(repo),
    }


def _revert_one(
    item: StoreDriftItem,
    *,
    upstream: _Upstream,
    office_store,
    board_store,
    office_baseline: dict[str, str],
    board_baseline: dict[str, str],
    dry_run: bool,
) -> RevertRow:
    """One item, decided purely and then applied. Every store refusal is caught
    HERE and reported as a row: a realm must not stop converging because one
    file would not archive (the pull's per-entity isolation rule)."""

    entity, unreadable = upstream.lookup(item.family, item.container, item.item_key)
    row = RevertRow(
        family=item.family,
        container=item.container,
        item_key=item.item_key,
        kind=item.kind,
        outcome=REFUSED_UNREADABLE_UPSTREAM,
    )
    if unreadable:
        row.detail = "subtree_artifact_unreadable"
        return row

    decision = classify_revert(
        family=item.family, kind=item.kind, upstream_present=entity is not None
    )
    row.outcome = decision.outcome
    if decision.action is RevertAction.REFUSE:
        row.detail = "no_subtree_artifact"
        return row

    baseline = (
        office_baseline
        if item.family in (DRIFT_FAMILY_OFFICE_ACTOR, DRIFT_FAMILY_OFFICE_SURFACE)
        else board_baseline
    )
    key = item.baseline_key()

    if decision.action is RevertAction.DROP_BASELINE:
        if not dry_run:
            baseline.pop(key, None)
        return row

    if decision.action in (RevertAction.ADOPT, RevertAction.RESTORE):
        from .sync_admission import refuse_entity

        # The same door every pulled payload passes. A revert adopts bytes this
        # machine did not author, so it inherits the pull's trust boundary
        # rather than opening a second one beside it.
        refusal = refuse_entity(key, payload=to_jsonable(entity))
        if refusal is not None:
            row.outcome = REFUSED_ADMISSION
            row.detail = refusal.code
            return row

    if dry_run:
        return row

    try:
        if decision.action is RevertAction.RESTORE:
            _restore_from_upstream(item, entity, office_store=office_store, board_store=board_store)
        elif decision.action is RevertAction.ADOPT:
            _adopt_from_upstream(item, entity, office_store=office_store, board_store=board_store)
        else:  # ARCHIVE_LOCAL
            _archive_local_only(item, office_store=office_store, board_store=board_store)
    except Exception as exc:  # noqa: BLE001 — accounted, never silent; the pass continues
        row.outcome = REFUSED_STORE_ERROR
        row.detail = type(exc).__name__
        return row

    if decision.action is RevertAction.ARCHIVE_LOCAL:
        # A local-only row has no baseline entry by definition; ``pop`` states
        # that rather than assuming it.
        baseline.pop(key, None)
        return row

    try:
        baseline[key] = _current_content_hash(item, office_store=office_store, board_store=board_store)
    except Exception as exc:  # noqa: BLE001 — the write landed; the receipt did not
        # The write happened, so the row is NOT reported as refused — but a
        # baseline entry that cannot be re-read is left absent rather than
        # guessed, which reclassifies the row as locally added on the next
        # status. Honest, and repairable by a publish.
        baseline.pop(key, None)
        row.detail = f"baseline_unrecorded:{type(exc).__name__}"
    return row


def _restore_from_upstream(item: StoreDriftItem, entity, *, office_store, board_store) -> None:
    """The un-archive door, then the upstream content on top of it.

    Two writes and not one, deliberately. The archive copy holds THIS machine's
    last local bytes, and only the store's own restore verb clears the
    resurrection-guard ledger entry that made the row invisible; adopting the
    upstream copy straight over a live-but-tombstoned key would leave a desk
    that the next pull's classifier still reads as archived. When the restored
    content already equals upstream's, the second write is skipped — the
    idempotence this lane promises is not paid for with a revision bump per
    call.

    A key that is in the resurrection-guard ledger with NO archive copy behind
    it falls through to the adopt arm alone, which leaves the ledger entry
    standing. No writer in this runtime produces that state (the archive copy is
    written BEFORE the ledger entry, and ``remove_actor`` itself reports
    not-found in it), so it is not machinery this lane grows a second door for —
    but it is why the ledger check is spelled as "is there an archive copy" and
    not "is this key archived".
    """

    if item.family == DRIFT_FAMILY_OFFICE_ACTOR:
        if paths.office_archived_actor_path(item.container, item.item_key).exists():
            restored = office_store.restore_actor(
                item.container, item.item_key, updated_by=REVERT_ACTOR_REF
            )
            if office_models.office_content_hash(restored) == office_models.office_content_hash(entity):
                return
        _adopt_from_upstream(item, entity, office_store=office_store, board_store=board_store)
        return
    if paths.board_archived_card_path(item.container, item.item_key).exists():
        restored = board_store.restore_card(
            item.item_key, board_id=item.container, updated_by=REVERT_ACTOR_REF
        )
        if board_models.board_content_hash(restored) == board_models.board_content_hash(entity):
            return
    _adopt_from_upstream(item, entity, office_store=office_store, board_store=board_store)


def _adopt_from_upstream(item: StoreDriftItem, entity, *, office_store, board_store) -> None:
    """Write the subtree artifact over the local row — the pull's adopt arms,
    unchanged.

    The board families go through ``atomic_json_write`` because that is what
    ``board_sync.apply_board_pull`` still does: the office store grew evented
    ``adopt_remote_*`` verbs in the actor-lifecycle wave (H1) and the board
    store did not, so this lane matches its family's pull arm rather than
    inventing a third spelling. (Queue row filed: the board pull's adopt arm is
    still event-less.)
    """

    if item.family == DRIFT_FAMILY_OFFICE_ACTOR:
        entity.workspace_id = item.container
        entity.state = "active"
        office_store.adopt_remote_actor(entity, updated_by=REVERT_ACTOR_REF)
        return
    if item.family == DRIFT_FAMILY_OFFICE_SURFACE:
        entity.workspace_id = item.container
        office_store.adopt_remote_surface(entity, updated_by=REVERT_ACTOR_REF)
        return
    if item.family == DRIFT_FAMILY_BOARD:
        entity.board_id = item.container
        atomic_json_write(
            paths.board_def_path(item.container), to_jsonable(entity), indent=2, sort_keys=True
        )
        return
    entity.board_id = item.container
    entity.state = "active"
    atomic_json_write(
        paths.board_card_path(item.container, item.item_key),
        to_jsonable(entity),
        indent=2,
        sort_keys=True,
    )


def _archive_local_only(item: StoreDriftItem, *, office_store, board_store) -> None:
    """THE ruling, spelled once: a revert archives through the
    ``record_tombstone=False`` lane on both stores, so no realm-visible ledger
    entry is minted. See the module docstring (§AX7)."""

    if item.family == DRIFT_FAMILY_OFFICE_ACTOR:
        office_store.remove_actor(
            item.container,
            item.item_key,
            reason="revert_local_only",
            updated_by=REVERT_ACTOR_REF,
            record_tombstone=False,
        )
        return
    board_store.archive_card(
        item.item_key,
        board_id=item.container,
        reason="revert_local_only",
        updated_by=REVERT_ACTOR_REF,
        record_tombstone=False,
    )


def _current_content_hash(item: StoreDriftItem, *, office_store, board_store) -> str:
    """The baseline is realigned from the STORE's post-write content, not from
    the upstream artifact's hash.

    The two are not always equal and the difference is the honest part:
    ``adopt_remote_surface`` UNIONS the resurrection-guard ledger (C1), so a
    surface holding a local-only tombstone the realm has not seen re-hashes
    away from upstream. Recording the store's own content says "this is what I
    would publish", which is what a baseline means; recording the remote's would
    make the sheet read in-sync for content this install does not hold.
    """

    if item.family == DRIFT_FAMILY_OFFICE_ACTOR:
        return office_models.office_content_hash(office_store.get_actor(item.container, item.item_key))
    if item.family == DRIFT_FAMILY_OFFICE_SURFACE:
        return office_models.office_content_hash(office_store.get_surface(item.container))
    if item.family == DRIFT_FAMILY_BOARD:
        return board_models.board_content_hash(board_store.get(item.container))
    return board_models.board_content_hash(board_store.get_card(item.item_key, board_id=item.container))
