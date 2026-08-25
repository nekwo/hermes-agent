"""The staged draft machine: what each verb refuses, and what it installs.

Everything runs against a real ``HERMES_HOME`` under ``tmp_path`` with the
provider seam (``pipeline._generate_image``) replaced by a deterministic
draftsman, so the stage transitions, the revision bookkeeping and the installed
manifest/payload are exercised on real files with no network.

The spec under test is two states in 4-way — small on purpose: the stage machine
and the payload are direction-count agnostic, so the cheap scheme proves them.
"""

from __future__ import annotations

import ast
import base64
import json
import math
import re
import warnings
from pathlib import Path

import pytest

from agent.charsheet import pipeline
from agent.charsheet.draft import (
    DEFAULT_THUMB_SCALE,
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
from agent.charsheet.revisions import STATE_FILENAME, ImageRevisionStore
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


# ───────────────────────── adding a state later ─────────────────────────
#
# The owner ask (plan slice A3): put a state on a character that is already
# composed and installed, without re-authoring it and without touching a single
# approved row. `reopen` is the only door back, so every case here runs at stage
# `rows`.

ADDED = StateSpec("jump", 3, True)
GROWN = SheetSpec(states=SPEC.states + (ADDED,), scheme=FOUR_WAY)
ADDED_FIXED = StateSpec("cheer", 4, False)
GROWN_FIXED = SheetSpec(states=SPEC.states + (ADDED_FIXED,), scheme=FOUR_WAY)

# The direction the operator rerolls and then declines, and the attempt they
# keep. Named because two tests below turn on the pair being DIFFERENT files.
REROLLED_DIRECTION = "e"
PREFERRED_ATTEMPT = 0


@pytest.fixture
def fake_grown(tmp_path, monkeypatch):
    """A draftsman that also knows the rows ``add_state`` is about to author.

    Same seam as ``fake``; a superset spec, because the provider builds its row
    lookup once and a row keyed by a state that did not exist at fixture time
    would be an unexpected prefix rather than a picture.
    """
    provider = FakeProvider(GROWN, tmp_path / "generated")
    monkeypatch.setattr(pipeline, "_generate_image", provider)
    return provider


@pytest.fixture
def fake_grown_fixed(tmp_path, monkeypatch):
    """``fake_grown``, but the added state is ``:fixed`` — ONE row, no direction."""
    provider = FakeProvider(GROWN_FIXED, tmp_path / "generated")
    monkeypatch.setattr(pipeline, "_generate_image", provider)
    return provider


def run_to_rows_keeping_the_older_attempt(base, *, direction=REROLLED_DIRECTION):
    """Stage ``rows``, with ONE direction whose approved attempt is not its newest.

    The live operator move: reroll a direction, look at both, prefer the
    ORIGINAL, and pin it with ``approve-direction --attempt 0``. That leaves the
    item at ``approved=0, latest=1`` — the ONLY shape in which "the approved
    reference" and "the newest reference" are different files.

    Every other fixture in this module approves the latest attempt, so
    ``store.current`` and ``store.latest`` answer identically there and a test
    written against either one passes. That is exactly how the anchoring
    assertion below was vacuous until 2026-08-25.
    """
    draft = CharacterDraft.create(concept=CONCEPT, slug=SLUG, spec=SPEC, base_image=base)
    draft.run_turnaround()
    draft.reroll_direction(direction, note="the shoulder line is broken")
    draft.approve_direction(direction, attempt=PREFERRED_ATTEMPT)
    # `approve_all_directions` approves the LATEST of every direction, which
    # would silently undo the divergence this fixture exists to create.
    for other in FOUR_WAY.authored:
        if other != direction:
            draft.approve_direction(other)
    assert draft.stage == "rows"
    return draft


def test_adding_a_state_leaves_every_approved_row_exactly_as_it_was(fake_grown, base):
    draft = run_to_composed(base)
    draft.reopen()
    # A reroll first, so the rows carry an attempt count, an approved index and
    # an operator note -- the three things "touches no approved row" is about.
    draft.reroll_row("walk-e", note="the hem reads as one straight line")
    before = draft.status_payload()["rows"]

    result = draft.add_state("jump:3")

    assert result["state"] == {"name": "jump", "frames": 3, "directional": True}
    assert result["states"] == ["idle", "walk", "jump"]
    assert result["rows"] == [f"jump-{d}" for d in FOUR_WAY.authored]

    status = CharacterDraft.load(draft.id).status_payload()
    for key, item in before.items():
        assert status["rows"][key] == item, key
    for key in result["rows"]:
        assert status["rows"][key]["attempts"] == 0
        assert status["rows"][key]["approved"] is None
        assert status["rows"][key]["current"] is None
    # "Seeded at attempts: 0" is not a placeholder attempt written into the
    # store -- it is what an un-generated row already looks like everywhere else.
    assert status["missing"]["rows"] == result["rows"]
    assert status["pending"]["rows"] == result["rows"]


def test_a_new_state_is_appended_so_no_installed_row_changes_index(fake_grown, base):
    draft = run_to_composed(base)
    installed = json.loads(
        (characters_dir() / draft.slug / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )["rows"]
    draft.reopen()

    draft.add_state("jump:3")

    grown = [
        {
            "row": row.index,
            "state": row.state,
            "direction": row.direction,
            "frames": row.frames,
            "key": row.key,
        }
        for row in CharacterDraft.load(draft.id).spec.rows()
    ]
    # The sheet grows DOWNWARD: every row the installed manifest published keeps
    # its index, so a consumer holding the old manifest still addresses the same
    # pictures.
    assert grown[: len(installed)] == installed
    assert [row["state"] for row in grown[len(installed) :]] == ["jump"] * len(
        FOUR_WAY.authored
    )


def test_the_new_rows_are_anchored_to_the_turnaround_already_approved(fake_grown, base):
    """Anchored to the APPROVED attempt — which is not always the newest one.

    The failure this exists to catch: at ``turnaround`` the operator rerolls one
    direction, prefers the ORIGINAL, and pins it with
    ``approve-direction --attempt 0``. That direction now sits at
    ``approved=0, latest=1``, and a state added later must ground its strip on
    the attempt the operator KEPT — not on the one they rejected, silently.

    So the expectation here is written as an attempt INDEX this test chose
    (``PREFERRED_ATTEMPT``), never as ``store.current(...)``. Asserting against
    ``store.current`` is asserting the expression ``_row_reference`` computes:
    on a fixture where nothing was ever rerolled, ``current`` and ``latest``
    are the same file, so swapping the production call to ``store.latest``
    left the whole suite green (mutation run 2026-08-25, 65 passed).
    """
    draft = run_to_rows_keeping_the_older_attempt(base)
    draft.run_rows()
    store = draft.store
    rerolled = turnaround_item(REROLLED_DIRECTION)
    kept = store.attempt_path(rerolled, PREFERRED_ATTEMPT)
    declined = store.latest(rerolled)
    # The fixture's whole point, asserted rather than assumed: two real files,
    # and the one the operator kept is the older.
    assert kept != declined
    assert (kept.name, declined.name) == ("attempt-1.png", "attempt-2.png")

    added = draft.add_state("jump:3")

    # Generating a new row needs no new approval: it grounds on the reference
    # the operator signed off before the first row was ever drawn.
    result = draft.run_rows(only=added["rows"])
    for key in added["rows"]:
        direction = key.split("-")[-1]
        assert result["rows"][key]["reference"] == str(
            store.attempt_path(turnaround_item(direction), PREFERRED_ATTEMPT)
        )
        assert result["rows"][key]["approved"] is True
    # Spelled out for the one direction where the two answers differ.
    assert result["rows"][f"jump-{REROLLED_DIRECTION}"]["reference"] == str(kept)
    assert result["rows"][f"jump-{REROLLED_DIRECTION}"]["reference"] != str(declined)


def test_a_fixed_state_adds_one_row_and_it_grounds_on_the_base_image(
    fake_grown_fixed, base
):
    """``:fixed`` is advertised, it works, and it never sees a turnaround.

    ``_row_reference`` branches on ``row.direction is None`` and answers
    ``_require_base()`` — the identity anchor, not a direction view.
    ``add_state``'s docstring said "grounded on the turnaround references the
    operator already approved" for every row until 2026-08-25, and no test
    covered a fixed state at all.
    """
    draft = run_to_rows(base)
    draft.run_rows()

    added = draft.add_state("cheer:4:fixed")

    assert added["state"] == {"name": "cheer", "frames": 4, "directional": False}
    # One row, keyed by the bare state name: `row_key(state, None)` is the state.
    assert added["rows"] == ["cheer"]
    assert added["states"] == ["idle", "walk", "cheer"]

    result = draft.run_rows(only=added["rows"])

    reloaded = CharacterDraft.load(draft.id)
    assert result["rows"]["cheer"]["reference"] == str(reloaded.base_image)
    assert result["rows"]["cheer"]["approved"] is True
    # ...and it is emphatically not any turnaround reference.
    store = draft.store
    turnaround_paths = {
        str(store.current(turnaround_item(d))) for d in FOUR_WAY.authored
    }
    assert result["rows"]["cheer"]["reference"] not in turnaround_paths


def test_compose_refuses_until_the_new_states_rows_are_generated(fake_grown, base):
    draft = run_to_composed(base)
    manifest_path = characters_dir() / draft.slug / MANIFEST_FILENAME
    installed_before = manifest_path.read_bytes()
    draft.reopen()
    added = draft.add_state("jump:3")

    with pytest.raises(ValueError) as excinfo:
        draft.compose()

    # Adding a state cannot install a sheet whose new rows are blank; the
    # refusal names every one of them.
    message = str(excinfo.value)
    assert "have no approved strip" in message
    for key in added["rows"]:
        assert key in message
    assert manifest_path.read_bytes() == installed_before
    assert draft.stage == "rows"


def test_the_recomposed_sheet_carries_the_new_state(fake_grown, base):
    draft = run_to_composed(base)
    manifest_path = characters_dir() / draft.slug / MANIFEST_FILENAME
    draft.reopen()
    added = draft.add_state("jump:3")

    draft.run_rows(only=added["rows"])
    composed = draft.compose()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert [state["name"] for state in manifest["spec"]["states"]] == [
        "idle",
        "walk",
        "jump",
    ]
    assert [row["key"] for row in manifest["rows"]] == [row.key for row in GROWN.rows()]
    assert (
        composed["validation"]["width"],
        composed["validation"]["height"],
    ) == GROWN.sheet_size()
    payload = sprite_payload(draft.slug)
    assert [state["name"] for state in payload["states"]] == ["idle", "walk", "jump"]
    assert payload["framesByRow"]["jump-s"] == 3
    assert draft.stage == "composed"


def test_add_state_refuses_a_state_the_sheet_already_has(fake_grown, base):
    draft = run_to_rows(base)

    with pytest.raises(ValueError, match="is already on this sheet"):
        draft.add_state("walk:5")

    assert CharacterDraft.load(draft.id).spec.states == SPEC.states


def test_add_state_takes_one_state_and_not_a_list(fake_grown, base):
    draft = run_to_rows(base)

    with pytest.raises(ValueError, match="takes ONE state"):
        draft.add_state("jump:3,cheer:4:fixed")

    assert CharacterDraft.load(draft.id).spec.states == SPEC.states


@pytest.mark.parametrize(
    "build, stage",
    [
        (
            lambda base: CharacterDraft.create(
                concept=CONCEPT, slug=SLUG, spec=SPEC, base_image=base
            ),
            "turnaround",
        ),
        (run_to_composed, "composed"),
    ],
)
def test_add_state_is_refused_at_every_stage_but_rows(fake_grown, base, build, stage):
    draft = build(base)
    assert draft.stage == stage

    with pytest.raises(ValueError, match="add_state requires draft stage 'rows'"):
        draft.add_state("jump:3")

    assert CharacterDraft.load(draft.id).spec.states == SPEC.states


def test_a_state_below_the_frame_floor_is_refused_before_a_generation_is_spent(
    fake_grown, base
):
    """The trap A2 measured live, closed at the door A3 opens.

    ``start --states idle:1`` built a draft, spent the base anchor and three
    direction generations, and only refused at ``rows`` -- because
    ``spec.parse_states`` accepted 1 while ``prompts`` demanded 2, and no verb
    could change ``--states`` afterwards. ``add-state`` is a second door into
    exactly that trap, so the floor moved to the declaration and the refusal
    lands before anything is generated.
    """
    draft = run_to_rows(base)
    draft.run_rows()
    spent = len(fake_grown.calls)

    with pytest.raises(ValueError, match="out of range"):
        draft.add_state("jump:1")

    assert CharacterDraft.load(draft.id).spec.states == SPEC.states
    assert len(fake_grown.calls) == spent


def test_the_declaration_floor_is_the_number_the_prompt_builder_enforces(
    fake_grown, base
):
    """Two modules, ONE number -- the half of the trap that was a duplicated floor."""
    from agent.charsheet import prompts
    from agent.charsheet.spec import MIN_FRAMES_PER_ROW

    # ONE NUMBER, ONE SOURCE -- pinned at the SOURCE, because it cannot be
    # pinned at runtime. `prompts` binds the constant with `from ... import` at
    # import time, so `prompts.MIN_FRAMES_PER_ROW` is a plain int:
    # `prompts.MIN_FRAMES_PER_ROW is spec.MIN_FRAMES_PER_ROW` is True even when
    # `prompts` re-hardcodes its own `MIN_FRAMES_PER_ROW = 2`, because CPython
    # interns every int in -5..256 and identity cannot tell a shared constant
    # from a re-typed literal (verified 2026-08-25). Monkeypatching `spec`
    # cannot tell them apart either: a `from X import Y` binding does not follow
    # X. So the guarantee this test is NAMED for lives in the source text, and
    # the mutation it must catch -- re-spell the number in `prompts`, drop the
    # import -- is caught here and nowhere else.
    tree = ast.parse(Path(prompts.__file__).read_text(encoding="utf-8"))
    read_from = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "MIN_FRAMES_PER_ROW" for alias in node.names)
    }
    written = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "MIN_FRAMES_PER_ROW"
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    ]
    assert read_from == {"agent.charsheet.spec"}, (
        "prompts must READ the frame floor from spec; it imports it from "
        f"{read_from or 'nowhere'}"
    )
    assert not written, "prompts assigns MIN_FRAMES_PER_ROW -- that is a second number"

    with pytest.raises(ValueError, match=f"at least {MIN_FRAMES_PER_ROW}"):
        prompts.build_directional_row_prompt(
            "jump", "s", MIN_FRAMES_PER_ROW - 1, CONCEPT
        )
    assert prompts.build_directional_row_prompt("jump", "s", MIN_FRAMES_PER_ROW, CONCEPT)

    draft = run_to_rows(base)
    with pytest.raises(ValueError, match="out of range"):
        draft.add_state(f"jump:{MIN_FRAMES_PER_ROW - 1}")
    added = draft.add_state(f"jump:{MIN_FRAMES_PER_ROW}")
    assert added["state"]["frames"] == MIN_FRAMES_PER_ROW


# ──────────────────────────── row crops (looking) ────────────────────────────
#
# FIXTURE RULE for every test below that judges PIXELS: the input must come
# from the pipeline that produces it in production — `generate_row_strip`
# through the fake provider, i.e. `run_rows` / `reroll_row` — never from a
# hand-built PIL image `propose`d straight into the store.
#
# This is not style. A hand-built input is one the author chose to have the
# property under test, so the assertion passes whether or not the code puts it
# there. The A1 review found both consequences at once: a crop test fed a
# TRANSPARENT PNG proved a backdrop composite that is a guaranteed no-op on
# every image the verb can actually receive (row attempts are opaque magenta,
# alpha extrema (255, 255) on all eight live items), and the CLI's
# "the crop must be opaque" assertion survived deleting the composite entirely.
#
# Hand-built inputs stay legitimate in `test_charsheet_revisions.py`, where the
# store genuinely does not care what the bytes are.


def test_a_row_crop_is_named_the_way_the_store_names_the_attempt(fake, base):
    """A filename is a human surface, so it counts the way the store's do.

    `revisions/row@walk-e/attempt-1.png` is the file this crop came from; a
    thumb called `walk-e-a0-x3.png` left an operator correlating the two off by
    one. The payload keeps the 0-based machine truth.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])

    result = draft.row_thumb("walk-e", attempt=0, scale=3)

    out = Path(result["path"])
    assert out.parent == draft.directory / "thumbs"
    assert out.name == "walk-e-attempt-1-frame-1-x3.png"
    assert out.is_file()
    assert draft.store.attempt_path(row_item("walk-e"), 0).name == "attempt-1.png"
    assert (result["attempt"], result["frame"]) == (0, 0)
    assert result["scale"] == 3 and result["row"] == "walk-e"
    with Image.open(out) as crop:
        assert crop.size == (result["width"], result["height"])


def test_the_default_crop_is_one_frame_and_not_the_whole_strip(fake, base):
    """The §F.2 procedure is crop-THEN-upscale, and the crop is the half that
    removes pixels.

    Upscaling a whole row strip is not a crop — it is an enlargement, and at
    card width it resolves no better than the raw attempt while decoding twice
    the installed sheet it exists to avoid. So the default addresses one frame
    cell, sliced by the row's own frame count.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])
    frames = next(row.frames for row in SPEC.authored_rows() if row.key == "walk-e")
    source = draft.store.attempt_path(row_item("walk-e"), 0)
    with Image.open(source) as strip:
        strip_w, strip_h = strip.size

    result = draft.row_thumb("walk-e", scale=2)

    assert result["frames"] == frames
    assert result["height"] == strip_h * 2
    assert result["width"] == round(strip_w / frames) * 2
    assert result["width"] < strip_w, (
        "the crop is wider than the strip it came from — nothing was cropped"
    )
    with Image.open(result["path"]) as crop:
        assert crop.size == (result["width"], result["height"])


def test_each_frame_of_a_strip_crops_to_a_different_picture(fake, base):
    """`--frame` addresses a cell, so two cells are two pictures.

    Within-strip identity is the point of generating a row as one image; what
    differs between cells is the pose, which is exactly what an operator is
    looking at frame by frame.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])

    first = draft.row_thumb("walk-e", frame=0, scale=1)
    second = draft.row_thumb("walk-e", frame=1, scale=1)

    assert first["path"] != second["path"]
    assert Path(first["path"]).name.endswith("-frame-1-x1.png")
    assert Path(second["path"]).name.endswith("-frame-2-x1.png")
    assert Path(first["path"]).read_bytes() != Path(second["path"]).read_bytes()


def test_a_crop_keys_the_chroma_field_out_so_the_dark_backdrop_can_show(fake, base):
    """The §F.2 point, taken on the image the verb actually receives.

    A row strip off the provider is a full-bleed magenta field at alpha 255
    everywhere — `alpha_composite`-ing it over a backdrop replaces every
    backdrop pixel, so a backdrop with nothing keyed out is a step that renders
    and changes nothing. And §F.1's complaint is precisely that a one-pixel seam
    is invisible AGAINST the magenta. Keying is what gives the flat dark ground
    something to show through, and what makes "is it opaque?" a real question.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])
    source = draft.store.attempt_path(row_item("walk-e"), 0)
    with Image.open(source) as strip:
        strip = strip.convert("RGBA")
        # The premise: this input is opaque magenta, like every live attempt.
        assert strip.getchannel("A").getextrema() == (255, 255)
        assert strip.getpixel((0, 0))[:3] == pipeline.MAGENTA

    result = draft.row_thumb("walk-e", attempt=0, scale=1)

    with Image.open(result["path"]) as crop:
        crop = crop.convert("RGBA")
        assert crop.getpixel((0, 0)) == pipeline.QA_BACKDROP, (
            "the chroma field reached the crop: the backdrop never showed"
        )
        assert crop.getchannel("A").getextrema() == (255, 255)
        drawn = {crop.getpixel((x, y))[:3] for x in range(crop.width) for y in range(crop.height)}
        assert drawn - {pipeline.QA_BACKDROP[:3]}, "the art was keyed away with the field"
        assert pipeline.MAGENTA not in drawn


def test_a_crop_is_upscaled_with_nearest_so_a_one_pixel_defect_survives(fake, base):
    """Any smoothing filter averages a one-pixel seam into its neighbours.

    Pinned at a real edge of a real strip rather than at a synthetic pixel: the
    2x crop's block over that edge must be one flat colour equal to the 1x
    crop's pixel. A bilinear/bicubic resize blends there and the set grows.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])

    one = draft.row_thumb("walk-e", frame=0, scale=1)
    two = draft.row_thumb("walk-e", frame=0, scale=2)

    with Image.open(one["path"]) as small, Image.open(two["path"]) as big:
        small = small.convert("RGBA")
        big = big.convert("RGBA")
        edge = next(
            (x, y)
            for y in range(small.height)
            for x in range(small.width - 1)
            if small.getpixel((x, y)) != small.getpixel((x + 1, y))
        )
        x, y = edge
        block = {big.getpixel((2 * x + dx, 2 * y + dy)) for dx in (0, 1) for dy in (0, 1)}
        assert block == {small.getpixel((x, y))}


def test_looking_at_a_row_is_never_out_of_order(fake, base):
    """A composed draft is exactly when the operator goes hunting for a defect."""
    draft = run_to_composed(base)

    result = draft.row_thumb("walk-e")

    assert draft.stage == "composed"
    assert Path(result["path"]).is_file()


@pytest.mark.parametrize(
    "kwargs, error, message",
    [
        ({"row_key": "idle-w"}, ValueError, "is not an authored row"),
        ({"row_key": "sprint-e"}, ValueError, "is not an authored row"),
        ({"row_key": "walk-e", "attempt": 7}, IndexError, "out of range"),
        ({"row_key": "walk-e", "frame": 7}, IndexError, "frame 7 out of range"),
        ({"row_key": "walk-e", "frame": -1}, IndexError, "frame -1 out of range"),
        ({"row_key": "walk-e", "frame": "0"}, ValueError, "frame must be an integer"),
        ({"row_key": "walk-e", "scale": 0}, ValueError, "must be an integer >= 1"),
        ({"row_key": "walk-e", "scale": "2"}, ValueError, "must be an integer"),
    ],
)
def test_a_crop_that_cannot_be_taken_is_refused_with_a_reason(fake, base, kwargs, error, message):
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])

    with pytest.raises(error, match=message):
        draft.row_thumb(**kwargs)


def test_a_crop_budget_is_output_pixels_and_the_refusal_names_the_source(fake, base):
    """`--scale N` alone can never express what a caller cares about.

    A fixed 1-8 ceiling is a count unrelated to the source, so the same factor
    is harmless on one image and a decompression bomb on another: `--scale 8` on
    a live 1536x1024 attempt wrote 12288x8192 = 100_663_296 px, past Pillow's own
    `MAX_IMAGE_PIXELS` — a file this package writes and Pillow then refuses to
    reopen without a `DecompressionBombWarning`, which RAISES under `-W error`.
    The budget is on the output and is checked before the resize, so nothing
    oversized is ever allocated, let alone written.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])
    cell = draft.row_thumb("walk-e", scale=1)
    over = math.ceil(math.sqrt(pipeline.MAX_THUMB_PIXELS / (cell["width"] * cell["height"]))) + 1
    before = sorted(p.name for p in (draft.directory / "thumbs").iterdir())

    with pytest.raises(ValueError) as refusal:
        draft.row_thumb("walk-e", scale=over)

    message = str(refusal.value)
    assert f"{cell['width']}x{cell['height']} source" in message
    assert "budget" in message
    assert sorted(p.name for p in (draft.directory / "thumbs").iterdir()) == before


def test_the_crop_budget_stays_under_pillows_decompression_bomb_threshold(fake, base):
    """The invariant the budget exists for, stated where a raise would break it.

    A crop this package writes must be one a consumer can reopen — including a
    consumer running under `-W error`, where the bomb warning is an exception.
    """
    assert pipeline.MAX_THUMB_PIXELS < Image.MAX_IMAGE_PIXELS

    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])
    result = draft.row_thumb("walk-e", scale=2)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with Image.open(result["path"]) as crop:
            crop.load()


def test_the_card_budget_is_the_sheet_and_not_the_bomb_threshold(fake, base):
    """Two bounds, two values. One number could not be right for both.

    `MAX_THUMB_PIXELS` answers "may this file exist?" and is set against
    Pillow's bomb threshold. Risk D.3 asks a different question — "may a chat
    card decode this?" — so the card budget is SIZED from `CHAR8`'s sheet, the
    largest this package's default spec composes.

    **What it is not, corrected 2026-08-25:** a comparison against the caller's
    own sheet. This docstring used to say the budget was "derived from the spec
    so it cannot drift from the thing it is measured against", and it does drift
    — it is a module constant. An `add-state`-grown sheet measured 1.50x it and
    a `--directions 4`, `idle:2` sheet 13.3x lighter. It is a fixed console
    decode ceiling; see `pipeline.MAX_CARD_PIXELS`.

    The killing mutation is the state this replaced: one constant serving both,
    at which point a crop 3.94x heavier than the sheet passes the only bound
    there is.
    """
    width, height = CHAR8.sheet_size()

    assert pipeline.MAX_CARD_PIXELS == width * height
    assert pipeline.MAX_CARD_PIXELS < pipeline.MAX_THUMB_PIXELS
    assert pipeline.fits_card_budget(width, height)
    assert not pipeline.fits_card_budget(width, height + 1)


def test_a_default_crop_heavier_than_the_sheet_is_refused_rather_than_declared(
    fake, base, tmp_path
):
    """The default crop is the one an agent hands to a card, so it is bounded.

    A hand-sized strip is the stimulus here, not the property: what is under
    test is arithmetic on the source's dimensions, and the only way to reach the
    real budget is a real-sized source. The fake draftsman draws small on
    purpose, which is exactly why this case never appeared on its own.

    A refusal, not a clamp, and it names the escape: `--scale 1` is the same
    pixels without the enlargement, and is what a one-frame row wanted anyway.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])
    frames = next(row.frames for row in SPEC.authored_rows() if row.key == "walk-e")
    # Sized so ONE cell at the default 2x lands just over the sheet's own count.
    big = tmp_path / "oversized-strip.png"
    Image.new("RGBA", (512 * frames, 1600), MAGENTA).save(big)
    draft.store.propose(row_item("walk-e"), big)
    cell_pixels = 512 * 1600
    assert cell_pixels * DEFAULT_THUMB_SCALE**2 > pipeline.MAX_CARD_PIXELS

    with pytest.raises(ValueError) as refusal:
        draft.row_thumb("walk-e")

    message = str(refusal.value)
    assert "card budget" in message
    assert f"{pipeline.MAX_CARD_PIXELS:,}" in message
    assert "--scale 1" in message
    # And it names a FIXED ceiling rather than a comparison it does not make.
    # It read "heavier than the sheet this crop exists to avoid decoding" until
    # 2026-08-25, which is false on exactly the drafts an operator reaches for
    # this verb on: an `add-state`-grown sheet is 1.50x the budget, so the
    # refusal fires for crops lighter than that draft's own sheet.
    assert "heavier than the sheet" not in message
    assert "NOT a comparison" in message
    assert not (draft.directory / "thumbs").exists(), "the refusal wrote a file anyway"
    # ...and the escape the refusal names actually works.
    assert draft.row_thumb("walk-e", scale=1)["cardSafe"] is True


def test_a_deliberate_deep_zoom_is_written_and_labelled_viewer_only(fake, base):
    """Above the default the caller has asked for a zoom, and gets one.

    What they must not get is silence: `--scale 8` on the live draft wrote
    2176x5792 = 12_603_392 px — 3.94x the sheet — at exit 0 with nothing in the
    payload to stop an agent declaring it with a `MEDIA:` line. The file is
    legitimate (the viewer opens it); the claim "this is a card" is not.
    """
    draft = run_to_rows(base)
    draft.run_rows(only=["walk-e"])

    default = draft.row_thumb("walk-e")
    zoomed = draft.row_thumb("walk-e", scale=10)

    assert default["cardSafe"] is True
    assert default["width"] * default["height"] <= pipeline.MAX_CARD_PIXELS
    assert zoomed["cardSafe"] is False
    assert zoomed["width"] * zoomed["height"] > pipeline.MAX_CARD_PIXELS
    assert Path(zoomed["path"]).is_file(), "a viewer artifact is still written"


def test_a_row_with_no_attempt_yet_says_so_instead_of_cropping_nothing(fake, base):
    draft = run_to_rows(base)

    with pytest.raises(ValueError, match="has no attempt to crop yet"):
        draft.row_thumb("walk-e")


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


def test_a_draft_records_who_authored_it_without_that_scoping_where_it_lives(fake, base):
    """`authored_by` is provenance (companion doc §13.6), never a home or a gate."""
    authored = CharacterDraft.create(
        concept=CONCEPT, slug=SLUG, spec=SPEC, base_image=base, authored_by="alice"
    )

    assert authored.authored_by == "alice"
    assert CharacterDraft.load(authored.id).authored_by == "alice"
    assert authored.status_payload()["authoredBy"] == "alice"
    # Same drafts directory as any other draft: the persona names the author, it
    # does not scope the store.
    assert authored.directory.parent == drafts_dir()


def test_an_unattributed_draft_stays_unattributed_rather_than_guessing(draft):
    """Absence is a fact, and it has to survive to the consumer.

    An empty string is a third spelling of "no author" that reads as a value:
    it is what `.get(..., "")` produces for a draft written before the field
    existed AND what a `--authored-by`-less start used to store, so the payload
    collapsed the two and a backfill could select neither. The key is simply not
    written, and the payload says `null`.
    """
    on_disk = json.loads((draft.directory / "draft.json").read_text(encoding="utf-8"))

    assert "authored_by" not in on_disk
    assert draft.authored_by is None
    assert CharacterDraft.load(draft.id).authored_by is None
    assert draft.status_payload()["authoredBy"] is None


def test_the_authored_by_key_is_absent_rather_than_empty_when_it_is_not_given(fake, base):
    """A blank `--authored-by` is not an author; it must not mint an empty one."""
    blank = CharacterDraft.create(
        concept=CONCEPT, slug=SLUG, spec=SPEC, base_image=base, authored_by="   "
    )

    on_disk = json.loads((blank.directory / "draft.json").read_text(encoding="utf-8"))

    assert "authored_by" not in on_disk
    assert blank.authored_by is None


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
    assert item["approvedPath"] is None
    assert item["current"] == str(draft.store.latest(turnaround_item("e")))
    assert item["history"][-1]["note"] == "again"
    assert "e" in status["pending"]["turnaround"]


def test_every_attempt_in_the_status_history_carries_the_file_the_store_persisted(draft):
    """Attempt 2 must be addressable beside attempt 3 — one path per attempt.

    The killing mutation is reporting the item's ``current`` image for every
    entry: the payload still looks populated, and a QA surface silently shows
    the same picture three times.
    """
    draft.run_turnaround()
    draft.reroll_direction("e", note="again")
    draft.reroll_direction("e", note="once more")

    item = draft.status_payload()["turnaround"]["e"]
    paths = [entry["path"] for entry in item["history"]]
    item_dir = draft.store.root / turnaround_item("e")
    recorded = json.loads((item_dir / STATE_FILENAME).read_text(encoding="utf-8"))

    assert len(paths) == 3
    assert len(set(paths)) == 3, "every attempt needs its own path, not the item's"
    # The store's own filenames, not a filename this payload re-derived.
    assert [Path(path).name for path in paths] == [r["file"] for r in recorded["attempts"]]
    assert all(Path(path).parent == item_dir and Path(path).is_file() for path in paths)
    # `current` is one of the attempts (the newest here) — not all of them.
    assert paths.count(item["current"]) == 1


def test_an_attempt_with_no_file_recorded_reports_no_path_rather_than_a_guess(draft):
    """Absence is `None`, and it has exactly one spelling in this payload.

    `""` is not a path. A consumer handed one cannot tell "no image was
    recorded" from any other empty value, and an agent following the
    `MEDIA:<path>` protocol interpolates it into a bare `MEDIA:` line. The store
    already answers `Path | None`; the killing mutation is the `str(x or "")`
    that flattened it at the payload boundary — which is what `authored_by` was
    fixed for while these three fields, in the same response, kept the old
    spelling.
    """
    draft.run_turnaround()
    item_dir = draft.store.root / turnaround_item("e")
    state = json.loads((item_dir / STATE_FILENAME).read_text(encoding="utf-8"))
    state["attempts"][0].pop("file")
    (item_dir / STATE_FILENAME).write_text(json.dumps(state), encoding="utf-8")

    item = draft.status_payload()["turnaround"]["e"]

    assert item["history"][0]["path"] is None
    assert item["current"] is None
    assert item["approvedPath"] is None
    # Every path field in the item agrees: one spelling of "nothing here".
    paths = [item["approvedPath"], item["current"], *(e["path"] for e in item["history"])]
    assert "" not in paths


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
