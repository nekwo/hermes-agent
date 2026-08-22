"""Unbounded-by-default tool access — the 2026-08-09 operator ruling, pinned.

Plan: ``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/UNBOUNDED_DEFAULT_PLAN_2026-08-09.md``.

What is actually at stake in this file, so a future reader does not "tidy" one
of these away:

* The runtime's standing posture is ``unbounded`` and it is decided at ONE
  chokepoint. Every consumer inherits it; nothing per-persona was migrated.
* The refusals that posture removes are traded for RECEIPTS, not for nothing.
  ``test_every_formerly_refused_class_...`` is the compensating control: if a
  formerly-refused command can run without a row landing in
  ``terminal_envelope_decisions.jsonl`` naming the mode, the ruling's safety
  argument is gone rather than weakened.
* Two things deliberately do NOT yield to the mode — registry hygiene and MCP
  cross-persona scoping. They are junk-removal and process-isolation, not
  permission tiers.
* A config fault resolves NARROW (``profile_default``), never to the shipped
  wide default.

Every test here was proven red by reverting the exact line it pins.
"""

from __future__ import annotations

import json
import textwrap
import types

import pytest

from agent_runtime.config import load_agent_runtime_config, load_root_runtime_config
from agent_runtime.models import AgentPersona
from agent_runtime.permission_modes import (
    PERMISSION_MODE_BOUNDED,
    PERMISSION_MODE_PROFILE_DEFAULT,
    PERMISSION_MODE_READ_ONLY,
    PERMISSION_MODE_UNBOUNDED,
)
from agent_runtime.personas import REGISTRY_HYGIENE_BLOCKED_TOOLS
from agent_runtime.runtime_config import TerminalEnvelopeConfig
from agent_runtime.runtime_hud import render_capability_block, resolve_capability_block
from agent_runtime.terminal_envelope import (
    DESTRUCTIVE_GIT,
    ENVELOPE_COMMAND_REQUIRES_GRANT,
    ENVELOPE_DECISION_LOG,
    GIT_PUSH,
    GRANT_SOURCE_CONFIG,
    GRANT_SOURCE_PERMISSION_MODE,
    GRANTABLE_COMMAND_CLASSES,
    LANE_MISSION_CHAT,
    NETWORK_EGRESS,
    OUTCOME_GRANTED,
    OUTCOME_REFUSE,
    RECURSIVE_DELETE,
    TerminalEnvelopeScope,
    envelope_decision,
    explain_terminal_envelope,
    record_envelope_decision,
    terminal_envelope_scope,
)
from agent_runtime.tool_permissions import (
    READ_ONLY_BLOCKS,
    ChatToolPermissionStore,
    default_permission_mode,
    default_permission_mode_issues,
    permission_options_for_chat,
)
from agent_runtime.tool_visibility import ToolVisibilityOptions, resolve_tool_visibility


# ── helpers ─────────────────────────────────────────────────────────────────


def _persona(persona_id: str = "dev", *, role: str = "dev") -> AgentPersona:
    return AgentPersona(
        id=persona_id,
        display_name=persona_id,
        role=role,
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=["file", "terminal", "todo"],
        system_prompt_path="",
    )


def _write_root_config(tmp_path, monkeypatch, body: str):
    """Lay down a ROOT ``config.yaml`` and point HERMES_HOME at that root.

    The production reader (``config.load_root_runtime_config``) is exercised for
    real — no monkeypatched config object — because the whole point of the knob
    is that an operator can write it in YAML.
    """

    root = tmp_path / "hermes-root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(textwrap.dedent(body), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    # The config parse is mtime-cached; drop it so a same-second write is seen.
    from agent_runtime.parse_cache import clear_parse_cache

    clear_parse_cache()
    return root


def _cfg(**grants) -> types.SimpleNamespace:
    return types.SimpleNamespace(terminal_envelope=TerminalEnvelopeConfig(grants=dict(grants)))


def _scope(mode: str = "", *, role: str = "dev", runtime_root="") -> TerminalEnvelopeScope:
    return TerminalEnvelopeScope(
        lane=LANE_MISSION_CHAT,
        role=role,
        persona_id="dev",
        session_id="chat-1",
        runtime_root=str(runtime_root or ""),
        permission_mode=mode,
    )


def _decision_rows(root) -> list[dict]:
    path = root / ENVELOPE_DECISION_LOG
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── 1. the chokepoint answers ``unbounded`` by default ──────────────────────


def test_chokepoint_resolves_unbounded_by_default_with_an_empty_store():
    """Plan §7.1. No stored record, no config override ⇒ the shipped default.

    ``permission_source`` matters as much as the mode: ``runtime_default`` is
    how every surface tells "the operator set this session" apart from "this is
    the standing posture".
    """

    options = permission_options_for_chat(_persona(), session_id="chat-1")

    assert options.permission_mode == PERMISSION_MODE_UNBOUNDED
    assert options.permission_source == "runtime_default"
    assert options.blocked_tool_names == []
    assert options.turns_remaining is None


def test_unbounded_default_reaches_the_chat_lane_block_and_toolsets():
    """The default is not a scalar on a preview — it changes what a turn ships."""

    from agent_runtime.persona_runtime import (
        _blocked_tool_names_for_chat,
        _enabled_toolsets_for_chat,
    )

    persona = _persona()

    assert _blocked_tool_names_for_chat(persona, session_id="chat-1") == []
    toolsets = _enabled_toolsets_for_chat(persona, session_id="chat-1")
    # The chat-lane cost policy cuts ``browser``; unbounded resolves the full
    # registry instead, so its presence is the observable difference.
    assert "browser" in toolsets


# ── 2. an operator can configure the old posture back ───────────────────────


def test_root_config_can_restore_the_bounded_default(tmp_path, monkeypatch):
    """Plan §7.2. ``default_mode: profile_default`` reproduces the old world."""

    _write_root_config(
        tmp_path,
        monkeypatch,
        """
        agent_runtime:
          tool_permissions:
            default_mode: profile_default
        """,
    )

    assert default_permission_mode() == PERMISSION_MODE_PROFILE_DEFAULT

    from agent_runtime.persona_runtime import (
        _blocked_tool_names_for_chat,
        _enabled_toolsets_for_chat,
    )

    persona = _persona()
    blocked = _blocked_tool_names_for_chat(persona, session_id="chat-1")
    assert "delegate_task" in blocked
    assert "browser" not in _enabled_toolsets_for_chat(persona, session_id="chat-1")


def test_unknown_default_mode_falls_back_bounded_with_a_typed_issue(tmp_path, monkeypatch):
    """Plan §7.3. A config fault must never resolve to MORE capability.

    The shipped default is the wide mode, so "unparseable ⇒ ship the default"
    would have been the natural (and wrong) implementation: it would turn a typo
    into full access. The fallback is the narrow mode, and the fault is typed
    rather than silent.
    """

    _write_root_config(
        tmp_path,
        monkeypatch,
        """
        agent_runtime:
          tool_permissions:
            default_mode: banana
        """,
    )

    assert default_permission_mode() == PERMISSION_MODE_PROFILE_DEFAULT
    issues = default_permission_mode_issues()
    assert [issue["code"] for issue in issues] == ["tool_permission_default_mode_unknown"]
    assert "banana" in issues[0]["summary"]

    # And the operator hears about it from the config doctor, not only from
    # agents quietly having fewer tools than the config appears to grant.
    from agent_runtime.migrations import validate_runtime_config

    warnings = validate_runtime_config(load_root_runtime_config())["warnings"]
    assert any(
        row["field"] == "agent_runtime.tool_permissions.default_mode" for row in warnings
    )


# ── 3. the store: stale rows, restrictions, expiry ──────────────────────────


def test_stale_profile_default_record_does_not_pin_a_session_bounded():
    """Plan §7.4 — THE migration trap.

    ``consume_turn`` has been writing ``mode=profile_default,
    source=operator:elevation_expired`` for months. Read as an explicit
    restriction, every session that ever held a temporary grant would be frozen
    out of the new default forever, and no migration script would fix it because
    the rows look exactly like an operator decision.
    """

    persona = _persona()
    ChatToolPermissionStore().set(
        persona_id=persona.id,
        session_id="chat-stale",
        mode=PERMISSION_MODE_PROFILE_DEFAULT,
        reason="grant lapsed",
        source="operator:elevation_expired",
        turns_remaining=0,
    )

    options = permission_options_for_chat(persona, session_id="chat-stale")

    assert options.permission_mode == PERMISSION_MODE_UNBOUNDED
    assert options.permission_source == "runtime_default"


def test_operator_restriction_still_bites_under_the_new_default():
    """Plan §7.5. The store is the RESTRICTION lane; it must actually restrict."""

    persona = _persona()
    ChatToolPermissionStore().set(
        persona_id=persona.id,
        session_id="chat-ro",
        mode=PERMISSION_MODE_READ_ONLY,
        reason="review only",
        source="operator",
    )

    options = permission_options_for_chat(persona, session_id="chat-ro")

    assert options.permission_mode == PERMISSION_MODE_READ_ONLY
    assert options.permission_source == "operator"
    assert set(options.blocked_tool_names) == set(READ_ONLY_BLOCKS)

    from agent_runtime.persona_runtime import _blocked_tool_names_for_chat

    blocked = set(_blocked_tool_names_for_chat(persona, session_id="chat-ro"))
    assert READ_ONLY_BLOCKS <= blocked


def test_bounded_is_the_explicit_restriction_spelling():
    """§3.5 option (a). ``bounded`` says what ``profile_default`` can no longer
    say: an operator restriction, unambiguously distinct from a lapsed grant."""

    persona = _persona()
    ChatToolPermissionStore().set(
        persona_id=persona.id,
        session_id="chat-bounded",
        mode=PERMISSION_MODE_BOUNDED,
        reason="hold at the old tier",
        source="operator",
    )

    options = permission_options_for_chat(persona, session_id="chat-bounded")

    # It ENFORCES as the historical bounded tier, and reports itself in the
    # vocabulary every existing consumer already parses.
    assert options.permission_mode == PERMISSION_MODE_PROFILE_DEFAULT
    assert options.permission_source == "operator"

    from agent_runtime.persona_runtime import _blocked_tool_names_for_chat

    assert "delegate_task" in _blocked_tool_names_for_chat(persona, session_id="chat-bounded")


def test_turns_bounded_read_only_decrements_and_expires_to_the_default():
    """Plan §7.6 — the latent bug the ruling promotes to the main path.

    ``consume_turn`` only decremented when the stored mode was ``unbounded``, so
    a turns-bounded ``read_only`` restriction never counted down and never
    expired. Now that restriction IS the store's purpose, that is the path.
    """

    persona = _persona()
    store = ChatToolPermissionStore()
    store.set(
        persona_id=persona.id,
        session_id="chat-exp",
        mode=PERMISSION_MODE_READ_ONLY,
        reason="one turn only",
        source="operator",
        turns_remaining=2,
    )

    after_one = store.consume_turn(persona_id=persona.id, session_id="chat-exp")
    assert after_one.turns_remaining == 1
    assert after_one.mode == PERMISSION_MODE_READ_ONLY

    after_two = store.consume_turn(persona_id=persona.id, session_id="chat-exp")
    assert after_two.mode == PERMISSION_MODE_PROFILE_DEFAULT
    assert after_two.source == "operator:restriction_expired"

    # Expired restriction ⇒ back to the runtime default, not stuck bounded.
    options = permission_options_for_chat(persona, session_id="chat-exp")
    assert options.permission_mode == PERMISSION_MODE_UNBOUNDED


# ── 4. the terminal envelope: mode grants, with receipts ────────────────────


def test_unbounded_scope_grants_git_push_without_a_config_stanza():
    """Plan §7.7. Schema-plane "full access" was hollow while the execution
    plane still refused ``git push`` for want of a per-role grant."""

    decision = envelope_decision(
        "git push origin main", scope=_scope(PERMISSION_MODE_UNBOUNDED), cfg=_cfg()
    )

    assert decision.outcome == OUTCOME_GRANTED
    assert decision.command_class == GIT_PUSH
    assert decision.grant_source == GRANT_SOURCE_PERMISSION_MODE
    assert decision.permission_mode == PERMISSION_MODE_UNBOUNDED
    # No config key: the grants table is not why this ran, and naming it would
    # send an operator to a stanza that had nothing to do with the outcome.
    assert decision.config_key is None
    assert decision.granted_by == "permission_mode=unbounded"


def test_bounded_scope_still_requires_a_grant():
    """The companion negative. If this ever passes for a bounded scope, the
    restriction lane is decorative."""

    decision = envelope_decision(
        "git push origin main", scope=_scope(PERMISSION_MODE_PROFILE_DEFAULT), cfg=_cfg()
    )

    assert decision.outcome == OUTCOME_REFUSE
    assert decision.failure_class == ENVELOPE_COMMAND_REQUIRES_GRANT
    assert decision.config_key == "agent_runtime.terminal_envelope.grants.dev.mission_chat"


def test_config_grant_keeps_its_own_provenance_under_the_new_posture():
    """A config grant must not start reporting itself as a mode grant — an
    operator reading a receipt has to be able to tell which lever fired."""

    decision = envelope_decision(
        "git push origin main",
        scope=_scope(PERMISSION_MODE_PROFILE_DEFAULT),
        cfg=_cfg(dev={LANE_MISSION_CHAT: [GIT_PUSH]}),
    )

    assert decision.outcome == OUTCOME_GRANTED
    assert decision.grant_source == GRANT_SOURCE_CONFIG
    assert decision.granted_by == "agent_runtime.terminal_envelope.grants.dev.mission_chat"


@pytest.mark.parametrize(
    "command,command_class",
    [
        ("git push origin main", GIT_PUSH),
        ("git reset --hard HEAD~1", DESTRUCTIVE_GIT),
        ("rm -rf /tmp/anything", RECURSIVE_DELETE),
        ("curl https://example.com/x", NETWORK_EGRESS),
    ],
)
def test_every_formerly_refused_class_writes_a_receipt_naming_the_mode(
    tmp_path, command, command_class
):
    """THE compensating control (plan §3.2 / §4.1).

    The ruling removes a PREVENTIVE control and substitutes a DETECTIVE one. A
    mode-granted command that runs unrecorded is not a weaker version of the
    ruling — it is the ruling with its safety argument deleted. This walks the
    real writer, through the real ladder, for all four classes.
    """

    root = tmp_path / "receipts"
    scope = _scope(PERMISSION_MODE_UNBOUNDED, runtime_root=root)

    decision = envelope_decision(command, scope=scope, cfg=_cfg())
    assert decision.outcome == OUTCOME_GRANTED
    assert decision.command_class == command_class

    record_envelope_decision(decision, command, scope=scope)

    rows = _decision_rows(root)
    assert len(rows) == 1
    row = rows[0]
    assert row["decision"] == OUTCOME_GRANTED
    assert row["command_class"] == command_class
    assert row["permission_mode"] == PERMISSION_MODE_UNBOUNDED
    assert row["grant_source"] == GRANT_SOURCE_PERMISSION_MODE
    assert row["granted_by"] == "permission_mode=unbounded"
    assert command[:20] in row["command_preview"]


def test_terminal_tool_seam_executes_and_receipts_a_mode_granted_command(tmp_path):
    """End-to-end through the seam the terminal tool actually consults.

    ``_harness_envelope_block`` returning ``None`` IS "this command proceeds to
    execution", so this proves both halves at the real integration point: the
    command is no longer blocked, AND a receipt exists for it.
    """

    from tools import terminal_tool as terminal_tool_module

    root = tmp_path / "seam"
    scope = _scope(PERMISSION_MODE_UNBOUNDED, runtime_root=root)

    with terminal_envelope_scope(scope):
        blocked = terminal_tool_module._harness_envelope_block("git push origin main")

    assert blocked is None  # would have been a typed refusal payload before
    rows = _decision_rows(root)
    assert [row["granted_by"] for row in rows] == ["permission_mode=unbounded"]


def test_permission_mode_never_lifts_a_hard_floor(monkeypatch):
    """R-2 left the floor empty, but the BRANCH is the guarantee. A future
    re-instated floor must not be liftable by a mode."""

    import agent_runtime.terminal_envelope as te

    monkeypatch.setattr(te, "GRANTABLE_COMMAND_CLASSES", frozenset())
    decision = te.envelope_decision(
        "git push origin main", scope=_scope(PERMISSION_MODE_UNBOUNDED), cfg=_cfg()
    )

    assert decision.outcome == OUTCOME_REFUSE
    assert decision.failure_class == te.ENVELOPE_COMMAND_NOT_GRANTABLE


def test_ungoverned_lane_is_untouched_by_the_mode():
    """No scope for a governed lane ⇒ ``None`` ⇒ legacy behavior, byte-for-byte.
    The ruling covers harness personas, not every Hermes surface on the box."""

    scope = TerminalEnvelopeScope(
        lane="free_chat", role="dev", permission_mode=PERMISSION_MODE_UNBOUNDED
    )

    assert envelope_decision("git push origin main", scope=scope, cfg=_cfg()) is None


# ── 5. invariants that must NOT move ────────────────────────────────────────


def test_registry_hygiene_survives_the_unbounded_default():
    """Plan §7.8(b) / §3.4. Kanban + feishu deregistration is junk removal, not a
    permission tier: ``profile_runner`` unions it at agent construction on every
    lane, so a preview that showed those tools as available would be lying about
    17 tools the runtime strips."""

    visibility = resolve_tool_visibility(_persona())

    assert visibility["permission_mode"] == PERMISSION_MODE_UNBOUNDED
    final = set(visibility["final_model_tools"])
    assert not (final & REGISTRY_HYGIENE_BLOCKED_TOOLS)
    blocked = {entry["name"] for entry in visibility["blocked_tools"]}
    assert blocked == set(REGISTRY_HYGIENE_BLOCKED_TOOLS)
    # The plan's stated wire move: 22 (5 persona-safety + 17 hygiene) -> 17.
    assert len(visibility["blocked_tools"]) == len(REGISTRY_HYGIENE_BLOCKED_TOOLS) == 17
    assert all(entry["reason"] == "registry_hygiene" for entry in visibility["blocked_tools"])


def test_persona_safety_tools_are_visible_again_under_the_default():
    """The other half of the same resolution: the five persona-safety names DO
    yield to the mode (that is the ruling), so the count moved to 17 rather than
    staying at 22 for a different reason."""

    from agent_runtime.personas import PERSONA_BLOCKED_TOOLS

    visibility = resolve_tool_visibility(_persona())
    final = set(visibility["final_model_tools"])
    blocked = {entry["name"] for entry in visibility["blocked_tools"]}

    # Not one of the five is blocked any more...
    safety_names = PERSONA_BLOCKED_TOOLS - REGISTRY_HYGIENE_BLOCKED_TOOLS
    assert safety_names and not (safety_names & blocked)
    # ...and the ones this environment registers are actually callable.
    # ``send_message`` is deliberately not asserted here: it is service-gated
    # (``check_fn``) and simply not registered without a messaging platform
    # configured, which is a capability fact, not a permission one.
    assert {"delegate_task", "memory", "cronjob"} <= final


def test_unbounded_never_widens_the_admitted_mcp_set():
    """Plan §7.8(a) / §3.3. Cross-persona isolation in a warm multi-persona
    process is not a permission question: ``unbounded`` resolves the FULL
    registry, which can contain another persona's admitted ``mcp-*`` toolsets."""

    from agent_runtime.mcp_admission import scope_toolsets_to_admission

    resolved = ["file", "terminal", "mcp-launcher_qa", "mcp-other"]

    assert scope_toolsets_to_admission(resolved, admitted_servers=()) == ["file", "terminal"]
    assert scope_toolsets_to_admission(resolved, admitted_servers=("launcher_qa",)) == [
        "file",
        "terminal",
        "mcp-launcher_qa",
    ]


def test_read_only_blocks_is_the_single_mutation_set():
    """The 7-name duplication the plan called out: ``tool_visibility`` no longer
    keeps a second copy, it reads this one."""

    from agent_runtime.tool_visibility import _mutating_tools

    assert _mutating_tools() is READ_ONLY_BLOCKS


# ── 6. the surfaces render the new state honestly ───────────────────────────


def test_capability_block_states_the_unbounded_posture_explicitly():
    """Plan §5. An ``unbounded`` turn drops nothing, so the block used to be
    empty — "honest silence" only reads as honest against an assumed bounded
    baseline. Under a DEFAULT of unbounded, an absence is just an absence."""

    block = resolve_capability_block(
        permission_mode=PERMISSION_MODE_UNBOUNDED,
        permission_source="runtime_default",
        envelope=explain_terminal_envelope(
            role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg(), permission_mode=PERMISSION_MODE_UNBOUNDED
        ),
    )

    assert block["posture"]["permission_mode"] == PERMISSION_MODE_UNBOUNDED
    rendered = render_capability_block(block)
    assert "No lane restrictions" in rendered
    assert "runtime default" in rendered
    assert ENVELOPE_DECISION_LOG in rendered


def test_capability_block_stays_silent_for_a_bounded_session():
    """The negative that keeps the line from becoming noise: a restricted
    session's block is byte-stable with what it emitted before this change."""

    block = resolve_capability_block(
        permission_mode=PERMISSION_MODE_PROFILE_DEFAULT,
        permission_source="operator",
    )

    assert "posture" not in block
    assert render_capability_block(block) == ""


def test_envelope_explanation_is_mode_aware():
    """An operator preview that read only the grants table would show classes as
    refused that the very next command would run."""

    explained = explain_terminal_envelope(
        role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg(), permission_mode=PERMISSION_MODE_UNBOUNDED
    )

    assert set(explained["granted"]) == set(GRANTABLE_COMMAND_CLASSES)
    assert set(explained["granted_by_permission_mode"]) == set(GRANTABLE_COMMAND_CLASSES)
    assert explained["granted_by_config"] == []
    assert explained["refused"] == []

    bounded = explain_terminal_envelope(role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg())
    assert bounded["granted"] == []
    assert set(bounded["refused"]) == set(GRANTABLE_COMMAND_CLASSES)


def test_agent_summary_reports_the_runtime_default_posture(tmp_path, monkeypatch):
    """The launcher's agents drawer renders these scalars; they must not describe
    a posture no turn runs under.

    What actually drives this is ``ToolVisibilityOptions.permission_mode``'s
    default factory: ``_agent_summary`` calls ``resolve_tool_visibility(agent)``
    with no options, and that resolve ALWAYS sets ``permission_mode``. So the
    ``or default_permission_mode()`` fallback beside it is defensive and
    unreachable — reverting it does NOT turn this test red, and a reader must not
    mistake this for coverage of that branch. The guarantee worth pinning is the
    one below: the scalar TRACKS THE CONFIGURED DEFAULT rather than any literal,
    which is exactly what a no-options preview would get wrong.
    """

    from agent_runtime.snapshot import _agent_summary

    summary = _agent_summary(_persona(), include_tool_details=True)

    assert summary["permission_mode"] == PERMISSION_MODE_UNBOUNDED
    assert summary["blocked_tools_count"] == len(REGISTRY_HYGIENE_BLOCKED_TOOLS)

    # Configure the runtime narrower and the drawer must follow it — not stay
    # pinned to the shipped default, and not fall back to a hardcoded literal.
    _write_root_config(
        tmp_path,
        monkeypatch,
        """
        agent_runtime:
          tool_permissions:
            default_mode: profile_default
        """,
    )
    narrowed = _agent_summary(_persona(), include_tool_details=True)

    assert narrowed["permission_mode"] == PERMISSION_MODE_PROFILE_DEFAULT
    # The five persona-safety names come back on top of the 17 hygiene names.
    assert narrowed["blocked_tools_count"] > len(REGISTRY_HYGIENE_BLOCKED_TOOLS)


def test_tool_diff_preview_can_still_ask_for_the_bounded_shape():
    """Plan §5. The hypothetical-mode preview is how an operator inspects what a
    restriction would do — it must survive the default flip."""

    bounded = resolve_tool_visibility(
        _persona(), ToolVisibilityOptions(permission_mode=PERMISSION_MODE_PROFILE_DEFAULT)
    )

    blocked = {entry["name"] for entry in bounded["blocked_tools"]}
    assert "delegate_task" in blocked
    assert len(bounded["blocked_tools"]) == 22
