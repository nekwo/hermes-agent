from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event, Incident
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, TaskStore


def make_task(state=TaskState.RUNNING):
    ts = now()
    return Task(
        id="task_visibility",
        title="Visibility smoke",
        description="show the operator why this is not done",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        current_stage_id="stage_1",
        proof_ids=["proof_cmd"],
    )


def test_snapshot_exposes_goal_timeline_proof_summaries_and_why_not_done():
    events = EventLog()
    tasks = TaskStore(event_log=events)
    incidents = IncidentStore(event_log=events)
    task = make_task()
    tasks.create(task)
    events.append(Event(now(), "run.opened", task.id, "run_dev", "dev", {"tick_id": "tick_1"}))
    incidents.open(
        Incident(
            id="inc_waiting_qa",
            task_id=task.id,
            run_id=None,
            kind="qa_intervention_required",
            summary="needs QA verdict",
            detail_path=None,
            opened_at=now(),
        )
    )

    snap = build_snapshot(task_store=tasks, incident_store=incidents, event_log=events)
    summary = next(item for item in list(snap["goals"].values()) if item["task_id"] == task.id)

    assert summary["why_not_done"][0]["kind"] == "open_incident"
    assert summary["next_action"]["action"] == "blocked_by_incident"
    # S8: proof_summaries / timeline are GOAL_DETAIL_ONLY_FIELDS — evicted from
    # the frame HEAD behind ``detail_ref`` and served by ``harness goal detail``.
    # The head must not carry them; the on-demand detail rebuild must.
    assert "proof_summaries" not in summary
    assert "timeline" not in summary
    assert summary["detail_ref"]["evicted"] is True
    from agent_runtime.snapshot import goal_detail_for_task

    detail = goal_detail_for_task(task.id, event_log=events)
    assert detail is not None
    assert detail["proof_summaries"] == []
    assert [item["type"] for item in detail["timeline"]][-2:] == ["run.opened", "incident.opened"]
    assert "needs QA verdict" not in str(summary)
    assert "proofs/task_visibility" not in str(summary)
