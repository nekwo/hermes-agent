import json
from pathlib import Path

import pytest

from agent_runtime.decision_schema import (
    DECISION_SCHEMA,
    AgentDecision,
    DecisionPayloadInvalid,
    DecisionType,
    parse_structured_decision,
    to_decision_jsonable,
    validate_decision_for_role,
)
from agent_runtime.personas import AgentRole


def decision_blob(decision_type, payload=None):
    return json.dumps(
        {
            "type": decision_type,
            "summary": "short useful summary",
            "rationale": "because the harness needs typed decisions",
            "payload": payload or {},
            "requires_approval": False,
            "schema_version": 1,
        }
    )


@pytest.mark.parametrize("decision_type", list(DecisionType))
def test_every_decision_type_parses_and_serializes_losslessly(decision_type):
    decision = parse_structured_decision(decision_blob(decision_type.value))

    assert isinstance(decision, AgentDecision)
    assert decision.type == decision_type
    assert parse_structured_decision(json.dumps(to_decision_jsonable(decision))) == decision


def test_parser_extracts_first_fenced_json_block():
    decision = parse_structured_decision(
        "Here is the decision:\n```json\n"
        + decision_blob("request_test_run", {"commands": ["pytest tests/foo.py"]})
        + "\n```\nThanks"
    )

    assert decision.type == DecisionType.REQUEST_TEST_RUN
    assert decision.payload == {"commands": ["pytest tests/foo.py"]}


def test_parser_extracts_first_balanced_json_object_when_model_appends_text_with_braces():
    decision = parse_structured_decision(
        decision_blob("approve", {"approved": True})
        + "\nFollow-up note: do not parse this {not json} prose as part of the decision."
    )

    assert decision.type == DecisionType.APPROVE
    assert decision.payload == {"approved": True}


def test_parser_skips_non_decision_json_and_extracts_first_valid_agent_decision():
    decision = parse_structured_decision(
        'Evidence snippet: {"cmd": "pytest tests/foo.py", "exit_code": 0}\n'
        + decision_blob("approve", {"approved": True})
    )

    assert decision.type == DecisionType.APPROVE
    assert decision.payload == {"approved": True}


def test_schema_rejects_long_summary_and_bad_shape():
    raw = json.loads(decision_blob("complete"))
    raw["summary"] = "x" * 281

    with pytest.raises(DecisionPayloadInvalid):
        parse_structured_decision(json.dumps(raw))


def test_schema_has_all_decision_type_enum_values():
    assert set(DECISION_SCHEMA["properties"]["type"]["enum"]) == {item.value for item in DecisionType}


def test_role_validation_accepts_configured_role_tokens():
    decision = parse_structured_decision(decision_blob("propose_patch", {"patch": "..."}))

    validate_decision_for_role(decision, AgentRole.PM)
    validate_decision_for_role(decision, "custom-reviewer")


def test_decision_types_are_not_filtered_by_dev_role():
    hand_off = parse_structured_decision(decision_blob("hand_off", {"stage_id": "stage_1", "summary": "done"}))
    qa_review = parse_structured_decision(decision_blob("request_qa_review", {"proof_ids": ["proof_1"]}))
    approve = parse_structured_decision(decision_blob("approve", {"review_scope": "implementation", "proof_ids": ["proof_1"]}))
    qa_verdict = parse_structured_decision(decision_blob("qa_verdict", {"verdict": "approved", "proof_ids": ["proof_1"]}))

    validate_decision_for_role(hand_off, AgentRole.DEV)
    validate_decision_for_role(qa_review, AgentRole.DEV)
    validate_decision_for_role(approve, AgentRole.DEV)
    validate_decision_for_role(qa_verdict, AgentRole.DEV)


def test_decision_types_are_not_filtered_by_supervisor_role():
    scope = parse_structured_decision(decision_blob("scope_route", {"objective": "ship", "acceptance_criteria": ["proved"], "target_owner": "dev", "target_repo": "hermes-agent"}))
    close = parse_structured_decision(decision_blob("approve", {"review_scope": "implementation", "verdict": "approved", "proof_ids": ["proof_1"]}))
    hand_off = parse_structured_decision(decision_blob("hand_off", {"stage_id": "stage_1"}))
    qa_verdict = parse_structured_decision(decision_blob("qa_verdict", {"verdict": "approved"}))

    validate_decision_for_role(scope, AgentRole.ALICE_SUPERVISOR)
    validate_decision_for_role(close, AgentRole.ALICE_SUPERVISOR)
    validate_decision_for_role(hand_off, AgentRole.ALICE_SUPERVISOR)
    validate_decision_for_role(qa_verdict, AgentRole.ALICE_SUPERVISOR)


def test_neko_prompt_matches_mission_lead_role_boundary():
    prompt = (Path(__file__).resolve().parents[2] / "agent_runtime" / "prompts" / "alice_supervisor.md").read_text(encoding="utf-8")

    assert "Neko Mission Lead" in prompt
    assert "scope_route" in prompt
    assert "Allowed AgentDecision types" in prompt
    assert "propose_acceptance" not in prompt
    assert "report_qa_verdict" not in prompt
