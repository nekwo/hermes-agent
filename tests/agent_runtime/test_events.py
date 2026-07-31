import json

import pytest

from hermes_time import now

from agent_runtime import paths
from agent_runtime.errors import EventPayloadTooLarge
from agent_runtime.events import EventLog
from agent_runtime.models import Event


def test_event_log_appends_jsonl_and_tails_events(isolate_agent_runtime_root):
    log = EventLog()
    first = Event(ts=now(), type="persona_instance.created", task_id="task_1", run_id=None, persona_id=None)
    second = Event(
        ts=now(),
        type="persona_instance.steered",
        task_id="task_1",
        run_id=None,
        persona_id="pm",
        payload={"from": "created", "to": "pm_triage"},
    )

    log.append(first)
    log.append(second)

    raw_lines = (isolate_agent_runtime_root / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2
    assert json.loads(raw_lines[1])["payload"] == {"from": "created", "to": "pm_triage"}
    assert log.tail(1) == [second]
    assert list(log.iter_since(first.ts)) == [first, second]


def test_event_log_for_task_filters_before_decoding_and_preserves_order(isolate_agent_runtime_root):
    log = EventLog()
    for index in range(6):
        log.append(Event(ts=now(), type="persona_instance.created", task_id=f"task_noise_{index}", run_id=None, persona_id=None))
        log.append(
            Event(
                ts=now(),
                type="run.progress",
                task_id="task_target",
                run_id=f"run_{index}",
                persona_id="dev",
                payload={"summary": f"target {index}"},
            )
        )

    all_events = log.for_task("task_target", limit=0)
    assert [event.run_id for event in all_events] == [f"run_{index}" for index in range(6)]

    limited_events = log.for_task("task_target", limit=2)
    assert [event.run_id for event in limited_events] == ["run_4", "run_5"]


def test_event_log_for_session_filters_session_lane_and_ignores_task_events(isolate_agent_runtime_root):
    log = EventLog()
    # A task-run event: keyed on task_id, no session lineage.
    log.append(
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id="task_run",
            run_id="run_1",
            persona_id="dev",
            payload={"tool_name": "pytest", "status": "passed"},
        )
    )
    # Two chat-turn events on the target session, interleaved with another session.
    log.append(
        Event(
            ts=now(),
            type="run.tool.started",
            task_id=None,
            run_id=None,
            persona_id="neko_supervisor",
            payload={"tool_name": "terminal"},
            session_id="chat_target",
        )
    )
    log.append(
        Event(
            ts=now(),
            type="run.tool.started",
            task_id=None,
            run_id=None,
            persona_id="neko_supervisor",
            payload={"tool_name": "terminal"},
            session_id="chat_other",
        )
    )
    log.append(
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id=None,
            run_id=None,
            persona_id="neko_supervisor",
            payload={"tool_name": "terminal", "status": "passed"},
            session_id="chat_target",
        )
    )

    rows = log.for_session("chat_target")
    assert [event.type for event in rows] == ["run.tool.started", "run.tool.finished"]
    assert all(event.session_id == "chat_target" for event in rows)
    # The task-run event never leaks into the session lane.
    assert all(event.task_id is None for event in rows)
    assert log.for_session("chat_target", limit=1)[0].type == "run.tool.finished"


def test_event_log_for_session_decodes_legacy_rows_without_session_field(isolate_agent_runtime_root):
    # A legacy JSONL row written before Event grew a session_id field must still
    # decode (session_id defaults to None) and must not match a session query.
    path = isolate_agent_runtime_root / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "ts": now().isoformat().replace("+00:00", "Z"),
                "type": "run.progress",
                "task_id": "legacy_task",
                "run_id": "run_legacy",
                "persona_id": "dev",
                "payload": {"summary": "legacy"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log = EventLog()
    assert log.for_session("anything") == []
    assert log.tail(1)[0].session_id is None


def test_cached_event_log_matches_base_and_reads_once(isolate_agent_runtime_root, monkeypatch):
    from agent_runtime.events import CachedEventLog

    base = EventLog()
    for index in range(4):
        base.append(Event(ts=now(), type="run.tool.started", task_id="task_a", run_id=f"r{index}", persona_id="dev", payload={"tool_name": "x"}))
    base.append(
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id=None,
            run_id=None,
            persona_id="neko_supervisor",
            payload={"tool_name": "terminal", "status": "passed"},
            session_id="chat_z",
        )
    )

    cached = CachedEventLog()
    # Equivalence with the base log across every read method.
    assert [e.run_id for e in cached.for_task("task_a")] == [e.run_id for e in base.for_task("task_a")]
    assert cached.for_task("task_a", limit=2)[-1].run_id == base.for_task("task_a", limit=2)[-1].run_id
    assert [e.type for e in cached.for_session("chat_z")] == [e.type for e in base.for_session("chat_z")]
    assert [e.type for e in cached.tail(2)] == [e.type for e in base.tail(2)]
    assert len(list(cached.iter_all())) == len(list(base.iter_all()))

    # The file is read exactly once regardless of how many reads happen.
    reads = {"n": 0}
    real_read_text = type(paths.events_path()).read_text

    def counting_read_text(self, *a, **k):
        reads["n"] += 1
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(type(paths.events_path()), "read_text", counting_read_text)
    fresh = CachedEventLog()
    fresh.for_task("task_a")
    fresh.for_session("chat_z")
    fresh.tail(1)
    assert reads["n"] == 1


def test_event_log_for_task_type_filter_counts_matches_not_raw_rows(isolate_agent_runtime_root):
    # Live failure shape (task_bd98d444, 2026-07-05): a budget-incident loop
    # floods the newest task events with hundreds of non-trace rows, starving
    # any fetch window that counts raw rows before type filtering.
    log = EventLog()
    trace_types = {"run.tool.started", "run.tool.finished", "run.progress"}
    for index in range(10):
        log.append(
            Event(
                ts=now(),
                type="run.tool.started",
                task_id="task_flooded",
                run_id=f"run_{index}",
                persona_id="dev",
                payload={"tool_name": "read_file", "summary": f"tool {index}"},
            )
        )
    for _ in range(300):
        log.append(Event(ts=now(), type="incident.opened", task_id="task_flooded", run_id=None, persona_id="dev"))

    # Untyped fetch: the window is consumed by the flood (documents the trap).
    untyped = log.for_task("task_flooded", limit=10)
    assert all(event.type == "incident.opened" for event in untyped)

    # Typed fetch: limit counts matched trace rows, so the flood cannot starve it.
    typed = log.for_task("task_flooded", limit=10, types=trace_types)
    assert [event.run_id for event in typed] == [f"run_{index}" for index in range(10)]
    assert all(event.type == "run.tool.started" for event in typed)

    # A tighter typed limit still returns the newest matches, oldest-first.
    newest_two = log.for_task("task_flooded", limit=2, types=trace_types)
    assert [event.run_id for event in newest_two] == ["run_8", "run_9"]


def test_event_log_for_session_type_filter_counts_matches_not_raw_rows(isolate_agent_runtime_root):
    log = EventLog()
    trace_types = {"run.tool.started", "run.tool.finished", "run.progress"}
    for index in range(4):
        log.append(
            Event(
                ts=now(),
                type="run.tool.finished",
                task_id=None,
                run_id=None,
                persona_id="base",
                payload={"tool_name": "terminal", "status": "passed", "summary": f"turn {index}"},
                session_id="chat_flooded",
            )
        )
    # Filler is any registered type OUTSIDE ``trace_types``; S25 retargeted it
    # off run.opened (de-registered with its writer) onto a live chat-lane type.
    for index in range(50):
        log.append(
            Event(
                ts=now(),
                type="persona_instance.created",
                task_id=None,
                run_id=None,
                persona_id="base",
                payload={"persona_instance_id": f"personainst_flood_{index}"},
                session_id="chat_flooded",
            )
        )

    typed = log.for_session("chat_flooded", limit=4, types=trace_types)
    assert [event.payload["summary"] for event in typed] == [f"turn {index}" for index in range(4)]


def test_operator_events_receive_redaction_safe_summaries(isolate_agent_runtime_root):
    log = EventLog()
    samples = [
        Event(now(), "repo_bundle.assigned", "task_1", "run_1", "dev", {"repo_bundle_id": "bundle_1", "repo": "hermes-agent", "state": "assigned"}),
        Event(now(), "repo_bundle.updated", "task_1", "run_1", "dev", {"repo_bundle_id": "bundle_1", "repo": "hermes-agent", "state": "running"}),
        # S25 retargeted two samples: off run.opened and off repo_bundle.delivered
        # (both de-registered with their writers) onto live operator-summary arms.
        Event(now(), "run.tool.started", "task_1", "run_1", "dev", {"tool_name": "terminal"}),
        Event(now(), "run.tool.finished", "task_1", "run_1", "dev", {"tool_name": "terminal", "status": "passed"}),
        Event(now(), "run.closed", "task_1", "run_1", "dev", {"state": "completed", "decision_type": "deliver"}),
    ]

    for event in samples:
        log.append(event)

    events = list(log.iter_all())
    assert all(str(event.payload.get("summary") or "").strip() for event in events)
    updated = next(event for event in events if event.type == "repo_bundle.updated")
    assert updated.payload["summary"] == "Updated hermes-agent bundle to running."


def test_run_progress_receives_stable_event_id(isolate_agent_runtime_root):
    log = EventLog()

    log.append(
        Event(
            now(),
            "run.progress",
            "task_1",
            "run_1",
            "dev",
            {"phase": "proof", "step": "proof_command_running", "status": "running", "command_index": 1},
        )
    )

    event = list(log.iter_all())[0]
    assert event.payload["event_id"] == "progress:run_1:proof:proof_command_running:1"
    assert event.payload["summary"] == "Progress: proof proof_command_running running."


def test_cached_event_log_type_filter_matches_base(isolate_agent_runtime_root):
    from agent_runtime.events import CachedEventLog

    base = EventLog()
    trace_types = {"run.tool.started", "run.tool.finished", "run.progress"}
    for index in range(6):
        base.append(
            Event(
                ts=now(),
                type="run.progress",
                task_id="task_typed",
                run_id=f"run_{index}",
                persona_id="dev",
                payload={"summary": f"progress {index}"},
            )
        )
        base.append(Event(ts=now(), type="incident.opened", task_id="task_typed", run_id=None, persona_id="dev"))

    cached = CachedEventLog()
    for limit in (0, 3, 6):
        assert [e.run_id for e in cached.for_task("task_typed", limit=limit, types=trace_types)] == [
            e.run_id for e in base.for_task("task_typed", limit=limit, types=trace_types)
        ]
    assert all(e.type == "run.progress" for e in cached.for_task("task_typed", limit=0, types=trace_types))


def test_cached_event_log_does_not_duplicate_events_whose_payload_echoes_their_id(isolate_agent_runtime_root):
    # A real ``lane.created`` row (runtime_instances.RuntimeInstanceStore.save)
    # repeats the task id inside its own payload, so the serialized line carries
    # the ``"task_id":"…"`` token twice. The cached index must still hand that
    # line to the scan once.
    from agent_runtime.events import CachedEventLog

    base = EventLog()
    base.append(
        Event(
            ts=now(),
            type="lane.created",
            task_id="task_lane",
            run_id=None,
            persona_id=None,
            payload={
                "runtime_instance_id": "goalrt_abc123",
                "task_id": "task_lane",
                "lane": "goalrt_abc123",
                "state": "queued",
                "reason": "lane created",
            },
        )
    )
    base.append(
        Event(
            ts=now(),
            type="run.progress",
            task_id="task_lane",
            run_id="run_1",
            persona_id="dev",
            payload={"summary": "after the lane"},
        )
    )

    cached = CachedEventLog()
    expected = base.for_task("task_lane", limit=0)
    actual = cached.for_task("task_lane", limit=0)
    assert len(actual) == len(expected)
    assert [e.type for e in actual] == [e.type for e in expected]
    assert [e.run_id for e in actual] == [e.run_id for e in expected]
    # Duplicates would also burn the caller's window.
    assert [e.type for e in cached.for_task("task_lane", limit=1)] == [
        e.type for e in base.for_task("task_lane", limit=1)
    ]


def test_cached_event_log_indexes_one_line_under_each_distinct_token(isolate_agent_runtime_root):
    # One line legitimately carrying two DIFFERENT tokens must resolve once from
    # each index — deduping per line must not collapse task and session lanes.
    from agent_runtime.events import CachedEventLog

    base = EventLog()
    base.append(
        Event(
            ts=now(),
            type="run.tool.finished",
            task_id="task_dual",
            run_id="run_1",
            persona_id="dev",
            payload={"tool_name": "pytest", "status": "passed"},
            session_id="chat_dual",
        )
    )

    cached = CachedEventLog()
    assert [e.run_id for e in cached.for_task("task_dual")] == [e.run_id for e in base.for_task("task_dual")]
    assert len(cached.for_task("task_dual")) == 1
    assert [e.run_id for e in cached.for_session("chat_dual")] == [e.run_id for e in base.for_session("chat_dual")]
    assert len(cached.for_session("chat_dual")) == 1


def test_event_log_rejects_payloads_over_4kb_and_does_not_write(isolate_agent_runtime_root):
    log = EventLog()
    event = Event(
        ts=now(),
        type="persona_instance.created",
        task_id="task_1",
        run_id=None,
        persona_id=None,
        payload={"blob": "x" * 5000},
    )

    with pytest.raises(EventPayloadTooLarge):
        log.append(event)

    assert not (isolate_agent_runtime_root / "events.jsonl").exists()
