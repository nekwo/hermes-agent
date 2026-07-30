from __future__ import annotations

import re
from typing import Any

from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from .models import AgentPersona, AgentRun
from .packets import latest_packet
from .personas import role_from_persona

_BROAD_TITLE_MARKERS = (
    "swarm",
    "multi-agent",
    "multi agent",
    "backend dev",
    "frontend",
    "backend",
    "mission control agent model",
)
_BROAD_DESCRIPTION_MARKERS = (
    "data types",
    "launcher ui",
    "backend",
    "frontend",
    "large swarm",
    "multi-agent",
    "multiple specialist",
)

_BACKEND_FIRST_FLAGS = frozenset(
    {
        "backend_contract_first",
        "cross_stack_contract_handoff",
        "cross_stack_sequential_handoff",
        "frontend_backend_contract_handoff",
        "sequential_specialist_handoff",
        "sequential_specialists_required",
    }
)

_BACKEND_SLICE_MARKERS = (
    "backend dev first",
    "backend dev operates only",
    "backend proof",
    "backend contract/proof packet",
    "launcher dev is released only after backend",
    "launcher dev is not released",
    "no launcher bridge/ui verification in this first specialist slice",
)

_HARNESS_SUPPORT_REPO_MARKERS = (
    "hermes-agent",
    "hermes_agent",
    "agent-runtime",
    "agent_runtime",
)


def needs_supervisor_slicing(task: Task, *, event_log=None) -> bool:
    """Return true when a PM-ready mission is too broad for first Dev tick.

    This is intentionally conservative: it only catches unsliced multi-surface
    specialist work. Small one-repo fixes still go directly to Dev.
    """

    repos = [str(repo).strip().lower() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()]
    text = " ".join(
        [
            str(getattr(task, "title", "") or ""),
            str(getattr(task, "description", "") or ""),
            " ".join(str(item) for item in (getattr(task, "acceptance_criteria", []) or [])),
        ]
    ).lower()
    if _has_bounded_specialist_handoff_packet(task, event_log=event_log):
        return False
    if _is_backend_first_slice(task, repos=repos, text=text):
        return False
    slicing_repos = _repos_that_require_specialist_slicing(repos)
    multi_repo = len(set(slicing_repos)) >= 2
    broad_title = any(marker in str(getattr(task, "title", "") or "").lower() for marker in _BROAD_TITLE_MARKERS)
    broad_body_hits = sum(1 for marker in _BROAD_DESCRIPTION_MARKERS if marker in text)
    many_acceptance = len(getattr(task, "acceptance_criteria", []) or []) >= 3
    return multi_repo and (broad_title or broad_body_hits >= 2 or many_acceptance)


def _repos_that_require_specialist_slicing(repos: list[str]) -> list[str]:
    """Ignore Harness support scope when counting product specialist surfaces."""

    product_repos = [repo for repo in repos if not _is_harness_support_repo(repo)]
    return product_repos or repos


def _is_harness_support_repo(repo: str) -> bool:
    normalized = str(repo or "").strip().lower().replace("\\", "/")
    return any(marker in normalized for marker in _HARNESS_SUPPORT_REPO_MARKERS)


def _has_bounded_specialist_handoff_packet(task: Task, *, event_log=None) -> bool:
    try:
        packet = latest_packet(task.id, "handoff_packet", event_log=event_log)
    except Exception:
        return False
    body = packet.get("body") if isinstance(packet, dict) else None
    if not isinstance(body, dict):
        return False
    mode = str(body.get("handoff_mode") or "")
    target_owner = str(body.get("target_owner") or "")
    target_repo = str(body.get("target_repo") or "")
    proof_gate = body.get("proof_gate") if isinstance(body.get("proof_gate"), dict) else {}
    if (
        mode in {"single_specialist", "sequential_specialists"}
        and target_owner in {"dev", "backend_dev", "launcher_dev"}
        and target_repo
        and proof_gate.get("required") is True
    ):
        return True
    return (
        mode in {"backend_first_cross_stack", "sequential_specialists"}
        and target_owner == "backend_dev"
        and target_repo == "EterniaBackend"
        and proof_gate.get("required") is True
    )


def _is_backend_first_slice(task: Task, *, repos: list[str], text: str) -> bool:
    """Recognize Neko's narrowed backend-first cross-stack slice.

    A task can still mention Launcher/frontend because that is the downstream
    join gate. The important routing signal is that Neko constrained the first
    specialist pass to Backend Dev and explicitly withheld Launcher/QA release.
    """

    flags = {str(flag).strip().lower() for flag in (getattr(task, "risk_flags", []) or [])}
    if not flags.intersection(_BACKEND_FIRST_FLAGS):
        return False
    repo_text = " ".join(repos)
    if "backend" not in repo_text and "eterniabackend" not in repo_text:
        return False
    if any("launcher" in repo or "frontend" in repo or "eternialauncher" in repo for repo in repos):
        return False
    return any(marker in text for marker in _BACKEND_SLICE_MARKERS)


_PROGRESS_OK_DECISIONS = frozenset({
    DecisionType.PROPOSE_STAGE_PLAN,
    DecisionType.CORRECT_STAGE,
    DecisionType.REQUEST_TEST_RUN,
    DecisionType.HAND_OFF,
    DecisionType.BLOCK,
    DecisionType.ESCALATE,
    DecisionType.NEEDS_CONTEXT,
    DecisionType.REQUEST_FILE_READS,
    DecisionType.REPORT_ISSUE_DISCOVERY,
})

_BUDGET_PRESSURE_OK_DECISIONS = frozenset({
    DecisionType.REQUEST_TEST_RUN,
    DecisionType.HAND_OFF,
    DecisionType.BLOCK,
    DecisionType.NEEDS_CONTEXT,
})

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


def validate_dev_progress_gate(persona: AgentPersona, run: AgentRun, decision: AgentDecision) -> None:
    """Fail noisy Dev handoffs that spent the whole small budget with no proofful progress."""

    if role_from_persona(persona) != "dev":
        return
    progress = run.progress if isinstance(run.progress, dict) else {}
    _validate_failed_proof_reuse(progress, decision)
    if _has_budget_pressure(progress):
        if decision.type in _BUDGET_PRESSURE_OK_DECISIONS:
            return
        if decision.type == DecisionType.REQUEST_QA_REVIEW and _decision_has_proof_ids(decision):
            return
        raise DecisionPayloadInvalid(
            "Dev budget pressure gate failed: run is approaching budget without a proof-oriented handoff; stop exploration and emit hand_off, or block with exact evidence so Neko can steer."
        )
    if decision.type in _PROGRESS_OK_DECISIONS:
        return
    if _has_empirical_progress(progress):
        return
    api_calls = _safe_int((run.llm or {}).get("api_calls"))
    max_api_calls = _safe_int(getattr(run, "max_api_calls", None))
    tool_calls = _safe_int(progress.get("tool_call_count")) or 0
    threshold = max_api_calls if max_api_calls is not None else 6
    if (api_calls is not None and api_calls >= threshold) or tool_calls >= 6:
        raise DecisionPayloadInvalid(
            "Dev early progress gate failed: high-call run produced no patch/test/proof progress and did not split or block; return a smaller stage plan, request_test_run, or block with the exact prerequisite."
        )


def _has_empirical_progress(progress: dict[str, Any]) -> bool:
    return any(
        progress.get(key) is True or (_safe_int(progress.get(key)) or 0) > 0
        for key in (
            "has_patch_progress",
            "has_test_progress",
            "has_proof_progress",
            "patch_count",
            "test_count",
            "proof_count",
        )
    )


def _has_budget_pressure(progress: dict[str, Any]) -> bool:
    if str(progress.get("step") or "") == "budget_pressure":
        return True
    ratio = progress.get("budget_ratio")
    try:
        return float(ratio) >= 0.8
    except (TypeError, ValueError):
        return False


def _decision_has_proof_ids(decision: AgentDecision) -> bool:
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    proof_ids = payload.get("proof_ids")
    return isinstance(proof_ids, list) and any(str(item).strip() for item in proof_ids)


def _validate_failed_proof_reuse(progress: dict[str, Any], decision: AgentDecision) -> None:
    failed_ids = [str(item).strip() for item in (progress.get("last_failed_proof_ids") or []) if str(item).strip()] if isinstance(progress.get("last_failed_proof_ids"), list) else []
    if not failed_ids or decision.type != DecisionType.REQUEST_TEST_RUN:
        return
    if _environment_changed(progress.get("environment_fingerprint_status")) or progress.get("self_heal_applied") is True:
        return
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    text = " ".join(str(item) for item in (payload.get("commands") or []))
    text += " " + str(decision.summary or "") + " " + str(decision.rationale or "")
    if any(proof_id in text for proof_id in failed_ids):
        payload["failed_proof_ids"] = _dedupe_strings(_safe_string_list(payload.get("failed_proof_ids")), failed_ids)
        return
    payload["failed_proof_ids"] = _dedupe_strings(_safe_string_list(payload.get("failed_proof_ids")), failed_ids)
    payload["failed_proof_auto_attached"] = True


def _dedupe_strings(existing: list[Any], additions: list[str]) -> list[str]:
    values: list[str] = []
    for item in [*existing, *additions]:
        text = str(item).strip()
        if text and text not in values:
            values.append(text)
    return values


def _safe_string_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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


def _environment_changed(value: Any) -> bool:
    return str(value or "").strip().lower().startswith("changed")


def _safe_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
