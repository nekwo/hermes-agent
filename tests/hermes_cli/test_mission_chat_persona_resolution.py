"""Persona-id resolution at the mission-chat CLI boundary.

Mission Control payloads, legacy SessionDB rows, and agent tool calls leak
persona INSTANCE ids (``personainst_*``) into persona-id slots. The CLI
boundary resolves them at the single persona-id chokepoint instead of failing
the send with an uncaught ``unsupported persona`` ValueError (the Launcher
surfaced that as ``code=invalid_request`` with no actionable detail).
"""

import argparse
import json

import pytest

import hermes_cli.harness as harness
from hermes_cli.harness import build_parser


def parser():
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command")
    build_parser(subs)
    return p


def test_instance_shaped_id_resolves_to_persona(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    assert (
        harness._normalize_cli_persona_or_template_id("personainst_neko_supervisor")
        == "neko_supervisor"
    )


def test_instance_shaped_profile_id_resolves_to_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    assert (
        harness._normalize_cli_persona_or_template_id("personainst_profile_alice")
        == "profile:alice"
    )


def test_unresolvable_id_still_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    with pytest.raises(ValueError):
        harness._normalize_cli_persona_or_template_id("definitely_not_a_persona_xyz")


def test_mangled_persona_falls_back_to_instance_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    resolved = harness._resolve_mission_chat_persona_id(
        "Some Display Name", "personainst_dev"
    )
    assert resolved == "dev"


def test_mission_chat_message_rejects_unknown_persona_with_typed_payload(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    args = parser().parse_args(
        [
            "harness",
            "mission-chat",
            "message",
            "--persona",
            "totally_unknown_xyz",
            "--message",
            "hi",
            "--json",
        ]
    )

    assert args.func(args) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    assert data["capability_id"] == "mission.chat.message"
    assert data["execution_state"] == "rejected"
    assert data["error_kind"] == "unsupported_persona"
    assert "next_expected" in data
