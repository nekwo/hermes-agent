from datetime import timedelta
from hermes_time import now
from agent_runtime.recovery import mark_stale_runs
from agent_runtime.store import IncidentStore, RunStore


def test_stale_run_becomes_incident_once():
    rs=RunStore(); ins=IncidentStore(); run=rs.open_run("dev", "task_1"); run.last_heartbeat_at=now()-timedelta(seconds=100); rs.update(run)
    first=mark_stale_runs(rs, ins, heartbeat_ttl_seconds=1)
    second=mark_stale_runs(rs, ins, heartbeat_ttl_seconds=1)
    assert len(first)==1 and second==[]
    assert len(ins.list_open())==1
