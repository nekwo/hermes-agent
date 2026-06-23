from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_runtime.models import AgentPersona
from agent_runtime.persona_runtime import build_system_prompt
from agent_runtime.personas import AgentRole, default_personas, effective_toolsets, validate_toolsets


def _persona(**overrides) -> AgentPersona:
    data = {
        "id": "dev",
        "display_name": "Dev",
        "role": AgentRole.DEV.value,
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
    assert "skills" in validate_toolsets(AgentRole.DEV, ["file", "skills", "cronjob"])
    assert "skills" in effective_toolsets(_persona())


def test_harness_system_prompt_lists_recommended_skills_without_preloading_bodies():
    prompt = build_system_prompt(_persona(), task_id="task-123")

    assert "# Recommended Harness Persona Skills" in prompt
    assert "Recommended skills:" in prompt
    assert "- aaa-feature-delivery" in prompt
    assert "- test-driven-development" in prompt
    assert "skill use is the default for non-trivial Harness ticks" in prompt
    assert "start with skill_search(query=...)" in prompt
    assert "Two loaded skills is the normal maximum" in prompt
    assert "Never preload or bulk-load the whole manifest" in prompt
    assert "skill_search(query=...)" in prompt
    assert "skills_list/skill_view" in prompt
    assert "do not shell out to `hermes skills search`" in prompt
    assert "Loaded Harness persona skill" not in prompt
    assert "Loaded by Agent Runtime Harness persona skill manifest" not in prompt


def test_stage46_personas_expose_mission_dev_and_qa_skills():
    personas = {persona.id: persona for persona in default_personas()}

    assert "harness-mission-lead" in personas["neko_supervisor"].skills
    assert "harness-dev-delivery" in personas["dev"].skills
    assert "launcher-analyze-proof" in personas["dev"].skills
    assert "harness-dev-delivery" in personas["backend_dev"].skills
    assert "launcher-analyze-proof" not in personas["backend_dev"].skills
    assert "harness-qa-verdict" in personas["qa"].skills


def test_stage46_install_uses_persona_declared_skills_not_role_map():
    from agent_runtime.skill_install import stage46_required_skills_for_persona

    dev_without_stage46 = _persona(id="dev", skills=["aaa-feature-delivery"])
    dev_with_stage46 = _persona(id="dev", skills=["aaa-feature-delivery", "harness-dev-delivery"])

    assert stage46_required_skills_for_persona(dev_without_stage46) == []
    assert stage46_required_skills_for_persona(dev_with_stage46) == ["harness-dev-delivery"]


def test_stage59_hud_skill_sections_exist_in_role_skills():
    root = Path(__file__).resolve().parents[2] / "docs" / "agent-runtime-harness" / "stage46-skills"
    expected = {
        "harness-mission-lead": {"Scoped Handoff", "Bounded Recovery", "QA Release", "Incident Resolution"},
        "harness-dev-delivery": {"Deliver Patch", "Request Proof Recipe", "Request Context", "Stage Plan", "Report Blocker"},
        "harness-qa-verdict": {"QA Verdict", "Request Missing Proof", "Report Blocker"},
    }

    for skill_id, sections in expected.items():
        text = (root / skill_id / "SKILL.md").read_text(encoding="utf-8")
        for section in sections:
            assert f"## {section}" in text
        assert "decision_menu[].shape_id" not in text
        assert "primary_worker_action" not in text
        assert "next_required_move" not in text


def test_stage46_skill_install_allows_readiness_from_temp_home(tmp_path, monkeypatch):
    from agent_runtime.profile_readiness import profile_readiness_for_persona
    from agent_runtime.skill_install import install_stage46_skills

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    results = install_stage46_skills(hermes_home=tmp_path)
    assert all(result.ok for result in results)

    qa = _persona(id="qa", role=AgentRole.QA.value, system_prompt_path="personas/qa/system.md", skills=["harness-qa-verdict"])
    readiness = profile_readiness_for_persona(qa)

    assert readiness["missing_skills"] == []
    assert readiness["skill_hash_mismatches"] == []


def test_stage46_skill_cli_defaults_to_persona_profiles(monkeypatch, capsys):
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
    monkeypatch.setattr(harness, "configured_personas", lambda _cfg: ["qa"])
    monkeypatch.setattr(harness, "install_stage46_skills_for_personas", lambda _personas: calls.append("personas") or [result])
    monkeypatch.setattr(harness, "install_stage46_skills", lambda: calls.append("active") or [result])

    assert harness._cmd_install_stage46_skills(SimpleNamespace(active_profile_only=False, all_persona_profiles=False, json=True)) == 0
    assert calls == ["personas"]
    assert '"ok": true' in capsys.readouterr().out

    assert harness._cmd_install_stage46_skills(SimpleNamespace(active_profile_only=True, all_persona_profiles=False, json=True)) == 0
    assert calls == ["personas", "active"]
