from __future__ import annotations

from typing import Any, Callable
import re

from hermes_time import now

from .dev_discipline import update_progress_telemetry
from .errors import EventPayloadTooLarge
from .events import EventLog
from .models import Event
from .child_events import emit_child_progress
from .config import load_root_runtime_config
from .self_test_evidence import record_self_test_from_progress
from .states import RunState
from .store import RunStore
from .redaction_mode import redaction_observe_enabled

_SAFE_PROGRESS_KEYS = {
    "type", "event_id", "phase", "severity", "step", "state", "tool", "tool_name", "status",
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
    # Operator-console detail lane (Mission Control only — the Telegram-safe
    # field stays the path-stripped command_label). These carry real commands,
    # tool targets, changed paths, and bounded output tails so a goal run reads
    # as streamed work, not turn overviews. Secrets are scrubbed per-line;
    # sizes are bounded below to respect the 4KB event payload cap.
    "command_full", "output", "target_label", "changed_paths", "skill_name",
    # First-class agent-to-agent dispatch (agent_chat_send): the target persona
    # and the FULL order, so the operator console shows exactly what each
    # teammate was told without parsing the 90-char-excerpted target_label prose.
    "dispatch_target", "dispatch_order",
    # Generic tool-call input/result record for tools with no dedicated field
    # (non-terminal, non-dev-work): a bounded key-per-line rendering of the raw
    # invocation and result, produced by profile_runner._attach_tool_io. This is
    # what lets the operator console expand ANY tool row instead of showing
    # "no input or result detail was emitted".
    "tool_input", "tool_result",
}

# Bounds for the operator-detail fields (event payload cap is 4096 bytes).
_OPERATOR_COMMAND_FULL_MAX = 500
_OPERATOR_TARGET_MAX = 300
_OPERATOR_OUTPUT_TAIL_MAX = 1200
_OPERATOR_PATHS_MAX = 12
_OPERATOR_DISPATCH_TARGET_MAX = 120
_OPERATOR_DISPATCH_ORDER_MAX = 1500
# Producer bounds are 1000/1600 (profile_runner) plus a one-line truncation
# marker; these re-scrub bounds sit just above so the marker itself is never
# re-truncated into garbage.
_OPERATOR_TOOL_INPUT_MAX = 1100
_OPERATOR_TOOL_RESULT_MAX = 1700

_INTERNAL_RUN_PROGRESS_KEYS = {
    "repo_baseline",
    "repo_execution",
}


class RunProgressSink:
    def __init__(self, *, run_store: RunStore, event_log: EventLog | None = None, run_id: str, config=None):
        self.run_store = run_store
        self.event_log = event_log or EventLog()
        self.run_id = run_id
        self.config = config or load_root_runtime_config()

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
            _maybe_record_visual_screenshot(run, event_type, payload or {}, event_log=self.event_log)
            persisted = self.run_store.get(self.run_id)
            if persisted.state in {RunState.COMPLETED, RunState.FAILED, RunState.STALE, RunState.CANCELLED}:
                return None
            # ``phase: timing`` run.progress is pure performance telemetry, not a
            # state-changing fact. Its durations are already rolled up, per turn,
            # into the observability ``timing`` / ``profile_timing`` aggregate
            # (persona_runtime / profile_runner build them independently of this
            # event), and NO durable reader consumes the per-measurement events:
            # persona_chat_history keeps only signal-bearing progress and
            # observability keeps only ``reasoning_summary``/``decision_summary``
            # steps — timing carries neither. So it was ~74% of events.jsonl read
            # by nothing. The live ``run.progress`` snapshot + heartbeat were
            # already updated above (liveness/real-time telemetry unaffected); the
            # durable event log is the state-fact authority, and timing does not
            # belong in it. Prune it at this one chokepoint — the only place run
            # timing is persisted (the chat sink already drops it via the
            # signal-key gate). This is a policy, not a silent drop: the value is
            # retained in the aggregate, and the run's live progress still carries
            # the latest timing.
            is_timing_progress = (
                event_type == "run.progress"
                and str((payload or {}).get("phase") or "") == "timing"
            )
            if not is_timing_progress:
                _append_bounded_event(
                    self.event_log,
                    Event(
                        ts=now(),
                        type=event_type,
                        task_id=run.task_id,
                        run_id=run.id,
                        persona_id=run.persona_id,
                        payload=safe_payload,
                    ),
                )
                if event_type == "run.progress":
                    emit_child_progress(run=persisted, payload=safe_payload, config=self.config, event_log=self.event_log)
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
        turn_id: str | None = None,
        event_log: EventLog | None = None,
        before_first_trace: Callable[[dict[str, Any]], None] | None = None,
        on_trace: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.session_id = session_id
        self.persona_id = persona_id
        self.run_id = run_id
        # Turn key (the operator's client_message_id token): stamped on every
        # event so downstream projections carry ONE reconciliation identity for
        # the whole turn instead of clients matching rows by content.
        self.turn_id = turn_id
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
            # before_first_trace is the operator-channel "agent started tool
            # work" hook (the mission-chat handler persists an acknowledgment
            # row from it). Latch it on the first REAL tool start only: a
            # reasoning-summary run.progress event also reaches this sink (it
            # belongs in the Trace lane), and latching on it persisted a
            # canned "I'll check that now…" row on every tool-less chat turn —
            # a phantom transcript row with no client_message_id that popped
            # in above the streamed reply at snapshot reconcile and made the
            # console order jump.
            if not self._did_emit_first_trace and event_type == "run.tool.started":
                self._did_emit_first_trace = True
                if self.before_first_trace is not None:
                    self.before_first_trace(safe_payload)
            if self.on_trace is not None:
                self.on_trace(safe_payload)
            _append_bounded_event(
                self.event_log,
                Event(
                    ts=now(),
                    type=event_type,
                    task_id=None,
                    run_id=self.run_id,
                    persona_id=self.persona_id,
                    payload=safe_payload,
                    session_id=self.session_id,
                    turn_id=self.turn_id,
                ),
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


def _append_bounded_event(event_log: EventLog, event: Event) -> None:
    """Append, degrading oversized payloads instead of silently dropping them.

    The operator detail fields (``tool_result`` / ``output`` / ``tool_input`` /
    ``dispatch_order``) are the variable-size fields that can push a payload
    past the 4KB event cap; a too-large event previously vanished into the
    sink's bare except. Shed them largest-and-least-critical first so the tool
    row itself (command, target, status, files, the ``→ target`` chip) always
    survives. If the row is still too large after all four, the final append
    re-raises to the sink's best-effort boundary (unchanged terminal behavior).
    """

    try:
        event_log.append(event)
        return
    except EventPayloadTooLarge:
        payload = dict(event.payload or {})
    for drop_key, marker in (
        ("tool_result", "tool_result_truncated"),
        ("output", "output_truncated"),
        ("tool_input", "tool_input_truncated"),
        ("dispatch_order", "dispatch_order_truncated"),
    ):
        if drop_key not in payload:
            continue
        payload.pop(drop_key, None)
        payload[marker] = True
        try:
            event_log.append(_rebuild_event_payload(event, payload))
            return
        except EventPayloadTooLarge:
            continue
    event_log.append(_rebuild_event_payload(event, payload))


def _rebuild_event_payload(event: Event, payload: dict[str, Any]) -> Event:
    return Event(
        ts=event.ts,
        type=event.type,
        task_id=event.task_id,
        run_id=event.run_id,
        persona_id=event.persona_id,
        payload=payload,
        session_id=event.session_id,
        turn_id=event.turn_id,
    )


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


def _maybe_record_visual_screenshot(run, event_type: str, payload: dict[str, Any], *, event_log: EventLog) -> None:
    # Root-node substrate only: this recorder writes Proof rows as a side effect of
    # the tool stream. Gated on root_node_mode so the flag-off legacy path is
    # byte-for-byte unchanged (no new proofs from a shared progress sink).
    return


def _safe_progress_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {"type": event_type}
    observe = redaction_observe_enabled()
    for key, value in payload.items():
        if key not in _SAFE_PROGRESS_KEYS:
            if observe:
                safe[key] = _observe_value(value)
                _mark_would_redact(safe, key, "unsupported_progress_key")
            continue
        if isinstance(value, list) and key == "changed_files":
            labels = _safe_file_labels(value)
            if labels:
                safe[key] = labels
            continue
        if isinstance(value, list) and key == "changed_paths":
            paths = _safe_operator_path_list(value)
            if paths:
                safe[key] = paths
            continue
        if isinstance(value, str) and key == "command_full":
            text = _safe_operator_line(value, limit=_OPERATOR_COMMAND_FULL_MAX)
            if text:
                safe[key] = text
            elif observe:
                safe[key] = _observe_text(value, limit=_OPERATOR_COMMAND_FULL_MAX)
                _mark_would_redact(safe, key, "operator_command")
            continue
        if isinstance(value, str) and key == "target_label":
            text = _safe_operator_line(value, limit=_OPERATOR_TARGET_MAX)
            if text:
                safe[key] = text
            elif observe:
                safe[key] = _observe_text(value, limit=_OPERATOR_TARGET_MAX)
                _mark_would_redact(safe, key, "operator_target")
            continue
        if isinstance(value, str) and key == "dispatch_target":
            text = " ".join(value.strip().split())
            if text and not _looks_sensitive(text):
                safe[key] = text[:_OPERATOR_DISPATCH_TARGET_MAX]
            elif observe and text:
                safe[key] = _observe_text(value, limit=_OPERATOR_DISPATCH_TARGET_MAX)
                _mark_would_redact(safe, key, "dispatch_target")
            continue
        if isinstance(value, str) and key == "dispatch_order":
            text = _safe_dispatch_order(value)
            if text:
                safe[key] = text
            elif observe:
                safe[key] = _observe_text(value, limit=_OPERATOR_DISPATCH_ORDER_MAX)
                _mark_would_redact(safe, key, "dispatch_order")
            continue
        if isinstance(value, str) and key == "output":
            text = _safe_operator_output_tail(value)
            if text:
                safe[key] = text
            continue
        if isinstance(value, str) and key in ("tool_input", "tool_result"):
            limit = _OPERATOR_TOOL_INPUT_MAX if key == "tool_input" else _OPERATOR_TOOL_RESULT_MAX
            text = _safe_operator_block_head(value, limit=limit)
            if text:
                safe[key] = text
            continue
        if isinstance(value, str) and key == "skill_name":
            text = " ".join(value.strip().split())
            if text and not _looks_sensitive(text) and len(text) <= 120:
                safe[key] = text
            elif observe:
                safe[key] = _observe_text(value, limit=120)
                _mark_would_redact(safe, key, "skill_name")
            continue
        if isinstance(value, list) and key == "last_failed_proof_ids":
            labels = _safe_token_labels(value)
            if labels:
                safe[key] = labels
            continue
        if isinstance(value, str) and key == "reasoning_summary":
            text = " ".join(value.strip().split())
            if not text or _looks_sensitive_or_pathish(text):
                if observe and text:
                    safe[key] = _observe_text(text, limit=500)
                    _mark_would_redact(safe, key, "reasoning_summary")
                continue
            safe[key] = f"{text[:497]}…" if len(text) > 500 else text
            continue
        if isinstance(value, str) and key == "command_label":
            text = " ".join(value.strip().replace("\\", "/").split())
            if not text or _looks_sensitive(text):
                if observe and text:
                    safe[key] = _observe_text(text, limit=240)
                    _mark_would_redact(safe, key, "command_label")
                continue
            safe[key] = f"{text[:237]}..." if len(text) > 240 else text
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            text = str(value) if isinstance(value, str) else value
            if isinstance(text, str) and _looks_sensitive_or_pathish(text):
                if observe:
                    safe[key] = _observe_text(text, limit=500)
                    _mark_would_redact(safe, key, "scalar_progress_value")
                continue
            safe[key] = text
    return safe


def _mark_would_redact(payload: dict[str, Any], key: str, reason: str) -> None:
    markers = payload.setdefault("would_redact", {})
    if isinstance(markers, dict):
        markers[str(key)] = reason


def _observe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _observe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_observe_value(item) for item in value[:200]]
    if isinstance(value, tuple):
        return [_observe_value(item) for item in value[:200]]
    if isinstance(value, str):
        return _observe_text(value, limit=1600)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _observe_text(str(value), limit=500)


def _observe_text(value: str, *, limit: int) -> str:
    lines = [
        "[redacted line — contained a secret]" if _looks_sensitive(line) else line
        for line in str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    text = "\n".join(lines).strip()
    if len(text) > limit:
        return f"{text[: max(0, limit - 1)]}…"
    return text


def _safe_operator_line(value: str, *, limit: int) -> str | None:
    """One-line operator-console text: paths allowed, secrets blocked, bounded."""

    text = " ".join(value.strip().split())
    if not text or _looks_sensitive(text):
        return None
    return f"{text[: limit - 1]}…" if len(text) > limit else text


def _safe_dispatch_order(value: str) -> str | None:
    """Redaction boundary for the full agent-to-agent order: drop any secret-
    bearing line, keep the rest with newline structure intact (never whitespace-
    collapsed), bounded at :data:`_OPERATOR_DISPATCH_ORDER_MAX`. Consistent with
    the profile-runner scrub that produces the field; idempotent when re-applied.
    """

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    kept = [line for line in text.split("\n") if not _looks_sensitive(line)]
    order = "\n".join(kept).strip()
    if not order:
        return None
    if len(order) > _OPERATOR_DISPATCH_ORDER_MAX:
        order = f"{order[: _OPERATOR_DISPATCH_ORDER_MAX - 1]}…"
    return order


def _safe_operator_output_tail(value: str) -> str | None:
    """Bounded output tail with line structure kept and secret lines redacted.

    The 4KB event payload cap is the hard ceiling; this keeps the newest
    ~1.2KB, which is the part of a command's output the operator acts on.
    """

    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    lines = [
        "[redacted line — contained a secret]" if _looks_sensitive(line) else line
        for line in text.split("\n")
    ]
    text = "\n".join(lines)
    if len(text) > _OPERATOR_OUTPUT_TAIL_MAX:
        text = f"…(earlier output truncated)…\n{text[-_OPERATOR_OUTPUT_TAIL_MAX:]}"
    return text


def _safe_operator_block_head(value: str, *, limit: int) -> str | None:
    """Bounded HEAD of a key-per-line tool input/result block, line structure
    kept and secret-bearing lines redacted. Head-biased (unlike the output
    tail): the leading keys are what the operator reads first. A block whose
    EVERY line was redacted carries zero signal — dropped whole."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return None
    kept_any = False
    lines: list[str] = []
    for line in text.split("\n"):
        if _looks_sensitive(line):
            lines.append("[redacted line — contained a secret]")
        else:
            lines.append(line)
            if line.strip():
                kept_any = True
    if not kept_any:
        return None
    text = "\n".join(lines).strip()
    if not text:
        return None
    if len(text) > limit:
        text = f"{text[:limit]}\n…(rest truncated)…"
    return text


def _safe_operator_path_list(value: list[Any]) -> list[str]:
    """Operator-grade changed-path list: RELATIVE paths only, bounded."""

    paths: list[str] = []
    for item in value:
        text = " ".join(str(item or "").strip().split()).replace("\\", "/")
        if not text or _looks_sensitive(text):
            continue
        if re.match(r"^([A-Za-z]:/|//|/|~)", text):
            continue
        if len(text) > 200:
            text = f"…{text[-199:]}"
        if text not in paths:
            paths.append(text)
        if len(paths) >= _OPERATOR_PATHS_MAX:
            break
    return paths


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
