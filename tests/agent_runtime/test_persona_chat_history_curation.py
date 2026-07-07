"""Operator-facing curation of the agent's raw working session (audit Stage 2C / S3).

The persona instance binds the agent's *internal* session, whose raw rows are
verbose tick-context prompts and serialized decision dicts. The operator must
see a clean transcript: decision summaries, no internal scaffolding/tool/system
noise. Regression guard for the live-smoke breakage (raw JSON + tick context
leaking into the Agent Console).
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event, PersonaAssignment, PersonaInstance
from agent_runtime.mission_chat_turns import (
    mission_chat_turn_elements,
    persist_mission_chat_turn,
)
from agent_runtime.persona_chat_history import (
    _iso_timestamp,
    _safe_recent_messages,
    persona_chat_history_summary,
    persona_chat_trace_summary,
)
from agent_runtime.states import WorkerSessionState


class FakeSessionDB:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self, session_id, include_inactive=False):
        return list(self._messages)


class FakeHistorySessionDB(FakeSessionDB):
    def __init__(self, sessions, messages=None):
        super().__init__(messages or [])
        self._sessions = list(sessions)

    def list_sessions_rich(self, **kwargs):
        source = kwargs.get("source")
        exclude_sources = set(kwargs.get("exclude_sources") or [])
        rows = list(self._sessions)
        if source is not None:
            rows = [row for row in rows if row.get("source") == source]
        if exclude_sources:
            rows = [row for row in rows if row.get("source") not in exclude_sources]
        return rows[: kwargs.get("limit", len(rows))]

    def get_session(self, session_id):
        for row in self._sessions:
            if row.get("id") == session_id:
                return dict(row)
        return None


_DECISION = json.dumps(
    {
        "type": "propose_acceptance",
        "summary": "Route the greeting as a scope clarification.",
        "rationale": "The mission description is only 'hi'.",
        "payload": {"objective": "Clarify", "risk_flags": ["scope_missing"]},
        "handoff_packet": {"packet_kind": "fresh_scope"},
    }
)


def test_decision_dict_collapses_to_summary_and_rationale():
    db = FakeSessionDB([{"role": "assistant", "content": _DECISION}])
    rows, status = _safe_recent_messages(db, session_id="s1")
    assert status == "safe"
    assert len(rows) == 1
    assert rows[0]["role"] == "agent"
    assert "Route the greeting as a scope clarification." in rows[0]["text"]
    assert "The mission description is only 'hi'." in rows[0]["text"]
    # Internal structure must not leak.
    assert "risk_flags" not in rows[0]["text"]
    assert "handoff_packet" not in rows[0]["text"]
    assert "payload" not in rows[0]["text"]


def test_internal_scaffolding_operator_rows_are_dropped():
    db = FakeSessionDB(
        [
            {"role": "user", "content": "# Agent Runtime Tick Context\n## Task\n- id: task_1\n..."},
            {"role": "assistant", "content": _DECISION},
        ]
    )
    rows, _ = _safe_recent_messages(db, session_id="s1")
    # Only the curated agent reply survives; the tick-context prompt is dropped.
    assert [r["role"] for r in rows] == ["agent"]


def test_clean_operator_message_is_kept():
    db = FakeSessionDB([{"role": "user", "content": "hi neko"}])
    rows, _ = _safe_recent_messages(db, session_id="s1")
    assert len(rows) == 1
    assert rows[0]["role"] == "operator"
    assert rows[0]["text"] == "hi neko"


def test_long_markdown_agent_message_is_not_preview_truncated():
    body = "\n\n".join(
        [
            "## Gap 1 — Chat-to-goal action execution is not proven",
            "**Severity: High**",
            "The current Mission Control chat path can send a message.",
            "## Gap 2 — Runtime graph and mission blueprint need a join contract",
            "1. Mission blueprint defines stages, owners, repos, proof expectations.",
            "## Gap 3 — Raw terminal should not become the product layer",
            "Use typed Harness actions instead of arbitrary shell visibility.",
            "## Gap 4 — Child-agent spawning needs explicit permission checks",
            "Bottom line: build the typed Neko supervisor action bridge.",
        ]
    )
    db = FakeSessionDB([{"role": "assistant", "content": body}])

    rows, status = _safe_recent_messages(db, session_id="s1")

    assert status == "safe"
    assert rows[0]["text"] == body
    assert "## Gap 4" in rows[0]["text"]
    assert rows[0]["text"].count("\n\n") >= 8


def test_tool_system_and_empty_rows_are_dropped():
    db = FakeSessionDB(
        [
            {"role": "system", "content": "you are an agent"},
            {"role": "tool", "content": '{"success": true}'},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": _DECISION},
        ]
    )
    rows, _ = _safe_recent_messages(db, session_id="s1")
    assert [r["role"] for r in rows] == ["agent"]


def test_unparseable_raw_dict_is_not_shown():
    # A serialized dict we can't parse must not dump as raw JSON to the operator.
    db = FakeSessionDB([{"role": "assistant", "content": '{"type": broken json'}])
    rows, _ = _safe_recent_messages(db, session_id="s1")
    assert rows == []


def test_message_timestamps_are_iso_so_they_merge_with_trace_by_ts():
    # SessionDB stores epoch-seconds floats; trace rows are ISO. The Launcher
    # merges both channels by parsing ts, so an unparseable epoch float would push
    # the whole trace block ahead of curated history. Project history as ISO too.
    db = FakeSessionDB(
        [{"id": "m1", "role": "assistant", "content": "Agent update", "timestamp": 1782162002.5}]
    )

    rows, _ = _safe_recent_messages(db, session_id="s1")

    assert rows[0]["timestamp"] == "2026-06-22T21:00:02.500000Z"
    # Same shape the trace channel emits, so DateTime.tryParse orders them together.
    assert _iso_timestamp(1782162002.5) == rows[0]["timestamp"]
    assert _iso_timestamp("2026-06-22T21:00:02.500000Z") == "2026-06-22T21:00:02.500000Z"
    assert _iso_timestamp(None) is None


def test_history_row_timestamps_are_iso_utc_at_projection_boundary():
    db = FakeHistorySessionDB(
        [
            {
                "id": "chat_neko",
                "title": "Neko briefing",
                "message_count": 2,
                "started_at": 1782162000.25,
                "last_active": "1782162002.5",
            }
        ]
    )

    rows = persona_chat_history_summary(
        persona_instances=[_chat_persona_instance("personainst_neko", "neko_supervisor", "chat_neko")],
        session_db=db,
    )

    assert rows[0]["created_at"] == "2026-06-22T21:00:00.250000Z"
    assert rows[0]["updated_at"] == "2026-06-22T21:00:02.500000Z"


def test_history_row_normalizes_iso_and_drops_garbage_timestamps():
    db = FakeHistorySessionDB(
        [
            {
                "id": "chat_neko",
                "title": "Neko briefing",
                "started_at": "2026-07-07T09:00:40",
                "last_active": "not a timestamp",
            }
        ]
    )

    rows = persona_chat_history_summary(
        persona_instances=[_chat_persona_instance("personainst_neko", "neko_supervisor", "chat_neko")],
        session_db=db,
    )

    assert rows[0]["created_at"] == "2026-07-07T09:00:40.000000Z"
    assert rows[0]["updated_at"] is None


def test_persona_chat_history_rows_always_emit_kind():
    db = FakeHistorySessionDB(
        [
            {
                "id": "chat_neko",
                "title": "Neko briefing",
                "message_count": 1,
                "started_at": 1782162000.25,
                "last_active": 1782162002.5,
            }
        ]
    )
    mission_instance = PersonaInstance(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="Dev",
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        session_id="mission_dev",
        current_task_id="task_1",
    )

    rows = persona_chat_history_summary(
        persona_instances=[
            _chat_persona_instance("personainst_neko", "neko_supervisor", "chat_neko"),
            mission_instance,
        ],
        session_db=db,
    )

    assert {row["kind"] for row in rows} == {"chat", "mission"}
    assert all(row["kind"] in {"chat", "mission"} for row in rows)
    assert next(row for row in rows if row["kind"] == "chat")["live_mission"] is False
    assert next(row for row in rows if row["kind"] == "mission")["live_mission"] is True


def test_synthetic_mission_row_anchors_to_assignment_created_at():
    # R3: PersonaInstance persists no assigned_at, so the synthetic row anchors
    # to the bound PersonaAssignment's created_at via current_assignment_id.
    db = FakeHistorySessionDB([])
    assignment = _persona_assignment(
        "assign_1",
        instance_id="personainst_dev",
        task_id="task_1",
        created_at=datetime(2026, 6, 22, 21, 0, 2, 500000, tzinfo=timezone.utc),
        updated_at=datetime(2026, 6, 22, 21, 30, 0, tzinfo=timezone.utc),
    )
    instance = _mission_persona_instance("personainst_dev", "dev", "task_1", assignment_id="assign_1")

    first = persona_chat_history_summary(
        persona_instances=[instance], session_db=db, persona_assignments=[assignment]
    )
    second = persona_chat_history_summary(
        persona_instances=[instance], session_db=db, persona_assignments=[assignment]
    )

    assert first[0]["kind"] == "mission"
    assert first[0]["created_at"] == "2026-06-22T21:00:02.500000Z"
    # updated_at anchors to the SAME assignment moment (spec Slice 2 baseline:
    # last_active = assigned) so consecutive builds stay byte-identical even
    # while assignment state transitions bump the assignment's own updated_at.
    assert first[0]["updated_at"] == "2026-06-22T21:00:02.500000Z"
    assert first[0]["created_at"] == second[0]["created_at"]
    assert first[0]["updated_at"] == second[0]["updated_at"]


def test_synthetic_mission_row_matches_assignment_by_instance_and_task():
    # When current_assignment_id is unset, the newest assignment for the same
    # (persona_instance_id, task_id) vouches — still no store scan.
    db = FakeHistorySessionDB([])
    older = _persona_assignment(
        "assign_old",
        instance_id="personainst_dev",
        task_id="task_1",
        created_at=datetime(2026, 6, 22, 20, 0, 0, tzinfo=timezone.utc),
    )
    newer = _persona_assignment(
        "assign_new",
        instance_id="personainst_dev",
        task_id="task_1",
        created_at=datetime(2026, 6, 22, 21, 0, 2, 500000, tzinfo=timezone.utc),
    )
    other_task = _persona_assignment(
        "assign_other",
        instance_id="personainst_dev",
        task_id="task_2",
        created_at=datetime(2026, 6, 23, 9, 0, 0, tzinfo=timezone.utc),
    )
    instance = _mission_persona_instance("personainst_dev", "dev", "task_1", assignment_id=None)

    rows = persona_chat_history_summary(
        persona_instances=[instance],
        session_db=db,
        persona_assignments=[older, newer, other_task],
    )

    assert rows[0]["kind"] == "mission"
    assert rows[0]["created_at"] == "2026-06-22T21:00:02.500000Z"
    assert rows[0]["updated_at"] == "2026-06-22T21:00:02.500000Z"


def test_synthetic_mission_row_falls_back_to_instance_assigned_at():
    # Forward-compat duck path: if an instance ever grows assigned_at, it backs
    # the anchor when no assignment record is resolvable.
    db = FakeHistorySessionDB([])
    instance = SimpleNamespace(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="Dev",
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        session_id="mission_dev",
        current_task_id="task_1",
        current_chat_goal="Implement the fix",
        current_assignment_id=None,
        assigned_at=1782162002.5,
    )

    first = persona_chat_history_summary(persona_instances=[instance], session_db=db)
    second = persona_chat_history_summary(persona_instances=[instance], session_db=db)

    assert first[0]["kind"] == "mission"
    assert first[0]["created_at"] == "2026-06-22T21:00:02.500000Z"
    assert first[0]["updated_at"] == "2026-06-22T21:00:02.500000Z"
    assert first[0]["updated_at"] == second[0]["updated_at"]


def test_iso_timestamp_normalizes_datetime_values():
    aware = datetime(2026, 6, 22, 21, 0, 2, 500000, tzinfo=timezone.utc)
    naive = datetime(2026, 6, 22, 21, 0, 2, 500000)

    assert _iso_timestamp(aware) == "2026-06-22T21:00:02.500000Z"
    assert _iso_timestamp(naive) == "2026-06-22T21:00:02.500000Z"


def test_synthetic_mission_row_keeps_unknown_timestamp_null():
    db = FakeHistorySessionDB([])
    instance = PersonaInstance(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="Dev",
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        session_id="mission_dev",
        current_task_id="task_1",
    )

    rows = persona_chat_history_summary(persona_instances=[instance], session_db=db)

    assert rows[0]["kind"] == "mission"
    assert rows[0]["created_at"] is None
    assert rows[0]["updated_at"] is None


def test_safe_recent_messages_returns_deeper_bounded_tail():
    db = FakeSessionDB(
        [
            {"id": f"msg_{index}", "role": "assistant", "content": f"Agent update {index}"}
            for index in range(12)
        ]
    )

    rows, status = _safe_recent_messages(db, session_id="s1")

    assert status == "safe"
    assert len(rows) == 12
    assert rows[0]["text"] == "Agent update 0"
    assert rows[-1]["text"] == "Agent update 11"


def test_safe_recent_messages_projects_persisted_turn_elements(isolate_agent_runtime_root):
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="client_1",
        turn_id="turn_1",
        elements=[
            {
                "kind": "segment",
                "id": "seg_2",
                "turn_id": "turn_1",
                "seq": 3,
                "seg_type": "answer",
                "text": "Done.",
            },
            {
                "kind": "tool",
                "id": "tool_1",
                "turn_id": "turn_1",
                "seq": 2,
                "name": "shell_command",
                "args": "rg plan",
                "status": "ok",
                "duration_ms": 42,
                "files": ["safe.dart", "C:/Users/beast/private_token.txt"],
            },
            {
                "kind": "segment",
                "id": "seg_1",
                "turn_id": "turn_1",
                "seq": 1,
                "seg_type": "plan",
                "text": "I will inspect.",
            },
            {
                "kind": "segment",
                "id": "bad",
                "turn_id": "turn_1",
                "seq": "not-an-int",
                "text": "drop me",
            },
        ],
    )
    db = FakeSessionDB(
        [
            {
                "id": "agent_1",
                "role": "assistant",
                "content": "Done.",
                "platform_message_id": "client_1",
            }
        ]
    )

    rows, status = _safe_recent_messages(db, session_id="s1")

    assert status == "safe"
    assert rows[0]["client_message_id"] == "client_1"
    assert [item["id"] for item in rows[0]["turn_elements"]] == ["seg_1", "tool_1", "seg_2"]
    assert rows[0]["turn_elements"][1]["files"] == ["safe.dart"]
    assert mission_chat_turn_elements(session_id="s1", client_message_id="missing") == []


def test_mission_chat_turn_persists_durable_partial_prefix(isolate_agent_runtime_root):
    # Mid-turn (before turn.end) the emitter persists on every update, so a
    # dropped stream / reload still has the segments-so-far prefix. Here the
    # answer segment never arrived and seg_1 is still "streaming" — the partial
    # must read back intact and ordered (I-DURABLE).
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="client_1",
        turn_id="turn_1",
        elements=[
            {
                "kind": "segment",
                "id": "seg_1",
                "turn_id": "turn_1",
                "seq": 1,
                "seg_type": "plan",
                "text": "I'll inspect",
                "state": "streaming",
            },
            {
                "kind": "tool",
                "id": "tool_1",
                "turn_id": "turn_1",
                "seq": 2,
                "name": "read_file",
                "state": "started",
            },
        ],
    )

    partial = mission_chat_turn_elements(session_id="s1", client_message_id="client_1")

    assert [item["id"] for item in partial] == ["seg_1", "tool_1"]
    assert partial[0]["state"] == "streaming"
    assert partial[0]["text"] == "I'll inspect"
    assert partial[1]["state"] == "started"

    # A later update (the same client_message_id) supersedes the partial without
    # duplicating it — idempotent replay, no dup bubbles on re-attach.
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="client_1",
        turn_id="turn_1",
        elements=[
            {
                "kind": "segment",
                "id": "seg_1",
                "turn_id": "turn_1",
                "seq": 1,
                "seg_type": "plan",
                "text": "I'll inspect the doc",
                "state": "settled",
            },
            {
                "kind": "tool",
                "id": "tool_1",
                "turn_id": "turn_1",
                "seq": 2,
                "name": "read_file",
                "state": "finished",
                "status": "ok",
            },
            {
                "kind": "segment",
                "id": "seg_2",
                "turn_id": "turn_1",
                "seq": 3,
                "seg_type": "answer",
                "text": "Done.",
                "state": "settled",
            },
        ],
    )

    settled = mission_chat_turn_elements(session_id="s1", client_message_id="client_1")
    assert [item["id"] for item in settled] == ["seg_1", "tool_1", "seg_2"]
    assert settled[0]["state"] == "settled"
    assert settled[0]["text"] == "I'll inspect the doc"


def test_mission_chat_turn_persists_command_output_and_exit_code(isolate_agent_runtime_root):
    persist_mission_chat_turn(
        session_id="s1",
        client_message_id="client_1",
        turn_id="turn_1",
        elements=[
            {
                "kind": "tool",
                "id": "tool_1",
                "turn_id": "turn_1",
                "seq": 1,
                "name": "terminal",
                "state": "finished",
                "status": "failed",
                "command": "flutter test -j1",
                "exit_code": 1,
                "output": "00:01 +0 -1: boom\nException: failed",
                "detail": "see failing test",
                "duration_ms": 900,
            },
        ],
    )

    elements = mission_chat_turn_elements(session_id="s1", client_message_id="client_1")

    tool = elements[0]
    assert tool["command"] == "flutter test -j1"
    assert tool["exit_code"] == 1
    assert tool["detail"] == "see failing test"
    assert "Exception: failed" in tool["output"]


def test_persona_chat_trace_projects_tool_events_by_persona_without_raw_secrets():
    events = EventLog()
    ts = now()
    events.append(
        Event(
            ts=ts,
            type="run.tool.started",
            task_id="task_chat",
            run_id="run_dev",
            persona_id="dev",
            payload={
                "tool_name": "shell_command",
                "summary": "Running focused tests",
                "stage_id": "implementation",
                "changed_files": ["widget.dart", "C:/Users/beast/private_token.txt"],
                "status": "ok",
            },
        )
    )
    events.append(
        Event(
            ts=ts,
            type="run.tool.finished",
            task_id="task_chat",
            run_id="run_qa",
            persona_id="qa",
            payload={
                "tool_name": "shell_command",
                "summary": "QA proof should stay on the QA thread",
            },
        )
    )
    events.append(
        Event(
            ts=ts,
            type="run.progress",
            task_id="task_chat",
            run_id="run_dev",
            persona_id="dev",
            payload={
                "summary": "api_key=super-secret-value",
                "command_label": "flutter analyze lib/features/mission_control",
            },
        )
    )

    rows = persona_chat_trace_summary(
        persona_instances=[
            _persona_instance("personainst_dev", "dev", "task_chat"),
            _persona_instance("personainst_qa", "qa", "task_chat"),
        ],
        event_log=events,
    )

    by_persona = {row["persona_id"]: row for row in rows}
    assert [entry["event"] for entry in by_persona["dev"]["entries"]] == [
        "tool_started",
        "progress",
    ]
    assert by_persona["dev"]["entries"][0]["files"] == ["widget.dart"]
    assert by_persona["dev"]["entries"][1]["summary"] is None
    assert "super-secret-value" not in repr(rows)
    assert [entry["run_id"] for entry in by_persona["qa"]["entries"]] == ["run_qa"]


def test_busy_task_does_not_starve_a_quiet_personas_trace():
    events = EventLog()
    ts = now()
    # The quiet persona's only trace event is the OLDEST row in a shared task log.
    events.append(
        Event(
            ts=ts,
            type="run.tool.finished",
            task_id="busy",
            run_id="run_qa",
            persona_id="qa",
            payload={"tool_name": "pytest", "summary": "qa ran the suite"},
        )
    )
    # A burst from another agent that would push qa past a flat tail*4 window.
    for index in range(20):
        events.append(
            Event(
                ts=ts,
                type="run.progress",
                task_id="busy",
                run_id="run_dev",
                persona_id="dev",
                payload={"summary": f"dev step {index}"},
            )
        )

    rows = persona_chat_trace_summary(
        persona_instances=[
            _persona_instance("personainst_dev", "dev", "busy"),
            _persona_instance("personainst_qa", "qa", "busy"),
        ],
        event_log=events,
        message_tail=2,
    )

    by_persona = {row["persona_id"]: row for row in rows}
    # The quiet persona stays represented despite the dev burst (window is sized
    # for all the task's agents, not a flat per-persona slice).
    assert [entry["run_id"] for entry in by_persona["qa"]["entries"]] == ["run_qa"]
    # The busy persona is still bounded to message_tail.
    assert len(by_persona["dev"]["entries"]) == 2


def test_trace_entry_carries_operator_detail_fields():
    events = EventLog()
    ts = now()
    events.append(
        Event(
            ts=ts,
            type="run.tool.finished",
            task_id="task_detail",
            run_id="run_dev",
            persona_id="dev",
            payload={
                "tool_name": "terminal",
                "status": "passed",
                "summary": "Finished tool terminal: passed",
                "command_full": "flutter test test/features/library/petdex_menu_test.dart",
                "output": "00:05 +12: All tests passed!",
                "exit_code": 0,
                "duration_ms": 5300,
            },
        )
    )
    events.append(
        Event(
            ts=ts,
            type="run.tool.finished",
            task_id="task_detail",
            run_id="run_dev",
            persona_id="dev",
            payload={
                "tool_name": "patch",
                "status": "passed",
                "summary": "Patched 2 files: a.dart, b.dart",
                "changed_files": ["a.dart", "b.dart"],
                "changed_paths": ["lib/features/library/a.dart", "lib/features/library/b.dart"],
            },
        )
    )
    events.append(
        Event(
            ts=ts,
            type="run.tool.started",
            task_id="task_detail",
            run_id="run_dev",
            persona_id="dev",
            payload={
                "tool_name": "read_file",
                "status": "started",
                "summary": "Started tool read_file: lib/main.dart",
                "target_label": "lib/main.dart",
            },
        )
    )

    rows = persona_chat_trace_summary(
        persona_instances=[_persona_instance("personainst_dev", "dev", "task_detail")],
        event_log=events,
    )

    entries = rows[0]["entries"]
    by_tool = {entry["tool_name"]: entry for entry in entries}
    terminal = by_tool["terminal"]
    assert terminal["command"] == "flutter test test/features/library/petdex_menu_test.dart"
    assert terminal["output"] == "00:05 +12: All tests passed!"
    assert terminal["exit_code"] == 0
    assert terminal["duration_ms"] == 5300
    patch = by_tool["patch"]
    assert patch["paths"] == [
        "lib/features/library/a.dart",
        "lib/features/library/b.dart",
    ]
    read = by_tool["read_file"]
    assert read["target"] == "lib/main.dart"


def test_trace_entry_carries_reasoning_summary_but_never_the_placeholder():
    events = EventLog()
    ts = now()
    events.append(
        Event(
            ts=ts,
            type="run.progress",
            task_id="task_detail",
            run_id="run_dev",
            persona_id="dev",
            payload={
                "step": "reasoning_summary",
                "status": "running",
                "summary": "Agent thinking process updated",
                "reasoning_summary": "Reading docs/scratch/goal_turn_probe.md before the echo proof.",
            },
        )
    )
    events.append(
        Event(
            ts=ts,
            type="run.progress",
            task_id="task_detail",
            run_id="run_dev",
            persona_id="dev",
            payload={
                # Legacy events recorded the callback placeholder as content.
                "step": "reasoning_summary",
                "status": "running",
                "summary": "Agent thinking process updated",
                "reasoning_summary": "_thinking",
            },
        )
    )

    rows = persona_chat_trace_summary(
        persona_instances=[_persona_instance("personainst_dev", "dev", "task_detail")],
        event_log=events,
    )

    reasoning = [entry.get("reasoning_summary") for entry in rows[0]["entries"]]
    assert "Reading docs/scratch/goal_turn_probe.md before the echo proof." in reasoning
    assert "_thinking" not in reasoning


def test_incident_flood_does_not_starve_trace_window():
    # Live failure shape (task_bd98d444, 2026-07-05): the task's newest ~640
    # events were incident/budget rows, so a fetch window that counts raw rows
    # found zero trace events → channel trace null → trace_empty warning.
    events = EventLog()
    ts = now()
    for index in range(8):
        events.append(
            Event(
                ts=ts,
                type="run.tool.finished",
                task_id="task_flooded",
                run_id=f"run_dev_{index}",
                persona_id="dev",
                payload={"tool_name": "read_file", "summary": f"dev tool {index}"},
            )
        )
    # Newest tail: a runaway incident loop far larger than the fetch window.
    for _ in range(600):
        events.append(Event(ts=ts, type="incident.opened", task_id="task_flooded", run_id=None, persona_id="dev"))

    rows = persona_chat_trace_summary(
        persona_instances=[_persona_instance("personainst_dev", "dev", "task_flooded")],
        event_log=events,
        message_tail=4,
    )

    assert len(rows) == 1
    entries = rows[0]["entries"]
    # The newest trace rows survive the flood, bounded to message_tail.
    assert len(entries) == 4
    assert [entry["run_id"] for entry in entries] == [f"run_dev_{index}" for index in range(4, 8)]


def test_persona_chat_trace_projects_session_keyed_chat_tool_calls():
    events = EventLog()
    ts = now()
    # A conversational (non-task) chat turn's tool calls, keyed on the session.
    events.append(
        Event(
            ts=ts,
            type="run.tool.started",
            task_id=None,
            run_id=None,
            persona_id="neko_supervisor",
            payload={"tool_name": "terminal", "command_label": "echo PARITY_OK_2026"},
            session_id="chat_neko",
        )
    )
    events.append(
        Event(
            ts=ts,
            type="run.tool.finished",
            task_id=None,
            run_id=None,
            persona_id="neko_supervisor",
            payload={"tool_name": "terminal", "status": "passed"},
            session_id="chat_neko",
        )
    )

    rows = persona_chat_trace_summary(
        persona_instances=[_chat_persona_instance("personainst_neko", "neko_supervisor", "chat_neko")],
        event_log=events,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["persona_instance_id"] == "personainst_neko"
    assert row["task_id"] is None
    assert row["session_id"] == "chat_neko"
    assert [entry["event"] for entry in row["entries"]] == ["tool_started", "tool_finished"]
    assert row["entries"][0]["summary"] == "echo PARITY_OK_2026"


def test_persona_chat_trace_merges_task_and_session_lanes_for_one_instance():
    events = EventLog()
    early = now()
    # Lane 1: a task-run tool event (task-keyed, no session lineage).
    events.append(
        Event(
            ts=early,
            type="run.tool.started",
            task_id="task_live",
            run_id="run_dev",
            persona_id="dev",
            payload={"tool_name": "pytest", "summary": "running suite"},
        )
    )
    # Lane 2: a later conversational tool call on the same instance's session.
    events.append(
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id=None,
            run_id=None,
            persona_id="dev",
            payload={"tool_name": "terminal", "status": "passed", "command_label": "git status"},
            session_id="chat_dev",
        )
    )

    instance = PersonaInstance(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="dev",
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        current_task_id="task_live",
        session_id="chat_dev",
    )

    rows = persona_chat_trace_summary(persona_instances=[instance], event_log=events)

    assert len(rows) == 1
    row = rows[0]
    # Both lanes merge into one chronologically-ordered trace, no double counting.
    assert [entry["event"] for entry in row["entries"]] == ["tool_started", "tool_finished"]
    assert [entry["tool_name"] for entry in row["entries"]] == ["pytest", "terminal"]
    assert row["task_id"] == "task_live"
    assert row["session_id"] == "chat_dev"


def test_persona_chat_trace_projects_profile_persona_with_colon_id():
    # Regression: profile instances carry ids like "profile:alice". The events
    # store that raw id; the projection must canonicalize (not token-mangle to
    # "profile_alice") or every profile-instance tool call is silently dropped.
    events = EventLog()
    ts = now()
    events.append(
        Event(
            ts=ts,
            type="run.tool.started",
            task_id=None,
            run_id=None,
            persona_id="profile:alice",
            payload={"tool_name": "terminal", "command_label": "echo hi"},
            session_id="persona_chat_alice",
        )
    )

    instance = PersonaInstance(
        id=None,
        persona_id="profile:alice",
        role="alice_supervisor",
        display_name="Alice Agent",
        profile_id="alice",
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id="persona_chat_alice",
    )

    rows = persona_chat_trace_summary(persona_instances=[instance], event_log=events)

    assert len(rows) == 1
    row = rows[0]
    assert row["persona_id"] == "profile:alice"
    # Matches the history projection's id derivation so the Launcher links them.
    assert row["persona_instance_id"] == "personainst_profile_alice"
    assert [entry["event"] for entry in row["entries"]] == ["tool_started"]


def test_persona_chat_trace_accountant_records_drops():
    from agent_runtime.parity import ProjectionAccountant

    events = EventLog()
    ts = now()
    # Three renderable chat tool events for the instance's persona...
    for _ in range(3):
        events.append(
            Event(
                ts=ts,
                type="run.tool.started",
                task_id=None,
                run_id=None,
                persona_id="neko_supervisor",
                payload={"tool_name": "terminal"},
                session_id="chat_x",
            )
        )
    # ...and one event for a DIFFERENT persona on the same session (mismatch).
    events.append(
        Event(
            ts=ts,
            type="run.tool.started",
            task_id=None,
            run_id=None,
            persona_id="other_persona",
            payload={"tool_name": "terminal"},
            session_id="chat_x",
        )
    )

    accountant = ProjectionAccountant("persona_chat_trace")
    persona_chat_trace_summary(
        persona_instances=[_chat_persona_instance("personainst_neko", "neko_supervisor", "chat_x")],
        event_log=events,
        message_tail=2,
        accountant=accountant,
    )

    summary = accountant.summary()
    assert summary["included"] == 2  # tail=2
    assert summary["reasons"].get("tail_truncated") == 1  # 3 renderable − 2 kept
    assert summary["reasons"].get("persona_mismatch") == 1  # the other-persona event
    assert summary["truncated"] is True


def test_persona_chat_trace_tolerates_event_log_without_for_session():
    class _TaskOnlyLog:
        def for_task(self, task_id, *, limit=50):
            return []

    rows = persona_chat_trace_summary(
        persona_instances=[_chat_persona_instance("personainst_neko", "neko_supervisor", "chat_neko")],
        event_log=_TaskOnlyLog(),
    )
    assert rows == []


def _mission_persona_instance(
    instance_id: str, persona_id: str, task_id: str, *, assignment_id: str | None
) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id=persona_id,
        role="dev",
        display_name=persona_id,
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        session_id=f"mission_{persona_id}",
        current_task_id=task_id,
        current_assignment_id=assignment_id,
        current_chat_goal="Implement the fix",
    )


def _persona_assignment(
    assignment_id: str,
    *,
    instance_id: str,
    task_id: str,
    created_at: datetime,
    updated_at: datetime | None = None,
) -> PersonaAssignment:
    return PersonaAssignment(
        id=assignment_id,
        persona_instance_id=instance_id,
        persona_id="dev",
        kind="task",
        state="running",
        title="Mission",
        message="Do the mission",
        created_by="operator",
        created_at=created_at,
        updated_at=updated_at or created_at,
        task_id=task_id,
    )


def _persona_instance(instance_id: str, persona_id: str, task_id: str) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id=persona_id,
        role="dev",
        display_name=persona_id,
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="task_bound",
        current_task_id=task_id,
    )


def _chat_persona_instance(instance_id: str, persona_id: str, session_id: str) -> PersonaInstance:
    return PersonaInstance(
        id=instance_id,
        persona_id=persona_id,
        role="alice_supervisor",
        display_name=persona_id,
        profile_id=None,
        runtime_root="test-runtime",
        state=WorkerSessionState.IDLE,
        mode="chat",
        session_id=session_id,
    )
