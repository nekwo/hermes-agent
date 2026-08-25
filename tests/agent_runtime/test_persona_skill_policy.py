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

    assert "harness-mission-lead" in personas["neko_supervisor"].skills
    assert "harness-continuity" in personas["neko_supervisor"].skills
    assert "harness-dev-delivery" in personas["dev"].skills
    assert "harness-continuity" in personas["dev"].skills
    assert "launcher-analyze-proof" in personas["dev"].skills
    assert "harness-dev-delivery" in personas["backend_dev"].skills
    assert "harness-continuity" in personas["backend_dev"].skills
    assert "launcher-analyze-proof" not in personas["backend_dev"].skills
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
        "harness-mission-lead": {"Scope Route", "Bounded Recovery", "QA Release", "Incident Resolution"},
        "harness-continuity": {"Spawn And Resume", "Return Command", "Progress Peek", "Never Slurp"},
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
    text = _charsheet_skill_text()

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
    text = _charsheet_skill_text()

    # 1 — the plan-gated account that fails politely at HTTP 200.
    assert "`image_generation` tool silently stripped" in text
    # 2 — the stale token a plan change leaves behind.
    assert "`401 token_expired`" in text
    assert "STALE STORED TOKEN, not" in text
    assert "Force a refresh and" in text
    # 3 — HERMES_HOME is not one value.
    assert "A relative `HERMES_HOME` resolves against the shell's cwd" in text


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
    this repo does not control (see the pre-push gate). So the fixture is the
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


def test_installed_canonical_skill_drift_fails_the_pre_push_gate(tmp_path, monkeypatch, capsys):
    """The join A0 shipped without a gate: the repo copy vs the copy turns READ.

    ``docs/agent-runtime-harness/harness-skills/<id>/SKILL.md`` is the source;
    a chat turn loads ``<hermes root>/shared/skills/<id>/SKILL.md`` and nothing
    else, because ``agent.skill_utils`` refuses any candidate for a canonical id
    whose ``source_kind`` is not ``shared_core``. Nothing joins the two on
    commit. On 2026-08-24 two edits to the charsheet skill landed on ``main``
    and reached no turn at all — and every test stayed green throughout, because
    every test reads the repo copy.

    So this exercises the verifier the pre-push hook runs, at the level the
    guarantee lives at: against an INSTALLED package. ``--check`` reports and
    never repairs; the default mode repairs first, then still verifies, so an
    install that did not take fails rather than passing quietly.
    """
    from scripts.verify_harness_skill_install import main

    source_root = tmp_path / "repo-skills"
    shared_root = tmp_path / "shared" / "skills"
    skill = "harness-continuity"  # a real canonical id — the installer refuses any other

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
    skill = "harness-continuity"

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


def test_mission_lead_skill_answers_graph_from_supplied_task_plan():
    root = Path(__file__).resolve().parents[2] / "docs" / "agent-runtime-harness" / "harness-skills"
    text = (root / "harness-mission-lead" / "SKILL.md").read_text(encoding="utf-8")

    assert 'When asked "what graph/flow are you using?"' in text
    assert "supplied active task's `mission_plan`" in text
    assert "not from the most recent running goal" in text
    assert "`blueprint_id`, active stage, stage order, owners, and outgoing edges" in text


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
