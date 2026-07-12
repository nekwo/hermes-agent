from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from hermes_time import now

from .delivery_directive import reap_orphan_worktrees
from .errors import NotFound
from .events import EventLog, compact_archived_task_events, event_log_health
from .models import Event, Incident, Task
from .snapshot import build_snapshot
from .states import RunState, TaskState, WorkerSessionState
from .store import ACTIVE_RUN_STATES, IncidentStore, RunStore, TaskStore
from .worker_sessions import ACTIVE_WORKER_STATES, WorkerSessionStore


DEFAULT_STALE_RUN_HOURS = 6
DEFAULT_STALE_WORKER_HOURS = 6
DEFAULT_STALE_TASK_DAYS = 2
DEFAULT_STALE_INCIDENT_DAYS = 7
DEFAULT_STALE_INCIDENT_HOURS = DEFAULT_STALE_INCIDENT_DAYS * 24
DEFAULT_WORKTREE_MIN_AGE_SECONDS = 3600
STALE_BULK_CLOSE_REASON = "ttl_expired"


def run_harness_doctor(
    *,
    fix: bool = False,
    dry_run: bool = False,
    stale_run_hours: int = DEFAULT_STALE_RUN_HOURS,
    stale_worker_hours: int = DEFAULT_STALE_WORKER_HOURS,
    stale_task_days: int = DEFAULT_STALE_TASK_DAYS,
    stale_incident_hours: int = DEFAULT_STALE_INCIDENT_HOURS,
    stale_incident_days: int | None = None,
    worktree_min_age_seconds: int = DEFAULT_WORKTREE_MIN_AGE_SECONDS,
    include_worktrees: bool = True,
    compact_events: bool = False,
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
    stale_incident_hours = _stale_incident_hours(stale_incident_hours, stale_incident_days)
    stale_incidents = _stale_incidents(incident_store, task_store, ref=ref, stale_hours=stale_incident_hours)
    if include_worktrees:
        worktrees = reap_orphan_worktrees(
            min_age_seconds=max(0, int(worktree_min_age_seconds or 0)),
            event_log=event_log,
            dry_run=not fix or dry_run,
        )
    else:
        worktrees = {"reaped": [], "kept": [], "dry_run": True, "skipped": "worktree_scan_disabled"}
    snapshot_defects = _snapshot_null_id_defects(snapshot_builder)
    event_health = event_log_health()
    event_compaction = _skipped_event_compaction(event_health)

    repairs = {
        "stale_run_ids": [],
        "stale_run_incident_ids": [],
        "closed_worker_session_ids": [],
        "blocked_task_ids": [],
        "blocked_task_incident_ids": [],
        "archived_task_ids": [],
        "waiting_for_operator_task_ids": [],
        "waiting_for_operator_incident_ids": [],
        "closed_incident_ids": [],
        "closed_incident_count_by_kind": {},
        "incident_sweep_reason": STALE_BULK_CLOSE_REASON,
        "worktrees_reaped": [] if not fix or dry_run else [item.get("worktree") for item in worktrees.get("reaped", []) if item.get("worktree")],
        "event_log_compaction": event_compaction,
        "dry_run": bool(dry_run),
    }
    if fix:
        if dry_run:
            repairs["stale_run_ids"] = [item["run_id"] for item in stale_runs]
            repairs["closed_worker_session_ids"] = [item["worker_session_id"] for item in stale_workers]
            repairs["blocked_task_ids"] = [item["task_id"] for item in stale_tasks]
            repairs["archived_task_ids"] = [item["task_id"] for item in stale_tasks]
            repairs["closed_incident_ids"] = [item["incident_id"] for item in stale_incidents]
            repairs["closed_incident_count_by_kind"] = _count_by_key(stale_incidents, "kind")
        else:
            repairs.update(
                _repair_stale_runtime(
                    stale_runs=stale_runs,
                    stale_workers=stale_workers,
                    stale_tasks=stale_tasks,
                    stale_incidents=stale_incidents,
                    run_store=run_store,
                    worker_store=worker_store,
                    task_store=task_store,
                    incident_store=incident_store,
                )
            )
            _emit_doctor_fix_event(event_log, repairs)

    if compact_events:
        event_compaction = compact_archived_task_events(dry_run=not fix or dry_run)
        repairs["event_log_compaction"] = event_compaction
        event_health = event_compaction.get("after") if isinstance(event_compaction.get("after"), dict) else event_log_health()

    finding_counts = {
        "stale_runs": len(stale_runs),
        "stale_workers": len(stale_workers),
        "stale_open_tasks": len(stale_tasks),
        "stale_incidents": len(stale_incidents),
        "orphan_worktrees": len(worktrees.get("reaped") or []),
        "snapshot_null_id_rows": len(snapshot_defects),
        "event_log_compactable_rows": int(event_compaction.get("removed_event_count") or 0),
    }
    model_authority = _model_authority_report()
    return {
        "schema_version": 1,
        "generated_at": ref,
        "ok": True,
        "mode": {"fix": bool(fix), "dry_run": bool(dry_run)},
        "thresholds": {
            "stale_run_hours": int(stale_run_hours),
            "stale_worker_hours": int(stale_worker_hours),
            "stale_task_days": int(stale_task_days),
            "stale_incident_hours": int(stale_incident_hours),
            "stale_incident_days": round(float(stale_incident_hours) / 24, 3),
            "worktree_min_age_seconds": int(worktree_min_age_seconds),
            "include_worktrees": bool(include_worktrees),
            "compact_events": bool(compact_events),
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
            "stale_incidents": stale_incidents,
            "orphan_worktrees": worktrees,
            "snapshot_null_id_rows": snapshot_defects,
            "event_log": event_health,
            "event_log_compaction": event_compaction,
        },
        # Informational, deliberately OUTSIDE finding_counts: a stale/redundant
        # model pin is operator judgment, not something `--fix` should silently
        # rewrite (config.yaml's single writer is upstream save_config). Doctor
        # detects and labels; the operator edits.
        "model_authority": model_authority,
        "repairs": repairs,
    }


def _model_authority_report() -> dict[str, Any]:
    """Detect stale/redundant runtime-default overrides and per-persona pins.

    Reads config only; never mutates. Surfaces the "agent_runtime.default_model
    shadows model.default" divergence (the stale-pin class) plus redundant pins,
    so a recurrence is visible without hand-diffing config.yaml.
    """
    from .config import describe_runtime_default_authority

    try:
        authority = describe_runtime_default_authority()
    except Exception as exc:  # pragma: no cover - defensive; doctor must not crash
        return {"available": False, "error": str(exc)}

    override = authority.get("harness_override", {})
    pins = authority.get("persona_pins", []) or []
    redundant_pins = [p for p in pins if p.get("matches_runtime_default") is True]
    provider_only_pins = [p for p in pins if p.get("provider_pinned_without_model")]
    notices: list[str] = []
    if override.get("model_state") == "shadowing":
        notices.append(
            f"agent_runtime.default_model ({override.get('model')}) shadows the runtime "
            f"default from model.default — agents are NOT running the model you set; remove it "
            "unless the harness is deliberately pinned"
        )
    elif override.get("model_state") == "redundant":
        notices.append(
            "agent_runtime.default_model duplicates model.default and is unmaintained — remove it"
        )
    if redundant_pins:
        ids = ", ".join(sorted(p.get("persona_id", "?") for p in redundant_pins))
        notices.append(
            f"persona pins duplicate the runtime default (likely stale): {ids} — "
            "remove the model/provider/api_mode pin so they follow the default"
        )
    if provider_only_pins:
        ids = ", ".join(sorted(p.get("persona_id", "?") for p in provider_only_pins))
        notices.append(
            f"persona provider pinned without a model (pairing hazard on default change): {ids}"
        )

    return {
        "available": True,
        "resolved": authority.get("resolved", {}),
        "top_level": authority.get("top_level", {}),
        "harness_override": override,
        "persona_pins": pins,
        "divergent": override.get("model_state") == "shadowing",
        "notices": notices,
    }


def _skipped_event_compaction(event_health: dict[str, Any]) -> dict[str, Any]:
    return {
        "dry_run": True,
        "skipped": "event_compaction_not_requested",
        "removed_event_count": 0,
        "removed_bytes": 0,
        "before": event_health,
        "after": event_health,
        "watermark_reset": False,
    }


def _stale_incident_hours(stale_incident_hours: int, stale_incident_days: int | None) -> int:
    if stale_incident_days is not None:
        return max(1, int(stale_incident_days or 1) * 24)
    return max(1, int(stale_incident_hours or DEFAULT_STALE_INCIDENT_HOURS))


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


def _stale_incidents(
    incident_store: IncidentStore,
    task_store: TaskStore,
    *,
    ref: datetime,
    stale_hours: int,
) -> list[dict[str, Any]]:
    cutoff = ref - timedelta(hours=max(1, int(stale_hours or 1)))
    open_incidents = incident_store.list_open()
    findings: list[dict[str, Any]] = []

    newest_budget_incident_by_group: dict[tuple[str | None, str | None, str, str], str] = {}
    budget_incidents = [incident for incident in open_incidents if incident.kind == "mission_budget_exceeded"]
    for incident in budget_incidents:
        group = (incident.task_id, incident.run_id, incident.kind, incident.summary)
        current_id = newest_budget_incident_by_group.get(group)
        if current_id is None:
            newest_budget_incident_by_group[group] = incident.id
            continue
        current = next((item for item in budget_incidents if item.id == current_id), None)
        if current is None or _aware(incident.opened_at) > _aware(current.opened_at):
            newest_budget_incident_by_group[group] = incident.id

    for incident in open_incidents:
        opened_at = _aware(getattr(incident, "opened_at", None))
        if opened_at is None or opened_at >= cutoff:
            continue
        reason: str | None = None
        task_state: str | None = None
        if incident.task_id:
            try:
                task = task_store.get(incident.task_id)
                task_state = task.state.value
                if task.state in {TaskState.DONE, TaskState.CANCELLED}:
                    reason = "terminal_task"
            except NotFound:
                reason = "missing_task"
        if reason is None and incident.kind == "mission_budget_exceeded":
            group = (incident.task_id, incident.run_id, incident.kind, incident.summary)
            if newest_budget_incident_by_group.get(group) != incident.id:
                reason = "duplicate_budget_incident"
        if reason is None:
            continue
        findings.append(
            {
                "incident_id": incident.id,
                "task_id": incident.task_id,
                "run_id": incident.run_id,
                "kind": incident.kind,
                "opened_age_seconds": _age_seconds(ref, opened_at),
                "reason": reason,
                "task_state": task_state,
                "close_reason": STALE_BULK_CLOSE_REASON,
            }
        )
    return findings


def _repair_stale_runtime(
    *,
    stale_runs: list[dict[str, Any]],
    stale_workers: list[dict[str, Any]],
    stale_tasks: list[dict[str, Any]],
    stale_incidents: list[dict[str, Any]],
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
        "archived_task_ids": [],
        "archive_results": [],
        "waiting_for_operator_task_ids": [],
        "waiting_for_operator_incident_ids": [],
        "closed_incident_ids": [],
        "closed_incident_count_by_kind": {},
        "incident_sweep_reason": STALE_BULK_CLOSE_REASON,
    }
    for item in stale_incidents:
        incident_id = item["incident_id"]
        try:
            incident = incident_store.close(incident_id, reason=STALE_BULK_CLOSE_REASON)
        except Exception:
            continue
        repairs["closed_incident_ids"].append(incident.id)
    repairs["closed_incident_count_by_kind"] = _count_by_incident_kind(repairs["closed_incident_ids"], incident_store)
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
        try:
            task = task_store.get(task_id)
        except Exception:
            continue
        try:
            if task.state not in {TaskState.DONE, TaskState.CANCELLED}:
                task = task_store.cancel(
                    task_id,
                    actor="doctor",
                    reason="stale open task archived by Harness doctor",
                )
            archive_result = task_store.archive(
                task.id,
                actor="doctor",
                reason="stale open task archived by Harness doctor",
            )
            archived_ids = list(archive_result.get("archived_task_ids") or [])
            if task.id in archived_ids:
                repairs["archived_task_ids"].append(task.id)
                repairs["archive_results"].append(archive_result)
                continue
        except Exception:
            pass
        incident = _open_or_get_incident(
            incident_store,
            task_id=task_id,
            run_id=None,
            kind="operator_intervention",
            summary="Open task idled past Harness doctor threshold; operator disposition required.",
            metadata={
                "source": "harness_doctor",
                "intervention": "waiting_for_operator",
                "stale_reason": "open task idle exceeded Harness doctor threshold",
            },
        )
        repairs["blocked_task_incident_ids"].append(incident.id)
        repairs["waiting_for_operator_incident_ids"].append(incident.id)
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
                reason="stale open task moved to waiting_for_operator intervention",
            )
            repairs["blocked_task_ids"].append(task.id)
            repairs["waiting_for_operator_task_ids"].append(task.id)
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
    metadata: dict[str, Any] | None = None,
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
            metadata=metadata or {"source": "harness_doctor"},
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
        "tasks": "task_id",
        "goals": "id",
        "agents": "persona_id",
        "runs": "run_id",
        "worker_sessions": "worker_session_id",
        "persona_instances": "persona_instance_id",
        "repo_bundles": "repo_bundle_id",
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
                    "archived_tasks": len(repairs.get("archived_task_ids") or []),
                    "closed_incidents": len(repairs.get("closed_incident_ids") or []),
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


def _count_by_key(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return counts


def _count_by_incident_kind(incident_ids: list[str], incident_store: IncidentStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    for incident_id in incident_ids:
        try:
            incident = incident_store.get(incident_id)
        except Exception:
            continue
        counts[incident.kind] = counts.get(incident.kind, 0) + 1
    return counts
