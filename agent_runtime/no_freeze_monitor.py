from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
import uuid

from hermes_time import now

from .events import EventLog
from .errors import NotFound
from .models import Incident, Proof
from .proof_rules import ProofType
from .states import TaskState
from .store import IncidentStore, ProofStore, TaskStore


@dataclass(frozen=True, slots=True)
class NoFreezeThresholds:
    active_run_heartbeat_stale_seconds: int = 120
    no_progress_seconds: int = 300
    liveness_poll_seconds: int = 60
    liveness_quiet_strikes: int = 2
    liveness_hung_seconds: int = 300
    same_next_action_repeated: int = 3
    same_persona_stage_retries: int = 2
    neko_scope_updates: int = 2


def classify_freezes(
    *,
    status: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
    reference_time: datetime | None = None,
    thresholds: NoFreezeThresholds | None = None,
) -> list[dict[str, Any]]:
    status = status or {}
    snapshot = snapshot or {}
    history = history or []
    ref = reference_time or now()
    thresholds = thresholds or NoFreezeThresholds()
    findings: list[dict[str, Any]] = []

    observability = status.get("observability") if isinstance(status.get("observability"), dict) else {}
    for item in observability.get("interventions") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if kind in {"daemon_stale", "run_stalled", "context_request_loop"}:
            findings.append(_finding(kind, item.get("severity") or "high", item.get("task_id"), item.get("run_id"), item.get("summary") or kind))

    for run in _active_runs(snapshot):
        age = _run_heartbeat_age_seconds(run, ref)
        if age is not None and age > thresholds.active_run_heartbeat_stale_seconds:
            findings.append(_finding("run_stalled", "high", run.get("task_id"), run.get("run_id"), "Active run heartbeat is stale", age_seconds=age))
        progress = run.get("progress") if isinstance(run.get("progress"), dict) else {}
        persona_id = str(run.get("persona_id") or "")
        if persona_id in {"neko_supervisor", "dev", "backend_dev", "qa"} and not progress.get("autonomy_packet_id"):
            findings.append(_finding("autonomy_packet_missing", "high", run.get("task_id"), run.get("run_id"), "Active specialist run has no autonomy packet"))
        if _tool_budget_exceeded(progress):
            findings.append(
                _finding(
                    "tool_budget_exceeded_without_new_signal",
                    "high",
                    run.get("task_id"),
                    run.get("run_id"),
                    "Read/search budget exceeded without patch, test, or proof progress",
                    read_search_count=_safe_int(progress.get("read_search_count")) or 0,
                    read_search_limit=_safe_int(progress.get("read_search_limit")) or 0,
                )
            )
        if run.get("session_id") and not progress.get("context_receipt_id"):
            findings.append(_finding("context_resume_without_receipt", "medium", run.get("task_id"), run.get("run_id"), "Resumed run has no context receipt"))

    repeated_action = _repeated_history_value(history, "next_action")
    if repeated_action and repeated_action["count"] >= thresholds.same_next_action_repeated:
        findings.append(
            _finding(
                "same_next_action_repeated",
                "medium",
                repeated_action.get("task_id"),
                None,
                "Same next action repeated across monitor samples",
                count=repeated_action["count"],
            )
        )

    repeated_owner_stage = _repeated_history_value(history, "persona_stage")
    if repeated_owner_stage and repeated_owner_stage["count"] >= thresholds.same_persona_stage_retries:
        findings.append(
            _finding(
                "same_stage_retry_without_signal_change",
                "high",
                repeated_owner_stage.get("task_id"),
                None,
                "Same persona/stage retried without a changed signal",
                count=repeated_owner_stage["count"],
            )
        )

    invalid_count = _incident_kind_count(snapshot, "model_invalid_output")
    if invalid_count >= 2:
        findings.append(_finding("persona_invalid_output_loop", "high", None, None, "Repeated invalid model outputs", count=invalid_count))

    return _dedupe_findings(findings)


def record_freeze_findings(
    *,
    task_id: str | None,
    findings: list[dict[str, Any]],
    proof_store: ProofStore | None = None,
    incident_store: IncidentStore | None = None,
    task_store: TaskStore | None = None,
    event_log: EventLog | None = None,
) -> dict[str, Any]:
    proof_store = proof_store or ProofStore()
    incident_store = incident_store or IncidentStore()
    task_store = task_store or TaskStore()
    _ = event_log
    recorded_proofs: list[str] = []
    opened_incidents: list[str] = []
    for finding in findings:
        resolved_task_id = task_id or str(finding.get("task_id") or "runtime")
        proof = Proof(
            id=f"proof_monitor_{uuid.uuid4().hex[:8]}",
            task_id=resolved_task_id,
            stage_id=None,
            type=ProofType.LOG,
            title=f"Monitor finding: {finding['kind']}",
            path_or_value=f"monitor:{finding['kind']}",
            created_by="harness_monitor",
            created_at=now(),
            metadata={"status": "blocked", "finding": _safe_finding(finding)},
            redaction_status="safe",
        )
        proof_store.attach(proof)
        recorded_proofs.append(proof.id)
        incident_kind = "run_hung" if str(finding.get("kind") or "") == "run_hung" else "runtime_freeze"
        incident = Incident(
            id=f"inc_{uuid.uuid4().hex[:8]}",
            task_id=task_id or finding.get("task_id"),
            run_id=finding.get("run_id"),
            kind=incident_kind,
            summary=f"{finding['kind']}: {finding['summary']}",
            detail_path=None,
            opened_at=now(),
            metadata={"finding_kind": finding["kind"], "proof_id": proof.id},
        )
        incident_store.open(incident)
        opened_incidents.append(incident.id)
        _link_incident_to_task(task_store, incident.id, task_id or finding.get("task_id"))
    return {"proof_ids": recorded_proofs, "incident_ids": opened_incidents, "finding_count": len(findings)}


def _link_incident_to_task(task_store: TaskStore, incident_id: str, task_id: Any) -> None:
    if not task_id:
        return
    try:
        task = task_store.get(str(task_id))
    except NotFound:
        return
    if incident_id not in task.open_incident_ids:
        task.open_incident_ids.append(incident_id)
    if task.state not in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
        task.state = TaskState.RUNNING
    task.updated_at = now()
    task_store.update(task, actor="harness_monitor", reason="runtime freeze monitor finding")


def _active_runs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    observability = snapshot.get("observability") if isinstance(snapshot.get("observability"), dict) else {}
    runs = observability.get("active_runs")
    if isinstance(runs, list):
        return [run for run in runs if isinstance(run, dict)]
    runs = snapshot.get("runs")
    if not isinstance(runs, list):
        return []
    return [run for run in runs if isinstance(run, dict) and str(run.get("state") or "") in {"running", "queued", "waiting_on_tool", "waiting_on_approval"}]


def _run_heartbeat_age_seconds(run: dict[str, Any], ref: datetime) -> float | None:
    heartbeat = _coerce_datetime(run.get("last_heartbeat_at") or run.get("started_at"))
    if heartbeat is None:
        return None
    if heartbeat.tzinfo is None and ref.tzinfo is not None:
        heartbeat = heartbeat.replace(tzinfo=ref.tzinfo)
    if heartbeat.tzinfo is not None and ref.tzinfo is None:
        ref = ref.replace(tzinfo=heartbeat.tzinfo)
    return max(0.0, (ref - heartbeat).total_seconds())


def _repeated_history_value(history: list[dict[str, Any]], key: str) -> dict[str, Any] | None:
    values = []
    for item in history[-8:]:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if not value:
            continue
        values.append((value, item))
    if not values:
        return None
    last_value = values[-1][0]
    count = 0
    task_id = None
    for value, item in reversed(values):
        if value != last_value:
            break
        count += 1
        task_id = task_id or item.get("task_id")
    return {"value": last_value, "count": count, "task_id": task_id}


def _incident_kind_count(snapshot: dict[str, Any], kind: str) -> int:
    incidents = snapshot.get("incidents")
    if not isinstance(incidents, list):
        return 0
    return sum(
        1
        for item in incidents
        if isinstance(item, dict)
        and str(item.get("kind") or item.get("incident_kind") or "") == kind
        and _incident_is_open(item)
    )


def _incident_is_open(item: dict[str, Any]) -> bool:
    if item.get("is_open") is not None:
        return item.get("is_open") is True
    return item.get("closed_at") in {None, ""}


def _tool_budget_exceeded(progress: dict[str, Any]) -> bool:
    read_count = _safe_int(progress.get("read_search_count")) or 0
    read_limit = _safe_int(progress.get("read_search_limit")) or 0
    if read_limit <= 0 or read_count < read_limit:
        return False
    if progress.get("has_patch_progress") or progress.get("has_test_progress") or progress.get("has_proof_progress"):
        return False
    return str(progress.get("loop_warning") or "") == "read_search_without_patch_threshold" or read_count > read_limit


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _finding(kind: str, severity: str, task_id: Any, run_id: Any, summary: Any, **extra: Any) -> dict[str, Any]:
    data = {
        "kind": kind,
        "severity": str(severity or "medium"),
        "task_id": str(task_id) if task_id else None,
        "run_id": str(run_id) if run_id else None,
        "summary": _safe_text(str(summary or kind)),
    }
    for key, value in extra.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            data[key] = _safe_text(value) if isinstance(value, str) else value
    return data


def _safe_finding(finding: dict[str, Any]) -> dict[str, Any]:
    return {key: _safe_text(value) if isinstance(value, str) else value for key, value in finding.items() if isinstance(value, (str, int, float, bool)) or value is None}


def _safe_text(value: str) -> str:
    text = " ".join(value.split())
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "api_key", "bearer")):
        return "[redacted]"
    if ":/" in text or "\\" in text:
        return "[redacted-path]"
    return text[:240]


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for finding in findings:
        key = (finding.get("kind"), finding.get("task_id"), finding.get("run_id"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped
