"""Declarative delivery directive: what happens to a delivered repo bundle.

The directive is DECLARED once on the goal (Stage 38 request field, falling
back to the contract default), stored on the Task, projected to the HUD, and
executed by ONE executor at the terminal-settle boundary. The harness makes no
ad-hoc micro-decisions here: it obeys the declared directive and reports every
step as events or incidents.

Steps, in contract order:

1. ``preserve_diff`` — the bundle's worktree patch is captured at delivery time
   into ``repo_bundles/<task_id>/<bundle_id>.patch`` so the existing archive
   move preserves it with no archive-writer changes. A ``done`` task whose diff
   only lives in a disposable worktree is the failure mode this kills.
2. ``promote`` — apply the captured patch to the real product repo and commit,
   guarded (patch must apply cleanly; touched paths must not be dirty in the
   target). Failure opens an incident and leaves the worktree in place.
3. ``worktree`` — reap the run worktree only after the promote step succeeded
   (or was a deliberate hold / empty no-op), never before.
"""

from __future__ import annotations

import subprocess
import os
import uuid
import hashlib
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
from .events import EventLog
from .models import Event, Incident, RepoBundle
from .repo_context import (
    changed_files_from_diff,
    existing_run_worktrees,
    remove_harness_worktree_for_repo,
    resolve_affected_repo_workdir,
    worktree_patch_size_estimate,
    worktree_patch_text,
)
from .states import TaskState

DIRECTIVE_KEY = "delivery_directive"

PROMOTE_MODES = frozenset({"apply_to_repo", "hold"})
PRESERVE_DIFF_MODES = frozenset({"archive", "none"})
WORKTREE_MODES = frozenset({"reap_after_promote", "keep"})

DEFAULT_DELIVERY_DIRECTIVE: dict[str, str] = {
    "promote": "apply_to_repo",
    "preserve_diff": "archive",
    "worktree": "reap_after_promote",
}

_PATCH_MAX_BYTES = 8 * 1024 * 1024


class DeliveryDirectiveInvalid(ValueError):
    """Raised when a declared directive has unsupported keys or values."""


def normalize_delivery_directive(value: Any) -> dict[str, str]:
    """Validate a declared directive; missing keys take contract defaults."""

    if value is None:
        return dict(DEFAULT_DELIVERY_DIRECTIVE)
    if not isinstance(value, dict):
        raise DeliveryDirectiveInvalid("delivery_directive must be an object")
    extra = sorted(set(value.keys()) - set(DEFAULT_DELIVERY_DIRECTIVE))
    if extra:
        raise DeliveryDirectiveInvalid(f"delivery_directive has unsupported keys: {extra}")
    directive = dict(DEFAULT_DELIVERY_DIRECTIVE)
    for key, allowed in (
        ("promote", PROMOTE_MODES),
        ("preserve_diff", PRESERVE_DIFF_MODES),
        ("worktree", WORKTREE_MODES),
    ):
        if key in value:
            mode = str(value[key] or "").strip()
            if mode not in allowed:
                raise DeliveryDirectiveInvalid(
                    f"delivery_directive.{key} must be one of {sorted(allowed)}"
                )
            directive[key] = mode
    return directive


def task_delivery_directive(task: Any) -> dict[str, str]:
    declared = getattr(task, "delivery_directive", None)
    try:
        return normalize_delivery_directive(declared)
    except DeliveryDirectiveInvalid:
        return dict(DEFAULT_DELIVERY_DIRECTIVE)


def bundle_patch_path(task_id: str, bundle_id: str) -> Path:
    return paths.repo_bundles_task_dir(task_id) / f"{bundle_id}.patch"


def bundle_promotion_record_path(task_id: str, bundle_id: str) -> Path:
    return paths.repo_bundles_task_dir(task_id) / f"{bundle_id}.promotion.json"


def read_bundle_promotion_record(task_id: str, bundle_id: str) -> dict[str, Any] | None:
    """Recorded outcome of the delivery-directive execution for a bundle."""

    import json

    record_path = bundle_promotion_record_path(task_id, bundle_id)
    if not record_path.is_file():
        return None
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def capture_bundle_patch(bundle: RepoBundle, *, event_log: EventLog | None = None) -> dict[str, Any]:
    """Persist the delivering run's worktree patch next to the bundle record.

    Called at ``mark_delivered`` time so the diff survives every later failure
    (gate, archive, worktree GC). Idempotent: recapture overwrites.
    """

    result: dict[str, Any] = {
        "captured": False,
        "patch_path": None,
        "patch_bytes": 0,
        "changed_files": [],
        "reason": None,
    }
    run_id = _bundle_run_id(bundle)
    if not run_id:
        result["reason"] = "bundle_has_no_active_run"
        return result
    worktrees = existing_run_worktrees(bundle.repo, task_id=bundle.task_id, run_id=run_id)
    patch = ""
    source_worktree: Path | None = None
    for worktree in worktrees:
        text = worktree_patch_text(worktree)
        if text.strip():
            patch = text
            source_worktree = worktree
            break
    if not patch.strip():
        result["reason"] = "worktree_missing_or_clean" if not worktrees else "worktree_clean"
        return result
    if len(patch.encode("utf-8", errors="ignore")) > _PATCH_MAX_BYTES:
        result["reason"] = "patch_exceeds_size_cap"
        _emit(
            event_log,
            "bundle.diff_capture_failed",
            bundle,
            {"reason": result["reason"], "cap_bytes": _PATCH_MAX_BYTES},
        )
        return result
    destination = bundle_patch_path(bundle.task_id, bundle.id)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # newline="" — the patch is byte-faithful (may mix LF/CRLF); no translation.
    destination.write_text(patch, encoding="utf-8", newline="")
    result.update(
        {
            "captured": True,
            "patch_path": str(destination),
            "patch_bytes": destination.stat().st_size,
            "changed_files": changed_files_from_diff(patch),
            "worktree": str(source_worktree) if source_worktree else None,
        }
    )
    _emit(
        event_log,
        "bundle.diff_captured",
        bundle,
        {
            "patch_bytes": result["patch_bytes"],
            "changed_files": result["changed_files"][:20],
        },
    )
    return result


def execute_delivery_directive(
    task: Any,
    bundle: RepoBundle,
    *,
    event_log: EventLog | None = None,
    incident_store: Any | None = None,
) -> dict[str, Any]:
    """Run the declared directive for one delivered bundle. One executor, no forks.

    Promotion only runs for ``done`` tasks; cancelled tasks keep their captured
    diff and reap the worktree, so evidence survives and litter does not.
    """

    directive = task_delivery_directive(task)
    outcome: dict[str, Any] = {
        "bundle_id": bundle.id,
        "directive": directive,
        "promote": {"status": "skipped", "reason": None, "commit": None},
        "worktree": {"status": "skipped", "reason": None},
        "patch_path": None,
    }
    patch_path = bundle_patch_path(bundle.task_id, bundle.id)
    if not patch_path.is_file():
        # Late capture for bundles delivered before this contract existed, or
        # whose delivery-time capture raced the worktree.
        capture_bundle_patch(bundle, event_log=event_log)
    has_patch = patch_path.is_file() and patch_path.stat().st_size > 0
    outcome["patch_path"] = str(patch_path) if has_patch else None

    promote_ok = False
    if directive["promote"] != "apply_to_repo":
        outcome["promote"] = {"status": "held", "reason": "directive_hold", "commit": None}
        promote_ok = True
    elif task.state != TaskState.DONE:
        outcome["promote"] = {"status": "skipped", "reason": f"task_state_{task.state}", "commit": None}
        promote_ok = True
    elif not has_patch:
        outcome["promote"] = {"status": "skipped", "reason": "no_patch_captured", "commit": None}
        promote_ok = True
    else:
        outcome["promote"] = _promote_patch_to_repo(task, bundle, patch_path, event_log=event_log)
        promote_ok = outcome["promote"]["status"] in {"promoted", "already_applied"}
        if not promote_ok:
            _open_promotion_incident(task, bundle, outcome["promote"], incident_store=incident_store, event_log=event_log)

    if directive["worktree"] == "reap_after_promote" and promote_ok:
        outcome["worktree"] = _reap_bundle_worktrees(bundle, event_log=event_log)
    elif directive["worktree"] == "reap_after_promote":
        outcome["worktree"] = {"status": "kept", "reason": "promotion_failed"}
    else:
        outcome["worktree"] = {"status": "kept", "reason": "directive_keep"}

    _write_promotion_record(task, bundle, outcome)
    return outcome


def execute_task_delivery_directives(
    task: Any,
    bundles: list[RepoBundle],
    *,
    event_log: EventLog | None = None,
    incident_store: Any | None = None,
) -> list[dict[str, Any]]:
    delivered_states = {"delivered_waiting_for_qa", "delivered", "verified"}
    outcomes: list[dict[str, Any]] = []
    for bundle in bundles:
        if bundle.state not in delivered_states:
            continue
        try:
            outcomes.append(
                execute_delivery_directive(
                    task, bundle, event_log=event_log, incident_store=incident_store
                )
            )
        except Exception as exc:  # never let directive execution break settle/archive
            outcomes.append(
                {
                    "bundle_id": bundle.id,
                    "promote": {"status": "failed", "reason": type(exc).__name__, "commit": None},
                    "worktree": {"status": "kept", "reason": "executor_error"},
                }
            )
    return outcomes


def execute_task_worktree_delivery_directives(
    task: Any,
    *,
    run_ids: list[str],
    repos: list[str],
    event_log: EventLog | None = None,
    incident_store: Any | None = None,
) -> list[dict[str, Any]]:
    """Apply the task delivery directive to dirty run worktrees with no bundle.

    Repo bundles are the primary delivery surface, but a terminal task can still
    own a dirty run worktree when bundle routing was disabled or unavailable.
    That fallback must obey the same promote/preserve/reap contract; otherwise
    archive settlement can report ``done`` while the actual patch only survives
    as a disposable worktree diff.
    """

    deliveries: list[dict[str, Any]] = []
    clean_reaps: list[dict[str, Any]] = []
    seen_worktrees: set[str] = set()
    for run_id in [str(item or "").strip() for item in run_ids if str(item or "").strip()]:
        for repo in [str(item or "").strip() for item in repos if str(item or "").strip()]:
            for worktree in existing_run_worktrees(repo, task_id=task.id, run_id=run_id):
                try:
                    resolved = str(worktree.resolve())
                except OSError:
                    resolved = str(worktree)
                if resolved in seen_worktrees:
                    continue
                seen_worktrees.add(resolved)
                patch = worktree_patch_text(worktree)
                if patch.strip():
                    bundle = _synthetic_worktree_bundle(task, repo=repo, run_id=run_id, worktree=worktree)
                    try:
                        outcome = execute_delivery_directive(
                            task,
                            bundle,
                            event_log=event_log,
                            incident_store=incident_store,
                        )
                    except Exception as exc:  # keep evidence/worktree for operator recovery
                        outcome = {
                            "bundle_id": bundle.id,
                            "directive": task_delivery_directive(task),
                            "promote": {"status": "failed", "reason": type(exc).__name__, "commit": None},
                            "worktree": {"status": "kept", "reason": "executor_error"},
                            "patch_path": None,
                        }
                    deliveries.append(
                        {
                            "source": "task_run_worktree",
                            "repo": repo,
                            "run_id": run_id,
                            "worktree": str(worktree),
                            **outcome,
                        }
                    )
                    continue

                entry: dict[str, Any] = {"worktree": worktree.name, "run_id": run_id, "repo": repo}
                if remove_harness_worktree_for_repo(repo, worktree, reason="task_archived"):
                    entry["removed"] = True
                else:
                    entry["removed"] = False
                clean_reaps.append(entry)

    results: list[dict[str, Any]] = []
    if deliveries:
        results.append({"task_worktree_delivery": deliveries})
    if clean_reaps:
        _emit_task_reap_event(task.id, clean_reaps, event_log=event_log)
        results.append({"task_worktree_reap": clean_reaps})
    return results


def reap_task_run_worktrees(
    task_id: str,
    *,
    run_ids: list[str],
    repos: list[str],
    event_log: EventLog | None = None,
) -> list[dict[str, Any]]:
    """Capture-then-reap every worktree owned by a terminal task's runs.

    Precise counterpart to :func:`reap_orphan_worktrees`: the caller (archive)
    knows exactly which runs the task owned, so nothing else is ever touched.
    """

    from .repo_context import remove_harness_worktree_for_repo

    reaped: list[dict[str, Any]] = []
    for run_id in run_ids:
        for repo in repos:
            for worktree in existing_run_worktrees(repo, task_id=task_id, run_id=run_id):
                entry: dict[str, Any] = {"worktree": worktree.name, "run_id": run_id}
                patch = worktree_patch_text(worktree)
                if patch.strip():
                    capture_dir = paths.store_root() / "wt_reaped_patches"
                    capture_dir.mkdir(parents=True, exist_ok=True)
                    capture_path = capture_dir / f"{worktree.name}.patch"
                    try:
                        capture_path.write_text(patch, encoding="utf-8", newline="")
                        entry["captured_patch"] = capture_path.name
                    except OSError:
                        entry["kept_reason"] = "capture_write_failed"
                        reaped.append(entry)
                        continue
                if remove_harness_worktree_for_repo(repo, worktree, reason="task_archived"):
                    entry["removed"] = True
                else:
                    entry["removed"] = False
                reaped.append(entry)
    if reaped:
        _emit_task_reap_event(task_id, reaped, event_log=event_log)
    return reaped


def reap_orphan_worktrees(
    *,
    min_age_seconds: int = 3600,
    event_log: EventLog | None = None,
    dry_run: bool = False,
    include_legacy_temp: bool = False,
) -> dict[str, Any]:
    """Capture-then-reap harness worktrees no open task run owns.

    Protection, in order: worktrees younger than ``min_age_seconds`` are left
    alone; every older worktree has its diff captured into
    ``wt_reaped_patches/`` (when dirty) before removal. Nothing is ever deleted
    with an uncaptured diff.
    """

    import time as _time

    from .repo_context import (
        harness_worktree_inventory,
        current_harness_worktree_base_dir,
        legacy_harness_worktree_base_dir,
        remove_orphan_worktree,
        worktree_source_root,
    )
    candidate_bases = [current_harness_worktree_base_dir()]
    if include_legacy_temp:
        candidate_bases.append(legacy_harness_worktree_base_dir())
    capture_dir = paths.store_root() / "wt_reaped_patches"
    now_ts = _time.time()
    reaped: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for worktree, candidate_base, source, unsafe_reason in harness_worktree_inventory(
        include_legacy_temp=include_legacy_temp
    ):
        entry: dict[str, Any] = {
            "worktree": worktree.name,
            "base": str(candidate_base),
            "source": source,
        }
        if unsafe_reason is not None:
            kept.append({**entry, "reason": unsafe_reason})
            continue
        try:
            resolved = worktree.resolve()
        except OSError:
            kept.append({**entry, "reason": "unresolvable"})
            continue
        try:
            age = now_ts - worktree.stat().st_mtime
        except OSError:
            age = 0.0
        if age < min_age_seconds:
            kept.append({**entry, "reason": "younger_than_min_age"})
            continue
        if worktree_source_root(worktree) is None:
            # Git no longer recognizes this directory. Remove it only when it
            # holds no files at all; otherwise keep it for a human to inspect.
            if _is_empty_husk(worktree):
                if dry_run:
                    reaped.append({**entry, "husk": True, "dry_run": True})
                    continue
                import shutil

                try:
                    shutil.rmtree(worktree)
                    reaped.append({**entry, "husk": True})
                except OSError:
                    kept.append({**entry, "reason": "husk_remove_failed"})
            else:
                kept.append({**entry, "reason": "not_a_git_worktree_with_files"})
            continue
        if dry_run:
            patch_bytes_estimate = worktree_patch_size_estimate(worktree)
            if patch_bytes_estimate > 0:
                entry["would_capture_patch"] = True
                entry["patch_bytes_estimate"] = patch_bytes_estimate
            entry["dry_run"] = True
            reaped.append(entry)
            continue
        patch = worktree_patch_text(worktree)
        if patch.strip():
            capture_path = _write_reap_patch_exclusive(
                capture_dir,
                patch,
                source=source,
                candidate_base=candidate_base,
                worktree=worktree,
            )
            if capture_path is None:
                kept.append({**entry, "reason": "capture_write_failed"})
                continue
            entry["captured_patch"] = capture_path.name
            entry["patch_bytes"] = capture_path.stat().st_size
        if remove_orphan_worktree(worktree, reason="orphan_reap"):
            reaped.append(entry)
        else:
            kept.append({**entry, "reason": "remove_failed"})
    if not dry_run:
        try:
            (event_log or EventLog()).append(
                Event(
                    ts=now(),
                    type="worktree.orphans_reaped",
                    task_id=None,
                    run_id=None,
                    persona_id=None,
                    payload={
                        "reaped_count": len(reaped),
                        "kept_count": len(kept),
                        "captured": [item["captured_patch"] for item in reaped if item.get("captured_patch")][:20],
                    },
                )
            )
        except Exception:
            pass
    return {
        "reaped": reaped,
        "kept": kept,
        "capture_dir": str(capture_dir),
        "dry_run": dry_run,
        "include_legacy_temp": include_legacy_temp,
    }


def _write_reap_patch_exclusive(
    capture_dir: Path,
    patch: str,
    *,
    source: str,
    candidate_base: Path,
    worktree: Path,
) -> Path | None:
    """Create one collision-proof capture without truncating any prior artifact."""

    try:
        capture_dir.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256(
            f"{source}|{candidate_base.resolve()}|{worktree.resolve()}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:12]
    except OSError:
        return None
    safe_name = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in worktree.name
    )[:80]
    stamp = now().strftime("%Y%m%dT%H%M%S%fZ")
    payload = patch.encode("utf-8")
    for collision in range(100):
        name = f"{source}_{safe_name}_{identity}_{stamp}_{collision:02d}.patch"
        capture_path = capture_dir / name
        try:
            descriptor = os.open(
                capture_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            continue
        except OSError:
            return None
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
            return capture_path
        except OSError:
            try:
                capture_path.unlink()
            except OSError:
                pass
            return None
    return None


def _promote_patch_to_repo(
    task: Any,
    bundle: RepoBundle,
    patch_path: Path,
    *,
    event_log: EventLog | None,
) -> dict[str, Any]:
    target_root = resolve_affected_repo_workdir(bundle.repo)
    if target_root is None or not target_root.is_dir():
        return {"status": "failed", "reason": "target_repo_unresolved", "commit": None}
    patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    touched = changed_files_from_diff(patch_text)
    if not touched:
        return {"status": "skipped", "reason": "patch_names_no_files", "commit": None}

    dirty = _dirty_paths(target_root)
    overlapping = sorted(set(touched) & dirty)
    if overlapping:
        return {
            "status": "failed",
            "reason": "target_paths_dirty",
            "commit": None,
            "paths": overlapping[:10],
        }

    check = _run_git(target_root, ["git", "apply", "--binary", "--check", str(patch_path)])
    if check.returncode != 0:
        already = _run_git(
            target_root, ["git", "apply", "--binary", "--check", "--reverse", str(patch_path)]
        )
        if already.returncode == 0:
            return {"status": "already_applied", "reason": None, "commit": None}
        three_way = _run_git(
            target_root, ["git", "apply", "--binary", "--3way", str(patch_path)]
        )
        if three_way.returncode != 0:
            return {
                "status": "failed",
                "reason": "patch_apply_failed",
                "commit": None,
                "stderr": (three_way.stderr or check.stderr or "").strip()[:400],
            }
    else:
        applied = _run_git(target_root, ["git", "apply", "--binary", str(patch_path)])
        if applied.returncode != 0:
            return {
                "status": "failed",
                "reason": "patch_apply_failed",
                "commit": None,
                "stderr": (applied.stderr or "").strip()[:400],
            }

    add = _run_git(target_root, ["git", "add", "--", *touched])
    if add.returncode != 0:
        return {
            "status": "failed",
            "reason": "git_add_failed",
            "commit": None,
            "stderr": (add.stderr or "").strip()[:400],
        }
    message = (
        f"{task.title.strip() or 'Harness delivery'}\n\n"
        f"Promoted by delivery directive from goal {task.id} ({bundle.id}).\n"
        f"Proofs: {', '.join(bundle.proof_ids[:6]) or 'none recorded'}"
    )
    commit = _run_git(target_root, ["git", "commit", "-m", message])
    if commit.returncode != 0:
        return {
            "status": "failed",
            "reason": "git_commit_failed",
            "commit": None,
            "stderr": (commit.stderr or commit.stdout or "").strip()[:400],
        }
    sha = _run_git(target_root, ["git", "rev-parse", "--short", "HEAD"]).stdout.strip()
    _emit(
        event_log,
        "bundle.promoted",
        bundle,
        {"commit": sha, "files": touched[:20], "repo": bundle.repo},
    )
    return {"status": "promoted", "reason": None, "commit": sha, "files": touched}


def _is_empty_husk(worktree: Path) -> bool:
    """True only for a directory with no regular files anywhere — a broken
    leftover that git no longer recognizes and that holds nothing to lose."""

    import os as _os

    for _root, _dirs, files in _os.walk(worktree):
        if files:
            return False
    return True


def _bundle_run_id(bundle: RepoBundle) -> str:
    """Delivering run id: live ``active_run_id`` or the delivery-time capture."""

    live = str(bundle.active_run_id or "").strip()
    if live:
        return live
    capture = getattr(bundle, "delivery_capture", None) or {}
    return str(capture.get("run_id") or "").strip()


def _synthetic_worktree_bundle(task: Any, *, repo: str, run_id: str, worktree: Path) -> RepoBundle:
    digest = hashlib.sha256(
        f"{task.id}|{repo}|{run_id}|{worktree.name}".encode("utf-8", errors="ignore")
    ).hexdigest()[:12]
    return RepoBundle(
        id=f"runwt_{digest}",
        task_id=task.id,
        repo=repo,
        owner_persona_id=None,
        state="delivered",
        title=task.title or "Task run worktree delivery",
        objective=task.description or task.title or "Promote terminal task worktree patch.",
        active_run_id=run_id,
        proof_ids=list(getattr(task, "proof_ids", None) or []),
        created_at=now(),
        updated_at=now(),
    )


def _reap_bundle_worktrees(bundle: RepoBundle, *, event_log: EventLog | None) -> dict[str, Any]:
    run_id = _bundle_run_id(bundle)
    if not run_id:
        return {"status": "kept", "reason": "bundle_has_no_active_run"}
    worktrees = existing_run_worktrees(bundle.repo, task_id=bundle.task_id, run_id=run_id)
    if not worktrees:
        return {"status": "reaped", "reason": "already_gone", "removed": []}
    removed: list[str] = []
    for worktree in worktrees:
        if remove_harness_worktree_for_repo(bundle.repo, worktree, reason="delivery_directive"):
            removed.append(str(worktree))
    status = "reaped" if len(removed) == len(worktrees) else "partial"
    _emit(event_log, "bundle.worktree_reaped", bundle, {"removed": removed, "status": status})
    return {"status": status, "reason": None, "removed": removed}


def _open_promotion_incident(
    task: Any,
    bundle: RepoBundle,
    promote_outcome: dict[str, Any],
    *,
    incident_store: Any | None,
    event_log: EventLog | None,
) -> None:
    if incident_store is None:
        from .store import IncidentStore

        incident_store = IncidentStore(event_log=event_log or EventLog())
    incident_store.open(
        Incident(
            id=f"inc_{uuid.uuid4().hex[:8]}",
            task_id=task.id,
            run_id=bundle.active_run_id,
            kind="bundle_promotion_failed",
            summary=(
                f"delivery directive could not promote {bundle.id} to {bundle.repo}: "
                f"{promote_outcome.get('reason') or 'unknown'}"
            ),
            detail_path=None,
            opened_at=now(),
            metadata={"stage_id": bundle.stage_ids[0] if bundle.stage_ids else None},
        )
    )


def _write_promotion_record(task: Any, bundle: RepoBundle, outcome: dict[str, Any]) -> None:
    from utils import atomic_json_write

    record_path = bundle_promotion_record_path(bundle.task_id, bundle.id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(
        record_path,
        {
            "schema_version": 1,
            "recorded_at": now().isoformat(),
            "task_id": task.id,
            "task_state": str(task.state),
            **outcome,
        },
    )


def _dirty_paths(root: Path) -> set[str]:
    status = _run_git(root, ["git", "status", "--short", "--untracked-files=all"])
    dirty: set[str] = set()
    for line in (status.stdout or "").splitlines():
        raw = line[3:].strip() if len(line) > 3 else line.strip()
        for part in raw.split(" -> "):
            part = part.strip().strip('"')
            if part:
                dirty.add(part.replace("\\", "/"))
    return dirty


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )


def _emit_task_reap_event(task_id: str, reaped: list[dict[str, Any]], *, event_log: EventLog | None) -> None:
    try:
        (event_log or EventLog()).append(
            Event(
                ts=now(),
                type="worktree.task_reaped",
                task_id=task_id,
                run_id=None,
                persona_id=None,
                payload={
                    "count": len(reaped),
                    "removed": len([item for item in reaped if item.get("removed")]),
                },
            )
        )
    except Exception:
        pass


def _emit(event_log: EventLog | None, event_type: str, bundle: RepoBundle, payload: dict[str, Any]) -> None:
    try:
        (event_log or EventLog()).append(
            Event(
                ts=now(),
                type=event_type,
                task_id=bundle.task_id,
                run_id=bundle.active_run_id,
                persona_id=None,
                payload={"bundle_id": bundle.id, **payload},
            )
        )
    except Exception:
        pass
