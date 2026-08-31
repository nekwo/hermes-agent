"""The unaimed-placement lattice, and its cross-repo agreement (plan S1/D2).

Two halves, deliberately separated:

* the POLICY's own properties — determinism, wrapping, folder scope, lane
  offset, termination — proved here against the lattice rather than against
  magic numbers;
* the AGREEMENT with the launcher's ``MissionOfficePlacementPolicy``, driven by
  ``tests/fixtures/office_layout/cases.json``, which the launcher commits
  byte-identically and drives through its own test.

The second half is the one that matters for D2: hermes owns the policy and the
launcher keeps a prediction, so a case file that only one repo could satisfy
would put the two back in the silent-disagreement state the plan exists to end.
The fixture's README states the update rule.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from agent_runtime import office_layout_policy as policy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "office_layout"

#: The launcher's mirror, named here so a reader of either side finds the other.
LAUNCHER_FIXTURE = "EterniaLauncher/test/fixtures/harness_office_layout/cases.json"


def _fixture() -> dict:
    return json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))


# ── the cross-repo pin ───────────────────────────────────────────────────────


def test_manifest_pins_fixture_bytes():
    """KILLING MUTATION: edit either repo's ``cases.json`` without re-hashing
    both manifests and this reds. That is the whole point — the two copies are
    only a pin while a change to one is a change to both.
    """

    manifest = (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8")
    entries = dict(
        reversed(line.split("  ", 1)) for line in manifest.strip().splitlines()
    )
    assert set(entries) == {"cases.json"}
    for name, digest in entries.items():
        actual = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
        assert actual == digest, (
            f"{name} drifted from MANIFEST.sha256 — office-layout cases change "
            "only in a cross-stack change that lands hermes + the launcher "
            f"together (mirror: {LAUNCHER_FIXTURE})"
        )


def test_the_fixture_pins_every_constant_this_module_spends():
    """A case file whose constants had drifted from the code would still pass
    every case — it would simply be describing a different lattice. This is the
    join: the numbers the launcher mirrors are the numbers this module uses.

    KILLING MUTATION: change ``ORIGIN_Y`` by 0.1 here (or in the Dart) and this
    reds naming the constant, before any case has to be read.
    """

    constants = _fixture()["constants"]
    assert constants == {
        "origin_x": policy.ORIGIN_X,
        "origin_y": policy.ORIGIN_Y,
        "column_spacing": policy.COLUMN_SPACING,
        "row_spacing": policy.ROW_SPACING,
        "rows_per_column": policy.ROWS_PER_COLUMN,
        "occupancy_radius": policy.OCCUPANCY_RADIUS,
        "desk_lane_offset": policy.DESK_LANE_OFFSET,
    }


def test_the_case_list_is_not_empty_and_names_the_shapes_S1_requires():
    """Anti-vacuity. Every case assertion below is a ``for`` over the file, so a
    file that failed to load — or a schema change that emptied ``cases`` —
    would pass them all. The named set is S1's done-when list; a fixture that
    silently lost one of them must red rather than run shorter.
    """

    doc = _fixture()
    assert doc["schema_version"] == 1
    names = [case["name"] for case in doc["cases"]]
    assert len(names) == len(set(names)), names
    assert {
        "empty_floor",
        "occupied_origin_steps_up_one_full_row",
        "full_first_column_wraps",
        "hidden_item_blocks",
        "desk_lane_offset",
        "off_lattice_item_blocks_its_nearest_slot",
        "boundary_item_at_exact_radius",
        "pigeonhole_200_occupied",
        "blank_folder_falls_back_to_kind",
    } <= set(names), names


@pytest.mark.parametrize("case", _fixture()["cases"], ids=lambda c: c["name"])
def test_every_fixture_case_resolves_to_its_slot(case):
    """THE cross-repo assertion, one test per case so a failure names the shape.

    Compared within the fixture's own tolerance rather than exactly: the
    launcher stores positions as 32-bit floats, so ``6.4`` is ``6.400000095…``
    there and a 25-row scan accumulates about ``1e-6``. ``1e-4`` sits four
    orders above that and four below the smallest gap this lattice produces.
    """

    tolerance = _fixture()["tolerance"]

    # The kind→lane mapping is part of the agreement, not a launcher detail: a
    # repo that resolved a desk onto the agent lattice would answer every case
    # correctly and still stack the first unaimed desk under the first agent.
    assert policy.folder_for_kind(case["kind"]) == case["folder"]
    assert list(policy.lane_offset_for_kind(case["kind"])) == case["lane_offset"]

    occupied = policy.occupied_item_positions(case["items"], folder=case["folder"])
    slot = policy.next_free_slot(occupied, lane_offset=tuple(case["lane_offset"]))

    expected = case["expected"]
    assert math.isclose(slot[0], expected[0], abs_tol=tolerance), (case["name"], slot)
    assert math.isclose(slot[1], expected[1], abs_tol=tolerance), (case["name"], slot)


@pytest.mark.parametrize("case", _fixture()["cases"], ids=lambda c: c["name"])
def test_every_fixture_slot_is_genuinely_free(case):
    """The fixture's ``expected`` is data, and data can be wrong. This is the
    independent check on it: whatever slot the file names, nothing in the
    case's own floor may be sitting within the occupancy radius of it.

    Without this, a case authored with a mistyped expectation would be
    satisfied by both repos agreeing on the wrong answer.
    """

    occupied = policy.occupied_item_positions(case["items"], folder=case["folder"])
    x, y = case["expected"]
    for ox, oy in occupied:
        assert math.hypot(x - ox, y - oy) >= policy.OCCUPANCY_RADIUS, case["name"]


# ── the policy's own properties ──────────────────────────────────────────────


def test_an_empty_floor_lands_on_the_lattice_origin():
    assert policy.next_free_slot([]) == policy.slot_at(0, 0)


def test_the_scan_is_deterministic():
    occupied = [policy.slot_at(0, 0), policy.slot_at(0, 2)]
    first = policy.next_free_slot(occupied)
    for _ in range(5):
        assert policy.next_free_slot(occupied) == first


def test_the_scan_fills_the_gap_rather_than_appending():
    occupied = [policy.slot_at(0, 0), policy.slot_at(2, 0)]
    assert policy.next_free_slot(occupied) == policy.slot_at(1, 0)


def test_forty_sequential_placements_never_collide():
    """The property that matters at the surface: pressing "place" repeatedly
    must not pile agents onto one spot, which is exactly what a fixed point or
    a viewport centre would do.
    """

    placed: list[policy.Point] = []
    for _ in range(40):
        placed.append(policy.next_free_slot(placed))
    for i, a in enumerate(placed):
        for b in placed[i + 1 :]:
            assert math.hypot(a[0] - b[0], a[1] - b[1]) >= policy.OCCUPANCY_RADIUS, (
                a,
                b,
            )


def test_the_lane_offset_is_applied_inside_the_scan_not_to_the_result():
    """Shifting the winning slot AFTERWARDS would test occupancy at a point the
    placement never lands on. Proof: a desk sitting on the desk lane's origin
    must push the next desk along the LANE, not off a base-lattice answer.
    """

    lane = policy.lane_offset_for_kind("desk")
    occupied = [policy.slot_at(0, 0, lane_offset=lane)]
    assert policy.next_free_slot(occupied, lane_offset=lane) == policy.slot_at(
        1, 0, lane_offset=lane
    )


def test_a_blocked_first_column_still_terminates_and_returns_something_free():
    """Every lattice slot of column 0 blocked by off-lattice drags — the scan
    must still return, and return a genuinely free point.
    """

    occupied = [
        (policy.slot_at(r, 0)[0] + 0.05, policy.slot_at(r, 0)[1] + 0.05)
        for r in range(policy.ROWS_PER_COLUMN)
    ]
    x, y = policy.next_free_slot(occupied)
    for ox, oy in occupied:
        assert math.hypot(x - ox, y - oy) >= policy.OCCUPANCY_RADIUS


def test_an_item_exactly_at_the_occupancy_radius_does_not_block():
    """STRICTLY within, matching the launcher's ``<``. If the boundary blocked,
    two placements at the minimum separation this policy produces would each
    declare the other occupied and the scan would drift forever.

    Asked of the PREDICATE rather than through :func:`next_free_slot`, and that
    is the whole repair. This test used to place the item at ``slot_at(0, 0) +
    (0, OCCUPANCY_RADIUS)`` and believe it was on the boundary; ``6.4 + 0.7``
    is ``7.1000000000000005``, so the distance it actually measured was
    ``0.7000000000000002`` — OUTSIDE the radius, where ``<`` and ``<=`` answer
    the same thing. It named a guarantee it could not fail on, and ``<`` →
    ``<=`` survived the whole file (measured 2026-08-27).

    No ONE-AXIS spelling fixes that. Every candidate the scan tests is a
    ``slot_at`` point, whose coordinates (``-5.0``, ``6.4``) have float64
    neighbours ``2**-50`` apart, so a distance measured along a single axis
    from one is an exact multiple of ``2**-50``; ``OCCUPANCY_RADIUS`` is
    ``float64(0.7)``, a multiple of ``2**-53`` and of no coarser power.

    An OFF-AXIS one does. This docstring used to say that no lattice-shaped
    spelling could reach the boundary; that was FALSE and is withdrawn (S9
    review, 2026-08-27). The one-axis argument does not extend, because
    ``dx*dx + dy*dy`` is not the exact ``(m*m + n*n) * 2**-100`` the two
    squares would give — the sum is ROUNDED. An item at
    ``(-4.300000000000001, 6.400000029802323)`` probed from ``slot_at(0, 0)``
    sums to ``0.4899999999999999``, below both ``OCCUPANCY_RADIUS ** 2``
    (``0.48999999999999994``) and ``0.49``, and its root is EXACTLY
    ``float64(0.7)`` — pinned in
    ``test_an_off_axis_lattice_probe_can_measure_the_radius_exactly`` below.

    Asking the PREDICATE is still the right seam, for the reason above the
    paragraph rather than for the impossibility: it is the direct one, and it
    does not make the guarantee hostage to an operator's floor happening to
    hold such a point. The launcher's 32-bit width is a separate, narrower
    question — an equivalent mutant AT THE LATTICE ORIGIN — which is why the
    cross-repo fixture's ``boundary_item_at_exact_radius`` pins the RADIUS (to
    one float32 ulp) rather than the comparison. See that fixture's README.

    KILLING MUTATION: ``<`` → ``<=`` in ``_is_blocked`` — registered as claim
    ``s9-occupancy-predicate-is-strict`` in ``tests/mutation_claims.json``.
    """

    radius = policy.OCCUPANCY_RADIUS
    assert policy._is_blocked((0.0, 0.0), [(radius, 0.0)]) is False

    # Anti-vacuity: one float64 step INSIDE the radius does block, so the
    # assertion above is a boundary and not a predicate that never fires.
    inside = math.nextafter(radius, 0.0)
    assert policy._is_blocked((0.0, 0.0), [(inside, 0.0)]) is True


def test_an_off_axis_lattice_probe_can_measure_the_radius_exactly():
    """The lattice CAN reach the boundary on float64 — just not along one axis.

    Withdraws the "no lattice-shaped spelling can reach it" claim that
    ``_is_blocked``, the test above, and both copies of the cross-repo
    fixture's README carried until the S9 review (2026-08-27). The sum of the
    two squares is rounded, so it can land on a value whose root rounds to
    exactly ``float64(0.7)`` even though neither square is exact and the sum is
    strictly below ``OCCUPANCY_RADIUS ** 2``.

    This case cannot live in ``cases.json``: the item's coordinates are not
    float32-representable, and the launcher would parse them into a different
    floor. It is hermes-only on purpose.
    """

    candidate = policy.slot_at(0, 0)
    # RE-DERIVED 2026-08-27 when the lattice moved to the world origin. The old
    # witness was spelled against ORIGIN (-5.0, 6.4), where the subtraction
    # itself rounded; at the origin the differences are exact, so the witness
    # had to be searched for again. It still lands on the SAME squared value,
    # which is the point: this is a property of the radius, not of where the
    # lattice happens to sit.
    item = (0.6999999999999998, 1e-08)
    dx = candidate[0] - item[0]
    dy = candidate[1] - item[1]
    squared = dx * dx + dy * dy

    # Strictly INSIDE the radius by the squares, and exactly ON it by the root.
    assert squared == 0.4899999999999999
    assert squared < policy.OCCUPANCY_RADIUS * policy.OCCUPANCY_RADIUS
    assert policy.OCCUPANCY_RADIUS * policy.OCCUPANCY_RADIUS == 0.48999999999999994
    assert squared < 0.49
    assert squared**0.5 == 0.7 == policy.OCCUPANCY_RADIUS

    # So the boundary is on the grid the SCAN reaches, and `<` frees the slot.
    assert policy._is_blocked(candidate, [item]) is False
    assert policy.next_free_slot([item]) == candidate

    # The one-axis spelling — and moving the lattice to the world origin
    # CHANGED what it demonstrates, which is worth stating rather than quietly
    # re-asserting. Against ORIGIN (-5.0, 6.4) this distance came out as
    # 0.7000000000000002: a rounding artifact of subtracting two numbers of that
    # magnitude, which landed the point just OUTSIDE the radius, where `<` and
    # `<=` agree and the strictness did not matter.
    #
    # At the origin the subtraction is exact, so the obvious spelling is now
    # EXACTLY ON the radius. `<` frees the slot and `<=` would block it, so the
    # strictness of that comparison is now load-bearing on the plainest spelling
    # anyone would write — where before it only mattered off-axis. That is the
    # sharper reason `cases.json` pins a boundary case at all.
    on_axis = (candidate[0] + policy.OCCUPANCY_RADIUS, candidate[1])
    on_axis_distance = abs(candidate[0] - on_axis[0])
    assert on_axis_distance == 0.7
    assert on_axis_distance == policy.OCCUPANCY_RADIUS
    assert not on_axis_distance < policy.OCCUPANCY_RADIUS
    assert policy._is_blocked(candidate, [on_axis]) is False


def test_rows_are_spaced_further_apart_than_columns():
    assert policy.ROW_SPACING > policy.COLUMN_SPACING


def test_the_occupancy_radius_cannot_swallow_an_adjacent_slot():
    assert policy.OCCUPANCY_RADIUS <= policy.COLUMN_SPACING / 2


# ── the store-shaped entry point ─────────────────────────────────────────────


def test_occupied_positions_flattens_actors_the_way_the_store_hands_them_over():
    """``OfficeStore.scan_actors`` returns ACTORS, and one actor file holds
    several scene items (an agent placement plus its coupled desk). The
    flattening lives in the policy so no caller has to know that — and so no
    caller can flatten it differently.
    """

    from agent_runtime.models import OfficeActor, OfficeItem

    actors = [
        OfficeActor(
            actor_key="personainst_qa",
            workspace_id="ws",
            persona_id="qa",
            items=[
                OfficeItem(
                    item_id="personainst_qa",
                    persona_id="qa",
                    kind="agent",
                    position=[0.0, 0.0],
                    folder="Agents",
                ),
                OfficeItem(
                    item_id="desk-personainst_qa",
                    persona_id="qa",
                    kind="desk",
                    position=[0.7, 0.7],
                    folder="Desks",
                ),
            ],
        )
    ]

    desk_lane = policy.lane_offset_for_kind("desk")
    assert policy.occupied_positions(actors, folder="Agents") == [policy.slot_at(0, 0)]
    assert policy.occupied_positions(actors, folder="Desks") == [
        policy.slot_at(0, 0, lane_offset=desk_lane)
    ]
    assert policy.next_free_slot_for_kind(actors, "agent") == policy.slot_at(1, 0)
    assert policy.next_free_slot_for_kind(actors, "desk") == policy.slot_at(
        1, 0, lane_offset=desk_lane
    )


def test_a_stored_blank_folder_is_scanned_as_its_kinds_default():
    """``office_store._normalize_item`` persists ``folder: ""`` for an item
    written without one. The launcher's decoder substitutes the kind's default
    at READ time, so a policy scanning the raw string would miss this blocker
    here and see it there.
    """

    from agent_runtime.models import OfficeActor, OfficeItem

    actors = [
        OfficeActor(
            actor_key="personainst_qa",
            workspace_id="ws",
            persona_id="qa",
            items=[
                OfficeItem(
                    item_id="legacy",
                    persona_id="qa",
                    kind="agent",
                    position=[0.0, 0.0],
                    folder="",
                )
            ],
        )
    ]

    assert policy.occupied_positions(actors, folder="Agents") == [policy.slot_at(0, 0)]
    assert policy.occupied_positions(actors, folder="Desks") == []


def test_an_unreadable_position_is_skipped_rather_than_crashing_the_scan():
    """A corrupt item must not take the whole placement down with it: the slot
    it would have blocked is simply free, which is the same answer the office
    projection already gives for an actor file it cannot decode.
    """

    items = [
        {"kind": "agent", "folder": "Agents", "position": None},
        {"kind": "agent", "folder": "Agents", "position": ["x", "y"]},
        {"kind": "agent", "folder": "Agents", "position": [float("nan"), 0.0]},
        {"kind": "agent", "folder": "Agents", "position": [-5.0, 6.4]},
    ]
    assert policy.occupied_item_positions(items, folder="Agents") == [(-5.0, 6.4)]
