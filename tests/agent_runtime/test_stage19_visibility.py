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
    snap = build_snapshot()
    for key in ("goals", "runs", "proofs", "incidents", "stage_verification"):
        assert key not in snap
    assert snap["parity"]["contract_version"] == 49