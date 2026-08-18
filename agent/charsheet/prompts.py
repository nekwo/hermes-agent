"""Prompt builders for 8-way character sheets.

Three prompt shapes, one per QA stage of the charsheet draft flow (plan H §4):

* :func:`build_turnaround_prompt` — ONE wide strip holding the same neutral
  standing pose seen from N authored camera views. This is the identity anchor:
  cross-call identity drift becomes within-image consistency, because a model
  holds a character together far better inside a single generation (§7.1).
* :func:`build_direction_view_prompt` — a square-canvas re-roll of one view
  when the operator rejects a slice, with the operator's note injected.
* :func:`build_directional_row_prompt` — an animation strip where the *action*
  varies frame to frame but the *facing* is nailed to one compass direction.

The direction language is deliberately explicit and verbose: it is free, and it
is what stops the model drawing a face on a back view (§7.4).
"""

from __future__ import annotations

import re

# --- Upstream reuse (the one intentional drift surface) ---------------------
# House policy: import upstream pieces, never copy them. `style_hint` is public
# API of the pet prompt module; `_BACKGROUND`, `_spacing_spec`, and
# `_ASSUMED_STRIP_WIDTH` are private to it and imported here on purpose — the
# chroma-key wording and the proportional-containment spacing math were tuned
# against a real provider, and a copy of them here would drift silently as
# upstream retunes. Centralized in this ONE block so an upstream rename breaks
# loudly, at import time, in a single place (plan §A-6).
from agent.pet.generate.prompts import (
    _ASSUMED_STRIP_WIDTH,
    _BACKGROUND,
    _spacing_spec,
    style_hint,
)

# Camera-view phrasing per compass direction, walking the compass RING. The ring
# order is load-bearing: `pipeline.turnaround_order` ranks each direction by its
# ring distance from the front view, so shuffling this dict silently reorders
# every turnaround. It is NOT `spec.DirectionScheme.order`, which is sheet ROW
# order (front-first, authored-first) and deliberately differs. Convention,
# fixed here and relied on by every other charsheet module: "s" is the character facing the viewer,
# "n" is the character seen from behind, "e"/"w" are clean profiles, diagonals
# are three-quarter views. "the viewer's right/left" (never the character's) is
# used throughout so the mirror map ("w" <- "e", "nw" <- "ne", "sw" <- "se")
# stays a literal horizontal flip.
VIEW_LANGUAGE: dict[str, str] = {
    "n": (
        "seen directly from behind, the back of the head and body, no face "
        "visible — no eyes, no nose, no mouth, not one facial feature anywhere"
    ),
    "ne": (
        "seen in three-quarter BACK view turned toward the viewer's right: mostly "
        "the back of the head and shoulders, at most a sliver of the far cheek or "
        "jaw, never a front-facing face"
    ),
    "e": (
        "seen in full RIGHT-facing profile, the body turned a clean 90 degrees "
        "toward the viewer's right, exactly one eye and one side of the face visible"
    ),
    "se": (
        "seen in three-quarter FRONT view turned toward the viewer's right: the "
        "near shoulder leads, both eyes still read, the far cheek angles away"
    ),
    "s": (
        "seen from directly in front, facing the viewer straight on, the whole "
        "face and chest squarely visible"
    ),
    "sw": (
        "seen in three-quarter FRONT view turned toward the viewer's left: the "
        "near shoulder leads, both eyes still read, the far cheek angles away"
    ),
    "w": (
        "seen in full LEFT-facing profile, the body turned a clean 90 degrees "
        "toward the viewer's left, exactly one eye and one side of the face "
        "visible (the mirror of the right-facing profile)"
    ),
    "nw": (
        "seen in three-quarter BACK view turned toward the viewer's left: mostly "
        "the back of the head and shoulders, at most a sliver of the far cheek or "
        "jaw, never a front-facing face"
    ),
}

# What each character-sheet state depicts. Phrased against the same sprite-gen
# failure modes the petdex STATE_ACTIONS fight (detached effects, motion lines,
# shadows) plus the one that is specific to a directional sheet: a walk row is a
# TREADMILL cycle, so the frames must not become a character sliding across the
# strip (that would defeat per-frame registration at compose time).
CHARACTER_STATE_ACTIONS: dict[str, str] = {
    "idle": (
        "a calm idle loop: quiet breathing rise-and-fall, a small blink or a "
        "gentle weight shift, no big gestures, no added effects"
    ),
    "walk": (
        "an IN-PLACE walk cycle: a full walk gait loop (contact, down, passing, "
        "up) with clear alternating leg steps and natural arm/limb swing, but the "
        "character walks on the spot like a treadmill — the body stays centered "
        "over the SAME point and does NOT travel, slide, or advance across the "
        "strip, and there are no motion lines, speed trails, dust puffs, or shadows"
    ),
}

# Same token rule as `spec.parse_states`: states are CLI-definable, so a state
# with no tuned entry above must still yield a usable row prompt.
_STATE_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


def state_action_for(state: str) -> str:
    """Action language for *state*: the tuned entry, else a contained generic loop.

    ``--states`` lets the operator define states this module has never heard of
    (``cheer:5``), and refusing them would make the advertised capability a lie.
    The generic text names the state and keeps the same anti-failure-mode fences
    as the tuned entries (in place, contained, no added effects). Invalid tokens
    still raise — they could not become row keys anyway.
    """
    key = (state or "").strip().lower()
    action = CHARACTER_STATE_ACTIONS.get(key)
    if action is not None:
        return action
    if not _STATE_TOKEN_RE.match(key):
        raise ValueError(
            f"invalid character state {state!r}: expected a lowercase token "
            "matching [a-z][a-z0-9_-]*"
        )
    return (
        f"a clear, readable '{key}' action loop: the character performs the "
        f"'{key}' motion in place with contained, repeating movement — the body "
        "stays centered over the SAME point, and there are no motion lines, "
        "speed trails, dust puffs, shadows, or detached effects"
    )

# Locks the generated poses to the attached reference instead of letting the
# model re-invent the design each call.
_IDENTITY_LOCK = (
    "Using the attached reference image as the exact same character (same "
    "species, face, colors, markings, proportions, and props), preserving the "
    "same emotional tone/mood (e.g., scary stays scary, cute stays cute), "
)

# The turnaround/re-roll pose. Only the camera moves — this is what makes the
# five authored views sliceable into a coherent set of direction references.
_NEUTRAL_POSE = (
    "POSE (identical in every view): the neutral standing rest pose — upright and "
    "symmetric, arms/limbs relaxed at the sides, feet together on the ground, any "
    "cape/accessories hanging straight and still. ONLY the camera angle changes "
    "from pose to pose; the stance itself never changes. "
)

# Refs are transparent cutouts re-composited onto flat magenta before grounding
# (plan §7.3) — say so, or "same background as the reference" has no anchor.
_SAME_BG_AS_REF = (
    "BACKGROUND: the same flat chroma-key field as the attached reference image's "
    "background — ONE identical hue behind every pose, no per-pose colour shift "
    "(the reference was re-composited onto that flat chroma field before being "
    "attached, so match it exactly). "
)

# Registration is what stops a composed sheet sliding or pulsing: the character
# must be the same size at the same baseline in every slice, and the baseline
# must stay conceptual (a drawn ground line keys into the sprite).
_REGISTRATION = (
    "REGISTRATION (critical): the character is the SAME height and SAME width in "
    "every pose, drawn at the SAME scale, centered over the SAME point, with all "
    "feet aligned to the SAME invisible horizontal baseline across the whole strip "
    "— that baseline is conceptual ONLY: draw NO ground line, floor, platform, "
    "horizon, or contact shadow beneath the feet. No pose is cropped at the strip "
    "edges. "
)

# Row-only addendum: in an animation strip everything except the acting limbs is
# supposed to hold still.
_FRAME_HOLD = (
    "Keep the body's center, size, and stance fixed frame to frame — ONLY the "
    "limbs/features the action needs may move. Capes, cloaks, bags, and scarves "
    "stay in the SAME place and shape every frame (no swinging, flowing, or "
    "drifting) unless the action itself requires it. "
)


def _known_directions() -> str:
    return ", ".join(VIEW_LANGUAGE)


def _view_of(direction: str) -> tuple[str, str]:
    """(normalized key, view language) or ValueError naming the bad input."""
    key = (direction or "").strip().lower()
    if key not in VIEW_LANGUAGE:
        raise ValueError(
            f"unknown direction {direction!r}: no camera-view language for it "
            f"(known directions: {_known_directions()})"
        )
    return key, VIEW_LANGUAGE[key]


def _operator_note(note: str) -> str:
    """The note goes LAST so it is the final instruction the model reads."""
    text = (note or "").strip()
    if not text:
        return ""
    return f" OPERATOR NOTE (must be honored): {text}"


def _strip_layout_spec(slot_count: int, *, unit: str) -> str:
    """LAYOUT + SPACING language for a strip of *slot_count* poses.

    Pixel numbers and the containment ratio come from upstream's
    :func:`_spacing_spec` so charsheet strips slice with the same extractor
    tolerances the pet strips were tuned for.
    """
    pose_px, gap_px = _spacing_spec(slot_count)
    return (
        f"LAYOUT: arrange the {slot_count} {unit} in ONE horizontal row at equal "
        "spacing, left to right, each pose centered in its own imaginary equal "
        "region. Draw NO panel borders, NO comic cells, NO boxes, NO vertical "
        "divider/gutter lines, NO grid, NO frame outlines between poses — the "
        "backdrop is one unbroken flat field behind all of them. "
        f"SPACING (critical): draw each pose at a consistent, healthy, clearly "
        f"visible size (roughly {pose_px}px wide on a {_ASSUMED_STRIP_WIDTH}px "
        "strip) — do NOT shrink it tiny — but keep its ENTIRE silhouette (cape, "
        "tail, hair, weapon, every appendage) fully INSIDE its own cell. Leave at "
        f"least {gap_px}px of empty chroma-key background between neighboring "
        "silhouettes at their closest point, and the same empty margin before the "
        "first pose and after the last. If a cape, tail, or weapon would reach "
        "into a neighbor, FOLD or angle it inward rather than letting it cross the "
        "gap. Silhouettes must NEVER touch, overlap, share a shadow, share a "
        "ground line, share motion trails, or merge into one connected shape. "
    )


def build_turnaround_prompt(
    concept: str,
    directions: tuple[str, ...],
    *,
    style: str | None = "auto",
) -> str:
    """The cardinal-direction gate: one strip, one pose, N camera views.

    *directions* is the authored subset in the order the strip is drawn (and
    therefore the order :func:`extract_strip_frames` will slice it back out) —
    turnaround convention is front to back, ``("s", "se", "e", "ne", "n")``.
    The mirrored directions are never asked for; they are derived at compose.
    """
    if not directions:
        raise ValueError("cannot build a turnaround prompt for zero directions")
    views = [_view_of(direction) for direction in directions]
    concept = (concept or "the character").strip()
    slot_count = len(views)
    # Numbered, slot-by-slot: the model reliably honours an explicit per-slot
    # list where a single "draw a turnaround" instruction wanders.
    slots = "".join(
        f"\n{index}. Pose {index} (leftmost is pose 1), direction {key.upper()}: "
        f"the character {view}."
        for index, (key, view) in enumerate(views, start=1)
    )
    return (
        f"{_IDENTITY_LOCK}"
        f"draw a single WIDE horizontal strip: {slot_count} poses of {concept} "
        "left to right, the SAME character in the SAME neutral standing pose in "
        "every pose, each pose seen from a DIFFERENT camera view. "
        f"{_NEUTRAL_POSE}"
        f"VIEWS, in strict left-to-right order ({slot_count} poses, one per view):"
        f"{slots}\n"
        "Every view must be clearly distinct from its neighbors — do not repeat a "
        "view, do not swap two views, and do not turn a back or profile view "
        "toward the viewer to show more face. "
        f"{_strip_layout_spec(slot_count, unit='poses')}"
        f"{_REGISTRATION}"
        f"{_SAME_BG_AS_REF}{_BACKGROUND}{style_hint(style)}"
    )


def build_direction_view_prompt(
    concept: str,
    direction: str,
    *,
    style: str | None = "auto",
    note: str = "",
) -> str:
    """A single square-canvas re-roll of ONE turnaround view.

    Used when the operator rejects one slice of the turnaround strip: same
    neutral pose, same identity source, one view, plus the operator's note.
    """
    key, view = _view_of(direction)
    concept = (concept or "the character").strip()
    return (
        f"{_IDENTITY_LOCK}"
        f"draw ONE single pose of {concept} on a SQUARE canvas, direction "
        f"{key.upper()}: the character {view}. "
        f"{_NEUTRAL_POSE}"
        "The character is centered, whole-body, uncropped, drawn at a healthy size "
        "with an even margin of empty background on all four sides, feet resting on "
        "an invisible conceptual baseline (draw NO ground line, platform, horizon, "
        "or contact shadow). Draw ONLY this one pose in this one view — no strip, "
        "no turnaround, no second pose, no inset or corner views, no panels. "
        f"{_SAME_BG_AS_REF}{_BACKGROUND}{style_hint(style)}{_operator_note(note)}"
    )


def build_directional_row_prompt(
    state: str,
    direction: str,
    frame_count: int,
    concept: str,
    *,
    style: str | None = "auto",
    note: str = "",
) -> str:
    """An animation row: *frame_count* poses of *state*, all held in one facing.

    Grounded on the APPROVED turnaround reference for *direction*, so the row's
    identity and its camera angle both come from something the operator already
    signed off on.
    """
    action = state_action_for(state)
    direction_key, view = _view_of(direction)
    frames = int(frame_count)
    if frames < 2:
        raise ValueError(
            f"frame_count must be at least 2 for an animation row, got {frame_count!r}"
        )
    concept = (concept or "the character").strip()
    return (
        f"{_IDENTITY_LOCK}"
        f"draw a single WIDE horizontal strip of {frames} animation frames of "
        f"{concept} showing {action}. "
        f"FACING (critical — applies to EVERY one of the {frames} frames): in every "
        f"frame the character is {view}. This is the {direction_key.upper()} facing "
        "and it is IDENTICAL in all frames: the camera angle and the direction the "
        "character faces must not drift, rotate, or turn toward the viewer as the "
        "animation progresses. Only the action moves; the facing is frozen. "
        f"{_strip_layout_spec(frames, unit='frames')}"
        f"{_REGISTRATION}{_FRAME_HOLD}"
        f"{_SAME_BG_AS_REF}{_BACKGROUND}{style_hint(style)}{_operator_note(note)}"
    )
