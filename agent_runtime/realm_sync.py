from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

import yaml

from agent.skill_utils import EXCLUDED_SKILL_DIRS, SKILL_SUPPORT_DIRS
from hermes_constants import get_config_path, get_hermes_home, get_shared_skills_dir
from hermes_time import now
from utils import atomic_json_write

if TYPE_CHECKING:
    from .realm_membership import RealmSyncCredential

from . import paths
from .config import ensure_persisted_personas, load_agent_runtime_config
from .events import EventLog
from .machine_roots import MACHINE_ROOTS_FILENAME
from .models import AgentPersona, Event, Realm, Workspace
from .profile_context import active_profile_name, resolve_persona_profile
from .redaction import SECRET_ASSIGNMENT_RE
from .skill_install import HARNESS_SKILLS, install_harness_skills, install_harness_skills_for_personas
from .store import RealmStore, WorkspaceStore

logger = logging.getLogger(__name__)


SECRET_PATH_MARKERS = {
    ".env",
    "auth.json",
    "credentials",
    "credential",
    "creds",
    "oauth",
    "private_key",
    "secret",
    "secrets",
    "state.db",
    "token",
    "tokens",
}
# The machine seed every free Hermes profile forks from. Its RAW settings never
# travel through realm sync (Office layout realm-sync plan §5.1) — only the
# allowlisted persona definitions bound to it do. Same spelling as
# the historical base persona id; kept as its own constant here because the guard
# is about the profile HOME, which is what the original persona-id guard missed.
BASE_PROFILE_NAME = "base"
HARD_EXCLUDED_PATH_PARTS = {
    "blueprints",
    "blueprint_runs",
    # The machine-root registry binds logical roots to absolute paths on THIS
    # box. It is the half of the portable-config split that must never travel —
    # publishing it would push one member's drive layout onto everyone else.
    MACHINE_ROOTS_FILENAME,
    "proofs",
    "runs",
    "state.db",
    "worker_sessions",
}
# ``SECRET_ASSIGNMENT_RE`` — the publish gate's secret rule — is imported from
# ``agent_runtime.redaction``, the ONE home for every spelling of "secret-ish
# key + separator + value". Read the header there for the JSON blind spot that
# consolidation retired (``{"token": "…"}`` matched none of the twelve
# copies). It stays re-exported from this module under its historical name, so
# ``office_store``/``sync_admission`` keep importing it from here; both may now
# do so eagerly if they wish, since ``redaction`` is stdlib-only and carries
# none of this module's weight.

# Canonical line-ending policy for the realm sync repo. The publisher already
# canonicalizes every artifact to LF (see ``_canonicalize_text_bytes``); this
# repo-root ``.gitattributes`` makes member clones keep LF on checkout no matter
# what their local ``core.autocrlf`` is set to, so nobody re-flips the endings.
# ``text=auto`` leaves binary assets (skill PNG/JPG/… ) untouched. It is never an
# artifact, so it neither enters the artifact manifest nor the secret scanner.
_REALM_SYNC_GITATTRIBUTES = (
    "# Realm sync canonical line endings (managed by agent_runtime/realm_sync.py).\n"
    "# Published artifacts are written LF by the publisher; pin eol=lf so member\n"
    "# clones never re-flip endings on checkout regardless of local core.autocrlf.\n"
    "# text=auto leaves binary assets untouched.\n"
    "* text=auto eol=lf\n"
)
_REALM_SYNC_GITATTRIBUTES_MARKER = b"managed by agent_runtime/realm_sync.py"


class RealmSyncError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, safe_details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.safe_details = safe_details or {}


@dataclass(frozen=True, slots=True)
class RealmSyncArtifact:
    kind: str
    source: Path
    relative_path: str
    destination: Path
    #: Synthesized published bytes. When set, THESE are what publish writes and
    #: ``source`` degrades to provenance (the file the projection was derived
    #: from). Added for the portable persona-config projection: the one artifact
    #: family that must never ship a raw file verbatim still rides the single
    #: publish lane — manifest, secret scan, EOL canonicalization, change
    #: detection — instead of growing a side channel.
    content: bytes | None = None
    #: The persona this artifact was resolved FOR, when it has one. Attribution
    #: only, never authority. ``sync_artifacts_for_workspace_agent`` used to
    #: infer it from a ``/<persona_token>/`` substring of the published path; the
    #: profile-file family publishes at a destination-shaped path
    #: (``store/profile_files/<profile>/…``) where that token no longer appears,
    #: so attribution is carried explicitly instead of guessed.
    persona_id: str | None = None

    def read_bytes(self) -> bytes:
        """The bytes this artifact publishes: synthesized content when present,
        otherwise the source file. Every publish-side reader MUST go through
        here — reading ``source`` directly would publish the raw file a
        synthesized artifact exists precisely to avoid."""

        if self.content is not None:
            return self.content
        return self.source.read_bytes()

    def row(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "path": self.relative_path.replace("\\", "/"),
            "destination": _safe_display_path(self.destination),
        }


@dataclass(frozen=True, slots=True)
class MembershipDecision:
    allowed: bool
    code: str | None = None
    message: str = ""


class RealmMembershipProvider:
    """Backend-authoritative realm sync authorization boundary.

    TODO(Stage 41 production wiring): replace this local allow stub with the
    Eternia backend route that maps server membership and server roles to
    pull/publish permissions, plus git credential brokering.
    """

    def authorize(self, realm: Realm, action: str) -> MembershipDecision:
        if action not in {"pull", "publish", "status"}:
            return MembershipDecision(False, "invalid_request", f"unsupported sync action: {action}")
        return MembershipDecision(True)


def realm_sync_status(
    realm_id: str,
    *,
    membership: RealmMembershipProvider | None = None,
    credential: "RealmSyncCredential | None" = None,
) -> dict[str, Any]:
    realm = RealmStore().get(realm_id)
    _authorize(realm, "status", membership, credential)
    repo = _ensure_sync_repo(realm, credential=credential)
    git = _git_state(repo)
    artifacts = resolve_realm_sync_artifacts(realm_id)
    agent_state = realm_agent_selection_state(realm_id)
    workspaces = _workspaces_for_realm(realm)
    workspace_statuses = _workspace_sync_statuses(realm, repo)
    skills_drift = _held_skill_packages_for_realm(realm)
    state = _sync_state(git)
    # Local store drift vs the never-synced baseline sidecar: the git state above
    # only knows the checked-out realm repo, so a local board card add — which
    # never touches the repo until publish — leaves git ``in_sync`` while real
    # unpublished changes sit in the store. This surfaces that honestly (pure
    # hash/baseline compare — no extra git/network on the status path).
    board_drift = _board_store_drift(realm.id, workspaces)
    store_drift = {"boards": board_drift}
    profile_artifacts_held = _held_profile_artifacts(realm, repo)
    _write_sync_sidecar(
        realm,
        repo=repo,
        git=git,
        skills_drift=skills_drift,
        artifacts=artifacts,
        profile_artifacts_held=profile_artifacts_held,
    )
    return {
        "schema_version": 1,
        "id": realm.id,
        "kind": "realm_sync",
        "state": state,
        "ahead": git["ahead"],
        "behind": git["behind"],
        "skills_drift": skills_drift,
        "skill_publish_mode": realm.skill_publish_mode,
        "skill_selection": sorted(realm.skill_selection or []),
        "skills_published": _distinct_skill_package_count(artifacts),
        "agent_publish_mode": agent_state["mode"],
        "agent_selection": agent_state["selection"],
        "agents_published": len(agent_state["published"]),
        "conflicts": git["conflicts"],
        "last_pull": _timestamp_file(repo, "last_pull.txt"),
        "last_publish": _timestamp_file(repo, "last_publish.txt"),
        "artifacts": len(artifacts),
        "sync_repo": _safe_display_path(repo),
        "workspace_statuses": workspace_statuses,
        # Additive honesty fields (launcher consumes these; absent-tolerant).
        # ``state``/``ahead``/``behind`` are UNCHANGED — other consumers key off
        # them — this only ADDS store-vs-baseline drift accounting on top.
        "store_drift": store_drift,
        "unpublished_changes": _any_store_drift(store_drift),
        # Held profile FILES (MEMORY.md / core context / persona prompts whose
        # member copy diverged from the realm's). A hold the operator cannot see
        # is the same as a loss, so it is surfaced here and resolvable with
        # ``hermes harness realm sync resolve <realm> --key <k> --take …``.
        "profile_artifacts_held": profile_artifacts_held,
    }


def publish_realm_sync(
    realm_id: str,
    *,
    dry_run: bool = False,
    membership: RealmMembershipProvider | None = None,
    credential: "RealmSyncCredential | None" = None,
) -> dict[str, Any]:
    realm = RealmStore().get(realm_id)
    _authorize(realm, "publish", membership, credential)
    repo = _ensure_sync_repo(realm, credential=credential)
    git = _git_state(repo)
    if git["conflicts"]:
        raise RealmSyncError("sync_conflict", "Realm sync repo has unresolved git conflicts.", safe_details={"conflicts": git["conflicts"]})
    if git["behind"] > 0:
        raise RealmSyncError("sync_behind", "Realm sync repo is behind its upstream; pull before publishing.", retryable=True, safe_details={"behind": git["behind"]})
    resolved = _resolve_artifacts_with_projection(realm_id)
    artifacts = resolved.artifacts
    projection = resolved.projection
    profile_files_withheld = resolved.profile_files_withheld
    _assert_no_secret_artifacts(artifacts)
    _assert_no_raw_profile_config(artifacts)
    _assert_portable_artifacts(artifacts)
    if dry_run:
        result = _sync_result(realm, "publish", "dry_run", artifacts, repo=repo, git=git, changed=False)
        result["persona_projection"] = _persona_projection_row(projection, resolved.bound_profiles)
        result["profile_files"] = _profile_files_row(artifacts, profile_files_withheld)
        result["office_sync"] = {"refused": list(resolved.office_refused or [])}
        result["board_sync"] = {"refused": list(resolved.board_refused or [])}
        return result

    subtree = _realm_subtree(repo, realm.id)
    subtree_rel = f"realms/{paths.safe_path_token(realm.id)}"
    # Canonicalize every published artifact to LF at this single write/copy
    # chokepoint (binary assets pass through untouched — see
    # ``_canonicalize_text_bytes``). The store lane writes CRLF on Windows while
    # the pull lane writes LF; copying those raw bytes made every publish a
    # whole-file EOL churn and reported changed=true on no-op runs.
    desired = {
        artifact.relative_path.replace("\\", "/"): _canonicalize_text_bytes(artifact.read_bytes())
        for artifact in artifacts
    }
    # Content-aware change detection: only (re)write the subtree when the
    # canonical artifact bytes actually differ from what is already published.
    # manifest.json is excluded from this comparison because it carries a
    # volatile generated_at — a timestamp-only rewrite is never a real change.
    content_changed = _published_artifacts_differ(subtree, desired)
    # The repo-root .gitattributes is materialized at the ensure chokepoint; make
    # sure a newly-introduced one still rides this publish even when no artifact
    # changed. It is never an artifact, so it skips the manifest + secret scan.
    # Only stage .gitattributes when it actually exists — its write is
    # best-effort, and ``git add`` errors on a pathspec that matches nothing.
    add_paths = [subtree_rel]
    if (repo / ".gitattributes").exists():
        add_paths.append(".gitattributes")
    gitattributes_pending = ".gitattributes" in add_paths and bool(
        _git(repo, "status", "--porcelain", "--", ".gitattributes").strip()
    )
    if content_changed:
        if subtree.exists():
            shutil.rmtree(subtree)
        for artifact in artifacts:
            target = subtree / artifact.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(desired[artifact.relative_path.replace("\\", "/")])
        _write_sync_metadata(subtree, realm=realm, artifacts=artifacts)
    changed = False
    if content_changed or gitattributes_pending:
        _git(repo, "add", "--", *add_paths)
        changed = bool(_git(repo, "status", "--porcelain", "--", *add_paths).strip())
        if changed:
            _ensure_git_identity(repo)
            _git(repo, "commit", "-m", f"Publish realm sync {realm.id}")
            try:
                _git(repo, "push", extra_config=_credential_git_config(credential))
            except RealmSyncError as exc:
                raise RealmSyncError("sync_remote_unreachable", "Realm sync publish committed locally but could not push upstream.", retryable=True, safe_details=exc.safe_details) from exc
    _write_timestamp(repo, "last_publish.txt")
    # Record the published board+card content hashes as the new sync baseline so
    # a subsequent pull sees local == baseline (no spurious conflict on my own
    # publish). Board ids are resolved from the local boards that contributed
    # artifacts (not the tokenized subtree dir names). Baseline is a never-synced
    # sidecar; best-effort so it never fails a publish.
    from .board_sync import update_board_baseline_after_sync

    published_board_ids = sorted({
        artifact.source.parent.parent.name if artifact.kind == "board_card" else artifact.source.parent.name
        for artifact in artifacts
        if artifact.kind in ("board", "board_card")
    })
    if published_board_ids:
        try:
            update_board_baseline_after_sync(realm.id, published_board_ids)
        except Exception:  # noqa: BLE001 — baseline is best-effort; never fail publish
            pass
    # Mission Office: same baseline discipline, per workspace (tokens resolved
    # from the local office dirs that contributed artifacts).
    from .office_sync import update_office_baseline_after_sync

    published_office_workspaces = sorted({
        artifact.source.parent.parent.name if artifact.kind == "office_actor" else artifact.source.parent.name
        for artifact in artifacts
        if artifact.kind in ("office", "office_actor")
    })
    office_baseline: dict[str, Any] = {"recorded": [], "refused": []}
    if published_office_workspaces:
        try:
            office_baseline = update_office_baseline_after_sync(realm.id, published_office_workspaces).as_dict()
        except Exception:  # noqa: BLE001 — baseline is best-effort; never fail publish
            pass
    # Persona definitions: same baseline discipline as boards/office, so my own
    # publish never comes back as a pull conflict.
    from .persona_config_sync import update_persona_config_baseline_after_publish

    try:
        update_persona_config_baseline_after_publish(realm.id, projection)
    except Exception:  # noqa: BLE001 — baseline is best-effort; never fail publish
        pass
    # Profile files (MEMORY.md / core context / persona prompts): same baseline
    # discipline, so my own publish never comes back as a pull hold.
    from .profile_artifact_sync import update_profile_artifact_baseline_after_publish

    try:
        update_profile_artifact_baseline_after_publish(realm.id, _published_profile_file_hashes(artifacts))
    except Exception:  # noqa: BLE001 — baseline is best-effort; never fail publish
        pass
    warnings = _notify_publish(realm, repo=repo, artifacts=artifacts, credential=credential) if changed else []
    git_after = _git_state(repo)
    result = _sync_result(realm, "publish", "published", artifacts, repo=repo, git=git_after, changed=changed)
    result["persona_projection"] = _persona_projection_row(projection, resolved.bound_profiles)
    result["profile_files"] = _profile_files_row(artifacts, profile_files_withheld)
    # Additive publish accounting for the office family: which workspaces did
    # not travel at all and why, and which of the ones that did got their
    # baseline recorded. A workspace that publishes nothing because a file would
    # not decode is a fact the operator must be able to READ — the alternative,
    # a quiet partial publish, is what turns one quarantined file here into desk
    # removals on every peer.
    result["office_sync"] = {
        "refused": list(resolved.office_refused or []),
        "baseline": office_baseline,
    }
    result["board_sync"] = {"refused": list(resolved.board_refused or [])}
    if warnings:
        result["warnings"] = warnings
    _write_sync_sidecar(realm, repo=repo, git=git_after, skills_drift=_held_skill_packages_for_realm(realm), artifacts=artifacts)
    _append_realm_sync_event(
        "realm.sync.published",
        realm,
        changed=changed,
        artifacts=len(artifacts),
    )
    return result


def pull_realm_sync(
    realm_id: str,
    *,
    dry_run: bool = False,
    membership: RealmMembershipProvider | None = None,
    credential: "RealmSyncCredential | None" = None,
) -> dict[str, Any]:
    realm = RealmStore().get(realm_id)
    _authorize(realm, "pull", membership, credential)
    repo = _ensure_sync_repo(realm, credential=credential)
    git = _git_state(repo)
    if git["conflicts"]:
        raise RealmSyncError("sync_conflict", "Realm sync repo has unresolved git conflicts.", safe_details={"conflicts": git["conflicts"]})
    if _has_remote(repo):
        _git(repo, "pull", "--ff-only", extra_config=_credential_git_config(credential))
    subtree = _realm_subtree(repo, realm.id)
    artifacts = _artifacts_from_subtree(subtree)
    _assert_no_secret_artifacts(artifacts)
    if dry_run:
        return _sync_result(realm, "pull", "dry_run", artifacts, repo=repo, git=_git_state(repo), changed=False)
    changed = False
    for artifact in artifacts:
        artifact.destination.parent.mkdir(parents=True, exist_ok=True)
        before = artifact.destination.read_bytes() if artifact.destination.exists() else None
        data = _pulled_artifact_bytes(artifact, realm=realm)
        # Compare canonically so a CRLF local store file vs an LF published
        # artifact is not mistaken for a change (the JSON parses identically);
        # only a real content edit rewrites the destination and flags changed.
        if before is None or _canonicalize_text_bytes(before) != _canonicalize_text_bytes(data):
            artifact.destination.write_bytes(data)
            changed = True
    # Mission Board: board card files are excluded from the generic overwrite
    # loop above (_destination_for_sync_path returns None for store/boards/*);
    # apply the per-card LWW decision table + baseline + conflict sidecars here.
    from .board_sync import apply_board_pull

    board_summary = apply_board_pull(realm.id, subtree)
    if board_summary.adopted or board_summary.converged or board_summary.archived:
        changed = True
    # Mission Office: same exclusion (store/office/*), same shape — the
    # per-actor 3-way baseline merge owns the office pull (plan §5).
    from .office_sync import apply_office_pull

    office_summary = apply_office_pull(realm.id, subtree)
    if office_summary.adopted or office_summary.converged or office_summary.archived:
        changed = True
    # Realm skills: excluded from the generic loop too (skills/* →
    # _destination_for_sync_path None). Mirror them into the resolver-invisible
    # per-realm inbox and admit them to the canonical root only through the one
    # guarded promotion door (auto-adopt new, converge identical, hold divergent).
    skill_summary = apply_skill_inbox_pull(realm, subtree)
    if skill_summary.adopted or skill_summary.removed:
        changed = True
    # Persona definitions: excluded from the generic loop too
    # (profiles/<name>/config.yaml → _destination_for_sync_path None). The member's
    # config is merged key-wise against a never-synced baseline — the realm owns
    # the shared persona surface, the member keeps every machine section they
    # authored, and divergent definitions are HELD, never clobbered.
    from .persona_config_sync import apply_persona_config_pull

    persona_summary = apply_persona_config_pull(realm.id, subtree)
    if persona_summary.changed:
        changed = True
    # Profile FILES (MEMORY.md, core context, persona prompts): excluded from the
    # generic loop too (``profiles/*`` and ``store/profile_files/*`` →
    # ``_destination_for_sync_path`` None). These were the last four kinds still
    # overwritten wholesale — a member's accumulated MEMORY.md included. The lane
    # merges per DESTINATION against a never-synced baseline: adopt when the
    # member has nothing (or an untouched copy), converge when identical, HOLD
    # whenever their content diverged, and never delete.
    from .profile_artifact_sync import apply_profile_artifact_pull

    profile_files_summary = apply_profile_artifact_pull(realm.id, subtree)
    if profile_files_summary.changed:
        changed = True
    # Workspace deletions: honor the pulled realm's deleted_workspace_ids
    # resurrection-guard ledger so a member's surviving local copy neither
    # lingers nor republishes a workspace another member deleted.
    tombstone_summary = _apply_workspace_tombstones(realm.id)
    if tombstone_summary["deleted"] or tombstone_summary["archived"]:
        changed = True
    install_results = [
        *install_harness_skills(skills=sorted(HARNESS_SKILLS)),
        *install_harness_skills_for_personas(ensure_persisted_personas(load_agent_runtime_config())),
    ]
    _write_timestamp(repo, "last_pull.txt")
    git_after = _git_state(repo)
    result = _sync_result(realm, "pull", "pulled", artifacts, repo=repo, git=git_after, changed=changed)
    result["board_sync"] = board_summary.as_dict()
    result["office_sync"] = office_summary.as_dict()
    result["skill_sync"] = skill_summary.as_dict()
    result["profile_artifact_sync"] = profile_files_summary.as_dict()
    if tombstone_summary["deleted"] or tombstone_summary["archived"] or tombstone_summary["warnings"]:
        result["workspace_tombstones"] = tombstone_summary
    # ``profile_sync`` carries the W-H4 rows (which profile homes this pull
    # touched/materialized) PLUS the persona-definition merge accounting PLUS the
    # profile-FILE merge accounting. Emitted whenever any half has something to
    # say — a realm that publishes persona definitions but no per-profile files
    # must still report its merge, and vice versa.
    if profile_files_summary.source is not None or persona_summary.source is not None:
        result["profile_sync"] = {
            "profiles": sorted(set(profile_files_summary.profiles)),
            "created": sorted(set(profile_files_summary.created_profiles)),
            "personas": persona_summary.as_dict(),
            "files": profile_files_summary.as_dict(),
        }
    result["skill_reconcile"] = {
        "installed": [item.skill for item in install_results],
        "changed": [item.skill for item in install_results if item.changed],
        "ok": all(item.ok for item in install_results),
    }
    _write_sync_sidecar(
        realm,
        repo=repo,
        git=git_after,
        skills_drift=skill_summary.held,
        artifacts=artifacts,
        profile_artifacts_held=sorted(set(profile_files_summary.held)),
    )
    _append_realm_sync_event(
        "realm.sync.pulled",
        realm,
        changed=changed,
        artifacts=len(artifacts),
    )
    return result


def _append_realm_sync_event(event_type: str, realm: Realm, *, changed: bool, artifacts: int) -> None:
    """Advance the EventLog watermark after a sync mutation so stream /
    read-model consumers refresh (event-less store/sidecar writes are
    invisible to them). Emitted for pull/publish only — NEVER for
    status, which itself runs off published events and would loop.
    Best effort: a broken event log must not fail the sync verb."""
    try:
        EventLog().append(
            Event(
                now(),
                event_type,
                None,
                None,
                None,
                {
                    "realm_id": realm.id,
                    "changed": changed,
                    "artifacts": artifacts,
                },
            )
        )
    except Exception:  # noqa: BLE001 — evidence channel, not the mutation
        pass


def _apply_workspace_tombstones(realm_id: str) -> dict[str, list]:
    """Apply the realm's ``deleted_workspace_ids`` ledger after a pull.

    A workspace another member deleted must not survive here as a live copy —
    it would ride this member's next publish straight back into the realm.
    Hard-delete the local copy through the store chokepoint (cascade + event);
    a copy that still owns local live-store goals degrades to ARCHIVE instead
    (evidence is never destroyed by a sync), reported as a warning.
    """
    summary: dict[str, list] = {"deleted": [], "archived": [], "warnings": []}
    try:
        realm = RealmStore().get(realm_id)  # re-read: the pull may have rewritten it
    except Exception:  # noqa: BLE001 — no realm, nothing to reconcile
        return summary
    ledger = list(getattr(realm, "deleted_workspace_ids", None) or [])
    if not ledger:
        return summary
    from .errors import WorkspaceDeleteBlocked
    from .store import WorkspaceStore as _WorkspaceStore

    store = _WorkspaceStore()
    for workspace_id in ledger:
        if not paths.workspace_path(workspace_id).exists():
            continue
        try:
            store.delete(workspace_id, reason="realm_sync_tombstone")
            summary["deleted"].append(workspace_id)
        except WorkspaceDeleteBlocked as exc:
            try:
                workspace = store.get(workspace_id)
                if not workspace.archived:
                    store.archive(workspace_id)
                summary["archived"].append(workspace_id)
                summary["warnings"].append(
                    {
                        "code": "workspace_tombstone_archived",
                        "workspace_id": workspace_id,
                        "message": f"Deleted in realm but kept archived here: {exc}",
                    }
                )
            except Exception as inner:  # noqa: BLE001 — accounted, never silent
                summary["warnings"].append(
                    {"code": "workspace_tombstone_failed", "workspace_id": workspace_id, "message": str(inner)}
                )
        except Exception as exc:  # noqa: BLE001 — accounted, never silent
            summary["warnings"].append(
                {"code": "workspace_tombstone_failed", "workspace_id": workspace_id, "message": str(exc)}
            )
    return summary


@dataclass(frozen=True, slots=True)
class _ResolvedPublish:
    """Everything ONE resolution pass of "what does this realm publish" yields.

    The publish lane needs the projection's ACCOUNTING (which keys the allowlist
    dropped, which definitions came purely from a store record, which config keys
    the record shadowed, which wanted personas had no definition at all)
    alongside the artifacts. Resolving them in one pass keeps a single authority
    for "what does this realm publish" — a second independent computation could
    drift from the bytes actually written.

    - ``profile_files_withheld`` — the same discipline for the profile-FILE
      family: a prompt that could not travel is a typed row, never a silent
      omission.
    - ``bound_profiles`` — the profile HOMES this publish resolved persona files
      out of, taken from ``resolve_persona_profile`` (the binding authority
      ``_persona_artifacts`` itself uses). ``profiles_withheld`` used to be
      re-derived by reading ``hermes_profile`` back out of the projected bodies,
      which went blind for the same reason the projection did: a partial body
      reported the wrong profile set and ``base_seed_guarded: false``. A derived
      artifact is not an authority.
    """

    artifacts: list[RealmSyncArtifact]
    projection: Any
    profile_files_withheld: list[dict[str, str]]
    bound_profiles: list[str]
    #: Office workspaces this pass would not publish because their actor
    #: directory did not fully read. Same discipline as ``profile_files_withheld``
    #: one field up: a family that could not travel is a typed row, never a
    #: silent omission — and here silence would have published a partial office
    #: that every peer reads as desk removals.
    office_refused: list[dict[str, Any]] = ()  # type: ignore[assignment]
    #: The board family's twin of the field above, same discipline.
    board_refused: list[dict[str, Any]] = ()  # type: ignore[assignment]


def resolve_realm_sync_artifacts(realm_id: str) -> list[RealmSyncArtifact]:
    return _resolve_artifacts_with_projection(realm_id).artifacts


def _resolve_artifacts_with_projection(realm_id: str) -> _ResolvedPublish:
    realm = RealmStore().get(realm_id)
    workspaces = _workspaces_for_realm(realm)
    cfg = load_agent_runtime_config()
    personas = {persona.id: persona for persona in ensure_persisted_personas(cfg)}
    artifacts: list[RealmSyncArtifact] = []
    artifacts.extend(_skill_artifacts(realm))
    artifacts.extend(_workspace_realm_artifacts(realm, workspaces))
    board_scan = _board_publish_scan(workspaces)
    artifacts.extend(board_scan.artifacts)
    # ONE office pass: the artifacts, the persona ids those placements require,
    # and the workspaces that would not publish at all, resolved together so the
    # three cannot disagree about which offices are in this publish.
    office_scan = _office_publish_scan(workspaces)
    artifacts.extend(office_scan.artifacts)
    # Personas referenced by synced office placements travel with the office
    # (plan §5): an office-only persona must be materializable on pull. The
    # wanted set was workspace.agent_ids only, which would sync a placement
    # referencing a persona the member cannot resolve.
    required_persona_ids = _required_realm_persona_ids(
        workspaces, office_persona_ids=office_scan.persona_ids
    )
    selected_persona_ids = (
        list(realm.agent_selection or [])
        if getattr(realm, "agent_publish_mode", "workspace") == "selected"
        else []
    )
    wanted_persona_ids = list(
        dict.fromkeys([*required_persona_ids, *selected_persona_ids])
    )
    published_persona_ids: list[str] = []
    profile_files_withheld: list[dict[str, str]] = []
    bound_profiles: set[str] = set()
    for persona_id in wanted_persona_ids:
        persona = personas.get(persona_id)
        if persona is None:
            continue
        published_persona_ids.append(persona_id)
        bound_profiles.add(_bound_profile_name(persona))
        persona_artifacts, withheld = _persona_artifacts(persona)
        artifacts.extend(persona_artifacts)
        profile_files_withheld.extend(withheld)
    # ONE synthesized, portable persona-definition document for the whole realm,
    # pruned to exactly the personas above. Replaces the per-profile raw
    # ``config.yaml`` artifact that used to leak the base seed and every
    # machine-shaped MCP/env/path value on it.
    from .persona_config_sync import project_persona_definitions

    projection = project_persona_definitions(
        published_persona_ids,
        raw_config=_raw_active_config(),
        records=personas,
    )
    if projection.personas:
        artifacts.append(_persona_config_artifact(projection))
    return _ResolvedPublish(
        artifacts=_dedupe_artifacts(artifacts),
        projection=projection,
        profile_files_withheld=profile_files_withheld,
        bound_profiles=sorted(bound_profiles),
        office_refused=office_scan.refused,
        board_refused=board_scan.refused,
    )


def _workspaces_for_realm(realm: Realm) -> list[Workspace]:
    workspace_store = WorkspaceStore()
    workspace_ids = set(realm.workspace_ids or [])
    for workspace in workspace_store.list_all(include_archived=True):
        if workspace.realm_id == realm.id:
            workspace_ids.add(workspace.id)
    # Tombstoned workspaces never publish (defense-in-depth: the pull already
    # deletes local copies, but a publish racing ahead of its pull must not
    # resurrect a deleted workspace into the realm subtree).
    workspace_ids -= set(realm.deleted_workspace_ids or [])
    return [
        workspace_store.get(workspace_id)
        for workspace_id in sorted(workspace_ids)
        if paths.workspace_path(workspace_id).exists()
    ]


def _required_realm_persona_ids(
    workspaces: list[Workspace], *, office_persona_ids: list[str] | None = None
) -> list[str]:
    """Persona definitions required by synchronized references.

    These rows are pinned regardless of the explicit Realm selection: a
    pulled workspace roster or Office placement must never reference a persona
    definition the same publish deliberately omitted.

    ``office_persona_ids`` is passed by the publish resolver so this answer and
    the office ARTIFACTS come from the same scan; recomputing it here would let
    a workspace refused for unreadable actors still pin its personas.
    """
    workspace_ids = [
        persona_id
        for workspace in workspaces
        for persona_id in (workspace.agent_ids or [])
    ]
    office_ids = (
        list(office_persona_ids)
        if office_persona_ids is not None
        else _office_wanted_persona_ids(workspaces)
    )
    return list(dict.fromkeys([*workspace_ids, *office_ids]))


def realm_agent_selection_state(realm_id: str) -> dict[str, Any]:
    """Return the local catalog and effective Realm persona selection.

    Pure with respect to Realm selection: unknown ids are preserved and
    reported, while required workspace/Office references remain pinned in the
    effective published set.
    """
    realm = RealmStore().get(realm_id)
    workspaces = _workspaces_for_realm(realm)
    catalog_personas = ensure_persisted_personas(load_agent_runtime_config())
    catalog = sorted({persona.id for persona in catalog_personas})
    required = sorted(set(_required_realm_persona_ids(workspaces)))
    selection = sorted(set(getattr(realm, "agent_selection", None) or []))
    mode = getattr(realm, "agent_publish_mode", "workspace") or "workspace"
    effective = set(required)
    if mode == "selected":
        effective.update(selection)
    published = sorted(effective & set(catalog))
    missing = sorted(effective - set(catalog))
    return {
        "mode": mode,
        "selection": selection,
        "catalog": catalog,
        "required": required,
        "published": published,
        "missing": missing,
    }


def sync_artifacts_for_workspace_agent(workspace_id: str, persona_id: str) -> list[dict[str, str]]:
    workspace = WorkspaceStore().get(workspace_id)
    if not workspace.realm_id:
        return []
    artifacts = resolve_realm_sync_artifacts(workspace.realm_id)
    needle = f"/{paths.safe_path_token(persona_id)}/"
    # Explicit attribution first (the profile-file family publishes at a
    # destination-shaped path where the persona token no longer appears), path
    # substring second (skills and everything else that still encodes it).
    return [
        artifact.row()
        for artifact in artifacts
        if artifact.persona_id == persona_id or needle in f"/{artifact.relative_path}"
    ]


def _skill_artifacts(realm: Realm) -> list[RealmSyncArtifact]:
    # Publish the shared canonical skills root (see get_shared_skills_dir) —
    # the one physical dir every persona references. Walk each skill package
    # WHOLE (not just SKILL.md) so multi-file skills — references/, scripts/,
    # assets/, templates/ — travel intact to every realm member. Sub-path
    # filenames are kept verbatim (rglob cannot emit ``..``) so files like
    # ``__init__.py`` are not mangled. Junk/VCS/cache/dot components are pruned;
    # the shared secret/state validation still runs over the result
    # (_assert_no_secret_artifacts).
    #
    # Package shapes (C5): a top-level dir WITH a SKILL.md publishes as a bare
    # slug; a top-level dir WITHOUT one is a category whose immediate child dirs
    # with a SKILL.md publish as ``<parent>/<child>`` (one level only). A
    # categorized skill such as ``software-development/hermes-agent`` — selected
    # BY PATH by personas — otherwise never reaches a realm.
    #
    # Per-realm selection: mode "all" (default) publishes every catalog package;
    # mode "selected" publishes a package whose slug — or, for a categorized
    # package, the bare child name — is in realm.skill_selection. Because publish
    # rebuilds the realm subtree from scratch, filtering here naturally prunes
    # deselected skills on the next publish. Bare slugs line up with what the
    # Launcher picker offers; categorized selection by bare child name works
    # today (the picker doesn't yet offer categorized slugs — documented
    # follow-up), and the categorized id itself is honored too.
    root = get_shared_skills_dir()
    artifacts: list[RealmSyncArtifact] = []
    if not root.exists():
        return artifacts
    selected_only = realm.skill_publish_mode == "selected"
    selection = set(realm.skill_selection or [])
    for slug, package_dir in _iter_publishable_skill_packages(root):
        if selected_only and not _skill_slug_selected(slug, selection):
            continue
        _append_skill_package_artifacts(artifacts, root, slug, package_dir)
    return artifacts


def _iter_publishable_skill_packages(root: Path):
    """Yield ``(slug, package_dir)`` for every publishable canonical skill package.

    A top-level dir with a ``SKILL.md`` is a bare package (slug = its name). A
    top-level dir WITHOUT a ``SKILL.md`` is a category: each immediate child dir
    with a ``SKILL.md`` publishes as ``<parent>/<child>`` (one level only —
    multi-level nesting is out of scope). Dot-prefixed dirs (the
    resolver-invisible ``.realm_inbox`` / ``.provenance`` / ``.archive`` live
    here), excluded housekeeping dirs, and — under a category — support dirs are
    skipped, so quarantine and provenance are publish-invisible for free.
    """

    for top in sorted(p for p in root.iterdir() if p.is_dir()):
        name = top.name
        if name.startswith(".") or name in EXCLUDED_SKILL_DIRS:
            continue
        if (top / "SKILL.md").is_file():
            yield name, top
            continue
        for child in sorted(p for p in top.iterdir() if p.is_dir()):
            cname = child.name
            if (
                cname.startswith(".")
                or cname in EXCLUDED_SKILL_DIRS
                or cname in SKILL_SUPPORT_DIRS
            ):
                continue
            if (child / "SKILL.md").is_file():
                yield f"{name}/{cname}", child


def _skill_slug_selected(slug: str, selection: set[str]) -> bool:
    """``selected``-mode match: the package slug itself, or — for a categorized
    ``<parent>/<child>`` slug — the bare child name (C5)."""

    if slug in selection:
        return True
    if "/" in slug:
        return slug.split("/", 1)[1] in selection
    return False


def _append_skill_package_artifacts(
    artifacts: list[RealmSyncArtifact], root: Path, slug: str, package_dir: Path
) -> None:
    from agent.skill_utils import resolve_skill

    resolution = resolve_skill(slug)
    selected = resolution.candidate
    if (
        resolution.status != "resolved"
        or selected is None
        or selected.source_kind != "shared_core"
    ):
        raise RealmSyncError(
            "skill_authority_conflict",
            f"Skill cannot publish until shared authority resolves uniquely: {slug}",
            safe_details={
                "skill": slug,
                "resolution_status": resolution.status,
                "candidate_count": len(resolution.candidates),
            },
        )
    skill_dir = selected.skill_dir or selected.skill_md.parent
    safe_parts = [paths.safe_path_token(part) for part in slug.split("/")]
    prefix = "/".join(safe_parts)
    dest_root = root.joinpath(*safe_parts)
    for source in sorted(skill_dir.rglob("*")):
        if not source.is_file():
            continue
        rel_parts = source.relative_to(skill_dir).parts
        if any(
            part.startswith(".") or part in EXCLUDED_SKILL_DIRS for part in rel_parts
        ):
            continue
        rel_within = "/".join(rel_parts)
        artifacts.append(
            RealmSyncArtifact(
                kind="skill",
                source=source,
                relative_path=f"skills/{prefix}/{rel_within}",
                destination=dest_root / Path(*rel_parts),
            )
        )


def _workspace_realm_artifacts(realm: Realm, workspaces: list[Workspace]) -> list[RealmSyncArtifact]:
    artifacts = [
        RealmSyncArtifact(
            kind="realm",
            source=paths.realm_path(realm.id),
            relative_path=f"store/realms/{paths.safe_path_token(realm.id)}.json",
            destination=paths.realm_path(realm.id),
        )
    ]
    for workspace in workspaces:
        artifacts.append(
            RealmSyncArtifact(
                kind="workspace",
                source=paths.workspace_path(workspace.id),
                relative_path=f"store/workspaces/{paths.safe_path_token(workspace.id)}.json",
                destination=paths.workspace_path(workspace.id),
            )
        )
    return [item for item in artifacts if item.source.exists()]


class BoardPublishScan(NamedTuple):
    """What ONE pass over the board store says this realm publishes, and what
    it refused. ``OfficePublishScan``'s twin, for the same defect."""

    artifacts: list[RealmSyncArtifact]
    refused: list[dict[str, Any]]


def _board_publish_scan(workspaces: list[Workspace]) -> BoardPublishScan:
    """Mission Board artifact family: board.json + active card files for boards
    whose workspace belongs to this realm. ``archive/``, ``conflicts/``,
    ``idempotency/`` and the never-synced baseline are all excluded (only
    ``board.json`` and ``cards/`` are walked). Publish replaces the realm subtree
    wholesale (see ``publish_realm_sync``), so card removals/archives propagate
    as absences; pull applies per-card LWW via ``board_sync.apply_board_pull``.

    A board whose card directory does not fully read publishes NOTHING and is
    refused typed instead — the office arm's reasoning, unchanged: publish
    copies card FILES verbatim, absences ARE removals, so a card that merely
    would not decode here becomes a card archived on every peer. Dropping the
    whole board from the subtree is safe where dropping one card is not, because
    a pull only classifies the board directories the subtree contains.
    """

    from .board_store import BoardStore

    workspace_ids = {ws.id for ws in workspaces}
    store = BoardStore()
    artifacts: list[RealmSyncArtifact] = []
    refused: list[dict[str, Any]] = []
    board_scan = store.scan_all()
    if board_scan.unreadable:
        # Cannot be realm-filtered or even named: the board id and its workspace
        # both live inside the file that would not parse. Counted anyway —
        # silence here published a realm that quietly lacked a board.
        refused.append(
            {
                "board_id": None,
                "reason": "board_unreadable",
                "unreadable": board_scan.unreadable,
            }
        )
    for board in board_scan.boards:
        if board.workspace_id not in workspace_ids:
            continue
        card_scan = store.scan_cards(board.board_id)
        if card_scan.unreadable:
            refused.append(
                {
                    "board_id": board.board_id,
                    "reason": "sync_unknowable",
                    "unreadable": card_scan.unreadable,
                }
            )
            continue
        board_token = paths.safe_path_token(board.board_id)
        def_path = paths.board_def_path(board.board_id)
        if def_path.exists():
            artifacts.append(
                RealmSyncArtifact(
                    kind="board",
                    source=def_path,
                    relative_path=f"store/boards/{board_token}/board.json",
                    destination=def_path,
                )
            )
        cards_dir = paths.board_cards_dir(board.board_id)
        if cards_dir.exists():
            for card_path in sorted(cards_dir.glob("*.json")):
                artifacts.append(
                    RealmSyncArtifact(
                        kind="board_card",
                        source=card_path,
                        relative_path=f"store/boards/{board_token}/cards/{card_path.name}",
                        destination=card_path,
                    )
                )
    return BoardPublishScan(artifacts=artifacts, refused=refused)


def _board_store_drift(realm_id: str, workspaces: list[Workspace]) -> dict[str, int]:
    """Board content drift vs the never-synced baseline sidecar, for boards whose
    ``workspace_id`` belongs to this realm's workspaces.

    Compares CURRENT ``BoardStore`` semantic content hashes (``board_content_hash``
    — revision/timestamps excluded) against ``read_board_baseline(realm_id)``: the
    exact hash/baseline machinery the publish and pull lanes already use. A
    missing/empty baseline on a server-bound realm means nothing has been
    published yet, so every board+card counts as unpublished — that is honest.

    Pure and read-only: no git, no network, no new merge rules (office plan §10
    simplicity budget). ``boards_changed`` counts board defs whose hash drifted;
    ``cards_changed`` counts active cards whose hash drifted from a known
    baseline; ``cards_added`` counts active cards with no baseline entry; and
    ``cards_removed`` counts baseline cards no longer active locally (archived /
    deleted since the last publish).
    """

    from . import board_models
    from .board_store import BoardStore
    from .board_sync import read_board_baseline

    workspace_ids = {ws.id for ws in workspaces}
    baseline = read_board_baseline(realm_id)
    store = BoardStore()
    boards_changed = cards_changed = cards_added = cards_removed = 0
    for board in store.list_all():
        if board.workspace_id not in workspace_ids:
            continue
        if baseline.get(f"{board.board_id}:board") != board_models.board_content_hash(board):
            boards_changed += 1
        card_prefix = f"{board.board_id}:card:"
        baseline_card_ids = {key[len(card_prefix):] for key in baseline if key.startswith(card_prefix)}
        current_card_ids: set[str] = set()
        for card in store.list_cards(board.board_id):
            current_card_ids.add(card.card_id)
            base_hash = baseline.get(f"{card_prefix}{card.card_id}")
            if base_hash is None:
                cards_added += 1
            elif base_hash != board_models.board_content_hash(card):
                cards_changed += 1
        cards_removed += len(baseline_card_ids - current_card_ids)
    return {
        "boards_changed": boards_changed,
        "cards_changed": cards_changed,
        "cards_added": cards_added,
        "cards_removed": cards_removed,
    }


def _any_store_drift(store_drift: dict[str, dict[str, int]]) -> bool:
    """True iff any drift family reports a nonzero count (drives the additive
    ``unpublished_changes`` status flag)."""

    return any(count for family in store_drift.values() for count in family.values())


class OfficePublishScan(NamedTuple):
    """What ONE pass over the office store says this realm publishes.

    Three facts, resolved together on purpose. The artifacts and the persona ids
    the placements REQUIRE used to be two independent walks of the same
    directories, and a workspace excluded from one but not the other publishes a
    placement whose persona definition never travelled. ``refused`` is the third
    because it is the reason the other two are short — a shortened answer that
    does not carry its own shortfall is the defect this stage retires.
    """

    artifacts: list[RealmSyncArtifact]
    persona_ids: list[str]
    refused: list[dict[str, Any]]


def _office_publish_scan(workspaces: list[Workspace]) -> OfficePublishScan:
    """Mission Office artifact family: office.json + active actor files for
    surfaces whose workspace belongs to this realm. ``archive/``,
    ``conflicts/`` and the never-synced baseline are all excluded (only
    ``office.json`` and ``actors/`` are walked). Publish replaces the realm
    subtree wholesale (see ``publish_realm_sync``), so actor removals/archives
    propagate as absences; pull applies the per-actor 3-way baseline merge via
    ``office_sync.apply_office_pull``.

    A workspace whose actor directory does not fully read publishes NOTHING and
    is refused typed instead. That is the whole point of the scan: publish
    copies actor FILES verbatim, so the undecodable one travels, and "removals
    propagate as absences" then turns every peer's pull into a desk removal for
    an actor whose file merely would not open here. Dropping the workspace from
    the subtree is safe where dropping one actor is not — a pull only classifies
    the office directories the subtree actually contains.
    """

    from .office_store import OfficeStore
    from .office_sync import OfficeSyncRefusal

    workspace_ids = {ws.id for ws in workspaces}
    store = OfficeStore()
    artifacts: list[RealmSyncArtifact] = []
    persona_ids: list[str] = []
    refused: list[dict[str, Any]] = []
    for workspace_token in store.list_workspaces():
        try:
            surface = store.get_surface(workspace_token)
        except Exception as exc:  # noqa: BLE001 — accounted, never silent
            # Cannot be realm-filtered: the workspace id lives INSIDE the file
            # that would not parse, so this row names the store token and is
            # reported to whoever publishes. Silence here published an empty
            # office for a surface that exists.
            refused.append(
                {
                    "workspace_id": str(workspace_token),
                    "reason": "surface_unreadable",
                    "error": type(exc).__name__,
                }
            )
            continue
        if surface.workspace_id not in workspace_ids:
            continue
        # ``scan_actors``, not ``list_actors``: the thin view answers "these are
        # the actors" for a directory it only partly read.
        scan = store.scan_actors(workspace_token)
        refusal = OfficeSyncRefusal.for_scan(surface.workspace_id, scan)
        if refusal is not None:
            refused.append(refusal.as_dict())
            continue
        ws_token = paths.safe_path_token(workspace_token)
        surface_path = paths.office_surface_path(workspace_token)
        if surface_path.exists():
            artifacts.append(
                RealmSyncArtifact(
                    kind="office",
                    source=surface_path,
                    relative_path=f"store/office/{ws_token}/office.json",
                    destination=surface_path,
                )
            )
        actors_dir = paths.office_actors_dir(workspace_token)
        if actors_dir.exists():
            for actor_path in sorted(actors_dir.glob("*.json")):
                artifacts.append(
                    RealmSyncArtifact(
                        kind="office_actor",
                        source=actor_path,
                        relative_path=f"store/office/{ws_token}/actors/{actor_path.name}",
                        destination=actor_path,
                    )
                )
        for actor in scan.actors:
            if actor.persona_id and actor.persona_id not in persona_ids:
                persona_ids.append(actor.persona_id)
    return OfficePublishScan(artifacts=artifacts, persona_ids=persona_ids, refused=refused)


def _office_wanted_persona_ids(workspaces: list[Workspace]) -> list[str]:
    """Persona ids referenced by office placements in this realm's workspaces
    (plan §5's one-line union — office-only personas travel with the office).

    Thin view over :func:`_office_publish_scan`, so a workspace whose office is
    refused never contributes a persona id: the placement that would have needed
    it is not travelling either."""

    return _office_publish_scan(workspaces).persona_ids


def _published_profile_file_hashes(artifacts: list[RealmSyncArtifact]) -> dict[str, str]:
    """``{entity key: content hash}`` for the profile FILES this publish wrote.

    Feeds the publish-side baseline update so a member who publishes then pulls
    sees local == baseline (no self-inflicted hold)."""

    from .profile_artifact_sync import (
        PROFILE_FILES_ROOT,
        classify_destination,
        content_hash,
        entity_key,
    )

    prefix = f"{PROFILE_FILES_ROOT}/"
    hashes: dict[str, str] = {}
    for artifact in artifacts:
        rel = artifact.relative_path.replace("\\", "/")
        if not rel.startswith(prefix):
            continue
        tail = rel[len(prefix):].split("/", 1)
        if len(tail) != 2 or classify_destination(tail[1]) is None:
            continue
        try:
            hashes[entity_key(tail[0], tail[1])] = content_hash(artifact.read_bytes())
        except OSError:
            continue
    return hashes


def _profile_files_row(
    artifacts: list[RealmSyncArtifact], withheld: list[dict[str, str]]
) -> dict[str, Any]:
    """Typed publish accounting for the profile-FILE family.

    ``published`` names every destination that travels (keyed exactly as the pull
    side reconciles it, so a publish row and a pull hold are the same string);
    ``withheld`` names every file that deliberately did not — today only
    repository-bundled prompts, which already ship with every member's hermes.
    """

    from .profile_artifact_sync import PROFILE_FILES_ROOT

    prefix = f"{PROFILE_FILES_ROOT}/"
    published = sorted(
        {
            artifact.relative_path.replace("\\", "/")[len(prefix):].replace("/", ":", 1)
            for artifact in artifacts
            if artifact.relative_path.replace("\\", "/").startswith(prefix)
        }
    )
    return {"published": published, "withheld": list(withheld)}


def _persona_artifacts(persona: AgentPersona) -> tuple[list[RealmSyncArtifact], list[dict[str, str]]]:
    """Per-profile FILES a persona carries: prompts, soul overlay, memory, core
    context. Returns ``(artifacts, withheld rows)``.

    The bound profile home's RAW ``config.yaml`` is deliberately NOT here any
    more. It used to publish as ``profiles/<profile>/config.yaml`` and overwrite
    the member's file wholesale on pull, which (a) clobbered the base fork seed
    whenever a persona bound to ``hermes_profile: base`` (Office plan §5.1 ruled
    that must never happen) and (b) shipped machine-shaped ``mcp_servers``
    commands/env and absolute Windows paths that resolve to nothing on any other
    machine. The persona DEFINITIONS that were the only shareable part of that
    file now travel as one synthesized, allowlisted projection
    (``persona_config_sync.project_persona_definitions``).

    The published PATH changed on 2026-07-25 (see
    ``profile_artifact_sync``): these files now publish at
    ``store/profile_files/<profile>/<profile-relative destination>``, so the
    published tail IS the destination. That (a) makes a prompt round-trip to the
    exact path the persona definition names — the basename-keyed destination did
    not, and left an orphan — and (b) is a path an older hermes does not map, so
    an old member degrades to "no profile files" instead of having their
    accumulated ``MEMORY.md`` overwritten wholesale.

    A prompt that resolves OUTSIDE the bound profile home (a repository-bundled
    role prompt) is deliberately withheld and accounted: it already ships with
    every member's hermes, and publishing it would write a file into the member's
    profile home that no persona definition addresses.
    """

    from .profile_artifact_sync import (
        CORE_CONTEXT_FILENAMES,
        MEMORY_DESTINATION,
        classify_destination,
        published_relative_path,
    )
    from .prompt_sources import resolve_persona_system_prompt_path

    binding = resolve_persona_profile(persona)
    profile_home = binding.profile_home or get_hermes_home()
    profile = paths.safe_path_token(binding.hermes_profile or active_profile_name() or "default")
    artifacts: list[RealmSyncArtifact] = []
    withheld: list[dict[str, str]] = []

    def _add(kind: str, source: Path, dest_rel: str) -> None:
        # Publish through the SAME admissibility authority the pull side applies.
        # Without it the two sides can disagree and a file publishes into a realm
        # that every member then refuses — a silent one-way loss. That shipped for
        # a profile-root ``soul.md`` and was caught by the rebind-delta suite on
        # 2026-07-25; this call is why it cannot recur.
        if classify_destination(dest_rel) is None:
            withheld.append(
                {
                    "persona_id": persona.id,
                    "kind": kind,
                    "reason": "destination_not_publishable",
                    "message": f"{dest_rel} is not an admissible profile-file destination; members would refuse it",
                }
            )
            return
        artifacts.append(
            RealmSyncArtifact(
                kind=kind,
                source=source,
                relative_path=published_relative_path(profile, dest_rel),
                destination=source,
                persona_id=persona.id,
            )
        )

    for label, raw in (("system_prompt", persona.system_prompt_path), ("soul_overlay", persona.soul_overlay_path)):
        path = (
            resolve_persona_system_prompt_path(persona)
            if label == "system_prompt"
            else _profile_relative_file(profile_home, raw)
        )
        if path is None or not path.exists():
            continue
        dest_rel = _profile_relative_destination(profile_home, path)
        if dest_rel is None:
            withheld.append(
                {
                    "persona_id": persona.id,
                    "kind": label,
                    "reason": "not_profile_owned",
                    "message": "prompt resolves outside the bound profile home (repository-bundled); it ships with hermes and is not republished",
                }
            )
            continue
        _add(label, path, dest_rel)
    if persona.include_profile_memory:
        memory = profile_home / "memories" / "MEMORY.md"
        if memory.exists():
            _add("profile_memory", memory, MEMORY_DESTINATION)
    if persona.include_core_context_files:
        for name in CORE_CONTEXT_FILENAMES:
            context = profile_home / name
            if context.exists():
                _add("core_context", context, name)
    return artifacts, withheld


def _profile_relative_destination(profile_home: Path, path: Path) -> str | None:
    """``path`` expressed relative to ``profile_home``, or ``None`` when it lives
    outside it. The returned POSIX string is BOTH the published tail and the
    member's destination, which is what makes the round trip exact."""

    try:
        rel = path.resolve().relative_to(profile_home.resolve())
    except (OSError, ValueError):
        return None
    text = rel.as_posix()
    return text or None


def _profile_relative_file(profile_home: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute() or ".." in path.parts:
        return None
    return profile_home / path


def _raw_active_config() -> dict[str, Any]:
    """The parsed ``config.yaml`` persona definitions are authored in.

    ``agent_runtime.personas.<id>`` is read by ``load_agent_runtime_config()``
    from ``get_config_path()`` — the ACTIVE profile's config — not from each
    persona's bound profile home. The projection is sourced from the same file
    the runtime resolves definitions out of, so publish and pull are symmetric.
    """

    from .persona_config_sync import load_raw_config

    return load_raw_config()


def _persona_config_artifact(projection) -> RealmSyncArtifact:
    """The single synthesized ``persona_config`` artifact.

    Published at ``store/personas.yaml``, NOT ``profiles/<name>/config.yaml``:
    an older hermes maps the latter onto ``<profile_home>/config.yaml`` and would
    overwrite a member's real config with this personas-only document. The new
    path is unknown to every older client (``_destination_for_sync_path`` →
    ``None`` → skipped), so an old member degrades to "no persona definitions"
    instead of losing their configuration.
    """

    from .persona_config_sync import PROJECTION_RELATIVE_PATH

    config = get_config_path()
    return RealmSyncArtifact(
        kind="persona_config",
        source=config,
        relative_path=PROJECTION_RELATIVE_PATH,
        destination=config,
        content=projection.to_bytes(),
    )


def _persona_projection_row(projection, bound_profiles: list[str]) -> dict[str, Any]:
    """Typed publish accounting for the projection — including the explicit
    base-seed guard (Office plan §5.1).

    ``profiles_withheld`` names every profile home whose RAW settings this
    publish deliberately did not ship; ``base_seed_guarded`` is the §5.1 answer
    specifically. The guard is reported even though (and precisely because) the
    projection makes the clobber structurally impossible — a silent skip is not
    accounting.

    ``bound_profiles`` arrives from the resolution pass, which reads it off the
    binding authority. It used to be recovered by reading ``hermes_profile`` back
    out of the projected bodies — so when the projection published partial
    bodies (2026-07-25) this row reported ``profiles_withheld: ["default"]`` and
    ``base_seed_guarded: false``, a false all-clear on the §5.1 guard produced by
    the very defect it was meant to watch. Accounting derived from the artifact
    it is accounting for cannot detect that the artifact is wrong.
    """

    return {
        **projection.as_dict(),
        "profiles_withheld": list(bound_profiles),
        "base_seed_guarded": BASE_PROFILE_NAME in bound_profiles,
    }


def _bound_profile_name(persona: AgentPersona) -> str:
    """The profile HOME this persona's files publish out of.

    Same authority and same fallback chain ``_persona_artifacts`` uses
    (``resolve_persona_profile`` → declared binding, else the active profile), so
    "profiles whose raw config.yaml was withheld" is exactly the set of profile
    homes this publish actually read from. Un-tokenized on purpose: this names a
    profile, not a published path segment.
    """

    binding = resolve_persona_profile(persona)
    return str(binding.hermes_profile or active_profile_name() or "default")


def _assert_no_raw_profile_config(artifacts: list[RealmSyncArtifact]) -> None:
    """Structural base-seed guard (Office plan §5.1), enforced for EVERY profile.

    §5.1 ruled the base profile — the machine seed every free profile forks from
    — must never travel: overwriting a member's fork seed changes every agent
    they create afterwards, including outside this realm. The original guard was
    written against the base PERSONA id, so a
    persona merely *bound to* ``hermes_profile: base`` walked past it and
    published ``profiles/base/config.yaml`` anyway.

    The rule is now structural and profile-agnostic: no raw profile
    ``config.yaml`` may ever be an artifact. Only allowlisted persona definitions
    leave, via the synthesized projection.
    """

    offenders = sorted(
        artifact.relative_path.replace("\\", "/")
        for artifact in artifacts
        if _is_raw_profile_config_path(artifact.relative_path.replace("\\", "/"))
    )
    if offenders:
        raise RealmSyncError(
            "sync_profile_config_excluded",
            "Realm sync refused to publish a raw profile config.yaml; only the "
            "portable persona-definition projection may travel.",
            safe_details={"paths": offenders, "base_profile": BASE_PROFILE_NAME},
        )


def _is_raw_profile_config_path(rel: str) -> bool:
    parts = Path(rel).parts
    return len(parts) == 3 and parts[0] == "profiles" and parts[2] == "config.yaml"


def _assert_portable_artifacts(artifacts: list[RealmSyncArtifact]) -> None:
    """Refuse to publish machine/installation-shaped values in CONFIGURATION.

    Scope is deliberate. This runs over the STRUCTURED persona-definition
    projection — parsed, key by key — not over free text. A skill's SKILL.md, a
    profile ``MEMORY.md``, or an ``AGENTS.md`` legitimately mentions absolute
    paths as prose; refusing those would brick every publish on this machine
    without protecting anything, and "a refusal that bricks a publish over a
    false positive" is a failure class this file has already paid for. What
    matters is that no absolute path ends up as live WIRING on another member's
    machine — and wiring only ever comes from the projection.

    Refuse rather than warn: the projection is synthesized under our own
    allowlist, so a machine-shaped value in it is a genuine authoring defect, and
    shipping it silently is exactly the bug being retired. ALL offenders are
    named in one typed error so an operator fixes them in a single pass.
    """

    from .persona_config_sync import NONPORTABLE_HINT, find_nonportable_values, raw_persona_overrides

    offenders: list[dict[str, str]] = []
    for artifact in artifacts:
        if artifact.kind != "persona_config":
            continue
        try:
            data = yaml.safe_load(artifact.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        personas = data.get("personas") if isinstance(data, dict) else None
        if not isinstance(personas, dict):
            personas = raw_persona_overrides(data)
        for row in find_nonportable_values(personas, prefix="personas"):
            offenders.append({**row, "value": _redact_text(row["value"])})
    if offenders:
        raise RealmSyncError(
            "sync_nonportable_path",
            "Realm sync refused to publish machine-shaped values that cannot "
            "resolve on another member's machine.",
            safe_details={"offenders": offenders, "hint": NONPORTABLE_HINT},
        )


def _artifacts_from_subtree(subtree: Path) -> list[RealmSyncArtifact]:
    if not subtree.exists():
        return []
    artifacts: list[RealmSyncArtifact] = []
    for source in sorted(path for path in subtree.rglob("*") if path.is_file() and path.name != "manifest.json"):
        rel = source.relative_to(subtree).as_posix()
        destination = _destination_for_sync_path(rel)
        if destination is None:
            continue
        artifacts.append(RealmSyncArtifact(kind=_kind_for_sync_path(rel), source=source, relative_path=rel, destination=destination))
    return artifacts


def _pulled_artifact_bytes(artifact: RealmSyncArtifact, *, realm: Realm) -> bytes:
    """Preserve backend-owned realm identity during a Git pull.

    The realm JSON is shared so workspace membership can travel through Git,
    but a server-bound realm's identity/default pointer is authoritative from
    backend adoption. An older repo snapshot must never roll that pointer back.
    """
    data = artifact.source.read_bytes()
    if artifact.kind != "realm" or not realm.server_id or not artifact.destination.exists():
        return data
    try:
        incoming = json.loads(data.decode("utf-8"))
        current = json.loads(artifact.destination.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return data
    if not isinstance(incoming, dict) or not isinstance(current, dict):
        return data
    authority_fields = {
        "id",
        "name",
        "slug",
        "server_id",
        "default_workspace_id",
        "default_workspace_name",
        "default_workspace_version",
        "sync_manifest_ref",
    }
    for field in authority_fields:
        if field in current:
            incoming[field] = current[field]
    return json.dumps(incoming, indent=2, sort_keys=True, default=str).encode("utf-8")


def _destination_for_sync_path(rel: str) -> Path | None:
    parts = Path(rel).parts
    if parts and parts[0] == "skills":
        # Skills no longer overwrite the canonical shared root through the generic
        # pull loop. ``apply_skill_inbox_pull`` mirrors them into the
        # resolver-invisible per-realm inbox and admits them to the canonical root
        # only through the one guarded promotion door (C3) — same board/office
        # exclusion precedent (store/boards/*, store/office/* → None). Returning
        # None here keeps a realm pull from silently clobbering a local canonical
        # skill of the same id.
        return None
    if len(parts) == 3 and parts[0] == "store" and parts[1] == "workspaces":
        return paths.workspaces_dir() / parts[2]
    if len(parts) == 3 and parts[0] == "store" and parts[1] == "realms":
        return paths.realms_dir() / parts[2]
    # store/boards/* and store/office/* deliberately fall through to None: the
    # generic overwrite loop never touches them — board_sync.apply_board_pull /
    # office_sync.apply_office_pull own those pulls (3-way baseline merge).
    if parts and parts[0] == "store" and len(parts) == 2 and parts[1] == "personas.yaml":
        # The portable persona-definition projection. Owned by
        # ``persona_config_sync.apply_persona_config_pull`` (key-wise merge
        # against a never-synced baseline), never the generic overwrite loop —
        # same exclusion precedent as store/boards/*, store/office/*, skills/*.
        return None
    if len(parts) > 2 and parts[0] == "store" and parts[1] == "profile_files":
        # The per-profile FILE family (MEMORY.md, core context, persona prompts).
        # Owned by ``profile_artifact_sync.apply_profile_artifact_pull``.
        return None
    if parts and parts[0] == "profiles" and len(parts) > 1:
        # EVERY legacy ``profiles/…`` artifact is now owned by an applier, none
        # by the generic overwrite loop:
        #   - ``config.yaml``            → persona_config_sync (allowlisted merge)
        #   - memories / context / prompts → profile_artifact_sync (baseline merge)
        # Before 2026-07-25 the last four were written wholesale here, which
        # DESTROYED a member's accumulated ``MEMORY.md`` on every pull and keyed
        # prompt destinations by filename only (two personas on one profile
        # clobbered each other — Office plan §5.1). Same exclusion precedent as
        # store/boards/*, store/office/*, skills/*, store/personas.yaml.
        return None
    return None


def _profile_home_for_token(token: str) -> Path | None:
    """Profile-aware pull destination (W-H4, plan §5.1).

    Before 2026-07-17 this mapping collapsed EVERY ``profiles/<name>/…``
    artifact into the active profile home (a degenerate ternary — both
    branches returned ``get_hermes_home()``), so a multi-profile realm pull
    last-write-wins'd every profile's config.yaml/MEMORY.md onto one home.
    Now: the active profile keeps the active home; any other published profile
    resolves to ITS OWN home via ``get_profile_dir`` (materialized by the pull
    write-loop's mkdir and reported as a typed ``profile_sync`` row). Untrusted
    remote component: refuse traversal/absolute/drive-letter shapes.
    """

    if token in ("", ".", "..") or ":" in token or token.startswith(("/", "\\")):
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", token):
        return None
    if token == paths.safe_path_token(active_profile_name()):
        return get_hermes_home()
    try:
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name

        return get_profile_dir(normalize_profile_name(token))
    except Exception:
        return None


def _kind_for_sync_path(rel: str) -> str:
    if rel.startswith("skills/"):
        return "skill"
    if rel.startswith("store/workspaces/"):
        return "workspace"
    if rel.startswith("store/realms/"):
        return "realm"
    if rel.startswith("store/office/"):
        return "office_actor" if "/actors/" in rel else "office"
    if rel == "store/personas.yaml":
        return "persona_config"
    if rel.startswith("store/profile_files/"):
        # Kind is derived from the DESTINATION the published tail names — the
        # same authority the pull applier uses, never a second spelling.
        from .profile_artifact_sync import classify_destination

        tail = rel.split("/", 3)
        return (classify_destination(tail[3]) if len(tail) > 3 else None) or "artifact"
    if rel.endswith("config.yaml"):
        return "persona_config"
    if "/memories/" in rel:
        return "profile_memory"
    if "/context/" in rel:
        return "core_context"
    if "/system_prompt/" in rel:
        return "system_prompt"
    if "/soul_overlay/" in rel:
        return "soul_overlay"
    return "artifact"


def _assert_no_secret_artifacts(artifacts: list[RealmSyncArtifact]) -> None:
    blocked: list[str] = []
    for artifact in artifacts:
        rel = artifact.relative_path.replace("\\", "/")
        if _is_secretish_path(rel) or _is_hard_excluded_path(rel) or _artifact_contains_secret_assignment(artifact):
            blocked.append(rel)
    if blocked:
        raise RealmSyncError("sync_secret_excluded", "Realm sync refused to include excluded secret/state artifacts.", safe_details={"paths": blocked[:20]})


def _is_secretish_path(rel: str) -> bool:
    parts = {part.lower() for part in Path(rel).parts}
    return bool(parts & SECRET_PATH_MARKERS)


def _is_hard_excluded_path(rel: str) -> bool:
    parts = {part.lower() for part in Path(rel).parts}
    return bool(parts & HARD_EXCLUDED_PATH_PARTS)


def _artifact_contains_secret_assignment(artifact: RealmSyncArtifact) -> bool:
    """Scan what actually publishes.

    A synthesized artifact must be scanned by its CONTENT — scanning its
    provenance ``source`` would test a file whose secrets the projection already
    filtered out, and (worse) could refuse a publish over a secret that never
    leaves the machine. The secret-exclusion path itself is unchanged.
    """

    if artifact.content is not None:
        return bool(SECRET_ASSIGNMENT_RE.search(artifact.content.decode("utf-8", errors="ignore")))
    return _file_contains_secret_assignment(artifact.source)


def _file_contains_secret_assignment(path: Path) -> bool:
    try:
        if path.stat().st_size > 1_000_000:
            return False
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return bool(SECRET_ASSIGNMENT_RE.search(text))


def _authorize(realm: Realm, action: str, membership: RealmMembershipProvider | None, credential: "RealmSyncCredential | None" = None) -> None:
    if membership is None:
        if not _looks_like_remote(str(realm.sync_manifest_ref or "")):
            membership = RealmMembershipProvider()
        else:
            # Stage 43 fail-closed selection: remote server-bound realms require the
            # backend-authoritative provider (credential-backed); server-less and
            # local-path realms keep the local allow stub unchanged.
            from .realm_membership import select_membership_provider

            membership = select_membership_provider(realm, credential)
    decision = membership.authorize(realm, action)
    if not decision.allowed:
        raise RealmSyncError(decision.code or "membership_denied", decision.message or "Realm membership does not allow this sync action.")


def _ensure_sync_repo(realm: Realm, *, credential: "RealmSyncCredential | None" = None) -> Path:
    repo = _sync_repo_path(realm)
    if repo.exists() and (repo / ".git").exists():
        _ensure_repo_gitattributes(repo)
        return repo
    if _looks_like_remote(str(realm.sync_manifest_ref or "")):
        repo.parent.mkdir(parents=True, exist_ok=True)
        _git_clone(str(realm.sync_manifest_ref), repo, extra_config=_credential_git_config(credential))
    else:
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init")
    _ensure_repo_gitattributes(repo)
    return repo


def _ensure_repo_gitattributes(repo: Path) -> None:
    """Materialize the LF line-ending pin at the realm sync repo root.

    Idempotent: rewrites only the file we manage (identified by our marker) and
    respects any foreign ``.gitattributes`` a repo already carries. It is written
    here but committed by ``publish_realm_sync`` (it rides the same publish lane
    as the realm subtree). Best-effort — a write failure never fails the sync
    verb (the publisher still canonicalizes bytes to LF regardless)."""
    path = repo / ".gitattributes"
    desired = _REALM_SYNC_GITATTRIBUTES.encode("utf-8")
    try:
        if path.exists():
            existing = path.read_bytes()
            if existing == desired:
                return
            if _REALM_SYNC_GITATTRIBUTES_MARKER not in existing:
                return  # respect a foreign .gitattributes; only manage our own
        path.write_bytes(desired)
    except OSError:
        return


def _credential_git_config(credential: "RealmSyncCredential | None") -> list[str] | None:
    """Per-invocation git auth config (Decision 4): rendered as ``git -c`` args,
    never written to ``.git/config`` and never surfaced in safe_details/logs."""
    if credential is None:
        return None
    return credential.git_extra_config()


def _sync_repo_path(realm: Realm) -> Path:
    ref = str(realm.sync_manifest_ref or "").strip()
    if ref and not _looks_like_remote(ref):
        return Path(ref).expanduser()
    key = paths.safe_path_token(realm.server_id or "local")
    return paths.store_root() / "realm_sync" / key


def _realm_subtree(repo: Path, realm_id: str) -> Path:
    return repo / "realms" / paths.safe_path_token(realm_id)


def _git_state(repo: Path) -> dict[str, Any]:
    conflicts = [line.strip() for line in _git(repo, "diff", "--name-only", "--diff-filter=U", check=False).splitlines() if line.strip()]
    ahead = behind = 0
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False).strip()
    if upstream:
        counts = _git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}", check=False).split()
        if len(counts) == 2:
            ahead, behind = int(counts[0]), int(counts[1])
    dirty = bool(_git(repo, "status", "--porcelain", check=False).strip())
    return {"ahead": ahead, "behind": behind, "conflicts": conflicts, "dirty": dirty}


def _sync_state(git: dict[str, Any]) -> str:
    if git["conflicts"]:
        return "conflict"
    if git["behind"]:
        return "behind"
    if git["ahead"] or git["dirty"]:
        return "ahead"
    return "in_sync"


def _has_remote(repo: Path) -> bool:
    return bool(_git(repo, "remote", check=False).strip())


def _git(repo: Path, *args: str, check: bool = True, extra_config: Sequence[str] | None = None) -> str:
    proc = subprocess.run(["git", *_render_git_config(extra_config), "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        code = "sync_auth_failed" if "authentication" in (proc.stderr or "").lower() else "sync_remote_unreachable"
        # safe_details carries the plain subcommand args only — the -c config
        # pairs (which can hold an Authorization header) are never included,
        # and their values are scrubbed from stderr before redaction.
        raise RealmSyncError(
            code,
            "git command failed for realm sync.",
            retryable=True,
            safe_details={"git_args": list(args), "stderr": _redact_text(_scrub_config_values(proc.stderr, extra_config))},
        )
    return proc.stdout


def _git_clone(ref: str, repo: Path, *, extra_config: Sequence[str] | None = None) -> None:
    proc = subprocess.run(["git", *_render_git_config(extra_config), "clone", ref, str(repo)], capture_output=True, text=True)
    if proc.returncode != 0:
        code = "sync_auth_failed" if "authentication" in (proc.stderr or "").lower() else "sync_remote_unreachable"
        raise RealmSyncError(
            code,
            "Could not clone realm sync repository.",
            retryable=True,
            safe_details={"stderr": _redact_text(_scrub_config_values(proc.stderr, extra_config))},
        )


def _render_git_config(extra_config: Sequence[str] | None) -> list[str]:
    rendered: list[str] = []
    for pair in extra_config or []:
        rendered.extend(["-c", str(pair)])
    return rendered


def _scrub_config_values(text: str, extra_config: Sequence[str] | None) -> str:
    scrubbed = text or ""
    for pair in extra_config or []:
        _key, _sep, value = str(pair).partition("=")
        if value.strip():
            scrubbed = scrubbed.replace(value, "[redacted]")
    return scrubbed


def _ensure_git_identity(repo: Path) -> None:
    if not _git(repo, "config", "user.email", check=False).strip():
        _git(repo, "config", "user.email", "realm-sync@localhost")
    if not _git(repo, "config", "user.name", check=False).strip():
        _git(repo, "config", "user.name", "Hermes Realm Sync")


def _notify_publish(realm: Realm, *, repo: Path, artifacts: list[RealmSyncArtifact], credential: "RealmSyncCredential | None") -> list[dict[str, Any]]:
    """Best-effort counts-only publish notification (Stage 43 C4).

    A notify failure is downgraded to a ``warnings[]`` entry on the success
    envelope — the publish itself already landed and must not be failed.
    """
    if credential is None:
        return []
    commit = _git(repo, "rev-parse", "HEAD", check=False).strip()
    counts: dict[str, int] = {}
    for artifact in artifacts:
        counts[artifact.kind] = counts.get(artifact.kind, 0) + 1
    from .realm_membership import notify_realm_published

    try:
        notify_realm_published(credential, realm.id, commit=commit, artifact_counts=counts)
    except RealmSyncError as exc:
        return [
            {
                "code": "sync_notify_failed",
                "message": "Publish succeeded but the backend publish notification failed; members will not receive a realtime update signal.",
                "retryable": bool(exc.retryable),
            }
        ]
    return []


def realm_sync_sidecar_path(realm_id: str) -> Path:
    return paths.store_root() / "realm_sync_state" / f"{paths.safe_path_token(realm_id)}.json"


def read_realm_sync_sidecar(realm_id: str) -> dict[str, Any] | None:
    """Read the cached sync state written by the sync verbs.

    Pure file read — this is the ONLY realm-sync surface ``build_snapshot`` may
    touch (Decision 7: zero git calls / artifact resolution in the snapshot).
    Returns ``None`` when no sidecar exists (launcher renders "not checked").
    """
    try:
        raw = json.loads(realm_sync_sidecar_path(realm_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return {
        "state": raw.get("state"),
        "ahead": raw.get("ahead"),
        "behind": raw.get("behind"),
        "skills_drift": raw.get("skills_drift") or [],
        "skill_publish_mode": raw.get("skill_publish_mode") or "all",
        "skill_selection": raw.get("skill_selection") or [],
        "skills_published": raw.get("skills_published") or 0,
        "agent_publish_mode": raw.get("agent_publish_mode") or "workspace",
        "agent_selection": raw.get("agent_selection") or [],
        "agents_published": raw.get("agents_published") or 0,
        "conflicts": raw.get("conflicts") or [],
        "last_pull": raw.get("last_pull"),
        "last_publish": raw.get("last_publish"),
        "artifacts": raw.get("artifacts"),
        "checked_at": raw.get("checked_at"),
        "workspace_statuses": raw.get("workspace_statuses") or [],
        # Additive + absent-tolerant: a sidecar written before the profile-file
        # lane existed simply reports no holds.
        "profile_artifacts_held": raw.get("profile_artifacts_held") or [],
    }


def _held_profile_artifacts(realm: Realm, repo: Path) -> list[str]:
    """Profile-file entity keys currently HELD for this realm.

    A classify-only (``dry_run``) pass through the ONE applier — never a second
    decision table, and never a write from the status path.
    """

    from .profile_artifact_sync import apply_profile_artifact_pull

    try:
        summary = apply_profile_artifact_pull(realm.id, _realm_subtree(repo, realm.id), dry_run=True)
    except Exception:  # noqa: BLE001 — status must never fail over an evidence field
        logger.exception("held profile-artifact classification failed for realm %s", realm.id)
        return []
    return sorted(set(summary.held))


def _write_sync_sidecar(
    realm: Realm,
    *,
    repo: Path,
    git: dict[str, Any],
    skills_drift: list[str],
    artifacts: list[RealmSyncArtifact],
    profile_artifacts_held: list[str] | None = None,
) -> None:
    agent_state = realm_agent_selection_state(realm.id)
    payload = {
        "schema_version": 2,
        "realm_id": realm.id,
        "state": _sync_state(git),
        "ahead": git["ahead"],
        "behind": git["behind"],
        "skills_drift": skills_drift,
        "skill_publish_mode": realm.skill_publish_mode,
        "skill_selection": sorted(realm.skill_selection or []),
        "skills_published": _distinct_skill_package_count(artifacts),
        "agent_publish_mode": agent_state["mode"],
        "agent_selection": agent_state["selection"],
        "agents_published": len(agent_state["published"]),
        "conflicts": git["conflicts"],
        "last_pull": _timestamp_file(repo, "last_pull.txt"),
        "last_publish": _timestamp_file(repo, "last_publish.txt"),
        "artifacts": len(artifacts),
        "checked_at": now().astimezone(timezone.utc).isoformat(),
        "workspace_statuses": _workspace_sync_statuses(realm, repo),
        "profile_artifacts_held": list(profile_artifacts_held or []),
    }
    try:
        atomic_json_write(realm_sync_sidecar_path(realm.id), payload)
    except OSError:
        return  # the sidecar is a best-effort snapshot cache; never fail the sync verb over it


def _write_sync_metadata(subtree: Path, *, realm: Realm, artifacts: list[RealmSyncArtifact]) -> None:
    manifest = {
        "schema_version": 1,
        "kind": "realm_sync_manifest",
        "realm_id": realm.id,
        "server_id": realm.server_id,
        "generated_at": now().isoformat(),
        "artifacts": [artifact.row() for artifact in artifacts],
    }
    (subtree / "manifest.json").write_bytes(
        _canonicalize_text_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
    )


def _sync_result(realm: Realm, action: str, state: str, artifacts: list[RealmSyncArtifact], *, repo: Path, git: dict[str, Any], changed: bool) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "id": realm.id,
        "kind": "realm_sync",
        "action": action,
        "state": state,
        "ahead": git["ahead"],
        "behind": git["behind"],
        "conflicts": git["conflicts"],
        "changed": bool(changed),
        "artifacts": [artifact.row() for artifact in artifacts],
        "artifact_count": len(artifacts),
        "secrets_excluded": [],
        "sync_repo": _safe_display_path(repo),
        "updated_at": now(),
        "workspace_statuses": _workspace_sync_statuses(realm, repo),
    }


def _workspace_sync_statuses(realm: Realm, repo: Path) -> list[dict[str, str]]:
    """Return honest per-workspace publication truth for this realm.

    Local realms are device-owned. A server-bound workspace is published only
    when its current store file byte-matches the file in the checked-out realm
    subtree; a missing or changed remote artifact is unpublished. Transient
    syncing is deliberately a launcher phase, never persisted here.
    """
    workspace_store = WorkspaceStore()
    workspace_ids = set(realm.workspace_ids or [])
    for workspace in workspace_store.list_all(include_archived=True):
        if workspace.realm_id == realm.id:
            workspace_ids.add(workspace.id)
    rows: list[dict[str, str]] = []
    subtree = _realm_subtree(repo, realm.id) / "store" / "workspaces"
    for workspace_id in sorted(workspace_ids):
        local = paths.workspace_path(workspace_id)
        if not local.exists():
            continue
        if not realm.server_id:
            state = "local"
        else:
            published = subtree / f"{paths.safe_path_token(workspace_id)}.json"
            try:
                # Canonical compare: the published file is LF while the local
                # store file is CRLF on Windows — byte-equality would falsely
                # report a just-published workspace as "unpublished".
                matches = published.exists() and _canonicalize_text_bytes(published.read_bytes()) == _canonicalize_text_bytes(local.read_bytes())
            except OSError:
                matches = False
            state = "published" if matches else "unpublished"
        rows.append({"workspace_id": workspace_id, "state": state})
    return rows


@dataclass(frozen=True, slots=True)
class SkillSyncSummary:
    """Outcome of :func:`apply_skill_inbox_pull` — the per-package reconcile
    verdicts for one realm pull. ``adopted`` were promoted new into the canonical
    root, ``converged`` already matched canonical (no write), ``held`` diverge and
    were quarantined without touching canonical (an operator resolves them via
    ``hermes harness skills promote --adopt-divergent``), ``removed`` were pruned
    from the inbox because the realm no longer publishes them, and ``refused`` are
    packages the guarded door would not admit — an invalid/hostile/reserved slug,
    a canonical slot occupied by a non-skill-package (a bare-slug landing on an
    existing category dir, or a categorized child whose parent is a bare skill),
    or a per-package error — isolated so ONE bad package can never abort the pull.
    Refused packages are deliberately kept OUT of ``skills_drift`` (which stays the
    held-divergent set the operator resolves); they are surfaced only here. All
    lists are sorted, de-duplicated skill slugs (bare or ``<category>/<name>``)."""

    adopted: list[str]
    converged: list[str]
    held: list[str]
    removed: list[str]
    refused: list[str]

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "adopted": list(self.adopted),
            "converged": list(self.converged),
            "held": list(self.held),
            "removed": list(self.removed),
            "refused": list(self.refused),
        }


def apply_skill_inbox_pull(realm: Realm, subtree: Path) -> SkillSyncSummary:
    """Mirror the pulled ``subtree/skills/**`` into the realm's resolver-invisible
    inbox, then reconcile each package through the one guarded promotion door.

    Mirrors the board/office pull-applier precedent (``apply_board_pull`` /
    ``apply_office_pull``): the generic overwrite loop no longer touches
    ``skills/…`` (``_destination_for_sync_path`` returns ``None``), so this owns
    the whole skill lane. The inbox is a byte-faithful, LF-canonical copy of that
    realm's current skill packages that the resolver never sees
    (``EXCLUDED_SKILL_DIRS`` — C1). Each package is then classified against the
    canonical shared root:

    - ``promote_new`` (no canonical copy) → auto-adopted, provenance recorded
      (``source={"kind": "realm", "realm_id": realm.id}``); the inbox mirror is
      **never** moved (``move_source=False``).
    - ``noop_identical`` → converged; canonical untouched.
    - ``hold_divergent`` → held; canonical untouched, surfaced as drift.

    Never called on a dry-run pull (``pull_realm_sync`` returns before this),
    so the mirror — itself a mutation — is skipped when ``dry_run=True``.
    """

    from .skill_promotion import (
        _iter_packages,
        classify_promotion,
        execute_promotion,
        realm_inbox_dir,
    )

    inbox = realm_inbox_dir(realm.id)
    removed, reserved_refused = _mirror_realm_skill_inbox(subtree / "skills", inbox)

    adopted: list[str] = []
    converged: list[str] = []
    held: list[str] = []
    # Packages the mirror skipped (a reserved device-name component would crash a
    # Windows write) start the refused set; each is already NOT on disk.
    refused: list[str] = list(reserved_refused)
    # Iterate the mirrored inbox package-by-package with per-package isolation:
    # a package that refuses OR raises (a malformed source, a TOCTOU-occupied
    # canonical slot, an unexpected I/O error) must never abort the whole pull —
    # it is recorded as ``refused`` and reconciliation continues (F1c).
    from .sync_admission import refuse_package

    for slug, source_dir in _iter_packages(inbox):
        try:
            # Admission scan (defect (b), 2026-07-25): the generic pull loop's
            # ``_assert_no_secret_artifacts`` only covers artifacts it MAPS, and
            # ``skills/…`` maps to None — so a pulled package was never scanned
            # on the way in. Per-package isolation: one hostile package is
            # refused, the rest of the pull continues. Portability is
            # deliberately NOT scanned here — a skill's documentation
            # legitimately names absolute paths (see ``sync_admission``).
            refusal = refuse_package(slug, source_dir)
            if refusal is not None:
                logger.warning("skill package refused at the realm door: %s (%s)", slug, refusal.code)
                refused.append(slug)
                continue
            plan = classify_promotion(slug, source_dir)
            if plan.action == "promote_new":
                result = execute_promotion(
                    plan,
                    source={"kind": "realm", "realm_id": realm.id},
                    move_source=False,
                )
                if result.action == "promoted":
                    adopted.append(slug)
                elif result.action == "held":
                    held.append(slug)
                else:  # 'refused' / anything non-terminal — never became canonical
                    refused.append(slug)
            elif plan.action == "noop_identical":
                converged.append(slug)
            elif plan.action == "hold_divergent":
                held.append(slug)
            else:  # refuse_invalid
                refused.append(slug)
        except Exception:  # noqa: BLE001 — one bad package must not abort the pull
            logger.exception(
                "skill inbox reconcile raised for %r (realm %s); refusing package",
                slug,
                realm.id,
            )
            refused.append(slug)
    return SkillSyncSummary(
        adopted=sorted(set(adopted)),
        converged=sorted(set(converged)),
        held=sorted(set(held)),
        removed=sorted(set(removed)),
        refused=sorted(set(refused)),
    )


def _mirror_realm_skill_inbox(source_skills: Path, inbox: Path) -> tuple[list[str], list[str]]:
    """Mirror ``source_skills`` (the pulled ``subtree/skills``) into ``inbox`` as
    an LF-canonical, resolver-invisible copy.

    Returns ``(removed, reserved_refused)``: ``removed`` are the top-level packages
    pruned (present in the inbox, gone from the subtree); ``reserved_refused`` are
    the top-level packages skipped WITHOUT any write because a relative path
    component maps to a Windows reserved device name (``con`` / ``nul`` /
    ``com1`` … — a realm publishing such a package would otherwise crash the
    mirror on Windows, a pull DoS). Reserved packages are skipped on every
    platform for deterministic behaviour (a reserved slug can never be promoted
    anyway — ``_validate_slug`` refuses it) and reported so the caller can record
    them as ``refused``.

    Text is LF-normalized at this write chokepoint (matching the publisher's
    ``_canonicalize_text_bytes``) so a package converges against an LF canonical
    regardless of the incoming EOL; binary assets (NUL byte) pass through
    byte-for-byte. A file whose only difference from the existing inbox copy is
    its line endings is left untouched, so a no-op pull neither rewrites bytes nor
    churns mtimes (the content-hash cache stays warm — same discipline as the
    publish EOL guard, ``test_realm_sync_eol.py``).
    """

    from .skill_promotion import is_windows_reserved_component

    # First pass: collect legal source files and the set of top-level package
    # families that contain a reserved-device-name component anywhere in their
    # subtree. Any file under such a family is skipped BEFORE a write is attempted
    # (creating a dir/file named ``con`` on Windows fails), and the whole family
    # is quarantined out so a package is never partially mirrored.
    reserved_tops: set[str] = set()
    candidates: list[tuple[tuple[str, ...], Path]] = []
    if source_skills.is_dir():
        for src in sorted(p for p in source_skills.rglob("*") if p.is_file()):
            rel_parts = src.relative_to(source_skills).parts
            # Untrusted remote tree: a mirror path must never escape the inbox
            # (rglob cannot emit ``..``, but guard defensively regardless).
            if any(part in ("", ".", "..") for part in rel_parts):
                continue
            if any(is_windows_reserved_component(part) for part in rel_parts):
                reserved_tops.add(rel_parts[0])
                continue
            candidates.append((rel_parts, src))

    desired: dict[str, bytes] = {}
    for rel_parts, src in candidates:
        # A sibling file of a reserved path within the same top-level family is
        # dropped too, so the family is quarantined whole (never half-written).
        if rel_parts[0] in reserved_tops:
            continue
        desired["/".join(rel_parts)] = _canonicalize_text_bytes(src.read_bytes())

    existing: dict[str, bytes] = {}
    if inbox.is_dir():
        for path in sorted(p for p in inbox.rglob("*") if p.is_file()):
            existing[path.relative_to(inbox).as_posix()] = path.read_bytes()

    desired_top = {rel.split("/", 1)[0] for rel in desired}
    existing_top = {rel.split("/", 1)[0] for rel in existing}
    removed = sorted(existing_top - desired_top - reserved_tops)

    for rel, data in desired.items():
        prior = existing.get(rel)
        if prior is not None and _canonicalize_text_bytes(prior) == data:
            continue  # EOL-only (or no) difference — leave it to avoid churn
        target = inbox.joinpath(*rel.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    for rel in existing:
        if rel not in desired:
            inbox.joinpath(*rel.split("/")).unlink()
    _prune_empty_dirs(inbox)
    return removed, sorted(reserved_tops)


def _prune_empty_dirs(root: Path) -> None:
    """Remove now-empty subdirectories left behind by inbox pruning (deepest
    first). ``root`` itself is preserved even when empty — it is the realm's
    inbox anchor."""

    if not root.is_dir():
        return
    for directory in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass  # not empty (or vanished) — leave it


def _held_skill_packages_for_realm(realm: Realm) -> list[str]:
    """``skills_drift`` = the realm's inbox packages whose canonical copy diverges.

    Scans this realm's resolver-invisible inbox and returns the sorted slugs
    classified ``hold_divergent`` against the current canonical root — the set an
    operator must explicitly resolve (``promote --adopt-divergent``). Redefines
    the historic drift meaning (formerly a source-vs-destination byte compare over
    publish artifacts, which was structurally always empty since publish source
    and destination were the same canonical file) while keeping the sidecar/result
    key name and ``list[str]`` shape stable (Launcher realm-sync sheet compat)."""

    from .skill_promotion import list_inbox_packages

    return sorted(
        {
            row["skill"]
            for row in list_inbox_packages(realm.id)
            if row["action"] == "hold_divergent"
        }
    )


def _distinct_skill_package_count(artifacts: list[RealmSyncArtifact]) -> int:
    """Number of distinct skill *packages* (top-level skill dir names) among
    resolved artifacts — not the file count. A multi-file skill counts once."""
    packages: set[str] = set()
    for artifact in artifacts:
        if artifact.kind != "skill":
            continue
        parts = Path(artifact.relative_path).parts
        if len(parts) >= 2:
            packages.add(parts[1])
    return len(packages)


def _timestamp_file(repo: Path, name: str) -> str | None:
    try:
        return (repo / ".git" / "hermes" / name).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_timestamp(repo: Path, name: str) -> None:
    path = repo / ".git" / "hermes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now().isoformat(), encoding="utf-8")


def _canonicalize_text_bytes(raw: bytes) -> bytes:
    """Normalize published-artifact line endings to LF — the ONE canonicalization
    chokepoint for realm sync.

    Realm-sync artifacts are read from stores that write CRLF on Windows
    (``atomic_json_write`` / ``str.write_text`` use text mode) while the pull
    lane writes LF (``json.dumps(...).encode()``). Committing those raw bytes
    turns every publish into a whole-file CRLF<->LF churn and reports
    ``changed=true`` on no-op runs; diffs/merges between members carry EOL noise.
    LF-normalizing at the write/copy boundary keeps the repo tree byte-stable.

    Binary/asset artifacts (skill PNG/JPG/…) are detected by a NUL byte — git's
    own text/binary heuristic — and passed through byte-for-byte untouched. The
    3-way merge classifiers and office/board baselines hash the PARSED model
    (EOL-agnostic), so canonicalizing bytes here never desyncs those hashes.
    """
    if b"\x00" in raw:
        return raw
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _published_artifacts_differ(subtree: Path, desired: dict[str, bytes]) -> bool:
    """True when the canonical published bytes differ from what is already in the
    realm subtree, ignoring manifest.json (its ``generated_at`` is volatile).

    ``desired`` is canonical (LF); the on-disk bytes are compared RAW, so a legacy
    CRLF subtree triggers a one-time LF migration on the next publish while an
    already-canonical subtree is a true no-op (no rewrite, no commit)."""
    if not subtree.exists():
        return bool(desired)
    existing: dict[str, bytes] = {}
    for path in subtree.rglob("*"):
        if path.is_file() and path.name != "manifest.json":
            existing[path.relative_to(subtree).as_posix()] = path.read_bytes()
    return existing != desired


def _dedupe_artifacts(artifacts: list[RealmSyncArtifact]) -> list[RealmSyncArtifact]:
    deduped: dict[str, RealmSyncArtifact] = {}
    for artifact in artifacts:
        # A synthesized artifact has no file to exist — its bytes ARE the
        # artifact. Only file-backed artifacts are dropped when their source
        # vanished between resolution and publish.
        if artifact.content is None and not artifact.source.exists():
            continue
        rel = artifact.relative_path.replace("\\", "/")
        if _is_hard_excluded_path(rel):
            continue
        deduped[rel] = artifact
    return [deduped[key] for key in sorted(deduped)]


def _looks_like_remote(value: str) -> bool:
    return value.startswith(("http://", "https://", "ssh://", "git@"))


def _safe_display_path(path: Path) -> str:
    try:
        return path.relative_to(get_hermes_home()).as_posix()
    except ValueError:
        try:
            return path.relative_to(paths.store_root()).as_posix()
        except ValueError:
            return path.name


def _redact_text(text: str) -> str:
    redacted = SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text or "")
    home = str(get_hermes_home())
    config = str(get_config_path())
    redacted = redacted.replace(home, "<HERMES_HOME>").replace(config, "<CONFIG>")
    return redacted[-800:]
