"""Realm-sync for the Mission Office: per-actor 3-way baseline merge with loud
conflicts, no CRDT — the board family's shape applied to office actor files
(``docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md`` §5,
launcher repo).

The pure classifier is the SHARED ``sync_merge.classify_three_way_pull`` (lifted
from board_sync when this module became its second consumer). Change is
detected against a NEVER-synced baseline sidecar (semantic content hash H,
timestamps excluded — so the deterministic default surface converges instead of
conflicting). Conflicts are never silent: a sidecar in ``conflicts/`` + a
snapshot parity warning + an explicit ``office resolve-conflict`` verb.

Office files are excluded from the generic realm-sync pull overwrite
(``_destination_for_sync_path`` returns None for ``store/office/*``); this
module owns the office pull instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple

from utils import atomic_json_write

from . import office_models, paths
from .events import EventLog
from .models import OfficeActor, OfficeSurface
from .office_store import ActorScan, OfficeStore, _read_json
from .serde import from_jsonable, to_jsonable
from .sync_merge import PullAction, classify_three_way_pull

#: The one word every office sync arm spends when it cannot READ the world it
#: was asked to decide about. Minted in exactly ONE place
#: (:meth:`OfficeSyncRefusal.for_scan`) so no arm can quietly reuse another
#: arm's sentence for a condition it does not describe.
SYNC_UNKNOWABLE = "sync_unknowable"


@dataclass(frozen=True, slots=True)
class OfficeSyncRefusal:
    """One workspace's sync arm refusing rather than deciding on a short list.

    ``office_store.read_actor_dir`` skips a file it cannot decode and returns
    the rest, so every arm that reads only the ROWS receives a SHORTER world
    that describes itself as complete. For a reader that is a wrong number; for
    these two arms it is worse, because both of them are writers:

    * publish copies the actor FILES verbatim, so the undecodable file travels —
      and every peer's :func:`apply_office_pull` then finds an actor key present
      locally, absent from the remote map it could decode, and ARCHIVES it. One
      quarantined file on one member's disk becomes a desk removal on every
      other member's.
    * the pull's compare arm reads the LOCAL actors to classify; an actor whose
      file will not decode arrives as "locally absent", which is exactly the
      input the three-way classifier reads as a local delete.

    So the arm refuses this workspace and says how many rows it could not read.
    Per-workspace, never global: unreadability in one office says nothing about
    the next one, and a refusal that froze the whole realm would be a worse
    failure than the one being prevented — the bystander rule the class-key
    fence already spends (``test_an_unreadable_sibling_does_not_refuse_the_
    writes_it_cannot_be_about``).
    """

    workspace_id: str
    unreadable: int
    reason: str = SYNC_UNKNOWABLE

    @classmethod
    def for_scan(cls, workspace_id: str, scan: ActorScan) -> "OfficeSyncRefusal | None":
        """THE mint. ``None`` means the world was fully readable — so an arm can
        neither report a refusal it did not earn nor default-construct one that
        swallows the count."""

        if not scan.unreadable:
            return None
        return cls(workspace_id=workspace_id, unreadable=scan.unreadable)

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "reason": self.reason,
            "unreadable": self.unreadable,
        }


#: The one word an archive that actually happened is spelled with. Failures are
#: ``archive_failed:<ExceptionClass>`` — the class, never the message, the same
#: disclosure rule the rest of this runtime's receipts follow.
ARCHIVE_OUTCOME_ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class OfficeArchiveOutcome:
    """What the pull's delete-shaped arm actually DID to one actor.

    The arm was ``try: remove_actor(...) / except Exception: pass`` with the
    ``baseline.pop`` outside the ``try``, which is the "best-effort lane
    discarded its outcome" class this repo already tracks (see the queue row;
    Track H-H3 closes the class across ``office_store``, this is the pull-side
    instance). The cost here is not a missing log line: dropping the baseline
    entry for a row that is STILL LIVE re-classifies it on the next pull as a
    local ADD (``_local_state`` reads a missing baseline as ``new``), so a
    failed archive becomes a desk the pull will offer to publish back to the
    realm. The fenced sibling beside it has had an accounting row
    (``delete_fenced``) since the day it was written; the failed case had none.

    One outcome per key, successes included, because a list of only failures
    cannot answer "did this arm reach this key at all" — the same reason
    ``archive_actors_for_instance`` names the keys it archived beside the ones
    it could not.
    """

    workspace_id: str
    actor_key: str
    outcome: str

    @classmethod
    def archived(cls, workspace_id: str, actor_key: str) -> "OfficeArchiveOutcome":
        return cls(workspace_id=workspace_id, actor_key=actor_key, outcome=ARCHIVE_OUTCOME_ARCHIVED)

    @classmethod
    def failed(cls, workspace_id: str, actor_key: str, exc: BaseException) -> "OfficeArchiveOutcome":
        return cls(
            workspace_id=workspace_id,
            actor_key=actor_key,
            outcome=f"archive_failed:{type(exc).__name__}",
        )

    @property
    def succeeded(self) -> bool:
        return self.outcome == ARCHIVE_OUTCOME_ARCHIVED

    def as_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "actor_key": self.actor_key,
            "outcome": self.outcome,
        }


@dataclass(slots=True)
class OfficeBaselineSummary:
    """What the publish-side baseline arm actually recorded, and what it would
    not. Returned rather than logged because the caller publishes on it."""

    recorded: list[str] = None  # type: ignore[assignment]
    refused: list[dict[str, Any]] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "recorded": list(self.recorded or []),
            "refused": list(self.refused or []),
        }


# --- baseline sidecar (never synced, never published) --------------------


def read_office_baseline(realm_id: str) -> dict[str, str]:
    path = paths.office_baseline_path(realm_id)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return {str(k): str(v) for k, v in entries.items()} if isinstance(entries, dict) else {}


def write_office_baseline(realm_id: str, entries: dict[str, str]) -> None:
    atomic_json_write(
        paths.office_baseline_path(realm_id),
        {"schema_version": 1, "entries": entries},
        indent=2,
        sort_keys=True,
    )


def _actor_key(workspace_id: str, actor_key: str) -> str:
    return f"{workspace_id}:actor:{actor_key}"


def _surface_key(workspace_id: str) -> str:
    return f"{workspace_id}:office"


# --- publish baseline update ---------------------------------------------


def update_office_baseline_after_sync(realm_id: str, workspace_ids: list[str]) -> OfficeBaselineSummary:
    """Record H for every surface + active actor of the given workspaces as the
    new baseline (called after a successful publish OR pull).

    A workspace whose actor directory does not fully READ is REFUSED here rather
    than recorded short. The baseline is the answer to "what did I last
    publish"; writing it from a list that silently lost a row states that the
    row was never published, and the very next pull reads that absence as the
    peer having deleted the desk. The strip-then-rewrite is therefore
    per-workspace: a refused workspace keeps the rows it already had, so the
    refusal costs the operator a stale baseline (repairable) instead of a wiped
    one (a delete-shaped lie).
    """

    store = OfficeStore()
    baseline = read_office_baseline(realm_id)
    summary = OfficeBaselineSummary(recorded=[], refused=[])
    for workspace_id in workspace_ids:
        exists = store.surface_exists(workspace_id)
        # The SCAN, and its ``unreadable`` spent rather than dropped: the rows
        # alone describe a directory this read may only have half-decoded, which
        # is precisely the fact this arm has to know before it writes a
        # completeness claim.
        scan = store.scan_actors(workspace_id) if exists else ActorScan([], 0)
        refusal = OfficeSyncRefusal.for_scan(workspace_id, scan)
        if refusal is not None:
            summary.refused.append(refusal.as_dict())
            continue
        prefix = f"{workspace_id}:"
        baseline = {k: v for k, v in baseline.items() if not k.startswith(prefix)}
        if not exists:
            continue
        surface = store.get_surface(workspace_id)
        baseline[_surface_key(workspace_id)] = office_models.office_content_hash(surface)
        for actor in scan.actors:
            baseline[_actor_key(workspace_id, actor.actor_key)] = office_models.office_content_hash(actor)
        summary.recorded.append(workspace_id)
    write_office_baseline(realm_id, baseline)
    return summary


# --- pull application ------------------------------------------------------


@dataclass(slots=True)
class OfficePullSummary:
    adopted: int = 0
    converged: int = 0
    kept_local: int = 0
    archived: int = 0
    conflicts: int = 0
    workspaces: list[str] = None  # type: ignore[assignment]
    #: Entities the admission guard would not admit (secret-shaped content, or a
    #: machine-shaped WIRING value — a ``backing_profile`` holding an absolute
    #: path, say). Per-entity isolation: one bad actor never aborts a pull.
    refused: list[dict[str, str]] = None  # type: ignore[assignment]
    #: Workspaces whose LOCAL actor directory would not fully read, so the
    #: compare arm declined to classify them at all. A different fact from
    #: ``refused`` (the door turned that content away) and it keeps its own
    #: word: nothing here was judged, so nothing here may be reported as kept,
    #: adopted, or archived.
    unknowable: list[dict[str, Any]] = None  # type: ignore[assignment]
    #: How many PULLED actor files existed in the subtree and would not decode,
    #: across every office directory in this pull. Never folded into any other
    #: count and never silently zero: a remote row this side could not read is
    #: the one input that is indistinguishable from "the peer deleted it".
    unreadable_remote: int = 0
    #: The delete-shaped decisions this pull declined to take because the remote
    #: office they would have been derived from was not fully readable. Each row
    #: names the workspace and actor key so the desk that was NOT archived is a
    #: fact the operator can read, not an inference from a missing count.
    delete_fenced: list[dict[str, Any]] = None  # type: ignore[assignment]
    #: One :class:`OfficeArchiveOutcome` per key the archive arm REACHED —
    #: ``archived`` or ``archive_failed:<ExceptionClass>``. Beside
    #: ``delete_fenced``, which names the deletes this pull declined to take;
    #: this names the ones it took, and the ones it tried and could not.
    #: ``archived`` is this list's success count by construction.
    archive_outcomes: list[dict[str, Any]] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": self.adopted,
            "converged": self.converged,
            "kept_local": self.kept_local,
            "archived": self.archived,
            "conflicts": self.conflicts,
            "workspaces": list(self.workspaces or []),
            "refused": list(self.refused or []),
            "unknowable": list(self.unknowable or []),
            "unreadable_remote": self.unreadable_remote,
            "delete_fenced": list(self.delete_fenced or []),
            "archive_outcomes": list(self.archive_outcomes or []),
        }


class RemoteOffice(NamedTuple):
    """One pulled office directory as it could actually be READ.

    ``unreadable`` travels with the rows for the same reason ``ActorScan``'s
    does, and here the stake is a deletion rather than a wrong number: the pull
    derives "the peer removed this desk" from a key being ABSENT from
    ``actors``. A file that arrived intact and merely would not decode lands in
    exactly that absence — so a reader holding only the dict would archive the
    member's own placement on the strength of a parse error.
    """

    surface: OfficeSurface | None
    actors: dict[str, OfficeActor]
    unreadable: int
    #: The surface file existed and would not decode — distinct from "this
    #: directory has no surface", which is what ``surface is None`` alone says.
    surface_unreadable: bool = False


def _read_remote_office(office_dir: Path) -> RemoteOffice:
    """One PULLED office directory, decoded through the same reader the local
    store uses (AX6).

    This was the THIRD actor-directory reader in the runtime and it spelled the
    walk, the swallow and the unreadable count itself — a second spelling of
    ``OfficeStore``'s discipline, sitting in the one place where the two
    disagreeing produces a DELETION rather than a wrong number: an actor key
    absent from ``actors`` is exactly how the pull infers "the peer removed this
    desk". It now calls ``office_store.read_actor_dir``, which was lifted out of
    the store for this (it never took ``self``), so the two cannot drift and the
    pulled directory gains the per-class warning line the local ones have had.

    ONE decode outcome this reader used to have and the store did not, now
    folded rather than dropped: a payload that decoded FINE and carries no
    ``actor_key``. There is nothing to key it by, so it cannot enter the map —
    and it used to leave with no trace at all, which put it in the same silent
    absence a corrupt file used to occupy. It counts as unreadable, which is the
    safe direction: ``unreadable`` is what fences this workspace's delete-shaped
    decisions (``_reconcile_actors``' ``deletes_fenced``), and a remote office
    holding a row nobody can route is not a remote office this pull should be
    inferring removals from.
    """

    from .office_store import read_actor_dir

    surface_path = office_dir / "office.json"
    surface: OfficeSurface | None = None
    surface_unreadable = False
    if surface_path.exists():
        try:
            surface = from_jsonable(OfficeSurface, _read_json(surface_path))
        except Exception:
            surface = None
            surface_unreadable = True
    scan = read_actor_dir(office_dir / "actors")
    actors: dict[str, OfficeActor] = {}
    unkeyed = 0
    for actor in scan.actors:
        # Payload is truth; the filename is routing only (plan §4.3) — which is
        # why a payload with no key cannot be rescued by the file it arrived in.
        if not actor.actor_key:
            unkeyed += 1
            continue
        actors[actor.actor_key] = actor
    return RemoteOffice(
        surface=surface,
        actors=actors,
        unreadable=scan.unreadable + unkeyed,
        surface_unreadable=surface_unreadable,
    )


def _write_conflict_sidecar(
    workspace_id: str,
    actor_key: str,
    *,
    kind: str,
    remote_actor: OfficeActor | None,
    local_hash: str | None,
    remote_hash: str | None,
) -> None:
    atomic_json_write(
        paths.office_conflict_path(workspace_id, actor_key),
        {
            "schema_version": 1,
            "workspace_id": workspace_id,
            "actor_key": actor_key,
            "kind": kind,
            "local_hash": local_hash,
            "remote_hash": remote_hash,
            "remote_actor": to_jsonable(remote_actor) if remote_actor is not None else None,
        },
        indent=2,
        sort_keys=True,
    )


def _reconcile_surface(
    store: OfficeStore,
    baseline: dict[str, str],
    *,
    remote_surface: OfficeSurface,
    local_surface: OfficeSurface | None,
) -> None:
    """The SURFACE half of one workspace's pull.

    File-granular merge, keep-local-wins on both-changed (v1 — folders are
    additive taxonomy, and a lost folder re-adds trivially). Takes no
    ``summary``: the surface arm has never counted anything, and a parameter it
    could write to would invite it to start.
    """

    workspace_id = remote_surface.workspace_id
    surface_key = _surface_key(workspace_id)
    local_hash = office_models.office_content_hash(local_surface) if local_surface is not None else None
    remote_hash = office_models.office_content_hash(remote_surface)
    decision = classify_three_way_pull(local_hash, remote_hash, baseline.get(surface_key))
    if decision.action != PullAction.WRITE_REMOTE and local_surface is not None:
        return
    # Through the store (H1), not ``atomic_json_write``: an event-less write is
    # invisible to the watermark-gated snapshot/serve pipeline, and the archive
    # arm has always emitted. The baseline stays keyed off the REMOTE hash — the
    # adopt verb stamps ``updated_by``, which ``office_content_hash`` excludes,
    # and unions the tombstone ledger, which it does not (C1): where the union
    # actually keeps a local-only key the surface reads as locally edited until
    # the next publish, which is the honest state.
    store.adopt_remote_surface(remote_surface)
    baseline[surface_key] = remote_hash


def _reconcile_actors(
    store: OfficeStore,
    summary: OfficePullSummary,
    baseline: dict[str, str],
    *,
    workspace_id: str,
    local_actors: dict[str, OfficeActor],
    remote: RemoteOffice,
    archived_keys: set[str],
    refused_actor_keys: set[str],
) -> None:
    """The ACTOR half of one workspace's pull: one three-way decision per key,
    over the union of local-active, remote, and locally archived keys, minus
    anything the admission door refused.

    ``deletes_fenced`` is derived HERE from ``remote.unreadable`` rather than
    passed in, because this is the only arm entitled to ask the question:
    whether the remote half read completely decides one thing only, and decides
    it for this workspace alone — whether an absent key may be read as the peer
    having removed the desk.
    """

    deletes_fenced = remote.unreadable > 0
    remote_actors = remote.actors
    for actor_key in sorted((set(local_actors) | set(remote_actors) | archived_keys) - refused_actor_keys):
        local_actor = local_actors.get(actor_key)
        remote_actor = remote_actors.get(actor_key)
        local_hash = office_models.office_content_hash(local_actor) if local_actor is not None else None
        remote_hash = office_models.office_content_hash(remote_actor) if remote_actor is not None else None
        key = _actor_key(workspace_id, actor_key)
        decision = classify_three_way_pull(
            local_hash,
            remote_hash,
            baseline.get(key),
            locally_archived=actor_key in archived_keys,
        )
        if decision.action == PullAction.NOOP:
            continue
        if decision.action == PullAction.KEEP_LOCAL:
            summary.kept_local += 1
            continue
        if decision.action == PullAction.WRITE_REMOTE and remote_actor is not None:
            remote_actor.workspace_id = workspace_id
            remote_actor.state = "active"
            # THE adopt arm, through the store's evented verb (H1). It was a raw
            # ``atomic_json_write`` until 2026-08-30, which is why a pull that
            # ARCHIVED a desk was visible to every live consumer and a pull that
            # GAVE you one was visible to none.
            store.adopt_remote_actor(remote_actor)
            baseline[key] = remote_hash or office_models.office_content_hash(remote_actor)
            if decision.reason == "converged":
                summary.converged += 1
            else:
                summary.adopted += 1
            continue
        if decision.action == PullAction.ARCHIVE_LOCAL:
            if deletes_fenced:
                # THE delete-shaped decision, and the one this pull has not
                # earned: "remote_removed" is inferred from the key being absent
                # from a remote map we know to be short. Hold the desk, name it,
                # and leave the baseline alone so a repaired pull can still
                # converge it.
                summary.delete_fenced.append(
                    {
                        "workspace_id": workspace_id,
                        "actor_key": actor_key,
                        "reason": "unreadable_remote",
                        "unreadable_remote": remote.unreadable,
                    }
                )
                continue
            try:
                store.remove_actor(workspace_id, actor_key, reason="remote_removed", updated_by="realm_sync")
            except Exception as exc:  # noqa: BLE001 — accounted, never silent
                # The loop SURVIVES one bad file — a whole realm must not stop
                # converging because one actor would not archive — but the
                # outcome leaves with the summary, and the baseline entry STAYS.
                # Popping it here (which this arm did unconditionally until C2)
                # tells the next pull there is no baseline for a row that is
                # still live, and a row with no baseline reads as a local ADD:
                # the failed delete came back as something to publish. Kept, the
                # next pull re-decides ARCHIVE_LOCAL and retries, which is the
                # repair.
                summary.archive_outcomes.append(
                    OfficeArchiveOutcome.failed(workspace_id, actor_key, exc).as_dict()
                )
                continue
            summary.archived += 1
            summary.archive_outcomes.append(
                OfficeArchiveOutcome.archived(workspace_id, actor_key).as_dict()
            )
            # The baseline entry leaves WITH the row, and only with it.
            baseline.pop(key, None)
            continue
        if decision.action == PullAction.CONFLICT:
            _write_conflict_sidecar(
                workspace_id,
                actor_key,
                kind=decision.reason,
                remote_actor=remote_actor,
                local_hash=local_hash,
                remote_hash=remote_hash,
            )
            summary.conflicts += 1
            continue


def apply_office_pull(realm_id: str, subtree: Path, *, event_log: EventLog | None = None) -> OfficePullSummary:
    """Apply the per-actor decision table across every office surface in the
    pulled realm subtree. Updates the baseline for converged/adopted actors,
    archives remote-removed actors (never deletes), and writes loud conflict
    sidecars. Never a silent overwrite of local changes.
    """

    from .sync_admission import refuse_entity

    store = OfficeStore(event_log=event_log)
    baseline = read_office_baseline(realm_id)
    summary = OfficePullSummary(
        workspaces=[], refused=[], unknowable=[], delete_fenced=[], archive_outcomes=[]
    )
    office_root = subtree / "store" / "office"
    if not office_root.exists():
        return summary

    for office_dir in sorted(p for p in office_root.iterdir() if p.is_dir()):
        remote = _read_remote_office(office_dir)
        remote_surface, remote_actors = remote.surface, remote.actors
        summary.unreadable_remote += remote.unreadable
        if remote_surface is None:
            if remote.surface_unreadable:
                # A directory whose surface will not decode names no workspace,
                # so nothing here can be attributed — but it is a real remote
                # office this pull did not apply, and the count says so.
                summary.unreadable_remote += 1
            continue
        workspace_id = remote_surface.workspace_id
        # The LOCAL half has to be knowable before any decision is taken about
        # it, so ``scan.unreadable`` is READ here and not discarded: an actor
        # whose file will not decode is absent from the rows, reaches the
        # classifier below as ``local_hash=None``, and is then indistinguishable
        # from an actor the member deleted — so the pull would either adopt over
        # it or archive it. Refuse the workspace, keep the count, judge nothing.
        local_scan = store.scan_actors(workspace_id) if store.surface_exists(workspace_id) else ActorScan([], 0)
        unknowable = OfficeSyncRefusal.for_scan(workspace_id, local_scan)
        if unknowable is not None:
            summary.unknowable.append(unknowable.as_dict())
            continue
        # Admission scan (defect (b), 2026-07-25): office files are excluded from
        # the generic pull loop, so ``_assert_no_secret_artifacts`` never saw
        # them. A surface that will not pass the door refuses WHOLE; a single bad
        # actor refuses alone and is EXCLUDED from the reconcile below — treating
        # it as "absent remotely" would archive the member's own placement,
        # turning a hostile payload into a local deletion.
        surface_refusal = refuse_entity(_surface_key(workspace_id), payload=to_jsonable(remote_surface))
        if surface_refusal is not None:
            summary.refused.append(surface_refusal.as_dict())
            continue
        refused_actor_keys: set[str] = set()
        for actor_key in sorted(remote_actors):
            actor_refusal = refuse_entity(
                _actor_key(workspace_id, actor_key), payload=to_jsonable(remote_actors[actor_key])
            )
            if actor_refusal is not None:
                summary.refused.append(actor_refusal.as_dict())
                refused_actor_keys.add(actor_key)
                remote_actors.pop(actor_key, None)
        summary.workspaces.append(workspace_id)

        local_surface = store.get_surface(workspace_id) if store.surface_exists(workspace_id) else None
        local_actors: dict[str, OfficeActor] = {}
        archived_keys: set[str] = set()
        if local_surface is not None:
            archived_keys = set(local_surface.archived_actor_keys)
            # The scan taken above, not a second read: one authority for "what
            # this workspace locally HAS", so the completeness the gate checked
            # and the rows the merge classifies cannot come apart.
            for actor in local_scan.actors:
                local_actors[actor.actor_key] = actor

        _reconcile_surface(
            store, baseline, remote_surface=remote_surface, local_surface=local_surface
        )
        _reconcile_actors(
            store,
            summary,
            baseline,
            workspace_id=workspace_id,
            local_actors=local_actors,
            remote=remote,
            archived_keys=archived_keys,
            refused_actor_keys=refused_actor_keys,
        )

    write_office_baseline(realm_id, baseline)
    return summary
