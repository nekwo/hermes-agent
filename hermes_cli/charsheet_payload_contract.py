"""The character payload contract: every key ``harness characters`` can emit.

WHY THIS EXISTS
---------------
The launcher/hermes sprite payload is a cross-repo contract with a
default-deny comparison on one side (``tool/spatial/characters/
character_bundle.dart``'s ``sidecarDisagreementsWithHermes`` walks EVERY payload
key) and an "additive is safe" habit on the other. There is no shared schema, so
three field moves have already landed blind:

* ``34a8dad32e`` ADDED ``handednessAccepted`` -> every ``bundle_character.dart``
  run on a machine with hermes installed threw for every character, and stayed
  silently fine on every machine without one (which is CI);
* ``4659127eba`` REMOVED ``cardSafe`` (split into ``withinConsoleBudget`` +
  ``withinOwnSheet``, deliberately not aliased) -> the launcher's client kept
  asking for it and read every live crop as unjudged. A removal is the worse
  half: an added field is merely unread, a removed one leaves the reader holding
  a stale DEFAULT it acts on;
* ``a4f8e62af7`` added the CONDITIONAL ``sheet`` slot (``--no-sheet``) -> a key
  that is present in one mode and absent in the other, which a single captured
  snapshot cannot describe at all.

Every one of the three was found by a person, because the only launcher-side
lane that can see a producer move spawns a real ``hermes`` and skips wherever
there is none. This verb is the other half: hermes publishes the payload's KEY
SET as an artifact the launcher commits and diffs, so the two sides disagree in
a file rather than at runtime.

HOW THE KEY SET IS DERIVED
--------------------------
By RUNNING the verbs, never by reading them. A hand-written list is a snapshot
with the same blindness as the capture it replaces; a list built from
``sprite_payload``'s own output cannot miss a key that function grew. So this
module builds a throwaway character library in a temp directory
(``HERMES_SHARED_CHARACTERS``), installs a synthetic character and a synthetic
draft into it, calls the real ``_cmd_characters_*`` handlers with ``--json``,
and reads the key paths off what they PRINT -- envelope included, because the
envelope keys (``ok``, ``draft``, ``stage``) are keys the launcher decodes too.

DATA KEYS VERSUS SCHEMA KEYS
----------------------------
``framesByRow`` is keyed by row name and ``directions.mirrored`` by compass
sector: those keys are DATA, and a contract that recorded them would pin the
probe's own row vocabulary as if it were the schema. Nothing here declares which
maps are dynamic. Instead every kind is probed TWICE with a deliberately
different sheet vocabulary (states, frame counts, direction scheme, slug), and
only the paths BOTH probes agree on are contract paths. A key that moved when
the vocabulary moved is data, by measurement.

CONDITIONAL KEYS
----------------
A kind that has several MODES (``sprite`` has two: with the sheet bytes and
with the path in their slot) is probed in each, and a path present in some modes
and not others is marked ``conditional`` with the modes that carry it. That is
the shape ``sheet``/``spritesheetBase64`` needs and the one a flat key list
cannot express.

CONTRACT
--------
stdout is one JSON object. Key paths only -- never a value, so the dump is
byte-stable across runs, machines and clocks even though the probes it is
derived from carry temp paths, ``mtime_ns`` revisions and generated draft ids.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

# Bumped, never tolerated: a reader that silently under-read an older shape
# would report "clean" about a hole this dump exists to show. The launcher's
# gate refuses a schema it does not know.
SCHEMA = "charsheet_payload_contract/v1"

#: Env var :func:`hermes_constants.get_shared_characters_dir` reads first. The
#: probes redirect the library through it so nothing touches the operator's real
#: characters.
_LIBRARY_ENV = "HERMES_SHARED_CHARACTERS"


# ── path walking ───────────────────────────────────────────────────────────


def _walk(value: Any, path: str, paths: set[str], children: dict[str, set[str]]) -> None:
    """Collect every dotted key path in *value*, plus each map's own key set.

    A list contributes its ELEMENT shape under ``[]`` -- a payload's list is
    homogeneous by construction, and recording an index would pin the probe's
    row count. *children* records, per path, the keys the map at that path
    carried; comparing those across probes is what tells a record apart from a
    map whose KEYS are data.
    """
    if isinstance(value, dict):
        children.setdefault(path, set()).update(str(key) for key in value)
        for key in value:
            child = f"{path}.{key}" if path else str(key)
            paths.add(child)
            _walk(value[key], child, paths, children)
    elif isinstance(value, list):
        for item in value:
            _walk(item, f"{path}[]", paths, children)


def _shape_of(document: Any) -> tuple[set[str], dict[str, set[str]]]:
    paths: set[str] = set()
    children: dict[str, set[str]] = {}
    _walk(document, "", paths, children)
    return paths, children


def _agreed_shape(documents: list[Any]) -> tuple[set[str], set[str]]:
    """``(contract paths, dynamic-keyed paths)`` across parallel probes.

    A map whose key set MOVED when the sheet vocabulary moved is keyed by data
    (``framesByRow`` by row name, ``directions.mirrored`` by sector), so it is
    reported as one dynamic key and its children are not contract keys at all.
    Nothing declares which maps those are; the disagreement between the probes
    is the measurement.

    Intersecting the path sets is not enough on its own and the reason is
    exactly the case that motivated this: 8-way and 4-way BOTH mirror ``w``, so
    ``directions.mirrored.w`` survives an intersection and would have pinned one
    probe's sector vocabulary into the contract as if it were schema.
    """
    shapes = [_shape_of(document) for document in documents]
    paths = shapes[0][0].intersection(*(shape[0] for shape in shapes[1:]))
    dynamic = {
        path
        for path in shapes[0][1]
        if any(shape[1].get(path) != shapes[0][1][path] for shape in shapes[1:])
    }
    prefixes = tuple(f"{path}." for path in dynamic if path)
    return {p for p in paths if not p.startswith(prefixes)}, dynamic & paths


# ── the probes ─────────────────────────────────────────────────────────────


def _probe_specs() -> list[dict]:
    """Two sheet vocabularies that share no state name, row key or sector set.

    The whole point is disagreement: whatever the two have in common is
    structure, whatever they do not is data. The second carries a NON-directional
    state as well, so a row with ``direction: null`` is in the sample.
    """
    from agent.charsheet.spec import CHAR8, SheetSpec, parse_directions, parse_states

    return [
        {
            "slug": "contract-probe-eight",
            "display_name": "Contract Probe Eight",
            "spec": CHAR8,
            "accepted": [{"row": "idle-ne", "gain": 0.41, "basis": "rotation+states"}],
        },
        {
            "slug": "contract-probe-four",
            "display_name": "Contract Probe Four",
            "spec": SheetSpec(
                states=parse_states("bounce:3,cheer:4:fixed"),
                scheme=parse_directions("4"),
            ),
            # The ROUND-TWO spelling (bare row keys), so the probe also exercises
            # `_handedness_accepted`'s normalisation rather than only its
            # pass-through: both shapes must land on the same key set.
            "accepted": ["bounce-e"],
        },
    ]


def _install_probe_character(probe: dict) -> str:
    """Write a synthetic installed character into the redirected library.

    The manifest is spelled through ``spec_to_dict``/``_row_json`` -- the same
    helpers ``compose`` writes it with -- so a spec-format change reaches the
    probe instead of leaving a hand-written manifest behind.
    """
    from agent.charsheet import draft as charsheet_draft
    from agent.pet.constants import DEFAULT_SCALE, LOOP_MS

    spec = probe["spec"]
    directory = charsheet_draft.characters_dir() / probe["slug"]
    directory.mkdir(parents=True, exist_ok=True)
    # The bytes are never decoded by `sprite_payload` (it base64s them and
    # stats the file), so a few of them are a whole sheet for this purpose.
    (directory / charsheet_draft.SHEET_FILENAME).write_bytes(b"RIFFprobe")
    manifest = {
        "schema": charsheet_draft.SCHEMA,
        "slug": probe["slug"],
        "displayName": probe["display_name"],
        "concept": "payload contract probe",
        "style": "auto",
        "spec": charsheet_draft.spec_to_dict(spec),
        "rows": [charsheet_draft._row_json(row) for row in spec.rows()],
        "frameW": spec.frame_w,
        "frameH": spec.frame_h,
        "loopMs": LOOP_MS,
        "scale": DEFAULT_SCALE,
        "generator": "charsheet",
        "draftId": "payload-contract-probe",
        "created": "1970-01-01T00:00:00Z",
        "handednessAccepted": probe["accepted"],
    }
    (directory / charsheet_draft.MANIFEST_FILENAME).write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return probe["slug"]


def _start_probe_draft(probe: dict, workspace: Path) -> tuple[str, str]:
    """A draft carrying ONE proposed row attempt; returns ``(draft id, row key)``.

    Nothing is generated: the strip is an 8-pixel-tall solid RGBA image proposed
    straight into the revision store, which is all ``row_thumb`` needs to crop a
    cell. The crop is taken at ``--scale 1`` so no bound is anywhere near being
    the thing under test here -- the KEYS are.
    """
    from PIL import Image

    from agent.charsheet import draft as charsheet_draft

    draft = charsheet_draft.CharacterDraft.create(
        concept="payload contract probe",
        slug=probe["slug"],
        display_name=probe["display_name"],
        spec=probe["spec"],
    )
    row = probe["spec"].authored_rows()[0]
    strip = workspace / f"{probe['slug']}-strip.png"
    Image.new("RGBA", (8 * row.frames, 8), (32, 64, 128, 255)).save(strip)
    draft.store.propose(charsheet_draft.row_item(row.key), strip)
    return draft.id, row.key


def _emit(handler: Callable[[Any], int], **args: Any) -> dict:
    """Run one ``_cmd_characters_*`` handler and parse the JSON it PRINTS.

    Reading stdout rather than calling the backend directly is deliberate: the
    envelope (``ok``, ``draft``, ``stage``, ``character``) is added by the CLI
    layer, and the launcher decodes those keys too. A contract derived from the
    backend function alone would be silent about exactly the keys
    ``charaRowThumbFromPayload`` reads first.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = handler(argparse.Namespace(json=True, **args))
    printed = buffer.getvalue().strip()
    if code != 0:
        raise RuntimeError(f"probe verb exited {code}: {printed[:400]}")
    return json.loads(printed)


# ── the kinds ──────────────────────────────────────────────────────────────


def _kind(
    *,
    argv: str,
    producer: str,
    object_path: str,
    modes: dict[str, tuple[set[str], set[str]]],
) -> dict:
    """One payload kind, from ``{mode: (paths, dynamic)}`` measured per mode."""
    all_modes = sorted(modes)
    every = sorted(set().union(*(paths for paths, _ in modes.values())))
    dynamic = set().union(*(marked for _, marked in modes.values()))
    keys: dict[str, dict] = {}
    for path in every:
        carried = sorted(mode for mode in all_modes if path in modes[mode][0])
        keys[path] = {
            "modes": carried,
            "conditional": len(carried) != len(all_modes),
            # A map whose OWN keys are data (`framesByRow` by row name). The key
            # is contract; what is under it is the character.
            "dynamic": path in dynamic,
        }
    return {
        "argv": argv,
        "producer": producer,
        # Where the launcher's decode is SCOPED. `sprite` nests its payload
        # under `character`; `thumb` merges into the envelope, so the envelope
        # IS the answer and there is no key to scope to.
        "object": object_path,
        "modes": all_modes,
        "keys": keys,
    }


def _sprite_kind(probes: list[dict]) -> dict:
    from hermes_cli.harness import _cmd_characters_sprite

    modes: dict[str, tuple[set[str], set[str]]] = {}
    for mode, no_sheet in (("sheet", False), ("no-sheet", True)):
        modes[mode] = _agreed_shape(
            [
                _emit(_cmd_characters_sprite, slug=probe["slug"], no_sheet=no_sheet)
                for probe in probes
            ]
        )
    return _kind(
        argv="harness characters sprite <slug> --json [--no-sheet]",
        producer="agent/charsheet/draft.py::sprite_payload",
        object_path="character",
        modes=modes,
    )


def _thumb_kind(drafts: list[tuple[str, str]]) -> dict:
    from hermes_cli.harness import _cmd_characters_thumb

    measured = _agreed_shape(
        [
            _emit(
                _cmd_characters_thumb,
                draft=draft_id,
                row=row_key,
                attempt=-1,
                frame=None,
                scale=1,
                square=False,
            )
            for draft_id, row_key in drafts
        ]
    )
    return _kind(
        argv="harness characters thumb --draft <id> --row <key> --json",
        producer="agent/charsheet/draft.py::CharacterDraft.row_thumb",
        object_path="",
        modes={"default": measured},
    )


# ── the dump ───────────────────────────────────────────────────────────────


def build_payload_contract() -> dict:
    """Measure every payload kind and return the contract document."""
    probes = _probe_specs()
    with tempfile.TemporaryDirectory(prefix="hermes-payload-contract-") as tmp:
        workspace = Path(tmp)
        library = workspace / "characters"
        library.mkdir(parents=True, exist_ok=True)
        previous = os.environ.get(_LIBRARY_ENV)
        os.environ[_LIBRARY_ENV] = str(library)
        try:
            for probe in probes:
                _install_probe_character(probe)
            drafts = [_start_probe_draft(probe, workspace) for probe in probes]
            payloads = {
                "sprite": _sprite_kind(probes),
                "thumb": _thumb_kind(drafts),
            }
        finally:
            if previous is None:
                os.environ.pop(_LIBRARY_ENV, None)
            else:
                os.environ[_LIBRARY_ENV] = previous
    return {"schema": SCHEMA, "payloads": payloads}
