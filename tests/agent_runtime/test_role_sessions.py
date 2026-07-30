from __future__ import annotations

from hermes_time import now

from agent_runtime.decision_schema import DecisionType
from agent_runtime.models import AgentRun
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.role_sessions import (
    CLOSE_BLOCKED,
    CLOSE_COMPLETED,
    CLOSE_HANDOFF,
    CLOSE_INVALID,
    CLOSE_WATCHDOG,
    CONTINUE_SAME_RUN,
    RoleSessionEnvelope,
    role_session_progress,
    should_continue_role_session,
    update_envelope_after_invocation,
)
from agent_runtime.runtime_config import ContinuousRoleSessionConfig
from agent_runtime.states import RunState, TaskState


def _task(*, state=TaskState.RUNNING, stage_id="stage_1"):
    ts = now()
    return Task(
        id="task_1",
        title="T",
        description="D",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
        current_stage_id=stage_id,
    )


def _run(*, session_id="session-1"):
    ts = now()
    return AgentRun(
        id="run_1",
        persona_id="dev",
        task_id="task_1",
        stage_id="stage_1",
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        session_id=session_id,
    )


def _envelope(**overrides):
    data = {
        "task_id": "task_1",
        "persona_id": "dev",
        "stage_id": "stage_1",
        "opened_run_id": "run_1",
        "decision_count": 1,
    }
    data.update(overrides)
    return RoleSessionEnvelope(**data)


def _decision(**overrides):
    before = _task()
    after = _task()
    data = {
        "config": ContinuousRoleSessionConfig(enabled=True, observe_only=False),
        "before_task": before,
        "after_task": after,
        "persona_id": "dev",
        "run": _run(),
        "decision_type": DecisionType.CORRECT_STAGE.value,
        "envelope": _envelope(),
        "next_action_type": "run_dev",
        "next_persona_id": "dev",
    }
    data.update(overrides)
    return should_continue_role_session(**data)


def test_role_session_continues_same_owner_same_stage():
    result = _decision()

    assert result.action == CONTINUE_SAME_RUN
    assert result.close_reason == "same_owner_same_stage"


def test_role_session_closes_on_owner_change_and_qa_verdict():
    assert _decision(next_action_type="run_qa", next_persona_id="qa").action == CLOSE_HANDOFF
    assert _decision(decision_type=DecisionType.REPORT_QA_VERDICT.value).action == CLOSE_COMPLETED


def test_role_session_closes_on_open_incident_and_failed_proof():
    assert _decision(open_incident_count=1).action == CLOSE_BLOCKED
    assert _decision(proof_ids_added=["proof_1"], proof_statuses=["failed"]).action == CLOSE_BLOCKED


def test_role_session_allows_passing_proof_only_when_owner_still_matches():
    assert _decision(proof_ids_added=["proof_1"], proof_statuses=["passed"]).action == CONTINUE_SAME_RUN
    assert _decision(proof_ids_added=["proof_1"], proof_statuses=["passed"], next_persona_id="qa", next_action_type="run_qa").action == CLOSE_HANDOFF


def test_role_session_closes_on_caps_and_live_session_safety():
    assert _decision(envelope=_envelope(decision_count=4)).action == CLOSE_WATCHDOG
    assert _decision(envelope=_envelope(continuation_count=3)).action == CLOSE_WATCHDOG
    assert _decision(run=_run(session_id=None), is_live_runtime=True).action == CLOSE_INVALID


def test_role_session_counts_loop_warning_as_watchdog_warning():
    envelope = _envelope()
    run = _run()
    run.llm = {"api_calls": 2, "total_tokens": 100, "tool_turns": 1}
    run.progress = {"loop_warning": "read_search_without_patch_threshold"}

    update_envelope_after_invocation(envelope, run, proof_ids_added=[])

    assert envelope.watchdog_warnings == 1
    assert role_session_progress(envelope)["watchdog_warnings"] == 1
    assert _decision(run=run).action == CLOSE_WATCHDOG
