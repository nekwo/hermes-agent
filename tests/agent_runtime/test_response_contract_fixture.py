from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hermes_cli.harness_support import ERROR_EXIT_CODES


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "response_envelopes"


def _read(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_response_fixture_manifest_pins_generated_bytes():
    rows = (FIXTURES / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines()
    manifest = {name: digest for digest, name in (row.split("  ", 1) for row in rows)}
    assert set(manifest) == {"work_list_empty.json", "work_peek_not_found.json"}
    for name, expected in manifest.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected


def test_work_peek_fixture_is_the_real_typed_not_found_response():
    fixture = _read("work_peek_not_found.json")
    assert fixture["argv"] == [
        "harness",
        "work",
        "peek",
        "terminal:fixture-missing",
        "--json",
    ]
    assert fixture["exit_code"] == ERROR_EXIT_CODES["not_found"]
    assert fixture["stdout"]["kind"] == "error"
    assert fixture["stdout"]["error"]["code"] == "not_found"


def test_work_list_fixture_is_an_accepted_empty_response():
    fixture = _read("work_list_empty.json")
    assert fixture["exit_code"] == 0
    assert fixture["stdout"]["kind"] == "list"
    assert fixture["stdout"]["item_kind"] == "running_work"
    assert fixture["stdout"]["items"] == []
