"""S26 — the retired mission lane's last live re-entry point.

The goal/task mission lane is gone (contract 45); chat is the only lane. But
``mission-chat message`` kept ``--task`` / ``--goal``, and unlike the S22 flags
these were not merely inert: ``_cmd_mission_chat_message`` consumed them by
writing ``instance.current_task_id`` / ``instance.goal_id`` and flipping
``instance.mode`` to the RETIRED ``task_bound`` value, then persisting the row.

They survived only because the shipped Launcher emitted them on every operator
chat send, sourced from the instance row's own scalars. No live row is armed
today (0 of 15 carry a goal_id/current_task_id), so the write was a no-op in
practice — but one ``persona instance steer --goal`` arms a row, and the
operator's next ordinary message would then silently re-arm retired runtime
state. The Launcher stopped emitting first; this contract removes the parser
flags and the write in lockstep.

What this contract deliberately does NOT remove:

* ``persona instance steer --goal`` — ``goal_id`` rides the contract-45 wire and
  the Launcher groups its agent rooms by it. Byte-identical, guarded below.
* ``persona instance update-profile --goal`` — same wire field, operator-set.
* ``persona tool-diff --task/--goal`` — a different command with its own flags;
  out of this contract's scope.
"""

from __future__ import annotations

import argparse

import pytest


def _harness_commands() -> dict:
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="root"))
    harness = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices["harness"]
    return next(
        action
        for action in harness._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices


def _mission_chat_commands() -> dict:
    mission_chat = _harness_commands()["mission-chat"]
    return next(
        action
        for action in mission_chat._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices


def _persona_instance_commands() -> dict:
    persona = _harness_commands()["persona"]
    persona_subs = next(
        action
        for action in persona._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices
    instance = persona_subs["instance"]
    return next(
        action
        for action in instance._actions
        if isinstance(action, argparse._SubParsersAction)
    ).choices


def _message_argv(*extra: str) -> list[str]:
    return [
        "--persona",
        "dev",
        "--message",
        "hello from s26",
        *extra,
    ]


@pytest.mark.parametrize("removed", ["--task", "--goal"])
def test_mission_chat_message_rejects_the_retired_mission_flags(removed):
    parser = _mission_chat_commands()["message"]

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(_message_argv(removed, "mission_record_s26"))

    assert excinfo.value.code == 2


def test_mission_chat_message_namespace_carries_no_mission_record_attributes():
    parser = _mission_chat_commands()["message"]

    args = parser.parse_args(_message_argv("--json"))

    assert not hasattr(args, "task_id")
    assert not hasattr(args, "goal_id")


def test_mission_chat_message_verb_is_still_alive():
    """The reap is of two flags, not of the only chat lane there is."""

    parser = _mission_chat_commands()["message"]

    args = parser.parse_args(
        _message_argv("--session-id", "chat_s26", "--intent-hint", "chat")
    )

    assert args.persona_id == "dev"
    assert args.message == "hello from s26"
    assert args.session_id == "chat_s26"
    assert args.intent_hint == "chat"


def test_persona_instance_steer_still_accepts_goal():
    """ABSOLUTE KEEP: the contract-45 correlation id the Launcher groups by."""

    parser = _persona_instance_commands()["steer"]

    args = parser.parse_args(
        ["personainst_dev_s26", "--parent", "personainst_lead_s26", "--goal", "goal_s26"]
    )

    assert args.goal_id == "goal_s26"


def test_persona_instance_update_profile_still_accepts_goal():
    """Same wire field on the operator-set lane; not this contract's business."""

    parser = _persona_instance_commands()["update-profile"]

    args = parser.parse_args(["personainst_dev_s26", "--goal", "goal_s26"])

    assert args.goal_id == "goal_s26"


def test_mission_chat_message_handler_never_writes_the_retired_task_bound_mode():
    """The write the flags fed is gone from the handler source.

    A parser-only reap would leave the ``task_bound`` write reachable by any
    future caller that sets the attribute another way. The retired mode value
    must not be written by the chat send at all.

    Asserts on the WRITE expressions rather than the bare tokens: the handler
    still carries a comment explaining what used to live there, and that
    comment is documentation, not a write.
    """

    import inspect

    # persona_commands.py is not an importable module: harness.py loads it via
    # _load_command_parts() and execs it in its OWN globals, so the command
    # bodies are attributes of hermes_cli.harness.
    from hermes_cli import harness

    source = inspect.getsource(harness._cmd_mission_chat_message)

    assert 'instance.mode = "task_bound"' not in source
    assert "instance.current_task_id =" not in source
    assert "instance.goal_id =" not in source
    # And the argv the write fed is gone from the namespace read, too.
    assert 'getattr(args, "task_id"' not in source
    assert 'getattr(args, "goal_id"' not in source
