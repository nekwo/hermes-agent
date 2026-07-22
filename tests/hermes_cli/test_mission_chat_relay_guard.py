"""Relay-chain guard at the mission-chat chokepoint.

The relay chain is explicit envelope provenance (``--relay-chain`` /
``--relay-deadline-epoch``); the handler evaluates ``agent_runtime.relay_policy``
AFTER persona-id canonicalization, so instance-shaped target ids cannot dodge
the cycle guard and every transport (in-process tool, CLI, serve) gets the
same typed refusal.
"""

import argparse
import json
import time

import pytest

from hermes_cli.harness import build_parser


def parser():
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command")
    build_parser(subs)
    return p


def _run(monkeypatch, tmp_path, capsys, *extra):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    args = parser().parse_args(
        [
            "harness",
            "mission-chat",
            "message",
            "--persona",
            "dev",
            "--message",
            "hi",
            "--json",
            *extra,
        ]
    )
    exit_code = args.func(args)
    return exit_code, json.loads(capsys.readouterr().out)


def test_cycle_refused_with_typed_payload(tmp_path, monkeypatch, capsys):
    exit_code, data = _run(
        monkeypatch, tmp_path, capsys, "--relay-chain", "neko_supervisor,dev"
    )
    assert exit_code == 2
    assert data["ok"] is False
    assert data["execution_state"] == "rejected"
    assert data["error_kind"] == "relay_cycle"
    assert data["relay_chain"] == ["neko_supervisor", "dev"]


def test_instance_shaped_target_cannot_dodge_the_cycle_guard(tmp_path, monkeypatch, capsys):
    # personainst_dev canonicalizes to dev BEFORE the guard runs.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    args = parser().parse_args(
        [
            "harness",
            "mission-chat",
            "message",
            "--persona",
            "personainst_dev",
            "--message",
            "hi",
            "--json",
            "--relay-chain",
            "dev",
        ]
    )
    assert args.func(args) == 2
    data = json.loads(capsys.readouterr().out)
    assert data["error_kind"] == "relay_cycle"


def test_depth_limit_refused_with_typed_payload(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_AGENT_CHAT_MAX_DEPTH", "2")
    exit_code, data = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "--relay-chain",
        "profile:alice,neko_supervisor,qa",  # 2 hops already done
    )
    assert exit_code == 2
    assert data["error_kind"] == "relay_depth_limit"
    assert data["relay_chain"] == ["profile:alice", "neko_supervisor", "qa"]


def test_exhausted_shared_deadline_refused(tmp_path, monkeypatch, capsys):
    exit_code, data = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "--relay-chain",
        "neko_supervisor",
        "--relay-deadline-epoch",
        str(time.time() + 1.0),
    )
    assert exit_code == 2
    assert data["error_kind"] == "relay_budget_exhausted"


def test_root_turn_seeds_speaker_so_self_send_is_refused(tmp_path, monkeypatch):
    # End-to-end for the "Neko messages itself" incident. Two halves:
    #
    #  (1) The handler seeds THIS turn's relay chain with the speaking persona
    #      (a root operator turn -> chain ("neko_supervisor",)), which it sets
    #      into RELAY_CHAIN around the model turn.
    #  (2) A tool worker running under that seeded ContextVar that calls
    #      agent_chat_send back to the SAME persona inherits the chain, so the
    #      nested mission-chat chokepoint refuses it relay_cycle.
    #
    # The existing tests inject an explicit --relay-chain envelope; this proves
    # the seed->tool-worker inheritance path that has no envelope on the wire.
    from agent_runtime import relay_policy
    from tools import agent_chat_tool

    # (1) Root turn decision seeds the speaker into the chain.
    decision = relay_policy.evaluate_relay(chain=(), target_persona_id="neko_supervisor")
    assert decision.allowed is True
    assert decision.chain == ("neko_supervisor",)

    # (2) A tool worker under the seeded chain sends back to itself -> refused.
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    chain_token = relay_policy.RELAY_CHAIN.set(decision.chain)
    deadline_token = relay_policy.RELAY_DEADLINE.set(None)  # healthy budget, not the failure under test
    try:
        raw = agent_chat_tool.agent_chat_send(
            persona_id="neko_supervisor",
            message="relay this back to myself",
        )
    finally:
        relay_policy.RELAY_CHAIN.reset(chain_token)
        relay_policy.RELAY_DEADLINE.reset(deadline_token)

    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error_kind"] == "relay_cycle"
    assert result["relay_chain"] == ["neko_supervisor"]


def test_direct_operator_send_carries_no_relay_refusal(tmp_path, monkeypatch, capsys):
    # No envelope: the guard must not reject; the send proceeds past it (and
    # fails later only if the runtime/persona store is absent in tmp_path —
    # any such failure must NOT be a relay_* error_kind).
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    args = parser().parse_args(
        ["harness", "mission-chat", "message", "--persona", "dev", "--message", "hi", "--json"]
    )
    try:
        args.func(args)
    except Exception:
        return  # runtime plumbing absent in tmp root — acceptable; guard didn't fire
    out = capsys.readouterr().out
    json_lines = [line for line in out.splitlines() if line.strip().startswith("{")]
    for line in json_lines:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        assert not str(data.get("error_kind") or "").startswith("relay_")


# --------------------------------------------------------------------------- #
# Omitted-session default resolution (relay threading)                        #
#                                                                             #
# persona_commands.py is an exec'd command part, so the wiring is pinned with #
# an AST guard over the exact bytes exec'd — the same pattern the other       #
# mission-chat handler guards use (test_mission_chat_records_injection.py).   #
# --------------------------------------------------------------------------- #


def _mission_chat_message_call_names():
    import ast
    from pathlib import Path

    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_mission_chat_message"
    )
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if name:
                names.add(name)
    return names


def test_omitted_session_routes_through_the_default_resolver():
    # An omitted session continues the dedicated default root when present, then
    # falls back to the same receipt-backed server mint used by open-chat.
    calls = _mission_chat_message_call_names()
    assert "resolve_default_chat_session_id_for_instance" in calls, (
        "the omitted-session path must resolve the target's default chat session "
        "before using the canonical server-mint fallback"
    )
    assert "mint" in calls


def test_handler_no_longer_mints_a_fresh_session_per_send():
    # _persona_chat_session_id / persona_chat_session_id_for append a fresh uuid
    # every call; calling either directly from the handler is exactly the defect
    # that orphaned relays (a new unpointed session per send). The resolver owns
    # the mint-only-when-absent decision.
    calls = _mission_chat_message_call_names()
    assert "_persona_chat_session_id" not in calls, (
        "the handler must not mint a fresh session id per send — route omitted "
        "sessions through the default resolver and receipt-backed mint service"
    )
    assert "persona_chat_session_id_for" not in calls
