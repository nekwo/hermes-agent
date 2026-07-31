from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .personas import AgentRole


class DecisionPayloadInvalid(ValueError):
    """Raised when a model decision is missing JSON, malformed, or role-invalid."""


class DecisionType(StrEnum):
    NEEDS_CONTEXT = "needs_context"
    PROPOSE_ACCEPTANCE = "propose_acceptance"
    PROPOSE_STAGE_PLAN = "propose_stage_plan"
    CORRECT_STAGE = "correct_stage"
    REQUEST_FILE_READS = "request_file_reads"
    HAND_OFF = "hand_off"
    ESCALATE = "escalate"
    SCOPE_ROUTE = "scope_route"
    QA_VERDICT = "qa_verdict"
    PROPOSE_PATCH = "propose_patch"
    REQUEST_TEST_RUN = "request_test_run"
    REQUEST_SCREENSHOT = "request_screenshot"
    REQUEST_VIDEO = "request_video"
    REQUEST_QA_REVIEW = "request_qa_review"
    REPORT_QA_VERDICT = "report_qa_verdict"
    APPROVE = "approve"
    BLOCK = "block"
    COMPLETE = "complete"
    REQUEST_HUMAN = "request_human"
    PERSONA_MESSAGE_REPLY = "persona_message.reply"
    REPORT_ISSUE_DISCOVERY = "report_issue_discovery"
    TRIAGE_ISSUE_DISCOVERY = "triage_issue_discovery"
    RESOLVE_INCIDENT = "resolve_incident"


@dataclass(slots=True)
class AgentDecision:
    type: DecisionType
    summary: str
    rationale: str
    payload: dict[str, Any]
    requires_approval: bool = False
    schema_version: int = 1


from .decision_contract_registry import agent_decision_json_schema


DECISION_SCHEMA: dict[str, Any] = agent_decision_json_schema()

def parse_structured_decision(text: str) -> AgentDecision:
    errors: list[str] = []
    for blob in _extract_json_blobs(text):
        try:
            raw = json.loads(blob)
            _validate_raw_decision(raw)
            return AgentDecision(
                type=DecisionType(raw["type"]),
                summary=raw["summary"],
                rationale=raw["rationale"],
                payload=raw["payload"],
                requires_approval=bool(raw.get("requires_approval", False)),
                schema_version=int(raw.get("schema_version", 1)),
            )
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise DecisionPayloadInvalid(f"could not parse AgentDecision JSON: {errors[-1]}")
    raise DecisionPayloadInvalid("could not parse AgentDecision JSON: no JSON object found")


def to_decision_jsonable(decision: AgentDecision) -> dict[str, Any]:
    return {
        "type": decision.type.value,
        "summary": decision.summary,
        "rationale": decision.rationale,
        "payload": decision.payload,
        "requires_approval": decision.requires_approval,
        "schema_version": decision.schema_version,
    }


def validate_decision_for_role(decision: AgentDecision, role: AgentRole | str) -> None:
    del decision, role


def _extract_json_blobs(text: str) -> list[str]:
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced

    blobs: list[str] = []
    search_from = 0
    while True:
        start = text.find("{", search_from)
        if start == -1:
            break
        end = _find_balanced_json_end(text, start)
        if end is None:
            search_from = start + 1
            continue
        blobs.append(text[start:end])
        search_from = end
    return blobs


def _find_balanced_json_end(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx + 1
    return None


def _validate_raw_decision(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise DecisionPayloadInvalid("decision must be a JSON object")
    allowed_keys = set(DECISION_SCHEMA["properties"])
    extra = set(raw) - allowed_keys
    if extra:
        raise DecisionPayloadInvalid(f"unexpected keys: {sorted(extra)}")
    for key in DECISION_SCHEMA["required"]:
        if key not in raw:
            raise DecisionPayloadInvalid(f"missing required key: {key}")
    if raw["type"] not in DECISION_SCHEMA["properties"]["type"]["enum"]:
        raise DecisionPayloadInvalid(f"unknown decision type: {raw['type']}")
    if not isinstance(raw["summary"], str) or not (1 <= len(raw["summary"]) <= 280):
        raise DecisionPayloadInvalid("summary must be a 1..280 char string")
    if not isinstance(raw["rationale"], str) or not (1 <= len(raw["rationale"]) <= 4096):
        raise DecisionPayloadInvalid("rationale must be a 1..4096 char string")
    if not isinstance(raw["payload"], dict):
        raise DecisionPayloadInvalid("payload must be an object")
    if "requires_approval" in raw and not isinstance(raw["requires_approval"], bool):
        raise DecisionPayloadInvalid("requires_approval must be boolean")
    if int(raw.get("schema_version", 1)) != 1:
        raise DecisionPayloadInvalid("schema_version must be 1")
