"""Character sheets — multi-directional sprite sheets built on the pet pipeline.

A character sheet is the petdex atlas generalized: instead of one fixed row
taxonomy, the layout is described by a :class:`~agent.charsheet.spec.SheetSpec`
(states x directions) that is generated with the sheet and shipped alongside it,
so a consumer never has to infer the row list from the image's height the way
the pet readers do.

Only the authored directions are drawn (S, SE, E, NE, N for the 8-way default),
and since launcher ADR 0024 ruling 3-B they are also the only ones COMPOSED: the
derived directions (NW, W, SW) have no row of their own and are horizontal flips
the CONSUMER makes at draw time. Nothing in this package mirrors anything -- an
earlier wording of this paragraph said "derived at compose time", which was true
before that ruling and has not been true since; ``pipeline.compose_draft_frames``
is the chokepoint and it has no flip in it. That asymmetry is why a row drawn as
the mirror of the direction it claims corrupts TWO directions, and why
``pipeline.detect_mirrored_art`` refuses such a sheet at validation -- unless the
operator accepts that ROW by name, which is recorded on the installed character.

This subpackage is fork-owned but lives inside the upstream ``agent`` namespace
so it ships with the plain hermes wheel (the packaging boundary rules out
``agent_runtime``); nothing here imports ``agent_runtime``, and the pixel work
imports upstream ``agent.pet.generate`` rather than editing it.

- :mod:`agent.charsheet.spec` — the data model (pure stdlib, no Pillow).
"""

from agent.charsheet.spec import (
    CHAR8,
    DEFAULT_FRAME_H,
    DEFAULT_FRAME_W,
    DIRECTION_TOKENS,
    EIGHT_WAY,
    FOUR_WAY,
    MAX_FRAMES_PER_ROW,
    MIN_FRAMES_PER_ROW,
    DirectionScheme,
    RowSpec,
    SheetSpec,
    StateSpec,
    parse_directions,
    parse_states,
    row_key,
)

__all__ = [
    "CHAR8",
    "DEFAULT_FRAME_H",
    "DEFAULT_FRAME_W",
    "DIRECTION_TOKENS",
    "EIGHT_WAY",
    "FOUR_WAY",
    "MAX_FRAMES_PER_ROW",
    "MIN_FRAMES_PER_ROW",
    "DirectionScheme",
    "RowSpec",
    "SheetSpec",
    "StateSpec",
    "parse_directions",
    "parse_states",
    "row_key",
]
