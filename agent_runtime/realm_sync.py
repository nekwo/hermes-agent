from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hermes_constants import get_config_path, get_hermes_home
from hermes_time import now
from utils import atomic_json_write

if TYPE_CHECKING:
    from .realm_membership import RealmSyncCredential

from . import paths
from .config import ensure_persisted_personas, load_agent_runtime_config
from .events import EventLog
from .models import AgentPersona, Event, Realm, Workspace
from .profile_context import active_profile_name, resolve_persona_profile
from .skill_install import HARNESS_SKILLS, install_harness_skills, install_harness_skills_for_personas
from .store import RealmStore, WorkspaceStore


SYNC_STATES = {"in_sync", "behind", "ahead", "conflict", "publishing"}
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
HARD_EXCLUDED_PATH_PARTS = {
    "blueprints",
    "blueprint_runs",
    "packet_artifacts",
    "proofs",
    "runs",
    "state.db",
    "worker_sessions",
}
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|client[_-]?secret|oauth[_-]?token|password|private[_-]?key|secret|token)\b"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=:-]{16,}"
)


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
    workspace_statuses = _workspace_sync_statuses(realm, repo)
    skills_drift = _skills_drift_for_artifacts(artifacts)
    state = _sync_state(git)
    _write_sync_sidecar(realm, repo=repo, git=git, skills_drift=skills_drift, artifact_count=len(artifacts))
    return {
        "schema_version": 1,
        "id": realm.id,
        "kind": "realm_sync",
        "state": state,
        "ahead": git["ahead"],
        "behind": git["behind"],
        "skills_drift": skills_drift,
        "conflicts": git["conflicts"],
        "last_pull": _timestamp_file(repo, "last_pull.txt"),
        "last_publish": _timestamp_file(repo, "last_publish.txt"),
        "artifacts": len(artifacts),
        "sync_repo": _safe_display_path(repo),
        "workspace_statuses": workspace_statuses,
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
    artifacts = resolve_realm_sync_artifacts(realm_id)
    _assert_no_secret_artifacts(artifacts)
    if dry_run:
        return _sync_result(realm, "publish", "dry_run", artifacts, repo=repo, git=git, changed=False)

    subtree = _realm_subtree(repo, realm.id)
    if subtree.exists():
        shutil.rmtree(subtree)
    for artifact in artifacts:
        target = subtree / artifact.relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact.source, target)
    _write_sync_metadata(subtree, realm=realm, artifacts=artifacts)
    _git(repo, "add", "--", f"realms/{_safe_token(realm.id)}")
    changed = bool(_git(repo, "status", "--porcelain", f"realms/{_safe_token(realm.id)}").strip())
    if changed:
        _ensure_git_identity(repo)
        _git(repo, "commit", "-m", f"Publish realm sync {realm.id}")
        try:
            _git(repo, "push", extra_config=_credential_git_config(credential))
        except RealmSyncError as exc:
            raise RealmSyncError("sync_remote_unreachable", "Realm sync publish committed locally but could not push upstream.", retryable=True, safe_details=exc.safe_details) from exc
    _write_timestamp(repo, "last_publish.txt")
    warnings = _notify_publish(realm, repo=repo, artifacts=artifacts, credential=credential) if changed else []
    git_after = _git_state(repo)
    result = _sync_result(realm, "publish", "published", artifacts, repo=repo, git=git_after, changed=changed)
    if warnings:
        result["warnings"] = warnings
    _write_sync_sidecar(realm, repo=repo, git=git_after, skills_drift=_skills_drift_for_artifacts(artifacts), artifact_count=len(artifacts))
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
        if before != data:
            artifact.destination.write_bytes(data)
            changed = True
    install_results = [
        *install_harness_skills(skills=sorted(HARNESS_SKILLS)),
        *install_harness_skills_for_personas(ensure_persisted_personas(load_agent_runtime_config())),
    ]
    _write_timestamp(repo, "last_pull.txt")
    git_after = _git_state(repo)
    result = _sync_result(realm, "pull", "pulled", artifacts, repo=repo, git=git_after, changed=changed)
    result["skill_reconcile"] = {
        "installed": [item.skill for item in install_results],
        "changed": [item.skill for item in install_results if item.changed],
        "ok": all(item.ok for item in install_results),
    }
    _write_sync_sidecar(realm, repo=repo, git=git_after, skills_drift=_skills_drift_for_artifacts(artifacts), artifact_count=len(artifacts))
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


def resolve_realm_sync_artifacts(realm_id: str) -> list[RealmSyncArtifact]:
    realm = RealmStore().get(realm_id)
    workspace_store = WorkspaceStore()
    workspace_ids = set(realm.workspace_ids or [])
    for workspace in workspace_store.list_all(include_archived=True):
        if workspace.realm_id == realm.id:
            workspace_ids.add(workspace.id)
    workspaces = [workspace_store.get(workspace_id) for workspace_id in sorted(workspace_ids) if paths.workspace_path(workspace_id).exists()]
    cfg = load_agent_runtime_config()
    personas = {persona.id: persona for persona in ensure_persisted_personas(cfg)}
    wanted_persona_ids = list(dict.fromkeys(persona_id for ws in workspaces for persona_id in (ws.agent_ids or [])))
    artifacts: list[RealmSyncArtifact] = []
    artifacts.extend(_skill_artifacts())
    artifacts.extend(_workspace_realm_artifacts(realm, workspaces))
    for persona_id in wanted_persona_ids:
        persona = personas.get(persona_id)
        if persona is None:
            continue
        artifacts.extend(_persona_artifacts(persona))
    return _dedupe_artifacts(artifacts)


def sync_artifacts_for_workspace_agent(workspace_id: str, persona_id: str) -> list[dict[str, str]]:
    workspace = WorkspaceStore().get(workspace_id)
    if not workspace.realm_id:
        return []
    artifacts = resolve_realm_sync_artifacts(workspace.realm_id)
    needle = f"/{_safe_token(persona_id)}/"
    return [artifact.row() for artifact in artifacts if needle in f"/{artifact.relative_path}"]


def _skill_artifacts() -> list[RealmSyncArtifact]:
    root = get_hermes_home() / "skills"
    artifacts: list[RealmSyncArtifact] = []
    if not root.exists():
        return artifacts
    for skill_md in sorted(root.glob("*/SKILL.md")):
        skill = skill_md.parent.name
        artifacts.append(
            RealmSyncArtifact(
                kind="skill",
                source=skill_md,
                relative_path=f"skills/{_safe_token(skill)}/SKILL.md",
                destination=get_hermes_home() / "skills" / _safe_token(skill) / "SKILL.md",
            )
        )
    return artifacts


def _workspace_realm_artifacts(realm: Realm, workspaces: list[Workspace]) -> list[RealmSyncArtifact]:
    artifacts = [
        RealmSyncArtifact(
            kind="realm",
            source=paths.realm_path(realm.id),
            relative_path=f"store/realms/{_safe_token(realm.id)}.json",
            destination=paths.realm_path(realm.id),
        )
    ]
    for workspace in workspaces:
        artifacts.append(
            RealmSyncArtifact(
                kind="workspace",
                source=paths.workspace_path(workspace.id),
                relative_path=f"store/workspaces/{_safe_token(workspace.id)}.json",
                destination=paths.workspace_path(workspace.id),
            )
        )
    return [item for item in artifacts if item.source.exists()]


def _persona_artifacts(persona: AgentPersona) -> list[RealmSyncArtifact]:
    binding = resolve_persona_profile(persona)
    profile_home = binding.profile_home or get_hermes_home()
    profile = _safe_token(binding.hermes_profile or active_profile_name() or "default")
    persona_token = _safe_token(persona.id)
    artifacts: list[RealmSyncArtifact] = []
    config = profile_home / "config.yaml"
    if config.exists():
        artifacts.append(
            RealmSyncArtifact(
                kind="persona_config",
                source=config,
                relative_path=f"profiles/{profile}/config.yaml",
                destination=config,
            )
        )
    for label, raw in (("system_prompt", persona.system_prompt_path), ("soul_overlay", persona.soul_overlay_path)):
        path = _profile_relative_file(profile_home, raw)
        if path is not None and path.exists():
            artifacts.append(
                RealmSyncArtifact(
                    kind=label,
                    source=path,
                    relative_path=f"profiles/{profile}/personas/{persona_token}/{label}/{path.name}",
                    destination=path,
                )
            )
    if persona.include_profile_memory:
        memory = profile_home / "memories" / "MEMORY.md"
        if memory.exists():
            artifacts.append(
                RealmSyncArtifact(
                    kind="profile_memory",
                    source=memory,
                    relative_path=f"profiles/{profile}/personas/{persona_token}/memories/MEMORY.md",
                    destination=memory,
                )
            )
    if persona.include_core_context_files:
        for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            context = profile_home / name
            if context.exists():
                artifacts.append(
                    RealmSyncArtifact(
                        kind="core_context",
                        source=context,
                        relative_path=f"profiles/{profile}/personas/{persona_token}/context/{name}",
                        destination=context,
                    )
                )
    return artifacts


def _profile_relative_file(profile_home: Path, raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(str(raw))
    if path.is_absolute() or ".." in path.parts:
        return None
    return profile_home / path


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
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
        return get_hermes_home() / "skills" / parts[1] / "SKILL.md"
    if len(parts) == 3 and parts[0] == "store" and parts[1] == "workspaces":
        return paths.workspaces_dir() / parts[2]
    if len(parts) == 3 and parts[0] == "store" and parts[1] == "realms":
        return paths.realms_dir() / parts[2]
    if parts and parts[0] == "profiles":
        profile_home = get_hermes_home() if len(parts) > 1 and parts[1] == _safe_token(active_profile_name()) else get_hermes_home()
        if len(parts) == 3 and parts[2] == "config.yaml":
            return profile_home / "config.yaml"
        if "memories" in parts and parts[-1] == "MEMORY.md":
            return profile_home / "memories" / "MEMORY.md"
        if "context" in parts:
            return profile_home / parts[-1]
        if "system_prompt" in parts or "soul_overlay" in parts:
            return profile_home / "personas" / parts[-1]
    return None


def _kind_for_sync_path(rel: str) -> str:
    if rel.startswith("skills/"):
        return "skill"
    if rel.startswith("store/workspaces/"):
        return "workspace"
    if rel.startswith("store/realms/"):
        return "realm"
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
        if _is_secretish_path(rel) or _is_hard_excluded_path(rel) or _file_contains_secret_assignment(artifact.source):
            blocked.append(rel)
    if blocked:
        raise RealmSyncError("sync_secret_excluded", "Realm sync refused to include excluded secret/state artifacts.", safe_details={"paths": blocked[:20]})


def _is_secretish_path(rel: str) -> bool:
    parts = {part.lower() for part in Path(rel).parts}
    return bool(parts & SECRET_PATH_MARKERS)


def _is_hard_excluded_path(rel: str) -> bool:
    parts = {part.lower() for part in Path(rel).parts}
    return bool(parts & HARD_EXCLUDED_PATH_PARTS)


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
        return repo
    if _looks_like_remote(str(realm.sync_manifest_ref or "")):
        repo.parent.mkdir(parents=True, exist_ok=True)
        _git_clone(str(realm.sync_manifest_ref), repo, extra_config=_credential_git_config(credential))
    else:
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init")
    return repo


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
    key = _safe_token(realm.server_id or "local")
    return paths.store_root() / "realm_sync" / key


def _realm_subtree(repo: Path, realm_id: str) -> Path:
    return repo / "realms" / _safe_token(realm_id)


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
    return paths.store_root() / "realm_sync_state" / f"{_safe_token(realm_id)}.json"


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
        "conflicts": raw.get("conflicts") or [],
        "last_pull": raw.get("last_pull"),
        "last_publish": raw.get("last_publish"),
        "artifacts": raw.get("artifacts"),
        "checked_at": raw.get("checked_at"),
        "workspace_statuses": raw.get("workspace_statuses") or [],
    }


def _write_sync_sidecar(realm: Realm, *, repo: Path, git: dict[str, Any], skills_drift: list[str], artifact_count: int) -> None:
    payload = {
        "schema_version": 2,
        "realm_id": realm.id,
        "state": _sync_state(git),
        "ahead": git["ahead"],
        "behind": git["behind"],
        "skills_drift": skills_drift,
        "conflicts": git["conflicts"],
        "last_pull": _timestamp_file(repo, "last_pull.txt"),
        "last_publish": _timestamp_file(repo, "last_publish.txt"),
        "artifacts": artifact_count,
        "checked_at": now().astimezone(timezone.utc).isoformat(),
        "workspace_statuses": _workspace_sync_statuses(realm, repo),
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
    (subtree / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


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
            published = subtree / f"{_safe_token(workspace_id)}.json"
            try:
                matches = published.exists() and published.read_bytes() == local.read_bytes()
            except OSError:
                matches = False
            state = "published" if matches else "unpublished"
        rows.append({"workspace_id": workspace_id, "state": state})
    return rows


def _skills_drift_for_artifacts(artifacts: list[RealmSyncArtifact]) -> list[str]:
    drift: list[str] = []
    for artifact in artifacts:
        if artifact.kind != "skill":
            continue
        if artifact.destination.exists() and artifact.source.exists() and artifact.destination.read_bytes() != artifact.source.read_bytes():
            drift.append(Path(artifact.relative_path).parts[1])
    return sorted(set(drift))


def _timestamp_file(repo: Path, name: str) -> str | None:
    try:
        return (repo / ".git" / "hermes" / name).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_timestamp(repo: Path, name: str) -> None:
    path = repo / ".git" / "hermes" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(now().isoformat(), encoding="utf-8")


def _dedupe_artifacts(artifacts: list[RealmSyncArtifact]) -> list[RealmSyncArtifact]:
    deduped: dict[str, RealmSyncArtifact] = {}
    for artifact in artifacts:
        if not artifact.source.exists():
            continue
        rel = artifact.relative_path.replace("\\", "/")
        if _is_hard_excluded_path(rel):
            continue
        deduped[rel] = artifact
    return [deduped[key] for key in sorted(deduped)]


def _safe_token(value: str | None) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(value or "").strip())
    return text.strip("._")[:120] or "item"


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
