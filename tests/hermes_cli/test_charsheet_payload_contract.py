"""`characters payload-contract` publishes a key set that can actually MOVE.

The verb exists because the launcher/hermes character payload is a cross-repo
contract with no shared schema, and three field moves have already landed blind
(`hermes_cli/charsheet_payload_contract.py` names them). Its whole value rests
on one property: the dump is DERIVED from the producers, so a key a producer
grows or drops is in it the same day. A dump built from a hand-written list
would pass every shape assertion below and be exactly as blind as the captured
fixture it replaces.

So the load-bearing test here is `a planted key`: it edits the producer and
requires the dump to notice, in both directions (added AND removed), for both
payload kinds. Everything else — byte-stability, the conditional slot, the
dynamic-map rule — is about the dump being a fixture the launcher can commit and
diff without churn.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime.cli_format import emit_json
from hermes_cli import charsheet_payload_contract as contract


@pytest.fixture(scope="module")
def document():
    return contract.build_payload_contract()


def _keys(document, kind):
    return document["payloads"][kind]["keys"]


def test_the_dump_is_byte_stable(document):
    """Two runs, the same bytes — which is what makes it committable.

    Everything the probes touch is unstable: a temp directory that moves every
    run, a `spritesheetRevision` of `mtime_ns:size`, a draft id stamped from the
    clock. The dump records key PATHS and no values, so none of it reaches the
    artifact. A fixture that churned on every refresh would be re-vendored by
    reflex, which is how a real diff gets waved through.
    """
    again = contract.build_payload_contract()
    assert emit_json(again) == emit_json(document)


def test_no_probe_local_data_reaches_the_dump(document):
    """The other half of stability: nothing about the exhibits is in the artifact.

    The probes carry slugs, row keys, sector names and temp paths. A dump that
    leaked any of them would pin one throwaway character's vocabulary into the
    launcher's fixture as if it were the schema — and the leak that actually
    happened while this was being built was `directions.mirrored.w`, which
    survived a naive path intersection because BOTH direction schemes mirror
    `w`.
    """
    text = emit_json(document)
    for leak in (
        "contract-probe",
        "idle-ne",
        "bounce-e",
        "framesByRow.",
        "directions.mirrored.",
        "hermes-payload-contract-",
    ):
        assert leak not in text, f"probe-local data {leak!r} reached the dump"


def test_a_planted_key_in_the_producer_shows_up_in_the_dump():
    """The property the whole artifact rests on, proved in BOTH directions.

    An ADDED key is the `handednessAccepted` case (`34a8dad32e`): merely unread
    by the launcher, but its default-deny comparison threw for every character.
    A REMOVED key is the `cardSafe` case (`4659127eba`) and the worse half: the
    reader keeps a stale DEFAULT and acts on it. A dump that could only see
    additions would be blind to the instance that cost more.
    """
    from agent.charsheet import draft as charsheet_draft

    real_sprite = charsheet_draft.sprite_payload
    real_thumb = charsheet_draft.CharacterDraft.row_thumb

    def planted_sprite(slug, **kwargs):
        payload = real_sprite(slug, **kwargs)
        payload["plantedBySuite"] = True
        payload.pop("mime")
        return payload

    def planted_thumb(self, *args, **kwargs):
        payload = real_thumb(self, *args, **kwargs)
        payload["plantedBySuite"] = True
        payload.pop("source")
        return payload

    # Plain setattr rather than `monkeypatch`: the plant has to be undone MID-TEST
    # to take the second measurement, and `monkeypatch.undo()` unwinds the whole
    # stack — including this tree's autouse pins, whose tripwire correctly reds
    # when a body does that.
    charsheet_draft.sprite_payload = planted_sprite
    charsheet_draft.CharacterDraft.row_thumb = planted_thumb
    try:
        planted = contract.build_payload_contract()
    finally:
        charsheet_draft.sprite_payload = real_sprite
        charsheet_draft.CharacterDraft.row_thumb = real_thumb

    assert "character.plantedBySuite" in _keys(planted, "sprite")
    assert "character.mime" not in _keys(planted, "sprite")
    assert "plantedBySuite" in _keys(planted, "thumb")
    assert "source" not in _keys(planted, "thumb")

    # And the plant is what moved it: with the producers restored, the same
    # build says the opposite. Without this the four assertions above would pass
    # against a dump that always reported those keys.
    clean = contract.build_payload_contract()
    assert "character.plantedBySuite" not in _keys(clean, "sprite")
    assert "character.mime" in _keys(clean, "sprite")
    assert "plantedBySuite" not in _keys(clean, "thumb")
    assert "source" in _keys(clean, "thumb")


def test_the_conditional_slot_is_two_spellings_of_one_key(document):
    """`--no-sheet` fills one slot two ways, and the dump says which mode carries which.

    This is the shape a flat key list cannot express, and the third unrepaired
    instance of the class: `a4f8e62af7` added `sheet` as a CONDITIONAL key, so a
    consumer reading it unconditionally and a consumer reading
    `spritesheetBase64` unconditionally are both wrong half the time.
    """
    keys = _keys(document, "sprite")
    assert keys["character.spritesheetBase64"]["modes"] == ["sheet"]
    assert keys["character.sheet"]["modes"] == ["no-sheet"]
    conditional = sorted(path for path, entry in keys.items() if entry["conditional"])
    assert conditional == ["character.sheet", "character.spritesheetBase64"], (
        "exactly one slot in the sprite payload is mode-dependent; a second one "
        "is a contract change the launcher's gate has to be taught about"
    )


def test_a_dynamic_map_is_one_key_and_its_children_are_not(document):
    """`framesByRow` is keyed by ROW NAME: the key is schema, what is under it is data.

    Nothing declares which maps those are — the two probes carry different state
    vocabularies and different direction schemes, and a map whose key set moved
    with them is data by measurement. The rule matters because the alternative
    (record everything) writes one character's row names into the launcher's
    fixture, where the next character reds it.
    """
    keys = _keys(document, "sprite")
    dynamic = sorted(path for path, entry in keys.items() if entry["dynamic"])
    assert dynamic == ["character.directions.mirrored", "character.framesByRow"]
    for path in keys:
        for parent in dynamic:
            assert not path.startswith(f"{parent}."), path


def test_the_envelope_is_in_the_contract_too(document):
    """The keys the CLI layer adds are keys the launcher decodes.

    `charaRowThumbFromPayload` reads `draft` and `stage` FIRST, and neither is in
    `row_thumb`'s return — `_characters_verb` merges them. A contract derived
    from the backend function alone would be silent about exactly the fields the
    launcher's thumb reader opens with, so the probes read what the verb PRINTS.
    """
    assert set(_keys(document, "thumb")) >= {"ok", "draft", "stage"}
    assert document["payloads"]["thumb"]["object"] == ""
    assert set(_keys(document, "sprite")) >= {"ok", "character"}
    assert document["payloads"]["sprite"]["object"] == "character"


def test_the_verb_prints_the_document(capsys):
    """The parser accepts it and `--json` prints the dump and nothing else."""
    from hermes_cli.harness import build_parser

    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(["harness", "characters", "payload-contract", "--json"])
    assert args.func(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["schema"] == contract.SCHEMA
    assert sorted(printed["payloads"]) == ["sprite", "thumb"]


def test_the_human_line_counts_the_keys(capsys):
    """A bare call is for a person, and says how big the contract is."""
    from hermes_cli.harness import _cmd_characters_payload_contract

    assert _cmd_characters_payload_contract(argparse.Namespace(json=False)) == 0
    line = capsys.readouterr().out.strip()
    assert "sprite:" in line and "thumb:" in line
    assert "conditional" in line
