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


def test_safe_progress_payload_passes_the_dispatch_target_thread():
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "agent_chat_send",
            "status": "passed",
            "dispatch_target_session_id": "persona_chat_personainst_backend_dev_bbbbbbbbbbbb",
        },
    )

    assert payload["dispatch_target_session_id"] == (
        "persona_chat_personainst_backend_dev_bbbbbbbbbbbb"
    )


def test_safe_progress_payload_drops_a_malformed_dispatch_target_thread():
    # Re-asserted AT THE SINK, not only at the producer: this is the redaction
    # boundary, and a caller that hand-built the payload gets the same contract.
    # Truncating would hand the console a WRONG session to open, so an over-long
    # or prose value is dropped whole.
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "agent_chat_send",
            "dispatch_target_session_id": "the dev chat, probably",
        },
    )
    assert "dispatch_target_session_id" not in payload

    over_long = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "agent_chat_send",
            "dispatch_target_session_id": "s" * 241,
        },
    )
    assert "dispatch_target_session_id" not in over_long


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


def test_safe_progress_payload_passes_the_dispatch_reply_and_its_author():
    # The allowlist is the whole gate: a key missing from _SAFE_PROGRESS_KEYS
    # vanishes SILENTLY at this boundary, which is exactly the bug class that
    # keeps a producer-side field from ever reaching the operator.
    payload = _safe_progress_payload(
        "run.tool.finished",
        {
            "type": "run.tool.finished",
            "tool_name": "agent_chat_send",
            "status": "passed",
            "dispatch_reply": (
                "On it — patch landed.\n"
                "api_key=SECRET-VALUE must be dropped\n"
                "Suite is green."
            ),
            "dispatch_reply_from": "Neko Mission Lead",
        },
    )

    # Block grade: the secret LINE is dropped, the newline structure survives.
    assert payload["dispatch_reply"] == "On it — patch landed.\nSuite is green."
    assert "SECRET-VALUE" not in payload["dispatch_reply"]
    assert payload["dispatch_reply_from"] == "Neko Mission Lead"


def test_safe_progress_payload_bounds_dispatch_reply_at_1500():
    payload = _safe_progress_payload(
        "run.tool.finished",
        {"type": "run.tool.finished", "tool_name": "agent_chat_send", "dispatch_reply": "y" * 4000},
    )

    assert len(payload["dispatch_reply"]) == 1500
    assert payload["dispatch_reply"].endswith("…")


def test_append_bounded_event_sheds_tool_result_before_dispatch_reply(isolate_agent_runtime_root):
    # On a finished relay the co-resident heavy field is tool_result — the raw
    # JSON that CONTAINS the same reply. It sheds first, so the operator-facing
    # signal survives with the row.
    log = EventLog()
    payload = {
        "type": "run.tool.finished",
        "tool_name": "agent_chat_send",
        "status": "passed",
        "dispatch_target": "neko",
        "dispatch_reply": "On it — patch landed.",
        "dispatch_reply_from": "Neko Mission Lead",
        "tool_result": "r" * 5000,
    }
    _append_bounded_event(
        log,
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id="task_shed_before_reply",
            run_id="run_a",
            persona_id="neko",
            payload=payload,
        ),
    )

    rows = log.for_task("task_shed_before_reply")
    assert len(rows) == 1
    assert "tool_result" not in rows[0].payload
    assert rows[0].payload["tool_result_truncated"] is True
    assert rows[0].payload["dispatch_reply"] == "On it — patch landed."
    assert rows[0].payload["dispatch_reply_from"] == "Neko Mission Lead"
    assert "dispatch_reply_truncated" not in rows[0].payload


def test_append_bounded_event_sheds_dispatch_reply_when_the_rest_is_not_enough(
    isolate_agent_runtime_root,
):
    log = EventLog()
    payload = {
        "type": "run.tool.finished",
        "tool_name": "agent_chat_send",
        "status": "passed",
        "dispatch_target": "neko",
        "dispatch_reply": "y" * 4200,
        "tool_result": "r" * 4200,
        "output": "x" * 4200,
        "tool_input": "i" * 4200,
    }
    _append_bounded_event(
        log,
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id="task_shed_reply",
            run_id="run_a",
            persona_id="neko",
            payload=payload,
        ),
    )

    rows = log.for_task("task_shed_reply")
    assert len(rows) == 1
    # A truthful marker instead of a vanished event; the tool row survives.
    assert "dispatch_reply" not in rows[0].payload
    assert rows[0].payload["dispatch_reply_truncated"] is True
    assert rows[0].payload["tool_result_truncated"] is True
    assert rows[0].payload["dispatch_target"] == "neko"


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


# --------------------------------------------------------------------------- #
# The phase-timing marker bypass                                               #
# --------------------------------------------------------------------------- #
# The mission-chat turn record's `request_assembled` mark is taken by the
# handler from a payload the CONVERSATION LOOP emits — the loop sits below the
# harness and cannot hold the turn's TurnPhaseMarks itself. On that lane the
# loop's status callback lands here, and a timing marker names an INSTANT: it
# carries no tool, no command, no summary of work, so `_chat_progress_has_signal`
# judged it Trace-lane noise and dropped it. Live proof (2026-08-23): every
# mission-chat turn record written to that date lacks `request_assembled` while
# the marker fired on every turn, so the whole `run_conversation` prologue was
# billed to the provider. These rows pin the bypass AND its boundaries.
def _marker_payload(**extra):
    from hermes_constants import CONVERSATION_REQUEST_ASSEMBLED_STEP

    return {
        "type": "run.progress",
        "phase": "timing",
        "step": CONVERSATION_REQUEST_ASSEMBLED_STEP,
        "status": "reached",
        "summary": "Provider request assembled; dispatching.",
        **extra,
    }


def test_a_phase_timing_marker_reaches_the_trace_observer(isolate_agent_runtime_root):
    """The defect, as a row: the marker must survive the signal filter."""

    from hermes_constants import CONVERSATION_REQUEST_ASSEMBLED_STEP

    observed: list[dict] = []
    sink = ChatProgressSink(
        session_id="chat_1", persona_id="dev", event_log=EventLog(), on_trace=observed.append
    )

    sink.emit("run.progress", _marker_payload())

    assert observed == [
        {
            "type": "run.progress",
            "phase": "timing",
            "step": CONVERSATION_REQUEST_ASSEMBLED_STEP,
            "status": "reached",
        }
    ], "the phase-timing marker must reach the handler that converts it to a mark"


def test_the_marker_adds_no_row_to_the_trace_lane(isolate_agent_runtime_root):
    """An instrument, not an event.

    The rule at `_CHAT_PROGRESS_SIGNAL_KEYS` — the operator's Trace lane carries
    real work, never bare progress rows — stays exactly true: the bypass must
    not buy the mark at the price of a phantom row on every turn.
    """

    log = EventLog()
    latched: list[dict] = []
    sink = ChatProgressSink(
        session_id="chat_1",
        persona_id="dev",
        event_log=log,
        before_first_trace=latched.append,
        on_trace=lambda payload: None,
    )

    sink.emit("run.progress", _marker_payload())

    assert log.for_session("chat_1") == []
    assert latched == [], "the marker is not the turn's first tool work"
    assert sink._did_emit_first_trace is False


def test_the_marker_forward_carries_no_free_text(isolate_agent_runtime_root):
    """The bypass skips `_safe_progress_payload`, so it carries nothing to scrub.

    Only a `step` matched against the closed set of marker steps and a bare
    `status` token are forwarded. A producer that hangs prose — or a secret — on
    the marker cannot push it through this seam.
    """

    observed: list[dict] = []
    sink = ChatProgressSink(
        session_id="chat_1", persona_id="dev", event_log=EventLog(), on_trace=observed.append
    )

    sink.emit(
        "run.progress",
        _marker_payload(
            status="reached because api_key=SECRET_TOKEN_VALUE",
            summary="SECRET prose C:/Users/beast/private_token.txt",
            detail="SECRET detail",
            command_full="curl -H 'authorization: Bearer SECRET'",
        ),
    )

    assert set(observed[0]) == {"type", "phase", "step"}, (
        f"only the closed marker shape may be forwarded, got {observed[0]!r}"
    )
    assert "SECRET" not in repr(observed)


def test_an_unrecognized_timing_step_is_still_dropped_as_noise(isolate_agent_runtime_root):
    """The bypass is keyed on the CLOSED set of marker steps, not on `timing`.

    `profile_runner`/`conversation_loop` emit many `phase: timing` spans that
    nothing converts into a phase mark. Forwarding those would put a row per
    span on the trace lane for no reader.
    """

    log = EventLog()
    observed: list[dict] = []
    sink = ChatProgressSink(
        session_id="chat_1", persona_id="dev", event_log=log, on_trace=observed.append
    )

    sink.emit(
        "run.progress",
        {
            "type": "run.progress",
            "phase": "timing",
            "step": "profile_agent_construct",
            "status": "completed",
            "duration_ms": 1200,
            "timing_key": "profile_agent_construct_ms",
        },
    )

    assert observed == []
    assert log.for_session("chat_1") == []


def test_ordinary_progress_still_obeys_the_signal_filter(isolate_agent_runtime_root):
    """The bypass must not become a hole in the noise rule it stands beside."""

    log = EventLog()
    observed: list[dict] = []
    sink = ChatProgressSink(
        session_id="chat_1", persona_id="dev", event_log=log, on_trace=observed.append
    )

    sink.emit("run.progress", {"type": "run.progress", "summary": "Run progress update"})

    assert observed == []
    assert log.for_session("chat_1") == []


def test_the_WHOLE_live_chain_carries_the_marker_from_the_loop_to_the_mark(
    isolate_agent_runtime_root,
):
    """End to end across four modules — the seam every existing row skipped.

    `tests/hermes_cli/test_mission_chat_turn_phases.py` proved the mark/emit
    ENDS agreed, by handing the payload straight to the handler's callback. The
    live lane runs
    ``conversation_loop._emit_request_assembled_marker → agent.status_callback →
    profile_runner._profile_status_callback → persona_runtime._chat_trace_callback
    (a real ChatProgressSink) → on_trace → mark_from_trace_payload``,
    and the sink in the middle silently ate the payload for weeks. This row
    walks the real chain with only the transport faked, so the middle can never
    go quiet again.
    """

    from types import SimpleNamespace

    from agent.conversation_loop import _emit_request_assembled_marker
    from agent_runtime.mission_chat_phases import TurnPhaseMarks, mark_from_trace_payload
    from agent_runtime.persona_runtime import _chat_trace_callback
    from agent_runtime.profile_runner import AgentRunRequest, _profile_status_callback

    marks = TurnPhaseMarks()
    progress_callback = _chat_trace_callback(
        session_id="chat_1",
        persona=SimpleNamespace(id="dev"),
        turn_id="turn_1",
        on_trace=lambda payload: mark_from_trace_payload(marks, payload),
    )
    request = AgentRunRequest(
        profile=None,
        provider="openai-codex",
        model="gpt-5.6-luna",
        progress_callback=progress_callback,
    )
    agent = SimpleNamespace(status_callback=_profile_status_callback(request, {}))

    _emit_request_assembled_marker(agent, api_call_count=1)

    assert "request_assembled" in marks.snapshot(), (
        "the loop's dispatch marker did not survive the progress chain; the "
        "turn record loses the hermes-assembly / provider-TTFB split"
    )
