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


# ────────────────────────── frame-cell geometry ──────────────────────────


@pytest.mark.parametrize("width, frames", [(768, 3), (770, 3), (2172, 8), (100, 7), (13, 13)])
def test_a_frame_cell_is_the_frames_own_slot_and_the_slots_tile_the_strip(width, frames):
    """WHERE the crop is taken, not just how wide it comes out.

    A 2026-08-24 mutation audit shifted this window by +3px on BOTH bounds and
    the whole suite stayed green: every assertion in it read the cell's SIZE,
    which a shift does not change, so the verb could have been showing an
    operator the wrong slice of the strip while reporting the right dimensions.
    A defect is hunted frame by frame — a cell that is three pixels off cuts
    through the neighbour it was supposed to exclude.

    Each column carries its own x in its pixels, so a cell's contents name the
    columns it came from. The boundaries are the strip's own rounding: the slots
    must tile it with no gap, no overlap and no dropped column, which is what
    makes `round()` on both bounds the contract rather than an implementation
    detail (widths 770/100/13 are the cases where the division does not divide).
    """
    height = 24
    row = b"".join(bytes((x % 256, x // 256, 40, 255)) for x in range(width))
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


def test_a_row_drawn_as_the_mirror_of_its_direction_blocks_the_install():
    """The whole point: this refuses, it does not warn.

    A warning is the shape the failure already had — the defective sheet passed
    every check there was, composed, installed and shipped.
    """
    result = pipeline.validate_sheet(EIGHT, eight_way_sheet(mirror=("walk-ne",)))

    assert not result["ok"]
    assert [finding["row"] for finding in result["handedness"]["flagged"]] == ["walk-ne"]
    assert len(result["errors"]) == 1
    message = result["errors"][0]
    assert "walk-ne" in message
    # The refusal has to be actionable at the verb an operator actually has.
    assert "characters reroll-row --row walk-ne" in message
    assert result["warnings"] == []


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


@functools.lru_cache(maxsize=None)
def fixture_findings(name: str):
    """:func:`detect_mirrored_art` on an UNMODIFIED fixture, computed once."""
    spec, sheet = load_fixture_sheet(name)
    return pipeline.detect_mirrored_art(spec, sheet)


@functools.lru_cache(maxsize=None)
def fixture_validation(name: str, accepted: tuple[str, ...] = ()):
    """:func:`validate_sheet` on an UNMODIFIED fixture, computed once."""
    spec, sheet = load_fixture_sheet(name)
    return pipeline.validate_sheet(spec, sheet, accept_handedness=accepted)


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
    spec, sheet = load_fixture_sheet("handedness_8way.webp")

    refused = fixture_validation("handedness_8way.webp")
    assert not refused["ok"]
    assert [finding["row"] for finding in refused["handedness"]["flagged"]] == ["idle-ne"]

    repaired = pipeline.validate_sheet(spec, flip_rows(spec, sheet, "idle-ne"))
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


def test_the_threshold_sits_between_the_two_measured_populations():
    """What the number 0.08 is FOR, asserted as the ordering it encodes.

    The threshold had no test at all: the suite was green from ~0.03 to at least
    0.25, because the glyph fixture's true positives scored an order above the
    live discrimination band, so nothing noticed a threshold raised past the very
    defect the check shipped for. This pins the property instead of the value —
    every correct row of a real sheet must fall BELOW the line and the real
    mirrored row must fall above it — so moving the number in either direction
    reddens exactly when it stops separating the populations.

    Measured on this fixture: the loudest false is `idle-e` at +5.03% (a correct
    row, pulled up because the row beside it is mirrored) and the only true
    positive is `idle-ne` at +12.05% in the rotation and +15.40% across states.
    """
    found = fixture_findings("handedness_8way.webp")

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
        pipeline.detect_mirrored_art(spec, slid)["flagged"]
        == fixture_findings("handedness_8way.webp")["flagged"]
    )


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
    left = pipeline._row_cells(sheet, spec, spec.row_by_key("walk-e"))[0]
    right = pipeline._row_cells(sheet, spec, spec.row_by_key("walk-ne"))[0]
    flip = Image.FLIP_LEFT_RIGHT

    assert pipeline._registered_distance(left, right, window)[0] == pytest.approx(
        pipeline._registered_distance(
            left.transpose(flip), right.transpose(flip), window
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
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    found = pipeline.detect_mirrored_art(spec, flip_rows(spec, sheet, "walk-e"))

    walk = [finding for finding in found["flagged"] if finding["state"] == "walk"]
    assert [finding["row"] for finding in walk] == ["walk-e"]
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


def test_a_whole_state_drawn_mirrored_is_caught_across_the_states():
    """The blind spot `add_state` makes reachable, and the pass that closes it.

    The rotation pass is a fixed point per STATE: mirror every row of one state
    and the chain still fits itself perfectly, which matters because `add_state`
    generates all of a new state's rows in one batch against one reference and
    one prompt — the same shape as the generation that drew `ne` backwards three
    times. Comparing the same direction ACROSS states is what sees it, and both
    halves are asserted here: the rotation finds nothing in `jump`, the states
    pass convicts all three of its judged rows.
    """
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    correct = flip_rows(spec, sheet, "idle-ne")
    mirrored_state = flip_rows(
        spec, correct, *(f"jump-{direction}" for direction in ("s", "se", "e", "ne", "n"))
    )

    found = pipeline.detect_mirrored_art(spec, mirrored_state)

    rotation = gains_by_row(found, "rotation")
    assert all(
        rotation[f"jump-{direction}"] < pipeline.MIRROR_GAIN_THRESHOLD
        for direction in ("se", "e", "ne")
    ), rotation
    assert sorted(
        (finding["row"], finding["basis"]) for finding in found["flagged"]
    ) == [("jump-e", "states"), ("jump-ne", "states"), ("jump-se", "states")]


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
    found = pipeline.detect_mirrored_art(spec, blank_row(spec, sheet, "walk-e"))

    rotation_unjudged = unjudged_rows(found, "rotation")
    assert {"walk-se", "walk-e", "walk-ne"} <= rotation_unjudged
    assert set(gains_by_row(found, "rotation")) == {
        "idle-se",
        "idle-e",
        "idle-ne",
        "jump-se",
        "jump-e",
        "jump-ne",
    }
    # The defect in another state is still caught, and the blank row is named for
    # what it is rather than folded into its neighbours' reason.
    assert "idle-ne" in {finding["row"] for finding in found["flagged"]}
    assert any(
        entry["rows"] == ["walk-e"] and "empty row has no facing" in entry["reason"]
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
    spec, sheet = load_fixture_sheet("handedness_8way.webp")
    mirrored = flip_rows(spec, sheet, "walk-e")

    partial = pipeline.validate_sheet(spec, mirrored, accept_handedness=["idle-ne"])
    assert not partial["ok"]
    assert [error for error in partial["errors"] if "walk-e" in error]
    assert partial["handedness"]["accepted"] == ["idle-ne"]
    assert any("accepted by the operator" in text for text in partial["warnings"])

    both = pipeline.validate_sheet(
        spec, mirrored, accept_handedness=["idle-ne", "walk-e"]
    )
    assert both["ok"], both["errors"]
    assert both["handedness"]["accepted"] == ["idle-ne", "walk-e"]
    # Accepted, not erased: the refusal text is still in the payload, verbatim.
    assert len(both["warnings"]) == 2
    assert all("looks drawn as the MIRROR of" in text for text in both["warnings"])


@pytest.mark.parametrize(
    ("accepted", "fragment"),
    [
        ("walk-e", "was not flagged"),
        ("idle-nw", "not a row of this sheet"),
    ],
)
def test_accepting_a_row_with_nothing_to_accept_is_itself_a_refusal(accepted, fragment):
    """An acceptance that accepts nothing is a bypass waiting for a refusal.

    `--accept-handedness idle-e` carried along in a shell history because it once
    worked would silently disarm the check the day that row is genuinely wrong.
    """
    result = fixture_validation("handedness_8way.webp", (accepted,))

    assert not result["ok"]
    assert any(fragment in error for error in result["errors"])
    assert result["handedness"]["accepted"] == []


def test_the_handedness_accounting_is_a_line_an_operator_can_read():
    """`handedness.unjudged` reached nobody: `compose` printed WxH and raised.

    On a clean sheet the operator saw `composed → 1536x3120` and never learned
    that six of fifteen rows were never judged; on a refusal the payload carrying
    that list was discarded exactly when it mattered.
    """
    handedness = fixture_validation("handedness_8way.webp")["handedness"]

    line = pipeline.handedness_summary(handedness)

    assert line.startswith("handedness: 9 row(s) judged, 1 flagged, 6 unjudged (")
    for end in ("idle-n", "idle-s", "jump-n", "jump-s", "walk-n", "walk-s"):
        assert end in line
    # A row the rotation answered for is not reported unjudged just because the
    # cross-state pass had one state to work with.
    four = pipeline.handedness_summary(
        fixture_validation("handedness_4way.webp")["handedness"]
    )
    assert four == "handedness: 1 row(s) judged, 2 unjudged (idle-n, idle-s)"
