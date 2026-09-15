"""Volatility is declared ONCE per HUD field, and both consumers derive from it.

The HUD has a hashed BODY and an always-emitted volatile TAIL, and which lane a
fact rides is a contract (``turn_budget`` would re-snapshot the whole stable
block every turn; ``capability`` would go stale behind a cached ``unchanged``
delivery). The predecessor of :data:`HUD_FIELDS` was a ``_VOLATILE_HUD_KEYS``
frozenset read by the revision hash, plus a hand-written promise in the body
renderer's docstring that it would never touch those keys — declaration in one
place, enforcement in none.

These tests pin the replacement: one :class:`HudField` row per key carries
``volatile``, and BOTH the hash and the body renderer read
:func:`stable_hud_fields`. A volatile fact is therefore not merely "not rendered
into the body by convention" — it is absent from the dict the body renderer
reads.
"""

from __future__ import annotations

import types

import pytest

from agent_runtime.runtime_hud import (
    CAPABILITY_HUD_KEY,
    HUD_FIELDS,
    hud_field,
    is_volatile_hud_key,
    render_situational_hud_block,
    resolve_situational_hud,
    situational_hud_revision,
    stable_hud_fields,
    volatile_hud_keys,
)


def _instance(**overrides):
    base = dict(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="Launcher Dev",
        goal_id="goal_1",
        current_task_id=None,
        state="idle",
        mode="configured",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ── the declaration ─────────────────────────────────────────────────────────


def test_every_key_is_declared_exactly_once():
    keys = [field.key for field in HUD_FIELDS]
    assert len(keys) == len(set(keys))


def test_the_declared_volatile_set_is_exactly_the_two_tail_riders():
    """Widening this set is a product decision (a new fact that must be true
    every turn), never an incidental edit — so it is stated here."""

    assert volatile_hud_keys() == {"turn_budget", CAPABILITY_HUD_KEY}


def test_an_undeclared_key_is_treated_as_stable():
    """The safe direction. An undeclared key stays in the hash, so the worst
    case is an extra re-snapshot; defaulting the other way would silently drop a
    new fact out of the revision and let a cached body go stale."""

    assert hud_field("something_new") is None
    assert is_volatile_hud_key("something_new") is False
    assert stable_hud_fields({"something_new": 1}) == {"something_new": 1}


def test_every_key_the_resolver_can_emit_is_declared():
    """The roster cannot silently fall behind the resolver.

    A key added to ``resolve_situational_hud`` without a ``HUD_FIELDS`` row
    would default to stable — correct by accident for a stable fact, WRONG and
    silent for a volatile one.
    """

    instance = _instance()
    hud = resolve_situational_hud(
        instance,
        realm="default",
        workspace="alpha",
        roster=[instance],
        board={"queued": 1, "active": 0, "review": 0},
        turn_budget={"total_seconds": 240.0, "remaining_seconds": 100.0},
        capability={"toolsets_dropped": ["terminal"]},
    )
    undeclared = {key for key in hud if hud_field(key) is None}
    assert not undeclared, f"undeclared HUD keys: {sorted(undeclared)}"


# ── both consumers derive from the one declaration ──────────────────────────


@pytest.mark.parametrize("key", sorted(volatile_hud_keys()))
def test_a_volatile_field_never_moves_the_revision(key):
    base = {"preview": True, "lane": {"role": "dev"}}
    assert situational_hud_revision(base) == situational_hud_revision(
        {**base, key: {"changes": "every turn"}}
    )


@pytest.mark.parametrize("key", sorted(volatile_hud_keys()))
def test_a_volatile_field_is_absent_from_the_dict_the_body_renderer_reads(key):
    """Structural, not conventional. The body renderer receives
    ``stable_hud_fields(hud)``, so it CANNOT render a volatile field even if a
    later edit reaches for one by name."""

    hud = {"preview": True, "lane": {"role": "dev"}, key: {"secret": "volatile"}}
    assert key not in stable_hud_fields(hud)
    assert "volatile" not in render_situational_hud_block(hud)


def test_a_stable_field_does_move_the_revision_and_does_reach_the_body():
    """The other half of the contract: stable facts are hashed AND rendered, so
    a changed picture really does re-snapshot."""

    base = {"preview": True, "scope": {"realm": "default", "workspace": "alpha"}}
    moved = {"preview": True, "scope": {"realm": "default", "workspace": "beta"}}
    assert situational_hud_revision(base) != situational_hud_revision(moved)
    assert "beta" in render_situational_hud_block(moved)


def test_a_hud_carrying_only_volatile_fields_is_unavailable_on_both_lanes():
    """The revision and the body agree on the degenerate case.

    A HUD with nothing stable to say hashes to ``hud_unavailable`` (delivery
    drops the body entirely), so rendering a body for it would produce content
    no delivery would ever carry.
    """

    volatile_only = {key: {"x": 1} for key in volatile_hud_keys()}
    assert situational_hud_revision(volatile_only) == "hud_unavailable"
    assert render_situational_hud_block(volatile_only) == ""
