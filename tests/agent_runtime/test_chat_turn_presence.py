"""C1h-bis — a chat turn publishes on its own account, and the pairing is honest.

Stage C1h-bis of ``remote-chat-parity.md``. The real-serve proof that a second
console actually SEES a running turn lives in
``test_serve_gateway_chat_reply_lanes.py``; this file pins the two things that
proof cannot show from the outside:

1. **the publisher's own contract** — what each append carries, and the pairing
   rule that an END is published only for a START that actually landed, exactly
   once, and never at the cost of the turn;
2. **that the publishes sit in the chat-turn CORE** rather than in the
   ``runtime.chat.message`` shim. That is a structural fact, and it is what
   makes the local launcher's ``harness mission-chat message`` lane publish too
   — so it is pinned by AST rather than left to a reader of one lane's test.

A note on why the END rides a ``finally`` and is pinned as one:
``_mission_chat_commit_turn`` has fourteen terminal journal transitions and its
caller sees only an exit code. A publish placed at "the" end would be a publish
placed at one of them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from agent_runtime.chat_turn_presence import (
    EVENT_TURN_ENDED,
    EVENT_TURN_STARTED,
    STATE_ABSENT,
    ChatTurnPresence,
)
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import EventLog

SESSION = "persona_chat_personainst_qa_agent_deadbeef_0011"
CLIENT_MESSAGE_ID = "gesture-from-windows-1"
TURN_ID = "turn_0011"
INSTANCE = "personainst_qa_agent_deadbeef"


def _rows(event_type: str):
    return [event for event in EventLog().tail(40) if event.type == event_type]


def _presence() -> ChatTurnPresence:
    return ChatTurnPresence()


def _start(presence: ChatTurnPresence, **overrides) -> bool:
    params = {
        "session_id": SESSION,
        "client_message_id": CLIENT_MESSAGE_ID,
        "turn_id": TURN_ID,
        "persona_id": "qa",
        "persona_instance_id": INSTANCE,
        "active_session_id": SESSION,
    }
    params.update(overrides)
    return presence.publish_started(**params)


# ── the two registrations ───────────────────────────────────────────────────


def test_both_types_are_registered_with_the_fields_the_publisher_sends():
    """A payload short of its summary fields is a contract violation under
    ``HERMES_EVENT_CONTRACT_STRICT``, which CI sets — so the catalog and the
    emitter are pinned together rather than each against itself."""

    catalog = event_catalog()
    assert catalog[EVENT_TURN_STARTED]["summary_fields"] == [
        "persona_instance_id",
        "root_chat_session_id",
        "client_message_id",
        "turn_id",
    ]
    assert catalog[EVENT_TURN_ENDED]["summary_fields"] == [
        "persona_instance_id",
        "root_chat_session_id",
        "client_message_id",
        "turn_id",
        "state",
    ]


# ── the START publish ───────────────────────────────────────────────────────


def test_the_start_publish_names_the_turn_the_projection_will_carry(
    isolate_agent_runtime_root,
):
    assert _start(_presence()) is True
    rows = _rows(EVENT_TURN_STARTED)
    assert len(rows) == 1
    event = rows[0]
    assert event.payload["root_chat_session_id"] == SESSION
    # THE join key a second console holds: the projection's ``work_id`` is
    # ``chat_turn:<client_message_id>``, the id the launcher minted and sent.
    assert event.payload["client_message_id"] == CLIENT_MESSAGE_ID
    assert event.payload["turn_id"] == TURN_ID
    assert event.payload["persona_instance_id"] == INSTANCE
    # Session lineage rides the ENVELOPE, the way every other chat-turn event in
    # this runtime carries it, so a per-session reader needs no payload dig.
    assert event.session_id == SESSION
    assert event.turn_id == TURN_ID
    assert event.persona_id == "qa"
    assert event.task_id is None and event.run_id is None


def test_a_second_start_publishes_nothing(isolate_agent_runtime_root):
    presence = _presence()
    assert _start(presence) is True
    assert _start(presence) is False
    assert len(_rows(EVENT_TURN_STARTED)) == 1


def test_an_append_that_fails_does_not_reach_the_turn(isolate_agent_runtime_root):
    """A notification is never the turn's problem — and an unpublished START
    must leave the pairing closed rather than owing an unpaired END.

    The patch is SCOPED (never ``monkeypatch.undo()``, which drops the shared
    stack and with it this suite's home/credential guards — see
    ``tests/conftest.py``): the second half of the assertion needs a working
    append to prove the silence is the pairing rule and not the sabotage.
    """

    presence = _presence()
    with pytest.MonkeyPatch.context() as patched:
        def _boom(self, evt):  # noqa: ANN001 - signature mirrors EventLog.append
            raise RuntimeError("event log is full")

        patched.setattr(EventLog, "append", _boom)
        assert _start(presence) is False
        assert presence.started is False
    assert presence.publish_ended() is False
    assert _rows(EVENT_TURN_ENDED) == []


# ── the END publish ─────────────────────────────────────────────────────────


def test_an_end_without_a_start_publishes_nothing(isolate_agent_runtime_root):
    """The refused and busy paths never reach a write-ahead. Announcing the end
    of a turn that never began would put a row's disappearance on the wire for a
    row that was never there."""

    assert _presence().publish_ended() is False
    assert _rows(EVENT_TURN_ENDED) == []


def test_the_end_publish_carries_the_journal_state(isolate_agent_runtime_root):
    from agent_runtime.mission_chat_turns import transition_mission_chat_turn

    presence = _presence()
    assert _start(presence) is True
    transition_mission_chat_turn(
        session_id=SESSION,
        client_message_id=CLIENT_MESSAGE_ID,
        turn_id=TURN_ID,
        state="pending",
        elements=[],
        metadata={"root_chat_session_id": SESSION, "persona_instance_id": INSTANCE},
    )
    assert presence.publish_ended() is True
    rows = _rows(EVENT_TURN_ENDED)
    assert len(rows) == 1
    # READ from the store at publish time, not passed in: the state the wire
    # reports and the state the projection will report are the same read.
    assert rows[0].payload["state"] == "pending"
    assert rows[0].payload["client_message_id"] == CLIENT_MESSAGE_ID


def test_a_turn_with_no_journal_record_ends_as_absent(isolate_agent_runtime_root):
    """"The record is gone" is an answer, and a different one from "the state
    could not be read"."""

    presence = _presence()
    assert _start(presence) is True
    assert presence.publish_ended() is True
    assert _rows(EVENT_TURN_ENDED)[0].payload["state"] == STATE_ABSENT


def test_a_second_end_publishes_nothing(isolate_agent_runtime_root):
    presence = _presence()
    assert _start(presence) is True
    assert presence.publish_ended() is True
    assert presence.publish_ended() is False
    assert len(_rows(EVENT_TURN_ENDED)) == 1


# ── where the publishes live ────────────────────────────────────────────────


def _persona_commands_tree() -> ast.Module:
    path = (
        Path(__file__).resolve().parents[2]
        / "hermes_cli"
        / "harness_parts"
        / "persona_commands.py"
    )
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not in persona_commands.py any more")


def _calls(node: ast.AST, attr: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == attr
    ]


@pytest.mark.parametrize(
    "function_name, attr",
    [
        ("_mission_chat_commit_turn", "publish_started"),
        ("_cmd_mission_chat_message", "publish_ended"),
    ],
)
def test_the_publishes_live_in_the_chat_turn_core(function_name, attr):
    """Both entry points publish because the publish is in the core they SHARE.

    ``runtime.chat.message`` lowers to argv and runs the same argparse tree
    ``harness mission-chat message`` runs (``agent_runtime/chat_turn.py``), so a
    publish in the method shim would leave every locally-typed turn — the lane
    the local launcher still uses — silent on the stream lane. This asserts the
    call sites are in the shared handler, which is the property that makes that
    true and the one a later refactor could quietly lose.
    """

    tree = _persona_commands_tree()
    assert _calls(_function(tree, function_name), attr), (
        f"{function_name} no longer calls {attr}"
    )


@pytest.mark.usefixtures("persisted_persona_samples")
def test_a_turn_typed_at_the_cli_publishes_both_frames(
    monkeypatch, capsys, isolate_agent_runtime_root
):
    """The OTHER entry point, driven rather than asserted structurally.

    The real-serve proof in ``test_serve_gateway_chat_reply_lanes.py`` drives
    ``runtime.chat.message``. This drives the lane the LOCAL launcher still uses
    — ``_cmd_mission_chat_message`` itself, on the rig
    ``test_chat_lease_finalization_tail`` built for exactly this (the production
    lease, journal and finalization tail; a stub provider and transcript DB) —
    and asserts the same two appends land, in order, with the journal's terminal
    state on the end row.
    """

    from tests.agent_runtime.test_chat_lease_finalization_tail import (
        _args,
        _install_chat_lane,
    )

    harness = _install_chat_lane(monkeypatch)
    assert harness._cmd_mission_chat_message(_args("cm-presence-1")) == 0
    capsys.readouterr()

    started = _rows(EVENT_TURN_STARTED)
    ended = _rows(EVENT_TURN_ENDED)
    assert len(started) == 1, started
    assert len(ended) == 1, ended
    assert started[0].payload["client_message_id"] == "cm-presence-1"
    assert ended[0].payload["client_message_id"] == "cm-presence-1"
    # The row's disappearance is the end frame's whole content, so the state it
    # reports must be one the ``running_work`` chat-turn lane no longer counts.
    from agent_runtime.mission_chat_turns import INFLIGHT_TURN_STATES

    assert ended[0].payload["state"] not in INFLIGHT_TURN_STATES, ended[0].payload
    # Order, off the log itself: a turn cannot end before it starts.
    tail = [event.type for event in EventLog().tail(40)]
    assert tail.index(EVENT_TURN_STARTED) < tail.index(EVENT_TURN_ENDED)


def test_the_end_publish_is_in_a_finally():
    """Not at "the" end — there is no such place.

    ``_mission_chat_commit_turn`` has fourteen terminal journal transitions and
    can also raise past its caller; a publish at any one of them would be a
    publish at one of fourteen. The ``finally`` is the only construct that is
    every exit, which is why it is pinned as one rather than as a line number.
    """

    handler = _function(_persona_commands_tree(), "_cmd_mission_chat_message")
    in_finally = [
        call
        for node in ast.walk(handler)
        if isinstance(node, ast.Try)
        for statement in node.finalbody
        for call in _calls(statement, "publish_ended")
    ]
    assert in_finally, "publish_ended is not in a finally any more"
