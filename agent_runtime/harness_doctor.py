from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hermes_time import now

from .delivery_directive import reap_orphan_worktrees
from .events import EventLog
from .models import Event, Incident, Task
from .snapshot import build_snapshot
from .states import RunState, TaskState, WorkerSessionState
from .store import ACTIVE_RUN_STATES, IncidentStore, RunStore, TaskStore
from .worker_sessions import ACTIVE_WORKER_STATES, WorkerSessionStore


DEFAULT_STALE_RUN_HOURS = 6
DEFAULT_STALE_WORKER_HOURS = 6
DEFAULT_STALE_TASK_DAYS = 2
DEFAULT_WORKTREE_MIN_AGE_SECONDS = 3600


def run_harness_doctor(
    *,
    fix: bool = False,
    dry_run: bool = False,
    stale_run_hours: int = DEFAULT_STALE_RUN_HOURS,
    stale_worker_hours: int = DEFAULT_STALE_WORKER_HOURS,
    stale_task_days: int = DEFAULT_STALE_TASK_DAYS,
    worktree_min_age_seconds: int = DEFAULT_WORKTREE_MIN_AGE_SECONDS,
    include_worktrees: bool = True,
    task_store: TaskStore | None = None,
    run_store: RunStore | None = None,
    worker_store: WorkerSessionStore | None = None,
    incident_store: IncidentStore | None = None,
    event_log: EventLog | None = None,
    snapshot_builder: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Report and optionally repair stale Harness runtime state.

    Read-only mode performs no writes. Repair mode preserves evidence by
    terminal-closing runs/workers, blocking idle tasks with an incident, and
    delegating worktree cleanup to the capture-then-reap path.
    """

    ref = now()
    task_store = task_store or TaskStore()
    run_store = run_store or RunStore()
    worker_store = worker_store or WorkerSessionStore()
    incident_store = incident_store or IncidentStore()
    event_log = event_log or EventLog()
    snapshot_builder = snapshot_builder or build_snapshot

    stale_runs = _stale_runs(run_store, ref=ref, stale_hours=stale_run_hours)
    stale_workers = _stale_workers(worker_store, ref=ref, stale_hours=stale_worker_hours)
    stale_tasks = _stale_open_tasks(task_store, run_store, ref=ref, stale_days=stale_task_days)
    if include_worktrees:
        worktrees = reap_orphan_worktrees(
            min_age_seconds=max(0, int(worktree_min_age_seconds or 0)),
            event_log=event_log,
            dry_run=not fix or dry_run,
        )
    else:
        worktrees = {"reaped": [], "kept": [], "dry_run": True, "skipped": "worktree_scan_disabled"}
    snapshot_defects = _snapshot_null_id_defects(snapshot_builder)

    repairs = {
        "stale_run_ids": [],
        "stale_run_incident_ids": [],
        "closed_worker_session_ids": [],
        "blocked_task_ids": [],
        "blocked_task_incident_ids": [],
        "worktrees_reaped": [] if not fix or dry_run else [item.get("worktree") for item in worktrees.get("reaped", []) if item.get("worktree")],
        "dry_run": bool(dry_run),
    }
    if fix:
        if dry_run:
            repairs["stale_run_ids"] = [item["run_id"] for item in stale_runs]
            repairs["closed_worker_session_ids"] = [item["worker_session_id"] for item in stale_workers]
            repairs["blocked_task_ids"] = [item["task_id"] for item in stale_tasks]
        else:
            repairs.update(
                _repair_stale_runtime(
                    stale_runs=stale_runs,
                    stale_workers=stale_workers,
                    stale_tasks=stale_tasks,
                    run_store=run_store,
                    worker_store=worker_store,
                    task_store=task_store,
                    incident_store=incident_store,
                )
            )
            _emit_doctor_fix_event(event_log, repairs)

    finding_counts = {
        "stale_runs": len(stale_runs),
        "stale_workers": len(stale_workers),
        "stale_open_tasks": len(stale_tasks),
        "orphan_worktrees": len(worktrees.get("reaped") or []),
        "snapshot_null_id_rows": len(snapshot_defects),
    }
    return {
        "schema_version": 1,
        "generated_at": ref,
        "ok": True,
        "mode": {"fix": bool(fix), "dry_run": bool(dry_run)},
        "thresholds": {
            "stale_run_hours": int(stale_run_hours),
            "stale_worker_hours": int(stale_worker_hours),
            "stale_task_days": int(stale_task_days),
            "worktree_min_age_seconds": int(worktree_min_age_seconds),
            "include_worktrees": bool(include_worktrees),
        },
        "summary": {
            "finding_counts": finding_counts,
            "needs_fix": any(finding_counts.values()),
            "repairs_applied": bool(fix and not dry_run),
            "preserved_evidence": True,
            "product_repos_modified": False,
        },
        "findings": {
            "stale_runs": stale_runs,
            "stale_workers": stale_workers,
            "stale_open_tasks": stale_tasks,
            "orphan_worktrees": worktrees,
            "snapshot_null_id_rows": snapshot_defects,
        },
        "repairs": repairs,
    }


def _stale_runs(run_store: RunStore, *, ref: datetime, stale_hours: int) -> list[dict[str, Any]]:
    cutoff = ref - timedelta(hours=max(1, int(stale_hours or 1)))
    findings: list[dict[str, Any]] = []
    for run in run_store.list_all():
        if run.state not in ACTIVE_RUN_STATES or run.state == RunState.WAITING_ON_APPROVAL:
            continue
        heartbeat = _aware(getattr(run, "last_heartbeat_at", None))
        if heartbeat is None or heartbeat >= cutoff:
            continue
        findings.append(
            {
                "run_id": run.id,
                "task_id": run.task_id,
                "persona_id": run.persona_id,
                "state": run.state.value,
                "heartbeat_age_seconds": _age_seconds(ref, heartbeat),
            }
        )
    return findings


def _stale_workers(worker_store: WorkerSessionStore, *, ref: datetime, stale_hours: int) -> list[dict[str, Any]]:
    cutoff = ref - timedelta(hours=max(1, int(stale_hours or 1)))
    findings: list[dict[str, Any]] = []
    for worker in worker_store.list_all():
        if worker.state not in ACTIVE_WORKER_STATES:
            continue
        heartbeat = _aware(getattr(worker, "last_heartbeat_at", None))
        if heartbeat is None or heartbeat >= cutoff:
            continue
        findings.append(
            {
                "worker_session_id": worker.id,
                "task_id": worker.task_id,
                "persona_id": worker.persona_id,
                "state": worker.state.value,
                "heartbeat_age_seconds": _age_seconds(ref, heartbeat),
            }
        )
    return findings


def _stale_open_tasks(
    task_store: TaskStore,
    run_store: RunStore,
    *,
    ref: datetime,
    stale_days: int,
) -> list[dict[str, Any]]:
    cutoff = ref - timedelta(days=max(1, int(stale_days or 1)))
    active_task_ids = {run.task_id for run in run_store.find_active()}
    findings: list[dict[str, Any]] = []
    for task in task_store.list_open():
        if task.state == TaskState.BLOCKED or task.id in active_task_ids:
            continue
        updated_at = _aware(getattr(task, "updated_at", None))
        if updated_at is None or updated_at >= cutoff:
            continue
        findings.append(
            {
                "task_id": task.id,
                "state": task.state.value,
                "updated_age_seconds": _age_seconds(ref, updated_at),
                "open_incident_ids": list(getattr(task, "open_incident_ids", []) or []),
            }
        )
    return findings


def _repair_stale_runtime(
    *,
    stale_runs: list[dict[str, Any]],
    stale_workers: list[dict[str, Any]],
    stale_tasks: list[dict[str, Any]],
    run_store: RunStore,
    worker_store: WorkerSessionStore,
    task_store: TaskStore,
    incident_store: IncidentStore,
) -> dict[str, Any]:
    repairs = {
        "stale_run_ids": [],
        "stale_run_incident_ids": [],
        "closed_worker_session_ids": [],
        "blocked_task_ids": [],
        "blocked_task_incident_ids": [],
    }
    for item in stale_runs:
        run_id = item["run_id"]
        try:
            run = run_store.close_run(
                run_id,
                state=RunState.STALE,
                error={
                    "type": "stale_run",
                    "summary": "Run heartbeat exceeded Harness doctor threshold.",
                },
            )
        except Exception:
            continue
        repairs["stale_run_ids"].append(run.id)
        incident = _open_or_get_incident(
            incident_store,
            task_id=run.task_id,
            run_id=run.id,
            kind="stale_run",
            summary="Run heartbeat exceeded Harness doctor threshold.",
        )
        repairs["stale_run_incident_ids"].append(incident.id)
        _link_task_incident(task_store, run.task_id, incident.id)
    for item in stale_workers:
        try:
            worker = worker_store.close(
                item["worker_session_id"],
                reason="Harness doctor closed stale worker heartbeat",
                state=WorkerSessionState.CLOSED,
            )
        except Exception:
            continue
        repairs["closed_worker_session_ids"].append(worker.id)
    for item in stale_tasks:
        task_id = item["task_id"]
        incident = _open_or_get_incident(
            incident_store,
            task_id=task_id,
            run_id=None,
            kind="stale_open_task",
            summary="Open task idled past Harness doctor threshold.",
        )
        repairs["blocked_task_incident_ids"].append(incident.id)
        try:
            task = task_store.get(task_id)
            if incident.id not in (task.open_incident_ids or []):
                task.open_incident_ids.append(incident.id)
            if task.state != TaskState.BLOCKED:
                task.state = TaskState.BLOCKED
                task.updated_at = now()
                task_store.update(
                    task,
                    actor="doctor",
                    reason="open task idle exceeded Harness doctor threshold",
                )
            else:
                task_store.update(task, actor="doctor", reason="doctor linked stale task incident")
            repairs["blocked_task_ids"].append(task.id)
        except Exception:
            continue
    return repairs


def _open_or_get_incident(
    incident_store: IncidentStore,
    *,
    task_id: str | None,
    run_id: str | None,
    kind: str,
    summary: str,
) -> Incident:
    for incident in incident_store.list_open():
        if incident.task_id == task_id and incident.run_id == run_id and incident.kind == kind:
            return incident
    return incident_store.open(
        Incident(
            id=f"inc_{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            run_id=run_id,
            kind=kind,
            summary=summary,
            detail_path=None,
            opened_at=now(),
            metadata={"source": "harness_doctor"},
        )
    )


def _link_task_incident(task_store: TaskStore, task_id: str | None, incident_id: str) -> None:
    if not task_id:
        return
    try:
        task = task_store.get(task_id)
    except Exception:
        return
    if incident_id not in (task.open_incident_ids or []):
        task.open_incident_ids.append(incident_id)
        task.updated_at = now()
        task_store.update(task, actor="doctor", reason="doctor linked stale run incident")


def _snapshot_null_id_defects(snapshot_builder: Callable[[], dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        snapshot = snapshot_builder()
    except Exception as exc:
        return [{"collection": "snapshot", "index": None, "id_key": None, "reason": type(exc).__name__}]
    expected = {
        "tasks": "id",
        "goals": "id",
        "agents": "persona_id",
        "runs": "id",
        "worker_sessions": "worker_session_id",
        "persona_instances": "persona_instance_id",
        "repo_bundles": "id",
        "runtime_instances": "id",
    }
    defects: list[dict[str, Any]] = []
    for collection, id_key in expected.items():
        rows = snapshot.get(collection)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            if row.get(id_key):
                continue
            defects.append({"collection": collection, "index": index, "id_key": id_key})
    return defects


def _emit_doctor_fix_event(event_log: EventLog, repairs: dict[str, Any]) -> None:
    try:
        event_log.append(
            Event(
                ts=now(),
                type="doctor.fixed",
                task_id=None,
                run_id=None,
                persona_id=None,
                payload={
                    "stale_runs": len(repairs.get("stale_run_ids") or []),
                    "stale_workers": len(repairs.get("closed_worker_session_ids") or []),
                    "stale_tasks": len(repairs.get("blocked_task_ids") or []),
                    "worktrees": len(repairs.get("worktrees_reaped") or []),
                },
            )
        )
    except Exception:
        pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _age_seconds(ref: datetime, then: datetime) -> int:
    return int(max(0.0, (_aware(ref) - _aware(then)).total_seconds()))
