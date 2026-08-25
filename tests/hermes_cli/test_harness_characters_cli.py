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
from agent.charsheet.draft import drafts_dir
from agent.charsheet.revisions import STATE_FILENAME
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


ADDED_STATE_FLAG = "jump:3"
SPEC_WITH_ADDED = SheetSpec(
    states=SPEC.states + (StateSpec("jump", 3, True),), scheme=FOUR_WAY
)


@pytest.fixture
def fake_grown(tmp_path, monkeypatch):
    """A draftsman that also knows the rows `add-state` is about to author."""
    provider = FakeProvider(SPEC_WITH_ADDED, tmp_path / "generated")
    monkeypatch.setattr(pipeline, "_generate_image", provider)
    return provider


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
        (["add-state", "--draft", "d", "--state", "jump:3"], "add-state"),
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


# FIXTURE RULE for every assertion below that judges PIXELS: the crop's input
# comes from `generate_row_strip` through the fake provider (`rows` /
# `reroll-row`), never from a hand-built PIL image proposed into the store. A
# hand-built input is one the author chose to have the property under test, so
# the assertion holds whether or not the code puts it there — which is how
# `assert crop.getpixel((0, 0))[3] == 255, "the crop must be opaque"` came to
# survive deleting the backdrop composite entirely (a row attempt is opaque
# magenta already). Longer note: tests/agent/test_charsheet_draft.py.


def test_thumb_writes_the_crop_its_payload_describes(fake, base_image, capsys, tmp_path):
    """The byte shape: the payload's path, size and ground are the file's.

    A card-size crop is the whole point of the verb (plan §F.2), so the numbers
    it reports have to be readable back out of the PNG — and the picture has to
    show the flat dark ground, because a chroma field that survives into the
    crop is the field the seam was invisible against in the first place.
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
    assert set(payload) >= {
        "ok", "path", "row", "attempt", "frame", "frames", "width", "height", "cardSafe",
    }
    assert payload["cardSafe"] is True, "the default crop is the one a card may draw"
    assert (payload["row"], payload["attempt"], payload["draft"]) == ("walk-e", 0, draft_id)
    assert (payload["frame"], payload["frames"]) == (0, 2)
    out = Path(payload["path"])
    assert out.name == "walk-e-attempt-1-frame-1-x2.png"
    assert out.parent.name == "thumbs"
    with Image.open(out) as opened, Image.open(payload["source"]) as opened_source:
        crop = opened.convert("RGBA")
        source = opened_source.convert("RGBA")
        assert (crop.width, crop.height) == (payload["width"], payload["height"])
        # ONE frame cell of the strip, then upscaled — not the whole strip.
        assert (crop.width, crop.height) == (round(source.width / 2) * 2, source.height * 2)
        assert source.getpixel((0, 0))[:3] == pipeline.MAGENTA, "not a live-shaped strip"
        assert crop.getpixel((0, 0)) == pipeline.QA_BACKDROP, (
            "the chroma field reached the crop: the backdrop never showed"
        )
    # A path, never bytes: nothing in the payload carries an image inline.
    assert "base64" not in json.dumps(payload).lower()


def test_thumb_addresses_one_attempt_and_one_frame_at_a_time(fake, base_image, capsys):
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
    _, other_frame = run(
        ["harness", "characters", "thumb", "--draft", draft_id, "--row", "walk-e",
         "--frame", "1", "--json"],
        capsys,
    )
    status = run(["harness", "characters", "status", "--draft", draft_id, "--json"], capsys)[1]

    assert code == 0
    assert Path(first["path"]).name == "walk-e-attempt-1-frame-1-x3.png"
    assert (latest["attempt"], Path(latest["path"]).name) == (
        1,
        "walk-e-attempt-2-frame-1-x2.png",
    )
    assert (other_frame["frame"], Path(other_frame["path"]).name) == (
        1,
        "walk-e-attempt-2-frame-2-x2.png",
    )
    assert len({first["path"], latest["path"], other_frame["path"]}) == 3
    # The thumb filename counts the way the store's own filenames count, so an
    # operator walks from one to the other with no off-by-one.
    assert Path(first["source"]).name == "attempt-1.png"
    assert Path(latest["source"]).name == "attempt-2.png"
    # Each crop is taken from that attempt's own file, as `status` reports it.
    history = status["status"]["rows"]["walk-e"]["history"]
    assert [entry["path"] for entry in history] == [first["source"], latest["source"]]


def test_a_missing_attempt_file_travels_as_json_null_and_never_as_an_empty_string(
    fake, base_image, capsys
):
    """`null` is what B2 and the `MEDIA:` protocol can actually read.

    Same payload, two spellings of absence: `authoredBy` was fixed to `null`
    while `history[].path`, `current` and `approvedPath` — the field A1 itself
    introduced among them — still flattened `Path | None` to `""`. A consumer
    handed `""` cannot tell "no image recorded" from any other empty value, and
    an agent interpolating it emits a bare `MEDIA:` line. Asserted on the raw
    JSON text, because the coercion this pins happens on the way out.
    """
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)
    item_dir = drafts_dir() / draft_id / "revisions" / "turnaround@e"
    state = json.loads((item_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    state["attempts"][0].pop("file")
    (item_dir / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    args = parser().parse_args(
        ["harness", "characters", "status", "--draft", draft_id, "--json"]
    )
    assert args.func(args) == 0
    text = capsys.readouterr().out
    item = one_json_object(text)["status"]["turnaround"]["e"]

    assert item["history"][0]["path"] is None
    assert item["current"] is None
    assert item["approvedPath"] is None
    assert '"path": null' in text
    assert '"authoredBy": null' in text, "the field whose rule these three now follow"


def test_a_deep_zoom_says_it_is_not_a_card_in_the_payload_and_in_the_line(
    fake, base_image, capsys
):
    """An agent reads the sentence as often as the JSON, so both carry it.

    The one thing it must not do with a `--scale 10` crop is declare it with a
    `MEDIA:` line — that hands the console a decode several times the sheet's
    for a 420px square (risk D.3). The file is fine; the claim "this is a card"
    is not, and silence is what let the claim through.
    """
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)
    run(["harness", "characters", "approve-direction", "--draft", draft_id, "--all", "--json"], capsys)
    run(["harness", "characters", "rows", "--draft", draft_id, "--only", "walk-e", "--json"], capsys)

    code, zoomed = run(
        ["harness", "characters", "thumb", "--draft", draft_id, "--row", "walk-e",
         "--scale", "10", "--json"],
        capsys,
    )
    spoken = parser().parse_args(
        ["harness", "characters", "thumb", "--draft", draft_id, "--row", "walk-e",
         "--scale", "10"]
    )
    assert spoken.func(spoken) == 0
    line = capsys.readouterr().out.strip()

    assert code == 0, "a viewer artifact is written, not refused"
    assert zoomed["cardSafe"] is False
    assert zoomed["width"] * zoomed["height"] > pipeline.MAX_CARD_PIXELS
    assert "open it in the viewer" in line


def test_a_human_line_counts_attempts_the_way_the_store_names_its_files(
    fake, base_image, capsys
):
    """One helper renders every attempt an operator reads (`_attempt_label`).

    The payload stays 0-based machine truth; the sentence beside it is 1-based,
    because the file it is talking about is `attempt-2.png`. "attempt 1, 2
    total" for the SECOND attempt put two bases in one sentence.
    """
    draft_id = start_draft(capsys, "--base-image", str(base_image))
    run(["harness", "characters", "turnaround", "--draft", draft_id, "--json"], capsys)
    run(["harness", "characters", "approve-direction", "--draft", draft_id, "--all", "--json"], capsys)
    run(["harness", "characters", "rows", "--draft", draft_id, "--only", "walk-e", "--json"], capsys)

    rerolled = parser().parse_args(
        ["harness", "characters", "reroll-row", "--draft", draft_id, "--row", "walk-e"]
    )
    assert rerolled.func(rerolled) == 0
    reroll_line = capsys.readouterr().out.strip()

    thumbed = parser().parse_args(
        ["harness", "characters", "thumb", "--draft", draft_id, "--row", "walk-e",
         "--attempt", "1"]
    )
    assert thumbed.func(thumbed) == 0
    thumb_line = capsys.readouterr().out.strip()

    assert "attempt 2 of 2" in reroll_line
    assert "attempt 1 of 2" not in reroll_line
    assert "attempt 2 of 2" in thumb_line
    assert "frame 1 of 2" in thumb_line
    assert "walk-e-attempt-2-frame-1-x2.png" in thumb_line


@pytest.mark.parametrize(
    "argv, message",
    [
        (["--row", "idle-w"], "is not an authored row"),
        (["--row", "walk-e", "--attempt", "9"], "out of range"),
        (["--row", "walk-e", "--frame", "9"], "frame 9 out of range"),
        (["--row", "walk-e", "--scale", "0"], "must be an integer >= 1"),
        (["--row", "walk-e", "--scale", "9999"], "budget"),
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


def test_an_unattributed_draft_reports_a_null_author_in_every_payload(fake, capsys):
    """Absence has to reach the consumer, in both payload spellings.

    B2/P1 see only the payload. `""` reads as a value and is what a draft
    written before the field existed also produced, so a consumer could not tell
    "no author recorded" from "authored by the empty string" and no backfill
    could select the drafts that need one.
    """
    draft_id = start_draft(capsys)

    _, status = run(["harness", "characters", "status", "--draft", draft_id, "--json"], capsys)
    _, listed = run(["harness", "characters", "list", "--json"], capsys)

    # Both spellings, read back off real stdout: the key is present and null,
    # not absent and not "".
    assert status["status"]["authoredBy"] is None
    assert listed["drafts"][0]["authoredBy"] is None
    # `baseImage` is a PATH field and answers the same way, in both payloads.
    # It was the one `path_or_none` did not reach — absence kept a second
    # spelling in the very response that had just been cleaned of it, and the
    # `MEDIA:<path>` protocol turns `""` into a bare `MEDIA:` line that renders
    # no card at all.
    assert status["status"]["baseImage"] is None
    assert listed["drafts"][0]["baseImage"] is None


def test_a_base_image_still_travels_as_a_string_in_every_payload(fake, base_image, capsys):
    """The other half of the rule: present means a `str`, not a truthy object.

    Fixing absence by returning `None` everywhere would be a payload that never
    names the file, so the presence case is pinned beside it.
    """
    draft_id = start_draft(capsys, "--base-image", str(base_image))

    _, status = run(["harness", "characters", "status", "--draft", draft_id, "--json"], capsys)
    _, listed = run(["harness", "characters", "list", "--json"], capsys)

    for payload in (status["status"], listed["drafts"][0]):
        assert isinstance(payload["baseImage"], str)
        assert Path(payload["baseImage"]).is_file()


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
    # A draft started without --base-image has no base image, and absence is
    # spelled the ONE way this payload spells it. This line read `== ""` until
    # 2026-08-24: `list`'s `baseImage` was the fourth path field still
    # flattening the store's `Path | None`, in a response whose other three had
    # already been fixed and whose helper docstring already claimed there was a
    # single spelling. The test PINNED the defect, which is why a review found
    # it and the suite did not.
    assert summary["baseImage"] is None


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


# ------------------------- add-state (more strips later) -------------------------


def drive(capsys, draft_id, *argvs):
    """Run a sequence of `characters` verbs, asserting each one answered ok."""
    for argv in argvs:
        code, payload = run(["harness", "characters", *argv, "--json"], capsys)
        assert (code, payload["ok"]) == (0, True), (argv, payload)
    return draft_id


def rows_stage_draft(capsys, base_image):
    draft_id = start_draft(capsys)
    return drive(
        capsys,
        draft_id,
        ["base", "--draft", draft_id, "--image", str(base_image)],
        ["turnaround", "--draft", draft_id],
        ["approve-direction", "--draft", draft_id, "--all"],
    )


def test_add_state_runs_the_reopen_add_rows_compose_loop_through_the_cli(
    fake_grown, base_image, capsys
):
    draft_id = rows_stage_draft(capsys, base_image)
    drive(
        capsys,
        draft_id,
        ["rows", "--draft", draft_id],
        ["compose", "--draft", draft_id],
        # `reopen` is the only door: an installed character is at `composed`.
        ["reopen", "--draft", draft_id],
    )

    code, added = run(
        [
            "harness", "characters", "add-state",
            "--draft", draft_id,
            "--state", ADDED_STATE_FLAG,
            "--json",
        ],
        capsys,
    )
    assert (code, added["ok"], added["stage"]) == (0, True, "rows")
    assert added["state"] == {"name": "jump", "frames": 3, "directional": True}
    assert added["states"] == ["idle", "walk", "jump"]
    assert added["rows"] == [f"jump-{d}" for d in FOUR_WAY.authored]

    # `--only` has NO glob: the CLI comma-splits and `run_rows` matches exactly,
    # so the keys the verb handed back are the keys that go back in.
    code, rows = run(
        [
            "harness", "characters", "rows",
            "--draft", draft_id,
            "--only", ",".join(added["rows"]),
            "--json",
        ],
        capsys,
    )
    assert (code, rows["ok"]) == (0, True)
    assert sorted(rows["rows"]) == sorted(added["rows"])

    code, recomposed = run(
        ["harness", "characters", "compose", "--draft", draft_id, "--json"], capsys
    )
    assert (code, recomposed["ok"], recomposed["stage"]) == (0, True, "composed")
    assert (
        recomposed["validation"]["width"],
        recomposed["validation"]["height"],
    ) == SPEC_WITH_ADDED.sheet_size()

    code, sprite = run(
        ["harness", "characters", "sprite", recomposed["slug"], "--json"], capsys
    )
    assert (code, sprite["ok"]) == (0, True)
    character = sprite["character"]
    assert [state["name"] for state in character["states"]] == ["idle", "walk", "jump"]
    assert character["stateRows"] == [row.key for row in SPEC_WITH_ADDED.rows()]
    assert character["framesByRow"]["jump-s"] == 3


@pytest.mark.parametrize(
    "state_flag, message",
    [
        # The trap: a one-frame state used to pass `start` and die at `rows`,
        # several generations later. `add-state` refuses it at the door.
        ("jump:1", "out of range"),
        ("walk:5", "is already on this sheet"),
        ("jump:3,cheer:4:fixed", "takes ONE state"),
        ("spin-kick:4", "may not contain '-'"),
    ],
)
def test_add_state_refuses_in_the_flat_pets_error_shape(
    fake_grown, base_image, capsys, state_flag, message
):
    draft_id = rows_stage_draft(capsys, base_image)

    code, payload = run(
        [
            "harness", "characters", "add-state",
            "--draft", draft_id,
            "--state", state_flag,
            "--json",
        ],
        capsys,
    )

    assert code == 2
    assert payload["ok"] is False
    assert message in payload["error"]
    assert (payload["draft"], payload["stage"]) == (draft_id, "rows")

    code, status = run(
        ["harness", "characters", "status", "--draft", draft_id, "--json"], capsys
    )
    assert [state["name"] for state in status["status"]["spec"]["states"]] == [
        "idle",
        "walk",
    ]


def test_the_add_state_human_line_hands_over_the_exact_only_list(
    fake_grown, base_image, capsys
):
    """The no-glob trap, answered by the verb that knows the keys.

    `--only jump-*` is one unknown row key, not a wildcard, so the operator
    needs the list spelled out — and the only surface that has it at that
    moment is this line.
    """
    draft_id = rows_stage_draft(capsys, base_image)

    args = parser().parse_args(
        [
            "harness", "characters", "add-state",
            "--draft", draft_id,
            "--state", ADDED_STATE_FLAG,
        ]
    )
    assert args.func(args) == 0

    line = capsys.readouterr().out.strip()
    expected = ",".join(f"jump-{d}" for d in FOUR_WAY.authored)
    assert f"--only {expected}" in line
    assert line.startswith(f"Draft {draft_id}: state jump added")
