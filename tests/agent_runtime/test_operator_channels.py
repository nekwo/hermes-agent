from agent_runtime.models import PersonaInstance
from agent_runtime.models import Task
from agent_runtime.operator_channels import operator_channel_summary
from agent_runtime.states import TaskState, WorkerSessionState
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
                        "client_message_id": "agent-chat-send-1",
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
    assert agent["client_message_id"] == "agent-chat-send-1"
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


def test_operator_channel_reports_missing_history_loudly():
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
        persona_chat_trace=[],
    )

    assert len(channels) == 1
    assert any(
        warning["code"] == "session_without_history"
        for warning in channels[0]["warnings"]
    )
    assert any(warning["code"] == "trace_empty" for warning in channels[0]["warnings"])


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
    task = Task(
        id="task_live",
        goal_id="goal_live",
        title="Live Neko observability proof",
        description="Route the goal prompt into Neko and show safe progress.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        acceptance_criteria=["Agent Console shows the initial goal input."],
    )
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
        tasks=[task],
    )

    conversation = channels[0]["conversation"]
    assert conversation["status"] == "complete"
    assert conversation["task_id"] == "task_live"
    assert conversation["goal_id"] == "goal_live"
    assert [message["kind"] for message in conversation["messages"]] == [
        "goal_input",
        "handoff",
        "handoff",
    ]
    assert "Goal: Live Neko observability proof" in conversation["messages"][0]["display_text"]
    assert conversation["messages"][1]["display_title"] == "Subagent prompt"
    assert "Prompted dev." in conversation["messages"][1]["display_text"]
    assert "Inspect the canonical conversation" in conversation["messages"][1]["display_text"]
    assert conversation["messages"][1]["target_persona_id"] == "dev"
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


def test_operator_channel_mirrors_child_assignment_without_claiming_child_instance():
    ts = now()
    task = Task(
        id="task_live",
        goal_id="goal_live",
        title="Neko root handoff proof",
        description="Show child prompts on the root conversation.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )
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
                current_chat_goal="Neko root handoff proof",
                updated_at=ts,
            ),
            PersonaInstance(
                id="personainst_dev",
                persona_id="dev",
                role="developer",
                display_name="Dev",
                profile_id=None,
                runtime_root="test-runtime",
                state=WorkerSessionState.RUNNING,
                mode="task_bound",
                current_task_id="task_live",
                goal_id="goal_live",
                session_id="persona_chat_dev_live",
                current_chat_goal="Neko root handoff proof",
                updated_at=ts,
            ),
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
                "goal_id": "goal_live",
                "entries": [
                    {
                        "event": "progress",
                        "summary": "Neko routed work to Dev.",
                        "status": "handoff",
                        "ts": ts.isoformat(),
                    }
                ],
            },
            {
                "session_id": "persona_chat_dev_live",
                "persona_id": "dev",
                "persona_instance_id": "personainst_dev",
                "task_id": "task_live",
                "goal_id": "goal_live",
                "entries": [
                    {
                        "event": "assignment_created",
                        "persona_id": "dev",
                        "persona_instance_id": "personainst_dev",
                        "assignment_id": "assign_dev",
                        "stage_id": "implement",
                        "title": "Implement",
                        "message": "Implement the scoped work and attach proof.",
                        "proof_targets": ["python -m pytest tests/agent_runtime -q"],
                        "ts": ts.isoformat(),
                    }
                ],
            },
        ],
        tasks=[task],
    )

    root = next(channel for channel in channels if channel["persona_id"] == "neko_supervisor")
    assert root["source_instance_ids"] == ["personainst_neko_supervisor"]
    assert not any(
        warning["code"] == "duplicate_instances_same_channel"
        for warning in root["warnings"]
    )
    texts = "\n".join(
        message["display_text"] for message in root["conversation"]["messages"]
    )
    assert "Prompted dev." in texts
    assert "Implement the scoped work" in texts


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


def _goal_task(ts) -> Task:
    return Task(
        id="task_goal",
        goal_id="goal_goal",
        title="Enterprise-grade Petdex library menu",
        description="Complete the implement-stage proof package.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
    )


def _dev_run_summary(run_id: str, *, started: str, finished: str, **overrides):
    run = {
        "run_id": run_id,
        "persona_id": "dev",
        "task_id": "task_goal",
        "stage_id": "implement",
        "state": "completed",
        "started_at": started,
        "finished_at": finished,
        "duration_ms": 1000,
        "decision_type": "hand_off",
        "decision_summary": f"Handed off the Petdex menu ({run_id}).",
        "decision_rationale": None,
        "reasoning_summary": f"Reviewed the Petdex widget tree before patching ({run_id}).",
        "llm": {"model": "gpt-5.5", "input_tokens": 1000, "output_tokens": 200, "latency_ms": 900},
        "has_error": False,
    }
    run.update(overrides)
    return run


def test_goal_conversation_projects_turns_and_tool_calls_as_flow_messages():
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
        tasks=[_goal_task(ts)],
        run_summaries=[
            _dev_run_summary("run_a", started="2026-07-05T05:48:00Z", finished="2026-07-05T05:49:00Z")
        ],
    )

    assert len(channels) == 1
    channel = channels[0]
    conversation = channel["conversation"]
    assert conversation["schema_version"] == 2
    kinds = [message["kind"] for message in conversation["messages"]]
    assert kinds[0] == "goal_input"
    assert "thinking_summary" in kinds
    assert "turn" in kinds
    assert kinds.count("tool_call") == 2

    by_kind = {message["kind"]: message for message in conversation["messages"]}
    thinking = by_kind["thinking_summary"]
    assert thinking["display_title"] == "Thinking"
    assert "Reviewed the Petdex widget tree" in thinking["display_text"]
    turn = by_kind["turn"]
    assert turn["display_title"] == "Turn"
    assert turn["llm"]["model"] == "gpt-5.5"
    assert turn["refs"]["decision_type"] == "hand_off"
    assert turn["refs"]["run_id"] == "run_a"

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
    """The streamed think→act loop: each progress row carrying real reasoning
    becomes its own Thinking message in timeline order, and a per-step text
    that repeats the per-run summary is deduped to a single bubble."""

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
                        # Final step repeats the run-summary reasoning — must
                        # collapse into a single Thinking bubble.
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
        tasks=[_goal_task(ts)],
        run_summaries=[
            _dev_run_summary("run_a", started="2026-07-05T05:48:00Z", finished="2026-07-05T05:49:00Z")
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
        tasks=[_goal_task(ts)],
        run_summaries=[],
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


def test_trace_empty_warning_suppressed_when_flow_messages_exist_without_trace():
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[],
        tasks=[_goal_task(ts)],
        run_summaries=[
            _dev_run_summary("run_b", started="2026-07-05T05:50:00Z", finished="2026-07-05T05:51:00Z")
        ],
    )

    channel = channels[0]
    kinds = [message["kind"] for message in channel["conversation"]["messages"]]
    assert "turn" in kinds
    assert not any(warning["code"] == "trace_empty" for warning in channel["warnings"])

    # Without run summaries (status-page caller shape) the warning still fires.
    legacy = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[],
        tasks=[_goal_task(ts)],
    )[0]
    assert any(warning["code"] == "trace_empty" for warning in legacy["warnings"])


def test_failed_run_projects_blocker_turn():
    ts = now()
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[],
        tasks=[_goal_task(ts)],
        run_summaries=[
            _dev_run_summary(
                "run_err",
                started="2026-07-05T05:52:00Z",
                finished="2026-07-05T05:52:30Z",
                state="failed",
                has_error=True,
                decision_type=None,
                decision_summary=None,
                reasoning_summary=None,
            )
        ],
    )

    turn = next(m for m in channels[0]["conversation"]["messages"] if m["kind"] == "turn")
    assert turn["role"] == "blocker"
    assert turn["status"] == "failed"
    assert turn["display_title"] == "Turn failed"


def test_conversation_caps_and_emits_turns_collapsed_marker():
    ts = now()
    run_summaries = [
        _dev_run_summary(
            f"run_{index:04d}",
            started=f"2026-07-05T0{5 + index // 3600}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
            finished=f"2026-07-05T0{5 + index // 3600}:{(index // 60) % 60:02d}:{index % 60:02d}Z",
        )
        for index in range(150)
    ]
    channels = operator_channel_summary(
        persona_instances=[_dev_task_instance(ts)],
        persona_chat_history=[],
        persona_chat_trace=[],
        tasks=[_goal_task(ts)],
        run_summaries=run_summaries,
    )

    messages = channels[0]["conversation"]["messages"]
    assert len(messages) <= 201  # cap + goal_input protection headroom
    kinds = [message["kind"] for message in messages]
    assert kinds[0] == "goal_input"
    assert "turns_collapsed" in kinds
    marker = next(m for m in messages if m["kind"] == "turns_collapsed")
    assert "collapsed" in marker["display_text"]
    assert marker["refs"]["collapsed_count"] > 0
    # The newest turn survives the trim.
    assert any(m["kind"] == "turn" and "run_0149" in m["refs"]["run_id"] for m in messages)


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
        tasks=[_goal_task(ts)],
        run_summaries=[
            _dev_run_summary("run_a", started="2026-07-05T05:48:00Z", finished="2026-07-05T05:49:00Z")
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
        tasks=[],
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
        tasks=[],
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
        tasks=[],
    )[0]

    assert orphan["conversation_status"] == "incomplete"
    assert orphan["conversation"]["incomplete_reason"]
