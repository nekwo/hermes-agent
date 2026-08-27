"""``harness persona set-skills`` — the TEMPLATE tier of the skills cascade.

The gap this closes, in the operator's own sentence: *set the skills, place a
new agent from that persona, and it has those skills.* Broken by construction
before this verb — the only skills door (``persona instance update-profile
--skill``) writes ONE instance's ``skill_overrides``, while a placement made
later inherits ``persona.skills``, and nothing an operator could run wrote
``persona.skills``.

Two properties carry the whole module and both are pinned by re-reading the
STORE, never by trusting the command's own reply:

1. **Absent is never a write.** ``--skill`` is ``action="append",
   default=None`` so an omitted flag is distinguishable from ``[]``. At the
   instance tier absent means "inherit"; at THIS tier there is nothing above to
   inherit from, so absent is a typed ``nothing_to_write`` refusal. Collapsing
   it to a write of ``[]`` is the same defect that once cleared every skill of
   every renamed agent (``list(args.skills or [])`` — see
   ``tests/hermes_cli/test_persona_instance_update_profile_skills.py``), and the
   refusal is what keeps a transport-mangled argv from reaching a template.
2. **Inheritance is LIVE, not a copy.** ``apply_instance_model_overrides`` falls
   back to ``list(persona.skills)`` at EVERY resolution for an instance whose
   ``skill_overrides`` is ``None``, so a template write moves existing
   non-overridden instances too — and must leave overridden ones alone.

Every test drives the REAL argparse tree through ``args.func``. A handler poked
directly would not prove that the flag's ``default=None`` and the handler's
reading of it agree, and that agreement is the first property above.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root inside this test's tmp dir, and prove it landed."""

    from agent_runtime import paths

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "write into a runtime root nobody in this repo controls."
    )
    return root


def _seed_persona(persona_id: str = "qa", *, skills: list[str] | None = None):
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id=persona_id,
        display_name=f"{persona_id.upper()} Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
        hermes_profile=persona_id,
        skills=list(skills if skills is not None else ["seeded-skill"]),
    )
    AgentStore().save(persona)
    return persona


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _set_skills(persona_id: str, *flags: str) -> list[str]:
    return ["harness", "persona", "set-skills", persona_id, *flags, "--json"]


def _row_on_disk(persona_id: str = "qa") -> dict:
    """The persona's store row read as BYTES off disk, not through the store.

    The store's own reader would happily answer from a record the save never
    persisted; the file is what the next process — the next placement — will
    actually read.
    """

    from agent_runtime import paths

    return json.loads(paths.agent_path(persona_id).read_text(encoding="utf-8"))


def _events(event_type: str) -> list[dict]:
    from agent_runtime import paths

    path = paths.store_root() / "events.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("type") == event_type]


# --- S1: the refusal matrix ---------------------------------------------------


def test_omitted_skill_flag_is_a_refusal_not_a_clear(capsys):
    """THE pin. KILLING MUTATION ``pts-s1-absent-becomes-clear``: turn the
    ``nothing_to_write`` raise into ``clear = True`` and this reds with

        assert 0 == 2

    on the exit code, and — with that assertion removed — again on the row:

        assert [] == ['seeded-skill']

    ANTI-VACUITY: the probe is the row FILE re-read after the command, so a
    handler that printed the old set while writing ``[]`` still fails.
    """

    _seed_persona(skills=["seeded-skill"])

    code = _dispatch(_set_skills("qa"))
    data = json.loads(capsys.readouterr().out)

    assert code == 2
    assert data["ok"] is False
    assert data["error_code"] == "nothing_to_write"
    assert data["scope"] == "persona_template"
    assert _row_on_disk()["skills"] == ["seeded-skill"]


def test_both_flags_at_once_is_a_conflict_refusal(capsys):
    """``--skill`` and ``--clear-skills`` are two different writes; guessing
    which one the operator meant is how a template loses its set silently."""

    _seed_persona(skills=["seeded-skill"])

    code = _dispatch(_set_skills("qa", "--skill", "alpha", "--clear-skills"))
    data = json.loads(capsys.readouterr().out)

    assert code == 2
    assert data["error_code"] == "conflicting_args"
    assert _row_on_disk()["skills"] == ["seeded-skill"]


def test_a_skill_flag_whose_every_value_is_rejected_is_not_a_clear(capsys):
    """``--skill ''`` reaches the handler as a PRESENT flag whose only value
    token safety drops. Writing the survivors would be an empty set — the very
    clear the absent-flag branch refuses to infer — so it gets the same answer
    rather than a second route to the same damage.
    """

    _seed_persona(skills=["seeded-skill"])

    code = _dispatch(_set_skills("qa", "--skill", "   "))
    data = json.loads(capsys.readouterr().out)

    assert code == 2
    assert data["error_code"] == "invalid_value"
    assert _row_on_disk()["skills"] == ["seeded-skill"]


def test_unknown_persona_is_refused_before_any_write(capsys):
    code = _dispatch(_set_skills("definitely_not_a_persona_xyz", "--skill", "alpha"))
    data = json.loads(capsys.readouterr().out)

    assert code == 2
    assert data["error_code"] == "persona_not_found"


def test_a_config_only_persona_is_refused_not_promoted(monkeypatch, capsys):
    """R1, and the reason it is a refusal rather than a promotion.

    ``ensure_persisted_personas`` merges ``{**catalog, **stored}`` — a store row
    wins WHOLESALE. Minting a row to persist one field would therefore freeze
    every OTHER field of that persona at its write-time value, so a skills edit
    would quietly pin the persona's model, toolsets and budgets too. The verb
    refuses and names the reason; a silent promotion could not.
    """

    from agent_runtime.config import AgentRuntimeConfig
    from hermes_cli import harness

    cfg = AgentRuntimeConfig(
        personas={"catalog_only": {"role": "dev", "display_name": "Catalog Only"}}
    )
    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: cfg)

    code = _dispatch(_set_skills("catalog_only", "--skill", "alpha"))
    data = json.loads(capsys.readouterr().out)

    assert code == 2
    assert data["error_code"] == "persona_not_persisted"
    from agent_runtime import paths

    assert not paths.agent_path("catalog_only").exists(), (
        "the refusal must not have minted the row it refused to write to"
    )


def test_profile_id_targets_the_single_backing_store_row(capsys):
    _seed_persona("qa", skills=["seeded-skill"])

    code = _dispatch(_set_skills("profile:qa", "--skill", "alpha"))
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["applied_to_persona_id"] == "qa"
    assert _row_on_disk()["skills"] == ["alpha"]


def test_an_ambiguous_profile_is_refused_and_names_the_candidates(capsys):
    _seed_persona("qa", skills=["seeded-skill"])
    _seed_persona("qa_twin", skills=["seeded-skill"])
    from agent_runtime.store import AgentStore

    twin = AgentStore().get("qa_twin")
    twin.hermes_profile = "qa"
    AgentStore().save(twin)

    code = _dispatch(_set_skills("profile:qa", "--skill", "alpha"))
    data = json.loads(capsys.readouterr().out)

    assert code == 2
    assert data["error_code"] == "ambiguous_profile_persona"
    assert sorted(data["candidates"]) == ["qa", "qa_twin"]


# --- S1: the write ------------------------------------------------------------


def test_the_write_lands_on_the_store_row_and_stamps_its_own_clock(capsys):
    """KILLING MUTATION ``pts-s1-store-write-dropped``: delete the
    ``store.save(target)`` line and this reds with

        assert ['seeded-skill'] == ['alpha', 'beta']

    ANTI-VACUITY: the assertion reads the row FILE, so an in-memory mutation
    that never reached disk fails exactly where a dropped save does.
    """

    _seed_persona(skills=["seeded-skill"])

    code = _dispatch(_set_skills("qa", "--skill", "alpha", "--skill", "beta"))
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["status"] == "applied"
    assert data["changed"] is True
    assert data["scope"] == "persona_template"
    assert data["persistence"] == "agent_store"
    assert data["skills"] == ["alpha", "beta"]

    row = _row_on_disk()
    assert row["skills"] == ["alpha", "beta"]
    assert row["skills_override_issued_at"], "the template skills clock must advance on a write"


def test_clear_skills_writes_an_empty_set(capsys):
    _seed_persona(skills=["seeded-skill"])

    code = _dispatch(_set_skills("qa", "--clear-skills"))
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["cleared"] is True
    assert data["skills"] == []
    assert _row_on_disk()["skills"] == []


def test_the_write_emits_persona_updated_at_the_store_chokepoint(capsys):
    _seed_persona(skills=["seeded-skill"])
    before = len(_events("persona.updated"))

    assert _dispatch(_set_skills("qa", "--skill", "alpha")) == 0
    capsys.readouterr()

    assert len(_events("persona.updated")) > before


def test_ids_are_deduped_and_capped_at_forty(capsys):
    """The instance tier's ``_safe_skill_overrides`` is IMPORTED, not re-spelled:
    two spellings of one cap is how the create lane's ``MAX_SKILLS`` comment says
    drift starts, and an operator must not be told a template holds 45 skills
    when the instance tier would have kept 40.
    """

    _seed_persona(skills=["seeded-skill"])
    flags: list[str] = ["--skill", "dupe", "--skill", "dupe"]
    for index in range(45):
        flags += ["--skill", f"skill-{index:02d}"]

    code = _dispatch(_set_skills("qa", *flags))
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert len(data["skills"]) == 40
    assert data["skills"][0] == "dupe"
    assert data["skills"].count("dupe") == 1
    assert _row_on_disk()["skills"] == data["skills"]


def test_unresolvable_ids_warn_in_the_ack_and_are_still_written(capsys):
    """R3 — warn, never refuse.

    The instance tier does not refuse unresolvable ids either, placement-time
    strictness already lives in the create verb's skills phase, and readiness
    carries the standing truth. A hard gate here would make a realm-synced
    persona uneditable on any machine that lacks one of its skills.
    """

    _seed_persona(skills=["seeded-skill"])

    code = _dispatch(_set_skills("qa", "--skill", "no-such-skill-anywhere"))
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["unresolved"] == ["no-such-skill-anywhere"]
    assert _row_on_disk()["skills"] == ["no-such-skill-anywhere"], (
        "an unresolved id is a warning, not a rejected write"
    )


# --- S1: the supersede clock --------------------------------------------------


def test_a_stale_issued_at_is_superseded_not_applied(capsys):
    """KILLING MUTATION ``pts-s1-stale-write-applies``: invert the
    ``issued <= applied_at`` comparison and this reds with

        assert 'applied' == 'superseded'

    and, on the row, ``assert ['stale'] == ['fresh']``.
    """

    _seed_persona(skills=["seeded-skill"])
    newer = datetime.now(timezone.utc)
    older = newer - timedelta(seconds=45)

    assert _dispatch(_set_skills("qa", "--skill", "fresh", "--issued-at", newer.isoformat())) == 0
    capsys.readouterr()

    code = _dispatch(_set_skills("qa", "--skill", "stale", "--issued-at", older.isoformat()))
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["status"] == "superseded"
    assert data["changed"] is False
    assert _row_on_disk()["skills"] == ["fresh"]


def test_the_skills_clock_is_independent_of_the_model_clock(capsys):
    """``skills_override_issued_at`` is its OWN field on purpose.

    A shared clock would let a model write from one surface supersede a skills
    write from another — two verbs that touch disjoint fields silently racing
    each other. Proven both directions in one test: a skills write leaves the
    model clock alone, and a model write stamped LATER does not supersede a
    subsequent skills write stamped EARLIER.
    """

    _seed_persona(skills=["seeded-skill"])
    early = datetime.now(timezone.utc) - timedelta(seconds=60)
    late = datetime.now(timezone.utc)

    assert _dispatch([
        "harness", "persona", "set-model", "qa",
        "--model", "model-late", "--issued-at", late.isoformat(), "--json",
    ]) == 0
    capsys.readouterr()

    code = _dispatch(_set_skills("qa", "--skill", "alpha", "--issued-at", early.isoformat()))
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["status"] == "applied", "a model write must not supersede a skills write"
    row = _row_on_disk()
    assert row["skills"] == ["alpha"]
    assert row["model"] == "model-late", "the skills write must not disturb the model lane"


# --- S2: the inheritance proof, end to end -----------------------------------


@pytest.fixture
def placement_surface():
    """A workspace + office surface so ``harness agent create`` can place."""

    from agent_runtime.office_store import OfficeStore
    from agent_runtime.store import WorkspaceStore

    WorkspaceStore().create(name="Probe", workspace_id="ws_pts")
    OfficeStore().ensure_surface("ws_pts", created_by="test")
    return "ws_pts"


def _create_agent(persona_id: str, placement_id: str, *flags: str) -> dict:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = _dispatch([
            "harness", "agent", "create",
            "--persona", persona_id,
            "--workspace", "ws_pts",
            "--display-name", placement_id,
            "--placement-id", placement_id,
            *flags,
            "--json",
        ])
    payload = json.loads(buffer.getvalue())
    assert code == 0, payload
    return payload


def _resolved_skills(instance_id: str, persona_id: str = "qa") -> list[str]:
    """What the runtime will actually run this instance with.

    Read through ``apply_instance_model_overrides`` — the ONE overlay both the
    chat lane and the run lane resolve through — and against the persona record
    re-read from the store, which is what a fresh process would see.
    """

    from agent_runtime.models import apply_instance_model_overrides
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore

    persona = AgentStore().get(persona_id)
    instance = PersonaInstanceStore().get(instance_id)
    return list(apply_instance_model_overrides(persona, instance).skills)


def test_a_new_placement_with_no_skill_flag_carries_the_new_template_set(
    placement_surface, capsys
):
    """The operator's sentence, executed: set-skills, then place, then look.

    The create ack answers ``inherited: True`` with ``assigned: []`` — that is
    the D11 shape and it is deliberately NOT the skill list — so the carried set
    is read where the runtime reads it, through the overlay.
    """

    _seed_persona(skills=["seeded-skill"])
    assert _dispatch(_set_skills("qa", "--skill", "alpha", "--skill", "beta")) == 0
    capsys.readouterr()

    created = _create_agent("qa", "pl_fresh")
    capsys.readouterr()

    assert created["skills"]["inherited"] is True
    assert created["skills"]["assigned"] == []

    instance_id = created["persona_instance_id"]
    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert PersonaInstanceStore().get(instance_id).skill_overrides is None, (
        "an absent --skill must leave the instance INHERITING, not pin a copy"
    )
    assert _resolved_skills(instance_id) == ["alpha", "beta"]


def test_a_pre_existing_non_overridden_instance_follows_the_template_live(
    placement_surface, capsys
):
    """Inheritance is a live read, not a create-time copy.

    This is also the test that would catch a persona record cached across the
    store write: the instance is placed BEFORE the template write and is asked
    again afterwards.
    """

    _seed_persona(skills=["seeded-skill"])
    created = _create_agent("qa", "pl_early")
    capsys.readouterr()
    instance_id = created["persona_instance_id"]
    assert _resolved_skills(instance_id) == ["seeded-skill"]

    assert _dispatch(_set_skills("qa", "--skill", "alpha", "--skill", "beta")) == 0
    capsys.readouterr()

    assert _resolved_skills(instance_id) == ["alpha", "beta"]


def test_an_instance_with_its_own_overrides_is_untouched_by_the_template_write(
    placement_surface, capsys
):
    """The other half of the ack's promise, and the reason the copy is honest.

    The override is written through the INSTANCE-tier verb the launcher's skills
    panel actually submits (``persona instance update-profile --skill``), not by
    poking the dataclass, so the two tiers are exercised against each other
    through their real doors. ``apply_instance_model_overrides`` substitutes the
    instance list WHOLESALE, so the template write must move this agent not at
    all.
    """

    _seed_persona(skills=["seeded-skill"])
    created = _create_agent("qa", "pl_over")
    capsys.readouterr()
    instance_id = created["persona_instance_id"]
    assert created["skills"]["inherited"] is True

    assert _dispatch([
        "harness", "persona", "instance", "update-profile", instance_id,
        "--skill", "seeded-skill", "--json",
    ]) == 0
    capsys.readouterr()

    assert _dispatch(_set_skills("qa", "--skill", "alpha", "--skill", "beta")) == 0
    capsys.readouterr()

    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert PersonaInstanceStore().get(instance_id).skill_overrides == ["seeded-skill"]
    assert _resolved_skills(instance_id) == ["seeded-skill"]


def test_the_placement_lane_and_the_roster_row_both_see_the_write(capsys):
    """Zero read-side changes were needed, and this is why.

    ``ensure_persisted_personas`` is what the placement lane resolves personas
    through, and ``_agent_summary`` is what the snapshot's roster row projects
    from — both read the same store record the verb just wrote, so the write is
    visible to the next placement and the next frame without touching either.
    """

    from agent_runtime.config import ensure_persisted_personas
    from agent_runtime.snapshot import _agent_summary
    from agent_runtime.store import AgentStore

    _seed_persona(skills=["seeded-skill"])
    assert _dispatch(_set_skills("qa", "--skill", "alpha", "--skill", "beta")) == 0
    capsys.readouterr()

    resolved = {persona.id: persona for persona in ensure_persisted_personas()}
    assert resolved["qa"].skills == ["alpha", "beta"]
    assert _agent_summary(AgentStore().get("qa"))["skills"] == ["alpha", "beta"]
