"""S4 Mission Board agent-tool tests: registration + toolset gating, the §10
board-resolution rule, session-based attribution, the situational-HUD digest
(presence/absence), and prompt-path parity. Autouse conftest isolates the root.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import board_models
from agent_runtime.board_store import BoardStore
from agent_runtime.personas import ALLOWED_TOOLSETS_BY_ROLE, PROFILE_CHAT_FALLBACK_TOOLSETS
from agent_runtime.persona_runtime import _CHAT_CAPABILITY_TOOLSETS
from agent_runtime.runtime_hud import _board_digest_for_workspace, render_situational_hud_block
from agent_runtime.store import TaskStore, WorkspaceStore
from tools.registry import discover_builtin_tools, registry


@pytest.fixture(autouse=True)
def _tools_loaded():
    discover_builtin_tools()


def _make_workspace(name: str = "Default") -> str:
    ws = WorkspaceStore().create(name=name)
    WorkspaceStore().set_active(ws.id)
    return ws.id


# ── registration + toolset gating ─────────────────────────────────────────


def test_board_tools_registered_in_board_toolset():
    add = registry.get_entry("board_card_add")
    lst = registry.get_entry("board_cards")
    assert add is not None and add.toolset == "board"
    assert lst is not None and lst.toolset == "board"


def test_board_toolset_gated_for_all_mission_roles():
    for role, toolsets in ALLOWED_TOOLSETS_BY_ROLE.items():
        assert "board" in toolsets, role
    assert "board" in _CHAT_CAPABILITY_TOOLSETS
    assert "board" in PROFILE_CHAT_FALLBACK_TOOLSETS


# ── the chat-lane augmentation must not be role-keyed (mission-lane removal S1) ──


def _chat_persona(role: str = "dev"):
    from agent_runtime.models import AgentPersona

    return AgentPersona(
        id=f"persona_{role}",
        display_name=role,
        role=role,
        model=None,
        provider=None,
        api_mode="chat",
        toolsets=["search"],
        system_prompt_path=None,
    )


def test_board_and_agent_chat_survive_an_empty_role_toolset_table(monkeypatch):
    """``_augment_chat_capabilities`` is the ONLY path that puts ``board`` and
    ``agent_chat`` on a chat lane, and both are explicit KEEP.

    It used to read ``ALLOWED_TOOLSETS_BY_ROLE.get(role, frozenset())`` and append
    only what the role allowed. That dict is deleted in S11 of the mission-lane
    removal, and it already answers with an EMPTY set for any role token it does
    not know — so a role-keyed gate here strips the Mission Board and
    agent-to-agent chat from every chat persona the moment either happens, with no
    error.

    This test simulates the post-S11 world by emptying the table. It fails if the
    append is ever made conditional on the role again.
    """

    from agent_runtime import persona_runtime as PR

    monkeypatch.setattr(PR, "ALLOWED_TOOLSETS_BY_ROLE", {})
    augmented = PR._augment_chat_capabilities(_chat_persona(), ["search"])
    assert "board" in augmented
    assert "agent_chat" in augmented
    assert "clarify" in augmented


def test_mission_goal_stays_role_gated_when_the_table_is_empty(monkeypatch):
    """The de-gating is scoped: the privileged mission/task/graph capability keeps
    its role gate (removed with the toolset itself in S4), so making board and
    agent_chat unconditional does not quietly widen goal creation."""

    from agent_runtime import persona_runtime as PR

    monkeypatch.setattr(PR, "ALLOWED_TOOLSETS_BY_ROLE", {})
    assert "mission_goal" not in PR._augment_chat_capabilities(_chat_persona(), ["search"])


def test_supervisor_no_longer_gets_the_retired_mission_goal_toolset():
    from agent_runtime import persona_runtime as PR

    augmented = PR._augment_chat_capabilities(_chat_persona("alice_supervisor"), ["search"])
    assert augmented == ["search", "agent_chat", "board", "clarify"]


def test_dev_chat_lane_augmentation_is_unchanged_for_a_known_role():
    from agent_runtime import persona_runtime as PR

    # dev has no mission_goal in ALLOWED_TOOLSETS_BY_ROLE, so it is still withheld.
    augmented = PR._augment_chat_capabilities(_chat_persona("dev"), ["search"])
    assert augmented == ["search", "agent_chat", "board", "clarify"]


# ── §10 board-resolution rule ─────────────────────────────────────────────


def test_add_resolves_active_workspace_and_attributes_agent():
    ws = _make_workspace()
    result = json.loads(
        registry.dispatch("board_card_add", {"title": "Track this"}, task_id=None, session_id=None)
    )
    assert result["ok"] is True
    assert result["board_id"] == board_models.default_board_id(ws)
    assert result["column_id"] == "col_queued"
    assert result["created_by"] == "agent"  # unresolved session → generic label


def test_explicit_board_id_wins():
    ws = _make_workspace()
    store = BoardStore()
    store.ensure_default_board(ws)
    other = store.create(workspace_id=ws, title="Explicit board")
    result = json.loads(
        registry.dispatch(
            "board_card_add",
            {"title": "Onto explicit", "board_id": other.board_id},
            task_id=None,
            session_id=None,
        )
    )
    assert result["board_id"] == other.board_id


def test_bound_goal_workspace_resolves_over_active():
    active = _make_workspace("Active")
    other = WorkspaceStore().create(name="Goal WS")
    # a task bound to the OTHER workspace
    from hermes_time import now
    from agent_runtime.models import Task
    from agent_runtime.states import TaskState

    task = TaskStore().create(
        Task(
            id="task_board_res",
            title="t",
            description="d",
            state=TaskState.CREATED,
            created_at=now(),
            updated_at=now(),
            requested_by="op",
            workspace_id=other.id,
        )
    )
    result = json.loads(
        registry.dispatch("board_card_add", {"title": "From goal lane"}, task_id=task.id, session_id=None)
    )
    # the bound goal's workspace wins over the active workspace
    assert result["board_id"] == board_models.default_board_id(other.id)
    assert result["board_id"] != board_models.default_board_id(active)


def test_no_workspace_no_board_returns_invalid_request():
    WorkspaceStore().set_active(None)
    result = json.loads(
        registry.dispatch("board_card_add", {"title": "orphan"}, task_id=None, session_id=None)
    )
    assert result["ok"] is False
    assert "invalid_request" in result["error"]


def test_explicit_missing_board_id_is_invalid_request():
    _make_workspace()
    result = json.loads(
        registry.dispatch(
            "board_card_add",
            {"title": "x", "board_id": "board_nope"},
            task_id=None,
            session_id=None,
        )
    )
    assert result["ok"] is False and "invalid_request" in result["error"]


# ── attribution from the chat session ─────────────────────────────────────


def test_created_by_resolves_persona_from_session():
    ws = _make_workspace()
    from agent_runtime.persona_assignments import PersonaInstanceStore

    instance = PersonaInstanceStore().create_operator_chat(
        persona_id="dev", display_name="Dev", session_id="sess_board_attr"
    )
    result = json.loads(
        registry.dispatch(
            "board_card_add",
            {"title": "Agent card"},
            task_id=None,
            session_id="sess_board_attr",
        )
    )
    assert result["ok"] is True
    assert result["created_by"] == instance.persona_id


# ── board_cards listing ───────────────────────────────────────────────────


def test_board_cards_lists_active_cards():
    ws = _make_workspace()
    registry.dispatch("board_card_add", {"title": "One"}, task_id=None, session_id=None)
    registry.dispatch("board_card_add", {"title": "Two"}, task_id=None, session_id=None)
    result = json.loads(registry.dispatch("board_cards", {}, task_id=None, session_id=None))
    assert result["ok"] is True
    assert {row["title"] for row in result["cards"]} == {"One", "Two"}


# ── situational-HUD digest presence / absence ─────────────────────────────


def test_hud_digest_present_when_open_cards_exist():
    ws = _make_workspace()
    store = BoardStore()
    store.add_card(workspace_id=ws, title="Q1")
    store.add_card(workspace_id=ws, title="Q2")
    digest = _board_digest_for_workspace(ws)
    assert digest == {"queued": 2, "active": 0, "review": 0}
    block = render_situational_hud_block({"preview": True, "board": digest})
    assert "Board: 2 queued" in block
    assert "MAY add a card" in block


def test_hud_digest_absent_without_board():
    ws = _make_workspace()  # no board created yet (lazy)
    assert _board_digest_for_workspace(ws) is None
    # no board line rendered
    block = render_situational_hud_block({"preview": True})
    assert "- Board:" not in block


def test_hud_digest_absent_when_only_done_cards():
    ws = _make_workspace()
    store = BoardStore()
    card = store.add_card(workspace_id=ws, title="Ship")
    store.move_card(card.card_id, column_id="col_done")
    # only a done card → no OPEN cards → no digest line (nudged-never-forced)
    assert _board_digest_for_workspace(ws) is None


# ── prompt-path parity ────────────────────────────────────────────────────


def test_board_sentence_present_in_both_prompt_paths():
    from pathlib import Path

    from agent_runtime.models import AgentPersona
    from agent_runtime.persona_runtime import _persona_chat_system_prompt

    persona = AgentPersona(
        id="dev",
        display_name="Dev",
        role="dev",
        model="",
        provider="",
        api_mode="chat",
        toolsets=[],
        system_prompt_path=None,
    )
    chat_prompt = _persona_chat_system_prompt(persona)
    assert "workspace board" in chat_prompt.lower()
    assert "may add a card" in chat_prompt.lower()

    overlay = (
        Path(__file__).resolve().parents[2]
        / "agent_runtime"
        / "prompts"
        / "shared_harness_overlay.md"
    ).read_text(encoding="utf-8")
    assert "Mission Board" in overlay
    assert "may add a card" in overlay.lower()
