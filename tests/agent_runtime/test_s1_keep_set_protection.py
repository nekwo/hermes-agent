"""S1 of the mission-lane removal — the protective stage.

Nothing in S1 deletes a feature; every item relocates, extracts, or de-gates
something so the KEEP set survives the deletions in S3-S12. These tests pin the
contracts those later stages will lean on:

1. the permanent ``TaskStore`` stub for upstream ``tools/board_tool.py`` (ruling R-3),
2. ``promote_profile_to_persona`` at its new home in ``agent_runtime.personas``,
   still reachable from the upstream import path,
3. the extracted Stage C command-argument checks,
4. the extracted Stage C trace parsers,
5. the Stage C rebuild artifact no longer landing in the ``proofs/`` store.

Item 6 (``_augment_chat_capabilities`` appending unconditionally) is pinned in
``test_board_agent_tools.py``, next to the board toolset gating it protects.
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


# ── item 3: extracted Stage C command-argument checks ─────────────────────


def test_stagec_command_policy_rejects_composed_path_screenshot_args():
    from agent_runtime.decision_schema import DecisionPayloadInvalid
    from agent_runtime.stagec_command_policy import reject_invalid_stagec_screenshot_window_args

    with pytest.raises(DecisionPayloadInvalid):
        reject_invalid_stagec_screenshot_window_args(
            ["mcp_launcher_qa_screenshot_window --screenshot_stabilize_ms 500"]
        )
    # the arguments screenshot_window actually accepts pass
    reject_invalid_stagec_screenshot_window_args(
        ["mcp_launcher_qa_screenshot_window --max_retries 3 --retry_delay_ms 250"]
    )


def test_stagec_command_policy_rejects_unpinned_mission_control_capture():
    from agent_runtime.decision_schema import DecisionPayloadInvalid
    from agent_runtime.stagec_command_policy import reject_unpinned_mission_control_stagec_commands

    with pytest.raises(DecisionPayloadInvalid) as excinfo:
        reject_unpinned_mission_control_stagec_commands(
            ["mcp_launcher_qa_open_app_tab --tab MissionControl --hermes_profile alice"]
        )
    assert "harness_runtime_root" in str(excinfo.value)

    reject_unpinned_mission_control_stagec_commands(
        [
            "mcp_launcher_qa_open_app_tab --tab MissionControl --hermes_profile alice "
            "--harness_runtime_root X:/root --hermes_home X:/home"
        ]
    )


def test_stagec_command_policy_still_reachable_from_proof_command_policy():
    """The dying module must delegate, not keep a divergent copy."""

    from agent_runtime import proof_command_policy, stagec_command_policy

    assert (
        proof_command_policy._reject_invalid_stagec_screenshot_window_args
        is stagec_command_policy.reject_invalid_stagec_screenshot_window_args
    )
    assert (
        proof_command_policy._reject_unpinned_mission_control_stagec_commands
        is stagec_command_policy.reject_unpinned_mission_control_stagec_commands
    )


# ── item 4: extracted Stage C trace parsers ───────────────────────────────


def test_trace_parsers_recognise_the_direct_and_wrapper_screenshot_paths(tmp_path):
    from agent_runtime.stagec_trace_parsers import (
        is_launcher_qa_screenshot_tool,
        terminal_wrapper_screenshot,
    )

    assert is_launcher_qa_screenshot_tool("mcp_launcher_qa_screenshot_window")
    assert not is_launcher_qa_screenshot_tool("terminal")

    shot = tmp_path / "missioncontrol_001.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    payload = {
        "tool_name": "terminal",
        "status": "passed",
        "command_label": (
            "pwsh Invoke-LauncherQaMcpTool.ps1 -Tool mcp_launcher_qa_screenshot_window "
            f"-ArgsJson '{json.dumps({'label': 'missioncontrol', 'out_dir': str(tmp_path)})}'"
        ),
    }
    resolved = terminal_wrapper_screenshot(payload, run=None)
    assert resolved is not None
    tool_name, path = resolved
    assert tool_name == "mcp_launcher_qa_screenshot_window"
    assert path == str(shot)


def test_trace_parsers_reject_an_artifact_older_than_the_run(tmp_path):
    import os
    import time

    from agent_runtime.stagec_trace_parsers import latest_wrapper_artifact

    stale = tmp_path / "stale.png"
    stale.write_bytes(b"\x89PNG\r\n\x1a\n")
    old = time.time() - 10_000
    os.utime(stale, (old, old))

    assert latest_wrapper_artifact(label="", out_dir=str(tmp_path), min_mtime=None) == stale
    assert latest_wrapper_artifact(label="", out_dir=str(tmp_path), min_mtime=time.time()) is None


def test_png_dimensions_sniffs_the_header(tmp_path):
    import struct

    from agent_runtime.stagec_trace_parsers import png_dimensions

    png = tmp_path / "sized.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + struct.pack(">II", 1920, 1080))
    assert png_dimensions(png) == (1920, 1080)
    assert png_dimensions(tmp_path / "missing.png") == (0, 0)


def test_visual_trace_evidence_delegates_to_the_surviving_parsers():
    from agent_runtime import stagec_trace_parsers, visual_trace_evidence

    assert (
        visual_trace_evidence._terminal_wrapper_screenshot
        is stagec_trace_parsers.terminal_wrapper_screenshot
    )
    assert visual_trace_evidence._png_dimensions is stagec_trace_parsers.png_dimensions


# ── item 5: Stage C rebuild artifacts leave the proofs/ store ─────────────


def test_rebuild_artifact_is_written_under_stagec_artifacts_not_proofs():
    from agent_runtime.stagec_mcp_visual_provider import _write_rebuild_artifact

    rel = _write_rebuild_artifact("task_stagec_probe", "stdout", "build log line\n")
    assert rel.startswith("stagec_artifacts/")
    assert "proofs/" not in rel

    written = paths.store_root() / rel
    assert written.read_text(encoding="utf-8") == "build log line\n"
    assert not (paths.store_root() / "proofs").exists()
