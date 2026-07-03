from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import uuid

from hermes_time import now

from . import paths
from .events import EventLog
from .models import Event
from .no_freeze_monitor import NoFreezeThresholds, record_freeze_findings
from .runtime_config import RuntimeConfig
from .states import RunState
from .store import IncidentStore, RunStore, TaskStore
from .worker_sessions import WorkerSessionStore

_LIVENESS_EVENT_TYPES = {"liveness.poll", "run.liveness.warning"}


@dataclass(slots=True)
class _RunProbeState:
    event_offset: int
    classification: str = "advancing"
    quiet_strikes: int = 0
    poll_count: int = 0
    tool_in_flight: bool = False


class LivenessProbe:
    """Active liveness watchdog for live Harness runs.

    The probe is intentionally store-only: no model calls and no snapshot builds.
    It watches active runs by heartbeat plus byte-offset event progress, then routes
    hung runs through the existing incident/recovery surfaces.
    """

    def __init__(
        self,
        *,
        config: RuntimeConfig | None = None,
        run_store: RunStore | None = None,
        incident_store: IncidentStore | None = None,
        task_store: TaskStore | None = None,
        event_log: EventLog | None = None,
        worker_session_store: WorkerSessionStore | None = None,
        thresholds: NoFreezeThresholds | None = None,
    ) -> None:
        self.config = config or RuntimeConfig()
        self.run_store = run_store or RunStore()
        self.incident_store = incident_store or IncidentStore()
        self.task_store = task_store or TaskStore()
        self.event_log = event_log or EventLog()
        self.worker_session_store = worker_session_store or WorkerSessionStore()
        self.thresholds = thresholds or NoFreezeThresholds(
            liveness_poll_seconds=int(getattr(self.config, "liveness_poll_seconds", 60) or 60),
            liveness_quiet_strikes=int(getattr(self.config, "liveness_quiet_strikes", 2) or 2),
            liveness_hung_seconds=int(getattr(self.config, "liveness_hung_seconds", 300) or 300),
        )
        self._states: dict[str, _RunProbeState] = {}
        self.last_poll_at: datetime | None = None
        self.last_result: dict[str, Any] = {"checked_runs": 0, "hung_runs": 0, "warnings": 0}

    def poll_once(self) -> dict[str, Any]:
        if not bool(getattr(self.config, "liveness_enabled", True)):
            self.last_result = {"enabled": False, "checked_runs": 0, "hung_runs": 0, "warnings": 0}
            return self.last_result

        checked = 0
        warnings = 0
        hung = 0
        active_runs = self.run_store.find_active()
        active_ids = {run.id for run in active_runs}
        for stale_id in set(self._states) - active_ids:
            self._states.pop(stale_id, None)

        ref = now()
        for run in active_runs:
            checked += 1
            state = self._states.setdefault(run.id, _RunProbeState(event_offset=_current_event_offset()))
            state.poll_count += 1
            new_offset, event_advanced = self._consume_run_events(run.id, state.event_offset, state=state)
            state.event_offset = new_offset
            age_seconds = _age_seconds(ref, run.last_heartbeat_at)
            heartbeat_fresh = age_seconds <= int(getattr(self.config, "liveness_poll_seconds", 60) or 60)
            previous = state.classification

            # A tool that announced run.tool.started but has not finished is
            # allowed to stay quiet for the tool-wait allowance on top of the
            # hung threshold: long legitimate commands (builds, test suites)
            # emit nothing between started and finished, and cancelling them
            # would be a false positive. The 900s TTL floor still applies.
            hung_threshold = int(getattr(self.config, "liveness_hung_seconds", 300) or 300)
            if state.tool_in_flight:
                hung_threshold += int(getattr(self.config, "tool_wait_timeout_seconds", 300) or 300)

            if heartbeat_fresh or event_advanced:
                state.quiet_strikes = 0
                state.classification = "advancing"
            elif age_seconds >= hung_threshold:
                state.quiet_strikes += 1
                state.classification = "hung"
            else:
                state.quiet_strikes += 1
                state.classification = "quiet" if state.quiet_strikes >= int(getattr(self.config, "liveness_quiet_strikes", 2) or 2) else "advancing"

            if self._should_emit_poll(previous, state):
                self._emit_poll(run, state, age_seconds)
            if state.classification == "quiet" and previous != "quiet":
                warnings += 1
                self._warn(run, state, age_seconds)
            if state.classification == "hung":
                if self._remediate_hung(run, age_seconds):
                    hung += 1

        self.last_poll_at = ref
        self.last_result = {
            "enabled": True,
            "checked_runs": checked,
            "warnings": warnings,
            "hung_runs": hung,
            "last_poll_at": ref,
        }
        return self.last_result

    def status(self) -> dict[str, Any]:
        return dict(self.last_result, last_poll_at=self.last_poll_at)

    def _consume_run_events(self, run_id: str, offset: int, *, state: _RunProbeState | None = None) -> tuple[int, bool]:
        latest = max(0, int(offset or 0))
        advanced = False
        for new_offset, evt in self.event_log.iter_from_offset(offset) or ():
            latest = max(latest, int(new_offset or latest))
            if evt.run_id != run_id:
                continue
            if evt.type not in _LIVENESS_EVENT_TYPES:
                advanced = True
            if state is not None:
                if evt.type == "run.tool.started":
                    state.tool_in_flight = True
                elif evt.type == "run.tool.finished":
                    state.tool_in_flight = False
        return latest, advanced

    def _should_emit_poll(self, previous: str, state: _RunProbeState) -> bool:
        return state.classification != previous or state.poll_count % 10 == 0

    def _emit_poll(self, run, state: _RunProbeState, age_seconds: float) -> None:
        self.event_log.append(
            Event(
                ts=now(),
                type="liveness.poll",
                task_id=run.task_id,
                run_id=run.id,
                persona_id=run.persona_id,
                payload={
                    "run_id": run.id,
                    "classification": state.classification,
                    "status": "blocked" if state.classification == "hung" else "running",
                    "summary": _safe_text(f"Run liveness is {state.classification}"),
                    "age_seconds": int(age_seconds),
                    "poll_count": state.poll_count,
                },
            )
        )

    def _warn(self, run, state: _RunProbeState, age_seconds: float) -> None:
        worker_id = self._active_worker_id_for_run(run.id)
        if worker_id:
            self.worker_session_store.record_watchdog_warning(
                worker_id,
                kind="run_quiet",
                summary="Run heartbeat has gone quiet.",
            )
        self.event_log.append(
            Event(
                ts=now(),
                type="run.liveness.warning",
                task_id=run.task_id,
                run_id=run.id,
                persona_id=run.persona_id,
                payload={
                    "run_id": run.id,
                    "kind": "run_quiet",
                    "summary": "Run heartbeat has gone quiet.",
                    "age_seconds": int(age_seconds),
                    "quiet_strikes": state.quiet_strikes,
                    "worker_session_id": worker_id,
                },
            )
        )

    def _remediate_hung(self, run, age_seconds: float) -> bool:
        if self._has_open_run_hung_incident(run.id):
            return False
        cancelled = self.run_store.cancel(run.id, reason="liveness_hung")
        for worker in self.worker_session_store.find_active(task_id=run.task_id, persona_id=run.persona_id):
            if worker.active_run_id and worker.active_run_id != run.id:
                continue
            self.worker_session_store.record_watchdog_warning(
                worker.id,
                kind="run_hung",
                summary="Run exceeded the active liveness threshold.",
            )
            self.worker_session_store.update_after_run(
                worker.id,
                cancelled,
                close_reason="liveness_hung",
                count_decision=False,
            )
            self.worker_session_store.close(worker.id, reason="liveness_hung")
        record_freeze_findings(
            task_id=run.task_id,
            findings=[
                {
                    "kind": "run_hung",
                    "severity": "critical",
                    "task_id": run.task_id,
                    "run_id": run.id,
                    "summary": "Run exceeded the active liveness threshold",
                    "age_seconds": int(age_seconds),
                }
            ],
            incident_store=self.incident_store,
            task_store=self.task_store,
            event_log=self.event_log,
        )
        return True

    def _has_open_run_hung_incident(self, run_id: str) -> bool:
        return any(incident.kind == "run_hung" and incident.run_id == run_id for incident in self.incident_store.list_open())

    def _active_worker_id_for_run(self, run_id: str) -> str | None:
        for worker in self.worker_session_store.find_active():
            if worker.active_run_id == run_id:
                return worker.id
        return None


def _current_event_offset() -> int:
    path = paths.events_path()
    try:
        return path.stat().st_size if path.exists() else 0
    except OSError:
        return 0


def _age_seconds(ref: datetime, value: datetime | None) -> float:
    if value is None:
        return 0.0
    heartbeat = value
    if heartbeat.tzinfo is None and ref.tzinfo is not None:
        heartbeat = heartbeat.replace(tzinfo=ref.tzinfo)
    if heartbeat.tzinfo is not None and ref.tzinfo is None:
        ref = ref.replace(tzinfo=heartbeat.tzinfo)
    return max(0.0, (ref - heartbeat).total_seconds())


def _safe_text(value: str) -> str:
    text = " ".join(str(value or "").split())
    lowered = text.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "api_key", "bearer")):
        return "[redacted]"
    if ":/" in text or "\\" in text:
        return "[redacted-path]"
    return text[:240]


def run_liveness_bounded_command(
    invocation: str | list[str],
    *,
    shell: bool,
    cwd: Path,
    timeout_seconds: int,
    task_id: str,
    run_id: str,
    persona_id: str,
    run_store: RunStore | None = None,
    incident_store: IncidentStore | None = None,
    task_store: TaskStore | None = None,
    event_log: EventLog | None = None,
):
    """Run a worker command with proof-runner timeout semantics and liveness events."""

    from .models import Incident
    from .proof_runner import _run_bounded_process

    run_store = run_store or RunStore()
    incident_store = incident_store or IncidentStore()
    task_store = task_store or TaskStore()
    event_log = event_log or EventLog()

    def progress(payload: dict[str, object]) -> None:
        try:
            run_store.heartbeat(run_id)
        except Exception:
            pass
        status = str(payload.get("status") or "running")
        event_log.append(
            Event(
                ts=now(),
                type="run.progress",
                task_id=task_id,
                run_id=run_id,
                persona_id=persona_id,
                payload={
                    "phase": "tool",
                    "step": "bounded_command",
                    "status": "blocked" if status == "timeout" else "running",
                    "summary": "Worker command timed out." if status == "timeout" else "Worker command is running.",
                    "elapsed_seconds": int(payload.get("elapsed_seconds") or 0),
                    "timeout_seconds": int(payload.get("timeout_seconds") or timeout_seconds),
                },
            )
        )

    completed = _run_bounded_process(
        invocation,
        shell=shell,
        cwd=cwd,
        timeout_seconds=max(1, int(timeout_seconds or 1)),
        progress_callback=progress,
    )
    if getattr(completed, "timed_out", False) and not any(
        incident.kind == "command_hung" and incident.run_id == run_id for incident in incident_store.list_open()
    ):
        incident = Incident(
            id=f"inc_{uuid.uuid4().hex[:8]}",
            task_id=task_id,
            run_id=run_id,
            kind="command_hung",
            summary="command_hung: worker command exceeded timeout",
            detail_path=None,
            opened_at=now(),
            metadata={"timeout_seconds": int(timeout_seconds or 1)},
        )
        incident_store.open(incident)
        _link_command_incident(task_store, task_id, incident.id)
    return completed


def _link_command_incident(task_store: TaskStore, task_id: str, incident_id: str) -> None:
    try:
        task = task_store.get(task_id)
    except Exception:
        return
    if incident_id not in task.open_incident_ids:
        task.open_incident_ids.append(incident_id)
        task.updated_at = now()
        task_store.update(task, actor="harness_liveness", reason="command hung")
