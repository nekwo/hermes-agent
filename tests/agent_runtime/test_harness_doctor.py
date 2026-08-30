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
        # The census contributes THREE counts, because they are three
        # verdicts: an orphan actor is a defect, an unplaced row is a legal
        # state of a supported door, and a litter desk is authored furniture
        # standing where its agent no longer is.
        "orphan_actors",
        "unplaced_rows",
        "desk_litter",
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
        "desk_litter": 0,
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


# ── the section table: one declaration, four derived rosters (H-H1) ───────────


def _synthetic_section(**overrides):
    from agent_runtime.harness_doctor import DoctorSection

    fields = {
        "name": "synthetic_probe",
        "probe": lambda _context: {
            "health": "defect",
            "error": "synthetic probe error",
            "widgets": [{"id": "one"}, {"id": "two"}],
        },
        "publish": (("findings.synthetic_probe", None),),
        "detail_source": "findings.synthetic_probe",
        "counts": (("synthetic_widgets", "widgets"),),
    }
    fields.update(overrides)
    return DoctorSection(**fields)


def test_one_table_row_is_the_whole_cost_of_a_new_doctor_section(
    isolate_agent_runtime_root, monkeypatch
):
    """A section declared ONCE reaches every roster — the H-H1 guarantee.

    The defect this pins against is not hypothetical: ``placement_census`` was
    added by editing ``finding_counts``, ``section_health`` and ``findings``
    here plus ``detail_sources`` in the CLI printer — four hand-maintained lists
    of one set, of which only ``section_health``'s key set was pinned. A section
    added to three of the four is counted and verdicted while rendering no
    operator line, and nothing fails.

    ANTI-VACUITY: the synthetic section is added to the TABLE and to nothing
    else. Every assertion below is reachable only if that one row fed the
    roster it names, so re-typing any roster by hand reds this test.
    """

    from agent_runtime import harness_doctor

    monkeypatch.setattr(
        harness_doctor,
        "DOCTOR_SECTIONS",
        (*harness_doctor.DOCTOR_SECTIONS, _synthetic_section()),
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    # 1: section_health.  2: finding_counts.  3: the payload placement.
    assert report["summary"]["section_health"]["synthetic_probe"] == "defect"
    assert report["summary"]["finding_counts"]["synthetic_widgets"] == 2
    assert report["findings"]["synthetic_probe"]["error"] == "synthetic probe error"
    # 4: the CLI's detail roster, derived from the same row.
    assert (
        harness_doctor.doctor_detail_sources(report)["synthetic_probe"]["error"]
        == "synthetic probe error"
    )
    # And the derived verdict spends it like any other section.
    assert "synthetic_probe" in report["summary"]["defective_sections"]
    assert report["summary"]["needs_fix"] is True
    assert report["ok"] is False


def test_an_unexamined_section_counts_none_from_the_same_table_row(
    isolate_agent_runtime_root, monkeypatch
):
    """The None-not-zero rule is applied ONCE, for every count in the table.

    It used to be re-typed per count — six copies of one rule, each free to be
    forgotten on the seventh. A synthetic section whose probe reports
    ``unknown`` must count ``None`` without anybody having written that arm for
    it.

    KILLING MUTATION: count ``len(...)`` unconditionally and this reds on a
    ``0`` — "looked, found none" — for a class nothing looked at.
    """

    from agent_runtime import harness_doctor

    unknown = _synthetic_section(
        probe=lambda _context: {"health": "unknown", "error": "probe raised", "widgets": None}
    )
    monkeypatch.setattr(
        harness_doctor,
        "DOCTOR_SECTIONS",
        (*harness_doctor.DOCTOR_SECTIONS, unknown),
    )

    report = run_harness_doctor(include_worktrees=False, snapshot_builder=lambda: {})

    assert report["summary"]["finding_counts"]["synthetic_widgets"] is None
    assert "synthetic_probe" in report["summary"]["unexamined_sections"]
    assert report["summary"]["needs_fix"] is False
    assert report["ok"] is False


def test_a_section_may_publish_two_payload_keys_from_one_probe(
    isolate_agent_runtime_root,
):
    """``snapshot_null_id_rows`` and ``snapshot_build``: one probe, two keys.

    The row list and "did the frame build at all" are two different facts and
    stay two payload keys — but ONE section, contributing one health and one
    count. This is the case the table's per-key ``publish`` exists for, so it is
    pinned rather than left as an implementation detail of one entry.
    """

    report = run_harness_doctor(
        include_worktrees=False,
        snapshot_builder=lambda: {"agents": [{"persona_id": None}]},
    )

    assert report["findings"]["snapshot_null_id_rows"] == [
        {"collection": "agents", "index": 0, "id_key": "persona_id"}
    ]
    assert report["findings"]["snapshot_build"] == {"health": "defect", "observed": True}
    assert report["summary"]["finding_counts"]["snapshot_null_id_rows"] == 1
    assert report["summary"]["section_health"]["snapshot_null_id_rows"] == "defect"


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


def test_the_orphan_remediation_names_the_verb_that_works_for_a_pulled_orphan(
    isolate_agent_runtime_root,
):
    """A4 (M13). The remediation said "retiring or re-creating its agent" — and
    for the orphan this census reports most often, retire is the ONE verb that
    cannot work.

    A realm-pulled placement is born orphaned: office actors sync, persona
    instances are per-install by ruling, so the instance never existed on this
    machine and ``agent retire`` refuses ``not_found`` terminally. The refusal is
    correct; prescribing it was not, and the verb that does work
    (``harness office actor-remove``, which needs only the surface and the actor
    FILE) was not named at all.

    The string keys on the FACT that picks between the two repairs — does this
    install hold the instance — never on the id's shape, which is the mistake
    one layer up in the launcher's removal plan.

    AX7: and the form it names is ``--local-only``. A doctor remediation is
    DIAGNOSTIC intent — the operator asked what is wrong with this install's
    projection — so the repair it prescribes must not mint a realm-visible
    tombstone that deletes the placement on every machine in the realm. The
    authored form is named too, as the deliberate opposite.
    """

    _report, census = _census()
    remediation = census["remediation"]

    assert "harness office actor-remove" in remediation, (
        "the doctor still does not name the verb that clears a pulled orphan"
    )
    # The DIAGNOSTIC form, and its opposite said out loud rather than implied.
    assert "--local-only" in remediation, (
        "the doctor prescribes the tombstoning form for a diagnostic repair"
    )
    assert "delete the placement realm-wide" in remediation
    # Retire is still prescribed — for the orphan it CAN clear — and the two
    # arms are told apart by whether this install holds the instance.
    assert "retiring or re-creating its agent" in remediation
    assert "never held" in remediation


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


# ── desk litter: the item-level sweep the join cannot see (plan DL-H1) ────────


def _desk_actor(store, workspace_id: str, item_id: str, *, persona_id: str = "qa"):
    """A CLASS-KEYED desk actor, written through the store's own verb.

    The shape §1 of the plan measures: a desk minted by the launcher's
    ``materializeAgentDesk`` carries the persona CLASS id and no instance
    binding, so it lands in its own class-keyed actor file while the agent it
    belongs to lands in the instance-keyed one. Hand-writing the JSON would pin
    the census against this fixture's idea of a desk rather than the store's.
    """

    return store.upsert_actor(
        workspace_id,
        {
            "persona_id": persona_id,
            "items": [
                {
                    "item_id": item_id,
                    "persona_id": persona_id,
                    "kind": "desk",
                    "position": [1.0, 1.4],
                }
            ],
        },
        updated_by="desk litter fixture",
    )


def test_the_desk_litter_vocabulary_is_closed():
    """The four reasons ARE the vocabulary, and the classifier never invents one.

    Pinned because the buckets are the whole point of the sweep: DL-H2's reap
    branches on them, so a fifth reason appearing without a decision is a write
    verb meeting a value it has no arm for.
    """

    from agent_runtime import harness_doctor as doctor

    assert doctor.DESK_LITTER_REASONS == (
        "agent_missing",
        "agent_scope_stale",
        "persona_retired",
        "desk_kind_agent_binding",
    )
    assert len(set(doctor.DESK_LITTER_REASONS)) == 4
    assert {
        doctor.DESK_LITTER_AGENT_MISSING,
        doctor.DESK_LITTER_AGENT_SCOPE_STALE,
        doctor.DESK_LITTER_PERSONA_RETIRED,
        doctor.DESK_LITTER_DESK_KIND_AGENT_BINDING,
    } == set(doctor.DESK_LITTER_REASONS)


def test_the_item_id_shape_reads_the_launchers_minting_conventions():
    """The desk marker WINS, and an unreadable id is ``unknown``, not "agent".

    Pinned directly because both facts are load-bearing and neither is
    reachable through a store fixture. ``desk-<agentItemId>`` is what
    ``materializeAgentDesk`` mints for EVERY materialized desk on every store,
    and it carries an ``_agent`` tail; reading the tail first would file all of
    them as mis-kinded agents. And the agent test is POSITIVE — over-claiming
    would fold widowed desks into ``desk_kind_agent_binding``, the exact
    conflation the plan's §0 was written to stop.

    KILLING MUTATION: check the tail before the head, or return ``agent`` for
    an id carrying no marker, and this reds.
    """

    from agent_runtime.harness_doctor import _office_item_id_shape

    assert _office_item_id_shape("desk-qa_agent") == "desk"
    assert _office_item_id_shape("desk-personainst_qa_agent_2") == "desk"
    assert _office_item_id_shape("qa_desk") == "desk"
    assert _office_item_id_shape("qa_desk_2") == "desk"
    assert _office_item_id_shape("qa_agent") == "agent"
    assert _office_item_id_shape("qa_agent_2") == "agent"
    assert _office_item_id_shape("personainst_neko_supervisor_agent_2a26ddcc") == "agent"
    assert _office_item_id_shape("") == "unknown"
    assert _office_item_id_shape("a_prop_somebody_named") == "unknown"


def test_a_widowed_class_keyed_desk_is_agent_missing(isolate_agent_runtime_root):
    """The retire seam's own litter: the agent's actor goes, the desk's stays.

    ``OfficeStore.archive_actors_for_instance`` archives only actors BOUND to
    the instance, and the desk lives in the class-keyed actor, which is bound to
    nothing — so every retire that does not go through the launcher's scene
    removal leaves this behind. Invisible to ``orphan_actors``, which skips
    class-keyed actors by construction.

    KILLING MUTATION: skip class-keyed actors in the desk sweep the way the
    join does, and this reds with an empty list.
    """

    workspace = "ws_litter_widowed"
    _qa_persona_saved()
    store = _seed_office(workspace)

    agent = _create(workspace, "qa_widow_agent_2")
    _desk_actor(store, workspace, f"desk-{agent['persona_instance_id']}")
    store.remove_actor(workspace, agent["actor_key"], reason="retire seam")

    report, census = _census()

    from agent_runtime.harness_doctor import DESK_LITTER_REASONS

    assert [row["reason"] for row in census["desk_litter"]] == ["agent_missing"]
    assert {row["reason"] for row in census["desk_litter"]} <= set(DESK_LITTER_REASONS)
    assert census["desk_litter"][0]["item_id"] == f"desk-{agent['persona_instance_id']}"
    assert census["desk_litter"][0]["persona_id"] == "qa"
    # A class-keyed actor carries no binding, and the row says so rather than
    # inventing one.
    assert census["desk_litter"][0]["persona_instance_id"] is None
    assert census["workspaces"][workspace]["desk_litter"] == census["desk_litter"]
    assert report["summary"]["finding_counts"]["desk_litter"] == 1
    # Never a DEFECT: the desk renders as a desk. Promoting litter would turn
    # ``needs_fix`` on for a store whose only fault is furniture. The
    # ``notice`` half is pinned on the mis-kinded fixture below, where litter
    # is the sole cause — here the archived agent actor also leaves an unplaced
    # row, so a ``notice`` assertion would pass with the litter term removed.
    assert report["summary"]["needs_fix"] is False
    assert report["summary"]["section_health"]["placement_census"] != "defect"


def test_a_desk_beside_a_dead_instance_is_agent_scope_stale(isolate_agent_runtime_root):
    """The store-side shadow of the launcher's projection scope drop.

    The agent item is still there; every one of them is bound to an instance no
    live roster row backs, so the launcher's scope policy drops the character
    node and the desk renders on alone. The overlap with ``orphan_actors`` is
    DELIBERATE and asserted here: that row names the actor, this one names the
    desk left standing — two pointers to one fault, not a duplicate.

    KILLING MUTATION: treat "an agent item exists" as healthy without testing
    its binding, and this reds with an empty list.
    """

    from agent_runtime import paths

    workspace = "ws_litter_stale"
    _qa_persona_saved()
    store = _seed_office(workspace)

    agent = _create(workspace, "qa_stale_agent_2")
    _desk_actor(store, workspace, f"desk-{agent['persona_instance_id']}")
    paths.persona_instance_path(agent["persona_instance_id"]).unlink()

    _report, census = _census()

    assert [row["reason"] for row in census["desk_litter"]] == ["agent_scope_stale"]
    assert [row["persona_instance_id"] for row in census["orphan_actors"]] == [
        agent["persona_instance_id"]
    ]
    # The agent item IS present — otherwise this is indistinguishable from the
    # widowed case above and proves nothing about the binding test.
    assert store.get_actor(workspace, agent["actor_key"]).items[0].kind == "agent"


def test_a_mis_kinded_agent_item_is_never_reported_as_a_widowed_desk(
    isolate_agent_runtime_root,
):
    """Cause 1, kept out of ``agent_missing`` — the confusion the lane paid for.

    Two rows, one per clause, so neither can carry the other. They sit in
    separate workspaces because the store allows a persona ONE live desk per
    level (``DuplicateDeskRefused``) — the fence is per workspace, and a fixture
    that fought it would be authoring a shape the store refuses:

    * the LIVE INSTANCE BINDING clause — a ``kind: "desk"`` item riding an
      actor whose ``persona_instance_id`` names a live roster row. This is the
      shape measured on the operator's store on 2026-08-30. Its id is
      desk-shaped, so only the binding clause can fire.
    * the AGENT-SHAPED ID clause — a ``kind: "desk"`` item on a class-keyed
      actor whose ``item_id`` is an instance id, i.e. what
      ``agent_create.placement_actor_payload`` mints for an AGENT.

    KILLING MUTATIONS: drop the binding clause and the first row vanishes (its
    persona has a live agent item, so it reads healthy); drop the id clause and
    the second vanishes for the same reason. Reorder the classifier so the
    absence buckets are tested first and both come back as ``agent_missing`` —
    the misreport that sends an operator to reap an agent.
    """

    bound_ws = "ws_litter_miskinded_binding"
    id_ws = "ws_litter_miskinded_id"
    _qa_persona_saved()
    store = _seed_office(bound_ws)
    _seed_office(id_ws)

    agent = _create(bound_ws, "qa_live_agent_2")
    instance_id = agent["persona_instance_id"]
    # The agent's OWN actor, re-written to carry its agent item plus a desk
    # item — the mixed-actor shape §1 says older stores hold.
    store.upsert_actor(
        bound_ws,
        {
            "persona_id": "qa",
            "persona_instance_id": instance_id,
            "items": [
                {
                    "item_id": instance_id,
                    "persona_id": "qa",
                    "kind": "agent",
                    "position": [0.0, 0.0],
                },
                {
                    "item_id": "desk-qa_agent",
                    "persona_id": "qa",
                    "kind": "desk",
                    "position": [0.0, 1.4],
                },
            ],
        },
        updated_by="desk litter fixture",
    )

    # Second workspace: a LIVE agent placement beside a class-keyed desk whose
    # item id is an agent's. Without the id clause this row reads healthy.
    other = _create(id_ws, "qa_other_agent_2")
    _desk_actor(store, id_ws, "personainst_qa_ghost_agent_9")

    report, census = _census()

    assert {row["reason"] for row in census["desk_litter"]} == {
        "desk_kind_agent_binding"
    }
    assert {row["item_id"] for row in census["desk_litter"]} == {
        "desk-qa_agent",
        "personainst_qa_ghost_agent_9",
    }
    by_item = {row["item_id"]: row for row in census["desk_litter"]}
    assert by_item["desk-qa_agent"]["persona_instance_id"] == instance_id
    assert by_item["desk-qa_agent"]["workspace_id"] == bound_ws
    assert by_item["personainst_qa_ghost_agent_9"]["persona_instance_id"] is None
    assert by_item["personainst_qa_ghost_agent_9"]["workspace_id"] == id_ws
    # Both personas are alive and placed — so ``agent_missing`` was never the
    # honest answer for either row.
    assert census["placed"] == 2
    assert other["persona_instance_id"] != instance_id
    # THE health pin lives here rather than on the widowed fixture, because
    # this is the only one of the four where litter is the SOLE cause: nothing
    # is orphaned and nothing is unplaced, so ``notice`` can have come from
    # nowhere else. (Measured: on the widowed fixture the archived agent actor
    # leaves a live roster row behind, which is an unplaced row, which raises
    # the same notice — an assertion there proves nothing about litter.)
    assert census["orphan_actors"] == []
    assert census["unplaced_rows"] == []
    assert census["health"] == "notice"
    assert report["summary"]["needs_fix"] is False
    assert report["summary"]["finding_counts"]["desk_litter"] == 2


def test_a_healthy_split_agent_and_desk_pair_reports_nothing(
    isolate_agent_runtime_root,
):
    """The CURRENT common shape, and it must produce no row at all.

    The agent lands in the instance-keyed actor, its desk in the class-keyed
    one (§1) — two files, one persona, nothing wrong. This is the anti-vacuity
    half of the four tests above: a sweep that flagged every desk would pass all
    of them and fail only here.
    """

    workspace = "ws_litter_healthy"
    _qa_persona_saved()
    store = _seed_office(workspace)

    agent = _create(workspace, "qa_healthy_agent_2")
    _desk_actor(store, workspace, f"desk-{agent['persona_instance_id']}")

    report, census = _census()

    assert census["desk_litter"] == []
    assert census["workspaces"][workspace]["desk_litter"] == []
    assert report["summary"]["finding_counts"]["desk_litter"] == 0
    assert census["health"] == "ok"
    # The desk EXISTS — otherwise the empty list above is satisfied by a store
    # with no desks in it and proves nothing.
    assert any(
        item.kind == "desk"
        for actor in store.list_actors(workspace)
        for item in actor.items
    )


def test_one_unreadable_actor_file_makes_the_whole_census_unknown(
    isolate_agent_runtime_root,
):
    """WHOLE-world-or-nothing, and ``desk_litter`` is ``None`` — not ``[]``.

    Every one of the four buckets is a statement about ABSENCE ("no live agent
    item for this persona", "no roster row backs this binding"), and a file that
    will not decode is exactly what absence is indistinguishable from. A sweep
    over the readable remainder would report a perfectly healthy pair as
    widowed, because the file that would not open is the agent's — inventing
    the reap target out of an outage.

    KILLING MUTATION: partition the readable remainder anyway, or seed the key
    with ``[]`` in ``_census_unknown``, and this reds on the count, which would
    read ``0`` — "looked, found none" — for a class nothing looked at.
    """

    from agent_runtime import paths

    workspace = "ws_litter_short"
    _qa_persona_saved()
    store = _seed_office(workspace)

    agent = _create(workspace, "qa_short_agent_2")
    _desk_actor(store, workspace, f"desk-{agent['persona_instance_id']}")
    # The AGENT's file is the one that will not open — the case that would turn
    # a healthy desk into ``agent_missing`` under the mutation.
    paths.office_actor_path(workspace, agent["actor_key"]).write_text(
        "{ this is not json", encoding="utf-8"
    )

    report, census = _census()

    assert census["health"] == "unknown"
    assert census["observed"] is False
    assert census["desk_litter"] is None
    assert report["summary"]["finding_counts"]["desk_litter"] is None
    assert "placement_census" in report["summary"]["unexamined_sections"]
