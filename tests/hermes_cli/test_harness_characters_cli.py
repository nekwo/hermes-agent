"""The `harness characters` bridge — the launcher's transport for character QA.

Same shape as the pets CLI tests beside this file: build the real parser, drive
``args.func`` against a temp ``HERMES_HOME``, and read stdout. The provider is
replaced at the single charsheet seam (``pipeline._generate_image``) so the whole
staged flow runs offline.

Two contracts are asserted deliberately literally, because a shipped client
parses them: every ``--json`` call writes exactly ONE JSON object, and the pets
sprite payload still carries the exact key set the Dart petdex client reads.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import pytest

from agent.charsheet import pipeline
from agent.charsheet.spec import FOUR_WAY, SheetSpec, StateSpec
from hermes_cli.harness import build_parser

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402

SPEC = SheetSpec(
    states=(StateSpec("idle", 2, True), StateSpec("walk", 2, True)),
    scheme=FOUR_WAY,
)
STATES_FLAG = "idle:2,walk:2"
DIRECTIONS_FLAG = "4"

STRIP_W, STRIP_H = 512, 192
SQUARE_PX = 384
GLYPH_PX = 44
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


def parser():
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command")
    build_parser(subs)
    return p


def one_json_object(out: str) -> dict:
    """Parse stdout as exactly one JSON object — nothing before, nothing after."""
    text = out.strip()
    payload, end = json.JSONDecoder().raw_decode(text)
    assert isinstance(payload, dict), f"expected a JSON object, got {type(payload).__name__}"
    assert text[end:].strip() == "", f"extra output after the JSON object: {text[end:]!r}"
    return payload


def run(argv, capsys):
    args = parser().parse_args(argv)
    code = args.func(args)
    return code, one_json_object(capsys.readouterr().out)


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


def strip_image(slots):
    image = Image.new("RGBA", (STRIP_W, STRIP_H), MAGENTA)
    draw = ImageDraw.Draw(image)
    width = STRIP_W / len(slots)
    for index, (direction, tick, ticks) in enumerate(slots):
        _draw_glyph(draw, int(width * (index + 0.5)), STRIP_H // 2, GLYPH_PX, direction, tick, ticks)
    return image


def square_image(direction):
    image = Image.new("RGBA", (SQUARE_PX, SQUARE_PX), MAGENTA)
    _draw_glyph(ImageDraw.Draw(image), SQUARE_PX // 2, SQUARE_PX // 2, 200, direction, 0, 1)
    return image


class FakeProvider:
    def __init__(self, spec, out_dir):
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[str] = []
        self.order = pipeline.turnaround_order(spec.scheme.authored)
        self._rows = {pipeline.row_prefix(row.key): row for row in spec.authored_rows()}
        self._views = {pipeline.view_prefix(d): d for d in spec.scheme.order}

    def __call__(self, prompt, *, reference_images, aspect_ratio, prefix, provider):
        self.calls.append(prefix)
        if prefix == pipeline.PREFIX_TURNAROUND:
            image = strip_image([(direction, 0, 1) for direction in self.order])
        elif prefix in self._views:
            image = square_image(self._views[prefix])
        elif prefix in self._rows:
            row = self._rows[prefix]
            direction = row.direction or pipeline.NON_DIRECTIONAL_VIEW
            image = strip_image([(direction, i, row.frames) for i in range(row.frames)])
        else:  # pragma: no cover - a new generation kind would need a fixture
            raise AssertionError(f"unexpected generation prefix {prefix!r}")
        path = self.out_dir / f"{prefix}-{len(self.calls)}.png"
        image.save(path, format="PNG")
        return path


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    provider = FakeProvider(SPEC, tmp_path / "generated")
    monkeypatch.setattr(pipeline, "_generate_image", provider)
    return provider


@pytest.fixture
def base_image(tmp_path):
    path = tmp_path / "src" / "base.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    square_image("s").save(path, format="PNG")
    return path


def start_draft(capsys, *extra):
    code, payload = run(
        [
            "harness", "characters", "start",
            "--concept", "an arrow knight",
            "--slug", "arrow-knight",
            "--states", STATES_FLAG,
            "--directions", DIRECTIONS_FLAG,
            *extra,
            "--json",
        ],
        capsys,
    )
    assert code == 0
    return payload["draft"]


# ───────────────────────────── parser surface ─────────────────────────────


@pytest.mark.parametrize(
    "argv, verb",
    [
        (["start", "--concept", "x"], "start"),
        (["list"], "list"),
        (["status", "--draft", "d"], "status"),
        (["base", "--draft", "d", "--image", "i.png"], "base"),
        (["turnaround", "--draft", "d"], "turnaround"),
        (["reroll-direction", "--draft", "d", "--direction", "ne"], "reroll-direction"),
        (["approve-direction", "--draft", "d", "--all"], "approve-direction"),
        (["approve-direction", "--draft", "d", "--direction", "ne"], "approve-direction"),
        (["rows", "--draft", "d"], "rows"),
        (["thumb", "--draft", "d", "--row", "walk-e"], "thumb"),
        (["reroll-row", "--draft", "d", "--row", "walk-e"], "reroll-row"),
        (["compose", "--draft", "d"], "compose"),
        (["reopen", "--draft", "d"], "reopen"),
        (["sprite", "arrow-knight"], "sprite"),
    ],
)
def test_the_parser_exposes_every_characters_verb_with_json(argv, verb):
    args = parser().parse_args(["harness", "characters", *argv, "--json"])

    assert args.command == "harness"
    assert args.harness_command == "characters"
    assert args.characters_command == verb
    assert args.json is True
    assert callable(args.func)


def test_approve_direction_will_not_take_a_direction_and_all_at_once():
    with pytest.raises(SystemExit):
        parser().parse_args(
            ["harness", "characters", "approve-direction", "--draft", "d", "--all", "--direction", "ne"]
        )


# ───────────────────────────── the whole flow ─────────────────────────────


def test_the_full_qa_flow_runs_through_the_cli(fake, base_image, capsys):
    draft_id = start_draft(capsys)

    code, based = run(
        ["harness", "characters", "base", "--draft", draft_id, "--image", str(base_image), "--json"],
        capsys,
    )
    assert (code, based["ok"], based["stage"]) == (0, True, "turnaround")
    assert based["baseImage"].endswith(".png")

    code, turned = run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)
    assert (code, turned["ok"]) == (0, True)
    assert sorted(turned["turnaround"]) == sorted(SPEC.scheme.authored)
    assert turned["stage"] == "turnaround"

    code, status = run(["harness", "characters", "status", "--draft", draft_id, "--json"], capsys)
    assert (code, status["ok"]) == (0, True)
    assert sorted(status["status"]["pending"]["turnaround"]) == sorted(SPEC.scheme.authored)

    code, approved = run(
        ["harness", "characters", "approve-direction", "--draft", draft_id, "--all", "--json"], capsys
    )
    assert (code, approved["ok"], approved["stage"]) == (0, True, "rows")
    assert sorted(approved["approved"]) == sorted(SPEC.scheme.authored)

    code, rows = run(["harness", "characters", "rows", "--draft", draft_id, "--json"], capsys)
    assert (code, rows["ok"]) == (0, True)
    assert sorted(rows["rows"]) == sorted(row.key for row in SPEC.authored_rows())

    code, composed = run(["harness", "characters", "compose", "--draft", draft_id, "--json"], capsys)
    assert (code, composed["ok"], composed["stage"]) == (0, True, "composed")
    assert composed["validation"]["ok"] is True
    assert (composed["validation"]["width"], composed["validation"]["height"]) == SPEC.sheet_size()

    # A composed draft reopens for a row fix and recomposes; nothing else moves.
    code, reopened = run(["harness", "characters", "reopen", "--draft", draft_id, "--json"], capsys)
    assert (code, reopened["ok"], reopened["stage"]) == (0, True, "rows")

    code, rerolled = run(
        ["harness", "characters", "reroll-row", "--draft", draft_id, "--row", "walk-e", "--json"],
        capsys,
    )
    assert (code, rerolled["ok"], rerolled["approved"]) == (0, True, True)

    code, recomposed = run(["harness", "characters", "compose", "--draft", draft_id, "--json"], capsys)
    assert (code, recomposed["ok"], recomposed["stage"]) == (0, True, "composed")

    code, listed = run(["harness", "characters", "list", "--json"], capsys)
    assert (code, listed["ok"]) == (0, True)
    assert [row["id"] for row in listed["drafts"]] == [draft_id]
    assert [row["slug"] for row in listed["characters"]] == [composed["slug"]]
    assert listed["characters"][0]["installed"] is True

    code, sprite = run(["harness", "characters", "sprite", composed["slug"], "--json"], capsys)
    character = sprite["character"]
    assert (code, sprite["ok"]) == (0, True)
    assert character["slug"] == composed["slug"]
    assert character["framesByRow"] == {row.key: row.frames for row in SPEC.rows()}
    assert character["stateRows"] == [row.key for row in SPEC.rows()]
    assert character["directions"]["mirrored"] == dict(SPEC.scheme.mirrored)
    assert base64.standard_b64decode(character["spritesheetBase64"])[:4] == b"RIFF"
    assert character["mime"] == "image/webp"
    # The overlap with the pet payload keeps its pet meanings; the additions are
    # what let a consumer read a directional sheet without inferring its layout.
    assert set(character) >= {
        "slug",
        "displayName",
        "mime",
        "spritesheetBase64",
        "spritesheetRevision",
        "frameW",
        "frameH",
        "framesByRow",
        "loopMs",
        "scale",
        "stateRows",
    }
    assert set(character) >= {"directions", "states", "rows"}
    assert "framesPerState" not in character


def test_a_reroll_and_a_row_reroll_are_reachable_through_the_cli(fake, base_image, capsys):
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)

    code, rerolled = run(
        [
            "harness", "characters", "reroll-direction", "--draft", draft_id,
            "--direction", "e", "--note", "taller plume", "--json",
        ],
        capsys,
    )
    assert (code, rerolled["ok"], rerolled["approved"]) == (0, True, False)
    assert rerolled["attempts"] == 2
    assert rerolled["note"] == "taller plume"

    run(["harness", "characters", "approve-direction", "--draft", draft_id, "--all", "--json"], capsys)
    run(["harness", "characters", "rows", "--draft", draft_id, "--only", "walk-e", "--json"], capsys)

    code, row = run(
        [
            "harness", "characters", "reroll-row", "--draft", draft_id,
            "--row", "walk-e", "--note", "tighter step", "--json",
        ],
        capsys,
    )
    assert (code, row["ok"], row["approved"]) == (0, True, True)
    assert row["row"] == "walk-e"
    assert row["attempts"] == 2


def test_thumb_writes_the_crop_its_payload_describes(fake, base_image, capsys, tmp_path):
    """The byte shape: the payload's path, size and opacity are the file's.

    A card-size crop is the whole point of the verb (plan §F.2), so the numbers
    it reports have to be readable back out of the PNG — and the picture has to
    be opaque, because the seam this exists to reveal is drawn over transparent
    pixels.
    """
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)
    run(["harness", "characters", "approve-direction", "--draft", draft_id, "--all", "--json"], capsys)
    run(["harness", "characters", "rows", "--draft", draft_id, "--only", "walk-e", "--json"], capsys)

    code, payload = run(
        ["harness", "characters", "thumb", "--draft", draft_id, "--row", "walk-e", "--json"],
        capsys,
    )

    assert (code, payload["ok"]) == (0, True)
    assert set(payload) >= {"ok", "path", "row", "attempt", "width", "height"}
    assert (payload["row"], payload["attempt"], payload["draft"]) == ("walk-e", 0, draft_id)
    out = Path(payload["path"])
    assert out.name == "walk-e-a0-x2.png"
    assert out.parent.name == "thumbs"
    with Image.open(out) as crop, Image.open(payload["source"]) as source:
        assert (crop.width, crop.height) == (payload["width"], payload["height"])
        assert (crop.width, crop.height) == (source.width * 2, source.height * 2)
        assert crop.convert("RGBA").getpixel((0, 0))[3] == 255, "the crop must be opaque"
    # A path, never bytes: nothing in the payload carries an image inline.
    assert "base64" not in json.dumps(payload).lower()


def test_thumb_addresses_one_attempt_at_a_time(fake, base_image, capsys):
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)
    run(["harness", "characters", "approve-direction", "--draft", draft_id, "--all", "--json"], capsys)
    run(["harness", "characters", "rows", "--draft", draft_id, "--only", "walk-e", "--json"], capsys)
    run(
        ["harness", "characters", "reroll-row", "--draft", draft_id, "--row", "walk-e",
         "--note", "tighter step", "--json"],
        capsys,
    )

    code, first = run(
        ["harness", "characters", "thumb", "--draft", draft_id, "--row", "walk-e",
         "--attempt", "0", "--scale", "3", "--json"],
        capsys,
    )
    _, latest = run(
        ["harness", "characters", "thumb", "--draft", draft_id, "--row", "walk-e", "--json"],
        capsys,
    )
    status = run(["harness", "characters", "status", "--draft", draft_id, "--json"], capsys)[1]

    assert code == 0
    assert Path(first["path"]).name == "walk-e-a0-x3.png"
    assert (latest["attempt"], Path(latest["path"]).name) == (1, "walk-e-a1-x2.png")
    assert first["path"] != latest["path"]
    # Each crop is taken from that attempt's own file, as `status` reports it.
    history = status["status"]["rows"]["walk-e"]["history"]
    assert [entry["path"] for entry in history] == [first["source"], latest["source"]]


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--row", "idle-w"], "is not an authored row"),
        (["--row", "walk-e", "--attempt", "9"], "out of range"),
        (["--row", "walk-e", "--scale", "0"], "out of range"),
    ],
)
def test_a_crop_that_cannot_be_taken_reports_the_pets_error_shape(
    fake, base_image, capsys, argv, message
):
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)
    run(["harness", "characters", "approve-direction", "--draft", draft_id, "--all", "--json"], capsys)
    run(["harness", "characters", "rows", "--draft", draft_id, "--only", "walk-e", "--json"], capsys)

    code, payload = run(
        ["harness", "characters", "thumb", "--draft", draft_id, *argv, "--json"], capsys
    )

    assert code == 2
    assert payload["ok"] is False
    assert message in payload["error"]
    assert not isinstance(payload["error"], dict)  # flat, not the Stage-42 envelope
    assert payload["draft"] == draft_id


def test_start_records_the_authoring_persona_as_provenance(fake, capsys):
    code, started = run(
        [
            "harness", "characters", "start",
            "--concept", "an arrow knight",
            "--slug", "arrow-knight",
            "--states", STATES_FLAG,
            "--directions", DIRECTIONS_FLAG,
            "--authored-by", "alice",
            "--json",
        ],
        capsys,
    )
    draft_id = started["draft"]

    _, status = run(["harness", "characters", "status", "--draft", draft_id, "--json"], capsys)
    _, listed = run(["harness", "characters", "list", "--json"], capsys)

    assert (code, started["ok"]) == (0, True)
    assert started["summary"]["authoredBy"] == "alice"
    assert status["status"]["authoredBy"] == "alice"
    assert listed["drafts"][0]["authoredBy"] == "alice"


def test_start_shapes_the_sheet_from_the_states_and_directions_flags(fake, capsys):
    code, payload = run(
        [
            "harness", "characters", "start",
            "--concept", "a tall knight",
            "--states", "idle:4,cheer:3:fixed",
            "--directions", "4",
            "--json",
        ],
        capsys,
    )
    summary = payload["summary"]
    expected = SheetSpec(
        states=(StateSpec("idle", 4, True), StateSpec("cheer", 3, False)), scheme=FOUR_WAY
    )

    assert code == 0
    assert summary["directions"] == len(FOUR_WAY.order)
    assert summary["rows"] == len(expected.rows())
    assert summary["authoredRows"] == len(expected.authored_rows())
    assert summary["slug"] == "a-tall-knight"
    assert summary["stage"] == "turnaround"
    assert summary["baseImage"] == ""


# ────────────────────────────── the error shape ──────────────────────────────


def test_an_out_of_order_verb_reports_the_pets_error_shape(fake, base_image, capsys):
    draft_id = start_draft(capsys, "--base-image", str(base_image))

    args = parser().parse_args(["harness", "characters", "rows", "--draft", draft_id, "--json"])
    code = args.func(args)
    payload = one_json_object(capsys.readouterr().out)

    assert code == 2
    assert payload["ok"] is False
    assert "requires draft stage" in payload["error"]
    assert payload == {"ok": False, "error": payload["error"], "draft": draft_id, "stage": "turnaround"}
    assert not isinstance(payload["error"], dict)  # flat, not the Stage-42 envelope


@pytest.mark.parametrize(
    "argv",
    [
        ["status", "--draft", "no-such-draft"],
        ["turnaround", "--draft", "no-such-draft"],
        ["compose", "--draft", "no-such-draft"],
        ["reopen", "--draft", "no-such-draft"],
        ["thumb", "--draft", "no-such-draft", "--row", "walk-e"],
    ],
)
def test_an_unknown_draft_is_an_error_with_the_id_echoed_back(fake, capsys, argv):
    code, payload = run(["harness", "characters", *argv, "--json"], capsys)

    assert code == 2
    assert payload["ok"] is False
    assert payload["draft"] == "no-such-draft"
    assert "no draft" in payload["error"]


def test_an_uninstalled_slug_is_an_error_with_the_slug_echoed_back(capsys):
    code, payload = run(["harness", "characters", "sprite", "ghost-knight", "--json"], capsys)

    assert code == 2
    assert payload["ok"] is False
    assert payload["slug"] == "ghost-knight"
    assert "not installed" in payload["error"]


def test_a_malformed_states_flag_is_refused_before_a_draft_exists(fake, capsys):
    code, payload = run(
        ["harness", "characters", "start", "--concept", "x", "--states", "idle", "--json"], capsys
    )

    assert code == 2
    assert payload["ok"] is False
    assert "malformed state entry" in payload["error"]

    code, listed = run(["harness", "characters", "list", "--json"], capsys)
    assert listed["drafts"] == []


def test_a_base_image_that_does_not_exist_is_refused(fake, capsys, tmp_path):
    code, payload = run(
        [
            "harness", "characters", "start", "--concept", "x",
            "--base-image", str(tmp_path / "missing.png"), "--json",
        ],
        capsys,
    )

    assert code == 2
    assert payload["ok"] is False
    assert "is not an existing file" in payload["error"]


def test_an_unauthored_direction_is_refused(fake, base_image, capsys):
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)

    code, payload = run(
        ["harness", "characters", "approve-direction", "--draft", draft_id, "--direction", "w", "--json"],
        capsys,
    )

    assert code == 2
    assert payload["ok"] is False
    assert "is not authored for this sheet" in payload["error"]
    assert payload["stage"] == "turnaround"


def test_without_json_the_output_is_a_human_line_not_a_payload(fake, capsys):
    args = parser().parse_args(["harness", "characters", "start", "--concept", "a tall knight"])

    assert args.func(args) == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("Draft ")
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


# ───────────────── the shipped pets payload keys (regression) ─────────────────


def _write_pet(tmp_path, slug="milo", display_name="Milo"):
    from agent.pet import constants

    pets = tmp_path / "home" / "pets" / slug
    pets.mkdir(parents=True)
    sheet = Image.new(
        "RGBA",
        (constants.FRAME_W * constants.FRAMES_PER_STATE, constants.FRAME_H * 2),
        (0, 0, 0, 0),
    )
    draw = ImageDraw.Draw(sheet)
    for col in range(3):
        x = col * constants.FRAME_W
        draw.rectangle((x + 20, 20, x + 120, 150), fill=(30 + col * 40, 90, 220, 255))
    for col in range(2):
        x = col * constants.FRAME_W
        draw.ellipse(
            (x + 30, constants.FRAME_H + 30, x + 130, constants.FRAME_H + 150),
            fill=(220, 90 + col * 40, 30, 255),
        )
    sheet.save(pets / "spritesheet.png")
    (pets / "pet.json").write_text(
        json.dumps(
            {
                "id": slug,
                "displayName": display_name,
                "description": "A test pet",
                "spritesheetPath": "spritesheet.png",
            }
        ),
        encoding="utf-8",
    )
    return pets


def test_the_pets_sprite_payload_still_carries_the_keys_the_launcher_reads(tmp_path, capsys):
    _write_pet(tmp_path, slug="milo", display_name="Milo")

    code, payload = run(["harness", "pets", "sprite", "milo", "--json"], capsys)

    assert (code, payload["ok"]) == (0, True)
    assert set(payload["pet"]) == {
        "slug",
        "displayName",
        "description",
        "mime",
        "spritesheetBase64",
        "spritesheetRevision",
        "frameW",
        "frameH",
        "framesPerState",
        "framesByState",
        "framesByRow",
        "loopMs",
        "scale",
        "stateRows",
    }
    assert payload["pet"]["slug"] == "milo"
    assert base64.standard_b64decode(payload["pet"]["spritesheetBase64"]).startswith(b"\x89PNG")
def test_start_refuses_a_hyphenated_state_name(fake, capsys):
    """The row-key separator is refused at the CLI door, in the pets error shape.

    A hyphen in a state name would put a second separator into every row key of
    that state, and the launcher splits on the LAST one — so the refusal has to
    happen before a draft directory exists, not at compose time.
    """
    code, payload = run(
        [
            "harness", "characters", "start",
            "--concept", "x",
            "--states", "idle:6,spin-kick:4",
            "--json",
        ],
        capsys,
    )

    assert code == 2
    assert payload["ok"] is False
    assert "may not contain '-'" in payload["error"]
    assert not isinstance(payload["error"], dict)

    code, listed = run(["harness", "characters", "list", "--json"], capsys)
    assert listed["drafts"] == []
