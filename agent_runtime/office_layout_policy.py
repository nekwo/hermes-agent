"""Where a placement goes when nobody pointed at the floor.

A drag on the Mission Office canvas carries a drop point — the operator aimed
at a spot. Every other door does not: ``harness agent create`` with no
``--pos``, ``runtime.agent.create`` with no ``position``, the launcher's
palette click-add, the agent browser's "Place in workspace". Those need a
position derived from the layout itself, and the two obvious answers are both
wrong: a fixed point stacks every placement onto the last one until the floor
is an unreadable pile, and a random point is not reproducible, so the same
gesture lands somewhere different each time and nothing can be tested.

So: a deterministic lattice scan returning the FIRST unoccupied slot. Pure —
no store, no I/O, no clock — so the decision table is unit-testable directly
and the caller decides which actor set to feed it.

THE CROSS-REPO PIN, and why this module is a PORT rather than an invention
-------------------------------------------------------------------------
The launcher shipped this policy first, as
``lib/features/mission_control/office/mission_office_placement_policy.dart``
(``MissionOfficePlacementPolicy``). Plan D2 moved the AUTHORITY here — one
policy every door shares — while the launcher keeps its copy as a PREDICTION,
because a pending chip and a staged scene node need a world position before the
create's ack comes back.

Two policies that can disagree are the defect that plan exists for, so the
constants and the scan order below are the launcher's, value for value, and the
agreement is pinned by a fixture committed byte-identical in both repos:
``tests/fixtures/office_layout/cases.json`` here,
``test/fixtures/harness_office_layout/cases.json`` there. Read that directory's
README before changing ANY constant in this file — a change lands in both repos
or in neither.

One consequence of the port worth stating at the top, because it is the shape
most likely to diverge in silence: ``Vector2`` on the launcher side stores
**32-bit** floats, so ``6.4`` round-trips there as ``6.400000095…``. The
fixture's tolerance is ``1e-4``, which is four orders of magnitude above that
and four orders below the smallest gap this lattice produces. Never assert
exact equality across the two sides.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

#: First lattice column's world X. Matches the office's own unaimed-drop row so
#: browser and CLI placements land in the band the canvas already uses.
ORIGIN_X = -5.0

#: First lattice row's world Y.
ORIGIN_Y = 6.4

#: Horizontal gap between lattice columns. Mirrors the office scene adapter's
#: agent fan-out spacing, so a placed row reads as a row rather than a clump.
COLUMN_SPACING = 1.4

#: Vertical gap between lattice rows. Deliberately WIDER than
#: :data:`COLUMN_SPACING`: agent sprites are taller than they are wide, so equal
#: spacing reads as overlapping even when the anchor points do not.
ROW_SPACING = 1.6

#: Slots per lattice row before wrapping to the next one.
COLUMNS_PER_ROW = 8

#: How close an existing item has to be for a slot to count as taken.
#:
#: Exactly half :data:`COLUMN_SPACING`: two items at the minimum separation this
#: policy produces are never both "occupying" the same slot, and an item dropped
#: anywhere between two lattice points blocks the one it is nearest.
OCCUPANCY_RADIUS = COLUMN_SPACING / 2

#: How far the DESK lattice sits diagonally off the AGENT lattice.
#:
#: The scan is folder-scoped and desks live in their own folder, so without a
#: per-lane nudge the first unaimed desk and the first unaimed agent would both
#: land on the origin slot — a cross-folder stack, which is the exact defect
#: this policy exists to retire.
DESK_LANE_OFFSET = 0.7

#: The launcher's structural default folders (``office_models.DEFAULT_FOLDERS``
#: carries the same pair for the deterministic default surface).
AGENT_FOLDER = "Agents"
DESK_FOLDER = "Desks"

#: The lane every non-desk kind scans on.
NO_LANE: tuple[float, float] = (0.0, 0.0)

Point = tuple[float, float]


def folder_for_kind(kind: Any) -> str:
    """The folder an item of ``kind`` belongs to when nobody named one.

    Mirrors the launcher's ``_defaultFolderForKind``. ``office_models
    .normalize_item_kind`` maps every unknown spelling to ``"agent"``, and so
    does this: an item kind nobody recognises is an agent, never a fourth
    folder invented at read time.
    """

    return DESK_FOLDER if str(kind or "").strip().lower() == "desk" else AGENT_FOLDER


def lane_offset_for_kind(kind: Any) -> Point:
    """The lattice nudge for ``kind`` — see :data:`DESK_LANE_OFFSET`.

    Agents own the base lattice; desks are offset diagonally off it.
    """

    return (
        (DESK_LANE_OFFSET, DESK_LANE_OFFSET)
        if str(kind or "").strip().lower() == "desk"
        else NO_LANE
    )


def slot_at(row: int, column: int, *, lane_offset: Point | None = None) -> Point:
    """The world position of ONE lattice slot.

    Exposed so tests (and any future adopter) can assert against the lattice
    rather than against magic numbers.
    """

    lane = _lane(lane_offset)
    return (
        ORIGIN_X + column * COLUMN_SPACING + lane[0],
        ORIGIN_Y - row * ROW_SPACING + lane[1],
    )


def item_folder(item: Any) -> str:
    """The folder an item is REALLY in, blank resolved to its kind's default.

    Load-bearing, and the one place the two repos could most easily disagree
    without either noticing. ``office_store._normalize_item`` persists
    ``folder=""`` for an item written without one (``_safe_folder`` of ``None``
    is the empty string), while the launcher's ``MissionOfficeSceneItem``
    substitutes the kind's default at DECODE time (``_normalizeFolder(…,
    fallback: _defaultFolderForKind(kind))``). A policy that scanned the raw
    stored string would therefore miss a blank-foldered blocker here and see it
    there — the same floor, two answers. The fixture case
    ``blank_folder_falls_back_to_kind`` is the pin.
    """

    raw = _get(item, "folder")
    normalized = " ".join(str(raw or "").split())
    return normalized or folder_for_kind(_get(item, "kind"))


def occupied_positions(
    actors: Iterable[Any], *, folder: str = AGENT_FOLDER
) -> list[Point]:
    """Positions in ``folder`` that a new placement must avoid.

    ``actors`` is an iterable of :class:`~agent_runtime.models.OfficeActor`
    (or of any object/mapping exposing ``items``) — the ACTOR is the unit
    ``OfficeStore.list_actors`` hands back, and flattening happens here so no
    caller has to know that one actor file holds several scene items.

    Every item counts, including ones the launcher is currently hiding.
    Hiding is launcher-LOCAL view state — this store has no ``hidden`` field at
    all — and an item tucked away keeps its coordinates and can be un-hidden at
    any moment, so treating it as empty floor would quietly stack a new
    placement onto one the operator merely put out of sight.
    """

    return occupied_item_positions(
        (item for actor in actors or () for item in (_get(actor, "items") or ())),
        folder=folder,
    )


def occupied_item_positions(
    items: Iterable[Any], *, folder: str = AGENT_FOLDER
) -> list[Point]:
    """:func:`occupied_positions` over already-flattened scene items."""

    wanted = " ".join(str(folder or "").split()) or AGENT_FOLDER
    positions: list[Point] = []
    for item in items or ():
        if item_folder(item) != wanted:
            continue
        point = _point(_get(item, "position"))
        if point is not None:
            positions.append(point)
    return positions


def next_free_slot(
    occupied: Iterable[Point], *, lane_offset: Point | None = None
) -> Point:
    """The first free slot on the lattice shifted by ``lane_offset``.

    Deterministic — the same occupancy always yields the same slot, so the same
    operator gesture is reproducible and testable. Guaranteed to terminate and
    to return a genuinely free slot: the scan covers ``(occupied + 1) *
    COLUMNS_PER_ROW`` candidates while at most ``occupied`` of them can be
    blocked, so by pigeonhole a free one always exists inside the scan.

    ``lane_offset`` is applied INSIDE the scan, never to the result: shifting
    the winning slot afterwards would test occupancy at a point the placement
    never lands on, and could drop the item straight onto a neighbour.
    """

    lane = _lane(lane_offset)
    taken = list(occupied or ())
    # +1 row so an empty floor still scans a full row, and so the pigeonhole
    # bound holds for any number of blockers.
    rows = len(taken) + 1
    for row in range(rows):
        for column in range(COLUMNS_PER_ROW):
            candidate = slot_at(row, column, lane_offset=lane)
            if not _is_blocked(candidate, taken):
                return candidate
    # Unreachable by the pigeonhole argument above; falling through to a slot
    # past every scanned row is still deterministic and still free, which is
    # better than returning a knowingly-occupied point.
    return slot_at(rows, 0, lane_offset=lane)


def next_free_slot_for_kind(actors: Iterable[Any], kind: Any) -> Point:
    """The first free slot for a new item of ``kind``: its own folder's scan,
    on its own lane's lattice.

    The single entry point for "this placement was not aimed at the floor".
    Both the folder scope and the lane offset are resolved here so no caller has
    to do placement arithmetic.
    """

    lane = lane_offset_for_kind(kind)
    return next_free_slot(
        occupied_positions(actors, folder=folder_for_kind(kind)), lane_offset=lane
    )


# ── internals ────────────────────────────────────────────────────────────────


def _is_blocked(candidate: Point, occupied: Sequence[Point]) -> bool:
    """Whether ``candidate`` is within :data:`OCCUPANCY_RADIUS` of anything.

    STRICTLY within, matching the launcher's ``<``: an item at exactly the
    radius does not block, which is what keeps two placements at the minimum
    separation this policy produces from each declaring the other occupied.

    That strictness is pinned at THIS predicate rather than through
    :func:`next_free_slot` — see
    ``test_an_item_exactly_at_the_occupancy_radius_does_not_block`` — because
    the predicate is the DIRECT seam for it, NOT because the lattice cannot
    reach the boundary. On this side it can.

    ONE-AXIS it cannot. A :func:`slot_at` point's coordinates (``-5.0``,
    ``6.4``) have float64 neighbours ``2**-50`` apart, so a distance measured
    along a single axis from one is an exact multiple of ``2**-50``, and
    ``float64(0.7)`` is a multiple of ``2**-53`` and of no coarser power — the
    obvious ``slot_at(0, 0) + (OCCUPANCY_RADIUS, 0)`` measures
    ``0.7000000000000002``.

    OFF-AXIS it can, and the old claim here that no lattice-shaped spelling
    could reach the boundary was FALSE and is withdrawn (S9 review,
    2026-08-27). The one-axis argument does not extend, because
    ``dx*dx + dy*dy`` is not the exact ``(m*m + n*n) * 2**-100`` that both
    squares would give: the sum is ROUNDED to float64, and the rounding can
    land it on a square whose root rounds to exactly the radius. An item at
    ``(-4.300000000000001, 6.400000029802323)`` — two ordinary float64s an
    office store can hold — probed from ``slot_at(0, 0)`` sums to
    ``0.4899999999999999``, BELOW both ``OCCUPANCY_RADIUS * OCCUPANCY_RADIUS``
    (``0.48999999999999994``) and ``0.49``, and its root is EXACTLY
    ``float64(0.7)``. So ``<`` frees that slot and ``<=`` takes it, on the real
    scan. See ``test_an_off_axis_lattice_probe_can_measure_the_radius_exactly``.

    The launcher's 32-bit width is a DIFFERENT question with a narrower answer:
    an equivalent mutant AT THE LATTICE ORIGIN, which is where every cross-repo
    fixture case sits. That fixture's README states the scope with the numbers
    so nobody re-files it as a coverage gap, and so nobody reads it as an
    impossibility.
    """

    for x, y in occupied:
        dx = candidate[0] - x
        dy = candidate[1] - y
        if (dx * dx + dy * dy) ** 0.5 < OCCUPANCY_RADIUS:
            return True
    return False


def _lane(value: Point | None) -> Point:
    if value is None:
        return NO_LANE
    return (float(value[0]), float(value[1]))


def _get(source: Any, key: str) -> Any:
    """One accessor for both shapes this module is fed.

    ``OfficeStore`` hands back dataclasses; the RPC projection and the
    cross-repo fixture hand back mappings. Reading both here keeps the callers
    from converting, and keeps a converter from becoming a second place where
    the field names could drift.
    """

    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _point(value: Any) -> Point | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError):
        return None
    if x != x or y != y or abs(x) == float("inf") or abs(y) == float("inf"):
        return None
    return (x, y)
