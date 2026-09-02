"""The CLI-contract dump is FRESH, and its freshness check can actually red.

`scripts/dump_cli_contract.py --check` is the gate the always-on push lane
(`.githooks/pre-push`, lane A) runs on every push. It exists because the
launcher checks every Mission Control operator button's argv against a dump of
THESE parsers that is committed in the LAUNCHER — so until this gate existed, a
hermes-side argparse change left every launcher test green while that fixture
lied, and only a hand-run refresh noticed. Measured: the `--message` deletion on
`persona instance create` (`ab6254643`) left it stale for three days across five
hermes commits and six changes.

This file lives in `tests/hermes_cli/` on purpose. That directory is inside the
validated 4-directory suite lane, so the gate is reachable from BOTH lanes of
the push hook rather than only from the one that invokes the script directly.

The second test is the one that matters most: a gate that has only ever been
green is indistinguishable from one that cannot fail.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "dump_cli_contract.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "hermes_cli_contract.json"


def _load_module():
    """Import the script by path — `scripts/` is not a package."""
    spec = importlib.util.spec_from_file_location("dump_cli_contract", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dump():
    return _load_module()


def test_the_committed_dump_matches_the_live_parsers(dump):
    """The gate's green case, asserted against the real tree.

    A red here means a parser moved and the fixture did not:
        python scripts/dump_cli_contract.py --write
    and then READ the diff — a removed command or flag is not a fixture
    update, it is a launcher operator button that now exits 2.
    """
    assert dump.main(["--check"]) == 0


def test_the_check_reds_when_the_committed_dump_drifts(dump, tmp_path):
    """Round-trip the failure the gate exists for: fixture says X, parsers say Y.

    Written as a MUTATION of the real dump rather than a hand-built stub, so the
    thing being detected is a genuine drift in the recorded tree and not a
    schema mismatch the reader would have rejected anyway.
    """
    contract = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # Delete one flag from one command — the exact shape of `ab6254643`.
    victim = "harness persona instance create"
    assert victim in contract["commands"], sorted(contract["commands"])[:5]
    options = contract["commands"][victim]["options"]
    assert options, victim
    options.pop(sorted(options)[0])

    stale = tmp_path / "stale_contract.json"
    stale.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert dump.main(["--check", "--fixture", str(stale)]) == 1


def test_the_check_reds_when_the_committed_dump_is_missing(dump, tmp_path):
    """A gate whose input has vanished must fail, never pass by absence."""
    assert dump.main(["--check", "--fixture", str(tmp_path / "nope.json")]) == 1


def test_render_is_byte_stable_and_ends_in_exactly_one_newline(dump):
    """The artifact is compared byte-for-byte, so its rendering is pinned.

    Byte-equality is also what makes the launcher's copy of this walker
    checkable on sync: the two emit identical JSON for one checkout, and that
    is only a meaningful claim while the rendering is fixed here.
    """
    contract = dump.build_contract(REPO_ROOT, quiet=True)
    first = dump.render(contract)
    second = dump.render(contract)
    assert first == second
    assert first.endswith("}\n")
    assert not first.endswith("}\n\n")
    assert "\r" not in first
    # sort_keys, so the reader can diff two dumps line by line.
    assert json.loads(first) == contract


def test_the_schema_string_is_the_one_the_launcher_reader_requires(dump):
    """The launcher's Dart reader REFUSES an older schema rather than defaulting.

    So the version string is a shared contract, and bumping it on one side only
    breaks the other. Pinned here so a bump is a deliberate two-repo act.
    """
    contract = dump.build_contract(REPO_ROOT, quiet=True)
    assert contract["schema"] == "hermes_cli_contract/v4"
    assert contract["roots"] == ["harness", "profile"]
