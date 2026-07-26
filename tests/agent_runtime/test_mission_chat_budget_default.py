"""The mission-chat wall budget's default is operator config, not a CLI literal (G10).

240 s is a *conversation*-shaped window — after the graceful checkpoint reserve
(``turn_budget``: ``max(60s, 15%)``) a default turn has ~180 s of tool-using
time — and it was hardcoded in the mission-chat parser. The lane is now the
primary home for agent work, so the default becomes
``agent_runtime.mission_chat.default_max_seconds`` in the ROOT config
(mission-chat-lane-gap-audit.md G10).

The rules pinned here: an ABSENT stanza keeps today's behavior byte for byte,
an explicit ``--max-seconds`` always wins, and a configured value is clamped to
a window that can actually host a turn.
"""

from __future__ import annotations

import textwrap

import pytest

from agent_runtime.config import (
    MISSION_CHAT_MAX_MAX_SECONDS,
    MISSION_CHAT_MIN_MAX_SECONDS,
    load_agent_runtime_config,
    mission_chat_default_max_seconds,
    resolve_mission_chat_max_seconds,
)
from agent_runtime.runtime_config import MissionChatConfig

#: The value the CLI parser hardcoded before this block existed.
LEGACY_DEFAULT = 240.0


def _write_config(text: str):
    from hermes_constants import get_config_path

    path = get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _cfg(text: str):
    return load_agent_runtime_config(config_path=_write_config(text))


# ── the default ─────────────────────────────────────────────────────────────


def test_absent_stanza_keeps_the_historical_default():
    assert MissionChatConfig().default_max_seconds == LEGACY_DEFAULT
    assert (
        mission_chat_default_max_seconds(_cfg("agent_runtime: {}\n")) == LEGACY_DEFAULT
    )


def test_configured_value_is_applied():
    cfg = _cfg(
        """
        agent_runtime:
          mission_chat:
            default_max_seconds: 1800
        """
    )

    assert mission_chat_default_max_seconds(cfg) == 1800.0


def test_configured_value_is_clamped_at_both_ends():
    low = _cfg(
        """
        agent_runtime:
          mission_chat:
            default_max_seconds: 5
        """
    )
    high = _cfg(
        """
        agent_runtime:
          mission_chat:
            default_max_seconds: 999999
        """
    )

    # Below the floor the checkpoint reserve eats the whole window; above the
    # ceiling one turn outlives the mission wall-clock deadline.
    assert mission_chat_default_max_seconds(low) == MISSION_CHAT_MIN_MAX_SECONDS
    assert mission_chat_default_max_seconds(high) == MISSION_CHAT_MAX_MAX_SECONDS


def test_a_malformed_value_degrades_instead_of_failing_every_turn():
    cfg = _cfg(
        """
        agent_runtime:
          mission_chat:
            default_max_seconds: "not a number"
        """
    )

    assert mission_chat_default_max_seconds(cfg) == LEGACY_DEFAULT


def test_a_config_fault_degrades_to_the_built_in_default(monkeypatch):
    import agent_runtime.config as config_module

    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(config_module, "load_root_runtime_config", _boom)

    assert mission_chat_default_max_seconds() == LEGACY_DEFAULT


def test_the_default_resolves_against_the_root_config(monkeypatch, tmp_path):
    # Harness-wide operator policy: whichever profile happens to be sticky-active
    # must not be able to shorten (or extend) every other profile's turns — the
    # shadowing bug harness_root_config_path() documents for the restore knob.
    root = tmp_path / "hermes-root"
    profile_home = root / "profiles" / "tester"
    profile_home.mkdir(parents=True)
    (root / "config.yaml").write_text(
        textwrap.dedent(
            """
            agent_runtime:
              mission_chat:
                default_max_seconds: 900
            """
        ),
        encoding="utf-8",
    )
    (profile_home / "config.yaml").write_text(
        textwrap.dedent(
            """
            agent_runtime:
              mission_chat:
                default_max_seconds: 45
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    assert mission_chat_default_max_seconds() == 900.0


# ── precedence ──────────────────────────────────────────────────────────────


def test_explicit_request_always_wins():
    cfg = _cfg(
        """
        agent_runtime:
          mission_chat:
            default_max_seconds: 1800
        """
    )

    assert resolve_mission_chat_max_seconds(60.0, cfg) == 60.0
    # Including a value outside the config clamp: the clamp guards a
    # deployment-wide default, it does not cap a caller who states a number.
    assert resolve_mission_chat_max_seconds(5.0, cfg) == 5.0
    assert resolve_mission_chat_max_seconds(100_000.0, cfg) == 100_000.0


def test_absent_request_takes_the_configured_default():
    cfg = _cfg(
        """
        agent_runtime:
          mission_chat:
            default_max_seconds: 1800
        """
    )

    assert resolve_mission_chat_max_seconds(None, cfg) == 1800.0


def test_a_nonsense_request_falls_back_rather_than_arming_a_dead_budget():
    cfg = _cfg("agent_runtime: {}\n")

    assert resolve_mission_chat_max_seconds(0.0, cfg) == LEGACY_DEFAULT
    assert resolve_mission_chat_max_seconds(-30.0, cfg) == LEGACY_DEFAULT
    assert resolve_mission_chat_max_seconds("abc", cfg) == LEGACY_DEFAULT


# ── the CLI seam ────────────────────────────────────────────────────────────


def test_parser_default_keeps_absent_distinguishable_from_explicit():
    import argparse

    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    absent = parser.parse_args(
        ["harness", "mission-chat", "message", "--persona", "qa", "--message", "hi"]
    )
    explicit = parser.parse_args(
        [
            "harness",
            "mission-chat",
            "message",
            "--persona",
            "qa",
            "--message",
            "hi",
            "--max-seconds",
            "240",
        ]
    )

    # An argparse default of 240.0 would be indistinguishable from an explicit
    # `--max-seconds 240`, and "explicit wins" would be undecidable.
    assert absent.max_seconds is None
    assert explicit.max_seconds == 240.0


def test_absent_flag_resolves_to_the_configured_lane_default():
    import argparse

    from hermes_cli.harness import build_parser

    cfg = _cfg(
        """
        agent_runtime:
          mission_chat:
            default_max_seconds: 1200
        """
    )
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        ["harness", "mission-chat", "message", "--persona", "qa", "--message", "hi"]
    )

    assert resolve_mission_chat_max_seconds(args.max_seconds, cfg) == 1200.0
