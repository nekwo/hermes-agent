"""The two scope pointers never durably straddle realms.

Operator ruling, 2026-09-01 — **newest explicit gesture wins**. There are two
explicit gestures (``activate_workspace``, ``activate_realm``), they are ordered
against each other by the ``issued_at`` basis the launcher stamps once, and
after ANY interleaving both pointers land in the newer gesture's realm while the
older verb answers ``superseded``. The reconcile's own write is not a gesture.

The hole this closes, in the shape it was filed (Mission Control queue,
"``activate_realm`` hands its reconcile a basis it never checked against the
workspace pointer"): a realm switch's reconcile carries the REALM intent's
basis, so a newer explicit workspace selection refuses it — and until
2026-09-01 that refusal was DISCARDED. The realm pointer sat in the realm the
older gesture asked for, the workspace pointer in the newer one's, both verbs
answered ``applied: true``, and nothing on the tree ever ran again to notice.

**Why the stamps in this file are in 2099.** ``_resolve_activation_write``
stamps ``now()`` when a caller presents no basis, and a wall-clock ``now()``
beats any stamp in the past — so a mutant that dropped the healing write's basis
entirely would keep passing against 2026 stamps for the wrong reason. Stamps
strictly in the future make "no basis" and "the wrong basis" both LOSE, which is
what puts the heal's choice of basis under test rather than under assumption.
"""

from __future__ import annotations

import pytest

from agent_runtime.events import EventLog
from agent_runtime.scope_activation import (
    RECONCILE_KEPT,
    activate_realm,
    activate_workspace,
    reconcile_active_workspace_to_realm,
)
from agent_runtime.store import RealmStore, WorkspaceStore


def _stamp(second: int) -> str:
    return f"2099-01-01T00:00:{second:02d}+00:00"


#: T0 < T1 < T1_5 < T2 < T3, all strictly after any wall clock this suite runs
#: on. Every write in this file presents one: an unstamped write is stamped
#: ``now()`` by the store, which against these would read as ANCIENT and be
#: refused — a trap worth naming rather than discovering.
T0, T1, T1_5, T2, T3 = (_stamp(second) for second in (0, 10, 15, 20, 30))


@pytest.fixture()
def scope(isolate_agent_runtime_root):
    """Two realms; realm A holds TWO workspaces.

    The second one in A is what makes order A observable: a workspace selection
    inside the realm the pointer already holds does not move the realm pointer,
    so the realm pointer keeps an OLDER basis than the workspace pointer — which
    is the only arrangement in which a late realm switch can win the realm CAS
    and then lose the reconcile.
    """

    realms = RealmStore()
    workspaces = WorkspaceStore()
    realm_a = realms.create(name="Realm A")
    realm_b = realms.create(name="Realm B")
    return {
        "realm_a": realm_a,
        "realm_b": realm_b,
        "ws_a1": workspaces.create(name="WS A1", realm_id=realm_a.id),
        "ws_a2": workspaces.create(name="WS A2", realm_id=realm_a.id),
        "ws_b": workspaces.create(name="WS B", realm_id=realm_b.id),
    }


def _pointers() -> tuple[str | None, str | None]:
    return WorkspaceStore().active_id(), RealmStore().active_id()


# ── the two race orders ──────────────────────────────────────────────────────


def test_order_a_a_late_realm_switch_loses_the_scope_to_a_newer_workspace_selection(scope):
    """The filed hole, driven end to end.

    The realm pointer is parked at T0, an explicit workspace selection inside
    that same realm lands at T2, and the realm switch to B arrives late carrying
    T1. It wins the realm CAS (T1 > T0) and then loses the reconcile (T1 < T2) —
    the exact interleaving that used to straddle.
    """

    RealmStore().set_active(scope["realm_a"].id, issued_at=T0)
    assert activate_workspace(scope["ws_a2"].id, issued_at=T2)["applied"] is True

    row = activate_realm(scope["realm_b"].id, issued_at=T1)

    # Both pointers in the NEWER gesture's realm, and the loser says so.
    assert _pointers() == (scope["ws_a2"].id, scope["realm_a"].id)
    assert row["applied"] is False
    assert row["reason"] == "superseded"
    assert row["superseded"] is True
    assert row["id"] == scope["realm_a"].id
    assert row["requested_realm_id"] == scope["realm_b"].id


def test_order_b_a_workspace_selection_after_a_realm_switch_pulls_the_realm_with_it(scope):
    """The other order: the realm switch applies in full first — reconcile and
    all — and the newer workspace selection lands after it, pointing into
    another realm. Nothing would reconcile again, so the workspace door carries
    the follow arm."""

    assert activate_realm(scope["realm_b"].id, issued_at=T1)["applied"] is True
    assert _pointers() == (scope["ws_b"].id, scope["realm_b"].id)

    row = activate_workspace(scope["ws_a1"].id, issued_at=T2)

    assert row["applied"] is True
    assert _pointers() == (scope["ws_a1"].id, scope["realm_a"].id)


# ── the basis each healing write carries ─────────────────────────────────────


def test_the_healed_realm_pointer_carries_the_winning_gestures_basis(scope):
    """Not just "the pointer moved" — it moved under the WINNING gesture's
    stamp. An intent issued between the loser (T1) and the winner (T2) must
    still lose, because the workspace gesture at T2 owns the whole scope. A heal
    that re-parked under the realm intent's own basis, or under none, would let
    T1_5 back in and straddle the pointers a second time."""

    RealmStore().set_active(scope["realm_a"].id, issued_at=T0)
    activate_workspace(scope["ws_a2"].id, issued_at=T2)
    activate_realm(scope["realm_b"].id, issued_at=T1)

    later_but_still_older = activate_realm(scope["realm_b"].id, issued_at=T1_5)

    assert later_but_still_older["applied"] is False
    assert later_but_still_older["reason"] == "superseded"
    assert _pointers() == (scope["ws_a2"].id, scope["realm_a"].id)


def test_the_followed_realm_pointer_carries_the_selecting_gestures_basis(scope):
    """The follow arm's twin claim, for the same reason and with the same
    probe."""

    activate_realm(scope["realm_b"].id, issued_at=T1)
    activate_workspace(scope["ws_a1"].id, issued_at=T2)

    assert activate_realm(scope["realm_b"].id, issued_at=T1_5)["applied"] is False
    assert _pointers() == (scope["ws_a1"].id, scope["realm_a"].id)


def test_the_follow_arm_never_overrides_a_strictly_newer_realm_intent(scope):
    """The follow is a CONSEQUENCE of the gesture, never an override of the
    ordering: it writes through ``RealmStore.set_active`` and that
    compare-and-set still holds.

    Built by hand from two store writes, because no pair of GESTURES reaches
    this state — a realm gesture always reconciles the workspace pointer under
    its own basis, so a workspace gesture older than the realm pointer's basis
    loses the workspace pointer first and never gets as far as the follow. The
    claim is narrow and worth pinning anyway: the follow does not reach around
    the CAS to force the pointer.
    """

    RealmStore().set_active(scope["realm_b"].id, issued_at=T3)
    WorkspaceStore().set_active(scope["ws_b"].id, issued_at=T0)

    assert activate_workspace(scope["ws_a1"].id, issued_at=T2)["applied"] is True

    assert RealmStore().active_id() == scope["realm_b"].id


# ── the arms that must NOT fire ──────────────────────────────────────────────


def test_a_cleared_workspace_selection_does_not_move_the_realm_pointer(scope):
    """``workspace use --clear`` has no realm to follow — there is no workspace
    to read one off. A follow arm that fired here would have to invent a realm.
    """

    activate_realm(scope["realm_b"].id, issued_at=T1)

    row = activate_workspace(None, issued_at=T2)

    assert row == {"id": None, "name": None, "applied": True}
    assert _pointers() == (None, scope["realm_b"].id)


def test_an_ordinary_switch_inside_one_realm_leaves_the_realm_pointer_alone(scope):
    """The non-race path, and the reason the follow arm reads the pointer before
    writing: without that check every ordinary workspace switch would emit a
    second ``realm.activated`` for a pointer that never changed."""

    activate_realm(scope["realm_a"].id, issued_at=T0)
    log = EventLog()
    before = len(log.tail(200))

    assert activate_workspace(scope["ws_a2"].id, issued_at=T2)["applied"] is True

    emitted = [event.type for event in log.tail(200)][before:]
    assert "realm.activated" not in emitted
    assert _pointers() == (scope["ws_a2"].id, scope["realm_a"].id)


def test_a_declined_workspace_selection_moves_neither_pointer(scope):
    """A gesture that lost the workspace pointer must not move the realm one:
    the follow arm is on the APPLIED path only."""

    activate_workspace(scope["ws_b"].id, issued_at=T2)
    before = _pointers()

    row = activate_workspace(scope["ws_a1"].id, issued_at=T1)

    assert row["applied"] is False
    assert row["reason"] == "superseded"
    assert _pointers() == before


def test_a_realm_switch_that_loses_its_own_pointer_never_reaches_the_heal(scope):
    """The first arm still short-circuits: a realm intent older than the one
    that owns the realm pointer is refused before any reconcile runs, so the
    heal cannot fire on a switch that never happened."""

    activate_realm(scope["realm_b"].id, issued_at=T2)
    before = _pointers()

    row = activate_realm(scope["realm_a"].id, issued_at=T1)

    assert row["applied"] is False
    assert row["reason"] == "superseded"
    assert row["id"] == scope["realm_b"].id
    assert _pointers() == before


# ── the reconcile's own arms, unchanged by the ruling ────────────────────────


def test_the_reconcile_keeps_a_workspace_that_already_belongs_and_says_so(scope):
    """The early return, now with an answer. ``RECONCILE_KEPT`` is what stops a
    kept pointer being read as a refusal — a heal on this arm would drag the
    realm pointer back out of the realm just switched to."""

    WorkspaceStore().set_active(scope["ws_a1"].id, issued_at=T0)

    outcome = reconcile_active_workspace_to_realm(
        RealmStore().get(scope["realm_a"].id), issued_at=T1
    )

    assert outcome == {
        "workspace_id": scope["ws_a1"].id,
        "applied": False,
        "reason": RECONCILE_KEPT,
    }
    assert WorkspaceStore().active_id() == scope["ws_a1"].id


def test_a_realm_switch_whose_workspace_already_belongs_still_answers_applied(scope):
    """The kept arm, read from the CALLER: the most ordinary realm switch there
    is — one whose active workspace already belongs to the realm being asked for
    — still answers ``applied: true`` and moves nothing.

    Written expecting to prove more than it does. The mutation
    ``RECONCILE_KEPT`` → ``"superseded"`` SURVIVES this, and correctly: a kept
    workspace belongs to the target realm by definition, so the heal's own
    ``winning_realm_id == requested_realm_id`` check turns the mislabelling into
    a wasted read. The registered claim on this arm is the one that IS a
    guarantee — the early return keeps the operator's chosen workspace instead of
    letting the ladder re-derive one — and it lives on
    :func:`test_the_reconcile_keeps_a_workspace_that_already_belongs_and_says_so`.
    """

    activate_workspace(scope["ws_a1"].id, issued_at=T0)
    RealmStore().set_active(scope["realm_b"].id, issued_at=T1)

    row = activate_realm(scope["realm_a"].id, issued_at=T2)

    assert row["applied"] is True
    assert row["id"] == scope["realm_a"].id
    assert _pointers() == (scope["ws_a1"].id, scope["realm_a"].id)


def test_the_reconcile_prefers_the_declared_default_then_configured_then_listing(scope):
    """The fallback ladder, all three rungs, exercised by moving only the realm
    row that names them."""

    workspaces = WorkspaceStore()
    realms = RealmStore()
    clock = iter(_stamp(second) for second in range(40, 60))

    def _reconcile_from(scope_out_of_realm_a, realm) -> str | None:
        workspaces.set_active(scope_out_of_realm_a, issued_at=next(clock))
        assert reconcile_active_workspace_to_realm(realm, issued_at=next(clock))["applied"] is True
        return workspaces.active_id()

    # Rung 3 — nothing declared: id order among realm A's unarchived workspaces.
    by_listing = _reconcile_from(scope["ws_b"].id, realms.get(scope["realm_a"].id))
    assert by_listing in {scope["ws_a1"].id, scope["ws_a2"].id}
    other = scope["ws_a2"].id if by_listing == scope["ws_a1"].id else scope["ws_a1"].id

    # Rung 2 — a configured order that disagrees with the listing order wins.
    configured = realms.get(scope["realm_a"].id)
    configured.workspace_ids = [other, by_listing]
    assert _reconcile_from(scope["ws_b"].id, configured) == other

    # Rung 1 — a declared default beats the configured order in turn.
    declared = realms.get(scope["realm_a"].id)
    declared.workspace_ids = [other, by_listing]
    declared.default_workspace_id = by_listing
    assert _reconcile_from(scope["ws_b"].id, declared) == by_listing


def test_the_reconcile_clears_the_pointer_when_the_realm_has_no_workspace(scope):
    empty = RealmStore().create(name="Realm Empty")
    WorkspaceStore().set_active(scope["ws_a1"].id, issued_at=T0)

    outcome = reconcile_active_workspace_to_realm(empty, issued_at=T1)

    assert outcome == {"workspace_id": None, "applied": True}
    assert WorkspaceStore().active_id() is None


def test_a_realm_switch_into_an_empty_realm_clears_rather_than_heals(scope):
    """The heal's cleared-pointer arm: a reconcile that CLEARED the workspace
    pointer applied, so there is no winning workspace and no straddle — the
    realm switch keeps its ``applied: true`` answer."""

    empty = RealmStore().create(name="Realm Empty")
    activate_workspace(scope["ws_a1"].id, issued_at=T0)

    row = activate_realm(empty.id, issued_at=T1)

    assert row["applied"] is True
    assert _pointers() == (None, empty.id)
