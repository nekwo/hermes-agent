from __future__ import annotations

from datetime import timedelta
from typing import Any
import uuid

from hermes_time import now

from .events import EventLog
from .models import Event, Incident
from .dirty_state import build_dirty_state
from .launcher_process_hygiene import clean_launcher_visual_processes
from .persona_assignments import PersonaInstanceStore
from .runtime_instances import GoalRuntimeInstanceStore, runtime_instances_summary
from .states import RunState, TaskState
from .store import ACTIVE_RUN_STATES, IncidentStore, RunStore, TaskStore
from .worker_sessions import WorkerSessionStore


def _mark_stale_runs(
    run_store: RunStore,
    incident_store: IncidentStore,
    *,
    heartbeat_ttl_seconds: int,
) -> list[str]:
    """Preserve hygiene while the mission dispatcher itself is retired."""

    opened: list[str] = []
    for run in run_store.find_stale(heartbeat_ttl_seconds=heartbeat_ttl_seconds):
        run.state = RunState.STALE
        run.finished_at = now()
        run_store.update(run)
        existing = [
            incident
            for incident in incident_store.list_open()
            if incident.run_id == run.id and incident.kind == "stale_run"
        ]
        if existing:
            continue
        incident = Incident(
            id=f"inc_{uuid.uuid4().hex[:8]}",
            task_id=run.task_id,
            run_id=run.id,
            kind="stale_run",
            summary="Run heartbeat exceeded TTL",
            detail_path=None,
            opened_at=now(),
        )
        incident_store.open(incident)
        opened.append(incident.id)
    return opened


def prepare_new_goal_runtime(
    *,
    task_store: TaskStore | None = None,
    run_store: RunStore | None = None,
    incident_store: IncidentStore | None = None,
    worker_session_store: WorkerSessionStore | None = None,
    cleanup_stage47_temp: bool = False,
    cleanup_launcher_visual_processes: bool = False,
    heartbeat_ttl_seconds: int = 300,
    foreground_mode: bool = False,
    park_open_tasks: bool = False,
    exclude_task_ids: set[str] | None = None,
    preempt_background_runs: bool = False,
) -> dict[str, Any]:
    """Clean Harness-owned temporary state before creating a new goal.

    This never deletes evidence and never modifies product repositories. It
    marks stale runs, cancels orphan active runs, and optionally cancels
    Stage-47 burn-in temp tasks/runs so repeated certification attempts start
    from a deterministic Harness slate.
    """

    task_store = task_store or TaskStore()
    run_store = run_store or RunStore()
    incident_store = incident_store or IncidentStore()
    worker_session_store = worker_session_store or WorkerSessionStore()
    event_log = getattr(task_store, "event_log", None) or EventLog()
    runtime_store = GoalRuntimeInstanceStore(event_log=event_log)
    persona_instance_store = PersonaInstanceStore(event_log=event_log)
    exclude_task_ids = {str(item) for item in (exclude_task_ids or set()) if str(item).strip()}
    launcher_process_cleanup = clean_launcher_visual_processes(enabled=cleanup_launcher_visual_processes)
    stale_incidents = _mark_stale_runs(run_store, incident_store, heartbeat_ttl_seconds=heartbeat_ttl_seconds)
    worker_cleanup = worker_session_store.close_for_new_goal(reason="new goal hygiene")
    persona_instance_cleanup = persona_instance_store.sweep_orphaned_task_bound_instances(
        reason="new goal hygiene",
    )
    cancelled_runs: list[str] = []
    cancelled_tasks: list[str] = []
    parked_task_ids: list[str] = []
    blocking_active_run_ids: list[str] = []
    tasks = task_store.list_all()
    tasks_by_id = {task.id: task for task in tasks}

    for run in list(run_store.find_active()):
        task = tasks_by_id.get(run.task_id)
        if task is None or task.state in {TaskState.DONE, TaskState.CANCELLED}:
            run_store.cancel(run.id, reason="new goal hygiene cancelled orphan active run")
            cancelled_runs.append(run.id)
            continue
        if foreground_mode and run.task_id not in exclude_task_ids:
            if _run_is_stale(run, heartbeat_ttl_seconds=heartbeat_ttl_seconds):
                run_store.cancel(run.id, reason="foreground runtime cancelled stale foreign active run")
                cancelled_runs.append(run.id)
                _append_runtime_event(
                    event_log,
                    "foreground_runtime.cancelled_stale_run",
                    task_id=run.task_id,
                    run_id=run.id,
                    reason="stale foreign active run blocked foreground goal",
                )
            elif preempt_background_runs and _runtime_lane_for_task(runtime_store, run.task_id) == "background":
                run_store.cancel(run.id, reason="foreground runtime preempted background active run")
                cancelled_runs.append(run.id)
                _append_runtime_event(
                    event_log,
                    "foreground_runtime.preempted_background_run",
                    task_id=run.task_id,
                    run_id=run.id,
                    reason="background active run preempted by foreground goal",
                )
            else:
                blocking_active_run_ids.append(run.id)
                _append_runtime_event(
                    event_log,
                    "foreground_runtime.waiting_on_fresh_run",
                    task_id=run.task_id,
                    run_id=run.id,
                    reason="fresh active run belongs to another task",
                )

    # Lane-only runtime: new goals no longer park preserved open goals.

    if cleanup_stage47_temp:
        for task in task_store.list_open():
            if not _is_stage47_temp_task(task):
                continue
            for run in run_store.list_for_task(task.id):
                if run.state in ACTIVE_RUN_STATES:
                    run_store.cancel(run.id, reason="new burn-in cancelled previous Stage 47 temp run")
                    cancelled_runs.append(run.id)
            task_store.cancel(task.id, reason="new burn-in cancelled previous Stage 47 temp task", actor="harness")
            cancelled_tasks.append(task.id)

    after_tasks = task_store.list_all()
    after_runs = run_store.list_all()
    after_incidents = incident_store.list_all()
    after_workers = worker_session_store.list_all()
    runtime_instances = runtime_store.list_all()
    foreground_summary = _foreground_dirty_summary(
        runtime_instances=runtime_instances,
        runs=after_runs,
        history_open_tasks=[task for task in after_tasks if task.state not in {TaskState.DONE, TaskState.CANCELLED}],
        blocking_active_run_ids=blocking_active_run_ids,
    )
    if foreground_mode:
        event_log.append(
            Event(
                ts=now(),
                type="foreground_runtime.prepared",
                task_id=None,
                run_id=None,
                persona_id=None,
                payload={
                    "state": "prepared",
                    "foreground_task_id": foreground_summary.get("foreground_task_id"),
                    "parked_task_count": len(_dedupe(parked_task_ids)),
                    "blocking_active_run_ids": _dedupe(blocking_active_run_ids),
                },
            )
        )
    return {
        "cleanup_stage47_temp": cleanup_stage47_temp,
        "cleanup_launcher_visual_processes": cleanup_launcher_visual_processes,
        "launcher_visual_process_cleanup": launcher_process_cleanup,
        "stale_incident_ids": stale_incidents,
        "cancelled_run_ids": _dedupe(cancelled_runs),
        "cancelled_task_ids": _dedupe(cancelled_tasks),
        "parked_open_task_ids": _dedupe(parked_task_ids),
        "blocking_active_run_ids": _dedupe(blocking_active_run_ids),
        "closed_worker_session_ids": worker_cleanup["closed_worker_session_ids"],
        "expired_possession_worker_session_ids": worker_cleanup["expired_possession_worker_session_ids"],
        "proof_sandbox_readonly_markers": worker_cleanup["proof_sandbox_readonly_markers"],
        "persona_instance_cleanup": persona_instance_cleanup,
        "dirty_state_after_cleanup": build_dirty_state(tasks=after_tasks, runs=after_runs, incidents=after_incidents, workers=after_workers, runtime_instances=runtime_instances),
        "foreground_runtime": foreground_summary,
        "runtime_instances": runtime_instances_summary(runtime_instances),
        "preserved_evidence": True,
        "product_repos_modified": False,
    }


def activate_foreground_runtime(
    task_id: str,
    *,
    started_by: str = "cli",
    runtime_store: GoalRuntimeInstanceStore | None = None,
) -> dict[str, Any]:
    runtime_store = runtime_store or GoalRuntimeInstanceStore()
    existing = runtime_store.active_for_task(task_id)
    if existing:
        instance = runtime_store.transition(existing.id, "running", reason="reactivated existing lane", parked_reason=None)
    else:
        instance = runtime_store.create_lane(task_id=task_id, started_by=started_by, state="running")
    return {
        "instance_id": instance.id,
        "target_task_id": instance.task_id,
        "queue_mode": "lane",
        "lane_id": instance.lane,
        "state": instance.state,
        "parked_open_task_ids": [],
    }


def repo_clean_baseline_from_hygiene(hygiene: dict | None) -> dict[str, Any]:
    """Redaction-safe repo dirty baseline captured at goal creation.

    Preflight's repo_clean check passes when the dirty set is unchanged from this
    baseline, so pre-existing operator edits never block a fresh goal.
    """
    dirty_state = hygiene.get("dirty_state_after_cleanup") if isinstance(hygiene, dict) else None
    repos = dirty_state.get("repos") if isinstance(dirty_state, dict) else None
    if not isinstance(repos, list):
        return {"repos": []}
    safe_repos = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        safe_repos.append(
            {
                "label": str(repo.get("label") or "")[:80],
                "dirty": bool(repo.get("dirty")),
                "dirty_count": int(repo.get("dirty_count") or 0),
                "error": str(repo.get("error") or "")[:80] or None,
                "status_excerpt": [str(item)[:180] for item in list(repo.get("status_excerpt") or [])[:20]],
            }
        )
    return {"created_at": now().isoformat(), "repos": safe_repos}


def _is_stage47_temp_task(task) -> bool:
    return str(getattr(task, "requested_by", "") or "") == "stage47_burn_in" or str(getattr(task, "id", "") or "").startswith("task_burn_")


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _run_is_stale(run, *, heartbeat_ttl_seconds: int) -> bool:
    heartbeat = getattr(run, "last_heartbeat_at", None)
    if heartbeat is None:
        return False
    return heartbeat < now() - timedelta(seconds=max(1, int(heartbeat_ttl_seconds or 1)))


def _runtime_lane_for_task(runtime_store: GoalRuntimeInstanceStore, task_id: str) -> str | None:
    instance = runtime_store.latest_for_task(task_id)
    return getattr(instance, "lane", None) if instance else None


def _append_runtime_event(event_log: EventLog, event_type: str, *, task_id: str, run_id: str, reason: str) -> None:
    event_log.append(
        Event(
            ts=now(),
            type=event_type,
            task_id=task_id,
            run_id=run_id,
            persona_id=None,
            payload={"run_id": run_id, "task_id": task_id, "reason": reason[:160]},
        )
    )


def _foreground_dirty_summary(*, runtime_instances, runs, history_open_tasks, blocking_active_run_ids: list[str]) -> dict[str, Any]:
    active_lane_task_ids = {
        getattr(instance, "task_id", None)
        for instance in runtime_instances
        if str(getattr(instance, "state", "") or "") in {"queued", "activating", "running", "active", "waiting"}
    }
    foreground_active_runs = [
        run.id for run in runs if run.state in ACTIVE_RUN_STATES and getattr(run, "task_id", None) in active_lane_task_ids
    ]
    foreground_clean = not foreground_active_runs and not blocking_active_run_ids
    return {
        "foreground_task_id": None,
        "foreground_runtime_instance_id": None,
        "foreground_clean": foreground_clean,
        "foreground_active_runs": len(foreground_active_runs),
        "foreground_active_run_ids": foreground_active_runs,
        "background_open_tasks": 0,
        "background_task_ids": [],
        "blocking_active_run_ids": _dedupe(blocking_active_run_ids),
        "history": {
            "open_preserved_task_count": len(history_open_tasks),
            "open_preserved_task_ids": [task.id for task in history_open_tasks[:25]],
        },
    }
