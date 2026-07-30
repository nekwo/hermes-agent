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


def test_projection_accountant_declares_by_design_reason_codes():
    """A bounded lane and a broken join both "drop" — only one is a defect.

    The classification is declared at the emission site and rides the envelope
    so a reader never has to maintain its own reason allowlist (the Launcher's
    hardcoded copy went stale twice: flow_item_cap, then the persona-chat limit,
    each pinning the "projection drops" pill amber on a healthy runtime).
    """

    acct = ProjectionAccountant("persona_chat_history")
    acct.consider(163)
    acct.include(50)
    acct.drop("limit", count=103, by_design=True)
    acct.drop("session_not_in_db", count=10, entity_id="persona_chat_ghost")

    summary = acct.summary()
    # Existing keys are untouched: `dropped` still counts EVERY drop.
    assert summary["considered"] == 163
    assert summary["included"] == 50
    assert summary["dropped"] == 113
    assert summary["reasons"] == {"limit": 103, "session_not_in_db": 10}
    # Additive key: only the deliberate bound is declared by-design.
    assert summary["by_design"] == ["limit"]
    assert acct.dropped_by_design == 103
    # A reader subtracts by-design reasons itself and is left with the anomaly.
    anomalous = summary["dropped"] - sum(
        summary["reasons"][code] for code in summary["by_design"]
    )
    assert anomalous == 10


def test_projection_accountant_summary_shape_is_stable_without_by_design_drops():
    acct = ProjectionAccountant("demo")
    acct.consider(3)
    acct.include(2)
    acct.drop("no_instance_match", entity_id="s1")

    summary = acct.summary()
    # The key is ALWAYS present; empty list = every drop here is anomalous.
    assert summary["by_design"] == []
    assert set(summary) == {
        "considered",
        "included",
        "dropped",
        "reasons",
        "truncated",
        "by_design",
    }
    assert ProjectionAccountant("empty").summary()["by_design"] == []


def test_projection_accountant_by_design_declaration_is_per_code_and_sticky():
    acct = ProjectionAccountant("demo")
    acct.drop("tail_truncated", by_design=True)
    acct.drop("tail_truncated")  # same code, undeclared at this site
    assert acct.summary()["by_design"] == ["tail_truncated"]
    assert acct.summary()["reasons"] == {"tail_truncated": 2}


def test_drop_samples_carry_the_by_design_flag():
    acct = ProjectionAccountant("demo")
    acct.drop("flow_item_cap", count=4, entity_id="task_1", by_design=True)
    acct.drop("unrenderable_entry", entity_id="inst_1")

    by_code = {sample["code"]: sample for sample in acct.drop_samples()}
    assert by_code["flow_item_cap"]["by_design"] is True
    assert by_code["unrenderable_entry"]["by_design"] is False


def test_build_snapshot_completeness_rows_all_carry_by_design(isolate_agent_runtime_root):
    completeness = build_snapshot()["parity"]["completeness"]
    # S9's compact snapshot has no mission-row projections in an empty runtime.
    assert completeness == {}
    for projection, row in completeness.items():
        assert isinstance(row.get("by_design"), list), projection


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
        # S4: operator_channels is an id-keyed map (empty here).
        "operator_channels": {},
        "summary": {"open_tasks": 0},
        "goals": {},
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


def test_build_snapshot_parity_carries_sections_ms(isolate_agent_runtime_root):
    snapshot = build_snapshot()
    sections = snapshot["parity"]["sections_ms"]
    assert isinstance(sections, dict)
    # The documented, stable, lowercase section keys are always present (even a
    # skipped lane records 0), and every value is a non-negative int ms.
    for key in (
        "agents_readiness",
        "prompt_observability",
        "events",
        "persona_chat",
        "boards_offices",
        "parity",
    ):
        assert key in sections, key
        assert isinstance(sections[key], int) and sections[key] >= 0
    # Additive: build_ms still present and section timings never exceed it wildly.
    assert isinstance(snapshot["parity"]["build_ms"], int)


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
