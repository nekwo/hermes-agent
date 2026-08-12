"""The ``resolution`` / ``chat_scope`` envelope blocks
(``agent_runtime/root_observability.py``) and the incident-shaped envelope pin.

The structural gate over the harness verbs lives at
``tests/hermes_cli/test_harness_json_root_observability.py``; these tests pin
the attach helper's own contract and the one envelope the 2026-08-12 incident
needed: an EMPTY chat read that says which root and which state.db answered.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_runtime.root_observability import attach_root_observability


pytestmark = pytest.mark.usefixtures("isolate_agent_runtime_root")


def _decode_stdout_envelopes(text: str) -> list[dict]:
    """emit_json pretty-prints; a line-wise json.loads finds ZERO envelopes and
    every assertion over the empty list passes. Scan with raw_decode."""

    decoder = json.JSONDecoder()
    envelopes = []
    index = 0
    while index < len(text):
        stripped = text[index:].lstrip()
        if not stripped:
            break
        offset = len(text) - len(stripped) - index
        try:
            payload, consumed = decoder.raw_decode(text, index + offset)
        except ValueError:
            index += offset + 1
            continue
        envelopes.append(payload)
        index = consumed
    return envelopes


def test_attach_stamps_resolution():
    payload = attach_root_observability({"ok": True})
    block = payload["resolution"]
    assert set(block) == {"store_root", "layer", "trace"}
    assert block["layer"] == "env"  # the isolation fixture pins the env rung


def test_attach_never_overwrites_a_producer_block():
    payload = attach_root_observability({"resolution": {"mine": True}})
    assert payload["resolution"] == {"mine": True}


def test_attach_stamps_chat_scope_on_request(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    payload = attach_root_observability({"ok": True}, chat_scope=True)
    scope = payload["chat_scope"]
    assert set(scope) == {
        "head_home",
        "db_path",
        "source",
        "authoritative",
        "explicitly_named",
    }
    assert scope["source"] == "ambient_home"
    assert scope["authoritative"] is False


def test_attach_reports_a_failed_resolution_as_typed_data(monkeypatch):
    import agent_runtime.resolution as resolution_module

    def boom(resolution=None):
        raise RuntimeError("resolver exploded")

    monkeypatch.setattr(resolution_module, "resolution_payload", boom)
    payload = attach_root_observability({"ok": True})
    assert payload["resolution"] == {
        "error_kind": "resolution_unavailable",
        "error": "RuntimeError",
    }


def test_attach_leaves_non_dict_payloads_alone():
    assert attach_root_observability(["not", "a", "dict"]) == ["not", "a", "dict"]


def test_empty_chat_history_envelope_now_carries_its_frame_of_reference(
    tmp_path, monkeypatch, capsys
):
    """THE incident pin. ``persona chat history --json`` for a session the
    resolved state.db does not hold returns the well-formed empty envelope
    (``ok: true, count: 0``) — which is exactly why it must now say WHICH db
    and WHICH root produced it, and that the head was only ambient."""

    import hermes_cli.harness as harness

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_HEAD_HOME", raising=False)
    args = SimpleNamespace(session_id="sess_not_here", json=True, limit=40, before=None)
    assert harness._cmd_persona_chat_history(args) == 0
    envelopes = _decode_stdout_envelopes(capsys.readouterr().out)
    assert len(envelopes) == 1, "expected exactly one --json envelope on stdout"
    envelope = envelopes[0]

    assert envelope["ok"] is True
    assert envelope["count"] == 0
    # The three facts the incident's envelope lacked entirely:
    assert envelope["resolution"]["store_root"]
    assert envelope["chat_scope"]["head_home"]
    assert envelope["chat_scope"]["source"] == "ambient_home"


def test_status_envelope_carries_resolution(capsys):
    import hermes_cli.harness as harness

    args = SimpleNamespace(json=True)
    assert harness._cmd_status(args) == 0
    envelopes = _decode_stdout_envelopes(capsys.readouterr().out)
    assert len(envelopes) == 1
    assert envelopes[0]["resolution"]["layer"] == "env"


def test_active_profile_name_fallback_uses_the_canonical_marker_reader(
    tmp_path, monkeypatch
):
    """Slice 3: the sticky-marker fallback must delegate to
    ``hermes_cli.profiles.get_active_profile`` (which resolves the platform
    default via ``get_default_hermes_root``), never a hand-spelled
    ``Path.home()/.hermes/active_profile`` — the spelling that made
    ``agents --json``'s source_profile lie on native Windows."""

    import agent_runtime.profile_context as profile_context

    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    # A HERMES_HOME that is NOT profile-shaped, so the fallback arm runs.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "plain-home"))
    monkeypatch.setattr(profile_context, "get_active_profile", lambda: "sticky_pick")
    assert profile_context.active_profile_name() == "sticky_pick"
