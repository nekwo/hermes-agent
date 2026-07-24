"""Pure resolution table for the workspace-scoping authority.

``agent_runtime.workspace_scope`` decides which persona placements are
addressable from a given workspace. The contract: a ``None`` workspace pointer
is runtime-global (visible everywhere), a non-None pointer belongs to exactly
one workspace, and a ``None`` scope disables filtering. No I/O, so the table is
asserted directly against value objects.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_runtime import workspace_scope


def _inst(workspace_id="__unset__", **overrides):
    base = {"id": "personainst_x", "persona_id": "dev"}
    base.update(overrides)
    if workspace_id != "__unset__":
        base["workspace_id"] = workspace_id
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# instance_in_scope: the (candidate_ws, scope_ws) -> in/out table              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "candidate_ws, scope_ws, expected",
    [
        # Runtime-global candidate is addressable from every workspace.
        (None, "ws_a", True),
        # Same workspace: in scope.
        ("ws_a", "ws_a", True),
        # Different workspace: out of scope.
        ("ws_b", "ws_a", False),
        # No active workspace at all: everything is in scope (no hiding).
        ("ws_a", None, True),
        (None, None, True),
        # A blank/whitespace pointer collapses to runtime-global.
        ("", "ws_a", True),
        ("   ", "ws_a", True),
        # Surrounding whitespace does not defeat an otherwise-matching id.
        (" ws_a ", "ws_a", True),
    ],
)
def test_instance_in_scope_table(candidate_ws, scope_ws, expected):
    assert workspace_scope.instance_in_scope(candidate_ws, scope_ws) is expected


# --------------------------------------------------------------------------- #
# effective_workspace_id: own pointer wins, else active fallback              #
# --------------------------------------------------------------------------- #


def test_effective_workspace_id_prefers_own_pointer():
    inst = _inst(workspace_id="ws_b")
    assert workspace_scope.effective_workspace_id(inst, active_workspace_id="ws_a") == "ws_b"


def test_effective_workspace_id_falls_back_to_active_when_global():
    inst = _inst(workspace_id=None)
    assert workspace_scope.effective_workspace_id(inst, active_workspace_id="ws_a") == "ws_a"


def test_effective_workspace_id_none_when_neither_set():
    inst = _inst(workspace_id=None)
    assert workspace_scope.effective_workspace_id(inst, active_workspace_id=None) is None


def test_effective_workspace_id_tolerates_missing_attribute():
    # A value object without a workspace_id attribute is treated as global.
    inst = _inst()  # no workspace_id key at all
    assert workspace_scope.effective_workspace_id(inst, active_workspace_id="ws_a") == "ws_a"


def test_effective_workspace_id_blank_pointer_falls_back():
    inst = _inst(workspace_id="   ")
    assert workspace_scope.effective_workspace_id(inst, active_workspace_id="ws_a") == "ws_a"


# --------------------------------------------------------------------------- #
# scope_roster: filter to scope, preserve input order, never mutate           #
# --------------------------------------------------------------------------- #


def test_scope_roster_keeps_scope_and_global_in_input_order():
    a = _inst(workspace_id="ws_a", id="a")
    b = _inst(workspace_id="ws_b", id="b")
    c = _inst(workspace_id=None, id="c")
    d = _inst(workspace_id="ws_a", id="d")
    scoped = workspace_scope.scope_roster([a, b, c, d], scope_workspace_id="ws_a")
    # ws_b row dropped; ws_a + global rows kept in original order.
    assert [i.id for i in scoped] == ["a", "c", "d"]


def test_scope_roster_none_scope_keeps_everything():
    a = _inst(workspace_id="ws_a", id="a")
    b = _inst(workspace_id="ws_b", id="b")
    scoped = workspace_scope.scope_roster([a, b], scope_workspace_id=None)
    assert [i.id for i in scoped] == ["a", "b"]


def test_scope_roster_empty_input_is_empty():
    assert workspace_scope.scope_roster(None, scope_workspace_id="ws_a") == []
    assert workspace_scope.scope_roster([], scope_workspace_id="ws_a") == []


def test_scope_roster_all_out_of_scope_yields_empty():
    b = _inst(workspace_id="ws_b", id="b")
    assert workspace_scope.scope_roster([b], scope_workspace_id="ws_a") == []


# --------------------------------------------------------------------------- #
# shadow_canonical_by_placement: drop a persona's canonical row when a         #
# PLACEMENT of it is present; keep it when alone; never touch identity/order.  #
# The canonical discriminator is a passed predicate (here a plain flag), so    #
# the table is pure and independent of the harness row model.                 #
# --------------------------------------------------------------------------- #


def _srow(id, persona_id, *, canonical=False, workspace_id="__unset__"):
    base = {"id": id, "persona_id": persona_id, "canonical": canonical}
    if workspace_id != "__unset__":
        base["workspace_id"] = workspace_id
    return SimpleNamespace(**base)


def _is_canonical(inst):
    return bool(getattr(inst, "canonical", False))


def test_shadow_drops_canonical_when_a_placement_is_present():
    canonical = _srow("personainst_dev", "dev", canonical=True)
    placement = _srow("personainst_dev_agent_2", "dev")
    out = workspace_scope.shadow_canonical_by_placement(
        [canonical, placement], is_canonical=_is_canonical
    )
    assert [i.id for i in out] == ["personainst_dev_agent_2"]


def test_shadow_keeps_canonical_when_it_stands_alone():
    canonical = _srow("personainst_dev", "dev", canonical=True)
    out = workspace_scope.shadow_canonical_by_placement([canonical], is_canonical=_is_canonical)
    assert [i.id for i in out] == ["personainst_dev"]


def test_shadow_keeps_two_placements_when_there_is_no_canonical():
    p1 = _srow("personainst_dev_agent_2", "dev")
    p2 = _srow("personainst_dev_agent_3", "dev")
    out = workspace_scope.shadow_canonical_by_placement([p1, p2], is_canonical=_is_canonical)
    assert [i.id for i in out] == ["personainst_dev_agent_2", "personainst_dev_agent_3"]


def test_shadow_multi_persona_mix_preserves_input_order():
    # dev: canonical + one placement → placement only.
    # qa: canonical alone → kept.
    # ops: two placements, no canonical → both kept.
    dev_c = _srow("personainst_dev", "dev", canonical=True)
    dev_p = _srow("personainst_dev_agent_2", "dev")
    qa_c = _srow("personainst_qa", "qa", canonical=True)
    ops_p1 = _srow("personainst_ops_a", "ops")
    ops_p2 = _srow("personainst_ops_b", "ops")
    rows = [dev_c, ops_p1, qa_c, dev_p, ops_p2]  # deliberately interleaved
    out = workspace_scope.shadow_canonical_by_placement(rows, is_canonical=_is_canonical)
    # Only dev's canonical is dropped; survivors keep their original order.
    assert [i.id for i in out] == [
        "personainst_ops_a",
        "personainst_qa",
        "personainst_dev_agent_2",
        "personainst_ops_b",
    ]


def test_shadow_never_mutates_input():
    canonical = _srow("personainst_dev", "dev", canonical=True)
    placement = _srow("personainst_dev_agent_2", "dev")
    rows = [canonical, placement]
    workspace_scope.shadow_canonical_by_placement(rows, is_canonical=_is_canonical)
    assert rows == [canonical, placement]


def test_shadow_empty_input_is_empty():
    assert workspace_scope.shadow_canonical_by_placement(None, is_canonical=_is_canonical) == []
    assert workspace_scope.shadow_canonical_by_placement([], is_canonical=_is_canonical) == []


# --------------------------------------------------------------------------- #
# addressable_roster: scope FIRST, then shadow — so an out-of-scope placement  #
# cannot shadow a canonical row that is still reachable from here.             #
# --------------------------------------------------------------------------- #


def test_addressable_roster_scopes_before_shadowing():
    # dev: global canonical + an in-scope (ws_a) placement → canonical shadowed.
    # qa:  global canonical + an OUT-of-scope (ws_b) placement → the placement is
    #      filtered by scope first, so nothing shadows qa's canonical (it stays).
    dev_c = _srow("personainst_dev", "dev", canonical=True, workspace_id=None)
    dev_a = _srow("personainst_dev_agent_2", "dev", workspace_id="ws_a")
    qa_c = _srow("personainst_qa", "qa", canonical=True, workspace_id=None)
    qa_b = _srow("personainst_qa_agent_2", "qa", workspace_id="ws_b")
    out = workspace_scope.addressable_roster(
        [dev_c, dev_a, qa_c, qa_b],
        scope_workspace_id="ws_a",
        is_canonical=_is_canonical,
    )
    assert [i.id for i in out] == ["personainst_dev_agent_2", "personainst_qa"]


def test_addressable_roster_none_scope_shadows_across_everything():
    # No active workspace (scope None): every row is in scope, so a placement
    # shadows its canonical exactly as the bare shadow does.
    dev_c = _srow("personainst_dev", "dev", canonical=True, workspace_id="ws_a")
    dev_p = _srow("personainst_dev_agent_2", "dev", workspace_id="ws_b")
    out = workspace_scope.addressable_roster(
        [dev_c, dev_p], scope_workspace_id=None, is_canonical=_is_canonical
    )
    assert [i.id for i in out] == ["personainst_dev_agent_2"]
