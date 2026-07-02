from __future__ import annotations

from typing import Any

from hermes_time import now

from .events import EventLog
from .models import Event
from .persona_assignments import safe_assignment_text, safe_assignment_token
from .runtime_instances import GoalRuntimeInstanceStore
from .states import RunState
from .store import RunStore
from .worker_sessions import WorkerSessionStore


def operator_takeover_worker(
    worker_session_id: str,
    *,
    actor: str = "operator",
    reason: str = "operator takeover",
    lease_seconds: int = 900,
    cancel_active_run: bool = False,
    approve_destructive: bool = False,
    worker_store: WorkerSessionStore | None = None,
    run_store: RunStore | None = None,
    runtime_store: GoalRuntimeInstanceStore | None = None,
    event_log: EventLog | None = None,
) -> dict[str, Any]:
    """Freeze peer work and possess one worker under an audited operator lease."""

    safe_worker_id = safe_assignment_token(worker_session_id)
    safe_actor = safe_assignment_token(actor) or "operator"
    safe_reason = safe_assignment_text(reason, limit=300) or "operator takeover"
    if not safe_worker_id:
        raise ValueError("worker_session_id is required")

    log = event_log or EventLog()
    workers = worker_store or WorkerSessionStore(event_log=log)
    runs = run_store or RunStore(event_log=log)
    runtimes = runtime_store or GoalRuntimeInstanceStore(event_log=log)
    target_before = workers.get(safe_worker_id)

    log.append(
        Event(
            ts=now(),
            type="operator.takeover.requested",
            task_id=target_before.task_id,
            run_id=target_before.active_run_id,
            persona_id=target_before.persona_id,
            payload={
                "worker_session_id": safe_worker_id,
                "actor": safe_actor,
                "reason": safe_reason,
                "cancel_active_run": bool(cancel_active_run),
                "approve_destructive": bool(approve_destructive),
            },
        )
    )

    parked_lane_ids: list[str] = []
    for lane in runtimes.list_all():
        if str(lane.state) in {"queued", "activating", "running", "active", "waiting", "parked"}:
            try:
                parked = runtimes.park_lane(lane.id, reason=f"operator takeover by {safe_actor}: {safe_reason}")
                parked_lane_ids.append(parked.id)
            except Exception:
                continue

    paused_worker_ids: list[str] = []
    for worker in workers.find_active():
        if worker.id == safe_worker_id:
            continue
        try:
            paused = workers.pause(worker.id, actor=safe_actor, reason=f"operator takeover freeze: {safe_reason}")
            paused_worker_ids.append(paused.id)
        except Exception:
            continue

    approval_required = False
    cancelled_run_id: str | None = None
    active_run_id = target_before.active_run_id
    if active_run_id and cancel_active_run:
        if not approve_destructive:
            approval_required = True
            log.append(
                Event(
                    ts=now(),
                    type="operator.takeover.approval_required",
                    task_id=target_before.task_id,
                    run_id=active_run_id,
                    persona_id=target_before.persona_id,
                    payload={
                        "worker_session_id": safe_worker_id,
                        "actor": safe_actor,
                        "approval": "approve_destructive",
                        "reason": safe_reason,
                    },
                )
            )
        else:
            cancelled = runs.cancel(active_run_id, reason=f"operator takeover by {safe_actor}: {safe_reason}")
            if cancelled.state == RunState.CANCELLED:
                cancelled_run_id = cancelled.id
                workers.update_after_run(
                    safe_worker_id,
                    cancelled,
                    close_reason="operator_takeover_cancelled_run",
                    count_decision=False,
                )

    possessed = workers.possess(safe_worker_id, actor=safe_actor, lease_seconds=lease_seconds)
    log.append(
        Event(
            ts=now(),
            type="operator.takeover.applied",
            task_id=possessed.task_id,
            run_id=cancelled_run_id or possessed.active_run_id,
            persona_id=possessed.persona_id,
            payload={
                "worker_session_id": possessed.id,
                "actor": safe_actor,
                "parked_lane_ids": parked_lane_ids[:20],
                "paused_worker_ids": paused_worker_ids[:20],
                "cancelled_run_id": cancelled_run_id,
                "approval_required": approval_required,
                "lease_expires_at": possessed.lease_expires_at,
            },
        )
    )

    return {
        "ok": True,
        "capability_id": "worker.takeover",
        "worker_session_id": possessed.id,
        "task_id": possessed.task_id,
        "persona_id": possessed.persona_id,
        "state": possessed.state.value,
        "possession_state": possessed.possession_state.value,
        "lease_owner": possessed.lease_owner,
        "lease_expires_at": possessed.lease_expires_at,
        "parked_lane_ids": parked_lane_ids,
        "paused_worker_ids": paused_worker_ids,
        "cancelled_run_id": cancelled_run_id,
        "approval_required": approval_required,
    }
