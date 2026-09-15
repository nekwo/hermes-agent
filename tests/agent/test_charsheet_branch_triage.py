"""The cases that reach the charsheet package's one-armed branches.

WHERE THIS LIST CAME FROM
-------------------------
`scripts/unreachable_branch_report.py` traces the charsheet pipeline suite under
branch coverage and splits arcs off never-executed lines (`cold`: nothing calls
the code) from arcs off lines that DID run and never took one side
(`one-armed`: the predicate was evaluated, over and over, and that arm never
once won). Its first run reported 46 of the second kind over a green suite, and
nobody had triaged them. This file is the triage's green half: one test per
branch the report named, each reaching exactly the arm no fixture reached, with
the arc it closes in its name and its docstring.

The report's own caveat is why these live in `tests/agent/test_charsheet_*.py`
and not somewhere private: "unreachable" there means *unreached by the suite the
run traced*, and that suite is exactly this glob. A case parked outside it would
close a branch the report keeps reporting.

WHAT IS NOT HERE, AND WHY
-------------------------
Three of the 46 could not be given a case and were answered in the code instead
(the standing answer for a branch no reachable input distinguishes), and the
draft-lock pair are genuine races that a unit test can only fake. The full
verdict table — arc by arc, including the ones this file does not touch — is in
`docs/agent-runtime-harness/planned/w16-ha-field-notes-2026-09-05.md`.

A note on shape: these are DELIBERATELY small and direct. A guard clause's case
is the bad argument, and dressing it up in a full draft would hide which arc it
is about.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent.charsheet import palette as palette_mod
from agent.charsheet import pipeline
from agent.charsheet import prompts as prompts_mod
from agent.charsheet.draft import (
    DRAFT_FILENAME,
    DRAFTS_DIRNAME,
    CharacterDraft,
    _handedness_accepted,
    _migration_entry_id,
    migrate_characters_home,
    stamp_recorded_home,
)
from agent.charsheet.revisions import ImageRevisionStore
from agent.charsheet.spec import FOUR_WAY, SheetSpec, StateSpec, parse_directions

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Never let a guard's case reach the operator's real library."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def _rgba(color=(10, 20, 30, 255), size=(8, 8)):
    return Image.new("RGBA", size, color)


# ───────────────────────────── palette.py ─────────────────────────────


def test_palette_sources_may_be_paths_not_only_images(tmp_path):
    """`palette.py:56→57` — `_as_rgba`'s path arm.

    `build_palette`'s docstring says *images are RGBA images or paths*, and
    every fixture passed images, so the documented half of the contract had
    never been executed. A path is what the real caller uses: `compose` hands
    `build_palette` the approved turnaround references off disk.
    """
    source = tmp_path / "ref.png"
    _rgba((200, 40, 60, 255), (16, 16)).save(source)

    from_path = palette_mod.build_palette([source])
    from_str = palette_mod.build_palette([str(source)])
    from_image = palette_mod.build_palette([_rgba((200, 40, 60, 255), (16, 16))])

    assert from_path.mode == "P"
    # The same pixels by either door, so the path arm is not a second policy.
    assert palette_mod.palette_colors(from_path) == palette_mod.palette_colors(from_image)
    assert palette_mod.palette_colors(from_str) == palette_mod.palette_colors(from_image)


@pytest.mark.parametrize("bad", [True, False, "48", 48.0, None])
def test_build_palette_refuses_a_max_colors_that_is_not_an_int(bad):
    """`palette.py:75→76` — the type guard, including the `bool` clause.

    `bool` is an `int` subclass, so `isinstance(True, int)` is true and only the
    explicit second clause catches `max_colors=True` — which would otherwise
    reach `quantize(colors=True)`, i.e. a one-colour palette, silently.
    """
    with pytest.raises(ValueError, match="max_colors must be an int"):
        palette_mod.build_palette([_rgba()], max_colors=bad)


@pytest.mark.parametrize("bad", [1, 0, -1, 257, 1000])
def test_build_palette_refuses_a_max_colors_outside_pillows_range(bad):
    """`palette.py:77→78` — the range guard.

    Pillow's own failure for an out-of-range palette size is neither the same
    message nor the same exception type, and the plan's 48 sits inside a band
    this guard is the only statement of.
    """
    with pytest.raises(ValueError, match=r"max_colors must be 2\.\.256"):
        palette_mod.build_palette([_rgba()], max_colors=bad)


def test_build_palette_refuses_an_empty_source_list():
    """`palette.py:81→82` — no images at all.

    Reachable from the product: `compose` builds the source list by walking the
    approved directions, and a scheme whose authored set is empty hands this
    function `[]`.
    """
    with pytest.raises(ValueError, match="at least one image"):
        palette_mod.build_palette([])


def test_build_palette_refuses_sources_with_no_opaque_pixels():
    """`palette.py:100→101` — every pixel at or below `ALPHA_FLOOR`.

    The real case is a reference whose subject was keyed out entirely: the
    quantizer would otherwise be handed zero votes and Pillow's error would name
    a sample image the operator never saw.
    """
    transparent = Image.new("RGBA", (8, 8), (255, 0, 0, palette_mod.ALPHA_FLOOR))
    with pytest.raises(ValueError, match="no opaque pixels"):
        palette_mod.build_palette([transparent])


@pytest.mark.parametrize("mode", ["RGBA", "RGB", "L"])
def test_palette_colors_refuses_an_image_that_is_not_p_mode(mode):
    """`palette.py:125→126` — the mode guard on the reader.

    Its twin at `lock_to_palette` had the same hole. Both exist because the
    caller's mistake is passing the reference IMAGE where the built palette
    belongs, and Pillow's own answer for that is an empty list rather than an
    error — a silently empty colour table, which is the failure this whole
    module was built to make impossible.
    """
    with pytest.raises(ValueError, match="expected a 'P'-mode palette image"):
        palette_mod.palette_colors(Image.new(mode, (4, 4)))


def test_palette_colors_answers_the_same_list_with_or_without_assigned_slots():
    """`palette.py:130` — the branch this lane DELETED, and why that is safe.

    The w15 field note named the false arm of `if used:` the clearest deletion
    candidate on the theory that every palette here comes out of
    `Image.quantize`, which always populates `.palette.colors`. Re-measured, the
    reason is different and stronger: `getpalette()` reads out of the SAME
    palette object as `.colors`, so palette BYTES imply a non-empty `.colors`
    and no bytes imply an empty `triples` — the two arms returned the identical
    list for every reachable image, and the false one only ever answered `[]`
    the long way round. `used` now defaults to `{}` and the filter is
    unconditional.

    Both inputs are asserted here so the equivalence is a measurement rather
    than a claim in a commit message.
    """
    # No palette data at all: the arm that used to skip the filter.
    unassigned = Image.new("P", (4, 4))
    assert unassigned.palette.colors == {}
    assert unassigned.getpalette() in (None, [])
    assert palette_mod.palette_colors(unassigned) == []

    # Assigned: the filter still drops Pillow's padding.
    quantized = palette_mod.build_palette([_rgba((7, 9, 11, 255))], max_colors=2)
    triples = palette_mod.palette_colors(quantized)
    assert triples and len(triples) < 256
    assert set(triples) == set(quantized.palette.colors)


@pytest.mark.parametrize("mode", ["RGBA", "RGB", "L"])
def test_lock_to_palette_refuses_a_palette_that_is_not_p_mode(mode):
    """`palette.py:193→194` — the palette argument's mode guard.

    Without it Pillow quantizes against a non-palette image with a message that
    names neither argument, and the caller's mistake (passing the reference
    IMAGE where the built palette belongs) is exactly this shape.
    """
    with pytest.raises(ValueError, match="must be a 'P'-mode image"):
        palette_mod.lock_to_palette(_rgba(), Image.new(mode, (4, 4)))


# ───────────────────────────── prompts.py ─────────────────────────────


@pytest.mark.parametrize("bad", ["Not A State", "9lives", "", "   ", "idle!"])
def test_state_action_for_refuses_a_token_that_cannot_become_a_row_key(bad):
    """`prompts.py:164→165` — the invalid-state raise.

    `--states` admits states this module has never heard of, so the generic
    fallback below this guard is the common path and every fixture took it. The
    guard is the line between "a state I have no tuned language for" and "a
    string that cannot be a row key at all".
    """
    with pytest.raises(ValueError, match="invalid character state"):
        prompts_mod.state_action_for(bad)


def test_a_turnaround_prompt_refuses_a_direction_with_no_camera_view():
    """`prompts.py:231→232` — `_view_of`'s unknown-direction raise.

    Reached through the public builder rather than the private helper, because
    the case that matters is an authored scheme carrying a token the view
    language has never been taught.
    """
    with pytest.raises(ValueError, match="unknown direction"):
        prompts_mod.build_turnaround_prompt("an arrow knight", ("s", "up"))


def test_a_turnaround_prompt_refuses_zero_directions():
    """`prompts.py:288→289` — the empty-strip guard.

    Without it the builder emits a numbered slot list with no slots, which the
    model answers with an arbitrary single pose that then fails strip
    extraction far downstream.
    """
    with pytest.raises(ValueError, match="zero directions"):
        prompts_mod.build_turnaround_prompt("an arrow knight", ())


# ───────────────────────────── spec.py ─────────────────────────────


def test_parse_directions_names_the_missing_flag_rather_than_crashing():
    """`spec.py:460→461` — `--directions` absent.

    `None` reaches here whenever a caller forwards an unset flag straight
    through; the guard is what turns an `AttributeError` deep in `strip()` into
    a message naming the flag and its two legal values.
    """
    with pytest.raises(ValueError, match="--directions is missing"):
        parse_directions(None)


# ───────────────────────────── revisions.py ─────────────────────────────


def test_a_revision_store_whose_root_vanished_answers_no_keys(tmp_path):
    """`revisions.py:111→112` — `keys()` after the root is gone.

    Re-measured before writing: the constructor `mkdir`s the root, so "before
    anything was written" does NOT reach this arm — the root always exists by
    the time a caller holds a store. What reaches it is the root disappearing
    UNDER a live store, which is exactly what `characters delete` does to a
    draft whose `CharacterDraft` object another frame is still holding. The
    reader must answer `[]` rather than raise `FileNotFoundError` out of
    `iterdir`.
    """
    root = tmp_path / "vanishing"
    store = ImageRevisionStore(root)
    assert root.is_dir()  # the constructor made it — hence the re-measure

    shutil.rmtree(root)

    assert store.keys() == []


# ───────────────────────────── draft.py ─────────────────────────────


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_draft_file_that_is_not_an_object_is_refused_by_every_reader(tmp_path):
    """`draft.py:192→193`, `213→214`, `637→638` — the three `not a dict` arms.

    One case, three readers, because they are one fact about the same file: a
    `draft.json` holding a JSON *list* parses fine and then answers `.get` with
    an `AttributeError` several frames away. Each reader has its own answer —
    the stamper refuses to write, the migration falls back to the leaf name, the
    loader raises "corrupt" naming the path — and none of the three had a case.
    """
    directory = tmp_path / "drafts" / "chara_x"
    _write(directory / DRAFT_FILENAME, ["not", "an", "object"])

    assert stamp_recorded_home(directory, "profiles/base") is False
    # The file is left exactly as found: a refusal is not a rewrite.
    assert json.loads((directory / DRAFT_FILENAME).read_text(encoding="utf-8")) == [
        "not",
        "an",
        "object",
    ]
    assert _migration_entry_id(directory) == "chara_x"


def test_loading_a_draft_whose_file_is_not_an_object_names_the_path(tmp_path, monkeypatch):
    """`draft.py:637→638` — `CharacterDraft.load`'s object guard."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    from agent.charsheet.draft import drafts_dir

    _write(drafts_dir() / "chara_y" / DRAFT_FILENAME, [1, 2, 3])

    with pytest.raises(ValueError, match="corrupt draft file"):
        CharacterDraft.load("chara_y")


def test_migration_skips_a_drafts_child_that_is_not_a_draft(tmp_path):
    """`draft.py:297→298` — the `continue` in the drafts walk.

    Two shapes reach it and both exist on the live store: a loose FILE beside
    the draft directories (`.DS_Store`, a stray export), and a directory with no
    `draft.json` (an interrupted create). Neither may be swept into the library,
    and neither may abort the entries around it.
    """
    source = tmp_path / "legacy"
    destination = tmp_path / "library"
    (source / DRAFTS_DIRNAME).mkdir(parents=True)
    (source / DRAFTS_DIRNAME / "stray.txt").write_text("not a draft", encoding="utf-8")
    (source / DRAFTS_DIRNAME / "half-made").mkdir()
    _write(
        source / DRAFTS_DIRNAME / "chara_real" / DRAFT_FILENAME,
        {"id": "chara_real", "hermes_home": "profiles/base"},
    )

    receipt = migrate_characters_home(source, destination, source_home="profiles/base")

    assert [row["id"] for row in receipt["moved"]] == ["chara_real"]
    # Left where they are, and not reported as refusals either: they were never
    # entries.
    assert (source / DRAFTS_DIRNAME / "stray.txt").is_file()
    assert (source / DRAFTS_DIRNAME / "half-made").is_dir()
    assert receipt["skipped"] == []


def test_migration_survives_an_installed_manifest_that_is_not_an_object(tmp_path):
    """`draft.py:330→331` — the installed arm's `not a dict` fallback.

    A `character.json` holding a list is still an installed character by the
    definition the CLI uses (the file exists), so the entry must MOVE — with the
    slug falling back to the directory name — rather than take the store down
    over one unreadable manifest.
    """
    source = tmp_path / "legacy"
    destination = tmp_path / "library"
    _write(source / "arrow-knight" / "character.json", ["not", "an", "object"])

    receipt = migrate_characters_home(source, destination, source_home="profiles/base")

    assert [row["slug"] for row in receipt["moved"]] == ["arrow-knight"]
    assert (destination / "arrow-knight" / "character.json").is_file()


def test_setting_a_stage_the_machine_does_not_know_is_refused_before_the_write(tmp_path):
    """`draft.py:828→829` — `_set_stage`'s vocabulary guard.

    Every verb passes a literal from `STAGES`, so the guard's only cause is a
    future verb spelling a stage wrong — and the cost of it not firing is a
    `draft.json` on disk carrying a stage nothing can advance from, written
    before anyone notices. Asserted with the file too, because the point of the
    guard is that it raises BEFORE `_save`.
    """
    directory = tmp_path / "chara_z"
    directory.mkdir()
    draft = CharacterDraft(directory, {"id": "chara_z", "stage": "turnaround"})

    with pytest.raises(ValueError, match="unknown stage"):
        draft._set_stage("composing")

    assert draft._data["stage"] == "turnaround"
    assert not (directory / DRAFT_FILENAME).exists()


def test_handedness_accepted_tolerates_a_non_list_and_the_round_two_spelling():
    """`draft.py:1985→1986` and `1989→1998` — the back-compat arms.

    Deliberate tolerance with no fixture, and the population it protects is
    installed characters ON DISK: a character installed by the ROUND-TWO build
    recorded `handednessAccepted` as a bare list of row keys, and one written by
    an older build can carry a non-list entirely. `sprite_payload` reads this on
    every `characters` call, so raising here takes the whole sprite down over a
    provenance field — which is exactly the `handednessAccepted` incident that
    put a contract dump between these two repos.
    """
    # 1985: not a list at all — the whole field is dropped.
    assert _handedness_accepted({"handednessAccepted": {"idle_s": True}}) == []
    assert _handedness_accepted({"handednessAccepted": "idle_s"}) == []
    assert _handedness_accepted({}) == []

    # 1989 FALSE arm: a list whose entries are the ROUND-TWO bare row keys.
    # Re-measured before asserting — these are NORMALISED, not dropped: the row
    # survives and the two facts round two never recorded are spelled as such.
    assert _handedness_accepted({"handednessAccepted": ["idle_s", "walk_n"]}) == [
        {"row": "idle_s", "gain": 0.0, "basis": "unrecorded"},
        {"row": "walk_n", "gain": 0.0, "basis": "unrecorded"},
    ]

    # And a mixed manifest — one build's spelling beside the other's — comes
    # back as one uniform list, which is the shape the payload publishes.
    assert _handedness_accepted(
        {"handednessAccepted": [{"row": "idle_s", "gain": 1.5, "basis": "rotation"}, "walk_n"]}
    ) == [
        {"row": "idle_s", "gain": 1.5, "basis": "rotation"},
        {"row": "walk_n", "gain": 0.0, "basis": "unrecorded"},
    ]


# ───────────────────────────── pipeline.py ─────────────────────────────


@pytest.mark.parametrize("bad", [0, -1, True, False, "4", 4.0, None])
def test_frame_cell_refuses_a_frame_count_that_is_not_a_positive_int(bad):
    """`pipeline.py:481→482` — the `frames` type/range guard.

    `bool` is an `int` subclass, so `frames=True` would otherwise mean "one
    frame" and quietly return the whole strip as cell 0 for a caller who meant
    to pass a count.
    """
    with pytest.raises(ValueError, match="frames must be an integer >= 1"):
        pipeline.frame_cell(Image.new("RGBA", (64, 16)), frame=0, frames=bad)


def test_frame_cell_refuses_a_strip_too_narrow_for_the_frame_count():
    """`pipeline.py:492→493` — a slice with no width.

    Reachable from the product: `extract_strip_frames` divides whatever the
    provider returned by the AUTHORED count, and a model that answered with a
    thumbnail instead of a strip lands here. Without the guard the cell comes
    back as a 0-px image and fails much later, in the composer.
    """
    with pytest.raises(ValueError, match="cannot be split into 8 frame"):
        pipeline.frame_cell(Image.new("RGBA", (4, 16), (1, 2, 3, 255)), frame=3, frames=8)


def test_face_offset_is_none_when_the_subject_is_too_faint_to_weigh():
    """`pipeline.py:521→522` and `559→560` — the empty alpha profile.

    `getbbox()` on RGBA is alpha-only, so a non-`None` box guarantees SOME
    alpha — which is why the two guards below it look unreachable. They are not:
    the profile is the alpha channel resized to one row, so a subject whose only
    surviving pixels sit at alpha 1 across a tall crop averages to zero under
    that downsample and weighs nothing. That is a real reference: a
    near-magenta character mostly consumed by `remove_background`'s key.

    `None` and `0.0` are different answers here — "there is nothing to measure"
    versus "it faces straight at you" — and this is the arm that says the first.
    """
    faint = Image.new("RGBA", (4, 600), (0, 0, 0, 0))
    faint.putpixel((1, 0), (10, 20, 30, 1))
    faint.putpixel((1, 599), (10, 20, 30, 1))

    assert faint.getbbox() is not None
    assert pipeline._column_centroid(faint.crop(faint.getbbox())) is None
    assert pipeline.face_offset(faint) is None


def test_face_offset_is_none_when_the_body_weighs_but_the_head_band_does_not():
    """`pipeline.py:563→564` — the head-band arm, distinct from the body's.

    Same mechanism one crop deeper: the body profile spans the whole subject and
    survives, the `FACE_BAND` slice off the top holds only the faint pixel that
    set the bbox and does not. Asserted separately from the body arm because a
    single "returns None" case cannot tell the two guards apart.
    """
    top_heavy = Image.new("RGBA", (4, 300), (0, 0, 0, 0))
    top_heavy.putpixel((1, 0), (10, 20, 30, 1))
    top_heavy.putpixel((1, 299), (10, 20, 30, 255))

    box = top_heavy.getbbox()
    subject = top_heavy.crop(box)
    band = max(1, round((box[3] - box[1]) * pipeline.FACE_BAND))
    # The body DOES weigh — so this is the head guard and not the body one.
    assert pipeline._column_centroid(subject) is not None
    assert pipeline._column_centroid(subject.crop((0, 0, box[2] - box[0], band))) is None
    assert pipeline.face_offset(top_heavy) is None


def test_upscale_on_backdrop_keys_the_chroma_field_out_by_default():
    """`pipeline.py:637→639` — the `chroma_key is not None` arm.

    The parameter DEFAULTS to `MAGENTA` and every fixture passed `None`, so the
    documented first step of the §F.2 looking procedure had never run. It is the
    step the procedure exists for: everything this package generates arrives on
    a full-bleed magenta field at alpha 255, and compositing that over the
    backdrop replaces the backdrop pixel for pixel.
    """
    on_field = Image.new("RGBA", (8, 8), (*pipeline.MAGENTA, 255))

    keyed = pipeline.upscale_on_backdrop(on_field, scale=2)
    unkeyed = pipeline.upscale_on_backdrop(on_field, scale=2, chroma_key=None)

    assert keyed.size == unkeyed.size == (16, 16)
    # Keyed: the field is gone and the backdrop shows. Unkeyed: magenta wins.
    assert keyed.getpixel((0, 0))[:3] == tuple(pipeline.QA_BACKDROP)[:3]
    assert unkeyed.getpixel((0, 0))[:3] == pipeline.MAGENTA


def test_pad_to_square_refuses_a_crop_whose_square_blows_the_write_budget():
    """`pipeline.py:673→674` — the square's own pixel budget.

    The budget is on the SQUARE, not the crop: a wide, short strip is small and
    its square is enormous, so the check upstream on the source cannot catch
    this and the message has to name the square's own dimensions.
    """
    with pytest.raises(ValueError, match="crop square would write"):
        pipeline.pad_to_square(Image.new("RGBA", (5000, 10), (1, 2, 3, 255)))


def test_pad_to_square_returns_an_already_square_crop_unchanged():
    """`pipeline.py:679→680` — the early return.

    Not a guard but the cheap path, and it had no case: an already-square crop
    must not be composited onto a backdrop, because that would replace its
    transparent margins with the QA ground.
    """
    square = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
    square.putpixel((8, 8), (10, 200, 10, 255))

    padded = pipeline.pad_to_square(square)

    assert padded.size == (16, 16)
    # Still transparent where it was: no backdrop was composited under it.
    assert padded.getpixel((0, 0)) == (0, 0, 0, 0)


def test_turnaround_order_refuses_a_view_language_with_no_front_view(monkeypatch):
    """`pipeline.py:695→696` — the self-consistency guard on `VIEW_LANGUAGE`.

    The trap asserted before the repair, which is the standing answer for a
    guard whose only reachable cause is a future edit one module over: the whole
    turnaround order is defined by rotating the ring to start at the front view,
    and a `VIEW_LANGUAGE` that lost that key would silently reorder every strip
    rather than fail.
    """
    without_front = {
        key: value
        for key, value in prompts_mod.VIEW_LANGUAGE.items()
        if key != pipeline.NON_DIRECTIONAL_VIEW
    }
    monkeypatch.setattr(prompts_mod, "VIEW_LANGUAGE", without_front)

    with pytest.raises(ValueError, match="has no 's' entry"):
        pipeline.turnaround_order(("s", "e"))


def test_the_seam_measure_refuses_two_rows_with_no_frames():
    """`pipeline.py:1131→1132` — nothing to pair.

    Reached whenever the mirror detector is asked about a row the extractor
    produced no cells for; without the guard the loop below divides by a zero
    pair count.
    """
    with pytest.raises(ValueError, match="no frames"):
        pipeline._seam_distance([], [], window=4)


def test_the_handedness_summary_omits_the_unjudged_clause_when_every_row_was_judged():
    """`pipeline.py:2227→2229` — the arm that SKIPS the unjudged clause.

    The one-armed arm is the false one: every fixture left something unjudged,
    so the summary had never once been produced for a sheet the check answered
    completely. And "unjudged" here means *no pass answered for it* — a row the
    rotation judged is not reported as unjudged just because the cross-state
    pass had only one state to work with, which is exactly the shape below and
    the reason the set difference is there at all.
    """
    fully_judged = pipeline.handedness_summary(
        {
            "judged": [{"row": "idle_s"}, {"row": "walk_n"}],
            "flagged": [],
            "accepted": [],
            # The cross-state pass could not answer for rows the rotation DID.
            "unjudged": [{"rows": ["idle_s", "walk_n"], "reason": "one state only"}],
        }
    )
    assert fully_judged == "handedness: 2 row(s) judged"

    # The true arm, for contrast: a row no pass answered for is still named.
    partly_judged = pipeline.handedness_summary(
        {
            "judged": [{"row": "idle_s"}],
            "flagged": [],
            "accepted": [],
            "unjudged": [{"rows": ["idle_s", "walk_n"], "reason": "one state only"}],
        }
    )
    assert partly_judged == "handedness: 1 row(s) judged, 1 unjudged (walk_n)"


def test_two_isolated_flagged_rows_are_two_findings_not_one_run():
    """`pipeline.py:1301→1302` — the gap between two over-threshold rows.

    Every fixture's flagged rows were contiguous, so the run was never CLOSED by
    a gap — only by the end of the list below the loop. The difference is the
    whole point of grouping: two adjacent flagged rows raised each other and
    cannot be taken apart (one unattributed "run" finding), while two isolated
    ones have nothing to be confused with and are each named. Both are asserted
    here, because the arm is only meaningful against its opposite.
    """

    def scored(row, gain, direction):
        return {"row": row, "gain": gain, "direction": direction, "state": "idle"}

    over = pipeline.MIRROR_GAIN_THRESHOLD * 2

    isolated = pipeline._run_findings(
        [(0, scored("idle-s", over, "s")), (2, scored("idle-n", over, "n"))], {}, set()
    )
    adjacent = pipeline._run_findings(
        [(0, scored("idle-s", over, "s")), (1, scored("idle-e", over, "e"))], {}, set()
    )

    assert [(f["row"], f["attribution"]) for f in isolated] == [
        ("idle-s", "rotation"),
        ("idle-n", "rotation"),
    ]
    assert [(f["row"], f["attribution"]) for f in adjacent] == [("idle-s", "run")]


# The two sheets below are PAINTED, not composed: the geometry checks read
# nothing but each cell's bounding box, so a rectangle per frame is the whole
# input they need and a full provider run would only make the case slower and
# harder to read.

_TRIAGE_SPEC = SheetSpec(
    states=(StateSpec("idle", 2, True), StateSpec("walk", 3, True)),
    scheme=FOUR_WAY,
)


def _painted_sheet(cells):
    """A sheet with one opaque rectangle per `(row, frame, width, height)`."""
    sheet = Image.new("RGBA", _TRIAGE_SPEC.sheet_size(), (0, 0, 0, 0))
    for row, frame, width, height in cells:
        left = frame * _TRIAGE_SPEC.frame_w + (_TRIAGE_SPEC.frame_w - width) // 2
        top = row * _TRIAGE_SPEC.frame_h + (_TRIAGE_SPEC.frame_h - height) // 2
        for x in range(left, left + width):
            for y in range(top, top + height):
                sheet.putpixel((x, y), (10, 200, 10, 255))
    return sheet


def test_a_sheet_of_tiny_sprites_is_refused_and_a_single_frame_row_is_skipped():
    """`pipeline.py:2385→2386` and `2392→2393`, in one sheet.

    The collapse FLOOR (`2385`) exists because `normalize_cells` shares one
    scale across every row, so one degenerate row shrinks the whole character
    while every cell still passes a non-empty check. The floor is absolute — the
    per-row comparison below it is relative and cannot see a sheet that is
    uniformly too small.

    `2392` is the other arm: a row carrying ONE box has no median to compare
    against itself, and the per-row checks must skip it rather than divide a
    one-element sample. Painting a single frame in a single row produces both at
    once.
    """
    verdict = pipeline.validate_sheet(_TRIAGE_SPEC, _painted_sheet([(0, 0, 20, 20)]))

    assert not verdict["ok"]
    assert any("too small after normalization" in error for error in verdict["errors"])
    # And nothing about a per-row median: the one-box row was skipped.
    assert not any("appears collapsed" in error for error in verdict["errors"])


def test_a_wide_outlier_and_a_shrunken_row_are_both_named():
    """`pipeline.py:2398→2399` and `2409→2412` — the two per-row geometry errors.

    `2398` is the multi-pose frame the extractor missed: one cell far wider than
    its row's median and no taller, which is two poses sharing a slot rather
    than one drawn large. `2409` is the collapse comparison the floor above
    cannot make — a row whose own median is a fraction of the sheet's, which is
    what a bad row does to a character once one scale is shared across all of
    them.

    `2409` is also the branch this lane's deletion renumbered: it used to sit
    under `if global_med_w and global_med_h:`, a guard no sheet could take.
    """
    cells = []
    # Two full-size states set the sheet median.
    for row in (1, 2):
        cells += [(row, frame, 120, 150) for frame in range(2)]
    for row in (4, 5):
        cells += [(row, frame, 120, 150) for frame in range(3)]
    # A shrunken row, and a row carrying one very wide cell.
    cells += [(0, 0, 30, 40), (0, 1, 30, 40)]
    cells += [(3, 0, 20, 40), (3, 1, 20, 40), (3, 2, 180, 40)]

    verdict = pipeline.validate_sheet(_TRIAGE_SPEC, _painted_sheet(cells))

    assert "row 'walk-s' contains a multi-pose frame outlier" in verdict["errors"]
    assert any(
        error.startswith("row 'idle-s' appears collapsed") for error in verdict["errors"]
    )
    # The full-size rows are not accused of anything.
    assert not any("'walk-e'" in error or "'idle-e'" in error for error in verdict["errors"])


def test_validate_sheet_ignores_a_blank_accept_handedness_token():
    """`pipeline.py:2431→2432` — the empty-token `continue`.

    Reachable straight off the CLI: `--accept-handedness ""` and a trailing
    comma both arrive here as an empty string. Without the skip the empty token
    partitions into an empty row key and is reported as an acceptance of a row
    that does not exist — a complaint about a flag the operator did not really
    pass.
    """
    spec = SheetSpec(
        states=(StateSpec("idle", 2, True), StateSpec("walk", 3, True)),
        scheme=FOUR_WAY,
    )
    blank_sheet = Image.new("RGBA", spec.sheet_size(), (0, 0, 0, 0))

    with_blank = pipeline.validate_sheet(spec, blank_sheet, accept_handedness=("", "   "))
    without = pipeline.validate_sheet(spec, blank_sheet)

    # The blank tokens changed nothing: same errors, no acceptance recorded.
    assert with_blank["errors"] == without["errors"]
    assert with_blank["handedness"]["accepted"] == []
