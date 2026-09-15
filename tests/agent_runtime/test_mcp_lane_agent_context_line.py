"""The agent hears the same MCP truth the operator does — with the flag OFF.

G5, the flag-off blind spot. ``mcp_lane`` made the harness lane's MCP drop TYPED
for the OPERATOR (``requirement_failures``), and the admission design's §D3 gave
the AGENT its own line — but only when ``agent_runtime.mcp_admission.enabled``
is true. The flag is false in every deployment today, so a persona whose
declared server is dark on this lane got a tool list with no
``mcp__<server>__*`` entries and no explanation, while the operator's preview
said exactly what happened. An unexplained capability gap is the W3 failure the
design names: the agent improvises (this is the road that ends in ``pwsh -File``
in agent output), and no permission mode can ever expose a tool that was never
registered.

These tests pin the flag-off line AND the two invariants that make it safe to
render there: it costs no root-config load and no persona-profile read, and a
persona with nothing declared still pays nothing (so the volatile envelope is
byte-identical for every turn that had nothing to be told).

Nothing here should ever grow an assertion that the harness lane REGISTERS MCP
tools. This is accounting, not registration.
"""

from __future__ import annotations

import dataclasses

import pytest

from agent_runtime.mcp_admission import render_mcp_admission_line
from agent_runtime.mcp_lane import (
    HARNESS_LANE,
    MCP_CONTEXT_LINE_PREFIX,
    MCP_CONTEXT_LINE_TAIL,
    MCP_NOT_REGISTERED_ON_LANE,
    mcp_lane_requirement_failures,
    mission_chat_mcp_lane_line,
    render_mcp_lane_line,
    set_entry_point_lane,
)
from tests.agent_runtime.persona_samples import sample_personas


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


def _qa_declaring(*servers: str):
    return dataclasses.replace(_persona("qa"), required_mcp_servers=list(servers))


@pytest.fixture(autouse=True)
def _harness_lane():
    # The lane pin is process-wide; never let one test's pin leak into the next.
    set_entry_point_lane(HARNESS_LANE)
    yield
    set_entry_point_lane(None)


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    import agent_runtime.persona_runtime as persona_runtime

    monkeypatch.setattr(persona_runtime, "admission_enabled", lambda: False)


# ── the renderer ────────────────────────────────────────────────────────────


def test_the_line_names_the_server_and_the_typed_code():
    line = render_mcp_lane_line(
        mcp_lane_requirement_failures(
            declared_servers=["launcher_qa"],
            lane=HARNESS_LANE,
            registered_servers=[],
        )
    )

    assert line.startswith(f"{MCP_CONTEXT_LINE_PREFIX} launcher_qa ({MCP_NOT_REGISTERED_ON_LANE})")
    # The whole point of the line: stop the agent chasing a permission problem
    # and stop it inventing a shell workaround.
    assert "not a permission problem" in line
    assert "PowerShell" in line
    # ...and, since the harness-side screenshot contract was deleted, tell it
    # plainly that there is nothing to fall back to, what to do instead, and who
    # can unblock it. Pointing at a lane that does not exist CAUSES improvising.
    assert "no harness-side fallback contract" in line
    assert "closed for the turn" in line
    assert "finish the turn" in line
    assert "Only an operator can lift this" in line


def test_the_line_never_points_at_the_deleted_screenshot_contract():
    """Regression fence: ``qa.request_screenshot`` / ``VisualProofRunner`` /
    ``stagec_mcp_visual_provider`` no longer exist. Naming one in agent-facing
    text routes a denied agent at a lane that is not there."""

    line = render_mcp_lane_line(
        mcp_lane_requirement_failures(
            declared_servers=["launcher_qa"],
            lane=HARNESS_LANE,
            registered_servers=[],
        )
    )

    assert "request_screenshot" not in line
    assert "VisualProofRunner" not in line
    # The shared constant itself, not just one rendering of it.
    assert "request_screenshot" not in MCP_CONTEXT_LINE_TAIL


def test_nothing_dropped_renders_nothing():
    """A turn that HAS its tools must not pay a line explaining a mechanism it
    never met."""

    assert render_mcp_lane_line([]) == ""
    assert render_mcp_lane_line(None) == ""


def test_one_entry_per_server_first_code_wins():
    line = render_mcp_lane_line(
        [
            {"code": "first", "server": "a"},
            {"code": "second", "server": "a"},
            {"code": "third", "server": "b"},
        ]
    )

    assert line.count("a (first)") == 1
    assert "a (second)" not in line
    assert "b (third)" in line


def test_the_flag_off_and_flag_on_lines_are_ONE_voice():
    """Drift guard. The agent must not learn that "MCP tools:" means two
    different registers depending on a flag it cannot see. Everything after the
    ``<server> (<code>)`` detail is shared verbatim with the admission
    renderer — if either side is reworded, this fails."""

    from agent_runtime.mcp_admission import (
        LANE_MISSION_CHAT,
        MCP_ADMISSION_TIMEOUT,
        McpAdmission,
        McpAdmissionDenial,
    )

    admission_line = render_mcp_admission_line(
        McpAdmission(
            lane=LANE_MISSION_CHAT,
            role="qa",
            permission_mode="profile_default",
            enabled=True,
            requested=("launcher_qa",),
            denied=(
                McpAdmissionDenial(
                    server="launcher_qa", code=MCP_ADMISSION_TIMEOUT, summary="x"
                ),
            ),
        )
    )
    lane_line = render_mcp_lane_line(
        [{"code": MCP_NOT_REGISTERED_ON_LANE, "server": "launcher_qa"}]
    )

    marker = " — "
    assert admission_line.split(marker, 1)[1] == lane_line.split(marker, 1)[1]
    assert admission_line.split(marker, 1)[0].startswith(MCP_CONTEXT_LINE_PREFIX)
    assert lane_line.split(marker, 1)[0].startswith(MCP_CONTEXT_LINE_PREFIX)


# ── the per-turn seam ───────────────────────────────────────────────────────


def test_the_agent_is_told_about_its_declared_dark_server_with_the_flag_off():
    """THE G5 FIX. This returned "" before 2026-07-26."""

    import agent_runtime.persona_runtime as persona_runtime

    line = persona_runtime.mission_chat_admission_line(
        _qa_declaring("launcher_qa"), session_id=None
    )

    assert "launcher_qa" in line
    assert MCP_NOT_REGISTERED_ON_LANE in line


def test_a_persona_that_declares_nothing_still_pays_nothing():
    """Byte-stability where it is owed: the overwhelming majority of turns
    declare no MCP server, and their volatile envelope must not change."""

    import agent_runtime.persona_runtime as persona_runtime

    assert (
        persona_runtime.mission_chat_admission_line(_qa_declaring(), session_id=None)
        == ""
    )


def test_an_mcp_registering_lane_says_nothing():
    """``hermes -p launcher-qa chat`` DOES run discovery. Claiming a drop there
    would be the mirror-image lie of the one this fixes."""

    set_entry_point_lane("chat")

    assert mission_chat_mcp_lane_line(_qa_declaring("launcher_qa")) == ""


def test_a_server_actually_registered_in_process_says_nothing(monkeypatch):
    """Registry ground truth beats the lane label — an admitted server that DID
    register must not be reported as dropped."""

    import agent_runtime.mcp_lane as mcp_lane

    monkeypatch.setattr(
        mcp_lane, "registered_mcp_server_names", lambda: frozenset({"launcher_qa"})
    )

    assert mission_chat_mcp_lane_line(_qa_declaring("launcher_qa")) == ""


def test_the_flag_off_line_costs_no_root_config_load_and_no_profile_read(monkeypatch):
    """The admission design's flag-off invariant, kept while the line went live.

    Honesty is not allowed to buy itself a config load: the declaration read is
    the persona's own ``required_mcp_servers`` plus the existing role policy,
    which is in-memory arithmetic. The wider profile-config union stays on the
    OPERATOR's rows, where it is already paid for.
    """

    import agent_runtime.mcp_admission as mcp_admission
    import agent_runtime.parse_cache as parse_cache
    import agent_runtime.persona_runtime as persona_runtime
    import agent_runtime.profile_context as profile_context

    def _never_config(*_args, **_kwargs):
        raise AssertionError("the flag-off path must not load root runtime config")

    def _never_profile(*_args, **_kwargs):
        raise AssertionError("the flag-off path must not read the persona profile")

    monkeypatch.setattr(mcp_admission, "resolve_mcp_admission", _never_config)
    monkeypatch.setattr(persona_runtime, "resolve_mcp_admission", _never_config)
    monkeypatch.setattr(parse_cache, "cached_yaml_file", _never_profile)
    monkeypatch.setattr(profile_context, "resolve_persona_profile", _never_profile)

    line = persona_runtime.mission_chat_admission_line(
        _qa_declaring("launcher_qa"), session_id=None
    )

    assert MCP_NOT_REGISTERED_ON_LANE in line


def test_the_line_never_fails_a_turn(monkeypatch):
    import agent_runtime.mcp_lane as mcp_lane

    def _boom(*_a, **_k):
        raise RuntimeError("the registry is wedged")

    monkeypatch.setattr(mcp_lane, "mcp_lane_requirement_failures", _boom)

    assert mission_chat_mcp_lane_line(_qa_declaring("launcher_qa")) == ""


def test_the_role_policy_is_imported_never_re_implemented():
    """The design's standing rule: ``_effective_required_mcp_servers`` is THE
    role→server policy. A second copy in ``mcp_lane`` would be a parallel
    authority that drifts the day the first one changes."""

    import inspect

    import agent_runtime.mcp_lane as mcp_lane

    source = inspect.getsource(mcp_lane.mission_chat_mcp_lane_line)
    assert "_effective_required_mcp_servers" in source
    assert "launcher_qa" not in source.split('"""')[2]


def test_the_mcp_line_stays_its_own_voice_on_the_volatile_tail():
    """The MCP line must never be folded into the capability-drop block: the two
    are resolved at different lifecycle points and gated differently, and giving
    one fact two voices is how an agent learns to discount both."""

    from agent_runtime.runtime_hud import render_capability_block

    capability_block = render_capability_block(
        {
            "toolsets_dropped": ["terminal"],
            "restorable_via": ["agent_runtime.chat_lane.keep_toolsets"],
        }
    )
    line = render_mcp_lane_line(
        [{"code": MCP_NOT_REGISTERED_ON_LANE, "server": "launcher_qa"}]
    )

    # Same list grammar (both ride the same tail), separate sentences: the MCP
    # line owns the "MCP tools:" register and the capability block owns
    # "Dropped on this lane:". Neither renderer may emit the other's line.
    assert line.startswith(MCP_CONTEXT_LINE_PREFIX)
    assert "\n" not in line
    assert MCP_CONTEXT_LINE_PREFIX not in capability_block
    assert "Dropped on this lane:" not in line
