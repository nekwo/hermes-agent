"""The payload-contract dump is FRESH, and its freshness check can actually red.

`scripts/dump_payload_contract.py --check` is the Lane A gate on
`tests/fixtures/charsheet_payload_contract.json`. It exists because the launcher
compares every character payload key against a vendored copy of THIS document
(`tool/charsheet_payload_contract/`, read by
`character_bundle.dart`'s `sidecarDisagreementsWithHermes`, which walks every
key by default-deny) — so until this gate existed, a hermes-side producer move
left every hermes test green while the launcher's copy lied, and only the repo
that did NOT move could go red. Three field moves have already landed blind that
way: `handednessAccepted` added (`34a8dad32e`), `cardSafe` removed
(`4659127eba`), the conditional `sheet` slot added (`a4f8e62af7`).

This file lives in `tests/hermes_cli/` on purpose: that directory is inside the
validated four-directory suite lane, so the gate is reachable from the suite as
well as from the script.

The second test is the one that matters most: a gate that has only ever been
green is indistinguishable from one that cannot fail.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dump_payload_contract.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "charsheet_payload_contract.json"


def _load_module():
    """Import the script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("dump_payload_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dump():
    return _load_module()


@pytest.fixture(scope="module")
def document(dump):
    """One probe run, shared: each build spawns a throwaway character library."""
    return dump.build_contract(REPO_ROOT, quiet=True)


def test_the_committed_dump_matches_the_live_producers(dump):
    """The gate's green case, asserted against the real producers.

    A red here means a payload key moved and the fixture did not:
        python scripts/dump_payload_contract.py --write
    and then READ the diff — a REMOVED key is not a fixture update, it is a
    launcher reader left acting on a stale default.
    """
    assert dump.main(["--check"]) == 0


def test_the_check_reds_when_the_committed_dump_drifts(dump, tmp_path):
    """Round-trip the failure the gate exists for: fixture says X, producers say Y.

    Written as a MUTATION of the real dump rather than a hand-built stub, so the
    thing being detected is a genuine drift in the recorded key set and not a
    schema mismatch a reader would have rejected anyway. The mutation is the
    exact shape of `4659127eba`: one key gone from one kind.
    """
    contract = json.loads(FIXTURE.read_text(encoding="utf-8"))
    victim = "sprite"
    assert victim in contract["payloads"], sorted(contract["payloads"])
    keys = contract["payloads"][victim]["keys"]
    assert keys, victim
    keys.pop(sorted(keys)[0])

    stale = tmp_path / "stale_contract.json"
    stale.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert dump.main(["--check", "--fixture", str(stale)]) == 1


def test_the_check_reds_when_the_committed_dump_is_missing(dump, tmp_path):
    """A gate whose input has vanished must fail, never pass by absence."""
    assert dump.main(["--check", "--fixture", str(tmp_path / "nope.json")]) == 1


def test_render_is_byte_stable_and_ends_in_exactly_one_newline(dump, document):
    """The artifact is compared byte-for-byte, so its rendering is pinned.

    Byte-equality is also what makes the launcher's vendored copy checkable on
    sync: the two hold the same document for one checkout, and that is only a
    meaningful claim while the rendering is fixed here.
    """
    first = dump.render(document)
    second = dump.render(document)
    assert first == second
    assert first.endswith("}\n")
    assert not first.endswith("}\n\n")
    assert "\r" not in first
    # sort_keys, so the reader can diff two dumps line by line.
    assert json.loads(first) == document


def test_the_committed_fixture_is_exactly_what_render_emits(dump, document):
    """No hand edit, no CRLF, no BOM — the committed bytes are the rendered bytes."""
    committed = FIXTURE.read_bytes()
    assert committed == dump.render(document).encode("utf-8")


def test_the_schema_string_is_the_one_the_launcher_reader_requires(document):
    """The launcher's reader REFUSES an unknown schema rather than defaulting.

    So the version string is a shared contract, and bumping it on one side only
    breaks the other. Pinned here so a bump is a deliberate two-repo act.
    """
    assert document["schema"] == "charsheet_payload_contract/v1"
    assert sorted(document["payloads"]) == ["list", "sprite", "status", "thumb"]
