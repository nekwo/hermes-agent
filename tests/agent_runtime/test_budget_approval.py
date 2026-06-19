from hermes_time import now

from agent_runtime.budget_approval import budget_incident_can_continue, budget_incident_needs_scope_recovery
from agent_runtime.incidents import RUN_BUDGET_EXCEEDED
from agent_runtime.models import Incident
from agent_runtime.store import RunStore


def test_budget_incident_with_read_search_loop_without_delivery_is_not_eligible(isolate_agent_runtime_root):
    runs = RunStore()
    run = runs.open_run("dev", "task_loop", stage_id="launcher_implementation", session_id="safe_session")
    run.progress = {
        "loop_warning": "read_search_without_patch_threshold",
        "read_search_count": 6,
        "read_search_limit": 6,
        "patch_count": 0,
        "proof_count": 0,
    }
    runs.update(run)
    runs.close_run(run.id, state="waiting_on_approval", error={"type": RUN_BUDGET_EXCEEDED})
    incident = Incident(
        id="inc_loop",
        task_id="task_loop",
        run_id=run.id,
        kind=RUN_BUDGET_EXCEEDED,
        summary="live run budget exceeded",
        detail_path=None,
        opened_at=now(),
    )

    assert budget_incident_can_continue(incident, runs) is False
    assert budget_incident_needs_scope_recovery(incident, runs) is True


def test_budget_incident_with_patch_progress_remains_eligible(isolate_agent_runtime_root):
    runs = RunStore()
    run = runs.open_run("dev", "task_patch", stage_id="launcher_implementation", session_id="safe_session")
    run.progress = {
        "loop_warning": "read_search_without_patch_threshold",
        "read_search_count": 6,
        "read_search_limit": 6,
        "patch_count": 1,
        "proof_count": 0,
    }
    runs.update(run)
    runs.close_run(run.id, state="waiting_on_approval", error={"type": RUN_BUDGET_EXCEEDED})
    incident = Incident(
        id="inc_patch",
        task_id="task_patch",
        run_id=run.id,
        kind=RUN_BUDGET_EXCEEDED,
        summary="live run budget exceeded",
        detail_path=None,
        opened_at=now(),
    )

    assert budget_incident_can_continue(incident, runs) is True
    assert budget_incident_needs_scope_recovery(incident, runs) is False
