from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from .repo_context import resolve_affected_repo_workdir, safe_affected_repo_labels
from .states import RunState
from .store import ACTIVE_RUN_STATES


DEFAULT_DIRTY_REPOS = ("EterniaBackend", "EterniaLauncher", "hermes-agent")
_MAX_STATUS_LINES = 10
_MAX_STATUS_LINE_CHARS = 180


def build_dirty_state(*, runs=None, incidents=None, repos=None, runtime_instances=None) -> dict[str, Any]:
    """Return a redaction-safe dirty-state indicator for Mission Control.

    Runtime dirtiness means the Harness still has open work or active/stale run
    state. Repo dirtiness means a known target worktree has pre-existing git
    changes; it is reported but never cleaned by the Harness.

    S56 removed the ``workers`` parameter and the four rows it alone fed
    (``active_worker_sessions`` / ``possessed_worker_sessions`` and their two id
    lists), plus the ``workers=`` arm of the summary line. With the
    WorkerSessionStore write lane gone nothing can put a worker into an ACTIVE
    or POSSESSED state, so all four were constants by construction and could
    never contribute to ``dirty``.
    """

    runs = list(runs or [])
    incidents = list(incidents or [])
    runtime = _runtime_dirty_state(runs=runs, incidents=incidents, runtime_instances=list(runtime_instances or []))
    repo_items = repo_dirty_states(repos or DEFAULT_DIRTY_REPOS)
    repo_dirty = any(item.get("dirty") or item.get("error") for item in repo_items)
    runtime_dirty = bool(runtime["dirty"])
    dirty = runtime_dirty or repo_dirty
    return {
        "dirty": dirty,
        "summary": _summary(runtime=runtime, repos=repo_items),
        "runtime": runtime,
        "repos": repo_items,
    }


def repo_dirty_states(repos) -> list[dict[str, Any]]:
    seen: set[str] = set()
    states: list[dict[str, Any]] = []
    for raw_repo in repos or []:
        label = (safe_affected_repo_labels([str(raw_repo)])[0] if str(raw_repo).strip() else "repo")[:80]
        resolved = resolve_affected_repo_workdir(str(raw_repo))
        key = str(resolved.resolve()).lower() if resolved is not None else f"unresolved:{label.lower()}"
        if key in seen:
            continue
        seen.add(key)
        if resolved is None:
            states.append(
                {
                    "label": label,
                    "resolved": False,
                    "dirty": True,
                    "dirty_count": 0,
                    "status_excerpt": [],
                    "error": "repo_unresolved",
                    "message": "Repository scope could not be resolved; Harness cannot prove it is clean.",
                }
            )
            continue
        states.append(_git_dirty_state(label=label, root=resolved))
    return states


# S54 removed ``no_product_edit_dirty_check``. ``build_dirty_state`` is the live
# projection; this per-task variant lost its caller with the mission lane.

def _runtime_dirty_state(*, runs, incidents, runtime_instances=None) -> dict[str, Any]:
    active_runs = [run for run in runs if _run_state(run) in ACTIVE_RUN_STATES]
    stale_runs = [run for run in runs if _run_state(run) == RunState.STALE]
    open_incidents = [incident for incident in incidents if getattr(incident, "closed_at", None) is None]
    foreground = _foreground_runtime_state(runtime_instances or [], active_runs=active_runs)
    dirty = bool(active_runs or open_incidents or stale_runs)
    return {
        "dirty": dirty,
        "foreground": foreground,
        "active_runs": len(active_runs),
        "stale_runs": len(stale_runs),
        "open_incidents": len(open_incidents),
        "active_run_ids": _ids(active_runs),
        "stale_run_ids": _ids(stale_runs),
    }


def _git_dirty_state(*, label: str, root: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--short", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return {
            "label": label,
            "resolved": True,
            "dirty": True,
            "dirty_count": 0,
            "status_excerpt": [],
            "error": "git_status_failed",
            "message": "git status could not complete; Harness cannot prove this repository is clean.",
        }
    if completed.returncode != 0:
        return {
            "label": label,
            "resolved": True,
            "dirty": True,
            "dirty_count": 0,
            "status_excerpt": [],
            "error": "git_status_nonzero",
            "message": "git status returned non-zero; Harness cannot prove this repository is clean.",
        }
    lines = [_safe_status_line(line) for line in completed.stdout.splitlines() if line.strip()]
    return {
        "label": label,
        "resolved": True,
        "dirty": bool(lines),
        "dirty_count": len(lines),
        "status_excerpt": lines[:_MAX_STATUS_LINES],
        "error": None,
        "message": "clean" if not lines else f"{len(lines)} dirty path(s)",
    }


def _summary(*, runtime: dict[str, Any], repos: list[dict[str, Any]]) -> str:
    repo_dirty = [repo for repo in repos if repo.get("dirty") or repo.get("error")]
    parts: list[str] = []
    if runtime.get("active_runs"):
        parts.append(f"runtime={runtime.get('active_runs', 0)} active run(s)")
    if runtime.get("open_incidents"):
        parts.append(f"incidents={runtime.get('open_incidents')} open")
    if repo_dirty:
        labels = ", ".join(repo["label"] for repo in repo_dirty[:4])
        extra = "" if len(repo_dirty) <= 4 else f" +{len(repo_dirty) - 4}"
        parts.append(f"repo_dirty={labels}{extra}")
    return "; ".join(parts) if parts else "clean"


def _foreground_runtime_state(runtime_instances, *, active_runs) -> dict[str, Any]:
    foreground = None
    for instance in runtime_instances:
        if str(getattr(instance, "lane", "")) == "foreground" and str(getattr(instance, "state", "")) in {"active", "waiting"}:
            foreground = instance
    foreground_task_id = str(getattr(foreground, "task_id", "") or "") if foreground else None
    foreground_active_runs = [run for run in active_runs if str(getattr(run, "task_id", "") or "") == foreground_task_id]
    return {
        "foreground_task_id": foreground_task_id,
        "foreground_runtime_instance_id": str(getattr(foreground, "id", "") or "") if foreground else None,
        "foreground_active_runs": len(foreground_active_runs),
        "foreground_active_run_ids": _ids(foreground_active_runs),
        "foreground_clean": not foreground_active_runs,
    }


def _run_state(run) -> RunState:
    state = getattr(run, "state", None)
    return state if isinstance(state, RunState) else RunState(state)


def _ids(items) -> list[str]:
    return [str(getattr(item, "id", "")) for item in items if getattr(item, "id", None)][:20]


def _safe_status_line(line: str) -> str:
    text = re.sub(r"[\x00-\x1f]+", " ", str(line or "")).strip().replace("\\", "/")
    if re.search(r"[A-Za-z]:/", text):
        text = re.sub(r"[A-Za-z]:/[^ ]+", "<path-withheld>", text)
    return text[:_MAX_STATUS_LINE_CHARS]
