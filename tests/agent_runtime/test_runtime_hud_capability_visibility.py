"""The agent can SEE its own capability drops, mid-turn.

Wave-2 typed both halves of what this lane takes away and neither reached the
model:

* ``toolset_dropped_by_chat_lane_policy`` / ``tool_dropped_by_chat_lane_policy``
  rows (``chat_lane_toolsets``, commit ``7b8c68942``) — accounted for the
  OPERATOR through ``persona tool-diff``, invisible to the agent.
* the terminal-envelope command classes (``terminal_envelope``, commits
  ``6889656ad..a3316572d``) — an agent discovered a refusal only by running the
  command and reading the block.

So a mission-chat agent asked to run a command found no ``terminal`` tool, read
the plain absence as a permission problem, and improvised — the exact failure
class ``mcp_lane`` was written to retire, recurring (lane gap audit §6 / G5,
and ``archive/2026-08-22-pre-consolidation/mission-chat-terminal-envelope-grants.md`` §7.2 "turn-start visibility").

These tests pin the fix and, more importantly, its CONTRACT: the account rides
the runtime-context envelope's volatile tail exactly like the wall-budget line
(``8e7a37d6d``) and never enters the HUD revision hash, so a cached body can
never show a stale capability claim and a stable picture never re-snapshots
because a drop list was restated.
"""

from __future__ import annotations

import types

import pytest

from agent_runtime.chat_lane_toolsets import (
    ChatLaneDrop,
    DROP_KIND_TOOL,
    DROP_KIND_TOOLSET,
    chat_lane_restore_config_key,
    chat_lane_tool_drops,
    chat_lane_toolset_drops,
)
from agent_runtime.runtime_hud import (
    CAPABILITY_HUD_KEY,
    SITUATIONAL_HUD_CAPABILITY_CAP,
    capability_block_for_persona,
    render_capability_block,
    render_runtime_context_envelope,
    render_situational_hud_block,
    resolve_capability_block,
    resolve_situational_hud,
    situational_hud_revision,
)
from agent_runtime.terminal_envelope import (
    COMMAND_CLASSES,
    DESTRUCTIVE_GIT,
    GIT_PUSH,
    GRANTABLE_COMMAND_CLASSES,
    LANE_MISSION_CHAT,
    NETWORK_EGRESS,
    RECURSIVE_DELETE,
    explain_terminal_envelope,
    grant_config_key,
)
from agent_runtime.runtime_config import TerminalEnvelopeConfig


# ── helpers ─────────────────────────────────────────────────────────────────


def _cfg(**grants) -> types.SimpleNamespace:
    """A stand-in runtime config carrying only the terminal-envelope block.

    Same shape as ``test_terminal_envelope_grants._cfg`` — the grants table is
    read from the ROOT config, and these tests must never depend on the host's.
    """

    return types.SimpleNamespace(
        terminal_envelope=TerminalEnvelopeConfig(grants=dict(grants))
    )


def _dev_drops():
    """The REAL droppers' output for a dev-shaped chat lane.

    Produced by ``chat_lane_toolset_drops`` / ``chat_lane_tool_drops`` rather
    than hand-built rows, so a change to the cost policy shows up here instead
    of being mirrored into a fixture that quietly drifts.
    """

    resolved = ["file", "search", "terminal", "session_search", "skills"]
    return chat_lane_toolset_drops(resolved, persona_id="dev") + chat_lane_tool_drops(
        persona_id="dev",
        enabled_toolsets=["search", "session_search", "skills"],
        toolset_for_tool=lambda name: "skills",
    )


def _instance(**overrides):
    base = dict(
        id="personainst_dev",
        persona_id="dev",
        role="dev",
        display_name="Launcher Dev",
        goal_id=None,
        current_task_id=None,
        state="idle",
        mode="configured",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


# ── (1) the block renders the rows ──────────────────────────────────────────


def test_the_capability_block_renders_the_typed_drops_and_the_envelope_refusals():
    """The whole point: what was dropped, what is refused, and by whose key.

    A row the agent cannot act on is decoration. Each half must name the ONE
    authority that could change it — the restore key for a cost-policy drop, the
    grant key for a grantable refusal — so the agent relays a fact instead of
    hunting for a permission mode that does not exist.
    """

    block = resolve_capability_block(
        drops=_dev_drops(),
        envelope=explain_terminal_envelope(
            role="dev",
            lane=LANE_MISSION_CHAT,
            cfg=_cfg(dev={LANE_MISSION_CHAT: [GIT_PUSH]}),
        ),
    )

    # The typed halves survive into the projection the operator's CONTEXT peek
    # reads, keyed apart so a toolset drop is never confused with a tool cut.
    assert block["toolsets_dropped"] == ["file", "terminal"]
    assert block["tools_dropped"] == ["skill_manage"]
    assert block["restorable_via"] == [chat_lane_restore_config_key("dev")]

    rendered = render_capability_block(block)

    # Drops half: subjects, the restore key, and the "not a permission problem"
    # framing that keeps the agent off the goose chase.
    assert "file" in rendered and "terminal" in rendered and "skill_manage" in rendered
    assert chat_lane_restore_config_key("dev") in rendered
    assert "By design" in rendered
    assert "NOT a permission problem" in rendered
    assert "OPERATOR" in rendered

    # Envelope half: what was granted, what an operator could grant, and the key
    # that would grant it.
    assert GIT_PUSH in rendered
    assert grant_config_key(role="dev", lane=LANE_MISSION_CHAT) in rendered
    for name in (DESTRUCTIVE_GIT, RECURSIVE_DELETE, NETWORK_EGRESS):
        assert name in rendered
    assert "hard floor" not in rendered

    # Compact by construction: this rides EVERY turn.
    assert len(rendered.splitlines()) == 2


def test_a_granted_class_is_reported_as_granted_not_refused():
    """Honesty in the other direction: an agent that CAN push must be told so."""

    block = resolve_capability_block(
        envelope=explain_terminal_envelope(
            role="dev",
            lane=LANE_MISSION_CHAT,
            cfg=_cfg(dev={LANE_MISSION_CHAT: [GIT_PUSH, DESTRUCTIVE_GIT]}),
        )
    )
    envelope = block["envelope"]
    assert sorted(envelope["granted"]) == sorted([GIT_PUSH, DESTRUCTIVE_GIT])
    assert GIT_PUSH not in envelope.get("refused_grantable", [])
    assert GIT_PUSH not in envelope.get("refused_hard_floor", [])


def test_s12_leaves_no_hard_floor_classes():

    block = resolve_capability_block(
        envelope=explain_terminal_envelope(
            role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg()
        )
    )
    envelope = block["envelope"]
    assert set(envelope["refused_grantable"]) == set(GRANTABLE_COMMAND_CLASSES)
    assert set(COMMAND_CLASSES) == set(GRANTABLE_COMMAND_CLASSES)
    assert "refused_hard_floor" not in envelope
    # The two partition the taxonomy — a class can never be missing from both.
    assert set(envelope["refused_grantable"]) == set(COMMAND_CLASSES)


# ── (2) volatile: the revision hash must not move ───────────────────────────


def test_the_hud_revision_is_unchanged_when_only_the_capability_account_changes():
    """The volatile-tail contract, mirroring ``turn_budget`` (``8e7a37d6d``).

    Hashing this into the HUD revision would re-snapshot the whole stable block
    the moment a drop list, a grant, or a config issue changed — and the block
    is restated every turn, so the snapshot/unchanged delivery contract would
    collapse into "always snapshot".
    """

    instance = _instance()
    kwargs = dict(realm="default", workspace="default", roster=[instance])

    quiet = resolve_situational_hud(instance, capability={}, **kwargs)
    dropped = resolve_situational_hud(
        instance,
        capability=resolve_capability_block(
            drops=_dev_drops(),
            envelope=explain_terminal_envelope(
                role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg()
            ),
        ),
        **kwargs,
    )
    granted = resolve_situational_hud(
        instance,
        capability=resolve_capability_block(
            envelope=explain_terminal_envelope(
                role="dev",
                lane=LANE_MISSION_CHAT,
                cfg=_cfg(dev={LANE_MISSION_CHAT: [GIT_PUSH]}),
            )
        ),
        **kwargs,
    )

    # The account really did change between snapshots...
    assert dropped[CAPABILITY_HUD_KEY] != granted[CAPABILITY_HUD_KEY]
    assert CAPABILITY_HUD_KEY not in quiet
    # ...and the revision did not move for any of them.
    assert (
        situational_hud_revision(quiet)
        == situational_hud_revision(dropped)
        == situational_hud_revision(granted)
    )


def test_the_capability_account_never_reaches_the_hashed_hud_body():
    """The body is what the revision hashes; the account must ride the tail.

    Guards the failure mode the contract exists to prevent: a later change that
    renders the block into ``render_situational_hud_block`` would silently move
    volatile content behind the hash.
    """

    instance = _instance()
    capability = resolve_capability_block(
        drops=_dev_drops(),
        envelope=explain_terminal_envelope(
            role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg()
        ),
    )
    hud = resolve_situational_hud(
        instance, realm="default", workspace="default", roster=[instance],
        capability=capability,
    )

    body = render_situational_hud_block(hud)
    assert "Dropped on this lane" not in body
    assert "Terminal envelope" not in body
    assert "skill_manage" not in body


def test_the_block_rides_the_volatile_tail_on_every_delivery():
    """Emitted on snapshot, unchanged AND unavailable — the whole reason it is
    volatile rather than cheap. An ``unavailable`` delivery drops the HUD body
    entirely; a capability fact that vanished with it would leave the agent
    believing it still has what this turn took away."""

    hud = {"preview": True, "lane": {"role": "dev"}}
    revision = situational_hud_revision(hud)
    line = render_capability_block(
        resolve_capability_block(
            drops=_dev_drops(),
            envelope=explain_terminal_envelope(
                role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg()
            ),
        )
    )
    assert line

    for delivery in ("snapshot", "unchanged", "unavailable"):
        envelope = render_runtime_context_envelope(
            context_id="ctx_1",
            revision=revision,
            delivery=delivery,
            situational_hud_content=render_situational_hud_block(hud),
            volatile_content=line,
        )
        assert line in envelope

    assert situational_hud_revision(hud) == revision
    assert line not in revision


# ── (3) silence when there is nothing to say ────────────────────────────────


def test_an_empty_capability_account_renders_no_block():
    """No drops and no governed envelope ⇒ NO line. The block enters the agent's
    context every turn, so a lane with nothing to report must pay nothing —
    a standing "you were not denied anything" bullet is noise that teaches the
    agent to skim the tail."""

    assert resolve_capability_block() == {}
    assert resolve_capability_block(drops=(), envelope=None) == {}
    assert render_capability_block({}) == ""
    assert render_capability_block(None) == ""
    assert render_capability_block(resolve_capability_block()) == ""


def test_an_unbounded_turn_reports_no_drops():
    """``unbounded`` genuinely bypasses the cost policy, so there is nothing to
    account for — ``chat_lane_capability_drops`` already returns ``()`` there and
    the block must not invent a row from an empty tuple."""

    block = resolve_capability_block(drops=())
    assert "toolsets_dropped" not in block
    assert "tools_dropped" not in block


def test_an_ungoverned_lane_contributes_no_envelope_half():
    """Only ``mission_chat`` is governed. On every other lane the legacy pattern
    table decides and this policy has no answer — claiming a refusal posture
    there would be a HUD that describes a gate that is not bound."""

    block = resolve_capability_block(
        envelope=explain_terminal_envelope(
            role="dev", lane="worker", cfg=_cfg(dev={"worker": [GIT_PUSH]})
        )
    )
    assert "envelope" not in block
    assert render_capability_block(block) == ""


def test_a_malformed_drop_row_is_skipped_rather_than_rendered_as_a_subject():
    """A drop with no kind belongs to no list. Rendering it under a guessed key
    would put a capability claim in the agent's context that no dropper made."""

    block = resolve_capability_block(
        drops=(
            ChatLaneDrop(subject="", kind=DROP_KIND_TOOLSET, code="c", restorable_via="k"),
            ChatLaneDrop(subject="mystery", kind="unknown", code="c", restorable_via="k"),
            ChatLaneDrop(subject="file", kind=DROP_KIND_TOOLSET, code="c", restorable_via="k"),
            ChatLaneDrop(subject="skill_manage", kind=DROP_KIND_TOOL, code="c", restorable_via="k"),
        )
    )
    assert block["toolsets_dropped"] == ["file"]
    assert block["tools_dropped"] == ["skill_manage"]
    assert "mystery" not in render_capability_block(block)


# ── bounds + degradation ────────────────────────────────────────────────────


def test_capability_lists_are_capped_so_the_block_cannot_become_a_wall():
    """Bounded exactly like the roster: this rides every turn, and a policy that
    later widens the excluded set must not silently turn two lines into a wall.
    Overflow is COUNTED, never dropped without saying so."""

    many = tuple(
        ChatLaneDrop(
            subject=f"toolset_{index}",
            kind=DROP_KIND_TOOLSET,
            code="c",
            restorable_via="key",
        )
        for index in range(SITUATIONAL_HUD_CAPABILITY_CAP + 5)
    )
    rendered = render_capability_block(resolve_capability_block(drops=many))
    assert "(+5 more)" in rendered
    assert f"toolset_{SITUATIONAL_HUD_CAPABILITY_CAP}" not in rendered


def test_grant_config_issues_are_surfaced_so_a_typo_is_not_read_as_a_grant():
    """An operator who mistyped ``git-push`` believes the grant is live. The
    agent must be told the stanza grants less than it reads, not silently kept
    at the refusal it cannot explain."""

    block = resolve_capability_block(
        envelope=explain_terminal_envelope(
            role="dev",
            lane=LANE_MISSION_CHAT,
            cfg=_cfg(dev={LANE_MISSION_CHAT: ["git-push", "credential_" + "read"]}),
        )
    )
    assert block["envelope"]["grant_issues"]
    assert "grant-config issue" in render_capability_block(block)


def test_capability_block_for_persona_degrades_each_half_independently(monkeypatch):
    """A fault resolving the drops must not blank the envelope posture, and vice
    versa. Both halves are best-effort — the account decorates a turn, it never
    blocks one — but "best effort" must not mean "all or nothing"."""

    import agent_runtime.persona_runtime as persona_runtime
    import agent_runtime.terminal_envelope as terminal_envelope

    persona = types.SimpleNamespace(id="dev", role="dev")

    def _boom(*_args, **_kwargs):
        raise RuntimeError("resolution fault")

    # Drops fault ⇒ envelope half survives.
    monkeypatch.setattr(persona_runtime, "chat_lane_capability_drops", _boom)
    monkeypatch.setattr(
        terminal_envelope,
        "explain_terminal_envelope",
        lambda **_kwargs: explain_terminal_envelope(
            role="dev", lane=LANE_MISSION_CHAT, cfg=_cfg()
        ),
    )
    block = capability_block_for_persona(persona, session_id="chat-1")
    assert "envelope" in block
    assert "toolsets_dropped" not in block

    # Envelope fault ⇒ drops half survives.
    monkeypatch.setattr(
        persona_runtime, "chat_lane_capability_drops", lambda *a, **k: _dev_drops()
    )
    monkeypatch.setattr(terminal_envelope, "explain_terminal_envelope", _boom)
    block = capability_block_for_persona(persona, session_id="chat-1")
    assert block["toolsets_dropped"] == ["file", "terminal"]
    assert "envelope" not in block

    # No persona ⇒ nothing, never a raise.
    assert capability_block_for_persona(None) == {}


@pytest.mark.parametrize("key", ["turn_budget", CAPABILITY_HUD_KEY])
def test_every_volatile_key_is_excluded_from_the_revision(key):
    """The contract itself, stated once per member: anything on the tail is out
    of the hash. A key added to the HUD without being added to
    ``_VOLATILE_HUD_KEYS`` would churn the revision every turn."""

    base = {"preview": True, "lane": {"role": "dev"}}
    assert situational_hud_revision(base) == situational_hud_revision(
        {**base, key: {"changed": "every turn"}}
    )
