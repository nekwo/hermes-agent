from argparse import Namespace


def test_task_create_start_daemon_passes_target_task(monkeypatch, capsys, isolate_agent_runtime_root):
    from hermes_cli import harness as harness_cli
    from agent_runtime import mission_goal

    calls = []

    def fake_start_daemon(**kwargs):
        calls.append(kwargs)
        return {
            "started": True,
            "pid": 123,
            "state": "starting",
            "target_task_id": kwargs.get("task_id"),
            "queue_mode": "lane",
        }

    # task create delegates to agent_runtime.mission_goal.create_mission_goal,
    # which calls start_daemon from the mission_goal module's namespace.
    monkeypatch.setattr(mission_goal, "start_daemon", fake_start_daemon)

    code = harness_cli._cmd_task_create(
        Namespace(
            title="Foreground daemon smoke",
            description="Create a task and start targeted daemon",
            requested_by="test",
            start_daemon=True,
            json=True,
        )
    )
    capsys.readouterr()

    assert code == 0
    assert len(calls) == 1
    assert calls[0]["task_id"].startswith("task_")
    assert calls[0]["foreground_runtime_instance_id"].startswith("goalrt_")
