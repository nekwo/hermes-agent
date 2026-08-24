"""The staged draft machine: what each verb refuses, and what it installs.

Everything runs against a real ``HERMES_HOME`` under ``tmp_path`` with the
provider seam (``pipeline._generate_image``) replaced by a deterministic
draftsman, so the stage transitions, the revision bookkeeping and the installed
manifest/payload are exercised on real files with no network.

The spec under test is two states in 4-way — small on purpose: the stage machine
and the payload are direction-count agnostic, so the cheap scheme proves them.
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from agent.charsheet import pipeline
from agent.charsheet.draft import (
    MANIFEST_FILENAME,
    SHEET_FILENAME,
    STAGES,
    CharacterDraft,
    characters_dir,
    drafts_dir,
    row_item,
    slugify,
    spec_from_dict,
    spec_to_dict,
    sprite_payload,
    turnaround_item,
)
from agent.charsheet.revisions import ImageRevisionStore
from agent.charsheet.spec import CHAR8, EIGHT_WAY, FOUR_WAY, SheetSpec, StateSpec

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402

SPEC = SheetSpec(
    states=(StateSpec("idle", 2, True), StateSpec("walk", 3, True)),
    scheme=FOUR_WAY,
)
CONCEPT = "an arrow knight"
SLUG = "arrow-knight"

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
    """Answers every call at the seam with a deterministic synthetic image."""

    def __init__(self, spec, out_dir):
        self.spec = spec
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.calls: list[dict] = []
        self.order = pipeline.turnaround_order(spec.scheme.authored)
        self._rows = {pipeline.row_prefix(row.key): row for row in spec.authored_rows()}
        self._views = {pipeline.view_prefix(d): d for d in spec.scheme.order}

    def __call__(self, prompt, *, reference_images, aspect_ratio, prefix, provider):
        self.calls.append(
            {
                "prompt": prompt,
                "refs": [str(ref) for ref in (reference_images or [])],
                "aspect": aspect_ratio,
                "prefix": prefix,
            }
        )
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


def write_base(directory):
    path = directory / "base.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    square_image("s").save(path, format="PNG")
    return path


def run_to_rows(base, *, slug=SLUG):
    draft = CharacterDraft.create(concept=CONCEPT, slug=slug, spec=SPEC, base_image=base)
    draft.run_turnaround()
    draft.approve_all_directions()
    return draft


def run_to_composed(base, *, slug=SLUG):
    draft = run_to_rows(base, slug=slug)
    draft.run_rows()
    draft.compose()
    return draft


# ───────────────────────────────── fixtures ─────────────────────────────────


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
def base(tmp_path):
    return write_base(tmp_path / "src")


@pytest.fixture
def draft(fake, base):
    """A fresh draft at stage 'turnaround' with its identity anchor in place."""
    return CharacterDraft.create(concept=CONCEPT, slug=SLUG, spec=SPEC, base_image=base)


@pytest.fixture(scope="module")
def _installed(tmp_path_factory):
    """One composed + installed character, shared read-only by several tests."""
    root = tmp_path_factory.mktemp("installed")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HERMES_HOME", str(root / "home"))
        patch.setattr(pipeline, "_generate_image", FakeProvider(SPEC, root / "generated"))
        draft = run_to_composed(write_base(root / "src"))
        return {"home": root / "home", "id": draft.id, "slug": draft.slug}


@pytest.fixture
def installed(_installed, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(_installed["home"]))
    return _installed


# ─────────────────────────── stage machine order ───────────────────────────


def test_stages_are_declared_in_qa_order(draft):
    assert STAGES == ("turnaround", "rows", "composed")
    assert draft.stage == STAGES[0]


@pytest.mark.parametrize(
    "verb",
    [
        lambda d: d.run_rows(),
        lambda d: d.reroll_row("walk-e"),
        lambda d: d.compose(),
    ],
)
def test_a_row_or_compose_verb_is_refused_while_the_directions_are_unapproved(draft, verb):
    with pytest.raises(ValueError, match="requires draft stage"):
        verb(draft)

    assert draft.stage == "turnaround"
    assert CharacterDraft.load(draft.id).stage == "turnaround"


@pytest.mark.parametrize(
    "verb",
    [
        lambda d: d.run_turnaround(),
        lambda d: d.reroll_direction("e"),
        lambda d: d.approve_direction("e"),
        lambda d: d.approve_all_directions(),
    ],
)
def test_a_turnaround_verb_is_refused_once_the_stage_has_advanced(fake, base, verb):
    draft = run_to_rows(base)

    with pytest.raises(ValueError, match="requires draft stage 'turnaround'"):
        verb(draft)

    assert draft.stage == "rows"


@pytest.mark.parametrize(
    "verb",
    [
        lambda d: d.run_rows(),
        lambda d: d.reroll_row("walk-e"),
        lambda d: d.compose(),
        lambda d: d.run_turnaround(),
    ],
)
def test_every_generation_verb_is_refused_after_the_sheet_is_composed(installed, verb):
    draft = CharacterDraft.load(installed["id"])
    assert draft.stage == "composed"

    with pytest.raises(ValueError, match="requires draft stage"):
        verb(draft)

    assert CharacterDraft.load(installed["id"]).stage == "composed"


def test_the_refusal_names_the_current_stage_and_the_stage_order(draft):
    with pytest.raises(ValueError) as excinfo:
        draft.run_rows()

    message = str(excinfo.value)
    assert "run_rows" in message
    assert "stage 'turnaround'" in message
    assert " -> ".join(STAGES) in message


# ──────────────────────── turnaround: pending QA ────────────────────────


def test_the_turnaround_proposes_one_unapproved_reference_per_authored_direction(draft):
    result = draft.run_turnaround()

    assert sorted(result["turnaround"]) == sorted(SPEC.scheme.authored)
    assert all(item["approved"] is False for item in result["turnaround"].values())
    assert sorted(draft.store.pending()) == sorted(
        turnaround_item(d) for d in SPEC.scheme.authored
    )
    assert draft.stage == "turnaround"


def test_the_stage_advances_only_when_every_direction_is_approved(draft):
    draft.run_turnaround()
    authored = list(SPEC.scheme.authored)

    for direction in authored[:-1]:
        result = draft.approve_direction(direction)
        assert result["advanced"] is False
        assert draft.stage == "turnaround"

    result = draft.approve_direction(authored[-1])
    assert result["advanced"] is True
    assert draft.stage == "rows"
    assert CharacterDraft.load(draft.id).stage == "rows"


def test_re_rolling_a_direction_withdraws_its_approval(draft):
    draft.run_turnaround()
    draft.approve_direction("e")
    assert draft.store.current(turnaround_item("e")) is not None

    result = draft.reroll_direction("e", note="taller plume")

    assert result["approved"] is False
    assert result["attempts"] == 2
    assert draft.store.current(turnaround_item("e")) is None
    assert draft.store.history(turnaround_item("e"))[-1]["note"] == "taller plume"
    assert draft.stage == "turnaround"


def test_a_mirrored_direction_is_never_a_qa_item(draft):
    draft.run_turnaround()
    mirrored = next(iter(SPEC.scheme.mirrored))

    with pytest.raises(ValueError, match="is not authored for this sheet"):
        draft.reroll_direction(mirrored)
    with pytest.raises(ValueError, match="is not authored for this sheet"):
        draft.approve_direction(mirrored)
    assert turnaround_item(mirrored) not in draft.store.keys()


def test_approving_everything_before_anything_was_generated_is_refused(draft):
    with pytest.raises(ValueError, match="has no attempt to approve"):
        draft.approve_all_directions()

    assert draft.stage == "turnaround"


# ───────────────────────────── rows: auto-approved ─────────────────────────────


def test_every_accepted_row_strip_is_proposed_and_approved(fake, base):
    draft = run_to_rows(base)

    result = draft.run_rows()

    authored = [row.key for row in SPEC.authored_rows()]
    assert sorted(result["rows"]) == sorted(authored)
    assert all(item["approved"] is True for item in result["rows"].values())
    assert draft.store.pending() == []
    assert all(draft.store.current(row_item(key)) is not None for key in authored)


def test_a_directional_row_is_grounded_on_its_approved_direction_reference(fake, base):
    draft = run_to_rows(base)

    result = draft.run_rows(only=["walk-e"])

    assert list(result["rows"]) == ["walk-e"]
    assert result["rows"]["walk-e"]["reference"] == str(draft.store.current(turnaround_item("e")))


def test_re_rolling_a_row_keeps_it_approved_and_records_the_note(fake, base):
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])

    result = draft.reroll_row("walk-e", note="tighter step")

    assert result["approved"] is True
    assert result["attempts"] == 2
    assert draft.store.approved_index(row_item("walk-e")) == 1
    assert draft.store.history(row_item("walk-e"))[-1]["note"] == "tighter step"


@pytest.mark.parametrize("bad", ["walk-nope", "fly-e", "walk", ""])
def test_a_row_key_the_sheet_does_not_author_is_refused(fake, base, bad):
    draft = run_to_rows(base)

    with pytest.raises(ValueError, match="not an authored row"):
        draft.reroll_row(bad)
    with pytest.raises(ValueError, match="unknown row key"):
        draft.run_rows(only=[bad])


def test_a_mirrored_row_cannot_be_generated(fake, base):
    """`walk-w` is FOUR_WAY's flip of `walk-e`: not drawn, and since 3-B not a row."""
    draft = run_to_rows(base)

    with pytest.raises(ValueError, match="not an authored row"):
        draft.reroll_row("walk-w")


# ────────────────────────────── compose / install ──────────────────────────────


def test_compose_refuses_while_any_authored_row_lacks_an_approved_strip(fake, base):
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])
    missing = [row.key for row in SPEC.authored_rows() if row.key != "walk-e"]

    with pytest.raises(ValueError) as excinfo:
        draft.compose()

    message = str(excinfo.value)
    assert f"{len(missing)} row(s) have no approved strip" in message
    for key in missing:
        assert key in message
    assert draft.stage == "rows"


def test_compose_installs_a_validated_sheet_and_a_manifest_carrying_the_spec(installed):
    directory = characters_dir() / installed["slug"]
    manifest = json.loads((directory / MANIFEST_FILENAME).read_text(encoding="utf-8"))

    with Image.open(directory / SHEET_FILENAME) as opened:
        sheet = opened.convert("RGBA")
    assert sheet.size == SPEC.sheet_size()
    assert pipeline.validate_sheet(SPEC, sheet)["ok"] is True

    assert manifest["draftId"] == installed["id"]
    assert manifest["generator"] == "charsheet"
    assert spec_from_dict(manifest["spec"]) == SPEC
    assert [row["key"] for row in manifest["rows"]] == [row.key for row in SPEC.rows()]
    assert (manifest["frameW"], manifest["frameH"]) == (SPEC.frame_w, SPEC.frame_h)


def test_reopen_returns_a_composed_draft_to_rows_for_a_fix_and_recompose_reinstalls(fake, base):
    draft = run_to_composed(base)
    manifest_path = characters_dir() / draft.slug / MANIFEST_FILENAME
    installed_before = manifest_path.read_bytes()

    assert draft.reopen() == {"stage": "rows"}
    assert draft.stage == "rows"
    # Reopening deletes nothing: the install is untouched until the next compose.
    assert manifest_path.read_bytes() == installed_before

    draft.reroll_row("walk-e")
    composed = draft.compose()
    assert composed["slug"] == draft.slug
    assert draft.stage == "composed"
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["draftId"] == draft.id


def test_reopen_refuses_any_draft_that_is_not_composed(fake, base):
    draft = run_to_rows(base)

    with pytest.raises(ValueError, match="reopen requires draft stage 'composed'"):
        draft.reopen()

    assert draft.stage == "rows"


def test_a_second_draft_may_not_overwrite_another_characters_slug(installed, fake, tmp_path):
    manifest_path = characters_dir() / installed["slug"] / MANIFEST_FILENAME
    before = manifest_path.read_bytes()
    second = run_to_rows(write_base(tmp_path / "src2"), slug=installed["slug"])
    second.run_rows()
    assert second.id != installed["id"]

    with pytest.raises(ValueError, match="already installed from draft"):
        second.compose()

    assert manifest_path.read_bytes() == before
    assert json.loads(before)["draftId"] == installed["id"]
    assert second.stage == "rows"


# ───────────────────────────── the base image ─────────────────────────────


def test_a_base_image_that_does_not_exist_leaves_no_draft_behind(fake, tmp_path):
    before = sorted(p.name for p in drafts_dir().iterdir())

    with pytest.raises(ValueError, match="is not an existing file"):
        CharacterDraft.create(concept=CONCEPT, spec=SPEC, base_image=tmp_path / "nope.png")

    assert sorted(p.name for p in drafts_dir().iterdir()) == before
    assert CharacterDraft.list_drafts() == []


def test_a_draft_started_without_a_base_can_be_repaired_and_then_generate(fake, base):
    draft = CharacterDraft.create(concept=CONCEPT, slug=SLUG, spec=SPEC)
    assert draft.base_image is None

    with pytest.raises(ValueError, match="has no base image"):
        draft.run_turnaround()
    assert fake.calls == []

    stored = draft.set_base_image(base)

    assert stored.read_bytes() == base.read_bytes()
    assert CharacterDraft.load(draft.id).base_image == stored
    assert sorted(draft.run_turnaround()["turnaround"]) == sorted(SPEC.scheme.authored)


def test_a_draft_needs_a_concept(fake):
    with pytest.raises(ValueError, match="needs a concept"):
        CharacterDraft.create(concept="   ", spec=SPEC)


def test_setting_a_base_image_that_does_not_exist_is_refused(draft, tmp_path):
    with pytest.raises(ValueError, match="is not an existing file"):
        draft.set_base_image(tmp_path / "gone.png")


# ──────────────────────────── status / reporting ────────────────────────────


def test_status_shows_a_pending_item_by_its_latest_attempt(draft):
    draft.run_turnaround()
    draft.reroll_direction("e", note="again")

    status = draft.status_payload()
    item = status["turnaround"]["e"]

    assert item["attempts"] == 2
    assert item["approved"] is None
    assert item["approvedPath"] == ""
    assert item["current"] == str(draft.store.latest(turnaround_item("e")))
    assert item["history"][-1]["note"] == "again"
    assert "e" in status["pending"]["turnaround"]


def test_status_reports_what_has_not_been_generated_yet(draft):
    status = draft.status_payload()

    assert sorted(status["missing"]["turnaround"]) == sorted(SPEC.scheme.authored)
    assert sorted(status["missing"]["rows"]) == sorted(row.key for row in SPEC.authored_rows())
    assert status["stage"] == "turnaround"
    assert status["stages"] == list(STAGES)
    assert (status["spec"]["sheetWidth"], status["spec"]["sheetHeight"]) == SPEC.sheet_size()
    assert [row["key"] for row in status["spec"]["rows"]] == [row.key for row in SPEC.rows()]
    assert json.dumps(status)  # JSON-safe by construction


def test_status_only_lists_authored_rows_as_qa_items(draft):
    status = draft.status_payload()

    assert set(status["rows"]) == {row.key for row in SPEC.authored_rows()}
    assert set(status["rows"]) & {"idle-w", "walk-w"} == set()


def test_the_draft_on_disk_is_the_truth_across_instances(fake, base):
    draft = run_to_rows(base)
    reloaded = CharacterDraft.load(draft.id)

    assert reloaded.stage == "rows"
    assert reloaded.slug == draft.slug
    assert reloaded.concept == CONCEPT
    assert reloaded.spec == SPEC
    assert reloaded.directory == draft.directory

    reloaded.run_rows(only=["walk-e"])
    assert draft.store.current(row_item("walk-e")) is not None


def test_list_drafts_skips_directories_that_hold_no_draft(draft):
    (drafts_dir() / "not-a-draft").mkdir()

    assert [item.id for item in CharacterDraft.list_drafts()] == [draft.id]


def test_loading_an_unknown_draft_names_the_path_it_looked_at(fake):
    with pytest.raises(FileNotFoundError, match="no draft 'nope'"):
        CharacterDraft.load("nope")


def test_a_draft_id_cannot_escape_the_drafts_directory(fake, draft):
    with pytest.raises(FileNotFoundError):
        CharacterDraft.load("../../etc/passwd")


# ───────────────────────── revision-key compatibility ─────────────────────────


def test_the_draft_item_keys_are_valid_revision_store_keys():
    for direction in CHAR8.scheme.authored:
        key = turnaround_item(direction)
        assert ImageRevisionStore.validate_key(key) == key
    for row in CHAR8.authored_rows():
        key = row_item(row.key)
        assert ImageRevisionStore.validate_key(key) == key


@pytest.mark.parametrize(
    "name, expected",
    [("Arrow Knight", "arrow-knight"), ("  a//b  ", "a-b"), ("", "character"), ("!!", "character")],
)
def test_slugify_produces_one_safe_path_segment(name, expected):
    assert slugify(name) == expected


# ──────────────────────────── spec round-trip ────────────────────────────


@pytest.mark.parametrize("spec", [SPEC, CHAR8])
def test_the_spec_survives_a_json_round_trip(spec):
    as_dict = spec_to_dict(spec)

    assert json.loads(json.dumps(as_dict)) == as_dict
    assert spec_from_dict(as_dict) == spec
    assert spec_from_dict(json.loads(json.dumps(as_dict))).rows() == spec.rows()


@pytest.mark.parametrize(
    "payload, message",
    [
        ("not a dict", "must be a JSON object"),
        ({}, "states must be a non-empty list"),
        ({"states": []}, "states must be a non-empty list"),
        ({"states": ["idle"]}, "must be an object"),
        ({"states": [{"name": "idle", "frames": 2}], "scheme": {}}, "missing 'directional'"),
        ({"states": [{"name": "idle", "frames": 2, "directional": True}]}, "scheme must be a JSON object"),
        (
            {
                "states": [{"name": "idle", "frames": 2, "directional": True}],
                "scheme": {"order": ["e"]},
            },
            "scheme is missing 'authored'",
        ),
    ],
)
def test_a_malformed_spec_is_refused_with_a_reason(payload, message):
    with pytest.raises(ValueError, match=message):
        spec_from_dict(payload)


# ───────────────────────────── sprite payload ─────────────────────────────


def test_the_sprite_payload_ships_the_installed_bytes(installed):
    payload = sprite_payload(installed["slug"])
    sheet_path = characters_dir() / installed["slug"] / SHEET_FILENAME

    assert base64.standard_b64decode(payload["spritesheetBase64"]) == sheet_path.read_bytes()
    assert payload["mime"] == "image/webp"
    assert re.fullmatch(r"\d+:\d+", payload["spritesheetRevision"])
    assert payload["spritesheetRevision"] == "{}:{}".format(
        sheet_path.stat().st_mtime_ns, sheet_path.stat().st_size
    )
    assert json.dumps(payload)


def test_the_sprite_payload_describes_every_row_of_the_sheet(installed):
    payload = sprite_payload(installed["slug"])

    assert payload["framesByRow"] == {row.key: row.frames for row in SPEC.rows()}
    assert payload["stateRows"] == [row.key for row in SPEC.rows()]
    assert [row["row"] for row in payload["rows"]] == [row.index for row in SPEC.rows()]
    assert (payload["frameW"], payload["frameH"]) == (SPEC.frame_w, SPEC.frame_h)
    assert len(payload["framesByRow"]) * payload["frameH"] == SPEC.sheet_size()[1]


def test_the_sprite_payload_carries_the_direction_scheme_instead_of_implying_it(installed):
    payload = sprite_payload(installed["slug"])

    assert payload["directions"] == {
        "order": list(SPEC.scheme.order),
        "authored": list(SPEC.scheme.authored),
        "mirrored": dict(SPEC.scheme.mirrored),
    }
    assert payload["states"] == [
        {"name": state.name, "frames": state.frames, "directional": state.directional}
        for state in SPEC.states
    ]
    # Characters carry true per-row counts; the pet payload's capped
    # framesPerState/framesByState would misdescribe a directional sheet.
    assert "framesPerState" not in payload
    assert "framesByState" not in payload


def test_the_sprite_payload_for_an_uninstalled_slug_says_so(installed):
    with pytest.raises(FileNotFoundError, match="is not installed"):
        sprite_payload("no-such-character")


def test_an_installed_character_without_its_sheet_is_reported_separately(home):
    directory = characters_dir() / "ghost"
    directory.mkdir(parents=True)
    (directory / MANIFEST_FILENAME).write_text(
        json.dumps({"slug": "ghost", "displayName": "Ghost", "spec": spec_to_dict(SPEC)}),
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="has no sheet"):
        sprite_payload("ghost")
def test_sprite_payload_state_rows_are_hyphen_keyed_and_front_first(installed):
    """The payload the launcher actually receives, at the join.

    Everything above proves the spec objects; this proves the JSON. The launcher
    reads ``stateRows`` and hands each entry to
    ``AvatarSpriteSheet._deriveDirectionSectors``, so the hyphen separator and
    the front-first row order have to survive compose, install and serialisation
    — not merely hold in ``SheetSpec.rows()``.
    """
    payload = sprite_payload(installed["slug"])

    assert payload["stateRows"][0] == "idle-s"
    assert all("@" not in key for key in payload["framesByRow"])
    assert payload["stateRows"] == [row["key"] for row in payload["rows"]]
    assert payload["directions"]["order"][0] == "s"


def test_sprite_payload_state_rows_are_authored_only_and_front_first(installed):
    """Ruling 3-B where the launcher reads it: the rows, and the pixels behind them.

    The installed spec is FOUR_WAY (authored `s, e, n`, `w` flipped from `e`), so
    a mirror-baking compose would ship eight rows and every `-w` key would be in
    `stateRows`. The row list is transcribed here rather than derived from `SPEC`,
    and it is checked against the DECODED sheet height as well, because a payload
    that merely under-reports its rows would be a worse bug than a baked sheet.
    """
    payload = sprite_payload(installed["slug"])

    assert payload["stateRows"] == [
        "idle-s", "idle-e", "idle-n", "walk-s", "walk-e", "walk-n",
    ]
    assert set(payload["framesByRow"]) == set(payload["stateRows"])
    assert [row["row"] for row in payload["rows"]] == [0, 1, 2, 3, 4, 5]
    assert not any(key.endswith("-w") for key in payload["stateRows"])
    assert "w" in payload["directions"]["mirrored"]

    with Image.open(characters_dir() / installed["slug"] / SHEET_FILENAME) as opened:
        assert opened.size[1] == len(payload["stateRows"]) * payload["frameH"]


def test_directions_mirrored_still_names_the_three_runtime_flips(installed):
    """The map outlives the baked rows, because the CONSUMER does the flipping now.

    The launcher derives its sector count from the row NAMES —
    `AvatarSpriteSheet._deriveDirectionSectors` mirror-closes the set it collects
    — so an authored-only sheet resolves all eight sectors without this map. The
    map is metadata for whoever authors a sheet (Studio): it names the three
    directions no artist is ever asked to draw. Dropping it along with the baked
    rows would have been the easy over-reach.
    """
    payload = sprite_payload(installed["slug"])

    assert payload["directions"] == {
        "order": ["s", "e", "n", "w"],
        "authored": ["s", "e", "n"],
        "mirrored": {"w": "e"},
    }
    # CHAR8's three, transcribed from the launcher SPEC section A.1
    # (`CharacterFacingSector.mirrored`: e<->w, se<->sw, ne<->nw).
    assert EIGHT_WAY.mirrored == {"nw": "ne", "w": "e", "sw": "se"}
    assert {row.key for row in CHAR8.rows()} & {
        f"{state}-{direction}"
        for state in ("idle", "walk")
        for direction in ("nw", "w", "sw")
    } == set()
