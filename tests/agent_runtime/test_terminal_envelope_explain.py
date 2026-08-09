"""`hermes harness persona tool-diff --explain-envelope` — the operator's view.

``runtime_hud.render_capability_block`` already tells the AGENT what the
terminal envelope will refuse. The operator had no equivalent: debugging "why
did Dev refuse to push?" meant reading ``terminal_envelope.py`` to learn the
command taxonomy, then guessing the config key.

The verb closes that. These tests pin two things: that the payload answers the
questions the operator actually has (scope bound or not, which grants are
live, what disposition the lane is in), and that every
one of those answers is READ from the canonical authority rather than
re-derived here — a second derivation of the taxonomy is exactly how an operator
surface starts telling a different story than the runtime.
"""

from __future__ import annotations

import types

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime.permission_modes import PERMISSION_MODE_PROFILE_DEFAULT
from agent_runtime.runtime_config import TerminalEnvelopeConfig
from agent_runtime.terminal_envelope import (
    COMMAND_CLASSES,
    DESTRUCTIVE_GIT,
    GIT_PUSH,
    GOVERNED_LANES,
    GRANTABLE_COMMAND_CLASSES,
    LANE_MISSION_CHAT,
    NETWORK_EGRESS,
    RECURSIVE_DELETE,
    grant_config_key,
    hard_floor_command_classes,
)
from agent_runtime.terminal_envelope_explain import (
    DISPOSITION_DETERMINISTIC,
    DISPOSITION_LEGACY_AMBIENT,
    explain_persona_terminal_envelope,
    render_terminal_envelope_explanation,
)


def _cfg(**grants) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        terminal_envelope=TerminalEnvelopeConfig(grants=dict(grants))
    )


def _persona(persona_id: str = "dev", role: str = "dev"):
    return types.SimpleNamespace(id=persona_id, role=role)


#: This file's subject is the ROOT-config GRANTS TABLE — the lever that governs a
#: session an operator RESTRICTED. Since the 2026-08-09 ruling the runtime
#: default is ``unbounded``, which grants every grantable class by mode, so these
#: cases name the bounded tier explicitly rather than inheriting a default that
#: would make every one of them vacuous. The mode-granted view has its own
#: coverage in ``test_unbounded_default_posture.py`` and below.
BOUNDED = PERMISSION_MODE_PROFILE_DEFAULT


# ── the accessor this verb reads from ───────────────────────────────────────


def test_the_hard_floor_is_the_grantable_sets_complement():
    """Ruling R-2 leaves the compatibility accessor empty."""

    assert hard_floor_command_classes() == COMMAND_CLASSES - GRANTABLE_COMMAND_CLASSES
    assert hard_floor_command_classes() == set()


# ── the payload ─────────────────────────────────────────────────────────────


def test_an_ungranted_governed_lane_names_the_floor_and_the_key():
    """Deny by default. Every gated class is refused, and the split says which
    of them an operator could actually grant."""

    explained = explain_persona_terminal_envelope(_persona(), cfg=_cfg(), permission_mode=BOUNDED)

    assert explained["lane"] == LANE_MISSION_CHAT
    assert explained["role"] == "dev"
    assert explained["persona_id"] == "dev"
    assert explained["governed"] is True
    assert explained["governed_lanes"] == sorted(GOVERNED_LANES)
    assert explained["disposition"] == DISPOSITION_DETERMINISTIC
    assert explained["config_key"] == grant_config_key(role="dev", lane=LANE_MISSION_CHAT)

    assert explained["granted"] == []
    assert explained["refused"] == sorted(COMMAND_CLASSES)
    assert explained["refused_grantable"] == sorted(GRANTABLE_COMMAND_CLASSES)
    assert explained["refused_hard_floor"] == sorted(hard_floor_command_classes())
    assert explained["grant_issues"] == []


def test_an_active_grant_moves_the_class_out_of_refused():
    explained = explain_persona_terminal_envelope(
        _persona(), permission_mode=BOUNDED, cfg=_cfg(dev={LANE_MISSION_CHAT: [GIT_PUSH]})
    )
    assert explained["granted"] == [GIT_PUSH]
    assert GIT_PUSH not in explained["refused"]
    assert GIT_PUSH not in explained["refused_grantable"]
    assert set(explained["refused_grantable"]) == {
        DESTRUCTIVE_GIT,
        RECURSIVE_DELETE,
        NETWORK_EGRESS,
    }


def test_the_runtime_default_posture_shows_the_classes_as_granted_by_mode():
    """The operator view must not send someone writing a grant stanza for a
    class the runtime already allows.

    Under the 2026-08-09 default an unbounded run is granted every grantable
    class by MODE, with no config stanza. A view that read only the grants table
    would render all four as refused-but-grantable — the exact "surface tells a
    different story than the runtime" failure this module was written against.
    """

    explained = explain_persona_terminal_envelope(
        _persona(), permission_mode="unbounded", cfg=_cfg()
    )

    assert explained["permission_mode"] == "unbounded"
    assert set(explained["granted"]) == set(GRANTABLE_COMMAND_CLASSES)
    assert set(explained["granted_by_permission_mode"]) == set(GRANTABLE_COMMAND_CLASSES)
    assert explained["granted_by_config"] == []
    assert explained["refused"] == []

    text = "\n".join(render_terminal_envelope_explanation(explained))
    assert "permission mode: unbounded" in text
    assert "by permission mode:" in text
    assert "receipted" in text


def test_a_removed_floor_name_is_reported_as_unknown():
    """Old config does not resurrect a retired command class."""

    retired = "credential_" + "read"
    explained = explain_persona_terminal_envelope(
        _persona(), permission_mode=BOUNDED, cfg=_cfg(dev={LANE_MISSION_CHAT: [retired, GIT_PUSH]})
    )
    assert retired not in explained["granted"]
    assert explained["refused_hard_floor"] == []
    codes = {issue["code"] for issue in explained["grant_issues"]}
    assert "envelope_grant_unknown_command_class" in codes


def test_a_malformed_stanza_grants_nothing_and_says_so():
    explained = explain_persona_terminal_envelope(
        _persona(), permission_mode=BOUNDED, cfg=_cfg(dev={LANE_MISSION_CHAT: "git_push"})
    )
    assert explained["granted"] == []
    assert [issue["code"] for issue in explained["grant_issues"]] == [
        "envelope_grant_malformed"
    ]


def test_an_ungoverned_lane_reports_the_legacy_ambient_disposition():
    """The fail-open/fail-closed coin flip the governed lane retired. An
    operator must be able to tell which world a lane is in — an ungoverned lane
    behaves two opposite ways for the same command depending on whether
    ``HERMES_AGENT_RUNTIME_ROOT`` happens to be exported."""

    explained = explain_persona_terminal_envelope(
        _persona(), permission_mode=BOUNDED, lane="worker_tick", cfg=_cfg()
    )
    assert explained["governed"] is False
    assert explained["disposition"] == DISPOSITION_LEGACY_AMBIENT
    assert "fail-CLOSED" in explained["disposition_summary"]
    assert "fail-OPEN" in explained["disposition_summary"]


def test_the_supervisor_role_alias_resolves_to_the_canonical_grant_key():
    """The live persona is spelled ``neko``; the canonical role is
    ``alice_supervisor``. An operator reading this payload must be given the key
    the grant table actually reads."""

    explained = explain_persona_terminal_envelope(
        _persona("neko_supervisor", "neko_supervisor"),
        permission_mode=BOUNDED,
        cfg=_cfg(alice_supervisor={LANE_MISSION_CHAT: [NETWORK_EGRESS]}),
    )
    assert explained["role"] == "alice_supervisor"
    assert explained["config_key"].endswith("alice_supervisor.mission_chat")
    assert explained["granted"] == [NETWORK_EGRESS]


def test_the_payload_reads_the_taxonomy_from_the_authority():
    """No parallel class list here. If the taxonomy ever grows, this payload
    grows with it automatically."""

    explained = explain_persona_terminal_envelope(_persona(), cfg=_cfg(), permission_mode=BOUNDED)
    assert explained["command_classes"] == sorted(COMMAND_CLASSES)
    assert explained["grantable_command_classes"] == sorted(GRANTABLE_COMMAND_CLASSES)
    assert explained["hard_floor_command_classes"] == sorted(hard_floor_command_classes())
    assert set(explained["refused_grantable"]) | set(explained["refused_hard_floor"]) == set(
        explained["refused"]
    )


def test_explaining_is_side_effect_free():
    """Inspection only, exactly like ``--explain-mcp``: an operator can read what
    a persona WOULD get without running a turn, a command, or a tool."""

    first = explain_persona_terminal_envelope(_persona(), cfg=_cfg(), permission_mode=BOUNDED)
    second = explain_persona_terminal_envelope(_persona(), cfg=_cfg(), permission_mode=BOUNDED)
    assert first == second


# ── the human rendering ─────────────────────────────────────────────────────


def test_the_text_rendering_states_every_decision_relevant_fact():
    explained = explain_persona_terminal_envelope(
        _persona(), permission_mode=BOUNDED, cfg=_cfg(dev={LANE_MISSION_CHAT: [GIT_PUSH, "git-push"]})
    )
    text = "\n".join(render_terminal_envelope_explanation(explained))

    assert "terminal envelope (mission_chat, role=dev): GOVERNED" in text
    assert "grant config key: agent_runtime.terminal_envelope.grants.dev.mission_chat" in text
    assert "granted:  git_push" in text
    assert "refused (operator-grantable): destructive_git, network_egress, recursive_delete" in text
    assert "refused (HARD FLOOR, no config lifts): -" in text
    assert "grant issue [envelope_grant_unknown_command_class]" in text


def test_the_text_rendering_uses_a_dash_for_an_empty_set_never_a_blank():
    explained = explain_persona_terminal_envelope(_persona(), cfg=_cfg(), permission_mode=BOUNDED)
    text = "\n".join(render_terminal_envelope_explanation(explained))
    assert "granted:  -" in text


def test_rendering_an_empty_payload_is_silent():
    assert render_terminal_envelope_explanation({}) == []
    assert render_terminal_envelope_explanation(None) == []


# ── the CLI verb ────────────────────────────────────────────────────────────


def _tool_diff(argv: list[str]):
    import argparse
    import json

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["harness", "persona", "tool-diff", *argv])


def test_the_flag_emits_the_envelope_payload_and_nothing_runs(capsys):
    """Mirrors ``--explain-mcp``: inspection only, machine-readable, no side
    effects. An operator reads what the lane WOULD do without running a
    command."""

    import json

    args = _tool_diff(["dev", "--explain-envelope", "--json"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    envelope = payload["terminal_envelope"]
    assert envelope["lane"] == LANE_MISSION_CHAT
    assert envelope["governed"] is True
    assert envelope["disposition"] == DISPOSITION_DETERMINISTIC
    assert envelope["hard_floor_command_classes"] == sorted(hard_floor_command_classes())
    assert envelope["config_key"].startswith("agent_runtime.terminal_envelope.grants.")


def test_without_the_flag_the_payload_is_unchanged(capsys):
    """Additive, exactly like ``--explain-mcp``: a caller that does not ask pays
    nothing and sees the pre-existing shape."""

    import json

    args = _tool_diff(["dev", "--json"])
    assert args.func(args) == 0
    payload = json.loads(capsys.readouterr().out)

    assert "terminal_envelope" not in payload
    assert "tool_visibility" in payload


def test_the_human_output_prints_the_envelope_lines(capsys):
    args = _tool_diff(["dev", "--explain-envelope"])
    assert args.func(args) == 0
    out = capsys.readouterr().out

    assert "terminal envelope (mission_chat, role=dev)" in out
    assert "refused (HARD FLOOR, no config lifts):" in out
