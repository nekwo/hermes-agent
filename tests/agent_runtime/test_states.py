from agent_runtime.states import AgentState, RunState, StageStatus, TaskState


def test_task_state_values_are_workflow_positional():
    assert TaskState.CREATED == "created"
    assert TaskState.PM_TRIAGE == "pm_triage"
    assert TaskState.DEV_IMPLEMENTING == "dev_implementing"
    assert TaskState.VERIFIED == "qa_approved"
    assert TaskState.DONE == "done"


def test_agent_run_and_stage_states_have_expected_values():
    assert AgentState.CAPTURING_PROOF == "capturing_proof"
    assert AgentState.CRASHED == "crashed"
    assert RunState.WAITING_ON_APPROVAL == "waiting_on_approval"
    assert StageStatus.NEEDS_FIXES == "needs_fixes"
