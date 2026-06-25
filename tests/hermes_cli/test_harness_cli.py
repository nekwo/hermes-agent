import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_time import now
from hermes_cli.harness import build_parser
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.goal_runner import GoalRunResult
from agent_runtime.models import AgentRun, Incident, Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import IncidentStore, ProofStore, RunStore, TaskStore


def parser():
    p=argparse.ArgumentParser(); subs=p.add_subparsers(dest="command"); build_parser(subs); return p


def test_harness_parser_exposes_task_create():
    args=parser().parse_args(["harness", "task", "create", "--title", "T", "--description", "D", "--json"])
    assert args.command == "harness" and args.task_command == "create"


def test_harness_task_create_reports_new_goal_hygiene(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        "agent_runtime.mission_goal.start_daemon",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("daemon should not start by default")),
    )

    args = parser().parse_args(["harness", "task", "create", "--title", "T", "--description", "D", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["task_id"].startswith("task_")
    assert data["new_goal_hygiene"]["preserved_evidence"] is True
    assert data["new_goal_hygiene"]["dirty_state_after_cleanup"]["summary"]
    assert data["daemon_start"]["attempted"] is False
    assert data["daemon_start"]["started"] is False
    assert "harness goal run" in data["daemon_start"]["summary"]


def test_harness_task_create_can_start_daemon_with_explicit_flag(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    daemon_starts = []
    monkeypatch.setattr(
        "agent_runtime.mission_goal.start_daemon",
        lambda **kwargs: daemon_starts.append(kwargs) or {"started": True, "pid": 1234, "state": "starting"},
    )

    args = parser().parse_args(["harness", "task", "create", "--title", "T", "--description", "D", "--start-daemon", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["daemon_start"]["attempted"] is True
    assert data["daemon_start"]["started"] is True
    assert daemon_starts[0]["task_id"] == data["task_id"]
    assert daemon_starts[0]["foreground_runtime_instance_id"] == data["foreground_runtime"]["instance_id"]
    assert daemon_starts[0]["interval_seconds"] == 10
    assert daemon_starts[0]["idle_interval_seconds"] == 30


def test_harness_task_create_can_skip_daemon_start(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr("agent_runtime.mission_goal.start_daemon", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("daemon should not start")))

    args = parser().parse_args(["harness", "task", "create", "--title", "T", "--description", "D", "--no-start-daemon", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["task_id"].startswith("task_")
    assert data["daemon_start"] == {
        "attempted": False,
        "started": False,
        "summary": "disabled; use harness goal run for in-process execution or --start-daemon for daemon mode",
    }


def test_harness_parser_exposes_task_archive_ready_json():
    args = parser().parse_args(["harness", "task", "archive-ready", "--json"])
    assert args.command == "harness"
    assert args.task_command == "archive-ready"
    assert args.json is True


def test_harness_parser_exposes_goal_run():
    args = parser().parse_args(["harness", "goal", "run", "--title", "T", "--description", "D", "--json"])
    assert args.command == "harness"
    assert args.harness_command == "goal"
    assert args.goal_command == "run"
    assert args.max_actions == 16


def test_harness_goal_run_returns_controller_exit_code(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    class Controller:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_goal(self, options):
            assert options.title == "T"
            assert options.description == "D"
            assert options.max_actions == 2
            return GoalRunResult(
                ok=False,
                task_id="task_1",
                title="T",
                final_task_state="created",
                stop_reason="max_actions",
                tick_stop_reason="max_actions",
                exit_code=3,
                elapsed_seconds=0.0,
                actions_taken=0,
                ticks=1,
                run_ids=[],
                proof_ids=[],
                open_incident_ids=[],
                all_incident_ids=[],
                hygiene={},
            )

    monkeypatch.setattr("hermes_cli.harness.MissionRuntimeController", Controller)

    args = parser().parse_args(["harness", "goal", "run", "--title", "T", "--description", "D", "--max-actions", "2", "--json"])

    assert args.func(args) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["stop_reason"] == "max_actions"


def test_harness_task_archive_ready_preserves_evidence_and_removes_open_listing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    ts = TaskStore()
    rs = RunStore()
    ps = ProofStore()
    stamp = now()
    done = Task(id="task_done", title="Done mission", description="d", state=TaskState.DONE, created_at=stamp, updated_at=stamp, requested_by="tony")
    active = Task(id="task_active", title="Active mission", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="tony")
    ts.create(done)
    ts.create(active)
    run = AgentRun(id="run_done", persona_id="dev", task_id="task_done", stage_id="stage_impl", state=RunState.COMPLETED, started_at=stamp, last_heartbeat_at=stamp, finished_at=stamp)
    from utils import atomic_json_write
    from agent_runtime import paths
    atomic_json_write(paths.run_path(run.id), {"id": run.id, "persona_id": run.persona_id, "task_id": run.task_id, "stage_id": run.stage_id, "state": run.state.value, "started_at": stamp.isoformat(), "last_heartbeat_at": stamp.isoformat(), "finished_at": stamp.isoformat()})
    ps.attach(Proof(id="proof_done", task_id="task_done", stage_id="stage_impl", type=ProofType.TEST_RUN, title="pytest", path_or_value="pytest.log", created_by="dev", created_at=stamp, metadata={"status": "passed", "exit_code": 0}))

    args = parser().parse_args(["harness", "task", "archive-ready", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["archived_task_ids"] == ["task_done"]
    assert data["skipped_task_ids"] == []
    assert data["archive_dir"]
    archive_dir = tmp_path / "runtime" / "deleted_archive" / data["archive_batch"]
    assert (archive_dir / "manifest.json").exists()
    assert (archive_dir / "tasks" / "task_done.json").exists()
    assert (archive_dir / "runs" / "run_done.json").exists()
    assert (archive_dir / "proofs" / "task_done" / "proof_proof_done.json").exists()
    assert not (tmp_path / "runtime" / "tasks" / "task_done.json").exists()
    assert (tmp_path / "runtime" / "tasks" / "task_active.json").exists()
    assert [task.id for task in ts.list_open()] == ["task_active"]


def test_harness_task_archive_refuses_active_task_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    stamp = now()
    TaskStore().create(Task(id="task_active", title="Active mission", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="tony"))

    args = parser().parse_args(["harness", "task", "archive", "task_active", "--json"])

    assert args.func(args) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["archived_task_ids"] == []
    assert data["skipped_task_ids"] == ["task_active"]
    assert data["archive_batch"] is None
    assert data["skipped_tasks"][0]["reason"] == "not_terminal"
    assert (tmp_path / "runtime" / "tasks" / "task_active.json").exists()
    assert not (tmp_path / "runtime" / "deleted_archive").exists()


def test_harness_task_cancel_cancels_active_runs_for_archive_cleanup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    stamp = now()
    TaskStore().create(Task(id="task_cancel", title="Cancel mission", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="tony"))
    run = RunStore().open_run("dev", "task_cancel", stage_id="stage_1", session_id="session_budget")

    args = parser().parse_args(["harness", "task", "cancel", "task_cancel", "--reason", "operator cleanup", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["cancelled_run_ids"] == [run.id]
    assert RunStore().get(run.id).state == RunState.CANCELLED

    archive_args = parser().parse_args(["harness", "task", "archive-ready", "--json"])
    assert archive_args.func(archive_args) == 0
    archived = json.loads(capsys.readouterr().out)
    assert archived["archived_task_ids"] == ["task_cancel"]
    assert archived["skipped_task_ids"] == []


def test_harness_task_show_can_include_task_scoped_events(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    stamp = now()
    TaskStore().create(Task(id="task_events", title="Event mission", description="d", state=TaskState.CREATED, created_at=stamp, updated_at=stamp, requested_by="tony"))

    args = parser().parse_args(["harness", "task", "show", "task_events", "--events", "5", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["task"]["id"] == "task_events"
    assert data["events"]["ok"] is True
    assert data["events"]["count"] == 1
    assert data["events"]["items"][0]["type"] == "task.created"


def test_harness_task_history_returns_event_envelope(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    stamp = now()
    TaskStore().create(Task(id="task_history", title="History mission", description="d", state=TaskState.CREATED, created_at=stamp, updated_at=stamp, requested_by="tony"))

    args = parser().parse_args(["harness", "task", "history", "task_history", "--limit", "10", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["task_id"] == "task_history"
    assert data["archived"] is False
    assert data["event_count"] == 1
    assert data["events"][0]["type"] == "task.created"


def test_harness_run_show_returns_run_proofs_and_events(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    stamp = now()
    TaskStore().create(Task(id="task_run_show", title="Run show mission", description="d", state=TaskState.RUNNING, created_at=stamp, updated_at=stamp, requested_by="tony"))
    run = RunStore().open_run("dev", "task_run_show", stage_id="stage_impl", session_id="session_run_show")
    ProofStore().attach(
        Proof(
            id="proof_run_show",
            task_id="task_run_show",
            stage_id="stage_impl",
            type=ProofType.TEST_RUN,
            title="pytest",
            path_or_value="pytest.log",
            created_by="dev",
            created_at=stamp,
            metadata={"status": "passed", "exit_code": 0, "run_id": run.id},
        )
    )

    args = parser().parse_args(["harness", "run", "show", run.id, "--events", "20", "--json"])

    assert args.func(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True
    assert data["run"]["id"] == run.id
    assert data["proofs"][0]["id"] == "proof_run_show"
    assert any(item["type"] == "run.opened" for item in data["events"]["items"])


def test_harness_parser_exposes_daemon_start_status_stop():
    p = parser()
    assert p.parse_args(["harness", "daemon", "start", "--json"]).daemon_command == "start"
    assert p.parse_args(["harness", "daemon", "status", "--json"]).daemon_command == "status"
    assert p.parse_args(["harness", "daemon", "stop", "--json"]).daemon_command == "stop"
    assert p.parse_args(["harness", "daemon", "run-once", "--json"]).daemon_command == "run-once"


def test_harness_parser_exposes_config_migrate_and_verify():
    p = parser()
    assert p.parse_args(["harness", "config", "show", "--json"]).config_command == "show"
    assert p.parse_args(["harness", "migrate", "--check", "--json"]).harness_command == "migrate"
    verify = p.parse_args(["harness", "verify", "--mode", "temp-root", "--skip-tests", "--json"])
    assert verify.harness_command == "verify"
    assert verify.mode == "temp-root"
    assert verify.skip_tests is True
    burn = p.parse_args(["harness", "burn-in", "run", "noop-orchestration", "--max-actions", "3", "--json"])
    assert burn.harness_command == "burn-in"
    assert burn.burn_in_command == "run"
    assert burn.case_id == "noop-orchestration"
    assert burn.max_actions == 3


def test_harness_config_show_and_migrate_check_are_redaction_safe(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    args = parser().parse_args(["harness", "config", "show", "--json"])
    assert args.func(args) == 0
    config_data = json.loads(capsys.readouterr().out)
    assert config_data["validation"]["ok"] is True
    assert config_data["schema_version"] == 1
    assert "store_root" in config_data

    args = parser().parse_args(["harness", "migrate", "--check", "--json"])
    assert args.func(args) == 0
    migration_data = json.loads(capsys.readouterr().out)
    assert migration_data["pending"] is False
    assert migration_data["check_only"] is True


def test_harness_verify_skip_tests_emits_proof_packet(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    args = parser().parse_args(["harness", "verify", "--mode", "temp-root", "--skip-tests", "--json"])

    assert args.func(args) == 0
    packet = json.loads(capsys.readouterr().out)
    assert packet["schema_version"] == 1
    assert packet["mode"] == "temp-root"
    assert packet["runtime_root"] == str(tmp_path / "runtime")
    assert {item["label"] for item in packet["commands"]} >= {"harness status", "harness snapshot", "harness config show"}
    assert packet["tests"] == []
    assert packet["runtime_config"]["validation"]["ok"] is True


def test_harness_burn_in_run_returns_nonzero_for_blocked_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        "hermes_cli.harness.run_burn_in_case",
        lambda *args, **kwargs: {
            "burn_id": "burn_1",
            "case_id": args[0],
            "status": "blocked",
            "failure_class": "run_stalled",
        },
    )

    args = parser().parse_args(["harness", "burn-in", "run", "noop-orchestration", "--json"])

    assert args.func(args) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "blocked"
    assert data["failure_class"] == "run_stalled"


def test_harness_burn_in_summarize_returns_nonzero_when_evidence_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        "hermes_cli.harness.summarize_burn_in",
        lambda burn_id: {
            "burn_id": burn_id,
            "ok": False,
            "status": "blocked",
            "failure_class": "incomplete_evidence",
            "missing_files": ["snapshot_after.json"],
        },
    )

    args = parser().parse_args(["harness", "burn-in", "summarize", "burn_1", "--json"])

    assert args.func(args) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["missing_files"] == ["snapshot_after.json"]


def test_harness_burn_in_summarize_missing_ledger_returns_clean_json(capsys):
    args = parser().parse_args(["harness", "burn-in", "summarize", "missing_burn", "--json"])

    assert args.func(args) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["error"] == "FileNotFoundError"


def test_harness_process_exit_code_propagates_for_burn_in_failure(tmp_path):
    env = os.environ.copy()
    env["HERMES_AGENT_RUNTIME_ROOT"] = str(tmp_path / "runtime")
    repo = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "harness", "burn-in", "summarize", "missing_burn", "--json"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 2
    data = json.loads(completed.stdout)
    assert data["ok"] is False
    assert "Traceback" not in completed.stderr


def test_harness_parser_exposes_observe():
    args = parser().parse_args(["harness", "observe", "--json"])
    assert args.command == "harness" and args.harness_command == "observe"


def test_harness_parser_exposes_smoke():
    args = parser().parse_args(["harness", "smoke", "--json", "--temp-root", "--no-model"])
    assert args.command == "harness" and args.harness_command == "smoke"
    assert args.temp_root is True and args.no_model is True


def test_harness_incident_close_closes_incident_with_reason(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    store = IncidentStore()
    store.open(Incident(id="inc_1", task_id="task_1", run_id="run_1", kind="runtime_dependency_missing", summary="missing openai", detail_path=None, opened_at=now()))

    args = parser().parse_args(["harness", "incident", "close", "inc_1", "--reason", "wrong interpreter smoke", "--json"])

    assert args.func(args) == 0
    closed = store.get("inc_1")
    assert closed.closed_at is not None
    data = json.loads(capsys.readouterr().out)
    assert data["incident_id"] == "inc_1"
    assert data["closed"] is True
    assert data["reason"] == "wrong interpreter smoke"


def test_harness_parser_has_no_import_kanban():
    p=parser()
    try:
        p.parse_args(["harness", "import-kanban", "x"])
    except SystemExit as e:
        assert e.code != 0
    else:
        raise AssertionError("import-kanban unexpectedly parsed")


def test_harness_cli_init_create_tick_status_snapshot_e2e(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    class Runtime:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_tick(self, persona, ctx, *, run):
            return AgentDecision(
                type=DecisionType.PROPOSE_ACCEPTANCE,
                summary="pm fleshed",
                rationale="r",
                payload={"objective": "obj", "acceptance_criteria": ["done"]},
            )

    monkeypatch.setattr("hermes_cli.harness.GPTPersonaRuntime", Runtime)

    for argv in [
        ["harness", "init", "--json"],
        ["harness", "task", "create", "--title", "T", "--description", "D", "--json"],
        ["harness", "tick", "--json"],
        ["harness", "status", "--json"],
        ["harness", "observe", "--json"],
        ["harness", "snapshot", "--json"],
    ]:
        args = parser().parse_args(argv)
        assert args.func(args) == 0

    output = capsys.readouterr().out
    assert "pm fleshed" in output
    assert (tmp_path / "runtime" / "snapshot.json").exists()
