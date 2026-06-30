from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from .decision_contract_registry import contract_hash, validate_object_payload
from .decision_contracts import validate_planning_decision
from .decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType, parse_structured_decision, validate_decision_for_role
from .personas import AgentRole


HARNESS_SKILL_ROLES: dict[str, AgentRole] = {
    "harness-dev-delivery": AgentRole.DEV,
    "launcher-analyze-proof": AgentRole.DEV,
    "harness-qa-verdict": AgentRole.QA,
    "harness-mission-lead": AgentRole.ALICE_SUPERVISOR,
}


def harness_skill_root() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "agent-runtime-harness" / "harness-skills"


def verify_harness_skill_examples(root: Path | None = None) -> dict[str, Any]:
    root = root or harness_skill_root()
    failures: list[dict[str, Any]] = []
    checked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for skill_dir in sorted(root.iterdir()) if root.exists() else []:
        if not skill_dir.is_dir():
            continue
        role = HARNESS_SKILL_ROLES.get(skill_dir.name)
        skill_file = skill_dir / "SKILL.md"
        if role is None or not skill_file.exists():
            continue
        text = skill_file.read_text(encoding="utf-8")
        for index, block in enumerate(_json_code_blocks(text), start=1):
            ref = {"skill": skill_dir.name, "path": str(skill_file), "example_index": index}
            try:
                raw = json.loads(block)
            except json.JSONDecodeError as exc:
                failures.append({**ref, "kind": "json", "error": str(exc)})
                continue
            if not isinstance(raw, dict):
                skipped.append({**ref, "reason": "not_object"})
                continue
            try:
                kind = _validate_example(raw, role=role)
            except DecisionPayloadInvalid as exc:
                failures.append({**ref, "kind": "contract", "error": str(exc), "raw_keys": sorted(raw)})
                continue
            if kind:
                checked.append({**ref, "kind": kind})
            else:
                skipped.append({**ref, "reason": "not_contract_example", "raw_keys": sorted(raw)})
    return {
        "ok": not failures and bool(checked),
        "contract_hash": contract_hash(),
        "root": str(root),
        "checked_count": len(checked),
        "skipped_count": len(skipped),
        "failure_count": len(failures),
        "checked": checked,
        "failures": failures,
    }


def _json_code_blocks(text: str) -> list[str]:
    return re.findall(r"```json\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)


def _validate_example(raw: dict[str, Any], *, role: AgentRole) -> str | None:
    if _looks_like_agent_decision(raw):
        decision = parse_structured_decision(json.dumps(raw, sort_keys=True))
        validate_decision_for_role(decision, role)
        validate_planning_decision(copy.deepcopy(decision))
        return f"agent_decision:{decision.type.value}"
    if "packet_kind" in raw and "handoff_mode" in raw:
        validate_object_payload("handoff_packet", raw)
        validate_planning_decision(_wrap_handoff_packet(raw))
        return "packet:handoff_packet"
    if "work_status" in raw:
        validate_object_payload("delivery", raw)
        validate_planning_decision(_wrap_delivery_packet(raw))
        return "packet:delivery"
    if "coverage" in raw and "next_owner" in raw:
        validate_object_payload("qa_review", raw)
        validate_planning_decision(_wrap_qa_review(raw))
        return "packet:qa_review"
    return None


def _looks_like_agent_decision(raw: dict[str, Any]) -> bool:
    return {"type", "summary", "rationale", "payload"}.issubset(raw)


def _wrap_handoff_packet(packet: dict[str, Any]) -> AgentDecision:
    return AgentDecision(
        type=DecisionType.PROPOSE_ACCEPTANCE,
        summary="Validate handoff packet example.",
        rationale="Harness skill handoff examples must pass the live planning validator.",
        payload={
            "objective": "Validate the handoff packet example.",
            "acceptance_criteria": ["The example passes contract validation."],
            "handoff_packet": copy.deepcopy(packet),
        },
    )


def _wrap_delivery_packet(packet: dict[str, Any]) -> AgentDecision:
    status = str(packet.get("work_status") or "").strip()
    payload: dict[str, Any]
    if status == "planned":
        return AgentDecision(
            type=DecisionType.PROPOSE_STAGE_PLAN,
            summary="Validate delivery packet example.",
            rationale="Stage 46 delivery examples must pass the live planning validator.",
            payload={
                "stages": [
                    {
                        "id": "stage_example",
                        "title": "Example stage",
                        "objective": "Validate delivery packet.",
                        "acceptance_criteria": ["The example passes contract validation."],
                        "delivery": copy.deepcopy(packet),
                    }
                ]
            },
        )
    if status == "patch_proposed":
        payload = {"summary": "Example patch.", "delivery": copy.deepcopy(packet)}
        decision_type = DecisionType.PROPOSE_PATCH
    elif status == "proof_requested":
        payload = {"stage_id": "stage_example", "commands": ["python -c \"print('example')\""], "delivery": copy.deepcopy(packet)}
        decision_type = DecisionType.REQUEST_TEST_RUN
    elif status == "ready_for_qa":
        payload = {
            "stage_id": "stage_example",
            "proof_ids": ["proof_example"],
            "handoff": {"to": "qa", "stage_complete": True},
            "delivery": copy.deepcopy(packet),
        }
        decision_type = DecisionType.REQUEST_QA_REVIEW
    elif status == "blocked":
        payload = {
            "reason": "Example blocker.",
            "log_ref": {"path": "events.jsonl", "line": 1, "summary": "Example blocker evidence."},
            "delivery": copy.deepcopy(packet),
        }
        decision_type = DecisionType.BLOCK
    elif status == "issue_discovered":
        payload = {"title": "Example issue", "summary": "Example issue.", "delivery": copy.deepcopy(packet)}
        decision_type = DecisionType.REPORT_ISSUE_DISCOVERY
    else:
        raise DecisionPayloadInvalid(f"delivery.work_status has no validation wrapper: {status}")
    return AgentDecision(
        type=decision_type,
        summary="Validate delivery packet example.",
        rationale="Stage 46 delivery examples must pass the live planning validator.",
        payload=payload,
    )


def _wrap_qa_review(packet: dict[str, Any]) -> AgentDecision:
    return AgentDecision(
        type=DecisionType.REPORT_QA_VERDICT,
        summary="Validate QA review packet example.",
        rationale="Stage 46 QA review examples must pass the live planning validator.",
        payload={
            "review_scope": "implementation",
            "verdict": "approved",
            "proof_ids": ["proof_example"],
            "findings": [],
            "qa_review": copy.deepcopy(packet),
        },
    )
