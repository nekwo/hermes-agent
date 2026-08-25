"""The charsheet pixel pipeline: turnaround → direction refs → rows → sheet.

Stage order is the operator's QA order (plan H §4): one wide turnaround strip is
sliced into the authored direction references, each approved reference grounds
that direction's animation strips, and at compose time those strips are
registered, palette-locked, packed and validated. Nothing is mirrored on the way:
a sheet carries the authored directions only (launcher ADR 0024 ruling 3-B) and
the consumer flips the other three at draw time.

Three constraints are load-bearing here:

* **One provider seam.** Every generation goes through :func:`_generate_image`.
  Nothing else in the charsheet package talks to a provider, so the whole flow
  runs offline against a fake by replacing that one function.
* **Grounding refs carry the chroma field.** The row prompts tell the model to
  reuse "the same flat chroma-key field as the attached reference"; a sliced
  turnaround cutout is transparent, so it is re-composited onto flat magenta
  before it is written (§7.3). The same treatment is applied to a re-rolled
  single view, so every direction reference is interchangeable.
* **Geometry is gated mechanically, identity is gated by a human.** A strip whose
  poses touch cannot be sliced into frames, and no operator should be asked to
  review that — such rolls are rejected and regenerated here (the pet hatch
  retry pattern), and only strips that *can* be sliced reach QA.

Composition and validation are written fresh rather than reused: upstream's
``compose_atlas``/``validate_atlas`` are welded to the module-global ``ROW_SPECS``
taxonomy, while a character sheet's row list is data that arrives with the spec.
Everything below therefore loops over ``spec.rows()`` and never imports
``ROW_SPECS``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from pathlib import Path

from agent.charsheet import prompts
from agent.charsheet.palette import DEFAULT_MAX_COLORS, build_palette, lock_to_palette
from agent.charsheet.spec import CHAR8, RowSpec, SheetSpec, row_key

# --- Upstream reuse (the ONE intentional drift surface) ----------------------
# House policy: import upstream pixel machinery, never edit or copy it. The two
# leading-underscore helpers are private to `agent.pet.generate.atlas` and are
# imported deliberately — `_fit_to_cell` is the exact cell-fit contract the pet
# renderer assumes and `_clear_transparent_rgb` is the residue rule the atlas
# validator enforces, so a local copy of either would drift silently as upstream
# retunes. `CELL_WIDTH`/`CELL_HEIGHT` come along because `_fit_to_cell` hardcodes
# that cell geometry: a spec with a different frame size must be refused rather
# than silently re-fitted to 192x208. Centralized in this ONE block so an
# upstream rename breaks loudly, at import time, in a single place (plan §A-6).
from agent.pet.generate import imagegen
from agent.pet.generate.atlas import (
    CELL_HEIGHT,
    CELL_WIDTH,
    _clear_transparent_rgb,
    _fit_to_cell,
    atlas_to_webp_bytes,
    extract_strip_frames,
    normalize_cells,
    remove_background,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAGENTA",
    "PREFIX_TURNAROUND",
    "atlas_to_webp_bytes",
    "build_sheet_palette",
    "compose_draft_frames",
    "compose_sheet",
    "frame_cell",
    "generate_direction_view",
    "generate_row_strip",
    "generate_turnaround",
    "recomposite_on_magenta",
    "row_prefix",
    "turnaround_order",
    "upscale_on_backdrop",
    "validate_sheet",
    "view_prefix",
]

# The chroma field every generated charsheet image is drawn on and every
# grounding reference is re-composited onto. Hot magenta is what the upstream
# prompt language asks for and what `remove_background`'s saturated fast path is
# tuned against.
MAGENTA: tuple[int, int, int] = (255, 0, 255)

# What a QA crop is composited onto. Near-black but not black: an image with a
# black outline still reads against it, while a hole in the art does not
# masquerade as one. Opaque by construction — the whole point of the backdrop is
# that a defect over TRANSPARENT pixels shows up as something rather than as
# nothing (the 2026-08-24 seam hunt, plan §F.2).
#
# It only shows through something that HAS transparency, which is why
# `upscale_on_backdrop` keys the chroma field out first: a row attempt off the
# provider is a full-bleed magenta field at alpha 255 everywhere, and
# compositing that over the backdrop replaces every backdrop pixel — the step
# renders, costs a full-image composite, and changes nothing.
QA_BACKDROP: tuple[int, int, int, int] = (18, 18, 22, 255)

# Two bounds with two different correct values. They were ONE number until a
# 2026-08-24 re-review, and one number could not be right for both.
#
# `MAX_THUMB_PIXELS` is the WRITE-SAFETY ceiling — what this package may put on
# disk at all, evaluated before the resize. The old ceiling was a bare
# `1 <= scale <= 8`: a count with no relationship to the source. `--scale 8` on a
# live 1536x1024 row attempt wrote 12288x8192 = 100_663_296 pixels — past
# Pillow's own `Image.MAX_IMAGE_PIXELS` bomb threshold (89_478_485), so the verb
# produced a file Pillow refuses to reopen without a `DecompressionBombWarning`,
# and RAISES for a caller running under `-W error`. The quantity a caller cares
# about is the output's pixel count; a scale factor alone can never express it,
# because the same factor is harmless on one source and a bomb on another.
#
# 16M pixels is 64 MiB decoded RGBA: under a fifth of Pillow's threshold, so a
# crop this package writes always reopens cleanly.
MAX_THUMB_PIXELS = 16_000_000

# `MAX_CARD_PIXELS` is the CARD-WEIGHT budget, and it is the bound the crop verb
# exists to serve. Launcher risk D.3 asks `thumb` to retire the cost of decoding
# a full sheet into a chat card, and states the check plainly: *a crop that is
# not smaller than the sheet is not a mitigation.* A bomb threshold cannot
# express that — it sits 28x above the sheet, so `--scale 8` on a live attempt
# passed the write ceiling at 2176x5792 = 12_603_392 px, 3.94x the sheet it is
# supposed to be lighter than, and nothing in the payload said so.
#
# So the budget is SIZED from a sheet — `CHAR8`, 1536x2080 = 3_194_880 px /
# 12.2 MiB decoded RGBA, the largest sheet this package's default spec composes
# — but what it MEANS is a fixed console decode ceiling: *will this file sink a
# chat card*, never *is this lighter than the sheet in your hand*. It is a
# module constant. It does not move with a draft, and `fits_card_budget` has
# never compared anything to the caller's own sheet.
#
# An earlier wording of this comment claimed the opposite — "a sheet that grows
# moves the budget with it, and the number can never drift from the thing it is
# measured against" — and both halves are false, now measured at both ends:
#
#   * GROWN: `characters add-state --state jumping:6` recomposed the live
#     `anime-girl` sheet at 1536x3120 = 4_792_320 px, **1.50x this number**,
#     which did not move. On such a draft the default-scale refusal in
#     `draft.row_thumb` can reject a crop that is genuinely lighter than the
#     sheet that draft will compose.
#   * SMALL: a `--directions 4`, `idle:2` draft composes 384x624 = 239,616 px,
#     **13.3x lighter than this number**, so a `cardSafe: True` crop there can
#     be many times that draft's whole sheet (A2 measured 13.1x live).
#
# Whether the budget SHOULD become the draft's own `spec.sheet_size()` is an
# open question for launcher slice B2, not a comment's to settle: it would
# change what `cardSafe` MEANS to the consumer that reads it. Recorded with
# these measurements in the authoring skill's FIELD-NOTES.
MAX_CARD_PIXELS = CHAR8.sheet_size()[0] * CHAR8.sheet_size()[1]


def require_scale(scale) -> int:
    """The ONE gate on an upscale factor; returns it, or refuses with a reason.

    Public because two callers must agree: :func:`upscale_on_backdrop` reads it
    before allocating, and a caller weighing the OUTPUT against a budget has to
    know the number is an int before multiplying by it — ``512 * "2"`` is a
    perfectly good string, and arithmetic on one is how a type error reaches a
    consumer wearing a budget refusal's clothes. A second spelling of this
    check is a second answer to "is 0 a scale?".
    """
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 1:
        raise ValueError(f"scale must be an integer >= 1, got {scale!r}")
    return scale


def fits_card_budget(width: int, height: int) -> bool:
    """Is a ``width`` x ``height`` image light enough to draw as a chat card?

    Pure, and public because the answer is a FACT a payload has to carry: the
    verb that writes a crop cannot know whether its caller will declare the path
    to a card or open it in a fullscreen viewer, so it reports which the file is
    fit for instead of guessing. See :data:`MAX_CARD_PIXELS`.
    """
    return width * height <= MAX_CARD_PIXELS

# A non-directional state has no facing to hold, but the row prompt is built
# around explicit view language. Front view is the neutral choice: it is the one
# view that shows the whole character, and a fixed row is drawn once and shown
# from whatever angle the consumer likes. Such rows are grounded on the base
# image, not on a direction reference (see `generate_row_strip`).
NON_DIRECTIONAL_VIEW = "s"

# Attempts per row strip, last one lenient — mirrors the pet hatch loop.
_ROW_GEN_ATTEMPTS = 3

# Provider-file prefixes. Public so tests and the CLI can key off them instead of
# duplicating the strings.
PREFIX_TURNAROUND = "charsheet_turnaround"


def view_prefix(direction: str) -> str:
    """Provider-file prefix for a single-view re-roll of *direction*."""
    return f"charsheet_view_{direction}"


def row_prefix(key: str) -> str:
    """Provider-file prefix for a row strip (``charsheet_row_walk-e``).

    Row keys are filename-safe by construction: the state half is
    ``[a-z][a-z0-9_]*`` and the separator is a hyphen, so the key travels into
    a provider filename verbatim.
    """
    return "charsheet_row_" + str(key)


def _open_rgba(source):
    """An RGBA image from an image or a path (paths are opened and closed)."""
    from PIL import Image

    if isinstance(source, (str, Path)):
        with Image.open(source) as opened:
            return opened.convert("RGBA")
    return source.convert("RGBA")


def _save_png(image_or_path, out: Path) -> Path:
    """Write *image_or_path* to *out* as PNG, creating parent directories."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    _open_rgba(image_or_path).save(out, format="PNG")
    return out


# ───────────────────────────── provider seam ─────────────────────────────


def _generate_image(
    prompt: str,
    *,
    reference_images: Sequence[Path] | None,
    aspect_ratio: str,
    prefix: str,
    provider,
) -> Path:
    """The single provider call in the charsheet pipeline; returns one image.

    Every generation in this module funnels through here, so replacing this one
    function drives the whole staged flow offline with synthetic strips. Keep the
    signature stable: it is the test seam.
    """
    images = imagegen.generate(
        prompt,
        n=1,
        reference_images=[Path(ref) for ref in (reference_images or [])],
        provider=provider,
        prefix=prefix,
        aspect_ratio=aspect_ratio,
    )
    if not images:
        raise ValueError(f"image generation for {prefix!r} returned no image")
    return Path(images[0])


# ───────────────────────── grounding references ─────────────────────────


def recomposite_on_magenta(image_or_path):
    """The cutout over a flat magenta field — a usable grounding reference.

    Slicing a turnaround leaves transparent cutouts, but the row prompt anchors
    the backdrop to "the same flat chroma field as the attached reference". A
    transparent reference silently removes that anchor, so the field is painted
    back in (plan §7.3, assumption A-2).
    """
    from PIL import Image

    cutout = _open_rgba(image_or_path)
    field = Image.new("RGBA", cutout.size, (*MAGENTA, 255))
    field.alpha_composite(cutout)
    return field


def frame_cell(image_or_path, *, frame: int, frames: int):
    """Crop ONE frame cell out of a row strip, at full strip height.

    The half of the §F.2 procedure that actually REMOVES pixels. A row strip is
    already one row, so upscaling the whole strip is not a crop at all — it is
    an enlargement, and at card size it resolves no better than the raw attempt
    it was made from (measured 2026-08-24: ≤2/255 per channel against the source
    at 360 px, 0 of 86_400 pixels differing by more than 8). The remaining
    reduction is per-frame, and a frame is the unit an operator judges: within-
    strip identity means a defect is looked for frame by frame.

    Slot geometry is the strip's own: *frames* equal columns, boundaries rounded
    so no column is dropped or double-counted. Full height is kept deliberately
    — a seam sits wherever the model drew it, and trimming to the subject would
    be this module guessing which pixels the operator came to look at.
    """
    if isinstance(frames, bool) or not isinstance(frames, int) or frames < 1:
        raise ValueError(f"frames must be an integer >= 1, got {frames!r}")
    if isinstance(frame, bool) or not isinstance(frame, int):
        raise ValueError(f"frame must be an integer, got {frame!r}")
    if not 0 <= frame < frames:
        raise IndexError(
            f"frame {frame} out of range: this row has {frames} frame(s), "
            f"addressed 0-{frames - 1}"
        )
    strip = _open_rgba(image_or_path)
    left = round(frame * strip.width / frames)
    right = round((frame + 1) * strip.width / frames)
    if right <= left:
        raise ValueError(
            f"a {strip.width}px strip cannot be split into {frames} frame(s): "
            "frame 0 would be empty"
        )
    return strip.crop((left, 0, right, strip.height))


def upscale_on_backdrop(
    image_or_path,
    *,
    scale: int,
    backdrop=QA_BACKDROP,
    chroma_key: tuple[int, int, int] | None = MAGENTA,
):
    """The §F.2 looking procedure: key, composite on flat dark, NEAREST upscale.

    Three steps, all learned the expensive way on 2026-08-24. **Key the chroma
    field out** because everything this package generates arrives on a full-bleed
    magenta field at alpha 255 — composite that over a backdrop and the backdrop
    is replaced pixel for pixel, so a "flat dark ground" that never renders is
    exactly as useful as no ground at all. And §F.1's actual complaint is that
    the seam is invisible *against the magenta*. **A flat opaque backdrop**
    because once the field is gone the pixels a QA surface must judge sit over
    transparency, and a dark line drawn over transparency renders as nothing at
    all in a chat card. **NEAREST** because any smoothing filter averages a
    one-pixel seam into its neighbours — the defect is destroyed by the very
    step meant to make it visible.

    *chroma_key* is the field to remove; ``None`` composites the source as it
    stands (for an image that is already a cutout). An image that already
    carries transparency is left alone by the keyer either way.

    The output pixel count is checked against the WRITE-SAFETY ceiling BEFORE
    the resize (:data:`MAX_THUMB_PIXELS`) and the refusal names the source
    dimensions, so a caller can see which half of ``source x scale**2`` was the
    problem. That ceiling is about what may exist on disk, not about what a chat
    card may decode — the card-weight bound is :data:`MAX_CARD_PIXELS`, and it
    is applied by the verb that knows which crop is the default one.

    Returns an RGBA image whose alpha is 255 everywhere: the caller writes a
    picture, not a mask, and "is it opaque?" must be answerable from the file.
    """
    from PIL import Image

    scale = require_scale(scale)
    source = _open_rgba(image_or_path)
    pixels = source.width * source.height * scale * scale
    if pixels > MAX_THUMB_PIXELS:
        raise ValueError(
            f"scale {scale} on a {source.width}x{source.height} source would write "
            f"{source.width * scale}x{source.height * scale} = {pixels:,} pixels, "
            f"over the {MAX_THUMB_PIXELS:,}-pixel write budget; ask for a smaller "
            "scale, or a single frame instead of a whole strip"
        )
    if chroma_key is not None:
        source = remove_background(source, chroma_key=tuple(chroma_key))
    field = Image.new("RGBA", source.size, tuple(backdrop))
    field.alpha_composite(source)
    if scale == 1:
        return field
    return field.resize((field.width * scale, field.height * scale), Image.NEAREST)


def turnaround_order(authored: Iterable[str]) -> tuple[str, ...]:
    """*authored* sorted in turnaround convention: front view first, back last.

    Derived from the compass ring in :data:`prompts.VIEW_LANGUAGE` rather than
    written out per scheme: each direction is ranked by its ring distance from the
    front view, so the 8-way authored set yields ``s, se, e, ne, n`` and the
    4-way set yields ``s, e, n`` with no direction count anywhere in the code.
    """
    ring = list(prompts.VIEW_LANGUAGE)
    if NON_DIRECTIONAL_VIEW not in ring:
        raise ValueError(
            f"prompts.VIEW_LANGUAGE has no {NON_DIRECTIONAL_VIEW!r} entry; the "
            "turnaround order is defined relative to the front view"
        )
    front = ring.index(NON_DIRECTIONAL_VIEW)
    size = len(ring)

    def rank(direction: str) -> tuple[int, int]:
        if direction not in ring:
            raise ValueError(
                f"unknown direction {direction!r}: no camera-view language for it "
                f"(known directions: {', '.join(ring)})"
            )
        offset = abs(ring.index(direction) - front)
        return (min(offset, size - offset), ring.index(direction))

    return tuple(sorted(authored, key=rank))


def generate_turnaround(
    spec: SheetSpec,
    concept: str,
    base_image,
    *,
    style: str | None = "auto",
    provider=None,
    out_dir,
) -> dict[str, Path]:
    """ONE strip → one grounding reference per authored direction.

    A single generation is the point: cross-call identity drift becomes
    within-image consistency, because the model holds a character together far
    better inside one image than across five (§7.1). The slice index → direction
    mapping is exactly :func:`turnaround_order`, the same order the prompt numbers
    its slots in, so a mis-slice shows up as a wrong-looking direction in QA
    rather than as a silently mislabelled reference.

    Returns ``{direction: png path}``. Slicing failures raise: a strip that
    cannot be cut into the authored views is a failed roll, and re-rolling the
    whole turnaround is the operator's call, not a silent salvage.
    """
    order = turnaround_order(spec.scheme.authored)
    base = Path(base_image)
    if not base.is_file():
        raise ValueError(f"base image not found: {base}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    strip = _generate_image(
        prompts.build_turnaround_prompt(concept, order, style=style),
        reference_images=[base],
        aspect_ratio="landscape",
        prefix=PREFIX_TURNAROUND,
        provider=provider,
    )
    cutouts = extract_strip_frames(strip, len(order), fit=False)
    if len(cutouts) != len(order):
        raise ValueError(
            f"turnaround strip yielded {len(cutouts)} cutouts for {len(order)} "
            f"authored directions ({', '.join(order)})"
        )

    refs: dict[str, Path] = {}
    for direction, cutout in zip(order, cutouts):
        refs[direction] = _save_png(
            recomposite_on_magenta(cutout), out_dir / f"turnaround-{direction}.png"
        )
    logger.info("charsheet turnaround: %d references from one strip", len(refs))
    return refs


def generate_direction_view(
    direction: str,
    concept: str,
    base_image,
    *,
    style: str | None = "auto",
    note: str = "",
    provider=None,
    out,
) -> Path:
    """Re-roll ONE direction reference on a square canvas, with the note applied.

    Grounded on the base image (not on the rejected slice — the operator rejected
    it) and given the same key-then-magenta treatment as a turnaround slice, so a
    re-rolled reference is interchangeable with a sliced one.
    """
    base = Path(base_image)
    if not base.is_file():
        raise ValueError(f"base image not found: {base}")

    generated = _generate_image(
        prompts.build_direction_view_prompt(concept, direction, style=style, note=note),
        reference_images=[base],
        aspect_ratio="square",
        prefix=view_prefix(direction),
        provider=provider,
    )
    keyed = remove_background(_open_rgba(generated))
    return _save_png(recomposite_on_magenta(keyed), Path(out))


def generate_row_strip(
    row: RowSpec,
    concept: str,
    direction_ref,
    *,
    style: str | None = "auto",
    note: str = "",
    provider=None,
    out,
) -> Path:
    """Generate one animation strip for *row*, gated on being sliceable.

    *direction_ref* is the approved reference this row is grounded on: the
    direction's turnaround view for a directional row, the base image for a fixed
    row (which is also why a fixed row is prompted in the front view — see
    :data:`NON_DIRECTIONAL_VIEW`).

    The gate is mechanical and runs before the strip is accepted: the frames must
    segment with clean per-pose gutters (``method="components"``, which raises
    when poses touch). Up to :data:`_ROW_GEN_ATTEMPTS` rolls, the last one lenient
    (``method="auto"``, never raises) so a stubborn row still yields something the
    operator can look at and re-roll. Returns the path of the ACCEPTED strip.
    """
    direction = row.direction or NON_DIRECTIONAL_VIEW
    ref = Path(direction_ref)
    if not ref.is_file():
        raise ValueError(f"grounding reference for row {row.key!r} not found: {ref}")

    prompt = prompts.build_directional_row_prompt(
        row.state, direction, row.frames, concept, style=style, note=note
    )
    last_error: Exception | None = None
    for attempt in range(_ROW_GEN_ATTEMPTS):
        strict = attempt < _ROW_GEN_ATTEMPTS - 1
        try:
            candidate = _generate_image(
                prompt,
                reference_images=[ref],
                aspect_ratio="landscape",
                prefix=row_prefix(row.key),
                provider=provider,
            )
            extract_strip_frames(
                candidate,
                row.frames,
                method="components" if strict else "auto",
                fit=False,
            )
        except Exception as exc:  # noqa: BLE001 - retried; the reason is reported below
            last_error = exc
            logger.warning(
                "charsheet row %r attempt %d/%d rejected: %s",
                row.key,
                attempt + 1,
                _ROW_GEN_ATTEMPTS,
                exc,
            )
            continue
        logger.info("charsheet row %r accepted on attempt %d", row.key, attempt + 1)
        return _save_png(candidate, Path(out))

    raise ValueError(
        f"row {row.key!r} produced no sliceable strip in {_ROW_GEN_ATTEMPTS} "
        f"attempts; last failure: {last_error}"
    ) from last_error


# ─────────────────────────── compose / validate ───────────────────────────


def build_sheet_palette(palette_sources: Iterable, *, max_colors: int = DEFAULT_MAX_COLORS):
    """The locked palette for a sheet, from its approved grounding references.

    The references on disk carry the magenta field, so they are keyed back to
    cutouts first: leaving the chroma in would spend palette slots on a colour
    that must never reach a cell, and would pull every locked pixel toward
    magenta. §7.2 specifies the *cutouts'* opaque pixels, and this is where that
    happens — :func:`~agent.charsheet.palette.build_palette` deliberately knows
    nothing about chroma keys.
    """
    cutouts = [remove_background(_open_rgba(source)) for source in palette_sources]
    if not cutouts:
        raise ValueError("build_sheet_palette needs at least one approved reference")
    return build_palette(cutouts, max_colors=max_colors)


def compose_draft_frames(
    spec: SheetSpec,
    strips_by_key: dict[str, Path],
    palette_sources: list[Path],
) -> dict[str, list]:
    """Approved strips → registered, palette-locked cells for every sheet row.

    Order matters and is fixed by the plan (§4.3):

    1. Extract raw (``fit=False``) frames from each row's strip. Every row is an
       authored one — mirrored directions are never composed (ruling 3-B), so
       there is no derive step here and no ``mirror_frames`` call anywhere in
       this package; the consumer flips them at draw time.
    2. ``normalize_cells`` over ALL rows at once. One shared scale is the whole
       point: a character that changes size as it turns is the failure this
       prevents, and per-row normalization would guarantee it. Dropping the
       mirrored rows does not move that scale — a horizontal flip preserves
       every bounding box it measures.
    3. Palette-lock every cell.

    Re-extraction uses ``method="auto"``: the strict geometry gate already ran at
    generation time (a strip only became approvable by slicing), so compose must
    be deterministic and total, not a second chance to fail.
    """
    frames_by_key: dict[str, list] = {}
    for row in spec.authored_rows():
        strip = strips_by_key.get(row.key)
        if strip is None:
            raise ValueError(
                f"no strip for authored row {row.key!r}; rows present: "
                f"{sorted(strips_by_key)}"
            )
        frames_by_key[row.key] = extract_strip_frames(
            Path(strip), row.frames, method="auto", fit=False
        )

    normalized = normalize_cells(frames_by_key)
    palette = build_sheet_palette(palette_sources)
    return {
        key: [lock_to_palette(cell, palette) for cell in cells]
        for key, cells in normalized.items()
    }


def compose_sheet(spec: SheetSpec, cells_by_key: dict[str, list]):
    """Pack cells into the sheet described by *spec* (RGBA, residue cleared).

    Row placement is ``row.index`` from the spec, column placement is frame
    order; a row with missing or short cell lists leaves its tail transparent
    rather than shifting anything.
    """
    from PIL import Image

    width, height = spec.sheet_size()
    sheet = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for row in spec.rows():
        cells = cells_by_key.get(row.key) or []
        for column, frame in enumerate(cells[: row.frames]):
            cell = frame.convert("RGBA")
            if cell.size != (spec.frame_w, spec.frame_h):
                if (spec.frame_w, spec.frame_h) != (CELL_WIDTH, CELL_HEIGHT):
                    raise ValueError(
                        f"cell {column} of row {row.key!r} is "
                        f"{cell.width}x{cell.height}, expected "
                        f"{spec.frame_w}x{spec.frame_h}; only the upstream "
                        f"{CELL_WIDTH}x{CELL_HEIGHT} cell geometry can be re-fitted"
                    )
                cell = _fit_to_cell(cell)
            sheet.alpha_composite(cell, (column * spec.frame_w, row.index * spec.frame_h))
    return _clear_transparent_rgb(sheet)


# How much better a chain of authored directions must fit together once ONE row
# is flipped horizontally before we call that row's art mirrored.
#
# The number is measured, not chosen. On the live ``anime-girl`` draft — whose
# ``ne`` row was drawn facing north-WEST in all three of its states, shipped,
# and was found in a 3D scene rather than by any check here — flipping the
# offending row improved the fit of the seams it touches by 18.5% (idle), 13.1%
# (walk) and 13.0% (jumping). On the same character's corrected sheet, and on
# every other row of both sheets, the best any single flip could do was 3.9%; on
# a second, deliberately asymmetric character (a robot with a satchel over one
# shoulder, 4-way) it was 1.9%. The two populations are separated by a factor of
# three and this sits near their geometric mean — roughly 2x above the loudest
# false signal and 1.6x below the quietest true one.
#
# It is a RATIO on purpose: an absolute pixel distance is a property of the
# character's palette and silhouette and would need retuning per art style. A
# ratio is scale-free, and assumes nothing about skin tone, hue or style — only
# that a rotation sequence is a rotation sequence.
MIRROR_GAIN_THRESHOLD = 0.08


def _cell_distance(left, right) -> float:
    """Mean absolute per-channel difference of two same-size RGBA cells.

    Pillow only. ``numpy`` is not a dependency of this package — it enters the
    project solely through the ``voice``/``wake`` extras — and the runtime venv
    this pipeline actually executes in does not have it. A gate that imports
    numpy is a gate that never runs where it matters.
    """
    from PIL import ImageChops, ImageStat

    difference = ImageChops.difference(left, right)
    return sum(ImageStat.Stat(difference).mean) / len(difference.getbands())


def _row_cells(rgba, spec: SheetSpec, row: RowSpec) -> list:
    """The row's frame cells, left to right, cut out of a composed sheet."""
    top = row.index * spec.frame_h
    return [
        rgba.crop(
            (
                column * spec.frame_w,
                top,
                (column + 1) * spec.frame_w,
                top + spec.frame_h,
            )
        )
        for column in range(row.frames)
    ]


def _seam_distance(left_cells, right_cells) -> tuple[float, float]:
    """``(as drawn, with one side flipped)`` — mean over column-paired frames.

    Paired by column index rather than by pose: neighbouring rows are separate
    generations and their animation phases do not line up anyway, so averaging
    across the whole row is what takes the phase noise out of the number.

    ``distance(flip(a), b) == distance(a, flip(b))`` — flipping both operands is
    a re-indexing that changes no per-pixel difference — so a seam has exactly
    ONE mirrored distance and which side we flip to compute it does not matter.
    """
    from PIL import Image

    paired = min(len(left_cells), len(right_cells))
    if paired == 0:
        raise ValueError("cannot measure a seam between rows with no frames")
    direct = 0.0
    flipped = 0.0
    for index in range(paired):
        left = left_cells[index]
        right = right_cells[index]
        direct += _cell_distance(left, right)
        flipped += _cell_distance(left.transpose(Image.FLIP_LEFT_RIGHT), right)
    return (direct / paired, flipped / paired)


def detect_mirrored_art(spec: SheetSpec, image) -> dict:
    """Which authored rows are drawn as the MIRROR of the direction they claim.

    The question, stated once: *walking the authored directions in turnaround
    order, would flipping this one row horizontally make it fit its neighbours
    in the rotation noticeably better than it does as drawn?* Pure per-pixel
    distance — no skin tone, hue, silhouette or art-style assumption — and no
    reference art, because a rotation sequence is its own reference.

    Why it scores a ROW and not a seam: a seam only says the two rows either
    side of it disagree, never which of them is wrong. Summing the seams a row
    touches, before and after flipping IT, is what attributes the fault — and it
    is also what makes a nearly symmetric neighbour harmless rather than
    dangerous. The front and back views are close to their own mirror images, so
    a seam against one carries almost no handedness information; here it lands as
    a near-zero term that dilutes the ratio, not as a vote that can veto it.
    That distinction is not academic. On the live defective sheet ``idle-ne``'s
    seam against ``idle-n`` preferred the UNFLIPPED art by 2.1% — noise off a
    symmetric view — and the two-neighbour rule that this replaced, which
    required both neighbours to agree, cleared the row on that vote. It was
    mirrored, in all three states, and had shipped.

    **What this cannot see, written down so nobody has to rediscover it.** The
    measure is invariant under flipping every row at once, because
    ``distance(flip(a), flip(b)) == distance(a, b)``. A character drawn
    consistently mirrored on ALL rows is therefore a perfect fixed point and
    passes cleanly. Nothing internal to a sheet can catch that: the sheet is
    self-consistent, and only the world outside it — the launcher's screen axes —
    says which way is east. The same algebra bounds the second blind spot: a
    contiguous BLOCK of mirrored rows is visible only at the block's edges, so
    its interior rows score near zero.

    **The rotation's two END rows are never judged**, and are listed as such.
    They touch one seam each, and one seam cannot say which of the two rows
    either side of it is the mirrored one — flipping either scores identically.
    They are also the front and back views, the two closest to their own mirror
    image. Both halves of that were measured: on the correct 4-way
    ``cobalt-robot-courier`` sheet the back view's single seam preferred the
    mirror by 11%, and an earlier draft of this check refused that character's
    install over it, while the same seam diluted to 1.9% inside its interior
    neighbour's two-seam score.

    **A 4-way scheme is nearly blind here and that is worth knowing**: it authors
    ``s, e, n``, so its ONE interior row's two neighbours are both near-symmetric
    views and neither carries much handedness information. Mirroring that row on
    the live 4-way character moved its score from +1.9% to -1.9% — the check
    passes it either way. The 8-way scheme judges ``se, e, ne``, each against at
    least one profile or diagonal, which is where the signal lives.

    Returns ``{"flagged": [...], "unjudged": [...]}``. A ``flagged`` entry carries
    the row, the gain and every seam that voted, so a refusal can quote its own
    evidence. ``unjudged`` is the accounting: a row this cannot answer for is
    named with the reason rather than silently dropped.
    """
    rgba = _open_rgba(image)
    flagged: list[dict] = []
    unjudged: list[dict] = []
    rows_by_key = {row.key: row for row in spec.rows()}

    for state in spec.states:
        if not state.directional:
            unjudged.append(
                {
                    "rows": [row_key(state.name, None)],
                    "reason": "state is not directional — it has no rotation to walk",
                }
            )
            continue

        chain = [
            rows_by_key[row_key(state.name, direction)]
            for direction in turnaround_order(spec.scheme.authored)
        ]
        if len(chain) < 3:
            unjudged.append(
                {
                    "rows": [row.key for row in chain],
                    "reason": (
                        f"only {len(chain)} authored direction(s) — across one seam "
                        "a flip of either side scores identically, so no row can be "
                        "blamed"
                    ),
                }
            )
            continue

        cells_by_key = {row.key: _row_cells(rgba, spec, row) for row in chain}
        blank = [
            row.key
            for row in chain
            if not any(cell.getbbox() for cell in cells_by_key[row.key])
        ]
        if blank:
            unjudged.append(
                {
                    "rows": [row.key for row in chain],
                    "reason": (
                        f"the chain has empty row(s) ({', '.join(blank)}) — an "
                        "empty row has no facing"
                    ),
                }
            )
            continue

        seams: list[dict] = []
        for left, right in zip(chain, chain[1:]):
            direct, flipped = _seam_distance(
                cells_by_key[left.key], cells_by_key[right.key]
            )
            seams.append(
                {
                    "rows": (left.key, right.key),
                    "distance": direct,
                    "mirroredDistance": flipped,
                }
            )

        unjudged.append(
            {
                "rows": [chain[0].key, chain[-1].key],
                "reason": (
                    "the ends of the rotation touch one seam each, and one seam "
                    "cannot say WHICH of the two rows either side of it is "
                    "mirrored — flipping either scores identically. They are also "
                    "the two views closest to their own mirror image, so there is "
                    "little to see"
                ),
            }
        )

        # Interior rows only. A row with two seams is corroborated from both
        # sides; an end row with one is not, and judging it anyway is not a
        # theoretical worry: on the 4-way `cobalt-robot-courier` sheet — correct
        # art, verified by eye — the back view's single seam preferred the mirror
        # by 11% and an earlier draft of this check REFUSED that character's
        # install. The same seam contributes to its interior neighbour's score as
        # one of two terms, where it is diluted to 1.9% and correctly ignored.
        for row in chain[1:-1]:
            touching = [seam for seam in seams if row.key in seam["rows"]]
            as_drawn = sum(seam["distance"] for seam in touching)
            if as_drawn <= 0:
                continue
            as_flipped = sum(seam["mirroredDistance"] for seam in touching)
            gain = (as_drawn - as_flipped) / as_drawn
            if gain < MIRROR_GAIN_THRESHOLD:
                continue
            flagged.append(
                {
                    "row": row.key,
                    "state": row.state,
                    "direction": row.direction,
                    "gain": gain,
                    "seams": [
                        {
                            "with": next(key for key in seam["rows"] if key != row.key),
                            "distance": seam["distance"],
                            "mirroredDistance": seam["mirroredDistance"],
                        }
                        for seam in touching
                    ],
                }
            )

    return {"flagged": flagged, "unjudged": unjudged}


def mirrored_art_error(finding: dict) -> str:
    """The refusal text for ONE :func:`detect_mirrored_art` finding.

    Public so the message has a single spelling: the validator raises it and a
    test asserts it, and a second copy is how a refusal starts naming a verb
    that no longer exists.
    """
    evidence = "; ".join(
        f"vs '{seam['with']}' {seam['distance']:.2f} -> "
        f"{seam['mirroredDistance']:.2f} flipped"
        for seam in finding["seams"]
    )
    return (
        f"row '{finding['row']}' looks drawn as the MIRROR of "
        f"{finding['direction']!r}: flipping it fits its neighbours in the "
        f"rotation {finding['gain'] * 100:.0f}% better ({evidence}). A mirrored "
        "authored row corrupts the derived direction with it, because the "
        "consumer builds that one by flipping this row. Re-roll it "
        f"(characters reroll-row --row {finding['row']} --note ...) with the "
        "facing spelled in frame terms, and look at the strip before composing."
    )


def validate_sheet(spec: SheetSpec, image) -> dict:
    """Geometry, occupancy, collapse and residue checks, driven by *spec*.

    Returns ``{ok, width, height, errors, warnings, filled_rows, handedness}``.
    Errors block an install (wrong size, empty sheet, sprites collapsed by a bad
    row, a multi-pose frame that slipped the extractor, RGB residue under
    transparency, a row drawn as the mirror of the direction it claims); a
    single blank row is a warning, because which rows are required is the
    caller's policy, not the validator's.

    The geometry guards are upstream's ``validate_atlas`` guards generalized off
    ``ROW_SPECS``: the collapse floor exists because ``normalize_cells`` shares
    one scale across all rows, so one degenerate row can shrink the entire
    character while every cell still passes a non-empty check (§A-5).

    ``handedness`` is :func:`detect_mirrored_art`'s whole answer, carried in the
    payload whether or not it found anything — including its ``unjudged`` list,
    so a caller can always see which rows this could not answer for. A flagged
    row is an ERROR and not a warning on purpose: the one time this defect
    happened it shipped, was installed, was bundled into the launcher and was
    finally caught by a human looking at a 3D scene. A warning is the shape that
    failure already had.
    """
    rgba = _open_rgba(image)
    errors: list[str] = []
    warnings: list[str] = []
    expected = spec.sheet_size()
    if rgba.size != expected:
        errors.append(
            f"expected {expected[0]}x{expected[1]}, got {rgba.width}x{rgba.height}"
        )
        return {
            "ok": False,
            "width": rgba.width,
            "height": rgba.height,
            "errors": errors,
            "warnings": warnings,
            "filled_rows": [],
            "handedness": {
                "flagged": [],
                "unjudged": [
                    {
                        "rows": [row.key for row in spec.rows()],
                        "reason": (
                            "the sheet is not the size its spec describes; rows "
                            "cannot be cut out of it"
                        ),
                    }
                ],
            },
        }

    filled_rows: list[str] = []
    boxes_by_row: dict[str, list[tuple[int, int, int, int]]] = {}
    for row in spec.rows():
        row_pixels = 0
        boxes: list[tuple[int, int, int, int]] = []
        top = row.index * spec.frame_h
        for column in range(row.frames):
            left = column * spec.frame_w
            cell = rgba.crop((left, top, left + spec.frame_w, top + spec.frame_h))
            row_pixels += sum(cell.getchannel("A").histogram()[1:])
            bbox = cell.getbbox()
            if bbox is not None:
                boxes.append(bbox)
        if row_pixels > 0:
            filled_rows.append(row.key)
            boxes_by_row[row.key] = boxes
        else:
            warnings.append(f"row '{row.key}' has no frames")

    if not filled_rows:
        errors.append("sheet is empty — no row produced any frames")

    all_widths = sorted(
        right - left for boxes in boxes_by_row.values() for left, _t, right, _b in boxes
    )
    all_heights = sorted(
        bottom - top for boxes in boxes_by_row.values() for _l, top, _r, bottom in boxes
    )
    global_med_w = 0
    global_med_h = 0
    if all_widths and all_heights:
        global_med_w = all_widths[len(all_widths) // 2]
        global_med_h = all_heights[len(all_heights) // 2]
        min_h = max(56, round(spec.frame_h * 0.28))
        if global_med_h < min_h:
            errors.append(
                f"sheet sprites are too small after normalization (median frame "
                f"height {global_med_h}px, floor {min_h}px)"
            )

    for key, boxes in boxes_by_row.items():
        if len(boxes) <= 1:
            continue
        widths = sorted(right - left for left, _t, right, _b in boxes)
        heights = sorted(bottom - top for _l, top, _r, bottom in boxes)
        med_w = max(1, widths[len(widths) // 2])
        med_h = max(1, heights[len(heights) // 2])
        if widths[-1] > max(med_w * 3.0, med_w + 96) and heights[-1] <= med_h * 1.6:
            errors.append(f"row '{key}' contains a multi-pose frame outlier")
        if global_med_w and global_med_h:
            if med_w < max(32, round(global_med_w * 0.42)) or med_h < max(
                40, round(global_med_h * 0.50)
            ):
                errors.append(
                    f"row '{key}' appears collapsed (median {med_w}x{med_h}px, "
                    f"sheet median {global_med_w}x{global_med_h}px)"
                )

    data = rgba.tobytes()
    residue = sum(
        1
        for i in range(0, len(data), 4)
        if data[i + 3] == 0 and (data[i] or data[i + 1] or data[i + 2])
    )
    if residue:
        errors.append(f"{residue} transparent pixels retain RGB residue")

    # Last, and only on a sheet whose geometry already holds: the handedness
    # check reads real cells, and there is nothing to say about the facing of a
    # row that is the wrong size or collapsed. Its findings are errors — see the
    # docstring for why this one is not allowed to be a warning.
    handedness = detect_mirrored_art(spec, rgba)
    errors.extend(mirrored_art_error(finding) for finding in handedness["flagged"])

    return {
        "ok": not errors,
        "width": rgba.width,
        "height": rgba.height,
        "errors": errors,
        "warnings": warnings,
        "filled_rows": filled_rows,
        "handedness": handedness,
    }
