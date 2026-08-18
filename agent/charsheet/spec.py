"""Character-sheet geometry as data — states, directions, rows, sheet size.

The petdex generator bakes its row taxonomy into a module-level ``ROW_SPECS``
list (``agent.pet.generate.atlas``), so its layout is a compile-time fact: nine
rows, fixed order, fixed frame counts. Character sheets cannot work that way —
the same pipeline must emit an 8-way sheet, a 4-way sheet, or a sheet with an
extra non-directional state, and the *consumer* has to be told which one it got.
So the taxonomy lives in a :class:`SheetSpec` that travels with the sheet, and
the direction count is always ``len(scheme.order)``. Nothing here (and nothing
downstream) may branch on the literal 8.

Pure stdlib on purpose: this module is the one piece of the charsheet package
that the CLI, the payload builder, and the tests all touch, and it must import
with no Pillow, no ``agent_runtime`` (not in the shipped wheel — see the plan's
§0.3 packaging boundary), and no ``agent.pet`` coupling.

Row identity is the string key ``"<state>-<direction>"`` for directional rows
and the bare state name otherwise; ``-`` is therefore reserved and rejected in
state names.

The separator, the direction tokens and the row vocabulary are not ours to
choose. The contract authority is the launcher spec — EterniaLauncher
``docs/spatial/CHARACTER_8WAY_SPRITE_FORMAT_SPEC_2026-08-17.md``, sections A, C
and D — whose ``AvatarSpriteSheet._deriveDirectionSectors`` splits every row
name on its LAST hyphen and matches the tail against the eight direction
tokens. Hermes conforms to that document rather than restating it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Cell geometry, matching the petdex/Codex 192x208 cell the renderers already
# assume. Duplicated as literals rather than imported from
# ``agent.pet.constants`` so this module keeps zero coupling to the pet package
# (§3 module boundaries); a divergence would be a deliberate format change, not
# a drift, and would surface in the payload's frameW/frameH.
DEFAULT_FRAME_W = 192
DEFAULT_FRAME_H = 208

# A row is one horizontal strip generated in a single model call, and the
# extractor's pose-separation heuristics degrade badly past eight poses per
# strip; the pet atlas caps at eight columns for the same reason.
MAX_FRAMES_PER_ROW = 8

# The launcher's ``CharacterFacingSector`` vocabulary, in that enum's theta
# order. ``AvatarSpriteSheet._deriveDirectionSectors`` (launcher
# ``packages/eternia_spatial/lib/src/cosmetics/avatar_sprite_resolver.dart``)
# splits a row name on its LAST hyphen and matches the tail against exactly
# these eight tokens; a tail outside the set contributes nothing to the sheet's
# derived sector count, so an unknown token would silently downgrade a sheet.
# Row ORDER is NOT required to be this theta order — the launcher addresses
# rows by NAME — which is why ``DirectionScheme.order`` below is free to be the
# authoring order instead.
DIRECTION_TOKENS: tuple[str, ...] = ("e", "se", "s", "sw", "w", "nw", "n", "ne")

# Lowercase token: state names become row keys, filenames, and JSON keys. ``-``
# is excluded on purpose — it is the row-key separator (see the module
# docstring), and a hyphenated state name would hand the launcher's deriver a
# last segment it never meant to parse.
_STATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Marker suffix in ``--states`` meaning "this state is not directional".
_FIXED_MARKER = "fixed"


@dataclass(frozen=True)
class StateSpec:
    """One animation state: ``idle`` with 6 frames, drawn per direction."""

    name: str
    frames: int
    directional: bool


@dataclass(frozen=True)
class DirectionScheme:
    """Which compass directions a sheet carries, and which are mirror-derived.

    ``order`` is the canonical iteration order (also the row order within a
    state), ``authored`` the subset actually generated, and ``mirrored`` maps
    each derived direction to the authored direction it is a horizontal flip
    of. Every direction in ``order`` must be exactly one of the two, so a sheet
    can never contain a row with no way to produce it.
    """

    order: tuple[str, ...]
    authored: tuple[str, ...]
    mirrored: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.order:
            raise ValueError("DirectionScheme.order must not be empty")
        if len(set(self.order)) != len(self.order):
            raise ValueError(f"DirectionScheme.order has duplicates: {self.order!r}")
        if len(set(self.authored)) != len(self.authored):
            raise ValueError(
                f"DirectionScheme.authored has duplicates: {self.authored!r}"
            )
        if not self.authored:
            raise ValueError("DirectionScheme.authored must not be empty")

        order_set = set(self.order)
        authored_set = set(self.authored)

        unknown_authored = sorted(authored_set - order_set)
        if unknown_authored:
            raise ValueError(
                f"authored directions not in order: {unknown_authored} "
                f"(order={list(self.order)})"
            )

        for derived, source in sorted(self.mirrored.items()):
            if derived not in order_set:
                raise ValueError(
                    f"mirrored direction {derived!r} is not in order "
                    f"{list(self.order)}"
                )
            if derived in authored_set:
                raise ValueError(
                    f"direction {derived!r} is both authored and mirrored; "
                    "it must be exactly one"
                )
            if source not in authored_set:
                raise ValueError(
                    f"mirror source {source!r} for {derived!r} is not authored "
                    f"(authored={list(self.authored)}); a derived row can only "
                    "flip a row that is actually generated"
                )

        covered = authored_set | set(self.mirrored)
        if covered != order_set:
            missing = sorted(order_set - covered)
            raise ValueError(
                f"directions with no source: {missing}; every direction in "
                "order must be authored or mirrored"
            )

        # Last, so that every other failure keeps its own message: the tokens
        # themselves must be ones the launcher deriver can read.
        unknown_tokens = [
            direction for direction in self.order if direction not in DIRECTION_TOKENS
        ]
        if unknown_tokens:
            raise ValueError(
                f"unknown direction token(s) {unknown_tokens}; the launcher "
                "deriver only reads e se s sw w nw n ne"
            )

        # Defensive copy: the field is a plain dict for the CLI/JSON round-trip,
        # so freezing the dataclass alone would not stop a caller mutating the
        # mapping they passed in out from under a constructed scheme.
        object.__setattr__(self, "mirrored", dict(self.mirrored))

    def is_mirrored(self, direction: str) -> bool:
        return direction in self.mirrored

    def source_of(self, direction: str) -> str | None:
        """The authored direction ``direction`` is flipped from, if derived."""
        return self.mirrored.get(direction)


# Row order is front-first, authored-first, for two reasons. (1) Row 0 of the
# default character sheet is then ``idle-s``, so the launcher's degenerate
# ``clipFor`` fallback — which lands on row 0 when nothing else matches —
# reads front-facing rather than side-on (launcher spec section G, risk 7).
# (2) The authored directions lead, in the same front-to-back walk that
# ``pipeline.turnaround_order`` ranks out of ``prompts.VIEW_LANGUAGE``, so each
# state block reads 'drawn rows, then derived rows' and the authored prefix is
# exactly the launcher's ``s, se, e, ne, n``. This is deliberately NOT the theta
# order of ``DIRECTION_TOKENS``; it does not need to be, because rows are
# addressed by name.
EIGHT_WAY = DirectionScheme(
    order=("s", "se", "e", "ne", "n", "nw", "w", "sw"),
    authored=("s", "se", "e", "ne", "n"),
    mirrored={"nw": "ne", "w": "e", "sw": "se"},
)

FOUR_WAY = DirectionScheme(
    order=("s", "e", "n", "w"),
    authored=("s", "e", "n"),
    mirrored={"w": "e"},
)

_DIRECTION_SCHEMES: dict[str, DirectionScheme] = {
    "8": EIGHT_WAY,
    "4": FOUR_WAY,
}


@dataclass(frozen=True)
class RowSpec:
    """One composed sheet row: where it sits, what it shows, how it is keyed."""

    index: int
    state: str
    direction: str | None
    frames: int
    key: str


@dataclass(frozen=True)
class SheetSpec:
    """A full sheet layout: states x directions, in row order, plus geometry."""

    states: tuple[StateSpec, ...]
    scheme: DirectionScheme
    frame_w: int = DEFAULT_FRAME_W
    frame_h: int = DEFAULT_FRAME_H

    def __post_init__(self) -> None:
        if not self.states:
            raise ValueError("SheetSpec.states must not be empty")
        names = [state.name for state in self.states]
        if len(set(names)) != len(names):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate state names: {dupes}")
        for state in self.states:
            if not _STATE_NAME_RE.match(state.name):
                raise ValueError(
                    f"invalid state name {state.name!r}: expected a lowercase "
                    "token matching [a-z][a-z0-9_]*"
                )
            if not 1 <= state.frames <= MAX_FRAMES_PER_ROW:
                raise ValueError(
                    f"state {state.name!r} has {state.frames} frames; "
                    f"expected 1..{MAX_FRAMES_PER_ROW}"
                )
        if self.frame_w < 1 or self.frame_h < 1:
            raise ValueError(
                f"frame size must be positive, got {self.frame_w}x{self.frame_h}"
            )

    def rows(self) -> list[RowSpec]:
        """Every row in composed order: state-major, directions in scheme order."""
        rows: list[RowSpec] = []
        for state in self.states:
            directions: tuple[str | None, ...]
            directions = self.scheme.order if state.directional else (None,)
            for direction in directions:
                rows.append(
                    RowSpec(
                        index=len(rows),
                        state=state.name,
                        direction=direction,
                        frames=state.frames,
                        key=row_key(state.name, direction),
                    )
                )
        return rows

    def columns(self) -> int:
        """Sheet width in cells — the widest row; shorter rows tail off transparent."""
        return max(row.frames for row in self.rows())

    def sheet_size(self) -> tuple[int, int]:
        rows = self.rows()
        columns = max(row.frames for row in rows)
        return (columns * self.frame_w, len(rows) * self.frame_h)

    def authored_rows(self) -> list[RowSpec]:
        """Rows a generator must actually draw (non-directional or authored view)."""
        authored = set(self.scheme.authored)
        return [
            row
            for row in self.rows()
            if row.direction is None or row.direction in authored
        ]

    def mirrored_rows(self) -> list[tuple[RowSpec, RowSpec]]:
        """``(derived, source)`` pairs — each derived row and the row it flips."""
        by_key = {row.key: row for row in self.rows()}
        pairs: list[tuple[RowSpec, RowSpec]] = []
        for row in self.rows():
            if row.direction is None:
                continue
            source_direction = self.scheme.source_of(row.direction)
            if source_direction is None:
                continue
            pairs.append((row, by_key[row_key(row.state, source_direction)]))
        return pairs

    def row_by_key(self, key: str) -> RowSpec:
        for row in self.rows():
            if row.key == key:
                return row
        raise ValueError(f"no such row key {key!r}")


def row_key(state: str, direction: str | None) -> str:
    """``"walk-ne"`` for a directional row, ``"cheer"`` when non-directional."""
    return state if direction is None else f"{state}-{direction}"


CHAR8 = SheetSpec(
    states=(StateSpec("idle", 6, True), StateSpec("walk", 8, True)),
    scheme=EIGHT_WAY,
)


def parse_states(text: str) -> tuple[StateSpec, ...]:
    """Parse ``--states``: ``"idle:6,walk:8,cheer:5:fixed"`` (``fixed`` = one row)."""
    if not text or not text.strip():
        raise ValueError("--states is empty; expected e.g. 'idle:6,walk:8'")

    states: list[StateSpec] = []
    seen: set[str] = set()
    for raw in text.split(","):
        entry = raw.strip()
        if not entry:
            raise ValueError(f"empty state entry in {text!r} (stray comma?)")

        parts = [part.strip() for part in entry.split(":")]
        if len(parts) not in (2, 3):
            raise ValueError(
                f"malformed state entry {entry!r}: expected "
                f"'name:frames' or 'name:frames:{_FIXED_MARKER}'"
            )

        name, frames_text = parts[0], parts[1]
        directional = True
        if len(parts) == 3:
            if parts[2] != _FIXED_MARKER:
                raise ValueError(
                    f"unknown state flag {parts[2]!r} in {entry!r}: "
                    f"only {_FIXED_MARKER!r} is supported"
                )
            directional = False

        if "-" in name:
            raise ValueError(
                f"state name {name!r} may not contain '-': it separates state "
                "from direction in row keys"
            )
        if not _STATE_NAME_RE.match(name):
            raise ValueError(
                f"invalid state name {name!r} in {entry!r}: expected a "
                "lowercase token matching [a-z][a-z0-9_]*"
            )
        if name in seen:
            raise ValueError(f"duplicate state {name!r} in {text!r}")

        try:
            frames = int(frames_text)
        except ValueError:
            raise ValueError(
                f"frame count {frames_text!r} for state {name!r} is not an integer"
            ) from None
        if not 1 <= frames <= MAX_FRAMES_PER_ROW:
            raise ValueError(
                f"frame count {frames} for state {name!r} out of range; "
                f"expected 1..{MAX_FRAMES_PER_ROW}"
            )

        seen.add(name)
        states.append(StateSpec(name=name, frames=frames, directional=directional))

    return tuple(states)


def parse_directions(text: str) -> DirectionScheme:
    """Parse ``--directions``: ``"8"`` or ``"4"``."""
    if text is None:
        raise ValueError("--directions is missing; expected '8' or '4'")
    key = str(text).strip()
    scheme = _DIRECTION_SCHEMES.get(key)
    if scheme is None:
        raise ValueError(
            f"unknown direction scheme {text!r}; expected one of "
            f"{sorted(_DIRECTION_SCHEMES)}"
        )
    return scheme
