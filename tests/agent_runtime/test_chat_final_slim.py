"""C3 — pure unit coverage for the ``chat.final`` slim observability projection.

``slim_chat_final_observability`` is the ONE shape the terminal frame embeds
(ruling §7.3). It lives in ``agent_runtime.prompt_observability`` (importable)
because ``persona_commands.py`` is exec'd into harness globals; these tests pin
the projection without driving the CLI. The harness-driven emission-shape guard
(the sabotage anchor for the wire) lives in ``test_persona_assignments.py``.
"""

from __future__ import annotations

import json

from agent_runtime.prompt_observability import (
    CHAT_FINAL_OBSERVABILITY_FIELDS,
    slim_chat_final_observability,
)


def _fat_row() -> dict:
    """A representative built observability row — the slim fields plus the heavy
    record-at-injection payloads that must NOT ride the terminal frame."""

    return {
        "context_id": "ctx_abc123",
        "chat_id": "persona_chat_x",
        "chat_title": "Stage 48 briefing",
        "turn_usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        "model_selection": {"effective_model": "gpt-test", "effective_provider": "p"},
        "context_budget": {"assembled_tokens": 1234, "basis": "metered_first_call"},
        "situational_hud": {"runtime": {"scope": "x"}, "mission_hud": {"plan": []}},
        "situational_hud_revision": "hud_0123456789abcdef",
        "situational_hud_delivery": "snapshot",
        "used_skills": [{"name": "deep-audit", "kind": "skill", "status": "used"}],
        # Heavy payloads that must be projected out:
        "final_model_input": {"messages": [{"role": "system", "content": "x" * 5000}]},
        "prompt_layers": [{"name": "Persona identity"}] * 6,
        "context_files": [{"path": "MEMORY.md"}],
        "chat_history_context": [{"role": "user", "content": "prior turn"}],
        "accessible_skills": [{"name": "a"}],
        "available_skills": [{"name": "a"}, {"name": "b"}],
        "accessible_skills_ref": "deadbeef",
        "available_skills_ref": "feedface",
        "prompt_flags": {"skip_memory": True},
        "redaction": {"status": "safe"},
    }


def test_slim_keeps_exactly_the_ruling_field_set():
    slim = slim_chat_final_observability(_fat_row())
    assert set(slim) == set(CHAT_FINAL_OBSERVABILITY_FIELDS)


def test_slim_preserves_kept_field_values():
    row = _fat_row()
    slim = slim_chat_final_observability(row)
    for key in CHAT_FINAL_OBSERVABILITY_FIELDS:
        assert slim[key] == row[key], key


def test_slim_drops_every_heavy_payload():
    slim = slim_chat_final_observability(_fat_row())
    for dropped in (
        "final_model_input",
        "prompt_layers",
        "context_files",
        "chat_history_context",
        "accessible_skills",
        "available_skills",
        "accessible_skills_ref",
        "available_skills_ref",
        "prompt_flags",
        "redaction",
    ):
        assert dropped not in slim, dropped


def test_slim_is_a_fraction_of_the_row_bytes():
    row = _fat_row()
    row_bytes = len(json.dumps(row, separators=(",", ":")))
    slim_bytes = len(json.dumps(slim_chat_final_observability(row), separators=(",", ":")))
    assert slim_bytes < row_bytes // 4


def test_slim_never_mutates_the_source_row():
    row = _fat_row()
    before = json.dumps(row, sort_keys=True)
    slim_chat_final_observability(row)
    assert json.dumps(row, sort_keys=True) == before


def test_slim_coerces_collection_fields_to_typed_empty():
    # A partial row (e.g. a failure-lane pre-turn build) still yields the ONE
    # shape — collections keep their empty map/list so the launcher never
    # decodes a null where it expects a container.
    slim = slim_chat_final_observability({"context_id": "ctx_1"})
    assert slim["situational_hud"] == {}
    assert slim["used_skills"] == []
    assert slim["model_selection"] == {}
    assert slim["turn_usage"] is None
    assert slim["context_budget"] is None
    assert slim["chat_id"] is None
    assert slim["chat_title"] is None
    assert slim["context_id"] == "ctx_1"


def test_slim_non_dict_input_is_empty():
    assert slim_chat_final_observability(None) == {}
    assert slim_chat_final_observability("nope") == {}
    assert slim_chat_final_observability(["list"]) == {}
