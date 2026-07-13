from agent_runtime.parity import ProjectionAccountant, events_watermark
from agent_runtime.snapshot import _parity_warnings, build_snapshot


def test_projection_accountant_tallies_and_samples():
    acct = ProjectionAccountant("demo")
    acct.consider(10)
    acct.include(7)
    acct.drop("redacted", entity_id="msg_1")
    acct.drop("tail_truncated", count=2, entity_id="inst_1")
    acct.mark_truncated()

    summary = acct.summary()
    assert summary["considered"] == 10
    assert summary["included"] == 7
    assert summary["dropped"] == 3  # 1 redacted + 2 tail_truncated
    assert summary["reasons"] == {"redacted": 1, "tail_truncated": 2}
    assert summary["truncated"] is True

    samples = acct.drop_samples()
    # count>1 still records ONE sample record (the tally carries the count).
    assert [s["code"] for s in samples] == ["redacted", "tail_truncated"]
    assert samples[0]["entity_id"] == "msg_1"


def test_projection_accountant_caps_drop_samples():
    acct = ProjectionAccountant("demo")
    for index in range(120):
        acct.drop("noisy", entity_id=f"e{index}")
    assert acct.summary()["reasons"]["noisy"] == 120  # full tally
    assert len(acct.drop_samples()) == 50  # bounded sample


def test_events_watermark_reports_offset(isolate_agent_runtime_root):
    from hermes_time import now

    from agent_runtime.events import EventLog
    from agent_runtime.models import Event

    log = EventLog()
    log.append(Event(ts=now(), type="task.created", task_id="t1", run_id=None, persona_id=None))

    wm = events_watermark(last_event_ts="2026-06-25T00:00:00Z")
    assert wm["event_offset"] > 0  # file has bytes
    assert wm["last_event_ts"] == "2026-06-25T00:00:00Z"
    assert wm["captured_at"] is not None


def test_parity_warnings_flags_runtime_disabled():
    warnings = _parity_warnings({"persona_instance_runtime": {"enabled": False}})
    assert [w["code"] for w in warnings] == ["persona_instance_runtime_disabled"]


def test_parity_warnings_flags_orphaned_trace_row():
    data = {
        "persona_instance_runtime": {"enabled": True},
        "persona_instances": [{"persona_id": "neko_supervisor", "persona_instance_id": "personainst_neko"}],
        "persona_chat_trace": [
            {"persona_id": "profile:ghost", "persona_instance_id": "personainst_ghost", "entries": []},
        ],
        "summary": {"open_tasks": 0},
        "tasks": [],
    }
    codes = [w["code"] for w in _parity_warnings(data)]
    assert "trace_persona_not_in_instances" in codes


def test_parity_warnings_clean_when_trace_matches_instance():
    data = {
        "persona_instance_runtime": {"enabled": True},
        "persona_instances": [{"persona_id": "profile:alice", "persona_instance_id": "personainst_profile_alice"}],
        "persona_chat_trace": [
            {"persona_id": "profile:alice", "persona_instance_id": "personainst_profile_alice", "entries": []},
        ],
        "operator_channels": [],
        "summary": {"open_tasks": 0},
        "tasks": [],
    }
    assert _parity_warnings(data) == []


def test_build_snapshot_carries_parity_envelope(isolate_agent_runtime_root):
    snapshot = build_snapshot()
    parity = snapshot["parity"]
    assert parity["envelope_version"] == 1
    assert isinstance(parity["build_ms"], int) and parity["build_ms"] >= 0
    assert "event_offset" in parity["watermark"]
    assert isinstance(parity["completeness"], dict)
    assert isinstance(parity["warnings"], list)
    assert isinstance(parity["drops"], list)


def test_build_snapshot_carries_redaction_mode_from_env(isolate_agent_runtime_root, monkeypatch):
    monkeypatch.setenv("HERMES_REDACTION_MODE", "observe")

    snapshot = build_snapshot()

    assert snapshot["parity"]["redaction_mode"] == "observe"


def test_parity_warnings_flags_open_incident_budget(monkeypatch):
    monkeypatch.delenv("HERMES_REDACTION_MODE", raising=False)
    warnings = _parity_warnings(
        {
            "persona_instance_runtime": {"enabled": True},
            "persona_instances": [],
            "persona_chat_trace": [],
            "operator_channels": [],
            "summary": {"open_tasks": 0, "open_incidents": 101},
            "tasks": [],
        }
    )

    assert "open_incident_budget_exceeded" in [warning["code"] for warning in warnings]
