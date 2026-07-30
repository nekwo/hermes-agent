from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .decision_schema import DecisionPayloadInvalid, DecisionType
from .personas import AgentRole


CONTRACT_SCHEMA_VERSION = 1
_CHECKLIST_PAYLOAD_KEYS = ("active_checklist_item_id", "checklist_updates", "self_approval_status")


@dataclass(frozen=True, slots=True)
class FieldContract:
    name: str
    field_type: str = "any"
    required: bool = False
    enum: tuple[str, ...] = ()
    item_type: str | None = None
    min_items: int | None = None
    object_contract: str | None = None
    redaction: str = "safe"
    normalization: str = "reject_unknown"
    description: str = ""

    def manifest(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "name": self.name,
                "type": self.field_type,
                "required": self.required,
                "enum": list(self.enum),
                "item_type": self.item_type,
                "min_items": self.min_items,
                "object_contract": self.object_contract,
                "redaction": self.redaction,
                "normalization": self.normalization,
                "description": self.description,
            }
        )


@dataclass(frozen=True, slots=True)
class ObjectContract:
    id: str
    required_keys: tuple[str, ...] = ()
    optional_keys: tuple[str, ...] = ()
    enum_choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    normalization_policy: str = "reject_unknown"
    description: str = ""

    @property
    def allowed_keys(self) -> tuple[str, ...]:
        return (*self.required_keys, *tuple(key for key in self.optional_keys if key not in self.required_keys))

    def manifest(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "id": self.id,
                "required_keys": list(self.required_keys),
                "optional_keys": list(self.optional_keys),
                "allowed_keys": list(self.allowed_keys),
                "enum_choices": {key: list(value) for key, value in self.enum_choices.items()},
                "normalization_policy": self.normalization_policy,
                "description": self.description,
            }
        )


@dataclass(frozen=True, slots=True)
class HudShape:
    shape_id: str
    decision_type: DecisionType
    label: str
    roles: tuple[AgentRole, ...]
    when: str
    payload_template: dict[str, Any] = field(default_factory=dict)
    nested_required: dict[str, tuple[str, ...]] = field(default_factory=dict)
    enum_choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def manifest(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "shape_id": self.shape_id,
                "decision_type": self.decision_type.value,
                "label": self.label,
                "roles": [role.value for role in self.roles],
                "when": self.when,
                "payload_template": copy.deepcopy(self.payload_template),
                "nested_required": {key: list(value) for key, value in self.nested_required.items()},
                "enum_choices": {key: list(value) for key, value in self.enum_choices.items()},
                **copy.deepcopy(self.extras),
            }
        )


@dataclass(frozen=True, slots=True)
class DecisionContract:
    decision_type: DecisionType
    required_payload_keys: tuple[str, ...] = ()
    optional_payload_keys: tuple[str, ...] = ()
    shape_hint: str = ""
    repair_hint: str = ""
    stage_allowed_keys: tuple[str, ...] = ()
    nested_contracts: tuple[str, ...] = ()
    enum_choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    redaction_policy: str = "strict"
    normalization_policy: str = "reject_unknown"
    prompt_example: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed_payload_keys(self) -> tuple[str, ...]:
        base = (
            *self.required_payload_keys,
            *tuple(key for key in self.optional_payload_keys if key not in self.required_payload_keys),
        )
        return (*base, *tuple(key for key in _CHECKLIST_PAYLOAD_KEYS if key not in base))

    def payload_contract(self) -> dict[str, Any]:
        result = {
            "decision_type": self.decision_type.value,
            "required_payload_keys": list(self.required_payload_keys),
            "optional_payload_keys": list(self.optional_payload_keys),
            "allowed_payload_keys": list(self.allowed_payload_keys),
            "forbid_unknown_payload_keys": True,
            "shape_hint": self.shape_hint,
            "repair_hint": self.repair_hint or self.shape_hint,
            "nested_contracts": list(self.nested_contracts),
            "enum_choices": {key: list(value) for key, value in self.enum_choices.items()},
            "redaction_policy": self.redaction_policy,
            "normalization_policy": self.normalization_policy,
        }
        if self.stage_allowed_keys:
            result["stage_allowed_keys"] = list(self.stage_allowed_keys)
        if self.prompt_example:
            result["prompt_example"] = copy.deepcopy(self.prompt_example)
        return _drop_empty(result)

    def manifest(self) -> dict[str, Any]:
        # Identical to ``payload_contract`` — kept so every contract dataclass in
        # this module answers the same ``manifest()`` protocol. S16 removed the
        # ``allowed_roles`` tail that used to make the two differ.
        return self.payload_contract()


@dataclass(frozen=True, slots=True)
class EventContract:
    event_type: str
    display_label: str
    summary_fields: tuple[str, ...] = ()
    detail_fields: tuple[str, ...] = ()
    redacted_fields: tuple[str, ...] = ()

    def manifest(self) -> dict[str, Any]:
        return _drop_empty(
            {
                "event_type": self.event_type,
                "display_label": self.display_label,
                "summary_fields": list(self.summary_fields),
                "detail_fields": list(self.detail_fields),
                "redacted_fields": list(self.redacted_fields),
            }
        )


def agent_decision_json_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AgentDecision",
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "summary", "rationale", "payload"],
        "properties": {
            "type": {"type": "string", "enum": [item.value for item in DecisionType]},
            "summary": {"type": "string", "minLength": 1, "maxLength": 280},
            "rationale": {"type": "string", "minLength": 1, "maxLength": 4096},
            "payload": {"type": "object"},
            "requires_approval": {"type": "boolean", "default": False},
            "schema_version": {"type": "integer", "const": 1, "default": 1},
        },
    }


def all_decision_contracts() -> dict[DecisionType, DecisionContract]:
    return dict(_DECISION_CONTRACTS)


def decision_contract(decision_type: DecisionType | str) -> DecisionContract:
    resolved = decision_type if isinstance(decision_type, DecisionType) else DecisionType(str(decision_type))
    return _DECISION_CONTRACTS[resolved]


def payload_contract(decision_type: DecisionType | str) -> dict[str, Any]:
    return decision_contract(decision_type).payload_contract()


def canonical_role_value(role: AgentRole | str) -> str:
    return role.value if isinstance(role, AgentRole) else str(role)


def validate_payload_keys(decision) -> None:
    from .role_checklists import validate_checklist_payload_structure

    contract = decision_contract(decision.type)
    payload = decision.payload if isinstance(decision.payload, dict) else {}
    validate_checklist_payload_structure(payload)
    allowed = set(contract.allowed_payload_keys)
    extra = sorted(set(payload.keys()) - allowed)
    if extra:
        for key in extra:
            payload.pop(key, None)
        if "operator_note" in allowed:
            existing = str(payload.get("operator_note") or "").strip()
            note = f"ignored unsupported payload keys: {', '.join(extra[:8])}"
            payload["operator_note"] = f"{existing[:180]}; {note}" if existing else note
    missing = [key for key in contract.required_payload_keys if key not in payload]
    if missing:
        if contract.decision_type == DecisionType.BLOCK:
            if "reason" in missing:
                raise DecisionPayloadInvalid("block reason is required")
            if "log_ref" in missing:
                raise DecisionPayloadInvalid("block log_ref is required")
        raise DecisionPayloadInvalid(
            f"{decision.type.value} payload is missing required keys: {missing}; "
            f"allowed keys are {list(contract.allowed_payload_keys)}"
        )
    if contract.decision_type == DecisionType.PROPOSE_STAGE_PLAN:
        _validate_stage_payload_keys(payload, contract=contract)


def hud_shape(shape_id: str) -> dict[str, Any]:
    shape = _HUD_SHAPES[shape_id]
    result = _shape_from_contract(shape)
    result.update(shape.manifest())
    return result


def hud_shape_index_for_stage(owner: str | AgentRole) -> dict[str, dict[str, Any]]:
    del owner
    return {shape_id: hud_shape(shape_id) for shape_id in _HUD_SHAPES}


def role_shape_ids(role: str | AgentRole) -> list[str]:
    del role
    return list(_HUD_SHAPES)


def context_expansion_shape_ids(role: str | AgentRole) -> list[str]:
    del role
    return ["common.request_file_reads", "common.needs_context"]


def object_contract(contract_id: str) -> dict[str, Any]:
    return _OBJECT_CONTRACTS[contract_id].manifest()


def validate_object_payload(contract_id: str, payload: Any) -> None:
    contract = _OBJECT_CONTRACTS[contract_id]
    if not isinstance(payload, dict):
        raise DecisionPayloadInvalid(f"{contract_id} must be an object")
    missing = [key for key in contract.required_keys if key not in payload]
    if missing:
        raise DecisionPayloadInvalid(f"{contract_id} is missing required keys: {missing}")
    if contract.normalization_policy == "reject_unknown":
        extra = sorted(set(payload) - set(contract.allowed_keys))
        if extra:
            raise DecisionPayloadInvalid(f"{contract_id} has unsupported keys: {extra}")
    else:
        for key in sorted(set(payload) - set(contract.allowed_keys)):
            payload.pop(key, None)
    for key, choices in contract.enum_choices.items():
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, str) and "|" in value:
            parts = [part.strip() for part in value.split("|") if part.strip()]
            if parts and all(part in choices for part in parts):
                continue
        if isinstance(value, str) and value in choices:
            continue
        raise DecisionPayloadInvalid(f"{contract_id}.{key} must be one of {list(choices)}")
    for key, value in payload.items():
        child_id = f"{contract_id}.{key}"
        if child_id in _OBJECT_CONTRACTS and isinstance(value, dict):
            validate_object_payload(child_id, value)


def event_catalog() -> dict[str, dict[str, Any]]:
    return {event_type: contract.manifest() for event_type, contract in _EVENT_CONTRACTS.items()}


def allowed_event_types() -> frozenset[str]:
    return frozenset(_EVENT_CONTRACTS)


def validate_event_payload(event_type: str, payload: object) -> tuple[str, ...]:
    """Missing contract summary fields for a registered event type (Stage 12 D).

    ``summary_fields`` are the fields the contract declares every emission
    carries; a payload without them renders as a blank row on every consumer.
    Extra keys are always allowed — the contract is additive. Unregistered
    types return () here; ``EventLog.append`` already rejects those outright.
    """

    contract = _EVENT_CONTRACTS.get(str(event_type))
    if contract is None:
        return ()
    body = payload if isinstance(payload, dict) else {}
    return tuple(field for field in contract.summary_fields if field not in body)


def contract_manifest() -> dict[str, Any]:
    return {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "agent_decision_schema": agent_decision_json_schema(),
        "decisions": {key.value: value.manifest() for key, value in _DECISION_CONTRACTS.items()},
        "decisions_available": [item.value for item in DecisionType],
        "objects": {key: value.manifest() for key, value in _OBJECT_CONTRACTS.items()},
        "hud_shapes": {key: value.manifest() for key, value in _HUD_SHAPES.items()},
        "shape_ids": list(_HUD_SHAPES),
        "context_expansion_shape_ids": context_expansion_shape_ids("*"),
        "events": event_catalog(),
        "contract_hash": contract_hash(),
    }


def contract_hash() -> str:
    payload = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "decisions": {key.value: value.manifest() for key, value in _DECISION_CONTRACTS.items()},
        "objects": {key: value.manifest() for key, value in _OBJECT_CONTRACTS.items()},
        "hud_shapes": {key: value.manifest() for key, value in _HUD_SHAPES.items()},
        "events": {key: value.manifest() for key, value in _EVENT_CONTRACTS.items()},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def verify_registry() -> dict[str, Any]:
    missing = [item.value for item in DecisionType if item not in _DECISION_CONTRACTS]
    missing_object_contracts = sorted(
        contract_id
        for contract in _DECISION_CONTRACTS.values()
        for contract_id in contract.nested_contracts
        if contract_id not in _OBJECT_CONTRACTS
    )
    hud_template_errors = _validate_hud_templates()
    return {
        "ok": not missing and not missing_object_contracts and not hud_template_errors,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "contract_hash": contract_hash(),
        "missing_decision_types": missing,
        "missing_object_contracts": missing_object_contracts,
        "hud_template_errors": hud_template_errors,
        "decision_count": len(_DECISION_CONTRACTS),
        "hud_shape_count": len(_HUD_SHAPES),
        "event_count": len(_EVENT_CONTRACTS),
    }


def _shape_from_contract(shape: HudShape) -> dict[str, Any]:
    contract = decision_contract(shape.decision_type)
    result = contract.payload_contract()
    result.update(
        {
            "shape_id": shape.shape_id,
            "label": shape.label,
            "decision_type": shape.decision_type.value,
            "when": shape.when,
            "payload_template": copy.deepcopy(shape.payload_template),
            "nested_required": {key: list(value) for key, value in shape.nested_required.items()},
            "enum_choices": {key: list(value) for key, value in shape.enum_choices.items()},
        }
    )
    return _drop_empty(result)


def _validate_hud_templates() -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for shape_id, shape in _HUD_SHAPES.items():
        template = shape.payload_template
        contract = decision_contract(shape.decision_type)
        for key in contract.required_payload_keys:
            if key not in template:
                errors.append({"shape_id": shape_id, "object": "payload", "error": f"missing_required:{key}"})
        for object_id in contract.nested_contracts:
            for value in _template_object_values(template, object_id):
                try:
                    validate_object_payload(object_id, value)
                except DecisionPayloadInvalid as exc:
                    errors.append({"shape_id": shape_id, "object": object_id, "error": str(exc)})
    return errors


def _template_object_values(template: dict[str, Any], object_id: str) -> list[Any]:
    if object_id == "propose_stage_plan.stages[]":
        stages = template.get("stages")
        return [stages[0]] if isinstance(stages, list) and stages else []
    if object_id == "handoff_packet":
        return [template["handoff_packet"]] if isinstance(template.get("handoff_packet"), dict) else []
    if object_id.startswith("handoff_packet."):
        handoff = template.get("handoff_packet")
        key = object_id.split(".", 1)[1]
        return [handoff[key]] if isinstance(handoff, dict) and isinstance(handoff.get(key), dict) else []
    if object_id == "delivery":
        values: list[Any] = []
        if isinstance(template.get("delivery"), dict):
            values.append(template["delivery"])
        stages = template.get("stages")
        if isinstance(stages, list):
            values.extend(stage["delivery"] for stage in stages if isinstance(stage, dict) and isinstance(stage.get("delivery"), dict))
        return values
    if object_id == "delivery.contract_packet":
        delivery_values = _template_object_values(template, "delivery")
        return [delivery["contract_packet"] for delivery in delivery_values if isinstance(delivery, dict) and isinstance(delivery.get("contract_packet"), dict)]
    if object_id == "request_qa_review.handoff":
        return [template["handoff"]] if isinstance(template.get("handoff"), dict) else []
    if object_id == "qa_review":
        return [template["qa_review"]] if isinstance(template.get("qa_review"), dict) else []
    if object_id == "qa_review.coverage":
        qa_review = template.get("qa_review")
        return [qa_review["coverage"]] if isinstance(qa_review, dict) and isinstance(qa_review.get("coverage"), dict) else []
    if object_id == "block.log_ref":
        return [template["log_ref"]] if isinstance(template.get("log_ref"), dict) else []
    if object_id == "visual.required_launch_pins":
        return [template["required_launch_pins"]] if isinstance(template.get("required_launch_pins"), dict) else []
    return []


def _validate_stage_payload_keys(payload: dict[str, Any], *, contract: DecisionContract) -> None:
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return
    allowed = set(contract.stage_allowed_keys)
    for idx, stage in enumerate(stages, start=1):
        if not isinstance(stage, dict):
            continue
        extra = sorted(set(stage.keys()) - allowed)
        if extra:
            raise DecisionPayloadInvalid(
                f"propose_stage_plan stages[{idx}] has unsupported keys: {extra}; "
                f"allowed keys are {list(contract.stage_allowed_keys)}"
            )


def _drop_empty(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value not in (None, "", [], {}, ())}


_OBJECT_CONTRACTS: dict[str, ObjectContract] = {
    "propose_stage_plan.stages[]": ObjectContract(
        id="propose_stage_plan.stages[]",
        required_keys=("title", "objective", "acceptance_criteria"),
        optional_keys=("id", "affected_paths", "test_plan", "requires_visual_proof", "delivery"),
        description="One executable stage definition.",
    ),
    "handoff_packet": ObjectContract(
        id="handoff_packet",
        required_keys=("packet_kind", "mission_phase", "handoff_mode", "proof_gate"),
        optional_keys=(
            "packet_version",
            "decision_status",
            "target_owner",
            "target_repo",
            "target_dev_persona",
            "next_owner",
            "next_repo",
            "final_owner",
            "final_repo",
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
        ),
        enum_choices={
            "handoff_mode": ("single_specialist", "sequential_specialists", "backend_first_cross_stack", "parallel_specialists", "split_child_missions"),
            "target_owner": ("backend_dev", "launcher_dev", "dev", "qa", "neko_supervisor", "human"),
            "next_owner": ("backend_dev", "launcher_dev", "dev", "qa", "neko_supervisor", "human"),
            "final_owner": ("backend_dev", "launcher_dev", "dev", "qa", "neko_supervisor", "human", "harness"),
            "target_repo": ("EterniaLauncher", "EterniaBackend", "hermes-agent"),
        },
        normalization_policy="drop_unknown_with_operator_note",
        description="Neko specialist routing packet.",
    ),
    "handoff_packet.proof_gate": ObjectContract(
        id="handoff_packet.proof_gate",
        required_keys=("required", "minimum_status"),
        optional_keys=(
            "required_proof_types",
            "required_proof_ids",
            "visual_required",
            "proof_recipe_id",
            "recipe_id",
            "commands",
            "forbidden_commands",
            "self_test_command",
            "focused_self_test",
        ),
        enum_choices={"minimum_status": ("passed", "approved", "blocked")},
        normalization_policy="default_missing_minimum_status",
    ),
    "handoff_packet.join_gate": ObjectContract(
        id="handoff_packet.join_gate",
        required_keys=("release_condition",),
        optional_keys=("required_owner", "required_repo", "next_owner"),
    ),
    "handoff_packet.self_heal": ObjectContract(
        id="handoff_packet.self_heal",
        optional_keys=("classification", "action"),
        enum_choices={
            "classification": ("none", "environment", "code", "proof_command", "context", "prompt_skill", "routing", "provider", "human_only"),
            "action": ("none", "preflight_retry", "bounded_command_retry", "prompt_patch", "skill_patch", "routing_patch", "block"),
        },
        description="Neko self-heal classification and next bounded action.",
    ),
    "request_qa_review.handoff": ObjectContract(
        id="request_qa_review.handoff",
        required_keys=("to", "stage_complete"),
        optional_keys=("known_gaps", "next_owner"),
        enum_choices={"to": ("qa",)},
    ),
    "needs_context.handoff_request": ObjectContract(
        id="needs_context.handoff_request",
        required_keys=("target_repo", "target_stage"),
        optional_keys=("target_owner", "reason"),
        enum_choices={
            "target_owner": ("dev", "launcher_dev", "backend_dev", "qa", "neko_supervisor"),
            "target_repo": ("EterniaLauncher", "EterniaBackend", "hermes-agent"),
        },
        normalization_policy="drop_unknown_with_operator_note",
    ),
    "qa_review": ObjectContract(
        id="qa_review",
        required_keys=("coverage", "decision_basis", "remaining_gaps", "next_owner"),
        optional_keys=(
            "qa_review_version",
            "source_handoff_packet_id",
            "review_scope",
            "mission_phase",
            "proof_reviewed",
            "repo_bundle_ids",
            "contract_packets_reviewed",
            "delivery_packets_reviewed",
            "mcp_status",
            "operator_note",
        ),
        enum_choices={
            "decision_basis": ("proof_packet", "artifact_review", "runtime_state", "blocked_by_missing_proof"),
            "next_owner": ("harness", "neko_supervisor", "dev", "human"),
        },
        normalization_policy="drop_unknown_with_operator_note",
    ),
    "qa_review.coverage": ObjectContract(
        id="qa_review.coverage",
        required_keys=("backend_contract", "launcher_integration", "visual_or_mcp", "cross_stack_join"),
        enum_choices={
            "backend_contract": ("not_required", "missing", "reviewed", "blocked", "failed"),
            "launcher_integration": ("not_required", "missing", "reviewed", "blocked", "failed"),
            "visual_or_mcp": ("not_required", "missing", "reviewed", "blocked", "failed"),
            "cross_stack_join": ("not_required", "missing", "reviewed", "blocked", "failed"),
        },
    ),
    "delivery": ObjectContract(
        id="delivery",
        required_keys=(),
        optional_keys=(
            "delivery_version",
            "source_handoff_packet_id",
            "work_status",
            "self_test_evidence_ids",
            "repo_bundle_id",
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
            "consumed_contract_packet_ids",
            "consumed_proof_ids",
            "produced_contract_packet_id",
            "contract_packet",
            "next_owner",
            "operator_note",
        ),
        normalization_policy="drop_unknown_with_operator_note",
    ),
    "delivery.contract_packet": ObjectContract(
        id="delivery.contract_packet",
        required_keys=("endpoint", "request_shape", "response_shape", "error_shape", "example_response"),
        optional_keys=("contract_packet_id", "surface", "auth_shape", "operator_note"),
        normalization_policy="normalize_auth_shape",
    ),
    "block.log_ref": ObjectContract(
        id="block.log_ref",
        required_keys=("path", "line", "summary"),
        description="Redaction-safe relative log handle.",
    ),
    "visual.required_launch_pins": ObjectContract(
        id="visual.required_launch_pins",
        required_keys=("hermes_profile", "runtime_root_id"),
        optional_keys=("expected_instance",),
        description="Redaction-safe launch identity pins for visual proof.",
    ),
}


_ALL_ROLES = tuple(AgentRole)
_DEV_ONLY = (AgentRole.DEV,)
_QA_ONLY = (AgentRole.QA,)
_NEKO_ONLY = (AgentRole.ALICE_SUPERVISOR,)
_PM_NEKO = (AgentRole.PM, AgentRole.ALICE_SUPERVISOR)


_DECISION_CONTRACTS: dict[DecisionType, DecisionContract] = {
    DecisionType.NEEDS_CONTEXT: DecisionContract(
        DecisionType.NEEDS_CONTEXT,
        optional_payload_keys=("reason", "requested_context", "questions", "missing", "handoff_request", "operator_note"),
        nested_contracts=("needs_context.handoff_request",),
        shape_hint="Use only when a safe bounded answer is missing and request_file_reads is not the right tool.",
        prompt_example={"reason": "<specific missing context>", "requested_context": "<bounded context handle>", "handoff_request": {"target_repo": "EterniaLauncher", "target_stage": "<stage_id>"}},
    ),
    DecisionType.REQUEST_HUMAN: DecisionContract(
        DecisionType.REQUEST_HUMAN,
        required_payload_keys=("reason",),
        optional_payload_keys=("requested_action", "log_ref"),
        shape_hint="Escalate only external decisions or credentials/human approvals.",
    ),
    DecisionType.PERSONA_MESSAGE_REPLY: DecisionContract(
        DecisionType.PERSONA_MESSAGE_REPLY,
        required_payload_keys=("reply",),
        optional_payload_keys=("persona_instance_id", "session_id", "message_id", "turn_id"),
        shape_hint="Reply conversationally to a Mission Control operator persona-chat message. Keep reply redaction-safe and do not scope a task unless the operator asked for task work.",
        prompt_example={"reply": "<redaction-safe conversational answer>"},
    ),
    DecisionType.PROPOSE_ACCEPTANCE: DecisionContract(
        DecisionType.PROPOSE_ACCEPTANCE,
        required_payload_keys=("objective", "acceptance_criteria"),
        optional_payload_keys=("non_goals", "affected_repos", "suggested_roles", "requires_visual_proof", "risk_flags", "handoff_packet", "scope_override_reason"),
        nested_contracts=("handoff_packet", "handoff_packet.proof_gate", "handoff_packet.join_gate", "handoff_packet.self_heal"),
        shape_hint="Neko/PM scope or route one bounded owner; attach handoff_packet for specialist routing.",
    ),
    DecisionType.PROPOSE_STAGE_PLAN: DecisionContract(
        DecisionType.PROPOSE_STAGE_PLAN,
        required_payload_keys=("stages",),
        optional_payload_keys=("delivery",),
        stage_allowed_keys=("id", "title", "objective", "acceptance_criteria", "affected_paths", "test_plan", "requires_visual_proof", "delivery"),
        nested_contracts=("propose_stage_plan.stages[]", "delivery"),
        shape_hint="Define bounded executable stages only; avoid Neko/QA orchestration stages from Dev.",
    ),
    DecisionType.CORRECT_STAGE: DecisionContract(
        DecisionType.CORRECT_STAGE,
        required_payload_keys=("stage_id",),
        optional_payload_keys=("corrections", "audit_notes", "affected_paths", "test_plan", "target_stage_id", "set_current_stage_id"),
        shape_hint="Repair current-stage instructions/proof gates, or reroute to a known target_stage_id. Do not attach delivery here.",
    ),
    DecisionType.REQUEST_FILE_READS: DecisionContract(
        DecisionType.REQUEST_FILE_READS,
        required_payload_keys=("paths", "reason"),
        shape_hint="Ask the Harness for a bounded file bundle instead of dumping context.",
    ),
    DecisionType.HAND_OFF: DecisionContract(
        DecisionType.HAND_OFF,
        optional_payload_keys=("stage_id", "summary", "known_gaps", "log_ref"),
        shape_hint="Collapsed Dev signal: work is ready for Harness-observed diff capture and authoritative gate rerun. Do not declare changed files, proof IDs, delivery, or work_status.",
    ),
    DecisionType.ESCALATE: DecisionContract(
        DecisionType.ESCALATE,
        required_payload_keys=("title", "summary"),
        optional_payload_keys=("severity", "evidence", "log_ref"),
        enum_choices={"severity": ("low", "medium", "high", "critical")},
        shape_hint="Collapsed issue signal: use only when the issue is too large to fix inline.",
    ),
    DecisionType.SCOPE_ROUTE: DecisionContract(
        DecisionType.SCOPE_ROUTE,
        required_payload_keys=("objective", "acceptance_criteria", "target_owner", "target_repo", "proof_gate"),
        optional_payload_keys=("non_goals", "suggested_roles", "requires_visual_proof", "risk_flags", "release_stage_id", "scope_override_reason"),
        enum_choices={"target_owner": ("dev", "backend_dev", "qa", "neko_supervisor", "human"), "target_repo": ("EterniaLauncher", "EterniaBackend", "hermes-agent", "none")},
        shape_hint="Collapsed Neko signal: scope and route one bounded owner/repo/proof gate. Harness derives the handoff packet internally. target_repo must match a repo the goal title/description literally names (e.g. 'hermes-agent') unless scope_override_reason records why the goal text is wrong.",
    ),
    DecisionType.QA_VERDICT: DecisionContract(
        DecisionType.QA_VERDICT,
        required_payload_keys=("verdict",),
        optional_payload_keys=("coverage", "findings", "proof_ids"),
        enum_choices={"verdict": ("approved", "needs_fixes", "blocked")},
        shape_hint="Collapsed QA signal: verdict over Harness-verified proof. Do not declare delivery status.",
    ),
    DecisionType.PROPOSE_PATCH: DecisionContract(
        DecisionType.PROPOSE_PATCH,
        optional_payload_keys=("stage_id", "patch", "summary", "changed_files", "tests", "delivery", "proof_ids", "known_gaps"),
        nested_contracts=("delivery",),
        shape_hint="Use after actual code edits or a concrete patch plan; Harness derives any compatibility delivery status.",
    ),
    DecisionType.REQUEST_TEST_RUN: DecisionContract(
        DecisionType.REQUEST_TEST_RUN,
        required_payload_keys=("stage_id",),
        optional_payload_keys=("commands", "recipe_id", "proof_intent", "intent", "repo_scope", "delivery", "failed_proof_ids", "failed_proof_auto_attached"),
        nested_contracts=("delivery",),
        shape_hint="Use for deterministic command/test proof. Use either commands or recipe_id. Do not invent recipe IDs or command metadata keys.",
    ),
    DecisionType.REQUEST_SCREENSHOT: DecisionContract(
        DecisionType.REQUEST_SCREENSHOT,
        required_payload_keys=("stage_id", "target", "proof_requirement", "mcp_server", "required_launch_pins"),
        optional_payload_keys=("qa_review",),
        nested_contracts=("visual.required_launch_pins", "qa_review"),
        enum_choices={"mcp_server": ("launcher_qa",)},
        shape_hint="Request one visual proof with launcher_qa and redaction-safe launch pins.",
    ),
    DecisionType.REQUEST_VIDEO: DecisionContract(
        DecisionType.REQUEST_VIDEO,
        required_payload_keys=("stage_id", "target", "proof_requirement", "mcp_server", "required_launch_pins", "duration_seconds", "interaction_script"),
        optional_payload_keys=("qa_review",),
        nested_contracts=("visual.required_launch_pins", "qa_review"),
        enum_choices={"mcp_server": ("launcher_qa",)},
        shape_hint="Request one bounded interaction proof with launcher_qa and redaction-safe launch pins.",
    ),
    DecisionType.REQUEST_QA_REVIEW: DecisionContract(
        DecisionType.REQUEST_QA_REVIEW,
        required_payload_keys=("stage_id", "proof_ids", "handoff"),
        optional_payload_keys=("delivery",),
        nested_contracts=("request_qa_review.handoff", "delivery"),
        shape_hint="Use only with existing passed proof IDs; handoff.to must be qa and stage_complete true.",
    ),
    DecisionType.REPORT_QA_VERDICT: DecisionContract(
        DecisionType.REPORT_QA_VERDICT,
        required_payload_keys=("review_scope",),
        optional_payload_keys=("verdict", "proof_ids", "delivery_packets_reviewed", "findings", "reviewed_stage_ids", "proof_requirements_confirmed", "test_plan_confirmed", "qa_review"),
        nested_contracts=("qa_review", "qa_review.coverage"),
        enum_choices={"review_scope": ("plan", "implementation"), "verdict": ("approved", "needs_fixes", "blocked")},
        shape_hint="For implementation verdicts include proof_ids and findings; attach qa_review packet for machine routing.",
    ),
    DecisionType.APPROVE: DecisionContract(
        DecisionType.APPROVE,
        required_payload_keys=("review_scope",),
        optional_payload_keys=("verdict", "proof_ids", "findings", "reviewed_stage_ids", "proof_requirements_confirmed", "test_plan_confirmed", "qa_review"),
        nested_contracts=("qa_review", "qa_review.coverage"),
        enum_choices={"review_scope": ("plan", "implementation"), "verdict": ("approved", "needs_fixes", "blocked")},
        shape_hint="Plan approval or implementation approval; implementation approval requires proof IDs.",
    ),
    DecisionType.BLOCK: DecisionContract(
        DecisionType.BLOCK,
        required_payload_keys=("reason", "log_ref"),
        optional_payload_keys=("failed_proof_ids", "delivery"),
        nested_contracts=("block.log_ref", "delivery"),
        shape_hint="Block with exact evidence and a redaction-safe log handle; do not add free-form diagnostics keys.",
    ),
    DecisionType.COMPLETE: DecisionContract(
        DecisionType.COMPLETE,
        shape_hint="Reserved terminal signal; specialist personas should normally not emit this.",
    ),
    DecisionType.REPORT_ISSUE_DISCOVERY: DecisionContract(
        DecisionType.REPORT_ISSUE_DISCOVERY,
        required_payload_keys=("title", "summary"),
        optional_payload_keys=("severity", "relationship_hint", "evidence", "affected_paths", "suggested_child_title", "suggested_child_description", "suggested_acceptance_criteria", "delivery"),
        nested_contracts=("delivery",),
        enum_choices={"severity": ("low", "medium", "high", "critical"), "relationship_hint": ("blocks_current", "same_scope", "fork_child", "defer", "escalate", "unknown")},
        shape_hint="Report unrelated or out-of-scope findings without mutating the parent mission.",
    ),
    DecisionType.TRIAGE_ISSUE_DISCOVERY: DecisionContract(
        DecisionType.TRIAGE_ISSUE_DISCOVERY,
        required_payload_keys=("discovery_id", "decision", "rationale"),
        optional_payload_keys=("priority", "child_title", "child_description", "child_acceptance_criteria"),
        enum_choices={"decision": ("same_scope", "fork_child", "defer", "escalate", "blocks_current")},
        shape_hint="Neko/PM triages one existing issue discovery.",
    ),
    DecisionType.RESOLVE_INCIDENT: DecisionContract(
        DecisionType.RESOLVE_INCIDENT,
        required_payload_keys=("incident_id", "resolution"),
        optional_payload_keys=("next_state",),
        shape_hint="Close a specific open incident and optionally route to the next task state.",
    ),
}


_HUD_SHAPES: dict[str, HudShape] = {
    "common.block": HudShape(
        "common.block",
        DecisionType.BLOCK,
        "Block With Evidence",
        _ALL_ROLES,
        "Use when the next safe action is impossible with current evidence.",
        payload_template={"reason": "<exact blocker>", "log_ref": {"path": "events.jsonl", "line": 1, "summary": "<redaction-safe evidence>"}},
        nested_required={"log_ref": ("path", "line", "summary")},
    ),
    "common.request_file_reads": HudShape(
        "common.request_file_reads",
        DecisionType.REQUEST_FILE_READS,
        "Request File Reads",
        _ALL_ROLES,
        "Use instead of broad prompt context when a specific file excerpt is missing.",
        payload_template={"paths": ["path/relative/to/affected_repo"], "reason": "<why this exact bounded file bundle is required>"},
    ),
    "common.needs_context": HudShape(
        "common.needs_context",
        DecisionType.NEEDS_CONTEXT,
        "Needs Context",
        (AgentRole.PM, AgentRole.DEV, AgentRole.ALICE_SUPERVISOR),
        "Use only when request_file_reads cannot express the missing context.",
        payload_template={"reason": "<specific missing non-file context>", "requested_context": "<bounded context handle or question>", "missing": ["<missing context handle>"]},
    ),
    "common.request_human": HudShape(
        "common.request_human",
        DecisionType.REQUEST_HUMAN,
        "Request Human",
        _PM_NEKO,
        "Use for credentials, approvals, or external blockers the Harness cannot self-heal.",
        payload_template={"reason": "<external action required>", "requested_action": "<one concrete human action>"},
    ),
    "common.escalate": HudShape(
        "common.escalate",
        DecisionType.ESCALATE,
        "Escalate Issue",
        (AgentRole.DEV, AgentRole.QA),
        "Use only when the discovered issue is too large to fix inline.",
        payload_template={"title": "<short issue title>", "summary": "<redaction-safe summary>", "severity": "low|medium|high|critical", "evidence": ["<safe evidence handle>"]},
        enum_choices={"severity": ("low", "medium", "high", "critical")},
    ),
    "neko.scope_route": HudShape(
        "neko.scope_route",
        DecisionType.SCOPE_ROUTE,
        "Scope Route",
        _NEKO_ONLY,
        "Scope one bounded owner/repo/proof gate. Harness derives the internal handoff packet.",
        payload_template={"objective": "<bounded next objective>", "acceptance_criteria": ["<proof-backed completion criterion>"], "target_owner": "dev|backend_dev|qa|neko_supervisor|human", "target_repo": "EterniaLauncher|EterniaBackend|hermes-agent|none", "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed", "visual_required": False}},
        enum_choices={"target_owner": ("dev", "backend_dev", "qa", "neko_supervisor", "human"), "target_repo": ("EterniaLauncher", "EterniaBackend", "hermes-agent", "none")},
    ),
    "neko.bounded_visual_proof_recovery": HudShape(
        "neko.bounded_visual_proof_recovery",
        DecisionType.PROPOSE_ACCEPTANCE,
        "Bounded Visual Proof Recovery",
        _NEKO_ONLY,
        "Use after current-stage visual/MCP proof failed and Dev needs one bounded proof-command recovery.",
        payload_template={"objective": "<bounded visual proof recovery objective>", "acceptance_criteria": ["Passed fullscreen Stage C Mission Control visual proof is attached."], "affected_repos": ["EterniaLauncher"], "handoff_packet": {"packet_kind": "bounded_visual_proof_recovery", "mission_phase": "visual_proof_recovery", "handoff_mode": "single_specialist", "target_owner": "dev", "target_repo": "EterniaLauncher", "proof_gate": {"required": True, "minimum_status": "passed", "visual_required": True, "required_proof_types": ["fullscreen_stage_c_screenshot"]}, "join_gate": {"release_condition": "passed fullscreen visual proof attached"}}},
        nested_required={"handoff_packet": ("packet_kind", "mission_phase", "handoff_mode", "target_owner", "target_repo", "proof_gate")},
    ),
    "neko.bounded_dev_recovery": HudShape(
        "neko.bounded_dev_recovery",
        DecisionType.PROPOSE_ACCEPTANCE,
        "Bounded Dev Recovery",
        _NEKO_ONLY,
        "Use after current-stage command proof failed and Dev needs one bounded proof-backed recovery.",
        payload_template={"objective": "<bounded recovery objective>", "acceptance_criteria": ["Failed proof ID is reused and one changed-signal retry is attempted."], "handoff_packet": {"packet_kind": "bounded_dev_recovery", "mission_phase": "proof_recovery", "handoff_mode": "single_specialist", "target_owner": "dev", "target_repo": "EterniaLauncher|EterniaBackend|hermes-agent", "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed", "visual_required": False}}},
        nested_required={"handoff_packet": ("packet_kind", "mission_phase", "handoff_mode", "target_owner", "target_repo", "proof_gate")},
    ),
    "neko.qa_coordination_release": HudShape(
        "neko.qa_coordination_release",
        DecisionType.PROPOSE_ACCEPTANCE,
        "QA Coordination Release",
        _NEKO_ONLY,
        "Use after all required Dev proof is attached and QA is the next owner.",
        payload_template={"objective": "<QA coordination release objective>", "acceptance_criteria": ["QA verifies attached proof IDs before approval."], "handoff_packet": {"packet_kind": "qa_coordination_release", "mission_phase": "qa_handoff", "handoff_mode": "sequential_specialists", "target_owner": "qa", "target_repo": "EterniaLauncher|EterniaBackend|hermes-agent", "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed", "visual_required": False, "required_proof_ids": ["<all required passed proof IDs>"]}, "join_gate": {"release_condition": "backend and Launcher proof IDs are attached; QA verifies status, scope, and cross-stack join before verdict."}}},
        nested_required={"handoff_packet": ("packet_kind", "mission_phase", "handoff_mode", "target_owner", "target_repo", "proof_gate", "join_gate")},
    ),
    "neko.resolve_incident": HudShape(
        "neko.resolve_incident",
        DecisionType.RESOLVE_INCIDENT,
        "Resolve Incident",
        _NEKO_ONLY,
        "Use only for a specific open incident when the next state is deterministic.",
        payload_template={"incident_id": "<open incident id>", "resolution": "<bounded recovery route>", "next_state": "dev_implementing"},
    ),
    "neko.triage_issue_discovery": HudShape(
        "neko.triage_issue_discovery",
        DecisionType.TRIAGE_ISSUE_DISCOVERY,
        "Triage Discovery",
        _NEKO_ONLY,
        "Use only when issue discoveries are waiting for Neko triage.",
        payload_template={"discovery_id": "<existing discovery id>", "decision": "same_scope|fork_child|defer|escalate|blocks_current", "rationale": "<bounded rationale>"},
        enum_choices={"decision": ("same_scope", "fork_child", "defer", "escalate", "blocks_current")},
    ),
    "dev.request_test_run": HudShape(
        "dev.request_test_run",
        DecisionType.REQUEST_TEST_RUN,
        "Request Proof",
        _DEV_ONLY,
        "Use when the current stage has proof commands or after a bounded patch needs Harness-owned proof.",
        payload_template={"stage_id": "<current stage>", "commands": ["<focused proof command>"]},
    ),
    "dev.hand_off": HudShape(
        "dev.hand_off",
        DecisionType.HAND_OFF,
        "Hand Off",
        _DEV_ONLY,
        "Signal done. Harness captures the isolated worktree diff and reruns the authoritative gate.",
        payload_template={"stage_id": "<current stage>", "summary": "<optional short done signal>"},
    ),
    "dev.correct_stage": HudShape(
        "dev.correct_stage",
        DecisionType.CORRECT_STAGE,
        "Correct Stage",
        _DEV_ONLY,
        "Use when current stage instructions are stale or ambiguous; do not put delivery here.",
        payload_template={"stage_id": "<current stage>", "target_stage_id": "<known stage id when rerouting>", "corrections": ["<exact correction>"], "audit_notes": ["<why>"], "affected_paths": ["<relative path>"], "test_plan": ["<focused proof command>"]},
    ),
    "dev.propose_stage_plan": HudShape(
        "dev.propose_stage_plan",
        DecisionType.PROPOSE_STAGE_PLAN,
        "Propose Stage Plan",
        _DEV_ONLY,
        "Use only when there is no current executable stage and Dev must slice work.",
        payload_template={"stages": [{"id": "<stage_id>", "title": "<short title>", "objective": "<bounded objective>", "acceptance_criteria": ["<proof-backed criterion>"], "affected_paths": ["<relative path>"], "test_plan": ["<focused proof command>"]}]},
        nested_required={"stages[]": ("title", "objective", "acceptance_criteria")},
    ),
    "dev.request_screenshot": HudShape(
        "dev.request_screenshot",
        DecisionType.REQUEST_SCREENSHOT,
        "Request Screenshot",
        _DEV_ONLY,
        "Use when a no-product-edit visual stage must attach launcher_qa screenshot proof.",
        payload_template={"stage_id": "<current stage>", "target": "mission_control", "proof_requirement": "<exact visual claim>", "mcp_server": "launcher_qa", "required_launch_pins": {"hermes_profile": "<head_agent_profile>", "runtime_root_id": "agent-runtime"}},
        nested_required={"required_launch_pins": ("hermes_profile", "runtime_root_id")},
        enum_choices={"mcp_server": ("launcher_qa",)},
    ),
    "qa.request_screenshot": HudShape(
        "qa.request_screenshot",
        DecisionType.REQUEST_SCREENSHOT,
        "Request Screenshot",
        _QA_ONLY,
        "Use when required visual evidence is missing.",
        payload_template={"stage_id": "<current stage>", "target": "mission_control", "proof_requirement": "<exact visual claim>", "mcp_server": "launcher_qa", "required_launch_pins": {"hermes_profile": "<head_agent_profile>", "runtime_root_id": "agent-runtime"}},
        nested_required={"required_launch_pins": ("hermes_profile", "runtime_root_id")},
        enum_choices={"mcp_server": ("launcher_qa",)},
    ),
    "qa.request_video": HudShape(
        "qa.request_video",
        DecisionType.REQUEST_VIDEO,
        "Request Video",
        _QA_ONLY,
        "Use only when motion or interaction, not a still screenshot, is required.",
        payload_template={"stage_id": "<current stage>", "target": "mission_control", "proof_requirement": "<exact interaction claim>", "mcp_server": "launcher_qa", "required_launch_pins": {"hermes_profile": "<head_agent_profile>", "runtime_root_id": "agent-runtime"}, "duration_seconds": 5, "interaction_script": ["<bounded semantic interaction>"]},
        nested_required={"required_launch_pins": ("hermes_profile", "runtime_root_id")},
        enum_choices={"mcp_server": ("launcher_qa",)},
    ),
    "qa.verdict": HudShape(
        "qa.verdict",
        DecisionType.QA_VERDICT,
        "QA Verdict",
        _QA_ONLY,
        "Verdict over Harness-verified proof.",
        payload_template={"verdict": "approved|needs_fixes|blocked", "coverage": {"command_gate": "reviewed|blocked|missing"}, "findings": []},
        enum_choices={"verdict": ("approved", "needs_fixes", "blocked")},
    ),
    "qa.request_test_run": HudShape(
        "qa.request_test_run",
        DecisionType.REQUEST_TEST_RUN,
        "Request QA Proof",
        _QA_ONLY,
        "Use only advertised available_proof_recipes or one focused missing command.",
        payload_template={"stage_id": "<current stage>", "recipe_id": "<advertised_recipe_id>"},
    ),
    "qa.correct_stage": HudShape(
        "qa.correct_stage",
        DecisionType.CORRECT_STAGE,
        "Correct Proof Gate",
        _QA_ONLY,
        "Use only if the stage/proof gate is wrong; do not patch code.",
        payload_template={"stage_id": "<current stage>", "target_stage_id": "<known stage id when rerouting>", "corrections": ["<stage/proof gate correction>"], "audit_notes": ["<why QA corrected the stage>"], "test_plan": ["<optional proof command>"]},
    ),
}


_EVENT_CONTRACTS: dict[str, EventContract] = {
    "foreground_runtime.closed": EventContract("foreground_runtime.closed", "Foreground runtime closed", ("runtime_instance_id", "task_id", "lane", "state"), ("reason",)),
    "lane.created": EventContract("lane.created", "Lane created", ("runtime_instance_id", "task_id", "state"), ("lane_kind", "reason")),
    "lane.transitioned": EventContract("lane.transitioned", "Lane transitioned", ("runtime_instance_id", "task_id", "state"), ("reason",)),
    "lane.transition_rejected": EventContract("lane.transition_rejected", "Lane transition rejected", ("runtime_instance_id", "from", "to"), ("reason",)),
    # S17 de-registered run.heartbeat (RunStore.heartbeat) and run.approved
    # (RunStore.approve_continuation) with their writers. run.opened is the
    # third of that set: its writer (RunStore.open_run) is gone too, but two
    # filler appends in tests/agent_runtime/test_events.py still mint it, so it
    # stays registered until those are retargeted — de-registering first would
    # only convert a stale test into a crash. run.closed is NOT in this set: it
    # is still LIVE via RunStore.cancel -> close_run (operator takeover and
    # persona-chat replacement both reach it).
    "run.opened": EventContract("run.opened", "Run opened", ("run_id", "persona_id", "stage_id"), ("model", "provider")),
    "run.progress": EventContract("run.progress", "Run progress", ("phase", "step", "status", "summary"), ("next_expected", "proof_id")),
    "child.returned": EventContract("child.returned", "Child returned", ("parent_node_id", "child_node_id", "summary"), ("proof_ids", "artifact_refs", "stage_id", "persona_instance_id")),
    "run.tool.started": EventContract("run.tool.started", "Tool started", ("tool_name",), ("run_id",)),
    "run.tool.finished": EventContract("run.tool.finished", "Tool finished", ("tool_name", "status"), ("duration_ms",)),
    "decision_contract.parity": EventContract("decision_contract.parity", "Decision contract parity", ("mode", "status", "public_decision_type", "execution_decision_type"), ("legacy_decision_type", "shimmed", "blocked_reason")),
    "run.closed": EventContract("run.closed", "Run closed", ("state", "decision_type"), ("total_tokens",)),
    "role_envelope.opened": EventContract("role_envelope.opened", "Role envelope opened", ("envelope_id", "role_id"), ("mission_stage_id", "checklist_id")),
    "role_envelope.continued": EventContract("role_envelope.continued", "Role envelope continued", ("envelope_id", "status"), ("decision_type", "proof_count", "no_progress_count")),
    "role_envelope.paused": EventContract("role_envelope.paused", "Role envelope paused", ("envelope_id", "status"), ("decision_type", "proof_count", "no_progress_count")),
    "role_envelope.closed": EventContract("role_envelope.closed", "Role envelope closed", ("envelope_id", "close_reason"), ("role_id", "mission_stage_id")),
    "role_checklist.created": EventContract("role_checklist.created", "Role checklist created", ("checklist_id", "role_id"), ("mission_stage_id",)),
    "role_checklist.item_updated": EventContract("role_checklist.item_updated", "Role checklist item updated", ("checklist_id", "revision"), ("item_ids", "role_id")),
    "persona_instance.created": EventContract("persona_instance.created", "Persona instance created", ("persona_instance_id",), ("persona_id",)),
    "persona_instance.attributed": EventContract("persona_instance.attributed", "Persona instance attributed to a goal", ("persona_instance_id", "goal_id"), ("persona_id", "spawned_by")),
    "persona_instance.steered": EventContract("persona_instance.steered", "Persona instance steering edge changed", ("persona_instance_id", "goal_id"), ("persona_id", "spawned_by", "steered_by", "added", "removed", "detached")),
    "persona_instance.reaped": EventContract("persona_instance.reaped", "Stale persona instance reaped from the live graph", ("persona_instance_id", "reason"), ("task_id", "goal_id", "owner_state")),
    "persona_instance.reconciled": EventContract("persona_instance.reconciled", "Legacy-id persona instance row folded onto its canonical channel", ("persona_instance_id", "from_id", "to_id", "action"), ("persona_id",)),
    "persona_instance.pruned": EventContract("persona_instance.pruned", "Orphaned/legacy-role persona instance archived from the live graph", ("persona_instance_id", "reason"), ("persona_id", "role", "profile_id", "updated_at")),
    "persona_instance.chat_binding_cleared": EventContract("persona_instance.chat_binding_cleared", "Persona instance unbound from a chat session (operator delete, or a binding whose session SessionDB no longer has)", ("persona_instance_id", "session_id", "reason"), ("persona_id", "cleared_fields", "mode_before", "mode_after")),
    "persona_instance.retired": EventContract("persona_instance.retired", "Placement-backed persona instance retired (end-of-life) to the archive on placement removal", ("persona_instance_id", "reason"), ("persona_id", "mode", "requested_by", "archive_dir")),
    "steer.returned": EventContract("steer.returned", "Steer returned", ("action_id", "verb", "source_node_id", "target_node_id"), ("result", "stage_id", "persona_instance_id")),
    "operator.takeover.requested": EventContract("operator.takeover.requested", "Operator takeover requested", ("worker_session_id", "actor"), ("reason", "cancel_active_run")),
    "operator.takeover.approval_required": EventContract("operator.takeover.approval_required", "Operator takeover approval required", ("worker_session_id", "actor", "approval"), ("reason",)),
    "operator.takeover.applied": EventContract("operator.takeover.applied", "Operator takeover applied", ("worker_session_id", "actor"), ("parked_lane_ids", "paused_worker_ids", "cancelled_run_id", "approval_required")),
    "persona_instance.chat_opened": EventContract("persona_instance.chat_opened", "Persona instance chat opened", ("persona_instance_id", "session_id"), ("persona_id",)),
    "persona_chat.projected": EventContract("persona_chat.projected", "Persona chat turn projection committed", ("persona_instance_id", "root_chat_session_id", "client_message_id", "turn_id", "change_kind"), ("active_session_id", "native_revision")),
    "persona_chat.metadata_updated": EventContract("persona_chat.metadata_updated", "Persona chat session metadata updated", ("persona_instance_id", "root_chat_session_id", "change_kind"), ()),
    "persona_instance.profile_updated": EventContract("persona_instance.profile_updated", "Persona instance runtime profile updated", ("persona_instance_id",), ("persona_id", "display_name", "current_chat_goal", "goal_id", "skill_overrides", "provider", "model", "api_mode", "requested_by")),
    "persona_assignment.created": EventContract("persona_assignment.created", "Persona assignment created", ("assignment_id", "persona_instance_id", "kind"), ("state",)),
    "persona_assignment.closed": EventContract("persona_assignment.closed", "Persona assignment closed", ("assignment_id", "state"), ("kind",)),
    "repo_bundle.created": EventContract("repo_bundle.created", "Repo bundle created", ("repo_bundle_id", "repo", "state"), ("owner_persona_id",)),
    "repo_bundle.updated": EventContract("repo_bundle.updated", "Repo bundle updated", ("repo_bundle_id", "repo", "state"), ("wake_condition",)),
    "repo_bundle.assigned": EventContract("repo_bundle.assigned", "Repo bundle assigned", ("repo_bundle_id", "repo", "state"), ("assignment_id", "run_id")),
    "repo_bundle.running": EventContract("repo_bundle.running", "Repo bundle running", ("repo_bundle_id", "repo", "state"), ("run_id",)),
    "repo_bundle.delivered": EventContract("repo_bundle.delivered", "Repo bundle delivered", ("repo_bundle_id", "repo", "state"), ("proof_count",)),
    "repo_bundle.verified": EventContract("repo_bundle.verified", "Repo bundle verified", ("repo_bundle_id", "repo", "state"), ("proof_count",)),
    "repo_bundle.rejected": EventContract("repo_bundle.rejected", "Repo bundle rejected", ("repo_bundle_id", "repo", "state"), ("reason",)),
    "repo_bundle.woke": EventContract("repo_bundle.woke", "Repo bundle woke", ("repo_bundle_id", "repo", "state"), ("wake_condition",)),
    "worker_session.opened": EventContract("worker_session.opened", "Worker opened", ("worker_session_id", "persona_id"), ("stage_id",)),
    "worker_session.assigned": EventContract("worker_session.assigned", "Worker assigned", ("worker_session_id", "run_id"), ("stage_id",)),
    "worker_session.resumed": EventContract("worker_session.resumed", "Worker resumed", ("worker_session_id",), ("session_id_present",)),
    "worker_session.heartbeat": EventContract("worker_session.heartbeat", "Worker heartbeat", ("worker_session_id", "state"), ("heartbeat_age_seconds",)),
    "worker_session.context_absorbed": EventContract("worker_session.context_absorbed", "Context absorbed", ("worker_session_id", "context_receipt_id"), ("watchdog_warnings",)),
    "worker_session.steered": EventContract("worker_session.steered", "Worker steered", ("worker_session_id", "actor"), ("note",)),
    "worker_session.possessed": EventContract("worker_session.possessed", "Worker possessed", ("worker_session_id", "lease_owner"), ("lease_expires_at",)),
    "worker_session.released": EventContract("worker_session.released", "Worker released", ("worker_session_id", "actor"), ("handback",)),
    "worker_session.watchdog_warning": EventContract("worker_session.watchdog_warning", "Worker warning", ("worker_session_id", "kind"), ("summary",)),
    "worker_session.closed": EventContract("worker_session.closed", "Worker closed", ("worker_session_id", "close_reason"), ("state",)),
    "self_test.recorded": EventContract("self_test.recorded", "Self-test recorded", ("evidence_id", "status"), ("stage_id", "command_label")),
    "self_test.loop_detected": EventContract("self_test.loop_detected", "Self-test loop detected", ("command_hash", "repeat_count"), ("stage_id",)),
    "packet.recorded": EventContract("packet.recorded", "Packet recorded", ("packet_id", "packet_type"), ("content_hash",)),
    "packet.duplicate": EventContract("packet.duplicate", "Packet duplicate", ("packet_id", "duplicate_of"), ("content_hash",)),
    "packet.normalized": EventContract("packet.normalized", "Packet normalized", ("packet_id", "packet_type", "normalization_status"), ("dropped_fields", "renamed_fields", "truncated_fields", "raw_artifact_id")),
    "incident.opened": EventContract("incident.opened", "Incident opened", ("incident_id", "kind"), ("summary",)),
    "incident.closed": EventContract("incident.closed", "Incident closed", ("incident_id",), ("reason",)),
    # Realm store mutations. Every RealmStore write MUST ride one of
    # these: the stream/read-model pipeline is watermark-gated on the
    # EventLog, so an event-less mutation is invisible to every consumer
    # (launcher snapshot, serve read model) until an unrelated event
    # happens to advance the offset.
    "realm.adopted": EventContract("realm.adopted", "Realm adopted", ("realm_id", "name"), ("server_id",)),
    "realm.created": EventContract("realm.created", "Realm created", ("realm_id", "name"), ("server_id",)),
    "realm.updated": EventContract("realm.updated", "Realm updated", ("realm_id", "change"), ("server_id",)),
    # Activation events carry realm_id/workspace_id when a scope is activated
    # and {"cleared": true} when the active pointer is cleared — so the ids
    # live in detail_fields, not summary_fields (Stage 12 slice D validates
    # summary_fields on append).
    "realm.activated": EventContract("realm.activated", "Active realm changed", (), ("realm_id", "name", "cleared")),
    "workspace.created": EventContract("workspace.created", "Workspace created", ("workspace_id", "name"), ("realm_id",)),
    "workspace.updated": EventContract("workspace.updated", "Workspace updated", ("workspace_id", "change"), ("name", "persona_id")),
    "workspace.archived": EventContract("workspace.archived", "Workspace archived", ("workspace_id",), ("name",)),
    # Hard delete (archive stays the reversible path). ``reason`` is
    # "operator_delete" or "realm_sync_tombstone" — the resurrection-guard
    # ledger application on pull rides the same store chokepoint.
    "workspace.deleted": EventContract("workspace.deleted", "Workspace deleted", ("workspace_id",), ("name", "realm_id", "reason")),
    "workspace.activated": EventContract("workspace.activated", "Active workspace changed", (), ("workspace_id", "name", "cleared")),
    "realm.sync.pulled": EventContract("realm.sync.pulled", "Realm pulled", ("realm_id", "changed"), ("artifacts",)),
    "realm.sync.published": EventContract("realm.sync.published", "Realm published", ("realm_id", "changed"), ("artifacts", "commit")),
    # Mission Board store mutations. Every BoardStore write MUST ride one of
    # these (standing store rule): the stream/read-model pipeline is
    # watermark-gated on the EventLog, so an event-less board write is invisible
    # to every consumer until an unrelated event advances the offset. Cards
    # NEVER mutate goal state — these are planning-domain events only.
    "board.created": EventContract("board.created", "Board created", ("board_id", "workspace_id"), ("title",)),
    "board.updated": EventContract("board.updated", "Board updated", ("board_id", "change"), ("title", "revision")),
    "board.card.created": EventContract("board.card.created", "Board card created", ("board_id", "card_id"), ("title", "column_id", "created_by")),
    "board.card.moved": EventContract("board.card.moved", "Board card moved", ("board_id", "card_id", "column_id"), ("from_column_id", "order_key")),
    "board.card.edited": EventContract("board.card.edited", "Board card edited", ("board_id", "card_id"), ("fields", "revision")),
    "board.card.archived": EventContract("board.card.archived", "Board card archived", ("board_id", "card_id", "reason"), ("column_id",)),
    "board.card.restored": EventContract("board.card.restored", "Board card restored", ("board_id", "card_id"), ("column_id",)),
    "board.card.conflict_resolved": EventContract("board.card.conflict_resolved", "Board card sync conflict resolved", ("board_id", "card_id", "take"), ("revision",)),
    "board.rebalanced": EventContract("board.rebalanced", "Board column order keys rebalanced", ("board_id", "column_id"), ("card_count",)),
    # Mission Office events — the OfficeStore chokepoint emits one on EVERY
    # mutation (same standing store rule as boards; realm-synced the same way).
    # See EterniaLauncher docs/mission_control/OFFICE_LAYOUT_REALM_SYNC_PLAN_2026-07-17.md.
    "office.surface.created": EventContract("office.surface.created", "Office surface created", ("workspace_id",), ()),
    "office.surface.updated": EventContract("office.surface.updated", "Office surface updated", ("workspace_id", "change"), ("revision",)),
    "office.actor.upserted": EventContract("office.actor.upserted", "Office actor placement upserted", ("workspace_id", "actor_key"), ("persona_id", "items", "revision")),
    "office.actor.removed": EventContract("office.actor.removed", "Office actor placement archived", ("workspace_id", "actor_key", "reason"), ()),
    "office.actor.restored": EventContract("office.actor.restored", "Office actor placement restored", ("workspace_id", "actor_key"), ()),
    "office.actor.conflict_resolved": EventContract("office.actor.conflict_resolved", "Office actor sync conflict resolved", ("workspace_id", "actor_key", "take"), ("revision",)),
    "persona.updated": EventContract("persona.updated", "Persona updated", ("persona_id",), ("display_name",)),
    # The persona ⇄ Hermes-profile rebind chokepoint
    # (``persona_profile_binding.rebind_persona_profile``). ONE event per
    # operation: it moves the persona authority through ``AgentStore.save`` AND
    # cascades every ``persona_instance.profile_id`` projection, so the payload
    # names each moved row (bounded; the overflow is accounted in
    # ``instances_truncated``, never dropped silently). Deliberately NOT added to
    # ``patch_coverage.COVERED_DOMAIN_EVENT_TYPES`` — a rebind batch must degrade
    # to a full core, not ship a patch frame that folds nothing.
    "persona.profile_rebound": EventContract(
        "persona.profile_rebound",
        "Persona rebound to a different Hermes profile",
        ("persona_id", "from_profile", "to_profile"),
        (
            "actor",
            "instance_count",
            "instances",
            "instances_truncated",
            # Partial-apply accounting: the persona authority moved but these
            # projection rows did not. Emitted ON the run that stranded them —
            # the `_agent_*` placement rows have no self-heal, so this event is
            # the only durable record that they need a retry.
            "status",
            "failed_count",
            "failed",
            "failed_truncated",
        ),
    ),
    # Synthetic watchdog event: appended by stream_frames when the scope/catalog
    # fingerprint changed while the EventLog offset did not — an event-less write
    # slipped the Stage 12 rule. Advances the watermark so gated consumers
    # converge; every occurrence names a producer bug to fix at the source.
    "state.reconciled": EventContract("state.reconciled", "Read model reconciled after event-less write", ("fingerprint",), ("source",)),
    # S7-A read-model producer: an op-based, WIRE-LEVEL state-patch entry.
    # Payload is ``{entity, id, op, changed?}`` where op ∈ {upsert, remove,
    # refresh}. ``upsert`` carries ``changed`` — the projected wire fields the
    # mutation affected (derived dependents recomputed), sized to the 4KB cap
    # (oversize values become accounted {oversize,bytes} markers; an unavoidable
    # overflow degrades the op to ``refresh``). ``remove``/``refresh`` carry no
    # ``changed``. ``entity``/``id``/``op`` are the summary fields every emission
    # carries; ``changed`` is optional (detail). ``seq``/``ts`` ride the EventLog
    # envelope, not the payload. Emitted only when ``read_model.delta_patches``
    # is on (default off → this type never appears).
    "state.patched": EventContract("state.patched", "State patched", ("entity", "id", "op"), ("changed",)),
}
