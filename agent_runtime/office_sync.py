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
from typing import Any

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

    ``OfficeStore._read_actor_dir`` skips a file it cannot decode and returns
    the rest, so every arm reading ``list_actors`` received a SHORTER world that
    described itself as complete. For a reader that is a wrong number; for these
    two arms it is worse, because both of them are writers:

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
        # ``scan_actors``, not ``list_actors``: the thin list view drops the
        # files it could not decode, which is precisely the fact this arm has to
        # know before it writes a completeness claim.
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
        }


def _read_remote_office(office_dir: Path) -> tuple[OfficeSurface | None, dict[str, OfficeActor]]:
    surface_path = office_dir / "office.json"
    surface: OfficeSurface | None = None
    if surface_path.exists():
        try:
            surface = from_jsonable(OfficeSurface, _read_json(surface_path))
        except Exception:
            surface = None
    actors: dict[str, OfficeActor] = {}
    actors_dir = office_dir / "actors"
    if actors_dir.exists():
        for actor_path in sorted(actors_dir.glob("*.json")):
            try:
                actor = from_jsonable(OfficeActor, _read_json(actor_path))
            except Exception:
                continue
            # Payload is truth; the filename is routing only (plan §4.3).
            if actor.actor_key:
                actors[actor.actor_key] = actor
    return surface, actors


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


def apply_office_pull(realm_id: str, subtree: Path, *, event_log: EventLog | None = None) -> OfficePullSummary:
    """Apply the per-actor decision table across every office surface in the
    pulled realm subtree. Updates the baseline for converged/adopted actors,
    archives remote-removed actors (never deletes), and writes loud conflict
    sidecars. Never a silent overwrite of local changes.
    """

    from .sync_admission import refuse_entity

    store = OfficeStore(event_log=event_log)
    baseline = read_office_baseline(realm_id)
    summary = OfficePullSummary(workspaces=[], refused=[], unknowable=[])
    office_root = subtree / "store" / "office"
    if not office_root.exists():
        return summary

    for office_dir in sorted(p for p in office_root.iterdir() if p.is_dir()):
        remote_surface, remote_actors = _read_remote_office(office_dir)
        if remote_surface is None:
            continue
        workspace_id = remote_surface.workspace_id
        # The LOCAL half has to be knowable before any decision is taken about
        # it. ``scan_actors`` rather than ``list_actors``: an actor whose file
        # will not decode is dropped by the thin view, reaches the classifier
        # below as ``local_hash=None``, and is then indistinguishable from an
        # actor the member deleted — so the pull would either adopt over it or
        # archive it. Refuse the workspace, keep the count, judge nothing.
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

        # Surface def: file-granular merge (keep-local-wins on both-changed for
        # v1 — folders are additive taxonomy, a lost folder re-adds trivially).
        surface_key = _surface_key(workspace_id)
        local_surface_hash = office_models.office_content_hash(local_surface) if local_surface is not None else None
        remote_surface_hash = office_models.office_content_hash(remote_surface)
        surface_decision = classify_three_way_pull(local_surface_hash, remote_surface_hash, baseline.get(surface_key))
        if surface_decision.action == PullAction.WRITE_REMOTE or local_surface is None:
            atomic_json_write(
                paths.office_surface_path(workspace_id), to_jsonable(remote_surface), indent=2, sort_keys=True
            )
            baseline[surface_key] = remote_surface_hash

        # Actors: the union of local-active, remote, and locally archived keys,
        # minus anything the door refused (see above).
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
                atomic_json_write(
                    paths.office_actor_path(workspace_id, actor_key),
                    to_jsonable(remote_actor),
                    indent=2,
                    sort_keys=True,
                )
                baseline[key] = remote_hash or office_models.office_content_hash(remote_actor)
                if decision.reason == "converged":
                    summary.converged += 1
                else:
                    summary.adopted += 1
                continue
            if decision.action == PullAction.ARCHIVE_LOCAL:
                try:
                    store.remove_actor(workspace_id, actor_key, reason="remote_removed", updated_by="realm_sync")
                    summary.archived += 1
                except Exception:
                    pass
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

    write_office_baseline(realm_id, baseline)
    return summary
