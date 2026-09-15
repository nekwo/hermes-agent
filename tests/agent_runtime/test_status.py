from hermes_time import now
import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")
from agent_runtime.models import AgentRun, Incident
from agent_runtime import paths
import agent_runtime.store as store_module
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.states import RunState, TaskState
from agent_runtime.status import build_status
from agent_runtime.store import IncidentStore, RunStore, TaskStore
from agent_runtime.runtime_instances import GoalRuntimeInstanceStore


def _assert_no_mission_status() -> None:
    status = build_status()
    # S21 stopped publishing `next_actions` / `undispatchable_missions`: both were
    # computed over the `tasks = []` literal, so they could only ever be `[]` —
    # a constant reported in the shape of a measurement. Absence is now the pin.
    # S28 finished that cut with `open_tasks` (and `running_runs`), which S21 had
    # to retain while `_cmd_status` still printed them; this assertion used to
    # read `status["open_tasks"] == 0` — a literal, not a measurement. See
    # tests/agent_runtime/test_s28_status_observe_shrink.py.
    assert "open_tasks" not in status
    assert "running_runs" not in status
    assert "next_actions" not in status
    assert "undispatchable_missions" not in status
    assert not hasattr(TaskStore(), "create")




def test_status_lane_only_does_not_report_background_task_ids(isolate_agent_runtime_root):
    _assert_no_mission_status()


def test_status_omits_retired_burn_in_certification_state(isolate_agent_runtime_root):
    s = build_status()

    # S56 removed the whole `swarm` row (it echoed a config block nothing
    # enforced), which supersedes the narrower burn-in/certification pin this
    # case used to carry. Absence of the row is now the assertion.
    assert "swarm" not in s


def test_status_projects_operator_channels_for_persona_instances(isolate_agent_runtime_root):
    # S56 made the persona-instance roster unconditional; the
    # `enterprise_worker_sessions` config this case used to monkeypatch in is
    # gone, and the wire block now reports the truth for every config.
    s = build_status()

    assert s["persona_instance_runtime"]["enabled"] is True
    assert s["persona_instances"]
    assert s["operator_channels"]
    assert {
        channel["persona_instance_id"] for channel in s["operator_channels"]
    }.issuperset(
        {
            instance["persona_instance_id"]
            for instance in s["persona_instances"]
            if instance["persona_instance_id"]
        }
    )
    assert "persona_chat_history" in s
    assert "persona_chat_trace" in s
    assert "included" in s["parity"]["completeness"]["persona_chat_history"]
    assert "included" in s["parity"]["completeness"]["persona_chat_trace"]


def test_status_surfaces_lanes_repo_locks_and_retired_swarm_budget_shape(isolate_agent_runtime_root):
    """S56 finished the cut this case has been tracking since S52/S53.

    It used to mint a lane and a repo-bundle lock and read the rows back; when
    those writers were deleted it degraded to asserting the empty constants
    (``lanes == []``, ``repo_locks`` with zero locks) so the S47 item-5
    empty-by-construction debt stayed visible. S56 retired the rows themselves
    -- ``lanes``, ``repo_locks``, ``swarm_budget`` and ``production_envelope``
    are no longer published at all -- so the pins invert to absence rather than
    being dropped, keeping a stale producer from resurrecting a reader.
    """

    runs = RunStore()
    # The run store is historical/read-only; seed the row directly. This case
    # covers build_status, not a retired writer.
    ts = now()
    run = AgentRun(
        id="run_lane",
        persona_id="dev",
        task_id="task_lane",
        stage_id=None,
        state=RunState.RUNNING,
        started_at=ts,
        last_heartbeat_at=ts,
    )
    store_module._write_model(paths.run_path(run.id), run)

    s = build_status(run_store=runs)

    # S56: rows retired, pins inverted (superseded-pin-inversion).
    assert "lanes" not in s
    assert "repo_locks" not in s
    assert "swarm_budget" not in s
    assert "production_envelope" not in s
    # `runtime_instances` keeps its own `lanes` sub-key -- that block is the
    # projection the top-level row duplicated, and it still ships.
    assert "lanes" in s["runtime_instances"]


def test_status_marks_next_action_blocked_by_open_incident():
    _assert_no_mission_status()








def test_status_blocks_budget_incident_after_continuation_cap():
    _assert_no_mission_status()


def test_status_routes_read_search_budget_loop_to_neko_scope_recovery(isolate_agent_runtime_root):
    _assert_no_mission_status()


def test_status_reports_retired_dispatch_lane(isolate_agent_runtime_root):
    _assert_no_mission_status()
