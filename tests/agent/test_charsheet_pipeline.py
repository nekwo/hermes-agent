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

import pytest

from agent.charsheet import palette as palette_mod
from agent.charsheet import pipeline
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
