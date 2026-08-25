"""Character sheets — multi-directional sprite sheets built on the pet pipeline.

A character sheet is the petdex atlas generalized: instead of one fixed row
taxonomy, the layout is described by a :class:`~agent.charsheet.spec.SheetSpec`
(states x directions) that is generated with the sheet and shipped alongside it,
so a consumer never has to infer the row list from the image's height the way
the pet readers do.

Only the authored directions are drawn (N, NE, E, SE, S for the 8-way default);
the rest are horizontal flips derived at compose time, the same mechanism the
pet flow uses for its left-facing run cycle.

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
