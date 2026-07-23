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
