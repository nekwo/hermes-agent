"""S1 of the mission-lane removal — the protective stage.

Nothing in S1 deletes a feature; every item relocates, extracts, or de-gates
something so the KEEP set survives the deletions in S3-S12. These tests pin the
contracts those later stages will lean on:

1. the permanent ``TaskStore`` stub for upstream ``tools/board_tool.py`` (ruling R-3),
2. ``promote_profile_to_persona`` at its new home in ``agent_runtime.personas``,
   still reachable from the upstream import path.

Items 3-5 were the Stage C Python capture extractions, retired in S14 by operator
ruling — see the note below. Item 6 (``_augment_chat_capabilities`` appending
unconditionally) is pinned in ``test_board_agent_tools.py``, next to the board
toolset gating it protects.
"""

from __future__ import annotations

import json

import pytest

from agent_runtime import paths


# ── item 1: the permanent TaskStore stub ──────────────────────────────────


def test_stub_get_always_raises_not_found():
    from agent_runtime.errors import NotFound
    from agent_runtime.task_store_stub import TaskStoreStub

    with pytest.raises(NotFound):
        TaskStoreStub().get("task_anything")


def test_stub_accepts_the_event_log_keyword_the_real_store_took():
    from agent_runtime.task_store_stub import TaskStoreStub

    # Construction shape is part of the contract: existing `TaskStore(event_log=...)`
    # call sites must not have to change to keep constructing it.
    stub = TaskStoreStub(event_log=None)
    assert not hasattr(stub, "event_log")


def test_stub_exposes_nothing_but_get():
    from agent_runtime.task_store_stub import TaskStoreStub

    # Deliberate: a future caller wanting task data should fail loudly rather than
    # receive an empty list and conclude the mission lane still works.
    for name in ("list_all", "create", "update", "list_for_workspace", "get_goal"):
        assert not hasattr(TaskStoreStub(), name), name


def test_upstream_board_resolution_falls_through_when_taskstore_is_the_stub(monkeypatch):
    """The seam the stub exists for: upstream ``tools/board_tool.py``.

    ``_resolve_board_target`` calls ``TaskStore().get(task_id)`` inside
    ``try/except Exception: pass``. With the stub in place that raises, so the
    resolution falls through to the active workspace — the correct post-removal
    answer, since there are no bound goals left.
    """

    from agent_runtime import board_models, store as store_mod
    from agent_runtime.store import WorkspaceStore
    from agent_runtime.task_store_stub import TaskStoreStub
    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    workspace = WorkspaceStore().create(name="Active")
    WorkspaceStore().set_active(workspace.id)

    monkeypatch.setattr(store_mod, "TaskStore", TaskStoreStub)

    result = json.loads(
        registry.dispatch(
            "board_card_add",
            {"title": "Card with a dead task id"},
            task_id="task_that_no_longer_exists",
            session_id=None,
        )
    )
    assert result["ok"] is True
    assert result["board_id"] == board_models.default_board_id(workspace.id)


# ── item 2: promote_profile_to_persona re-homed ───────────────────────────


def test_promote_profile_to_persona_lives_in_personas():
    from agent_runtime.personas import promote_profile_to_persona

    assert promote_profile_to_persona.__module__ == "agent_runtime.personas"


def test_upstream_import_path_still_resolves_to_the_same_function():
    """``hermes_cli/web_server.py`` is upstream-owned and cannot be edited.

    It does ``from agent_runtime.blueprints.resolve import promote_profile_to_persona``
    to back ``POST /api/profiles/{name}/promote``, so that path must keep resolving
    to the one real implementation.
    """

    from agent_runtime.blueprints.resolve import promote_profile_to_persona as via_blueprints
    from agent_runtime.personas import promote_profile_to_persona as via_personas

    assert via_blueprints is via_personas


def test_promotion_clones_the_role_template_and_binds_the_profile():
    from agent_runtime.models import AgentPersona
    from agent_runtime.personas import promote_profile_to_persona
    from agent_runtime.store import AgentStore

    template = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model="m",
        provider="p",
        api_mode="chat",
        toolsets=["file", "search"],
        system_prompt_path="agent_runtime/prompts/qa.md",
    )
    store = AgentStore()
    store.save(template)

    persona = promote_profile_to_persona(
        "launcher-qa", slot_role="verifier", personas={"qa": template}, agent_store=store
    )
    # `verifier` maps to the `qa` template via the re-homed _ROLE_TEMPLATE.
    assert persona.id == "launcher-qa"
    assert persona.hermes_profile == "launcher-qa"
    assert persona.role == "qa"
    assert persona.toolsets == ["file", "search"]
    assert store.get("launcher-qa").hermes_profile == "launcher-qa"


def test_promotion_unknown_role_does_not_clone_an_unrelated_stored_persona():
    from agent_runtime.models import AgentPersona
    from agent_runtime.personas import promote_profile_to_persona
    from agent_runtime.store import AgentStore

    store = AgentStore()
    store.save(
        AgentPersona(
            id="qa",
            display_name="QA Agent",
            role="qa",
            model="qa-model",
            provider="qa-provider",
            api_mode="chat",
            toolsets=["vision"],
            system_prompt_path="agent_runtime/prompts/qa.md",
        )
    )

    persona = promote_profile_to_persona(
        "fresh-builder", slot_role="builder", agent_store=store
    )

    assert persona.role == "builder"
    assert persona.hermes_profile == "fresh-builder"
    assert persona.toolsets == []


# ── items 3-5: the Stage C Python capture lane (retired in S14) ───────────
#
# S1 extracted the Stage C command-argument checks, the trace parsers, and the
# rebuild-artifact path so a *Python* capture lane could survive S3-S12. The
# 2026-07-30 operator ruling retired that lane outright: Stage C visual proof lives
# only as the ``launcher_qa`` MCP server plus the marionette skill. Those three
# items therefore have nothing left to protect and their tests went with
# ``agent_runtime/stagec_command_policy.py``, ``stagec_trace_parsers.py``,
# ``proof_capture.py``, and ``stagec_mcp_visual_provider.py``.
# The removal itself is pinned in ``test_s14_stagec_python_capture_removal.py``.




def test_no_proofs_store_is_created_by_the_runtime():
    """The ``proofs/`` store S1 repointed away from must never come back."""

    assert not (paths.store_root() / "proofs").exists()
