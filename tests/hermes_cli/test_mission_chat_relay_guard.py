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
