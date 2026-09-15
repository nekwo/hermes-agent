from __future__ import annotations

from agent_runtime.config import AgentRuntimeConfig, persona_records_from_config




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
