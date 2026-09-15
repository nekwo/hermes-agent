"""Row 17 (mission-control-queue.md): nothing detects drift between the
canonical shared test venv and the live install's pins. These tests exercise
the pure diff logic in ``scripts/check_test_env_drift.py`` without touching
any real venv.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.check_test_env_drift import diff_pins, parse_freeze  # noqa: E402


def test_parse_freeze_skips_editable_and_comment_lines():
    lines = [
        "-e git+https://github.com/nekwo/hermes-agent@504953f6ad#egg=hermes_agent",
        "# a comment",
        "",
        "packaging==26.0",
        "mcp==1.26.0",
    ]

    assert parse_freeze(lines) == {"packaging": "26.0", "mcp": "1.26.0"}


def test_diff_pins_reports_no_drift_when_shared_pins_agree():
    live = {"packaging": "26.0", "mcp": "1.26.0"}
    test = {"packaging": "26.0", "mcp": "1.26.0", "pytest": "9.0.3"}

    assert diff_pins(live, test) == []


def test_diff_pins_reports_a_version_mismatch():
    live = {"packaging": "26.0"}
    test = {"packaging": "26.2"}

    (line,) = diff_pins(live, test)
    assert "DRIFT" in line
    assert "packaging" in line
    assert "26.0" in line and "26.2" in line


def test_diff_pins_reports_a_distribution_missing_from_the_test_venv():
    live = {"packaging": "26.0", "requests": "2.33.0"}
    test = {"packaging": "26.0"}

    (line,) = diff_pins(live, test)
    assert "MISSING" in line
    assert "requests" in line


def test_diff_pins_reports_an_untracked_addition_to_the_test_venv():
    live = {"packaging": "26.0"}
    test = {"packaging": "26.0", "some-new-thing": "1.0.0"}

    (line,) = diff_pins(live, test)
    assert "UNEXPECTED" in line
    assert "some-new-thing" in line


def test_diff_pins_does_not_flag_the_test_only_allowlist():
    live = {"packaging": "26.0"}
    test = {"packaging": "26.0", "pytest": "9.0.3", "ruff": "0.15.10"}

    assert diff_pins(live, test) == []
