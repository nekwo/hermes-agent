"""Palette extraction + lock — the structural fix for cross-row colour drift.

Every animation row of a character sheet is a separate model call, so the same
jacket comes back a slightly different blue in each one. Grounding on an
approved reference narrows that drift but never closes it. The lock closes it by
construction: one palette is derived from the approved direction references, and
every composed cell is quantized *into* that palette, so a colour that is not in
the reference set cannot reach the sheet. Accepted cost — a genuinely novel
accent colour snaps to its nearest approved neighbour (plan H §7.2).

Two constraints shape the implementation:

* **Alpha is carried separately.** ``Image.quantize`` only accepts ``RGB``/``L``
  input, and converting an RGBA sprite to ``P`` flattens the cutout's
  transparency into an opaque colour index. So the RGB plane is quantized and the
  *original* alpha channel is reattached byte-for-byte — semi-transparent edge
  pixels keep their exact alpha, and only fully-transparent pixels are forced to
  ``(0, 0, 0, 0)``.
* **Dither must be off.** Floyd-Steinberg (Pillow's default) spreads error into
  neighbouring pixels, which on a 192x208 sprite cell reads as speckle and
  defeats the whole point of a fixed palette.

Pillow is imported lazily inside each function, matching
:mod:`agent.pet.generate.atlas`: the module must import in a plain-hermes
install with no Pillow so the CLI can report the degraded state instead of
crashing at import.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# Alpha at/below which a pixel is background and contributes no colour. Same
# floor as the pet atlas's component detection, so the palette is built from
# exactly the pixels that survive keying.
ALPHA_FLOOR = 16

# Palette size the plan settled on: wide enough for a shaded character, narrow
# enough that drift has nowhere to hide.
DEFAULT_MAX_COLORS = 48

# Upper bound on the pixels fed to the median cut. Colour *frequency* has to
# survive into the sample (a one-pixel antialias speck must not outvote a body
# colour), but the sources are five ~1536x512 references, so counts are scaled
# down to this budget rather than replayed pixel for pixel.
_MAX_SAMPLE_PIXELS = 1 << 16

_SAMPLE_ROW_WIDTH = 256


def _as_rgba(source):
    """An RGBA image from an image or a path (paths are opened and closed)."""
    from PIL import Image

    if isinstance(source, (str, Path)):
        with Image.open(source) as opened:
            return opened.convert("RGBA")
    return source.convert("RGBA")


def build_palette(images: Iterable, max_colors: int = DEFAULT_MAX_COLORS):
    """Build a ``P``-mode palette image from the opaque pixels of *images*.

    *images* are RGBA images or paths; only pixels with ``alpha > ALPHA_FLOOR``
    vote, so transparent margins and keyed-out backdrops contribute nothing. The
    result is the palette argument :func:`lock_to_palette` expects.

    Deterministic: colours are accumulated into counts and replayed in a fixed
    (count, colour) order, so the same references always yield the same palette
    regardless of file order or dict iteration.
    """
    from PIL import Image

    if not isinstance(max_colors, int) or isinstance(max_colors, bool):
        raise ValueError(f"max_colors must be an int, got {max_colors!r}")
    if not 2 <= max_colors <= 256:
        raise ValueError(f"max_colors must be 2..256, got {max_colors}")

    sources = list(images)
    if not sources:
        raise ValueError("build_palette needs at least one image to sample")

    counts: dict[tuple[int, int, int], int] = {}
    for source in sources:
        rgba = _as_rgba(source)
        # getcolors is a C-level histogram; the cap is the pixel count so it can
        # only return None for an image with more distinct colours than pixels
        # (impossible), never for a photographic reference.
        colors = rgba.getcolors(maxcolors=max(1, rgba.width * rgba.height))
        if colors is None:  # pragma: no cover — unreachable by the line above
            raise ValueError(f"could not histogram {source!r}: too many distinct colours")
        for count, pixel in colors:
            red, green, blue, alpha = pixel
            if alpha <= ALPHA_FLOOR:
                continue
            key = (red, green, blue)
            counts[key] = counts.get(key, 0) + count

    if not counts:
        raise ValueError(
            "no opaque pixels in the palette sources (alpha > "
            f"{ALPHA_FLOOR}); nothing to build a palette from"
        )

    total = sum(counts.values())
    scale = min(1.0, _MAX_SAMPLE_PIXELS / total)
    pixels: list[tuple[int, int, int]] = []
    for color, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        # Every colour keeps at least one vote so a rare highlight can still win
        # a slot when the budget scales its count below one.
        pixels.extend([color] * max(1, round(count * scale)))

    width = min(_SAMPLE_ROW_WIDTH, len(pixels))
    height = -(-len(pixels) // width)
    # Filled with the most common colour so the row-major remainder padding adds
    # weight to a colour already dominant instead of introducing black.
    sample = Image.new("RGB", (width, height), pixels[0])
    sample.putdata(pixels)
    return sample.quantize(colors=min(max_colors, len(counts)), method=Image.Quantize.MEDIANCUT)


def palette_colors(palette) -> list[tuple[int, int, int]]:
    """The RGB triples a palette image can produce, in palette-index order."""
    if getattr(palette, "mode", "") != "P":
        raise ValueError(f"expected a 'P'-mode palette image, got mode {getattr(palette, 'mode', None)!r}")
    raw = palette.getpalette() or []
    # Empty, not None, when there is no palette object: the unfiltered fallback
    # this used to carry was a branch no input could distinguish. `getpalette()`
    # answers out of the SAME palette object, so bytes here imply a non-empty
    # `.colors` and no bytes imply an empty `triples` — the two arms returned
    # the identical list for every reachable image, and the false one only ever
    # returned `[]` the long way round. Measured 2026-09-05 against the
    # one-armed-branch report; the equivalence is pinned in
    # `tests/agent/test_charsheet_branch_triage.py`.
    used = palette.palette.colors if getattr(palette, "palette", None) else {}
    triples = [tuple(raw[i : i + 3]) for i in range(0, len(raw), 3)]
    # Pillow pads the palette out to 256 entries; keep only the ones the
    # quantizer actually assigned so callers can assert set membership.
    return [triple for triple in triples if triple in used]


def palette_table(image) -> list[str]:
    """*image*'s opaque colour table as ``#RRGGBBAA``, most-used pixel first.

    This is the COMPOSE-TIME palette in the shape a consumer can render. The
    sheet is quantized INTO :func:`build_palette`'s table, so its distinct
    opaque colours are that table — and counting them here gives the one thing
    the palette image cannot: how much of the character each colour is. A swatch
    strip's first swatches are then the character's dominant colours instead of
    whatever order the median cut happened to assign its slots.

    Measured off the composed sheet rather than off the ``P``-mode palette on
    purpose: a slot the quantizer allocated and no cell ever used is not a
    colour of this character, and would render as a swatch of a colour that is
    nowhere on the sheet.

    Alpha is ``ff`` on every entry, and that is a statement rather than filler.
    The locked palette is an OPAQUE RGB table — :func:`lock_to_palette` carries
    alpha separately, byte for byte, so one jacket blue appears at dozens of
    alphas along an antialiased edge — and grouping by RGBA would answer
    thousands of entries for a 48-colour sheet. Pixels at or below
    :data:`ALPHA_FLOOR` are background and vote for nothing: the same floor
    :func:`build_palette` samples with.

    Ties break on the colour itself, so equal coverage always comes back in one
    order and a consumer diffing two sheets' tables reads a real change rather
    than a histogram's iteration order.
    """
    rgba = _as_rgba(image)
    # Cap = pixel count, so this can only answer None for an image with more
    # distinct colours than pixels, which does not exist.
    colors = rgba.getcolors(maxcolors=max(1, rgba.width * rgba.height))
    if colors is None:  # pragma: no cover — unreachable by the line above
        raise ValueError("could not histogram the sheet: too many distinct colours")
    counts: dict[tuple[int, int, int], int] = {}
    for count, pixel in colors:
        red, green, blue, alpha = pixel
        if alpha <= ALPHA_FLOOR:
            continue
        key = (red, green, blue)
        counts[key] = counts.get(key, 0) + count
    return [
        "#{:02x}{:02x}{:02x}ff".format(*color)
        for color in sorted(counts, key=lambda color: (-counts[color], color))
    ]


def lock_to_palette(frame_rgba, palette):
    """Quantize *frame_rgba*'s RGB into *palette*, keeping its alpha unchanged.

    Fully-transparent pixels come back ``(0, 0, 0, 0)``: the quantizer assigns
    them some palette colour (it never sees alpha), and leaving that RGB behind
    would be exactly the halo residue the atlas validator rejects. Composition
    zeroes it again downstream, but a cell is not allowed to leave here dirty.
    """
    from PIL import Image

    if getattr(palette, "mode", "") != "P":
        raise ValueError(
            f"palette must be a 'P'-mode image from build_palette, got mode "
            f"{getattr(palette, 'mode', None)!r}"
        )

    rgba = _as_rgba(frame_rgba)
    alpha = rgba.getchannel("A")
    locked = rgba.convert("RGB").quantize(palette=palette, dither=Image.Dither.NONE).convert("RGBA")
    locked.putalpha(alpha)
    opaque = alpha.point(lambda value: 255 if value else 0)
    return Image.composite(locked, Image.new("RGBA", locked.size, (0, 0, 0, 0)), opaque)
