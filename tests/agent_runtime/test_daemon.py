import json
from pathlib import Path
from datetime import datetime, timezone

from hermes_time import now

from agent_runtime.daemon import MissionDaemon, read_daemon_status
from agent_runtime.models import Task
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import TaskStore
from agent_runtime.ticker import TickResult


class NoopEngine:
    def __init__(self, *, actions_per_tick):
        self.actions_per_tick = actions_per_tick
        self.calls = 0

    def tick_once(self):
        self.calls += 1
        count = self.actions_per_tick[self.calls - 1]
        return TickResult(
            tick_id=f"tick_{self.calls}",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            tasks_seen=1,
            actions_taken=[object()] * count,
        )


class SettledEngine:
    def __init__(self, *, stop_reason="task_terminal"):
        self.calls = 0
        self.stop_reason = stop_reason

    def run_until_settled(self, *, task_id=None, max_actions=None):
        self.calls += 1
        from agent_runtime.actions import HarnessAction, HarnessActionResult, HarnessActionType
        from agent_runtime.ticker import RunUntilSettledResult

        return RunUntilSettledResult(
            settle_id=f"settle_{self.calls}",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            ticks=2,
            actions_taken=[
                HarnessActionResult(HarnessAction(HarnessActionType.RUN_SLOT, "task_1", slot_id="neko_supervisor"), True, "scoped"),
                HarnessActionResult(HarnessAction(HarnessActionType.RUN_SLOT, "task_1", slot_id="dev"), True, "proved"),
            ],
            stop_reason=self.stop_reason,
        )


class TargetRecordingEngine:
    def __init__(self):
        self.calls = []

    def run_until_settled(self, *, task_id=None, max_actions=None):
        self.calls.append({"task_id": task_id, "max_actions": max_actions})
        from agent_runtime.ticker import RunUntilSettledResult

        return RunUntilSettledResult(
            settle_id="settle_target",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            ticks=1,
            actions_taken=[],
            stop_reason="no_eligible_action",
            task_id=task_id,
        )


class TargetAndQueueEngine:
    def __init__(self):
        self.calls = []

    def run_until_settled(self, *, task_id=None, max_actions=None):
        self.calls.append({"kind": "target", "task_id": task_id, "max_actions": max_actions})
        from agent_runtime.ticker import RunUntilSettledResult

        return RunUntilSettledResult(
            settle_id="settle_target",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            ticks=1,
            actions_taken=[],
            stop_reason="no_eligible_action",
            task_id=task_id,
        )

    def tick_once(self):
        self.calls.append({"kind": "queue", "task_id": None, "max_actions": None})
        from agent_runtime.actions import HarnessAction, HarnessActionResult, HarnessActionType

        return TickResult(
            tick_id="tick_queue",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
            tasks_seen=2,
            actions_taken=[
                HarnessActionResult(
                    HarnessAction(HarnessActionType.RUN_SLOT, "task_new", slot_id="neko_supervisor"),
                    True,
                    "queued task scoped",
                )
            ],
        )


class FailingEngine:
    def __init__(self):
        self.calls = 0

    def run_until_settled(self, *, task_id=None, max_actions=None):
        self.calls += 1
        raise AssertionError("daemon should settle terminal target before ticking")


def test_daemon_uses_settled_loop_and_records_compact_stop_reason(isolate_agent_runtime_root):
    engine = SettledEngine()
    daemon = MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0)

    result = daemon.run_foreground(max_loops=1)
    status = read_daemon_status()

    assert result["loops"] == 1
    assert engine.calls == 1
    assert status["last_tick_id"] == "settle_1"
    assert status["actions_last_tick"] == 2
    assert status["settle_stop_reason"] == "task_terminal"


def test_daemon_foreground_uses_target_task_id(isolate_agent_runtime_root):
    engine = TargetRecordingEngine()
    daemon = MissionDaemon(engine_factory=lambda: engine, target_task_id="task_new", interval_seconds=0, idle_interval_seconds=0)

    daemon.run_foreground(max_loops=1)
    status = read_daemon_status()

    assert engine.calls == [{"task_id": "task_new", "max_actions": 10}]
    assert status["target_task_id"] == "task_new"
    assert status["queue_mode"] == "lane"


def test_targeted_daemon_services_open_queue_after_target_pass(isolate_agent_runtime_root):
    engine = TargetAndQueueEngine()
    daemon = MissionDaemon(engine_factory=lambda: engine, target_task_id="task_existing", interval_seconds=0, idle_interval_seconds=0)

    daemon.run_foreground(max_loops=1)
    status = read_daemon_status()

    assert engine.calls == [
        {"kind": "target", "task_id": "task_existing", "max_actions": 10},
        {"kind": "queue", "task_id": None, "max_actions": None},
    ]
    assert status["actions_last_tick"] == 1
    assert status["settle_stop_reason"] == "background_progress"
    assert status["services_open_tasks"] is True


def test_targeted_daemon_exits_when_target_reaches_terminal_boundary(isolate_agent_runtime_root):
    engine = SettledEngine(stop_reason="task_terminal")
    sleeps = []
    daemon = MissionDaemon(engine_factory=lambda: engine, target_task_id="task_done", interval_seconds=10, idle_interval_seconds=30, sleep_fn=sleeps.append)

    result = daemon.run_foreground(max_loops=3)
    status = read_daemon_status()

    assert result["loops"] == 1
    assert result["stopped"] is True
    assert engine.calls == 1
    assert sleeps == []
    assert status["target_task_id"] == "task_done"
    assert status["settle_stop_reason"] == "task_terminal"


def test_daemon_does_not_sleep_active_interval_when_settled_batch_hits_action_cap(isolate_agent_runtime_root):
    engine = SettledEngine(stop_reason="max_actions")
    sleeps = []
    daemon = MissionDaemon(engine_factory=lambda: engine, interval_seconds=10, idle_interval_seconds=30, sleep_fn=sleeps.append)

    daemon.run_foreground(max_loops=1)
    status = read_daemon_status()

    assert status["settle_stop_reason"] == "max_actions"
    assert status["wait_seconds"] == 0
    assert sleeps == []


def test_daemon_runs_foreground_loop_and_records_heartbeat(isolate_agent_runtime_root):
    engine = NoopEngine(actions_per_tick=[1, 0])
    daemon = MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0)

    result = daemon.run_foreground(max_loops=2)
    status = read_daemon_status()

    assert result["loops"] == 2
    assert engine.calls == 2
    assert status["state"] == "idle"
    assert status["last_tick_id"] == "tick_2"
    assert status["loops"] == 2
    assert status["actions_last_tick"] == 0
    assert isinstance(status["pid"], int)
    assert "next_wake_at" in status


def test_status_includes_daemon_health(isolate_agent_runtime_root):
    engine = NoopEngine(actions_per_tick=[0])
    MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)

    from agent_runtime.status import build_status

    status = build_status(task_store=TaskStore())
    assert status["daemon"]["state"] == "idle"
    assert status["daemon"]["last_tick_id"] == "tick_1"


def test_snapshot_includes_daemon_health(isolate_agent_runtime_root):
    engine = NoopEngine(actions_per_tick=[0])
    MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)
    ts = now()
    task_store = TaskStore()
    task_store.create(Task(id="m", title="M", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="human"))

    from agent_runtime.snapshot import build_snapshot

    snapshot = build_snapshot(task_store=task_store)
    assert snapshot["daemon"]["state"] == "idle"
    assert snapshot["daemon"]["last_tick_id"] == "tick_1"


def test_daemon_start_does_not_spawn_duplicate_when_pid_alive(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "running", "pid": 1234})
    spawned = []
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda *args, **kwargs: spawned.append((args, kwargs)))

    result = daemon_mod.start_daemon()

    assert result["started"] is False
    assert result["pid"] == 1234
    assert spawned == []


def test_daemon_start_reports_target_conflict_when_existing_daemon_is_untargeted(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "running", "pid": 1234, "queue_mode": "lane"})
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 1234)

    result = daemon_mod.start_daemon(task_id="task_new")

    assert result["started"] is False
    assert result["error"] == "daemon_target_conflict"
    assert result["requested_task_id"] == "task_new"


def test_daemon_start_reuses_live_daemon_that_services_open_tasks(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "running", "pid": 1234, "target_task_id": "task_existing", "queue_mode": "lane", "services_open_tasks": True})
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 1234)

    result = daemon_mod.start_daemon(task_id="task_new")

    assert result["started"] is False
    assert result["pid"] == 1234
    assert result["target_task_id"] == "task_existing"
    assert result["requested_task_id"] == "task_new"
    assert result["will_service_open_tasks"] is True
    assert "error" not in result


def test_daemon_start_records_spawned_pid(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    class Proc:
        pid = 5678

    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 5678)
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda *args, **kwargs: Proc())

    result = daemon_mod.start_daemon(task_id="task_new", interval_seconds=0, idle_interval_seconds=0)
    status = read_daemon_status()

    assert result["started"] is True
    assert result["pid"] == 5678
    assert result["target_task_id"] == "task_new"
    assert result["queue_mode"] == "lane"
    assert status["state"] == "starting"
    assert status["pid"] == 5678
    assert status["target_task_id"] == "task_new"
    assert status["queue_mode"] == "lane"
    assert status["services_open_tasks"] is True


def test_daemon_start_spawns_process_with_task_argument(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    spawned = []

    class Proc:
        pid = 7777

    def fake_popen(cmd, **kwargs):
        spawned.append(list(cmd))
        return Proc()

    monkeypatch.setattr(daemon_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 7777)

    result = daemon_mod.start_daemon(task_id="task_target")

    assert result["started"] is True
    cmd = spawned[0]
    assert "--task" in cmd
    assert cmd[cmd.index("--task") + 1] == "task_target"


def test_daemon_start_refuses_live_lease_before_spawning(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    spawned = []
    daemon_mod._write_daemon_lease(2468)
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 2468)
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda *args, **kwargs: spawned.append((args, kwargs)))

    result = daemon_mod.start_daemon(task_id="task_new")

    assert result["started"] is False
    assert result["error"] == "daemon_lease_held"
    assert result["pid"] == 2468
    assert spawned == []


def test_daemon_foreground_exits_when_status_is_owned_by_other_live_pid(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    engine = NoopEngine(actions_per_tick=[1])
    daemon_mod._write_daemon_status({"state": "running", "pid": 4321, "target_task_id": "task_other"})
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 4321)

    result = MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)
    status = read_daemon_status()

    assert result["loops"] == 0
    assert result["stopped"] is True
    assert engine.calls == 0
    assert status["pid"] == 4321
    assert status["target_task_id"] == "task_other"


def test_daemon_foreground_rechecks_owner_after_status_registration(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    engine = NoopEngine(actions_per_tick=[1])
    writes = []
    original_write = daemon_mod._write_daemon_status

    def write_with_race(status):
        original_write(status)
        writes.append(status)
        if status.get("state") == "running":
            original_write({"state": "running", "pid": 9876, "target_task_id": "task_other"})

    monkeypatch.setattr(daemon_mod, "_write_daemon_status", write_with_race)
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 9876)

    result = MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)
    status = read_daemon_status()

    assert result["loops"] == 1
    assert result["stopped"] is True
    assert engine.calls == 0
    assert status["pid"] == 9876


def test_read_daemon_status_reports_offline_for_dead_pid(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "idle", "pid": 2468})
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: False)

    status = read_daemon_status()

    assert status["state"] == "offline"
    assert status["last_pid"] == 2468
    assert status["cleared_reason"] == "dead_pid"


class RaisingEngine:
    def tick_once(self):
        raise RuntimeError("boom SECRET_TOKEN should be redacted")


def test_daemon_writes_error_status_when_tick_raises(isolate_agent_runtime_root):
    daemon = MissionDaemon(engine_factory=RaisingEngine, interval_seconds=0, idle_interval_seconds=0)

    result = daemon.run_foreground(max_loops=1)
    status = read_daemon_status()

    assert result["loops"] == 1
    assert status["state"] == "error"
    assert status["error_class"] == "RuntimeError"
    assert "SECRET_TOKEN" not in str(status)
    assert "heartbeat_at" in status


def test_daemon_stop_does_not_report_offline_while_pid_survives(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "running", "pid": 1234})
    kills = []
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 1234)
    monkeypatch.setattr(daemon_mod.subprocess, "run", lambda *args, **kwargs: kills.append(args[0]))
    monkeypatch.setattr(daemon_mod.time, "sleep", lambda *_: None)
    monkeypatch.setattr(daemon_mod, "_wait_for_pid_exit", lambda pid, timeout_seconds: False)
    monkeypatch.setattr(daemon_mod, "_is_windows", lambda: True)

    result = daemon_mod.stop_daemon()
    status = read_daemon_status()

    assert result["stopped"] is False
    assert result["error"] == "daemon_pid_survived_stop"
    assert status["state"] != "offline"
    assert any("/F" in cmd for cmd in kills if isinstance(cmd, list))


def test_daemon_stop_escalates_to_force_kill_and_reports_offline(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "running", "pid": 1234})
    alive = {"value": True}
    kills = []

    def fake_run(cmd, **kwargs):
        kills.append(cmd)
        if "/F" in cmd:
            alive["value"] = False

    waits = iter([False, True])
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: alive["value"] and pid == 1234)
    monkeypatch.setattr(daemon_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(daemon_mod, "_wait_for_pid_exit", lambda pid, timeout_seconds: next(waits))
    monkeypatch.setattr(daemon_mod, "_is_windows", lambda: True)

    result = daemon_mod.stop_daemon()
    status = read_daemon_status()

    assert result["stopped"] is True
    assert status["state"] == "offline"
    assert status["last_pid"] == 1234


def test_daemon_writes_final_offline_status_on_clean_exit(isolate_agent_runtime_root):
    engine = SettledEngine(stop_reason="task_terminal")
    daemon = MissionDaemon(engine_factory=lambda: engine, target_task_id="task_gone", interval_seconds=0, idle_interval_seconds=0)

    daemon.run_foreground(max_loops=3)
    status = read_daemon_status()

    assert status["state"] == "offline"
    assert status["last_pid"] == __import__("os").getpid()
    assert "stopped_at" in status


def test_daemon_exit_does_not_clobber_status_owned_by_other_live_pid(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    engine = SettledEngine(stop_reason="no_eligible_action")
    daemon_mod._write_daemon_status({"state": "running", "pid": 4321})
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 4321)

    MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)
    status = read_daemon_status()

    assert status["pid"] == 4321
    assert status["state"] == "running"


def test_lease_held_start_does_not_clobber_owner_status(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "running", "pid": 4321})
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(daemon_mod, "_acquire_daemon_lease", lambda pid: {"acquired": False, "pid": 4321})

    engine = SettledEngine()
    result = MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)
    status = read_daemon_status()

    assert result["loops"] == 0
    assert status["pid"] == 4321
    assert status["state"] == "running"
    assert engine.calls == 0


def test_targeted_daemon_archives_terminal_task_on_exit(isolate_agent_runtime_root):
    from agent_runtime.store import TaskStore

    ts = now()
    store = TaskStore()
    store.create(Task(id="task_term", title="T", description="d", state=TaskState.DONE, created_at=ts, updated_at=ts, requested_by="human"))
    engine = SettledEngine(stop_reason="task_terminal")
    daemon = MissionDaemon(engine_factory=lambda: engine, target_task_id="task_term", interval_seconds=0, idle_interval_seconds=0)

    daemon.run_foreground(max_loops=3)

    remaining = [t.id for t in store.list_all()]
    assert "task_term" not in remaining


def test_targeted_daemon_settles_cancelled_target_before_tick(isolate_agent_runtime_root):
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import RunStore
    from agent_runtime.worker_sessions import WorkerSessionStore

    ts = now()
    store = TaskStore()
    store.create(Task(id="task_cancelled_target", title="T", description="d", state=TaskState.CANCELLED, created_at=ts, updated_at=ts, requested_by="human"))
    runs = RunStore()
    run = runs.open_run("dev", "task_cancelled_target", stage_id="implement")
    run.state = RunState.WAITING_ON_APPROVAL
    runs.update(run)
    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    workers = WorkerSessionStore()
    worker = workers.open(task_id="task_cancelled_target", persona=persona, stage_id="implement")
    engine = FailingEngine()
    daemon = MissionDaemon(engine_factory=lambda: engine, target_task_id="task_cancelled_target", interval_seconds=0, idle_interval_seconds=0)

    result = daemon.run_foreground(max_loops=3)
    status = read_daemon_status()

    assert result["loops"] == 1
    assert result["stopped"] is True
    assert engine.calls == 0
    assert status["state"] == "offline"
    assert status["settle_stop_reason"] == "task_cancelled"
    assert status["target_cleanup"]["cancelled_run_ids"] == [run.id]
    assert status["target_cleanup"]["closed_worker_session_ids"] == [worker.id]
    archive_dir = Path(status["target_cleanup"]["archive_result"]["archive_dir"])
    assert archive_dir.is_dir()
    archived_run = json.loads((archive_dir / "runs" / f"{run.id}.json").read_text(encoding="utf-8"))
    archived_worker = json.loads((archive_dir / "worker_sessions" / f"{worker.id}.json").read_text(encoding="utf-8"))
    assert archived_run["state"] == "cancelled"
    assert archived_worker["state"] == "closed"
    assert "task_cancelled_target" not in [task.id for task in store.list_all()]


def test_daemon_stop_reaps_orphaned_active_run_when_daemon_already_dead(isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod
    from agent_runtime.states import RunState
    from agent_runtime.store import RunStore

    runs = RunStore()
    orphan = runs.open_run("neko_supervisor", "task_orphan", stage_id="scope", session_id="session_orphan")

    result = daemon_mod.stop_daemon()

    assert orphan.id in result["orphan_runs_cancelled"]
    reaped = runs.get(orphan.id)
    assert reaped.state == RunState.CANCELLED


def test_daemon_start_reaps_orphan_immediately(isolate_agent_runtime_root):
    from agent_runtime.states import RunState
    from agent_runtime.store import RunStore

    runs = RunStore()
    orphan = runs.open_run("dev", "task_orphan2", stage_id="implement", session_id="session_orphan2")
    engine = SettledEngine(stop_reason="no_eligible_action")

    MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)

    reaped = runs.get(orphan.id)
    assert reaped.state == RunState.CANCELLED


def test_waiting_on_approval_run_survives_daemon_reap(isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod
    from agent_runtime.states import RunState
    from agent_runtime.store import RunStore

    runs = RunStore()
    waiting = runs.open_run("dev", "task_waiting", stage_id="implement", session_id="session_waiting")
    waiting.state = RunState.WAITING_ON_APPROVAL
    runs.update(waiting)

    result = daemon_mod.stop_daemon()

    assert result["orphan_runs_cancelled"] == []
    assert runs.get(waiting.id).state == RunState.WAITING_ON_APPROVAL


def test_loop_status_rewrite_preserves_liveness_block(isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    daemon_mod._write_daemon_status({"state": "idle", "liveness": {"enabled": True, "checked_runs": 3, "warnings": 0, "hung_runs": 0}})
    engine = SettledEngine(stop_reason="no_eligible_action")

    MissionDaemon(engine_factory=lambda: engine, interval_seconds=0, idle_interval_seconds=0).run_foreground(max_loops=1)
    status = read_daemon_status()

    assert status["liveness"] == {"enabled": True, "checked_runs": 3, "warnings": 0, "hung_runs": 0}


def test_untargeted_daemon_start_adopts_active_foreground_lane(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod
    from agent_runtime.runtime_instances import GoalRuntimeInstanceStore

    ts = now()
    task_store = TaskStore()
    task_store.create(Task(id="task_stale", title="Stale backlog", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="human"))
    task_store.create(Task(id="task_fresh", title="Fresh goal", description="d", state=TaskState.CREATED, created_at=ts, updated_at=ts, requested_by="human"))
    lane = GoalRuntimeInstanceStore().create_lane(task_id="task_fresh", started_by="test", state="running")

    class _Proc:
        pid = 4321

    spawned = []

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        return _Proc()

    # Only the fake spawned daemon pid counts as alive, so the pre-start check
    # sees no live daemon and the post-start status read does not collapse to offline.
    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: pid == _Proc.pid)
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", fake_popen)

    result = daemon_mod.start_daemon()

    assert result["started"] is True
    assert result["target_task_id"] == "task_fresh"
    assert result["target_source"] == "foreground_lane"
    assert result["queue_mode"] == "lane"
    assert any("--task" in cmd and "task_fresh" in cmd for cmd in spawned)
    status = daemon_mod.read_daemon_status()
    assert status["target_task_id"] == "task_fresh"
    assert status["foreground_runtime_instance_id"] == lane.id


def test_untargeted_daemon_start_without_lane_stays_lane_mode(monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import daemon as daemon_mod

    class _Proc:
        pid = 4322

    monkeypatch.setattr(daemon_mod, "_pid_is_alive", lambda pid: False)
    monkeypatch.setattr(daemon_mod.subprocess, "Popen", lambda cmd, **kwargs: _Proc())

    result = daemon_mod.start_daemon()

    assert result["started"] is True
    assert result["target_task_id"] is None
    assert result["target_source"] is None
    assert result["queue_mode"] == "lane"


def test_active_foreground_skips_lanes_for_terminal_tasks(isolate_agent_runtime_root):
    from agent_runtime.runtime_instances import GoalRuntimeInstanceStore

    ts = now()
    task_store = TaskStore()
    task_store.create(Task(id="task_done", title="Done goal", description="d", state=TaskState.DONE, created_at=ts, updated_at=ts, requested_by="human"))
    store = GoalRuntimeInstanceStore()
    store.create_lane(task_id="task_done", started_by="test", state="running")

    assert store.active_foreground() is None
