"""What a ``characters`` payload says about the door it drew through (RL-26).

The seam that lets a spawned runtime draw offline is invisible from the outside
unless the payloads say so, and an invisible seam is the dangerous kind: a
sandbox that forgot to arm it reads exactly like one that armed it, and the
difference is a provider bill. So every ``characters`` verb's ``--json`` result
carries ``"draftsman": "fake"`` while the variable is set, and carries NO
``draftsman`` key at all when it is not.

Absent rather than ``"real"`` on purpose. The launcher parses these payloads
today; "absent means the provider door" is the reading it already has, and a new
key on the path it has always taken would be a change to prove rather than a
change to make.

The flow below is driven through the real parser against a temp ``HERMES_HOME``
— the same shape as ``test_harness_characters_cli.py`` — but NOTHING here
monkeypatches the charsheet seam. The environment variable is the only thing
arming it, which is the property the child e2e then proves across a process
boundary.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent.charsheet import pipeline
from agent.charsheet.fake_draftsman import DRAFTSMAN_ENV, square_image
from hermes_cli.harness import build_parser

pytest.importorskip("PIL")

CONCEPT = "an arrow knight"
STATES = "idle:2"
DIRECTIONS = "4"  # authored: s, e, n → three references, three rows


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser()
    build_parser(top.add_subparsers(dest="command"))
    return top


def run(argv, capsys) -> tuple[int, dict]:
    args = parser().parse_args(argv)
    code = args.func(args)
    out = capsys.readouterr().out.strip()
    payload, end = json.JSONDecoder().raw_decode(out)
    assert out[end:].strip() == "", f"extra output after the JSON object: {out[end:]!r}"
    return code, payload


@pytest.fixture
def base_image(tmp_path):
    path = tmp_path / "base.png"
    square_image("s").save(path, format="PNG")
    return path


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv(DRAFTSMAN_ENV, "fake")


def start(capsys, base_image) -> str:
    code, payload = run(
        [
            "harness", "characters", "start",
            "--concept", CONCEPT,
            "--slug", "arrow-knight",
            "--states", STATES,
            "--directions", DIRECTIONS,
            "--base-image", str(base_image),
            "--json",
        ],
        capsys,
    )
    assert code == 0, payload
    return payload["draft"]


# ─────────────────────────── the key, on and off ───────────────────────────


def test_with_the_seam_unset_no_payload_carries_a_draftsman_key(capsys, base_image):
    """The unchanged shape. Every reader that exists today sees this one."""

    draft = start(capsys, base_image)

    for argv in (
        ["harness", "characters", "list", "--json"],
        ["harness", "characters", "status", "--draft", draft, "--json"],
    ):
        _, payload = run(argv, capsys)
        assert "draftsman" not in payload


def test_with_the_seam_armed_every_verb_says_which_draftsman_drew(armed, capsys, base_image):
    draft = start(capsys, base_image)

    for argv in (
        ["harness", "characters", "list", "--json"],
        ["harness", "characters", "status", "--draft", draft, "--json"],
        ["harness", "characters", "payload-contract", "--json"],
    ):
        _, payload = run(argv, capsys)
        assert payload["draftsman"] == "fake", argv


def test_every_line_of_the_autopilots_stream_says_it(armed, capsys, base_image, monkeypatch):
    """`auto` is the third long run and it does not emit through
    ``_characters_emit`` — it writes its own newline-framed stream, one line per
    stage as the stage lands. A consumer watching a batch mid-flight is exactly
    the reader who needs the door named, so every line carries the key."""

    def _billed(*args, **kwargs):
        raise AssertionError("a provider was called with the fake draftsman armed")

    monkeypatch.setattr(pipeline.imagegen, "generate", _billed)
    draft = start(capsys, base_image)

    args = parser().parse_args(
        ["harness", "characters", "auto", "--draft", draft, "--through", "rows", "--json"]
    )
    assert args.func(args) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "auto printed nothing"
    assert all(line["draftsman"] == "fake" for line in lines)
    assert lines[-1]["ran"] == ["turnaround", "approve-direction", "rows"]


def test_a_refusal_says_it_too(armed, capsys):
    """A run that refused still spent the draftsman it was going to spend, and
    the flat error shape is a ``characters`` result like any other."""

    code, payload = run(["harness", "characters", "status", "--draft", "nope", "--json"], capsys)

    assert code == 2
    assert payload["ok"] is False
    assert payload["draftsman"] == "fake"


def test_the_human_line_is_untouched(armed, capsys, base_image):
    """The key rides the ``--json`` result only. A person reading receipts is
    told by the sandbox they are standing in, not by a suffix on every line."""

    draft = start(capsys, base_image)
    args = parser().parse_args(["harness", "characters", "status", "--draft", draft])
    assert args.func(args) == 0

    assert "draftsman" not in capsys.readouterr().out


# ─────────────────────── a real batch, on the fake door ───────────────────────


def test_a_turnaround_and_a_rows_batch_run_to_completion_on_the_seam(armed, capsys, base_image, monkeypatch):
    """The whole point: a full generation, with the ONE call into the image
    backend booby-trapped, driven by nothing but the environment variable.

    Three references, three rows, every attempt on disk and readable back
    through ``status`` — that is what "real revision rows" means for the
    long-run proof this seam was cut for (RL-25)."""

    def _billed(*args, **kwargs):
        raise AssertionError("a provider was called with the fake draftsman armed")

    monkeypatch.setattr(pipeline.imagegen, "generate", _billed)
    draft = start(capsys, base_image)

    code, turnaround = run(["harness", "characters", "turnaround", "--draft", draft, "--json"], capsys)
    assert code == 0, turnaround
    assert turnaround["draftsman"] == "fake"
    assert len(turnaround["turnaround"]) == 3

    code, approved = run(
        ["harness", "characters", "approve-direction", "--draft", draft, "--all", "--json"], capsys
    )
    assert code == 0, approved
    assert approved["stage"] == "rows"

    code, rows = run(["harness", "characters", "rows", "--draft", draft, "--json"], capsys)
    assert code == 0, rows
    assert rows["draftsman"] == "fake"
    assert sorted(rows["rows"]) == ["idle-e", "idle-n", "idle-s"]
    assert all(entry["approved"] for entry in rows["rows"].values())

    _, status = run(["harness", "characters", "status", "--draft", draft, "--json"], capsys)
    assert status["draftsman"] == "fake"
    committed = status["status"]["rows"]
    assert sorted(committed) == ["idle-e", "idle-n", "idle-s"]
    assert all(item["attempts"] == 1 for item in committed.values())
    assert all(item["approved"] == 0 for item in committed.values())
