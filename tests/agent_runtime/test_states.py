from agent_runtime.states import RunState, StageStatus, TaskState


def test_task_state_values_are_workflow_positional():
    assert TaskState.CREATED == "created"
    assert TaskState.RUNNING == "running"
    assert TaskState.DONE == "done"


def test_legacy_task_state_values_deserialize_to_running():
    assert TaskState("pm_triage") is TaskState.RUNNING
    assert TaskState("dev_implementing") is TaskState.RUNNING
    assert TaskState("qa_approved") is TaskState.RUNNING


def test_agent_run_and_stage_states_have_expected_values():
    assert RunState.WAITING_ON_APPROVAL == "waiting_on_approval"
    assert StageStatus.REWORK == "needs_fixes"
