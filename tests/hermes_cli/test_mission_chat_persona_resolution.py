"""Persona identity resolution at the mission-chat CLI boundary.

Mission Control payloads, legacy SessionDB rows, and agent tool calls leak
persona INSTANCE ids (``personainst_*``) into persona-id slots. The CLI
boundary resolves them at the single persona-id chokepoint instead of failing
the send with an uncaught ``unsupported persona`` ValueError (the Launcher
surfaced that as ``code=invalid_request`` with no actionable detail).

The identity has TWO halves and the boundary owns both: the persona id (this
file's first half) and the persona INSTANCE the turn binds (the second half).
They must agree — the instance that resolves the root/mint is the instance
``open_chat`` binds — or the sibling-steal guard correctly refuses the pair's
own freshly minted session.
"""

import argparse
import json
import uuid

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


def test_unknown_data_declared_id_survives_normalization(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    assert (
        harness._normalize_cli_persona_or_template_id("definitely_not_a_persona_xyz")
        == "definitely_not_a_persona_xyz"
    )


def test_safe_data_declared_persona_precedes_instance_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))
    resolved = harness._resolve_mission_chat_persona_id(
        "Some Display Name", "personainst_dev"
    )
    assert resolved == "Some_Display_Name"


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


# --------------------------------------------------------------------------- #
# Mint/bind instance agreement                                                 #
#                                                                              #
# Live 2026-07-27, every --new-session dispatch to a persona with an in-scope  #
# placement: the omitted-session branch resolved the PLACEMENT (caller pin or  #
# "placements shadow canonical"), minted that placement's root — then handed   #
# ``open_chat`` the RAW pin, which was ``None``, so the bind fell back to the  #
# canonical channel and the sibling-steal guard refused the session the same   #
# turn had just minted:                                                        #
#                                                                              #
#   chat session 'persona_chat_personainst_qa_agent_f24601ba_...' belongs to   #
#   instance 'personainst_qa_agent_f24601ba'; it cannot be bound onto          #
#   'personainst_qa'                                                           #
#                                                                              #
# The guard is right; the two identities were not one decision. These pin the  #
# agreement (and that the guard still refuses a genuine sibling steal).        #
# --------------------------------------------------------------------------- #


class _StopAfterBind(Exception):
    """Sentinel: end the turn right after the chat bind, before model plumbing."""


def _seed_qa_placement(monkeypatch, tmp_path, placement_id="qa_agent_f24601ba"):
    """Fresh runtime root: canonical ``personainst_qa`` + one in-scope placement.

    Exactly the live shape — a deliberate placement on the level alongside the
    plumbing canonical row, which "placements shadow canonical" routes to.
    """
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    from agent_runtime.config import ensure_persisted_personas, load_agent_runtime_config
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore
    from agent_runtime.worker_sessions import WorkerSessionStore
    from tests.agent_runtime.persona_samples import sample_persona

    cfg = load_agent_runtime_config()
    AgentStore().save(sample_persona("qa"))
    store = PersonaInstanceStore()
    store.derive_from_workers(list(ensure_persisted_personas(cfg)), WorkerSessionStore().list_all())
    placement = store.add_instance(
        persona_id="qa", placement_id=placement_id, display_name="QA Agent (2)"
    )
    return store, placement.id


def _send_capturing_bind(monkeypatch, capsys, *extra):
    """Drive one ``mission-chat message`` and capture the chat BIND.

    The spy calls the REAL ``open_chat`` — the sibling-steal guard has to
    genuinely evaluate, since it is the thing that refused live — and then ends
    the turn before the model plumbing runs. ``PersonaChatMintReceiptStore.mint``
    also binds through ``open_chat`` (that is the mint chokepoint, one write
    path); only the SEND's bind carries ``default_display_name``, so that is the
    call the sentinel ends on.
    """
    from agent_runtime.persona_assignments import PersonaInstanceStore

    captured: dict = {"binds": []}
    real_open_chat = PersonaInstanceStore.open_chat

    def _spy(self, **kwargs):
        instance = real_open_chat(self, **kwargs)
        captured["binds"].append((kwargs.get("persona_instance_id"), instance.id))
        if "default_display_name" in kwargs:
            captured["bound_instance_id"] = instance.id
            captured["bound_session_id"] = kwargs.get("session_id")
            raise _StopAfterBind
        return instance

    monkeypatch.setattr(PersonaInstanceStore, "open_chat", _spy)
    args = parser().parse_args(
        ["harness", "mission-chat", "message", "--message", "hi", "--json", *extra]
    )
    try:
        captured["exit_code"] = args.func(args)
    except _StopAfterBind:
        captured["exit_code"] = None
    captured["out"] = capsys.readouterr().out
    return captured


def _assert_bound_the_session_owner(captured):
    """The bound instance IS the minted session's owner — one identity."""
    from agent_runtime.persona_assignments import chat_session_owner_instance_id

    assert "cannot be bound onto" not in captured["out"], captured["out"]
    assert captured["exit_code"] is None, captured["out"]
    assert (
        chat_session_owner_instance_id(captured["bound_session_id"])
        == captured["bound_instance_id"]
    )


def test_new_session_to_instance_handle_binds_that_instance(tmp_path, monkeypatch, capsys):
    # The live repro: an instance-shaped --persona is an explicit pin. It must
    # survive persona-id canonicalization and be the instance that BINDS.
    _, placement_id = _seed_qa_placement(monkeypatch, tmp_path)
    captured = _send_capturing_bind(
        monkeypatch, capsys, "--persona", placement_id, "--new-session", "--title", "w5 proof"
    )
    _assert_bound_the_session_owner(captured)
    assert captured["bound_instance_id"] == placement_id


def test_new_session_to_bare_persona_binds_the_shadowing_placement(tmp_path, monkeypatch, capsys):
    # No pin: "placements shadow canonical" picks the single in-scope placement
    # for the mint, so the bind must follow it there rather than to canonical.
    _, placement_id = _seed_qa_placement(monkeypatch, tmp_path)
    captured = _send_capturing_bind(
        monkeypatch, capsys, "--persona", "qa", "--new-session", "--title", "w5 proof"
    )
    _assert_bound_the_session_owner(captured)
    assert captured["bound_instance_id"] == placement_id


def test_omitted_session_continues_the_placement_owned_root(tmp_path, monkeypatch, capsys):
    # The continue lane: the placement already owns a default chat root, so the
    # omitted-session send resolves THAT root and must bind the placement.
    store, placement_id = _seed_qa_placement(monkeypatch, tmp_path)
    existing_root = f"persona_chat_{placement_id}_{uuid.uuid4().hex[:12]}"
    store.open_chat(persona_id="qa", persona_instance_id=placement_id, session_id=existing_root)

    captured = _send_capturing_bind(monkeypatch, capsys, "--persona", "qa")
    _assert_bound_the_session_owner(captured)
    assert captured["bound_instance_id"] == placement_id
    assert captured["bound_session_id"] == existing_root


def test_explicit_sibling_owned_session_is_still_refused(tmp_path, monkeypatch, capsys):
    # The guard stays intact: an explicit --session-id minted for a SIBLING is
    # never adopted by the target instance, and nothing binds.
    store, placement_id = _seed_qa_placement(monkeypatch, tmp_path)
    sibling = store.add_instance(
        persona_id="qa", placement_id="qa_agent_sibling", display_name="QA Agent (3)"
    )
    sibling_session = f"persona_chat_{sibling.id}_{uuid.uuid4().hex[:12]}"

    captured = _send_capturing_bind(
        monkeypatch,
        capsys,
        "--persona",
        placement_id,
        "--session-id",
        sibling_session,
    )
    assert captured["exit_code"] == 2
    assert "bound_instance_id" not in captured
    data = json.loads(captured["out"])
    assert data["ok"] is False
    assert data["execution_state"] == "rejected"
