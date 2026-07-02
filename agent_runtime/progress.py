from __future__ import annotations

from typing import Any, Callable
import re

from hermes_time import now

from .dev_discipline import update_progress_telemetry
from .events import EventLog
from .models import Event
from .self_test_evidence import record_self_test_from_progress
from .states import RunState
from .store import RunStore

_SAFE_PROGRESS_KEYS = {
    "type", "phase", "severity", "step", "state", "tool", "tool_name", "status",
    "summary", "detail", "elapsed_seconds", "duration_ms", "api_calls", "iteration",
    "max_iterations", "compact_count", "exit_code", "total_tokens", "proof_id", "stage_id",
    "proof_count", "decision_type", "validation_status", "error_class", "next_expected",
    "repo_label", "context_loaded", "patch_summary", "code_summary", "file_summary",
    "changed_files", "files_touched", "reasoning_summary", "tool_call_count",
    "read_search_count", "patch_count", "test_count", "loop_warning",
    "has_patch_progress", "has_test_progress", "has_proof_progress",
    "envelope_id", "decision_count", "continuation_count", "model_invocation_count",
    "close_reason", "next_action_before", "next_action_after", "proof_ids_added_count",
    "incident_ids_opened_count", "session_id_present", "api_calls_total",
    "input_tokens_total", "output_tokens_total", "total_tokens_total", "tool_turns_total",
    "would_continue", "watchdog_warnings",
    "autonomy_packet_id", "context_receipt_id", "selected_skill_count",
    "assignment_id", "persona_instance_id", "assignment_kind",
    "rejected_skill_count", "read_search_limit", "proof_retry_limit",
    "proof_command_limit", "skill_load_limit", "context_event_count",
    "context_proof_count", "context_incident_count", "context_size_estimate",
    "proof_intent", "environment_fingerprint", "environment_fingerprint_status",
    "last_failed_proof_ids", "self_heal_applied", "failed_proof_reused",
    "failed_proof_ignored", "dev_read_search_after_failed_proof", "timing_key",
    "command_label",
}

_INTERNAL_RUN_PROGRESS_KEYS = {
    "repo_baseline",
    "repo_execution",
}


class RunProgressSink:
    def __init__(self, *, run_store: RunStore, event_log: EventLog | None = None, run_id: str):
        self.run_store = run_store
        self.event_log = event_log or EventLog()
        self.run_id = run_id

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        try:
            run = self.run_store.get(self.run_id)
            if run.state in {RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED}:
                return None
            merged_payload = update_progress_telemetry(run.progress, event_type, payload or {})
            safe_payload = _safe_progress_payload(event_type, merged_payload)
            preserved = {
                key: value
                for key, value in (run.progress or {}).items()
                if key in _INTERNAL_RUN_PROGRESS_KEYS
            }
            run.progress = {**preserved, **safe_payload}
            run.last_heartbeat_at = now()
            if not self.run_store.update(run):
                return None
            _maybe_record_self_test(run, event_type, payload or {}, event_log=self.event_log)
            persisted = self.run_store.get(self.run_id)
            if persisted.state in {RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED}:
                return None
            self.event_log.append(
                Event(
                    ts=now(),
                    type=event_type,
                    task_id=run.task_id,
                    run_id=run.id,
                    persona_id=run.persona_id,
                    payload=safe_payload,
                )
            )
        except Exception:
            return None


_CHAT_TRACE_EVENT_TYPES = {"run.tool.started", "run.tool.finished", "run.progress"}
# run.progress payloads that carry one of these keys are real signal (a tool
# step, a command, dev work, or a reasoning summary). Bare "Run progress update"
# rows are dropped so the operator-channel Trace lane stays meaningful, not noisy.
_CHAT_PROGRESS_SIGNAL_KEYS = (
    "tool_name", "tool", "command_label", "reasoning_summary",
    "changed_files", "patch_summary", "code_summary", "file_summary",
)


class ChatProgressSink:
    """Record redaction-safe tool/progress trace events for a conversational
    (non-task) persona chat turn.

    Unlike :class:`RunProgressSink` there is no backing :class:`AgentRun`: an
    operator chat turn runs free of the task/decision pipeline, so there is no
    run row to update and no ``task_id`` to key on. Events are appended to the
    :class:`EventLog` keyed on ``session_id`` + ``persona_id`` instead, which is
    exactly what :func:`persona_chat_trace_summary` scans to surface chat-turn
    tool calls in the Mission Control operator channel's Trace lane.

    Payloads are sanitized through the same :func:`_safe_progress_payload`
    redaction boundary the task lane uses, and every emit is best-effort: a
    telemetry failure must never break the operator's chat reply.
    """

    def __init__(
        self,
        *,
        session_id: str,
        persona_id: str | None,
        run_id: str | None = None,
        event_log: EventLog | None = None,
        before_first_trace: Callable[[dict[str, Any]], None] | None = None,
        on_trace: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.session_id = session_id
        self.persona_id = persona_id
        self.run_id = run_id
        self.event_log = event_log or EventLog()
        self.before_first_trace = before_first_trace
        self.on_trace = on_trace
        self._did_emit_first_trace = False

    def emit(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        try:
            if event_type not in _CHAT_TRACE_EVENT_TYPES:
                return None
            payload = payload or {}
            if event_type == "run.progress" and not _chat_progress_has_signal(payload):
                return None
            safe_payload = _safe_progress_payload(event_type, payload)
            if not self.session_id:
                if self.on_trace is not None:
                    self.on_trace(safe_payload)
                return None
            if not self._did_emit_first_trace:
                self._did_emit_first_trace = True
                if self.before_first_trace is not None:
                    self.before_first_trace(safe_payload)
            if self.on_trace is not None:
                self.on_trace(safe_payload)
            self.event_log.append(
                Event(
                    ts=now(),
                    type=event_type,
                    task_id=None,
                    run_id=self.run_id,
                    persona_id=self.persona_id,
                    payload=safe_payload,
                    session_id=self.session_id,
                )
            )
        except Exception:
            return None

    def callback(self) -> "Callable[[dict[str, Any]], None]":
        """Adapter matching the runner's ``progress_callback`` contract."""

        def _emit(payload: dict[str, Any]) -> None:
            self.emit(str(payload.get("type", "run.progress")), payload)

        return _emit


def _chat_progress_has_signal(payload: dict[str, Any]) -> bool:
    return any(payload.get(key) for key in _CHAT_PROGRESS_SIGNAL_KEYS)


def _maybe_record_self_test(run, event_type: str, payload: dict[str, Any], *, event_log: EventLog) -> None:
    # Observed self-test proofs are additive, redaction-safe records of what the
    # agent actually ran in-session; they populate the HUD "observed" lane for a
    # stage. Capture is UNCONDITIONAL for a task run: it must never depend on a
    # re-loaded RuntimeConfig here, because the run-executing process can resolve
    # a different config than the ticker that owns the authoritative one (config
    # path / cross-process resolution). That mismatch silently dropped every
    # observed proof even with the contract enabled. The downstream gate decides
    # whether an observed proof is *required*; capturing one is always safe.
    try:
        record_self_test_from_progress(run, event_type, payload, event_log=event_log)
    except Exception:
        return


def _safe_progress_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {"type": event_type}
    for key, value in payload.items():
        if key not in _SAFE_PROGRESS_KEYS:
            continue
        if isinstance(value, list) and key == "changed_files":
            labels = _safe_file_labels(value)
            if labels:
                safe[key] = labels
            continue
        if isinstance(value, list) and key == "last_failed_proof_ids":
            labels = _safe_token_labels(value)
            if labels:
                safe[key] = labels
            continue
        if isinstance(value, str) and key == "reasoning_summary":
            text = " ".join(value.strip().split())
            if not text or _looks_sensitive_or_pathish(text):
                continue
            safe[key] = f"{text[:497]}…" if len(text) > 500 else text
            continue
        if isinstance(value, str) and key == "command_label":
            text = " ".join(value.strip().replace("\\", "/").split())
            if not text or _looks_sensitive(text):
                continue
            safe[key] = f"{text[:237]}..." if len(text) > 240 else text
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value) if isinstance(value, str) else value
            if isinstance(text, str) and _looks_sensitive_or_pathish(text):
                continue
            safe[key] = text
    return safe


def _safe_file_labels(value: list[Any]) -> list[str]:
    labels: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or _looks_sensitive_or_pathish(text):
            continue
        label = text.replace("\\", "/").rsplit("/", 1)[-1]
        if not label or _looks_sensitive_or_pathish(label):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", label):
            continue
        labels.append(label)
    return labels[:12]


def _safe_token_labels(value: list[Any]) -> list[str]:
    labels: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or _looks_sensitive_or_pathish(text):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", text):
            continue
        labels.append(text)
    return labels[:12]


def _looks_sensitive_or_pathish(value: str) -> bool:
    lowered = value.lower()
    if _looks_sensitive(value):
        return True
    if ":/" in value or "\\" in value:
        return True
    if value.startswith(("/", "~")):
        return True
    if re.search(r"(^|\s)([A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+", value):
        return True
    return False


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    sensitive_markers = (
        "secret", "token", "password", "api_key", "apikey", "authorization",
        "bearer", "credential", "cookie", "private_key", "sk-", "passwd",
    )
    return any(marker in lowered for marker in sensitive_markers)
