"""The declared draftsman seam: which door a generation resolves, and when.

RL-26. ``pipeline._generate_image`` is the charsheet package's one provider
door and every other test in this tree replaces it with a ``monkeypatch`` that
only reaches the process doing the patching. This file covers the arm a SPAWNED
process can reach — ``HERMES_CHARSHEET_DRAFTSMAN=fake`` — and, first of all, the
default: with the variable unset the resolution is the real door and nothing
else.

The door is pinned BY IDENTITY, never by calling it. A test that proved "the
real door is in place" by invoking it would be a test that bills.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.charsheet import fake_draftsman, pipeline, prompts
from agent.charsheet.fake_draftsman import (
    DRAFTSMAN_ENV,
    DraftsmanCannotRead,
    FakeDraftsman,
    active_draftsman_name,
    slots_for,
    square_image,
)
from agent.charsheet.spec import EIGHT_WAY, FOUR_WAY, SheetSpec, StateSpec, row_key

pytest.importorskip("PIL")

SPEC = SheetSpec(states=(StateSpec("idle", 2, True), StateSpec("walk", 3, True)), scheme=FOUR_WAY)
CONCEPT = "an arrow knight"


@pytest.fixture(autouse=True)
def _cold_seam(monkeypatch):
    """Every test starts from an unarmed seam and a process that has said nothing.

    The module memoizes two things on purpose — the fake instance (so one
    process draws through one draftsman) and the refusal line (so an ignored
    value is reported once, not once per generation). Both are process state,
    and a test that inherited either would be reading the previous test's.
    """
    monkeypatch.delenv(DRAFTSMAN_ENV, raising=False)
    monkeypatch.setattr(fake_draftsman, "_INSTANCE", None)
    monkeypatch.setattr(fake_draftsman, "_REPORTED", set())
    yield


def turnaround_prompt(directions=EIGHT_WAY.authored) -> str:
    return prompts.build_turnaround_prompt(CONCEPT, tuple(directions))


def row_prompt(state="walk", direction="e", frames=3) -> str:
    return prompts.build_directional_row_prompt(state, direction, frames, CONCEPT)


# ────────────────────────────── which door ──────────────────────────────


def test_with_the_seam_unset_the_resolution_is_the_real_provider_door():
    """The default, and the only assertion in this file that matters most:
    an unarmed runtime resolves ``_generate_image`` itself."""

    assert pipeline._draftsman() is pipeline._generate_image
    assert active_draftsman_name() == ""


def test_with_the_seam_armed_the_resolution_is_the_fake(monkeypatch):
    monkeypatch.setenv(DRAFTSMAN_ENV, "fake")

    door = pipeline._draftsman()

    assert isinstance(door, FakeDraftsman)
    assert door is not pipeline._generate_image
    assert active_draftsman_name() == "fake"


def test_the_environment_is_read_at_call_time_not_at_import(monkeypatch):
    """A serve started without the variable and a child started with it are two
    processes reading two environments. Import-time resolution would freeze
    whichever booted first, and the child e2e turns on this exact property."""

    assert pipeline._draftsman() is pipeline._generate_image
    monkeypatch.setenv(DRAFTSMAN_ENV, "fake")
    assert isinstance(pipeline._draftsman(), FakeDraftsman)
    monkeypatch.delenv(DRAFTSMAN_ENV)
    assert pipeline._draftsman() is pipeline._generate_image


def test_one_process_draws_through_one_draftsman(monkeypatch):
    monkeypatch.setenv(DRAFTSMAN_ENV, "fake")

    assert pipeline._draftsman() is pipeline._draftsman()


@pytest.mark.parametrize("value", ["FAKE", "fake ", "1", "true", "pillow", "real"])
def test_any_other_value_is_ignored_and_the_real_door_stands(monkeypatch, capsys, value):
    """Only the exact word is honoured. ``"fake "`` is in the list on purpose:
    the value is stripped before the comparison, so it IS honoured — every other
    spelling here, including the capitalised one, is not."""

    monkeypatch.setenv(DRAFTSMAN_ENV, value)

    door = pipeline._draftsman()

    if value.strip() == "fake":
        assert isinstance(door, FakeDraftsman)
        assert capsys.readouterr().err == ""
    else:
        assert door is pipeline._generate_image
        assert active_draftsman_name() == ""
        line = capsys.readouterr().err
        assert value in line and "only supported value is 'fake'" in line


def test_an_ignored_value_is_reported_once_per_process(monkeypatch, capsys):
    """One line, not one per generation: a rows batch resolves the door once per
    strip and a stderr line per attempt would bury the run's real output."""

    monkeypatch.setenv(DRAFTSMAN_ENV, "pillow")

    for _ in range(4):
        assert pipeline._draftsman() is pipeline._generate_image

    assert capsys.readouterr().err.count("only supported value") == 1


# ─────────────────────────── what it draws ───────────────────────────


def test_the_same_request_answers_the_same_bytes(tmp_path):
    """Determinism is the property the long-run proof rests on: a batch that
    drew something different on the second run would make every downstream
    assertion about revisions a coin toss."""

    prompt = row_prompt()
    request = dict(
        reference_images=[], aspect_ratio="landscape", prefix=pipeline.row_prefix("walk-e"), provider=None
    )
    one = FakeDraftsman(tmp_path / "one")
    first = one(prompt, **request)
    # The SAME draftsman, later in its own life: nothing about the call index,
    # the clock or the path may reach the picture. A re-roll is a different
    # FILE (a real provider's would be) holding the same bytes.
    again = one(prompt, **request)
    other = FakeDraftsman(tmp_path / "two")(prompt, **request)

    assert len({first, again, other}) == 3
    assert Path(first).read_bytes() == Path(again).read_bytes() == Path(other).read_bytes()


@pytest.mark.parametrize("directions", [FOUR_WAY.authored, EIGHT_WAY.authored])
def test_a_turnaround_carries_one_sliceable_pose_per_authored_direction(tmp_path, directions):
    draftsman = FakeDraftsman(tmp_path / "generated")

    strip = draftsman(
        turnaround_prompt(directions),
        reference_images=[],
        aspect_ratio="landscape",
        prefix=pipeline.PREFIX_TURNAROUND,
        provider=None,
    )

    assert len(pipeline.extract_strip_frames(strip, len(directions), method="components", fit=False)) == len(directions)


@pytest.mark.parametrize("frames", [2, 3, 8, 12])
def test_a_row_carries_every_frame_the_request_asked_for(tmp_path, frames):
    """Slot PITCH, not strip width, is what keeps a wide row sliceable — the
    fixtures this draftsman came from drew a fixed-width strip, which a
    twelve-frame row would have packed until the extractor refused it."""

    draftsman = FakeDraftsman(tmp_path / "generated")

    strip = draftsman(
        row_prompt(frames=frames),
        reference_images=[],
        aspect_ratio="landscape",
        prefix=pipeline.row_prefix("walk-e"),
        provider=None,
    )

    assert len(pipeline.extract_strip_frames(strip, frames, method="components", fit=False)) == frames


def test_a_direction_reroll_is_one_pose_on_a_square_canvas(tmp_path):
    draftsman = FakeDraftsman(tmp_path / "generated")

    view = draftsman(
        prompts.build_direction_view_prompt(CONCEPT, "ne"),
        reference_images=[],
        aspect_ratio="square",
        prefix=pipeline.view_prefix("ne"),
        provider=None,
    )

    from PIL import Image

    with Image.open(view) as image:
        assert image.size == (fake_draftsman.SQUARE_PX, fake_draftsman.SQUARE_PX)


# ─────────────────────────── the prompt anchors ───────────────────────────


def test_the_anchors_read_the_prompts_this_repository_builds():
    """The coupling, pinned. ``slots_for`` recovers what to draw from the built
    prompt, so a re-worded prompt must break HERE — on a fixture — rather than
    as a batch that draws four frames where five were asked for."""

    kind, slots = slots_for(turnaround_prompt(EIGHT_WAY.authored), pipeline.PREFIX_TURNAROUND)
    assert kind == "strip"
    assert [direction for direction, _, _ in slots] == list(EIGHT_WAY.authored)

    kind, slots = slots_for(row_prompt(frames=5), pipeline.row_prefix("walk-e"))
    assert kind == "strip"
    assert slots == [("e", index, 5) for index in range(5)]

    kind, slots = slots_for(
        prompts.build_directional_row_prompt("cheer", pipeline.NON_DIRECTIONAL_VIEW, 4, CONCEPT),
        pipeline.row_prefix(row_key("cheer", None)),
    )
    assert slots == [(pipeline.NON_DIRECTIONAL_VIEW, index, 4) for index in range(4)]

    assert slots_for(prompts.build_direction_view_prompt(CONCEPT, "n"), pipeline.view_prefix("n")) == ("square", "n")


def test_a_prompt_that_lost_its_anchor_refuses_instead_of_guessing():
    stripped = turnaround_prompt().replace("Pose", "Figure")

    with pytest.raises(DraftsmanCannotRead) as refusal:
        slots_for(stripped, pipeline.PREFIX_TURNAROUND)

    assert "prompts.py" in str(refusal.value)


def test_an_unknown_generation_kind_is_refused_by_name():
    with pytest.raises(DraftsmanCannotRead) as refusal:
        slots_for(turnaround_prompt(), "charsheet_hologram")

    assert "charsheet_hologram" in str(refusal.value)


# ─────────────────────── the whole flow, and no provider ───────────────────────


def test_a_full_offline_flow_under_the_seam_calls_no_provider(tmp_path, monkeypatch):
    """The claim the seam exists to make: turnaround → references → a row strip,
    with the ONE call into the image-generation backend booby-trapped. Nothing
    below monkeypatches the charsheet seam — the environment variable is the
    only thing arming this run, exactly as it is for a spawned serve."""

    def _billed(*args, **kwargs):
        raise AssertionError("a provider was called under the fake draftsman")

    monkeypatch.setattr(pipeline.imagegen, "generate", _billed)
    monkeypatch.setenv(DRAFTSMAN_ENV, "fake")
    base = tmp_path / "base.png"
    square_image("s").save(base, format="PNG")

    refs = pipeline.generate_turnaround(SPEC, CONCEPT, base, out_dir=tmp_path / "turnaround")
    row = next(row for row in SPEC.authored_rows() if row.direction == "e")
    strip = pipeline.generate_row_strip(row, CONCEPT, refs["e"], out=tmp_path / "walk-e.png")

    assert sorted(refs) == sorted(SPEC.scheme.authored)
    assert all(Path(ref).is_file() for ref in refs.values())
    assert len(pipeline.extract_strip_frames(strip, row.frames, method="components", fit=False)) == row.frames


def test_with_the_seam_unset_the_same_flow_reaches_the_provider(tmp_path, monkeypatch):
    """The other half of the claim, and the reason the trap above is evidence:
    unarmed, the same call arrives at the backend. It is intercepted at
    ``imagegen.generate`` — the boundary INSIDE which the money is spent — so
    this test proves the route without taking it."""

    seen: list[str] = []

    def _billed(prompt, **kwargs):
        seen.append(kwargs["prefix"])
        raise RuntimeError("no provider configured")

    monkeypatch.setattr(pipeline.imagegen, "generate", _billed)
    base = tmp_path / "base.png"
    square_image("s").save(base, format="PNG")

    with pytest.raises(RuntimeError):
        pipeline.generate_turnaround(SPEC, CONCEPT, base, out_dir=tmp_path / "turnaround")

    assert seen == [pipeline.PREFIX_TURNAROUND]
