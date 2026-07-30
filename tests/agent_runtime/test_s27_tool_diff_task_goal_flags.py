"""S27 removes ``persona tool-diff --task`` / ``--goal`` -- the FLAGS only.

S26 deliberately left these out of its scope ("a different command with its own
flags"). Tracing them settles what is actually dead:

**The flags are dead.** They are the only writers of ``args.task_id`` /
``args.goal_id`` on this verb, and the only consumer is
``_cmd_persona_tool_diff``. Nothing emits them: the Launcher's
``persona.permission_preview`` argv builder
(``mission_control_bridge.dart:2452``) spells this verb as
``harness persona tool-diff <id> [--session-id X] --permission-mode M
[--repo-scope R] --json`` and has never carried a task or goal argument. That
leaves a human typing ``--task`` by hand to correlate the preview against a
mission record deleted in S8.

**The threading behind them is NOT dead, so it stays.** The plan for this wave
allowed removing ``ToolVisibilityOptions.task_id`` / ``.goal_id`` "only if the
inputs are demonstrably inert". They are not: ``persona_assignments`` feeds both
from the LIVE persona-instance row --
``permission_options_for_chat(task_id=instance.current_task_id,
goal_id=instance.goal_id)`` at two call sites -- and they surface on the
``turn_tool_context`` wire, which the Launcher parses into
``MissionTurnToolContext.taskId`` / ``.goalId``. ``goal_id`` in particular is
the contract-45 correlation id the Launcher groups its agent rooms by (the same
absolute keep S26 recorded).

So this contract removes the operator-typed entry point and nothing downstream
of it. The surviving wire fields are pinned below so the next pass does not
mistake this for a half-finished cut.
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect

import pytest


def _persona_commands() -> dict:
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="root"))
    harness = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ).choices["harness"]
    persona = next(
        action for action in harness._actions if isinstance(action, argparse._SubParsersAction)
    ).choices["persona"]
    return next(
        action for action in persona._actions if isinstance(action, argparse._SubParsersAction)
    ).choices


@pytest.mark.parametrize("removed", ["--task", "--goal"])
def test_tool_diff_rejects_the_retired_mission_flags(removed):
    parser = _persona_commands()["tool-diff"]

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["qa", removed, "mission_record_s27", "--json"])

    assert excinfo.value.code == 2


def test_tool_diff_namespace_carries_no_mission_record_attributes():
    parser = _persona_commands()["tool-diff"]

    args = parser.parse_args(["qa", "--json"])

    assert not hasattr(args, "task_id")
    assert not hasattr(args, "goal_id")


def test_the_handler_no_longer_reads_the_retired_namespace_attributes():
    """A parser-only reap would leave the read reachable from any future caller
    that sets the attribute another way."""

    # persona_commands.py is exec'd into harness.py's globals, not imported.
    from hermes_cli import harness

    source = inspect.getsource(harness._cmd_persona_tool_diff)
    assert "args.task_id" not in source
    assert "args.goal_id" not in source


def test_the_surviving_flags_still_work():
    """The reap is of two flags, not of the operator's tool preview."""

    parser = _persona_commands()["tool-diff"]

    args = parser.parse_args(
        [
            "qa",
            "--session-id",
            "chat_s27",
            "--permission-mode",
            "unbounded",
            "--repo-scope",
            "hermes-agent",
            "--explain-mcp",
            "--explain-envelope",
            "--json",
        ]
    )

    assert args.persona_id == "qa"
    assert args.session_id == "chat_s27"
    assert args.permission_mode == "unbounded"
    assert args.repo_scope == "hermes-agent"
    assert args.explain_mcp is True
    assert args.explain_envelope is True


def test_the_live_task_goal_threading_is_kept():
    """ABSOLUTE KEEP: the persona-instance row feeds these, and the Launcher
    reads them off ``turn_tool_context``. Only the hand-typed flags were dead."""

    from agent_runtime import persona_assignments, tool_permissions, tool_visibility

    options = inspect.signature(tool_permissions.permission_options_for_chat).parameters
    assert "task_id" in options and "goal_id" in options

    fields = {field.name for field in dataclasses.fields(tool_visibility.ToolVisibilityOptions)}
    assert {"task_id", "goal_id"} <= fields

    # The live producer: both are read off the persona-instance row.
    source = inspect.getsource(persona_assignments)
    assert "task_id=instance.current_task_id" in source
    assert "goal_id=instance.goal_id" in source

    # And they still reach the wire the Launcher parses.
    wire = inspect.getsource(tool_visibility.turn_tool_context_for_persona)
    assert '"task_id": visibility.get("task_id")' in wire
    assert '"goal_id": visibility.get("goal_id")' in wire
