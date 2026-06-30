from agent_runtime.events import EventLog
from agent_runtime.progress import ChatProgressSink, _safe_progress_payload


def test_safe_progress_payload_preserves_dev_work_file_summary_but_not_paths():
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "phase": "dev_work",
            "step": "patch",
            "tool_name": "patch",
            "status": "passed",
            "summary": "Patched 2 files: mission_control_page.dart, private_token.dart",
            "detail": "Changed files: mission_control_page.dart, C:/Users/beast/private_token.dart",
            "patch_summary": "Patched 2 files",
            "changed_files": [
                "mission_control_page.dart",
                "C:/Users/beast/private_token.dart",
                "api_key.txt",
            ],
            "files_touched": 3,
            "diff": "SECRET raw diff must be dropped",
        },
    )

    assert payload == {
        "type": "run.tool.finished",
        "phase": "dev_work",
        "step": "patch",
        "tool_name": "patch",
        "status": "passed",
        "patch_summary": "Patched 2 files",
        "changed_files": ["mission_control_page.dart"],
        "files_touched": 3,
    }
    encoded = repr(payload)
    assert "C:/Users" not in encoded
    assert "api_key" not in encoded
    assert "SECRET" not in encoded


def test_safe_progress_payload_preserves_agent_thinking_summary_only():
    payload = _safe_progress_payload(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "thinking_process",
            "step": "reasoning_summary",
            "status": "running",
            "summary": "Agent thinking process updated",
            "reasoning_summary": "Checking proof coverage before QA handoff.",
            "raw_thoughts": "SECRET hidden chain-of-thought must be dropped",
            "detail": "C:/Users/beast/private_token.txt must be dropped",
        },
    )

    assert payload == {
        "type": "run.progress",
        "phase": "thinking_process",
        "step": "reasoning_summary",
        "status": "running",
        "summary": "Agent thinking process updated",
        "reasoning_summary": "Checking proof coverage before QA handoff.",
    }
    encoded = repr(payload)
    assert "raw_thoughts" not in encoded
    assert "SECRET" not in encoded
    assert "C:/Users" not in encoded


def test_chat_progress_sink_records_session_keyed_tool_events(isolate_agent_runtime_root):
    log = EventLog()
    sink = ChatProgressSink(session_id="chat_1", persona_id="neko_supervisor", event_log=log)

    sink.emit(
        "run.tool.started",
        {"type": "run.tool.started", "tool_name": "terminal", "command_label": "echo PARITY_OK_2026"},
    )
    sink.emit(
        "run.tool.finished",
        {"type": "run.tool.finished", "tool_name": "terminal", "status": "passed", "exit_code": 0},
    )

    rows = log.for_session("chat_1")
    assert [event.type for event in rows] == ["run.tool.started", "run.tool.finished"]
    # Chat trace is session-keyed, not task-keyed, and carries the persona.
    assert all(event.task_id is None for event in rows)
    assert all(event.session_id == "chat_1" for event in rows)
    assert all(event.persona_id == "neko_supervisor" for event in rows)
    assert rows[0].payload["command_label"] == "echo PARITY_OK_2026"


def test_chat_progress_sink_drops_noise_and_secrets(isolate_agent_runtime_root):
    log = EventLog()
    sink = ChatProgressSink(session_id="chat_1", persona_id="dev", event_log=log)

    # Generic progress with no tool/command/reasoning signal is dropped.
    sink.emit("run.progress", {"type": "run.progress", "summary": "Run progress update"})
    # A non-trace event type is ignored entirely.
    sink.emit("task.transition", {"type": "task.transition"})
    # Signal-bearing progress is kept but raw secrets are redacted out.
    sink.emit(
        "run.progress",
        {
            "type": "run.progress",
            "reasoning_summary": "Deciding whether to run the suite.",
            "raw_thoughts": "SECRET hidden chain of thought",
        },
    )

    rows = log.for_session("chat_1")
    assert [event.type for event in rows] == ["run.progress"]
    assert rows[0].payload["reasoning_summary"] == "Deciding whether to run the suite."
    assert "SECRET" not in repr(rows)


def test_chat_progress_sink_without_session_records_nothing(isolate_agent_runtime_root):
    log = EventLog()
    sink = ChatProgressSink(session_id="", persona_id="dev", event_log=log)
    sink.emit("run.tool.started", {"type": "run.tool.started", "tool_name": "terminal"})
    assert log.tail(5) == []


def test_chat_progress_sink_without_session_can_emit_safe_observer(isolate_agent_runtime_root):
    log = EventLog()
    observed = []
    sink = ChatProgressSink(
        session_id="",
        persona_id="dev",
        event_log=log,
        on_trace=observed.append,
    )

    sink.emit(
        "run.tool.started",
        {
            "type": "run.tool.started",
            "tool_name": "terminal",
            "command_label": "echo ok",
            "unsafe": "SECRET",
        },
    )

    assert log.tail(5) == []
    assert observed == [
        {
            "type": "run.tool.started",
            "tool_name": "terminal",
            "command_label": "echo ok",
        }
    ]
