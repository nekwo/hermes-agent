"""T6b — brief wire descriptions + details-on-demand (Context Cost Workstream).

Pins the invariants introduced by the description trims:
  * the wire schema ships a BRIEF description for every trimmed tool;
  * the FULL original text stays retrievable via tool_describe (from the
    fork-owned mirror), plus the live (untrimmed) parameter schema;
  * tool_describe is injected into every resolved lane, independent of
    tool-search deferral.

These are behavior contracts (relations between wire brief and full docs), not
frozen byte snapshots — the exact byte totals are reported by the workstream doc,
not asserted here.
"""

import json

import pytest

from tools.registry import registry, discover_builtin_tools
from tools.tool_full_descriptions import FULL_TOOL_DESCRIPTIONS, full_tool_description
from tools.tool_search import dispatch_tool_describe, TOOL_DESCRIBE_NAME


@pytest.fixture(scope="module", autouse=True)
def _tools_discovered():
    discover_builtin_tools()


TRIMMED_TOOLS = sorted(FULL_TOOL_DESCRIPTIONS)


def test_mirror_covers_thirty_four_tools():
    """The mirror covers every tool whose wire description T6b trimmed.

    T6b shipped 35 entries. ``mission_goal_create`` was retired with the
    mission-lane removal (doc 16 acceptance), which correctly dropped its
    mirror row but left this count asserting the pre-removal number — the
    invariant has been red on every platform since. 34 is the honest count;
    the mirror is complete. The companion checks below are what actually
    keep the mirror and the live registry in step: every key must resolve to
    a registered entry with a brief wire description.
    """
    assert len(TRIMMED_TOOLS) == 34
    assert "mission_goal_create" not in TRIMMED_TOOLS


def test_full_tool_description_resolves_for_every_trimmed_tool():
    for name in TRIMMED_TOOLS:
        full = full_tool_description(name)
        assert full, f"{name}: mirror returned empty full docs"


def test_wire_ships_brief_shorter_than_full_docs():
    """Each trimmed tool's on-the-wire description is strictly shorter than the
    full docs the mirror preserves — and the lane-wide reduction is large."""
    wire_total = 0
    full_total = 0
    for name in TRIMMED_TOOLS:
        entry = registry.get_entry(name)
        assert entry is not None, name
        wire = (entry.schema or {}).get("description", "")
        full = full_tool_description(name)
        wb = len(wire.encode("utf-8"))
        fb = len(full.encode("utf-8"))
        wire_total += wb
        full_total += fb
        assert wb <= fb, f"{name}: wire brief ({wb}) longer than full ({fb})"
    # Aggregate cut is dramatic (the workstream target was >= ~68%).
    assert wire_total < full_total * 0.4, (wire_total, full_total)


def test_tool_describe_returns_full_docs_and_live_params():
    """tool_describe serves the full mirror text + the untrimmed parameters."""
    for name in ("session_search", "browser_navigate", "execute_code"):
        result = json.loads(dispatch_tool_describe({"name": name}, current_tool_defs=[]))
        assert "error" not in result, (name, result)
        assert result["description"] == full_tool_description(name)
        # Wire brief is what the schema ships; tool_describe returns MORE.
        entry = registry.get_entry(name)
        wire = (entry.schema or {}).get("description", "")
        assert len(result["description"]) > len(wire), name
        # Parameters are never trimmed — live registry schema comes back.
        assert result["parameters"] == (entry.schema or {}).get("parameters", {})


def test_skill_manage_full_docs_stay_profile_aware():
    """skill_manage's full docs resolve the profile-aware skills home live."""
    full = full_tool_description("skill_manage")
    assert "/skills/" in full


def test_shell_policy_left_the_terminal_wire_but_stays_in_full_docs():
    """The 'do not use cat/grep/sed' policy moved to the system prompt; it is
    gone from the terminal wire brief but preserved in the full docs."""
    entry = registry.get_entry("terminal")
    wire = (entry.schema or {}).get("description", "")
    assert "Do NOT use cat/head/tail" not in wire
    full = full_tool_description("terminal")
    assert "Do NOT use cat/head/tail" in full


def test_tool_describe_injected_into_resolved_lane():
    import model_tools

    defs = model_tools.get_tool_definitions(
        enabled_toolsets=["session_search", "file"], quiet_mode=True
    )
    names = [d["function"]["name"] for d in defs]
    assert TOOL_DESCRIBE_NAME in names
    assert names.count(TOOL_DESCRIBE_NAME) == 1
