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
    # A reader subtracts by-design reasons itself and is left with the anomaly.
    # There is deliberately no ``dropped_by_design`` accessor to do it for
    # them: it was a second authority for a number ``summary()`` already
    # carries, and only tests ever asked it.
    assert sum(summary["reasons"][code] for code in summary["by_design"]) == 103
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
    # RETARGETED at S56 (2026-08-01), not deleted: the three persona-chat
    # accountants used to sit behind `persona_instance_runtime_enabled(cfg)`, so
    # an empty runtime published `{}`. S56 deleted that gate — the roster and its
    # accountants are unconditional now — so the empty-runtime frame carries the
    # three rows at zero rather than no rows at all. The pin moves to the exact
    # keyset so a FOURTH projection appearing (or one of these vanishing) still
    # turns this red, which "== {}" would no longer do.
    #
    # WP-H1 (2026-08-03) is that fourth projection, and this red is the pin
    # working: `running_work` accounts its own drops (exited PIDs, recycled
    # PIDs, bounded tails, lane caps) and would otherwise have shed rows with no
    # completeness row to declare them.
    assert set(completeness) == {
        "persona_chat_history",
        "persona_chat_trace",
        "operator_conversation",
        "running_work",
    }
    assert all(row["considered"] == 0 for row in completeness.values())
    for projection, row in completeness.items():
        assert isinstance(row.get("by_design"), list), projection


def test_projection_accountant_caps_drop_samples():
    acct = ProjectionAccountant("demo")
    for index in range(120):
        acct.drop("noisy", entity_id=f"e{index}")
    assert acct.summary()["reasons"]["noisy"] == 120  # full tally
    assert len(acct.drop_samples()) == 50  # bounded sample


def test_a_by_design_flood_cannot_starve_the_one_anomalous_drop():
    """H3 (plan ``realm-pull-live-projection``). THE live shape, in miniature.

    On the operator's store 132 by-design ``instance_retired`` rows filled every
    one of the 50 FIFO slots before the single ``session_not_in_db`` row was
    recorded, so the launcher's amber "projection drops 1" chip shipped with
    ``hasDetail == false`` — a bare pill with nothing behind it, and naming the
    offender took a Python join over a saved snapshot.

    The by-design half is bounded FIRST, and the anomalous rows lead the sample.

    Kill-mutation: put both kinds back in one FIFO list.
    """

    acct = ProjectionAccountant("persona_chat_history")
    for index in range(60):
        acct.drop("instance_retired", entity_id=f"retired_{index}", by_design=True)
    acct.drop("session_not_in_db", entity_id="personainst_profile_alice")

    samples = acct.drop_samples()
    assert samples[0]["code"] == "session_not_in_db"
    assert samples[0]["entity_id"] == "personainst_profile_alice"
    assert len(samples) <= 50
    # The envelope's own numbers do not move: every drop is still tallied and
    # the by-design declaration still rides the summary.
    assert acct.summary()["reasons"] == {"instance_retired": 60, "session_not_in_db": 1}
    assert acct.summary()["dropped"] == 61
    assert acct.summary()["by_design"] == ["instance_retired"]


def test_an_all_anomalous_projection_still_gets_the_whole_budget():
    """ANTI-VACUITY for the split: reserving a floor for anomalous rows must not
    have shrunk what an anomalous-only projection can sample. The by-design cap
    is a ceiling on ONE half, not a new total."""

    acct = ProjectionAccountant("demo")
    for index in range(120):
        acct.drop("unrenderable_entry", entity_id=f"e{index}")
    assert len(acct.drop_samples()) == 50
    assert all(sample["by_design"] is False for sample in acct.drop_samples())


def test_events_watermark_reports_offset(isolate_agent_runtime_root):
    from hermes_time import now

    from agent_runtime.events import EventLog
    from agent_runtime.models import Event

    log = EventLog()
    log.append(Event(ts=now(), type="persona_instance.created", task_id="t1", run_id=None, persona_id=None))

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


# B5 (2026-07-31): ``test_parity_warnings_flags_open_incident_budget`` stood
# here, hand-feeding ``summary: {"open_tasks": 0, "open_incidents": 101}`` — a
# summary shape ``build_snapshot`` has not emitted since contract 45 reduced the
# block to ``persona_instances``. This test WAS the only thing in the repo that
# ever produced ``open_incidents``, so it kept an unreachable production branch
# looking covered and green: a closed loop, not coverage (the same rule that
# retired the test-only modules in ledger item 2). The branch, the
# ``open_incident_warning_threshold`` knob it read, and this test went together.
# Reachability of every remaining warning code is now gated executably by
# tests/agent_runtime/test_parity_warning_catalog.py.


# ── an unreadable log has no position, and 0 is a position ──────────────────
#
# ``except OSError: offset = 0`` made an unreadable event log indistinguishable
# from an empty one. Every downstream reader acts on 0 as a real cursor at the
# head of the log.


def test_events_watermark_reports_unknown_when_the_log_cannot_be_read(monkeypatch):
    import agent_runtime.parity as parity_mod

    def _boom():
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(parity_mod.event_rotation, "log_end_offset", _boom)

    wm = events_watermark(last_event_ts="2026-08-09T00:00:00Z")

    # NOT 0 — 0 is "the head of the log", which is a full replay, not an unknown.
    assert wm["event_offset"] is None
    assert "cannot access the file" in wm["event_offset_error"]
    # The rest of the marker still resolves; only the position is unknown.
    assert wm["last_event_ts"] == "2026-08-09T00:00:00Z"
    assert wm["captured_at"] is not None


def test_readable_log_carries_no_error_key(isolate_agent_runtime_root):
    from hermes_time import now

    from agent_runtime.events import EventLog
    from agent_runtime.models import Event

    log = EventLog()
    log.append(Event(ts=now(), type="persona_instance.created", task_id="t1", run_id=None, persona_id=None))

    wm = events_watermark()

    assert wm["event_offset"] > 0
    assert "event_offset_error" not in wm


def test_parity_envelope_warns_when_the_frame_has_no_source_position(
    isolate_agent_runtime_root, monkeypatch
):
    import agent_runtime.parity as parity_mod

    def _boom():
        raise OSError(32, "share violation")

    monkeypatch.setattr(parity_mod.event_rotation, "log_end_offset", _boom)

    snapshot = build_snapshot()

    warnings = snapshot["parity"]["warnings"]
    codes = [w["code"] for w in warnings]
    assert "event_offset_unknown" in codes
    assert snapshot["parity"]["watermark"]["event_offset"] is None
    detail = next(w["detail"] for w in warnings if w["code"] == "event_offset_unknown")
    assert "resync" in detail


def test_the_parity_watermark_does_not_count_events_appended_during_the_build(
    isolate_agent_runtime_root, monkeypatch
):
    """The offset is captured BEFORE the first section is read, so it is a LOWER
    bound on what the core carries.

    THE DEFECT THIS NAMES (measured 2026-08-21). The offset used to be stat'd
    inside ``_parity_envelope``, at the END of a multi-second build whose content
    was read at the start. An operator who dropped a QA agent onto the office
    canvas while a build was in flight got a core that COUNTED the agent's
    creation events in its offset but did not CARRY the agent in its content. The
    launcher folded it, advanced its watermark past those events, and the stream
    resumed its tail from that offset -- so they were never replayed and the
    agent vanished from Mission Control. Every agent that predated the build
    survived, which is the asymmetry that identified this as the producer's half.

    The append below stands in for that drop: it lands strictly between
    build-start and the watermark stamp. The post-build reading counted it; the
    pre-build reading must not.
    """

    import pytest
    from hermes_time import now

    from agent_runtime import snapshot as snapshot_mod
    from agent_runtime.events import EventLog
    from agent_runtime.models import Event
    from agent_runtime.parity import events_position

    log = EventLog()
    log.append(Event(ts=now(), type="persona_instance.created", task_id=None, run_id=None, persona_id="qa"))
    before = events_position()["event_offset"]
    assert before is not None and before > 0

    appended: dict[str, int] = {}

    # SCOPED, never `monkeypatch.undo()` — that drops the SHARED stack, taking
    # tests/conftest.py's autouse root/credential pins with it and running the
    # rest of the body against the operator's live .hermes (the 2026-08-17 EG-0.1
    # leak the conftest witness now catches).
    with pytest.MonkeyPatch.context() as patched:
        original = snapshot_mod._parity_envelope

        def _append_then_stamp(data, **kwargs):
            # Mid-build write: the agent the operator just dropped.
            EventLog().append(
                Event(ts=now(), type="office.actor.upserted", task_id=None, run_id=None, persona_id="qa")
            )
            appended["after"] = events_position()["event_offset"]
            return original(data, **kwargs)

        patched.setattr(snapshot_mod, "_parity_envelope", _append_then_stamp)

        offset = build_snapshot()["parity"]["watermark"]["event_offset"]

    assert appended["after"] > before, "the fixture's mid-build append must move the log"
    assert offset == before, (
        "the parity watermark counted an event appended DURING the build "
        f"(stamped {offset}, log was {before} when the build started, "
        f"{appended['after']} when it ended). A client that folds this core "
        "advances past that event and never replays it."
    )

    # NARROWED, NOT DISABLED. The pre-build capture is still a LIVE reading: a
    # build that starts after that append reports the GROWN offset. A watermark
    # that had merely been pinned (or frozen at a first reading) would satisfy
    # the assertion above and fail this one.
    assert build_snapshot()["parity"]["watermark"]["event_offset"] == appended["after"]
