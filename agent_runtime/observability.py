from __future__ import annotations

from datetime import datetime
from typing import Any

from hermes_time import now

from .incidents import CRITICAL_INCIDENT_KINDS
from .models import Event, Incident
from .states import RunState
from .store import ACTIVE_RUN_STATES

DEFAULT_RUN_STALLED_AFTER_SECONDS = 900
DELIVERY_EVIDENCE_INCIDENT_KINDS = frozenset({"patch_landed_nowhere", "stage_no_progress"})

# S28 removed the three parameters both callers passed as literals -- ``tasks``
# (a ``[]`` since S8), ``proofs`` (a ``[]`` since the S6 store removal), and
# ``daemon_status`` (``None``; the Mission Daemon was retired before this wave).
# With them went every row they alone fed: ``signals.open_tasks`` /
# ``proofs_total`` / ``stale_daemon`` / ``repeated_context_request_tasks`` /
# ``untriaged_issue_discoveries``, the whole ``freshness.daemon_*`` block, the
# daemon / context-request / issue-discovery intervention families and their
# ``_risk_if_ignored`` / ``_allowed_actions`` arms, ``_latest_context_request``,
# the ``scope_control.untriaged_issue_discoveries`` import, and the task half of
# ``_self_heal_signals``. S21 (``c12e6850d``) identified this cut and deferred
# it only because ``_cmd_observe`` owned the keywords from another lane.


def build_observability(
    *,
    runs: list[Any],
    incidents: list[Incident],
    events: list[Event] | None = None,
    reference_time: datetime | None = None,
    run_stalled_after_seconds: int = DEFAULT_RUN_STALLED_AFTER_SECONDS,
    execution_mode: str = "manual",
) -> dict[str, Any]:
    """Build redaction-safe Mission Control observability envelope.

    This envelope is intended for Launcher and CLI health surfaces. It exposes
    counts, freshness, and concrete intervention handles, but never raw model
    outputs, incident summaries, file paths, proof artifact paths, or secrets.
    """

    ref = reference_time or now()
    open_incidents = [incident for incident in incidents if incident.closed_at is None]
    active_runs = [run for run in runs if run.state in ACTIVE_RUN_STATES]
    running_runs = [run for run in active_runs if run.state == RunState.RUNNING]
    queued_runs = [run for run in active_runs if run.state == RunState.QUEUED]
    waiting_runs = [run for run in active_runs if run.state in {RunState.WAITING_ON_TOOL, RunState.WAITING_ON_APPROVAL}]

    stalled_runs = [
        run
        for run in active_runs
        if _run_age_seconds(ref, run) is not None and _run_age_seconds(ref, run) > run_stalled_after_seconds
    ]
    interventions: list[dict[str, Any]] = []
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
            "stalled_run_threshold_seconds": run_stalled_after_seconds,
        },
        "signals": {
            "running_runs": len(running_runs),
            "active_runs": len(active_runs),
            "queued_runs": len(queued_runs),
            "waiting_runs": len(waiting_runs),
            "open_incidents": len(open_incidents),
            "stalled_running_runs": len(stalled_runs),
            "self_heal": _self_heal_signals(runs),
        },
        "interventions": interventions,
        "active_runs": [_run_summary(run) for run in active_runs],
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
        "risk_if_ignored": _risk_if_ignored(severity),
        "allowed_actions": _allowed_actions(kind, subject),
        "expires_at": expires_at,
        # S28: ``context_request_id`` / ``discovery_id`` were only ever supplied
        # by the two task-sourced intervention families, so they could no longer
        # be non-``None`` for any surviving caller.
        "safe_refs": {
            key: value
            for key, value in {
                "task_id": task_id,
                "run_id": run_id,
                "incident_id": extra.get("incident_id"),
                # S56: ``worker_session_id`` stood here. The
                # ``worker_stale_heartbeat`` family was its only supplier.
            }.items()
            if value
        },
    }
    data.update(extra)
    return data


def _risk_if_ignored(severity: str) -> str:
    # S28: the context-request arm went with the task-sourced interventions that
    # were its only producers.
    if severity in {"critical", "high"}:
        return "Mission progress can remain blocked or drift from Harness truth."
    return "Mission Control may remain degraded until this is resolved."


def _allowed_actions(kind: str, subject: str) -> list[str]:
    # S28: the daemon, context-request, and issue-discovery arms went with the
    # intervention families that produced those kinds. S56 took the
    # ``worker_session`` arm the same way — its only producer was the
    # ``worker_stale_heartbeat`` family. Every arm below is reachable from a
    # live parameter (``incidents``, ``runs``).
    if kind in DELIVERY_EVIDENCE_INCIDENT_KINDS:
        return ["answer_intervention", "cancel_run", "rescope"]
    if kind == "open_incident":
        return ["answer_intervention", "retry_stage"]
    if subject == "run":
        return ["cancel_run", "retry_stage"]
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
        "decision_type": decision_type,
        "session_id": getattr(run, "session_id", None),
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


def _self_heal_signals(runs: list[Any]) -> dict[str, Any]:
    # S28: seven counters (``scope_update``, ``same_stage_retry``,
    # ``read_search_after_failed_proof``, ``env_fingerprint_changed``, and the
    # three ``self_heal_*`` rows) were summed over ``task.harness_self_heal``
    # stage state. The task list has been a ``[]`` literal in both callers since
    # S8, so those seven could only ever report 0. The three below read
    # ``run.progress`` and still move.
    totals = {
        "skill_fanout": 0,
        "failed_proof_reused": 0,
        "failed_proof_ignored": 0,
    }
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
    # S21: the ``qa.verdict_recorded`` arm and ``task.blocked`` were dropped —
    # S15 de-registered both, so ``EventLog.append`` refuses them and no event
    # reaching this classifier can carry either type. ``incident.opened`` is the
    # surviving producer of the ``blocker`` kind.
    if event_type == "self_test.recorded":
        return "self_test"
    if event_type == "run.closed":
        return "run_closed"
    if event_type == "packet.recorded":
        return "delivery" if str(payload.get("packet_type") or "") == "delivery" else "handoff"
    if event_type == "incident.opened":
        return "blocker"
    if event_type.startswith("run.tool."):
        return "tool_call"
    if event_type == "run.progress" and str(payload.get("step") or "") in {"reasoning_summary", "decision_summary"}:
        return "thinking_summary"
    return "event"


def _event_display_title(event_type: str, payload: dict[str, Any], kind: str) -> str:
    # ``kind`` only ever comes from ``_event_display_kind``. S21 dropped the
    # ``proof`` arm (that classifier has never returned ``"proof"`` — it was
    # unreachable before the mission lane was touched) and the ``qa_verdict``
    # arm (its producer went with ``qa.verdict_recorded``).
    if kind == "self_test":
        return f"Self-test {payload.get('status') or 'recorded'}"
    if kind == "run_closed":
        return f"Run closed as {payload.get('state') or payload.get('status') or 'recorded'}"
    if kind == "delivery":
        return "Delivery packet"
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


# S56 removed the whole worker half of this envelope with the write lane that
# was its only producer: the ``worker_sessions`` parameter (``_cmd_observe``
# already passed a ``[]`` literal, and ``status.py`` was the only caller that
# passed rows), the ``worker_stale_heartbeat`` intervention family, the
# ``active_worker_sessions`` / ``stale_worker_sessions`` signals, the
# ``worker_sessions`` row list, and the three helpers only they reached
# (``_worker_summary``, ``_worker_is_active``, ``_worker_age_seconds``). No
# worker can reach an ACTIVE state now that nothing writes one, so every one of
# those was a constant by construction.


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
