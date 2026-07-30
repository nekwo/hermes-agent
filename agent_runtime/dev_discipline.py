"""Dev progress telemetry merged onto each ``run.*`` progress payload.

S27 removed this module's mission-lane half: ``needs_supervisor_slicing`` (the
"is this mission too broad for a first Dev tick?" router) and
``validate_dev_progress_gate`` (the budget/proof handoff gate), plus their
marker tables and helpers. Both took a ``Task`` deleted in S8 and were called
only from the dispatch loop deleted in S5; ``progress.py`` imports
``update_progress_telemetry`` and nothing else.
"""

from __future__ import annotations

import re
from typing import Any

_PERSISTENT_PROGRESS_KEYS = frozenset(
    {
        "autonomy_packet_id",
        "context_receipt_id",
        "selected_skill_count",
        "rejected_skill_count",
        "read_search_limit",
        "proof_retry_limit",
        "proof_command_limit",
        "skill_load_limit",
        "context_event_count",
        "context_proof_count",
        "context_incident_count",
        "context_size_estimate",
        "proof_intent",
        "environment_fingerprint",
        "environment_fingerprint_status",
        "last_failed_proof_ids",
        "self_heal_applied",
        "failed_proof_reused",
        "failed_proof_ignored",
        "dev_read_search_after_failed_proof",
        "has_patch_progress",
        "has_test_progress",
        "has_proof_progress",
    }
)


def update_progress_telemetry(previous: dict[str, Any] | None, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Merge redaction-safe aggregate Dev discipline counters into the latest payload."""

    merged = dict(payload)
    previous = previous if isinstance(previous, dict) else {}
    for key in _PERSISTENT_PROGRESS_KEYS:
        if key in previous and key not in merged:
            merged[key] = previous[key]
    tool_count = _safe_int(previous.get("tool_call_count")) or 0
    read_search_count = _safe_int(previous.get("read_search_count")) or 0
    patch_count = _safe_int(previous.get("patch_count")) or 0
    test_count = _safe_int(previous.get("test_count")) or 0
    proof_count = _safe_int(previous.get("proof_count")) or 0

    phase = str(payload.get("phase") or "").lower()
    step = str(payload.get("step") or "").lower()
    tool = str(payload.get("tool_name") or payload.get("tool") or "").lower()
    summary = str(payload.get("summary") or "").lower()

    is_completed_tool_event = event_type == "run.tool.finished"

    if is_completed_tool_event:
        tool_count += 1
    if is_completed_tool_event and tool in {"read_file", "search_files", "browser_snapshot", "session_search"}:
        read_search_count += 1
        if previous.get("last_failed_proof_ids"):
            merged["dev_read_search_after_failed_proof"] = (_safe_int(previous.get("dev_read_search_after_failed_proof")) or 0) + 1
    if is_completed_tool_event and (phase == "dev_work" or step in {"patch", "write_file", "code_edit"} or tool in {"patch", "write_file"}):
        patch_count += 1
    if is_completed_tool_event and tool in {"terminal", "execute_code"} and _looks_like_test(summary):
        test_count += 1
    if event_type.startswith("proof") or phase == "proof" or payload.get("proof_id"):
        proof_count += 1

    merged["tool_call_count"] = tool_count
    merged["read_search_count"] = read_search_count
    merged["patch_count"] = patch_count
    merged["test_count"] = test_count
    merged["proof_count"] = proof_count
    if patch_count > 0:
        merged["has_patch_progress"] = True
    if test_count > 0:
        merged["has_test_progress"] = True
    if proof_count > 0:
        merged["has_proof_progress"] = True
    read_search_limit = _safe_int(merged.get("read_search_limit")) or 4
    if read_search_count >= read_search_limit and patch_count == 0:
        merged["loop_warning"] = "read_search_without_patch_threshold"
    elif previous.get("loop_warning"):
        merged["loop_warning"] = previous.get("loop_warning")
    return merged


def _looks_like_test(value: str) -> bool:
    return bool(re.search(r"\b(pytest|flutter test|dart test|npm test|cargo test|go test|passed|fail(ed|ure)?)\b", value.lower()))


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
