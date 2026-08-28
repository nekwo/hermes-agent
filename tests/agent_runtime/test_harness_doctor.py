from __future__ import annotations

import inspect

import pytest

from agent_runtime.harness_doctor import run_harness_doctor
from agent_runtime.models import AgentPersona


def _persona() -> AgentPersona:
    return AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )


def _write_config(monkeypatch, tmp_path, body: str):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    monkeypatch.setattr("agent_runtime.config.get_config_path", lambda: p)
    return p


def test_harness_doctor_flags_shadowing_model_authority(isolate_agent_runtime_root, tmp_path, monkeypatch):
    _write_config(
        monkeypatch,
        tmp_path,
        "model:\n"
        "  default: gpt-5.6-luna\n"
        "agent_runtime:\n"
        "  default_model: gpt-5.5\n"
        "  personas:\n"
        "    neko_supervisor:\n"
        "      model: gpt-5.5\n"
        "    pm:\n"
        "      model: gpt-5.3-codex-spark\n",
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {"runs": [], "tasks": []})

    authority = report["model_authority"]
    assert authority["available"] is True
    assert authority["divergent"] is True
    assert authority["harness_override"]["model_state"] == "shadowing"
    assert any("shadows the runtime default" in notice for notice in authority["notices"])
    # Informational only — a stale pin never turns the doctor into a fix job.
    assert report["summary"]["needs_fix"] is False


def test_harness_doctor_model_authority_clean_when_only_top_level(isolate_agent_runtime_root, tmp_path, monkeypatch):
    _write_config(monkeypatch, tmp_path, "model:\n  default: gpt-5.6-luna\n")

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {"runs": [], "tasks": []})

    authority = report["model_authority"]
    assert authority["divergent"] is False
    assert authority["harness_override"]["model_state"] == "absent"
    assert authority["notices"] == []
    assert authority["resolved"]["model"] == "gpt-5.6-luna"


def test_harness_doctor_reports_snapshot_null_ids(isolate_agent_runtime_root):
    report = run_harness_doctor(
        include_worktrees=False,
        snapshot_builder=lambda: {"persona_instances": [{"persona_instance_id": None}]},
    )

    counts = report["summary"]["finding_counts"]
    assert counts["snapshot_null_id_rows"] == 1
    assert set(counts) == {
        "orphan_worktrees",
        "snapshot_null_id_rows",
        "misplaced_root_only_keys",
        # The census contributes TWO counts, because an orphan actor is a
        # defect and an unplaced row is a legal state of a supported door.
        "orphan_actors",
        "unplaced_rows",
    }
    assert report["findings"]["snapshot_null_id_rows"] == [
        {"collection": "persona_instances", "index": 0, "id_key": "persona_instance_id"}
    ]
    assert report["mode"] == {"fix": False, "dry_run": False}


def test_harness_doctor_fix_is_idempotent(isolate_agent_runtime_root):
    dry = run_harness_doctor(
        fix=True,
        dry_run=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert dry["repairs"] == {"worktrees_reaped": [], "dry_run": True}

    fixed = run_harness_doctor(
        fix=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert fixed["repairs"] == {"worktrees_reaped": [], "dry_run": False}

    again = run_harness_doctor(
        fix=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )
    assert again["summary"]["finding_counts"] == {
        "orphan_worktrees": 0,
        "snapshot_null_id_rows": 0,
        "misplaced_root_only_keys": 0,
        "orphan_actors": 0,
        "unplaced_rows": 0,
    }


def test_harness_doctor_rejects_the_removed_mission_era_parameters(isolate_agent_runtime_root):
    # The legacy threshold/store kwargs and the compaction switch left with the
    # mission lane (doc 16). The CLI stopped passing them in 126976088; the
    # library surface follows.
    for kwarg in (
        "stale_run_hours",
        "stale_worker_hours",
        "stale_task_days",
        "stale_incident_hours",
        "stale_incident_days",
        "compact_events",
        "task_store",
        "run_store",
        "worker_store",
        "incident_store",
    ):
        with pytest.raises(TypeError):
            run_harness_doctor(
                include_worktrees=False,
                snapshot_builder=lambda: {},
                **{kwarg: 1},
            )
    params = set(inspect.signature(run_harness_doctor).parameters)
    assert params == {
        "fix",
        "dry_run",
        "worktree_min_age_seconds",
        "include_worktrees",
        "event_log",
        "snapshot_builder",
    }


def _diverged_binding() -> "object":
    from agent_runtime.persona_profile_binding import EffectiveBinding

    return EffectiveBinding(
        persona_id="dev",
        config_profile="alice",
        store_profile="bob",
        config_declared=True,
        store_row_present=True,
        effective_profile="bob",
        source="store_wins",
        diverged=True,
    )


def test_harness_doctor_verdict_spans_the_persona_binding_section(
    isolate_agent_runtime_root, monkeypatch
):
    """A diverged binding is a finding, so it must move the verdict.

    ``needs_fix`` was derived from two of the five sections, so a report whose
    ``persona_binding`` block carried divergences (and the remediation command
    for them) still announced ``ok: true, needs_fix: false`` — the triage tool
    telling an operator to stop looking.
    """

    monkeypatch.setattr(
        "agent_runtime.persona_profile_binding.binding_index",
        lambda *_a, **_k: {"dev": _diverged_binding()},
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    assert report["persona_binding"]["diverged_count"] == 1
    assert report["persona_binding"]["health"] == "defect"
    assert report["summary"]["section_health"]["persona_binding"] == "defect"
    assert report["summary"]["defective_sections"] == ["persona_binding"]
    assert report["summary"]["needs_fix"] is True
    assert report["ok"] is False


def test_harness_doctor_reports_an_unexamined_section_instead_of_an_all_clear(
    isolate_agent_runtime_root, monkeypatch
):
    """A section whose probe RAISED clears ``ok`` without inventing a defect.

    The event log is read through ``stat`` on the live slice and the rotation
    manifest, which on this runtime's platform can fail under AV/share-violation
    contention. That must read as "not examined", not as a clean run.
    """

    def _boom():
        raise OSError(13, "share violation")

    monkeypatch.setattr("agent_runtime.harness_doctor.event_log_health", _boom)

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    assert report["findings"]["event_log"]["health"] == "unknown"
    assert "share violation" in report["findings"]["event_log"]["error"]
    assert report["summary"]["unexamined_sections"] == ["event_log"]
    # Unknown is not a defect: nothing to fix, but nothing to clear either.
    assert report["summary"]["needs_fix"] is False
    assert report["ok"] is False


def test_harness_doctor_model_authority_error_is_unknown_not_ok(
    isolate_agent_runtime_root, monkeypatch
):
    monkeypatch.setattr(
        "agent_runtime.config.describe_runtime_default_authority",
        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("config.yaml is not a mapping")),
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    assert report["model_authority"]["available"] is False
    assert report["model_authority"]["health"] == "unknown"
    assert "model_authority" in report["summary"]["unexamined_sections"]
    assert report["ok"] is False


def test_harness_doctor_snapshot_crash_is_not_counted_as_a_null_id_row(
    isolate_agent_runtime_root,
):
    """A build CRASH must not be reported as an observation of null-id rows.

    It used to be returned as one ``snapshot_null_id_rows`` defect, so the
    counter named a defect class nobody looked for and sent the investigator
    hunting null ids in a frame that never built.
    """

    def _builder():
        raise RuntimeError("snapshot build exploded")

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=_builder)

    assert report["findings"]["snapshot_null_id_rows"] == []
    # ``None`` — the class was not observed. A ``0`` here would be the same lie
    # in the other direction.
    assert report["summary"]["finding_counts"]["snapshot_null_id_rows"] is None
    assert report["findings"]["snapshot_build"]["health"] == "unknown"
    assert report["findings"]["snapshot_build"]["observed"] is False
    assert "snapshot build exploded" in report["findings"]["snapshot_build"]["error"]
    assert report["summary"]["needs_fix"] is False
    assert report["ok"] is False


def test_harness_doctor_clean_runtime_still_reads_ok(isolate_agent_runtime_root):
    """The derived verdict must not become permanently pessimistic."""

    report = run_harness_doctor(
        include_worktrees=False,
        snapshot_builder=lambda: {"agents": [{"persona_id": "dev"}]},
    )

    assert report["summary"]["section_health"] == {
        "orphan_worktrees": "ok",
        "snapshot_null_id_rows": "ok",
        "event_log": "ok",
        "model_authority": "ok",
        "persona_binding": "ok",
        "root_config_misplacement": "ok",
        "placement_census": "ok",
    }
    assert report["summary"]["needs_fix"] is False
    assert report["ok"] is True


def test_harness_doctor_thresholds_and_findings_carry_no_mission_rows(isolate_agent_runtime_root):
    report = run_harness_doctor(
        fix=True,
        dry_run=True,
        include_worktrees=False,
        snapshot_builder=lambda: {},
    )

    assert set(report["thresholds"]) == {"worktree_min_age_seconds", "include_worktrees"}
    assert "event_log_compaction" not in report["findings"]
    assert "stale_incidents" not in report["summary"]["finding_counts"]
    assert "closed_incident_ids" not in report["repairs"]


# ── the roster/office placement census (plan D8) ──────────────────────────────


def _qa_persona_saved():
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


def _create(workspace_id: str, placement_id: str):
    """One REAL placement through the create service, not a hand-built pair.

    Deliberate: a census seeded by writing the two stores by hand would pin the
    census against the fixture's idea of how a placement looks, and the
    2026-08-24 incident that produced this plan was exactly a hand-assembled
    pair behaving unlike the verb's. This goes through the same function both
    doors call.
    """

    from agent_runtime.agent_create import perform_agent_create

    outcome = perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": workspace_id,
            "position": [0.0, 0.0],
            "idempotency_key": f"census-{placement_id}",
            "placement_id": placement_id,
        }
    )
    assert outcome.refusal is None, outcome.refusal
    return outcome.result


def _seed_office(workspace_id: str):
    from agent_runtime.office_store import OfficeStore
    from tests.agent_runtime.office_seed import seed_workspace_record

    seed_workspace_record(workspace_id)
    store = OfficeStore()
    store.ensure_surface(workspace_id, created_by="seed")
    return store


def _census(**kwargs):
    report = run_harness_doctor(
        include_worktrees=False,
        snapshot_builder=lambda: {},
        **kwargs,
    )
    return report, report["findings"]["placement_census"]


def test_placement_census(isolate_agent_runtime_root):
    """One of each shape, in one workspace, read back off the real stores.

    ANTI-VACUITY. Every row asserted here is reachable only if the census
    actually opened BOTH stores: ``placed`` needs the actor and the roster row
    to be joined, the orphan needs an actor whose row was removed *after* the
    actor was written, and the unplaced row needs a row whose actor was
    archived. A census that read one store and guessed cannot produce this
    partition — it can only produce all-placed or all-orphan.
    """

    from agent_runtime import paths

    workspace = "ws_census"
    _qa_persona_saved()
    store = _seed_office(workspace)

    placed = _create(workspace, "qa_placed_agent_2")

    # An orphan actor: the ROW goes, the actor survives. This is the field shape
    # the plan names — a retire whose best-effort office half was swallowed, or
    # a compensation that archived the row and not the desk.
    orphan = _create(workspace, "qa_orphan_agent_2")
    paths.persona_instance_path(orphan["persona_instance_id"]).unlink()

    # An unplaced row: the ACTOR goes (archived, as removals always are) and the
    # row stays live. Legal, and what the roster-only recovery door mints.
    unplaced = _create(workspace, "qa_unplaced_agent_2")
    store.remove_actor(workspace, unplaced["actor_key"], reason="census fixture")

    report, census = _census()

    assert census["observed"] is True
    assert census["placed"] == 1
    assert [row["actor_key"] for row in census["placed_actors"]] == [
        placed["actor_key"]
    ]
    assert [row["persona_instance_id"] for row in census["orphan_actors"]] == [
        orphan["persona_instance_id"]
    ]
    assert [row["persona_instance_id"] for row in census["unplaced_rows"]] == [
        unplaced["persona_instance_id"]
    ]
    # Per workspace, not just in aggregate (D8).
    assert census["workspaces"][workspace]["placed"] == 1
    assert len(census["workspaces"][workspace]["orphan_actors"]) == 1
    assert len(census["workspaces"][workspace]["unplaced_rows"]) == 1

    # An orphan actor is a DEFECT and must move the verdict; the doctor is the
    # triage tool, so a half-state it can see must not report ``ok``.
    assert census["health"] == "defect"
    assert report["summary"]["section_health"]["placement_census"] == "defect"
    assert report["summary"]["needs_fix"] is True
    assert report["ok"] is False
    assert report["summary"]["finding_counts"]["orphan_actors"] == 1
    assert report["summary"]["finding_counts"]["unplaced_rows"] == 1


def test_the_census_repairs_nothing(isolate_agent_runtime_root):
    """Read-only, including under ``--fix``.

    ``harness doctor --fix`` reaps worktrees. The census must not acquire a
    repair by riding that flag: both of its remediations are deliberate operator
    gestures against two stores the doctor sees one snapshot of.
    """

    from agent_runtime import paths

    workspace = "ws_census_readonly"
    _qa_persona_saved()
    _seed_office(workspace)
    orphan = _create(workspace, "qa_readonly_agent_2")
    paths.persona_instance_path(orphan["persona_instance_id"]).unlink()
    actor_path = paths.office_actor_path(workspace, orphan["actor_key"])
    before = actor_path.read_bytes()

    _report, census = _census(fix=True)

    assert len(census["orphan_actors"]) == 1
    assert actor_path.exists()
    assert actor_path.read_bytes() == before


def test_the_canonical_operator_channel_is_never_an_unplaced_row(
    isolate_agent_runtime_root,
):
    """``is_canonical_persona_channel`` is the discriminator, and it is load-bearing.

    A persona's global operator channel holds no placement and never did.
    Counting it would put one permanent "unplaced" row per persona on every
    healthy runtime — a finding no operator can clear, which is how a census
    stops being read at all.

    KILLING MUTATION: drop the ``is_canonical_persona_channel`` guard and this
    reds with the canonical row listed.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore

    persona = _qa_persona_saved()
    _seed_office("ws_census_canonical")
    canonical = PersonaInstanceStore().ensure_for_persona(persona)

    _report, census = _census()

    assert census["unplaced_rows"] == []
    assert census["health"] == "ok"
    # The row EXISTS — otherwise the assertion above is satisfied by an empty
    # roster and proves nothing about the discriminator.
    assert PersonaInstanceStore().get(canonical.id).id == canonical.id


def test_the_census_is_unknown_when_the_office_is_unreadable(
    isolate_agent_runtime_root,
):
    """Unknown, never ok — the load-bearing arm.

    A census that answered ``ok`` here would be the doctor's worst shape: the
    triage tool telling an operator to stop looking, on a store it never opened.

    KILLING MUTATION: report ``ok`` (or an empty ``[]``) on an unreadable office
    and this reds on the health, on ``report["ok"]``, and on the two counts,
    which are ``None`` — "not observed" — rather than ``0``.
    """

    from agent_runtime import paths

    office_root = paths.office_root()
    office_root.parent.mkdir(parents=True, exist_ok=True)
    # A FILE where the office directory belongs: ``list_workspaces`` finds it
    # present and then cannot enumerate it, which is the real shape of "the
    # store is there and will not open".
    office_root.write_text("not a directory", encoding="utf-8")

    report, census = _census()

    assert census["health"] == "unknown"
    assert census["observed"] is False
    assert census["placed"] is None
    assert census["unplaced_rows"] is None
    assert census["orphan_actors"] is None
    assert census["error"]
    assert report["summary"]["section_health"]["placement_census"] == "unknown"
    assert report["summary"]["finding_counts"]["orphan_actors"] is None
    assert report["summary"]["finding_counts"]["unplaced_rows"] is None
    assert "placement_census" in report["summary"]["unexamined_sections"]
    assert report["ok"] is False
    # UNKNOWN is not a defect: nothing was observed, so nothing is claimed.
    assert report["summary"]["needs_fix"] is False


def test_a_short_world_is_unknown_rather_than_a_fabricated_orphan(
    isolate_agent_runtime_root,
):
    """An undecodable ROSTER row must not turn its live actor into an orphan.

    This is the subtler half of the rule, and the one a naive implementation
    gets wrong: ``scan_all`` returns the rows it could read and a count of the
    ones it could not, so a census that reads only the list computes a complete
    answer over a short world and reports a perfectly healthy placement as
    orphaned — inventing a defect out of an outage.
    """

    from agent_runtime import paths

    workspace = "ws_census_short"
    _qa_persona_saved()
    _seed_office(workspace)
    row = _create(workspace, "qa_short_agent_2")
    paths.persona_instance_path(row["persona_instance_id"]).write_text(
        "{ this is not json", encoding="utf-8"
    )

    _report, census = _census()

    assert census["health"] == "unknown"
    assert census["observed"] is False
    assert "persona_instances:1" in (census.get("unreadable") or [])
    # The point: NO partition is computed at all. Under the mutation this test
    # exists for — partition the readable remainder anyway — ``orphan_actors``
    # would be a one-row list naming a placement that is entirely healthy.
    assert census["orphan_actors"] is None
    assert census["placed"] is None
