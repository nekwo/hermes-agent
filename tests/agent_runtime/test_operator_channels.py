from agent_runtime.models import PersonaInstance

# S47 removed the ``Task = SimpleNamespace`` stand-in and the three task
# fixtures built from it: ``operator_channel_summary`` no longer takes a
# ``tasks`` argument, so nothing in this module can bind a task any more.
from agent_runtime.operator_channels import (
    _conversation_history_message,
    operator_channel_summary,
)
from agent_runtime.persona_chat_history import (
    PERSONA_PRE_TRACE_ACK_KIND,
    PERSONA_TURN_BUDGET_EXHAUSTED_KIND,
)
from agent_runtime.states import WorkerSessionState
from hermes_time import now


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


def test_operator_channel_collapses_alias_instances_and_keeps_trace_without_warning():
    session_id = "persona_chat_personainst_profile_alice_e898c1dc3794"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_operator_2c1f1de674e74942",
                session_id=session_id,
                updated_at="2026-06-25T21:54:04Z",
            ),
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-06-25T21:53:47Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Alice Agent chat",
                "message_count": 1,
                "messages": [{"role": "operator", "text": "run date"}],
                "updated_at": "2026-06-25T21:54:04Z",
            }
        ],
        persona_chat_trace=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_operator_2c1f1de674e74942",
                "task_id": None,
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started tool terminal: date",
                        "status": "started",
                        "ts": "2026-06-25T21:54:00Z",
                    }
                ],
            }
        ],
    )

    assert len(channels) == 1
    channel = channels[0]
    assert channel["persona_instance_id"] == "personainst_profile_alice"
    assert channel["session_id"] == session_id
    assert channel["tool_trace_count"] == 1
    assert channel["trace"]["entries"][0]["tool_name"] == "terminal"
    assert set(channel["source_instance_ids"]) == {
        "personainst_operator_2c1f1de674e74942",
        "personainst_profile_alice",
    }
    assert not any(
        warning["code"] == "duplicate_instances_same_channel"
        for warning in channel["warnings"]
    )


def test_operator_conversation_projects_turn_identity_keys_and_scopes_tool_pairing():
    session_id = "persona_chat_personainst_profile_alice_identity"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-07-07T14:00:00Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Alice Agent chat",
                "message_count": 2,
                "messages": [
                    {
                        "id": "operator_1",
                        "role": "operator",
                        "text": "run proof one",
                        "client_message_id": "agent-chat-send-1",
                        "timestamp": "2026-07-07T14:00:00Z",
                    },
                    {
                        "id": "agent_1",
                        "role": "agent",
                        "text": "proof one complete",
                        "client_message_id": "agent-chat-send-1:assistant:1",
                        "timestamp": "2026-07-07T14:00:08Z",
                    },
                ],
                "updated_at": "2026-07-07T14:00:08Z",
            }
        ],
        persona_chat_trace=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "entries": [
                    {
                        "event": "progress",
                        "summary": "Agent thinking process updated",
                        "reasoning_summary": "Preparing first proof.",
                        "status": "running",
                        "turn_id": "agent-chat-send-1",
                        "ts": "2026-07-07T14:00:01Z",
                    },
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started first terminal",
                        "status": "started",
                        "turn_id": "agent-chat-send-1",
                        "ts": "2026-07-07T14:00:02Z",
                    },
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started second terminal",
                        "status": "started",
                        "turn_id": "agent-chat-send-2",
                        "ts": "2026-07-07T14:00:03Z",
                    },
                    {
                        "event": "tool_finished",
                        "tool_name": "terminal",
                        "summary": "Finished second terminal: failed",
                        "status": "failed",
                        "turn_id": "agent-chat-send-2",
                        "ts": "2026-07-07T14:00:04Z",
                    },
                    {
                        "event": "tool_finished",
                        "tool_name": "terminal",
                        "summary": "Finished first terminal: passed",
                        "status": "passed",
                        "turn_id": "agent-chat-send-1",
                        "ts": "2026-07-07T14:00:05Z",
                    },
                ],
            }
        ],
    )

    messages = channels[0]["conversation"]["messages"]
    operator = next(message for message in messages if message["id"] == "operator_1")
    agent = next(message for message in messages if message["id"] == "agent_1")
    thinking = next(message for message in messages if message["kind"] == "thinking_summary")
    tool_calls = [message for message in messages if message["kind"] == "tool_call"]

    assert operator["client_message_id"] == "agent-chat-send-1"
    assert operator["turn_id"] == "agent-chat-send-1"
    assert agent["client_message_id"] == "agent-chat-send-1:assistant:1"
    assert agent["turn_id"] == "agent-chat-send-1"
    assert thinking["turn_id"] == "agent-chat-send-1"
    assert {message["turn_id"] for message in tool_calls} == {
        "agent-chat-send-1",
        "agent-chat-send-2",
    }
    by_turn = {message["turn_id"]: message for message in tool_calls}
    assert by_turn["agent-chat-send-1"]["status"] == "ok"
    assert by_turn["agent-chat-send-2"]["status"] == "failed"
    assert by_turn["agent-chat-send-1"]["id"].endswith(":tool:agent-chat-send-1:0")
    assert by_turn["agent-chat-send-2"]["id"].endswith(":tool:agent-chat-send-2:0")
    assert not any(
        warning["code"] == "operator_conversations.turn_identity_dropped"
        for warning in channels[0]["warnings"]
    )


def test_operator_conversation_projects_interrupted_turn_marker_and_settles_running_tools():
    session_id = "persona_chat_personainst_profile_alice_interrupted"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-07-08T09:00:00Z",
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
                        "text": "run the slow proof",
                        "client_message_id": "agent-chat-send-9",
                        "timestamp": "2026-07-08T09:00:00Z",
                    },
                    {
                        "id": f"{session_id}:turn-interrupted:agent-chat-send-9",
                        "role": "system",
                        "kind": "turn_interrupted",
                        "text": "Agent turn interrupted before a reply was recorded. Retry the message to run a fresh turn.",
                        "timestamp": "2026-07-08T09:00:30Z",
                        "redaction_status": "safe",
                        "client_message_id": "agent-chat-send-9",
                        "turn_id": "agent-chat-send-9",
                    },
                ],
                "updated_at": "2026-07-08T09:00:30Z",
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
                        "ts": "2026-07-08T09:00:05Z",
                    },
                ],
            }
        ],
    )

    messages = channels[0]["conversation"]["messages"]
    marker = next(m for m in messages if m["kind"] == "turn_interrupted")
    tool_call = next(m for m in messages if m["kind"] == "tool_call")

    # The typed terminal turn-status marker survives projection with its turn
    # identity so the Launcher can pair a retry affordance with the operator
    # message that started the turn.
    assert marker["role"] == "system"
    assert marker["status"] == "interrupted"
    assert marker["display_title"] == "Turn interrupted"
    assert marker["client_message_id"] == "agent-chat-send-9"
    assert marker["turn_id"] == "agent-chat-send-9"

    # The orphaned tool_started must not project an eternal "running" call:
    # the marker is the truth that this turn will never finish.
    assert tool_call["turn_id"] == "agent-chat-send-9"
    assert tool_call["status"] == "interrupted"
    assert tool_call["tool"]["status"] == "interrupted"


def test_operator_channel_reports_missing_history_loudly():
    """Genuine projection loss: real content flowed — a live trace with tool
    activity, which also projects a tool_call conversation message — but no
    curated chat history row backs it. session_without_history must still fire
    loudly. (A bare newborn chat with no content is covered separately and
    stays silent.)"""
    session_id = "persona_chat_personainst_profile_alice_missing"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-06-25T21:54:04Z",
            )
        ],
        persona_chat_history=[],
        persona_chat_trace=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "task_id": None,
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started tool terminal: date",
                        "status": "started",
                        "ts": "2026-06-25T21:54:00Z",
                    }
                ],
            }
        ],
    )

    assert len(channels) == 1
    assert any(
        warning["code"] == "session_without_history"
        for warning in channels[0]["warnings"]
    )
    # The trace lane carries real activity, so this is not an empty channel.
    assert not any(
        warning["code"] == "trace_empty" for warning in channels[0]["warnings"]
    )


def test_operator_channel_newborn_chat_emits_no_warnings():
    """A brand-new Mission Control chat has a session id and nothing else yet —
    no curated history row, no trace, no task binding, zero conversation
    messages. That NEWBORN state is not a contract breach: neither
    session_without_history nor trace_empty may fire (live 2026-07-18: creating
    a fresh neko_supervisor chat surfaced two false-positive 'Channel contract
    warning' bubbles)."""
    session_id = "persona_chat_personainst_profile_alice_newborn"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-07-18T09:00:00Z",
            )
        ],
        persona_chat_history=[],
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    codes = {warning["code"] for warning in channels[0]["warnings"]}
    assert "session_without_history" not in codes
    assert "trace_empty" not in codes


def test_operator_channel_trace_empty_fires_for_task_bound_channel_without_trace():
    """A task-bound channel with a goal but no trace and no flow messages is a
    genuine empty-trace anomaly — trace_empty must still fire. The newborn
    exemption is scoped to session-only channels with no task binding."""
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    assert any(
        warning["code"] == "trace_empty" for warning in channels[0]["warnings"]
    )


def test_operator_channel_dormant_instance_has_no_trace_empty_warning():
    """A seeded/probe instance that never chatted — no session, no history,
    no task, empty conversation — is dormant, not anomalous. It must not emit
    a standing trace_empty parity warning on every snapshot (live 2026-07-08:
    pm/qa + three codex probe instances produced 5 permanent warnings)."""
    channels = operator_channel_summary(
        persona_instances=[
            PersonaInstance(
                id="personainst_pm",
                persona_id="pm",
                role="pm",
                display_name="PM",
                profile_id=None,
                runtime_root="test-runtime",
                state=WorkerSessionState.IDLE,
                mode="chat",
                session_id=None,
                updated_at="2026-07-08T09:00:00Z",
            )
        ],
        persona_chat_history=[],
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    assert not any(
        warning["code"] == "trace_empty" for warning in channels[0]["warnings"]
    )


def test_operator_channel_allows_quiet_chat_without_trace():
    session_id = "persona_chat_personainst_profile_alice_quiet"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=session_id,
                updated_at="2026-06-25T21:54:04Z",
            )
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Alice Agent chat",
                "message_count": 2,
                "messages": [
                    {"role": "operator", "text": "hi"},
                    {"role": "agent", "text": "hello"},
                ],
                "updated_at": "2026-06-25T21:54:04Z",
            }
        ],
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    assert channels[0]["warnings"] == []


def test_operator_channel_projects_bound_session_after_open_chat_rebind():
    """persona.instance.open_chat rebinds an instance to an older saved chat;
    the channel must project the BOUND session, not the newest curated row.
    Observed live 2026-07-07: rebinding Alice to an older chat left her
    channel on the newest session, so the Launcher console never switched."""
    bound_session = "persona_chat_personainst_profile_alice_older"
    newest_session = "persona_chat_personainst_profile_alice_newest"
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_profile_alice",
                session_id=bound_session,
                updated_at="2026-07-07T16:10:00Z",
            )
        ],
        persona_chat_history=[
            {
                "session_id": bound_session,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Older chat",
                "message_count": 1,
                "messages": [{"role": "agent", "text": "older transcript"}],
                "updated_at": "2026-07-07T14:32:34Z",
            },
            {
                "session_id": newest_session,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Newest chat",
                "message_count": 1,
                "messages": [{"role": "agent", "text": "newest transcript"}],
                "updated_at": "2026-07-07T15:58:40Z",
            },
        ],
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    channel = channels[0]
    assert channel["session_id"] == bound_session
    assert channel["history"]["session_id"] == bound_session
    assert channel["history"]["title"] == "Older chat"


def test_operator_channel_keeps_per_session_channels_without_instance_binding():
    """History-only rows (no instance) keep one channel per session —
    the bound-session preference must not collapse or re-route them."""
    channels = operator_channel_summary(
        persona_instances=[],
        persona_chat_history=[
            {
                "session_id": "chat_older",
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Older chat",
                "message_count": 1,
                "messages": [{"role": "agent", "text": "older transcript"}],
                "updated_at": "2026-07-07T14:32:34Z",
            },
            {
                "session_id": "chat_newest",
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_profile_alice",
                "title": "Newest chat",
                "message_count": 1,
                "messages": [{"role": "agent", "text": "newest transcript"}],
                "updated_at": "2026-07-07T15:58:40Z",
            },
        ],
        persona_chat_trace=[],
    )

    assert sorted(channel["session_id"] for channel in channels) == [
        "chat_newest",
        "chat_older",
    ]


def test_operator_channel_projects_canonical_goal_conversation_and_filters_telemetry():
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[
            PersonaInstance(
                id="personainst_neko_supervisor",
                persona_id="neko_supervisor",
                role="supervisor",
                display_name="Neko Supervisor",
                profile_id=None,
                runtime_root="test-runtime",
                state=WorkerSessionState.RUNNING,
                mode="task_bound",
                current_task_id="task_live",
                goal_id="goal_live",
                session_id="persona_chat_neko_live",
                current_chat_goal="Live Neko observability proof",
                updated_at=ts,
            )
        ],
        persona_chat_history=[
            {
                "session_id": "persona_chat_neko_live",
                "persona_id": "neko_supervisor",
                "persona_instance_id": "personainst_neko_supervisor",
                "task_id": "task_live",
                "goal_id": "goal_live",
                "title": "Mission run",
                "message_count": 0,
                "messages": [],
                "updated_at": ts.isoformat(),
            }
        ],
        persona_chat_trace=[
            {
                "session_id": "persona_chat_neko_live",
                "persona_id": "neko_supervisor",
                "persona_instance_id": "personainst_neko_supervisor",
                "task_id": "task_live",
                "entries": [
                    {
                        "event": "assignment_created",
                        "persona_id": "dev",
                        "persona_instance_id": "personainst_dev",
                        "assignment_id": "assign_dev",
                        "stage_id": "implement",
                        "title": "Implement",
                        "message": "Inspect the canonical conversation and attach proof without product edits.",
                        "proof_targets": ["python -m pytest tests/agent_runtime -q"],
                        "allowed_decisions": ["scope_route", "hand_off"],
                        "ts": ts.isoformat(),
                    },
                    {
                        "event": "progress",
                        "summary": "Provider Call completed in 27400ms.",
                        "status": "running",
                        "ts": ts.isoformat(),
                    },
                    {
                        "event": "progress",
                        "summary": "Agent thinking process updated",
                        "status": "running",
                        "ts": ts.isoformat(),
                    },
                    {
                        "event": "progress",
                        "summary": "Decision Apply invalid in 1ms.",
                        "status": "failed",
                        "ts": ts.isoformat(),
                    },
                    {
                        "event": "progress",
                        "summary": "Agent decision process summarized",
                        "status": "completed",
                        "ts": ts.isoformat(),
                    },
                    {
                        "event": "progress",
                        "summary": "Neko selected Backend Dev and Launcher Dev for the graph.",
                        "status": "handoff",
                        "ts": ts.isoformat(),
                    },
                ],
            }
        ],
    )

    conversation = channels[0]["conversation"]
    assert conversation["status"] == "complete"
    assert conversation["task_id"] == "task_live"
    assert conversation["goal_id"] == "goal_live"
    # S47 removed the synthetic leading ``goal_input`` message: it could only be
    # minted from a resolved task, and no caller could supply one. The projected
    # rows are now exactly the real source rows.
    assert [message["kind"] for message in conversation["messages"]] == [
        "handoff",
        "handoff",
    ]
    assert conversation["messages"][0]["display_title"] == "Subagent prompt"
    assert "Prompted dev." in conversation["messages"][0]["display_text"]
    assert "Inspect the canonical conversation" in conversation["messages"][0]["display_text"]
    assert conversation["messages"][0]["target_persona_id"] == "dev"
    assert "Provider Call completed" not in "\n".join(
        message["display_text"] for message in conversation["messages"]
    )
    assert "Agent thinking process updated" not in "\n".join(
        message["display_text"] for message in conversation["messages"]
    )
    assert "Decision Apply invalid" not in "\n".join(
        message["display_text"] for message in conversation["messages"]
    )
    assert "Agent decision process summarized" not in "\n".join(
        message["display_text"] for message in conversation["messages"]
    )


def test_operator_channel_relationships_follow_custom_instance_graph_without_trace_mirroring():
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[
            PersonaInstance(
                id="personainst_research_lead",
                persona_id="research_lead",
                role="research_coordinator",
                display_name="Research Lead",
                profile_id=None,
                runtime_root="test-runtime",
                state=WorkerSessionState.RUNNING,
                mode="task_bound",
                current_task_id="task_live",
                goal_id="goal_live",
                session_id="persona_chat_research_live",
                current_chat_goal="Research handoff proof",
                updated_at=ts,
            ),
            PersonaInstance(
                id="personainst_fact_checker",
                persona_id="fact_checker",
                role="evidence_reviewer",
                display_name="Fact Checker",
                profile_id=None,
                runtime_root="test-runtime",
                state=WorkerSessionState.RUNNING,
                mode="task_bound",
                current_task_id="task_live",
                goal_id="goal_live",
                session_id="persona_chat_fact_checker_live",
                current_chat_goal="Research handoff proof",
                steered_by=["personainst_research_lead"],
                updated_at=ts,
            ),
        ],
        persona_chat_history=[
            {
                "session_id": "persona_chat_research_live",
                "persona_id": "research_lead",
                "persona_instance_id": "personainst_research_lead",
                "task_id": "task_live",
                "goal_id": "goal_live",
                "title": "Mission run",
                "message_count": 0,
                "messages": [],
                "updated_at": ts.isoformat(),
            }
        ],
        persona_chat_trace=[
            {
                "session_id": "persona_chat_research_live",
                "persona_id": "research_lead",
                "persona_instance_id": "personainst_research_lead",
                "task_id": "task_live",
                "goal_id": "goal_live",
                "entries": [
                    {
                        "event": "progress",
                        "summary": "Research lead routed work to the fact checker.",
                        "status": "handoff",
                        "ts": ts.isoformat(),
                    }
                ],
            },
            {
                "session_id": "persona_chat_fact_checker_live",
                "persona_id": "fact_checker",
                "persona_instance_id": "personainst_fact_checker",
                "task_id": "task_live",
                "goal_id": "goal_live",
                "entries": [
                    {
                        "event": "assignment_created",
                        "persona_id": "fact_checker",
                        "persona_instance_id": "personainst_fact_checker",
                        "assignment_id": "assign_fact_check",
                        "stage_id": "implement",
                        "title": "Implement",
                        "message": "Implement the scoped work and attach proof.",
                        "proof_targets": ["python -m pytest tests/agent_runtime -q"],
                        "ts": ts.isoformat(),
                    }
                ],
            },
        ],
    )

    root = next(channel for channel in channels if channel["persona_id"] == "research_lead")
    child = next(channel for channel in channels if channel["persona_id"] == "fact_checker")
    assert root["source_instance_ids"] == ["personainst_research_lead"]
    assert not any(
        warning["code"] == "duplicate_instances_same_channel"
        for warning in root["warnings"]
    )
    root_texts = "\n".join(
        message["display_text"] for message in root["conversation"]["messages"]
    )
    child_texts = "\n".join(
        message["display_text"] for message in child["conversation"]["messages"]
    )
    assert "Implement the scoped work" not in root_texts
    assert "Implement the scoped work" in child_texts
    assert root["conversation"]["root_thread_id"] == root["channel_id"]
    assert root["conversation"]["parent_thread_id"] is None
    assert child["conversation"]["root_thread_id"] == root["channel_id"]
    assert child["conversation"]["parent_thread_id"] == root["channel_id"]


def _graph_instance(instance_id: str, persona_id: str, *, parents=()) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id=persona_id,
        role="custom",
        display_name=persona_id.replace("_", " ").title(),
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="configured",
        steered_by=list(parents),
        updated_at=now(),
    )


def test_operator_channel_relationships_follow_multi_hop_primary_ancestry():
    instances = [
        _graph_instance("personainst_root", "root"),
        _graph_instance("personainst_middle", "middle", parents=("personainst_root",)),
        _graph_instance("personainst_leaf", "leaf", parents=("personainst_middle",)),
    ]
    by_persona = {
        row["persona_id"]: row
        for row in operator_channel_summary(
            persona_instances=instances,
            persona_chat_history=[],
            persona_chat_trace=[],
        )
    }

    assert by_persona["leaf"]["conversation"]["root_thread_id"] == by_persona["root"]["channel_id"]
    assert by_persona["leaf"]["conversation"]["parent_thread_id"] == by_persona["middle"]["channel_id"]


def test_operator_channel_missing_parent_degrades_to_standalone():
    (row,) = operator_channel_summary(
        persona_instances=[
            _graph_instance("personainst_leaf", "leaf", parents=("personainst_missing",))
        ],
        persona_chat_history=[],
        persona_chat_trace=[],
    )

    assert row["conversation"]["root_thread_id"] == row["channel_id"]
    assert row["conversation"]["parent_thread_id"] is None


def test_operator_channel_ancestry_cycle_degrades_every_member_to_standalone():
    rows = operator_channel_summary(
        persona_instances=[
            _graph_instance("personainst_alpha", "alpha", parents=("personainst_beta",)),
            _graph_instance("personainst_beta", "beta", parents=("personainst_alpha",)),
        ],
        persona_chat_history=[],
        persona_chat_trace=[],
    )

    for row in rows:
        assert row["conversation"]["root_thread_id"] == row["channel_id"]
        assert row["conversation"]["parent_thread_id"] is None


def _dev_task_instance(ts) -> PersonaInstance:
    return PersonaInstance(
        id="personainst_dev",
        persona_id="dev",
        role="developer",
        display_name="Launcher Dev Agent",
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.RUNNING,
        mode="task_bound",
        current_task_id="task_goal",
        goal_id="goal_goal",
        session_id="20260705_dev_session",
        updated_at=ts,
    )


def test_goal_conversation_projects_trace_tool_calls_as_flow_messages():
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[
            {
                "session_id": "20260705_dev_session",
                "persona_id": "dev",
                "persona_instance_id": "personainst_dev",
                "task_id": "task_goal",
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "read_file",
                        "summary": "Started tool read_file",
                        "status": "started",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:03Z",
                    },
                    {
                        "event": "tool_finished",
                        "tool_name": "read_file",
                        "summary": "Finished tool read_file: passed",
                        "status": "passed",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:05Z",
                    },
                    {
                        "event": "tool_started",
                        "tool_name": "shell_command",
                        "summary": "Started tool shell_command",
                        "status": "started",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:07Z",
                    },
                ],
            }
        ],
    )

    assert len(channels) == 1
    channel = channels[0]
    conversation = channel["conversation"]
    assert conversation["schema_version"] == 2
    kinds = [message["kind"] for message in conversation["messages"]]
    # S47: no synthetic goal_input row precedes the projected flow any more.
    assert "goal_input" not in kinds
    assert kinds.count("tool_call") == 2

    tool_calls = [message for message in conversation["messages"] if message["kind"] == "tool_call"]
    finished_call = next(m for m in tool_calls if m["tool"]["tool_name"] == "read_file")
    assert finished_call["status"] == "ok"
    assert finished_call["tool"]["status"] == "ok"
    assert finished_call["tool"]["duration_ms"] == 2000
    running_call = next(m for m in tool_calls if m["tool"]["tool_name"] == "shell_command")
    assert running_call["status"] == "running"
    assert "duration_ms" not in running_call["tool"]

    # A channel whose turns flow is not an empty channel.
    assert not any(warning["code"] == "trace_empty" for warning in channel["warnings"])


def test_goal_conversation_projects_per_step_thinking_between_tool_calls():
    """The streamed think→act loop projects each trace reasoning row in order."""

    ts = now()
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[
            {
                "session_id": "20260705_dev_session",
                "persona_id": "dev",
                "persona_instance_id": "personainst_dev",
                "task_id": "task_goal",
                "entries": [
                    {
                        "event": "progress",
                        "summary": "Agent thinking process updated",
                        "reasoning_summary": "Reading the probe doc before writing anything.",
                        "status": "running",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:02Z",
                    },
                    {
                        "event": "tool_finished",
                        "tool_name": "read_file",
                        "summary": "Finished tool read_file: passed",
                        "status": "passed",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:05Z",
                    },
                    {
                        "event": "progress",
                        "summary": "Agent thinking process updated",
                        "reasoning_summary": "File verified; now running the echo proof.",
                        "status": "running",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:08Z",
                    },
                    {
                        "event": "progress",
                        "summary": "Agent thinking process updated",
                        "reasoning_summary": "Reviewed the Petdex widget tree before patching (run_a).",
                        "status": "running",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:30Z",
                    },
                ],
            }
        ],
    )

    conversation = channels[0]["conversation"]
    thinking = [m for m in conversation["messages"] if m["kind"] == "thinking_summary"]
    texts = [m["display_text"] for m in thinking]
    assert "Reading the probe doc before writing anything." in texts
    assert "File verified; now running the echo proof." in texts
    assert texts.count("Reviewed the Petdex widget tree before patching (run_a).") == 1
    for message in thinking:
        assert message["display_title"] == "Thinking"
    # Timeline order: first per-step thinking precedes the tool call, the
    # second follows it.
    kinds_in_order = [
        (m["kind"], m.get("display_text", "")) for m in conversation["messages"]
    ]
    first_thinking_index = next(
        i for i, (kind, text) in enumerate(kinds_in_order)
        if kind == "thinking_summary" and text.startswith("Reading the probe doc")
    )
    tool_index = next(
        i for i, (kind, _) in enumerate(kinds_in_order) if kind == "tool_call"
    )
    second_thinking_index = next(
        i for i, (kind, text) in enumerate(kinds_in_order)
        if kind == "thinking_summary" and text.startswith("File verified")
    )
    assert first_thinking_index < tool_index < second_thinking_index


def test_tool_call_messages_carry_operator_detail():
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[
            {
                "session_id": "20260705_dev_session",
                "persona_id": "dev",
                "persona_instance_id": "personainst_dev",
                "task_id": "task_goal",
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "terminal",
                        "summary": "Started tool terminal: flutter test",
                        "status": "started",
                        "run_id": "run_a",
                        "command": "flutter test test/features/library/petdex_menu_test.dart",
                        "ts": "2026-07-05T05:48:03Z",
                    },
                    {
                        "event": "tool_finished",
                        "tool_name": "terminal",
                        "summary": "Finished tool terminal: passed",
                        "status": "passed",
                        "run_id": "run_a",
                        "output": "00:05 +12: All tests passed!",
                        "exit_code": 0,
                        "duration_ms": 5300,
                        "ts": "2026-07-05T05:48:09Z",
                    },
                    {
                        "event": "tool_finished",
                        "tool_name": "patch",
                        "summary": "Patched 2 files: a.dart, b.dart",
                        "status": "passed",
                        "run_id": "run_a",
                        "paths": ["lib/features/library/a.dart", "lib/features/library/b.dart"],
                        "files": ["a.dart", "b.dart"],
                        "ts": "2026-07-05T05:49:00Z",
                    },
                ],
            }
        ],
    )

    tool_calls = [
        message
        for message in channels[0]["conversation"]["messages"]
        if message["kind"] == "tool_call"
    ]
    terminal = next(m for m in tool_calls if m["tool"]["tool_name"] == "terminal")
    assert terminal["tool"]["command"] == "flutter test test/features/library/petdex_menu_test.dart"
    assert terminal["tool"]["output"] == "00:05 +12: All tests passed!"
    assert terminal["tool"]["exit_code"] == 0
    # Entry-reported duration wins over the ts delta.
    assert terminal["tool"]["duration_ms"] == 5300
    patch = next(m for m in tool_calls if m["tool"]["tool_name"] == "patch")
    assert patch["tool"]["paths"] == [
        "lib/features/library/a.dart",
        "lib/features/library/b.dart",
    ]


def test_tool_call_message_carries_dispatch_target_and_order():
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[
            {
                "session_id": "20260705_dev_session",
                "persona_id": "dev",
                "persona_instance_id": "personainst_dev",
                "task_id": "task_goal",
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "agent_chat_send",
                        "summary": "Started tool agent_chat_send: → backend_dev: run a bounded check",
                        "status": "started",
                        "run_id": "run_a",
                        "target": "→ backend_dev: run a bounded check",
                        "dispatch_target": "backend_dev",
                        "dispatch_order": (
                            "From Neko: run a bounded backend health check.\n"
                            "Keep it lightweight; no repo commits."
                        ),
                        "ts": "2026-07-05T05:48:03Z",
                    },
                    {
                        # The finished row carries NO dispatch fields — the merge
                        # must preserve the started row's values, not erase them.
                        "event": "tool_finished",
                        "tool_name": "agent_chat_send",
                        "summary": "Finished tool agent_chat_send: passed",
                        "status": "passed",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:09Z",
                    },
                ],
            }
        ],
    )

    tool_calls = [
        message
        for message in channels[0]["conversation"]["messages"]
        if message["kind"] == "tool_call"
    ]
    dispatch = next(m for m in tool_calls if m["tool"]["tool_name"] == "agent_chat_send")
    assert dispatch["tool"]["dispatch_target"] == "backend_dev"
    assert dispatch["tool"]["dispatch_order"] == (
        "From Neko: run a bounded backend health check.\n"
        "Keep it lightweight; no repo commits."
    )
    # The pair collapsed to one ok row with the fields intact after finish.
    assert dispatch["status"] == "ok"


def test_native_reasoning_mints_thinking_row_and_reply_echo_is_deduped():
    session_id = "persona_chat_personainst_neko_fanout"
    reply_text = "Dispatched to backend_dev, dev, and qa; each got a one-line bounded check."
    channels = operator_channel_summary(
        persona_instances=[
            _instance(
                "personainst_neko",
                session_id=session_id,
                updated_at="2026-07-17T14:00:08Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_neko",
                "title": "Neko chat",
                "message_count": 2,
                "messages": [
                    {
                        "id": "operator_1",
                        "role": "operator",
                        "text": "fan out a bounded check to backend_dev, dev, qa",
                        "client_message_id": "op-1",
                        "timestamp": "2026-07-17T14:00:00Z",
                    },
                    {
                        "id": "agent_1",
                        "role": "agent",
                        "text": reply_text,
                        "client_message_id": "op-1",
                        "timestamp": "2026-07-17T14:00:08Z",
                    },
                ],
                "updated_at": "2026-07-17T14:00:08Z",
            }
        ],
        persona_chat_trace=[
            {
                "session_id": session_id,
                "persona_id": "profile:alice",
                "persona_instance_id": "personainst_neko",
                "entries": [
                    {
                        # Native model reasoning (distinct from the reply) — this
                        # is the G3 emit; it must mint a first-class thinking row.
                        "event": "progress",
                        "summary": "Agent thinking process updated",
                        "reasoning_summary": "I'll fan out one bounded order to each teammate, then summarize who I told what.",
                        "status": "running",
                        "turn_id": "op-1",
                        "ts": "2026-07-17T14:00:01Z",
                    },
                    {
                        # A trailing reasoning step whose text IS the reply (the
                        # content echo) — dedup must drop it against reply_texts.
                        "event": "progress",
                        "summary": "Agent thinking process updated",
                        "reasoning_summary": reply_text,
                        "status": "running",
                        "turn_id": "op-1",
                        "ts": "2026-07-17T14:00:07Z",
                    },
                ],
            }
        ],
    )

    messages = channels[0]["conversation"]["messages"]
    thinking_texts = [m["display_text"] for m in messages if m["kind"] == "thinking_summary"]
    # The native reasoning surfaced as a thinking row.
    assert any(t.startswith("I'll fan out one bounded order") for t in thinking_texts)
    # The reply-echo thinking row was deduped against the real reply.
    assert reply_text not in thinking_texts
    # And the real reply survives exactly once.
    replies = [m for m in messages if m["kind"] == "reply" and m["display_text"] == reply_text]
    assert len(replies) == 1


def test_conversation_message_ids_stable_across_rebuilds():
    ts = now()
    kwargs = dict(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[
            {
                "session_id": "20260705_dev_session",
                "persona_id": "dev",
                "persona_instance_id": "personainst_dev",
                "task_id": "task_goal",
                "entries": [
                    {
                        "event": "tool_started",
                        "tool_name": "read_file",
                        "summary": "Started tool read_file",
                        "status": "started",
                        "run_id": "run_a",
                        "ts": "2026-07-05T05:48:03Z",
                    },
                ],
            }
        ],
    )

    first = operator_channel_summary(**kwargs)[0]["conversation"]["messages"]
    second = operator_channel_summary(**kwargs)[0]["conversation"]["messages"]
    assert [message["id"] for message in first] == [message["id"] for message in second]
    assert len({message["id"] for message in first}) == len(first)


def test_operator_channel_reports_empty_conversation_for_new_chats():
    channels = operator_channel_summary(
        persona_instances=[],
        persona_chat_history=[],
        persona_chat_trace=[],
    )

    assert channels == []

    # A brand-new chat session with no messages yet is NOT a contract breach:
    # it must project as "empty" (no intervention row in Mission Control),
    # not "incomplete".
    orphan = operator_channel_summary(
        persona_instances=[],
        persona_chat_history=[
            {
                "session_id": "persona_chat_orphan",
                "persona_id": "neko_supervisor",
                "persona_instance_id": "personainst_neko_supervisor",
                "messages": [],
            }
        ],
        persona_chat_trace=[],
    )[0]

    assert orphan["conversation_status"] == "empty"
    assert orphan["conversation"]["incomplete_reason"] is None


def test_operator_channel_reports_incomplete_when_sources_exist_but_nothing_projects():
    # Source rows exist but every one fails projection (blank text) — that IS
    # the contract breach "incomplete" is reserved for.
    orphan = operator_channel_summary(
        persona_instances=[],
        persona_chat_history=[
            {
                "session_id": "persona_chat_orphan",
                "persona_id": "neko_supervisor",
                "persona_instance_id": "personainst_neko_supervisor",
                "messages": [{"role": "user", "text": "   "}],
            }
        ],
        persona_chat_trace=[],
    )[0]

    assert orphan["conversation_status"] == "incomplete"
    assert orphan["conversation"]["incomplete_reason"]


def test_conversation_history_message_carries_pre_trace_ack_kind():
    # A history row marked with the pre_trace_ack kind keeps that typed kind in
    # the projected conversation message (role stays agent) so the Launcher
    # collapses/drops it structurally.
    message = _conversation_history_message(
        {
            "id": "m_ack",
            "role": "agent",
            "text": "I'll load the relevant guidance first, then report back with the useful part.",
            "kind": PERSONA_PRE_TRACE_ACK_KIND,
            "timestamp": "2026-07-13T20:51:00Z",
        },
        channel_id="chan_neko",
        index=1,
        persona_id="neko_supervisor",
        persona_instance_id="personainst_neko_supervisor",
    )
    assert message is not None
    assert message["role"] == "agent"
    assert message["kind"] == PERSONA_PRE_TRACE_ACK_KIND


def test_conversation_history_message_normal_reply_kind_is_reply():
    message = _conversation_history_message(
        {
            "id": "m_reply",
            "role": "agent",
            "text": "Currently one skill is loaded: hermes-agent.",
            "timestamp": "2026-07-13T20:51:02Z",
        },
        channel_id="chan_neko",
        index=2,
        persona_id="neko_supervisor",
        persona_instance_id="personainst_neko_supervisor",
    )
    assert message is not None
    assert message["kind"] == "reply"


_RUN_BUDGET_BLOCK = {
    "bounded_by": "wall",
    "budgets": [
        {
            "bound": "wall",
            "limit": 59.5,
            "consumed": 30.0,
            "tripped": True,
        }
    ],
}


def test_conversation_history_message_marker_carries_run_budget():
    # The terminal marker row is the ONLY row a reply-less budget_exhausted
    # turn gets, so the accounting block must survive this projection or the
    # cockpit can never read what bounded the turn.
    message = _conversation_history_message(
        {
            "id": "m_marker",
            "role": "system",
            "kind": PERSONA_TURN_BUDGET_EXHAUSTED_KIND,
            "text": "Wall budget reached before the turn settled.",
            "timestamp": "2026-07-27T05:00:00Z",
            "run_budget": _RUN_BUDGET_BLOCK,
        },
        channel_id="chan_neko",
        index=3,
        persona_id="neko_supervisor",
        persona_instance_id="personainst_neko_supervisor",
    )
    assert message is not None
    assert message["kind"] == PERSONA_TURN_BUDGET_EXHAUSTED_KIND
    assert message["run_budget"] == _RUN_BUDGET_BLOCK


def test_conversation_history_message_reply_carries_run_budget():
    message = _conversation_history_message(
        {
            "id": "m_reply_budget",
            "role": "agent",
            "text": "Settled under the wall.",
            "timestamp": "2026-07-27T05:00:01Z",
            "run_budget": _RUN_BUDGET_BLOCK,
        },
        channel_id="chan_neko",
        index=4,
        persona_id="neko_supervisor",
        persona_instance_id="personainst_neko_supervisor",
    )
    assert message is not None
    assert message["run_budget"] == _RUN_BUDGET_BLOCK


def test_conversation_history_message_without_run_budget_omits_key():
    # Absence-preserving in both directions: no block and an empty block both
    # project WITHOUT the key — an older turn never reads as "accounted".
    for row_extra in ({}, {"run_budget": {}}):
        message = _conversation_history_message(
            {
                "id": "m_reply_plain",
                "role": "agent",
                "text": "No accounting on this turn.",
                "timestamp": "2026-07-27T05:00:02Z",
                **row_extra,
            },
            channel_id="chan_neko",
            index=5,
            persona_id="neko_supervisor",
            persona_instance_id="personainst_neko_supervisor",
        )
        assert message is not None
        assert "run_budget" not in message


def test_conversation_history_message_canonicalizes_typed_assistant_identity():
    message = _conversation_history_message(
        {
            "id": "m_reply",
            "role": "agent",
            "text": "Hi Tony — Neko here. What’s the mission?",
            "client_message_id": "agent-chat-send-1784795889013735:assistant:1",
            # Preserve rollout compatibility with snapshots produced by the
            # buggy projector: the typed assistant id remains the authority.
            "turn_id": "agent-chat-send-1784795889013735_assistant_1",
            "timestamp": "2026-07-23T08:38:09Z",
        },
        channel_id="chan_neko",
        index=2,
        persona_id="neko_supervisor",
        persona_instance_id="personainst_neko_supervisor",
    )

    assert message is not None
    assert (
        message["client_message_id"]
        == "agent-chat-send-1784795889013735:assistant:1"
    )
    assert message["turn_id"] == "agent-chat-send-1784795889013735"


def _qa_instance(instance_id: str, *, session_id: str, display_name: str, updated_at: str) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id="qa",
        role="qa",
        display_name=display_name,
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id=session_id,
        updated_at=updated_at,
    )


def test_operator_channels_are_instance_scoped_for_same_persona_siblings():
    # 2026-07-18 channel-fold regression: two live instances of ONE persona, each
    # threading its OWN session, must project TWO channels, each carrying only its
    # own turns — no cross-bleed, no duplicate_instances_same_channel warning.
    primary_session = "persona_chat_personainst_qa_84406cdb480e"
    sibling_session = "persona_chat_personainst_qa_agent_2_32063a99b165"
    channels = operator_channel_summary(
        persona_instances=[
            _qa_instance(
                "personainst_qa",
                session_id=primary_session,
                display_name="QA Agent",
                updated_at="2026-07-18T06:50:26Z",
            ),
            _qa_instance(
                "personainst_qa_agent_2",
                session_id=sibling_session,
                display_name="QA Agent (2)",
                updated_at="2026-07-18T06:48:15Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": primary_session,
                "persona_id": "qa",
                "persona_instance_id": "personainst_qa",
                "title": "QA Agent chat",
                "message_count": 2,
                "messages": [
                    {"role": "operator", "text": "Hi test test"},
                    {"role": "agent", "text": "Hi! QA Agent online and ready."},
                ],
                "updated_at": "2026-07-18T06:50:26Z",
            },
            {
                "session_id": sibling_session,
                "persona_id": "qa",
                "persona_instance_id": "personainst_qa_agent_2",
                "title": "QA Agent (2) chat",
                "message_count": 2,
                "messages": [
                    {"role": "operator", "text": "Hi test test"},
                    {"role": "agent", "text": "QA Agent (2) here."},
                ],
                "updated_at": "2026-07-18T06:48:15Z",
            },
        ],
        persona_chat_trace=[],
    )

    assert len(channels) == 2
    by_instance = {channel["persona_instance_id"]: channel for channel in channels}
    assert set(by_instance) == {"personainst_qa", "personainst_qa_agent_2"}

    primary = by_instance["personainst_qa"]
    sibling = by_instance["personainst_qa_agent_2"]
    # Distinct channel identities keyed on the instance's own session.
    assert primary["channel_id"] != sibling["channel_id"]
    assert primary["session_id"] == primary_session
    assert sibling["session_id"] == sibling_session

    # Each channel carries ONLY its own instance's turns — the canonical reply
    # must not leak into the sibling's channel.
    primary_texts = [m.get("display_text") for m in primary["conversation"]["messages"]]
    sibling_texts = [m.get("display_text") for m in sibling["conversation"]["messages"]]
    assert "Hi! QA Agent online and ready." in primary_texts
    assert "Hi! QA Agent online and ready." not in sibling_texts
    assert "QA Agent (2) here." in sibling_texts
    assert "QA Agent (2) here." not in primary_texts

    for channel in channels:
        assert channel["source_instance_ids"] == [channel["persona_instance_id"]]
        assert not any(
            warning["code"] == "duplicate_instances_same_channel"
            for warning in channel["warnings"]
        )


def test_duplicate_instances_warning_fires_only_on_true_canonical_collision():
    # Two DIFFERENT canonical instance ids resolving onto ONE session is a genuine
    # identity collision (they should never share a chat lane) — the guard fires.
    # This is distinct from the legitimate-sibling case above, where distinct
    # sessions keep the instances in distinct channels.
    session_id = "persona_chat_personainst_qa_84406cdb480e"
    channels = operator_channel_summary(
        persona_instances=[
            _qa_instance(
                "personainst_qa",
                session_id=session_id,
                display_name="QA Agent",
                updated_at="2026-07-18T06:50:26Z",
            ),
            _qa_instance(
                "personainst_qa_agent_2",
                session_id=session_id,
                display_name="QA Agent (2)",
                updated_at="2026-07-18T06:48:15Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "qa",
                "persona_instance_id": "personainst_qa",
                "title": "QA Agent chat",
                "message_count": 1,
                "messages": [{"role": "operator", "text": "collision"}],
                "updated_at": "2026-07-18T06:50:26Z",
            }
        ],
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    channel = channels[0]
    assert set(channel["source_instance_ids"]) == {
        "personainst_qa",
        "personainst_qa_agent_2",
    }
    assert any(
        warning["code"] == "duplicate_instances_same_channel"
        for warning in channel["warnings"]
    )


# --------------------------------------------------------------------------- #
# Relayed-message conversation projection (sender attribution)                  #
# --------------------------------------------------------------------------- #


def _relayed_row(*, sender_persona_id, sender_instance_id, text="From Neko: hi"):
    return {
        "id": "row-relay",
        "role": "operator",
        "kind": "relayed_message",
        "relay_sender_persona_id": sender_persona_id,
        "relay_sender_instance_id": sender_instance_id,
        "text": text,
        "timestamp": "2026-07-18T00:00:00Z",
        "client_message_id": "cm-relay-1",
    }


def test_conversation_history_message_relayed_attributes_sender_and_names_it():
    message = _conversation_history_message(
        _relayed_row(sender_persona_id="neko", sender_instance_id="personainst_neko"),
        channel_id="qa::sess",
        index=0,
        persona_id="qa",
        persona_instance_id="personainst_qa",
        display_names={"personainst_neko": "Neko Mission Lead"},
    )
    assert message["kind"] == "relayed_message"
    assert message["actor_persona_id"] == "neko"
    assert message["actor_instance_id"] == "personainst_neko"
    assert message["actor_display_name"] == "Neko Mission Lead"
    # Lane semantics unchanged: an old consumer ignoring the typed kind still
    # renders it on the operator lane.
    assert message["role"] == "operator"


def test_conversation_history_message_relayed_omits_name_when_instance_absent():
    # The sender instance is not in the roster map → no fabricated name; and an
    # unresolvable sender persona degrades to the honest "agent".
    message = _conversation_history_message(
        _relayed_row(sender_persona_id=None, sender_instance_id=None),
        channel_id="qa::sess",
        index=0,
        persona_id="qa",
        persona_instance_id="personainst_qa",
        display_names={"personainst_neko": "Neko Mission Lead"},
    )
    assert message["kind"] == "relayed_message"
    assert message["actor_persona_id"] == "agent"
    assert message["actor_instance_id"] is None
    assert "actor_display_name" not in message


def test_conversation_history_message_true_operator_row_is_unchanged():
    message = _conversation_history_message(
        {"id": "row-op", "role": "operator", "text": "ping", "client_message_id": "cm-op"},
        channel_id="qa::sess",
        index=0,
        persona_id="qa",
        persona_instance_id="personainst_qa",
        display_names={"personainst_neko": "Neko Mission Lead"},
    )
    assert message["kind"] == "operator_message"
    assert message["actor_persona_id"] == "operator"
    assert message["actor_instance_id"] is None
    assert "actor_display_name" not in message


def test_conversation_history_message_carries_runtime_context_reference():
    message = _conversation_history_message(
        {
            "id": "row-op",
            "role": "operator",
            "text": "ping",
            "client_message_id": "cm-op",
            "runtime_context": {
                "context_id": "ctx_ping",
                "revision": "hud_0123456789abcdef",
                "delivery": "unchanged",
            },
        },
        channel_id="qa::sess",
        index=0,
        persona_id="qa",
        persona_instance_id="personainst_qa",
    )
    assert message["display_text"] == "ping"
    assert message["runtime_context"] == {
        "context_id": "ctx_ping",
        "revision": "hud_0123456789abcdef",
        "delivery": "unchanged",
    }


def test_operator_channel_summary_names_relayed_sender_from_full_roster():
    # display_names is built from the FULL roster, so a relay INTO qa's channel
    # can name neko even though neko is a different channel's owner.
    session_id = "persona_chat_personainst_qa_relaya1b2c3d4"
    channels = operator_channel_summary(
        persona_instances=[
            PersonaInstance(
                id="personainst_qa",
                persona_id="qa",
                role="seed",
                display_name="QA Agent",
                profile_id=None,
                runtime_root="test-runtime",
                state=WorkerSessionState.IDLE,
                mode="chat",
                session_id=session_id,
                updated_at="2026-07-18T00:00:00Z",
            ),
            PersonaInstance(
                id="personainst_neko",
                persona_id="neko",
                role="seed",
                display_name="Neko Mission Lead",
                profile_id=None,
                runtime_root="test-runtime",
                state=WorkerSessionState.IDLE,
                mode="chat",
                session_id="persona_chat_personainst_neko_ffff0000ffff",
                updated_at="2026-07-18T00:00:00Z",
            ),
        ],
        persona_chat_history=[
            {
                "session_id": session_id,
                "persona_id": "qa",
                "persona_instance_id": "personainst_qa",
                "title": "QA Agent chat",
                "message_count": 1,
                "messages": [
                    _relayed_row(
                        sender_persona_id="neko",
                        sender_instance_id="personainst_neko",
                        text="From Neko: status?",
                    )
                ],
                "updated_at": "2026-07-18T00:00:00Z",
            }
        ],
        persona_chat_trace=[],
    )
    qa_channel = [c for c in channels if c["session_id"] == session_id]
    assert len(qa_channel) == 1
    relayed = [
        m for m in qa_channel[0]["conversation"]["messages"] if m.get("kind") == "relayed_message"
    ]
    assert len(relayed) == 1
    assert relayed[0]["actor_persona_id"] == "neko"
    assert relayed[0]["actor_instance_id"] == "personainst_neko"
    assert relayed[0]["actor_display_name"] == "Neko Mission Lead"
