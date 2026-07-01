"""Base-profile foundation: only the `base` profile is seeded/shown, while the typed
pipeline personas stay resolvable as a dormant catalog.

Regression coverage for the seam where AgentStore (what Mission Control surfaces) holds
base only, but persona resolvers still find the mothballed dev/qa/neko personas.
"""

from __future__ import annotations

from agent_runtime.config import (
    ensure_persisted_personas,
    get_persisted_persona,
    persona_records_from_config,
)
from agent_runtime.personas import BASE_PERSONA_ID, PROFILE_ROLE_SENTINEL, seed_personas
from agent_runtime.snapshot import build_snapshot
from agent_runtime.store import AgentStore


def test_seed_personas_is_exactly_the_base_profile():
    seeds = seed_personas()
    assert [p.id for p in seeds] == [BASE_PERSONA_ID]
    base = seeds[0]
    assert base.role == PROFILE_ROLE_SENTINEL
    assert base.hermes_profile == BASE_PERSONA_ID
    # Goal creation is disabled for base (no pipeline personas to route to), so the
    # mission_goal capability must NOT be exposed as a broken affordance.
    assert "mission_goal" not in base.toolsets
    assert "file" in base.toolsets and "terminal" in base.toolsets
    # Base is the operator's default agent -> carries the Mission Control runtime-model
    # skill so it can read/operate goals + graphs out of the box, but NOT the typed
    # pipeline delivery skills (it is not a dev/qa worker).
    assert base.skills == ["harness-runtime-model"]


def test_store_seeds_base_only_but_resolution_sees_dormant_catalog():
    # ensure persists ONLY base into the store...
    ensure_persisted_personas()
    assert [p.id for p in AgentStore().list_all()] == [BASE_PERSONA_ID]

    # ...but RETURNS base plus the dormant typed catalog so resolvers keep working.
    resolvable = {p.id for p in ensure_persisted_personas()}
    assert BASE_PERSONA_ID in resolvable
    assert {"dev", "qa", "neko_supervisor"}.issubset(resolvable)

    # Typed pipeline personas resolve (dormant), and base resolves.
    assert get_persisted_persona("dev").id == "dev"
    assert get_persisted_persona("base").id == BASE_PERSONA_ID


def test_snapshot_agents_show_base_only():
    snapshot = build_snapshot()
    assert [a["persona_id"] for a in snapshot["agents"]] == [BASE_PERSONA_ID]


def test_catalog_still_contains_typed_pipeline_personas():
    # The catalog (persona_records_from_config) is untouched — the pipeline is mothballed,
    # not deleted, so its persona templates remain available for a future rebuild.
    catalog = {p.id for p in persona_records_from_config()}
    assert {"neko_supervisor", "dev", "backend_dev", "qa"}.issubset(catalog)
    assert BASE_PERSONA_ID not in catalog  # base is a seed, not a pipeline template
