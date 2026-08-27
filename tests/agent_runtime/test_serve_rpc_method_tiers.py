"""Stage A1 — every method DECLARES what credential it wants, and says so on the wire.

The stage that ships a fact before it ships a check. Nothing here asserts a
refusal (that is Stage A3's ``test_serve_rpc_authorization.py``); what these
tests defend is that the declaration exists for every method, that it is
reachable by a client, and that adding it did not move the contract integer.

Registry-driven on purpose
--------------------------

The completeness test iterates ``serve_rpc._METHODS`` rather than comparing
against a hand-written list of the ten names. A literal list is a second
authority that goes stale the day a method is added, and the tombstone-census
lesson in this repo is exactly that: loops, never literals. A hand-written list
would have gone green on a method registered with no tier; iterating the real
registry cannot.

The one literal that IS written out is the tier of the two agent verbs and of
``runtime.office.get``. Those are not a restatement of the registry — they are
the claim canon 06 and both service docstrings already make in prose
(``console`` for a level mutation), pinned so a later edit that quietly widens a
door to ``read`` fails here.
"""

from __future__ import annotations

import pytest

from agent_runtime import serve_rpc
from agent_runtime.call_authorization import TIER_CONSOLE, TIER_READ, TIERS


def test_every_registered_method_declares_a_tier() -> None:
    """No method may exist without a declared tier — asserted over the REGISTRY.

    A method added tomorrow is covered by this test today, which is the whole
    reason it iterates instead of listing.
    """

    assert set(serve_rpc._METHOD_TIERS) == set(serve_rpc._METHODS)
    assert set(serve_rpc._METHOD_TIERS.values()) <= set(TIERS)


def test_a_registration_without_a_tier_is_not_representable() -> None:
    """``method()`` has no default, so forgetting the word is a TypeError."""

    with pytest.raises(TypeError):
        serve_rpc.method("runtime.test.tierless")  # type: ignore[call-arg]


def test_an_unknown_tier_fails_at_registration_not_at_call_time() -> None:
    """A typo is a boot failure with a name in it, not a verb that mysteriously
    refuses in the field."""

    with pytest.raises(ValueError) as excinfo:
        serve_rpc.method("runtime.test.typo", tier="conosle")
    assert "conosle" in str(excinfo.value)
    assert "runtime.test.typo" in str(excinfo.value)
    # And it did not half-register: the failure raised before the decorator ran.
    assert "runtime.test.typo" not in serve_rpc._METHODS
    assert "runtime.test.typo" not in serve_rpc._METHOD_TIERS


def test_the_manifest_carries_a_tier_for_every_advertised_method() -> None:
    """A client reads ONE block and knows both what exists and what it costs."""

    manifest = serve_rpc.manifest()
    assert set(manifest["tiers"]) == set(manifest["methods"])


def test_level_mutations_are_console_and_reads_are_read() -> None:
    """The prose claim in canon 06 and in both service docstrings, pinned."""

    tiers = serve_rpc.manifest()["tiers"]
    assert tiers["runtime.agent.create"] == TIER_CONSOLE
    assert tiers["runtime.agent.retire"] == TIER_CONSOLE
    assert tiers["runtime.office.upsert"] == TIER_CONSOLE
    assert tiers["runtime.office.remove"] == TIER_CONSOLE
    assert tiers["runtime.office.surface.update"] == TIER_CONSOLE
    assert tiers["runtime.office.resolve_conflict"] == TIER_CONSOLE
    assert tiers["runtime.office.get"] == TIER_READ
    assert tiers["runtime.office.subscribe"] == TIER_READ
    assert tiers["runtime.office.unsubscribe"] == TIER_READ
    # The one row worth arguing: prewarm writes no store state, emits no event
    # and mints no id — its own contract — so it sits with the reads.
    assert tiers["runtime.persona.prewarm"] == TIER_READ


def test_adding_the_tiers_block_did_not_move_the_contract_integer() -> None:
    """A manifest is a set plus an integer. This stage added a KEY, and a key is
    not a shape change to any existing method's request or result — the same rule
    the D12 rollout gate already proved. A bump here would refuse every launcher
    that pins ``kMissionRuntimeRpcContract``."""

    assert serve_rpc.manifest()["contract"] == 1
    assert serve_rpc.RPC_CONTRACT_VERSION == 1


def test_method_tier_fails_closed_for_a_name_nothing_declared() -> None:
    """Unreachable through ``method()``; the fallback for a registry mutated by
    some other path answers ``console``, because the only safe answer to "nobody
    said" is the strongest tier."""

    assert serve_rpc.method_tier("runtime.nothing.declared") == TIER_CONSOLE
