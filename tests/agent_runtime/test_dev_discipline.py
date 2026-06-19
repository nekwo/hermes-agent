from __future__ import annotations

import pytest

from hermes_time import now
from agent_runtime.actions import HarnessActionType
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.dev_discipline import needs_supervisor_slicing, validate_dev_progress_gate
from agent_runtime.events import EventLog
from agent_runtime.models import AgentPersona, Event, Task
from agent_runtime.progress import RunProgressSink
from agent_runtime.state_machine import MissionStateMachine
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import RunStore


def make_task(**overrides):
    ts = now()
    data = dict(
        id="task_dev_hardening",
        title="Add Backend Dev persona and swarm-ready Mission Control agent model",
        description="Upgrade Launcher Mission Control data types, Launcher UI, Backend Dev profile binding, and future large swarm support across frontend and backend repos.",
        state=TaskState.PM_READY_FOR_DEV,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        affected_repos=["EterniaLauncher", "EterniaBackend"],
        acceptance_criteria=["frontend supports specialist agents", "backend dev profile is ready", "large swarms are supported"],
    )
    data.update(overrides)
    return Task(**data)


def dev_persona():
    return AgentPersona(
        id="dev",
        display_name="Launcher Dev Agent",
        role="dev",
        model="gpt-5.5",
        provider="openai-codex",
        api_mode="codex_responses",
        toolsets=["terminal", "file", "search"],
        system_prompt_path="personas/dev/system.md",
    )


def decision(decision_type: DecisionType, payload: dict | None = None):
    return AgentDecision(
        type=decision_type,
        summary="summary",
        rationale="rationale",
        payload=payload or {},
    )


def test_broad_unsliced_specialist_mission_routes_to_neko_before_dev():
    task = make_task()

    assert needs_supervisor_slicing(task) is True
    action = MissionStateMachine().next_action(task)

    assert action.type == HarnessActionType.RUN_NEKO_SUPERVISOR
    assert "slice" in action.reason.lower()


def test_small_repo_scoped_mission_still_routes_to_dev():
    task = make_task(
        title="Fix Mission Control archive button",
        description="Make View Archive reveal archived missions in one Mission History panel.",
        affected_repos=["EterniaLauncher"],
        acceptance_criteria=["View Archive toggles history"],
    )

    assert needs_supervisor_slicing(task) is False
    assert MissionStateMachine().next_action(task).type == HarnessActionType.RUN_DEV


def test_neko_backend_first_cross_stack_slice_routes_to_backend_dev():
    task = make_task(
        title="Stage 46 post-fix cross-stack frontend-backend smoke",
        description="Backend Dev verifies the existing backend-side Mission Control/Harness contract surface or integration-fixture visibility for Stage 46 without product edits, then returns a compact backend contract/proof packet for Neko join-gate review.",
        affected_repos=["EterniaBackend", "hermes-agent"],
        acceptance_criteria=[
            "Handoff mode remains sequential_specialists: Backend Dev first; Launcher Dev is released only after backend proof IDs and backend contract/proof packet exist; QA is released only after both backend and Launcher proof sets exist.",
            "Backend Dev operates only within EterniaBackend/hermes-agent Mission Control/Harness contract or integration-fixture visibility scope.",
            "Launcher Dev is not released by this decision; Neko must perform the join gate after the backend proof packet exists.",
        ],
        non_goals=[
            "No Launcher bridge/UI verification in this first specialist slice.",
            "No QA coordination until backend and Launcher proof IDs both exist.",
        ],
        risk_flags=["cross_stack_contract_handoff", "sequential_specialist_handoff"],
    )

    assert needs_supervisor_slicing(task) is False
    assert MissionStateMachine().next_action(task).type == HarnessActionType.RUN_DEV


def test_recorded_backend_first_handoff_packet_routes_broad_task_to_dev():
    task = make_task(risk_flags=["cross_stack_routing"])
    EventLog().append(
        Event(
            ts=now(),
            type="packet.recorded",
            task_id=task.id,
            run_id="run_neko",
            persona_id="neko_supervisor",
            payload={
                "packet_id": "packet_handoff_backend_first",
                "packet_type": "handoff_packet",
                "body": {
                    "packet_kind": "fresh_scope",
                    "mission_phase": "initial_scope",
                    "handoff_mode": "backend_first_cross_stack",
                    "target_owner": "backend_dev",
                    "target_repo": "EterniaBackend",
                    "next_owner": "dev",
                    "next_repo": "EterniaLauncher",
                    "proof_gate": {"required": True, "required_proof_types": ["test_run"], "minimum_status": "passed", "visual_required": False},
                    "join_gate": {"release_condition": "backend proof before launcher"},
                },
            },
        )
    )

    assert needs_supervisor_slicing(task) is False
    assert MissionStateMachine().next_action(task).type == HarnessActionType.RUN_DEV


def test_progress_sink_aggregates_tool_loop_patch_test_and_proof_telemetry():
    runs = RunStore()
    run = runs.open_run("dev", "task_progress")
    sink = RunProgressSink(run_store=runs, run_id=run.id)

    for _ in range(4):
        sink.emit("run.tool.finished", {"type": "run.tool.finished", "phase": "tool", "step": "tool_finished", "tool_name": "read_file", "status": "passed"})
    sink.emit("run.tool.finished", {"type": "run.tool.finished", "phase": "dev_work", "step": "patch", "tool_name": "patch", "status": "passed", "changed_files": ["foo.py"]})
    sink.emit("run.tool.finished", {"type": "run.tool.finished", "phase": "tool", "step": "tool_finished", "tool_name": "terminal", "status": "passed", "summary": "pytest tests/foo.py passed"})
    sink.emit("proof.attached", {"type": "proof.attached", "phase": "proof", "step": "command_proof", "status": "passed", "proof_id": "proof_1"})

    progress = runs.get(run.id).progress or {}

    assert progress["tool_call_count"] == 6
    assert progress["read_search_count"] == 4
    assert progress["patch_count"] == 1
    assert progress["test_count"] == 1
    assert progress["has_patch_progress"] is True
    assert progress["has_test_progress"] is True
    assert progress["has_proof_progress"] is True
    assert progress["loop_warning"] == "read_search_without_patch_threshold"


def test_progress_sink_preserves_autonomy_and_self_heal_fields_across_tool_events():
    runs = RunStore()
    run = runs.open_run("dev", "task_progress_autonomy")
    sink = RunProgressSink(run_store=runs, run_id=run.id)

    sink.emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "autonomy",
            "step": "autonomy_packet",
            "status": "ready",
            "autonomy_packet_id": "auto_1",
            "context_receipt_id": "ctxr_1",
            "read_search_limit": 3,
            "skill_load_limit": 1,
            "last_failed_proof_ids": ["proof_failed"],
            "environment_fingerprint_status": "unchanged",
        },
    )
    sink.emit("run.tool.finished", {"type": "run.tool.finished", "phase": "tool", "step": "tool_finished", "tool_name": "read_file", "status": "passed"})

    progress = runs.get(run.id).progress or {}

    assert progress["autonomy_packet_id"] == "auto_1"
    assert progress["context_receipt_id"] == "ctxr_1"
    assert progress["read_search_limit"] == 3
    assert progress["skill_load_limit"] == 1
    assert progress["last_failed_proof_ids"] == ["proof_failed"]
    assert progress["read_search_count"] == 1


def test_progress_sink_uses_autonomy_read_search_limit_for_loop_warning():
    runs = RunStore()
    run = runs.open_run("dev", "task_progress_budget")
    sink = RunProgressSink(run_store=runs, run_id=run.id)

    sink.emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "autonomy",
            "step": "autonomy_packet",
            "status": "ready",
            "read_search_limit": 2,
        },
    )
    sink.emit("run.tool.finished", {"type": "run.tool.finished", "phase": "tool", "step": "tool_finished", "tool_name": "search_files", "status": "passed"})
    sink.emit("run.tool.finished", {"type": "run.tool.finished", "phase": "tool", "step": "tool_finished", "tool_name": "read_file", "status": "passed"})

    progress = runs.get(run.id).progress or {}

    assert progress["read_search_count"] == 2
    assert progress["read_search_limit"] == 2
    assert progress["loop_warning"] == "read_search_without_patch_threshold"


def test_progress_sink_counts_tool_progress_pairs_once():
    runs = RunStore()
    run = runs.open_run("dev", "task_progress_pairs")
    sink = RunProgressSink(run_store=runs, run_id=run.id)

    sink.emit("run.progress", {"type": "run.progress", "phase": "tool", "step": "tool_started", "tool_name": "read_file", "status": "started"})
    sink.emit("run.tool.started", {"type": "run.tool.started", "phase": "tool", "step": "tool_started", "tool_name": "read_file", "status": "started"})
    sink.emit("run.progress", {"type": "run.progress", "phase": "tool", "step": "tool_finished", "tool_name": "read_file", "status": "passed"})
    sink.emit("run.tool.finished", {"type": "run.tool.finished", "phase": "tool", "step": "tool_finished", "tool_name": "read_file", "status": "passed"})

    progress = runs.get(run.id).progress or {}

    assert progress["tool_call_count"] == 1
    assert progress["read_search_count"] == 1


def test_dev_progress_gate_rejects_high_call_patch_handoff_without_progress():
    runs = RunStore()
    run = runs.open_run("dev", "task_gate", max_api_calls=6)
    run.llm = {"api_calls": 6, "total_tokens": 1000}
    runs.update(run)

    with pytest.raises(DecisionPayloadInvalid, match="early progress"):
        validate_dev_progress_gate(
            dev_persona(),
            runs.get(run.id),
            decision(DecisionType.PROPOSE_PATCH, {"proof_ids": ["proof_existing"]}),
        )


def test_dev_progress_gate_rejects_budget_pressure_non_proof_handoff_even_with_test_activity():
    runs = RunStore()
    run = runs.open_run("dev", "task_budget_pressure", max_api_calls=12, max_total_tokens=100)
    run.llm = {"api_calls": 4, "total_tokens": 81}
    run.progress = {
        "step": "budget_pressure",
        "budget_kind": "total_tokens",
        "budget_ratio": 0.81,
        "has_test_progress": True,
        "test_count": 2,
        "proof_count": 0,
        "tool_call_count": 8,
    }
    runs.update(run)

    with pytest.raises(DecisionPayloadInvalid, match="budget pressure"):
        validate_dev_progress_gate(
            dev_persona(),
            runs.get(run.id),
            decision(DecisionType.PROPOSE_PATCH, {"summary": "tests looked good but no proof handoff"}),
        )


def test_dev_progress_gate_allows_stage_split_test_request_block_or_patch_progress():
    runs = RunStore()
    for dtype, payload in [
        (DecisionType.PROPOSE_STAGE_PLAN, {"stages": []}),
        (DecisionType.REQUEST_TEST_RUN, {"stage_id": "stage_1", "commands": ["pytest tests/foo.py"]}),
        (DecisionType.BLOCK, {"reason": "missing fixture"}),
    ]:
        run = runs.open_run("dev", f"task_{dtype.value}", max_api_calls=6)
        run.llm = {"api_calls": 6, "total_tokens": 1000}
        runs.update(run)
        validate_dev_progress_gate(dev_persona(), runs.get(run.id), decision(dtype, payload))
        runs.close_run(run.id, state=RunState.COMPLETED)

    run = runs.open_run("dev", "task_patch_progress", max_api_calls=6)
    run.llm = {"api_calls": 6, "total_tokens": 1000}
    run.progress = {"has_patch_progress": True, "tool_call_count": 3}
    runs.update(run)
    validate_dev_progress_gate(
        dev_persona(),
        runs.get(run.id),
        decision(DecisionType.PROPOSE_PATCH, {"proof_ids": ["proof_existing"]}),
    )


def test_dev_progress_gate_allows_retry_after_operator_environment_change():
    runs = RunStore()
    run = runs.open_run("dev", "task_changed_preflight", max_api_calls=12)
    run.progress = {
        "last_failed_proof_ids": ["preflight_failed"],
        "environment_fingerprint_status": "changed_after_operator_patch",
    }
    runs.update(run)

    validate_dev_progress_gate(
        dev_persona(),
        runs.get(run.id),
        decision(DecisionType.REQUEST_TEST_RUN, {"stage_id": "stage_1", "commands": ["flutter analyze lib/main.dart"]}),
    )


def test_dev_progress_gate_auto_attaches_failed_proof_ids_for_retry():
    runs = RunStore()
    run = runs.open_run("dev", "task_auto_attach_failed_proof", max_api_calls=12)
    run.progress = {
        "last_failed_proof_ids": ["proof_failed"],
        "environment_fingerprint_status": "unchanged",
    }
    runs.update(run)
    retry = decision(
        DecisionType.REQUEST_TEST_RUN,
        {"stage_id": "stage_1", "commands": ["printf retry-ok\\n"]},
    )

    validate_dev_progress_gate(dev_persona(), runs.get(run.id), retry)

    assert retry.payload["failed_proof_ids"] == ["proof_failed"]
    assert retry.payload["failed_proof_auto_attached"] is True
