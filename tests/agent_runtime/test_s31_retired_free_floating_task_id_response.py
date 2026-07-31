"""S31 — the free-floating lane's copy of the write-only response key.

S30 retired the constant-``None`` ``task_id`` from ``mission-chat message``'s
reply envelopes and deliberately scoped itself there: the free-floating verbs
(``persona instance message`` / ``persona instance create --message``) carry the
same smell through DIFFERENT handlers, and their readers had not been audited.
This contract closes that half, after the audit.

Two emitters, one dead key:

* ``_run_free_floating_assignment_once`` emitted a literal ``"task_id": None``.
* ``_queue_free_floating_assignment`` emitted ``"task_id": assignment.task_id``,
  which was *also* always ``None`` on the free-floating construction lane — and
  which the runner's ``data.update(...)``
  then overwrote with its own ``None`` whenever ``auto_run`` was set. Two
  writers, both constant, neither read.

Why removing it is safe (the audit, recorded so the next reader need not redo
it). Both handlers emit through ``_emit_chat_final``, i.e. into the SAME
Launcher terminal parser the mission-chat lane uses:

* ``mission_control_bridge.dart`` ``_actionResultFromPayload`` (the accepted
  lane) and ``_streamFinalEvent`` (the ``chat.final`` lane, both its failure
  branch and its accepted branch) read no ``task_id``; the typed field they
  used to fill it into — ``MissionControlActionResult.taskId`` — no longer
  exists on the class (launcher 23bd05c6).
* Every other ``task_id`` reader in the Launcher parses a DIFFERENT envelope —
  snapshot rows, goal/board projections, event rows, run rows, the
  ``harness task history`` marker — none of which this key ever fed.
* The four ``capabilityPayload`` consumers (chat runtime controller, board card,
  flow apply, chat page) read named keys, none of them ``task_id``, so the
  removed key cannot be reached through the verbatim-envelope escape hatch
  either.
* Fork side: nothing reads ``task_id`` back off these two envelopes. The
  in-file readers are a different payload each — ``_cmd_persona_assignment_*``
  prints ``persona_assignment_summary``'s row, and the free-floating
  discriminators read the STORE MODEL's attribute, not this wire key.

S35 follow-up: the old split verdict is now retired. Persisted assignments with
a non-null task_id are archived before the spec field and its live readers go;
``evidence_kind`` is the sole free-floating discriminator.
"""

from __future__ import annotations

import dataclasses
import inspect


def _source(name: str) -> str:
    """Source of a free-floating lane helper.

    persona_commands.py is not an importable module: harness.py loads it via
    _load_command_parts() and execs it in its OWN globals, so the helpers are
    attributes of hermes_cli.harness. Same access path as S30.
    """

    from hermes_cli import harness

    return inspect.getsource(getattr(harness, name))


def test_free_floating_runner_emits_no_task_id_response_key():
    """``_run_free_floating_assignment_once``'s frame carries no task binding.

    Asserted on the emitted KEY spelling (double-quoted, as a dict literal
    writes it) rather than the bare token, so prose explaining the retirement
    can still name the field without re-tripping this gate.
    """

    source = _source("_run_free_floating_assignment_once")

    assert '"task_id"' not in source
    # Nor re-added after the dict literal is built.
    assert 'data["task_id"]' not in source
    assert "data['task_id']" not in source


def test_free_floating_queue_emits_no_task_id_response_key():
    """``_queue_free_floating_assignment``'s frame carries no task binding.

    This is the envelope the non-``auto_run`` call actually ships, so removing
    only the runner's copy would have left the queued lane still emitting it.
    """

    source = _source("_queue_free_floating_assignment")

    assert '"task_id"' not in source
    assert 'data["task_id"]' not in source
    assert "data['task_id']" not in source


def test_free_floating_envelopes_still_carry_every_key_the_launcher_reads():
    """The reap is of ONE dead key, not of either envelope.

    Guards the over-reap direction: each of these is parsed into a typed field
    the Launcher genuinely consumes (mission_control_bridge.dart
    ``_actionResultFromPayload`` / ``_streamFinalEvent``), so losing one would
    break a live turn in a way the task_id removal explicitly does not.
    """

    runner = _source("_run_free_floating_assignment_once")
    for kept in (
        '"ok"',
        '"session_id"',
        '"reply"',
        '"turn_id"',
        '"client_message_id"',
        '"run_ids"',
        '"execution_state"',
    ):
        assert kept in runner, f"{kept} is a LIVE runner reply key and must survive S31"

    queue = _source("_queue_free_floating_assignment")
    for kept in (
        '"ok"',
        '"agent_profile_id"',
        '"assignment_id"',
        '"persona_instance_id"',
        '"session_id"',
        '"client_message_id"',
        '"run_ids"',
        '"turn_id"',
        '"execution_state"',
    ):
        assert kept in queue, f"{kept} is a LIVE queued reply key and must survive S31"


def test_persona_assignment_spec_drops_its_migrated_task_id_field():
    """S35 archives the pre-retirement rows and clears S31's old blocker."""

    from agent_runtime.persona_assignments import PersonaAssignmentSpec

    fields = {field.name for field in dataclasses.fields(PersonaAssignmentSpec)}
    assert "task_id" not in fields


def test_free_floating_verbs_are_still_alive():
    """Sanity: the handlers this contract edits still exist and are commands."""

    from hermes_cli import harness

    assert callable(harness._cmd_persona_instance_message)
    assert callable(harness._cmd_persona_instance_create)
    assert callable(harness._queue_free_floating_assignment)
    assert callable(harness._run_free_floating_assignment_once)
