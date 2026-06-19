from agent_runtime.models import AgentPersona
from agent_runtime.persona_runtime import _safe_read_soul_overlay


def test_soul_overlay_rejects_absolute_or_secret_like_paths(tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("do not read", encoding="utf-8")

    assert _safe_read_soul_overlay(str(secret)) is None
    assert _safe_read_soul_overlay(".env") is None
    assert _safe_read_soul_overlay("auth/token.md") is None
    assert _safe_read_soul_overlay("config/profile.md") is None


def test_build_system_prompt_keeps_universal_rules_after_skill_context():
    from agent_runtime.persona_runtime import build_system_prompt

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/dev/system.md",
    )

    prompt = build_system_prompt(persona)

    assert prompt.rfind("# Universal Harness Rules") < prompt.rfind("# AgentDecision JSON Schema")
    assert "Do not use Kanban vocabulary" in prompt
    assert "returning a valid AgentDecision is the action for this tick" in prompt
    assert "Generic Hermes core guidance" in prompt


def test_build_system_prompt_appends_payload_contracts_after_schema():
    from agent_runtime.persona_runtime import build_system_prompt

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/dev/system.md",
    )

    prompt = build_system_prompt(persona)

    assert prompt.rfind("# AgentDecision Payload Contracts") > prompt.rfind("# AgentDecision JSON Schema")
    assert 'request_file_reads: payload must include {"paths"' in prompt
    assert '"reason"' in prompt
    assert 'block: payload must include {"reason": "...", "log_ref"' in prompt
    assert "line number" in prompt


def test_build_system_prompt_includes_stage_ownership_and_handoff_rules():
    from agent_runtime.persona_runtime import build_system_prompt

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file"],
        system_prompt_path="personas/dev/system.md",
    )

    prompt = build_system_prompt(persona)

    assert "Stage Ownership" in prompt
    assert "handoff" in prompt.lower()
    assert "proof_ids" in prompt
    assert "deterministic command/test proof" in prompt
    assert "After Harness attaches command proof IDs" in prompt


def test_build_system_prompt_specializes_launcher_dev_without_losing_shared_dev_discipline():
    from agent_runtime.persona_runtime import build_system_prompt

    persona = AgentPersona(
        id="dev",
        display_name="Launcher Dev Agent",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file", "search", "terminal"],
        system_prompt_path="personas/dev/system.md",
        repo_scope_label="EterniaLauncher",
    )

    prompt = build_system_prompt(persona)

    assert "# Specialist Dev Loop Guard" in prompt
    assert "# Launcher Dev Specialist Overlay" in prompt
    assert "Flutter" in prompt
    assert "MCP" in prompt
    assert "dirty tree" in prompt
    assert "Postgres Docker full gate" not in prompt


def test_build_system_prompt_specializes_backend_dev_without_launcher_ui_rules():
    from agent_runtime.persona_runtime import build_system_prompt

    persona = AgentPersona(
        id="backend_dev",
        display_name="Backend Dev Agent",
        role="dev",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=["file", "search", "terminal"],
        system_prompt_path="personas/dev/system.md",
        repo_scope_label="EterniaBackend",
    )

    prompt = build_system_prompt(persona)

    assert "# Specialist Dev Loop Guard" in prompt
    assert "# Backend Dev Specialist Overlay" in prompt
    assert "Postgres Docker full gate" in prompt
    assert "frontend-backend contract" in prompt
    assert "# Launcher Dev Specialist Overlay" not in prompt
