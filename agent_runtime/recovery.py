from __future__ import annotations

import uuid

from hermes_time import now

from .models import Incident
from .states import RunState
from .store import IncidentStore, RunStore


def mark_stale_runs(run_store: RunStore, incident_store: IncidentStore, *, heartbeat_ttl_seconds: int) -> list[str]:
    opened=[]
    for run in run_store.find_stale(heartbeat_ttl_seconds=heartbeat_ttl_seconds):
        run.state = RunState.STALE
        run.finished_at = now()
        run_store.update(run)
        existing=[i for i in incident_store.list_open() if i.run_id == run.id and i.kind == "stale_run"]
        if existing:
            continue
        inc=Incident(id=f"inc_{uuid.uuid4().hex[:8]}", task_id=run.task_id, run_id=run.id, kind="stale_run", summary="Run heartbeat exceeded TTL", detail_path=None, opened_at=now())
        incident_store.open(inc)
        opened.append(inc.id)
    return opened
