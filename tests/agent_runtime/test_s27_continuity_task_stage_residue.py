"""S27 removes the CLI-unreachable ``task_id`` / ``stage_id`` residue from the
continuity return primitive.

``harness persona instance return-summary`` lost its ``--task``/``--stage`` flags
in the wave-1 vestigial-parser-flag pass, so the only production call site
(``persona_commands._cmd_persona_instance_return_summary``) stopped supplying
them. What stayed behind was a chain of parameters that could only ever be
``None``:

* ``continuity.return_summary_to_parent_session(task_id=, stage_id=)`` --
  stamped onto the ``steer.returned`` event's ``task_id`` column and its
  ``stage_id`` payload key. With ``Task`` deleted in S8 there is no record for
  either to point at, so both rows were a permanent ``None``.
* ``child_events.emit_child_returned(task_id=, stage_id=)`` -- required
  keyword-only parameters that the function body **never reads**: the event it
  appends hardcodes ``task_id=None`` and carries no ``stage_id`` key at all.
  ``continuity`` is its only caller.

KEPT, deliberately: ``proof_ids``. It is live -- ``_format_parent_message``
renders a "Proof refs:" line into the parent's chat message, which the operator
reads.
"""

from __future__ import annotations

import inspect

from agent_runtime import child_events, continuity


def test_the_continuity_return_no_longer_declares_task_or_stage():
    parameters = inspect.signature(continuity.return_summary_to_parent_session).parameters
    assert "task_id" not in parameters
    assert "stage_id" not in parameters


def test_the_child_return_emitter_no_longer_declares_task_or_stage():
    """They were required keyword-only parameters the body never read."""

    parameters = inspect.signature(child_events.emit_child_returned).parameters
    assert "task_id" not in parameters
    assert "stage_id" not in parameters


def test_neither_module_still_stamps_a_task_or_stage_row():
    for module in (continuity, child_events):
        source = inspect.getsource(module)
        assert "stage_id" not in source, module.__name__


def test_proof_refs_remain_live_in_the_parent_message():
    """KEEP: the operator reads this line in the parent chat."""

    message = continuity._format_parent_message(
        "personainst_dev",
        "Bounded child summary.",
        proof_ids=["proof_a", "proof_b"],
        artifact_refs=["artifact://x"],
    )
    assert "Proof refs: proof_a, proof_b" in message
    assert "Artifact refs: artifact://x" in message
