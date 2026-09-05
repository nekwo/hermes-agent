"""The charsheet pixel pipeline, driven offline through its one provider seam.

``pipeline._generate_image`` is the documented test seam: every generation in the
package funnels through it, so replacing it with a deterministic draftsman runs
the whole flow with no network. The fixture draws a constant-size ring with a
direction arrow and a per-frame tick inside it — constant bbox so the validator's
collapse guards measure the pipeline rather than the fixture.

The spec under test is deliberately small (two states, 4-way) — the point is that
the same code paths carry any scheme, so the cheap one is the honest one to run.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

import pytest

from agent.charsheet import palette as palette_mod
from agent.charsheet.palette import palette_table
from agent.charsheet import pipeline
from agent.charsheet import prompts
from agent.charsheet.spec import (
    CHAR8,
    EIGHT_WAY,
    FOUR_WAY,
    DirectionScheme,
    SheetSpec,
    StateSpec,
)

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402

SPEC = SheetSpec(
    states=(StateSpec("idle", 2, True), StateSpec("walk", 3, True)),
    scheme=FOUR_WAY,
)

STRIP_W, STRIP_H = 768, 256
SQUARE_PX = 512
GLYPH_PX = 60
MAGENTA = (*pipeline.MAGENTA, 255)

_UNIT = {
    "n": (0.0, -1.0),
    "ne": (0.7071, -0.7071),
    "e": (1.0, 0.0),
    "se": (0.7071, 0.7071),
    "s": (0.0, 1.0),
    "sw": (-0.7071, 0.7071),
    "w": (-1.0, 0.0),
    "nw": (-0.7071, -0.7071),
}


@pytest.fixture(autouse=True)
def _hermes_home(tmp_path, monkeypatch):
    """Nothing here should touch the operator's home; make sure it cannot."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


# ────────────────────────── the fake draftsman ──────────────────────────


def _draw_glyph(draw, cx, cy, size, direction, tick, ticks):
    half = size // 2
    ring = max(4, size // 15)
    draw.rectangle([cx - half, cy - half, cx + half, cy + half], outline=(30, 40, 120, 255), width=ring)
    ux, uy = _UNIT[direction]
    reach = half - ring - max(4, size // 12)
    tip = (cx + ux * reach, cy + uy * reach)
    tail = (cx - ux * reach * 0.55, cy - uy * reach * 0.55)
    perp = (-uy, ux)
    wing = reach * 0.42
    draw.polygon(
        [
            tip,
            (tail[0] + perp[0] * wing, tail[1] + perp[1] * wing),
            (tail[0] - perp[0] * wing, tail[1] - perp[1] * wing),
        ],
        fill=(230, 110, 40, 255),
    )
    tick_px = max(6, size // 10)
    inner = size - 2 * ring - tick_px - 8
    step = inner / max(1, ticks)
    x0 = cx - half + ring + 4 + int(tick * step)
    y0 = cy - half + ring + 4
    draw.rectangle([x0, y0, x0 + tick_px, y0 + tick_px], fill=(30, 190, 110, 255))


def strip_image(slots, *, spread=1.0):
    """One landscape strip: ``slots`` is ``[(direction, tick, ticks), …]``."""
    image = Image.new("RGBA", (STRIP_W, STRIP_H), MAGENTA)
    draw = ImageDraw.Draw(image)
    width = STRIP_W / len(slots)
    for index, (direction, tick, ticks) in enumerate(slots):
        centre = STRIP_W / 2 + (width * (index + 0.5) - STRIP_W / 2) * spread
        _draw_glyph(draw, int(centre), STRIP_H // 2, GLYPH_PX, direction, tick, ticks)
    return image


def square_image(direction):
    image = Image.new("RGBA", (SQUARE_PX, SQUARE_PX), MAGENTA)
    _draw_glyph(ImageDraw.Draw(image), SQUARE_PX // 2, SQUARE_PX // 2, 200, direction, 0, 1)
    return image


class FakeProvider:
    """Records every call at the seam and answers it with a synthetic image."""

    def __init__(self, spec, out_dir, *, mode="good"):
        self.spec = spec
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.calls: list[dict] = []
        self.order = pipeline.turnaround_order(spec.scheme.authored)
        self._rows = {pipeline.row_prefix(row.key): row for row in spec.authored_rows()}
        self._views = {pipeline.view_prefix(d): d for d in spec.scheme.order}

    def __call__(self, prompt, *, reference_images, aspect_ratio, prefix, provider):
        refs = [str(ref) for ref in (reference_images or [])]
        self.calls.append(
            {"prompt": prompt, "refs": refs, "aspect": aspect_ratio, "prefix": prefix}
        )
        if prefix == pipeline.PREFIX_TURNAROUND:
            image = strip_image([(direction, 0, 1) for direction in self.order])
        elif prefix in self._views:
            image = square_image(self._views[prefix])
        elif prefix in self._rows:
            row = self._rows[prefix]
            direction = row.direction or pipeline.NON_DIRECTIONAL_VIEW
            slots = [(direction, index, row.frames) for index in range(row.frames)]
            if self.mode == "blank":
                image = Image.new("RGBA", (STRIP_W, STRIP_H), MAGENTA)
            elif self.mode == "touching-once" and len(self.calls) == 1:
                image = strip_image(slots, spread=0.04)
            else:
                image = strip_image(slots)
        else:  # pragma: no cover - a new generation kind would need a fixture
            raise AssertionError(f"unexpected generation prefix {prefix!r}")
        path = self.out_dir / f"{prefix}-{len(self.calls)}.png"
        image.save(path, format="PNG")
        return path


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """The seam, replaced. Returns the recorder so tests can read its calls."""
    provider = FakeProvider(SPEC, tmp_path / "generated")
    monkeypatch.setattr(pipeline, "_generate_image", provider)
    return provider


@pytest.fixture(scope="module")
def base_image(tmp_path_factory):
    path = tmp_path_factory.mktemp("base") / "base.png"
    square_image("s").save(path, format="PNG")
    return path


@pytest.fixture(scope="module")
def built(tmp_path_factory, base_image):
    """One full offline run — references, accepted strips, cells and the sheet.

    Module-scoped because it is the same deterministic artifact for every test
    that reads it, and re-running the extractor per test buys nothing. Tests that
    need to observe the provider seam use the function-scoped ``fake`` instead.
    """
    root = tmp_path_factory.mktemp("built")
    provider = FakeProvider(SPEC, root / "generated")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pipeline, "_generate_image", provider)
        refs = pipeline.generate_turnaround(
            SPEC, "an arrow knight", base_image, out_dir=root / "turnaround"
        )
        strips = {}
        for row in SPEC.authored_rows():
            reference = refs[row.direction] if row.direction else base_image
            strips[row.key] = pipeline.generate_row_strip(
                row,
                "an arrow knight",
                reference,
                out=root / "strips" / f"{row.key}.png",
            )
        cells = pipeline.compose_draft_frames(SPEC, strips, list(refs.values()))
        sheet = pipeline.compose_sheet(SPEC, cells)
    return {
        "refs": refs,
        "strips": strips,
        "cells": cells,
        "sheet": sheet,
        "calls": provider.calls,
    }


@pytest.fixture
def refs(built):
    return built["refs"]


@pytest.fixture
def strips(built):
    return built["strips"]


@pytest.fixture
def cells(built):
    return built["cells"]


@pytest.fixture
def sheet(built):
    return built["sheet"]


def cell_of(sheet, key, column=0):
    row = SPEC.row_by_key(key)
    left = column * SPEC.frame_w
    top = row.index * SPEC.frame_h
    return sheet.crop((left, top, left + SPEC.frame_w, top + SPEC.frame_h))


# ─────────────────────────── compose geometry ───────────────────────────


def test_composed_sheet_has_exactly_the_size_the_spec_describes(sheet):
    assert sheet.size == SPEC.sheet_size()
    assert sheet.mode == "RGBA"


def test_every_authored_row_lands_at_its_spec_row_index(sheet, cells):
    for row in SPEC.authored_rows():
        for column in range(row.frames):
            assert cell_of(sheet, row.key, column).tobytes() == cells[row.key][column].tobytes()


def test_a_short_row_leaves_its_trailing_columns_empty(sheet):
    short = SPEC.row_by_key("idle-e")

    assert short.frames < SPEC.columns()
    for column in range(short.frames, SPEC.columns()):
        assert cell_of(sheet, short.key, column).getbbox() is None


def test_compose_refuses_a_spec_row_with_no_strip(strips, refs):
    partial = {key: path for key, path in strips.items() if key != "walk-e"}

    with pytest.raises(ValueError, match="no strip for authored row 'walk-e'"):
        pipeline.compose_draft_frames(SPEC, partial, list(refs.values()))


# ──────────────────── mirrored rows: neither drawn nor baked ────────────────────

# Transcribed, not derived from the spec under test: `SPEC` is FOUR_WAY, whose one
# mirrored direction is w <- e, so these are the two row names a mirror-baking
# compose would produce. Reading them out of `SPEC.mirrored_rows()` would make the
# test follow the module under test into whatever it does next.
DERIVED_KEYS = ("idle-w", "walk-w")


def test_mirrored_rows_are_never_generated(built):
    """A mirrored direction is neither GENERATED nor COMPOSED (ruling 3-B).

    The first half held before H1 and the second did not: compose derived every
    mirrored row through `mirror_frames` and packed it into the sheet, which is
    the +60% decoded RAM the ADR priced. "We never asked the model for it" is
    exactly the half that stayed true while the sheet grew, so both halves are
    asserted here.
    """
    generated = {call["prefix"] for call in built["calls"]}
    cells = built["cells"]

    assert generated & {pipeline.row_prefix(key) for key in DERIVED_KEYS} == set()
    assert generated >= {pipeline.row_prefix(row.key) for row in SPEC.rows()}
    assert set(cells) & set(DERIVED_KEYS) == set()
    assert set(cells) == {row.key for row in SPEC.rows()}
    assert built["sheet"].size[1] == len(SPEC.rows()) * SPEC.frame_h == 6 * SPEC.frame_h


def test_char8_composes_ten_authored_rows_at_1536x2080():
    """The default sheet's geometry, in the numbers the memory budget was set from.

    ADR 0024 decision 2-C priced a character sheet at 12.8 MB decoded — that is
    1536 x 2080 x 4 bytes, and it is only true while the sheet is authored-only:
    baking the three mirrored directions back in makes CHAR8 sixteen rows,
    1536 x 3328, 20.4 MB. Every literal here is transcribed from the ADR and the
    launcher SPEC rather than computed from the spec object.
    """
    keys = [row.key for row in CHAR8.rows()]

    assert keys == [
        "idle-s", "idle-se", "idle-e", "idle-ne", "idle-n",
        "walk-s", "walk-se", "walk-e", "walk-ne", "walk-n",
    ]
    assert len(keys) == 10
    assert CHAR8.sheet_size() == (1536, 2080)
    assert not any(key.endswith(("-sw", "-w", "-nw")) for key in keys)

    cells = {
        row.key: [
            Image.new("RGBA", (CHAR8.frame_w, CHAR8.frame_h), (20 + 20 * row.index, 60, 90, 255))
            for _column in range(row.frames)
        ]
        for row in CHAR8.rows()
    }
    sheet = pipeline.compose_sheet(CHAR8, cells)
    report = pipeline.validate_sheet(CHAR8, sheet)

    assert sheet.size == (1536, 2080)
    for row in CHAR8.rows():
        assert sheet.getpixel((0, row.index * CHAR8.frame_h)) == (
            20 + 20 * row.index,
            60,
            90,
            255,
        )
    assert report["ok"] is True
    assert report["filled_rows"] == keys


# ────────────────────────────── validation ──────────────────────────────


def test_a_freshly_composed_sheet_validates_clean(sheet):
    report = pipeline.validate_sheet(SPEC, sheet)

    assert report["ok"] is True
    assert report["errors"] == []
    assert report["warnings"] == []
    assert sorted(report["filled_rows"]) == sorted(row.key for row in SPEC.rows())
    assert (report["width"], report["height"]) == SPEC.sheet_size()


def test_a_wrong_size_image_fails_geometry_before_anything_else(sheet):
    width, height = SPEC.sheet_size()
    report = pipeline.validate_sheet(SPEC, Image.new("RGBA", (width + SPEC.frame_w, height)))

    assert report["ok"] is False
    assert len(report["errors"]) == 1
    assert f"expected {width}x{height}" in report["errors"][0]
    assert report["filled_rows"] == []


def test_one_missing_row_is_a_warning_not_an_error(cells):
    dropped = "walk-n"
    partial = pipeline.compose_sheet(SPEC, {k: v for k, v in cells.items() if k != dropped})

    report = pipeline.validate_sheet(SPEC, partial)

    assert report["errors"] == []
    assert report["ok"] is True
    assert report["warnings"] == [f"row '{dropped}' has no frames"]
    assert dropped not in report["filled_rows"]


def test_a_sheet_with_no_frames_at_all_is_an_error(sheet):
    report = pipeline.validate_sheet(SPEC, Image.new("RGBA", SPEC.sheet_size(), (0, 0, 0, 0)))

    assert report["ok"] is False
    assert any("sheet is empty" in error for error in report["errors"])


def test_rgb_left_under_a_transparent_pixel_is_an_error(sheet):
    assert pipeline.validate_sheet(SPEC, sheet)["ok"] is True
    dirty = sheet.copy()
    assert dirty.getpixel((0, 0))[3] == 0
    dirty.putpixel((0, 0), (7, 9, 11, 0))

    report = pipeline.validate_sheet(SPEC, dirty)

    assert report["ok"] is False
    assert any("RGB residue" in error for error in report["errors"])


def test_composition_itself_leaves_no_rgb_under_transparency(sheet):
    colors = sheet.getcolors(maxcolors=sheet.width * sheet.height) or []

    assert colors, "getcolors must not overflow on a locked-palette sheet"
    assert all(alpha != 0 or (r, g, b) == (0, 0, 0) for _count, (r, g, b, alpha) in colors)


# ──────────────────────────── palette lock ────────────────────────────


def test_every_opaque_colour_on_the_sheet_comes_from_the_locked_palette(sheet, refs):
    palette = pipeline.build_sheet_palette(list(refs.values()))
    allowed = set(palette_mod.palette_colors(palette))
    colors = sheet.getcolors(maxcolors=sheet.width * sheet.height) or []
    opaque = {(r, g, b) for _count, (r, g, b, alpha) in colors if alpha > 0}

    assert opaque
    assert opaque <= allowed


def test_the_chroma_field_is_keyed_out_before_the_palette_is_built(refs):
    palette = pipeline.build_sheet_palette(list(refs.values()))

    assert pipeline.MAGENTA not in set(palette_mod.palette_colors(palette))


def test_the_palette_needs_at_least_one_reference():
    with pytest.raises(ValueError, match="at least one approved reference"):
        pipeline.build_sheet_palette([])


def test_locking_a_cell_keeps_its_alpha_and_zeroes_only_transparent_pixels(refs):
    palette = pipeline.build_sheet_palette(list(refs.values()))
    source = Image.new("RGBA", (8, 4), (0, 0, 0, 0))
    for x, alpha in enumerate((0, 1, 17, 64, 128, 200, 254, 255)):
        source.putpixel((x, 1), (200, 40, 90, alpha))

    locked = palette_mod.lock_to_palette(source, palette)

    assert locked.size == source.size
    assert locked.getchannel("A").tobytes() == source.getchannel("A").tobytes()
    for x in range(source.width):
        for y in range(source.height):
            r, g, b, alpha = locked.getpixel((x, y))
            if alpha == 0:
                assert (r, g, b) == (0, 0, 0)
            else:
                assert (r, g, b) in set(palette_mod.palette_colors(palette))


def test_locking_refuses_anything_that_is_not_a_built_palette(refs):
    with pytest.raises(ValueError, match="must be a 'P'-mode image"):
        palette_mod.lock_to_palette(Image.new("RGBA", (4, 4)), Image.new("RGB", (4, 4)))


# ───────────────────── the persisted colour table ─────────────────────


def _swatches(*runs):
    """A 1-pixel-tall RGBA image spelling ``(colour, count)`` runs left to right."""
    pixels = [color for color, count in runs for _ in range(count)]
    image = Image.new("RGBA", (len(pixels), 1))
    image.putdata(pixels)
    return image


def test_the_table_is_the_colours_in_DESCENDING_pixel_count():
    """Two colours, and the ORDER is the assertion — positionally, not as a set.

    The strip's first swatches have to be the character's dominant colours, so
    reversing or dropping the sort must go red on something. A membership
    assertion would pass under both.
    """
    table = palette_table(_swatches(((10, 20, 30, 255), 3), ((200, 100, 50, 255), 9)))

    assert table == ["#c86432ff", "#0a141eff"]


def test_a_third_colour_lands_where_its_coverage_puts_it():
    """Two entries can be ordered by luck; three cannot."""
    table = palette_table(
        _swatches(((1, 1, 1, 255), 5), ((2, 2, 2, 255), 50), ((3, 3, 3, 255), 20))
    )

    assert table == ["#020202ff", "#030303ff", "#010101ff"]


def test_equal_coverage_breaks_the_tie_on_the_colour_itself():
    """Determinism: the same sheet must answer the same table, run after run.

    Without a total order the tie falls to histogram iteration order, and a
    consumer diffing two sheets' tables would read a change that is not one.
    """
    runs = (((9, 9, 9, 255), 4), ((1, 2, 3, 255), 4))

    assert palette_table(_swatches(*runs)) == ["#010203ff", "#090909ff"]
    assert palette_table(_swatches(*reversed(runs))) == ["#010203ff", "#090909ff"]


def test_background_pixels_vote_for_nothing():
    """At or below the sample floor is background — the same floor the palette
    was BUILT with, so the table cannot name a colour the palette never saw."""
    image = _swatches(((7, 7, 7, 255), 2), ((250, 0, 250, palette_mod.ALPHA_FLOOR), 40))

    assert palette_table(image) == ["#070707ff"]


def test_one_colour_at_many_alphas_is_ONE_entry_at_full_alpha():
    """The locked palette is an opaque RGB table; alpha rides separately.

    Grouping by RGBA instead would answer one entry per antialiased edge step —
    thousands of swatches for a 48-colour sheet.
    """
    image = _swatches(
        ((60, 70, 80, 255), 2), ((60, 70, 80, 40), 2), ((60, 70, 80, 200), 2)
    )

    assert palette_table(image) == ["#3c4650ff"]


def test_the_composed_sheet_s_table_is_exactly_the_colours_on_it(sheet):
    """End to end: the table names the sheet's own distinct opaque colours."""
    colors = sheet.getcolors(maxcolors=sheet.width * sheet.height) or []
    opaque = {
        "#{:02x}{:02x}{:02x}ff".format(r, g, b)
        for _count, (r, g, b, alpha) in colors
        if alpha > palette_mod.ALPHA_FLOOR
    }

    table = palette_table(sheet)

    assert set(table) == opaque
    assert len(table) == len(opaque), "a colour must not appear twice in the table"


# ───────────────────── grounding references / magenta ─────────────────────


def test_recomposite_on_magenta_makes_a_cutout_fully_opaque_over_the_chroma_field():
    cutout = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
    cutout.putpixel((1, 0), (10, 20, 30, 255))
    cutout.putpixel((2, 0), (10, 20, 30, 128))

    field = pipeline.recomposite_on_magenta(cutout)

    assert field.size == cutout.size
    assert set(field.getchannel("A").tobytes()) == {255}
    assert field.getpixel((0, 0)) == MAGENTA
    assert field.getpixel((1, 0)) == (10, 20, 30, 255)
    assert field.getpixel((2, 0)) not in (MAGENTA, (10, 20, 30, 255))


def test_turnaround_slices_land_on_the_chroma_field_one_per_authored_direction(refs):
    assert sorted(refs) == sorted(SPEC.scheme.authored)
    for direction, path in refs.items():
        with Image.open(path) as opened:
            image = opened.convert("RGBA")
        assert set(image.getchannel("A").tobytes()) == {255}, direction
        assert image.getpixel((0, 0)) == MAGENTA


def test_one_turnaround_call_produces_every_authored_reference(fake, base_image, tmp_path):
    refs = pipeline.generate_turnaround(
        SPEC, "an arrow knight", base_image, out_dir=tmp_path / "turnaround"
    )

    (call,) = fake.calls
    assert call["prefix"] == pipeline.PREFIX_TURNAROUND
    assert call["aspect"] == "landscape"
    assert call["refs"] == [str(base_image)]
    assert sorted(refs) == sorted(SPEC.scheme.authored)


def test_a_missing_base_image_is_refused_before_any_generation(fake, tmp_path):
    with pytest.raises(ValueError, match="base image not found"):
        pipeline.generate_turnaround(SPEC, "x", tmp_path / "nope.png", out_dir=tmp_path / "out")
    with pytest.raises(ValueError, match="base image not found"):
        pipeline.generate_direction_view("e", "x", tmp_path / "nope.png", out=tmp_path / "o.png")

    assert fake.calls == []


def test_a_turnaround_strip_that_cannot_be_cut_into_the_authored_directions_is_refused(
    base_image, tmp_path, monkeypatch
):
    """A provider stub whose strip does not divide into the authored count.

    The arc w16/ha named and left unwritten (``pipeline.py`` 752→753): the
    turnaround's "yielded N cutouts for M authored directions" guard. Writing
    the case proves the guard could never speak — the extractor answers either
    EXACTLY the count it was asked for or an exception, so the branch was
    deleted and this test is what stands in its place.

    Both halves are here because either alone would be a half-proof:

    * A strip carrying FEWER poses than there are authored directions is
      refused — by ``_validate_extracted_frames``'s hard tier one module over
      (``agent/pet/generate/atlas.py``), which is the module that owns the
      contract. The docstring's promise ("slicing failures raise") is kept, and
      nothing is written to ``out_dir``: a failed roll is re-rolled whole, never
      salvaged into a partial turnaround.
    * A strip carrying MORE poses comes back at exactly ``len(order)``, which is
      the other way the deleted guard could have been true and is not.
    """

    order = pipeline.turnaround_order(SPEC.scheme.authored)
    assert len(order) >= 2, "the case needs a strip that can be one pose short"
    generated = tmp_path / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    out_dir = tmp_path / "turnaround"

    def _one_pose_short(prompt, *, reference_images, aspect_ratio, prefix, provider):
        assert prefix == pipeline.PREFIX_TURNAROUND
        path = generated / "short-turnaround.png"
        strip_image([(direction, 0, 1) for direction in order[:-1]]).save(path, format="PNG")
        return path

    monkeypatch.setattr(pipeline, "_generate_image", _one_pose_short)

    with pytest.raises(ValueError) as caught:
        pipeline.generate_turnaround(
            SPEC, "an arrow knight", base_image, out_dir=out_dir
        )
    # The extractor's own refusal, not a count re-check in this module.
    assert "cutouts for" not in str(caught.value)
    assert list(out_dir.glob("turnaround-*.png")) == []

    # The over-full strip: still exactly the authored count, never more.
    over_full = strip_image(
        [(order[index % len(order)], 0, 1) for index in range(len(order) + 1)]
    )
    assert len(pipeline.extract_strip_frames(over_full, len(order), fit=False)) == len(order)


def test_a_direction_reroll_is_one_square_generation_carrying_the_note(fake, base_image, tmp_path):
    out = pipeline.generate_direction_view(
        "e", "an arrow knight", base_image, note="less shine on the helm", out=tmp_path / "e.png"
    )

    (call,) = fake.calls
    assert call["aspect"] == "square"
    assert call["prefix"] == pipeline.view_prefix("e")
    assert "less shine on the helm" in call["prompt"]
    with Image.open(out) as opened:
        assert set(opened.convert("RGBA").getchannel("A").tobytes()) == {255}


def test_a_back_facing_row_prompt_keeps_the_no_face_clause(fake, refs, tmp_path):
    row = SPEC.row_by_key("walk-n")
    pipeline.generate_row_strip(
        row, "an arrow knight", refs["n"], note="heavier stride", out=tmp_path / "n.png"
    )

    prompt = fake.calls[-1]["prompt"]
    assert "no face" in prompt
    assert "heavier stride" in prompt
    assert "N facing" in prompt


def test_the_diagonal_view_language_pairs_are_exact_left_right_mirrors():
    """The property whose absence shipped a defect, pinned at the vocabulary.

    ``nw`` is produced by flipping ``ne`` and ``sw`` by flipping ``se``, so the
    two halves of each pair have to describe the same view with the sides
    exchanged and nothing else. The BACK pair failed that for months in a way no
    reader noticed, because both halves said "turned toward the viewer's X" and
    neither said which side of the FRAME the body or the visible cheek lands on
    — a phrase that is unambiguous while the character faces you and ambiguous
    the moment it faces away. Live, `ne` came back drawn as `nw` in all three
    states it was ever generated for.

    This does not check the wording is RIGHT — only that the pair stays
    symmetric, so an edit to one half that is not made to the other cannot land
    quietly. The wording being right is what `detect_mirrored_art` measures.
    """
    sides = {"right": "left", "left": "right", "RIGHT": "LEFT", "LEFT": "RIGHT"}

    def swap(text):
        return re.sub("|".join(sides), lambda hit: sides[hit.group(0)], text)

    for derived, source in (("nw", "ne"), ("sw", "se")):
        assert swap(prompts.VIEW_LANGUAGE[source]) == prompts.VIEW_LANGUAGE[derived], (
            f"{source!r} and {derived!r} are no longer mirror images of each other"
        )
        # And the two halves must not be identical, which a swap of a text
        # naming neither side would also satisfy.
        assert prompts.VIEW_LANGUAGE[source] != prompts.VIEW_LANGUAGE[derived]


# ──────────────────────── the mechanical row gate ────────────────────────


def test_a_row_whose_poses_touch_is_re_rolled_rather_than_accepted(monkeypatch, refs, tmp_path, base_image):
    provider = FakeProvider(SPEC, tmp_path / "retry", mode="touching-once")
    monkeypatch.setattr(pipeline, "_generate_image", provider)
    row = SPEC.row_by_key("walk-e")

    accepted = pipeline.generate_row_strip(
        row, "an arrow knight", refs["e"], out=tmp_path / "walk-e.png"
    )

    assert len(provider.calls) == 2
    assert accepted.is_file()


def test_a_row_that_never_becomes_sliceable_fails_loudly(monkeypatch, refs, tmp_path):
    provider = FakeProvider(SPEC, tmp_path / "blank", mode="blank")
    monkeypatch.setattr(pipeline, "_generate_image", provider)
    row = SPEC.row_by_key("walk-e")

    with pytest.raises(ValueError, match="produced no sliceable strip"):
        pipeline.generate_row_strip(row, "x", refs["e"], out=tmp_path / "walk-e.png")

    assert len(provider.calls) == 3
    assert not (tmp_path / "walk-e.png").exists()


def test_a_row_needs_an_existing_grounding_reference(fake, tmp_path):
    with pytest.raises(ValueError, match="grounding reference for row 'walk-e' not found"):
        pipeline.generate_row_strip(
            SPEC.row_by_key("walk-e"), "x", tmp_path / "gone.png", out=tmp_path / "o.png"
        )

    assert fake.calls == []


# ───────────────────────────── turnaround order ─────────────────────────────


@pytest.mark.parametrize("scheme", [EIGHT_WAY, FOUR_WAY])
def test_turnaround_order_is_front_first_and_back_last(scheme):
    order = pipeline.turnaround_order(scheme.authored)

    assert sorted(order) == sorted(scheme.authored)
    assert order.index("s") < order.index("e") < order.index("n")
    assert order[0] == "s"
    assert order[-1] == "n"


def test_turnaround_order_interleaves_the_diagonals_by_ring_distance():
    order = pipeline.turnaround_order(EIGHT_WAY.authored)

    assert order.index("se") < order.index("e") < order.index("ne")


def test_turnaround_order_is_independent_of_the_input_order():
    forward = pipeline.turnaround_order(EIGHT_WAY.authored)

    assert pipeline.turnaround_order(reversed(EIGHT_WAY.authored)) == forward
    assert pipeline.turnaround_order(sorted(EIGHT_WAY.authored)) == forward


def test_turnaround_order_rejects_a_direction_with_no_view_language():
    with pytest.raises(ValueError, match="unknown direction 'up'"):
        pipeline.turnaround_order(["s", "up"])


# ─────────────────────────── cell geometry guard ───────────────────────────


def test_a_cell_of_the_wrong_size_is_refitted_only_at_the_upstream_geometry():
    spec = SheetSpec(states=(StateSpec("cheer", 2, False),), scheme=FOUR_WAY)
    small = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    small.putpixel((20, 20), (10, 20, 30, 255))

    packed = pipeline.compose_sheet(spec, {"cheer": [small]})
    assert packed.size == spec.sheet_size()
    assert packed.getbbox() is not None

    odd = SheetSpec(states=(StateSpec("cheer", 2, False),), scheme=FOUR_WAY, frame_w=64, frame_h=64)
    with pytest.raises(ValueError, match="only the upstream"):
        pipeline.compose_sheet(odd, {"cheer": [small]})


# ──────────────────────────── which way it faces ────────────────────────────
#
# A number, not a verdict. `face_offset` measures where the HEAD sits against
# the body's own centre, which is the same quantity the 2026-08-25 field notes
# quoted by hand (`s se e ne n` = `+0.0 +5.2 +10.9 -6.5 -0.3`) when they found
# an approved `e` REFERENCE facing west while all three `e` rows drawn from it
# faced east. Approving a turnaround certified nothing about that; now the
# approval receipt carries the measurement.


def figure(*, head_dx: int, size: int = 200, on_chroma: bool = True):
    """A standing figure: a wide body, and a head offset *head_dx* from centre.

    Deliberately crude — the measure is a centroid comparison, so a body and a
    head are all it can read. `on_chroma` puts it on the full-bleed magenta
    field every generated reference actually arrives on, which is the input the
    verb has to survive: a measure that read the field as body would answer
    about the canvas.
    """
    ground = MAGENTA if on_chroma else (0, 0, 0, 0)
    image = Image.new("RGBA", (size, size), ground)
    draw = ImageDraw.Draw(image)
    mid = size // 2
    draw.rectangle([mid - 30, mid - 20, mid + 30, size - 20], fill=(40, 60, 200, 255))
    draw.ellipse(
        [mid + head_dx - 18, 20, mid + head_dx + 18, 56], fill=(230, 180, 140, 255)
    )
    return image


def test_a_figure_whose_head_sits_over_its_body_measures_near_zero():
    assert abs(pipeline.face_offset(figure(head_dx=0))) < 1.0


def test_the_face_offset_is_signed_and_the_sign_is_which_way_it_faces():
    """Positive is a head to the RIGHT of the body's centre, and that is the
    whole reading: the field notes' `+10.9` east and `-44.8` west are the two
    signs of one number.
    """
    right = pipeline.face_offset(figure(head_dx=30))
    left = pipeline.face_offset(figure(head_dx=-30))

    assert right > 5 and left < -5
    # A mirror is the only exact symmetry available, and it is the property that
    # makes the sign meaningful rather than an artefact of the drawing.
    assert right == pytest.approx(-left, abs=0.6)


def test_a_mirrored_reference_measures_the_negated_offset(tmp_path):
    """The measure travels through a FILE, because that is how it is called.

    `approve_direction` is handed a path out of the revision store, never an
    open image.
    """
    from PIL import ImageOps

    drawn = figure(head_dx=26)
    path = tmp_path / "reference.png"
    drawn.save(path, format="PNG")
    mirrored_path = tmp_path / "mirrored.png"
    ImageOps.mirror(drawn).save(mirrored_path, format="PNG")

    assert pipeline.face_offset(path) == pytest.approx(
        -pipeline.face_offset(mirrored_path), abs=0.6
    )


def test_the_chroma_field_is_not_read_as_body(tmp_path):
    """The same figure on magenta and on transparency is the same measurement.

    Every generated reference is full-bleed magenta at alpha 255, so a measure
    that skipped the keying step would compute the centroid of the CANVAS —
    which is 0.0 for every picture ever drawn, and would have reported the
    anime-girl reference as facing nowhere.
    """
    on_field = pipeline.face_offset(figure(head_dx=30, on_chroma=True))
    keyed = pipeline.face_offset(figure(head_dx=30, on_chroma=False))

    assert on_field == pytest.approx(keyed, abs=0.6)
    assert abs(on_field) > 5, "the offset collapsed to nothing: the field was read as body"


def test_an_empty_picture_has_no_facing_to_report():
    """`None`, never 0.0: "there is nothing here" and "it faces straight at you"
    are different answers, and a receipt that spelled them the same way would
    invite an operator to read a blank generation as a square-on pose.
    """
    assert pipeline.face_offset(Image.new("RGBA", (64, 64), (0, 0, 0, 0))) is None


# ────────────────────────── frame-cell geometry ──────────────────────────
#
# The defect this section pins SHIPPED, and an operator found it by opening a
# picture fullscreen. On 2026-08-28 `characters thumb` wrote a 272x724 crop of
# `walk-e` attempt 1 in which the character is SLICED mid-body: the pose the
# model drew spans x 66-298 of a 2172px strip, the even-slot boundary for an
# 8-frame row falls at x 272, and the crop stopped there — half a character,
# with the cut edge showing as a tall column of body pixels flush against the
# frame's right edge.
#
# The mechanism was two different boundary rules for the same strip. The real
# frame extraction (`atlas.extract_strip_frames`) has always been content-aware,
# precisely BECAUSE even slots are wrong on real strips; `frame_cell` had its
# own hand-rolled grammar that divided the width by the frame count. The dumb
# rule fed the QA surface — the one surface whose whole job is to show an
# operator the truth.
#
# So these tests are about WHERE the crop is taken. A 2026-08-24 mutation audit
# shifted the old window by +3px on BOTH bounds and the whole suite stayed
# green: every assertion read the cell's SIZE, which a shift does not change.
# Size assertions cannot see this class of defect at all; the assertions below
# read pose mass, per-column.

# The real measured pose ranges of the strip that carried the shipped defect
# (`row@walk-e/attempt-1.png`, 2172x724, 8 frames). Used as the synthetic
# fixture's geometry so the fixture is not a guess about what "off the grid"
# looks like — it is the row that broke.
WALK_E_POSES = [
    (66, 298),
    (340, 578),
    (630, 839),
    (900, 1094),
    (1139, 1359),
    (1390, 1626),
    (1684, 1877),
    (1927, 2116),
]
WALK_E_STRIP = Path(
    r"X:\Eternia\.hermes\shared\characters\.drafts\20260828-212742-2f3ec6"
    r"\revisions\row@walk-e\attempt-1.png"
)


def pose_columns(cell, *, field):
    """Per-column count of pixels that are NOT the flat field — the pose's mass.

    Deliberately independent of `atlas`'s keyer: the thing under test derives its
    geometry from that keyer, so a test that measured the result with the same
    tool could agree with a broken one. A pixel counts as pose when it is opaque
    and far from the field colour, which is the operator's own criterion —
    "something is drawn here" — expressed arithmetically.
    """
    px = cell.convert("RGBA").load()
    fr, fg, fb = field[:3]
    return [
        sum(
            1
            for y in range(cell.height)
            if (lambda p: p[3] > 16 and abs(p[0] - fr) + abs(p[1] - fg) + abs(p[2] - fb) > 90)(
                px[x, y]
            )
        )
        for x in range(cell.width)
    ]


def off_grid_strip(width=2172, height=724, poses=None):
    """A row whose poses sit where a model drew them, not on the even-slot grid.

    Every pose is a filled ellipse in its own measured x-range on the flat
    magenta field the package generates onto. Pose 0 straddles the 8-frame
    even-slot boundary at x 272 — the exact straddle that shipped.
    """
    strip = Image.new("RGBA", (width, height), MAGENTA)
    draw = ImageDraw.Draw(strip)
    for k, (left, right) in enumerate(poses or WALK_E_POSES):
        top = 140 + (k % 3) * 12
        draw.ellipse((left, top, right - 1, height - 170), fill=(20, 30 + k * 8, 220, 255))
    return strip


def test_a_frame_cell_follows_the_poses_the_model_drew_not_the_even_slots():
    """Every frame comes out WHOLE, including the pose that straddles a slot.

    Two claims, and the second is the one that shipped broken. **Whole**: the
    cell holds all of its own pose — measured as mass, so a cell that lost a
    limb to a boundary fails even though its size is right. **Only its own**:
    the cell's first and last columns are empty field, so nothing of the
    neighbour came along and, more to the point, the pose did not run off the
    edge of its own frame.
    """
    strip = off_grid_strip()
    whole = pose_columns(strip, field=MAGENTA)
    slots = [(round(k * 2172 / 8), round((k + 1) * 2172 / 8)) for k in range(8)]

    # The fixture is genuinely off the grid: pose 0 spans a slot boundary, so
    # the OLD rule had to cut it. Asserted on the slot arithmetic itself, not on
    # `frame_cell`, so this guard stays true after the fix.
    assert WALK_E_POSES[0][0] < slots[0][1] < WALK_E_POSES[0][1]

    for k, (left, right) in enumerate(WALK_E_POSES):
        cell = pipeline.frame_cell(strip, frame=k, frames=8)
        cols = pose_columns(cell, field=MAGENTA)
        assert cell.height == strip.height, "full strip height is deliberate"
        assert sum(cols) == sum(whole[left:right]), (
            f"frame {k} lost pose pixels: the cell holds {sum(cols)} of the "
            f"{sum(whole[left:right])} the pose is drawn with"
        )
        assert cols[0] == 0 and cols[-1] == 0, (
            f"frame {k} has content flush against a vertical edge "
            f"(left column {cols[0]}px, right column {cols[-1]}px) — the "
            "severing signature"
        )


@pytest.mark.skipif(not WALK_E_STRIP.is_file(), reason="live draft evidence not on this machine")
def test_the_shipped_walk_e_row_crops_whole_frames():
    """The real strip that produced the sliced 272x724 thumb, read-only.

    Frame 0 is the one an operator opened fullscreen and found half a character
    in. Under the even-slot rule its cell was (0, 272) against a pose spanning
    66-298: 26 columns of body cut off, and 193 body pixels standing in the
    cell's rightmost column. Under the content rule the pose is whole with field
    on both sides.
    """
    with Image.open(WALK_E_STRIP) as opened:
        strip = opened.convert("RGBA")
    assert strip.size == (2172, 724)
    field = strip.getpixel((0, 0))

    severed = pose_columns(strip.crop((0, 0, 272, 724)), field=field)
    assert severed[-1] > 724 * 0.15, "fixture check: the even slot really did sever the pose"

    for k in range(8):
        cell = pipeline.frame_cell(WALK_E_STRIP, frame=k, frames=8)
        cols = pose_columns(cell, field=field)
        assert cols[0] == 0 and cols[-1] == 0, (
            f"frame {k}: content flush to a vertical edge "
            f"(left {cols[0]}px, right {cols[-1]}px)"
        )
        assert sum(cols) > 0


def test_all_touching_poses_still_yield_ordered_non_overlapping_cells():
    """No gutters anywhere — the crop must degrade, never raise.

    A row the model drew shoulder to shoulder has no empty columns to read, so
    content segmentation has nothing to segment. The QA verb still owes the
    operator a picture: the frames come back in order, covering the row, without
    one cell reaching into the next.
    """
    touching = [(k * 271, (k + 1) * 271) for k in range(8)]
    strip = Image.new("RGBA", (2168, 724), MAGENTA)
    draw = ImageDraw.Draw(strip)
    for left, right in touching:
        draw.rectangle((left, 100, right - 1, 600), fill=(20, 40, 220, 255))

    cells = [pipeline.frame_cell(strip, frame=k, frames=8) for k in range(8)]

    assert all(cell.height == 724 for cell in cells)
    assert all(cell.width > 0 for cell in cells)
    assert sum(cell.width for cell in cells) <= strip.width * 1.05


@pytest.mark.parametrize("width, frames", [(768, 3), (770, 3), (2172, 8), (100, 7), (13, 13)])
def test_a_strip_with_no_readable_content_falls_back_to_even_slots(width, frames):
    """The last-resort rule, and the WHERE guard that outlived the old contract.

    Nothing is drawn (a fully transparent strip), so there are no content runs
    and no gutters to sever — the only honest answer left is the strip's own
    arithmetic. That answer must still be exact: the slots tile the strip with
    no gap, no overlap and no dropped column, which is what makes `round()` on
    both bounds a contract rather than an implementation detail (widths
    770/100/13 are the cases where the division does not divide).

    Each column carries its own x in its RGB, so a cell's contents name the
    columns it came from — the assertion the +3px mutation could not survive.
    """
    height = 24
    row = b"".join(bytes((x % 256, x // 256, 40, 0)) for x in range(width))
    strip = Image.frombytes("RGBA", (width, height), row * height)

    cells = [pipeline.frame_cell(strip, frame=k, frames=frames) for k in range(frames)]

    for k, cell in enumerate(cells):
        box = (round(k * width / frames), 0, round((k + 1) * width / frames), height)
        assert cell.size == (box[2] - box[0], height)
        assert cell.tobytes() == strip.crop(box).tobytes(), f"frame {k} is not its own slot"
    # The slots tile the strip: joined end to end they ARE the strip.
    joined = Image.new("RGBA", (width, height))
    at = 0
    for cell in cells:
        joined.paste(cell, (at, 0))
        at += cell.width
    assert at == width
    assert joined.tobytes() == strip.tobytes()


# ────────────────── handedness: art that contradicts its label ──────────────────
#
# The defect this section pins SHIPPED. `anime-girl`'s `ne` row was drawn facing
# north-WEST — in `idle` and `walk` on 2026-08-24 and again in `jumping` on
# 2026-08-25, three independent generations of the same wrong side — composed,
# installed, bundled into the launcher, and found by a human looking at a 3D
# scene. Because the consumer derives `nw` by flipping `ne`, one mirrored row
# corrupted BOTH rear diagonals while the other six directions stayed right.
#
# The 4-way SPEC above cannot exercise any of this and it is worth saying why
# rather than quietly using a second one: it authors `s, e, n`, so the only row
# with two seams has the front and back views as its neighbours, and each of
# those is close to its own mirror image. The glyph fixture reproduces that
# exactly — a south arrow IS symmetric — which is the same blindness the real
# 4-way character measures. Handedness lives in the diagonals, so these tests
# build an 8-way sheet.

EIGHT = SheetSpec(
    states=(StateSpec("idle", 2, True), StateSpec("walk", 3, True)),
    scheme=EIGHT_WAY,
)


BADGE = 14


def glyph_cells(spec, *, mirror=(), badge=None):
    """A composed-shaped cell map: every row drawn pointing where it claims.

    ``mirror`` names rows to flip horizontally — the defect itself, applied to
    art that is otherwise correct, which is the only way to hold everything else
    equal. Flipping each CELL in place (rather than the row band) keeps the
    frame order, so the row still animates forward while facing the wrong way.

    ``badge`` maps a row key to ``"left"`` or ``"right"`` and paints a bar down
    that edge of every cell in it. The arrows alone make a very tidy character —
    each end of the rotation is nearly its own mirror image, so no seam against
    one carries much handedness signal, which is true of the real anime-girl too
    and is exactly why the end rows are excluded from judging. A badge is how a
    test puts real signal on ONE seam, the way a satchel over one shoulder does
    on a real character.
    """
    cells = {}
    for row in spec.authored_rows():
        direction = row.direction or pipeline.NON_DIRECTIONAL_VIEW
        side = (badge or {}).get(row.key)
        frames = []
        for tick in range(row.frames):
            cell = Image.new("RGBA", (spec.frame_w, spec.frame_h), (0, 0, 0, 0))
            draw = ImageDraw.Draw(cell)
            _draw_glyph(
                draw,
                spec.frame_w // 2,
                spec.frame_h // 2,
                140,
                direction,
                tick,
                row.frames,
            )
            if side:
                near = 4 if side == "left" else spec.frame_w - 4 - BADGE
                draw.rectangle([near, 8, near + BADGE, 48], fill=(20, 200, 220, 255))
            if row.key in mirror:
                cell = cell.transpose(Image.FLIP_LEFT_RIGHT)
            frames.append(cell)
        cells[row.key] = frames
    return cells


def eight_way_sheet(*, mirror=(), badge=None):
    return pipeline.compose_sheet(EIGHT, glyph_cells(EIGHT, mirror=mirror, badge=badge))


def test_a_sheet_whose_rows_all_face_where_they_claim_is_clean(sheet):
    result = pipeline.validate_sheet(EIGHT, eight_way_sheet())

    assert result["ok"], result["errors"]
    assert result["handedness"]["flagged"] == []
    # And the 4-way sheet the rest of this module composes, for the same reason.
    assert pipeline.validate_sheet(SPEC, sheet)["handedness"]["flagged"] == []


def test_one_basis_warns_by_name_and_does_not_block_the_install():
    """The ruling, at the boundary it moved: ONE reading does not refuse.

    Round one made every flagged row an error, and round two kept that. What it
    bought was certainty the measurement does not have. Measured in both
    directions on real art: the true floor is +6.78% rotation / +7.64% states
    (`jumping-se` mirrored, flagged by NEITHER pass, composes and ships), and
    the false ceiling on CORRECT art slid sideways is +18.75%. The 8% line does
    not sit between two populations on one basis; it sits inside both, so no
    value of the threshold separates them and what changed instead is what a
    single reading is allowed to DO.

    This sheet has TWO states — which is exactly what `characters start`
    creates — so the cross-state pass has nothing to say and the rotation is the
    only reading there will ever be. It warns, by name, with its numbers, and it
    installs. Say that plainly rather than pretending otherwise: on the default
    character this check can only ever warn.
    """
    result = pipeline.validate_sheet(EIGHT, eight_way_sheet(mirror=("walk-ne",)))

    assert result["ok"], result["errors"]
    flagged = result["handedness"]["flagged"]
    assert [finding["row"] for finding in flagged] == ["walk-ne"]
    assert [finding["severity"] for finding in flagged] == ["warning"]
    assert result["errors"] == []
    assert len(result["warnings"]) == 1
    message = result["warnings"][0]
    assert "walk-ne" in message and "does not block" in message
    # A warning must NOT hand over a reroll command: a reroll auto-approves,
    # there is no approve-row verb, and one basis cannot justify spending art.
    assert "characters reroll-row" not in message


def test_two_bases_agreeing_is_what_refuses_an_install():
    """And the other half of the same ruling, on the art that shipped broken.

    `idle-ne` in the fixture is the row drawn facing north-WEST. The rotation
    reads it at +12.05% and the cross-state pass at +15.40% — two independent
    neighbourhoods, the same row — which is the only shape that blocks.
    """
    result = fixture_validation(EIGHT_WAY_FIXTURE)

    assert not result["ok"]
    (finding,) = result["handedness"]["flagged"]
    assert (finding["row"], finding["basis"], finding["severity"]) == (
        "idle-ne",
        "rotation and states",
        "error",
    )
    assert len(result["errors"]) == 1
    assert "characters reroll-row --row idle-ne" in result["errors"][0]


def test_the_flip_is_scored_against_both_neighbours_and_the_evidence_travels():
    finding = pipeline.validate_sheet(EIGHT, eight_way_sheet(mirror=("idle-ne",)))[
        "handedness"
    ]["flagged"][0]

    assert finding["direction"] == "ne"
    assert {seam["with"] for seam in finding["seams"]} == {"idle-e", "idle-n"}
    assert finding["gain"] >= pipeline.MIRROR_GAIN_THRESHOLD
    # The numbers a reader would have to trust the summary about are in the
    # payload, and the summary is derived from them rather than asserted.
    assert f"{finding['gain'] * 100:.0f}% better" in pipeline.mirrored_art_error(finding)
    for seam in finding["seams"]:
        assert f"{seam['distance']:.2f}" in pipeline.mirrored_art_error(finding)


def test_every_directional_state_is_judged_on_its_own_rows():
    """Two states, one mirrored row each: neither hides behind the other."""
    result = pipeline.validate_sheet(
        EIGHT, eight_way_sheet(mirror=("idle-ne", "walk-se"))
    )

    assert sorted(f["row"] for f in result["handedness"]["flagged"]) == [
        "idle-ne",
        "walk-se",
    ]


def test_the_ends_of_the_rotation_are_named_unjudged_never_silently_skipped():
    unjudged = pipeline.validate_sheet(EIGHT, eight_way_sheet())["handedness"][
        "unjudged"
    ]

    ends = {
        row
        for entry in unjudged
        if entry["basis"] == "rotation"
        for row in entry["rows"]
    }
    assert ends == {"idle-s", "idle-n", "walk-s", "walk-n"}
    assert all(entry["reason"] and entry["basis"] for entry in unjudged)


def test_an_end_row_is_not_blamed_for_a_seam_that_does_prefer_its_mirror():
    """The end-row exclusion, on the real character it was written for.

    `cobalt-robot-courier` is CORRECT 4-way art with a satchel over one shoulder.
    Its back view's single seam genuinely prefers the mirror — +10.91% here, past
    the threshold — and an earlier draft of this check judged end rows and refused
    that character's install over exactly that reading. The interior row, touching
    two seams, is absolved by the other one and scores +1.72% — which is the whole
    argument for scoring a row rather than a seam.

    This used to be pinned on the glyph fixture with a synthetic badge painted
    down one cell edge. Registration made that construction pathological — a mark
    whose mirror is exactly the other row's mark, which no character does — and it
    started convicting the interior row at +19%. The real sheet says what the
    synthetic one was standing in for.
    """
    spec, sheet = load_fixture_sheet("handedness_4way.webp")
    window = pipeline.registration_window(spec.frame_w)
    cells = {
        key: pipeline._row_cells(sheet, spec, spec.row_by_key(key))
        for key in ("idle-e", "idle-n")
    }

    direct, flipped = pipeline._seam_distance(
        cells["idle-e"], cells["idle-n"], window=window
    )
    # The evidence a single-seam rule would have convicted `idle-n` on.
    assert (direct - flipped) / direct > pipeline.MIRROR_GAIN_THRESHOLD

    found = fixture_findings("handedness_4way.webp")

    assert found["flagged"] == []
    assert gains_by_row(found, "rotation") == {"idle-e": pytest.approx(0.0172, abs=5e-4)}
    assert "idle-n" in unjudged_rows(found, "rotation")


def test_a_non_directional_state_is_reported_unjudged_with_its_reason():
    spec = SheetSpec(
        states=(StateSpec("idle", 2, True), StateSpec("cheer", 2, False)),
        scheme=EIGHT_WAY,
    )

    result = pipeline.validate_sheet(spec, pipeline.compose_sheet(spec, glyph_cells(spec)))

    assert result["ok"], result["errors"]
    reasons = {
        row: entry["reason"]
        for entry in result["handedness"]["unjudged"]
        for row in entry["rows"]
    }
    assert "not directional" in reasons["cheer"]


def test_a_sheet_mirrored_on_EVERY_row_passes_and_that_is_the_blind_spot():
    """Pinned deliberately, because it is the hole and it must stay a known one.

    ``distance(flip(a), flip(b)) == distance(a, b)``: a consistently mirrored
    character is a perfect fixed point of this measure. Nothing internal to a
    sheet can catch it — the sheet is self-consistent, and only the world outside
    it says which way is east. If this test ever turns red, the check grew an
    outside reference and this section's claims need rewriting.

    Registration does not weaken the property and that is not free: the shift
    grid is a symmetric MINIMUM rather than a correlation peak precisely so the
    equality below stays exact (see
    ``test_the_shift_grid_is_symmetric_so_a_global_flip_still_scores_identically``).
    """
    every_row = tuple(row.key for row in EIGHT.authored_rows())
    upright = eight_way_sheet()
    flipped = eight_way_sheet(mirror=every_row)

    # A genuinely different picture, not two names for the same bytes: the
    # diagonals and profiles all point the other way.
    assert flipped.tobytes() != upright.tobytes()
    assert pipeline.validate_sheet(EIGHT, flipped)["ok"]
    assert pipeline.detect_mirrored_art(EIGHT, flipped)["flagged"] == []

    # And the reason, asserted rather than described: every seam measures EXACTLY
    # the same on both — BOTH of its distances, not swapped but unchanged,
    # because distance(flip a, flip b) == distance(a, b) and
    # distance(flip(flip a), flip b) == distance(flip a, b).
    window = pipeline.registration_window(EIGHT.frame_w)
    chain = [EIGHT.row_by_key(f"idle-{d}") for d in ("s", "se", "e", "ne", "n")]
    for left, right in zip(chain, chain[1:]):
        before = pipeline._seam_distance(
            pipeline._row_cells(upright, EIGHT, left),
            pipeline._row_cells(upright, EIGHT, right),
            window=window,
        )
        after = pipeline._seam_distance(
            pipeline._row_cells(flipped, EIGHT, left),
            pipeline._row_cells(flipped, EIGHT, right),
            window=window,
        )
        assert after == pytest.approx(before), f"{left.key}|{right.key}"



def test_a_wrong_size_sheet_still_answers_the_handedness_question_honestly():
    """The early geometry return carries the key, and says nothing was judged.

    A caller reading ``handedness`` must never get a missing key that reads like
    "clean" — the whole reason it is reported on a passing sheet too.
    """
    result = pipeline.validate_sheet(EIGHT, eight_way_sheet().crop((0, 0, 10, 10)))

    assert not result["ok"]
    assert result["handedness"]["flagged"] == []
    named = {row for entry in result["handedness"]["unjudged"] for row in entry["rows"]}
    assert named == {row.key for row in EIGHT.rows()}


# ────────── handedness on REAL art: the checked-in fixtures ──────────
#
# Everything above this line is drawn by the glyph draftsman, and the glyph is
# systematically kinder than a character: its arrow is one enormous asymmetric
# mark, so a mirrored glyph row used to score 29-36% where a mirrored anime-girl
# row scores 13-18%. A whole class of question — where does the threshold sit
# between the populations, what does a displacement do to the ratio, what does a
# mirrored STATE look like — cannot be asked of a fixture whose numbers are an
# order out.
#
# Until 2026-08-25 the only real defective art anyone could measure lived in one
# operator's hermes home, in a hand-made `.backup-2026-08-25-nefix` folder that
# nothing protected and no test could reach. These two sheets are that evidence,
# checked in. See tests/fixtures/charsheet/handedness.json for what each holds.


FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "charsheet"
FIXTURE_META = json.loads((FIXTURE_ROOT / "handedness.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=None)
def load_fixture_sheet(name: str):
    """``(spec, image)`` for a checked-in sheet of REAL character art.

    The spec travels in the sidecar rather than in this module so the fixture
    stays self-describing: a sheet whose row list only exists in a test is a
    sheet nobody can re-measure.

    Cached, and the callers below never mutate what they are handed — every
    mutation here goes through :func:`edit_row`, which copies. A registered
    detection over a 15-row sheet is ~2.7 s and ``validate_sheet``'s residue scan
    walks 1.8M pixels in Python, so re-deriving both per test would spend a
    minute of the suite on the same answer.
    """
    described = FIXTURE_META["sheets"][name]["spec"]
    spec = SheetSpec(
        states=tuple(
            StateSpec(state["name"], state["frames"], state["directional"])
            for state in described["states"]
        ),
        scheme=DirectionScheme(
            order=tuple(described["scheme"]["order"]),
            authored=tuple(described["scheme"]["authored"]),
            mirrored=dict(described["scheme"]["mirrored"]),
        ),
        frame_w=described["frameW"],
        frame_h=described["frameH"],
    )
    with Image.open(FIXTURE_ROOT / name) as opened:
        return spec, opened.convert("RGBA")


EIGHT_WAY_FIXTURE = "handedness_8way.webp"
# The checked-in sheet carries the genuinely mirrored `idle-ne`. Flipping that
# one row back is the character as it SHIPS today, and it is the base most of
# the mutation tests below build on.
REPAIRED = (("flip", "idle-ne"),)

# The acceptance token for a row BOTH passes agree about. Read off the code
# rather than typed, because the whole point of `accept_basis_token` is that the
# refusal that demands a spelling and the message that teaches it are one
# function — a literal here would be a third copy free to disagree with both.
TWO_BASIS_TOKEN = pipeline.accept_basis_token("rotation and states")


@functools.lru_cache(maxsize=None)
def variant(name: str, ops: tuple = ()):
    """``(spec, image)`` for a fixture with a deterministic mutation applied.

    Cached across the whole module, and that is a timing decision, not a tidiness
    one: ``detect_mirrored_art`` on this fixture is ~2.3 s and ``validate_sheet``
    ~2.6 s, several tests want the same mutated sheet, and the worst test in the
    charsheet suites was measured at 17.53 s against a 30 s cap. Every mutation
    here is pure — :func:`edit_row` copies — so the same ops always produce the
    same bytes and computing them twice buys nothing.
    """
    spec, image = load_fixture_sheet(name)
    for kind, *rest in ops:
        if kind == "flip":
            image = flip_rows(spec, image, *rest)
        elif kind == "slide":
            image = slide_row(spec, image, rest[0], rest[1])
        elif kind == "blank":
            image = blank_row(spec, image, rest[0])
        else:  # pragma: no cover - a typo in a test, not a code path
            raise AssertionError(f"unknown fixture mutation {kind!r}")
    return spec, image


@functools.lru_cache(maxsize=None)
def variant_findings(name: str, ops: tuple = ()):
    """:func:`detect_mirrored_art` on a fixture variant, computed once."""
    spec, sheet = variant(name, ops)
    return pipeline.detect_mirrored_art(spec, sheet)


@functools.lru_cache(maxsize=None)
def variant_validation(name: str, ops: tuple = (), accepted: tuple[str, ...] = ()):
    """:func:`validate_sheet` on a fixture variant, computed once."""
    spec, sheet = variant(name, ops)
    return pipeline.validate_sheet(spec, sheet, accept_handedness=accepted)


def fixture_findings(name: str):
    """:func:`detect_mirrored_art` on an UNMODIFIED fixture, computed once."""
    return variant_findings(name, ())


def fixture_validation(name: str, accepted: tuple[str, ...] = ()):
    """:func:`validate_sheet` on an UNMODIFIED fixture, computed once."""
    return variant_validation(name, (), accepted)


def edit_row(spec, image, key, change):
    """A copy of *image* with *change* applied to every cell of row *key*."""
    row = spec.row_by_key(key)
    out = image.copy()
    top = row.index * spec.frame_h
    for column in range(row.frames):
        left = column * spec.frame_w
        box = (left, top, left + spec.frame_w, top + spec.frame_h)
        fresh = Image.new("RGBA", (spec.frame_w, spec.frame_h), (0, 0, 0, 0))
        fresh.alpha_composite(change(out.crop(box)))
        out.paste(fresh, (left, top))
    return out


def flip_rows(spec, image, *keys):
    for key in keys:
        image = edit_row(
            spec, image, key, lambda cell: cell.transpose(Image.FLIP_LEFT_RIGHT)
        )
    return image


def slide_row(spec, image, key, dx):
    """Move a row's art *dx* px sideways inside its cells — art untouched."""

    def shifted(cell):
        pad = abs(dx) + 1
        wide = Image.new("RGBA", (cell.width + 2 * pad, cell.height), (0, 0, 0, 0))
        wide.alpha_composite(cell, (pad + dx, 0))
        return wide.crop((pad, 0, pad + cell.width, cell.height))

    return edit_row(spec, image, key, shifted)


def blank_row(spec, image, key):
    return edit_row(
        spec, image, key, lambda cell: Image.new("RGBA", cell.size, (0, 0, 0, 0))
    )


def gains_by_row(found, basis):
    return {
        entry["row"]: entry["gain"]
        for entry in found["judged"]
        if entry["basis"] == basis
    }


def unjudged_rows(found, basis):
    return {
        row
        for entry in found["unjudged"]
        if entry["basis"] == basis or basis in entry["basis"]
        for row in entry["rows"]
    }


def test_the_real_defect_is_caught_and_flipping_that_one_row_clears_the_sheet():
    """The gate measures what it says it measures, on the art that shipped.

    `idle-ne` in this fixture is the row that was drawn facing north-WEST,
    composed, installed, bundled into the launcher and found by a human looking
    at a 3D scene. Every other row is the same character's correct art. Flipping
    that one row — nothing else — takes the sheet from refused to clean, which is
    the claim "this measures handedness" stated as an experiment rather than as
    an adjective.
    """
    refused = fixture_validation(EIGHT_WAY_FIXTURE)
    assert not refused["ok"]
    assert [finding["row"] for finding in refused["handedness"]["flagged"]] == ["idle-ne"]

    repaired = variant_validation(EIGHT_WAY_FIXTURE, REPAIRED)
    assert repaired["ok"], repaired["errors"]
    assert repaired["handedness"]["flagged"] == []
    # And not by a whisker: the row that read +12.05% reads -13.70% flipped.
    assert gains_by_row(repaired["handedness"], "rotation")["idle-ne"] < -0.10


def test_the_same_fixed_point_holds_on_real_art_with_a_real_defect_present():
    """The blind spot, restated where it bites: a whole CHARACTER drawn mirrored.

    The glyph version above shows the algebra. This shows the consequence — flip
    every row of a real sheet that has one genuinely mirrored row in it and the
    findings do not move by a hundredth of a percent, because the sheet is still
    self-consistent and nothing inside it says which way is east.
    """
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    whole = flip_rows(spec, sheet, *(row.key for row in spec.authored_rows()))

    assert whole.tobytes() != sheet.tobytes()
    assert (
        pipeline.detect_mirrored_art(spec, whole)["flagged"]
        == fixture_findings("handedness_8way.webp")["flagged"]
    )


def test_the_threshold_separates_the_populations_ON_THIS_FIXTURE():
    """What the number 0.08 is FOR — and the honest limit of what this proves.

    The threshold had no test at all: the suite was green from ~0.03 to at least
    0.25, because the glyph fixture's true positives scored an order above the
    live discrimination band, so nothing noticed a threshold raised past the very
    defect the check shipped for. This pins the property instead of the value —
    every correct row of THIS sheet must fall below the line and its real
    mirrored row above it.

    **It is not a proof that the two populations separate**, and the name says
    which fixture it is about because that claim was made once and is false.
    Measured across more of the space than one sheet holds: the true floor on
    real art is +6.78% rotation / +7.64% states (`jumping-se` mirrored, under
    the line on both) and the false ceiling on correct art slid sideways is
    +18.75%. The line sits inside both populations. That is why a single basis
    now warns instead of refusing — the threshold is not the lever, so this test
    pins an ordering on known art rather than a separation in general.

    Measured on this fixture: the loudest false is `idle-e` at +5.03% (a correct
    row, pulled up because the row beside it is mirrored) and the only true
    positive is `idle-ne` at +12.05% in the rotation and +15.40% across states.
    """
    found = fixture_findings(EIGHT_WAY_FIXTURE)

    true_rows = {"idle-ne"}
    gains = [(entry["row"], entry["gain"]) for entry in found["judged"]]
    loudest_false = max(gain for row, gain in gains if row not in true_rows)
    quietest_true = min(gain for row, gain in gains if row in true_rows)

    assert loudest_false < pipeline.MIRROR_GAIN_THRESHOLD < quietest_true, (
        f"false ceiling {loudest_false:.4f}, true floor {quietest_true:.4f}"
    )


def test_sliding_a_correct_row_sideways_is_not_handedness():
    """Registration, and the false refusal it retires, in one test.

    `walk-e` here is correct art. Moving it 8 px sideways in a 192 px frame and
    changing nothing else made the shipped measure refuse the install: it paired
    cells by column index with no alignment step, so a displacement entered the
    distance and therefore the ratio. What kept that off real characters was
    `normalize_cells` centring each row on its union bbox — upstream pet code
    this package does not own — and that centring pins the BOX, not the body, so
    a bag or a cape hanging off one side of one row moves the body by half its
    width and crosses the line on correct art.

    Both states are asserted here on purpose: window=0 is the shipped measure and
    it convicts, the real window does not, and the registered number is the same
    one the unslid row scores.
    """
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    window = pipeline.registration_window(spec.frame_w)
    slid = slide_row(spec, sheet, "walk-e", -8)

    def walk_e_gain(image, at):
        cells = {
            key: pipeline._row_cells(image, spec, spec.row_by_key(key))
            for key in ("walk-se", "walk-e", "walk-ne")
        }
        seams = [
            pipeline._seam_distance(cells["walk-se"], cells["walk-e"], window=at),
            pipeline._seam_distance(cells["walk-e"], cells["walk-ne"], window=at),
        ]
        drawn = sum(direct for direct, _flipped in seams)
        return (drawn - sum(flipped for _direct, flipped in seams)) / drawn

    unregistered = walk_e_gain(slid, 0)
    registered = walk_e_gain(slid, window)

    assert unregistered >= pipeline.MIRROR_GAIN_THRESHOLD, unregistered
    assert registered < 0, registered
    assert registered == pytest.approx(walk_e_gain(sheet, window), abs=1e-9)
    assert (
        variant_findings(EIGHT_WAY_FIXTURE, (("slide", "walk-e", -8),))["flagged"]
        == fixture_findings(EIGHT_WAY_FIXTURE)["flagged"]
    )


def test_the_registration_window_is_the_size_it_says_and_bounds_what_it_says():
    """Two sabotages that survived a whole round trip, in one test.

    `REGISTRATION_WINDOW_DIVISOR = 12` mutated to `6` (window 32 px) and to `3`
    (window 64 px) both left all 62 tests green, while moving the false-refusal
    boundary on correct art from -24 px to -40 px to past -56 px. Nothing
    asserted the window's SIZE, and nothing asserted the thing it exists FOR —
    that a slide LARGER than the window is still seen. The one test that slides
    a row moves it 8 px, inside every candidate window, so it could not see the
    knob it was written to justify.

    So: the size, and the boundary either side of it. A slide of exactly the
    window is registered away and changes nothing; a slide half again the window
    is not, and shows up. Both readings are on real art of the same row.
    """
    assert pipeline.registration_window(192) == 16
    assert pipeline.registration_window(96) == 8
    # A frame narrower than the divisor still gets a one-pixel window rather
    # than a window of zero, which would be no registration at all.
    assert pipeline.registration_window(4) == 1

    window = pipeline.registration_window(192)
    inside = gains_by_row(
        variant_findings(EIGHT_WAY_FIXTURE, REPAIRED + (("slide", "walk-e", -window),)),
        "rotation",
    )
    past = gains_by_row(
        variant_findings(
            EIGHT_WAY_FIXTURE, REPAIRED + (("slide", "walk-e", -window - 8),)
        ),
        "rotation",
    )
    clean = gains_by_row(variant_findings(EIGHT_WAY_FIXTURE, REPAIRED), "rotation")

    # Inside the window the displacement is registered away: the reading stays
    # within two points of the unslid row's and nowhere near the line. It is not
    # EXACTLY equal, and the reason is worth knowing — sliding a row inside its
    # own cell clips the art against the cell edge, so a window-sized slide is
    # not a pure translation. Registration cancels the displacement; nothing can
    # give back the pixels that fell off.
    assert inside["walk-e"] == pytest.approx(clean["walk-e"], abs=0.02)
    assert inside["walk-e"] < 0
    # Past it: the same correct art now reads over the line. That is the residue
    # the window BOUNDS rather than removes, and the reason a single basis warns
    # instead of refusing. A window twice as wide would register this away too,
    # which is what makes these two assertions a pin on the SIZE and not just on
    # the existence of a window.
    assert past["walk-e"] >= pipeline.MIRROR_GAIN_THRESHOLD


def test_ONE_displacement_can_fool_BOTH_bases_so_an_error_is_not_proof_either():
    """The two-basis ERROR tier is not proof of handedness, and here is the case.

    Both passes share one registration — `registration_window(spec.frame_w)` is
    computed once and handed to the rotation pass and the cross-state pass alike
    — so a displacement past the window biases them TOGETHER. That was an
    argument from the code with no fixture behind it, and a 2026-09-02 sweep
    struck the number it was argued with as never having existed.

    The number was real and only unpinned. Measured at HEAD on the shipped-
    correct art: sliding `walk-e` by -32 px (one sixth of a 192 px frame, twice
    the 16 px window, ART UNTOUCHED) produces a `rotation and states` finding on
    `walk-e` at +18.2%, attribution `both`, severity `error` — the tier that
    refuses the install. The same sheet unslid flags nothing at all.

    It is a narrow window in the literal sense: -24 px blames a NEIGHBOUR with a
    single basis, -48 px is exonerated by the second basis (`contradicted`,
    warning), and -64 and beyond flag nothing. That is not reassurance — an
    operator does not get to choose how far a prop hangs off one row — it is the
    shape of a blind spot, and it is why `--accept-handedness` exists as a door
    on `compose` rather than as a courtesy.
    """
    slid = REPAIRED + (("slide", "walk-e", -32),)

    findings = variant_findings(EIGHT_WAY_FIXTURE, slid)
    flagged = {entry["row"]: entry for entry in findings["flagged"]}

    assert not variant_findings(EIGHT_WAY_FIXTURE, REPAIRED)["flagged"], (
        "the base art is not clean, so nothing below is about the displacement"
    )
    assert "walk-e" in flagged
    assert flagged["walk-e"]["basis"] == "rotation and states"
    assert flagged["walk-e"]["attribution"] == "both"
    assert flagged["walk-e"]["severity"] == "error", (
        "a pure displacement of correct art reached the tier that refuses"
    )

    refused = variant_validation(EIGHT_WAY_FIXTURE, slid)
    assert not refused["ok"]
    assert [error for error in refused["errors"] if "walk-e" in error]

    # And the only defence: an operator who has LOOKED at the row can say so.
    # This is the argument for keeping that door, not a reason to trust the tier.
    accepted = variant_validation(
        EIGHT_WAY_FIXTURE, slid, (f"walk-e:{TWO_BASIS_TOKEN}",)
    )
    assert accepted["ok"], accepted["errors"]
    assert [entry["row"] for entry in accepted["handedness"]["accepted"]] == ["walk-e"]


def test_the_shift_grid_is_symmetric_so_a_global_flip_still_scores_identically():
    """Why the registration is a MINIMUM over a symmetric grid and not a peak.

    ``distance(shift(flip a, d), flip b) == distance(shift(a, -d), b)``, so over a
    grid symmetric about zero the set of scores is unchanged by a global flip and
    its minimum is exactly equal. A cross-correlation peak with a first-wins
    tie-break would pick a different shift on symmetric art and break that
    equality — and with it the fixed-point property the blind-spot test pins.
    """
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    window = pipeline.registration_window(spec.frame_w)
    flip = Image.FLIP_LEFT_RIGHT
    left = pipeline._row_cells(sheet, spec, spec.row_by_key("walk-e"))[0]
    # A pair whose registration lands on the LAST shift of the grid, because a
    # grid missing one endpoint is indistinguishable from a symmetric one on any
    # pair that lines up somewhere in the middle. Slid by exactly the window, the
    # two are the same picture, so the true minimum is 0 and it is reachable from
    # only one end — from the other end once both are flipped.
    right = pipeline._row_cells(
        slide_row(spec, sheet, "walk-e", window), spec, spec.row_by_key("walk-e")
    )[0]

    direct, forward = pipeline._registered_distance(left, right, window)
    mirrored, backward = pipeline._registered_distance(
        left.transpose(flip), right.transpose(flip), window
    )

    assert (forward, backward) == (window, -window)
    assert direct == pytest.approx(0.0, abs=1e-9)
    assert mirrored == pytest.approx(direct, abs=1e-9)
    # And on ordinary neighbours, where the minimum sits somewhere in the middle.
    neighbour = pipeline._row_cells(sheet, spec, spec.row_by_key("walk-ne"))[0]
    assert pipeline._registered_distance(left, neighbour, window)[0] == pytest.approx(
        pipeline._registered_distance(
            left.transpose(flip), neighbour.transpose(flip), window
        )[0]
    )


def test_only_the_culprit_of_a_run_is_reported_and_the_neighbour_is_named():
    """A mirrored row raises BOTH its neighbours; only one of them is the fault.

    Every flagged row used to be its own error, and every error says
    `characters reroll-row`. `reroll_row` proposes then approves unconditionally
    and there is no `approve-row` verb, so an operator obeying a three-row
    refusal spends correct approved art it cannot get back. Here `walk-e` is
    mirrored and `walk-ne` reads +14.40% because of it — one culprit, one
    message, and the message says not to touch the other one.
    """
    found = variant_findings(EIGHT_WAY_FIXTURE, (("flip", "walk-e"),))

    walk = [finding for finding in found["flagged"] if finding["state"] == "walk"]
    assert [finding["row"] for finding in walk] == ["walk-e"]
    assert walk[0]["attributed"] and walk[0]["attribution"] == "both"
    assert [entry["row"] for entry in walk[0]["corroborating"]] == ["walk-ne"]
    # `walk-ne` really is over the line — it is suppressed by attribution, not by
    # a threshold that happens to sit above it.
    assert (
        gains_by_row(found, "rotation")["walk-ne"] >= pipeline.MIRROR_GAIN_THRESHOLD
    )

    message = pipeline.mirrored_art_error(walk[0])
    assert "characters reroll-row --row walk-e" in message
    assert "characters reroll-row --row walk-ne" not in message
    assert "walk-ne" in message and "Do NOT re-roll them" in message


def test_the_culprit_is_not_simply_the_first_row_of_the_run():
    """The sabotage that survived a whole round trip, pinned.

    `_finding_from_run`'s `culprit = max(run, key=gain)` mutated to `run[0][1]`
    left all 62 tests green, because in this fixture the mirrored row is the
    FIRST of its run and the two rules name the same row. Mirroring `walk-ne`
    instead raises its PREDECESSOR `walk-e`, so the run reads `walk-e` then
    `walk-ne` and the first of the run is the innocent one.
    """
    spec, _sheet = load_fixture_sheet(EIGHT_WAY_FIXTURE)
    found = variant_findings(EIGHT_WAY_FIXTURE, REPAIRED + (("flip", "walk-ne"),))

    walk = [finding for finding in found["flagged"] if finding["state"] == "walk"]
    assert [finding["row"] for finding in walk] == ["walk-ne"]
    rotation = gains_by_row(found, "rotation")
    # The predecessor really was raised, and it really is the EARLIER row of the
    # chain — without both, this fixture cannot tell the repair from the bug
    # either, which is how the sabotage survived in the first place.
    assert rotation["walk-e"] > 0
    assert spec.row_by_key("walk-e").index < spec.row_by_key("walk-ne").index


@functools.lru_cache(maxsize=None)
def two_state_cut(ops: tuple = ()):
    """The fixture's `idle` and `walk` states only — the DEFAULT sheet's shape.

    `characters start` creates `idle:6, walk:8`, so this is what the check
    actually meets on most characters: no cross-state pass at all, and the
    rotation as the only reading there will ever be.
    """
    spec, source = variant(EIGHT_WAY_FIXTURE, REPAIRED)
    cut = SheetSpec(
        states=tuple(state for state in spec.states if state.name != "jump"),
        scheme=spec.scheme,
        frame_w=spec.frame_w,
        frame_h=spec.frame_h,
    )
    out = Image.new("RGBA", cut.sheet_size(), (0, 0, 0, 0))
    for row in cut.rows():
        cells = pipeline._row_cells(source, spec, spec.row_by_key(row.key))
        for column, cell in enumerate(cells[: row.frames]):
            out.paste(cell, (column * cut.frame_w, row.index * cut.frame_h))
    for kind, *rest in ops:
        if kind == "flip":
            out = flip_rows(cut, out, *rest)
        elif kind == "slide":
            out = slide_row(cut, out, rest[0], rest[1])
        else:  # pragma: no cover - a typo in a test
            raise AssertionError(f"unknown mutation {kind!r}")
    return cut, out


def test_on_the_DEFAULT_two_state_sheet_a_run_is_never_attributed():
    """The rotation ranks. It does not convict, and here it is all there is.

    Two states means no cross-state pass, so a run of adjacent flagged rows has
    no second basis that could take it apart — and the run's maximum is not the
    culprit: `walk-e` here is CORRECT art slid -24 px, and the UNTOUCHED
    `walk-ne` is the louder of the two. `culprit = max(run, key=gain)` and
    `culprit = run[0][1]` both name an innocent row on this shape; naming none
    is the only honest answer, and the message says which rows to crop.
    """
    cut, sheet = two_state_cut((("slide", "walk-e", -24),))
    found = pipeline.detect_mirrored_art(cut, sheet)

    assert gains_by_row(found, "states") == {}
    rotation = gains_by_row(found, "rotation")
    # The run really has two rows and its maximum really is the innocent one.
    assert rotation["walk-e"] >= pipeline.MIRROR_GAIN_THRESHOLD
    assert rotation["walk-ne"] > rotation["walk-e"]

    (finding,) = [f for f in found["flagged"] if f["state"] == "walk"]
    assert finding["attributed"] is False
    assert finding["attribution"] == "run"
    assert [entry["row"] for entry in finding["alternatives"]] == ["walk-ne", "walk-e"]
    assert "characters reroll-row" not in pipeline.mirrored_art_error(finding)


def test_a_lone_flagged_row_on_a_two_state_sheet_is_still_named():
    """The control: "never attribute a RUN" must not become "never attribute".

    One mirrored row whose neighbours stay under the line is a run of one. There
    is nothing for it to be confused with, so it is named — which is what keeps
    the founding defect (`ne` mirrored in every state, one isolated row per
    state) attributable on a sheet with no cross-state pass at all.
    """
    cut, sheet = two_state_cut((("flip", "walk-ne"),))
    found = pipeline.detect_mirrored_art(cut, sheet)

    rotation = gains_by_row(found, "rotation")
    assert rotation["walk-e"] < pipeline.MIRROR_GAIN_THRESHOLD  # a run of ONE

    (finding,) = [f for f in found["flagged"] if f["state"] == "walk"]
    assert finding["row"] == "walk-ne"
    assert (finding["attributed"], finding["attribution"]) == (True, "rotation")
    assert finding["severity"] == "warning"


def test_a_correct_row_slid_sideways_does_not_get_its_NEIGHBOUR_re_rolled():
    """Inversion shape one: placement. The run's maximum is an innocent row.

    `walk-e` here is CORRECT art slid -24 px in a 192 px frame — art untouched,
    a displacement half again the registration window, which a prop or a framing
    drift reaches. It reads +10.48% and drags its UNTOUCHED neighbour `walk-ne`
    to +10.68%, so "the culprit is the run's maximum" names `walk-ne` and files
    the disturbed row under "Do NOT re-roll them" — exactly backwards, and
    pointed at approved art a reroll cannot give back.

    Nothing internal to the sheet can attribute this, so the honest answer is to
    attribute nothing: the finding names the run, says so, and warns.
    """
    found = variant_findings(EIGHT_WAY_FIXTURE, REPAIRED + (("slide", "walk-e", -24),))

    walk = [finding for finding in found["flagged"] if finding["state"] == "walk"]
    assert len(walk) == 1
    finding = walk[0]
    rotation = gains_by_row(found, "rotation")
    # The run really does invert: both rows are over the line and the innocent
    # NEIGHBOUR is the louder of the two. Without this the test cannot tell the
    # repair from the bug.
    assert rotation["walk-e"] >= pipeline.MIRROR_GAIN_THRESHOLD
    assert rotation["walk-ne"] > rotation["walk-e"]

    assert finding["attributed"] is False
    assert [entry["row"] for entry in finding["alternatives"]] == ["walk-ne", "walk-e"]
    assert finding["corroborating"] == []
    assert finding["severity"] == "warning"

    message = pipeline.mirrored_art_error(finding)
    assert "cannot say which" in message
    assert "NOT attributed" in message
    assert "characters reroll-row" not in message


def test_a_lone_flagged_row_the_other_states_vouch_for_is_not_named():
    """The other half of finding 1: a run of ONE can still be an innocent row.

    `idle-e` slid -48 px is correct art displaced far past the window; it is the
    only row of its state over the line (+8.08%, its neighbours at +5.49% and
    +7.84%), so "the run's maximum" and "the only flagged row" are the same row
    and a run rule cannot help. What clears it is the SECOND basis reading the
    other way: the same direction in `walk` and `jump` prefers the art as drawn
    (-3.23%), and `e` is over the line in one rotation of three rather than
    three of three — so this is placement, not a direction drawn backwards, and
    nothing is named.
    """
    found = variant_findings(EIGHT_WAY_FIXTURE, REPAIRED + (("slide", "idle-e", -48),))

    rotation = gains_by_row(found, "rotation")
    assert rotation["idle-e"] >= pipeline.MIRROR_GAIN_THRESHOLD
    assert rotation["idle-se"] < pipeline.MIRROR_GAIN_THRESHOLD
    assert rotation["idle-ne"] < pipeline.MIRROR_GAIN_THRESHOLD  # a run of ONE
    assert gains_by_row(found, "states")["idle-e"] < 0

    (finding,) = [f for f in found["flagged"] if f["state"] == "idle"]
    assert (finding["attributed"], finding["attribution"]) == (False, "contradicted")
    assert finding["severity"] == "warning"
    message = pipeline.mirrored_art_error(finding)
    assert "vouches for it" in message and "PLACEMENT" in message
    assert "characters reroll-row" not in message


def test_a_correct_row_flanked_by_two_mirrored_ones_is_not_the_culprit():
    """Inversion shape two: flanked. Reachable from ONE more bad row prompt.

    Mirror `idle-se` and `idle-ne` and leave `idle-e` correct — which is what a
    SECOND badly-worded diagonal in `VIEW_LANGUAGE` produces, the way `ne` alone
    produced the first one. The correct middle row wins the rotation run at
    +14.28% and used to be reported as a standalone `rotation` culprit, while
    the evidence exonerating it — its cross-state reading of -97.62% — sat
    unused in the same payload.

    Now the cross-state pass is consulted BEFORE the rotation is allowed to
    point at anybody, so it names `idle-ne`, which both passes agree about, and
    lists the correct `idle-e` as corroborating with "do not re-roll".
    """
    found = variant_findings(
        EIGHT_WAY_FIXTURE, REPAIRED + (("flip", "idle-se", "idle-ne"),)
    )

    rotation = gains_by_row(found, "rotation")
    states = gains_by_row(found, "states")
    # The trap is real on this art: the CORRECT row is the loudest of its run.
    assert rotation["idle-e"] > rotation["idle-ne"] >= pipeline.MIRROR_GAIN_THRESHOLD
    assert states["idle-e"] < 0

    by_row = {finding["row"]: finding for finding in found["flagged"]}
    assert "idle-e" not in by_row
    assert by_row["idle-ne"]["basis"] == "rotation and states"
    assert by_row["idle-ne"]["severity"] == "error"
    assert [entry["row"] for entry in by_row["idle-ne"]["corroborating"]] == ["idle-e"]
    assert by_row["idle-se"]["basis"] == "states"

    message = pipeline.mirrored_art_error(by_row["idle-ne"])
    assert "characters reroll-row --row idle-ne" in message
    assert "'idle-e'" in message and "Do NOT re-roll them" in message


def test_a_direction_mirrored_in_EVERY_state_is_still_attributed():
    """And the reason a negative cross-state reading is not a character witness.

    The founding defect: `ne` drawn north-WEST in all three states. The
    cross-state pass says nothing there — a unanimous consensus convicts nobody,
    and each mirrored row reads -111.32% — so a rule that demoted any row its
    other states "vouch for" would silently stop naming the very defect this
    gate was built for. What separates the two cases is that `ne` is over the
    line in the rotation of THREE states out of three here, and in ONE of three
    in the flanked and placement shapes above.
    """
    found = variant_findings(
        EIGHT_WAY_FIXTURE, REPAIRED + (("flip", "idle-ne", "walk-ne", "jump-ne"),)
    )

    states = gains_by_row(found, "states")
    assert states["idle-ne"] < -1.0  # the pass is not merely quiet, it is inverted

    named = {
        finding["row"]: finding
        for finding in found["flagged"]
        if finding["attributed"]
    }
    assert sorted(named) == ["idle-ne", "jump-ne", "walk-ne"]
    # One basis only, so it warns rather than refusing — and says which.
    assert {finding["severity"] for finding in named.values()} == {"warning"}
    assert {finding["basis"] for finding in named.values()} == {"rotation"}


def test_a_whole_state_drawn_mirrored_is_caught_across_the_states():
    """The blind spot `add_state` makes reachable, and the pass that closes it.

    The rotation pass is a fixed point per STATE: mirror every row of one state
    and the chain still fits itself perfectly, which matters because `add_state`
    generates all of a new state's rows in one batch against one reference and
    one prompt — the same shape as the generation that drew `ne` backwards three
    times. Comparing the same direction ACROSS states is what sees it, and both
    halves are asserted here: the rotation finds nothing in `jump`, the states
    pass convicts all three of its judged rows.

    **And it REFUSES on that one basis (owner ruling 2026-08-25).** This test
    asserted `{"warning"}` until then, and named the rule that would change it:
    "a `states` finding covering EVERY judged row of one state is an error,
    which is a second-order consensus and not a second basis". That is now the
    code. Demanding a second basis here means demanding one that cannot exist —
    the rotation is blind to this defect by algebra, not by bad luck — so the
    wait would have been forever, and on the two-state default sheet a whole
    mirrored state scores bit-identical to the correct sheet, which is the only
    other reading there could have been.
    """
    mirrored_state = REPAIRED + (
        ("flip", "jump-s", "jump-se", "jump-e", "jump-ne", "jump-n"),
    )
    found = variant_findings(EIGHT_WAY_FIXTURE, mirrored_state)

    rotation = gains_by_row(found, "rotation")
    assert all(
        rotation[f"jump-{direction}"] < pipeline.MIRROR_GAIN_THRESHOLD
        for direction in ("se", "e", "ne")
    ), rotation
    assert sorted(
        (finding["row"], finding["basis"]) for finding in found["flagged"]
    ) == [("jump-e", "states"), ("jump-ne", "states"), ("jump-se", "states")]
    assert {finding["severity"] for finding in found["flagged"]} == {"error"}
    assert all(finding["attributed"] for finding in found["flagged"])
    # Every flagged row carries the state's whole roster, in sheet order, so the
    # message names the fault an operator has to fix rather than one row of it.
    assert all(
        finding["wholeState"] == ["jump-se", "jump-e", "jump-ne"]
        for finding in found["flagged"]
    )

    installed = variant_validation(EIGHT_WAY_FIXTURE, mirrored_state)
    assert not installed["ok"]
    assert len(installed["errors"]) == 3
    assert not [text for text in installed["warnings"] if "handedness" in text]
    assert pipeline.handedness_summary(dict(installed["handedness"], accepted=[])) \
        .startswith("handedness: 9 row(s) judged, 3 refused")


def test_the_whole_state_message_names_the_state_and_the_override_it_accepts():
    """An error with no reachable override is a wall, so the text carries one.

    Before this ruling the ONLY spelling the validator accepted was
    `rotation+states` — a hardcoded constant — so a refusal raised on the
    `states` basis alone had no legal acceptance at all. The message now spells
    the token this finding's own basis needs, and `validate_sheet` derives the
    demand from the same function, so the two cannot drift.
    """
    found = variant_findings(
        EIGHT_WAY_FIXTURE,
        REPAIRED + (("flip", "jump-s", "jump-se", "jump-e", "jump-ne", "jump-n"),),
    )
    finding = next(f for f in found["flagged"] if f["row"] == "jump-e")

    message = pipeline.mirrored_art_error(finding)

    assert "WHOLE STATE" in message
    assert "'jump'" in message
    # The roster, so the operator sees the size of what they are looking at.
    for row in ("jump-se", "jump-e", "jump-ne"):
        assert f"'{row}'" in message
    # Why one basis is enough HERE, stated where it is read.
    assert "FIXED POINT" in message
    assert "add-state" in message
    # The override, spelled the way the validator will accept it.
    assert "--accept-handedness jump-e:states" in message
    assert "rotation+states" not in message


def test_a_single_mirrored_row_across_states_is_NOT_escalated():
    """The half of the ruling that says what does NOT change.

    A 4-way scheme authors `s, e, n`, so `turnaround_order(...)[1:-1]` is ONE
    direction and the cross-state pass judges exactly one row per state. Without
    the `>= 2` guard "every judged row of the state" would be satisfied by a
    single row every time, and a 4-way sheet would refuse on precisely the
    reading the owner declined to escalate.

    Built from the real 4-way fixture's art rather than the 8-way one, because
    the one-judged-row shape IS the 4-way scheme.
    """
    spec, source = load_fixture_sheet("handedness_4way.webp")
    wide, sheet = four_way_three_states(spec, source, mirror=("extra-e",))
    found = pipeline.detect_mirrored_art(wide, sheet)

    states = gains_by_row(found, "states")
    # The premise: exactly one row per state is cross-state judged.
    assert sorted(states) == ["copy-e", "extra-e", "idle-e"]
    flagged = {finding["row"]: finding for finding in found["flagged"]}
    assert "extra-e" in flagged and flagged["extra-e"]["basis"] == "states"

    assert "wholeState" not in flagged["extra-e"]
    assert flagged["extra-e"]["severity"] == "warning"
    assert pipeline.validate_sheet(wide, sheet)["ok"]


def test_a_state_with_one_judged_row_still_CLEAN_is_not_a_whole_state():
    """"Every judged row", never a majority — and this is the difference.

    `idle-se` and `idle-ne` mirrored leaves `idle-e` judged and CLEAN across the
    states (-97.62%). Two of three is a contiguous BLOCK of mirrored rows, which
    the docstring already bounds; it is not a state drawn backwards, and the
    sheet says so itself. A majority rule would have called this a whole state
    and refused an install on evidence that contradicts it.
    """
    found = variant_findings(
        EIGHT_WAY_FIXTURE, REPAIRED + (("flip", "idle-se", "idle-ne"),)
    )

    states = gains_by_row(found, "states")
    assert sorted(states) == ["idle-e", "idle-ne", "idle-se", "jump-e", "jump-ne",
                              "jump-se", "walk-e", "walk-ne", "walk-se"]
    assert states["idle-e"] < 0, "the premise: one row of the state reads clean"

    by_row = {finding["row"]: finding for finding in found["flagged"]}
    assert "wholeState" not in by_row["idle-se"]
    assert by_row["idle-se"]["severity"] == "warning"
    # And the row both passes agree about still refuses, on the OTHER rule.
    assert by_row["idle-ne"]["severity"] == "error"
    assert "wholeState" not in by_row["idle-ne"]


def test_the_whole_state_refusal_is_overridable_row_by_row():
    """An error the operator cannot get past is a wall, and `compose` has no
    other door.

    The grammar that shipped on 2026-08-25 had a single hardcoded token
    (`rotation+states`) and refused every other spelling, so promoting a
    one-basis reading to an error without touching it would have produced
    exactly that wall. The token is now derived from the finding's basis, and a
    whole-state refusal is waived ONE ROW AT A TIME like any other — a
    state-wide reading waived state-wide in one token is the blanket this
    grammar exists to refuse.
    """
    mirrored_state = REPAIRED + (
        ("flip", "jump-s", "jump-se", "jump-e", "jump-ne", "jump-n"),
    )
    rows = ("jump-se", "jump-e", "jump-ne")

    # A bare row name is refused, and the refusal spells what to type.
    bare = variant_validation(EIGHT_WAY_FIXTURE, mirrored_state, ("jump-e",))
    assert not bare["ok"]
    assert bare["handedness"]["accepted"] == []
    assert any("with no basis" in error for error in bare["errors"])
    assert any(
        "--accept-handedness jump-e:states" in error for error in bare["errors"]
    )
    assert any("whole state reads as" in error for error in bare["errors"])

    # So is the OTHER shape's token: this finding's bases are not those.
    wrong = variant_validation(
        EIGHT_WAY_FIXTURE, mirrored_state, ("jump-e:rotation+states",)
    )
    assert not wrong["ok"]
    assert any("is spelled jump-e:states" in error for error in wrong["errors"])

    # Accepting ONE of the three leaves the other two refusing.
    partial = variant_validation(EIGHT_WAY_FIXTURE, mirrored_state, ("jump-e:states",))
    assert not partial["ok"]
    assert [entry["row"] for entry in partial["handedness"]["accepted"]] == ["jump-e"]
    assert len([e for e in partial["errors"] if "MIRROR" in e]) == 2

    # All three, and the install goes through carrying the record.
    every = variant_validation(
        EIGHT_WAY_FIXTURE, mirrored_state, tuple(f"{row}:states" for row in rows)
    )
    assert every["ok"], every["errors"]
    assert [entry["row"] for entry in every["handedness"]["accepted"]] == list(rows)
    assert all(entry["basis"] == "states" for entry in every["handedness"]["accepted"])
    # Accepted, not erased: the refusal text survives as a warning, verbatim.
    assert len(every["warnings"]) == 3
    assert all("WHOLE STATE" in text for text in every["warnings"])


def test_the_default_two_state_character_reaches_NEITHER_refusal():
    """The premise this ruling was checked against, pinned so it stays honest.

    `characters start` creates `idle:6, walk:8`. Two states is one cross-state
    pair, and one pair cannot say which side is wrong, so the states pass says
    nothing at all — no `rotation and states` finding is reachable and no
    whole-state consensus is either. Mirror a whole state on such a sheet and
    the check still only warns.

    This is not a defect the whole-state rule failed to fix. It is the same
    algebra: with one witness there is no consensus to take, and the honest
    answer is the warning plus a third state.
    """
    cut, sheet = two_state_cut(
        (("flip", "walk-s", "walk-se", "walk-e", "walk-ne", "walk-n"),)
    )
    found = pipeline.detect_mirrored_art(cut, sheet)

    assert gains_by_row(found, "states") == {}
    assert not [f for f in found["flagged"] if f.get("wholeState")]
    assert "error" not in {f["severity"] for f in found["flagged"]}
    assert pipeline.validate_sheet(cut, sheet)["ok"]


def four_way_three_states(spec, source, mirror: tuple[str, ...] = ()):
    """The 4-way fixture's one state, plus two more phase-rotated copies.

    Three states is the cross-state pass's minimum, and a 4-way scheme judges
    ONE direction (`e`) — which is the shape the `>= 2` guard exists for. A byte
    copy would be useless (identical cells measure zero and drop out of the
    ranking), so each added state rotates the frame order, exactly as
    `four_state_sheet` does for the 8-way fixture.

    NOT `lru_cache`d, unlike its 8-way sibling: a `SheetSpec` carries a dict
    (`scheme.mirrored`) and is therefore unhashable, and the 4-way fixture is
    2 frames over 3 rows per state — cheap enough that the cache would buy
    nothing anyway.
    """
    wide = SheetSpec(
        states=tuple(
            list(spec.states)
            + [StateSpec("copy", spec.states[0].frames, True),
               StateSpec("extra", spec.states[0].frames, True)]
        ),
        scheme=spec.scheme,
        frame_w=spec.frame_w,
        frame_h=spec.frame_h,
    )
    out = Image.new("RGBA", wide.sheet_size(), (0, 0, 0, 0))
    out.alpha_composite(source, (0, 0))
    base_state = spec.states[0].name
    for offset, name in ((1, "copy"), (2, "extra")):
        for direction in spec.scheme.authored:
            cells = pipeline._row_cells(
                source, spec, spec.row_by_key(f"{base_state}-{direction}")
            )
            row = wide.row_by_key(f"{name}-{direction}")
            for column in range(row.frames):
                out.paste(
                    cells[(column + offset) % len(cells)],
                    (column * wide.frame_w, row.index * wide.frame_h),
                )
    return wide, flip_rows(wide, out, *mirror) if mirror else out


@functools.lru_cache(maxsize=None)
def four_state_sheet(mirror: tuple[str, ...] = ()):
    """The three real states plus a fourth, then *mirror* applied.

    The fourth state is `idle`'s art with the frame order rotated by one: real
    art, a different animation phase, correct handedness. A byte COPY would be
    useless — identical cells measure zero, `_gain` returns ``None`` and the
    pair drops out of the ranking entirely — so the fourth state has to be a
    genuine independent witness. One `add-state` is all it takes to reach four.
    """
    spec, source = variant(EIGHT_WAY_FIXTURE, REPAIRED)
    wide = SheetSpec(
        states=tuple(list(spec.states) + [StateSpec("extra", 3, True)]),
        scheme=spec.scheme,
        frame_w=spec.frame_w,
        frame_h=spec.frame_h,
    )
    out = Image.new("RGBA", wide.sheet_size(), (0, 0, 0, 0))
    out.alpha_composite(source, (0, 0))
    for direction in spec.scheme.authored:
        cells = pipeline._row_cells(source, spec, spec.row_by_key(f"idle-{direction}"))
        row = wide.row_by_key(f"extra-{direction}")
        for column in range(row.frames):
            out.paste(
                cells[(column + 1) % len(cells)],
                (column * wide.frame_w, row.index * wide.frame_h),
            )
    return wide, flip_rows(wide, out, *mirror) if mirror else out


def test_an_EVEN_number_of_states_that_splits_evenly_convicts_nobody():
    """`len(pairs) // 2 + 1` is not a majority when the states are even.

    On a FOUR-state sheet that is 2 of 3, so a 2-2 split left every row inside a
    "majority" and convicted all four — the two CORRECT ones at basis `states`
    with no corroborating marker and no "Do NOT re-roll them", while their
    rotation readings sat well under the line. `SKILL.md` told the agent a
    multi-row `states` refusal is the one case where re-rolling several rows IS
    right, so following it spends two correct approved attempts. One `add-state`
    takes the live character from three states to four.

    The rule is now a strict majority of ALL the states that draw the direction:
    a row is convicted only when its camp is a strict MINORITY. Two camps of two
    are neither, so the direction goes unjudged and says why.
    """
    wide, sheet = four_state_sheet(("idle-e", "walk-e"))
    found = pipeline.detect_mirrored_art(wide, sheet)

    # `e` is the direction that split; `se` and `ne` are untouched and still
    # judged, which is the point — the refusal to convict is scoped to the
    # direction that has no minority, not to the whole pass.
    states = gains_by_row(found, "states")
    assert not [row for row in states if row.endswith("-e")]
    assert {row for row in states if row.endswith("-se")} == {
        f"{state}-se" for state in ("idle", "walk", "jump", "extra")
    }
    split = [
        entry
        for entry in found["unjudged"]
        if entry["basis"] == "states" and "split evenly" in entry["reason"]
    ]
    assert len(split) == 1
    assert sorted(split[0]["rows"]) == ["extra-e", "idle-e", "jump-e", "walk-e"]

    # The two CORRECT rows are not flagged at all now, and the two genuinely
    # mirrored ones are still seen — by the rotation, on one basis, so they warn.
    flagged = {finding["row"]: finding for finding in found["flagged"]}
    assert sorted(flagged) == ["idle-e", "walk-e"]
    assert {finding["severity"] for finding in flagged.values()} == {"warning"}
    assert {finding["basis"] for finding in flagged.values()} == {"rotation"}


def test_a_fourth_state_does_not_cost_the_cross_state_pass_its_sensitivity():
    """The control for the test above: the fix must not simply switch it off.

    One mirrored row out of four states is a camp of one against three, which is
    a strict minority under both the old rule and the new one — so it is still
    convicted, on both bases, and it still refuses.
    """
    wide, sheet = four_state_sheet(("idle-e",))
    found = pipeline.detect_mirrored_art(wide, sheet)

    (finding,) = found["flagged"]
    assert finding["row"] == "idle-e"
    assert finding["basis"] == "rotation and states"
    assert finding["severity"] == "error"
    assert gains_by_row(found, "states")["idle-e"] >= pipeline.MIRROR_GAIN_THRESHOLD


def test_across_two_states_the_cross_state_read_refuses_to_guess():
    """Two states is one pair, and one pair cannot say which side is wrong.

    Exactly the end-row rule, on the other axis — and the reason the default
    `idle:6, walk:8` sheet gets nothing from this pass until a third state
    arrives.
    """
    found = pipeline.detect_mirrored_art(EIGHT, eight_way_sheet())

    assert gains_by_row(found, "states") == {}
    reasons = [
        entry["reason"] for entry in found["unjudged"] if entry["basis"] == "states"
    ]
    assert reasons and all("only 2 state(s) draw" in reason for reason in reasons)


def test_a_blank_row_costs_only_the_seams_it_touches():
    """One empty row used to make its whole state unjudged, silently.

    `validate_sheet` records an empty row as a WARNING as long as some row is
    filled, so a sheet with a blank row still installs; the handedness check then
    gave up on that row's entire rotation and said so only in a payload nobody
    read. A row is now judged whenever it has a measurable seam on each side, so
    a blank row costs its two neighbours and nothing else — and the other states
    are untouched.
    """
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    # `walk-s` blank AND `walk-e` mirrored: under the old rule the empty row made
    # the whole `walk` chain unjudged, so the mirrored row in it shipped.
    broken = flip_rows(spec, blank_row(spec, sheet, "walk-s"), "walk-e")

    found = pipeline.detect_mirrored_art(spec, broken)

    assert "walk-e" in {finding["row"] for finding in found["flagged"]}
    # Only the neighbour of the blank row lost its judgement; the rest of the
    # chain kept both seams.
    rotation = gains_by_row(found, "rotation")
    assert {"walk-e", "walk-ne"} <= set(rotation)
    assert "walk-se" not in rotation
    assert {"walk-s", "walk-se"} <= unjudged_rows(found, "rotation")
    # The blank row is named for what it is, not folded into a neighbour's reason.
    assert any(
        entry["rows"] == ["walk-s"] and "empty row has no facing" in entry["reason"]
        for entry in found["unjudged"]
    )


def test_a_row_identical_to_both_neighbours_is_unjudged_not_dropped():
    """The one place the module used to break its own accounting rule.

    ``if as_drawn <= 0: continue`` dropped such a row from ``flagged`` AND from
    ``unjudged``, so a caller reading the payload saw a row that had simply
    vanished — which reads exactly like "clean".
    """
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    same = pipeline._row_cells(sheet, spec, spec.row_by_key("idle-se"))
    flat = sheet.copy()
    for key in ("idle-s", "idle-se", "idle-e"):
        row = spec.row_by_key(key)
        for column, cell in enumerate(same[: row.frames]):
            flat.paste(cell, (column * spec.frame_w, row.index * spec.frame_h))

    found = pipeline.detect_mirrored_art(spec, flat)

    assert any(
        entry["rows"] == ["idle-se"] and "measure zero" in entry["reason"]
        for entry in found["unjudged"]
    ), found["unjudged"]
    assert "idle-se" not in gains_by_row(found, "rotation")


def test_every_row_is_named_by_the_pass_that_could_or_could_not_judge_it():
    """The accounting rule, asserted rather than described.

    A row may be judged by one pass and unjudged by the other — a 4-way sheet's
    `e` row is judged in its rotation and unjudged across states, because there
    is only one state — but no row may be missing from both, and none may be both
    judged and unjudged under the SAME basis.
    """
    for name in ("handedness_8way.webp", "handedness_4way.webp"):
        spec, _sheet = load_fixture_sheet(name)
        found = fixture_findings(name)
        named = {entry["row"] for entry in found["judged"]} | {
            row for entry in found["unjudged"] for row in entry["rows"]
        }
        assert named == {row.key for row in spec.rows()}, name
        for basis in ("rotation", "states"):
            judged = {e["row"] for e in found["judged"] if e["basis"] == basis}
            gave_up = {
                row
                for entry in found["unjudged"]
                if entry["basis"] == basis
                for row in entry["rows"]
            }
            assert judged.isdisjoint(gave_up), (name, basis)


def test_an_operator_can_accept_one_named_row_and_the_rest_still_refuse():
    """The override, and why it is per ROW and never blanket.

    The gate separates its populations by about 2.5x on the two characters
    anyone has measured, and registration BOUNDS the placement blindness rather
    than removing it. A measurement that good is a strong signal, not a proof —
    and a refusal with no way past it made the wrong one permanent for that draft,
    because `compose` has no other door. So an operator who has LOOKED at a row
    may accept that row; the acceptance is recorded, the refusal text survives as
    a warning, and every other flagged row still refuses.
    """
    mirrored = (("flip", "walk-e"),)

    partial = variant_validation(
        EIGHT_WAY_FIXTURE, mirrored, ("idle-ne:rotation+states",)
    )
    assert not partial["ok"]
    assert [error for error in partial["errors"] if "walk-e" in error]
    assert [entry["row"] for entry in partial["handedness"]["accepted"]] == ["idle-ne"]
    assert any("accepted by the operator" in text for text in partial["warnings"])

    both = variant_validation(
        EIGHT_WAY_FIXTURE,
        mirrored,
        ("idle-ne:rotation+states", "walk-e:rotation+states"),
    )
    assert both["ok"], both["errors"]
    # Recorded as {row, gain, basis}: accepting a +22% finding and an +8.1% one
    # used to be indistinguishable the moment the compose was over.
    assert [entry["row"] for entry in both["handedness"]["accepted"]] == [
        "idle-ne",
        "walk-e",
    ]
    assert all(
        entry["basis"] == "rotation and states"
        and entry["gain"] >= pipeline.MIRROR_GAIN_THRESHOLD
        for entry in both["handedness"]["accepted"]
    )
    # Accepted, not erased: the refusal text is still in the payload, verbatim.
    assert len(both["warnings"]) == 2
    assert all("looks drawn as the MIRROR of" in text for text in both["warnings"])


def test_an_acceptance_must_name_the_bases_it_is_waiving():
    """One row key used to waive TWO independent bodies of evidence.

    A row both passes agree about merges into ONE finding with
    `basis: "rotation and states"`, and `--accept-handedness idle-ne` waived the
    whole thing — so an operator accepting it for a PLACEMENT reason (a prop, a
    framing drift, the thing registration bounds rather than removes) also
    silenced the cross-state reading, which placement cannot explain. The bare
    row name is now refused, and the refusal spells the token it wants.
    """
    bare = fixture_validation(EIGHT_WAY_FIXTURE, ("idle-ne",))

    assert not bare["ok"]
    assert bare["handedness"]["accepted"] == []
    assert any("with no basis" in error for error in bare["errors"])
    assert any(
        f"--accept-handedness idle-ne:{TWO_BASIS_TOKEN}" in error
        for error in bare["errors"]
    )

    named = fixture_validation(
        EIGHT_WAY_FIXTURE, (f"idle-ne:{TWO_BASIS_TOKEN}",)
    )
    assert named["ok"], named["errors"]


@pytest.mark.parametrize(
    ("accepted", "fragment"),
    [
        ("walk-e:rotation+states", "was not flagged"),
        ("idle-nw:rotation+states", "not a row of this sheet"),
        ("idle-ne:rotation", "is spelled idle-ne:rotation+states"),
        ("idle-ne:states", "is spelled idle-ne:rotation+states"),
    ],
)
def test_accepting_a_row_with_nothing_to_accept_is_itself_a_refusal(accepted, fragment):
    """An acceptance that accepts nothing is a bypass waiting for a refusal.

    `--accept-handedness idle-e` carried along in a shell history because it once
    worked would silently disarm the check the day that row is genuinely wrong.
    A basis that does not match the finding is the same defect wearing the new
    grammar, so it is refused with the spelling rather than honoured loosely.
    """
    result = fixture_validation(EIGHT_WAY_FIXTURE, (accepted,))

    assert not result["ok"]
    assert any(fragment in error for error in result["errors"])
    assert result["handedness"]["accepted"] == []


def test_a_warning_cannot_be_accepted_because_it_never_blocked():
    """The override is for refusals. There is nothing to accept about a warning.

    Left acceptable, `--accept-handedness` would quietly become the way an agent
    makes a warning it did not read go away — and the warning is now the only
    thing standing between a single-basis reading and a shipped mirrored row.
    """
    warned = pipeline.validate_sheet(
        EIGHT,
        eight_way_sheet(mirror=("walk-ne",)),
        accept_handedness=[f"walk-ne:{TWO_BASIS_TOKEN}"],
    )

    assert not warned["ok"]
    assert warned["handedness"]["accepted"] == []
    assert any("is a WARNING" in error for error in warned["errors"])


def strips_of_real_art(tmp_path, state_name, *, prop_row=None, prop=0):
    """One state's real cells, laid back out as row STRIPS on the chroma field.

    The charsheet fixtures are pre-composed SHEETS, so nothing in this package
    exercises the composition path on real art — which is the path
    `normalize_cells` lives on. This puts the art back into the shape `compose`
    starts from: a magenta strip per row, gutters between the frames, ready for
    `extract_strip_frames`.

    *prop* paints a bar that many pixels wide off ONE side of *prop_row*'s art —
    a bag, a cape, a sheathed sword. It is the reachable driver of a
    displacement, because `normalize_cells` centres each row's union BOX and a
    one-sided prop moves the body by half its width.
    """
    spec, source = variant(EIGHT_WAY_FIXTURE, REPAIRED)
    one = SheetSpec(
        states=(StateSpec(state_name, 3, True),),
        scheme=spec.scheme,
        frame_w=spec.frame_w,
        frame_h=spec.frame_h,
    )
    gutter = 40
    strips = {}
    for row in one.authored_rows():
        cells = pipeline._row_cells(source, spec, spec.row_by_key(row.key))
        strip = Image.new(
            "RGBA",
            (
                sum(cell.width for cell in cells) + gutter * (len(cells) + 1),
                cells[0].height + 2 * gutter,
            ),
            MAGENTA,
        )
        left = gutter
        for cell in cells:
            art = cell.copy()
            if prop and row.key == prop_row:
                box = art.getbbox()
                edge = max(0, box[0] - prop)
                ImageDraw.Draw(art).rectangle(
                    [edge, box[1] + 40, edge + prop, box[1] + 80],
                    fill=(90, 60, 30, 255),
                )
            strip.alpha_composite(art, (left, gutter))
            left += cell.width + gutter
        path = tmp_path / f"{row.key}.png"
        strip.save(path, format="PNG")
        strips[row.key] = path
    references = []
    for direction in spec.scheme.authored:
        cell = pipeline._row_cells(source, spec, spec.row_by_key(f"idle-{direction}"))[0]
        framed = Image.new("RGBA", (cell.width + 40, cell.height + 40), MAGENTA)
        framed.alpha_composite(cell, (20, 20))
        path = tmp_path / f"reference-{direction}.png"
        framed.save(path, format="PNG")
        references.append(path)
    return one, strips, references


def worst_registered_shift(spec, sheet):
    """The largest shift ``_registered_distance`` chooses over adjacent seams."""
    window = pipeline.registration_window(spec.frame_w)
    rows = {row.key: row for row in spec.rows()}
    worst = 0
    for state in spec.states:
        chain = [
            rows[pipeline.row_key(state.name, direction)]
            for direction in pipeline.turnaround_order(spec.scheme.authored)
        ]
        for left, right in zip(chain, chain[1:]):
            left_cells = pipeline._row_cells(sheet, spec, left)
            right_cells = pipeline._row_cells(sheet, spec, right)
            for index in range(min(len(left_cells), len(right_cells))):
                _distance, shift = pipeline._registered_distance(
                    left_cells[index], right_cells[index], window
                )
                worst = max(worst, abs(shift))
    return worst


def test_normalize_cells_still_registers_every_state_on_the_cell_s_centre():
    """The load-bearing UPSTREAM dependency, pinned where it is depended ON.

    `detect_mirrored_art` reads PLACEMENT as handedness — an 8 px horizontal
    shift of one correct row moves it from -38.2% to +9.4% and refuses the
    install — and what holds that at bay is `atlas.normalize_cells` centring
    each state on its union box. That is pet code this package imports and does
    not own.

    The composition test below CANNOT see it go, measured rather than assumed:
    `extract_strip_frames` content-crops each frame per slot BEFORE the centring
    runs, so replacing `normalize_cells` with a fit that centres nothing leaves
    every composed shift at 0. What that test does see is per-row DRIFT (red at
    6 px, green at 4). The centring itself needs a pin against the function, and
    it belongs in this file because the charsheet is what the removal would
    break — `tests/agent/test_pet_generate.py` is opt-in (`HERMES_RUN_SLOW_PET_TESTS`)
    and would not run in the sweep that deleted it.

    Two states drawn hard against opposite edges of their own canvases land
    centred in the same 192 px cell. Delete the horizontal centring
    (`px = round((CELL_WIDTH - sw) / 2)` in `normalize_cells`) and they land
    where their sources put them, tens of pixels apart.
    """
    from agent.pet.generate import atlas

    left_hugging = Image.new("RGBA", (160, 180), (0, 0, 0, 0))
    right_hugging = Image.new("RGBA", (160, 180), (0, 0, 0, 0))
    ImageDraw.Draw(left_hugging).rectangle((0, 40, 60, 170), fill=(80, 120, 220, 255))
    ImageDraw.Draw(right_hugging).rectangle((100, 40, 159, 170), fill=(220, 120, 80, 255))

    normalized = atlas.normalize_cells({"idle": [left_hugging], "walk": [right_hugging]})

    centres = {}
    for state, cells in normalized.items():
        box = cells[0].getbbox()
        assert box is not None, state
        left_margin, right_margin = box[0], atlas.CELL_WIDTH - box[2]
        assert abs(left_margin - right_margin) <= 1, (
            f"{state} is not centred in its cell: {left_margin} left, {right_margin} right"
        )
        centres[state] = (box[0] + box[2]) / 2

    assert abs(centres["idle"] - centres["walk"]) <= 1, (
        "two states drawn at opposite edges landed apart: the registration is gone"
    )


def test_composing_real_art_registers_every_row_inside_the_shift_window(tmp_path):
    """The composition path's shift budget, on real art, with its headroom.

    The charsheet fixtures are pre-composed SHEETS, so nothing here exercises
    the path a live `compose` takes — and the handedness gate's registration
    window is sized against how far that path leaves neighbouring rows apart. If
    the composition ever starts placing them further apart, the gate does not
    fail; it starts REFUSING correct characters, silently.

    Measured through the real path: the worst shift the gate picks on correct
    art is **9 px of a 16 px window**, so ordinary art already eats 56% of it.
    The propped control is what keeps the assertion from passing vacuously — a
    32 px one-sided bag on ONE row pushes the registration flat against the
    window edge, which is the boundary the first number has to stay inside.

    **What this does NOT detect, measured rather than assumed.** The field-note
    proposal this came from called it a pin on `normalize_cells`, the upstream
    pet code that centres each row on its union bbox. It is not. Replacing
    `normalize_cells` with a fit that centres nothing leaves every shift at 0,
    because `extract_strip_frames` content-crops each frame per slot BEFORE the
    centring runs — on this path extraction, not centring, is what puts rows in
    comparable positions. What this test does see is a per-row DRIFT in wherever
    the composition lands rows: measured red at 6 px and green at 4 px, i.e. a
    detection floor of ~5 px on top of the 9 px already spent. That is the same
    ~6 px of headroom the window has, stated as a test rather than as a note.
    The centring itself is pinned directly, one test up
    (`test_normalize_cells_still_registers_every_state_on_the_cell_s_centre`).
    """
    spec, strips, references = strips_of_real_art(tmp_path, "jump")
    composed = pipeline.compose_sheet(
        spec, pipeline.compose_draft_frames(spec, strips, references)
    )
    window = pipeline.registration_window(spec.frame_w)

    worst = worst_registered_shift(spec, composed)
    assert 0 < worst < window, worst

    propped_dir = tmp_path / "propped"
    propped_dir.mkdir()
    spec, strips, references = strips_of_real_art(
        propped_dir, "jump", prop_row="jump-e", prop=32
    )
    propped = pipeline.compose_sheet(
        spec, pipeline.compose_draft_frames(spec, strips, references)
    )

    assert worst_registered_shift(spec, propped) == window


def test_a_cross_state_pair_that_measures_nothing_is_named_not_dropped():
    """The states pass kept a silent `continue`, one loop over from the fix.

    `if len(ranked) < majority: continue` dropped a row from `flagged` AND from
    `unjudged` — the exact class the `as_drawn <= 0` repair retired in the
    rotation pass, surviving in the other one. Here `idle-e` is made
    byte-identical to `walk-e`, so that pair measures zero and drops out of the
    ranking, leaving each of them one usable pair where a conviction needs two.
    """
    spec, sheet = variant(EIGHT_WAY_FIXTURE, REPAIRED)
    same = pipeline._row_cells(sheet, spec, spec.row_by_key("walk-e"))
    twinned = sheet.copy()
    row = spec.row_by_key("idle-e")
    for column, cell in enumerate(same[: row.frames]):
        twinned.paste(cell, (column * spec.frame_w, row.index * spec.frame_h))

    found = pipeline.detect_mirrored_art(spec, twinned)

    states = gains_by_row(found, "states")
    assert "idle-e" not in states and "walk-e" not in states
    named = [
        entry
        for entry in found["unjudged"]
        if entry["basis"] == "states" and "measure anything" in entry["reason"]
    ]
    assert {row for entry in named for row in entry["rows"]} == {"idle-e", "walk-e"}
    # Still accounted for, which is the whole rule: no row vanishes from both.
    assert "idle-e" in unjudged_rows(found, "states")


def test_the_handedness_accounting_is_a_line_an_operator_can_read():
    """`handedness.unjudged` reached nobody: `compose` printed WxH and raised.

    On a clean sheet the operator saw `composed → 1536x3120` and never learned
    that six of fifteen rows were never judged; on a refusal the payload carrying
    that list was discarded exactly when it mattered.
    """
    handedness = fixture_validation(EIGHT_WAY_FIXTURE)["handedness"]

    line = pipeline.handedness_summary(handedness)

    assert line.startswith("handedness: 9 row(s) judged, 1 refused, 6 unjudged (")
    for end in ("idle-n", "idle-s", "jump-n", "jump-s", "walk-n", "walk-s"):
        assert end in line
    # A row the rotation answered for is not reported unjudged just because the
    # cross-state pass had one state to work with.
    four = pipeline.handedness_summary(
        fixture_validation("handedness_4way.webp")["handedness"]
    )
    assert four == "handedness: 1 row(s) judged, 2 unjudged (idle-n, idle-s)"

    # A refused row and a WARNED one are different facts and are counted apart —
    # a warning does not block, so the line has to name the rows it is about or
    # nobody will look at them.
    warned = pipeline.handedness_summary(
        pipeline.validate_sheet(EIGHT, eight_way_sheet(mirror=("walk-ne",)))[
            "handedness"
        ]
    )
    assert "1 warned (walk-ne)" in warned
    assert "refused" not in warned

    accepted = pipeline.handedness_summary(
        fixture_validation(
            EIGHT_WAY_FIXTURE, (f"idle-ne:{TWO_BASIS_TOKEN}",)
        )["handedness"]
    )
    assert "1 accepted by the operator (idle-ne)" in accepted
    assert "refused" not in accepted

# ───────────── the refusal as a MESSAGE: is the door findable, and does it fit ─────────────
#
# Everything below was written after 2026-08-26's fix and against real output,
# not against the code. The two defects it pins were both found by running
# `characters compose` on real mirrored art rather than by reading `pipeline.py`:
# the refusal never mentioned `--accept-handedness` on the shape an operator
# actually hits, and the whole diagnostic was one 1206-character line that a
# console card cannot render.

# Every spelling `--accept-handedness` accepts, taken from the code. A literal
# list here would be the third copy of the map `accept_basis_token` exists to be
# the only one of.
BLOCKING_TOKEN = {
    basis: pipeline.accept_basis_token(basis)
    for basis in ("rotation and states", "states")
}

WHOLE_STATE_MIRRORED = REPAIRED + (
    ("flip", "jump-s", "jump-se", "jump-e", "jump-ne", "jump-n"),
)


def tokens_offered(errors):
    """Every ``--accept-handedness <token>`` an error text hands the operator."""
    return re.findall(r"--accept-handedness (\S+?)[.\s]", "\n".join(errors) + "\n")


@pytest.mark.parametrize(
    ("ops", "refused", "basis"),
    [
        ((), ("idle-ne",), "rotation and states"),
        (WHOLE_STATE_MIRRORED, ("jump-se", "jump-e", "jump-ne"), "states"),
    ],
    ids=["two-basis", "whole-state"],
)
def test_a_refusal_hands_over_a_token_that_actually_reopens_the_compose(
    ops, refused, basis
):
    """The escape hatch has to be findable FROM the refusal, and it was not.

    Run against real art 2026-08-26, the two-basis refusal — the shape a
    mirrored authored row produces, and the one the founding `ne` defect hit —
    named `characters reroll-row` and nothing else. `--accept-handedness` was
    reachable only by guessing the flag existed; the whole-state branch spelled
    it and this one did not, so what shipped was not a policy of not advertising
    an override but a DRIFT between two spellings of the same thing. Ruling 18
    tightens this gate *because* the row-named override is reachable, and an
    operator who has LOOKED at the strip and believes the art was being sent to
    the one remedy that destroys it: a re-roll auto-approves, there is no
    approve-row verb, and the message says so itself.

    This is a ROUND TRIP on purpose, not a grep for the flag name. It takes the
    tokens out of the printed refusal, feeds exactly those back to
    `validate_sheet`, and requires the install to go through — so it fails both
    ways a message can lie: by not offering a token at all, and by offering one
    the validator then rejects (which is what a second, hardcoded spelling of
    the token would produce the moment a new basis can block).
    """
    spec, sheet = variant(EIGHT_WAY_FIXTURE, ops)
    refusal = pipeline.validate_sheet(spec, sheet)

    assert not refusal["ok"]
    blocking = [
        finding
        for finding in refusal["handedness"]["flagged"]
        if finding["severity"] == "error"
    ]
    assert sorted(finding["row"] for finding in blocking) == sorted(refused)
    assert {finding["basis"] for finding in blocking} == {basis}

    offered = tokens_offered(refusal["errors"])
    assert sorted(offered) == sorted(f"{row}:{BLOCKING_TOKEN[basis]}" for row in refused)

    reopened = pipeline.validate_sheet(spec, sheet, accept_handedness=offered)
    assert reopened["ok"], reopened["errors"]
    assert sorted(
        entry["row"] for entry in reopened["handedness"]["accepted"]
    ) == sorted(refused)


def test_the_override_is_offered_with_its_price_and_never_before_the_re_roll():
    """Shown, but not as the cheap door — the half of the ruling that is taste.

    Not advertising an override that lets bad art ship is a defensible design;
    what is not defensible is advertising it on one blocking shape and not the
    other. Having chosen to show it, the text has to carry why it is the more
    expensive door: a re-roll is private and costs art, while an acceptance
    becomes a permanent public fact about the character — `{row, gain, basis}`
    on the installed manifest, republished by `characters list`, by
    `sprite_payload` and by the launcher's bundle warnings (decision 19).

    Order is part of the meaning, so it is asserted: the remedy comes first and
    the waiver after it, conditional on having LOOKED.
    """
    finding = fixture_findings(EIGHT_WAY_FIXTURE)["flagged"][0]

    lines = pipeline.mirrored_art_error(finding).split("\n")
    labels = [line.split(":")[0].strip() for line in lines[1:]]
    assert labels.index("re-roll") < labels.index("accept")

    accept = next(line for line in lines if line.strip().startswith("accept:"))
    assert "only if you have LOOKED" in accept
    assert "not the cheaper one" in accept

    record = next(line for line in lines if line.strip().startswith("on record:"))
    assert "handednessAccepted" in record
    for republisher in ("characters list", "sprite_payload", "bundle warnings"):
        assert republisher in record


def test_a_refusal_says_a_re_roll_cannot_be_taken_back_even_with_nobody_to_blame():
    """The cost of obeying was on the corroborating tail, which is optional.

    `_REROLL_IS_ONE_WAY` rode only on "and do NOT re-roll these neighbours",
    so a refusal about an ISOLATED row — which is exactly the founding `ne`
    defect, one flagged row per state with clean neighbours — told the operator
    to spend approved art and never told them the spending was one-way. The
    checked-in fixture IS that shape, and the empty `corroborating` is asserted
    so this cannot pass for the wrong reason.
    """
    finding = fixture_findings(EIGHT_WAY_FIXTURE)["flagged"][0]
    assert finding["corroborating"] == [], "the premise: nobody else to warn about"

    message = pipeline.mirrored_art_error(finding)

    assert "characters reroll-row --row idle-ne" in message
    assert "auto-approves" in message
    assert "no approve-row verb to undo it" in message
    assert "spends correct approved art" in message


@pytest.mark.parametrize(
    ("ops", "row", "named"),
    [
        ((), "idle-ne", "idle-ne"),
        (WHOLE_STATE_MIRRORED, "jump-e", "jump-e"),
        (REPAIRED + (("flip", "walk-ne"),), "walk-ne", "walk-ne"),
        # The unattributed headline names the STATE and nobody in it, which is
        # the whole point of that shape: the rotation cannot say which row of a
        # run started it, so the ranking rides on a field and not the headline.
        (REPAIRED + (("slide", "walk-e", -24),), "walk-ne", "walk"),
    ],
    ids=["two-basis", "whole-state", "one-basis-warning", "unattributed"],
)
def test_the_diagnostic_is_labelled_lines_and_not_one_paragraph(ops, row, named):
    """All four shapes, and the property both consumers need.

    Measured on the live sheet 2026-08-26, before this change: the refusal was
    ONE line of 1206 characters, and 1519 when a malformed acceptance made the
    validator restate the same finding underneath the acceptance error. A
    terminal survives that by accident; the launcher console card that has to
    render exactly this text (slice B2) cannot.

    What is pinned is the shape rather than the prose: a headline that stands
    alone, then one `label: value` line per fact, labels unique inside a block,
    and no single line anywhere near the 1206 it used to be. Nothing here
    asserts a wrap width — the module must not choose one, because a column
    count picked here is wrong for every consumer that is not an 80-column
    terminal.
    """
    found = variant_findings(EIGHT_WAY_FIXTURE, ops)
    finding = next(entry for entry in found["flagged"] if entry["row"] == row)

    lines = pipeline.mirrored_art_error(finding).split("\n")

    assert len(lines) >= 6
    headline, fields = lines[0], lines[1:]
    assert not headline.startswith(" ") and named in headline
    assert headline.endswith("REFUSED") or "does not block" in headline

    labels = []
    for line in fields:
        label, separator, value = line.partition(":")
        assert separator == ":", line
        assert label.startswith("  ") and label.strip(), line
        assert value.strip(), line
        labels.append(label.strip())
    assert len(labels) == len(set(labels)), labels
    assert max(len(line) for line in lines) < 400


def test_a_refusal_keeps_every_fact_it_carried_as_one_paragraph():
    """The shape changed; the content did not, and that is the risk here.

    A shorter message that dropped the warning about spending correct approved
    art would be a regression wearing an improvement's clothes. This is the
    inventory, on the one shape that carries all of it at once — a named
    culprit with a neighbour riding along.
    """
    found = variant_findings(EIGHT_WAY_FIXTURE, REPAIRED + (("flip", "idle-se", "idle-ne"),))
    finding = next(entry for entry in found["flagged"] if entry["row"] == "idle-ne")
    assert finding["corroborating"], "the premise: a neighbour rode along"

    message = pipeline.mirrored_art_error(finding)

    # The per-neighbour before/after, every seam, both numbers.
    for seam in finding["seams"]:
        assert f"{seam['distance']:.2f}" in message
        assert f"{seam['mirroredDistance']:.2f}" in message
        assert seam["with"] in message
    assert f"{finding['gain'] * 100:.0f}% better" in message
    # The neighbour is not a second fault, and must not be re-rolled.
    assert "NOT separate faults" in message
    assert "Do NOT re-roll them" in message
    # What obeying a wrong name costs.
    assert "spends correct approved art" in message
    # What a mirrored row does downstream.
    assert "corrupts the derived direction" in message


def test_a_malformed_acceptance_does_not_reprint_the_whole_diagnostic():
    """One row is one block — it used to be the complaint plus a second copy.

    Typing `--accept-handedness idle-ne` (the spelling a shell history keeps)
    produced TWO entries in `errors` about one finding: the acceptance
    complaint, and then the entire refusal again underneath it. Measured on real
    art 2026-08-26 at 1519 characters against the plain refusal's 1206 — so the
    message for getting the flag wrong was LONGER than the one that taught the
    flag, and 79% of it was text the operator had just read.
    """
    plain = fixture_validation(EIGHT_WAY_FIXTURE)
    bare = fixture_validation(EIGHT_WAY_FIXTURE, ("idle-ne",))

    assert not bare["ok"] and bare["handedness"]["accepted"] == []
    about_the_row = [error for error in bare["errors"] if "idle-ne" in error]
    assert len(about_the_row) == 1, about_the_row
    (folded,) = about_the_row

    # The complaint is INSIDE the finding's block, not beside it: same string,
    # and the evidence lines are still there with it.
    assert "with no basis" in folded
    assert "looks drawn as the MIRROR of 'ne'" in folded
    assert f"--accept-handedness idle-ne:{TWO_BASIS_TOKEN}" in folded
    assert "you typed" in folded

    # And it grew by the complaint, not by a copy of the refusal.
    (was,) = plain["errors"]
    assert len(folded) < len(was) + len(was) // 2
