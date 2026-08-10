"""Terminal turns settle their tool rows — for EVERY terminal state, not one.

The defect this pins (2026-07-26): ``persona_chat_history`` synthesized its
terminal marker row only for the legacy ``interrupted`` state, and
``operator_channels`` settled running tool_call rows only off a
``turn_interrupted`` marker. When the wall-budget work landed the second
terminal state (``budget_exhausted``, a graceful checkpoint that needs no
operator turn-resolve), a turn could end with a ``tool_started`` trace entry and
no finish row, emit no marker, settle nothing — and spin a tool row in the
Mission Control cockpit forever.

Both halves are now table-driven. These tests pin the budget half AND assert the
legacy interrupted half is byte-for-byte what it always was, because a fix that
quietly changes the shape of the state everybody already renders is a
regression wearing a fix's clothes.
"""

from agent_runtime.mission_chat_turns import (
    TERMINAL_TURN_STATES,
    MissionChatTurnPersistOutcome,
    mission_chat_turn_record,
    persist_mission_chat_turn,
    transition_mission_chat_turn,
)
from agent_runtime.models import PersonaInstance
from agent_runtime.operator_channels import (
    _SETTLED_TOOL_CALL_STATUS,
    _TERMINAL_TURN_MARKER_PRESENTATION,
    operator_channel_summary,
)
from agent_runtime.persona_chat_history import (
    PERSONA_TURN_BUDGET_EXHAUSTED_KIND,
    PERSONA_TURN_INTERRUPTED_KIND,
    TERMINAL_TURN_MARKERS,
    _safe_recent_messages,
    _terminal_turn_marker_rows,
)
from agent_runtime.states import WorkerSessionState
from agent_runtime.transcript_order import TURN_SEQ_TERMINAL


class FakeSessionDB:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self, session_id, include_inactive=False):
        return list(self._messages)


def _instance(instance_id: str, *, session_id: str, updated_at: str) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id="profile:alice",
        role="profile",
        display_name="Alice Agent",
        profile_id="alice",
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id=session_id,
        updated_at=updated_at,
    )


def _settle_budget_exhausted(*, session_id: str, client_message_id: str, turn_id: str):
    """Drive a turn to ``budget_exhausted`` through the real journal transitions."""

    persist_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        elements=[],
        state="pending",
        write_ahead=True,
    )
    transition_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        elements=[],
        state="executing",
    )
    outcome = transition_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        elements=[],
        state="budget_exhausted",
        metadata={"provider_submitted": True, "budget_exhausted": True},
    )
    assert outcome is MissionChatTurnPersistOutcome.PERSISTED
    return mission_chat_turn_record(
        session_id=session_id, client_message_id=client_message_id
    )


# ── the marker table itself ──────────────────────────────────────────────────


def test_every_declared_marker_names_a_terminal_turn_state():
    """A marker for a non-terminal state would declare a LIVE turn over."""

    assert set(TERMINAL_TURN_MARKERS) <= TERMINAL_TURN_STATES
    assert {"interrupted", "budget_exhausted"} <= set(TERMINAL_TURN_MARKERS)


def test_every_marker_kind_has_a_conversation_presentation():
    """Producer and presenter cannot drift: a synthesized kind the conversation
    projection does not know would be a marker nobody renders and — worse — a
    marker that settles no tool row."""

    assert {marker.kind for marker in TERMINAL_TURN_MARKERS.values()} == set(
        _TERMINAL_TURN_MARKER_PRESENTATION
    )


def test_marker_ids_are_distinct_per_state():
    """Two terminal states of the SAME turn must not collide on one row id."""

    slugs = [marker.id_slug for marker in TERMINAL_TURN_MARKERS.values()]
    assert len(slugs) == len(set(slugs))


# ── history layer: the marker row ────────────────────────────────────────────


def test_budget_exhausted_turn_synthesizes_a_typed_marker_row(
    isolate_agent_runtime_root,
):
    record = _settle_budget_exhausted(
        session_id="s-budget",
        client_message_id="client_budget",
        turn_id="turn_budget",
    )
    assert record["state"] == "budget_exhausted"

    rows = _terminal_turn_marker_rows(
        session_id="s-budget", assistant_client_message_ids=set()
    )

    assert len(rows) == 1
    marker = rows[0]
    assert marker["kind"] == PERSONA_TURN_BUDGET_EXHAUSTED_KIND
    assert marker["id"] == "s-budget:turn-budget-exhausted:client_budget"
    assert marker["role"] == "system"
    assert marker["settled_state"] == "budget_exhausted"
    assert marker["client_message_id"] == "client_budget"
    assert marker["turn_id"] == "turn_budget"
    assert marker["turn_seq"] == TURN_SEQ_TERMINAL
    assert marker["redaction_status"] == "safe"
    # The prose must say checkpoint, not death: this turn settled gracefully and
    # its work may be committed. "Retry the message" would be a lie that costs
    # the operator a re-run.
    assert "wall budget" in marker["text"]
    assert "Retry the message" not in marker["text"]


def test_interrupted_marker_row_is_unchanged(isolate_agent_runtime_root):
    """The legacy path, pinned byte-for-byte against the table rewrite."""

    persist_mission_chat_turn(
        session_id="s-int",
        client_message_id="client_interrupted",
        turn_id="turn_interrupted",
        elements=[],
        state="interrupted",
    )

    rows = _terminal_turn_marker_rows(
        session_id="s-int", assistant_client_message_ids=set()
    )

    assert len(rows) == 1
    marker = rows[0]
    assert marker["kind"] == PERSONA_TURN_INTERRUPTED_KIND
    assert marker["id"] == "s-int:turn-interrupted:client_interrupted"
    assert marker["role"] == "system"
    assert marker["turn_id"] == "turn_interrupted"
    assert marker["turn_seq"] == TURN_SEQ_TERMINAL
    assert marker["text"] == (
        "Agent turn interrupted before a reply was recorded. Retry the message "
        "to run a fresh turn."
    )


def test_budget_exhausted_turn_with_a_recorded_reply_synthesizes_nothing(
    isolate_agent_runtime_root,
):
    """The graceful case: the checkpoint landed a real reply. A marker on top of
    it would double-terminate the turn."""

    _settle_budget_exhausted(
        session_id="s-budget-ok",
        client_message_id="client_budget_ok",
        turn_id="turn_budget_ok",
    )
    db = FakeSessionDB(
        [
            {
                "id": "agent_1",
                "role": "assistant",
                "content": "Checkpoint: committed the branch.",
                "platform_message_id": "client_budget_ok",
            }
        ]
    )

    rows, _status, _unread = _safe_recent_messages(db, session_id="s-budget-ok")

    assert [row["role"] for row in rows] == ["agent"]
    assert all("turn-budget-exhausted" not in row["id"] for row in rows)


def test_inflight_turn_synthesizes_no_marker(isolate_agent_runtime_root):
    """A running turn is not over. The table must never terminate it."""

    persist_mission_chat_turn(
        session_id="s-live",
        client_message_id="client_live",
        turn_id="turn_live",
        elements=[],
        state="executing",
        write_ahead=True,
    )

    assert (
        _terminal_turn_marker_rows(
            session_id="s-live", assistant_client_message_ids=set()
        )
        == []
    )


# ── conversation layer: projection + tool settlement ─────────────────────────


def _channel_with_marker(*, session_id: str, kind: str, settled_state: str | None):
    marker_row = {
        "id": f"{session_id}:marker:agent-chat-send-9",
        "role": "system",
        "kind": kind,
        "text": "the turn is over",
        "timestamp": "2026-07-26T09:00:30Z",
        "redaction_status": "safe",
        "client_message_id": "agent-chat-send-9",
        "turn_id": "agent-chat-send-9",
    }
    if settled_state is not None:
        marker_row["settled_state"] = settled_state
    return operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-07-26T09:00:00Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Alice Agent chat",
                "message_count": 1,
                "messages": [
                    {
                        "id": "operator_1",
                        "role": "operator",
                        "text": "run the long proof",
                        "client_message_id": "agent-chat-send-9",
                        "timestamp": "2026-07-26T09:00:00Z",
                    },
                    marker_row,
                ],
                "updated_at": "2026-07-26T09:00:30Z",
            }
        ],
        persona_chat_trace=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started terminal",
                        "status": "started",
                        "turn_id": "agent-chat-send-9",
                        "ts": "2026-07-26T09:00:05Z",
                    },
                ],
            }
        ],
    )


def test_budget_exhausted_marker_projects_and_settles_its_running_tool():
    channels = _channel_with_marker(
        session_id="persona_chat_personainst_profile_alice_budget",
        kind=PERSONA_TURN_BUDGET_EXHAUSTED_KIND,
        settled_state="budget_exhausted",
    )
    messages = channels[0]["conversation"]["messages"]
    marker = next(m for m in messages if m["kind"] == PERSONA_TURN_BUDGET_EXHAUSTED_KIND)
    tool_call = next(m for m in messages if m["kind"] == "tool_call")

    # The marker keeps the wire vocabulary the Launcher's adapter already reads
    # (`_budgetExhaustedFlowMessage`) — a graceful checkpoint, not an interrupt.
    assert marker["role"] == "system"
    assert marker["status"] == "budget_exhausted"
    assert marker["display_title"] == "Wall budget reached"
    assert marker["settled_state"] == "budget_exhausted"
    assert marker["turn_id"] == "agent-chat-send-9"

    # THE BUG: this tool_started has no finish row and used to project "running"
    # forever, because only `turn_interrupted` settled anything.
    assert tool_call["status"] == _SETTLED_TOOL_CALL_STATUS
    assert tool_call["tool"]["status"] == _SETTLED_TOOL_CALL_STATUS
    # …and it says WHY it stopped, in the turn store's own vocabulary, so the
    # settled call never has to pretend the turn was killed.
    assert tool_call["settled_reason"] == "budget_exhausted"
    assert tool_call["tool"]["settled_reason"] == "budget_exhausted"


def test_interrupted_marker_settlement_is_unchanged():
    channels = _channel_with_marker(
        session_id="persona_chat_personainst_profile_alice_interrupted",
        kind=PERSONA_TURN_INTERRUPTED_KIND,
        settled_state="interrupted",
    )
    messages = channels[0]["conversation"]["messages"]
    marker = next(m for m in messages if m["kind"] == PERSONA_TURN_INTERRUPTED_KIND)
    tool_call = next(m for m in messages if m["kind"] == "tool_call")

    assert marker["status"] == "interrupted"
    assert marker["display_title"] == "Turn interrupted"
    assert tool_call["status"] == "interrupted"
    assert tool_call["tool"]["status"] == "interrupted"
    assert tool_call["settled_reason"] == "interrupted"


def test_marker_row_without_settled_state_still_settles():
    """Archive-never-delete: rows persisted before ``settled_state`` existed must
    still settle their tools, falling back to the marker kind as the reason."""

    channels = _channel_with_marker(
        session_id="persona_chat_personainst_profile_alice_legacy",
        kind=PERSONA_TURN_INTERRUPTED_KIND,
        settled_state=None,
    )
    messages = channels[0]["conversation"]["messages"]
    tool_call = next(m for m in messages if m["kind"] == "tool_call")

    assert tool_call["status"] == _SETTLED_TOOL_CALL_STATUS
    assert tool_call["settled_reason"] == PERSONA_TURN_INTERRUPTED_KIND


def test_a_finished_tool_call_is_never_re_settled():
    """Settlement only touches rows that are still ``running``. A call that
    already reported ok/failed keeps its real outcome."""

    session_id = "persona_chat_personainst_profile_alice_finished"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-07-26T09:00:00Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Alice Agent chat",
                "message_count": 1,
                "messages": [
                    {
                        "id": "operator_1",
                        "role": "operator",
                        "text": "run the long proof",
                        "client_message_id": "agent-chat-send-9",
                        "timestamp": "2026-07-26T09:00:00Z",
                    },
                    {
                        "id": f"{session_id}:turn-budget-exhausted:agent-chat-send-9",
                        "role": "system",
                        "kind": PERSONA_TURN_BUDGET_EXHAUSTED_KIND,
                        "text": "the turn reached its wall budget",
                        "timestamp": "2026-07-26T09:00:30Z",
                        "redaction_status": "safe",
                        "client_message_id": "agent-chat-send-9",
                        "turn_id": "agent-chat-send-9",
                        "settled_state": "budget_exhausted",
                    },
                ],
                "updated_at": "2026-07-26T09:00:30Z",
            }
        ],
        persona_chat_trace=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started terminal",
                        "status": "started",
                        "turn_id": "agent-chat-send-9",
                        "ts": "2026-07-26T09:00:05Z",
                    },
                    {
                        "event": "tool_finished",
                        "tool_name": "terminal",
                        "summary": "Finished terminal",
                        "status": "ok",
                        "turn_id": "agent-chat-send-9",
                        "ts": "2026-07-26T09:00:07Z",
                    },
                ],
            }
        ],
    )

    tool_call = next(
        m for m in channels[0]["conversation"]["messages"] if m["kind"] == "tool_call"
    )

    assert tool_call["status"] == "ok"
    assert "settled_reason" not in tool_call
