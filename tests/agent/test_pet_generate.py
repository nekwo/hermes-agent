"""Tests for pet generation: deterministic atlas ops, store register, orchestration.

No network/API calls — image generation is mocked with synthetic strips so the
whole pipeline (segmentation → compose → validate → register → adopt) is
exercised hermetically.
"""

from __future__ import annotations

import logging
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_RUN_SLOW_PET_TESTS") != "1",
    reason=(
        "pet generation image-processing suite is opt-in; run with "
        "HERMES_RUN_SLOW_PET_TESTS=1 scripts/run_tests.sh tests/agent/test_pet_generate.py"
    ),
)

from agent.pet.generate import atlas

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402


def _strip(n_blobs: int, *, transparent: bool = True, bg=(0, 255, 0, 255), size=(208, 208)) -> Image.Image:
    """A horizontal strip with *n_blobs* clearly-separated colored ellipses."""
    w = size[0] * n_blobs
    h = size[1]
    base = (0, 0, 0, 0) if transparent else bg
    img = Image.new("RGBA", (w, h), base)
    draw = ImageDraw.Draw(img)
    for i in range(n_blobs):
        cx = i * size[0] + size[0] // 2
        cy = h // 2
        r = size[0] // 3
        color = (40 + i * 30 % 200, 80, 200 - i * 20 % 180, 255)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=color)
    return img


# ───────────────────────── frame extraction ─────────────────────────


def test_extract_strip_frames_transparent_returns_centered_cells():
    frames = atlas.extract_strip_frames(_strip(6), 6)
    assert len(frames) == 6
    for frame in frames:
        assert frame.size == (atlas.CELL_WIDTH, atlas.CELL_HEIGHT)
        # Background corners must be transparent.
        assert frame.getpixel((0, 0))[3] == 0
        # Something is drawn.
        assert frame.getchannel("A").getextrema()[1] > 0


# ─────────────────── severed subjects: the walk-se failure ───────────────────
#
# Live, a charsheet row died on "frame 3 contains multiple separated subjects".
# The pose was ONE connected component in its slot; it was severed into stacked
# slabs *inside* the extractor, and nothing put it back together. The boxes in
# these tests are the ones measured on the rejected artifact.


def test_merge_rejoins_a_subject_severed_into_stacked_slabs():
    # Measured on the rejected walk-se frame: near-total x-overlap, 1px and 2px
    # y-gaps. One character, three boxes.
    merged = atlas._merge_related_boxes(
        [(6, 303, 203, 426), (11, 427, 202, 450), (13, 452, 213, 581)]
    )

    assert merged == [(6, 303, 213, 581)]


def test_merge_keeps_two_real_poses_apart():
    poses = [(0, 0, 100, 200), (250, 0, 350, 200)]

    assert sorted(atlas._merge_related_boxes(poses)) == poses


def test_merge_keeps_two_grid_rows_apart():
    """The 2D-grid path depends on stacked POSES staying separate subjects.

    Rejoining slabs must not also rejoin the two visual rows a model draws when
    it ignores "one horizontal row" — that is the case ``_component_crops``
    exists to handle.
    """
    rows = [(0, 0, 200, 200), (0, 300, 200, 500)]

    assert sorted(atlas._merge_related_boxes(rows)) == rows


def test_merge_still_joins_a_prop_on_the_same_row():
    # The behaviour that was already there: a cape a few px off the body.
    merged = atlas._merge_related_boxes([(100, 50, 200, 250), (208, 80, 230, 200)])

    assert merged == [(100, 50, 230, 250)]


CHROMA = (255, 0, 255, 255)
_SEAM_SLOT = 208
_SEAM_FRAMES = 8


def _seam_severed_strip(*, seams=(-40, 20), severed=3):
    """An 8-pose chroma strip with one pose cut by thin chroma-coloured seams.

    Poses sit close enough that the strip-level horizontal merge collapses them
    into one box — that part is exactly what the live artifact did — so
    extraction falls through to the gutter path and each pose is validated as
    its own column.

    The seams themselves are this fixture's own mechanism, NOT the live one: a
    genuine seam the chroma key opens up. What actually severed the live walk-se
    pose was our own slot-scale line erase, which is fixed at the root and has
    its own tests below. A keyed seam remains possible — a provider really can
    draw one — so this stays as the merge's coverage.
    """
    width, height = _SEAM_SLOT * _SEAM_FRAMES, 800
    img = Image.new("RGBA", (width, height), CHROMA)
    draw = ImageDraw.Draw(img)
    rx, ry, cy = 95, 140, 400
    for i in range(_SEAM_FRAMES):
        cx = i * _SEAM_SLOT + _SEAM_SLOT // 2
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(60, 90, 200, 255))
        if i == severed:
            for offset in seams:
                y = cy + offset
                draw.rectangle((cx - rx, y, cx + rx, y + 1), fill=CHROMA)
    return img


def test_a_pose_severed_by_seam_lines_still_slices_into_whole_frames():
    frames = atlas.extract_strip_frames(
        _seam_severed_strip(), _SEAM_FRAMES, method="auto", fit=False
    )

    assert len(frames) == _SEAM_FRAMES
    severed = frames[3].getbbox()
    intact = frames[2].getbbox()
    assert severed is not None and intact is not None
    # The repaired frame must carry the WHOLE pose, not the tallest slab: its
    # vertical span matches an untouched neighbour's within the seam width.
    assert abs((severed[3] - severed[1]) - (intact[3] - intact[1])) <= 6


# ───────── drawn lines are a STRIP-scale fact, never a slot-scale one ─────────
#
# `_erase_long_axis_lines` deletes thin rows spanning >=85% of the image, to kill
# floors and panel dividers a model draws across a row. That test means what it
# says against a ~1700px strip. Against a ~220px single-pose crop it means
# something else entirely — a character's own shoulders, belt or outstretched
# arms span that width — and it was deleting body rows. This is the root of the
# live walk-se failure.


def _interior_empty_rows(frame):
    """Fully transparent rows strictly inside the frame's own content bbox."""
    bbox = frame.getbbox()
    if bbox is None:
        return []
    alpha = frame.getchannel("A")
    return [
        y
        for y in range(bbox[1] + 1, bbox[3] - 1)
        if alpha.crop((bbox[0], y, bbox[2], y + 1)).getbbox() is None
    ]


def _slot_crop_with_a_wide_body_row(width=221, height=400):
    """One pose, cropped to its own slot, with a bar of its own art across it.

    Already keyed, so the bar is the 3 rows the eraser would see after
    defringing: thin enough to read as a "line", and 88% of the crop wide.
    Nothing here is a drawn floor; it is all one character.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    body = round(width * 0.45)
    x0 = (width - body) // 2
    draw.rectangle((x0, 60, x0 + body, 340), fill=(60, 90, 200, 255))
    bar = round(width * 0.88)
    bx = (width - bar) // 2
    draw.rectangle((bx, 198, bx + bar, 200), fill=(60, 90, 200, 255))
    return img


def test_a_slot_crop_keeps_the_poses_own_wide_rows():
    isolated = atlas._isolate_slot_subject(_slot_crop_with_a_wide_body_row())

    assert _interior_empty_rows(isolated) == []
    # The wide row itself is still drawn, not merely bridged by something else.
    assert isolated.getchannel("A").crop((0, 198, 221, 201)).getbbox() is not None


def _strip_with_a_drawn_floor(*, floor=True):
    """Eight poses on chroma, optionally standing on one drawn ground line."""
    width, height = _SEAM_SLOT * _SEAM_FRAMES, 800
    img = Image.new("RGBA", (width, height), CHROMA)
    draw = ImageDraw.Draw(img)
    rx, ry, cy = 95, 140, 400
    for i in range(_SEAM_FRAMES):
        cx = i * _SEAM_SLOT + _SEAM_SLOT // 2
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(60, 90, 200, 255))
    if floor:
        # Spans the whole strip and touches every pose — the thing the eraser
        # was built for.
        draw.rectangle((0, 536, width, 540), fill=(90, 90, 90, 255))
    return img


def test_a_floor_line_drawn_across_the_whole_strip_stops_bridging_the_poses():
    """The floor's PURPOSE test: it must stop welding the row into one blob.

    Not "every floor pixel is gone" — the span under a pose's own feet has body
    directly above it and is kept, which is what protects a character's own
    aligned anatomy. That stub belongs to the pose it touches and separates
    nothing. What has to die is the span crossing the background BETWEEN poses.
    """
    keyed = atlas.remove_background(_strip_with_a_drawn_floor())
    assert len(atlas._component_boxes(keyed)) == 1  # one welded blob

    erased = atlas._erase_long_axis_lines(keyed)

    assert len(atlas._component_boxes(erased)) >= _SEAM_FRAMES
    # The gutter between the first two poses is clear through the floor band.
    gutter = (_SEAM_SLOT - 12, 537, _SEAM_SLOT + 12, 540)
    assert erased.getchannel("A").crop(gutter).getbbox() is None
    # The poses themselves are untouched.
    assert erased.getchannel("A").crop((0, 300, keyed.width, 500)).getbbox() is not None


def _aligned_band_strip():
    """The SE shape: every pose carries the SAME thin wide row at the SAME height.

    This is what a chin/shoulder contour does at a diagonal angle — it lands at
    one height in all eight poses and the band covers >=85% of the strip while
    being pure anatomy. Coverage cannot tell it from a drawn floor. What can:
    every column of it has body directly above and below, because it is the
    silhouette's own widest row, not something laid across the background.
    """
    width, height = _SEAM_SLOT * _SEAM_FRAMES, 800
    img = Image.new("RGBA", (width, height), CHROMA)
    draw = ImageDraw.Draw(img)
    rx, ry, cy = 95, 140, 400
    for i in range(_SEAM_FRAMES):
        cx = i * _SEAM_SLOT + _SEAM_SLOT // 2
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(60, 90, 200, 255))
        # The shoulder line: wider than the body here, same height every frame.
        draw.rectangle((cx - rx, cy - 101, cx + rx, cy - 97), fill=(60, 90, 200, 255))
    return img


def test_anatomy_aligned_across_every_pose_is_not_mistaken_for_a_floor():
    frames = atlas.extract_strip_frames(
        _aligned_band_strip(), _SEAM_FRAMES, method="auto", fit=False
    )

    assert len(frames) == _SEAM_FRAMES
    scanlines = {i: _interior_empty_rows(f) for i, f in enumerate(frames)}
    assert all(rows == [] for rows in scanlines.values()), scanlines
    heights = [f.getbbox()[3] - f.getbbox()[1] for f in frames]
    assert max(heights) - min(heights) <= 6, heights


def test_an_aligned_body_band_survives_the_eraser_and_a_floor_does_not():
    """Same width, same thinness, opposite verdicts — decided by context alone."""
    body = atlas.remove_background(_aligned_band_strip())
    floor = atlas.remove_background(_strip_with_a_drawn_floor())

    body_kept = atlas._erase_long_axis_lines(body)
    floor_cut = atlas._erase_long_axis_lines(floor)

    band = (0, 299, body.width, 303)
    before = body.getchannel("A").crop(band).getbbox()
    after = body_kept.getchannel("A").crop(band).getbbox()
    assert before is not None and after is not None
    # The anatomy band is still substantially there, not a residue.
    assert len(atlas._component_boxes(body_kept)) == len(atlas._component_boxes(body))
    # The floor, meanwhile, no longer welds the row together.
    assert len(atlas._component_boxes(floor_cut)) > len(atlas._component_boxes(floor))


def test_a_strip_with_a_drawn_floor_still_slices_into_whole_frames():
    frames = atlas.extract_strip_frames(
        _strip_with_a_drawn_floor(), _SEAM_FRAMES, method="auto", fit=False
    )

    assert len(frames) == _SEAM_FRAMES
    for frame in frames:
        assert frame.getbbox() is not None


def _wide_row_strip():
    """The live shape: every pose carries a thin wide row of its OWN art.

    Poses sit close enough that the strip-level merge collapses them, so this
    goes down the gutter path and each pose is isolated inside its own narrow
    crop — the exact place the eraser used to mistake a body row for a floor.

    What separates a body row from a floor is not how wide it is but whether the
    OTHER poses are wide at the same height. Only two frames get a bar, and each
    sits where every pose is narrow, so at strip scale the row is ~55% covered
    (no floor) while inside that pose's own slot it clears 85% and used to be
    deleted. This is the live artifact's shape: real poses differ frame to frame,
    which is why nothing was erased from the strip but plenty was from the slots.
    """
    width, height = _SEAM_SLOT * _SEAM_FRAMES, 800
    img = Image.new("RGBA", (width, height), CHROMA)
    draw = ImageDraw.Draw(img)
    rx, ry, cy = 95, 140, 400
    bars = {3: cy - 120, 6: cy + 108}
    for i in range(_SEAM_FRAMES):
        cx = i * _SEAM_SLOT + _SEAM_SLOT // 2
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(60, 90, 200, 255))
        if i in bars:
            top = bars[i]
            draw.rectangle((cx - rx, top, cx + rx, top + 4), fill=(60, 90, 200, 255))
    return img


def test_a_poses_own_wide_row_is_not_erased_into_a_scanline():
    frames = atlas.extract_strip_frames(_wide_row_strip(), _SEAM_FRAMES, method="auto", fit=False)

    assert len(frames) == _SEAM_FRAMES
    scanlines = {i: _interior_empty_rows(f) for i, f in enumerate(frames)}
    assert all(rows == [] for rows in scanlines.values()), scanlines

    # An erased row does not always leave a hole: when it cuts near one end, the
    # smaller slab is dropped as noise and the pose silently loses its head. So
    # measure the pose too — every frame must still be as tall as its unbarred
    # neighbours.
    heights = [f.getbbox()[3] - f.getbbox()[1] for f in frames]
    assert max(heights) - min(heights) <= 6, heights


# ─────────────── the auto/components leniency contract ───────────────


def _frame_with(boxes, size=(220, 600)):
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for left, top, right, bottom in boxes:
        draw.rectangle((left, top, right - 1, bottom - 1), fill=(200, 60, 60, 255))
    return img


def _multi_subject_frames():
    scattered = _frame_with([(20, 20, 200, 180), (20, 260, 200, 420), (20, 500, 200, 590)])
    plain = _frame_with([(20, 200, 200, 400)])
    return [plain, plain, scattered] + [plain] * 5


def _width_outlier_frames():
    narrow = _frame_with([(90, 200, 130, 400)])
    wide = _frame_with([(20, 200, 420, 400)], size=(440, 600))
    return [narrow] * 7 + [wide]


@pytest.mark.parametrize(
    "frames,message",
    [
        (_multi_subject_frames, "multiple separated subjects"),
        (_width_outlier_frames, "multi-pose width outlier"),
    ],
)
def test_a_soft_check_raises_strict_and_only_warns_lenient(frames, message, caplog):
    built = frames()

    with pytest.raises(ValueError, match=message):
        atlas._validate_extracted_frames(built, 8, strict=True)

    with caplog.at_level(logging.WARNING, logger=atlas.__name__):
        atlas._validate_extracted_frames(built, 8, strict=False)

    assert any(message in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("strict", [True, False])
def test_an_empty_frame_is_a_hard_error_under_both_methods(strict):
    frames = [_frame_with([(20, 200, 200, 400)])] * 7 + [_frame_with([])]

    with pytest.raises(ValueError, match="frame 7 is empty"):
        atlas._validate_extracted_frames(frames, 8, strict=strict)


@pytest.mark.parametrize("strict", [True, False])
def test_a_short_frame_count_is_a_hard_error_under_both_methods(strict):
    frames = [_frame_with([(20, 200, 200, 400)])] * 7

    with pytest.raises(ValueError, match="expected 8 frames, got 7"):
        atlas._validate_extracted_frames(frames, 8, strict=strict)


def test_a_fully_keyed_out_strip_still_raises_under_auto():
    blank = Image.new("RGBA", (_SEAM_SLOT * _SEAM_FRAMES, 800), CHROMA)

    with pytest.raises(ValueError):
        atlas.extract_strip_frames(blank, _SEAM_FRAMES, method="auto", fit=False)




def test_remove_background_defringes_antialiased_edge():
    # The contaminated antialiased ring where sprite meets backdrop survives the
    # key (it's a blend, too far from pure magenta). Defringe shaves that 1px ring:
    # the keyed silhouette comes back eroded ~1px on every side, core intact.
    img = Image.new("RGBA", (200, 200), (255, 0, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((50, 50, 149, 149), fill=(40, 200, 60, 255))  # 100x100 green
    keyed = atlas.remove_background(img)
    bbox = keyed.getbbox()
    assert bbox is not None
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    assert 96 <= w <= 99 and 96 <= h <= 99  # ~1px shaved per side
    assert keyed.getpixel((100, 100))[3] > 0  # core intact






















# ───────────────────────── atlas compose / validate ─────────────────────────


def _frames_for_all_states() -> dict[str, list]:
    out: dict[str, list] = {}
    for state, _row, count in atlas.ROW_SPECS:
        out[state] = atlas.extract_strip_frames(_strip(count), count)
    return out










def test_validate_atlas_rejects_postage_stamp_sprite():
    sheet = Image.new("RGBA", (atlas.ATLAS_WIDTH, atlas.ATLAS_HEIGHT), (0, 0, 0, 0))
    frame = Image.new("RGBA", (atlas.CELL_WIDTH, atlas.CELL_HEIGHT), (0, 0, 0, 0))
    ImageDraw.Draw(frame).rectangle((86, 174, 106, 201), fill=(220, 240, 255, 255))

    for _state, row, count in atlas.ROW_SPECS:
        for col in range(count):
            sheet.alpha_composite(frame, (col * atlas.CELL_WIDTH, row * atlas.CELL_HEIGHT))

    result = atlas.validate_atlas(sheet)

    assert not result["ok"]
    assert any("too small" in e for e in result["errors"])








def test_normalize_cells_uses_consistent_pose_scale_for_motion_rows():
    # A jump row needs a taller union crop than idle, but the pet itself should
    # not shrink just because the motion envelope is taller.
    idle = Image.new("RGBA", (160, 180), (0, 0, 0, 0))
    jump_low = Image.new("RGBA", (160, 180), (0, 0, 0, 0))
    jump_high = Image.new("RGBA", (160, 180), (0, 0, 0, 0))
    ImageDraw.Draw(idle).rectangle((50, 80, 110, 160), fill=(80, 120, 220, 255))
    ImageDraw.Draw(jump_low).rectangle((50, 80, 110, 160), fill=(220, 120, 80, 255))
    ImageDraw.Draw(jump_high).rectangle((50, 60, 110, 140), fill=(220, 120, 80, 255))

    normalized = atlas.normalize_cells({"idle": [idle], "jumping": [jump_low, jump_high]})
    idle_box = normalized["idle"][0].getbbox()
    jump_box = normalized["jumping"][0].getbbox()

    assert idle_box is not None
    assert jump_box is not None
    idle_h = idle_box[3] - idle_box[1]
    jump_h = jump_box[3] - jump_box[1]
    assert abs(idle_h - jump_h) <= 8


# ───────────────────────── store register / adopt ─────────────────────────


def test_slugify_and_unique_slug():
    from agent.pet import store

    assert store.slugify("My Cool Pet!") == "my-cool-pet"
    assert store.slugify("   ") == "pet"
    first = store.unique_slug("Robo")
    (store.pets_dir() / first).mkdir(parents=True)
    assert store.unique_slug("Robo") == "robo-2"


def test_register_local_pet_appears_and_is_adoptable():
    from agent.pet import store

    sheet = atlas.compose_atlas(_frames_for_all_states())
    pet = store.register_local_pet(sheet, slug="Sparky", display_name="Sparky", description="zappy")
    assert pet.slug == "sparky"
    assert pet.exists
    assert any(p.slug == "sparky" for p in store.installed_pets())

    # install_pet returns the on-disk pet without ever hitting the manifest.
    adopted = store.install_pet("sparky")
    assert adopted.slug == "sparky"
    assert adopted.display_name == "Sparky"


def test_register_local_pet_is_generated_and_exports_zip():
    import io
    import zipfile

    from agent.pet import store

    sheet = atlas.compose_atlas(_frames_for_all_states())
    store.register_local_pet(sheet, slug="zippy", display_name="Zippy")
    assert store.load_pet("zippy").generated is True  # createdBy=generator

    filename, data = store.export_pet("zippy")
    assert filename == "zippy.zip"
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "zippy/pet.json" in names
    assert any(n.startswith("zippy/spritesheet") for n in names)






# ───────────────────────── orchestration (mocked imagegen) ─────────────────────────




def test_generate_base_drafts_hardens_opaque_background(monkeypatch, tmp_path):
    """A provider that ignores background=transparent still yields a cutout."""
    from agent.pet.generate import imagegen, orchestrate

    def fake_generate(prompt, *, n=1, reference_images=None, provider=None, prefix="pet", aspect_ratio="square"):
        # Solid-green backdrop with a blob — i.e. the provider painted a backdrop.
        p = tmp_path / f"{prefix}_opaque.png"
        _strip(1, transparent=False, bg=(0, 255, 0, 255)).save(p)
        return [p]

    monkeypatch.setattr(imagegen, "resolve_provider", lambda **_: object())
    monkeypatch.setattr(imagegen, "generate", fake_generate)

    drafts = orchestrate.generate_base_drafts("a fox", n=1)
    assert len(drafts) == 1

    with Image.open(drafts[0]) as out:
        rgba = out.convert("RGBA")
    # The keyed backdrop is now transparent (corner pixel fully see-through).
    assert rgba.getpixel((0, 0))[3] == 0
    # The pet blob in the center is still opaque.
    assert rgba.getpixel((rgba.width // 2, rgba.height // 2))[3] > 0


def test_hatch_pet_end_to_end(monkeypatch, tmp_path):
    from agent.pet import store
    from agent.pet.generate import atlas as atlas_mod
    from agent.pet.generate import imagegen, orchestrate

    base = tmp_path / "base.png"
    _strip(1).save(base)

    def fake_generate(prompt, *, n=1, reference_images=None, provider=None, prefix="pet", aspect_ratio="square"):
        # Return a synthetic row strip; frame count is inferable from the spec.
        state = prefix.replace("pet_row_", "")
        count = atlas_mod.FRAME_COUNTS.get(state, 6)
        p = tmp_path / f"{prefix}.png"
        _strip(count).save(p)
        return [p]

    monkeypatch.setattr(imagegen, "resolve_provider", lambda **_: object())
    monkeypatch.setattr(imagegen, "generate", fake_generate)

    events: list[tuple[str, str]] = []
    result = orchestrate.hatch_pet(
        base_image=base,
        slug="mocky",
        display_name="Mocky",
        description="a test pet",
        concept="a fox",
        on_progress=lambda ev, detail: events.append((ev, detail)),
    )

    assert result.slug == "mocky"
    assert result.validation["ok"]
    assert set(result.states) == {s for s, _, _ in atlas_mod.ROW_SPECS}
    assert ("compose", "") in events
    # The pet is on disk and adoptable.
    assert store.load_pet("mocky").exists








class _FakeImgProvider:
    def __init__(self, name, available=True):
        self.name = name
        self._available = available

    def is_available(self):
        return self._available




def test_list_sprite_providers_marks_default(monkeypatch):
    """Lists only available ref-capable backends, flagging the default pick."""
    from agent.pet.generate import imagegen

    registry = {"openai": _FakeImgProvider("openai"), "nous": _FakeImgProvider("nous")}
    monkeypatch.setattr(imagegen, "_discover", lambda: None)
    monkeypatch.setattr("agent.image_gen_registry.get_active_provider", lambda: registry["openai"])
    monkeypatch.setattr("agent.image_gen_registry.get_provider", lambda name: registry.get(name))

    listed = imagegen.list_sprite_providers()
    names = {p["name"] for p in listed}
    assert names == {"openai", "nous"}
    # Every entry carries a display label (no quality note — all backends are equal).
    assert all(p["label"] for p in listed)
    assert all("note" not in p for p in listed)
    assert [p["name"] for p in listed if p["default"]] == ["openai"]
    # Listed in preference order: Nous Portal before OpenAI.
    assert [p["name"] for p in listed] == ["nous", "openai"]


def test_generate_retries_without_transparent_background(monkeypatch, tmp_path):
    """A model that rejects background=transparent still produces images."""
    from agent.pet.generate import imagegen

    saved = tmp_path / "img.png"
    _strip(1).save(saved)
    calls: list[dict] = []

    class FakeProvider:
        def generate(self, prompt, **kwargs):
            calls.append(kwargs)
            if kwargs.get("background") == "transparent":
                return {"success": False, "error": "Transparent background is not supported for this model."}
            return {"success": True, "image": str(saved)}

    sprite = imagegen.SpriteProvider(name="openai", provider=FakeProvider(), supports_references=False)

    out = imagegen.generate("a fox", n=2, provider=sprite)
    assert len(out) == 2
    # First variant probes transparent (rejected) then retries opaque; the second
    # variant skips the transparent probe entirely.
    backgrounds = [c.get("background") for c in calls]
    assert backgrounds == ["transparent", None, None]
