from __future__ import annotations

import json

from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from .decision_payload_contracts import validate_payload_keys
from .packets import validate_decision_packets
from .scope_control import validate_discovery_payload, validate_triage_payload


def require_keys(payload: dict, *keys: str) -> None:
    missing = [key for key in keys if key not in payload]
    if missing:
        raise DecisionPayloadInvalid(f"missing payload keys: {missing}")


def _list_of_strings(payload: dict, key: str, *, required: bool = False) -> list[str]:
    if key not in payload:
        if required:
            raise DecisionPayloadInvalid(f"missing payload key: {key}")
        return []
    value = payload[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise DecisionPayloadInvalid(f"{key} must be a list of non-empty strings")
    return [item.strip() for item in value]


def _normalize_qa_verdict_formatter(decision: AgentDecision) -> None:
    """Losslessly flatten common QA finding formatter drift to string rows.

    The contract remains a list of non-empty strings. Scalar strings are
    wrapped unchanged, structured JSON findings are serialized unchanged in
    meaning, and empty formatter rows are discarded. Unsupported primitives
    remain invalid and enter bounded contract repair.
    """

    if decision.type != DecisionType.QA_VERDICT:
        return
    findings = decision.payload.get("findings")
    if isinstance(findings, str) and findings.strip():
        decision.payload["findings"] = [findings]
        return
    if isinstance(findings, dict):
        decision.payload["findings"] = [json.dumps(findings, sort_keys=True, separators=(",", ":"))] if findings else []
        return
    if isinstance(findings, list):
        normalized: list[object] = []
        for finding in findings:
            if isinstance(finding, str):
                if finding.strip():
                    normalized.append(finding)
            elif isinstance(finding, (dict, list)) and finding:
                normalized.append(json.dumps(finding, sort_keys=True, separators=(",", ":")))
            else:
                normalized.append(finding)
        decision.payload["findings"] = normalized


def validate_planning_decision(decision: AgentDecision) -> None:
    p = decision.payload
    _normalize_qa_verdict_formatter(decision)
    validate_payload_keys(decision)
    validate_decision_packets(decision)
    if decision.type == DecisionType.PROPOSE_ACCEPTANCE:
        require_keys(p, "objective", "acceptance_criteria")
        if not str(p["objective"]).strip():
            raise DecisionPayloadInvalid("objective is required")
        _list_of_strings(p, "acceptance_criteria", required=True)
        _list_of_strings(p, "non_goals")
        _list_of_strings(p, "affected_repos")
        _list_of_strings(p, "suggested_roles")
        _list_of_strings(p, "risk_flags")
        if "requires_visual_proof" in p and not isinstance(p["requires_visual_proof"], bool):
            raise DecisionPayloadInvalid("requires_visual_proof must be boolean")
        if "mission_plan" in p or "mission_plan_patch" in p or "release_stage_id" in p:
            from .mission_plan import validate_mission_plan_payload

            validate_mission_plan_payload(p)
    elif decision.type == DecisionType.REQUEST_FILE_READS:
        _list_of_strings(p, "paths", required=True)
        if not str(p.get("reason", "")).strip():
            raise DecisionPayloadInvalid("reason is required")
    elif decision.type == DecisionType.HAND_OFF:
        if "known_gaps" in p:
            _list_of_strings(p, "known_gaps")
        for forbidden in ("delivery", "work_status", "changed_files", "proof_ids"):
            if forbidden in p:
                raise DecisionPayloadInvalid(f"hand_off must not declare observed field {forbidden}; Harness derives it from diff/trace/gate")
    elif decision.type == DecisionType.ESCALATE:
        require_keys(p, "title", "summary")
        if not str(p.get("title", "")).strip() or not str(p.get("summary", "")).strip():
            raise DecisionPayloadInvalid("escalate requires title and summary")
        if p.get("severity", "medium") not in {"low", "medium", "high", "critical"}:
            raise DecisionPayloadInvalid("escalate severity must be low, medium, high, or critical")
        _list_of_strings(p, "evidence")
    elif decision.type == DecisionType.SCOPE_ROUTE:
        require_keys(p, "objective", "acceptance_criteria", "target_owner", "target_repo", "proof_gate")
        if not str(p.get("objective", "")).strip():
            raise DecisionPayloadInvalid("scope_route objective is required")
        _list_of_strings(p, "acceptance_criteria", required=True)
        if str(p.get("target_owner")) not in {"dev", "backend_dev", "qa", "neko_supervisor", "human"}:
            raise DecisionPayloadInvalid("scope_route target_owner is invalid")
        if str(p.get("target_repo")) not in {"EterniaLauncher", "EterniaBackend", "hermes-agent", "none"}:
            raise DecisionPayloadInvalid("scope_route target_repo is invalid")
        if not isinstance(p.get("proof_gate"), dict):
            raise DecisionPayloadInvalid("scope_route proof_gate must be an object")
    elif decision.type == DecisionType.QA_VERDICT:
        if p.get("verdict") not in {"approved", "needs_fixes", "blocked"}:
            raise DecisionPayloadInvalid("qa_verdict verdict must be approved, needs_fixes, or blocked")
        _list_of_strings(p, "findings")
        _list_of_strings(p, "proof_ids")
    elif decision.type == DecisionType.PROPOSE_STAGE_PLAN:
        stages = p.get("stages")
        if not isinstance(stages, list) or not stages:
            raise DecisionPayloadInvalid("stages must be a non-empty list")
        seen: set[str] = set()
        for idx, stage in enumerate(stages, start=1):
            if not isinstance(stage, dict):
                raise DecisionPayloadInvalid("each stage must be an object")
            sid = str(stage.get("id") or f"stage_{idx}")
            if sid in seen:
                raise DecisionPayloadInvalid(f"duplicate stage id: {sid}")
            seen.add(sid)
            for key in ("title", "objective"):
                if not str(stage.get(key, "")).strip():
                    raise DecisionPayloadInvalid(f"stage {sid} missing {key}")
            _list_of_strings(stage, "acceptance_criteria", required=True)
            _list_of_strings(stage, "affected_paths")
            _list_of_strings(stage, "test_plan")
    elif decision.type == DecisionType.CORRECT_STAGE:
        require_keys(p, "stage_id")
        if not str(p["stage_id"]).strip():
            raise DecisionPayloadInvalid("stage_id is required")
        if "target_stage_id" in p and not str(p.get("target_stage_id", "")).strip():
            raise DecisionPayloadInvalid("target_stage_id must be non-empty when supplied")
        if "set_current_stage_id" in p and not str(p.get("set_current_stage_id", "")).strip():
            raise DecisionPayloadInvalid("set_current_stage_id must be non-empty when supplied")
        _list_of_strings(p, "corrections")
        _list_of_strings(p, "audit_notes")
        _list_of_strings(p, "affected_paths")
        _list_of_strings(p, "test_plan")
    elif decision.type == DecisionType.REQUEST_TEST_RUN:
        require_keys(p, "stage_id")
        _list_of_strings(p, "commands", required=True)
    elif decision.type == DecisionType.REQUEST_SCREENSHOT:
        _validate_visual_proof_request(p, "request_screenshot")
    elif decision.type == DecisionType.REQUEST_VIDEO:
        _validate_visual_proof_request(p, "request_video")
        seconds = p.get("duration_seconds")
        if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1 or seconds > 120:
            raise DecisionPayloadInvalid("request_video duration_seconds must be an integer from 1 to 120")
        _list_of_strings(p, "interaction_script", required=True)
    elif decision.type == DecisionType.REQUEST_QA_REVIEW:
        require_keys(p, "stage_id", "proof_ids", "handoff")
        _list_of_strings(p, "proof_ids", required=True)
        handoff = p.get("handoff")
        if not isinstance(handoff, dict):
            raise DecisionPayloadInvalid("handoff must be an object")
        if str(handoff.get("to", "")).strip() != "qa":
            raise DecisionPayloadInvalid("request_qa_review handoff.to must be qa")
        if handoff.get("stage_complete") is not True:
            raise DecisionPayloadInvalid("request_qa_review handoff.stage_complete must be true")
    elif decision.type == DecisionType.REPORT_ISSUE_DISCOVERY:
        validate_discovery_payload(p)
    elif decision.type == DecisionType.TRIAGE_ISSUE_DISCOVERY:
        validate_triage_payload(p)
    elif decision.type == DecisionType.RESOLVE_INCIDENT:
        require_keys(p, "incident_id", "resolution")
        if not str(p.get("incident_id", "")).strip():
            raise DecisionPayloadInvalid("incident_id is required")
        if not str(p.get("resolution", "")).strip():
            raise DecisionPayloadInvalid("resolution is required")
        if "next_state" in p and not str(p.get("next_state", "")).strip():
            raise DecisionPayloadInvalid("next_state must be non-empty when supplied")
    elif decision.type in {DecisionType.APPROVE, DecisionType.REPORT_QA_VERDICT}:
        review_scope = p.get("review_scope", "plan")
        if review_scope == "implementation":
            proof_ids = _list_of_strings(p, "proof_ids", required=False)
            delivery_packets_reviewed = _list_of_strings(p, "delivery_packets_reviewed", required=False)
            qa_review = p.get("qa_review") if isinstance(p.get("qa_review"), dict) else {}
            qa_delivery_packets_reviewed = _list_of_strings(qa_review, "delivery_packets_reviewed", required=False) if qa_review else []
            if not proof_ids and not delivery_packets_reviewed and not qa_delivery_packets_reviewed:
                raise DecisionPayloadInvalid("implementation reviews require non-empty proof_ids or delivery_packets_reviewed")
            if p.get("verdict", "approved") not in {"approved", "needs_fixes", "blocked"}:
                raise DecisionPayloadInvalid("implementation verdict must be approved, needs_fixes, or blocked")
            return
        if review_scope != "plan":
            raise DecisionPayloadInvalid("review_scope must be plan or implementation")
        reviewed_stage_ids = _list_of_strings(p, "reviewed_stage_ids", required=True)
        if not reviewed_stage_ids:
            raise DecisionPayloadInvalid("reviewed_stage_ids must be non-empty")
        if p.get("verdict", "approved") == "approved":
            for key in ("proof_requirements_confirmed", "test_plan_confirmed"):
                if p.get(key) is not True:
                    raise DecisionPayloadInvalid(f"approved plan reviews require {key}=true")
    elif decision.type == DecisionType.BLOCK:
        if not str(p.get("reason", "")).strip():
            raise DecisionPayloadInvalid("block reason is required")
        _validate_block_log_ref(p.get("log_ref"))


def _validate_block_log_ref(value) -> None:
    if not isinstance(value, dict):
        raise DecisionPayloadInvalid("block log_ref is required")
    path = str(value.get("path", "")).strip()
    if not path:
        raise DecisionPayloadInvalid("block log_ref.path is required")
    if not _is_safe_log_ref_path(path):
        raise DecisionPayloadInvalid("block log_ref.path must be a redaction-safe relative log handle")
    line = value.get("line")
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        raise DecisionPayloadInvalid("block log_ref.line must be an integer >= 1")
    if not str(value.get("summary", "")).strip():
        raise DecisionPayloadInvalid("block log_ref.summary is required")


def _is_safe_log_ref_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        return False
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return False
    unsafe_parts = {".env", "env", "auth", "credentials", "credential", "secrets", "secret", "tokens", "token", "config", ".ssh"}
    return not any(part.lower() in unsafe_parts or part.startswith(".") for part in parts)


def _validate_visual_proof_request(payload: dict, label: str) -> None:
    require_keys(payload, "stage_id", "target", "proof_requirement", "mcp_server", "required_launch_pins")
    for key in ("stage_id", "target", "proof_requirement", "mcp_server"):
        if not str(payload.get(key, "")).strip():
            raise DecisionPayloadInvalid(f"{label} {key} is required")
    if str(payload.get("mcp_server")) != "launcher_qa":
        raise DecisionPayloadInvalid(f"{label} mcp_server must be launcher_qa")
    pins = payload.get("required_launch_pins")
    if not isinstance(pins, dict):
        raise DecisionPayloadInvalid(f"{label} required_launch_pins must be an object")
    if not str(pins.get("hermes_profile", "")).strip():
        raise DecisionPayloadInvalid(f"{label} required_launch_pins.hermes_profile is required")
    runtime_root_id = str(pins.get("runtime_root_id", "")).strip()
    if not runtime_root_id:
        raise DecisionPayloadInvalid(f"{label} required_launch_pins.runtime_root_id is required")
    if not _is_safe_runtime_root_id(runtime_root_id):
        raise DecisionPayloadInvalid(f"{label} required_launch_pins.runtime_root_id must be a redaction-safe token")
    if "harness_runtime_root" in pins or "hermes_home" in pins:
        raise DecisionPayloadInvalid(f"{label} launch pins must use runtime_root_id, not absolute paths")


def _is_safe_runtime_root_id(value: str) -> bool:
    if ":" in value or "\\" in value or "/" in value or value.startswith("~"):
        return False
    return 1 <= len(value) <= 128 and all(ch.isalnum() or ch in "_.-" for ch in value)
