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

import re

import pytest

from agent.charsheet import palette as palette_mod
from agent.charsheet import pipeline
from agent.charsheet import prompts
from agent.charsheet.spec import CHAR8, EIGHT_WAY, FOUR_WAY, SheetSpec, StateSpec

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

    named = {row for entry in unjudged for row in entry["rows"]}
    assert named == {"idle-s", "idle-n", "walk-s", "walk-n"}
    assert all(entry["reason"] for entry in unjudged)


def test_an_end_row_is_not_blamed_for_a_seam_that_does_prefer_its_mirror():
    """The end-row exclusion, with the evidence present that would convict it.

    This is the situation that made the exclusion necessary, reproduced: the
    seam between the back view and the rear diagonal genuinely prefers the
    mirrored art — here by 25%, well past the threshold — and the back view is
    the only row touching it that has nothing to corroborate against. An earlier
    draft of the check DID judge it, and the consequence was not theoretical: it
    refused the install of `cobalt-robot-courier`, a correct character whose back
    view's single seam preferred the mirror by 11%.

    The rear diagonal, touching two seams, is absolved by the other one — which
    is the whole argument for scoring a row rather than a seam. Nothing here is
    mirrored; the badge is just an asymmetric mark, the way a satchel is.
    """
    sheet = eight_way_sheet(badge={"idle-ne": "left", "idle-n": "right"})

    chain = [EIGHT.row_by_key(f"idle-{d}") for d in ("s", "se", "e", "ne", "n")]
    cells_by_key = {row.key: pipeline._row_cells(sheet, EIGHT, row) for row in chain}
    direct, flipped = pipeline._seam_distance(
        cells_by_key["idle-ne"], cells_by_key["idle-n"]
    )
    # The evidence a single-seam rule would have convicted `idle-n` on.
    assert (direct - flipped) / direct > pipeline.MIRROR_GAIN_THRESHOLD

    found = pipeline.detect_mirrored_art(EIGHT, sheet)

    assert [finding["row"] for finding in found["flagged"]] == []
    assert "idle-n" in {row for entry in found["unjudged"] for row in entry["rows"]}


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
    sheet can catch it — the sheet is self-consistent, and only the world
    outside it says which way is east. If this test ever turns red, the check
    grew an outside reference and this section's claims need rewriting.
    """
    every_row = tuple(row.key for row in EIGHT.authored_rows())
    upright = eight_way_sheet(badge={"idle-ne": "left", "idle-n": "right"})
    flipped = eight_way_sheet(
        mirror=every_row, badge={"idle-ne": "left", "idle-n": "right"}
    )

    # A genuinely different picture — the badge makes sure of it, so this is not
    # two names for the same bytes.
    assert flipped.tobytes() != upright.tobytes()
    assert pipeline.validate_sheet(EIGHT, flipped)["ok"]
    assert pipeline.detect_mirrored_art(EIGHT, flipped)["flagged"] == []

    # And the reason, asserted rather than described: every seam measures
    # EXACTLY the same on both — BOTH of its distances, not swapped but
    # unchanged, because distance(flip a, flip b) == distance(a, b) and
    # distance(flip(flip a), flip b) == distance(flip a, b).
    chain = [EIGHT.row_by_key(f"idle-{d}") for d in ("s", "se", "e", "ne", "n")]
    for left, right in zip(chain, chain[1:]):
        before = pipeline._seam_distance(
            pipeline._row_cells(upright, EIGHT, left),
            pipeline._row_cells(upright, EIGHT, right),
        )
        after = pipeline._seam_distance(
            pipeline._row_cells(flipped, EIGHT, left),
            pipeline._row_cells(flipped, EIGHT, right),
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
