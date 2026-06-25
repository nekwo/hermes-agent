"""Shared, in-process creation of a real Mission Control goal.

This is the single source of truth for "create a live harness goal": the CLI
``harness task create`` handler and the operator-chat ``mission_goal_create``
tool both call :func:`create_mission_goal`. Keeping it here (in ``agent_runtime``,
not ``hermes_cli``) lets the chat tool create a real, self-driving goal fully
in-process — it never shells out to the ``hermes`` CLI, which can hit the
``agent.log`` rotation lock when one Hermes process invokes another.

Creating a new foreground goal runs the standard new-goal hygiene (parks other
open goals, preempts background runs) exactly as the CLI does, so a goal created
from chat behaves identically to one created from the terminal or Assign Work.
"""

from __future__ import annotations

import uuid
from typing import Any

from hermes_time import now

from .cli_format import task_summary
from .config import load_agent_runtime_config
from .daemon import start_daemon
from .default_plan import ensure_default_mission_plan
from .goal_hygiene import (
    activate_foreground_runtime,
    prepare_new_goal_runtime,
    repo_clean_baseline_from_hygiene,
)
from .launcher_process_hygiene import launcher_visual_cleanup_needed
from .models import Task
from .states import TaskState
from .store import TaskStore

DEFAULT_GOAL_REQUESTED_BY = "mission-control-chat"


def create_mission_goal(
    *,
    title: str,
    description: str,
    requested_by: str = DEFAULT_GOAL_REQUESTED_BY,
    start_daemon_mode: bool | None = None,
    config: Any | None = None,
) -> dict[str, Any]:
    """Create a real Mission Control goal and (optionally) start the daemon.

    ``start_daemon_mode`` mirrors the CLI's tri-state ``--start-daemon`` flag:
    ``True`` forces the targeted daemon on, ``False`` leaves the goal for manual
    ticking, and ``None`` defers to ``task_create_auto_start_daemon`` config.

    Returns the task summary augmented with ``new_goal_hygiene``,
    ``foreground_runtime`` and ``daemon_start`` — the same payload the CLI emits.
    """

    config = config or load_agent_runtime_config()
    cleanup_launcher_visual = launcher_visual_cleanup_needed(title, description)
    hygiene = prepare_new_goal_runtime(
        cleanup_stage47_temp=False,
        cleanup_launcher_visual_processes=cleanup_launcher_visual,
        heartbeat_ttl_seconds=config.heartbeat_ttl_seconds,
        foreground_mode=True,
        park_open_tasks=True,
        preempt_background_runs=True,
    )
    ts = now()
    task = Task(
        id=f"task_{uuid.uuid4().hex[:8]}",
        title=title,
        description=description,
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by=requested_by,
    )
    ensure_default_mission_plan(task)
    task.harness_self_heal["repo_clean_baseline"] = repo_clean_baseline_from_hygiene(hygiene)
    TaskStore().create(task)
    foreground_runtime = activate_foreground_runtime(
        task.id, started_by=requested_by or DEFAULT_GOAL_REQUESTED_BY
    )
    daemon_start = start_daemon_for_new_goal(
        config,
        task_id=task.id,
        start_daemon_mode=start_daemon_mode,
        foreground_runtime_instance_id=foreground_runtime.get("instance_id"),
    )
    data = task_summary(task)
    data["new_goal_hygiene"] = hygiene
    data["foreground_runtime"] = foreground_runtime
    data["daemon_start"] = daemon_start
    return data


def start_daemon_for_new_goal(
    config: Any,
    *,
    task_id: str,
    start_daemon_mode: bool | None,
    foreground_runtime_instance_id: str | None = None,
) -> dict[str, Any]:
    """Start the Mission Daemon for a freshly created goal, tolerant of failures.

    A daemon failure never raises — the task is already created; the caller and
    operator just need to know it requires a manual daemon start or ticks.
    """

    requested = start_daemon_mode
    if requested is None:
        requested = bool(getattr(config, "task_create_auto_start_daemon", False))
    if not requested:
        return {
            "attempted": False,
            "started": False,
            "summary": "disabled; use harness goal run for in-process execution or --start-daemon for daemon mode",
        }
    try:
        result = start_daemon(
            task_id=task_id,
            foreground_runtime_instance_id=foreground_runtime_instance_id,
            interval_seconds=getattr(config, "daemon_interval_seconds", None),
            idle_interval_seconds=getattr(config, "daemon_idle_interval_seconds", None),
        )
    except Exception as exc:
        return {
            "attempted": True,
            "started": False,
            "ok": False,
            "error_class": type(exc).__name__,
            "summary": "daemon start failed; task was created and requires manual daemon start or ticks",
        }
    if result.get("error") == "daemon_target_conflict":
        return {"attempted": True, "ok": False, "summary": "daemon already driving another foreground task", **result}
    summary = "started" if result.get("started") else f"already {result.get('state', 'running')}"
    return {"attempted": True, "ok": True, "summary": summary, **result}
