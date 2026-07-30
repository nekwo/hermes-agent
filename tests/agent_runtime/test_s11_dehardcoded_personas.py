from __future__ import annotations

import agent_runtime.personas as personas
from agent_runtime.config import AgentRuntimeConfig, persona_records_from_config


def test_persona_module_declares_no_bundled_team_or_role_policy() -> None:
    retired = {
        "default_" + "personas",
        "seed_" + "personas",
        "BUNDLED_" + "PERSONA_PROFILES",
        "BUNDLED_" + "PERSONA_IDS",
        "DEFAULT_" + "PERSONA_IDS",
        "BASE_" + "PERSONA_ID",
        "DEFAULT_SUPERVISOR_" + "PERSONA_ID",
        "ALLOWED_TOOLSETS_" + "BY_ROLE",
        "PER_ROLE_" + "TOOL_DENIES",
    }
    assert retired.isdisjoint(vars(personas))


def test_configured_persona_with_unknown_role_is_preserved() -> None:
    cfg = AgentRuntimeConfig(
        personas={
            "custom-reviewer": {
                "display_name": "Custom Reviewer",
                "role": "custom_review_role",
                "toolsets": ["file", "agent_chat"],
            }
        }
    )

    records = {persona.id: persona for persona in persona_records_from_config(cfg)}

    assert records["custom-reviewer"].role == "custom_review_role"
    assert records["custom-reviewer"].toolsets == ["file", "agent_chat"]
