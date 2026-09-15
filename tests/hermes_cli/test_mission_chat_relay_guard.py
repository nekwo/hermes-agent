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


def _json_documents(out: str) -> list[dict]:
    """Every JSON object in *out*, however it was formatted.

    The reader this replaces was LINE-oriented (``line.startswith("{")`` then
    ``json.loads(line)``), and the emitter on this lane PRETTY-PRINTS: the real
    payload arrives as a multi-line object, so that reader collected exactly
    ``['{']``, failed to decode it, ``continue``d, and inspected nothing. A
    stream decoder does not care how the producer chose to indent.
    """

    decoder = json.JSONDecoder()
    found: list[dict] = []
    index = 0
    while index < len(out):
        if out[index] != "{":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(out, index)
        except json.JSONDecodeError:
            index += 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = end
    return found


def test_the_json_reader_sees_a_pretty_printed_payload():
    """Anti-vacuity for the reader above, pinned on synthetic text.

    The gate below asserts that something is ABSENT from what it read. That is
    the shape that keeps passing once the reader stops reading, so the reader
    is proven independently — including on the exact formatting the lane uses.
    """

    pretty = json.dumps({"ok": False, "error_kind": "relay_cycle"}, indent=2, sort_keys=True)
    assert _json_documents(pretty) == [{"ok": False, "error_kind": "relay_cycle"}]
    assert [d["error_kind"] for d in _json_documents("noise\n" + pretty + "\n" + pretty)] == [
        "relay_cycle",
        "relay_cycle",
    ]
    assert _json_documents("not json at all\n") == []
    # The old line-oriented reader on the same text, for the record.
    assert [ln for ln in pretty.splitlines() if ln.strip().startswith("{")] == ["{"]


def test_direct_operator_send_carries_no_relay_refusal(tmp_path, monkeypatch, capsys):
    """No envelope: the guard must not reject. The send proceeds past it and
    fails later on the PERSONA lane, because the store is absent in tmp_path —
    and that failure must not be a ``relay_*`` one.

    REBUILT 2026-08-20 (MCF-53 sweep). The previous shape had two independent
    routes to a silent pass:

    1. The reader was LINE-oriented while this lane's emitter PRETTY-PRINTS.
       Measured on the real lane: it returns 2 and writes a multi-line object,
       so ``[line for line in out.splitlines() if line.strip().startswith("{")]``
       collected exactly ``['{']``, that entry raised ``JSONDecodeError``, it
       was ``continue``d, and the only assertion in the test never executed. A
       single-line refusal WOULD have been caught; a refusal in the formatting
       the runtime actually uses would not. That is the whole defect: the gate
       could see a shape the producer does not emit.
    2. ``try: args.func(args) / except Exception: return`` accepted ANY failure
       as success — including the relay guard itself raising, which is the one
       thing this test exists to notice.

    Both are fixed: the payload is decoded as a STREAM (indentation-agnostic),
    a raise is inspected instead of swallowed, and the claim is backed
    POSITIVELY first. A stability claim — "no relay refusal" — is satisfied
    exactly as well by producing nothing at all (MCF-50), so the test proves it
    read something before it says what it did not find.
    """

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    args = parser().parse_args(
        ["harness", "mission-chat", "message", "--persona", "dev", "--message", "hi", "--json"]
    )
    try:
        exit_code = args.func(args)
    except Exception as exc:  # noqa: BLE001 - the exception IS the evidence here
        # A raise is acceptable ONLY if it is not the relay lane. Blanket
        # acceptance is the second silent-pass route, and it swallowed the one
        # failure this test exists to notice: the guard itself blowing up.
        evidence = " ".join(
            str(part)
            for part in (
                type(exc).__name__,
                getattr(exc, "code", None),
                getattr(exc, "error_kind", None),
                exc,
            )
            if part is not None
        ).lower()
        assert "relay" not in evidence, (
            "the direct operator send — which carries NO relay envelope — failed "
            f"on the relay lane: {exc!r}"
        )
        return

    out = capsys.readouterr().out
    assert out.strip(), (
        "the direct operator send emitted NOTHING, so this test read nothing "
        "and its claim is unbacked. --json is on; the lane must answer."
    )
    payloads = _json_documents(out)
    assert payloads, (
        "stdout carried no decodable JSON document, so nothing was inspected:\n"
        + out[:400]
    )
    for data in payloads:
        assert not str(data.get("error_kind") or "").startswith("relay_"), (
            f"a send with NO relay envelope was refused by the relay guard: {data}"
        )
    # The positive half: this lane fails on the PERSONA store, which is what
    # "the guard did not fire, the send proceeded past it" actually looks like.
    kinds = [d.get("error_kind") for d in payloads]
    assert exit_code == 2 and "unsupported_persona" in kinds, (
        "the direct operator lane no longer fails on the absent persona store. "
        "That is not automatically wrong, but this test's whole premise is that "
        f"it gets PAST the relay guard and dies later: {exit_code} {kinds}"
    )


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
