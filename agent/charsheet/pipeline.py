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
# than silently re-fitted to 192x208. `frame_x_bounds` is here for the same
# reason under a sharper lesson: this module HAD a local copy of frame geometry
# (width / frames), it disagreed with upstream's content-aware rule on the first
# real strip, and it shipped a QA crop with half a character in it (2026-08-28).
# Centralized in this ONE block so an upstream rename breaks loudly, at import
# time, in a single place (plan §A-6).
from agent.pet.generate import imagegen
from agent.pet.generate.atlas import (
    CELL_HEIGHT,
    CELL_WIDTH,
    _clear_transparent_rgb,
    _fit_to_cell,
    atlas_to_webp_bytes,
    extract_strip_frames,
    frame_x_bounds,
    normalize_cells,
    remove_background,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MAGENTA",
    "MIRROR_GAIN_THRESHOLD",
    "PREFIX_TURNAROUND",
    "REGISTRATION_WINDOW_DIVISOR",
    "accept_basis_token",
    "atlas_to_webp_bytes",
    "build_sheet_palette",
    "compose_draft_frames",
    "compose_sheet",
    "fits_console_budget",
    "fits_own_sheet",
    "frame_cell",
    "generate_direction_view",
    "generate_row_strip",
    "generate_turnaround",
    "handedness_summary",
    "recomposite_on_magenta",
    "registration_window",
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

# `MAX_CONSOLE_CARD_PIXELS` is the CONSOLE DECODE ceiling — *will this file
# sink a chat card* — and it is ONE of the two bounds a crop is weighed against.
# Launcher risk D.3 asks `thumb` to retire the cost of decoding a full sheet
# into a chat card, and states a second check plainly: *a crop that is not
# smaller than the sheet is not a mitigation.* Those are two different
# questions, and until 2026-08-25 one boolean answered both under one name.
#
# A bomb threshold cannot express either — it sits 28x above the sheet, so
# `--scale 8` on a live attempt passed the write ceiling at 2176x5792 =
# 12_603_392 px, 3.94x the sheet it is supposed to be lighter than, and nothing
# in the payload said so.
#
# So this budget is SIZED from a sheet — `CHAR8`, 1536x2080 = 3_194_880 px /
# 12.2 MiB decoded RGBA, the largest sheet this package's default spec composes
# — but what it MEANS is a fixed console decode ceiling, never *is this lighter
# than the sheet in your hand*. It is a module constant. It does not move with a
# draft, and :func:`fits_console_budget` has never compared anything to the
# caller's own sheet.
#
# An earlier wording of this comment claimed the opposite — "a sheet that grows
# moves the budget with it, and the number can never drift from the thing it is
# measured against" — and both halves are false, measured at both ends:
#
#   * GROWN: `characters add-state --state jumping:6` recomposed the live
#     `anime-girl` sheet at 1536x3120 = 4_792_320 px, **1.50x this number**,
#     which did not move. On such a draft the default-scale refusal in
#     `draft.row_thumb` can reject a crop that is genuinely lighter than the
#     sheet that draft will compose.
#   * SMALL: a `--directions 4`, `idle:2` draft composes 384x624 = 239,616 px,
#     **13.3x lighter than this number**, so a crop that clears this budget can
#     be many times that draft's whole sheet (A2 measured 1774x1774 =
#     3_147_076 px live, 13.1x, and 1.5% under this ceiling).
#
# Those two measurements are why the boolean was SPLIT rather than re-aimed
# (owner ruling 2026-08-25). This constant answers the console question and
# keeps its old value; :func:`fits_own_sheet` answers the sheet question against
# the draft's OWN `spec.sheet_size()`; `draft.row_thumb` carries both answers.
# The name says which one this is, because the name is what a reader trusts.
MAX_CONSOLE_CARD_PIXELS = CHAR8.sheet_size()[0] * CHAR8.sheet_size()[1]


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


def fits_console_budget(width: int, height: int) -> bool:
    """Is a ``width`` x ``height`` image under the console's fixed decode ceiling?

    Pure, and public because the answer is a FACT a payload has to carry: the
    verb that writes a crop cannot know whether its caller will declare the path
    to a card or open it in a fullscreen viewer, so it reports which the file is
    fit for instead of guessing.

    This is HALF the question. It says the file will not sink the console; it
    says nothing about whether cropping bought anything, which is
    :func:`fits_own_sheet`. See :data:`MAX_CONSOLE_CARD_PIXELS`.
    """
    return width * height <= MAX_CONSOLE_CARD_PIXELS


def fits_own_sheet(width: int, height: int, spec: SheetSpec) -> bool:
    """Is a ``width`` x ``height`` image no larger than *spec*'s own sheet?

    The other half, and the one launcher risk D.3 actually states: *a crop that
    is not smaller than the sheet is not a mitigation.* It moves with the draft,
    because it is computed from that draft's ``spec.sheet_size()`` every time —
    which is exactly what :data:`MAX_CONSOLE_CARD_PIXELS` is not, and why one
    boolean could never carry both answers.

    The two disagree on real drafts in BOTH directions, which is the whole
    reason they are two booleans:

    * a ``--directions 4``, ``idle:2`` draft (384x624 = 239,616 px) took a
      default 1774x1774 = 3_147_076 px crop: under the console ceiling, 13.1x
      its own sheet.
    * an ``add-state``-grown ``anime-girl`` (1536x3120 = 4_792_320 px) can take
      a crop over the console ceiling that is still lighter than the sheet that
      draft will compose.

    ``<=`` rather than ``<``, matching :func:`fits_console_budget`: the
    guarantee is *not larger than*, and a name that promised *strictly smaller*
    would be the same defect one word further along.
    """
    sheet_w, sheet_h = spec.sheet_size()
    return width * height <= sheet_w * sheet_h


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

    Frame geometry is NOT this module's to invent: the x-range comes from
    :func:`atlas.frame_x_bounds`, the same content-aware rule the real frame
    extraction uses (gutters between poses, merged down to the frame count;
    thin severs at the expected boundaries when the poses touch; even columns
    ONLY as the last resort, when there is no content to read at all). This
    module used to divide the width by *frames* and call that a frame, and on
    2026-08-28 an operator opened the result fullscreen and found half a
    character: `walk-e` attempt 1 is a 2172px 8-frame row whose first pose spans
    x 66-298, the even boundary falls at 272, and the QA crop stopped there —
    26 columns of body cut off, the cut edge standing as a 205px column of
    pixels flush against the frame's right side. Even slots are wrong on real
    strips, which is exactly why the extraction is content-aware; one strip with
    two boundary rules meant the dumb one was feeding the surface whose whole
    job is to show an operator the truth.

    Full height is kept deliberately — a seam sits wherever the model drew it,
    and trimming to the subject would be this module guessing which pixels the
    operator came to look at. Width is the pose's, height is the strip's, and
    the asymmetry is the point: a frame boundary is a fact about the row that
    can be read off the pixels, a subject's top and bottom are not.

    The strip is decoded ONCE and the open image is handed to the geometry, so
    finding the boundary costs a keying pass, not a second read from disk.
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
    left, right = frame_x_bounds(strip, frames)[frame]
    if right <= left:
        raise ValueError(
            f"a {strip.width}px strip cannot be split into {frames} frame(s): "
            f"frame {frame} would be empty"
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
    card may decode — that is :data:`MAX_CONSOLE_CARD_PIXELS`, applied (with
    the draft's own sheet bound) by the verb that knows which crop is the
    default one.

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
    (``method="auto"``, which raises only on a wrong frame count or an empty
    frame) so a stubborn row still yields something the operator can look at and
    re-roll. Returns the path of the ACCEPTED strip.
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
# The number is measured, not chosen, and the population it separates was
# re-measured on 2026-08-25 once registration (below) entered the measure.
# Registration costs sensitivity — it takes back the part of the old signal that
# was really PLACEMENT — so both bands moved and the separation narrowed:
#
#   TRUE  (real art known to be mirrored): the live `anime-girl` pre-fix sheet's
#         three `ne` rows score +12.28% / +8.52% / +7.37%, and mirroring each of
#         the nine interior rows of the REPAIRED sheet one at a time scores
#         +4.33% ... +15.86%. Twelve samples, floor +4.33%.
#   FALSE (real art known to be correct): every interior row of the repaired
#         sheet reads -4.53% ... -18.85%, and `cobalt-robot-courier` — 4-way, a
#         satchel over one shoulder, deliberately asymmetric — reads +1.72%.
#         Ceiling +1.72%.
#
# **The threshold is not the lever, and moving it cannot become one.** Both
# bands were re-measured on 2026-08-25 in the direction the note above does not
# reach, and they OVERLAP:
#
#   The true floor is BELOW the line, on real art. Mirroring each interior row
#   of the repaired live sheet one at a time, `jumping-se` reads +6.78% in the
#   rotation and +7.64% across the states — flagged by NEITHER pass. That sheet
#   composes, installs and bundles with no refusal and no warning. The founding
#   defect sits AT the line: the pre-fix sheet's third genuinely mirrored row,
#   `jumping-ne`, reads +7.37%, and the install was refused only because the
#   other two cleared it.
#
#   The false ceiling is far ABOVE that floor once art is stressed. Sliding a
#   CORRECT `idle-e` sideways: -24 px reads +9.38%, -32 px +17.92%, -40 px
#   +18.75%, -56 px +7.76% and installs again. It is a BAND of roughly -20 to
#   -48 px, not a threshold.
#
# max(false) is about +18.75% and min(true) is +7.64%. An 8% line does not
# separate two populations there; it sits inside both of them. So the number
# stays where it is and what changed is what ONE reading is allowed to do: a
# single basis WARNS, and only two independent bases agreeing about the same row
# REFUSE (see :func:`detect_mirrored_art` and :func:`validate_sheet`). Sensitivity
# is bought back by the cross-state pass below and by the operator's eye, and the
# named acceptance in :func:`validate_sheet` is the door past a two-basis refusal.
#
# It is a RATIO on purpose: an absolute pixel distance is a property of the
# character's palette and silhouette and would need retuning per art style. A
# ratio is scale-free, and assumes nothing about skin tone, hue or style — only
# that a rotation sequence is a rotation sequence.
MIRROR_GAIN_THRESHOLD = 0.08


# How far one row may be slid sideways, before either distance is taken, so that
# PLACEMENT cannot be read as handedness. One twelfth of the frame each way —
# 16 px on the 192 px cell.
#
# Without this the measure has no registration step at all, and any horizontal
# displacement between neighbouring rows enters the distance and therefore the
# ratio. Measured on the REPAIRED (correct) live sheet by sliding `idle-e`
# sideways and changing nothing else: -7 px scored +9.81% and refused the
# install; -24 px scored +18.75%, past every genuine reading on the defective
# sheet. What kept that off real characters was ``normalize_cells`` centring each
# row on its union bbox — upstream pet code this package imports and does not
# own — and that centring is not as tight as it looks: it pins the union BOX,
# not the body, so the body still lands up to 10 px apart between neighbouring
# rows on art that passes (measured across all three states of the live sheet).
# The shipped measure charged all of that to handedness.
#
# The reachable driver is a PROP, not a stray offset: a bag, a cape or a sheathed
# sword hanging off one side of ONE row widens that row's union bbox, so centring
# it moves the BODY by half the prop's width. With this window a one-sided prop
# up to a quarter of the frame (48 px, body -24 px) still installs (+5.19%);
# without it a 24 px prop already refused (+9.05%).
#
# The window BOUNDS the blindness, it does not remove it: a pure translation
# larger than the window still crosses (-24 px reads +10.91% even registered).
# That residue is why the acceptance path in :func:`validate_sheet` exists.
REGISTRATION_WINDOW_DIVISOR = 12


def registration_window(frame_w: int) -> int:
    """Half-width, in pixels, of the shift search for a *frame_w*-wide cell."""
    return max(1, round(frame_w / REGISTRATION_WINDOW_DIVISOR))


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


def _padded(cell, pad: int):
    """*cell* on a transparent canvas *pad* px wider on each side."""
    from PIL import Image

    canvas = Image.new("RGBA", (cell.width + 2 * pad, cell.height), (0, 0, 0, 0))
    canvas.alpha_composite(cell, (pad, 0))
    return canvas


def _registered_distance(left, right, window: int) -> tuple[float, int]:
    """``(distance, shift)`` — the smallest distance over a symmetric shift grid.

    Both cells are compared on ONE canvas ``window`` px wider on each side, so no
    content falls off an edge and both distances are divided by the same pixel
    count: the padding scales the two terms of the ratio identically and cancels.

    The grid is symmetric about zero on purpose. ``distance(shift(flip a, d),
    flip b) == distance(shift(a, -d), b)``, so under a global flip the SET of
    scores over a symmetric grid is unchanged and its minimum is EXACTLY equal —
    which is what keeps the "a sheet mirrored on every row is a fixed point"
    property true of the registered measure and not merely of the raw one. An
    asymmetric grid, or a cross-correlation peak with a first-wins tie-break,
    would break that equality on symmetric art.
    """
    if window <= 0:
        return _cell_distance(left, right), 0
    width, height = left.size
    fixed = _padded(right, window)
    sliding = _padded(left, 2 * window)
    best: float | None = None
    best_shift = 0
    for shift in range(-window, window + 1):
        start = window - shift
        score = _cell_distance(
            sliding.crop((start, 0, start + width + 2 * window, height)), fixed
        )
        if best is None or score < best:
            best, best_shift = score, shift
    return best, best_shift


def _seam_distance(left_cells, right_cells, *, window: int) -> tuple[float, float]:
    """``(as drawn, with one side flipped)`` — mean over column-paired frames.

    Paired by column index rather than by pose: neighbouring rows are separate
    generations and their animation phases do not line up anyway, so averaging
    across the whole row is what takes the phase noise out of the number.

    Each pairing is REGISTERED first (:func:`_registered_distance`) and both
    orientations get the same freedom, so a sideways displacement between the two
    rows cancels out of the ratio instead of reading as handedness. *window* is
    the only knob; ``window=0`` is the unregistered measure and exists so a test
    can show what registration bought.

    ``distance(flip(a), b) == distance(a, flip(b))`` — flipping both operands is
    a re-indexing that changes no per-pixel difference, and the symmetric shift
    grid preserves it — so a seam has exactly ONE mirrored distance and which
    side we flip to compute it does not matter.
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
        direct += _registered_distance(left, right, window)[0]
        flipped += _registered_distance(
            left.transpose(Image.FLIP_LEFT_RIGHT), right, window
        )[0]
    return (direct / paired, flipped / paired)


def _seam_record(left: str, right: str, direct: float, flipped: float) -> dict:
    return {"rows": (left, right), "distance": direct, "mirroredDistance": flipped}


def _seam_evidence(row: str, seams: list[dict]) -> list[dict]:
    """The seams a finding quotes, named from the flagged row's point of view."""
    return [
        {
            "with": next(key for key in seam["rows"] if key != row),
            "distance": seam["distance"],
            "mirroredDistance": seam["mirroredDistance"],
        }
        for seam in seams
    ]


def _gain(seams: list[dict]) -> float | None:
    """``(as drawn - flipped) / as drawn`` over *seams*; ``None`` when 0 apart."""
    as_drawn = sum(seam["distance"] for seam in seams)
    if as_drawn <= 0:
        return None
    return (as_drawn - sum(seam["mirroredDistance"] for seam in seams)) / as_drawn


def _contradicted(entry: dict, cross: dict[str, float], suspected: set[str]) -> bool:
    """Do the OTHER states vouch for this row, or merely agree with it?

    A negative cross-state reading means "every state draws this direction the
    same way". That is exculpatory exactly when the other states' copies of the
    direction are not themselves under suspicion, and it is worth nothing when
    they are: a direction mirrored in EVERY state is a fixed point of the
    cross-state pass and reads strongly negative there PRECISELY BECAUSE it is
    consistently wrong. Measured on the fixture, both halves. Two mirrored rows
    flanking a correct one leave the correct middle row at -97.62% across the
    states, and `e` is over threshold in one state's rotation out of three. The
    founding defect — `ne` mirrored in all three states — leaves each mirrored
    row at -111.32% across the states, and `ne` is over threshold in three
    rotations out of three. Reading the SIGN alone would exonerate the second,
    which is the defect this whole gate was built for; *suspected* is what
    separates them.
    """
    gain = cross.get(entry["row"])
    if gain is None or gain >= 0:
        return False
    return entry["direction"] not in suspected


def _attribute_run(
    run: list[tuple[int, dict]], cross: dict[str, float], suspected: set[str]
) -> tuple[dict | None, str]:
    """``(culprit, how)`` — which row of a run the evidence can actually NAME.

    ``how`` is ``"both"`` (a second, independent basis convicts this row),
    ``"rotation"`` (ONE flagged row, alone, with nothing contradicting it), or
    the reason no row could be named at all: ``"run"`` (two or more flagged
    together, which the rotation cannot take apart) or ``"contradicted"`` (the
    single flagged row is vouched for by the other states).

    **"The culprit is the run's maximum" is retired, not re-tuned.** It was true
    of every case round two measured and false in two it did not, both of which
    put an INNOCENT row at the top of the run. A CORRECT row displaced sideways
    past the registration window reads high and drags its untouched neighbour
    higher still — measured on the fixture, `walk-e` slid -24 px reads +10.48%
    while the untouched `walk-ne` reads +10.68% and wins. And a correct row
    FLANKED by two mirrored ones wins its run outright: `idle-e`, correct,
    +14.28% between a mirrored `idle-se` and a mirrored `idle-ne`. Both are
    reachable — the first is a prop or a framing drift, the second is what a
    SECOND badly-worded diagonal in ``VIEW_LANGUAGE`` produces, the way `ne`
    alone produced the first defect this package ever saw.

    So the ranking never names anybody. Adjacent flagged rows are taken apart by
    a second BASIS or not at all, which is the same argument that leaves the
    rotation's end rows unjudged: one seam cannot say which of the two rows
    beside it is mirrored, and neither can two rows that raised each other. A
    lone flagged row has nothing to be confused with, so it is still named —
    which is what keeps the founding defect attributable, since `ne` mirrored in
    every state flags one isolated row per state.
    """
    entries = [entry for _position, entry in run]
    convicted = [
        entry
        for entry in entries
        if (cross.get(entry["row"]) or 0.0) >= MIRROR_GAIN_THRESHOLD
    ]
    # EXACTLY one, and the count is the rule rather than a tie-break. Ranking
    # two convictions would be a knob no fixture can reach — measured: two
    # ADJACENT mirrored rows never form a rotation run at all, because a
    # contiguous block is visible only at its edges and both rows' rotation
    # gains go NEGATIVE (`idle-e` + `idle-ne` mirrored reads -5.30% / -15.36%).
    # A run holding two cross-state convictions is a shape nothing here
    # understands, and the safe answer to a shape you do not understand is to
    # name nobody rather than to sort it.
    if len(convicted) == 1:
        return convicted[0], "both"
    if len(entries) >= 2:
        return None, "run"
    if _contradicted(entries[0], cross, suspected):
        return None, "contradicted"
    return entries[0], "rotation"


def _finding_from_run(
    run: list[tuple[int, dict]], cross: dict[str, float], suspected: set[str]
) -> dict:
    culprit, how = _attribute_run(run, cross, suspected)
    ranked = sorted(
        (entry for _position, entry in run),
        key=lambda entry: entry["gain"],
        reverse=True,
    )
    anchor = culprit if culprit is not None else ranked[0]
    finding = dict(anchor)
    finding["attributed"] = culprit is not None
    finding["attribution"] = how
    # ``corroborating`` carries "do NOT re-roll these", so it is only honest
    # once a row has actually been named as the fault. An unattributed run lists
    # its rows as ``alternatives`` instead: any of them may be the one.
    finding["corroborating"] = (
        [
            {"row": entry["row"], "gain": entry["gain"]}
            for entry in ranked
            if entry["row"] != anchor["row"]
        ]
        if culprit is not None
        else []
    )
    finding["alternatives"] = (
        []
        if culprit is not None
        else [{"row": entry["row"], "gain": entry["gain"]} for entry in ranked]
    )
    return finding


def _run_findings(
    scored: list[tuple[int, dict]], cross: dict[str, float], suspected: set[str]
) -> list[dict]:
    """One finding per contiguous RUN of over-threshold rows.

    A mirrored row raises BOTH its neighbours toward the line, because their
    seams against it prefer the flip from their side too. Reporting each of them
    as its own error is not a harmless excess of caution: every message names
    ``characters reroll-row``, ``reroll_row`` proposes and approves
    unconditionally, and there is no ``approve-row`` verb to undo it — so an
    operator who obeys a three-row refusal literally spends two correct approved
    attempts. Measured on the live repaired sheet: mirroring `idle-e` alone puts
    `idle-e` at +13.33% and `idle-ne` at +11.87%.

    WHICH row of the run is named is :func:`_attribute_run`'s job, and it is not
    simply the maximum — see that function for the two measured shapes where the
    maximum is an innocent row.
    """
    over = [item for item in scored if item[1]["gain"] >= MIRROR_GAIN_THRESHOLD]
    findings: list[dict] = []
    run: list[tuple[int, dict]] = []
    for item in over:
        if run and item[0] != run[-1][0] + 1:
            findings.append(_finding_from_run(run, cross, suspected))
            run = []
        run.append(item)
    if run:
        findings.append(_finding_from_run(run, cross, suspected))
    return findings


def detect_mirrored_art(spec: SheetSpec, image) -> dict:
    """Which authored rows are drawn as the MIRROR of the direction they claim.

    Two independent passes, both asking the same question — *would flipping this
    one row horizontally make it fit the art it has to agree with noticeably
    better than it does as drawn?* — of two different neighbourhoods:

    * **the rotation** (``basis: "rotation"``): the authored directions of ONE
      state, walked in turnaround order. Answers a single mis-drawn row.
    * **the states** (``basis: "states"``): the SAME direction across every
      state, all of which must face the same way. Answers a whole state drawn
      mirrored — the case ``add_state`` makes reachable in one batch, since it
      generates all of a new state's rows against one reference and one prompt,
      which is exactly how the `ne` defect arose along the other axis.

    Pure per-pixel distance — no skin tone, hue, silhouette or art-style
    assumption — and no reference art, because a rotation sequence and a set of
    states are each their own reference.

    Why the rotation pass scores a ROW and not a seam: a seam only says the two
    rows either side of it disagree, never which of them is wrong. Summing the
    seams a row touches, before and after flipping IT, is what attributes the
    fault — and it is also what makes a nearly symmetric neighbour harmless
    rather than dangerous. The front and back views are close to their own mirror
    images, so a seam against one carries almost no handedness information; here
    it lands as a near-zero term that dilutes the ratio, not as a vote that can
    veto it. On the live defective sheet ``idle-ne``'s seam against ``idle-n``
    preferred the UNFLIPPED art by 2.1% — noise off a symmetric view — and the
    two-neighbour rule that this replaced, which required both neighbours to
    agree, cleared the row on that vote. It was mirrored, in all three states,
    and had shipped.

    Why the state pass needs THREE states: across one pair a disagreement cannot
    say which of the two rows is the mirrored one — the same reason the
    rotation's end rows are never judged. With three or more, a row is convicted
    only when a strict majority of ALL the states that draw the direction
    disagree with it — that is, only when its camp is a strict MINORITY — and
    the quietest of those disagreeing readings is what ``gain`` reports for this
    basis. The default sheet has two states, so this pass says nothing until a
    third arrives, and an EVEN number of states that splits evenly convicts
    nobody: two camps of equal size, and nothing inside the sheet says which one
    is mirrored.

    **Two shapes refuse an install; everything else warns.** A single-basis
    finding about a single ROW carries ``severity: "warning"``. Two carry
    ``severity: "error"``:

    * ``basis: "rotation and states"`` — two independent neighbourhoods agreeing
      about the SAME row. The two populations do not separate at the threshold
      on one basis: the quietest true reading measured on real art is +6.78%
      rotation / +7.64% states, and the loudest false one, a CORRECT row
      displaced sideways, is +18.75%. The line sits inside both populations, so
      no value of :data:`MIRROR_GAIN_THRESHOLD` separates them and what changed
      instead is what one reading is permitted to do.
    * **a WHOLE STATE reading as mirrored** — every row of one state that the
      cross-state pass could judge, at least two of them, flagged. Such findings
      carry ``wholeState`` (the state's flagged rows, in sheet order) and are
      errors on ONE basis or two (owner ruling 2026-08-25). The rotation cannot
      corroborate this one *by algebra* — a wholly mirrored state is a fixed
      point of it — so demanding a second basis would mean never refusing the
      defect ``add_state`` is most likely to produce. A single row mirroring is
      NOT escalated: that is the warning above, and the ``>= 2`` guard is what
      keeps a 4-way scheme (one cross-state-judged row per state) out of here.

    The consequence for the default character is stated rather than hidden:
    ``characters start`` creates ``idle:6, walk:8``, two states have no
    cross-state pass at all, so on that DEFAULT sheet neither error shape is
    reachable and this check can only ever warn.

    **What this cannot see, written down so nobody has to rediscover it.** The
    measure is invariant under flipping every row at once, because
    ``distance(flip(a), flip(b)) == distance(a, b)``. A character drawn
    consistently mirrored on ALL rows is therefore a perfect fixed point and
    passes cleanly — and so is one whose every STATE is mirrored, since the state
    pass is a consensus and a unanimous one convicts nobody. Nothing internal to
    a sheet can catch either: the sheet is self-consistent, and only the world
    outside it — the launcher's screen axes — says which way is east. The same
    algebra bounds the third blind spot: a contiguous BLOCK of mirrored rows in
    one rotation is visible only at the block's edges.

    **The rotation's two END rows are never judged**, and neither is any row
    whose neighbour is blank: a row is judged only when it has a measurable seam
    on EACH side. One seam cannot say which of the two rows either side of it is
    the mirrored one — flipping either scores identically. Both halves of that
    were measured: on the correct 4-way ``cobalt-robot-courier`` sheet the back
    view's single seam preferred the mirror by 11%, and an earlier draft of this
    check refused that character's install over it, while the same seam diluted
    to 1.9% inside its interior neighbour's two-seam score.

    **A 4-way scheme is nearly blind here and that is worth knowing**: it authors
    ``s, e, n``, so its ONE interior row's two neighbours are both near-symmetric
    views and neither carries much handedness information. Mirroring that row on
    the live 4-way character moved its score from +1.9% to -1.9% — the check
    passes it either way. The 8-way scheme judges ``se, e, ne``, each against at
    least one profile or diagonal, which is where the signal lives.

    Returns ``{"flagged": [...], "judged": [...], "unjudged": [...]}``. Both
    lists carry a ``basis``, and every row of the sheet is named at least once
    across the two: a row may be judged by one pass and unjudged by the other
    (a 4-way sheet's ``e`` row is judged in the rotation and unjudged across
    states, because there is only one state), but never both under the SAME
    basis. ``judged`` carries the gain of every row a pass could answer for, over
    the threshold or under it, so the margin is a readable fact rather than
    something a caller re-derives.
    ``flagged`` is the subset worth acting on after attribution. Each entry
    quotes the seams that voted and carries ``severity`` (above), ``attributed``
    and ``attribution`` — plus ``wholeState`` on the rows a whole mirrored state
    carries, holding that state's flagged rows in sheet order. ``attributed`` is
    the honest half: ``True`` means the
    evidence NAMES this row, and the entry then lists any ``corroborating`` rows
    that read high because of it ("do not re-roll them"); ``False`` means a
    neighbourhood is wrong but nothing here can say which row, and the entry
    lists the whole run under ``alternatives`` instead. ``unjudged`` is the
    accounting: a row this cannot answer for is named with the reason rather
    than silently dropped.
    """
    rgba = _open_rgba(image)
    window = registration_window(spec.frame_w)
    rows_by_key = {row.key: row for row in spec.rows()}
    cells_by_key: dict[str, list] = {}

    def cells(row: RowSpec) -> list:
        if row.key not in cells_by_key:
            cells_by_key[row.key] = _row_cells(rgba, spec, row)
        return cells_by_key[row.key]

    def blank(row: RowSpec) -> bool:
        return not any(cell.getbbox() for cell in cells(row))

    judged: list[dict] = []
    unjudged: list[dict] = []

    # The states pass runs FIRST, because the rotation's attribution reads it.
    # Naming a row as the culprit of a rotation run is a claim the rotation
    # cannot make on its own (see _attribute_run), so the second basis has to
    # exist before the first one is allowed to point at anybody.
    state_flagged: list[dict] = []
    states_judged_per_state: dict[str, int] = {}
    directional = [state for state in spec.states if state.directional]
    for direction in turnaround_order(spec.scheme.authored)[1:-1]:
        drawn = [
            rows_by_key[row_key(state.name, direction)]
            for state in directional
            if not blank(rows_by_key[row_key(state.name, direction)])
        ]
        if len(drawn) < 3:
            unjudged.append(
                {
                    "rows": [row.key for row in drawn],
                    "basis": "states",
                    "reason": (
                        f"only {len(drawn)} state(s) draw {direction!r} — across one "
                        "pair a disagreement cannot say WHICH of the two states is "
                        "mirrored, so this read needs three"
                    ),
                }
            )
            continue
        across: dict[tuple[str, str], dict] = {}
        for index, left in enumerate(drawn):
            for right in drawn[index + 1 :]:
                direct, flipped = _seam_distance(
                    cells(left), cells(right), window=window
                )
                across[(left.key, right.key)] = _seam_record(
                    left.key, right.key, direct, flipped
                )
        # A strict majority of ALL the states that draw this direction, never a
        # majority of the OTHER ones. `len(pairs) // 2 + 1` was 2 of 3 on a
        # four-state sheet, so a 2-2 split left every row inside a "majority"
        # and convicted all four — the two CORRECT ones at +14.31% basis
        # `states`, with no corroborating marker at all, while their rotation
        # readings sat at -15.97%. `len(drawn) // 2 + 1` is the same number for
        # three states and for five, and one more for four, which is what makes
        # an even split convict nobody: a row is convicted only when its camp is
        # a strict MINORITY of the states — the same argument that forces the
        # three-state minimum above.
        needed = len(drawn) // 2 + 1
        entries: list[dict] = []
        against: dict[str, int] = {}
        for row in drawn:
            pairs = [seam for pair, seam in across.items() if row.key in pair]
            ranked = sorted(
                (seam for seam in pairs if _gain([seam]) is not None),
                key=lambda seam: _gain([seam]),
                reverse=True,
            )
            if len(ranked) < needed:
                # Never a bare `continue`: a row that vanishes from the payload
                # reads exactly like a clean one. Same accounting rule as the
                # rotation's `as_drawn <= 0` case.
                unjudged.append(
                    {
                        "rows": [row.key],
                        "basis": "states",
                        "reason": (
                            f"only {len(ranked)} of its {len(pairs)} cross-state "
                            "pairs measure anything, and a conviction here needs "
                            f"{needed} of {len(drawn)} states to disagree with it"
                        ),
                    }
                )
                continue
            against[row.key] = sum(
                1 for seam in ranked if _gain([seam]) >= MIRROR_GAIN_THRESHOLD
            )
            entries.append(
                {
                    "row": row.key,
                    "state": row.state,
                    "direction": row.direction,
                    "gain": _gain([ranked[needed - 1]]),
                    "basis": "states",
                    "seams": _seam_evidence(row.key, ranked[:needed]),
                }
            )
        # An even split is every row landing exactly ONE short of the conviction
        # line — which is why it is spelled `needed - 1` and not `len(drawn) //
        # 2`. The two are equal, and writing the second one made this branch
        # able to mask a wrong `needed`: with the old `len(pairs) // 2 + 1` the
        # conviction line drops to 2 of 4 and every row of a 2-2 split is
        # convicted, but a branch keyed to its own arithmetic still fired first
        # and hid it. One knob, one place.
        if (
            len(drawn) % 2 == 0
            and len(against) == len(drawn)
            and all(count == needed - 1 for count in against.values())
        ):
            # Every state disagrees with exactly half the others: two camps of
            # equal size, and nothing inside the sheet says which camp holds the
            # mirrored art. Reporting a gain here would read as a clean pass.
            unjudged.append(
                {
                    "rows": [row.key for row in drawn],
                    "basis": "states",
                    "reason": (
                        f"the {len(drawn)} states that draw {direction!r} split "
                        f"evenly, {needed - 1} against {needed - 1} — neither "
                        "camp is a "
                        "minority, so this pass cannot say which half is mirrored"
                    ),
                }
            )
            continue
        judged.extend(entries)
        # Counted per STATE, not per direction, because the whole-state rule
        # below asks "did EVERY row of this state that anyone could answer for
        # read as a mirror?" — and a row this pass gave up on (unjudged above)
        # must not be silently counted as agreement in either direction.
        for entry in entries:
            states_judged_per_state[entry["state"]] = (
                states_judged_per_state.get(entry["state"], 0) + 1
            )
        state_flagged.extend(
            dict(entry, corroborating=[], alternatives=[])
            for entry in entries
            if entry["gain"] >= MIRROR_GAIN_THRESHOLD
        )

    cross_gain = {
        entry["row"]: entry["gain"] for entry in judged if entry["basis"] == "states"
    }
    convicted_per_state: dict[str, int] = {}
    for finding in state_flagged:
        convicted_per_state[finding["state"]] = (
            convicted_per_state.get(finding["state"], 0) + 1
        )

    # THE WHOLE-STATE RULE (owner ruling 2026-08-25). A state whose EVERY
    # cross-state-judged row reads as a mirror is a whole state drawn backwards,
    # and that is an ERROR on this one basis. It is not a second basis and it
    # must not be mistaken for one: it is a second-order CONSENSUS over the same
    # pass, and it is the only reading that can exist for this defect, because
    # the rotation is a FIXED POINT of a wholly mirrored state — flip every row
    # of one state and its chain still fits itself perfectly. Waiting for a
    # second basis here means waiting forever.
    #
    # The shape is exactly what `add_state` produces: all of a new state's rows
    # in ONE batch, against one reference and one prompt — the same generation
    # shape that drew `ne` backwards three times along the other axis.
    #
    # TWO guards, and both are load-bearing:
    #
    #   * `>= 2` rows. A state with ONE judged row is the single-row case, which
    #     the ruling leaves a WARNING; escalating it would be the 4-way scheme's
    #     whole answer (`turnaround_order(...)[1:-1]` is one direction there), so
    #     without this guard a 4-way sheet would refuse on exactly the reading
    #     the owner declined to escalate.
    #   * EVERY judged row, never a majority. One row of the state judged CLEAN
    #     is the sheet saying the state faces the right way somewhere, which is
    #     a contiguous block of mirrored rows, not a mirrored state.
    #
    # What this buys and what it costs, said out loud: it makes the `add-state`
    # defect blocking on the only pass that can see it, and it makes a whole
    # state of CORRECT art that is displaced in every direction (one prop, drawn
    # in every direction of one state — the false population measured at +18.75%
    # on a single row) blocking too. That is what `--accept-handedness` is for,
    # and why the override had to work per row on this finding as well.
    whole_state_rows: dict[str, list[str]] = {}
    for state, convicted in convicted_per_state.items():
        if convicted >= 2 and convicted == states_judged_per_state.get(state, 0):
            whole_state_rows[state] = sorted(
                (
                    finding["row"]
                    for finding in state_flagged
                    if finding["state"] == state
                ),
                key=lambda key: rows_by_key[key].index,
            )

    # The rotation pass.
    rotation_scored: list[tuple[int, dict]] = []
    per_state_scored: list[list[tuple[int, dict]]] = []
    for state in spec.states:
        if not state.directional:
            unjudged.append(
                {
                    "rows": [row_key(state.name, None)],
                    "basis": "rotation and states",
                    "reason": (
                        "state is not directional — it has no rotation to walk, "
                        "and no other state holds a copy of a direction to "
                        "compare it against"
                    ),
                }
            )
            continue

        chain = [
            rows_by_key[row_key(state.name, direction)]
            for direction in turnaround_order(spec.scheme.authored)
        ]
        seams: dict[tuple[str, str], dict] = {}
        for left, right in zip(chain, chain[1:]):
            if blank(left) or blank(right):
                continue
            direct, flipped = _seam_distance(cells(left), cells(right), window=window)
            seams[(left.key, right.key)] = _seam_record(
                left.key, right.key, direct, flipped
            )

        scored: list[tuple[int, dict]] = []
        for position, row in enumerate(chain):
            if blank(row):
                unjudged.append(
                    {
                        "rows": [row.key],
                        "basis": "rotation",
                        "reason": "the row is empty — an empty row has no facing",
                    }
                )
                continue
            before = seams.get((chain[position - 1].key, row.key)) if position else None
            after = (
                seams.get((row.key, chain[position + 1].key))
                if position + 1 < len(chain)
                else None
            )
            touching = [seam for seam in (before, after) if seam is not None]
            if len(touching) < 2:
                unjudged.append(
                    {
                        "rows": [row.key],
                        "basis": "rotation",
                        "reason": (
                            f"{len(touching)} of the two seams it needs — a row is "
                            "judged only with a measurable neighbour on EACH side, "
                            "because one seam cannot say WHICH of the two rows "
                            "either side of it is mirrored (flipping either scores "
                            "identically). The ends of the rotation always land "
                            "here, and they are also the two views closest to their "
                            "own mirror image, so there is little to see"
                        ),
                    }
                )
                continue
            gain = _gain(touching)
            if gain is None:
                unjudged.append(
                    {
                        "rows": [row.key],
                        "basis": "rotation",
                        "reason": (
                            "both of its seams measure zero — a row identical to "
                            "its neighbours carries no handedness signal"
                        ),
                    }
                )
                continue
            scored.append(
                (
                    position,
                    {
                        "row": row.key,
                        "state": row.state,
                        "direction": row.direction,
                        "gain": gain,
                        "basis": "rotation",
                        "seams": _seam_evidence(row.key, touching),
                    },
                )
            )

        judged.extend(entry for _position, entry in scored)
        per_state_scored.append(scored)
        rotation_scored.extend(scored)

    # A direction the rotation suspects in a strict MAJORITY of the states that
    # judged it is the signature of a direction drawn the same wrong way every
    # time — which is exactly the case the cross-state pass is blind to, so its
    # silence there must not be read as a character reference.
    judged_per_direction: dict[str, int] = {}
    over_per_direction: dict[str, int] = {}
    for _position, entry in rotation_scored:
        judged_per_direction[entry["direction"]] = (
            judged_per_direction.get(entry["direction"], 0) + 1
        )
        if entry["gain"] >= MIRROR_GAIN_THRESHOLD:
            over_per_direction[entry["direction"]] = (
                over_per_direction.get(entry["direction"], 0) + 1
            )
    suspected = {
        direction
        for direction, seen in judged_per_direction.items()
        if seen >= 2 and over_per_direction.get(direction, 0) * 2 > seen
    }

    rotation_flagged: list[dict] = []
    for scored in per_state_scored:
        rotation_flagged.extend(_run_findings(scored, cross_gain, suspected))

    flagged: list[dict] = list(rotation_flagged)
    by_row = {finding["row"]: finding for finding in flagged}
    for finding in state_flagged:
        # The cross-state pass names one row, never a neighbourhood, so it has
        # no run to attribute. It still has to answer the same question the
        # rotation does: is this row named on evidence, or only ranked? A row
        # whose rotation reading CONTRADICTS the states one is named only when
        # its state is convicted as a whole — the `add-state` shape, where the
        # rotation is a fixed point and its silence means nothing.
        rotation_gain = next(
            (
                entry["gain"]
                for entry in judged
                if entry["row"] == finding["row"] and entry["basis"] == "rotation"
            ),
            None,
        )
        finding["attributed"] = not (
            rotation_gain is not None
            and rotation_gain < 0
            and convicted_per_state.get(finding["state"], 0) < 2
        )
        finding["attribution"] = "states" if finding["attributed"] else "contradicted"
        existing = by_row.get(finding["row"])
        if existing is None:
            # A row that only rode along as corroborating now has evidence of its
            # own: stop telling the operator not to touch it.
            for other in flagged:
                other["corroborating"] = [
                    entry
                    for entry in other["corroborating"]
                    if entry["row"] != finding["row"]
                ]
            flagged.append(finding)
            by_row[finding["row"]] = finding
        else:
            existing["basis"] = "rotation and states"
            existing["seams"] = existing["seams"] + finding["seams"]
            existing["gain"] = max(existing["gain"], finding["gain"])
            existing["attributed"] = True
            existing["attribution"] = "both"
            existing["alternatives"] = []

    for finding in flagged:
        # THE SEVERITY RULE, in two lines because there are two ways to refuse.
        #
        # (1) A single basis about a single ROW warns; two independent bases
        # agreeing about it REFUSE. The two populations do not separate on one
        # reading — measured in both directions, the true floor on real art is
        # +6.78% rotation / +7.64% states (`jumping-se` mirrored, caught by
        # neither pass) and the false ceiling on CORRECT art displaced sideways
        # is +18.75%. An 8% line does not sit BETWEEN two populations there; it
        # sits inside both of them. Moving the number cannot fix that, so what
        # moved instead is what a single reading is allowed to DO.
        #
        # (2) A whole STATE reading as mirrored REFUSES on one basis or two
        # (`whole_state_rows` above). That is not the rule in (1) relaxed: the
        # evidence is every judged row of the state agreeing, which the rotation
        # can never corroborate because it is blind to this defect by algebra.
        # `wholeState` carries the roster rather than a bare flag, so the
        # message can name the state's rows and nothing has to re-derive them.
        if "states" in finding["basis"] and finding["row"] in whole_state_rows.get(
            finding["state"], ()
        ):
            finding["wholeState"] = list(whole_state_rows[finding["state"]])
        finding["severity"] = (
            "error"
            if finding["basis"] == "rotation and states" or finding.get("wholeState")
            else "warning"
        )

    flagged.sort(key=lambda finding: rows_by_key[finding["row"]].index)
    return {"flagged": flagged, "judged": judged, "unjudged": unjudged}


# How an operator spells WHICH evidence they are waiving, per finding. There is
# no single constant here any more and there must not be one: two shapes block
# now — a row two bases agree about (`rotation+states`) and a row carried by a
# whole mirrored STATE (`states`) — and one hardcoded token would have made the
# second unacceptable at all, which is an error with no override, which is a
# wall. The token is DERIVED from the finding's own basis so the two can never
# drift apart: `validate_sheet` demands it and `mirrored_art_error` prints it,
# both through this one function.
_ACCEPT_BASIS_TOKENS = {
    "rotation": "rotation",
    "states": "states",
    "rotation and states": "rotation+states",
}


def accept_basis_token(basis: str) -> str:
    """The ``--accept-handedness`` basis token for a finding on *basis*.

    Public because the refusal that demands the spelling and the message that
    teaches it are in two places, and a second spelling of this map is how an
    operator gets told to type something the validator then rejects.
    """
    try:
        return _ACCEPT_BASIS_TOKENS[basis]
    except KeyError:  # pragma: no cover - a new basis would be a code change
        raise ValueError(f"no acceptance token for basis {basis!r}") from None


_MIRROR_BASIS = {
    "rotation": "flipping it fits its neighbours in the rotation",
    "states": "flipping it fits the same direction in the other states",
    "rotation and states": (
        "flipping it fits both its neighbours in the rotation and the same "
        "direction in the other states"
    ),
}


# The one spelling of "this row was named, and here is what it costs to obey a
# name that is wrong". Every branch of :func:`mirrored_art_error` quotes it
# exactly once, on the line that hands over an action — which is why the two
# ERROR branches carry it now. Before this it rode only on the corroborating
# tail, so a refusal with no corroborating rows told an operator to re-roll and
# never told them the re-roll could not be taken back.
_REROLL_IS_ONE_WAY = (
    "a re-roll auto-approves and there is no approve-row verb to undo it, so "
    "obeying a name that is wrong spends correct approved art"
)


# Why ``--accept-handedness`` is NOT the cheap door, said on the line that
# offers it. The override is the only way past a refusal (``compose`` has no
# other), so leaving it out of the text left an operator who had LOOKED at the
# art with one instruction — re-roll — that destroys the approved art they had
# just judged correct. It is shown, and it is shown with its price: a re-roll is
# private, an acceptance is a permanent public fact about the character.
_ACCEPT_IS_A_RECORD = (
    "an acceptance writes {row, gain, basis} onto the installed manifest as "
    "handednessAccepted, and characters list, sprite_payload and the launcher's "
    "bundle warnings all republish it for the life of the character"
)


def _block(headline: str, rows: Sequence[tuple[str, str]]) -> str:
    """A finding rendered as a headline plus one ``label: value`` line per fact.

    **No hard wrap, deliberately.** The whole diagnostic used to be one
    unwrapped paragraph — measured 2026-08-26 on the live sheet at 1206
    characters on a single line, and 1519 when a malformed acceptance made the
    validator restate the same finding a second time underneath the acceptance
    error. That survives a terminal by accident and cannot be rendered on a
    console card at all, which is the launcher surface that has to show exactly
    this text.

    Wrapping it here would only move the problem: a column count chosen in this
    module is wrong for every consumer that is not an 80-column terminal. What
    both consumers need is SEPARABLE FACTS — a first line that stands alone, and
    one self-contained field per line after it. A terminal soft-wraps them; a
    card wraps each field on its own width, or shows the headline and discloses
    the rest.
    """
    width = max(len(label) for label, _text in rows) + 1
    return "\n".join(
        [headline] + [f"  {label + ':':<{width}}  {text}" for label, text in rows]
    )


def _disposition(finding: dict, accepted: bool) -> str:
    """The headline's verdict word: what this finding DID to the compose.

    Read off ``severity`` rather than typed per branch, so a branch cannot
    announce a refusal the severity rule did not make.
    """
    if accepted:
        return "WAIVED by the operator, this install carries it"
    return "REFUSED" if finding.get("severity") == "error" else "WARNING, does not block"


def _corroborating_rows(finding: dict) -> list[tuple[str, str]]:
    """The "these neighbours are NOT separate faults" line, when there are any.

    Its "Do NOT re-roll them" is the load-bearing half: a mirrored row pulls
    both its neighbours toward the line, and an operator who obeys a three-row
    refusal literally spends two correct approved attempts that no verb can give
    back. The one-way cost itself is stated once per block, on the action line
    below this one, so it is said where the verb is handed over rather than
    twice in one message.
    """
    if not finding.get("corroborating"):
        return []
    names = ", ".join(
        f"'{entry['row']}' {entry['gain'] * 100:.0f}%"
        for entry in finding["corroborating"]
    )
    return [
        (
            "also high",
            f"{names} read high too and are NOT separate faults: a mirrored row "
            "pulls the seams of the rows either side of it toward the line as "
            "well. Do NOT re-roll them. Fix this row, compose again, and judge "
            "what is left then.",
        )
    ]


def _accept_rows(
    finding: dict, accepted: bool, *, looked: str
) -> list[tuple[str, str]]:
    """The override lines — the door, and its price; or the record of its use.

    Two lines rather than one because they are two facts, and the whole shape
    of this message is one self-contained fact per line. Splitting them also
    keeps the longest line in the block off 450 characters, which is where the
    unsplit version landed.

    The token comes from :func:`accept_basis_token` on this finding's own basis,
    which is the same call :func:`validate_sheet` makes when it decides whether
    to honour what the operator typed. One function, so what an operator is told
    to type and what the validator accepts cannot drift apart.
    """
    token = f"{finding['row']}:{accept_basis_token(finding['basis'])}"
    if accepted:
        return [
            (
                "recorded",
                f"--accept-handedness {token} was given, and it is a fact about "
                f"this character now, not a refusal that vanished: "
                f"{_ACCEPT_IS_A_RECORD}.",
            )
        ]
    return [
        (
            "accept",
            f"only if you have LOOKED at {looked} and the art is right: compose "
            f"--accept-handedness {token}. It is the more expensive door, not the "
            "cheaper one, and it is not a way to make the check quiet.",
        ),
        (
            "on record",
            f"{_ACCEPT_IS_A_RECORD}. Say in the turn what you saw on the art.",
        ),
    ]


def mirrored_art_error(
    finding: dict, *, acceptance_error: str | None = None, accepted: bool = False
) -> str:
    """The operator-facing text for ONE :func:`detect_mirrored_art` finding.

    Public so the message has a single spelling: the validator raises it and a
    test asserts it, and a second copy is how a refusal starts naming a verb
    that no longer exists.

    It renders four different things, and the difference is the point.
    ``severity: "error"`` blocks a compose and is the only severity that hands
    the operator a ``reroll-row`` command; it arrives in two shapes, and they
    get two texts because they have two remedies — two independent bases
    agreeing about ONE row (re-roll that row), and a WHOLE STATE whose every
    judged row reads mirrored (re-roll the state, and do not expect a second
    basis that cannot exist). A single-basis finding about a single row WARNS
    and says so, because one basis does not separate the two populations. An
    UNATTRIBUTED finding names no row at all: it lists the run and says the
    rotation alone cannot tell which of them is the fault, which is true in the
    two shapes where the run's maximum is an innocent row (see
    :func:`_attribute_run`).

    **Both blocking shapes show the override, and that is a decision rather than
    a slip** (2026-08-26). It was already shown on one of them — the whole-state
    branch spelled ``--accept-handedness`` and the two-basis branch did not — so
    what shipped was not a policy of not advertising the hatch but a drift
    between two spellings of the same thing, the exact class
    :func:`accept_basis_token` exists to retire. Owner ruling 18 tightens this
    gate *because* the row-named override is reachable, and an override an
    operator can only find by guessing the flag name is not reachable in any
    sense that argument can use. It is shown AFTER the re-roll line, conditional
    on having LOOKED at the strip, and priced (:data:`_ACCEPT_IS_A_RECORD`) so it
    cannot read as the cheap way out: a re-roll is private and only ever costs
    art, while an acceptance follows the character to the manifest, to
    ``characters list``, to ``sprite_payload`` and to the launcher's bundle
    warnings for good.

    *acceptance_error* folds the operator's own malformed ``--accept-handedness``
    token into THIS row's block. It exists because the two used to be separate
    entries in ``errors`` about the same row, so a bare row name printed the
    acceptance complaint and then the entire diagnostic again underneath it.

    Every branch that teaches an override spells it through
    :func:`accept_basis_token` on THIS finding's basis, so the token an operator
    is told to type is the token :func:`validate_sheet` will accept.
    """
    row = finding["row"]
    evidence = "; ".join(
        f"vs '{seam['with']}' {seam['distance']:.2f} -> "
        f"{seam['mirroredDistance']:.2f} flipped"
        for seam in finding["seams"]
    )
    corrupts = (
        "A mirrored authored row corrupts the derived direction with it, "
        "because the consumer builds that one by flipping this row."
    )
    rows: list[tuple[str, str]] = []
    if acceptance_error:
        rows.append(("you typed", acceptance_error))

    if not finding.get("attributed", True):
        alternatives = finding.get("alternatives") or [finding]
        ranked = ", ".join(
            f"'{entry['row']}' {entry['gain'] * 100:.0f}%" for entry in alternatives
        )
        why = (
            "flagged rows next to each other raise each other, and the rotation "
            "cannot say which of them started it — a correct row slid sideways, "
            "and a correct row flanked by two mirrored ones, both put an "
            "innocent row at the top. Only a second, independent read takes a "
            "run apart, and a third state is what provides one"
            if finding.get("attribution") == "run"
            else "the same direction in the other states vouches for it, so this "
            "reads as PLACEMENT — a prop or a framing drift — rather than "
            "handedness"
        )
        rows += [
            ("ranked", ranked),
            ("seams", evidence),
            (
                "why",
                f"it is NOT attributed to {row!r} or to any other single row — "
                f"{why}",
            ),
            ("corrupts", corrupts),
            (
                "look",
                "crop these rows and look at them. Do not re-roll on this alone: "
                f"{_REROLL_IS_ONE_WAY}. Reach for --accept-handedness only on a "
                "finding that actually blocks.",
            ),
        ]
        return _block(
            f"one of {len(alternatives)} rows in {finding['state']!r} reads as a "
            f"MIRROR and this pass cannot say which — "
            f"{_disposition(finding, accepted)}",
            rows,
        )

    if finding.get("wholeState"):
        roster = ", ".join(f"'{key}'" for key in finding["wholeState"])
        rows += [
            (
                "state",
                f"every one of the {len(finding['wholeState'])} rows of "
                f"{finding['state']!r} this pass could judge ({roster}) reads as "
                "the mirror of the direction it claims",
            ),
            (
                "reads",
                f"this row: {_MIRROR_BASIS[finding['basis']]} "
                f"{finding['gain'] * 100:.0f}% better",
            ),
            ("seams", evidence),
            (
                "blocks",
                "ONE basis refuses here, and that is not the single-row rule "
                "relaxed — a wholly mirrored state is a FIXED POINT of the "
                "rotation pass (flip every row of a state and its chain still "
                "fits itself), so the rotation's silence is not a second opinion "
                "and no second basis can ever arrive. It is the shape add-state "
                "produces: one batch, one reference, one prompt.",
            ),
            ("corrupts", corrupts),
        ]
        rows += _corroborating_rows(finding)
        rows += [
            (
                "re-roll",
                f"characters reroll-row --row {row} --note ... — and the same for "
                "the state's other rows, with the facing spelled in frame terms; "
                "look at the strips before composing. Be sure first: "
                f"{_REROLL_IS_ONE_WAY}.",
            ),
        ]
        rows += _accept_rows(finding, accepted, looked="the strips")
        return _block(
            f"row {row!r} belongs to a WHOLE STATE that reads as MIRRORED — "
            f"{_disposition(finding, accepted)}",
            rows,
        )

    if finding.get("severity") == "error":
        rows += [
            (
                "reads",
                f"{_MIRROR_BASIS[finding['basis']]} "
                f"{finding['gain'] * 100:.0f}% better",
            ),
            ("seams", evidence),
            (
                "blocks",
                "two independent reads agree about this one row, which is what "
                "refuses an install",
            ),
            ("corrupts", corrupts),
        ]
        rows += _corroborating_rows(finding)
        rows += [
            (
                "re-roll",
                f"characters reroll-row --row {row} --note ... — with the facing "
                "spelled in frame terms, and look at the strip before composing. "
                f"Be sure first: {_REROLL_IS_ONE_WAY}.",
            ),
        ]
        rows += _accept_rows(finding, accepted, looked="this row's strip")
        return _block(
            f"row {row!r} looks drawn as the MIRROR of {finding['direction']!r} — "
            f"{_disposition(finding, accepted)}",
            rows,
        )

    rows += [
        (
            "reads",
            f"{_MIRROR_BASIS[finding['basis']]} {finding['gain'] * 100:.0f}% better",
        ),
        ("seams", evidence),
        (
            "warns",
            "one basis is a WARNING and does not block the install — the true and "
            "false populations overlap on a single read (the quietest true "
            "reading measured on real art is +6.8%, the loudest false one "
            "+18.8%), so this cannot be told apart from placement on its own",
        ),
        ("corrupts", corrupts),
    ]
    rows += _corroborating_rows(finding)
    rows += [
        ("look", f"crop this row and look before you re-roll: {_REROLL_IS_ONE_WAY}."),
        (
            "next",
            "a third state is what gives the cross-state pass something to say, "
            "and two bases agreeing is what refuses an install.",
        ),
    ]
    return _block(
        f"row {row!r} reads as the MIRROR of {finding['direction']!r} on ONE basis "
        f"— {_disposition(finding, accepted)}",
        rows,
    )


def handedness_summary(handedness: dict) -> str:
    """One line saying what the handedness check could and could not answer for.

    Counts ROWS, not findings, and "unjudged" here means *no pass answered for
    it* — a row the rotation judged is not reported as unjudged just because the
    cross-state pass had only one state to work with.

    Exists because the payload it summarises was, until 2026-08-25, read by
    nothing outside this module and its tests: ``compose`` printed ``WxH`` and
    raised on a refusal, so on a clean sheet an operator never learned that six
    of fifteen rows were never judged, and on a refusal the whole payload — the
    unjudged list included — was discarded exactly when it mattered.
    """
    judged = {entry["row"] for entry in handedness["judged"]}
    unjudged = sorted(
        {row for entry in handedness["unjudged"] for row in entry["rows"]} - judged
    )
    parts = [f"{len(judged)} row(s) judged"]
    accepted = handedness.get("accepted") or []
    accepted_rows = {entry["row"] for entry in accepted}
    blocking = [
        finding
        for finding in handedness["flagged"]
        if finding.get("severity") == "error" and finding["row"] not in accepted_rows
    ]
    warned = [
        finding
        for finding in handedness["flagged"]
        if finding.get("severity") != "error" and finding["row"] not in accepted_rows
    ]
    if blocking:
        parts.append(f"{len(blocking)} refused")
    if warned:
        # Named, not just counted: a warning no longer blocks, so the only thing
        # standing between it and a shipped mirrored row is somebody reading it.
        parts.append(
            f"{len(warned)} warned ({', '.join(sorted(f['row'] for f in warned))})"
        )
    if accepted:
        parts.append(
            f"{len(accepted)} accepted by the operator "
            f"({', '.join(entry['row'] for entry in accepted)})"
        )
    if unjudged:
        parts.append(f"{len(unjudged)} unjudged ({', '.join(unjudged)})")
    return "handedness: " + ", ".join(parts)


def _rgb_residue_count(rgba) -> int:
    """Transparent pixels that still carry colour, counted in Pillow's C loops.

    The same predicate the pixel-by-pixel version used — ``alpha == 0 and any of
    R, G, B``— expressed as band operations, because this walks every pixel of
    the sheet on EVERY compose and the Python loop was 0.39 s of a 3.0 s
    ``validate_sheet`` on a 576x3120 fixture (12x, measured 2026-08-25). Pillow
    only: ``numpy`` is absent from the venv this pipeline runs in.
    """
    from PIL import ImageChops

    red, green, blue, alpha = rgba.split()
    coloured = ImageChops.lighter(ImageChops.lighter(red, green), blue).point(
        lambda value: 255 if value else 0
    )
    clear = alpha.point(lambda value: 255 if value == 0 else 0)
    return sum(ImageChops.multiply(coloured, clear).histogram()[1:])


def validate_sheet(
    spec: SheetSpec, image, *, accept_handedness: Sequence[str] = ()
) -> dict:
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
    so a caller can always see which rows this could not answer for.

    **Only a finding carrying ``severity: "error"`` blocks**, and there are two
    such shapes (see :func:`detect_mirrored_art`): one row that BOTH passes
    agree about, and a whole STATE whose every judged row reads as a mirror. A
    single-basis finding about a single row is a WARNING with the whole text
    intact. That is not a softening for convenience, it is what the measurements
    force: the true floor on real art is +6.78% rotation / +7.64% states and the
    false ceiling on correct art displaced sideways is +18.75%, so on ONE
    reading about ONE row the two populations overlap and no threshold separates
    them. Round one made every flagged row a refusal, which bought certainty it
    did not have and pointed ``reroll-row`` at correct art in two reachable
    shapes. Say the consequence out loud: ``characters start`` creates ``idle:6,
    walk:8``, the cross-state pass needs three states, so on the DEFAULT
    character neither refusal is reachable and this check can only ever warn —
    and on a two-state cut of the live art a whole mirrored state already scored
    bit-identical to the correct sheet, so it was nearly blind there before any
    of this existed.

    **``accept_handedness`` is the one way past a refusal, it names rows, and it
    names the basis with them.** It applies to the ERROR cases only: there is
    nothing to accept about a warning, which does not block. A row refused on
    two bases is refused by two independent bodies of evidence, and a bare row
    name waived both at once — an operator accepting a PLACEMENT reading also
    silenced the cross-state one, which placement cannot explain. So the token
    names what is being waived and is DERIVED from the finding
    (:func:`accept_basis_token`): ``<row>:rotation+states`` for a two-basis
    refusal, ``<row>:states`` for a whole-state one. A bare ``<row>`` is refused
    with the spelling it needs. **Both error shapes are overridable, and that is
    a requirement rather than a convenience: an error with no way past it is a
    wall, and ``compose`` has no other door.** A whole-state refusal is still
    accepted one ROW at a time — a state-wide reading waived state-wide, in one
    token, is the blanket this grammar exists to refuse. An accepted row becomes
    a warning that still carries the whole refusal text, and rides in
    ``handedness["accepted"]`` as ``{row, gain, basis}`` — accepting a +40%
    finding and an +8.1% one used to be indistinguishable afterwards. Naming a
    row that was NOT flagged is itself an error: an acceptance with nothing to
    accept is a bypass lying in wait for the next refusal.

    **A malformed acceptance is folded into the block for the row it names**,
    rather than appended beside it. A bare row name used to produce two entries
    in ``errors`` about one finding — the acceptance complaint, and then the
    entire diagnostic again underneath it — so the message an operator got for
    typing the flag wrong was LONGER than the refusal that taught them the flag,
    and 79% of it was text they had just read. One row is one block, and the
    spelling the validator wants is printed once, on that block's ``accept``
    line, through :func:`accept_basis_token`. An acceptance naming a row that is
    not on the sheet, was never flagged, or only warned has no block to fold
    into and stays an error of its own.
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
                "accepted": [],
                "judged": [],
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

    residue = _rgb_residue_count(rgba)
    if residue:
        errors.append(f"{residue} transparent pixels retain RGB residue")

    # Last, after the collapse/outlier/residue checks above — but NOT conditional
    # on them: only the wrong-SIZE early return short-circuits this, and every
    # other error still leaves the handedness answer in the payload. Its findings
    # are errors unless the operator accepted that row by name — see the
    # docstring for why this one is not allowed to be a plain warning.
    handedness = detect_mirrored_art(spec, rgba)
    flagged_by_row = {finding["row"]: finding for finding in handedness["flagged"]}
    known_rows = {row.key for row in spec.rows()}
    accepted: list[dict] = []
    # A complaint about a MALFORMED acceptance is folded into the block for the
    # row it is about, never appended beside it. Both used to be entries in
    # `errors` about the same finding, so `--accept-handedness walk-e` printed
    # the acceptance complaint and then the whole diagnostic a second time
    # underneath it — measured 2026-08-26 at 1519 characters against the plain
    # refusal's 1206, of which 1206 was text the operator had just read. There
    # is one row, so there is one block.
    acceptance_notes: dict[str, list[str]] = {}
    for token in dict.fromkeys(str(row).strip() for row in accept_handedness):
        if not token:
            continue
        key, _colon, basis = token.partition(":")
        key = key.strip()
        basis = basis.strip()
        finding = flagged_by_row.get(key)
        if key not in known_rows:
            errors.append(
                f"handedness acceptance names {key!r}, which is not a row of this "
                f"sheet ({', '.join(sorted(known_rows))})"
            )
        elif finding is None:
            errors.append(
                f"handedness acceptance names {key!r}, which was not flagged — an "
                "acceptance with nothing to accept is a bypass waiting for the "
                "next refusal; drop it"
            )
        elif finding.get("severity") != "error":
            errors.append(
                f"handedness acceptance names {key!r}, which is a WARNING and "
                "does not block this install — there is nothing to accept. Only "
                "a row both passes agree about is refused; drop it"
            )
        elif not basis:
            acceptance_notes.setdefault(key, []).append(
                f"--accept-handedness {key}, with no basis. "
                + (
                    "That row is refused because its whole state reads as "
                    "mirrored, and the evidence is every judged row of "
                    f"{finding['state']!r} — a bare row name waives a "
                    "state-wide reading one row at a time without saying so. "
                    if finding.get("wholeState")
                    else "That row is refused because TWO independent reads "
                    "agree about it, and a bare row name waives both — "
                    "including the cross-state evidence, which a placement or "
                    "framing argument cannot explain. "
                )
                + "Name what you are waiving; the spelling this finding needs is "
                "on the accept line below."
            )
        elif basis != accept_basis_token(finding["basis"]):
            acceptance_notes.setdefault(key, []).append(
                f"--accept-handedness {key}:{basis}, but this finding's bases are "
                f"{finding['basis']!r}, so the acceptance is spelled "
                f"{key}:{accept_basis_token(finding['basis'])}."
            )
        else:
            accepted.append(
                {"row": key, "gain": finding["gain"], "basis": finding["basis"]}
            )
    handedness["accepted"] = accepted
    accepted_rows = {entry["row"] for entry in accepted}
    for finding in handedness["flagged"]:
        waived = finding["row"] in accepted_rows
        message = mirrored_art_error(
            finding,
            acceptance_error=" ".join(acceptance_notes.get(finding["row"], ())) or None,
            accepted=waived,
        )
        if waived:
            warnings.append(f"handedness accepted by the operator — {message}")
        elif finding.get("severity") == "error":
            errors.append(message)
        else:
            # The disposition rides on the block's own headline now, so the
            # list-level tag says only which list this is.
            warnings.append(f"handedness warning — {message}")

    return {
        "ok": not errors,
        "width": rgba.width,
        "height": rgba.height,
        "errors": errors,
        "warnings": warnings,
        "filled_rows": filled_rows,
        "handedness": handedness,
    }
