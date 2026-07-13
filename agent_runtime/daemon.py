from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from utils import atomic_json_write

from . import paths
from .config import load_agent_runtime_config
from .liveness import LivenessProbe
from .serde import to_jsonable
from .ticker import RunUntilSettledResult, TickEngine, TickResult


DAEMON_LEASE_TTL_SECONDS = 15.0
DAEMON_STATUS_SCHEMA_VERSION = 1
ORPHAN_REAP_REASON_RESTART = "daemon_orphan_reap_restart"
ORPHAN_REAP_REASON_STOP = "daemon_orphan_reap_stop"

# Why this module keeps three cooperating loops:
# - The main loop owns TickEngine progress and target settlement under the daemon
#   lease.
# - The heartbeat thread keeps CLI/Mission Control freshness visible while the
#   main loop sleeps between idle polls.
# - The liveness thread classifies hung active runs even when no task is
#   currently eligible for a tick. Merging it into the main loop would hide
#   stalled runs behind idle sleeps.


def _is_windows() -> bool:
    return os.name == "nt"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MissionDaemon:
    """Foreground Mission Daemon loop.

    This central coordinator repeatedly invokes the bounded TickEngine and records
    redaction-safe heartbeat/status for CLI and Mission Control consumers.
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[[], TickEngine] | None = None,
        target_task_id: str | None = None,
        foreground_runtime_instance_id: str | None = None,
        interval_seconds: float = 10,
        idle_interval_seconds: float = 30,
        heartbeat_seconds: float = 5,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.engine_factory = engine_factory or TickEngine
        self.target_task_id = _safe_task_id(target_task_id)
        self.foreground_runtime_instance_id = _safe_task_id(foreground_runtime_instance_id)
        self.interval_seconds = interval_seconds
        self.idle_interval_seconds = idle_interval_seconds
        self.heartbeat_seconds = max(0.1, float(heartbeat_seconds))
        self.sleep_fn = sleep_fn
        self._stop_requested = False

    def request_stop(self, *_args) -> None:
        self._stop_requested = True

    def run_foreground(self, *, max_loops: int | None = None) -> dict:
        lease = _acquire_daemon_lease(os.getpid())
        if not lease.get("acquired"):
            # Do not clobber the live owner's status file: its heartbeat skips
            # offline/error states, so an error write here would never be reclaimed.
            status = read_daemon_status()
            if status.get("pid") in {None, os.getpid()}:
                _write_daemon_status({
                    "state": "error",
                    "pid": os.getpid(),
                    "heartbeat_at": _utc_now(),
                    "error": "daemon_lease_held",
                    "lease_pid": lease.get("pid"),
                })
            return {"loops": 0, "last_tick_id": None, "stopped": True}
        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        loops = 0
        last_tick_id: str | None = None
        # A previous daemon killed mid-turn leaves its run row active with no
        # engine; reap immediately instead of waiting on the liveness watchdog.
        orphans_reaped = reap_orphaned_active_runs(reason=ORPHAN_REAP_REASON_RESTART)
        self._emit_startup_doctor_report()
        self._emit_lifecycle_event("daemon.started", orphan_runs_reaped=len(orphans_reaped))
        try:
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(target=self._heartbeat_loop, args=(heartbeat_stop,), name="mission-daemon-heartbeat", daemon=True)
            heartbeat_thread.start()
            liveness_stop = threading.Event()
            liveness_thread = threading.Thread(target=self._liveness_loop, args=(liveness_stop,), name="mission-daemon-liveness", daemon=True)
            liveness_thread.start()
            while not self._stop_requested:
                if max_loops is not None and loops >= max_loops:
                    break
                if _daemon_status_owned_by_other_live_pid(os.getpid()):
                    self._stop_requested = True
                    break
                loops += 1
                _write_daemon_status(_carry_liveness({
                    "state": "running",
                    "pid": os.getpid(),
                    "loops": loops,
                    "heartbeat_at": _utc_now(),
                    "target_task_id": self.target_task_id,
                    "queue_mode": "lane",
                    "services_open_tasks": True,
                    "foreground_runtime_instance_id": self.foreground_runtime_instance_id,
                }))
                _refresh_daemon_lease(os.getpid())
                if _daemon_status_owned_by_other_live_pid(os.getpid()):
                    self._stop_requested = True
                    break
                terminal_cleanup = self._settle_terminal_target_if_needed()
                if terminal_cleanup is not None:
                    last_tick_id = terminal_cleanup.get("settle_id")
                    _write_daemon_status(_carry_liveness({
                        "state": "idle",
                        "pid": os.getpid(),
                        "loops": loops,
                        "heartbeat_at": _utc_now(),
                        "target_task_id": self.target_task_id,
                        "queue_mode": "lane",
                        "services_open_tasks": True,
                        "foreground_runtime_instance_id": self.foreground_runtime_instance_id,
                        "last_tick_id": last_tick_id,
                        "settle_stop_reason": terminal_cleanup.get("settle_stop_reason"),
                        "target_cleanup": terminal_cleanup,
                    }))
                    self._stop_requested = True
                    break
                try:
                    engine = self.engine_factory()
                    if hasattr(engine, "run_until_settled"):
                        tick = self._run_settled_cycle(engine)
                    else:
                        tick = engine.tick_once()
                except Exception as exc:
                    _write_daemon_status(_carry_liveness({
                        "state": "error",
                        "pid": os.getpid(),
                        "loops": loops,
                        "heartbeat_at": _utc_now(),
                        "target_task_id": self.target_task_id,
                        "queue_mode": "lane",
                        "services_open_tasks": True,
                        "foreground_runtime_instance_id": self.foreground_runtime_instance_id,
                        "error_class": type(exc).__name__,
                        "error_summary": "Mission Daemon tick failed",
                    }))
                    break
                last_tick_id = getattr(tick, "tick_id", getattr(tick, "settle_id", None))
                actions = len(tick.actions_taken)
                stop_reason = getattr(tick, "stop_reason", None)
                wait_seconds = 0 if actions and stop_reason == "max_actions" else (self.interval_seconds if actions else self.idle_interval_seconds)
                next_wake = _utc_now() + timedelta(seconds=wait_seconds)
                _write_daemon_status(_status_from_tick(tick, loops=loops, wait_seconds=wait_seconds, next_wake_at=next_wake))
                if self.target_task_id and stop_reason == "task_terminal":
                    terminal_cleanup = self._settle_terminal_target_if_needed()
                    if terminal_cleanup is not None:
                        last_tick_id = terminal_cleanup.get("settle_id") or last_tick_id
                    self._stop_requested = True
                    break
                if wait_seconds > 0 and not self._stop_requested:
                    self._sleep_with_stop(wait_seconds)
        finally:
            if "heartbeat_stop" in locals():
                heartbeat_stop.set()
            if "heartbeat_thread" in locals():
                heartbeat_thread.join(timeout=1)
            if "liveness_stop" in locals():
                liveness_stop.set()
            if "liveness_thread" in locals():
                liveness_thread.join(timeout=1)
            _clear_daemon_lease(os.getpid())
            if self._stop_requested:
                _write_final_offline_status(os.getpid(), loops=loops, last_tick_id=last_tick_id)
            self._emit_lifecycle_event("daemon.stopped", reason="stop_requested" if self._stop_requested else "loop_bound_reached")
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        return {"loops": loops, "last_tick_id": last_tick_id, "stopped": self._stop_requested}

    def _run_settled_cycle(self, engine) -> RunUntilSettledResult:
        if not self.target_task_id:
            return engine.run_until_settled(max_actions=10)
        tick = engine.run_until_settled(task_id=self.target_task_id, max_actions=10)
        if getattr(tick, "stop_reason", None) == "task_terminal":
            return tick
        if not hasattr(engine, "tick_once"):
            return tick
        queue_tick = engine.tick_once()
        if not queue_tick.actions_taken:
            return tick
        tick.actions_taken.extend(queue_tick.actions_taken)
        tick.open_incidents = max(int(getattr(tick, "open_incidents", 0) or 0), len(getattr(queue_tick, "incidents_opened", []) or []))
        tick.finished_at = queue_tick.finished_at or tick.finished_at
        if getattr(tick, "stop_reason", None) in {"no_eligible_action", "incident_opened", "task_blocked"}:
            tick.stop_reason = "background_progress"
        return tick

    def _settle_terminal_target_if_needed(self) -> dict | None:
        if not self.target_task_id:
            return None
        try:
            from .errors import NotFound
            from .states import TaskState
            from .store import ARCHIVABLE_TASK_STATES, ArchiveStore, RunStore, TaskStore
            from .worker_sessions import WorkerSessionStore

            try:
                task = TaskStore().get(self.target_task_id)
            except NotFound:
                if not _target_task_archived(self.target_task_id):
                    return None
                return {
                    "settle_id": f"settle_missing_{self.target_task_id}",
                    "settle_stop_reason": "task_archived",
                    "task_id": self.target_task_id,
                    "task_state": "archived_or_missing",
                    "cancelled_run_ids": [],
                    "closed_worker_session_ids": [],
                    "archive_result": None,
                }
            if task.state not in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
                return None
            stop_reason = "task_cancelled" if task.state == TaskState.CANCELLED else "task_terminal"
            run_store = RunStore()
            worker_store = WorkerSessionStore()
            cancelled_run_ids: list[str] = []
            for run in list(run_store.find_active(task_id=task.id)):
                try:
                    run_store.cancel(run.id, reason=f"target task is {task.state.value}")
                    cancelled_run_ids.append(run.id)
                except Exception:
                    continue
            closed_worker_ids: list[str] = []
            for worker in list(worker_store.find_active(task_id=task.id)):
                try:
                    closed = worker_store.close(
                        worker.id,
                        reason=f"target task is {task.state.value}",
                    )
                    closed_worker_ids.append(closed.id)
                except Exception:
                    continue
            archive_result = None
            if task.state in ARCHIVABLE_TASK_STATES:
                archive_result = ArchiveStore().archive_tasks(
                    [task.id],
                    actor="daemon",
                    reason=f"auto-archive foreground target {task.state.value}",
                )
            cleanup = {
                "settle_id": f"settle_terminal_{task.id}",
                "settle_stop_reason": stop_reason,
                "task_id": task.id,
                "task_state": task.state.value,
                "cancelled_run_ids": cancelled_run_ids,
                "closed_worker_session_ids": closed_worker_ids,
                "archive_result": archive_result,
            }
            self._emit_target_cleanup_event(cleanup)
            return cleanup
        except Exception as exc:
            return {
                "settle_id": f"settle_error_{self.target_task_id}",
                "settle_stop_reason": "target_cleanup_failed",
                "task_id": self.target_task_id,
                "error_class": type(exc).__name__,
            }

    def _emit_startup_doctor_report(self) -> None:
        if not self.target_task_id:
            return
        try:
            from .harness_doctor import run_harness_doctor
            from .events import EventLog
            from .models import Event
            from hermes_time import now

            report = run_harness_doctor(fix=False, include_worktrees=False)
            counts = dict((report.get("summary") or {}).get("finding_counts") or {})
            EventLog().append(
                Event(
                    now(),
                    "daemon.doctor_report",
                    self.target_task_id,
                    None,
                    None,
                    {"finding_counts": counts, "fix": False, "dry_run": False},
                )
            )
        except Exception:
            pass

    def _emit_target_cleanup_event(self, cleanup: dict) -> None:
        try:
            from .events import EventLog
            from .models import Event
            from hermes_time import now

            archive_result = cleanup.get("archive_result") if isinstance(cleanup.get("archive_result"), dict) else {}
            EventLog().append(
                Event(
                    now(),
                    "daemon.target_settled",
                    self.target_task_id,
                    None,
                    None,
                    {
                        "settle_stop_reason": cleanup.get("settle_stop_reason"),
                        "task_state": cleanup.get("task_state"),
                        "cancelled_runs": len(cleanup.get("cancelled_run_ids") or []),
                        "closed_workers": len(cleanup.get("closed_worker_session_ids") or []),
                        "archive_batch": archive_result.get("archive_batch"),
                    },
                )
            )
        except Exception:
            pass

    def _emit_lifecycle_event(self, event_type: str, *, reason: str | None = None, orphan_runs_reaped: int | None = None) -> None:
        try:
            from hermes_time import now

            from .events import EventLog
            from .models import Event

            payload = {
                "mode": "mission_daemon",
                "self_driven": True,
                "pid": os.getpid(),
                "queue_mode": "lane",
            }
            if reason:
                payload["reason"] = str(reason)[:120]
            if orphan_runs_reaped:
                payload["orphan_runs_reaped"] = int(orphan_runs_reaped)
            EventLog().append(Event(now(), event_type, self.target_task_id, None, None, payload))
        except Exception:
            # Lifecycle events are observability; never fail the daemon for them.
            pass

    def _heartbeat_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.wait(self.heartbeat_seconds):
            if self._stop_requested:
                return
            status = read_daemon_status()
            if status.get("state") in {"offline", "error"}:
                continue
            if status.get("pid") not in {None, os.getpid()}:
                continue
            status["pid"] = os.getpid()
            status["heartbeat_at"] = _utc_now()
            _write_daemon_status(status)
            _refresh_daemon_lease(os.getpid())

    def _liveness_loop(self, stop_event: threading.Event) -> None:
        try:
            config = load_agent_runtime_config()
            probe = LivenessProbe(config=config)
            poll_seconds = max(30.0, min(120.0, float(getattr(config, "liveness_poll_seconds", 60) or 60)))
        except Exception:
            return
        while not self._stop_requested and not stop_event.is_set():
            if bool(getattr(config, "liveness_enabled", True)):
                try:
                    result = probe.poll_once()
                    status = read_daemon_status()
                    if status.get("pid") in {None, os.getpid()} and status.get("state") not in {"offline", "error"}:
                        status["liveness"] = _liveness_status(result)
                        _write_daemon_status(status)
                except Exception:
                    pass
            if stop_event.wait(poll_seconds):
                return

    def _sleep_with_stop(self, wait_seconds: float) -> None:
        self.sleep_fn(wait_seconds)


def reap_orphaned_active_runs(*, reason: str) -> list[str]:
    """Cancel mid-turn runs that lost their driving engine (doc-07 gap 5).

    Only one mission driver holds the daemon lease at a time, so an active
    (queued/starting/running/waiting-on-tool) run found while the daemon is
    stopping — or while a fresh daemon is starting after a kill — has no
    engine left to finish it. Without this reap the run row stays ``running``
    until the liveness watchdog cancels it minutes later; the watchdog is the
    backstop, not the contract. WAITING_ON_APPROVAL runs are deliberately
    parked and survive restarts, so they are never reaped here.
    """

    from .states import RunState
    from .store import RunStore
    from .worker_sessions import WorkerSessionStore

    cancelled_ids: list[str] = []
    try:
        runs = RunStore()
        workers = WorkerSessionStore()
        for run in runs.find_active():
            if run.state not in {RunState.QUEUED, RunState.STARTING, RunState.RUNNING, RunState.WAITING_ON_TOOL}:
                continue
            try:
                cancelled = runs.cancel(run.id, reason=reason)
            except Exception:
                continue
            cancelled_ids.append(run.id)
            try:
                for worker in workers.find_active(task_id=run.task_id, persona_id=run.persona_id):
                    if worker.active_run_id and worker.active_run_id != run.id:
                        continue
                    workers.update_after_run(worker.id, cancelled, close_reason=reason, count_decision=False)
            except Exception:
                continue
    except Exception:
        return cancelled_ids
    return cancelled_ids


def _write_final_offline_status(pid: int, *, loops: int, last_tick_id: str | None) -> None:
    status = read_daemon_status()
    if status.get("pid") not in {None, pid}:
        # Another daemon owns the status file; do not clobber it on our way out.
        return
    status.pop("pid", None)
    status.update({
        "state": "offline",
        "last_pid": pid,
        "loops": loops,
        "last_tick_id": last_tick_id or status.get("last_tick_id"),
        "stopped_at": _utc_now(),
    })
    _write_daemon_status(status)


def start_daemon(*, task_id: str | None = None, foreground_runtime_instance_id: str | None = None, interval_seconds: float | None = None, idle_interval_seconds: float | None = None) -> dict:
    task_id = _safe_task_id(task_id)
    foreground_runtime_instance_id = _safe_task_id(foreground_runtime_instance_id)
    target_source = "explicit" if task_id else None
    if not task_id:
        adopted = _adopt_active_foreground_lane()
        if adopted is not None:
            task_id = adopted["task_id"]
            foreground_runtime_instance_id = foreground_runtime_instance_id or adopted["instance_id"]
            target_source = "foreground_lane"
    status = read_daemon_status()
    pid = status.get("pid")
    if isinstance(pid, int) and _pid_is_alive(pid):
        existing_target = _safe_task_id(status.get("target_task_id"))
        if task_id and existing_target != task_id:
            if status.get("services_open_tasks") is True:
                return {
                    "started": False,
                    "pid": pid,
                    "state": status.get("state", "running"),
                    "target_task_id": existing_target,
                    "requested_task_id": task_id,
                    "queue_mode": status.get("queue_mode") or "lane",
                    "services_open_tasks": True,
                    "will_service_open_tasks": True,
                }
            return {
                "started": False,
                "pid": pid,
                "state": status.get("state", "running"),
                "target_task_id": existing_target,
                "requested_task_id": task_id,
                "error": "daemon_target_conflict",
            }
        return {"started": False, "pid": pid, "state": status.get("state", "running")}
    lease = _read_daemon_lease()
    lease_pid = lease.get("pid") if isinstance(lease, dict) else None
    if isinstance(lease_pid, int) and _pid_is_alive(lease_pid) and not _lease_expired(lease):
        return {"started": False, "pid": lease_pid, "state": "running", "error": "daemon_lease_held"}

    cmd = [sys.executable, "-m", "hermes_cli.main", "harness", "daemon", "foreground"]
    if task_id:
        cmd.extend(["--task", task_id])
    if interval_seconds is not None:
        cmd.extend(["--interval", str(interval_seconds)])
    if idle_interval_seconds is not None:
        cmd.extend(["--idle-interval", str(idle_interval_seconds)])
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
    _write_daemon_lease(proc.pid)
    queue_mode = "lane"
    _write_daemon_status({
        "state": "starting",
        "pid": proc.pid,
        "heartbeat_at": _utc_now(),
        "target_task_id": task_id,
        "target_source": target_source,
        "queue_mode": queue_mode,
        "services_open_tasks": True,
        "foreground_runtime_instance_id": foreground_runtime_instance_id,
    })
    return {"started": True, "pid": proc.pid, "state": "starting", "target_task_id": task_id, "target_source": target_source, "queue_mode": queue_mode, "services_open_tasks": True}


def _adopt_active_foreground_lane() -> dict | None:
    """Declared foreground lane target for an untargeted daemon start."""

    try:
        from .runtime_instances import GoalRuntimeInstanceStore

        instance = GoalRuntimeInstanceStore().active_foreground()
    except Exception:
        return None
    if instance is None or not instance.task_id:
        return None
    return {"task_id": instance.task_id, "instance_id": instance.id}


def stop_daemon() -> dict:
    status = read_daemon_status()
    pid = status.get("pid")
    if not isinstance(pid, int) or not _pid_is_alive(pid):
        _write_daemon_status({"state": "offline"})
        _clear_daemon_lease(pid if isinstance(pid, int) else None)
        orphans = reap_orphaned_active_runs(reason=ORPHAN_REAP_REASON_STOP)
        return {"stopped": False, "state": "offline", "orphan_runs_cancelled": orphans}
    if _is_windows():
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.kill(pid, signal.SIGTERM)
    if not _wait_for_pid_exit(pid, timeout_seconds=5.0):
        if _is_windows():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGKILL)
        _wait_for_pid_exit(pid, timeout_seconds=5.0)
    if _pid_is_alive(pid):
        # Do not report offline while the daemon process is still alive; a false
        # offline status invites a second daemon to start and contend for ticks.
        return {"stopped": False, "pid": pid, "state": status.get("state", "running"), "error": "daemon_pid_survived_stop"}
    # The daemon process is gone; any run it was driving mid-turn can never
    # finish. Cancel it now instead of waiting on the liveness watchdog.
    orphans = reap_orphaned_active_runs(reason=ORPHAN_REAP_REASON_STOP)
    _write_daemon_status({"state": "offline", "last_pid": pid})
    _clear_daemon_lease(pid)
    return {"stopped": True, "pid": pid, "state": "offline", "orphan_runs_cancelled": orphans}


def _wait_for_pid_exit(pid: int, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _pid_is_alive(pid):
            return True
        time.sleep(0.25)
    return not _pid_is_alive(pid)


def _pid_is_alive(pid: int) -> bool:
    if _is_windows():
        try:
            result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=5)
        except Exception:
            return False
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except BaseException:
        return False
    return True


def _daemon_status_owned_by_other_live_pid(current_pid: int) -> bool:
    status = read_daemon_status()
    pid = status.get("pid")
    return isinstance(pid, int) and pid != current_pid and _pid_is_alive(pid)


def read_daemon_status() -> dict:
    path = paths.daemon_status_path()
    if not path.exists():
        return {"state": "offline"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"state": "error"}
    if not isinstance(data, dict):
        return {"state": "error"}
    pid = data.get("pid")
    if isinstance(pid, int) and data.get("state") not in {"offline", "error"} and not _pid_is_alive(pid):
        return {"state": "offline", "last_pid": pid, "cleared_reason": "dead_pid"}
    return data


def daemon_status_schema(status: dict | None = None) -> dict:
    """Public, versioned daemon status shape shared by every CLI/status surface."""

    raw = dict(status or read_daemon_status() or {})
    state = str(raw.get("state") or "offline")
    loops = raw.get("loops")
    try:
        loops = int(loops)
    except (TypeError, ValueError):
        loops = 0
    normalized = {
        "schema_version": DAEMON_STATUS_SCHEMA_VERSION,
        "field_set_version": DAEMON_STATUS_SCHEMA_VERSION,
        "state": state,
        "pid": raw.get("pid") if isinstance(raw.get("pid"), int) else None,
        "heartbeat_at": raw.get("heartbeat_at"),
        "target_task_id": raw.get("target_task_id"),
        "settle_stop_reason": raw.get("settle_stop_reason"),
        "loops": loops,
    }
    for key in (
        "last_pid",
        "last_tick_id",
        "last_tick_started_at",
        "last_tick_finished_at",
        "tasks_seen_last_tick",
        "actions_last_tick",
        "next_wake_at",
        "wait_seconds",
        "services_open_tasks",
        "queue_mode",
        "foreground_runtime_instance_id",
        "settle_ticks",
        "liveness",
        "cleared_by",
        "cleared_reason",
        "error",
    ):
        if key in raw and key not in normalized:
            normalized[key] = raw.get(key)
    return normalized


def _read_daemon_lease() -> dict:
    path = paths.daemon_lease_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_daemon_lease(pid: int) -> None:
    now_dt = _utc_now()
    atomic_json_write(
        paths.daemon_lease_path(),
        {
            "pid": pid,
            "acquired_at": now_dt.isoformat().replace("+00:00", "Z"),
            "expires_at": (now_dt + timedelta(seconds=DAEMON_LEASE_TTL_SECONDS)).isoformat().replace("+00:00", "Z"),
        },
        indent=2,
        sort_keys=True,
    )


def _acquire_daemon_lease(pid: int) -> dict:
    lease = _read_daemon_lease()
    existing_pid = lease.get("pid")
    if isinstance(existing_pid, int) and existing_pid != pid and _pid_is_alive(existing_pid) and not _lease_expired(lease):
        return {"acquired": False, "pid": existing_pid}
    _write_daemon_lease(pid)
    return {"acquired": True, "pid": pid}


def _refresh_daemon_lease(pid: int) -> None:
    lease = _read_daemon_lease()
    lease_pid = lease.get("pid")
    if lease_pid not in {None, pid}:
        return
    _write_daemon_lease(pid)


def _clear_daemon_lease(pid: int | None = None) -> None:
    lease = _read_daemon_lease()
    lease_pid = lease.get("pid")
    if pid is not None and isinstance(lease_pid, int) and lease_pid != pid and _pid_is_alive(lease_pid):
        return
    try:
        paths.daemon_lease_path().unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _lease_expired(lease: dict) -> bool:
    raw = lease.get("expires_at")
    if isinstance(raw, datetime):
        expires = raw
    else:
        try:
            expires = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires <= _utc_now()


def _status_from_tick(tick: TickResult, *, loops: int, wait_seconds: float, next_wake_at: datetime) -> dict:
    actions = len(tick.actions_taken)
    status = {
        "state": "active" if actions else "idle",
        "pid": os.getpid(),
        "heartbeat_at": _utc_now(),
        "last_tick_id": getattr(tick, "tick_id", getattr(tick, "settle_id", None)),
        "last_tick_started_at": tick.started_at,
        "last_tick_finished_at": tick.finished_at,
        "loops": loops,
        "tasks_seen_last_tick": getattr(tick, "tasks_seen", 0),
        "actions_last_tick": actions,
        "next_wake_at": next_wake_at,
        "wait_seconds": wait_seconds,
        "services_open_tasks": True,
    }
    previous = read_daemon_status()
    if previous.get("target_task_id"):
        status["target_task_id"] = previous.get("target_task_id")
        status["queue_mode"] = previous.get("queue_mode") or "lane"
    elif previous.get("queue_mode"):
        status["queue_mode"] = previous.get("queue_mode")
    else:
        status["queue_mode"] = "lane"
    if previous.get("foreground_runtime_instance_id"):
        status["foreground_runtime_instance_id"] = previous.get("foreground_runtime_instance_id")
    stop_reason = getattr(tick, "stop_reason", None)
    if stop_reason:
        status["settle_stop_reason"] = stop_reason
        status["settle_ticks"] = getattr(tick, "ticks", None)
    previous_liveness = previous.get("liveness")
    if isinstance(previous_liveness, dict):
        status["liveness"] = previous_liveness
    return status


def _liveness_status(result: dict) -> dict:
    safe = {
        "enabled": bool(result.get("enabled", True)),
        "checked_runs": int(result.get("checked_runs") or 0),
        "warnings": int(result.get("warnings") or 0),
        "hung_runs": int(result.get("hung_runs") or 0),
    }
    last_poll = result.get("last_poll_at")
    if last_poll is not None:
        safe["last_poll_at"] = last_poll
    return safe


def _carry_liveness(status: dict) -> dict:
    """Preserve the liveness block across full status rewrites.

    The liveness thread only refreshes its block every poll interval
    (30-120s); a loop-top rewrite without carrying it left `liveness: None`
    for most of each daemon loop.
    """

    previous = read_daemon_status().get("liveness")
    if isinstance(previous, dict) and "liveness" not in status:
        status["liveness"] = previous
    return status


def _target_task_archived(task_id: str) -> bool:
    archive_root = paths.deleted_archive_dir()
    if not archive_root.exists():
        return False
    task_name = f"{task_id}.json"
    try:
        return any(path.name == task_name for path in archive_root.glob("*/tasks/*.json"))
    except OSError:
        return False


def _write_daemon_status(status: dict) -> None:
    atomic_json_write(paths.daemon_status_path(), to_jsonable(status), indent=2, sort_keys=True)


def _safe_task_id(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if all(ch.isalnum() or ch in "_.:-" for ch in text) and len(text) <= 128:
        return text
    return None
