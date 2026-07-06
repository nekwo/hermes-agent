from pathlib import Path

from scripts.upstream_sync_gate import build_gate_commands


def test_gate_commands_include_required_harness_lanes():
    commands = build_gate_commands(
        repo_root=Path("repo"),
        launcher_root=Path("launcher"),
        include_launcher=False,
    )

    assert [command.name for command in commands] == [
        "agent_runtime_pytest",
        "hermes_cli_pytest",
        "harness_no_model_smoke",
    ]


def test_gate_commands_add_launcher_when_agent_seam_requires_it():
    commands = build_gate_commands(
        repo_root=Path("repo"),
        launcher_root=Path("launcher"),
        include_launcher=True,
    )

    assert commands[-1].name == "launcher_mission_control"


def test_rehearsal_broken_seam_runs_before_expensive_gates():
    commands = build_gate_commands(
        repo_root=Path("repo"),
        launcher_root=Path("launcher"),
        include_launcher=False,
        simulate_broken_seam=True,
    )

    assert commands[0].name == "rehearsal_broken_seam"
