"""A deterministic Pillow draftsman, and the declared seam that reaches it.

``pipeline._generate_image`` is the charsheet package's one provider door, and
every deterministic test in this repository replaces it the same way: with a
``monkeypatch.setattr`` inside the pytest process doing the patching. That works
for everything that runs in-process and for nothing that does not. A SPAWNED
``harness serve`` running ``characters turnaround`` has no monkeypatch — so the
only long-running verb the runtime has (``_LONG_RUN_COMMANDS``) could not be
exercised end to end against a real child without a billed provider call, and
the long-run acceptance proof stopped at that gate (launcher
``EterniaLauncher/docs/mission_control/planned/local-runtime-ownership-and-retry-safety.md``
§8.10b, ruling RL-26).

This module is that missing arm, and it is deliberately three small things:

* **the drawing**, moved out of the test fixtures that grew it so a process
  nobody is patching can import it (``draw_glyph`` / ``strip_image`` /
  ``square_image``, and the tests now import them from HERE — one copy);
* **a draftsman that reads the REQUEST**, not a spec. The in-test fakes were
  built from the ``SheetSpec`` under test and mapped a provider prefix back to
  the row it belonged to; a spawned runtime has no such handle, so this one
  recovers what to draw from the prompt the pipeline just built (the numbered
  turnaround slots, the ``LAYOUT: arrange the N …`` count and the row's frozen
  facing) plus the prefix. Those three anchors are the coupling to
  :mod:`agent.charsheet.prompts` and they are pinned by a test that builds real
  prompts and reads them back, so a re-worded prompt breaks here, loudly, in
  one place — never as a half-drawn batch;
* **the refusal**, which is what keeps it off in the field:
  :data:`DRAFTSMAN_ENV` is honoured ONLY when it is set to exactly
  :data:`FAKE_DRAFTSMAN`. Any other value is ignored — the real door stands —
  with one stderr line per distinct value per process. There is no CLI flag and
  no config key on purpose: a seam that can only be armed by an environment
  variable is armed by the process that SPAWNS the runtime, which is exactly
  the reach the e2e needed and nothing more.

Nothing here is imported by the pipeline unless the variable is set (the import
is inside :func:`agent.charsheet.pipeline._draftsman`), so a runtime that never
arms the seam never pays for Pillow on this path and never resolves anything but
the real door.

The seam is also VISIBLE: every ``characters`` verb's ``--json`` result carries
``"draftsman": "fake"`` while it is armed (``hermes_cli/harness.py``'s
``_characters_emit`` / ``_characters_error``). A sandbox that forgot to set it
reads as a paid run rather than a silent one, and a field run that set it by
accident says so on every row it writes.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from pathlib import Path

#: The declared seam. Default-off: unset is the real provider door.
DRAFTSMAN_ENV = "HERMES_CHARSHEET_DRAFTSMAN"

#: The ONLY value :data:`DRAFTSMAN_ENV` honours.
FAKE_DRAFTSMAN = "fake"

# Geometry. The defaults are the fixtures' (a 192px-tall strip, a 384px square,
# a 44px glyph), with ONE difference that matters out of process: the strip
# WIDTH grows with the slot count. A test fixture knows its spec has four
# frames; a spawned runtime may be asked for any row width the operator declared
# (``MAX_FRAMES_PER_ROW``), and a fixed-width strip would eventually draw slots
# so close together that the extractor's gutter rule rejects a picture this
# module drew correctly. Slot pitch, not strip width, is the invariant.
SLOT_PX = 128
MIN_STRIP_W = 512
STRIP_H = 192
GLYPH_PX = 44
SQUARE_PX = 384
SQUARE_GLYPH_PX = 200

# Unit vectors per compass direction — the arrow's heading inside the glyph.
UNIT: dict[str, tuple[float, float]] = {
    "n": (0.0, -1.0),
    "ne": (0.7071, -0.7071),
    "e": (1.0, 0.0),
    "se": (0.7071, 0.7071),
    "s": (0.0, 1.0),
    "sw": (-0.7071, 0.7071),
    "w": (-1.0, 0.0),
    "nw": (-0.7071, -0.7071),
}


# ─────────────────────────────── the drawing ───────────────────────────────


def draw_glyph(draw, cx, cy, size, direction, tick, ticks) -> None:
    """One pose: a constant-size ring, a direction arrow, a per-frame tick.

    Constant bounding box on purpose — the sheet validator's collapse and
    registration guards measure the PIPELINE, and a fixture whose silhouette
    changed size frame to frame would be measuring itself.
    """
    half = size // 2
    ring = max(4, size // 15)
    draw.rectangle([cx - half, cy - half, cx + half, cy + half], outline=(30, 40, 120, 255), width=ring)
    ux, uy = UNIT[direction]
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


def _magenta():
    from agent.charsheet.pipeline import MAGENTA

    return (*MAGENTA, 255)


def strip_size_for(slot_count: int) -> tuple[int, int]:
    """The canvas a strip of *slot_count* poses is drawn on."""
    return (max(MIN_STRIP_W, SLOT_PX * max(1, slot_count)), STRIP_H)


def strip_image(slots, *, size=None, glyph_px: int = GLYPH_PX, spread: float = 1.0):
    """One landscape strip; *slots* is ``[(direction, tick, ticks), …]``.

    *spread* pulls the poses toward the strip's centre (``0.04`` makes them
    touch), which is how the retry path's "unsliceable roll" is staged.
    """
    from PIL import Image, ImageDraw

    slots = list(slots)
    width, height = strip_size_for(len(slots)) if size is None else size
    image = Image.new("RGBA", (width, height), _magenta())
    draw = ImageDraw.Draw(image)
    pitch = width / len(slots)
    for index, (direction, tick, ticks) in enumerate(slots):
        centre = width / 2 + (pitch * (index + 0.5) - width / 2) * spread
        draw_glyph(draw, int(centre), height // 2, glyph_px, direction, tick, ticks)
    return image


def square_image(direction, *, size_px: int = SQUARE_PX, glyph_px: int = SQUARE_GLYPH_PX):
    """One pose on a square canvas — the shape a direction re-roll asks for."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size_px, size_px), _magenta())
    draw_glyph(ImageDraw.Draw(image), size_px // 2, size_px // 2, glyph_px, direction, 0, 1)
    return image


# ────────────────────────── reading the request ──────────────────────────
#
# The three anchors this module reads out of a built prompt. They are regexes
# against prose, which is a coupling worth naming rather than hiding: the
# alternative is a second copy of the sheet spec travelling beside every call,
# and the call signature is the pipeline's documented test seam. A prompt
# re-wording breaks
# `test_charsheet_fake_draftsman.py::test_the_anchors_read_the_prompts_this_repository_builds`
# first, which is a red on a fixture rather than a batch that half-draws.

#: ``1. Pose 1 (leftmost is pose 1), direction S: …`` — turnaround, in order.
_TURNAROUND_SLOT = re.compile(r"^\d+\. Pose \d+ \(leftmost is pose 1\), direction ([A-Z]{1,2}):", re.M)

#: ``LAYOUT: arrange the 6 frames in ONE horizontal row …`` — every strip.
_LAYOUT_SLOTS = re.compile(r"LAYOUT: arrange the (\d+) (?:poses|frames) in ONE horizontal row")

#: ``This is the E facing and it is IDENTICAL in all frames`` — row strips.
_ROW_FACING = re.compile(r"This is the ([A-Z]{1,2}) facing")


class DraftsmanCannotRead(ValueError):
    """The prompt no longer carries an anchor this draftsman reads.

    Raised instead of guessing: a wrong slot count is refused downstream by
    ``extract_strip_frames`` as a bad ROLL, which would blame the pipeline for a
    fixture that went stale.
    """


def _anchor(value, prefix: str, what: str):
    if not value:
        raise DraftsmanCannotRead(
            f"the fake draftsman could not read {what} out of the {prefix!r} prompt; "
            "agent/charsheet/prompts.py changed shape — re-read the anchors in "
            "agent/charsheet/fake_draftsman.py"
        )
    return value


def _layout_count(prompt: str, prefix: str) -> int:
    match = _LAYOUT_SLOTS.search(prompt)
    _anchor(match, prefix, "the LAYOUT slot count")
    return int(match.group(1))


def slots_for(prompt: str, prefix: str):
    """What the request asks to be drawn: ``("strip", slots)`` or ``("square", d)``.

    Every slot the request names is drawn — that is the property that lets a
    batch run to completion with real revision rows rather than dying on the
    first strip the extractor refuses.
    """
    from agent.charsheet import pipeline

    view_prefix = pipeline.view_prefix("")
    row_prefix = pipeline.row_prefix("")

    if prefix == pipeline.PREFIX_TURNAROUND:
        directions = [token.lower() for token in _TURNAROUND_SLOT.findall(prompt)]
        _anchor(directions, prefix, "the numbered per-slot direction list")
        expected = _layout_count(prompt, prefix)
        if len(directions) != expected:
            raise DraftsmanCannotRead(
                f"the {prefix!r} prompt names {len(directions)} slots in its list and "
                f"{expected} in its LAYOUT line"
            )
        return "strip", [(direction, 0, 1) for direction in directions]

    if prefix.startswith(view_prefix):
        direction = prefix[len(view_prefix):]
        if direction not in UNIT:
            raise DraftsmanCannotRead(f"unknown direction {direction!r} in prefix {prefix!r}")
        return "square", direction

    if prefix.startswith(row_prefix):
        frames = _layout_count(prompt, prefix)
        facing = _ROW_FACING.search(prompt)
        _anchor(facing, prefix, "the frozen facing")
        direction = facing.group(1).lower()
        if direction not in UNIT:
            raise DraftsmanCannotRead(f"unknown direction {direction!r} in the {prefix!r} prompt")
        return "strip", [(direction, index, frames) for index in range(frames)]

    raise DraftsmanCannotRead(f"unexpected generation prefix {prefix!r}")


# ─────────────────────────────── the draftsman ───────────────────────────────


class FakeDraftsman:
    """A drop-in for ``pipeline._generate_image``: same signature, no network.

    Deterministic by construction — the picture is a pure function of the
    request, so the same request answers the same PNG bytes every time, in this
    process or the next one. The path it is written to is not part of that
    promise (it carries a call counter, so a re-roll is a different file the way
    a real provider's would be).
    """

    def __init__(self, out_dir, *, strip_size=None, square_px: int = SQUARE_PX, glyph_px: int = GLYPH_PX):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.strip_size = strip_size
        self.square_px = square_px
        self.glyph_px = glyph_px
        self.calls: list[dict] = []
        self._lock = threading.Lock()

    def draw(self, prompt: str, prefix: str):
        """The picture for one request. Overridden by fixtures staging bad rolls."""
        kind, what = slots_for(prompt, prefix)
        if kind == "square":
            return square_image(what, size_px=self.square_px)
        return strip_image(what, size=self.strip_size, glyph_px=self.glyph_px)

    def __call__(self, prompt, *, reference_images, aspect_ratio, prefix, provider):
        with self._lock:
            self.calls.append(
                {
                    "prompt": prompt,
                    "refs": [str(ref) for ref in (reference_images or [])],
                    "aspect": aspect_ratio,
                    "prefix": prefix,
                }
            )
            index = len(self.calls)
        image = self.draw(prompt, prefix)
        path = self.out_dir / f"{prefix}-{index}.png"
        image.save(path, format="PNG")
        return path


# ──────────────────────────────── the seam ────────────────────────────────


#: Unsupported values this process has already reported. A set rather than a
#: memoizing decorator so a test can clear it without depending on how the
#: once-ness is implemented.
_REPORTED: set[str] = set()


def _refuse(value: str) -> None:
    """One stderr line per distinct unsupported value, per process.

    Once, not once per generation: a rows batch resolves the door for every
    strip and every attempt, and a line per resolution would bury the run's own
    output under a warning the operator has already read.
    """
    if value in _REPORTED:
        return
    _REPORTED.add(value)
    print(
        f"hermes: ignoring {DRAFTSMAN_ENV}={value!r} — the only supported value is "
        f"{FAKE_DRAFTSMAN!r}; the real provider door stands",
        file=sys.stderr,
    )


def active_draftsman_name(env=None) -> str:
    """``"fake"`` while the seam is armed, ``""`` for the real door.

    Reads the environment on EVERY call, never at import: a serve started
    without the variable and a child started with it must each behave as their
    own environment says, and a module-level constant would freeze whichever
    was first. Silent — the refusal line belongs to the resolution
    (:func:`draftsman_from_env`), so a read-only verb does not re-report it.
    """
    value = (os.environ if env is None else env).get(DRAFTSMAN_ENV, "")
    return FAKE_DRAFTSMAN if str(value).strip() == FAKE_DRAFTSMAN else ""


#: One draftsman per process, and one scratch directory for it. Both are lazy:
#: an unarmed runtime creates neither.
_OUT_DIR: Path | None = None
_INSTANCE: "FakeDraftsman | None" = None
_INSTANCE_LOCK = threading.Lock()


def _out_dir() -> Path:
    """Where the fake writes the images it hands back.

    Under the sandbox's own ``HERMES_HOME`` when there is one, so a sandboxed
    child leaves nothing outside the root its operator gave it; the pipeline
    COPIES what it keeps into the revision store, so what stays here is scratch.
    A process temp directory is the fallback and is never the live store: this
    module only draws, and the one caller that can arm it is a test or a
    deliberately sandboxed spawn.
    """
    global _OUT_DIR
    if _OUT_DIR is None:
        try:
            from hermes_constants import get_hermes_home

            root = Path(get_hermes_home()) / "tmp"
            root.mkdir(parents=True, exist_ok=True)
            _OUT_DIR = Path(tempfile.mkdtemp(prefix="fake-draftsman-", dir=str(root)))
        except Exception:  # noqa: BLE001 - a scratch directory must never fail a run
            _OUT_DIR = Path(tempfile.mkdtemp(prefix="hermes-fake-draftsman-"))
    return _OUT_DIR


def draftsman_from_env(env=None):
    """The fake while the seam is armed, else ``None`` (and one refusal line).

    ``None`` means "the caller keeps its real door" — this function never
    returns the real one, so the pipeline's own resolution stays readable at
    the site that owns it.
    """
    raw = str((os.environ if env is None else env).get(DRAFTSMAN_ENV, "")).strip()
    if not raw:
        return None
    if raw != FAKE_DRAFTSMAN:
        _refuse(raw)
        return None
    global _INSTANCE
    with _INSTANCE_LOCK:
        if _INSTANCE is None:
            _INSTANCE = FakeDraftsman(_out_dir())
        return _INSTANCE
