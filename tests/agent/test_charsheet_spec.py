"""Invariants of the charsheet data model — not snapshots of the current default.

Every assertion here derives its expectation from the spec objects themselves
(``scheme.order``, ``state.frames``), so a sheet that grows a state, changes a
frame count, or switches to 4-way still has to satisfy the same rules. In
particular nothing tests for the literal 8: the 4-way scheme runs through the
same code paths and is asserted with the same helpers.
"""

from __future__ import annotations

import pytest

from agent.charsheet.spec import (
    CHAR8,
    DEFAULT_FRAME_H,
    DEFAULT_FRAME_W,
    DIRECTION_TOKENS,
    EIGHT_WAY,
    FOUR_WAY,
    MAX_FRAMES_PER_ROW,
    DirectionScheme,
    SheetSpec,
    StateSpec,
    parse_directions,
    parse_states,
    row_key,
)

SCHEMES = [EIGHT_WAY, FOUR_WAY]


def spec_for(scheme: DirectionScheme, states=None, **geometry) -> SheetSpec:
    return SheetSpec(
        states=tuple(states or (StateSpec("idle", 6, True), StateSpec("walk", 8, True))),
        scheme=scheme,
        **geometry,
    )


# ───────────────────────────── mirror closure ─────────────────────────────


@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_direction_is_authored_or_mirrored_and_never_both(scheme):
    authored = set(scheme.authored)
    mirrored = set(scheme.mirrored)

    assert authored | mirrored == set(scheme.order)
    assert authored & mirrored == set()
    assert len(scheme.order) == len(authored) + len(mirrored)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_every_mirror_source_is_itself_authored(scheme):
    for derived, source in scheme.mirrored.items():
        assert source in scheme.authored, f"{derived} flips {source}, which is not drawn"
        assert scheme.source_of(derived) == source
        assert scheme.is_mirrored(derived) is True
    for direction in scheme.authored:
        assert scheme.is_mirrored(direction) is False
        assert scheme.source_of(direction) is None


def test_scheme_rejects_a_mirror_of_a_direction_nobody_draws():
    with pytest.raises(ValueError, match="not authored"):
        DirectionScheme(order=("n", "s"), authored=("n",), mirrored={"s": "s"})


def test_scheme_rejects_a_direction_that_is_both_authored_and_mirrored():
    with pytest.raises(ValueError, match="both authored and mirrored"):
        DirectionScheme(order=("e", "w"), authored=("e", "w"), mirrored={"w": "e"})


def test_scheme_rejects_a_direction_with_no_source_at_all():
    with pytest.raises(ValueError, match="no source"):
        DirectionScheme(order=("n", "e", "s"), authored=("n", "e"), mirrored={})


def test_scheme_rejects_a_mirrored_direction_outside_the_order():
    with pytest.raises(ValueError, match="not in order"):
        DirectionScheme(order=("e",), authored=("e",), mirrored={"w": "e"})


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"order": (), "authored": ()}, "must not be empty"),
        ({"order": ("e", "e"), "authored": ("e",)}, "duplicates"),
        ({"order": ("e",), "authored": ("e", "e")}, "duplicates"),
        ({"order": ("e",), "authored": ()}, "must not be empty"),
        ({"order": ("e",), "authored": ("q",), "mirrored": {}}, "not in order"),
    ],
)
def test_scheme_rejects_malformed_direction_sets(kwargs, message):
    with pytest.raises(ValueError, match=message):
        DirectionScheme(**kwargs)


def test_scheme_keeps_its_own_copy_of_the_mirror_map():
    mutable = {"w": "e"}
    scheme = DirectionScheme(order=("e", "w"), authored=("e",), mirrored=mutable)
    mutable["nonsense"] = "e"
    assert scheme.mirrored == {"w": "e"}


# ───────────────────────────── row ordering ─────────────────────────────


@pytest.mark.parametrize("scheme", SCHEMES)
def test_rows_are_state_major_with_the_authored_directions_in_scheme_order(scheme):
    states = (StateSpec("idle", 4, True), StateSpec("cheer", 3, False), StateSpec("walk", 6, True))
    spec = spec_for(scheme, states)

    expected = [
        (state.name, direction)
        for state in states
        for direction in (scheme.authored if state.directional else (None,))
    ]
    assert [(row.state, row.direction) for row in spec.rows()] == expected


@pytest.mark.parametrize("scheme", SCHEMES)
def test_row_indices_are_dense_and_keys_are_unique(scheme):
    rows = spec_for(scheme, (StateSpec("idle", 2, True), StateSpec("cheer", 2, False))).rows()

    assert [row.index for row in rows] == list(range(len(rows)))
    assert len({row.key for row in rows}) == len(rows)


@pytest.mark.parametrize("scheme", SCHEMES)
def test_a_non_directional_state_contributes_exactly_one_bare_keyed_row(scheme):
    spec = spec_for(scheme, (StateSpec("cheer", 5, False),))
    (row,) = spec.rows()

    assert row.direction is None
    assert row.key == "cheer" == row_key("cheer", None)
    assert row.frames == 5


@pytest.mark.parametrize("scheme", SCHEMES)
def test_row_count_follows_the_authored_direction_count_of_each_state(scheme):
    states = (StateSpec("idle", 2, True), StateSpec("cheer", 2, False))
    spec = spec_for(scheme, states)

    assert len(spec.rows()) == sum(
        len(scheme.authored) if state.directional else 1 for state in states
    )


@pytest.mark.parametrize("scheme", SCHEMES)
def test_row_keys_carry_state_and_direction(scheme):
    spec = spec_for(scheme, (StateSpec("walk", 3, True),))

    for row in spec.rows():
        assert row.key == f"walk-{row.direction}" == row_key(row.state, row.direction)
        assert spec.row_by_key(row.key) == row


def test_row_by_key_rejects_a_key_the_sheet_does_not_have():
    with pytest.raises(ValueError, match="no such row key"):
        CHAR8.row_by_key("walk-up")


# ───────────────────────── authored vs mirrored rows ─────────────────────────


@pytest.mark.parametrize("scheme", SCHEMES)
def test_authored_rows_are_exactly_the_rows_a_generator_must_draw(scheme):
    spec = spec_for(scheme, (StateSpec("walk", 3, True), StateSpec("cheer", 2, False)))

    # Since ruling 3-B every composed row IS an authored row, so the two lists
    # are one list — and `authored_rows` delegates rather than re-filtering, so
    # they cannot drift apart the next time `rows` is edited.
    assert spec.authored_rows() == spec.rows()
    assert len(spec.authored_rows()) == len(scheme.authored) + 1
    assert all(
        row.direction is None or row.direction in scheme.authored
        for row in spec.rows()
    )


@pytest.mark.parametrize("scheme", SCHEMES)
def test_a_mirrored_pair_names_a_row_the_sheet_lacks_and_a_row_it_has(scheme):
    """The derived half is a KEY: since 3-B no sheet carries that row.

    The pair is what the CONSUMER needs — "flip this composed row to draw that
    name" — and it is the only place the mirror map is still projected onto row
    names now that nothing composes them.
    """
    spec = spec_for(scheme, (StateSpec("walk", 3, True), StateSpec("cheer", 2, False)))
    row_keys = {row.key for row in spec.rows()}
    pairs = spec.mirrored_rows()

    assert [key for key, _source in pairs] == [
        f"walk-{direction}" for direction in scheme.order if direction in scheme.mirrored
    ]
    assert len(pairs) == len(scheme.mirrored)  # one directional state in this spec
    for derived_key, source in pairs:
        assert derived_key not in row_keys
        assert source.key in row_keys
        state, separator, direction = derived_key.rpartition("-")
        assert (state, separator) == (source.state, "-")
        assert scheme.source_of(direction) == source.direction
        assert source.frames == 3


@pytest.mark.parametrize("scheme", SCHEMES)
def test_no_mirrored_direction_ever_becomes_a_row(scheme):
    """Ruling 3-B in one assertion: the sheet is the authored rows, and no others."""
    spec = spec_for(scheme)
    on_the_sheet = {row.direction for row in spec.rows()}

    assert on_the_sheet == set(scheme.authored)
    assert on_the_sheet & set(scheme.mirrored) == set()
    assert [row.key for row in spec.rows()] == [row.key for row in spec.authored_rows()]
    assert len(spec.rows()) == len(spec.states) * len(scheme.authored)


# ─────────────────────────────── sheet size ───────────────────────────────


@pytest.mark.parametrize("scheme", SCHEMES)
def test_sheet_size_is_the_widest_row_by_the_row_count(scheme):
    spec = spec_for(scheme, (StateSpec("idle", 4, True), StateSpec("walk", 7, True)))
    width, height = spec.sheet_size()

    assert spec.columns() == max(state.frames for state in spec.states) == 7
    assert width == spec.columns() * spec.frame_w
    assert height == len(spec.rows()) * spec.frame_h


def test_widening_the_longest_state_widens_the_sheet_by_that_many_cells():
    narrow = spec_for(EIGHT_WAY, (StateSpec("walk", 4, True),))
    wide = spec_for(EIGHT_WAY, (StateSpec("walk", 6, True),))

    assert wide.sheet_size()[0] - narrow.sheet_size()[0] == 2 * DEFAULT_FRAME_W
    assert wide.sheet_size()[1] == narrow.sheet_size()[1]


def test_a_shorter_state_does_not_shrink_the_sheet_width():
    one_state = spec_for(EIGHT_WAY, (StateSpec("walk", 6, True),))
    with_short = spec_for(EIGHT_WAY, (StateSpec("walk", 6, True), StateSpec("cheer", 2, False)))

    assert with_short.sheet_size()[0] == one_state.sheet_size()[0]
    assert with_short.sheet_size()[1] - one_state.sheet_size()[1] == DEFAULT_FRAME_H


def test_adding_a_directional_state_adds_one_row_per_authored_direction():
    before = spec_for(FOUR_WAY, (StateSpec("walk", 3, True),))
    after = spec_for(FOUR_WAY, (StateSpec("walk", 3, True), StateSpec("idle", 3, True)))

    assert len(after.rows()) - len(before.rows()) == len(FOUR_WAY.authored)
    assert (
        after.sheet_size()[1] - before.sheet_size()[1]
        == len(FOUR_WAY.authored) * DEFAULT_FRAME_H
    )


def test_sheet_size_scales_with_a_custom_cell_geometry():
    spec = spec_for(FOUR_WAY, (StateSpec("walk", 3, True),), frame_w=64, frame_h=32)

    assert spec.sheet_size() == (3 * 64, len(spec.rows()) * 32)


def test_four_way_and_eight_way_differ_only_by_their_authored_direction_count():
    states = (StateSpec("walk", 5, True),)
    four = spec_for(FOUR_WAY, states)
    eight = spec_for(EIGHT_WAY, states)

    assert four.sheet_size()[0] == eight.sheet_size()[0]
    assert eight.sheet_size()[1] / four.sheet_size()[1] == len(EIGHT_WAY.authored) / len(
        FOUR_WAY.authored
    )


# ──────────────────────────── spec validation ────────────────────────────


def test_default_char8_sheet_is_all_directional_states_in_eight_way():
    assert CHAR8.scheme is EIGHT_WAY
    assert all(state.directional for state in CHAR8.states)
    assert len(CHAR8.rows()) == len(CHAR8.states) * len(EIGHT_WAY.authored)
    assert (CHAR8.frame_w, CHAR8.frame_h) == (DEFAULT_FRAME_W, DEFAULT_FRAME_H)


@pytest.mark.parametrize(
    "states, message",
    [
        ((), "must not be empty"),
        ((StateSpec("walk", 2, True), StateSpec("walk", 3, True)), "duplicate state names"),
        ((StateSpec("Walk", 2, True),), "invalid state name"),
        ((StateSpec("walk@e", 2, True),), "invalid state name"),
        ((StateSpec("spin-kick", 2, True),), "invalid state name"),
        ((StateSpec("walk", 0, True),), "expected 1.."),
        ((StateSpec("walk", MAX_FRAMES_PER_ROW + 1, True),), "expected 1.."),
    ],
)
def test_sheet_spec_rejects_unusable_state_lists(states, message):
    with pytest.raises(ValueError, match=message):
        SheetSpec(states=states, scheme=FOUR_WAY)


@pytest.mark.parametrize("geometry", [{"frame_w": 0}, {"frame_h": -1}])
def test_sheet_spec_rejects_a_non_positive_cell(geometry):
    with pytest.raises(ValueError, match="frame size must be positive"):
        spec_for(FOUR_WAY, (StateSpec("walk", 2, True),), **geometry)


# ─────────────────────────────── parse_states ───────────────────────────────


def test_parse_states_reads_frames_and_the_fixed_marker():
    states = parse_states("idle:6,walk:8,cheer:5:fixed")

    assert [(s.name, s.frames, s.directional) for s in states] == [
        ("idle", 6, True),
        ("walk", 8, True),
        ("cheer", 5, False),
    ]


def test_parse_states_round_trips_its_own_rendering():
    original = parse_states("idle:6,walk:8,cheer:5:fixed,jump_2:3")
    rendered = ",".join(
        f"{s.name}:{s.frames}" + ("" if s.directional else ":fixed") for s in original
    )

    assert parse_states(rendered) == original
    assert parse_states(rendered) == parse_states(f"  {rendered.replace(',', ' , ')}  ")


def test_parse_states_accepts_the_default_sheet_rendered_back():
    rendered = ",".join(f"{s.name}:{s.frames}" for s in CHAR8.states)

    assert parse_states(rendered) == CHAR8.states


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "empty"),
        ("   ", "empty"),
        ("idle:6,,walk:8", "empty state entry"),
        ("idle", "malformed state entry"),
        ("idle:6:fixed:extra", "malformed state entry"),
        ("idle:6:loose", "unknown state flag"),
        ("idle@e:6", "invalid state name"),
        ("idle-e:6", "may not contain '-'"),
        ("Idle:6", "invalid state name"),
        ("2idle:6", "invalid state name"),
        ("idle:6,idle:8", "duplicate state"),
        ("idle:six", "not an integer"),
        ("idle:0", "out of range"),
        (f"idle:{MAX_FRAMES_PER_ROW + 1}", "out of range"),
    ],
)
def test_parse_states_rejects_unusable_text(text, message):
    with pytest.raises(ValueError, match=message):
        parse_states(text)


def test_parsed_states_build_a_valid_sheet_in_either_scheme():
    states = parse_states("idle:6,walk:8,cheer:5:fixed")

    for scheme in SCHEMES:
        spec = SheetSpec(states=states, scheme=scheme)
        assert len(spec.rows()) == 2 * len(scheme.authored) + 1
        assert spec.authored_rows() == spec.rows()
        assert len(spec.mirrored_rows()) == 2 * len(scheme.mirrored)


# ───────────────────────────── parse_directions ─────────────────────────────


def test_parse_directions_maps_a_count_to_a_closed_scheme():
    assert parse_directions("8") is EIGHT_WAY
    assert parse_directions(" 4 ") is FOUR_WAY
    assert parse_directions(8) is EIGHT_WAY

    for text in ("8", "4"):
        scheme = parse_directions(text)
        assert len(scheme.order) == int(text)
        assert set(scheme.authored) | set(scheme.mirrored) == set(scheme.order)


@pytest.mark.parametrize("text", ["6", "eight", "", "  ", "0", "8way"])
def test_parse_directions_rejects_an_unknown_scheme(text):
    with pytest.raises(ValueError, match="unknown direction scheme"):
        parse_directions(text)
# ────────────────── the launcher row-key contract (C5) ──────────────────


def test_row_keys_are_the_launcher_deriver_vocabulary():
    """Every row key is one the launcher's sector deriver can actually read.

    The consumer is ``AvatarSpriteSheet._deriveDirectionSectors`` in the
    EterniaLauncher package ``eternia_spatial``
    (``lib/src/cosmetics/avatar_sprite_resolver.dart``): it splits a row name on
    its LAST hyphen and looks the tail up in an eight-token direction table. A
    key spelled with any other separator therefore contributes ZERO covered
    sectors and an 8-way sheet silently degrades to two-way at runtime — no
    error, no log, just a character that only ever faces east or west.

    The token tuple below is transcribed from the launcher spec
    (``docs/spatial/CHARACTER_8WAY_SPRITE_FORMAT_SPEC_2026-08-17.md``, §D), NOT
    imported from the module under test; the equality against
    ``DIRECTION_TOKENS`` is what makes a drift in that constant fail HERE rather
    than in the launcher.
    """
    launcher_tokens = ("e", "se", "s", "sw", "w", "nw", "n", "ne")
    assert tuple(DIRECTION_TOKENS) == launcher_tokens

    states = (
        StateSpec("idle", 6, True),
        StateSpec("walk", 8, True),
        StateSpec("cheer", 5, False),
    )
    for scheme in (EIGHT_WAY, FOUR_WAY):
        spec = spec_for(scheme, states)
        directional = [row for row in spec.rows() if row.direction is not None]
        assert directional, "the fixture must actually carry directional rows"

        for row in spec.rows():
            if row.direction is None:
                assert "-" not in row.key
                continue
            assert row.key.count("@") == 0
            state, sep, token = row.key.rpartition("-")
            assert sep == "-"
            assert state == row.state
            assert token == row.direction
            assert token in launcher_tokens


def test_char8_row_zero_is_the_front_facing_idle():
    """Row 0 is ``idle-s``, because the launcher's last-resort clip lands there.

    ``AvatarSpriteSheet.clipFor`` falls through to row 0 when nothing in a
    candidate chain matches, so row 0 decides what a degenerate lookup looks
    like on screen. Front-facing is the only defensible answer (launcher spec
    §G, risk 7), and it is a property of the ROW ORDER, not of any one sheet.
    """
    keys = [row.key for row in CHAR8.rows()]

    assert keys[0] == "idle-s"
    assert keys[:5] == ["idle-s", "idle-se", "idle-e", "idle-ne", "idle-n"]
    assert keys[5] == "walk-s"  # five rows per directional state since ruling 3-B
    assert EIGHT_WAY.authored == ("s", "se", "e", "ne", "n")
    assert FOUR_WAY.order == ("s", "e", "n", "w")


def test_a_state_name_may_not_carry_a_hyphen():
    """The hyphen belongs to the row key, so no state name may spend one.

    Closed by construction rather than by vocabulary: because the launcher
    splits on the LAST hyphen of every row, a state like ``spin-kick`` would
    make row parsing depend on which tokens happen to be in the direction table
    today. Rejecting the character outright keeps the contract decidable, and
    keeps ``<key>-<attempt>.png`` strip filenames unambiguous.
    """
    with pytest.raises(ValueError, match="may not contain '-'"):
        parse_states("walk-e:8")

    with pytest.raises(ValueError, match="invalid state name"):
        SheetSpec(states=(StateSpec("spin-kick", 2, True),), scheme=FOUR_WAY)

    with pytest.raises(ValueError, match="unknown direction token"):
        DirectionScheme(order=("s", "up"), authored=("s", "up"))
