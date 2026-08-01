"""S16 completes the S11 intent: the decision registry stops modelling roles.

S11 deleted the role matrix (``ALLOWED_TOOLSETS_BY_ROLE``, ``PER_ROLE_TOOL_DENIES``,
``R1_ADMISSIBLE_ROLES``) and ruled that persona/profile DATA is authoritative for
what an agent may do. S15 then removed the no-op shell it left behind
(``allowed_decisions_for_role``) but deliberately kept
``DecisionContract.allowed_roles``, because ``prompt_contract_markdown`` still read
it — ``if not contract.allowed_roles: continue`` was the last live consumer of a
role→decision matrix anywhere in the fork.

That prompt builder died with the persona prompt-builder lane
(``55bbb6ab7``: ``persona_runtime.build_system_prompt`` and everything only it
called). ``prompt_contract_markdown`` was its sole caller's sole use, so it now
has zero callers, and with it gone ``allowed_roles`` is a field nothing reads —
a role gate that is published on the wire (``DecisionContract.manifest``) and
therefore invites a consumer to start filtering on it again.

Consequence recorded here because it is load-bearing: removing a manifest key
changes ``contract_hash()``. Live persona instances stamp it as
``prompt_contract_hash``, so they read as contract-drifted until their next turn.
That is expected and self-heals; ``ALLOWED_EVENT_TYPES`` is append-checked only,
so nothing historical breaks.

The KEEP side — role tokens are still first-class DATA:
``HudShape.roles`` still publishes which shapes belong to which role in the HUD,
``canonical_role_value`` still passes any token through, and every ``DecisionType``
still reaches every role.
"""

from __future__ import annotations

from dataclasses import fields

from agent_runtime import decision_contract_registry as registry
from agent_runtime.decision_contract_registry import (
    DecisionContract,
    canonical_role_value,
    contract_manifest,
    hud_shape_index_for_stage,
    payload_contract,
    verify_registry,
)
from agent_runtime.decision_schema import DecisionType
from agent_runtime.personas import AgentRole


def test_the_caller_free_prompt_contract_builder_is_gone():
    """Its only caller was ``persona_runtime.build_system_prompt`` (55bbb6ab7)."""

    assert not hasattr(registry, "prompt_contract_markdown")


def test_the_decision_contract_no_longer_models_roles():
    field_names = {item.name for item in fields(DecisionContract)}

    assert "allowed_roles" not in field_names
    # The dataclass is frozen+slots; a removed field must not linger as an
    # attribute default either.
    assert not hasattr(DecisionContract, "allowed_roles")
    # S54 removed ``all_decision_contracts`` (a whole-map accessor with no
    # production caller). The same ground is covered by walking the live
    # DecisionType enum through the live ``decision_contract`` lookup.
    for decision_type in DecisionType:
        assert not hasattr(registry.decision_contract(decision_type), "allowed_roles")


def test_no_decision_contract_publishes_a_role_gate_on_the_wire():
    """``manifest()`` used to append ``allowed_roles``; nothing may read it back."""

    for decision_type in DecisionType:
        contract = registry.decision_contract(decision_type)
        manifest = contract.manifest()
        assert "allowed_roles" not in manifest, decision_type.value
        # manifest() is now exactly payload_contract() — no role tail.
        assert manifest == payload_contract(decision_type)

    published = contract_manifest()["decisions"]
    assert published
    for decision_type, entry in published.items():
        assert "allowed_roles" not in entry, decision_type


def test_the_role_tuple_aliases_used_only_by_the_removed_gate_are_gone():
    """``_PM_QA`` existed solely as an ``allowed_roles=`` value; the HUD shapes
    never used it. The other aliases survive because ``HudShape.roles`` uses them."""

    assert not hasattr(registry, "_PM_QA")
    # Dead since the shape index stopped being role-filtered (S11/S15): a tuple
    # of shape ids nothing reads.
    assert not hasattr(registry, "_COMMON_SHAPE_IDS")


def test_role_tokens_remain_first_class_data():
    """Negative gate: this removes a GATE, not role identity."""

    assert {item.value for item in DecisionType} == set(contract_manifest()["decisions"])
    for role in [*AgentRole, "custom-reviewer", "profile", "neko_supervisor"]:
        assert canonical_role_value(role)
        assert set(hud_shape_index_for_stage(role)) == set(contract_manifest()["hud_shapes"])

    # HUD shapes still declare their role audience — that is presentation data
    # the Agent Console renders, never an admission check.
    hud_shapes = contract_manifest()["hud_shapes"]
    assert hud_shapes["qa.verdict"]["roles"] == ["qa"]
    assert set(hud_shapes["common.block"]["roles"]) == {item.value for item in AgentRole}


def test_the_registry_still_verifies_clean():
    result = verify_registry()

    assert result["ok"] is True
    assert result["missing_decision_types"] == []
    assert result["missing_object_contracts"] == []
    assert result["hud_template_errors"] == []
    assert result["contract_hash"]
