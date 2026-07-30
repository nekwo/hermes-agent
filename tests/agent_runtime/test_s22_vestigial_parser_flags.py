"""S22 — mission-era flags the retired lane left behind on kept verbs.

``persona instance return-summary`` kept accepting ``--task`` and ``--stage``
after the mission lane went. Their values fed nothing a reader can reach:
``--task`` became ``Event.task_id`` — the mission-record key whose ``Task``
model was deleted in S8 — and ``--stage`` became the ``stage_id`` payload key of
the stage graph deleted in S7. A flag that accepts a value and writes it into a
retired record is worse than an absent one: the operator is told the binding
took.

What this contract deliberately does NOT remove:

* ``--proof-id`` / ``--artifact-ref`` — both render into the parent chat message
  body (``Proof refs:`` / ``Artifact refs:``) and are documented on the live
  continuity skill. They are output, not residue.
* ``persona instance steer --goal`` — ``goal_id`` rides the contract-45 wire and
  the Launcher groups its agent rooms by it. Byte-identical, guarded below.
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


def _return_summary_argv(*extra: str) -> list[str]:
    return [
        "personainst_dev_agent_s22",
        "--parent-session-id",
        "parent_session_s22",
        "--summary",
        "bounded summary",
        *extra,
    ]


@pytest.mark.parametrize("removed", ["--task", "--stage"])
def test_return_summary_rejects_the_removed_mission_flags(removed):
    parser = _persona_instance_commands()["return-summary"]

    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(_return_summary_argv(removed, "mission_record_s22"))

    assert excinfo.value.code == 2


def test_return_summary_namespace_carries_no_mission_record_attributes():
    parser = _persona_instance_commands()["return-summary"]

    args = parser.parse_args(_return_summary_argv("--json"))

    assert not hasattr(args, "task_id")
    assert not hasattr(args, "stage_id")


def test_return_summary_keeps_the_ref_flags_that_render_into_the_parent_message():
    parser = _persona_instance_commands()["return-summary"]

    args = parser.parse_args(
        _return_summary_argv(
            "--proof-id",
            "stagec_proof_s22",
            "--artifact-ref",
            "artifact://s22",
        )
    )

    assert args.proof_ids == ["stagec_proof_s22"]
    assert args.artifact_refs == ["artifact://s22"]


def test_return_summary_handler_forwards_no_mission_record_keys(monkeypatch, capsys):
    from hermes_cli import harness

    captured: dict = {}

    def _fake_return(persona_instance_id, **kwargs):
        captured["persona_instance_id"] = persona_instance_id
        captured.update(kwargs)
        return {
            "ok": True,
            "capability_id": "persona.instance.return_summary",
            "persona_instance_id": persona_instance_id,
            "parent_session_id": kwargs["parent_session_id"],
        }

    monkeypatch.setattr(harness, "return_summary_to_parent_session", _fake_return)

    parser = _persona_instance_commands()["return-summary"]
    args = parser.parse_args(
        _return_summary_argv("--proof-id", "stagec_proof_s22", "--json")
    )

    assert harness._cmd_persona_instance_return_summary(args) == 0
    capsys.readouterr()

    assert "task_id" not in captured
    assert "stage_id" not in captured
    assert captured["proof_ids"] == ["stagec_proof_s22"]
    assert captured["artifact_refs"] == []


def test_persona_instance_steer_still_carries_the_correlation_goal():
    """ABSOLUTE KEEP — ``goal_id`` is the contract-45 agent-room grouping key."""

    parser = _persona_instance_commands()["steer"]

    args = parser.parse_args(
        ["personainst_dev_agent_s22", "--parent", "personainst_neko", "--goal", "goal_s22"]
    )

    assert args.goal_id == "goal_s22"
