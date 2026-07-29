from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from hermes_time import now

from .incidents import CRITICAL_INCIDENT_KINDS
from .models import Event, Incident, Proof, Task
from .scope_control import untriaged_issue_discoveries
from .states import PossessionState, RunState, TaskState, WorkerSessionState
from .store import ACTIVE_RUN_STATES
from .worker_sessions import ACTIVE_WORKER_STATES, worker_session_summary

DEFAULT_DAEMON_STALE_AFTER_SECONDS = 120
DEFAULT_RUN_STALLED_AFTER_SECONDS = 900
DEFAULT_REPEATED_CONTEXT_REQUESTS_THRESHOLD = 2
DELIVERY_EVIDENCE_INCIDENT_KINDS = frozenset({"patch_landed_nowhere", "stage_no_progress"})


def build_observability(
    *,
    tasks: list[Task],
    runs: list[Any],
    incidents: list[Incident],
    proofs: list[Proof],
    daemon_status: dict[str, Any] | None,
    events: list[Event] | None = None,
    reference_time: datetime | None = None,
    daemon_stale_after_seconds: int = DEFAULT_DAEMON_STALE_AFTER_SECONDS,
    run_stalled_after_seconds: int = DEFAULT_RUN_STALLED_AFTER_SECONDS,
    repeated_context_requests_threshold: int = DEFAULT_REPEATED_CONTEXT_REQUESTS_THRESHOLD,
    execution_mode: str = "manual",
    worker_sessions: list[Any] | None = None,
) -> dict[str, Any]:
    """Build redaction-safe Mission Control observability envelope.

    This envelope is intended for Launcher and CLI health surfaces. It exposes
    counts, freshness, and concrete intervention handles, but never raw model
    outputs, incident summaries, file paths, proof artifact paths, or secrets.
    """

    ref = reference_time or now()
    daemon = daemon_status or {"state": "offline"}
    open_incidents = [incident for incident in incidents if incident.closed_at is None]
    open_tasks = [task for task in tasks if task.state not in {TaskState.DONE, TaskState.CANCELLED}]
    active_runs = [run for run in runs if run.state in ACTIVE_RUN_STATES]
    workers = list(worker_sessions or [])
    active_workers = [worker for worker in workers if _worker_is_active(worker)]
    running_runs = [run for run in active_runs if run.state == RunState.RUNNING]
    queued_runs = [run for run in active_runs if run.state == RunState.QUEUED]
    waiting_runs = [run for run in active_runs if run.state in {RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL}]

    daemon_heartbeat_at = _coerce_datetime(daemon.get("heartbeat_at"))
    daemon_heartbeat_age = _age_seconds(ref, daemon_heartbeat_at)
    daemon_state = str(daemon.get("state", "offline"))
    daemon_expected = execution_mode == "daemon"
    daemon_stale_relevant = daemon_expected or daemon_state == "running"
    stale_daemon = bool(
        daemon_stale_relevant
        and daemon_state not in {"offline", "error"}
        and (daemon_heartbeat_at is None or (daemon_heartbeat_age is not None and daemon_heartbeat_age > daemon_stale_after_seconds))
    )

    stalled_runs = [
        run
        for run in active_runs
        if _run_age_seconds(ref, run) is not None and _run_age_seconds(ref, run) > run_stalled_after_seconds
    ]
    stale_workers = [
        worker
        for worker in active_workers
        if _worker_age_seconds(ref, worker) is not None and _worker_age_seconds(ref, worker) > run_stalled_after_seconds
    ]
    repeated_context_tasks = [
        task
        for task in open_tasks
        if len(getattr(task, "context_requests", []) or []) >= repeated_context_requests_threshold
    ]
    untriaged_discoveries = [(task, item) for task in open_tasks for item in untriaged_issue_discoveries(task)]

    interventions: list[dict[str, Any]] = []
    if daemon_state == "offline" and daemon_expected:
        interventions.append(_intervention("daemon_offline", "critical", "mission_daemon", None, None, "Mission Daemon is offline"))
    elif daemon_state == "error":
        interventions.append(_intervention("daemon_error", "critical", "mission_daemon", None, None, "Mission Daemon status cannot be read"))
    elif stale_daemon:
        interventions.append(_intervention("daemon_stale", "critical", "mission_daemon", None, None, "Mission Daemon heartbeat is stale"))

    for incident in open_incidents:
        intervention_kind = incident.kind if incident.kind in DELIVERY_EVIDENCE_INCIDENT_KINDS else "open_incident"
        interventions.append(
            _intervention(
                intervention_kind,
                _severity_for_incident(incident),
                "incident",
                incident.task_id,
                incident.run_id,
                f"Open {incident.kind} incident requires review",
                incident_id=incident.id,
                incident_kind=incident.kind,
            )
        )

    for run in stalled_runs:
        interventions.append(
            _intervention(
                "run_stalled",
                "high",
                "run",
                run.task_id,
                run.id,
                "Running agent has exceeded the stalled-run threshold",
                persona_id=run.persona_id,
                age_seconds=_run_age_seconds(ref, run),
            )
        )
    for worker in stale_workers:
        interventions.append(
            _intervention(
                "worker_stale_heartbeat",
                "high",
                "worker_session",
                getattr(worker, "task_id", None),
                getattr(worker, "active_run_id", None),
                "Worker session heartbeat is stale",
                worker_session_id=getattr(worker, "id", None),
                persona_id=getattr(worker, "persona_id", None),
                age_seconds=_worker_age_seconds(ref, worker),
            )
        )

    for task in repeated_context_tasks:
        latest = _latest_context_request(task)
        interventions.append(
            _intervention(
                "context_request_loop",
                "medium",
                "mission",
                task.id,
                None,
                "Mission has repeated context requests without state advancement",
                context_request_count=len(getattr(task, "context_requests", []) or []),
                context_request_id=latest.get("id") if latest else None,
                context_request_status=latest.get("status") if latest else None,
            )
        )
    for task in open_tasks:
        latest = _latest_context_request(task)
        if latest and latest.get("status") in {"open", "unsupported", "superseded"}:
            interventions.append(
                _intervention(
                    "context_request_unfulfilled",
                    "high" if latest.get("status") == "unsupported" else "medium",
                    "mission",
                    task.id,
                    None,
                    "Mission is waiting on a context request before another persona run",
                    context_request_id=latest.get("id"),
                    context_request_status=latest.get("status"),
                    failure_reason=latest.get("failure_reason"),
                )
            )

    for task, discovery in untriaged_discoveries:
        severity = str(discovery.get("severity", "medium"))
        interventions.append(
            _intervention(
                "issue_discovery_triage_needed",
                "high" if severity in {"high", "critical"} else "medium",
                "mission",
                task.id,
                None,
                "Mission has an untriaged issue discovery",
                discovery_id=discovery.get("id"),
                issue_severity=severity,
                relationship_hint=discovery.get("relationship_hint"),
            )
        )

    health_status = _health_status(interventions)
    return {
        "schema_version": 1,
        "execution_mode": execution_mode,
        "generated_at": ref,
        "health": {
            "status": health_status,
            "summary": _health_summary(health_status, interventions),
        },
        "freshness": {
            "daemon_heartbeat_at": daemon_heartbeat_at,
            "daemon_heartbeat_age_seconds": daemon_heartbeat_age,
            "stalled_run_threshold_seconds": run_stalled_after_seconds,
            "daemon_stale_threshold_seconds": daemon_stale_after_seconds,
        },
        "signals": {
            "open_tasks": len(open_tasks),
            "running_runs": len(running_runs),
            "active_runs": len(active_runs),
            "queued_runs": len(queued_runs),
            "waiting_runs": len(waiting_runs),
            "active_worker_sessions": len(active_workers),
            "stale_worker_sessions": len(stale_workers),
            "open_incidents": len(open_incidents),
            "stale_daemon": stale_daemon,
            "stalled_running_runs": len(stalled_runs),
            "repeated_context_request_tasks": len(repeated_context_tasks),
            "untriaged_issue_discoveries": len(untriaged_discoveries),
            "proofs_total": len(proofs),
            "self_heal": _self_heal_signals(open_tasks, runs),
        },
        "interventions": interventions,
        "active_runs": [_run_summary(run) for run in active_runs],
        "worker_sessions": [_worker_summary(worker, reference_time=ref) for worker in workers],
        "recent_events": [_event_summary(event) for event in (events or [])],
        "recent_runs": [_run_summary(run) for run in sorted(runs, key=lambda item: getattr(item, "started_at", ref), reverse=True)[:10]],
    }


def _health_status(interventions: list[dict[str, Any]]) -> str:
    severities = {item.get("severity") for item in interventions}
    if severities & {"critical", "high"}:
        return "critical"
    if interventions:
        return "degraded"
    return "healthy"


def _health_summary(status: str, interventions: list[dict[str, Any]]) -> str:
    if status == "healthy":
        return "Mission runtime observability is healthy"
    return f"{len(interventions)} observability intervention(s) require attention"


def _intervention(
    kind: str,
    severity: str,
    subject: str,
    task_id: str | None,
    run_id: str | None,
    summary: str,
    **extra: Any,
) -> dict[str, Any]:
    expires_at = extra.pop("expires_at", None)
    data = {
        "kind": kind,
        "severity": severity,
        "subject": subject,
        "task_id": task_id,
        "run_id": run_id,
        "summary": summary,
        "ask": summary,
        "risk_if_ignored": _risk_if_ignored(kind, severity),
        "allowed_actions": _allowed_actions(kind, subject),
        "expires_at": expires_at,
        "safe_refs": {
            key: value
            for key, value in {
                "task_id": task_id,
                "run_id": run_id,
                "incident_id": extra.get("incident_id"),
                "worker_session_id": extra.get("worker_session_id"),
                "context_request_id": extra.get("context_request_id"),
                "discovery_id": extra.get("discovery_id"),
            }.items()
            if value
        },
    }
    data.update(extra)
    return data


def _risk_if_ignored(kind: str, severity: str) -> str:
    if severity in {"critical", "high"}:
        return "Mission progress can remain blocked or drift from Harness truth."
    if kind in {"context_request_loop", "context_request_unfulfilled"}:
        return "Agents may keep requesting context instead of advancing the mission."
    return "Mission Control may remain degraded until this is resolved."


def _allowed_actions(kind: str, subject: str) -> list[str]:
    if kind in DELIVERY_EVIDENCE_INCIDENT_KINDS:
        return ["answer_intervention", "cancel_run", "rescope"]
    if kind == "open_incident":
        return ["answer_intervention", "retry_stage"]
    if kind in {"daemon_offline", "daemon_error", "daemon_stale"}:
        return ["start_daemon"]
    if subject == "run":
        return ["cancel_run", "retry_stage"]
    if subject == "worker_session":
        return ["nudge_worker", "cancel_run"]
    if kind.startswith("context_request"):
        return ["answer_intervention"]
    if kind == "issue_discovery_triage_needed":
        return ["answer_intervention", "create_goal"]
    return ["answer_intervention"]


def _severity_for_incident(incident: Incident) -> str:
    if incident.kind in CRITICAL_INCIDENT_KINDS:
        return "critical"
    if incident.kind in DELIVERY_EVIDENCE_INCIDENT_KINDS:
        return "high"
    if incident.kind in {"model_invalid_output"}:
        return "high"
    return "medium"


def _run_summary(run: Any) -> dict[str, Any]:
    final_decision = getattr(run, "final_decision", None)
    decision_type = final_decision.get("type") if isinstance(final_decision, dict) else None
    llm = _safe_llm(getattr(run, "llm", None))
    progress = _safe_progress(getattr(run, "progress", None))
    return {
        "run_id": getattr(run, "id", None),
        "persona_id": getattr(run, "persona_id", None),
        "task_id": getattr(run, "task_id", None),
        "stage_id": getattr(run, "stage_id", None),
        "state": str(getattr(run, "state", "")),
        "started_at": getattr(run, "started_at", None),
        "last_heartbeat_at": getattr(run, "last_heartbeat_at", None),
        "finished_at": getattr(run, "finished_at", None),
        "decision_type": llm.get("decision_type") or decision_type,
        "session_id": getattr(run, "session_id", None) or llm.get("session_id"),
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "base_url_host": llm.get("base_url_host"),
        "total_tokens": llm.get("total_tokens"),
        "latency_ms": llm.get("latency_ms"),
        "timing": llm.get("timing"),
        "validation_status": llm.get("validation_status"),
        "progress": progress,
        "active_tool": _active_tool_summary(progress),
    }


def _safe_progress(progress: Any) -> dict[str, Any] | None:
    if not isinstance(progress, dict):
        return None
    allowed = {
        "type", "state", "tool", "tool_name", "status", "summary", "elapsed_seconds", "api_calls",
        "phase", "step", "command_label", "autonomy_packet_id", "context_receipt_id", "read_search_limit",
        "timing_key", "duration_ms",
        "proof_retry_limit", "proof_command_limit", "skill_load_limit", "selected_skill_count",
        "rejected_skill_count", "context_event_count", "context_proof_count",
        "context_incident_count", "environment_fingerprint_status", "loop_warning",
        "worker_session_id", "worker_state", "worker_heartbeat_age_seconds",
        "possession_state", "same_session_continuation",
        "patch_count", "test_count", "proof_count", "read_search_count",
        "new_evidence_count", "packet_repair_count", "invalid_packet_count",
        "malformed_packet_count", "recovery_action", "recovery_reason",
        "scope_recovery", "handoff_target", "waiting_on_persona_id",
    }
    return {key: progress.get(key) for key in allowed if isinstance(progress.get(key), (str, int, float, bool))}


def _active_tool_summary(progress: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(progress, dict):
        return None
    if progress.get("phase") != "tool" or progress.get("step") != "tool_started":
        return None
    tool_name = progress.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return None
    summary: dict[str, Any] = {
        "tool_name": tool_name,
        "status": progress.get("status") or "started",
        "summary": progress.get("summary") or f"Started tool {tool_name}",
    }
    command_label = progress.get("command_label")
    if isinstance(command_label, str) and command_label:
        summary["command_label"] = command_label
    return summary


def _safe_llm(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "provider", "model", "base_url_host", "session_id", "api_calls", "tool_turns",
        "input_tokens", "output_tokens", "total_tokens", "latency_ms", "finish_reason",
        "response_len", "validation_status", "decision_type",
    }
    safe = {key: value.get(key) for key in allowed if value.get(key) is not None}
    timing = value.get("timing")
    if isinstance(timing, dict):
        safe_timing: dict[str, int] = {}
        for key, item in timing.items():
            if not isinstance(key, str) or not (key.endswith("_ms") or key.endswith("_count")):
                continue
            try:
                parsed = int(item)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                safe_timing[key[:64]] = parsed
        if safe_timing:
            safe["timing"] = safe_timing
    metrics = value.get("decision_metrics")
    if isinstance(metrics, dict):
        safe_metrics: dict[str, Any] = {}
        for key in (
            "classification",
            "api_calls",
            "tool_turns",
            "read_search_count",
            "proof_count",
            "patch_count",
            "test_count",
            "packet_repair_count",
            "new_evidence_count",
            "reason",
        ):
            item = metrics.get(key)
            if isinstance(item, str):
                safe_metrics[key] = item[:160]
            elif isinstance(item, int) and item >= 0:
                safe_metrics[key] = item
        if safe_metrics:
            safe["decision_metrics"] = safe_metrics
    return safe


def _self_heal_signals(tasks: list[Task], runs: list[Any]) -> dict[str, Any]:
    totals = {
        "scope_update": 0,
        "same_stage_retry": 0,
        "read_search_after_failed_proof": 0,
        "skill_fanout": 0,
        "failed_proof_reused": 0,
        "failed_proof_ignored": 0,
        "env_fingerprint_changed": 0,
        "self_heal_proposed": 0,
        "self_heal_applied": 0,
        "self_heal_cannot": 0,
    }
    for task in tasks:
        root = getattr(task, "harness_self_heal", {}) or {}
        stages = root.get("stages") if isinstance(root, dict) else {}
        if not isinstance(stages, dict):
            continue
        for stage_state in stages.values():
            if not isinstance(stage_state, dict):
                continue
            counters = stage_state.get("counters") if isinstance(stage_state.get("counters"), dict) else {}
            totals["scope_update"] += _safe_counter(counters.get("scope_update_count"))
            totals["same_stage_retry"] += _safe_counter(counters.get("same_stage_retry_count"))
            totals["read_search_after_failed_proof"] += _safe_counter(counters.get("dev_read_search_after_failed_proof"))
            if stage_state.get("environment_fingerprint_status") == "changed":
                totals["env_fingerprint_changed"] += 1
            self_heal = stage_state.get("self_heal") if isinstance(stage_state.get("self_heal"), dict) else {}
            if self_heal.get("classification"):
                totals["self_heal_proposed"] += 1
            if _safe_counter(self_heal.get("attempt_number")):
                totals["self_heal_applied"] += 1
            if _safe_counter(self_heal.get("attempts_remaining")) == 0 and self_heal:
                totals["self_heal_cannot"] += 1
    for run in runs:
        progress = getattr(run, "progress", None)
        if not isinstance(progress, dict):
            continue
        totals["skill_fanout"] += _safe_counter(progress.get("skill_fanout_count"))
        if progress.get("failed_proof_reused"):
            totals["failed_proof_reused"] += 1
        if progress.get("failed_proof_ignored"):
            totals["failed_proof_ignored"] += 1
    return totals


def _safe_counter(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _latest_context_request(task: Task) -> dict[str, Any] | None:
    reqs = getattr(task, "context_requests", []) or []
    return reqs[-1] if reqs else None


def _event_summary(event: Event) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ts": event.ts,
        "type": event.type,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "persona_id": event.persona_id,
    }
    data.update(_event_display_projection(event))
    data.update(_safe_event_payload(event.payload))
    return data


def _event_display_projection(event: Event) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {}
    kind = _event_display_kind(event.type, payload)
    title = _event_display_title(event.type, payload, kind)
    summary = _safe_display_text(payload.get("summary") or payload.get("status") or payload.get("reason") or "")
    refs: list[dict[str, str]] = []
    for key in ("proof_id", "evidence_id", "packet_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            refs.append({"kind": key.removesuffix("_id"), "id": value.strip()[:160]})
    result: dict[str, Any] = {
        "display_kind": kind,
        "display_title": title,
    }
    if summary:
        result["display_summary"] = summary
    if refs:
        result["artifact_refs"] = refs
    if isinstance(payload.get("redaction_status"), str):
        result["redaction_status"] = payload["redaction_status"]
    return result


def _event_display_kind(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "self_test.recorded":
        return "self_test"
    if event_type == "packet.recorded":
        return "delivery" if str(payload.get("packet_type") or "") == "delivery" else "handoff"
    if event_type == "qa.verdict_recorded":
        return "qa_verdict"
    if event_type in {"task.blocked", "incident.opened"}:
        return "blocker"
    if event_type.startswith("run.tool."):
        return "tool_call"
    if event_type == "run.progress" and str(payload.get("step") or "") in {"reasoning_summary", "decision_summary"}:
        return "thinking_summary"
    return "event"


def _event_display_title(event_type: str, payload: dict[str, Any], kind: str) -> str:
    if kind == "self_test":
        return f"Self-test {payload.get('status') or 'recorded'}"
    if kind == "proof":
        return f"Proof {payload.get('status') or 'attached'}"
    if kind == "delivery":
        return "Delivery packet"
    if kind == "qa_verdict":
        return f"QA verdict {payload.get('verdict') or ''}".strip()
    if kind == "blocker":
        return "Blocker"
    if kind == "tool_call":
        return f"Tool {payload.get('tool_name') or payload.get('tool') or event_type}"
    return event_type


def _safe_display_text(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text or _looks_sensitive_or_pathish(text):
        return ""
    return f"{text[:497]}..." if len(text) > 500 else text


def _safe_event_payload(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "summary", "status", "tool", "tool_name", "state", "elapsed_seconds", "api_calls",
        "phase", "severity", "step", "detail", "duration_ms", "iteration", "max_iterations",
        "compact_count", "exit_code", "total_tokens", "proof_id", "evidence_id", "packet_id", "proof_count",
        "incident_count", "decision_type", "validation_status", "error_class", "next_expected", "stage_id",
        "patch_summary", "code_summary", "file_summary", "changed_files", "files_touched",
        "reasoning_summary", "envelope_id", "decision_count", "continuation_count",
        "model_invocation_count", "close_reason", "next_action_before", "next_action_after",
        "proof_ids_added_count", "incident_ids_opened_count", "session_id_present",
        "worker_session_id", "possession_state", "lease_owner",
        "api_calls_total", "input_tokens_total", "output_tokens_total", "total_tokens_total",
        "tool_turns_total", "would_continue", "watchdog_warnings",
        "autonomy_packet_id", "context_receipt_id", "selected_skill_count",
        "rejected_skill_count", "read_search_limit", "proof_retry_limit",
        "proof_command_limit", "skill_load_limit", "context_event_count",
        "context_proof_count", "context_incident_count", "context_size_estimate",
        "proof_intent", "environment_fingerprint", "environment_fingerprint_status",
        "display_kind", "display_title", "display_summary", "redaction_status", "gate_source",
        "patch_count", "test_count", "proof_count", "read_search_count",
        "new_evidence_count", "packet_repair_count", "invalid_packet_count",
        "malformed_packet_count", "recovery_action", "recovery_reason",
        "scope_recovery", "handoff_target", "waiting_on_persona_id",
    }
    safe: dict[str, Any] = {}
    for key in allowed:
        value = payload.get(key)
        if key == "changed_files" and isinstance(value, list):
            labels = _safe_file_labels(value)
            if labels:
                safe[key] = labels
            continue
        if key == "reasoning_summary" and isinstance(value, str):
            text = " ".join(value.strip().split())
            if not text or _looks_sensitive_or_pathish(text):
                continue
            safe[key] = f"{text[:497]}…" if len(text) > 500 else text
            continue
        if not isinstance(value, (str, int, float, bool)):
            continue
        if isinstance(value, str) and _looks_sensitive_or_pathish(value):
            continue
        safe[key] = value
    return safe


def _safe_file_labels(value: list[Any]) -> list[str]:
    labels: list[str] = []
    allowed_chars = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    for item in value:
        text = str(item or "").strip()
        if not text or _looks_sensitive_or_pathish(text):
            continue
        label = text.replace("\\", "/").rsplit("/", 1)[-1]
        if not label or _looks_sensitive_or_pathish(label):
            continue
        if len(label) > 96 or any(ch not in allowed_chars for ch in label):
            continue
        labels.append(label)
    return labels[:12]


def _looks_sensitive_or_pathish(value: str) -> bool:
    lowered = value.lower()
    sensitive_markers = (
        "secret", "token", "password", "api_key", "apikey", "authorization",
        "bearer", "credential", "cookie", "private_key", "sk-",
    )
    if any(marker in lowered for marker in sensitive_markers):
        return True
    return ":/" in value or "\\" in value or "/" in value


def _run_age_seconds(reference_time: datetime, run: Any) -> float | None:
    heartbeat = _coerce_datetime(getattr(run, "last_heartbeat_at", None))
    started = _coerce_datetime(getattr(run, "started_at", None))
    return _age_seconds(reference_time, heartbeat or started)


def _worker_summary(worker: Any, *, reference_time: datetime) -> dict[str, Any]:
    try:
        return worker_session_summary(worker, reference_time=reference_time)
    except Exception:
        state = getattr(worker, "state", None)
        possession_state = getattr(worker, "possession_state", None)
        return {
            "worker_session_id": getattr(worker, "id", None),
            "task_id": getattr(worker, "task_id", None),
            "persona_id": getattr(worker, "persona_id", None),
            "state": getattr(state, "value", state),
            "active_run_id": getattr(worker, "active_run_id", None),
            "session_id_present": bool(getattr(worker, "session_id", None)),
            "heartbeat_age_seconds": _worker_age_seconds(reference_time, worker),
            "possession_state": getattr(possession_state, "value", possession_state),
        }


def _worker_is_active(worker: Any) -> bool:
    state = getattr(worker, "state", None)
    possession = getattr(worker, "possession_state", None)
    if isinstance(state, WorkerSessionState):
        state_active = state in ACTIVE_WORKER_STATES
    else:
        state_active = str(state or "") in {item.value for item in ACTIVE_WORKER_STATES}
    possession_value = possession if isinstance(possession, PossessionState) else str(possession or "")
    return state_active or possession_value == PossessionState.POSSESSED or possession_value == PossessionState.POSSESSED.value


def _worker_age_seconds(reference_time: datetime, worker: Any) -> float | None:
    heartbeat = _coerce_datetime(getattr(worker, "last_heartbeat_at", None))
    opened = _coerce_datetime(getattr(worker, "opened_at", None))
    return _age_seconds(reference_time, heartbeat or opened)


def _age_seconds(reference_time: datetime, timestamp: datetime | None) -> float | None:
    if timestamp is None:
        return None
    ref = reference_time
    ts = timestamp
    if ref.tzinfo is None and ts.tzinfo is not None:
        ref = ref.replace(tzinfo=ts.tzinfo)
    elif ref.tzinfo is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=ref.tzinfo)
    return max(0.0, (ref - ts).total_seconds())


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None
