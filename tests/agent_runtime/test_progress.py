from agent_runtime.events import EventLog
from agent_runtime.models import Event
from agent_runtime.progress import ChatProgressSink, _append_bounded_event, _safe_progress_payload
from hermes_time import now


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


def test_chat_progress_sink_stamps_turn_id_on_events_and_retry(isolate_agent_runtime_root):
    log = EventLog()
    sink = ChatProgressSink(
        session_id="chat_1",
        persona_id="neko_supervisor",
        event_log=log,
        turn_id="agent-chat-send-1",
    )

    sink.emit("run.tool.started", {"type": "run.tool.started", "tool_name": "terminal"})
    sink.emit("run.tool.finished", {"type": "run.tool.finished", "tool_name": "terminal", "status": "passed"})

    rows = log.for_session("chat_1")
    assert [event.turn_id for event in rows] == ["agent-chat-send-1", "agent-chat-send-1"]

    _append_bounded_event(
        log,
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id=None,
            run_id=None,
            persona_id="neko_supervisor",
            session_id="chat_1",
            turn_id="agent-chat-send-1",
            payload={
                "type": "run.tool.finished",
                "tool_name": "terminal",
                "status": "passed",
                "output": "x" * 5000,
            },
        ),
    )
    retried = log.for_session("chat_1")[-1]
    assert retried.turn_id == "agent-chat-send-1"
    assert retried.payload["output_truncated"] is True


def test_chat_progress_sink_drops_noise_and_secrets(isolate_agent_runtime_root):
    log = EventLog()
    sink = ChatProgressSink(session_id="chat_1", persona_id="dev", event_log=log)

    # Generic progress with no tool/command/reasoning signal is dropped.
    sink.emit("run.progress", {"type": "run.progress", "summary": "Run progress update"})
    # A non-trace event type is ignored entirely.
    sink.emit("persona_instance.created", {"type": "persona_instance.created"})
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


def test_safe_progress_payload_passes_operator_detail_lane():
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "terminal",
            "status": "passed",
            "summary": "Finished tool terminal: passed",
            "command_full": "flutter test test/features/library/petdex_menu_test.dart",
            "target_label": "PetdexTile in lib/features/library",
            "output": "00:05 +12: All tests passed!\napi_key=SECRET-VALUE\nlast line",
            "changed_paths": ["lib/features/library/petdex_menu.dart", "test/a_test.dart"],
            "skill_name": "harness-dev-delivery",
        },
    )

    # Operator lane keeps real paths in commands/targets/changed paths.
    assert payload["command_full"] == "flutter test test/features/library/petdex_menu_test.dart"
    assert payload["target_label"] == "PetdexTile in lib/features/library"
    assert payload["changed_paths"] == [
        "lib/features/library/petdex_menu.dart",
        "test/a_test.dart",
    ]
    assert payload["skill_name"] == "harness-dev-delivery"
    # Output keeps line structure; the secret LINE is redacted, not the field.
    assert "All tests passed!" in payload["output"]
    assert "last line" in payload["output"]
    assert "SECRET-VALUE" not in payload["output"]
    assert "[redacted line" in payload["output"]


def test_safe_progress_payload_bounds_operator_output_tail():
    long_output = "\n".join(f"line {index}" for index in range(400))
    payload = _safe_progress_payload(
        "run.tool.finished",
        {"type": "run.tool.finished", "tool_name": "terminal", "output": long_output},
    )

    assert len(payload["output"]) <= 1300
    assert payload["output"].startswith("…(earlier output truncated)…")
    assert "line 399" in payload["output"]


def test_safe_progress_payload_passes_dispatch_fields():
    payload = _safe_progress_payload(
        "run.tool.started",
        {
            "type": "run.tool.started",
            "tool_name": "agent_chat_send",
            "status": "started",
            "target_label": "→ backend_dev: run a bounded backend health check",
            "dispatch_target": "backend_dev",
            "dispatch_order": (
                "Run a bounded backend health check.\n"
                "api_key=SECRET-VALUE must be dropped\n"
                "Report the one-line result."
            ),
        },
    )

    assert payload["dispatch_target"] == "backend_dev"
    # The full order keeps its newline structure; the secret LINE is dropped
    # (not redacted-in-place), the surrounding lines survive.
    assert payload["dispatch_order"] == (
        "Run a bounded backend health check.\nReport the one-line result."
    )
    assert "SECRET-VALUE" not in payload["dispatch_order"]
    # The prose target_label is untouched by the new handlers.
    assert payload["target_label"] == "→ backend_dev: run a bounded backend health check"


def test_safe_progress_payload_bounds_dispatch_order_at_1500():
    payload = _safe_progress_payload(
        "run.tool.started",
        {"type": "run.tool.started", "tool_name": "agent_chat_send", "dispatch_order": "y" * 4000},
    )

    assert len(payload["dispatch_order"]) == 1500
    assert payload["dispatch_order"].endswith("…")


def test_append_bounded_event_sheds_output_before_dispatch_order(isolate_agent_runtime_root):
    log = EventLog()
    payload = {
        "type": "run.tool.started",
        "tool_name": "agent_chat_send",
        "status": "started",
        "dispatch_target": "backend_dev",
        "dispatch_order": "run a bounded backend health check",
        "output": "x" * 5000,
    }
    _append_bounded_event(
        log,
        Event(
            ts=now(),
            type="run.tool.started",
            task_id="task_shed_output",
            run_id="run_a",
            persona_id="neko",
            payload=payload,
        ),
    )

    rows = log.for_task("task_shed_output")
    assert len(rows) == 1
    # Output shed first; the dispatch target + order survive untouched.
    assert "output" not in rows[0].payload
    assert rows[0].payload["output_truncated"] is True
    assert rows[0].payload["dispatch_target"] == "backend_dev"
    assert rows[0].payload["dispatch_order"] == "run a bounded backend health check"
    assert "dispatch_order_truncated" not in rows[0].payload


def test_append_bounded_event_sheds_dispatch_order_when_output_not_enough(isolate_agent_runtime_root):
    log = EventLog()
    payload = {
        "type": "run.tool.started",
        "tool_name": "agent_chat_send",
        "status": "started",
        "dispatch_target": "backend_dev",
        "dispatch_order": "y" * 4200,
        "output": "x" * 4200,
    }
    _append_bounded_event(
        log,
        Event(
            ts=now(),
            type="run.tool.started",
            task_id="task_shed_both",
            run_id="run_a",
            persona_id="neko",
            payload=payload,
        ),
    )

    rows = log.for_task("task_shed_both")
    assert len(rows) == 1
    # Both variable-size fields shed; the tool row (target/status) still survives.
    assert "output" not in rows[0].payload
    assert "dispatch_order" not in rows[0].payload
    assert rows[0].payload["output_truncated"] is True
    assert rows[0].payload["dispatch_order_truncated"] is True
    assert rows[0].payload["dispatch_target"] == "backend_dev"


def test_safe_progress_payload_carries_tool_io_blocks():
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "agent_chat_open",
            "tool_input": 'persona_id: "dev"\ninstance_id: "abc"',
            "tool_result": 'ok: true\nthreads: [{"id": "t1"}]',
        },
    )

    # Line structure survives (the console dropdown renders one key per line).
    assert payload["tool_input"] == 'persona_id: "dev"\ninstance_id: "abc"'
    assert payload["tool_result"] == 'ok: true\nthreads: [{"id": "t1"}]'


def test_safe_progress_payload_redacts_secret_tool_io_lines_and_bounds():
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "web_fetch",
            "tool_input": 'url: "https://x"\nauthorization: "Bearer abc"',
            "tool_result": "z" * 4000,
        },
    )

    lines = payload["tool_input"].split("\n")
    assert lines[0] == 'url: "https://x"'
    assert lines[1] == "[redacted line — contained a secret]"
    # Head-bounded with an explicit truncation marker (1700 + marker line).
    assert payload["tool_result"].startswith("z" * 100)
    assert payload["tool_result"].endswith("…(rest truncated)…")
    assert len(payload["tool_result"]) <= 1700 + len("\n…(rest truncated)…")


def test_append_bounded_event_sheds_tool_result_before_output(isolate_agent_runtime_root):
    log = EventLog()
    payload = {
        "type": "run.tool.finished",
        "tool_name": "agent_chat_threads",
        "status": "passed",
        "tool_input": "limit: 5",
        "tool_result": "r" * 5000,
    }
    _append_bounded_event(
        log,
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id="task_shed_tool_result",
            run_id="run_a",
            persona_id="neko",
            payload=payload,
        ),
    )

    rows = log.for_task("task_shed_tool_result")
    assert len(rows) == 1
    # tool_result shed first; the smaller tool_input survives with the row.
    assert "tool_result" not in rows[0].payload
    assert rows[0].payload["tool_result_truncated"] is True
    assert rows[0].payload["tool_input"] == "limit: 5"
    assert rows[0].payload["tool_name"] == "agent_chat_threads"


def test_append_bounded_event_degrades_oversized_output(isolate_agent_runtime_root):
    from hermes_time import now

    from agent_runtime.models import Event
    from agent_runtime.progress import _append_bounded_event

    log = EventLog()
    oversized = {
        "type": "run.tool.finished",
        "tool_name": "terminal",
        "status": "passed",
        "output": "x" * 5000,
    }
    _append_bounded_event(
        log,
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id="task_big",
            run_id="run_big",
            persona_id="dev",
            payload=oversized,
        ),
    )

    rows = log.for_task("task_big")
    assert len(rows) == 1
    # The event survived; only the oversized output was shed.
    assert "output" not in rows[0].payload
    assert rows[0].payload["output_truncated"] is True
    assert rows[0].payload["tool_name"] == "terminal"


# ── live chat-log mirror ───────────────────────────────────────────────────
#
# The sink is the ONE seam that knows a chat turn is running a tool right now.
# Mirroring compact tool lines into the session's live log is what makes "what
# is this teammate doing?" answerable by grep/tail while the turn is still in
# flight — the EventLog rows stay the authority.


def test_chat_progress_sink_mirrors_tool_lines_into_the_live_chat_log(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    import json

    from agent_runtime.chat_live_log import chat_live_log_path, reset_chat_live_log_state

    monkeypatch.setenv("HERMES_HEAD_HOME", str(tmp_path))
    reset_chat_live_log_state()
    try:
        sink = ChatProgressSink(
            session_id="chat_1", persona_id="dev", event_log=EventLog(), turn_id="turn-1"
        )
        sink.emit("run.tool.started", {"type": "run.tool.started", "tool_name": "terminal"})
        sink.emit(
            "run.tool.finished",
            {"type": "run.tool.finished", "tool_name": "terminal", "status": "passed"},
        )
        # run.progress carries signal but is NOT a tool transition — it stays off
        # the mirror so the file reads as a transcript, not a second event log.
        sink.emit(
            "run.progress",
            {"type": "run.progress", "reasoning_summary": "thinking about it"},
        )

        path = chat_live_log_path("chat_1")
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        tools = [row for row in rows if row.get("kind") == "tool"]
        assert [(row["tool"], row["status"], row["turn_id"]) for row in tools] == [
            ("terminal", "started", "turn-1"),
            ("terminal", "passed", "turn-1"),
        ]
    finally:
        reset_chat_live_log_state()


def test_chat_progress_sink_survives_a_broken_mirror(isolate_agent_runtime_root, monkeypatch):
    # A mirror failure must never cost the turn its canonical EventLog row.
    from agent_runtime import chat_live_log

    def _boom(**kwargs):
        raise OSError("mirror gone")

    monkeypatch.setattr(chat_live_log, "record_chat_tool", _boom)
    log = EventLog()
    sink = ChatProgressSink(session_id="chat_1", persona_id="dev", event_log=log)
    sink.emit("run.tool.started", {"type": "run.tool.started", "tool_name": "terminal"})
    assert [event.type for event in log.for_session("chat_1")] == ["run.tool.started"]
