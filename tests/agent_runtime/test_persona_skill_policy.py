from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.models import AgentPersona
from agent_runtime.personas import effective_toolsets, validate_toolsets
from tests.agent_runtime.persona_samples import sample_personas


def _persona(**overrides) -> AgentPersona:
    data = {
        "id": "dev",
        "display_name": "Dev",
        "role": "dev",
        "model": None,
        "provider": None,
        "api_mode": "codex_responses",
        "toolsets": ["file", "search", "terminal", "skills"],
        "system_prompt_path": "personas/dev/system.md",
        "skills": ["aaa-feature-delivery", "test-driven-development"],
    }
    data.update(overrides)
    return AgentPersona(**data)


def test_dev_role_allows_skills_toolset_for_alice_style_loading():
    assert "skills" in validate_toolsets(["file", "skills", "cronjob"])
    assert "skills" in effective_toolsets(_persona())


def test_harness_personas_expose_mission_dev_and_qa_skills():
    personas = {persona.id: persona for persona in sample_personas()}

    assert "harness-runtime-model" in personas["neko_supervisor"].skills
    assert "harness-dev-delivery" in personas["dev"].skills
    assert "harness-runtime-model" in personas["dev"].skills
    assert "harness-qa-verdict" in personas["dev"].skills
    assert "harness-dev-delivery" in personas["backend_dev"].skills
    assert "harness-runtime-model" in personas["backend_dev"].skills
    assert "launcher-mcp-operations" not in personas["backend_dev"].skills
    assert "harness-qa-verdict" in personas["qa"].skills




def test_harness_install_uses_persona_declared_skills_not_role_map():
    from agent_runtime.skill_install import harness_required_skills_for_persona

    dev_without_harness = _persona(id="dev", skills=["aaa-feature-delivery"])
    dev_with_harness = _persona(id="dev", skills=["aaa-feature-delivery", "harness-dev-delivery"])

    assert harness_required_skills_for_persona(dev_without_harness) == []
    assert harness_required_skills_for_persona(dev_with_harness) == ["harness-dev-delivery"]


def test_stage59_hud_skill_sections_exist_in_role_skills():
    root = Path(__file__).resolve().parents[2] / "docs" / "agent-runtime-harness" / "harness-skills"
    expected = {
        "harness-runtime-model": {"Delegation — helpers without context bloat"},
        "harness-dev-delivery": {"Hand Off", "Request Proof Recipe", "Request Context", "Stage Plan", "Report Blocker"},
        "harness-qa-verdict": {"QA Verdict", "Request Missing Proof", "Report Blocker"},
    }

    for skill_id, sections in expected.items():
        text = (root / skill_id / "SKILL.md").read_text(encoding="utf-8")
        for section in sections:
            assert f"## {section}" in text
        assert "decision_menu[].shape_id" not in text
        assert "primary_worker_action" not in text
        assert "next_required_move" not in text


def test_runtime_model_skill_documents_graph_and_level_agent_commands():
    root = Path(__file__).resolve().parents[2] / "docs" / "agent-runtime-harness" / "harness-skills"
    text = (root / "harness-runtime-model" / "SKILL.md").read_text(encoding="utf-8")

    assert "hermes harness task show <id> --json" in text
    assert "`.mission_plan`" in text
    assert "mcp_launcher_qa_get_buttons" in text
    assert "scope=mission_control.agent" in text
    assert "mcp_launcher_qa_get_widget_state" in text
    assert "widget=mission_control.graph" in text
    assert "status.agents" in text
    assert "configured/installed Harness agents" in text
    assert "Neko scope → Backend Dev → Launcher Dev" in text
    assert "QA is a node only if the selected blueprint binds it" in text


def _charsheet_skill_text() -> str:
    root = Path(__file__).resolve().parents[2] / "docs" / "agent-runtime-harness" / "harness-skills"
    return (root / "harness-charsheet-authoring" / "SKILL.md").read_text(encoding="utf-8")


def _charsheet_skill_package_text() -> str:
    """The head PLUS every reference — the shape b981b8d87a left the skill in.

    The 2026-08-28 restructure compressed SKILL.md 51.9k -> 19.7k chars and
    moved the deep teachings into six ``references/*.md`` files, each with a
    when-to-open pointer in the head. A teaching relocated there is still
    taught; a test sweeping only the head reds against a package that is
    perfectly correct. FIELD-NOTES.md is deliberately NOT here: it is the dated
    historical record (the head's turn-zero card bans re-reading it), so it
    legitimately quotes retired spellings the negative pins below must ban from
    the taught surface.
    """
    root = Path(__file__).resolve().parents[2] / "docs" / "agent-runtime-harness" / "harness-skills"
    package = root / "harness-charsheet-authoring"
    parts = [(package / "SKILL.md").read_text(encoding="utf-8")]
    for ref in sorted((package / "references").glob("*.md")):
        parts.append(ref.read_text(encoding="utf-8"))
    return "\n".join(parts)


def _live_characters_verbs() -> set[str]:
    """The `harness characters` verbs argparse ACTUALLY registers, right now."""
    import argparse

    from hermes_cli.harness import build_parser

    def choices(parser):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action.choices
        raise AssertionError(f"no subparsers on {parser.prog!r}")

    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="command"))
    return set(choices(choices(choices(root)["harness"])["characters"]))


def test_charsheet_skill_is_a_canonical_skill_the_authoring_persona_preloads():
    from hermes_constants import CANONICAL_SHARED_SKILL_IDS

    assert "harness-charsheet-authoring" in CANONICAL_SHARED_SKILL_IDS

    text = _charsheet_skill_text()
    assert "load_policy: required_preload" in text
    assert "name: harness-charsheet-authoring" in text


_CHARSHEET_VERB_TABLE_HEADER = "| Verb | What it does | Stage it needs |"


def _charsheet_verb_table() -> str:
    """Just the verb table — the rest of the skill is not a verb list.

    Scoped deliberately. This used to sweep the WHOLE file for ``^| `token``,
    which meant any second markdown table whose first cell opened with a
    backticked lowercase word would join the documented verb set and fail this
    test against a file that is perfectly correct. The table is addressed by its
    header, and the header's absence is its own failure rather than a silently
    empty set.
    """
    text = _charsheet_skill_text()
    assert _CHARSHEET_VERB_TABLE_HEADER in text, (
        "the charsheet skill's verb table header moved; this test addresses it by name"
    )
    return text.split(_CHARSHEET_VERB_TABLE_HEADER, 1)[1].split("\n\n", 1)[0]


def test_charsheet_skill_documents_exactly_the_characters_verbs_hermes_has():
    """The skill's verb table is pinned to the live parser tree.

    A skill that teaches a stale verb surface is worse than no skill — agents
    trust it. So the table cannot drift in either direction: a verb hermes grows
    (``add-state``) and a verb the skill invents both fail here.
    """
    import re

    documented = {
        match.group(1)
        for match in re.finditer(r"^\| `([a-z][a-z-]*)", _charsheet_verb_table(), re.MULTILINE)
    }

    assert documented == _live_characters_verbs()


def test_charsheet_skill_teaches_the_looking_procedure_not_just_the_verbs():
    text = _charsheet_skill_package_text()

    # The looking procedure lives in references/looking-procedure.md since the
    # b981b8d87a restructure; a reference nothing points to is unreachable, so
    # the head must carry the pointer.
    assert "references/looking-procedure.md" in _charsheet_skill_text()
    # The three field findings the verb list cannot carry: crop one FRAME, read
    # attempts side by side, and never trust an automated seam scan as a gate.
    assert "`--frame 0` is a default, not an answer" in text
    assert "attempt N beside attempt N−1" in text
    assert "Do not build a pass/fail scanner" in text
    # The two lines the console parses, and the fence that un-declares them.
    assert "`MEDIA:<absolute path>`" in text
    assert "`CHARSHEET-QA:{json}`" in text
    # A restricted session is not a broken feature.
    assert "chat_lane_restore_toolsets" in text


def test_charsheet_skill_teaches_all_three_environment_traps():
    """Plan §F.7's three traps, not two of them.

    Trap 2 shipped missing. It is the one that fires immediately AFTER the
    operator does the right thing about trap 1: the plan upgrade invalidates the
    stored token, the ``image_gen`` probe comes back ``401 token_expired`` with a
    local expiry still hours out, and the skill's only scripted answer was
    "report the image provider is unavailable in this home" — which sends the
    operator to check ``auth.json`` placement, correct for trap 1 and wrong here.
    """
    text = _charsheet_skill_package_text()

    # The traps live in references/homes-and-migration.md since b981b8d87a;
    # the head must still point there or they are unreachable.
    assert "references/homes-and-migration.md" in _charsheet_skill_text()
    # 1 — the plan-gated account that fails politely at HTTP 200.
    assert "`image_generation` tool silently stripped" in text
    # 2 — the stale token a plan change leaves behind.
    assert "`401 token_expired`" in text
    assert "STALE STORED TOKEN, not" in text
    assert "Force a refresh and" in text
    # 3 — HERMES_HOME is not one value.
    assert "A relative `HERMES_HOME` resolves against the shell's cwd" in text


def test_charsheet_skill_teaches_one_install_wide_library_and_no_home_scoping():
    """The reversal, pinned at the hermes end of a two-repo contract.

    The owner's 2026-08-27 ruling head-homed creation and reads to ONE location
    — ``<hermes_root>/shared/characters`` (§13.27) — whatever persona or profile
    runs the turn. Three of this skill's teachings died with the scoping they
    described, and each one is a live failure mode if it survives in the copy an
    agent preloads:

    * *"which home can see that draft"* was the question the whole preflight was
      shaped around. There is no such question now: every draft on the install
      is in every list. An agent still asking it reports a draft "not in my home"
      that is sitting right in front of it — or, worse, authors a second copy.
    * The launcher's ``CharaDraftBinding.home`` lost its production writer AND
      its policy reader in the same wave (§13.27 re-deriving §13.24/§13.25): the
      adopt door mints unknown, nothing reads a stored one, and the values that
      remain are LEGACY sightings preserved rather than deleted. A skill still
      teaching "an operator opening the adopt door stamps a home" describes a
      writer that no longer exists.
    * The resume seed's ``last observed home:`` line RETIRED with the sighting it
      quoted. The seed spelling is a contract across two repos — the launcher
      pins it in
      ``test/features/mission_control/state/mission_character_resume_seed_test.dart``
      and this is the other end — so a skill quoting a line the seed no longer
      composes is teaching an agent to expect a message it will never receive.

    What SURVIVES is the closing sentence, and the reason it survives is the
    interesting half: *"Echo the home you resolve; do not assume it."* was
    written as a scoping check and outlived its scoping. Under one library a
    wrong profile is harmless and a wrong ROOT is a different install — so the
    echo stopped being how an agent proves it can see a draft and became how a
    mis-resolved root gets surfaced instead of assumed. Pinning it positively is
    what stops this test being passable by deletion.

    Scope note (2026-08-30): swept over the PACKAGE (head + references), not
    the head alone — b981b8d87a moved the home teachings into
    ``references/homes-and-migration.md`` / ``console-and-costs.md``, and the
    bans are stronger package-wide anyway: a reference still teaching the
    retired mint would mislead exactly the agent the head sent there.
    """
    text = _charsheet_skill_package_text()
    lowered = text.lower()

    # Still banned, and now for a second reason on top of the first: no field
    # anywhere records the home an authoring turn resolved onto a binding, and
    # under one library there is nothing for such a field to have been for.
    assert "authoring home" not in lowered, (
        "the skill still says 'authoring home' about a launcher field that has "
        "never held one and, since §13.27, has no reader left either"
    )
    # The retired seed line, in both its spellings. A skill that still quotes it
    # tells an agent to read a line the launcher stopped composing.
    assert "last observed home:" not in lowered, (
        "the skill still quotes the resume seed's retired `last observed home:` "
        "line; §13.27 removed it and the seed now carries draft id and name only"
    )
    assert "never observed by the launcher" not in lowered
    # And no live MINT: the adopt door stopped writing sightings.
    assert "observed the draft readable in" not in lowered, (
        "the skill still teaches the adopt door's observed-home mint, which "
        "retired with the one-home scoping it existed to serve"
    )

    # Stated positively, so deleting the sentences is not a way to pass.
    assert "install-wide" in lowered
    assert "`<hermes_root>/shared/characters`" in text
    assert "§13.27" in text
    # §13.22's READER half stands and must still be taught: the `CHARSHEET-QA:`
    # line carries no home and is not to grow one — which under one library is
    # not a withholding any more, there is simply nothing to carry.
    assert "§13.22" in text
    # A stored observed home is legacy provenance: preserved, labelled, never
    # read. An agent that thinks it is live chases a path nothing maintains.
    assert "legacy" in lowered
    # The seed's closing sentence, which outlived the scoping it was written for
    # and is now the only thing that surfaces a mis-resolved ROOT.
    assert "Echo the home you resolve; do not assume it." in text
    # `hermes_home` on a draft is the run's provenance, not an address (§13.26
    # as re-derived by §13.27). This is the trap that replaced the old one: the
    # field still exists, still carries a path, and now names a home the draft
    # is NOT under.
    assert "provenance, not an address" in lowered


def _charsheet_qa_line_contract() -> dict:
    """The fixture risk D.5 names, hermes-side."""
    import json

    path = Path(__file__).resolve().parents[1] / "fixtures" / "charsheet_qa_line.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_charsheet_qa_line_fixture_pins_the_key_set_the_skill_promises():
    """The producer half of risk D.5's cross-repo pin.

    The ``CHARSHEET-QA:`` line has no code producer — an agent emits it, guided
    by the skill body — and no code consumer in this repo either; the launcher's
    B2 card and P1 project creation read it. Which meant the only thing pinning
    its key set was prose inside a skill package the runtime loads from a copy
    this repo does not control (see the install gate). So the fixture is the
    pin, and this test is the producer half of it: change what the skill
    promises and the fixture must move with it, in the same commit, or the
    launcher's twin fixture is parsing a shape nobody emits any more.
    """
    import json
    import re

    contract = _charsheet_qa_line_contract()
    text = _charsheet_skill_text()

    # What the skill promises, read out of the skill rather than restated here.
    #
    # The KEY SET is the contract; the prose around it is not. So: the first
    # ``":``-bearing span is the required object and the NEXT one is the optional
    # tail — later spans are restatement, which the bullet is free to add — and
    # both compare as SETS, because a JSON consumer cannot observe key order.
    # Asserting `len(spans) == 2` and ordered list equality made this test red on
    # two edits that changed nothing a consumer sees (a redundant restatement of
    # the same keys; reordering the example), which is the same false-positive
    # class already retired from the verb-table test.
    body = text.split("**`CHARSHEET-QA:{json}`**", 1)[1].split("- **Clarify chips**", 1)[0]
    spans = [span for span in re.findall(r"`([^`]+)`", body) if '":' in span]
    assert len(spans) >= 2, f"expected a required object and an optional tail, got {spans!r}"
    promised_required = set(re.findall(r'"([A-Za-z]+)"\s*:', spans[0]))
    promised_optional = set(re.findall(r'"([A-Za-z]+)"\s*:', spans[1]))

    assert promised_required == set(contract["requiredKeys"])
    assert promised_optional == set(contract["optionalKeys"])

    # And every pinned line is that shape, literally — prefix, then one JSON
    # object, on one line. This is the byte sequence the launcher's twin parses.
    for name, line in contract["lines"].items():
        assert "\n" not in line, f"{name}: the line is one line"
        assert line.startswith(contract["prefix"]), f"{name}: wrong prefix"
        payload = json.loads(line[len(contract["prefix"]) :])
        assert set(contract["requiredKeys"]) <= set(payload), f"{name}: missing a required key"
        assert set(payload) <= set(contract["requiredKeys"]) | set(
            contract["optionalKeys"]
        ), f"{name}: carries a key the skill never promised"
        assert all(str(value).strip() for value in payload.values()), f"{name}: empty value"


def _write_skill_package(root: Path, skill: str, body: str) -> Path:
    package = root / skill
    package.mkdir(parents=True, exist_ok=True)
    path = package / "SKILL.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_installed_canonical_skill_drift_fails_the_install_verifier(tmp_path, monkeypatch, capsys):
    """The join A0 shipped without a gate: the repo copy vs the copy turns READ.

    ``docs/agent-runtime-harness/harness-skills/<id>/SKILL.md`` is the source;
    a chat turn loads ``<hermes root>/shared/skills/<id>/SKILL.md`` and nothing
    else, because ``agent.skill_utils`` refuses any candidate for a canonical id
    whose ``source_kind`` is not ``shared_core``. Nothing joins the two on
    commit. On 2026-08-24 two edits to the charsheet skill landed on ``main``
    and reached no turn at all — and every test stayed green throughout, because
    every test reads the repo copy.

    So this exercises the verifier, at the level the guarantee lives at:
    against an INSTALLED package. ``--check`` reports and never repairs; the
    default mode repairs first, then still verifies, so an install that did not
    take fails rather than passing quietly.

    The CALLER moved on 2026-08-30 (plan
    ``archive/skill-install-trigger-relocation.md``) and this test did not need
    to: it ran from ``.githooks/pre-push``, the moment the PRODUCER publishes,
    and now runs from ``.githooks/post-merge`` — the moment a CONSUMER pulls the
    drift in. ``harness serve`` boot is the other half and calls the installers
    directly (``tests/agent_runtime/test_serve_boot_skill_install.py``). What is
    covered here is the verifier's own behaviour, which is unchanged by either.
    """
    from scripts.verify_harness_skill_install import main

    source_root = tmp_path / "repo-skills"
    shared_root = tmp_path / "shared" / "skills"
    skill = "harness-qa-verdict"  # a real canonical id — the installer refuses any other

    monkeypatch.setattr(
        "agent_runtime.skill_install.harness_skill_source_root", lambda: source_root
    )
    monkeypatch.setattr("agent_runtime.skill_install.get_shared_skills_dir", lambda: shared_root)
    monkeypatch.setattr("agent_runtime.skill_install.HARNESS_SKILLS", frozenset({skill}))
    monkeypatch.setattr("hermes_constants.CANONICAL_SHARED_SKILL_IDS", frozenset({skill}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    _write_skill_package(source_root, skill, "# v1\n")

    # Never installed is not clean: the resolver would answer nothing at all.
    assert main(["--check"]) == 1
    report = capsys.readouterr().out
    assert "NOT INSTALLED" in report
    # …and the run says which tree it checked, from an EXPLICIT rung.
    assert f"home      {tmp_path / 'home'}  (via env HERMES_HOME)" in report

    assert main([]) == 0
    installed = shared_root / skill / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == "# v1\n"

    # The exact 2026-08-24 shape: the repo copy moves on, the installed one does not.
    _write_skill_package(source_root, skill, "# v2 — the sentence no turn ever read\n")
    assert main(["--check"]) == 1
    assert "DIVERGED" in capsys.readouterr().out

    # Repair mode closes it, and the runtime's copy is the repo's again.
    assert main([]) == 0
    assert installed.read_text(encoding="utf-8") == "# v2 — the sentence no turn ever read\n"

    # And drift introduced on the INSTALLED side is caught from that direction too.
    installed.write_text(installed.read_text(encoding="utf-8") + "edited in place\n", encoding="utf-8")
    assert main(["--check"]) == 1
    assert "DIVERGED" in capsys.readouterr().out
    assert main([]) == 0


def test_the_gate_refuses_to_guess_a_root_instead_of_targeting_the_shadow_tree(
    monkeypatch, capsys
):
    """The destructive shape the first version of the gate had.

    A git hook inherits the pushing shell's environment, and that shell usually
    has no ``HERMES_HOME``. ``get_shared_skills_dir()`` then resolves through
    ``get_default_hermes_root()`` to the platform default — on Windows
    ``%LOCALAPPDATA%\\hermes``, a real populated shadow runtime, the very one
    ``hermes_process_identity.dart`` pins ``HERMES_HOME`` to avoid. Measured on
    this machine with it unset, that tree held six canonical packages whose bytes
    were not this repo's; repair mode would have overwritten all six, reported
    ``ok`` about the copy it had just written, and never looked at the root every
    persona in the live roster reads.

    So an unresolvable root is an ERROR (exit 2, the usage/environment code),
    not a default. Printing the resolved root would not do: nobody reads a
    passing hook's output.
    """
    from scripts.verify_harness_skill_install import main

    monkeypatch.delenv("ETERNIA_HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    # No machine declaration either — the config rung answers nothing.
    monkeypatch.setattr(
        "agent_runtime.chat_session_scope.declared_chat_head_home", lambda: None
    )

    assert main(["--check"]) == 2
    captured = capsys.readouterr()
    assert "REFUSING to guess the hermes home" in captured.err
    assert "HERMES_HOME=<hermes root>/profiles/base" in captured.err
    # It refused BEFORE reporting on any tree.
    assert "canonical shared skill(s)" not in captured.out


def test_the_gate_reads_the_machine_declaration_before_it_would_ever_guess(
    tmp_path, monkeypatch
):
    """The rungs, in order — and none of them is the platform default.

    ``ETERNIA_HERMES_HOME`` first, matching ``hermes_cli_io.dart``'s
    ``hermesProcessEnvironment`` (``ETERNIA_HERMES_HOME ?? HERMES_HOME``) so the
    gate targets the home the launcher spawned serve with. Then ``HERMES_HOME``.
    Then the machine root anchor ``harness serve`` publishes — a DECLARATION
    written by the process that provably knew, read through the function that
    owns that key rather than a second parser of it.
    """
    from scripts.verify_harness_skill_install import resolve_gate_hermes_home

    declared = tmp_path / "declared-home"
    declared.mkdir()
    monkeypatch.setattr(
        "agent_runtime.chat_session_scope.declared_chat_head_home", lambda: declared
    )

    monkeypatch.delenv("ETERNIA_HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert resolve_gate_hermes_home() == (str(declared), "config agent_runtime.head_home")

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "env-home"))
    assert resolve_gate_hermes_home() == (str(tmp_path / "env-home"), "env HERMES_HOME")

    monkeypatch.setenv("ETERNIA_HERMES_HOME", str(tmp_path / "launcher-home"))
    assert resolve_gate_hermes_home() == (
        str(tmp_path / "launcher-home"),
        "env ETERNIA_HERMES_HOME",
    )


def test_a_repair_archives_the_package_it_displaces_rather_than_destroying_it(
    tmp_path, monkeypatch
):
    """Repair-then-verify is the right design; unrecoverable repair is not.

    ``install_harness_skill`` renames the installed package aside and used to
    ``shutil.rmtree`` it in a ``finally``, so a repair aimed at the wrong root
    destroyed whatever was there with no way back. The root is explicit now, but
    the seatbelt is cheap: the displaced package moves into the shared root's
    ``.archive/`` — already resolver-invisible via
    ``agent.skill_utils.EXCLUDED_SKILL_DIRS`` and already the convention
    ``tools/skill_usage.archive_skill`` uses — so a wrong repair is a ``mv``
    away from undone. Nothing is written at all when the hashes already match.
    """
    from agent_runtime.skill_install import REPLACED_ARCHIVE_DIR_NAME, install_harness_skill

    source_root = tmp_path / "repo-skills"
    shared_root = tmp_path / "shared" / "skills"
    skill = "harness-qa-verdict"

    monkeypatch.setattr(
        "agent_runtime.skill_install.harness_skill_source_root", lambda: source_root
    )
    monkeypatch.setattr("agent_runtime.skill_install.get_shared_skills_dir", lambda: shared_root)

    # A package this repo does not own is already installed there.
    _write_skill_package(shared_root, skill, "# six packages that were not ours\n")
    _write_skill_package(source_root, skill, "# v1\n")

    result = install_harness_skill(skill)
    assert result.changed and result.ok
    assert (shared_root / skill / "SKILL.md").read_text(encoding="utf-8") == "# v1\n"

    archived = sorted((shared_root / REPLACED_ARCHIVE_DIR_NAME).glob(f"{skill}-*"))
    assert archived, "the displaced package must be archived, never rmtree'd"
    assert (archived[0] / "SKILL.md").read_text(encoding="utf-8") == (
        "# six packages that were not ours\n"
    )

    # An install with nothing to displace archives nothing.
    before = list((shared_root / REPLACED_ARCHIVE_DIR_NAME).iterdir())
    assert not install_harness_skill(skill).changed
    assert list((shared_root / REPLACED_ARCHIVE_DIR_NAME).iterdir()) == before


def test_harness_skill_install_allows_readiness_from_temp_home(tmp_path, monkeypatch):
    from agent_runtime.profile_readiness import profile_readiness_for_persona
    from agent_runtime.skill_install import install_harness_skills

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    results = install_harness_skills(hermes_home=tmp_path)
    assert all(result.ok for result in results)

    qa = _persona(id="qa", role="qa", system_prompt_path="personas/qa/system.md", skills=["harness-qa-verdict"])
    readiness = profile_readiness_for_persona(qa)

    assert readiness["missing_skills"] == []
    assert readiness["skill_hash_mismatches"] == []


def test_harness_skill_install_repairs_hash_mismatch(tmp_path, monkeypatch):
    from agent_runtime.skill_install import harness_skill_hash_mismatches, install_harness_skill

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = install_harness_skill("harness-runtime-model", hermes_home=tmp_path)
    assert first.ok is True
    assert first.changed is True

    installed = Path(first.destination)
    installed.write_text(installed.read_text(encoding="utf-8") + "\n# stale local edit\n", encoding="utf-8")
    assert harness_skill_hash_mismatches(["harness-runtime-model"], hermes_home=tmp_path) == ["harness-runtime-model"]

    repaired = install_harness_skill("harness-runtime-model", hermes_home=tmp_path)
    assert repaired.ok is True
    assert repaired.changed is True
    assert harness_skill_hash_mismatches(["harness-runtime-model"], hermes_home=tmp_path) == []


def test_harness_install_receipt_hashes_and_installs_the_complete_package(
    tmp_path, monkeypatch
):
    from agent_runtime import skill_install

    source_root = tmp_path / "source"
    package = source_root / "package-skill"
    (package / "references").mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: package-skill\n---\nbody\n", encoding="utf-8"
    )
    (package / "references" / "contract.md").write_text(
        "contract\n", encoding="utf-8"
    )
    shared = tmp_path / "shared"
    monkeypatch.setattr(skill_install, "HARNESS_SKILLS", frozenset({"package-skill"}))
    monkeypatch.setattr(skill_install, "harness_skill_source_root", lambda: source_root)
    monkeypatch.setattr(skill_install, "get_shared_skills_dir", lambda: shared)

    receipt = skill_install.install_harness_skill("package-skill")

    assert receipt.ok is True
    assert receipt.source_hash == receipt.installed_hash
    assert (shared / "package-skill" / "references" / "contract.md").read_text(
        encoding="utf-8"
    ) == "contract\n"


def test_harness_skill_cli_defaults_to_persona_profiles(monkeypatch, capsys):
    from agent_runtime.skill_install import SkillInstallResult
    from hermes_cli import harness

    calls: list[str] = []
    result = SkillInstallResult(
        skill="harness-qa-verdict",
        source="source",
        destination="destination",
        source_hash="sha256:1",
        installed_hash="sha256:1",
        installed=True,
        changed=False,
        ok=True,
    )

    monkeypatch.setattr(harness, "load_agent_runtime_config", lambda: object())
    monkeypatch.setattr(harness, "ensure_persisted_personas", lambda _cfg: ["qa"])
    monkeypatch.setattr(harness, "install_harness_skills_for_personas", lambda _personas: calls.append("personas") or [result])
    monkeypatch.setattr(harness, "install_harness_skills", lambda: calls.append("active") or [result])

    assert harness._cmd_install_harness_skills(SimpleNamespace(active_profile_only=False, json=True)) == 0
    assert calls == ["personas"]
    assert '"ok": true' in capsys.readouterr().out

    assert harness._cmd_install_harness_skills(SimpleNamespace(active_profile_only=True, json=True)) == 0
    assert calls == ["personas", "active"]


# ── the create verb's install gate (plan S4 / D5) ────────────────────────────
#
# The first place in this codebase where the repo↔installed hash is a GATE and
# not a report. ``profile_readiness`` files a mismatch at severity 15 and
# ``prompt_observability`` raises a HUD flag; both are advisory, which is how a
# running agent came to be reading a 14457-byte copy of a 14906-byte skill on
# 2026-08-24. A verb that ASSIGNS a skill is where "the copy is stale" has an
# answer that is not a warning.

_SKILL_WORKSPACE = "ws_agent_create_skills"


@pytest.fixture
def skills_create_fixture(tmp_path, monkeypatch):
    """An isolated shared skills root, a seeded office, and the ``qa`` persona.

    ``HERMES_SHARED_SKILLS`` and not a monkeypatched attribute: ``skill_install``
    and ``agent.skill_utils.skill_source_kind`` resolve the shared root
    independently, and pinning only one leaves the resolver classifying the
    installed copy as ``external`` — which is ``invalid_source`` for a canonical
    id, i.e. a red that has nothing to do with the subject. The assertion below
    is what turns a regression in that precedence into a loud failure instead of
    a silent write into the OPERATOR's live packages.
    """

    from agent_runtime.office_store import OfficeStore
    from hermes_constants import get_shared_skills_dir
    from tests.agent_runtime.office_seed import seed_workspace_record

    shared = tmp_path / "shared-skills"
    monkeypatch.setenv("HERMES_SHARED_SKILLS", str(shared))
    assert get_shared_skills_dir() == shared

    seed_workspace_record(_SKILL_WORKSPACE)
    OfficeStore().ensure_surface(_SKILL_WORKSPACE, created_by="seed")
    return shared


def _create_with_skills(*skills: str, placement: str, key: str):
    from agent_runtime.agent_create import perform_agent_create

    return perform_agent_create(
        {
            "persona_id": "qa",
            "workspace_id": _SKILL_WORKSPACE,
            "position": [0.0, 0.0],
            "idempotency_key": key,
            "placement_id": placement,
            "skills": list(skills),
        }
    )


def test_agent_create_installs_and_verifies_canonical_skill(skills_create_fixture):
    """A DELIBERATELY STALE installed copy ends hash-equal to the repo package.

    KILLING MUTATION (plan §C): skip ``install_harness_skill`` in the phase and
    the installed hash stays the stale one — reds.

    ANTI-VACUITY. The stale copy is seeded FIRST and its divergence is asserted
    before the create runs, so "the hashes match at the end" cannot be satisfied
    by a create that did nothing — at the moment the create starts, they do not
    match. And the hash is read off the FILE through the same
    ``harness_skill_hash_mismatches`` the rest of the runtime uses, not off the
    ack, so a create that reported a good hash while leaving a stale file fails.
    """

    from agent_runtime.skill_install import (
        harness_skill_destination,
        harness_skill_hash_mismatches,
        install_harness_skill,
    )

    skill = "harness-qa-verdict"
    install_harness_skill(skill)
    destination = harness_skill_destination(skill)
    destination.write_text(
        destination.read_text(encoding="utf-8") + "\n# a stale local edit\n",
        encoding="utf-8",
    )
    assert harness_skill_hash_mismatches([skill]) == [skill]

    outcome = _create_with_skills(skill, placement="qa_stale_agent_2", key="skills-stale")

    assert outcome.refusal is None
    assert harness_skill_hash_mismatches([skill]) == []
    assert outcome.result["skills"]["assigned"] == [skill]

    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert PersonaInstanceStore().get(
        outcome.result["persona_instance_id"]
    ).skill_overrides == [skill]


def test_agent_create_refuses_unresolved_skill(skills_create_fixture):
    """An id nothing resolves refuses ``skill_unresolved`` NAMING its status.

    KILLING MUTATION (plan §C): accept any string and the refusal is absent —
    reds.

    ANTI-VACUITY. The status is asserted, not just the reason: an implementation
    that refused every unknown id with a hard-coded ``missing`` would pass this
    and fail the ``invalid_source`` case below, which is why both are here.
    """

    outcome = _create_with_skills(
        "definitely-not-a-skill", placement="qa_unres_agent_2", key="skills-unresolved"
    )

    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "skill_unresolved"
    assert outcome.refusal.data["skill"] == "definitely-not-a-skill"
    assert outcome.refusal.data["status"] == "missing"
    assert outcome.refusal.data["phase"] == "skills"
    assert outcome.refusal.data["rolled_back"] is False


def test_the_status_is_the_resolvers_and_not_a_constant(
    skills_create_fixture, monkeypatch
):
    """``invalid_source`` reaches the client unchanged.

    ``_skill_resolution_status`` answers ``invalid_source`` for a CANONICAL id
    whose only candidate is not the shared root — a repo checkout, a profile-local
    copy — and that distinction is the operator's whole diagnosis: "you have this
    skill, in the wrong place" is a different instruction from "you do not have
    it". Reached honestly by planting the canonical id in the PROFILE-local root
    and pointing the shared root somewhere that does not hold it, then patching
    the install away so the gate cannot repair the shape the test is about.
    """

    from agent_runtime import skill_install
    from hermes_constants import get_skills_dir

    skill = "harness-qa-verdict"
    local = get_skills_dir() / skill
    local.mkdir(parents=True, exist_ok=True)
    (local / "SKILL.md").write_text(
        f"---\nname: {skill}\n---\nlocal copy\n", encoding="utf-8"
    )
    def _no_install(name):
        # The install is what would move this id into the shared root and make
        # it ``resolved``; stubbing it is what keeps the subject visible.
        class _Receipt:
            skill = name
            changed = False
            installed_hash = None

        return _Receipt()

    monkeypatch.setattr(
        skill_install, "install_and_verify_harness_skill", _no_install
    )
    outcome = _create_with_skills(
        skill, placement="qa_invsrc_agent_2", key="skills-invalid-source"
    )

    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "skill_unresolved"
    assert outcome.refusal.data["status"] == "invalid_source"


def test_a_copy_that_never_lands_is_a_divergence_and_not_a_clean_bill(
    skills_create_fixture, monkeypatch
):
    """An INJECTED copy fault refuses ``skill_install_diverged``.

    KILLING MUTATION (plan §C): drop the post-install verification and this
    reds — the refusal is absent and the create completes, handing the agent a
    skill whose package was never written.

    ANTI-VACUITY, and this is the case the obvious gate misses.
    ``harness_skill_hash_mismatches`` ``continue``s past a destination that does
    not EXIST, so on a fresh root a failed copy produces an EMPTY mismatch list —
    a false all-clear. That is why ``install_and_verify_harness_skill`` asks
    three questions and not one, and why the fault injected here is a copy that
    raises rather than a copy that writes the wrong bytes.
    """

    from agent_runtime import skill_install

    def _explode(*args, **kwargs):
        raise OSError("the disk said no")

    monkeypatch.setattr(skill_install.shutil, "copytree", _explode)

    outcome = _create_with_skills(
        "harness-qa-verdict", placement="qa_copyfault_agent_2", key="skills-copy-fault"
    )

    assert outcome.refusal is not None
    data = outcome.refusal.data
    assert data["reason"] == "skill_install_diverged"
    assert data["skill"] == "harness-qa-verdict"
    assert data["phase"] == "skills"
    # Not rolled back, and the agent it refused for is standing.
    assert data["rolled_back"] is False
    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert PersonaInstanceStore().get("personainst_qa_copyfault_agent_2").skill_overrides is None


def test_an_install_that_REPORTS_success_is_still_re_read_before_it_is_trusted(
    skills_create_fixture, monkeypatch
):
    """The install's own receipt is not the witness. The re-read is.

    KILLING MUTATION: drop the ``harness_skill_hash_mismatches`` re-read from
    ``install_and_verify_harness_skill`` and this reds — the create completes and
    the agent is handed the stale package.

    WHY THIS TEST EXISTS AND THE COPY-FAULT ONE DID NOT COVER IT. A copy that
    RAISES is caught by the phase's ``except`` whether or not anything verifies
    afterwards, so that test passes under the dropped-verification mutant — it
    was measured surviving in ``tests/mutation_claims.json``'s gate. The
    condition that has no other witness is this one: an install that reports
    ``ok`` while the bytes on disk are not the repo's.

    That is not a hypothetical shape. It is the 2026-08-24 incident exactly — a
    running agent reading a 14457-byte copy of a 14906-byte skill while every
    advisory reader reported the install fine — and it is why the helper asks an
    INDEPENDENT question (re-read both packages) rather than trusting the value
    the install computed on its way out.

    ANTI-VACUITY. The destination is corrupted AFTER a real install, so the file
    exists and ``resolve_skills`` answers ``resolved``: the mutant reaches the
    assignment and returns NO refusal at all, rather than tripping some other
    gate and looking killed for the wrong reason.
    """

    from agent_runtime import skill_install

    skill = "harness-qa-verdict"
    receipt = skill_install.install_harness_skill(skill)
    destination = skill_install.harness_skill_destination(skill)
    destination.write_text(
        destination.read_text(encoding="utf-8") + "\n# not the repo's bytes\n",
        encoding="utf-8",
    )

    def _lying_install(name, *, hermes_home=None):
        # An install that no-ops and reports success — the shape a silently
        # skipped copy, a wrong-root repair or a partially applied replace
        # leaves behind.
        return skill_install.SkillInstallResult(
            skill=name,
            source=receipt.source,
            destination=receipt.destination,
            source_hash=receipt.source_hash,
            installed_hash=receipt.source_hash,
            installed=True,
            changed=False,
            ok=True,
        )

    monkeypatch.setattr(skill_install, "install_harness_skill", _lying_install)

    outcome = _create_with_skills(
        skill, placement="qa_lying_agent_2", key="skills-lying-install"
    )

    assert outcome.refusal is not None
    assert outcome.refusal.data["reason"] == "skill_install_diverged"
    assert outcome.refusal.data["skill"] == skill
    assert outcome.refusal.data["phase"] == "skills"
    assert outcome.refusal.data["rolled_back"] is False
    # The agent it refused for is standing, and was handed nothing.
    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert PersonaInstanceStore().get("personainst_qa_lying_agent_2").skill_overrides is None


def test_the_verb_writes_the_INSTANCE_tier_and_never_the_persona_template(
    skills_create_fixture,
):
    """The persona record is untouched, and that is the load-bearing half.

    F12/F13: ``persona.skills`` is the TEMPLATE every future instance of that
    persona inherits, and no operator verb has ever written it. A create that
    assigned there would silently reconfigure every other instance of the
    persona — including ones an operator tuned by hand — which is why the phase
    calls ``PersonaInstanceStore.update_profile`` and nothing else.
    """

    from agent_runtime.store import AgentStore

    before = list(AgentStore().get("qa").skills or [])
    outcome = _create_with_skills(
        "harness-qa-verdict", placement="qa_tier_agent_2", key="skills-tier"
    )

    assert outcome.refusal is None
    assert list(AgentStore().get("qa").skills or []) == before
    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert PersonaInstanceStore().get("personainst_qa_tier_agent_2").skill_overrides == [
        "harness-qa-verdict"
    ]

