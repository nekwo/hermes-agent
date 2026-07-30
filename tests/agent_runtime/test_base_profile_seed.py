from __future__ import annotations

from agent_runtime.config import AgentRuntimeConfig, ensure_persisted_personas, get_persisted_persona, persona_records_from_config
from agent_runtime.models import AgentPersona
from agent_runtime.snapshot import build_snapshot
from agent_runtime.store import AgentStore


def _config() -> AgentRuntimeConfig:
    return AgentRuntimeConfig(
        personas={
            "base": {
                "role": "profile",
                "display_name": "Base Agent",
                "hermes_profile": "base",
                "toolsets": ["file", "terminal", "board", "agent_chat"],
            },
            "reviewer-one": {
                "role": "custom-reviewer",
                "display_name": "Reviewer One",
                "hermes_profile": "reviewer-one",
                "toolsets": ["search", "board"],
            },
        }
    )


def test_config_personas_are_data_owned_and_keep_unknown_roles():
    records = {item.id: item for item in persona_records_from_config(_config())}
    assert set(records) == {"base", "reviewer-one"}
    assert records["reviewer-one"].role == "custom-reviewer"


def test_ensure_merges_config_and_persisted_rows_without_writing_defaults():
    stored = AgentPersona("stored", "Stored", "another-role", None, None, None, ["file"], "")
    AgentStore().save(stored)
    resolved = {item.id: item for item in ensure_persisted_personas(_config())}
    assert set(resolved) == {"base", "reviewer-one", "stored"}
    assert {item.id for item in AgentStore().list_all()} == {"stored"}


def test_persisted_row_wins_over_config_row_with_the_same_id():
    stored = AgentPersona("base", "Operator Override", "operator-role", None, None, None, ["file"], "")
    AgentStore().save(stored)
    assert get_persisted_persona("base", _config()).display_name == "Operator Override"


def test_snapshot_surfaces_only_persisted_persona_rows():
    AgentStore().save(AgentPersona("stored", "Stored", "custom", None, None, None, ["file"], ""))
    snapshot = build_snapshot()
    assert [row["persona_id"] for row in snapshot["agents"]] == ["stored"]


def test_empty_config_and_store_do_not_synthesize_personas():
    assert ensure_persisted_personas(AgentRuntimeConfig()) == []
    assert AgentStore().list_all() == []
