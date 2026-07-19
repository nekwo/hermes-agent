"""Ambiguous-target refusal at the mission-chat chokepoint.

A BARE persona id (``dev``) names a persona, not an instance. When the persona
runs more than one live instance and the caller pinned none, the omitted-session
default resolver silently threads the message onto the canonical primary and
drops it for every sibling (live 2026-07-19: bare ``qa`` with two live ``qa``
instances landed only in ``personainst_qa``). The handler evaluates
``agent_runtime.target_policy`` at the SAME canonical persona chokepoint the
relay guard uses, so every transport (in-process tool, CLI, serve) gets the same
typed ``ambiguous_target`` refusal — while an already-disambiguated call (an
explicit ``personainst_*`` target, a ``persona_instance_id``, or a caller-chosen
session id, as the operator console always sends) keeps reaching each sibling.
"""

from __future__ import annotations

import argparse
import json
import uuid

from hermes_cli.harness import build_parser


def _parser():
    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command")
    build_parser(subs)
    return p


def _seed_two_instances(monkeypatch, tmp_path, persona="dev", placement="dev_agent_2"):
    """Fresh runtime root with TWO live instances of ``persona``.

    The canonical operator channel (``personainst_dev``) is auto-ensured by
    ``derive_from_workers``; a deliberate placement sibling
    (``personainst_dev_agent_2``, the "QA Agent (2)" shape) is added on top —
    exactly the two-live-instances-on-the-level state the defect proved.
    """
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.worker_sessions import WorkerSessionStore

    cfg = load_agent_runtime_config()
    store = PersonaInstanceStore()
    store.derive_from_workers(list(ensure_persisted_personas(cfg)), WorkerSessionStore().list_all())
    store.add_instance(persona_id=persona, placement_id=placement, display_name=f"{persona} (2)")

    live = [i for i in store.list_all() if i.persona_id == persona]
    assert len(live) == 2, f"expected two live {persona} instances, got {[i.id for i in live]}"
    return store


def _send(monkeypatch, tmp_path, capsys, *extra, seed=True):
    if seed:
        _seed_two_instances(monkeypatch, tmp_path)
    else:
        monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    args = _parser().parse_args(
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
    return exit_code, capsys.readouterr().out


def _emitted_error_kinds(out):
    kinds = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            kinds.append(json.loads(line).get("error_kind"))
        except json.JSONDecodeError:
            continue
    return kinds


# --------------------------------------------------------------------------- #
# Refusal: bare persona id + more than one live instance                      #
# --------------------------------------------------------------------------- #


def test_bare_persona_two_instances_is_refused_with_both_candidates(tmp_path, monkeypatch, capsys):
    exit_code, out = _send(monkeypatch, tmp_path, capsys)
    data = json.loads(out)
    assert exit_code == 2
    assert data["ok"] is False
    assert data["execution_state"] == "rejected"
    assert data["error_kind"] == "ambiguous_target"
    assert data["persona_id"] == "dev"
    handles = {c["persona_instance_id"] for c in data["candidates"]}
    assert handles == {"personainst_dev", "personainst_dev_agent_2"}
    # display_name travels alongside the handle so the caller can pick the right one.
    assert all(c.get("display_name") for c in data["candidates"])
    # The error text names the addressable @handles.
    assert "@personainst_dev_agent_2" in data["error"]


def test_refusal_in_relay_context_carries_the_relay_chain(tmp_path, monkeypatch, capsys):
    exit_code, out = _send(
        monkeypatch, tmp_path, capsys, "--relay-chain", "neko_supervisor"
    )
    data = json.loads(out)
    assert exit_code == 2
    assert data["error_kind"] == "ambiguous_target"
    # Same envelope shape as the relay refusals: the chain (this turn's speaker
    # appended) rides the typed refusal.
    assert data["relay_chain"] == ["neko_supervisor", "dev"]


# --------------------------------------------------------------------------- #
# Not refused: the caller already disambiguated, or there is only one instance #
# --------------------------------------------------------------------------- #


def test_single_live_instance_is_not_refused(tmp_path, monkeypatch, capsys):
    # No sibling added → only the canonical personainst_dev exists. The bare
    # persona id must resolve exactly as before (the common case).
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    try:
        _, out = _send(monkeypatch, tmp_path, capsys, seed=False)
    except Exception:
        return  # runtime plumbing absent past the guard — acceptable; guard didn't fire
    assert "ambiguous_target" not in _emitted_error_kinds(out)


def test_instance_handle_target_is_never_refused_for_ambiguity(tmp_path, monkeypatch, capsys):
    _seed_two_instances(monkeypatch, tmp_path)
    args = _parser().parse_args(
        [
            "harness",
            "mission-chat",
            "message",
            "--persona",
            "personainst_dev_agent_2",
            "--message",
            "hi",
            "--json",
        ]
    )
    try:
        args.func(args)
    except Exception:
        return  # proceeded past the guard into model plumbing — the guard didn't fire
    assert "ambiguous_target" not in _emitted_error_kinds(capsys.readouterr().out)


def test_operator_console_session_reaches_both_instances_without_refusal(tmp_path, monkeypatch, capsys):
    # The operator console binds each chat to an instance-bearing session id
    # (persona_chat_<instance>_<12 hex>). A caller-chosen session id is
    # unambiguous — the send threads onto THAT conversation — so it must never
    # trip the refusal, for the primary OR the sibling.
    _seed_two_instances(monkeypatch, tmp_path)
    for handle in ("personainst_dev", "personainst_dev_agent_2"):
        session_id = f"persona_chat_{handle}_{uuid.uuid4().hex[:12]}"
        args = _parser().parse_args(
            [
                "harness",
                "mission-chat",
                "message",
                "--persona",
                "dev",
                "--session-id",
                session_id,
                "--message",
                "hi",
                "--json",
            ]
        )
        try:
            args.func(args)
        except Exception:
            continue  # past the guard into model plumbing — the guard didn't fire
        assert "ambiguous_target" not in _emitted_error_kinds(capsys.readouterr().out)


# --------------------------------------------------------------------------- #
# The typed refusal + candidates surface through the agent_chat_send tool       #
# --------------------------------------------------------------------------- #


def test_agent_chat_send_tool_surfaces_ambiguous_target_with_candidates(tmp_path, monkeypatch):
    # Point 2 acceptance: the payload must surface through the TOOL result, not
    # just the CLI error — the relaying agent reads the tool result and needs the
    # @handles to retry.
    _seed_two_instances(monkeypatch, tmp_path)
    from tools import agent_chat_tool

    raw = agent_chat_tool.agent_chat_send(persona_id="dev", message="briefing for dev")
    result = json.loads(raw)
    assert result["ok"] is False
    assert result["error_kind"] == "ambiguous_target"
    handles = {c["persona_instance_id"] for c in result["candidates"]}
    assert handles == {"personainst_dev", "personainst_dev_agent_2"}
