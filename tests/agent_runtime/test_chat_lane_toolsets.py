"""Chat-lane toolset cost policy (T3, Context Cost Workstream 2026-07-18).

Covers the single chat-lane toolset chokepoint: the pure filter that drops
browser / vision / heavy-dev toolsets from a conversational lane, the
per-persona config restore override, and the ``_enabled_toolsets_for_chat``
request-assembly integration (default-scoped, restore, unbounded pass-through).
Worker/dev lanes never call the chokepoint and are covered by not-touching it.
"""

from __future__ import annotations

import textwrap

import pytest

from agent_runtime import persona_runtime as PR
from agent_runtime.chat_lane_toolsets import (
    DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS,
    resolve_chat_lane_excluded_toolsets,
    scope_chat_lane_toolsets,
)
from agent_runtime.config import chat_lane_restore_toolsets, load_agent_runtime_config
from agent_runtime.models import AgentPersona
from agent_runtime.personas import AgentRole, default_personas
from agent_runtime.tool_permissions import PERMISSION_MODE_UNBOUNDED
from agent_runtime.tool_visibility import ToolVisibilityOptions


# --------------------------------------------------------------------------- #
# Pure policy table.
# --------------------------------------------------------------------------- #
def test_default_excluded_set_is_browser_vision_heavy_dev():
    assert DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS == frozenset(
        {"browser", "vision", "code_execution", "debugging"}
    )


def test_scope_drops_excluded_and_keeps_the_rest_in_order():
    result = scope_chat_lane_toolsets(
        ["file", "browser", "search", "vision", "terminal", "code_execution", "skills"]
    )
    assert result == ["file", "search", "terminal", "skills"]


def test_scope_is_noop_when_nothing_excluded():
    keep = ["file", "search", "terminal", "session_search", "skills", "mission_goal"]
    assert scope_chat_lane_toolsets(keep) == keep


def test_restore_un_excludes_named_toolsets_only():
    # Restoring browser keeps browser; vision + code_execution still drop.
    result = scope_chat_lane_toolsets(
        ["file", "browser", "vision", "code_execution"], restore=["browser"]
    )
    assert result == ["file", "browser"]


def test_resolve_excluded_honors_restore_and_ignores_noise():
    assert resolve_chat_lane_excluded_toolsets(["browser", "  ", "not_a_toolset"]) == frozenset(
        {"vision", "code_execution", "debugging"}
    )
    assert resolve_chat_lane_excluded_toolsets(None) == DEFAULT_CHAT_LANE_EXCLUDED_TOOLSETS


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
              chat_lane_restore_toolsets: [browser, vision]
        """
    )
    cfg = load_agent_runtime_config(config_path=path)
    assert chat_lane_restore_toolsets("neko_supervisor", cfg) == ["browser", "vision"]


def test_config_restore_honors_alice_neko_alias():
    # A config keyed on the legacy alice_supervisor name still restores Neko.
    path = _write_config(
        """
        agent_runtime:
          personas:
            alice_supervisor:
              chat_lane_restore_toolsets: [browser]
        """
    )
    cfg = load_agent_runtime_config(config_path=path)
    assert chat_lane_restore_toolsets("neko_supervisor", cfg) == ["browser"]


def test_config_restore_absent_is_empty():
    path = _write_config("agent_runtime:\n  personas: {}\n")
    cfg = load_agent_runtime_config(config_path=path)
    assert chat_lane_restore_toolsets("neko_supervisor", cfg) == []


# --------------------------------------------------------------------------- #
# Request-assembly integration at the single chokepoint.
# --------------------------------------------------------------------------- #
def _persona_with_browser_vision():
    # A supervisor persona whose configured toolset surface includes browser +
    # vision (e.g. an operator added them, or a QA-shaped chat) — the lane the
    # policy must scope even though the role allows those toolsets.
    return AgentPersona(
        id="neko_supervisor",
        display_name="Neko",
        role=AgentRole.ALICE_SUPERVISOR.value,
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "search", "terminal", "browser", "vision", "code_execution", "skills"],
        system_prompt_path="personas/neko_supervisor/system.md",
    )


def test_default_neko_chat_lane_excludes_code_execution():
    neko = next(p for p in default_personas() if p.id == "neko_supervisor")
    enabled = PR._enabled_toolsets_for_chat(neko, session_id=None)
    assert "code_execution" not in enabled
    assert "browser" not in enabled and "vision" not in enabled
    # The supervision capabilities the lane legitimately keeps are still present.
    assert {"file", "terminal", "mission_goal", "skills"}.issubset(set(enabled))


def test_chat_lane_scopes_browser_vision_when_persona_carries_them():
    enabled = PR._enabled_toolsets_for_chat(_persona_with_browser_vision(), session_id=None)
    assert "browser" not in enabled
    assert "vision" not in enabled
    assert "code_execution" not in enabled
    assert "file" in enabled and "terminal" in enabled


def test_chat_lane_restore_keeps_named_toolset(monkeypatch):
    # Per-persona config restore flows through the chokepoint: browser survives.
    monkeypatch.setattr(PR, "chat_lane_restore_toolsets", lambda persona_id: ["browser"])
    enabled = PR._enabled_toolsets_for_chat(_persona_with_browser_vision(), session_id=None)
    assert "browser" in enabled
    # A non-restored excluded toolset still drops.
    assert "vision" not in enabled


def test_unbounded_permission_mode_is_not_scoped(monkeypatch):
    # Unbounded is the operator's explicit full-capability escape hatch — the
    # cost policy must not silently strip its browser/vision.
    monkeypatch.setattr(
        PR,
        "permission_options_for_chat",
        lambda persona, *, session_id: ToolVisibilityOptions(
            permission_mode=PERMISSION_MODE_UNBOUNDED
        ),
    )
    enabled = PR._enabled_toolsets_for_chat(_persona_with_browser_vision(), session_id="s1")
    assert "browser" in enabled
    assert "vision" in enabled
