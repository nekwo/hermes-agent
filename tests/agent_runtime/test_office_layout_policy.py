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
        "columns_per_row": policy.COLUMNS_PER_ROW,
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
        "full_first_row_wraps",
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
    occupied = [policy.slot_at(0, 0), policy.slot_at(0, 2)]
    assert policy.next_free_slot(occupied) == policy.slot_at(0, 1)


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
        0, 1, lane_offset=lane
    )


def test_a_blocked_first_row_still_terminates_and_returns_something_free():
    """Every lattice slot of row 0 blocked by off-lattice drags — the scan must
    still return, and return a genuinely free point.
    """

    occupied = [
        (policy.slot_at(0, c)[0] + 0.05, policy.slot_at(0, c)[1] + 0.05)
        for c in range(policy.COLUMNS_PER_ROW)
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

    No lattice-shaped spelling fixes that. Every candidate the scan tests is a
    ``slot_at`` point, whose coordinates (``-5.0``, ``6.4``) have float64
    neighbours ``2**-50`` apart, so every distance measured from one is an
    exact multiple of ``2**-50``; ``OCCUPANCY_RADIUS`` is ``float64(0.7)``, a
    multiple of ``2**-53`` and of no coarser power. The boundary is simply not
    on the grid the scan can reach — and at the launcher's 32-bit width it is
    further off it still, which is why the cross-repo fixture's
    ``boundary_item_at_exact_radius`` pins the RADIUS (to one float32 ulp) and
    says so rather than pretending to pin the comparison. See that fixture's
    README.

    KILLING MUTATION: ``<`` → ``<=`` in ``_is_blocked`` — registered as claim
    ``s9-occupancy-predicate-is-strict`` in ``tests/mutation_claims.json``.
    """

    radius = policy.OCCUPANCY_RADIUS
    assert policy._is_blocked((0.0, 0.0), [(radius, 0.0)]) is False

    # Anti-vacuity: one float64 step INSIDE the radius does block, so the
    # assertion above is a boundary and not a predicate that never fires.
    inside = math.nextafter(radius, 0.0)
    assert policy._is_blocked((0.0, 0.0), [(inside, 0.0)]) is True


def test_rows_are_spaced_further_apart_than_columns():
    assert policy.ROW_SPACING > policy.COLUMN_SPACING


def test_the_occupancy_radius_cannot_swallow_an_adjacent_slot():
    assert policy.OCCUPANCY_RADIUS <= policy.COLUMN_SPACING / 2


# ── the store-shaped entry point ─────────────────────────────────────────────


def test_occupied_positions_flattens_actors_the_way_the_store_hands_them_over():
    """``OfficeStore.list_actors`` returns ACTORS, and one actor file holds
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
                    position=[-5.0, 6.4],
                    folder="Agents",
                ),
                OfficeItem(
                    item_id="desk-personainst_qa",
                    persona_id="qa",
                    kind="desk",
                    position=[-4.3, 7.1],
                    folder="Desks",
                ),
            ],
        )
    ]

    assert policy.occupied_positions(actors, folder="Agents") == [(-5.0, 6.4)]
    assert policy.occupied_positions(actors, folder="Desks") == [(-4.3, 7.1)]
    assert policy.next_free_slot_for_kind(actors, "agent") == policy.slot_at(0, 1)
    assert policy.next_free_slot_for_kind(actors, "desk") == policy.slot_at(
        0, 1, lane_offset=policy.lane_offset_for_kind("desk")
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
                    position=[-5.0, 6.4],
                    folder="",
                )
            ],
        )
    ]

    assert policy.occupied_positions(actors, folder="Agents") == [(-5.0, 6.4)]
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
