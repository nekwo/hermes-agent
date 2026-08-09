"""Chat-lane toolset cost policy (T3 + T6a, Context Cost Workstream 2026-07-18).

Covers the single chat-lane toolset chokepoint: the pure filter that drops
browser / vision / heavy-dev / file / terminal toolsets from a conversational
lane, the single-tool ``skill_manage`` cut on the blocked-tool-names lane, the
per-persona config restore override, and the ``_enabled_toolsets_for_chat`` /
``_blocked_tool_names_for_chat`` request-assembly integration (default-scoped,
restore, unbounded pass-through). Worker/dev lanes never call the chokepoint and
are covered by not-touching it.
"""

from __future__ import annotations

import textwrap

import pytest

from agent_runtime import persona_runtime as PR
from agent_runtime.chat_lane_toolsets import (
    DEFAULT_CHAT_LANE_EXCLUDED_TOOLS,
    DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS,
    chat_lane_blocked_tools,
    resolve_chat_lane_excluded_tools,
    resolve_chat_lane_excluded_toolsets,
    scope_chat_lane_toolsets,
)
from agent_runtime.config import chat_lane_restore_toolsets, load_agent_runtime_config
from agent_runtime.models import AgentPersona
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.tool_permissions import PERMISSION_MODE_UNBOUNDED
from agent_runtime.tool_visibility import ToolVisibilityOptions


# --------------------------------------------------------------------------- #
# Pure policy table — toolset exclusion.
# --------------------------------------------------------------------------- #
def test_default_excluded_set_is_browser_vision_heavy_dev_file_terminal():
    # T6a extends the T3 browser/vision/heavy-dev set with the file + terminal
    # dev-toolkit toolsets.
    assert DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS == frozenset(
        {"browser", "vision", "code_execution", "debugging", "file", "terminal"}
    )


def test_scope_drops_excluded_and_keeps_the_rest_in_order():
    result = scope_chat_lane_toolsets(
        ["file", "browser", "search", "vision", "terminal", "code_execution", "skills"]
    )
    # file + terminal now drop alongside browser/vision/code_execution.
    assert result == ["search", "skills"]


def test_scope_is_noop_when_nothing_excluded():
    keep = ["search", "session_search", "skills", "mission_goal", "agent_chat", "board"]
    assert scope_chat_lane_toolsets(keep) == keep


def test_restore_un_excludes_named_toolsets_only():
    # Restoring file keeps file; browser + terminal still drop.
    result = scope_chat_lane_toolsets(
        ["file", "browser", "terminal", "search"], restore=["file"]
    )
    assert result == ["file", "search"]


def test_resolve_excluded_honors_restore_and_ignores_noise():
    assert resolve_chat_lane_excluded_toolsets(["file", "  ", "not_a_toolset"]) == frozenset(
        {"browser", "vision", "code_execution", "debugging", "terminal"}
    )
    assert resolve_chat_lane_excluded_toolsets(None) == DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS


# --------------------------------------------------------------------------- #
# Pure policy table — single-tool (skill_manage) exclusion.
# --------------------------------------------------------------------------- #
def test_default_excluded_tools_is_skill_manage_only():
    assert DEFAULT_CHAT_LANE_EXCLUDED_TOOLS == frozenset({"skill_manage"})


def test_chat_lane_blocked_tools_default_and_restore():
    assert chat_lane_blocked_tools() == ["skill_manage"]
    # The shared restore list un-blocks the single tool by name.
    assert chat_lane_blocked_tools(restore=["skill_manage"]) == []
    # A toolset name in the restore list is a no-op for the tool exclusion.
    assert chat_lane_blocked_tools(restore=["file", "terminal"]) == ["skill_manage"]


def test_resolve_excluded_tools_ignores_noise():
    assert resolve_chat_lane_excluded_tools(["  ", "not_a_tool"]) == frozenset({"skill_manage"})
    assert resolve_chat_lane_excluded_tools(None) == DEFAULT_CHAT_LANE_EXCLUDED_TOOLS


# --------------------------------------------------------------------------- #
# Per-persona config restore override.
# --------------------------------------------------------------------------- #
def _write_config(text: str):
    from hermes_constants import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_config_restore_reads_per_persona_key():
    path = _write_config(
        """
        agent_runtime:
          personas:
            neko_supervisor:
              chat_lane_restore_toolsets: [file, terminal, skill_manage]
        """
    )
    cfg = load_agent_runtime_config(config_path=path)
    assert chat_lane_restore_toolsets("neko_supervisor", cfg) == ["file", "terminal", "skill_manage"]


def test_config_restore_honors_alice_neko_alias():
    # A config keyed on the legacy alice_supervisor name still restores Neko.
    path = _write_config(
        """
        agent_runtime:
          personas:
            alice_supervisor:
              chat_lane_restore_toolsets: [terminal]
        """
    )
    cfg = load_agent_runtime_config(config_path=path)
    assert chat_lane_restore_toolsets("neko_supervisor", cfg) == ["terminal"]


def test_config_restore_absent_is_empty():
    path = _write_config("agent_runtime:\n  personas: {}\n")
    cfg = load_agent_runtime_config(config_path=path)
    assert chat_lane_restore_toolsets("neko_supervisor", cfg) == []


def test_config_restore_resolves_root_config_under_profile_home(monkeypatch, tmp_path):
    # Regression (2026-07-23): the CLI bootstrap redirects a bare invocation
    # into the sticky active profile's home, so a cfg-less call used to read
    # THAT profile's config.yaml and the operator's root-config restore
    # rulings were silently dead (live: alice active → dev lane lost its
    # restored file/terminal toolsets). The restore knob is harness-global
    # operator policy: with no explicit cfg it must resolve against the ROOT
    # config.yaml even when the process home points into a profile.
    import textwrap as _tw

    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "tester"
    profile_home.mkdir(parents=True)
    (root / "config.yaml").write_text(
        _tw.dedent(
            """
            agent_runtime:
              personas:
                dev:
                  chat_lane_restore_toolsets: [file, terminal]
            """
        ),
        encoding="utf-8",
    )
    # The profile's own config both omits the key for `dev` and tries to
    # shadow it for `qa` — neither may influence the cfg-less resolution.
    (profile_home / "config.yaml").write_text(
        _tw.dedent(
            """
            agent_runtime:
              personas:
                qa:
                  chat_lane_restore_toolsets: [browser]
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    assert chat_lane_restore_toolsets("dev") == ["file", "terminal"]
    assert chat_lane_restore_toolsets("qa") == []


# --------------------------------------------------------------------------- #
# Request-assembly integration at the single chokepoint.
# --------------------------------------------------------------------------- #
def _persona_with_dev_toolkit():
    # A supervisor persona whose configured toolset surface includes the full
    # dev toolkit (file/terminal/browser/vision/code_execution) — the lane the
    # policy must scope even though the role allows those toolsets.
    return AgentPersona(
        id="neko_supervisor",
        display_name="Neko",
        role="alice_supervisor",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal", "browser", "vision", "code_execution", "skills"],
        system_prompt_path="personas/neko_supervisor/system.md",
    )


def test_default_neko_chat_lane_excludes_dev_toolkit(bounded_chat_session):
    # The cost policy only applies on the BOUNDED lane, and since the 2026-08-09
    # ruling the runtime default is `unbounded` — so this test states the tier it
    # is about instead of inheriting it.
    neko = next(p for p in sample_personas() if p.id == "neko_supervisor")
    enabled = PR._enabled_toolsets_for_chat(
        neko, session_id=bounded_chat_session(neko.id)
    )
    # browser/vision/code_execution (T3) + file/terminal (T6a) all drop.
    assert not {"browser", "vision", "code_execution", "file", "terminal"} & set(enabled)
    # The supervision capabilities the lane legitimately keeps are still present.
    assert {"session_search", "agent_chat", "skills"}.issubset(set(enabled))
    assert "mission_goal" not in enabled


def test_chat_lane_scopes_dev_toolkit_when_persona_carries_it(bounded_chat_session):
    persona = _persona_with_dev_toolkit()
    enabled = PR._enabled_toolsets_for_chat(
        persona, session_id=bounded_chat_session(persona.id)
    )
    assert not {"browser", "vision", "code_execution", "file", "terminal"} & set(enabled)
    # skills survives as a toolset (skill_manage is cut at the tool level, below).
    assert "skills" in enabled


def test_chat_lane_restore_keeps_named_toolsets(monkeypatch, bounded_chat_session):
    # Per-persona config restore flows through the chokepoint: file + terminal
    # survive; a non-restored excluded toolset (browser) still drops.
    monkeypatch.setattr(PR, "chat_lane_restore_toolsets", lambda persona_id: ["file", "terminal"])
    persona = _persona_with_dev_toolkit()
    enabled = PR._enabled_toolsets_for_chat(
        persona, session_id=bounded_chat_session(persona.id)
    )
    assert "file" in enabled and "terminal" in enabled
    assert "browser" not in enabled and "vision" not in enabled


def test_unbounded_permission_mode_is_not_scoped(monkeypatch):
    # Unbounded is the operator's explicit full-capability escape hatch — the
    # cost policy must not silently strip its browser/vision/file/terminal.
    monkeypatch.setattr(
        PR,
        "permission_options_for_chat",
        lambda persona, *, session_id: ToolVisibilityOptions(
            permission_mode=PERMISSION_MODE_UNBOUNDED
        ),
    )
    enabled = PR._enabled_toolsets_for_chat(_persona_with_dev_toolkit(), session_id="s1")
    assert {"browser", "vision", "file", "terminal"}.issubset(set(enabled))
    assert "mission_goal" not in enabled


# --------------------------------------------------------------------------- #
# Single-tool (skill_manage) cut at the blocked-tool-names chokepoint.
# --------------------------------------------------------------------------- #
def test_default_neko_chat_lane_blocks_skill_manage_but_keeps_read_only_skill_tools(
    bounded_chat_session,
):
    neko = next(p for p in sample_personas() if p.id == "neko_supervisor")
    blocked = PR._blocked_tool_names_for_chat(
        neko, session_id=bounded_chat_session(neko.id)
    )
    assert "skill_manage" in blocked
    # Read-only skill recall stays available (never in the block list).
    assert "skill_search" not in blocked
    assert "skill_view" not in blocked
    assert "skills_list" not in blocked


def test_chat_lane_restore_unblocks_skill_manage(monkeypatch, bounded_chat_session):
    # BOUNDED on purpose: with the unbounded runtime default the block list is
    # empty for every reason at once, so this would assert nothing about the
    # restore knob it exists to pin.
    neko = next(p for p in sample_personas() if p.id == "neko_supervisor")
    session_id = bounded_chat_session(neko.id)
    assert "skill_manage" in PR._blocked_tool_names_for_chat(neko, session_id=session_id)
    monkeypatch.setattr(PR, "chat_lane_restore_toolsets", lambda persona_id: ["skill_manage"])
    blocked = PR._blocked_tool_names_for_chat(neko, session_id=session_id)
    assert "skill_manage" not in blocked


def test_unbounded_permission_mode_does_not_block_skill_manage(monkeypatch):
    neko = next(p for p in sample_personas() if p.id == "neko_supervisor")
    monkeypatch.setattr(
        PR,
        "permission_options_for_chat",
        lambda persona, *, session_id: ToolVisibilityOptions(
            permission_mode=PERMISSION_MODE_UNBOUNDED
        ),
    )
    # Unbounded returns no blocks at all — skill_manage stays callable.
    assert PR._blocked_tool_names_for_chat(neko, session_id="s1") == []
