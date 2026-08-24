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


def test_charsheet_skill_documents_exactly_the_characters_verbs_hermes_has():
    """The skill's verb table is pinned to the live parser tree.

    A skill that teaches a stale verb surface is worse than no skill — agents
    trust it. So the table cannot drift in either direction: a verb hermes grows
    (``add-state``) and a verb the skill invents both fail here.
    """
    import re

    documented = {
        match.group(1)
        for match in re.finditer(r"^\| `([a-z][a-z-]*)", _charsheet_skill_text(), re.MULTILINE)
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
