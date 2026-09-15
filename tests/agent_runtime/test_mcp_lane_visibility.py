"""The by-design "MCP never registers on the harness lane" drop must be TYPED.

MCP tool discovery runs only for the agent entry points (``hermes_cli/main.py``
gates ``_prepare_agent_startup`` on ``_AGENT_COMMANDS``/``_AGENT_SUBCOMMANDS``);
``hermes harness ...`` is excluded on purpose. Before this module existed,
``resolve_tool_visibility`` hardcoded ``requirement_failures: []``, so a persona
that DECLARED MCP servers and ran on the harness lane reported the resulting
capability drop as nothing at all — which is how "Blocked 21 tools" became a
permission-mode goose chase (2026-07-25).

These tests pin the accounting, not the registration: nothing here should ever
grow an assertion that the harness lane registers MCP tools.
"""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest

from agent_runtime.mcp_lane import (
    HARNESS_LANE,
    MCP_NOT_REGISTERED_ON_LANE,
    MCP_REGISTERING_LANES,
    UNKNOWN_LANE,
    _lane_from_argv,
    current_entry_point_lane,
    lane_registers_mcp,
    mcp_lane_requirement_failures,
    set_entry_point_lane,
)
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


def _mcp_declaring_persona(*servers: str):
    return dataclasses.replace(_persona("qa"), required_mcp_servers=list(servers))


def _mcp_failures(visibility: dict) -> list[dict]:
    return [
        row
        for row in visibility["requirement_failures"]
        if row.get("code") == MCP_NOT_REGISTERED_ON_LANE
    ]


@pytest.fixture(autouse=True)
def _clear_lane_pin():
    # The lane pin is process-wide; never let one test's pin leak into the next.
    set_entry_point_lane(None)
    yield
    set_entry_point_lane(None)


# ── resolver behavior ───────────────────────────────────────────────────────


def test_harness_lane_reports_declared_mcp_servers_as_a_typed_drop():
    visibility = resolve_tool_visibility(
        _mcp_declaring_persona("launcher_qa"),
        ToolVisibilityOptions(entry_point_lane=HARNESS_LANE),
    )

    assert visibility["entry_point_lane"] == HARNESS_LANE
    failures = _mcp_failures(visibility)
    assert len(failures) == 1
    failure = failures[0]
    assert failure["server"] == "launcher_qa"
    assert failure["entry_point_lane"] == HARNESS_LANE
    # The operator/agent-facing text has to name the server AND the lane, or the
    # row is just a different flavour of silence.
    assert "launcher_qa" in failure["summary"]
    assert HARNESS_LANE in failure["summary"]
    assert failure["fix_hint"]
    # ...and the hint has to send the reader somewhere that EXISTS. It used to
    # end "or use launcher_qa's own harness-side contract" — that contract
    # (`qa.request_screenshot` → `stagec_mcp_visual_provider.py`) was deleted in
    # `5a1267ef60`, so the hint was routing a blocked agent at a dead lane. It
    # now names the real remedy (an MCP-registering lane) and says plainly that
    # there is no fallback.
    hint = failure["fix_hint"]
    assert "request_screenshot" not in hint
    assert "VisualProofRunner" not in hint
    assert "no harness-side fallback contract" in hint
    assert "chat`)" in hint  # the operator-runnable alternative lane
    assert "finish the turn without it" in hint


def test_harness_lane_names_every_declared_server():
    visibility = resolve_tool_visibility(
        _mcp_declaring_persona("launcher_qa", "playwright"),
        ToolVisibilityOptions(entry_point_lane=HARNESS_LANE),
    )

    assert [row["server"] for row in _mcp_failures(visibility)] == [
        "launcher_qa",
        "playwright",
    ]


def test_row_shape_matches_the_typed_issue_contract():
    # Same key contract as machine_roots.PathTokenIssue.row(), so the operator
    # surfaces that already render typed issue rows need no new case
    # (archive/2026-08-22-pre-consolidation/mission-chat-mcp-admission.md section D).
    visibility = resolve_tool_visibility(
        _mcp_declaring_persona("launcher_qa"),
        ToolVisibilityOptions(entry_point_lane=HARNESS_LANE),
    )

    assert {"code", "server", "summary", "fix_hint"} <= set(_mcp_failures(visibility)[0])


def test_chat_lane_carries_no_mcp_drop():
    # The chat lane RUNS discovery, so an absent MCP tool there is a connect or
    # config fault (already reported as `mcp_attention` readiness) — not this
    # lane-shaped drop. Claiming it here would re-create the wrong-diagnosis bug
    # in the opposite direction.
    visibility = resolve_tool_visibility(
        _mcp_declaring_persona("launcher_qa"),
        ToolVisibilityOptions(entry_point_lane="chat"),
    )

    assert visibility["entry_point_lane"] == "chat"
    assert _mcp_failures(visibility) == []


def test_persona_without_declared_mcp_servers_carries_nothing_new():
    visibility = resolve_tool_visibility(
        _persona("qa"),
        ToolVisibilityOptions(entry_point_lane=HARNESS_LANE),
    )

    assert visibility["requirement_failures"] == []


def test_visibility_still_resolves_tools_unchanged_when_the_drop_is_reported():
    # Observability only: reporting the drop must not add, remove or re-block a
    # single tool.
    plain = resolve_tool_visibility(
        _persona("qa"), ToolVisibilityOptions(entry_point_lane=HARNESS_LANE)
    )
    declaring = resolve_tool_visibility(
        _mcp_declaring_persona("launcher_qa"),
        ToolVisibilityOptions(entry_point_lane=HARNESS_LANE),
    )

    assert declaring["final_model_tools"] == plain["final_model_tools"]
    assert declaring["blocked_tool_names"] == plain["blocked_tool_names"]
    assert declaring["effective_toolsets"] == plain["effective_toolsets"]


def test_unpinned_lane_falls_back_to_the_process_lane():
    visibility = resolve_tool_visibility(_persona("qa"))

    assert visibility["entry_point_lane"] == current_entry_point_lane()


# ── lane policy ─────────────────────────────────────────────────────────────


def test_only_registering_lanes_are_treated_as_registrars():
    assert lane_registers_mcp("chat") is True
    assert lane_registers_mcp("acp") is True
    assert lane_registers_mcp(HARNESS_LANE) is False
    assert lane_registers_mcp(UNKNOWN_LANE) is False
    assert lane_registers_mcp(None) is False


def test_lane_inference_survives_a_flag_value_before_the_command():
    # `hermes -p launcher-qa chat` — a naive "first non-flag token" read would
    # call the profile name the lane.
    assert _lane_from_argv(["hermes", "-p", "launcher-qa", "chat"]) == "chat"


def test_lane_inference_reads_the_harness_lane():
    assert (
        _lane_from_argv(["hermes", "harness", "persona", "tool-diff", "qa"])
        == HARNESS_LANE
    )
    assert _lane_from_argv(["hermes", "harness", "serve", "--ndjson"]) == HARNESS_LANE


def test_lane_inference_refuses_to_guess():
    assert _lane_from_argv(["hermes"]) == UNKNOWN_LANE
    assert _lane_from_argv(["hermes", "some-plugin-command"]) == UNKNOWN_LANE
    assert _lane_from_argv([]) == UNKNOWN_LANE


def test_pinned_lane_wins_over_inference():
    set_entry_point_lane(HARNESS_LANE)
    assert current_entry_point_lane(["hermes", "chat"]) == HARNESS_LANE
    set_entry_point_lane(None)
    assert current_entry_point_lane(["hermes", "chat"]) == "chat"


def test_env_labels_the_lane_for_embedders(monkeypatch):
    monkeypatch.setenv("HERMES_ENTRY_POINT_LANE", "gateway")
    assert current_entry_point_lane(["python", "-m", "whatever"]) == "gateway"


# ── failure-row policy (pure) ───────────────────────────────────────────────


def test_no_declared_servers_means_no_row():
    assert (
        mcp_lane_requirement_failures(
            declared_servers=[], lane=HARNESS_LANE, registered_servers=[]
        )
        == []
    )


def test_registered_servers_suppress_the_row_even_on_a_non_registering_lane():
    # The row states a FACT about the registry, not a guess from the lane label.
    assert (
        mcp_lane_requirement_failures(
            declared_servers=["launcher_qa"],
            lane=HARNESS_LANE,
            registered_servers=["launcher_qa"],
        )
        == []
    )


def test_only_the_unregistered_subset_is_reported():
    rows = mcp_lane_requirement_failures(
        declared_servers=["launcher_qa", "playwright"],
        lane=HARNESS_LANE,
        registered_servers=["playwright"],
    )

    assert [row["server"] for row in rows] == ["launcher_qa"]


def test_blank_and_duplicate_server_names_are_normalized():
    rows = mcp_lane_requirement_failures(
        declared_servers=["launcher_qa", " launcher_qa ", "", None],
        lane=HARNESS_LANE,
        registered_servers=[],
    )

    assert [row["server"] for row in rows] == ["launcher_qa"]


def test_unknown_lane_still_reports_the_drop():
    # We cannot prove discovery ran, and the servers are demonstrably absent —
    # reporting the drop honestly beats reporting nothing.
    rows = mcp_lane_requirement_failures(
        declared_servers=["launcher_qa"], lane=UNKNOWN_LANE, registered_servers=[]
    )

    assert rows[0]["entry_point_lane"] == UNKNOWN_LANE


# ── what counts as "declared" ───────────────────────────────────────────────


def _bind_profile(monkeypatch, profile_home):
    from agent_runtime import profile_readiness
    from agent_runtime.profile_context import PersonaProfileBinding

    monkeypatch.setattr(
        profile_readiness,
        "resolve_persona_profile",
        lambda persona: PersonaProfileBinding(
            persona_id=persona.id,
            hermes_profile="launcher-qa",
            profile_home=profile_home,
            readiness="ready",
            summary="profile exists",
        ),
    )
    return profile_readiness


def test_profile_configured_mcp_servers_count_as_declared(tmp_path, monkeypatch):
    # The drop is not limited to `required_mcp_servers`: a profile that
    # CONFIGURES servers would have been handed those tools on a registering
    # lane, so the harness lane drops them too.
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "mcp_servers:\n  launcher_qa:\n    command: noop\n", encoding="utf-8"
    )
    profile_readiness = _bind_profile(monkeypatch, home)

    assert profile_readiness.declared_mcp_server_names(_persona("qa")) == ["launcher_qa"]


def test_declared_servers_union_required_and_configured(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "mcp_servers:\n  playwright:\n    command: noop\n", encoding="utf-8"
    )
    profile_readiness = _bind_profile(monkeypatch, home)

    assert profile_readiness.declared_mcp_server_names(
        _mcp_declaring_persona("launcher_qa")
    ) == ["launcher_qa", "playwright"]


def test_unbound_persona_does_not_inherit_the_ambient_config(tmp_path, monkeypatch):
    # A persona with no bound profile must answer the same regardless of which
    # HERMES_HOME the probe happens to run under — an ambient-config read is the
    # exact class of lie this feature retires.
    profile_readiness = _bind_profile(monkeypatch, None)

    assert profile_readiness.declared_mcp_server_names(_persona("qa")) == []
    assert profile_readiness.declared_mcp_server_names(
        _mcp_declaring_persona("launcher_qa")
    ) == ["launcher_qa"]


def test_declaration_probe_never_raises(monkeypatch):
    from agent_runtime import profile_readiness

    def _boom(_persona):
        raise RuntimeError("profile resolution exploded")

    monkeypatch.setattr(profile_readiness, "resolve_persona_profile", _boom)

    assert profile_readiness.declared_mcp_server_names(
        _mcp_declaring_persona("launcher_qa")
    ) == ["launcher_qa"]


# ── mirror guard ────────────────────────────────────────────────────────────


def _upstream_lane_gate() -> tuple[set, dict]:
    """The MCP-registration gate as ``hermes_cli/main.py`` actually spells it.

    ``MCP_REGISTERING_LANES`` mirrors an upstream module we cannot edit and must
    not import (it would drag the whole CLI into every visibility resolve). A
    mirror that drifts silently would reintroduce exactly the silent lie this
    feature retires, so read the literals out of the source text instead.
    """

    source = (_REPO_ROOT / "hermes_cli" / "main.py").read_text(encoding="utf-8")
    commands = re.search(r"^_AGENT_COMMANDS = (\{.*?\})$", source, re.M)
    subcommands = re.search(r"^_AGENT_SUBCOMMANDS = (\{.*?^\})$", source, re.M | re.S)
    assert commands and subcommands, (
        "hermes_cli/main.py no longer spells the MCP-registration gate as "
        "_AGENT_COMMANDS/_AGENT_SUBCOMMANDS; re-derive agent_runtime.mcp_lane."
        "MCP_REGISTERING_LANES from whatever replaced it."
    )
    return ast.literal_eval(commands.group(1)), ast.literal_eval(subcommands.group(1))


def test_registering_lane_mirror_matches_the_cli_gate():
    commands, subcommands = _upstream_lane_gate()

    declared = {name for name in commands if name} | set(subcommands)
    assert declared <= MCP_REGISTERING_LANES, (
        "hermes_cli/main.py registers MCP on lanes agent_runtime.mcp_lane does not "
        f"mirror: {sorted(declared - MCP_REGISTERING_LANES)}"
    )


def test_the_harness_lane_is_still_excluded_from_mcp_registration():
    # The exclusion is BY DESIGN (the harness lane is a fast control plane and
    # must never pay an MCP connect budget). If this ever flips, it is a product
    # decision — not something to be discovered from a typed row going quiet.
    commands, subcommands = _upstream_lane_gate()

    assert HARNESS_LANE not in commands
    assert HARNESS_LANE not in subcommands
    assert HARNESS_LANE not in MCP_REGISTERING_LANES
