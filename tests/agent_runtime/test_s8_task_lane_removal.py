"""S8 removes persisted mission tasks while preserving the board import seam."""

from __future__ import annotations


def test_task_model_is_removed_and_store_name_is_the_permanent_stub():
    from agent_runtime.store import TaskStore
    from agent_runtime.task_store_stub import TaskStoreStub

    assert TaskStore is TaskStoreStub


def test_mission_record_command_families_are_not_registered():
    import argparse

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    roots = parser.add_subparsers(dest="root")
    build_parser(roots)
    harness = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices["harness"]
    commands = next(
        action
        for action in harness._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices

    removed = {
        "goal",
        "task",
        "swarm",
        "lane",
        "run",
        "worker",
        "issue",
        "incident",
    }
    assert removed.isdisjoint(commands)

    # The chat/board/realm/runtime-graph side survives this deletion stage.
    assert {"persona", "mission-chat", "board", "realm", "workspace", "flow"} <= set(commands)
