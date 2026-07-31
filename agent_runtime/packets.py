from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from .events import EventLog
from .redaction import ENV_SECRET_ASSIGNMENT_RE
from .serde import to_jsonable

_SECRET_PATTERNS = (
    ENV_SECRET_ASSIGNMENT_RE,
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-+/=]{12,})"),
)
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:(?:[\\/]+[^\"'<>|\r\n]+)+"),
    re.compile(r"(?i)\b[A-Z]:(?:[\\/]+[^\\/\s\"'<>|:]+)+"),
    re.compile(r"(?<![\w.-])/(?:Users|home|mnt|opt|var|tmp|Volumes)/(?:[^\s\"'<>|:]+/?)+"),
)


def adapt_eternia_backend_manage_py_command(command: str) -> str:
    if ".EterniaBackendVirtualEnv/Scripts/python.exe" in command:
        return command
    stripped = re.sub(
        r"^\s*(?:source|\.)\s+(?:\.?/)?venv/Scripts/activate\s*(?:&&|;)\s*",
        "",
        command,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    match = re.match(
        r"^(?:python|python\.exe|python3|python3\.exe)\s+manage\.py\b(?P<rest>.*)$",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        return f".EterniaBackendVirtualEnv/Scripts/python.exe manage.py{match.group('rest')}"
    return command

PACKET_TYPES = frozenset({"handoff_packet", "delivery", "qa_review"})
_NORMALIZATION_KEY = "_normalization"

HANDOFF_PACKET_KEYS = frozenset(
    {
        "packet_version",
        "packet_kind",
        "mission_phase",
        "handoff_mode",
        "decision_status",
        "target_owner",
        "target_repo",
        "target_dev_persona",
        "next_owner",
        "next_repo",
        "final_owner",
        "final_repo",
        "proof_gate",
        "join_gate",
        "harness_rules",
        "self_heal",
        "joined_proof_ids",
        "joined_contract_packet_ids",
        "cited_evidence_ids",
        "assumptions_made",
        "alternatives_considered",
        "operator_note",
        "human_action_required",
        _NORMALIZATION_KEY,
    }
)
DELIVERY_KEYS = frozenset(
    {
        "delivery_version",
        "source_handoff_packet_id",
        "work_status",
        "self_test_evidence_ids",
        "consumed_contract_packet_ids",
        "consumed_proof_ids",
        "produced_contract_packet_id",
        "contract_packet",
        "changed_files",
        "changed_paths",
        "inspected_paths",
        "dirty_baseline",
        "coverage_claims",
        "known_non_coverage",
        "proof_reuse_basis",
        "failed_proof_classification",
        "handoff_repair",
        "summary",
        "findings",
        "recommendations",
        "model_options",
        "wd_tagger_assessment",
        "questions",
        "proof_ids",
        "cited_evidence_ids",
        "proof_summary",
        "command_summary",
        "known_gaps",
        "next_owner",
        "operator_note",
        "repo_bundle_id",
        "analysis_sections",
        "commit_refs",
        "deploy_verification",
        _NORMALIZATION_KEY,
    }
)
DEPLOY_VERIFICATION_KEYS = frozenset({"status", "method", "proof_id", "command_summary"})
QA_REVIEW_KEYS = frozenset(
    {
        "qa_review_version",
        "source_handoff_packet_id",
        "review_scope",
        "mission_phase",
        "proof_reviewed",
        "coverage",
        "contract_packets_reviewed",
        "delivery_packets_reviewed",
        "mcp_status",
        "decision_basis",
        "remaining_gaps",
        "next_owner",
        "operator_note",
        "repo_bundle_ids",
        "repo_bundle_id",
        "missing_proof",
        "accepted_risk",
        _NORMALIZATION_KEY,
    }
)

HANDOFF_MODES = frozenset(
    {
        "single_specialist",
        "backend_first_cross_stack",
        "sequential_specialists",
        "parallel_specialists",
        "split_child_missions",
    }
)
HANDOFF_OWNERS = frozenset({"backend_dev", "launcher_dev", "dev", "qa", "neko_supervisor", "human"})
HANDOFF_REPOS = frozenset({"EterniaBackend", "EterniaLauncher", "hermes-agent"})
HANDOFF_REPO_ALIASES = {
    "api": "EterniaBackend",
    "backend": "EterniaBackend",
    "backend_dev": "EterniaBackend",
    "backenddev": "EterniaBackend",
    "django": "EterniaBackend",
    "eternia_backend": "EterniaBackend",
    "eterniabackend": "EterniaBackend",
    "server": "EterniaBackend",
    "eternia_launcher": "EterniaLauncher",
    "eternialauncher": "EterniaLauncher",
    "front_end": "EterniaLauncher",
    "frontend": "EterniaLauncher",
    "launcher": "EterniaLauncher",
    "launcher_dev": "EterniaLauncher",
    "launcherdev": "EterniaLauncher",
    "ui": "EterniaLauncher",
    "agent_runtime": "hermes-agent",
    "agentruntime": "hermes-agent",
    "harness": "hermes-agent",
    "hermes": "hermes-agent",
    "hermes_agent": "hermes-agent",
    "hermesagent": "hermes-agent",
}
NO_REPO_ALIASES = frozenset({"n/a", "n_a", "na", "none", "no_repo", "no_product_edits", "not_applicable", "notapplicable", "not_required"})
PROOF_STATUSES = frozenset({"passed", "approved", "blocked", "failed", "missing"})
PROOF_STATUS_ALIASES = {
    "ready": "passed",
    "ready_for_qa": "passed",
    "dev_ready_for_qa": "passed",
    "qa_ready": "passed",
    "ok": "passed",
    "success": "passed",
    "succeeded": "passed",
    "blocked_by_environment": "blocked",
    "environment_blocked": "blocked",
    "not_available": "missing",
}
SELF_HEAL_CLASSES = frozenset({"none", "environment", "code", "proof_command", "context", "prompt_skill", "routing", "provider", "human_only"})
SELF_HEAL_ACTIONS = frozenset({"none", "preflight_retry", "bounded_command_retry", "prompt_patch", "skill_patch", "routing_patch", "block"})
QA_COVERAGE_KEYS = frozenset({"backend_contract", "launcher_integration", "visual_or_mcp", "cross_stack_join"})
QA_COVERAGE_VALUES = frozenset({"not_required", "missing", "reviewed", "blocked", "failed"})
QA_NEXT_OWNERS = frozenset({"harness", "neko_supervisor", "dev", "human"})

_DELIVERY_STATUS_FOR_DECISION = {
    "planned": DecisionType.PROPOSE_STAGE_PLAN,
    "patch_proposed": DecisionType.PROPOSE_PATCH,
    "proof_requested": DecisionType.REQUEST_TEST_RUN,
    "ready_for_qa": DecisionType.REQUEST_QA_REVIEW,
    "blocked": DecisionType.BLOCK,
    "issue_discovered": DecisionType.REPORT_ISSUE_DISCOVERY,
}
_DELIVERY_STATUS_BY_DECISION = {decision: status for status, decision in _DELIVERY_STATUS_FOR_DECISION.items()}
_SECRET_WORDS = re.compile(r"(?i)\b(secret|token|password|credential|api[_ -]?key|authorization|cookie|bearer|private[_ -]?key)\b")
_SECRET_VALUE_FRAGMENTS = re.compile(r"(?i)\bsk-[A-Za-z0-9][A-Za-z0-9_-]{7,}\b")
_SECRET_EXPOSURE_PHRASES = re.compile(
    r"(?i)\b(secret|token|password|credential|api[_ -]?key|authorization|cookie|bearer|private[_ -]?key)\b"
    r".{0,80}\b(printed|logged|leaked|exposed|dumped|shown|displayed|revealed|committed|persisted)\b"
    r"|"
    r"\b(printed|logged|leaked|exposed|dumped|shown|displayed|revealed|committed|persisted)\b"
    r".{0,80}\b(secret|token|password|credential|api[_ -]?key|authorization|cookie|bearer|private[_ -]?key)\b"
)
_MASKED_SECRET_TERM = "[redacted-term]"
_RAW_LOG_MARKERS = ("--- stdout ---", "--- stderr ---", "traceback (most recent call last)", "\nstdout:", "\nstderr:")
_AUTH_SHAPE_KEYS = frozenset(
    {
        "auth",
        "authentication",
        "authorization",
        "bearer",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "token",
        "tokens",
        "api_key",
        "apikey",
    }
)


def validate_decision_packets(decision: AgentDecision) -> None:
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    for packet_type, packet in iter_packet_payloads(payload):
        if packet_type == "handoff_packet":
            _validate_handoff_packet(packet)
        elif packet_type == "delivery":
            _validate_delivery(packet, decision_type=decision.type)
        elif packet_type == "qa_review":
            _validate_qa_review(packet, decision_type=decision.type)


def iter_packet_payloads(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    packets: list[tuple[str, dict[str, Any]]] = []
    for key in PACKET_TYPES:
        packet = payload.get(key)
        if packet is not None:
            packets.append((key, _require_object(packet, key)))
    for idx, stage in enumerate(payload.get("stages") or []):
        if isinstance(stage, dict) and stage.get("delivery") is not None:
            packets.append(("delivery", _require_object(stage.get("delivery"), f"stages[{idx}].delivery")))
    handoff = payload.get("handoff")
    if isinstance(handoff, dict) and handoff.get("delivery") is not None:
            packets.append(("delivery", _require_object(handoff.get("delivery"), "handoff.delivery")))
    return packets
def latest_packet(task_id: str, packet_type: str, *, event_log: EventLog | None = None, stage_id: str | None = None) -> dict[str, Any] | None:
    event_log = event_log or EventLog()
    for event in reversed(event_log.for_task(task_id, limit=0)):
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.type != "packet.recorded" or payload.get("packet_type") != packet_type:
            continue
        if stage_id is not None and payload.get("stage_id") not in {None, stage_id}:
            continue
        return payload
    return None


def latest_packets_for_task(task_id: str, *, event_log: EventLog | None = None, stage_id: str | None = None) -> dict[str, dict[str, Any]]:
    return {
        packet_type: packet
        for packet_type in sorted(PACKET_TYPES)
        if (packet := latest_packet(task_id, packet_type, event_log=event_log, stage_id=stage_id)) is not None
    }
def content_hash(body: dict[str, Any]) -> str:
    text = json.dumps(to_jsonable(body), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
def _validate_handoff_packet(packet: dict[str, Any]) -> None:
    _normalize_unknown_packet_metadata(packet, HANDOFF_PACKET_KEYS, "handoff_packet")
    _scan_packet_redaction(packet)
    for key in ("packet_kind", "mission_phase", "handoff_mode", "proof_gate"):
        _require_non_empty_string_or_object(packet, key)
    mode = str(packet.get("handoff_mode"))
    if mode not in HANDOFF_MODES:
        raise DecisionPayloadInvalid("handoff_packet.handoff_mode is not supported")
    for owner_key in ("target_owner", "next_owner"):
        if owner_key in packet and str(packet.get(owner_key)) not in HANDOFF_OWNERS:
            raise DecisionPayloadInvalid(f"handoff_packet.{owner_key} is invalid")
    if packet.get("final_owner") is not None and str(packet.get("final_owner")) not in HANDOFF_OWNERS | {"harness"}:
        raise DecisionPayloadInvalid("handoff_packet.final_owner is invalid")
    if not packet.get("target_owner") and not packet.get("next_owner") and not packet.get("human_action_required"):
        raise DecisionPayloadInvalid("handoff_packet requires target_owner, next_owner, or human_action_required")
    _normalize_handoff_repos(packet)
    proof_gate = _require_object(packet.get("proof_gate"), "handoff_packet.proof_gate")
    if str(packet.get("packet_kind") or "") == "qa_coordination_release":
        _default_qa_coordination_proof_gate(packet, proof_gate)
    if "minimum_status" not in proof_gate:
        proof_gate["minimum_status"] = "passed"
    # Derivable booleans get defaulted (with an operator note), not hard-failed:
    # a missing `required` on a gate that names proof types or a recipe means
    # "required" in the STRICTER reading, and a missing `visual_required` means
    # no visual lane was asked for. Hard-failing here burned a full lead turn
    # live (2026-07-03, task_1b102976: neko omitted `required`; retryable=false
    # killed the goal driver) — the same validated-form-rejects-real-work class
    # Round 3 closed for delivery packets. qa_coordination_release already gets
    # this defaulting; fresh_scope handoffs deserve the same.
    if "required" not in proof_gate and (proof_gate.get("required_proof_types") or proof_gate.get("proof_recipe_id") or proof_gate.get("recipe_id")):
        proof_gate["required"] = True
        _append_operator_note(packet, "proof_gate.required defaulted to true from declared proof expectations")
    if "visual_required" not in proof_gate:
        proof_gate["visual_required"] = False
        _append_operator_note(packet, "proof_gate.visual_required defaulted to false")
    for key in ("required", "required_proof_types", "minimum_status", "visual_required"):
        if key not in proof_gate:
            raise DecisionPayloadInvalid(f"handoff_packet.proof_gate missing {key}")
    if not isinstance(proof_gate.get("required"), bool) or not isinstance(proof_gate.get("visual_required"), bool):
        raise DecisionPayloadInvalid("handoff_packet proof_gate booleans are invalid")
    if not isinstance(proof_gate.get("required_proof_types"), list) or not proof_gate.get("required_proof_types"):
        raise DecisionPayloadInvalid("handoff_packet.proof_gate.required_proof_types must be non-empty")
    for key in ("commands", "forbidden_commands", "required_proof_ids"):
        if key in proof_gate:
            value = proof_gate.get(key)
            if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
                raise DecisionPayloadInvalid(f"handoff_packet.proof_gate.{key} must be a list of non-empty strings")
    proof_gate["minimum_status"] = _normalize_proof_status(proof_gate.get("minimum_status"))
    if proof_gate.get("required") is False and str(proof_gate.get("minimum_status")) not in PROOF_STATUSES:
        proof_gate["minimum_status"] = "passed"
    if str(proof_gate.get("minimum_status")) not in PROOF_STATUSES:
        raise DecisionPayloadInvalid("handoff_packet.proof_gate.minimum_status is invalid")
    _normalize_backend_self_test_command(packet, proof_gate)
    if mode in {"backend_first_cross_stack", "sequential_specialists"}:
        if str(packet.get("packet_kind") or "") == "qa_coordination_release":
            _default_qa_coordination_join_gate(packet, proof_gate=proof_gate)
        join_gate = _require_object(packet.get("join_gate"), "handoff_packet.join_gate")
        if not str(join_gate.get("release_condition", "")).strip():
            raise DecisionPayloadInvalid("handoff_packet.join_gate.release_condition is required")
    _optional_string_list(packet, "joined_proof_ids")
    _optional_string_list(packet, "joined_contract_packet_ids")
    _optional_string_list(packet, "cited_evidence_ids")
    if packet.get("harness_rules") is not None:
        _validate_harness_rules(packet.get("harness_rules"))
    if isinstance(packet.get("self_heal"), dict):
        self_heal = packet["self_heal"]
        if "classification" in self_heal and str(self_heal.get("classification")) not in SELF_HEAL_CLASSES:
            raise DecisionPayloadInvalid("handoff_packet.self_heal.classification is invalid")
        if "action" in self_heal and str(self_heal.get("action")) not in SELF_HEAL_ACTIONS:
            raise DecisionPayloadInvalid("handoff_packet.self_heal.action is invalid")


def _normalize_backend_self_test_command(packet: dict[str, Any], proof_gate: dict[str, Any]) -> None:
    """Adapt a backend-targeted ``self_test_command`` to the repo-venv interpreter.

    Isolated worktrees carry no Python environment on PATH; the harness proof
    runner already adapts its OWN commands, but the agent-facing
    ``self_test_command`` reached the dev un-adapted, so every backend goal
    burned a discovery turn (naked ``python manage.py check`` → no Django →
    ``block`` → a full lead recovery turn). Normalize once at packet acceptance
    so the dev is handed the interpreter that actually exists in the worktree.
    """
    if str(packet.get("target_repo") or "") != "EterniaBackend":
        return
    # Neko improvises the key name (live: `focused_self_test`); normalize every
    # known self-test command key so the dev never sees the naked interpreter.
    commands = proof_gate.get("commands")
    if isinstance(commands, list):
        adapted_commands = [adapt_eternia_backend_manage_py_command(str(command).strip()) for command in commands]
        if adapted_commands != commands:
            proof_gate["commands"] = adapted_commands
            _append_operator_note(packet, "proof_gate.commands adapted to backend venv interpreter")
    for key in ("self_test_command", "focused_self_test"):
        command = str(proof_gate.get(key) or "").strip()
        if not command:
            continue
        adapted = adapt_eternia_backend_manage_py_command(command)
        if adapted != command:
            proof_gate[key] = adapted
            _append_operator_note(packet, f"proof_gate.{key} adapted to backend venv interpreter")


def _normalize_handoff_repos(packet: dict[str, Any]) -> None:
    for repo_key, owner_key in (
        ("target_repo", "target_owner"),
        ("next_repo", "next_owner"),
        ("final_repo", "final_owner"),
    ):
        if repo_key not in packet or packet.get(repo_key) is None:
            derived = _repo_from_owner(packet.get(owner_key))
            if derived and repo_key in {"target_repo", "next_repo"}:
                packet[repo_key] = derived
                _append_operator_note(packet, f"{repo_key} derived from {owner_key}")
            continue
        normalized = _normalize_repo_label(packet.get(repo_key), owner=packet.get(owner_key))
        if normalized is None:
            packet.pop(repo_key, None)
            _append_operator_note(packet, f"{repo_key} ignored because no product repo applies")
            continue
        if normalized not in HANDOFF_REPOS:
            raise DecisionPayloadInvalid(f"handoff_packet.{repo_key} is invalid")
        if str(packet.get(repo_key)) != normalized:
            packet[repo_key] = normalized
            _append_operator_note(packet, f"{repo_key} normalized to {normalized}")


def _normalize_repo_label(value: Any, *, owner: Any = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return _repo_from_owner(owner)
    if text in HANDOFF_REPOS:
        return text
    token = _repo_token(text)
    if token in NO_REPO_ALIASES:
        return _repo_from_owner(owner)
    if token in HANDOFF_REPO_ALIASES:
        return HANDOFF_REPO_ALIASES[token]
    owner_repo = _repo_from_owner(owner)
    if owner_repo:
        owner_token = _repo_token(owner_repo)
        if owner_token in token or token in owner_token:
            return owner_repo
    matches = {canonical for alias, canonical in HANDOFF_REPO_ALIASES.items() if alias and alias in token}
    for canonical in HANDOFF_REPOS:
        if _repo_token(canonical) in token:
            matches.add(canonical)
    if owner_repo and len(matches) > 1 and owner_repo in matches:
        return owner_repo
    if len(matches) == 1:
        return next(iter(matches))
    return text


def _repo_from_owner(owner: Any) -> str | None:
    owner_text = str(owner or "").strip()
    if owner_text == "backend_dev":
        return "EterniaBackend"
    if owner_text in {"dev", "launcher_dev"}:
        return "EterniaLauncher"
    return None


def _repo_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _append_operator_note(packet: dict[str, Any], note: str) -> None:
    existing = str(packet.get("operator_note") or "").strip()
    if note in existing:
        return
    packet["operator_note"] = f"{existing[:180]}; {note}" if existing else note


def _derive_delivery_work_status(packet: dict[str, Any], *, decision_type: DecisionType) -> None:
    expected = _DELIVERY_STATUS_BY_DECISION.get(decision_type)
    if expected is None:
        return
    existing = str(packet.get("work_status") or "").strip()
    if existing == expected:
        return
    packet["work_status"] = expected
    reason = "derived missing delivery.work_status from decision_type"
    if existing:
        reason = f"normalized delivery.work_status from {existing!r} to {expected!r}"
    _append_operator_note(packet, reason)
    _merge_normalization(packet, renamed_fields=["work_status"])


def _validate_delivery(packet: dict[str, Any], *, decision_type: DecisionType) -> None:
    _normalize_unknown_packet_metadata(packet, DELIVERY_KEYS, "delivery")
    _normalize_contract_packet_auth_shape(packet)
    _scan_packet_redaction(packet)
    _derive_delivery_work_status(packet, decision_type=decision_type)
    _optional_string_list(packet, "consumed_contract_packet_ids")
    _optional_string_list(packet, "consumed_proof_ids")
    _optional_string_list(packet, "self_test_evidence_ids")
    _optional_string_list(packet, "changed_files")
    _optional_string_list(packet, "changed_paths")
    _optional_string_list(packet, "inspected_paths")
    _optional_string_list(packet, "coverage_claims")
    _optional_string_list(packet, "known_non_coverage")
    _optional_string_list(packet, "failed_proof_classification")
    if "summary" in packet and not isinstance(packet.get("summary"), str):
        raise DecisionPayloadInvalid("delivery.summary must be a string")
    if "dirty_baseline" in packet and not isinstance(packet.get("dirty_baseline"), (str, dict, bool)):
        raise DecisionPayloadInvalid("delivery.dirty_baseline must be a string, object, or boolean")
    if "proof_reuse_basis" in packet and not isinstance(packet.get("proof_reuse_basis"), (str, dict)):
        raise DecisionPayloadInvalid("delivery.proof_reuse_basis must be a string or object")
    if "handoff_repair" in packet and not isinstance(packet.get("handoff_repair"), (bool, dict)):
        raise DecisionPayloadInvalid("delivery.handoff_repair must be a boolean or object")
    _optional_string_list(packet, "findings")
    _optional_string_list(packet, "recommendations")
    _optional_string_list(packet, "model_options")
    if "wd_tagger_assessment" in packet and not isinstance(packet.get("wd_tagger_assessment"), str):
        raise DecisionPayloadInvalid("delivery.wd_tagger_assessment must be a string")
    _optional_string_list(packet, "questions")
    _optional_string_list(packet, "proof_ids")
    _optional_string_list(packet, "cited_evidence_ids")
    _optional_string_list(packet, "known_gaps")
    _optional_string_list(packet, "commit_refs")
    if "deploy_verification" in packet:
        deploy = packet.get("deploy_verification")
        if not isinstance(deploy, dict):
            raise DecisionPayloadInvalid("delivery.deploy_verification must be an object")
        extra = sorted(set(deploy.keys()) - DEPLOY_VERIFICATION_KEYS)
        if extra:
            for key in extra:
                deploy.pop(key, None)
            _append_operator_note(packet, f"deploy_verification ignored unsupported keys: {', '.join(extra[:6])}")
    _normalize_delivery_contract_packet(packet)
    if packet.get("next_owner") is not None and str(packet.get("next_owner")) not in HANDOFF_OWNERS:
        raise DecisionPayloadInvalid("delivery.next_owner is invalid")


def _default_qa_coordination_join_gate(packet: dict[str, Any], *, proof_gate: dict[str, Any]) -> None:
    join_gate = packet.get("join_gate")
    if isinstance(join_gate, dict) and str(join_gate.get("release_condition") or "").strip():
        return
    proof_ids = proof_gate.get("required_proof_ids")
    if not isinstance(proof_ids, list):
        proof_ids = proof_gate.get("proof_ids")
    proof_count = len(proof_ids) if isinstance(proof_ids, list) else 0
    packet["join_gate"] = {
        "release_condition": (
            f"QA coordination release is based on {proof_count} attached required proof ID(s); "
            "QA must verify status, scope, and cross-stack join before verdict."
        )
    }
    _append_operator_note(packet, "join_gate.release_condition defaulted for QA coordination release")


def _normalize_delivery_contract_packet(packet: dict[str, Any]) -> None:
    contract_packet = packet.get("contract_packet")
    if contract_packet is None:
        return
    if not isinstance(contract_packet, dict) or not contract_packet:
        raise DecisionPayloadInvalid("delivery.contract_packet must be a non-empty object")
    if not _contract_packet_has_shape(contract_packet):
        raise DecisionPayloadInvalid("delivery.contract_packet requires endpoint/request/response/error/example contract shape")
    contract_packet_id = str(contract_packet.get("contract_packet_id") or "").strip()
    if not contract_packet_id:
        contract_packet_id = _contract_packet_id(contract_packet)
        contract_packet["contract_packet_id"] = contract_packet_id
        _append_operator_note(packet, "contract_packet.contract_packet_id defaulted from contract content")
    produced_id = str(packet.get("produced_contract_packet_id") or "").strip()
    if not produced_id or produced_id.lower() in {"pending", "pending_harness_contract_packet_record", "tbd", "todo", "none", "n/a"}:
        packet["produced_contract_packet_id"] = contract_packet_id
        _append_operator_note(packet, "produced_contract_packet_id defaulted from contract_packet")


def _normalize_contract_packet_auth_shape(packet: dict[str, Any]) -> None:
    contract_packet = packet.get("contract_packet")
    if not isinstance(contract_packet, dict):
        return
    request_shape = contract_packet.get("request_shape")
    if not isinstance(request_shape, dict):
        return
    changed = _normalize_auth_shape_mapping(request_shape)
    if changed:
        _append_operator_note(packet, "contract_packet.request_shape auth-like fields normalized to shape-only text")


def _normalize_auth_shape_mapping(mapping: dict[str, Any]) -> bool:
    changed = False
    for key in list(mapping.keys()):
        value = mapping.get(key)
        normalized_key = _auth_shape_key(key)
        if isinstance(value, dict):
            changed = _normalize_auth_shape_mapping(value) or changed
        if normalized_key is None:
            continue
        _reject_strict_secret_auth_shape_value(value, path=f"delivery.contract_packet.request_shape.{key}")
        mapping.pop(key, None)
        target_key = normalized_key
        suffix = 2
        while target_key in mapping:
            target_key = f"{normalized_key}_{suffix}"
            suffix += 1
        mapping[target_key] = "required; runtime value omitted; shape only"
        changed = True
    return changed


def _auth_shape_key(key: Any) -> str | None:
    text = str(key or "").strip()
    normalized = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if normalized in _AUTH_SHAPE_KEYS:
        return "auth_shape"
    if normalized in {"authorization_header", "auth_header", "bearer_header"}:
        return "authorization_header_shape"
    if normalized in {"api_key_header", "token_header"}:
        return "api_key_header_shape"
    return None


def _reject_strict_secret_auth_shape_value(value: Any, *, path: str) -> None:
    text = json.dumps(to_jsonable(value), sort_keys=True, default=str)
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS) or _SECRET_VALUE_FRAGMENTS.search(text):
        raise DecisionPayloadInvalid(f"{path} contains secret-looking text")


def _contract_packet_has_shape(contract_packet: dict[str, Any]) -> bool:
    text = json.dumps(to_jsonable(contract_packet), sort_keys=True).lower()
    return any(marker in text for marker in ("endpoint", "surface", "request", "response", "error", "example", "schema", "contract"))


def _contract_packet_id(contract_packet: dict[str, Any]) -> str:
    seed = {key: value for key, value in contract_packet.items() if key != "contract_packet_id"}
    digest = content_hash(seed)
    return f"packet_contract_{digest[7:23]}"


def _default_qa_coordination_proof_gate(packet: dict[str, Any], proof_gate: dict[str, Any]) -> None:
    proof_ids = proof_gate.get("required_proof_ids")
    if not isinstance(proof_ids, list):
        proof_ids = proof_gate.get("proof_ids")
    has_proof_ids = isinstance(proof_ids, list) and bool(proof_ids)
    if "required" not in proof_gate:
        proof_gate["required"] = True
        _append_operator_note(packet, "proof_gate.required defaulted for QA coordination release")
    if "required_proof_types" not in proof_gate and has_proof_ids:
        proof_gate["required_proof_types"] = ["test_run"]
        _append_operator_note(packet, "proof_gate.required_proof_types defaulted for QA coordination release")
    if "visual_required" not in proof_gate:
        proof_gate["visual_required"] = False
        _append_operator_note(packet, "proof_gate.visual_required defaulted for QA coordination release")


def _validate_qa_review(packet: dict[str, Any], *, decision_type: DecisionType) -> None:
    _normalize_unknown_packet_metadata(packet, QA_REVIEW_KEYS, "qa_review")
    _scan_packet_redaction(packet)
    coverage = _require_object(packet.get("coverage"), "qa_review.coverage")
    missing = QA_COVERAGE_KEYS - set(coverage.keys())
    if missing:
        raise DecisionPayloadInvalid(f"qa_review.coverage missing {sorted(missing)}")
    for key, value in coverage.items():
        if key not in QA_COVERAGE_KEYS or str(value) not in QA_COVERAGE_VALUES:
            raise DecisionPayloadInvalid("qa_review.coverage contains invalid key or value")
    _optional_string_list(packet, "proof_reviewed")
    _optional_string_list(packet, "contract_packets_reviewed")
    _optional_string_list(packet, "delivery_packets_reviewed")
    _optional_string_list(packet, "remaining_gaps")
    next_owner = str(packet.get("next_owner") or "").strip()
    if not next_owner:
        raise DecisionPayloadInvalid("qa_review.next_owner is required")
    if next_owner not in QA_NEXT_OWNERS:
        raise DecisionPayloadInvalid("qa_review.next_owner is invalid")
    cross_stack_gap = any(str(coverage.get(key)) in {"missing", "failed"} for key in ("backend_contract", "launcher_integration", "cross_stack_join"))
    if cross_stack_gap and next_owner != "neko_supervisor":
        raise DecisionPayloadInvalid("qa_review cross-stack gaps must route to neko_supervisor")
    if str(packet.get("decision_basis", "")) in {"missing_proof", "proof_plus_targeted_file_check"} and cross_stack_gap and next_owner != "neko_supervisor":
        raise DecisionPayloadInvalid("qa_review missing-proof or contract gaps must route to neko_supervisor")
def _normalize_unknown_packet_metadata(packet: dict[str, Any], allowed: frozenset[str], label: str) -> None:
    extra = sorted(set(packet.keys()) - allowed)
    if not extra:
        return
    for key in extra:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", str(key)):
            raise DecisionPayloadInvalid(f"{label} contains unsafe metadata key")
        _scan_packet_redaction(str(key), path=f"{label}.{key}", allow_bare_secret_terms=False)
    note = f"ignored unsupported metadata keys: {', '.join(extra[:8])}"
    if len(extra) > 8:
        note += f", +{len(extra) - 8} more"
    existing = str(packet.get("operator_note") or "").strip()
    packet["operator_note"] = f"{existing[:180]}; {note}" if existing else note
    _merge_normalization(packet, dropped_fields=extra, raw_dropped_values={key: packet.get(key) for key in extra})
    for key in extra:
        packet.pop(key, None)


def _merge_normalization(
    packet: dict[str, Any],
    *,
    dropped_fields: list[str] | None = None,
    renamed_fields: list[str] | None = None,
    truncated_fields: list[str] | None = None,
    raw_dropped_values: dict[str, Any] | None = None,
) -> None:
    raw = packet.get(_NORMALIZATION_KEY)
    info = raw if isinstance(raw, dict) else {}
    prior_raw = info.get("raw_dropped_values") if isinstance(info.get("raw_dropped_values"), dict) else {}
    packet[_NORMALIZATION_KEY] = {
        "dropped_fields": _dedupe_strings([*(info.get("dropped_fields") or []), *(dropped_fields or [])]),
        "renamed_fields": _dedupe_strings([*(info.get("renamed_fields") or []), *(renamed_fields or [])]),
        "truncated_fields": _dedupe_strings([*(info.get("truncated_fields") or []), *(truncated_fields or [])]),
        "raw_dropped_values": {**prior_raw, **(raw_dropped_values or {})},
    }
def _dedupe_strings(items: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text[:120])
    return result


def _string_list(value: Any) -> list[str]:
    return _dedupe_strings(value) if isinstance(value, list) else []


def _scan_packet_redaction(value: Any, *, path: str = "packet", allow_bare_secret_terms: bool = True) -> Any:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "raw_dropped_values" and path.endswith(_NORMALIZATION_KEY):
                continue
            _scan_packet_redaction(str(key), path=f"{path}.{key}", allow_bare_secret_terms=False)
            value[key] = _scan_packet_redaction(item, path=f"{path}.{key}", allow_bare_secret_terms=allow_bare_secret_terms)
        return value
    if isinstance(value, list):
        for idx, item in enumerate(value):
            value[idx] = _scan_packet_redaction(item, path=f"{path}[{idx}]", allow_bare_secret_terms=allow_bare_secret_terms)
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS) or _SECRET_VALUE_FRAGMENTS.search(text):
        raise DecisionPayloadInvalid(f"{path} contains secret-looking text")
    if _SECRET_EXPOSURE_PHRASES.search(text):
        return _mask_bare_secret_terms(value)
    if any(pattern.search(text) for pattern in _ABSOLUTE_PATH_PATTERNS) or _looks_like_absolute_path(text):
        return _mask_path_segments(value)
    lowered = text.lower().replace("\\", "/")
    unsafe_parts = {".env", "env", "auth", "credentials", "credential", "secrets", "secret", "tokens", "token", "config", ".ssh"}
    if any(part in unsafe_parts for part in lowered.split("/")):
        return _mask_path_segments(value)
    if any(marker in lowered for marker in _RAW_LOG_MARKERS) or len(text) > 4000:
        return text[:4000] + "\n<truncated redaction-safe log excerpt>"
    if _SECRET_WORDS.search(text):
        return _mask_bare_secret_terms(value)
    return value


def _mask_bare_secret_terms(value: str) -> str:
    return _SECRET_WORDS.sub(_MASKED_SECRET_TERM, value)


def _mask_path_segments(value: str) -> str:
    unsafe_parts = {".env", "env", "auth", "credentials", "credential", "secrets", "secret", "tokens", "token", "config", ".ssh"}
    text = re.sub(r"(?i)[A-Z]:[\\/][^\s,;]+", "<absolute-path-redacted>", value)
    text = re.sub(r"(?i)/(?:[^/\s]+/){1,}[^/\s,;]+", "<absolute-path-redacted>", text)
    parts = re.split(r"([\\/])", text)
    masked = [
        _MASKED_SECRET_TERM if part.lower() in unsafe_parts else part
        for part in parts
    ]
    return "".join(masked)
def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DecisionPayloadInvalid(f"{label} must be an object")
    return value


def _require_non_empty_string_or_object(packet: dict[str, Any], key: str) -> None:
    value = packet.get(key)
    if isinstance(value, dict):
        if not value:
            raise DecisionPayloadInvalid(f"handoff_packet.{key} is required")
        return
    if not str(value or "").strip():
        raise DecisionPayloadInvalid(f"handoff_packet.{key} is required")


def _optional_string_list(packet: dict[str, Any], key: str) -> None:
    if key not in packet:
        return
    value = packet[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise DecisionPayloadInvalid(f"{key} must be a list of non-empty strings")


def _validate_harness_rules(value: Any) -> None:
    if isinstance(value, list):
        if not value or not all(isinstance(item, str) and item.strip() for item in value):
            raise DecisionPayloadInvalid("handoff_packet.harness_rules must be a non-empty string list or object")
        return
    if not isinstance(value, dict):
        raise DecisionPayloadInvalid("handoff_packet.harness_rules must be a non-empty string list or object")
    if not value:
        raise DecisionPayloadInvalid("handoff_packet.harness_rules must not be empty")
    allowed = {"skill_loading", "retry_policy", "wait_semantic", "edit_policy", "observability", "self_heal"}
    extra = sorted(set(value.keys()) - allowed)
    if extra:
        raise DecisionPayloadInvalid(f"handoff_packet.harness_rules has unsupported keys: {extra}")


def _normalize_proof_status(value: Any) -> str:
    status = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return PROOF_STATUS_ALIASES.get(status, status)


def _looks_like_absolute_path(text: str) -> bool:
    normalized = text.replace("\\", "/")
    if normalized.startswith("//"):
        return True
    if re.match(r"^[A-Za-z]:/", normalized):
        return True
    if normalized.startswith(("/Users/", "/home/", "/mnt/", "/opt/", "/var/", "/tmp/", "/Volumes/")):
        return True
    return False


