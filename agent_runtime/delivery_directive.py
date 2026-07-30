"""Orphan-worktree janitor and the historical bundle-promotion read surface.

This module used to carry a second, larger half: a declarative delivery
directive DECLARED on a goal, stored on the ``Task``, and executed at terminal
settle by ``ArchiveStore.archive_tasks``. The goal/task lane, its request field,
and that choke point were all removed with the mission lane (S4-S12); S24 swept
the executors, the declaration path, and the delivery-time patch capture that
had no producer left. ``docs/agent-runtime-harness/delivery-directive.md``
records what the contract was and what survived.

What remains has live callers:

* :func:`reap_orphan_worktrees` — capture-then-reap for harness worktrees no
  open run owns, driven by ``hermes harness worktree reap`` and
  ``harness doctor --fix``. Nothing is deleted with an uncaptured diff: dirty
  candidates are written to ``<store_root>/wt_reaped_patches/`` first, under
  collision-proof exclusive-create names.
* :func:`read_bundle_promotion_record` — reads the promotion records the removed
  executor used to write, so ``repo_bundles.repo_bundle_summary`` can still
  label historical bundles honestly for ``status.py``. It can only ever describe
  the past; nothing writes new records.
"""

from __future__ import annotations

import os
import hashlib
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
from .events import EventLog
from .models import Event
from .repo_context import (
    worktree_patch_size_estimate,
    worktree_patch_text,
)


def bundle_promotion_record_path(task_id: str, bundle_id: str) -> Path:
    return paths.repo_bundles_task_dir(task_id) / f"{bundle_id}.promotion.json"


def read_bundle_promotion_record(task_id: str, bundle_id: str) -> dict[str, Any] | None:
    """Recorded outcome of a historical delivery-directive execution.

    The writer went with the terminal-settle executor; this stays because bundle
    summaries must keep describing records already on disk instead of silently
    relabelling them as never-promoted.
    """

    import json

    record_path = bundle_promotion_record_path(task_id, bundle_id)
    if not record_path.is_file():
        return None
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


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


def _is_empty_husk(worktree: Path) -> bool:
    """True only for a directory with no regular files anywhere — a broken
    leftover that git no longer recognizes and that holds nothing to lose."""

    import os as _os

    for _root, _dirs, files in _os.walk(worktree):
        if files:
            return False
    return True
