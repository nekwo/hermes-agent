"""Character-sheet geometry as data — states, directions, rows, sheet size.

The petdex generator bakes its row taxonomy into a module-level ``ROW_SPECS``
list (``agent.pet.generate.atlas``), so its layout is a compile-time fact: nine
rows, fixed order, fixed frame counts. Character sheets cannot work that way —
the same pipeline must emit an 8-way sheet, a 4-way sheet, or a sheet with an
extra non-directional state, and the *consumer* has to be told which one it got.
So the taxonomy lives in a :class:`SheetSpec` that travels with the sheet, and
the direction count is always ``len(scheme.order)``. Nothing here (and nothing
downstream) may branch on the literal 8.

A sheet composes the AUTHORED directions only; the mirrored ones are never baked
into it (launcher ADR 0024 ruling 3-B, 2026-08-18 — until then hermes baked them
and every sheet cost ~60% more decoded RAM). :meth:`SheetSpec.rows` is therefore
state-major over ``scheme.authored``, so ``CHAR8`` is ten rows and 1536x2080.
The derived directions live on as scheme knowledge (``scheme.mirrored``,
:meth:`SheetSpec.mirrored_rows`) that the CONSUMER flips at draw time: the
launcher's candidate chains try the exact row and then its mirror, and
``_deriveDirectionSectors`` mirror-closes its coverage set, so an authored-only
sheet still resolves all eight sectors.

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

# The floor a DECLARED state must clear, and the floor that actually binds.
# A one-frame row is representable — nothing in :meth:`SheetSpec.rows`, the
# compose step or the sheet geometry objects to it — but nothing can DRAW one:
# :func:`agent.charsheet.prompts.build_directional_row_prompt` refuses
# ``frame_count < 2`` for every row, directional or fixed.
#
# Those two floors sat in two modules holding two different numbers until
# 2026-08-25, and the gap was a live trap rather than a tidiness complaint:
# ``start --states idle:1`` built a draft, spent the base anchor and three
# direction generations, and only refused at ``rows`` — four generations late,
# by which point no verb could change ``--states``. So the number lives here
# and is enforced where a state is DECLARED (:func:`parse_states`, the one door
# for both ``start --states`` and ``add-state --state``), and the prompt builder
# reads this constant instead of spelling its own.
#
# Deliberately NOT raised on :class:`StateSpec` / :class:`SheetSpec`. Those are
# also the DESERIALIZERS (``draft.spec_from_dict``) for every ``draft.json`` and
# ``character.json`` already on disk — including the ``idle:1`` draft the trap
# produced. Refusing such a spec at load would not repair that draft; it would
# take `characters list` down with it.
#
# The mechanism, MEASURED 2026-08-25 at the chokepoint that applies it — an
# earlier wording of this comment named a different one, read off an ``except``
# clause, and it understated the damage. ``CharacterDraft.list_drafts`` does
# swallow an unreadable draft with a log warning, but that swallow never fires
# for a bad spec: ``CharacterDraft.load`` reads JSON only, and
# ``CharacterDraft.spec`` is a property computed on ACCESS, so ``list_drafts``
# returns the bad draft happily. The raise lands one level up, in
# ``hermes_cli.harness._characters_draft_summary`` (``spec = draft.spec``),
# inside ``_cmd_characters_list``'s own ``except _CHARACTERS_EXPECTED`` — which
# answers ``{"ok": false, "error": …}`` and exit 2 for the WHOLE verb. Over a
# home holding one good draft and one ``idle:1`` draft, raising here returned
# ``ok=false`` and ZERO drafts: the good draft vanishes with the bad one. The
# conclusion is unchanged and stronger than it was written.
#
# What the two floors mean, stated once: the SPEC says what a sheet can hold,
# ``parse_states`` says what an operator may ask us to draw.
MIN_FRAMES_PER_ROW = 2

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

    ``order`` is the full direction vocabulary the sheet can be addressed in and
    the canonical iteration order. ``authored`` is the subset actually generated
    and — since ruling 3-B — the subset that becomes ROWS, in ``authored``
    order. ``mirrored`` maps each derived direction to the authored direction it
    is a horizontal flip of; a derived direction has no row of its own and is
    produced by the consumer at draw time. Every direction in ``order`` must be
    exactly one of the two, so no direction is displayable with no way to
    produce it.
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


# Direction order is front-first, authored-first, for two reasons. (1) Row 0 of
# the default character sheet is then ``idle-s``, so the launcher's degenerate
# ``clipFor`` fallback — which lands on row 0 when nothing else matches —
# reads front-facing rather than side-on (launcher spec section G, risk 7).
# (2) The authored directions lead, in the same front-to-back walk that
# ``pipeline.turnaround_order`` ranks out of ``prompts.VIEW_LANGUAGE``, so the
# rows a directional state contributes ARE the launcher's ``s, se, e, ne, n`` in
# that order, and the prompt lane and the row lane can never disagree. Since
# ruling 3-B the mirrored tail of ``order`` (``nw, w, sw``) contributes no rows
# at all; it stays because it is the vocabulary the CONSUMER resolves. This is
# deliberately NOT the theta order of ``DIRECTION_TOKENS``; it does not need to
# be, because rows are addressed by name.
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
    """A sheet layout: states x AUTHORED directions, in row order, plus geometry."""

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
        """Every composed row: state-major, AUTHORED directions in scheme order.

        The mirrored directions are absent by construction — no sheet this
        package composes contains them (ruling 3-B). ``CHAR8`` is therefore ten
        rows at 1536x2080, not sixteen at 1536x3328.
        """
        rows: list[RowSpec] = []
        for state in self.states:
            directions: tuple[str | None, ...]
            directions = self.scheme.authored if state.directional else (None,)
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
        """Rows a generator must actually draw — since ruling 3-B, every row.

        Kept because it is the question the draft/QA lane and the CLI's
        ``authoredRows`` field actually ask ("which rows must I draw?"), but
        DELEGATED rather than re-derived: a second, separately filtered row list
        is precisely how two row lists would drift apart again the next time
        someone edits :meth:`rows`.
        """
        return self.rows()

    def mirrored_rows(self) -> list[tuple[str, RowSpec]]:
        """``(derived key, source row)`` — the flips the CONSUMER draws.

        The derived half is a KEY, not a :class:`RowSpec`, and that is exactly
        what ruling 3-B means: the sheet composes authored rows only, so a
        mirrored direction has no row — it has a NAME the runtime resolves by
        flipping the composed source row (launcher SPEC section C: every
        candidate chain tries the exact row, then its mirror). Returning a
        ``RowSpec`` would mean inventing an ``index`` into a sheet that does not
        contain it.

        Ordered by ``scheme.order`` within each directional state, so the pairs
        are stable however the mirror map was written down.
        """
        pairs: list[tuple[str, RowSpec]] = []
        for state in self.states:
            if not state.directional:
                continue
            for direction in self.scheme.order:
                source_direction = self.scheme.source_of(direction)
                if source_direction is None:
                    continue
                pairs.append(
                    (
                        row_key(state.name, direction),
                        self.row_by_key(row_key(state.name, source_direction)),
                    )
                )
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


def parse_states(
    text: str, *, flag: str = "--states", example: str = "idle:6,walk:8"
) -> tuple[StateSpec, ...]:
    """Parse ``--states``: ``"idle:6,walk:8,cheer:5:fixed"`` (``fixed`` = one row).

    The ONE door for operator-declared states — ``characters start --states`` and
    ``characters add-state --state`` both come through here, which is why the
    generation floor (:data:`MIN_FRAMES_PER_ROW`) is enforced at this point and
    not at each caller.

    *flag* and *example* exist because the two doors are spelled differently and
    an empty value is the one refusal that can only name the flag. Answering
    ``characters add-state --state ''`` with *"--states is empty; expected e.g.
    'idle:6,walk:8'"* named a flag that verb does not have and illustrated it
    with a two-state list that same verb refuses one check later — measured
    2026-08-25. Every other refusal here quotes the offending TEXT, which is the
    caller's own, so this is the only message that needs to know.
    """
    if not text or not text.strip():
        raise ValueError(f"{flag} is empty; expected e.g. {example!r}")

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
        if not MIN_FRAMES_PER_ROW <= frames <= MAX_FRAMES_PER_ROW:
            raise ValueError(
                f"frame count {frames} for state {name!r} out of range; "
                f"expected {MIN_FRAMES_PER_ROW}..{MAX_FRAMES_PER_ROW} — a row "
                f"below {MIN_FRAMES_PER_ROW} frames is not an animation and no "
                "prompt can draw one"
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
