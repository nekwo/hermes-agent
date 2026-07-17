from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event, Incident, Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.snapshot import build_snapshot
from agent_runtime.states import TaskState
from agent_runtime.store import IncidentStore, ProofStore, TaskStore


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
    proofs = ProofStore(event_log=events)
    incidents = IncidentStore(event_log=events)
    task = make_task()
    tasks.create(task)
    events.append(Event(now(), "run.opened", task.id, "run_dev", "dev", {"tick_id": "tick_1"}))
    proofs.attach(
        Proof(
            id="proof_cmd",
            task_id=task.id,
            stage_id="stage_1",
            type=ProofType.TEST_RUN,
            title="Command proof: smoke",
            path_or_value="proofs/task_visibility/artifacts/proof_cmd.log",
            created_by="harness",
            created_at=now(),
            metadata={"status": "passed", "exit_code": 0, "duration_ms": 15, "command": "printf ok"},
            redaction_status="safe",
        )
    )
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

    snap = build_snapshot(task_store=tasks, proof_store=proofs, incident_store=incidents, event_log=events)
    summary = next(item for item in list(snap["goals"].values()) if item["task_id"] == task.id)

    assert summary["why_not_done"][0]["kind"] == "open_incident"
    assert summary["next_action"]["action"] == "blocked_by_incident"
    assert summary["proof_summaries"] == [
        {
            "proof_id": "proof_cmd",
            "type": "test_run",
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 15,
            "created_by": "harness",
            "has_artifact": True,
        }
    ]
    assert [item["type"] for item in summary["timeline"]][-3:] == ["run.opened", "proof.attached", "incident.opened"]
    assert "needs QA verdict" not in str(summary)
    assert "proofs/task_visibility" not in str(summary)
