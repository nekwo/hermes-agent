from hermes_time import now

from agent_runtime.budget_approval import budget_incident_can_continue, budget_incident_needs_scope_recovery
from agent_runtime.incidents import RUN_BUDGET_EXCEEDED
from agent_runtime.models import AgentRun, Incident
from agent_runtime.states import RunState
from agent_runtime.store import RunStore


def _seed_run(store: RunStore, *, run_id: str, task_id: str) -> AgentRun:
    """Persist a run row without ``RunStore.open_run``.

    S17 removed ``open_run`` as write-dead (no production caller survived the
    mission lane). ``update`` is the surviving write path; these tests cover the
    budget-approval predicates, not the writer. ``session_id`` is a safe token
    because ``budget_incident_can_continue`` requires a reusable session.
    """

    ts = now()
    run = AgentRun(
        id=run_id,
        persona_id="dev",
        task_id=task_id,
        stage_id="launcher_implementation",
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
        session_id="safe_session",
    )
    assert store.update(run) is True
    return run



def test_budget_incident_with_read_search_loop_without_delivery_is_not_eligible(isolate_agent_runtime_root):
    runs = RunStore()
    run = _seed_run(runs, run_id="run_loop", task_id="task_loop")
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
    run = _seed_run(runs, run_id="run_patch", task_id="task_patch")
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
