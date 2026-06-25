"""Operator-facing curation of the agent's raw working session (audit Stage 2C / S3).

The persona instance binds the agent's *internal* session, whose raw rows are
verbose tick-context prompts and serialized decision dicts. The operator must
see a clean transcript: decision summaries, no internal scaffolding/tool/system
noise. Regression guard for the live-smoke breakage (raw JSON + tick context
leaking into the Agent Console).
"""

import json

from hermes_time import now

from agent_runtime.events import EventLog
from agent_runtime.models import Event, PersonaInstance
from agent_runtime.persona_chat_history import (
    _iso_timestamp,
    _safe_recent_messages,
    persona_chat_trace_summary,
)
from agent_runtime.states import WorkerSessionState


class FakeSessionDB:
    def __init__(self, messages):
        self._messages = messages

    def get_messages(self, session_id, include_inactive=False):
        return list(self._messages)


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
