"""Gap-4 regression tests: Neko adjudication closes terminal-run incidents.

An open incident whose underlying run is already terminal (cancelled, failed,
hung-reaped) is the adjudication turn's to close via resolve_incident. A Neko
``block`` on such an incident gets repair feedback instead of parking the goal
on an operator; the HUD recommends resolve_incident in ANY task state; the
incident context carries the terminal-run hint.
"""

import pytest
from hermes_time import now

from agent_runtime.context_builder import _incident_records, _next_required_move
from agent_runtime.decision_schema import AgentDecision, DecisionPayloadInvalid, DecisionType
from agent_runtime.models import Incident, Task
from agent_runtime.planning import apply_planning_decision
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, RunStore


def make_task(state=TaskState.RUNNING):
    ts = now()
    return Task(id="task_adj", title="T", description="adjudication test", state=state, created_at=ts, updated_at=ts, requested_by="tony")


def open_incident_with_run(*, cancel_run: bool):
    runs = RunStore()
    run = runs.open_run("dev", "task_adj", stage_id="implement", session_id="session_adj")
    if cancel_run:
        runs.cancel(run.id, reason="liveness reaped hung run")
    incidents = IncidentStore()
    incident = incidents.open(
        Incident(
            id="inc_adj_1",
            task_id="task_adj",
            run_id=run.id,
            kind="run_hung",
            summary="run exceeded hung threshold",
            detail_path=None,
            opened_at=now(),
        )
    )
    return incident, run


def block_decision():
    return AgentDecision(
        type=DecisionType.BLOCK,
        summary="blocked on open incident",
        rationale="r",
        payload={
            "reason": "open incident needs an operator",
            "log_ref": {"path": "events.jsonl", "line": 1, "summary": "incident opened"},
        },
    )


def test_neko_block_on_terminal_run_incident_gets_repair_feedback(isolate_agent_runtime_root):
    task = make_task()
    incident, _run = open_incident_with_run(cancel_run=True)
    task.open_incident_ids = [incident.id]

    with pytest.raises(DecisionPayloadInvalid) as exc:
        apply_planning_decision(task, block_decision(), actor="neko_supervisor", incident_store=IncidentStore())

    message = str(exc.value)
    assert "resolve_incident" in message
    assert incident.id in message


def _blocked_escalations(task):
    stack = (task.harness_self_heal or {}).get("evidence_stack") or []
    return [item for item in stack if isinstance(item, dict) and item.get("kind") == "blocked_escalation"]


def test_neko_block_still_valid_when_incident_run_is_active(isolate_agent_runtime_root):
    task = make_task()
    incident, _run = open_incident_with_run(cancel_run=False)
    task.open_incident_ids = [incident.id]

    apply_planning_decision(task, block_decision(), actor="neko_supervisor", incident_store=IncidentStore())

    assert _blocked_escalations(task)


def test_dev_block_is_not_rejected_for_terminal_run_incident(isolate_agent_runtime_root):
    task = make_task()
    incident, _run = open_incident_with_run(cancel_run=True)
    task.open_incident_ids = [incident.id]

    apply_planning_decision(task, block_decision(), actor="dev", incident_store=IncidentStore())

    assert _blocked_escalations(task)


def test_resolve_incident_closes_terminal_run_incident(isolate_agent_runtime_root):
    task = make_task()
    incident, _run = open_incident_with_run(cancel_run=True)
    task.open_incident_ids = [incident.id]

    decision = AgentDecision(
        type=DecisionType.RESOLVE_INCIDENT,
        summary="close reaped incident",
        rationale="r",
        payload={"incident_id": incident.id, "resolution": "underlying run already cancelled by liveness; no recovery needed"},
    )
    apply_planning_decision(task, decision, actor="neko_supervisor", incident_store=IncidentStore())

    assert task.open_incident_ids == []
    closed = IncidentStore().get(incident.id)
    assert closed.closed_at is not None


def test_incident_records_carry_terminal_run_hint(isolate_agent_runtime_root):
    task = make_task()
    incident, run = open_incident_with_run(cancel_run=True)
    task.open_incident_ids = [incident.id]

    records = _incident_records(task, incident_store=IncidentStore())

    assert len(records) == 1
    assert records[0]["underlying_run_terminal"] is True
    assert records[0]["underlying_run_state"] == "cancelled"
    assert "resolve_incident" in records[0]["resolution_hint"]


def test_incident_records_no_hint_for_active_run(isolate_agent_runtime_root):
    task = make_task()
    incident, _run = open_incident_with_run(cancel_run=False)
    task.open_incident_ids = [incident.id]

    records = _incident_records(task, incident_store=IncidentStore())

    assert len(records) == 1
    assert "underlying_run_terminal" not in records[0]


def test_hud_recommends_resolve_incident_for_running_task_with_incidents(isolate_agent_runtime_root):
    task = make_task(TaskState.RUNNING)
    incident, _run = open_incident_with_run(cancel_run=True)
    task.open_incident_ids = [incident.id]
    neko_run = RunStore().open_run("neko_supervisor", task.id, stage_id="scope", session_id="session_neko")

    move = _next_required_move(task, neko_run, handoff={}, stage_state={})

    assert move["decision_type"] == "resolve_incident"
    assert move["incident_ids"] == [incident.id]
