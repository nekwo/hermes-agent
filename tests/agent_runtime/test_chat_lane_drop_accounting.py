"""The chat-lane cost policy's drops must be TYPED, not silent (G5).

The policy itself is by design (``test_chat_lane_toolsets.py`` pins it). Its
INVISIBILITY was the defect: a mission-chat agent asked to run a command found
no ``terminal`` tool, and both it and the operator read the plain absence as a
permission problem — the same failure ``mcp_lane`` retired for MCP, recurring
one module over (archive/2026-08-22-pre-consolidation/mission-chat-lane-gap-audit.md §6 / G5).

These tests pin the ACCOUNTING, never the policy: nothing here should ever grow
an assertion that a dropped toolset comes back. The two rules they exist to
hold are (1) every drop the lane performs is reported, and (2) nothing that was
not dropped is ever reported — a resolve that never ran the policy claims
nothing, and a restored toolset claims nothing.
"""

from __future__ import annotations

import dataclasses

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import persona_runtime as PR
from agent_runtime.chat_lane_toolsets import (
    DROP_KIND_TOOL,
    DROP_KIND_TOOLSET,
    TOOL_DROPPED_BY_CHAT_LANE_POLICY,
    TOOLSET_DROPPED_BY_CHAT_LANE_POLICY,
    ChatLaneDrop,
    chat_lane_drop_rows,
    chat_lane_restore_config_key,
    chat_lane_tool_drops,
    chat_lane_toolset_drops,
    scope_chat_lane_toolsets,
)
from agent_runtime.mcp_lane import HARNESS_LANE, mcp_lane_requirement_failures
from tests.agent_runtime.persona_samples import sample_personas
from agent_runtime.tool_permissions import PERMISSION_MODE_UNBOUNDED
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility


def _persona(persona_id: str):
    return {persona.id: persona for persona in sample_personas()}[persona_id]


def _rows(visibility: dict, code: str) -> list[dict]:
    return [row for row in visibility["requirement_failures"] if row.get("code") == code]


# ── the droppers report what they removed ───────────────────────────────────


def test_every_dropped_toolset_produces_one_row():
    drops = chat_lane_toolset_drops(
        ["file", "search", "terminal", "browser", "vision", "code_execution", "skills"],
        persona_id="dev",
    )

    assert [drop.subject for drop in drops] == [
        "file",
        "terminal",
        "browser",
        "vision",
        "code_execution",
    ]
    assert {drop.code for drop in drops} == {TOOLSET_DROPPED_BY_CHAT_LANE_POLICY}
    assert {drop.kind for drop in drops} == {DROP_KIND_TOOLSET}


def test_drops_are_the_exact_inverse_of_the_filter():
    # The kept list and the drop list are two views of ONE policy answer; if they
    # can disagree, the account is fiction.
    resolved = ["file", "search", "terminal", "browser", "skills", "session_search"]
    kept = scope_chat_lane_toolsets(resolved)
    dropped = [drop.subject for drop in chat_lane_toolset_drops(resolved)]

    assert kept + dropped == ["search", "skills", "session_search", "file", "terminal", "browser"]
    assert set(kept).isdisjoint(dropped)
    assert set(kept) | set(dropped) == set(resolved)


def test_a_toolset_the_lane_never_had_is_not_claimed():
    # Accounting over the RESOLVED lane, not over the policy constant: a persona
    # with no browser toolset lost nothing to the browser exclusion.
    assert chat_lane_toolset_drops(["search", "skills"]) == ()


def test_restored_toolset_produces_no_row():
    drops = chat_lane_toolset_drops(
        ["file", "terminal", "browser"], restore=["file", "terminal"]
    )

    assert [drop.subject for drop in drops] == ["browser"]


def test_duplicate_toolsets_are_reported_once():
    drops = chat_lane_toolset_drops(["file", "file", "terminal"])

    assert [drop.subject for drop in drops] == ["file", "terminal"]


# ── the single-tool dropper ─────────────────────────────────────────────────


def test_tool_dropper_reports_the_skill_manage_cut():
    drops = chat_lane_tool_drops(persona_id="dev")

    assert [drop.subject for drop in drops] == ["skill_manage"]
    assert drops[0].code == TOOL_DROPPED_BY_CHAT_LANE_POLICY
    assert drops[0].kind == DROP_KIND_TOOL


def test_restore_suppresses_the_tool_row():
    assert chat_lane_tool_drops(restore=["skill_manage"]) == ()


def test_tool_row_is_withheld_when_its_toolset_is_not_on_the_lane():
    # Blocking skill_manage on a persona with no `skills` toolset removes
    # nothing — claiming a drop there is the same dishonesty as hiding one.
    assert (
        chat_lane_tool_drops(
            enabled_toolsets=["search"], toolset_for_tool=lambda name: "skills"
        )
        == ()
    )
    assert [
        drop.subject
        for drop in chat_lane_tool_drops(
            enabled_toolsets=["search", "skills"], toolset_for_tool=lambda name: "skills"
        )
    ] == ["skill_manage"]


def test_tool_row_survives_a_registry_fault():
    # A broken toolset lookup must not silently delete the account.
    def _boom(name: str) -> str:
        raise RuntimeError("registry unavailable")

    drops = chat_lane_tool_drops(enabled_toolsets=["skills"], toolset_for_tool=_boom)

    assert [drop.subject for drop in drops] == ["skill_manage"]


# ── row shape ───────────────────────────────────────────────────────────────


def test_row_shape_matches_the_typed_issue_contract():
    # Same contract as mcp_lane's row (and machine_roots.PathTokenIssue.row()),
    # so operator surfaces that already render typed issue rows need no new case.
    mcp_row = mcp_lane_requirement_failures(
        declared_servers=["launcher_qa"], lane=HARNESS_LANE, registered_servers=[]
    )[0]
    drop_row = ChatLaneDrop(
        subject="terminal",
        kind=DROP_KIND_TOOLSET,
        code=TOOLSET_DROPPED_BY_CHAT_LANE_POLICY,
        restorable_via=chat_lane_restore_config_key("dev"),
    ).row(role="dev", entry_point_lane=HARNESS_LANE)

    shared = {"code", "entry_point_lane", "summary", "fix_hint"}
    assert shared <= set(mcp_row)
    assert shared <= set(drop_row)
    # The subject rides under a key naming WHAT was dropped (mcp: `server`).
    assert drop_row["toolset"] == "terminal"
    assert drop_row["role"] == "dev"
    assert drop_row["entry_point_lane"] == HARNESS_LANE


def test_row_names_the_exact_restore_key_an_operator_must_edit():
    row = ChatLaneDrop(
        subject="file",
        kind=DROP_KIND_TOOLSET,
        code=TOOLSET_DROPPED_BY_CHAT_LANE_POLICY,
        restorable_via=chat_lane_restore_config_key("neko_supervisor"),
    ).row(role="alice_supervisor", entry_point_lane=HARNESS_LANE)

    assert (
        row["restorable_via"]
        == "agent_runtime.personas.neko_supervisor.chat_lane_restore_toolsets"
    )
    assert row["restorable_via"] in row["fix_hint"]
    # The hint must actively steer OFF the permission-mode goose chase that made
    # this class of absence expensive (2026-07-25 "Blocked 21 tools").
    assert "permission" in row["fix_hint"].lower()
    assert "file" in row["summary"]


def test_drop_rows_renders_every_drop():
    drops = chat_lane_toolset_drops(["file", "terminal"], persona_id="dev")

    assert [row["toolset"] for row in chat_lane_drop_rows(drops, role="dev")] == [
        "file",
        "terminal",
    ]
    assert chat_lane_drop_rows(None) == []


# ── the lane-level accounting twin ──────────────────────────────────────────


def test_capability_drops_match_what_the_chat_lane_actually_ships(bounded_chat_session):
    # BOUNDED session: the cost policy this accounts for is bypassed by the
    # runtime default since the 2026-08-09 ruling (pinned just below by
    # ``test_unbounded_mode_claims_no_drops``), so the tier is named explicitly.
    qa = _persona("qa")
    session_id = bounded_chat_session(qa.id)
    enabled = set(PR._enabled_toolsets_for_chat(qa, session_id=session_id))
    drops = PR.chat_lane_capability_drops(qa, session_id=session_id)
    toolsets = {drop.subject for drop in drops if drop.kind == DROP_KIND_TOOLSET}

    # Everything reported as dropped is genuinely absent from the shipped lane…
    assert toolsets.isdisjoint(enabled)
    # …and the QA persona's dev/visual toolkit is exactly what it loses.
    assert {"file", "terminal", "browser", "vision"} <= toolsets
    # The single-tool cut rides the same account.
    assert {"skill_manage"} == {
        drop.subject for drop in drops if drop.kind == DROP_KIND_TOOL
    }


def test_unbounded_mode_claims_no_drops():
    # `unbounded` genuinely bypasses the cost policy, so a row there would report
    # a drop that never happened.
    assert (
        PR.chat_lane_capability_drops(
            _persona("qa"), session_id="s1", permission_mode=PERMISSION_MODE_UNBOUNDED
        )
        == ()
    )


def test_restore_config_suppresses_the_lane_rows(monkeypatch, bounded_chat_session):
    monkeypatch.setattr(
        PR, "chat_lane_restore_toolsets", lambda persona_id: ["file", "terminal"]
    )
    qa = _persona("qa")
    drops = PR.chat_lane_capability_drops(qa, session_id=bounded_chat_session(qa.id))
    subjects = {drop.subject for drop in drops}

    assert not {"file", "terminal"} & subjects
    assert {"browser", "vision"} <= subjects


# ── resolver integration ────────────────────────────────────────────────────


def test_rows_ride_the_same_requirement_failures_list(bounded_chat_session):
    qa = _persona("qa")
    session_id = bounded_chat_session(qa.id)
    visibility = resolve_tool_visibility(
        qa,
        ToolVisibilityOptions(
            entry_point_lane=HARNESS_LANE,
            chat_lane_capability_drops=PR.chat_lane_capability_drops(
                qa, session_id=session_id
            ),
        ),
    )

    rows = _rows(visibility, TOOLSET_DROPPED_BY_CHAT_LANE_POLICY)
    assert {row["toolset"] for row in rows} >= {"file", "terminal", "browser", "vision"}
    assert {row["entry_point_lane"] for row in rows} == {HARNESS_LANE}
    assert {row["role"] for row in rows} == {"qa"}
    assert _rows(visibility, TOOL_DROPPED_BY_CHAT_LANE_POLICY)


def test_rows_compose_with_the_mcp_rows_without_displacing_them(bounded_chat_session):
    qa = dataclasses.replace(_persona("qa"), required_mcp_servers=["launcher_qa"])
    session_id = bounded_chat_session(qa.id)
    visibility = resolve_tool_visibility(
        qa,
        ToolVisibilityOptions(
            entry_point_lane=HARNESS_LANE,
            chat_lane_capability_drops=PR.chat_lane_capability_drops(
                qa, session_id=session_id
            ),
        ),
    )
    codes = [row["code"] for row in visibility["requirement_failures"]]

    assert "mcp_not_registered_on_lane" in codes
    assert TOOLSET_DROPPED_BY_CHAT_LANE_POLICY in codes
    # The MCP payload stays exactly where R0/R1 put it — first, unchanged.
    assert codes[0] == "mcp_not_registered_on_lane"


def test_a_resolve_that_never_ran_the_policy_claims_nothing():
    # The worker lane resolves toolsets directly (`effective_toolsets`) and never
    # passes through the cost policy. Reporting a chat-lane drop there would be
    # the wrong-diagnosis bug in the opposite direction.
    visibility = resolve_tool_visibility(
        _persona("qa"), ToolVisibilityOptions(entry_point_lane=HARNESS_LANE)
    )

    assert visibility["requirement_failures"] == []


def test_accounting_changes_no_tool():
    plain = resolve_tool_visibility(
        _persona("qa"), ToolVisibilityOptions(entry_point_lane=HARNESS_LANE)
    )
    accounted = resolve_tool_visibility(
        _persona("qa"),
        ToolVisibilityOptions(
            entry_point_lane=HARNESS_LANE,
            chat_lane_capability_drops=PR.chat_lane_capability_drops(
                _persona("qa"), session_id=None
            ),
        ),
    )

    assert accounted["final_model_tools"] == plain["final_model_tools"]
    assert accounted["blocked_tool_names"] == plain["blocked_tool_names"]
    assert accounted["effective_toolsets"] == plain["effective_toolsets"]


def test_chat_lane_preview_carries_the_rows_end_to_end(bounded_chat_session):
    # apply_chat_lane_tool_scope is what Mission Control's persona-instance
    # preview uses; the drops must ride it without any caller opting in.
    qa = _persona("qa")
    options = PR.apply_chat_lane_tool_scope(
        qa,
        ToolVisibilityOptions(entry_point_lane=HARNESS_LANE),
        session_id=bounded_chat_session(qa.id),
    )
    visibility = resolve_tool_visibility(qa, options)

    assert {row["toolset"] for row in _rows(visibility, TOOLSET_DROPPED_BY_CHAT_LANE_POLICY)} >= {
        "file",
        "terminal",
    }


# ── operator surface ────────────────────────────────────────────────────────


def test_tool_diff_reports_the_drops(capsys):
    import argparse
    import json

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    # ``--permission-mode profile_default`` is the documented way to preview the
    # BOUNDED shape now that the CLI's default follows the runtime default
    # (unbounded). Without it this asserts drops that no longer happen.
    args = parser.parse_args(
        [
            "harness",
            "persona",
            "tool-diff",
            "qa",
            "--permission-mode",
            "profile_default",
            "--json",
        ]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)
    rows = [
        row
        for row in payload["tool_visibility"]["requirement_failures"]
        if row["code"] == TOOLSET_DROPPED_BY_CHAT_LANE_POLICY
    ]

    assert {row["toolset"] for row in rows} >= {"file", "terminal", "browser", "vision"}
    assert all(row["fix_hint"] for row in rows)


def test_tool_diff_human_output_prints_the_fix(capsys):
    import argparse

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["harness", "persona", "tool-diff", "qa", "--permission-mode", "profile_default"]
    )

    assert args.func(args) == 0
    out = capsys.readouterr().out

    # No new rendering case: the existing requirement-failure printer carries it.
    assert TOOLSET_DROPPED_BY_CHAT_LANE_POLICY in out
    assert "chat_lane_restore_toolsets" in out


def test_tool_diff_under_unbounded_reports_no_drops(capsys):
    import argparse
    import json

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["harness", "persona", "tool-diff", "qa", "--permission-mode", "unbounded", "--json"]
    )

    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [
        row
        for row in payload["tool_visibility"]["requirement_failures"]
        if row["code"] == TOOLSET_DROPPED_BY_CHAT_LANE_POLICY
    ] == []
