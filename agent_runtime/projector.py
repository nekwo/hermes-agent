from __future__ import annotations

import json
import os
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

from hermes_time import now

from .config import load_agent_runtime_config
from .events import CachedEventLog, EventLog
from .parity import events_watermark
from .persona_assignments import (
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_assignment_store_enabled,
    persona_instance_runtime_enabled,
)
from .personas import seed_personas
from .read_model import ReadModel
from .repo_bundles import RepoBundleStore
from .role_checklists import RoleChecklistStore
from .role_envelopes import RoleEnvelopeStore
from .runtime_instances import GoalRuntimeInstanceStore
from .self_test_evidence import SelfTestEvidenceStore
from .snapshot import (
    _goal_head,
    _goal_projection_from_task,
    _incident_summary,
    _run_summary,
    build_snapshot,
)
from .store import AgentStore, IncidentStore, RunStore, TaskStore, WorkspaceStore
from .worker_sessions import WorkerSessionStore


LEASE_TTL_SECONDS = 30


@dataclass(slots=True)
class ProjectorResult:
    applied_events: int = 0
    from_offset: int = 0
    to_offset: int = 0
    incremental_apply_ms: int = 0
    changed: dict[str, list[str]] = field(default_factory=dict)
    stale_sections: list[str] = field(default_factory=list)
    lease_acquired: bool = False


class Projector:
    def __init__(self, read_model: ReadModel, *, config: Any, event_log: EventLog | None = None):
        self.read_model = read_model
        self.config = config
        self.event_log = event_log or EventLog()

    def acquire_lease(self) -> bool:
        lease = {
            "pid": os.getpid(),
            "acquired_at": now(),
            "expires_at_monotonic": time.monotonic() + LEASE_TTL_SECONDS,
        }
        with closing(self.read_model.connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", ("projector_lease",)).fetchone()
            if row is not None:
                try:
                    current = json.loads(row["value"])
                except Exception:
                    current = {}
                expires = float(current.get("expires_at_monotonic") or 0)
                if expires > time.monotonic() and int(current.get("pid") or 0) != os.getpid():
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
                ("projector_lease", json.dumps(lease, default=str, sort_keys=True)),
            )
            conn.commit()
        return True

    def apply_pending(self) -> ProjectorResult:
        started = time.perf_counter()
        if not self.acquire_lease():
            return ProjectorResult(lease_acquired=False)
        watermark = self.read_model.projection_watermark("snapshot")
        if watermark is None:
            self.full_rebuild()
            elapsed = int((time.perf_counter() - started) * 1000)
            current = self.read_model.projection_watermark("snapshot") or {}
            return ProjectorResult(
                applied_events=0,
                from_offset=0,
                to_offset=int(current.get("event_offset") or 0),
                incremental_apply_ms=elapsed,
                lease_acquired=True,
            )
        from_offset = int(watermark.get("event_offset") or 0)
        events = list(self.event_log.iter_from_offset(from_offset))
        if not events:
            return ProjectorResult(
                from_offset=from_offset,
                to_offset=from_offset,
                incremental_apply_ms=int((time.perf_counter() - started) * 1000),
                lease_acquired=True,
            )
        changed = _Changed.from_events([event for _, event in events])
        to_offset = events[-1][0]
        self._apply_changed(changed, to_offset=to_offset, last_event_ts=getattr(events[-1][1], "ts", None))
        return ProjectorResult(
            applied_events=len(events),
            from_offset=from_offset,
            to_offset=to_offset,
            incremental_apply_ms=int((time.perf_counter() - started) * 1000),
            changed=changed.as_dict(),
            stale_sections=sorted(changed.stale_sections),
            lease_acquired=True,
        )

    def full_rebuild(self) -> None:
        snapshot = build_snapshot()
        self.read_model.apply_full_rebuild(snapshot, watermark=(snapshot.get("parity") or {}).get("watermark") or events_watermark())

    def _apply_changed(self, changed: "_Changed", *, to_offset: int, last_event_ts: Any) -> None:
        task_store = TaskStore(event_log=self.event_log)
        run_store = RunStore(event_log=self.event_log)
        incident_store = IncidentStore(event_log=self.event_log)
        task_ids = sorted(changed.task_ids)
        run_ids = sorted(changed.run_ids)
        proof_ids = sorted(changed.proof_ids)
        incident_ids = sorted(changed.incident_ids)
        with closing(self.read_model.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if task_ids:
                    # ONE point-in-time event view for the whole rebuild, the
                    # same reader ``build_snapshot`` projects goals from. Two
                    # readers over one log are NOT interchangeable — the cached
                    # one indexes a line once per id token it contains, so an
                    # event whose payload echoes its own ``task_id`` (``lane.*``,
                    # for one) comes back twice from ``CachedEventLog.for_task``
                    # and once from the plain ``EventLog``. Whatever that view
                    # says, both paths must say the same thing or the rows
                    # diverge for a reason that has nothing to do with the
                    # delta. It is also the cheaper reader here: the plain log
                    # re-reads the whole file per ``for_task`` call, i.e. once
                    # per changed task.
                    projection_log = CachedEventLog()
                    all_tasks = task_store.list_all()
                    runs = run_store.list_all()
                    incidents = incident_store.list_all()
                    workers = WorkerSessionStore(event_log=projection_log).list_all()
                    workspaces = WorkspaceStore().list_all(include_archived=True)
                    # Every store ``build_snapshot`` feeds the goal projection
                    # (snapshot.py, the ``goals`` map) is read HERE too, and
                    # filtered per task the same way. Passing these empty made an
                    # incrementally projected row diverge in VALUE from a rebuilt
                    # one on any mission that owns them: the head keeps
                    # ``simplified_phase`` / ``repo_bundle_ids`` /
                    # ``repo_bundle_closeout`` / ``bundle_queue`` /
                    # ``qa_waiting_on`` (repo bundles), ``active_assignment_id`` /
                    # ``persona_assignment_ids`` (assignments), ``runtime_lane``
                    # (runtime instances) and ``mission_level_state``, whose
                    # ``role_streams`` input is built from the envelopes /
                    # checklists / proof batches — that nested input is NOT
                    # stripped by ``_goal_head``, so the role stores must be read
                    # even though their own goal-row fields are evicted. Same
                    # inputs in, same row out: that IS replay equivalence, and it
                    # outranks micro-optimizing a path that already lists four
                    # stores per delta.
                    role_envelopes = RoleEnvelopeStore(event_log=projection_log).list_all()
                    role_checklists = RoleChecklistStore(event_log=projection_log).list_all()
                    repo_bundles = RepoBundleStore(event_log=projection_log).list_all()
                    runtime_instances = GoalRuntimeInstanceStore(event_log=projection_log).list_all()
                    self_test_store = SelfTestEvidenceStore(event_log=projection_log)
                    # The persona lanes are CONFIG-GATED in ``build_snapshot``;
                    # mirror the gate exactly (and off the same authority —
                    # ``load_agent_runtime_config()``, NOT the caller-supplied
                    # ``self.config``, which callers build partially). Reading the
                    # assignment store unconditionally would swap one divergence
                    # for its mirror image: rows carrying assignments the full
                    # rebuild deliberately omits while the flag is off.
                    cfg = load_agent_runtime_config()
                    instance_store = PersonaInstanceStore(event_log=projection_log)
                    persona_instances = instance_store.list_all()
                    persona_assignments: list = []
                    if persona_instance_runtime_enabled(cfg):
                        agents = AgentStore().list_all() or seed_personas()
                        persona_instances = instance_store.derive_from_workers(agents, workers)
                        if persona_assignment_store_enabled(cfg):
                            persona_assignments = PersonaAssignmentStore(event_log=projection_log).list_all()
                    goals = []
                    for task_id in task_ids:
                        try:
                            task = task_store.get(task_id)
                        except Exception:
                            continue
                        # ``_goal_head`` — the SAME split ``build_snapshot``
                        # applies (snapshot.py, the ``goals`` map). The frame
                        # ships only the goal HEAD: the heavy detail-only fields
                        # leave the row and are replaced by a typed
                        # ``detail_ref`` pointer. Writing the raw projection here
                        # made a row's shape depend on which path last touched
                        # it — an incrementally projected goal carried the full
                        # detail inline and NO ``detail_ref``, a rebuilt one the
                        # head + pointer. That broke replay equivalence (the
                        # read model's whole correctness claim) and silently gave
                        # back the S8 frame-size win on every task event.
                        goals.append(
                            _goal_head(_goal_projection_from_task(
                                task,
                                [],
                                all_tasks,
                                incidents,
                                runs,
                                projection_log.for_task(task.id, limit=200),
                                workers=workers,
                                run_store=run_store,
                                self_tests=self_test_store.list_for_task(task.id),
                                role_envelopes=[item for item in role_envelopes if item.task_id == task.id],
                                role_checklists=[item for item in role_checklists if item.task_id == task.id],
                                persona_assignments=[item for item in persona_assignments if item.task_id == task.id],
                                repo_bundles=[item for item in repo_bundles if item.task_id == task.id],
                                runtime_instances=[item for item in runtime_instances if item.task_id == task.id],
                                # NOT per-task filtered — ``build_snapshot``
                                # passes the whole topology list here too.
                                persona_instances=persona_instances,
                                # Telemetry-only: the accountants record what the
                                # projection bounded, they never change a field's
                                # value (verified against ``_stage_verification``
                                # / ``_mission_flow_timeline``), so the
                                # incremental path leaves them unwired rather
                                # than emitting duplicate parity counters for a
                                # single-task delta.
                                stage_verification_accountant=None,
                                flow_timeline_accountant=None,
                                workspaces=workspaces,
                                # Feeds ``next_action`` (and through it
                                # ``why_not_done``) via the state machine — head
                                # fields, so the same log the rest of this row is
                                # projected from has to reach it.
                                event_log=projection_log,
                            ))
                        )
                    self.read_model._write_goals(conn, goals)
                if run_ids:
                    self.read_model._write_runs(conn, [_run_summary(run_store.get(run_id)) for run_id in run_ids])
                if incident_ids:
                    self.read_model._write_incidents(conn, [_incident_summary(incident_store.get(incident_id)) for incident_id in incident_ids])
                if changed.stale_sections:
                    self.read_model._write_misc(conn, "stale_sections", sorted(changed.stale_sections))
                conn.execute(
                    """
                    INSERT OR REPLACE INTO projection_watermarks(
                        projection, event_offset, last_event_ts, applied_at
                    ) VALUES(?, ?, ?, ?)
                    """,
                    ("snapshot", int(to_offset), str(last_event_ts) if last_event_ts is not None else None, str(now())),
                )
            except Exception:
                conn.rollback()
                raise
            else:
                conn.commit()


@dataclass(slots=True)
class _Changed:
    task_ids: set[str] = field(default_factory=set)
    run_ids: set[str] = field(default_factory=set)
    proof_ids: set[str] = field(default_factory=set)
    incident_ids: set[str] = field(default_factory=set)
    stale_sections: set[str] = field(default_factory=set)

    @classmethod
    def from_events(cls, events) -> "_Changed":
        changed = cls()
        for event in events:
            event_type = str(getattr(event, "type", "") or "")
            if getattr(event, "task_id", None):
                changed.task_ids.add(str(event.task_id))
            if getattr(event, "run_id", None):
                changed.run_ids.add(str(event.run_id))
            payload = getattr(event, "payload", None)
            payload = payload if isinstance(payload, dict) else {}
            proof_id = payload.get("proof_id")
            if isinstance(proof_id, str) and proof_id:
                changed.proof_ids.add(proof_id)
            incident_id = payload.get("incident_id")
            if isinstance(incident_id, str) and incident_id:
                changed.incident_ids.add(incident_id)
            if not (
                event_type.startswith("task.")
                or event_type.startswith("run.")
                or event_type.startswith("proof.")
                or event_type.startswith("incident.")
            ):
                changed.stale_sections.add("snapshot")
        return changed

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "goals": sorted(self.task_ids),
            "runs": sorted(self.run_ids),
            "proofs": sorted(self.proof_ids),
            "incidents": sorted(self.incident_ids),
            "sections": sorted(self.stale_sections),
        }
