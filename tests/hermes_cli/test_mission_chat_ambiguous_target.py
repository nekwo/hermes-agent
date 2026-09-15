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


def _seed_two_instances(monkeypatch, tmp_path, persona="dev"):
    """Fresh runtime root with two deliberate PLACEMENTS of ``persona``.

    The canonical operator channel (``personainst_dev``) is auto-ensured by
    ``ensure_for_personas``, but "placements shadow canonical" now drops it from
    the addressable candidate set whenever a placement of the persona is in
    scope. So a genuinely ambiguous bare-persona send needs TWO placements
    (``personainst_dev_agent_2`` + ``personainst_dev_agent_3``, both runtime-
    global here so both stay in scope) — canonical + one placement would instead
    auto-route to that single placement. These two placements are exactly the
    "two live siblings on the level" state the ambiguity defect proved.
    """
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore
    from tests.agent_runtime.persona_samples import sample_persona

    cfg = load_agent_runtime_config()
    AgentStore().save(sample_persona(persona))
    store = PersonaInstanceStore()
    store.ensure_for_personas(list(ensure_persisted_personas(cfg)))
    store.add_instance(persona_id=persona, placement_id="dev_agent_2", display_name=f"{persona} (2)")
    store.add_instance(persona_id=persona, placement_id="dev_agent_3", display_name=f"{persona} (3)")

    live = [i for i in store.list_all() if i.persona_id == persona]
    # canonical + two placements; the two placements are what the shadow leaves
    # addressable, and that is the ambiguous pair.
    assert len(live) == 3, f"expected canonical + two live {persona} placements, got {[i.id for i in live]}"
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
    # The two PLACEMENTS are the candidates; the plumbing canonical row is
    # shadowed out (placements shadow canonical), so it is never offered.
    assert handles == {"personainst_dev_agent_2", "personainst_dev_agent_3"}
    assert "personainst_dev" not in handles
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
    # Two placements are the candidates; the shadowed canonical row is not offered.
    assert handles == {"personainst_dev_agent_2", "personainst_dev_agent_3"}


# --------------------------------------------------------------------------- #
# Workspace-scoped candidate enumeration (D1)                                  #
#                                                                              #
# A BARE persona id is resolved only among the placements in the SENDER's      #
# workspace (plus runtime-global rows). Placements in other workspaces are not #
# counted, so a two-agent order does not fan out onto duplicate placements in  #
# unrelated workspaces. Explicit @handle targeting stays allowed cross-        #
# workspace. The typed ambiguous_target rejection is unchanged for genuine     #
# in-scope duplicates.                                                         #
# --------------------------------------------------------------------------- #


def _seed_dev_scoped(monkeypatch, tmp_path, placements, *, active="ws_home"):
    """Fresh runtime root with an ACTIVE workspace and ``dev`` placements.

    ``placements`` is ``[(placement_id, workspace_id), ...]`` for persona
    ``dev``. Every referenced workspace (and ``active``) is created with an
    explicit id and ``active`` is set active. The canonical ``personainst_dev``
    auto-ensured by ``ensure_for_personas`` is runtime-global (no workspace
    pointer), so it is always in scope.
    """
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore, WorkspaceStore
    from tests.agent_runtime.persona_samples import sample_persona

    cfg = load_agent_runtime_config()
    AgentStore().save(sample_persona("dev"))
    ws_store = WorkspaceStore()
    for workspace_id in {active, *(wid for _, wid in placements)}:
        ws_store.create(name=workspace_id, workspace_id=workspace_id)
    ws_store.set_active(active)

    store = PersonaInstanceStore()
    store.ensure_for_personas(list(ensure_persisted_personas(cfg)))
    for placement_id, workspace_id in placements:
        store.add_instance(
            persona_id="dev",
            placement_id=placement_id,
            workspace_id=workspace_id,
            display_name=placement_id,
        )
    return store


def _run_bare_dev(capsys, *extra, persona="dev"):
    """Run a bare mission-chat send and return stdout, tolerating the model
    plumbing that runs past an ALLOWED guard (the guard emits before it)."""
    args = _parser().parse_args(
        [
            "harness",
            "mission-chat",
            "message",
            "--persona",
            persona,
            "--message",
            "hi",
            "--json",
            *extra,
        ]
    )
    try:
        args.func(args)
    except Exception:
        pass
    return capsys.readouterr().out


def test_bare_persona_out_of_scope_siblings_are_not_counted(tmp_path, monkeypatch, capsys):
    # Two dev placements, both in OTHER workspaces than the active one. Only the
    # runtime-global canonical personainst_dev is in scope → single target → the
    # bare persona id resolves cleanly, no ambiguous_target.
    _seed_dev_scoped(
        monkeypatch,
        tmp_path,
        [("dev_far_1", "ws_far_1"), ("dev_far_2", "ws_far_2")],
        active="ws_home",
    )
    out = _run_bare_dev(capsys)
    assert "ambiguous_target" not in _emitted_error_kinds(out)


def test_bare_persona_two_in_scope_duplicates_still_refused_with_only_those(tmp_path, monkeypatch, capsys):
    # TWO dev placements in the active workspace + one in another workspace. Both
    # active-workspace placements are in scope and shadow the plumbing canonical
    # row → genuinely ambiguous → refused, and the out-of-scope placement is NOT a
    # candidate. Two PLACEMENTS (not canonical + one placement, which would
    # auto-route) are what keeps this ambiguous under "placements shadow canonical".
    _seed_dev_scoped(
        monkeypatch,
        tmp_path,
        [("dev_agent_2", "ws_home"), ("dev_agent_3", "ws_home"), ("dev_far", "ws_far")],
        active="ws_home",
    )
    out = _run_bare_dev(capsys)
    data = json.loads(out)
    assert data["error_kind"] == "ambiguous_target"
    handles = {c["persona_instance_id"] for c in data["candidates"]}
    assert handles == {"personainst_dev_agent_2", "personainst_dev_agent_3"}
    # The shadowed canonical row and the out-of-scope placement are both excluded.
    assert "personainst_dev" not in handles
    assert "personainst_dev_far" not in handles


def test_explicit_handle_targeting_out_of_scope_instance_is_allowed(tmp_path, monkeypatch, capsys):
    # Even with two in-scope duplicates (which make a BARE persona ambiguous),
    # naming an out-of-workspace instance by its explicit @handle is caller-
    # pinned and must never be refused for ambiguity.
    _seed_dev_scoped(
        monkeypatch,
        tmp_path,
        [("dev_agent_2", "ws_home"), ("dev_far", "ws_far")],
        active="ws_home",
    )
    out = _run_bare_dev(capsys, persona="personainst_dev_far")
    assert "ambiguous_target" not in _emitted_error_kinds(out)


def test_sender_session_scopes_candidates_to_sender_workspace(tmp_path, monkeypatch):
    # The sender's chat-root session identifies the sender's workspace: the same
    # seed resolves differently with vs without it. Unit-level so the sender
    # session can be threaded directly (the CLI arg path has no sender session).
    from hermes_cli.harness import _mission_chat_target_decision

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import WorkspaceStore

    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    cfg = load_agent_runtime_config()
    ws_store = WorkspaceStore()
    for workspace_id in ("ws_home", "ws_a", "ws_b"):
        ws_store.create(name=workspace_id, workspace_id=workspace_id)
    ws_store.set_active("ws_home")  # active is neither sender's ws nor a target's

    store = PersonaInstanceStore()
    store.ensure_for_personas(list(ensure_persisted_personas(cfg)))
    # TWO dev placements in ws_a (so the sender-scoped set stays ambiguous under
    # "placements shadow canonical"; canonical + one placement would auto-route)
    # plus one in ws_b that must never leak into ws_a's candidates.
    store.add_instance(persona_id="dev", placement_id="dev_ws_a_agent_2", workspace_id="ws_a", display_name="Dev A")
    store.add_instance(persona_id="dev", placement_id="dev_ws_a2_agent_2", workspace_id="ws_a", display_name="Dev A2")
    store.add_instance(persona_id="dev", placement_id="dev_ws_b_agent_2", workspace_id="ws_b", display_name="Dev B")
    # The sender lives in ws_a; its chat-root session id encodes its owner.
    sender = store.add_instance(
        persona_id="neko_supervisor", placement_id="sender_agent_2", workspace_id="ws_a", display_name="Sender"
    )
    sender_session = f"persona_chat_{sender.id}_{'a' * 12}"

    common = dict(
        instance_store=store,
        normalized_persona="dev",
        raw_persona_id="dev",
        persona_instance_id=None,
        session_id=None,
        relay_chain=(),
    )

    # No sender session → scope is the active workspace (ws_home): no ws_a/ws_b
    # placement is in scope, only the runtime-global canonical dev (no placement
    # shadows it here) → allowed.
    no_sender = _mission_chat_target_decision(**common, requested_by_session=None)
    assert no_sender.allowed is True

    # With the sender session → scope is the sender's workspace (ws_a): the two
    # ws_a placements are in scope and shadow the canonical dev → two candidates →
    # ambiguous, and the ws_b placement is excluded.
    scoped = _mission_chat_target_decision(**common, requested_by_session=sender_session)
    assert scoped.allowed is False
    assert scoped.error_kind == "ambiguous_target"
    handles = {c.instance_id for c in scoped.candidates}
    assert handles == {"personainst_dev_ws_a_agent_2", "personainst_dev_ws_a2_agent_2"}
    assert "personainst_dev" not in handles
    assert "personainst_dev_ws_b_agent_2" not in handles


# --------------------------------------------------------------------------- #
# Placements shadow canonical (the new contract)                              #
#                                                                              #
# When an in-scope PLACEMENT of a persona exists, its plumbing canonical row   #
# is shadowed: a bare persona id with exactly ONE in-scope placement is no     #
# longer ambiguous — it AUTO-ROUTES to that placement. With no in-scope        #
# placement the guard sees ZERO candidates (global canonicals are no longer    #
# advertised under a real scope) — still allowed, and the SEND falls back to   #
# the canonical channel until the global-row adoption migration retires it.    #
# --------------------------------------------------------------------------- #


def test_bare_persona_canonical_plus_one_in_scope_placement_auto_routes(tmp_path, monkeypatch):
    # Canonical dev + exactly one in-scope placement: the canonical is shadowed,
    # leaving one candidate, so the guard ALLOWS (evaluate_target auto-routes) and
    # the routing helper resolves the bare send onto the PLACEMENT, not canonical.
    from hermes_cli.harness import (
        _mission_chat_bare_persona_target,
        _mission_chat_target_decision,
    )

    store = _seed_dev_scoped(monkeypatch, tmp_path, [("dev_agent_2", "ws_home")], active="ws_home")
    decision = _mission_chat_target_decision(
        instance_store=store,
        normalized_persona="dev",
        raw_persona_id="dev",
        persona_instance_id=None,
        session_id=None,
        relay_chain=(),
        requested_by_session=None,
    )
    assert decision.allowed is True
    assert (
        _mission_chat_bare_persona_target(store, normalized_persona="dev", requested_by_session=None)
        == "personainst_dev_agent_2"
    )


def test_bare_persona_canonical_with_no_placement_routes_to_canonical(tmp_path, monkeypatch):
    # No in-scope placement (the only placement is in another workspace): the
    # canonical row stays addressable, the guard allows, and the routing helper
    # returns None so the caller falls back to the canonical channel.
    from hermes_cli.harness import (
        _mission_chat_bare_persona_target,
        _mission_chat_target_decision,
    )

    store = _seed_dev_scoped(monkeypatch, tmp_path, [("dev_far", "ws_far")], active="ws_home")
    decision = _mission_chat_target_decision(
        instance_store=store,
        normalized_persona="dev",
        raw_persona_id="dev",
        persona_instance_id=None,
        session_id=None,
        relay_chain=(),
        requested_by_session=None,
    )
    assert decision.allowed is True
    assert (
        _mission_chat_bare_persona_target(store, normalized_persona="dev", requested_by_session=None)
        is None
    )
