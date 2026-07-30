import json
from pathlib import Path

import pytest

from hermes_time import now

from agent_runtime import paths
from agent_runtime.errors import EventPayloadTooLarge
from agent_runtime.events import EventLog
from agent_runtime.models import Event
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.packets import make_packet, record_decision_packets, record_packet
from agent_runtime.decision_contracts import validate_planning_decision
from agent_runtime.decision_schema import AgentDecision, DecisionType
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore


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
        Event(now(), "repo_bundle.delivered", "task_1", "run_1", "dev", {"repo_bundle_id": "bundle_1", "repo": "hermes-agent", "state": "delivered_waiting_for_qa", "proof_count": 0, "diff_captured": False}),
        # S25 retargeted this sample off run.opened (de-registered with its
        # writer) onto the other live operator-summary arm.
        Event(now(), "run.tool.started", "task_1", "run_1", "dev", {"tool_name": "terminal"}),
        Event(now(), "run.closed", "task_1", "run_1", "dev", {"state": "completed", "decision_type": "deliver"}),
    ]

    for event in samples:
        log.append(event)

    events = list(log.iter_all())
    assert all(str(event.payload.get("summary") or "").strip() for event in events)
    delivered = next(event for event in events if event.type == "repo_bundle.delivered")
    assert "captured:false" in delivered.payload["summary"]


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


def test_packet_recording_is_idempotent_and_append_only():
    log = EventLog()
    task = Task(id="task_packet", title="Packet", description="Packet", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test")
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="proof",
        rationale="proof",
        payload={"stage_id": "stage_1", "commands": ["pytest"], "delivery": {"work_status": "proof_requested"}},
    )
    packet = make_packet(task=task, decision=decision, packet_type="delivery", body={"work_status": "proof_requested"}, actor="dev", run_id="run_1", stage_id="stage_1")

    assert record_packet(packet, event_log=log) is True
    assert record_packet(packet, event_log=log) is False

    events = log.for_task(task.id, limit=0)
    assert [event.type for event in events] == ["packet.recorded", "packet.duplicate"]
    assert events[1].payload["duplicate_of"] == packet.packet_id
    recorded = events[0].payload
    assert recorded["assignment_id"] is None
    assert recorded["target_owner"] is None
    assert recorded["validation_status"] == "valid"
    assert recorded["normalization_status"] == "unchanged"
    assert recorded["raw_artifact_id"] == f"{packet.packet_id}.raw"
    assert recorded["raw_artifact_path"].endswith(f"{packet.packet_id}.raw.json")


def test_packet_contract_repair_emits_redaction_safe_progress():
    log = EventLog()
    task = Task(id="task_packet", title="Packet", description="Packet", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test")
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="proof",
        rationale="proof",
        payload={
            "stage_id": "stage_1",
            "commands": ["pytest"],
            "delivery": {
                "work_status": "proof_requested",
                "known_gaps": ["auth token refresh contract still needs proof"],
                "notes": "move this into structured fields",
            },
        },
    )

    validate_planning_decision(decision)
    record_decision_packets(task, decision, actor="dev", run_id="run_1", event_log=log, stage_id="stage_1")

    events = log.for_task(task.id, limit=0)
    assert [event.type for event in events] == ["packet.recorded", "packet.normalized", "run.progress"]
    assert events[0].payload["body"]["known_gaps"] == ["auth [redacted-term] refresh contract still needs proof"]
    assert events[0].payload["normalization_status"] == "normalized"
    assert events[0].payload["dropped_fields"] == ["notes"]
    assert events[1].payload["dropped_fields"] == ["notes"]
    assert events[1].payload["raw_artifact_id"] == events[0].payload["raw_artifact_id"]
    assert events[2].payload == {
        "step": "contract_repaired",
        "status": "normalized",
        "summary": "packet metadata normalized; sensitive vocabulary masked",
        "stage_id": "stage_1",
    }


def test_packet_recording_preserves_raw_artifact_for_normalized_packet(isolate_agent_runtime_root):
    task = Task(id="task_packet", title="Packet", description="Packet", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test")
    log = EventLog()
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="proof",
        rationale="proof",
        payload={
            "stage_id": "stage_1",
            "commands": ["pytest"],
            "delivery": {
                "work_status": "proof_requested",
                "summary": "proof",
                "unsupported_context_blob": {"kept": "only in raw artifact"},
            },
        },
    )

    validate_planning_decision(decision)
    record_decision_packets(task, decision, actor="dev", run_id="run_1", event_log=log, stage_id="stage_1")

    recorded = log.for_task(task.id, limit=0)[0].payload
    raw_path = isolate_agent_runtime_root / recorded["raw_artifact_path"]
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw["packet_id"] == recorded["packet_id"]
    assert raw["raw_body"]["unsupported_context_blob"] == {"kept": "only in raw artifact"}


def test_archive_preserves_packet_raw_artifacts(isolate_agent_runtime_root):
    store = TaskStore()
    task = Task(id="task_packet_archive", title="Packet", description="Packet", state=TaskState.DONE, created_at=now(), updated_at=now(), requested_by="test")
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="proof",
        rationale="proof",
        payload={"stage_id": "stage_1", "commands": ["pytest"], "delivery": {"work_status": "proof_requested", "summary": "proof", "extra_detail": "raw only"}},
    )
    validate_planning_decision(decision)
    log = EventLog()
    record_decision_packets(task, decision, actor="dev", run_id="run_1", event_log=log, stage_id="stage_1")

    recorded = log.for_task(task.id, limit=0)[0].payload
    assert Path(isolate_agent_runtime_root / recorded["raw_artifact_path"]).exists()
    assert not hasattr(store, "archive")


def test_record_decision_packets_preserves_backend_contract_packet():
    task = Task(id="task_packet", title="Packet", description="Packet", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test")
    log = EventLog()
    decision = AgentDecision(
        type=DecisionType.REQUEST_TEST_RUN,
        summary="backend proof",
        rationale="proof",
        payload={
            "stage_id": "backend_contract",
            "commands": ["python manage.py check"],
            "delivery": {
                "work_status": "proof_requested",
                "produced_contract_packet_id": "pending_harness_contract_packet_record",
                "contract_packet": {
                    "endpoint": "GET /api/stage47",
                    "request_shape": {},
                    "response_shape": {"ok": "boolean"},
                    "error_shape": {"error": "string"},
                    "example_response": {"ok": True},
                },
                "consumed_proof_ids": ["proof_backend"],
                "known_gaps": [],
                "next_owner": "neko_supervisor",
            },
        },
    )

    validate_planning_decision(decision)
    record_decision_packets(task, decision, actor="backend_dev", run_id="run_backend", event_log=log, stage_id="backend_contract")

    event = log.for_task(task.id, limit=0)[0]
    body = event.payload["body"]
    assert body["contract_packet"]["contract_packet_id"].startswith("packet_contract_")
    assert body["produced_contract_packet_id"] == body["contract_packet"]["contract_packet_id"]
    assert body["contract_packet"]["endpoint"] == "GET /api/stage47"
    assert "ignored unsupported metadata keys: contract_packet" not in body.get("operator_note", "")


def test_record_decision_packets_preserves_no_edit_investigation_delivery_fields():
    task = Task(id="task_packet", title="Packet", description="Packet", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test")
    log = EventLog()
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="Delivered no-edit investigation report.",
        rationale="No product files changed.",
        payload={
            "summary": "No-edit report delivered.",
            "changed_files": [],
            "tests": ["no product edits"],
            "delivery": {
                "work_status": "patch_proposed",
                "summary": "NSFW filtering can leak when media is visible before moderation finishes.",
                "findings": ["MediaAsset finalize path can mark uploads visible before AI moderation evidence is attached."],
                "recommendations": ["Add pending/quarantine default and require passed moderation before public feed exposure."],
                "model_options": ["Qwen2.5-VL 7B or similar quantized local VLM can be tested as a cheap scanner with latency and calibration caveats."],
                "wd_tagger_assessment": "WD tagger safety ratings are weak auxiliary signals for triage/ranking, not sufficient as the sole enforcement gate.",
                "questions": ["Should borderline adult art be blocked or age-gated?"],
                "known_gaps": ["Need full feed route proof before implementation."],
            },
        },
    )

    validate_planning_decision(decision)
    record_decision_packets(task, decision, actor="backend_dev", run_id="run_backend", event_log=log, stage_id="backend_investigation")

    body = log.for_task(task.id, limit=0)[0].payload["body"]
    assert body["summary"].startswith("NSFW filtering")
    assert body["findings"] == ["MediaAsset finalize path can mark uploads visible before AI moderation evidence is attached."]
    assert body["recommendations"] == ["Add pending/quarantine default and require passed moderation before public feed exposure."]
    assert body["model_options"] == ["Qwen2.5-VL 7B or similar quantized local VLM can be tested as a cheap scanner with latency and calibration caveats."]
    assert body["wd_tagger_assessment"].startswith("WD tagger safety ratings are weak auxiliary signals")
    assert body["questions"] == ["Should borderline adult art be blocked or age-gated?"]


def test_no_edit_investigation_delivery_packet_is_compacted_below_event_limit():
    task = Task(id="task_packet", title="Packet", description="Packet", state=TaskState.CREATED, created_at=now(), updated_at=now(), requested_by="test")
    log = EventLog()
    decision = AgentDecision(
        type=DecisionType.PROPOSE_PATCH,
        summary="Delivered large no-edit investigation report.",
        rationale="No product files changed.",
        payload={
            "summary": "No-edit report delivered.",
            "changed_files": [],
            "tests": ["no product edits"],
            "delivery": {
                "work_status": "patch_proposed",
                "summary": "S" * 2000,
                "findings": ["F" * 1000 for _ in range(12)],
                "recommendations": ["R" * 1000 for _ in range(12)],
                "model_options": ["M" * 1000 for _ in range(12)],
                "wd_tagger_assessment": "W" * 1000,
                "questions": ["Q" * 1000 for _ in range(12)],
                "known_gaps": ["G" * 1000 for _ in range(12)],
            },
        },
    )

    validate_planning_decision(decision)
    record_decision_packets(task, decision, actor="backend_dev", run_id="run_backend", event_log=log, stage_id="backend_investigation")

    body = log.for_task(task.id, limit=0)[0].payload["body"]
    assert len(body["summary"]) <= 240
    assert len(body["findings"]) <= 4
    assert all(len(item) <= 140 for item in body["findings"])
    assert len(body["model_options"]) <= 4
    assert all(len(item) <= 180 for item in body["model_options"])
    assert len(body["wd_tagger_assessment"]) <= 360
