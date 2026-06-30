from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from utils import atomic_json_write

from . import paths
from .serde import to_jsonable
from .ticker import TickEngine, TickResult


DAEMON_LEASE_TTL_SECONDS = 15.0


def _is_windows() -> bool:
    return os.name == "nt"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class DaemonLoopResult:
    loops: int
    last_tick_id: str | None
    stopped: bool

    def to_json(self) -> dict:
        return {"loops": self.loops, "last_tick_id": self.last_tick_id, "stopped": self.stopped}


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
        self.target_task_id = None
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
            return DaemonLoopResult(loops=0, last_tick_id=None, stopped=True).to_json()
        previous_int = signal.getsignal(signal.SIGINT)
        previous_term = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        loops = 0
        last_tick_id: str | None = None
        self._emit_lifecycle_event("daemon.started")
        try:
            heartbeat_stop = threading.Event()
            heartbeat_thread = threading.Thread(target=self._heartbeat_loop, args=(heartbeat_stop,), name="mission-daemon-heartbeat", daemon=True)
            heartbeat_thread.start()
            while not self._stop_requested:
                if max_loops is not None and loops >= max_loops:
                    break
                if _daemon_status_owned_by_other_live_pid(os.getpid()):
                    self._stop_requested = True
                    break
                loops += 1
                _write_daemon_status({
                    "state": "running",
                    "pid": os.getpid(),
                    "loops": loops,
                    "heartbeat_at": _utc_now(),
                    "target_task_id": self.target_task_id,
                    "queue_mode": "lane",
                    "foreground_runtime_instance_id": self.foreground_runtime_instance_id,
                })
                _refresh_daemon_lease(os.getpid())
                if _daemon_status_owned_by_other_live_pid(os.getpid()):
                    self._stop_requested = True
                    break
                try:
                    engine = self.engine_factory()
                    if hasattr(engine, "run_until_settled"):
                        tick = engine.run_until_settled(max_actions=10)
                    else:
                        tick = engine.tick_once()
                except Exception as exc:
                    _write_daemon_status({
                        "state": "error",
                        "pid": os.getpid(),
                        "loops": loops,
                        "heartbeat_at": _utc_now(),
                        "target_task_id": self.target_task_id,
                        "queue_mode": "lane",
                        "foreground_runtime_instance_id": self.foreground_runtime_instance_id,
                        "error_class": type(exc).__name__,
                        "error_summary": "Mission Daemon tick failed",
                    })
                    break
                last_tick_id = getattr(tick, "tick_id", getattr(tick, "settle_id", None))
                actions = len(tick.actions_taken)
                stop_reason = getattr(tick, "stop_reason", None)
                wait_seconds = 0 if actions and stop_reason == "max_actions" else (self.interval_seconds if actions else self.idle_interval_seconds)
                next_wake = _utc_now() + timedelta(seconds=wait_seconds)
                _write_daemon_status(_status_from_tick(tick, loops=loops, wait_seconds=wait_seconds, next_wake_at=next_wake))
                if wait_seconds > 0 and not self._stop_requested:
                    self._sleep_with_stop(wait_seconds)
        finally:
            if "heartbeat_stop" in locals():
                heartbeat_stop.set()
            if "heartbeat_thread" in locals():
                heartbeat_thread.join(timeout=1)
            _clear_daemon_lease(os.getpid())
            if self._stop_requested:
                _write_final_offline_status(os.getpid(), loops=loops, last_tick_id=last_tick_id)
            self._emit_lifecycle_event("daemon.stopped", reason="stop_requested" if self._stop_requested else "loop_bound_reached")
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
        return DaemonLoopResult(loops=loops, last_tick_id=last_tick_id, stopped=self._stop_requested).to_json()

    def _archive_terminal_target(self) -> None:
        if not self.target_task_id:
            return
        try:
            from .store import ArchiveStore, TaskStore
            from .states import TaskState

            task = TaskStore().get(self.target_task_id)
            if task.state not in {TaskState.DONE, TaskState.CANCELLED}:
                return
            ArchiveStore().archive_tasks(
                [self.target_task_id],
                actor="daemon",
                reason="auto-archive terminal foreground goal",
            )
        except Exception:
            # Archiving is best-effort cleanup; the goal outcome is already recorded
            # and `task archive` remains available to the operator.
            pass

    def _emit_lifecycle_event(self, event_type: str, *, reason: str | None = None) -> None:
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

    def _sleep_with_stop(self, wait_seconds: float) -> None:
        self.sleep_fn(wait_seconds)


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
    task_id = None
    foreground_runtime_instance_id = _safe_task_id(foreground_runtime_instance_id)
    status = read_daemon_status()
    pid = status.get("pid")
    if isinstance(pid, int) and _pid_is_alive(pid):
        existing_target = _safe_task_id(status.get("target_task_id"))
        return {"started": False, "pid": pid, "state": status.get("state", "running")}
    lease = _read_daemon_lease()
    lease_pid = lease.get("pid") if isinstance(lease, dict) else None
    if isinstance(lease_pid, int) and _pid_is_alive(lease_pid) and not _lease_expired(lease):
        return {"started": False, "pid": lease_pid, "state": "running", "error": "daemon_lease_held"}

    cmd = [sys.executable, "-m", "hermes_cli.main", "harness", "daemon", "foreground"]
    if interval_seconds is not None:
        cmd.extend(["--interval", str(interval_seconds)])
    if idle_interval_seconds is not None:
        cmd.extend(["--idle-interval", str(idle_interval_seconds)])
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if _is_windows() else 0
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
    _write_daemon_lease(proc.pid)
    _write_daemon_status({
        "state": "starting",
        "pid": proc.pid,
        "heartbeat_at": _utc_now(),
        "target_task_id": task_id,
        "queue_mode": "lane",
        "foreground_runtime_instance_id": foreground_runtime_instance_id,
    })
    return {"started": True, "pid": proc.pid, "state": "starting", "target_task_id": task_id, "queue_mode": "lane"}


def stop_daemon() -> dict:
    status = read_daemon_status()
    pid = status.get("pid")
    if not isinstance(pid, int) or not _pid_is_alive(pid):
        _write_daemon_status({"state": "offline"})
        _clear_daemon_lease(pid if isinstance(pid, int) else None)
        return {"stopped": False, "state": "offline"}
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
    _write_daemon_status({"state": "offline", "last_pid": pid})
    _clear_daemon_lease(pid)
    return {"stopped": True, "pid": pid, "state": "offline"}


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
    return status


def _write_daemon_status(status: dict) -> None:
    atomic_json_write(paths.daemon_status_path(), to_jsonable(status), indent=2, sort_keys=True)


def _safe_task_id(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if all(ch.isalnum() or ch in "_.:-" for ch in text) and len(text) <= 128:
        return text
    return None
