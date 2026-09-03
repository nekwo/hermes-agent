"""The S0a ratchet: 43 tools, 0 withheld, 0 requirement failures, per persona.

Plan: ``docs/agent-runtime-harness/planned/s0a-atlas-cleanup.md`` §2 A3.

Baseline this replaces, measured 2026-09-03 on the live box with
``HERMES_HOME=X:\\Eternia\\.hermes``: every one of the four mission personas
resolved 32 registered toolsets, **79** callable tools, **17** withheld (12
``kanban_*`` + 5 ``feishu_*``, hygiene-blocked on every turn because "everything"
included them) and 1-3 ``requirement_failures`` — every one of those for a server
``--explain-mcp`` reported as ADMITTED. After A1-A3: **43 / 0 / 0**, with
``model_tool_tokens`` 2142 -> 1149.

Three things are pinned here, and none of them is a style preference:

1. The COUNT, so a toolset silently joining or leaving ``harness_core`` shows up
   as a diff instead of as prompt weight nobody notices.
2. ``withheld == 0``, which is only true while the declared set names no
   hygiene-blocked toolset. The anti-vacuity arm below declares ``kanban`` /
   ``feishu_*`` and asserts all 17 come BACK, so the zero is a property of the
   declaration and not of a broken counter.
3. ``requirement_failures == []`` for an ADMITTED server (R-S0a-4), with the
   denial arm still speaking.
"""

from __future__ import annotations

import textwrap

import pytest

from agent_runtime.parse_cache import clear_parse_cache
from agent_runtime.personas import declared_lane_toolsets, effective_toolsets
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility
from tests.agent_runtime.persona_samples import sample_personas


HARNESS_CORE_MEMBERS = [
    "agent_chat", "board", "clarify", "delegation", "terminal", "file",
    "web", "browser", "browser-cdp", "skills", "memory", "todo",
    "session_search", "vision", "code_execution",
]
DECLARED_TOOL_COUNT = 43
DECLARED_TOKEN_ESTIMATE = 1149
MISSION_PERSONAS = ("neko_supervisor", "dev", "backend_dev", "qa")


@pytest.fixture
def declaring_profile(bundled_persona_profiles):
    """Write a profile's ``toolsets:`` and hand back the personas bound to it."""

    from hermes_cli.profiles import get_profile_dir

    def _write(body: str, *, profile: str) -> None:
        home = get_profile_dir(profile)
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
        clear_parse_cache()

    return _write


def _persona(persona_id: str):
    return next(p for p in sample_personas() if p.id == persona_id)


def _preview(persona, **options):
    return resolve_tool_visibility(
        persona, ToolVisibilityOptions(permission_mode="unbounded", permission_source="test", **options)
    )


# ── 1. the pinned surface ────────────────────────────────────────────────────


@pytest.mark.parametrize("persona_id", MISSION_PERSONAS)
def test_every_mission_persona_resolves_the_same_declared_43(persona_id):
    persona = _persona(persona_id)

    preview = _preview(persona)

    assert preview["effective_toolsets"] == HARNESS_CORE_MEMBERS
    assert preview["configured_toolsets"] == HARNESS_CORE_MEMBERS
    assert preview["final_tool_count"] == DECLARED_TOOL_COUNT
    assert preview["model_tool_tokens"] == DECLARED_TOKEN_ESTIMATE
    assert preview["availability_counts"]["withheld"] == 0
    assert preview["withheld_tools"] == []
    assert preview["requirement_failures"] == []


@pytest.mark.parametrize("persona_id", MISSION_PERSONAS)
def test_the_declaration_is_the_lane_default_and_says_so(persona_id):
    declaration = declared_lane_toolsets(_persona(persona_id))

    assert declaration.declared == ("harness_core",)
    assert declaration.source in {"lane_default", "profile_unresolved"}


def test_the_fork_lanes_and_the_conversational_core_are_all_callable():
    """The count alone would pass with 43 of the wrong tools."""

    final = set(_preview(_persona("neko_supervisor"))["final_model_tools"])

    assert {
        "agent_chat_threads", "agent_chat_send", "agent_chat_open",
        "agent_chat_dispatches", "agent_chat_log_path",
        "board_card_add", "board_cards", "clarify", "delegate_task",
        "terminal", "read_file", "write_file", "patch", "search_files",
        "web_search", "session_search", "skill_search", "vision_analyze",
        "execute_code", "memory", "todo",
    } <= final


def test_no_hygiene_name_is_even_a_candidate_any_more():
    """Before A1 these 17 were withheld on EVERY turn — resolved as candidates by
    ``all_registered_toolsets()`` and then blocked. Now they are not declared."""

    from agent_runtime.personas import REGISTRY_HYGIENE_BLOCKED_TOOLS

    preview = _preview(_persona("qa"))

    assert not (set(preview["persona_candidate_tools"]) & REGISTRY_HYGIENE_BLOCKED_TOOLS)
    assert not (set(preview["final_model_tools"]) & REGISTRY_HYGIENE_BLOCKED_TOOLS)


# ── 2. the declaration decides — both directions ─────────────────────────────


def test_an_explicit_harness_core_declaration_is_the_same_43(declaring_profile):
    declaring_profile("toolsets:\n  - harness_core\n", profile="gpt-launcher")


    preview = _preview(_persona("dev"))

    assert preview["toolset_declaration"]["source"] == "profile_config"
    assert preview["final_tool_count"] == DECLARED_TOOL_COUNT
    assert preview["availability_counts"]["withheld"] == 0


def test_a_declaration_that_names_the_hygiene_toolsets_brings_the_17_back(
    declaring_profile,
):
    """THE anti-vacuity arm for ``withheld == 0``.

    The zero is a property of WHAT IS DECLARED, not of a counter that stopped
    counting: name ``kanban`` / ``feishu_*`` and all 17 are candidates again and
    all 17 are withheld, exactly as they were on every turn before A1. The red an
    operator would see here is a diff against 0, which is the cue to fix the
    declaration.
    """

    from agent_runtime.personas import REGISTRY_HYGIENE_BLOCKED_TOOLS

    declaring_profile(
        """
        toolsets:
          - harness_core
          - kanban
          - feishu_doc
          - feishu_drive
        """,
        profile="gpt-launcher",
    )

    preview = _preview(_persona("dev"))

    assert preview["toolset_declaration"]["source"] == "profile_config"
    assert preview["availability_counts"]["withheld"] == len(REGISTRY_HYGIENE_BLOCKED_TOOLS) == 17
    assert preview["final_tool_count"] == DECLARED_TOOL_COUNT  # none of them SHIP
    assert {entry["reason"] for entry in preview["withheld_tools"]} == {"registry_hygiene"}


def test_an_opt_in_integration_is_added_by_name_and_counted(declaring_profile):
    """The documented escape hatch (plan §5): a persona that wants a non-core
    toolset back writes it beside the bundle, and the count moves visibly."""

    declaring_profile(
        """
        toolsets:
          - harness_core
          - spotify
        """,
        profile="gpt-launcher",
    )

    preview = _preview(_persona("dev"))

    assert preview["effective_toolsets"] == HARNESS_CORE_MEMBERS + ["spotify"]
    assert preview["final_tool_count"] == DECLARED_TOOL_COUNT + 7


def test_a_static_bundle_declaration_resolves_by_registry_membership(declaring_profile):
    """A LIMIT of this preview, pinned so it is not mistaken for a promise.

    ``harness_core`` is composed of toolset NAMES on purpose. The preview counts
    tools by REGISTRY membership (``_tool_names_for_toolsets`` walks
    ``registry.get_all_tool_names()`` and asks each tool which toolset it was
    registered into), while a turn resolves tool NAMES through
    ``resolve_toolset`` (static list ∪ registry). For a bundle whose members are
    a static TOOL list — ``hermes-cli``, ``coding`` — those two lenses disagree:
    no tool is registered under the name ``hermes-cli``, so the preview sees 0
    where a turn would ship 62. Naming member TOOLSETS is what makes the two
    lenses agree at 43 for the declaration this stage ships; an operator who
    declares a static bundle instead gets an under-reporting preview, which is a
    row for whoever unifies the two resolvers, not a thing this stage fixed.
    """

    from toolsets import resolve_toolset

    declaring_profile(
        """
        toolsets:
          - hermes-cli
          - spotify
        """,
        profile="gpt-launcher",
    )

    preview = _preview(_persona("dev"))

    assert preview["toolset_declaration"]["source"] == "profile_config"
    assert preview["final_tool_count"] == 7  # spotify only, by registry membership
    assert len(resolve_toolset("hermes-cli")) == 62  # what a turn would resolve


def test_the_bounded_cost_policy_still_cuts_the_declared_set(bounded_chat_session):
    """The expansion to member NAMES is what keeps this working: a lane handed
    ``["harness_core"]`` unexpanded would slip past a policy that drops
    ``browser`` by name, and the bounded tier would silently widen."""

    from agent_runtime import persona_runtime as PR

    neko = _persona("neko_supervisor")
    enabled = PR._enabled_toolsets_for_chat(neko, session_id=bounded_chat_session(neko.id))

    assert not {"browser", "vision", "code_execution", "file", "terminal"} & set(enabled)
    assert {"agent_chat", "board", "session_search", "skills"} <= set(enabled)


def test_the_chokepoint_and_the_preview_agree_on_the_final_tool_list():
    """The T9b parity property, re-asserted on the new authority.

    ``apply_chat_lane_tool_scope`` threads the REAL chat-lane resolution onto the
    preview so ``final_model_tools`` is byte-identical to the schema the turn
    ships. Two lenses reach the tools differently — the chat lane resolves by
    tool name through ``resolve_toolset``, the preview by registry membership —
    which is exactly why ``browser-cdp`` is a named member of ``harness_core``.
    """

    from agent_runtime.persona_runtime import apply_chat_lane_tool_scope

    persona = _persona("dev")
    options = ToolVisibilityOptions(permission_mode="unbounded", permission_source="test")
    apply_chat_lane_tool_scope(persona, options, session_id="chat-parity")

    scoped = resolve_tool_visibility(persona, options)
    plain = _preview(persona)

    assert scoped["final_model_tools"] == plain["final_model_tools"]
    assert scoped["final_tool_count"] == DECLARED_TOOL_COUNT
    assert options.enabled_toolsets == effective_toolsets(persona)


# ── 3. admitted is not a failure (R-S0a-4) ───────────────────────────────────


_ADMITTING_PROFILE = """
toolsets:
  - harness_core
mcp_servers:
  launcher_qa:
    command: stagec_qa_mcp_server.exe
    args: ["--stdio"]
"""


@pytest.fixture
def admission_enabled_root(tmp_path, monkeypatch, declaring_profile):
    """Turn the admission flag on in a hermetic ROOT config."""

    root = tmp_path / "hermes-root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        textwrap.dedent(
            """
            agent_runtime:
              mcp_admission:
                enabled: true
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    declaring_profile(_ADMITTING_PROFILE, profile="qa")
    clear_parse_cache()
    return root


def test_an_admitted_server_is_reported_not_failed(admission_enabled_root):
    preview = _preview(_persona("qa"))

    assert preview["requirement_failures"] == []
    assert preview["admitted_mcp_servers"] == ["launcher_qa"]
    assert preview["final_tool_count"] == DECLARED_TOOL_COUNT


def test_a_denied_server_still_reports_its_denial(admission_enabled_root, monkeypatch):
    """ANTI-VACUITY: the zero above must come from admission, not from a
    requirement-failure lane that stopped speaking."""

    from agent_runtime import tool_visibility as TV
    from agent_runtime.mcp_admission import McpAdmissionDenial

    real = TV.resolve_mcp_admission

    def _denied(persona, **kwargs):
        import dataclasses

        admission = real(persona, **kwargs)
        return dataclasses.replace(
            admission,
            server_names=(),
            denied=(
                McpAdmissionDenial(
                    server="launcher_qa",
                    code="mcp_admission_timeout",
                    summary="registration exceeded its budget",
                    fix_hint="retry the turn",
                ),
            ),
        )

    monkeypatch.setattr(TV, "resolve_mcp_admission", _denied)

    preview = _preview(_persona("qa"))

    assert [row["code"] for row in preview["requirement_failures"]] == [
        "mcp_admission_timeout"
    ]
    assert preview["admitted_mcp_servers"] == []


def test_admission_disabled_leaves_the_key_empty_and_the_r0_rows_intact(
    declaring_profile,
):
    """The flag-off path — the default — must not change what R0 reported."""

    declaring_profile(_ADMITTING_PROFILE, profile="qa")

    preview = _preview(_persona("qa"))

    assert preview["admitted_mcp_servers"] == []
    assert [row["code"] for row in preview["requirement_failures"]] == [
        "mcp_not_registered_on_lane"
    ]
