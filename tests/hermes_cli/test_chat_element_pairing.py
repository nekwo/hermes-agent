"""Which started tool a finished tool event belongs to (Stage 6).

Measured on a live turn record 2026-08-28 and filed as bucket (f) of the
turn-efficiency plan: with two tools of the same name started concurrently, the
stream emitter paired the finished events by NAME and `pop()` — LIFO — so the
elements came out CROSSED. Element `[0]`'s `summary` named one skill while its
`tool_input` named the other, and `[1]` the reverse; the same happened to two
`read_file` pairs in the same turn. A trace an operator debugs from was
attributing each call's input to its neighbour.

The emitter is an exec'd command part, so these drive it exactly the way the
mission-chat handler does: construct it with frames suppressed and feed it the
runner's `run.tool.started` / `run.tool.finished` progress payloads.
"""

from __future__ import annotations

import pytest


def emitter(**kwargs):
    from hermes_cli import harness

    return harness._ChatProtocolV2Emitter(
        turn_id="turn_1",
        client_message_id="client_1",
        emit_frames=False,
        **kwargs,
    )


def started(name, summary, **extra):
    return {"type": "run.tool.started", "tool_name": name, "summary": summary, **extra}


def finished(name, **extra):
    return {"type": "run.tool.finished", "tool_name": name, "status": "ok", **extra}


def tools(item):
    return [element for element in item.elements if element["kind"] == "tool"]


def test_two_concurrent_calls_of_one_tool_keep_their_own_inputs():
    """The measured defect, reproduced and refused.

    Both `skill_view` calls start before either finishes, and the finishes
    arrive in start order. Under the old LIFO pop, finish-A landed on the
    element started SECOND — and since a finished payload's `tool_input`
    overrides the started one, the two elements swapped inputs.
    """
    item = emitter()

    item.progress(started("skill_view", "skill_view(harness-charsheet-authoring)",
                          tool_input="skill_id: harness-charsheet-authoring"))
    item.progress(started("skill_view", "skill_view(eternia-launcher-workflow)",
                          tool_input="skill_id: eternia-launcher-workflow"))
    item.progress(finished("skill_view", tool_input="skill_id: harness-charsheet-authoring",
                           output="the charsheet skill"))
    item.progress(finished("skill_view", tool_input="skill_id: eternia-launcher-workflow",
                           output="the launcher skill"))

    first, second = tools(item)
    assert first["summary"] == "skill_view(harness-charsheet-authoring)"
    assert first["tool_input"] == "skill_id: harness-charsheet-authoring"
    assert first["output"] == "the charsheet skill"
    assert second["summary"] == "skill_view(eternia-launcher-workflow)"
    assert second["tool_input"] == "skill_id: eternia-launcher-workflow"
    assert second["output"] == "the launcher skill"


def test_a_finish_out_of_start_order_still_finds_its_own_element():
    """Identity, not order — which is the whole point of matching on the input.

    The second call finishes first (a small read beside a big one). Arrival
    order would attribute the small read's output to the big read's element,
    and FIFO would be wrong here exactly as LIFO was wrong above.
    """
    item = emitter()

    item.progress(started("read_file", "read_file(SKILL.md)", tool_input="path: SKILL.md"))
    item.progress(started("read_file", "read_file(FIELD-NOTES.md)",
                          tool_input="path: FIELD-NOTES.md"))
    item.progress(finished("read_file", tool_input="path: FIELD-NOTES.md", output="notes"))
    item.progress(finished("read_file", tool_input="path: SKILL.md", output="skill"))

    first, second = tools(item)
    assert (first["summary"], first["output"]) == ("read_file(SKILL.md)", "skill")
    assert (second["summary"], second["output"]) == ("read_file(FIELD-NOTES.md)", "notes")


def test_a_terminal_call_is_matched_on_its_command():
    """The terminal class carries no `tool_input` — its input IS the command.

    `_attach_tool_io` suppresses the generic input record when a call already
    surfaced a command, against the event cap. So the command is the identity
    for exactly the tool an authoring turn runs most often, and concurrently.
    """
    item = emitter()

    item.progress(started("terminal", "run the rows batch", command_full="hermes … rows"))
    item.progress(started("terminal", "thumb the landed rows", command_full="hermes … thumb"))
    item.progress(finished("terminal", command_full="hermes … thumb", output="crop written"))
    item.progress(finished("terminal", command_full="hermes … rows", output="6 strips"))

    rows, thumb = tools(item)
    assert (rows["command"], rows["output"]) == ("hermes … rows", "6 strips")
    assert (thumb["command"], thumb["output"]) == ("hermes … thumb", "crop written")


def test_identity_free_calls_pair_in_arrival_order():
    """With nothing to match on, the honest guess is FIFO — not the old LIFO.

    Two indistinguishable calls are interchangeable to a reader, so this pins
    the ORDER of settlement rather than a distinction that does not exist: the
    element started first is the element finished first.
    """
    item = emitter()

    item.progress(started("todo_write", "checklist"))
    item.progress(started("todo_write", "checklist"))
    item.progress(finished("todo_write", duration_ms=10))
    item.progress(finished("todo_write", duration_ms=20))

    first, second = tools(item)
    assert first["duration_ms"] == 10
    assert second["duration_ms"] == 20


def test_a_finish_with_no_start_still_mints_its_own_element():
    """The pre-existing orphan arm, unchanged — a finish never goes unrecorded."""
    item = emitter()

    item.progress(finished("terminal", output="ran before anyone was watching"))

    orphan = tools(item)[0]
    assert orphan["state"] == "finished"
    assert orphan["output"] == "ran before anyone was watching"


def test_the_ordinary_one_at_a_time_shape_is_untouched():
    """Nothing about the serial case changes: start, finish, settle, repeat."""
    item = emitter()

    item.progress(started("terminal", "first", command_full="one"))
    item.progress(finished("terminal", command_full="one", output="1"))
    item.progress(started("terminal", "second", command_full="two"))
    item.progress(finished("terminal", command_full="two", output="2"))

    first, second = tools(item)
    assert [element["state"] for element in (first, second)] == ["finished", "finished"]
    assert (first["output"], second["output"]) == ("1", "2")
    assert first["seq"] < second["seq"]


@pytest.mark.parametrize("field", ["tool_input", "command"])
def test_a_matched_element_leaves_the_pending_set(field):
    """One start is settled by ONE finish; a repeat cannot re-settle it.

    Without the pop, a second finish carrying the same identity would land on
    the element already settled and overwrite its result — and the start it
    actually belonged to would be minted as an orphan instead.
    """
    item = emitter()
    key = {"tool_input": {"tool_input": "path: a"}, "command": {"command_full": "cmd a"}}[field]

    item.progress(started("read_file", "read a", **key))
    item.progress(finished("read_file", output="first", **key))
    item.progress(finished("read_file", output="second", **key))

    first, orphan = tools(item)
    assert first["output"] == "first"
    assert orphan["output"] == "second"
    assert orphan["id"] != first["id"]
