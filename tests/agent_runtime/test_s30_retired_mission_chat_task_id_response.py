"""S30 — the retired mission lane's last write-only response key.

S26 removed ``mission-chat message``'s ``--task`` / ``--goal`` argv and the
``task_bound`` write they fed. This contract closes the RESPONSE half.

``_cmd_mission_chat_message`` kept emitting a constant-``None`` ``task_id`` key
on its reply envelopes, and the comment beside it said exactly why: "kept
because the Launcher parses it". It did — into
``MissionControlActionResult.taskId``, a field that was ASSIGNED BUT NEVER
READ. So the key's only justification was a write-only field on the other side
of the wire: a dead key held alive by a dead reader, each citing the other.

Order matters and is why this lands second. The Launcher dropped the reader
first (launcher commit 23bd05c6); only once no reader exists can the emitter go
without a flag day. A ``task_id`` from an older hermes is now inert on the
Launcher side — it rides ``capabilityPayload`` verbatim and reaches no typed
field — so old-client tolerance costs nothing.

What this contract deliberately does NOT touch:

* ``persona instance message`` / ``persona instance create --message`` — the
  free-floating lane's ``_run_free_floating_assignment_once`` emits its own
  constant-``None`` ``task_id``, and ``_queue_free_floating_assignment`` emits
  ``assignment.task_id`` (always ``None`` there, since the spec is created with
  ``task_id=None``). Same smell, DIFFERENT verbs, and no reader was audited for
  them — reaping those belongs to a contract that audits that lane. Left alone
  rather than swept in silently.
* ``persona tool-diff --task/--goal``, ``persona instance steer --goal``,
  ``update-profile --goal`` — untouched by S26 and untouched here.
"""

from __future__ import annotations

import inspect


def _mission_chat_message_source() -> str:
    """Source of the mission-chat send handler, BOTH halves.

    persona_commands.py is not an importable module: harness.py loads it via
    _load_command_parts() and execs it in its OWN globals, so the command
    bodies are attributes of hermes_cli.harness. Same access path as S26.

    The handler was split on 2026-07-31 into a plan phase
    (``_cmd_mission_chat_message``) and the sole writer
    (``_mission_chat_commit_turn``), and the reply envelopes live in the writer.
    Both are concatenated so this contract still reads the whole send handler —
    reading only one half would let a ``task_id`` reappear in the other, which
    is precisely the regression S30 exists to prevent.
    """

    from hermes_cli import harness

    return "\n".join(
        inspect.getsource(getattr(harness, name))
        for name in ("_cmd_mission_chat_message", "_mission_chat_commit_turn")
    )


def test_mission_chat_message_emits_no_task_id_response_key():
    """The reply envelopes carry no task_id key at all.

    Asserted on the emitted KEY spelling (double-quoted, as a dict literal
    writes it) rather than the bare token, so prose explaining the retirement
    can still name the field without re-tripping this gate.
    """

    source = _mission_chat_message_source()

    assert '"task_id"' not in source
    # Nor re-added after the dict literal is built.
    assert 'data["task_id"]' not in source
    assert "data['task_id']" not in source


def test_mission_chat_message_emits_no_goal_id_response_key():
    """Its sibling went in S26; this pins that it stays gone."""

    source = _mission_chat_message_source()

    assert '"goal_id"' not in source


def test_mission_chat_message_still_emits_every_key_the_launcher_reads():
    """The reap is of ONE dead key, not of the reply envelope.

    Guards the over-reap direction: each of these is parsed into a typed field
    the Launcher genuinely consumes (mission_control_bridge.dart
    `_actionResultFromPayload`), so losing one would break a live turn in a way
    the task_id removal explicitly does not.
    """

    source = _mission_chat_message_source()

    for kept in (
        '"ok"',
        '"session_id"',
        '"turn_id"',
        '"client_message_id"',
        '"run_ids"',
        '"reply"',
        '"execution_state"',
        '"persona_instance_id"',
    ):
        assert kept in source, f"{kept} is a LIVE reply key and must survive S30"


def test_mission_chat_message_verb_is_still_alive():
    """Sanity: the handler this contract edits still exists and is a command."""

    from hermes_cli import harness

    assert callable(harness._cmd_mission_chat_message)
