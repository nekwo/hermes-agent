from __future__ import annotations

import pytest

from agent_runtime.decision_contracts import validate_planning_decision
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType


def decision(decision_type: DecisionType, payload: dict) -> AgentDecision:
    return AgentDecision(type=decision_type, summary="summary", rationale="rationale", payload=payload)


def handoff_packet(**overrides):
    data = {
        "packet_kind": "fresh_scope",
        "mission_phase": "initial_scope",
        "handoff_mode": "backend_first_cross_stack",
        "target_owner": "backend_dev",
        "target_repo": "EterniaBackend",
        "next_owner": "dev",
        "next_repo": "EterniaLauncher",
        "proof_gate": {
            "required": True,
            "required_proof_types": ["test_run"],
            "minimum_status": "passed",
            "visual_required": False,
        },
        "join_gate": {"release_condition": "backend proof passed"},
    }
    data.update(overrides)
    return data


def qa_review(**overrides):
    data = {
        "coverage": {
            "backend_contract": "reviewed",
            "launcher_integration": "reviewed",
            "visual_or_mcp": "not_required",
            "cross_stack_join": "reviewed",
        },
        "remaining_gaps": [],
        "decision_basis": "proof_packet",
        "next_owner": "harness",
    }
    data.update(overrides)
    return data


def test_handoff_packet_validates_underivable_core():
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": handoff_packet()},
        )
    )


def test_handoff_backend_self_test_command_adapted_to_venv_interpreter():
    """A naked `python manage.py …` self-test cannot run in an isolated worktree
    (no venv on PATH); packet acceptance must hand the dev the canonical repo
    interpreter instead of letting every backend goal burn a discovery turn."""
    packet = handoff_packet()
    packet["proof_gate"]["self_test_command"] = "python manage.py check"
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )
    assert packet["proof_gate"]["self_test_command"] == (
        ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"
    )
    assert "self_test_command adapted" in str(packet.get("operator_note") or "")


def test_handoff_backend_self_test_command_already_canonical_untouched():
    packet = handoff_packet()
    packet["proof_gate"]["self_test_command"] = ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )
    assert packet["proof_gate"]["self_test_command"] == (
        ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"
    )
    assert "self_test_command adapted" not in str(packet.get("operator_note") or "")


def test_handoff_launcher_self_test_command_not_adapted():
    packet = handoff_packet(
        handoff_mode="single_specialist",
        target_owner="dev",
        target_repo="EterniaLauncher",
    )
    packet.pop("next_owner", None)
    packet.pop("next_repo", None)
    packet.pop("join_gate", None)
    packet["proof_gate"]["self_test_command"] = "flutter analyze lib/main.dart"
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )
    assert packet["proof_gate"]["self_test_command"] == "flutter analyze lib/main.dart"


def test_handoff_packet_accepts_compact_harness_rules_metadata():
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "ship",
                "acceptance_criteria": ["proved"],
                "handoff_packet": handoff_packet(
                    harness_rules={
                        "skill_loading": "single relevant skill",
                        "retry_policy": "one proof-backed retry after environment change",
                        "wait_semantic": "beginning-only",
                    }
                ),
            },
        )
    )

    with pytest.raises(DecisionPayloadInvalid):
        validate_planning_decision(
            decision(
                DecisionType.PROPOSE_ACCEPTANCE,
                {
                    "objective": "ship",
                    "acceptance_criteria": ["proved"],
                    "handoff_packet": handoff_packet(harness_rules={"random": "nope"}),
                },
            )
        )


def test_handoff_packet_accepts_final_route_owner_metadata():
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "ship",
                "acceptance_criteria": ["proved"],
                "handoff_packet": handoff_packet(final_owner="qa", final_repo="EterniaLauncher"),
            },
        )
    )

    with pytest.raises(DecisionPayloadInvalid):
        validate_planning_decision(
            decision(
                DecisionType.PROPOSE_ACCEPTANCE,
                {
                    "objective": "ship",
                    "acceptance_criteria": ["proved"],
                    "handoff_packet": handoff_packet(final_owner="random_agent"),
                },
            )
        )


def test_handoff_packet_preserves_machine_readable_join_ids():
    packet = handoff_packet(
        handoff_mode="sequential_specialists",
        target_owner="dev",
        target_repo="EterniaLauncher",
        joined_proof_ids=["proof_backend"],
        joined_contract_packet_ids=["packet_backend_delivery"],
    )
    packet["proof_gate"]["required_proof_ids"] = ["proof_backend"]

    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )

    assert packet["joined_proof_ids"] == ["proof_backend"]
    assert packet["joined_contract_packet_ids"] == ["packet_backend_delivery"]
    assert packet["proof_gate"]["required_proof_ids"] == ["proof_backend"]


def test_qa_coordination_release_defaults_join_gate_from_required_proofs():
    packet = handoff_packet(
        packet_kind="qa_coordination_release",
        mission_phase="qa_handoff",
        handoff_mode="sequential_specialists",
        target_owner="qa",
        target_repo="EterniaLauncher",
    )
    packet["proof_gate"]["required_proof_ids"] = ["proof_backend", "proof_launcher"]
    packet["proof_gate"].pop("required_proof_types")
    packet.pop("join_gate")

    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )

    assert "2 attached required proof ID" in packet["join_gate"]["release_condition"]
    assert packet["proof_gate"]["required_proof_types"] == ["test_run"]
    assert "defaulted for QA coordination release" in packet["operator_note"]


def test_handoff_packet_normalizes_safe_repo_aliases_and_owner_defaults():
    packet = handoff_packet(
        target_repo="backend contract surface",
        next_repo="launcher ui",
        final_owner="qa",
        final_repo="not applicable",
    )

    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )

    assert packet["target_repo"] == "EterniaBackend"
    assert packet["next_repo"] == "EterniaLauncher"
    assert "final_repo" not in packet
    assert "target_repo normalized to EterniaBackend" in packet["operator_note"]


def test_handoff_packet_rejects_unknown_repo_after_safe_normalization():
    with pytest.raises(DecisionPayloadInvalid, match="target_repo is invalid"):
        validate_planning_decision(
            decision(
                DecisionType.PROPOSE_ACCEPTANCE,
                {
                    "objective": "ship",
                    "acceptance_criteria": ["proved"],
                    "handoff_packet": handoff_packet(target_repo="random external repository"),
                },
            )
        )


def test_handoff_packet_defaults_missing_minimum_status_to_passed():
    packet = handoff_packet()
    packet["proof_gate"] = {
        "required": True,
        "required_proof_types": ["test_run"],
        "visual_required": False,
    }

    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )

    assert packet["proof_gate"]["minimum_status"] == "passed"


def test_handoff_packet_normalizes_natural_proof_gate_status_aliases():
    packet = handoff_packet()
    packet["proof_gate"]["minimum_status"] = "ready_for_qa"

    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )

    assert packet["proof_gate"]["minimum_status"] == "passed"


def test_handoff_packet_defaults_irrelevant_non_required_minimum_status():
    packet = handoff_packet(
        handoff_mode="single_specialist",
        target_owner="neko_supervisor",
        target_repo="hermes-agent",
    )
    packet.pop("next_owner")
    packet.pop("next_repo")
    packet.pop("join_gate")
    packet["proof_gate"] = {
        "required": False,
        "required_proof_types": ["diagnostic_observation"],
        "minimum_status": "not_required",
        "visual_required": False,
    }

    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "diagnose", "acceptance_criteria": ["one turn"], "handoff_packet": packet},
        )
    )

    assert packet["proof_gate"]["minimum_status"] == "passed"


def test_handoff_packet_normalizes_unknown_metadata_and_masks_absolute_paths():
    packet = handoff_packet(extra="nope")
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "ship",
                "acceptance_criteria": ["proved"],
                "handoff_packet": packet,
            },
        )
    )

    assert "extra" not in packet
    assert "ignored unsupported metadata keys: extra" in packet["operator_note"]

    packet = handoff_packet(operator_note="see X:/secret/file.txt")
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "ship",
                "acceptance_criteria": ["proved"],
                "handoff_packet": packet,
            },
        )
    )
    assert "<absolute-path-redacted>" in packet["operator_note"]


def test_handoff_packet_drops_unknown_metadata_values_before_redaction_scan():
    packet = handoff_packet(
        launcher_dev_scope={
            "objective": "verify credential absence is treated as blocked environment evidence",
            "log": "see X:/should/not/persist.txt",
        }
    )

    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "ship",
                "acceptance_criteria": ["proved"],
                "handoff_packet": packet,
            },
        )
    )

    assert "launcher_dev_scope" not in packet
    assert "ignored unsupported metadata keys: launcher_dev_scope" in packet["operator_note"]

    packet = handoff_packet(api_key="value")
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "ship",
                "acceptance_criteria": ["proved"],
                "handoff_packet": packet,
            },
        )
    )
    assert "api_key" not in packet

    with pytest.raises(DecisionPayloadInvalid, match="secret-looking text"):
        validate_planning_decision(
            decision(
                DecisionType.PROPOSE_ACCEPTANCE,
                {
                    "objective": "ship",
                    "acceptance_criteria": ["proved"],
                    "handoff_packet": handoff_packet(operator_note="API_KEY=sk-secretsecret"),
                },
            )
        )


def test_delivery_work_status_is_derived_from_decision_type():
    payload = {"stage_id": "stage_1", "commands": ["pytest -q"], "delivery": {"work_status": "ready_for_qa"}}
    validate_planning_decision(decision(DecisionType.REQUEST_TEST_RUN, payload))
    assert payload["delivery"]["work_status"] == "proof_requested"
    assert payload["delivery"]["_normalization"]["renamed_fields"] == ["work_status"]
    assert "delivery.work_status" in payload["delivery"]["operator_note"]

    omitted = {"stage_id": "stage_1", "commands": ["pytest -q"], "delivery": {}}
    validate_planning_decision(
        decision(
            DecisionType.REQUEST_TEST_RUN,
            omitted,
        )
    )
    assert omitted["delivery"]["work_status"] == "proof_requested"


def test_top_level_payload_keys_are_closed_per_decision_type():
    payload = {
        "stage_id": "stage_1",
        "delivery": {"work_status": "proof_requested"},
    }
    validate_planning_decision(
        decision(
            DecisionType.CORRECT_STAGE,
            payload,
        )
    )
    assert "delivery" not in payload

    payload = {
        "stage_id": "stage_1",
        "commands": ["pytest -q"],
        "made_up_context": "do not accept invented fields",
    }
    validate_planning_decision(
        decision(
            DecisionType.REQUEST_TEST_RUN,
            payload,
        )
    )
    assert "made_up_context" not in payload


def test_stage_plan_stage_keys_are_closed():
    with pytest.raises(DecisionPayloadInvalid, match="stages\\[1\\] has unsupported keys"):
        validate_planning_decision(
            decision(
                DecisionType.PROPOSE_STAGE_PLAN,
                {
                    "stages": [
                        {
                            "id": "stage_1",
                            "title": "Proof",
                            "objective": "Run proof",
                            "acceptance_criteria": ["proof passes"],
                            "test_plan": ["pytest -q"],
                            "owner_guess": "qa",
                        }
                    ]
                },
            )
        )


def test_delivery_packet_preserves_consumed_proofs_and_changed_files():
    packet = {
        "work_status": "proof_requested",
        "consumed_contract_packet_ids": ["packet_backend_delivery"],
        "consumed_proof_ids": ["proof_backend"],
        "changed_files": ["lib/mission_control.dart"],
        "proof_ids": ["proof_launcher"],
        "known_gaps": [],
    }

    validate_planning_decision(
        decision(
            DecisionType.REQUEST_TEST_RUN,
            {"stage_id": "stage_1", "commands": ["flutter analyze lib/mission_control.dart"], "delivery": packet},
        )
    )

    assert packet["consumed_contract_packet_ids"] == ["packet_backend_delivery"]
    assert packet["consumed_proof_ids"] == ["proof_backend"]
    assert packet["changed_files"] == ["lib/mission_control.dart"]


def test_delivery_packet_preserves_first_class_handoff_repair_fields():
    packet = {
        "work_status": "ready_for_qa",
        "summary": "metadata repair only",
        "changed_paths": ["agent_runtime/packets.py"],
        "inspected_paths": ["agent_runtime/ticker.py", "agent_runtime/final_gate.py"],
        "dirty_baseline": {"present": True, "preserved": True},
        "coverage_claims": ["focused proof already passed"],
        "known_non_coverage": ["no visual proof required"],
        "proof_reuse_basis": {"proof_id": "proof_existing", "reason": "same command and unchanged paths"},
        "failed_proof_classification": ["shell_wrapper_error"],
        "handoff_repair": {"metadata_only": True},
        "proof_ids": ["proof_existing"],
    }

    validate_planning_decision(
        decision(
            DecisionType.REQUEST_QA_REVIEW,
            {
                "stage_id": "stage_1",
                "proof_ids": ["proof_existing"],
                "handoff": {"to": "qa", "stage_complete": True, "known_gaps": []},
                "delivery": packet,
            },
        )
    )

    assert packet["changed_paths"] == ["agent_runtime/packets.py"]
    assert packet["inspected_paths"] == ["agent_runtime/ticker.py", "agent_runtime/final_gate.py"]
    assert packet["dirty_baseline"]["preserved"] is True
    assert packet["handoff_repair"]["metadata_only"] is True


def test_delivery_packet_preserves_backend_contract_packet_and_defaults_id():
    packet = {
        "work_status": "proof_requested",
        "produced_contract_packet_id": "pending_harness_contract_packet_record",
        "contract_packet": {
            "endpoint": "GET /api/stage47",
            "request_shape": {},
            "response_shape": {"ok": "boolean"},
            "error_shape": {"error": "string"},
            "example_response": {"ok": True},
        },
        "consumed_proof_ids": ["proof_backend"],
        "known_gaps": [],
    }

    validate_planning_decision(
        decision(
            DecisionType.REQUEST_TEST_RUN,
            {"stage_id": "backend_contract", "commands": ["python manage.py check"], "delivery": packet},
        )
    )

    assert packet["contract_packet"]["contract_packet_id"].startswith("packet_contract_")
    assert packet["produced_contract_packet_id"] == packet["contract_packet"]["contract_packet_id"]
    assert "contract_packet" in packet
    assert "defaulted from contract_packet" in packet["operator_note"]


def test_delivery_contract_packet_normalizes_auth_shape_without_leaking_values():
    packet = {
        "work_status": "proof_requested",
        "contract_packet": {
            "endpoint": "GET /api/stage47",
            "request_shape": {
                "auth": "required by /api/auth/session; do not include runtime value",
                "headers": {"Authorization": "Bearer token shape only"},
            },
            "response_shape": {"ok": "boolean"},
            "error_shape": {"error": "string"},
            "example_response": {"ok": True},
        },
        "consumed_proof_ids": ["proof_backend"],
        "known_gaps": [],
    }

    validate_planning_decision(
        decision(
            DecisionType.REQUEST_TEST_RUN,
            {"stage_id": "backend_contract", "commands": ["python manage.py check"], "delivery": packet},
        )
    )

    request_shape = packet["contract_packet"]["request_shape"]
    assert request_shape["auth_shape"] == "required; runtime value omitted; shape only"
    assert request_shape["headers"]["auth_shape"] == "required; runtime value omitted; shape only"
    assert "auth" not in request_shape
    assert "Authorization" not in request_shape["headers"]
    assert "auth-like fields normalized" in packet["operator_note"]


def test_delivery_contract_packet_rejects_actual_secret_auth_shape_value():
    packet = {
        "work_status": "proof_requested",
        "contract_packet": {
            "endpoint": "GET /api/stage47",
            "request_shape": {"auth": "sk-live_actualsecret"},
            "response_shape": {"ok": "boolean"},
            "error_shape": {"error": "string"},
            "example_response": {"ok": True},
        },
        "known_gaps": [],
    }

    with pytest.raises(DecisionPayloadInvalid, match="secret-looking text"):
        validate_planning_decision(
            decision(
                DecisionType.REQUEST_TEST_RUN,
                {"stage_id": "backend_contract", "commands": ["python manage.py check"], "delivery": packet},
            )
        )


def test_qa_review_cross_stack_gap_routes_to_neko():
    with pytest.raises(DecisionPayloadInvalid):
        validate_planning_decision(
            decision(
                DecisionType.REPORT_QA_VERDICT,
                {
                    "review_scope": "implementation",
                    "verdict": "blocked",
                    "proof_ids": ["proof_backend"],
                    "qa_review": qa_review(
                        coverage={
                            "backend_contract": "reviewed",
                            "launcher_integration": "missing",
                            "visual_or_mcp": "missing",
                            "cross_stack_join": "missing",
                        },
                        next_owner="dev",
                    ),
                },
            )
        )
    validate_planning_decision(
        decision(
            DecisionType.REPORT_QA_VERDICT,
            {
                "review_scope": "implementation",
                "verdict": "blocked",
                "proof_ids": ["proof_backend"],
                "qa_review": qa_review(
                    coverage={
                        "backend_contract": "reviewed",
                        "launcher_integration": "missing",
                        "visual_or_mcp": "missing",
                        "cross_stack_join": "missing",
                    },
                    next_owner="neko_supervisor",
                ),
            },
        )
    )


def test_qa_verdict_wraps_one_scalar_finding_without_changing_its_text():
    verdict = decision(
        DecisionType.QA_VERDICT,
        {
            "verdict": "needs_fixes",
            "findings": "  Mission Control still projects duplicate QA markers.  ",
            "proof_ids": ["proof_visual"],
        },
    )

    validate_planning_decision(verdict)

    assert verdict.payload["findings"] == [
        "  Mission Control still projects duplicate QA markers.  "
    ]


def test_qa_verdict_serializes_structured_findings_and_drops_empty_rows():
    verdict = decision(
        DecisionType.QA_VERDICT,
        {
            "verdict": "blocked",
            "findings": [
                {"code": "profile_pin_missing", "proof_id": "proof_visual"},
                "",
                "  ",
            ],
            "proof_ids": ["proof_visual"],
        },
    )

    validate_planning_decision(verdict)

    assert verdict.payload["findings"] == [
        '{"code":"profile_pin_missing","proof_id":"proof_visual"}'
    ]


def test_qa_review_normalizes_notes_metadata_so_skill_shape_stays_strict():
    packet = qa_review(notes="looks good")

    validate_planning_decision(
        decision(
            DecisionType.REPORT_QA_VERDICT,
            {
                "review_scope": "implementation",
                "verdict": "approved",
                "proof_ids": ["proof_backend", "proof_launcher"],
                "findings": [],
                "qa_review": packet,
            },
        )
    )

    assert "notes" not in packet
    assert "ignored unsupported metadata keys: notes" in packet["operator_note"]


def test_delivery_normalizes_metadata_and_masks_bare_secret_vocabulary():
    packet = {
        "work_status": "proof_requested",
        "known_gaps": ["auth token refresh contract still needs proof"],
        "notes": "move this into structured fields",
    }

    validate_planning_decision(
        decision(
            DecisionType.REQUEST_TEST_RUN,
            {"stage_id": "stage_1", "commands": ["pytest -q"], "delivery": packet},
        )
    )

    assert "notes" not in packet
    assert "ignored unsupported metadata keys: notes" in packet["operator_note"]
    assert packet["known_gaps"] == ["auth [redacted-term] refresh contract still needs proof"]


def test_qa_review_allows_harness_real_token_phrase_without_allowing_secrets():
    packet = qa_review(remaining_gaps=["cross-stack real-token behavior beyond command proof remains unproven"])
    validate_planning_decision(
        decision(
            DecisionType.REPORT_QA_VERDICT,
            {
                "review_scope": "implementation",
                "verdict": "approved",
                "proof_ids": ["proof_backend", "proof_launcher"],
                "findings": [],
                "qa_review": packet,
            },
        )
    )

    assert packet["remaining_gaps"] == ["cross-stack real-[redacted-term] behavior beyond command proof remains unproven"]

    packet = qa_review(remaining_gaps=["auth token refresh contract still needs proof"])
    validate_planning_decision(
        decision(
            DecisionType.REPORT_QA_VERDICT,
            {
                "review_scope": "implementation",
                "verdict": "approved",
                "proof_ids": ["proof_backend", "proof_launcher"],
                "findings": [],
                "qa_review": packet,
            },
        )
    )

    assert packet["remaining_gaps"] == ["auth [redacted-term] refresh contract still needs proof"]

    packet = qa_review(remaining_gaps=["access token was printed"])
    validate_planning_decision(
        decision(
            DecisionType.REPORT_QA_VERDICT,
            {
                "review_scope": "implementation",
                "verdict": "blocked",
                "proof_ids": ["proof_backend", "proof_launcher"],
                "findings": [],
                "qa_review": packet,
            },
        )
    )
    assert packet["remaining_gaps"] == ["access [redacted-term] was printed"]

    with pytest.raises(DecisionPayloadInvalid, match="secret-looking text"):
        validate_planning_decision(
            decision(
                DecisionType.REPORT_QA_VERDICT,
                {
                    "review_scope": "implementation",
                    "verdict": "blocked",
                    "proof_ids": ["proof_backend", "proof_launcher"],
                    "findings": [],
                    "qa_review": qa_review(remaining_gaps=["API_KEY=sk-secretsecret"]),
                },
            )
        )


def test_harness_qa_skill_documents_exact_packet_keys():
    from pathlib import Path

    text = Path("docs/agent-runtime-harness/harness-skills/harness-qa-verdict/SKILL.md").read_text(encoding="utf-8")

    assert "Allowed `qa_verdict` payload keys only" in text
    assert "Do not add `notes`" in text
    assert '"type": "qa_verdict"' in text


def test_visual_proof_requests_require_safe_launch_pins():
    payload = {
        "stage_id": "launcher",
        "target": "mission_control",
        "proof_requirement": "visible state parity",
        "mcp_server": "launcher_qa",
        "required_launch_pins": {"hermes_profile": "alice", "runtime_root_id": "tony-runtime", "expected_instance": "active"},
        "qa_review": qa_review(),
    }
    validate_planning_decision(decision(DecisionType.REQUEST_SCREENSHOT, payload))

    bad = dict(payload)
    bad["required_launch_pins"] = {"hermes_profile": "alice", "harness_runtime_root": "X:/Eternia/.hermes/agent-runtime", "runtime_root_id": "bad"}
    with pytest.raises(DecisionPayloadInvalid):
        validate_planning_decision(decision(DecisionType.REQUEST_SCREENSHOT, bad))


def test_reserved_handoff_modes_validate_as_known_modes():
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {
                "objective": "ship",
                "acceptance_criteria": ["proved"],
                "handoff_packet": handoff_packet(handoff_mode="parallel_specialists"),
            },
        )
    )


def test_persona_message_reply_contract_accepts_conversational_payload():
    validate_planning_decision(
        decision(
            DecisionType.PERSONA_MESSAGE_REPLY,
            {
                "reply": "Hi Tony. I can continue from this persona chat.",
                "persona_instance_id": "personainst_dev",
                "session_id": "persona_chat_personainst_dev",
            },
        )
    )


def test_handoff_backend_focused_self_test_alias_adapted():
    """Live 2026-07-03: neko keyed the self-test as `focused_self_test`; every
    known self-test key must get the venv-interpreter adaptation."""
    packet = handoff_packet()
    packet["proof_gate"]["focused_self_test"] = "python manage.py check"
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )
    assert packet["proof_gate"]["focused_self_test"] == (
        ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"
    )


def test_handoff_proof_gate_defaults_required_and_visual_required():
    """Live 2026-07-03 (task_1b102976): neko omitted proof_gate.required on a
    fresh_scope handoff and the retryable=false contract failure killed the
    goal driver. Derivable booleans must default (with an operator note), not
    hard-fail — same normalization qa_coordination_release already gets."""
    packet = handoff_packet()
    packet["proof_gate"].pop("required")
    packet["proof_gate"].pop("visual_required")
    validate_planning_decision(
        decision(
            DecisionType.PROPOSE_ACCEPTANCE,
            {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
        )
    )
    assert packet["proof_gate"]["required"] is True
    assert packet["proof_gate"]["visual_required"] is False
    note = str(packet.get("operator_note") or "")
    assert "proof_gate.required defaulted" in note


def test_handoff_proof_gate_without_any_proof_expectation_still_requires_required():
    packet = handoff_packet()
    packet["proof_gate"] = {"minimum_status": "passed"}
    with pytest.raises(DecisionPayloadInvalid):
        validate_planning_decision(
            decision(
                DecisionType.PROPOSE_ACCEPTANCE,
                {"objective": "ship", "acceptance_criteria": ["proved"], "handoff_packet": packet},
            )
        )
