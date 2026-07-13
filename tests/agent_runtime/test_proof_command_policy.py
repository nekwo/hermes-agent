from __future__ import annotations

import pytest

from hermes_time import now
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.models import Task
from agent_runtime.proof_command_policy import narrow_launcher_contract_analyze_command, validate_request_test_run_policy
from agent_runtime.states import TaskState


def task(**overrides) -> Task:
    data = {
        "id": "task_policy",
        "title": "Stage 46 real-token backend smoke",
        "description": "No product edits; perform an observational smoke proof only.",
        "state": TaskState.RUNNING,
        "created_at": now(),
        "updated_at": now(),
        "requested_by": "test",
        "acceptance_criteria": ["Backend Dev requests one bounded proof command."],
        "non_goals": ["Do not run a full suite unless explicitly required."],
        "risk_flags": ["real_token_smoke", "no_product_edits"],
    }
    data.update(overrides)
    return Task(**data)


def decision(command: str) -> AgentDecision:
    return AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Request smoke proof",
        rationale="Bounded smoke evidence only",
        payload={"stage_id": "stage_smoke", "commands": [command]},
    )


def launcher_contract_decision(command: str) -> AgentDecision:
    return AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Request Launcher contract smoke proof",
        rationale="Consume the joined backend proof packet before QA.",
        payload={
            "stage_id": "launcher_contract_smoke",
            "proof_intent": "Validate backend proof packet consumption.",
            "commands": [command],
        },
    )


def mission_control_stagec_task() -> Task:
    return task(
        title="Mission Control Stage C visual proof",
        description="Use Stage C MCP screenshot proof for Mission Control against Tony's runtime root.",
        acceptance_criteria=["Fullscreen Mission Control screenshot proves the live runtime root/profile."],
        risk_flags=["requires_visual_proof", "stagec_mcp"],
    )


def mission_control_visual_decision(command: str) -> AgentDecision:
    return AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="Request Mission Control Stage C screenshot proof",
        rationale="Pinned MCP visual proof is required before QA.",
        payload={"stage_id": "stage_launcher_analyze_and_visual_proof", "commands": [command]},
    )


def test_smoke_policy_rejects_unbounded_backend_full_suite():
    with pytest.raises(DecisionPayloadInvalid, match="Smoke/no-edit proof command policy"):
        validate_request_test_run_policy(task(), decision("python manage.py test --noinput"))


def test_smoke_policy_rejects_venv_python_backend_full_suite():
    command = '".EterniaBackendVirtualEnv/Scripts/python.exe" manage.py test --noinput'

    with pytest.raises(DecisionPayloadInvalid, match="Smoke/no-edit proof command policy"):
        validate_request_test_run_policy(task(), decision(command))


def test_smoke_policy_rejects_shell_chained_backend_full_suite():
    command = "cd '/x/Unreal Engine/Engine/EterniaBackend/eternia-backend' && . .EterniaBackendVirtualEnv/Scripts/activate && python -V && python manage.py test --noinput"

    with pytest.raises(DecisionPayloadInvalid, match="Smoke/no-edit proof command policy"):
        validate_request_test_run_policy(task(), decision(command))


def test_smoke_policy_rejects_unbounded_pytest_and_flutter_full_suite():
    for command in ("pytest -q", "python -m pytest --quiet", "flutter test"):
        with pytest.raises(DecisionPayloadInvalid, match="Smoke/no-edit proof command policy"):
            validate_request_test_run_policy(task(), decision(command))


def test_smoke_policy_allows_targeted_backend_command():
    validate_request_test_run_policy(task(), decision(".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"))
    validate_request_test_run_policy(task(), decision("python manage.py check --deploy"))
    validate_request_test_run_policy(task(), decision("python manage.py test apps.mission_control.tests.test_contract --noinput"))
    validate_request_test_run_policy(task(), decision("python -m pytest tests/agent_runtime/test_ticker.py -q"))
    validate_request_test_run_policy(task(), decision("flutter test test/mission_control_archive_test.dart"))


def test_explicit_full_suite_gate_allows_full_suite_command():
    t = task(description="Full-suite proof is explicitly required for this stage; full-suite gate.")

    validate_request_test_run_policy(t, decision("python manage.py test --noinput"))


def test_non_goal_path_rejects_matching_proof_command():
    t = task(
        description="Harness docs only.",
        non_goals=["Do not rerun the broad tests/agent_runtime suite."],
    )

    with pytest.raises(DecisionPayloadInvalid, match="bundle non-goal"):
        validate_request_test_run_policy(t, decision("python -m pytest tests/agent_runtime -q"))

    validate_request_test_run_policy(t, decision("python -m pytest tests/agent_runtime/test_stream.py -q"))


def test_launcher_contract_policy_rejects_generic_flutter_readiness_proof():
    t = task(
        title="Stage 47 Launcher contract smoke",
        description="Launcher must consume the joined backend proof packet before QA.",
    )

    with pytest.raises(DecisionPayloadInvalid, match="Launcher contract proof policy"):
        validate_request_test_run_policy(t, launcher_contract_decision("flutter --version"))


def test_launcher_contract_policy_allows_deterministic_contract_consumption_proof():
    t = task(
        title="Stage 47 Launcher contract smoke",
        description="Launcher must consume the joined backend proof packet before QA.",
    )

    validate_request_test_run_policy(
        t,
        launcher_contract_decision(
            "python -c \"backend_proof='proof_backend_123'; contract_packet='packet_backend_stage47_contract_v1'; "
            "assert backend_proof and contract_packet; print('contract_packet_consumed')\""
        ),
    )


def test_launcher_contract_policy_narrows_trailing_main_analyze_after_contract_signal():
    t = task(
        title="Stage 47 Launcher contract smoke",
        description="Launcher must consume the joined backend proof packet before QA.",
    )
    command = (
        "python -c \"contract_packet='packet_backend_stage47_contract_v1'; print('contract_packet_consumed')\" "
        "&& flutter analyze lib/main.dart"
    )

    narrowed = narrow_launcher_contract_analyze_command(t, command, stage_id="launcher_contract_smoke")

    assert narrowed.endswith("print('contract_packet_consumed')\"")
    assert "flutter analyze lib/main.dart" not in narrowed


def test_launcher_contract_policy_keeps_main_analyze_when_bootstrap_is_in_scope():
    t = task(
        title="Stage 47 Launcher contract smoke",
        description="Launcher must consume the joined backend proof packet before QA; lib/main.dart changed.",
    )
    command = (
        "python -c \"contract_packet='packet_backend_stage47_contract_v1'; print('contract_packet_consumed')\" "
        "&& flutter analyze lib/main.dart"
    )

    assert narrow_launcher_contract_analyze_command(t, command, stage_id="launcher_contract_smoke") == command


def test_stagec_screenshot_window_rejects_composer_retry_args_on_primitive():
    command = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 "
        "-Tool mcp_launcher_qa_screenshot_window "
        "-ArgsJson '{\"window_title_prefix\":\"Eternia Launcher\",\"label\":\"mc_retry\","
        "\"screenshot_stabilize_ms\":4000,\"screenshot_max_retries\":5}'"
    )

    with pytest.raises(DecisionPayloadInvalid, match="screenshot_window proof command policy"):
        validate_request_test_run_policy(task(), decision(command))


def test_stagec_screenshot_window_allows_primitive_retry_args_after_composed_open_tab_args():
    command = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 "
        "-Tool mcp_launcher_qa_open_app_tab "
        "-ArgsJson '{\"tab\":\"missionControl\",\"browser_login\":true,\"credential_profile\":\"stagec-smoke\","
        "\"screenshot\":true,\"screenshot_stabilize_ms\":4000,\"screenshot_max_retries\":8,"
        "\"hermes_profile\":\"alice\",\"harness_runtime_root\":\"X:/Eternia/.hermes/agent-runtime\","
        "\"hermes_home\":\"X:/Eternia/.hermes/profiles/alice\"}' "
        "&& powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 "
        "-Tool mcp_launcher_qa_screenshot_window "
        "-ArgsJson '{\"window_title_prefix\":\"Eternia Launcher\",\"label\":\"mc_retry\","
        "\"max_retries\":8,\"retry_delay_ms\":750}'"
    )

    validate_request_test_run_policy(mission_control_stagec_task(), mission_control_visual_decision(command))


def test_mission_control_stagec_policy_rejects_unpinned_open_tab():
    command = (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -File docs/stages/qa-reboot/scripts/Invoke-LauncherQaMcpTool.ps1 "
        "-Tool mcp_launcher_qa_open_app_tab "
        "-ArgsJson '{\"tab\":\"missionControl\",\"browser_login\":true,\"credential_profile\":\"stagec-smoke\","
        "\"screenshot\":false,\"reap_stale\":true}'"
    )

    with pytest.raises(DecisionPayloadInvalid, match="must pin Tony's Harness runtime"):
        validate_request_test_run_policy(mission_control_stagec_task(), mission_control_visual_decision(command))
