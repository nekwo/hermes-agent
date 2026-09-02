"""The persona CLI replies stop carrying two constants nobody read.

``harness persona list`` / ``show`` / ``assignments`` emitted
``feature_enabled: true`` and ``assignment_store_enabled: true`` — literal
``True`` in the dict, never derived from anything. S56 kept them on the reply
"so operator tooling that reads them keeps parsing"; the 2026-09-01 trace found
no such tooling:

* in this repo the only other ``feature_enabled`` belongs to
  ``tools/skills_sync_client.py``, a different key on a different reply, read by
  ``hermes_cli/main.py``'s ``sync status`` arm;
* in the launcher, ``lib/`` names neither key, and the CLI contract dump
  (``test/features/mission_control/fixtures/hermes_cli_contract.json``) covers
  ARGV only, so no launcher gate is bound to this reply's shape.

It is the same always-true shape AX2 took off the snapshot wire in ``s76``, and
this file follows ``test_s76_assignment_wire_prune``'s form for the same stated
reason: **a wire-key cut may not be pinned by a CODE row.** A name scan asserts
that a SPELLING is absent from source, which a retirement comment can satisfy
and a respelling can walk through — and here it would false-positive on
skills-sync's honest key. So the replies are BUILT and read back.

What is deliberately still asserted present is what a caller actually branches
on: ``ok`` on both ``show`` arms, and the rows themselves. This cut removes
constants, not the reply.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli.harness import build_parser


def parser() -> argparse.ArgumentParser:
    """The harness subparser tree, built the way every test here builds it."""

    p = argparse.ArgumentParser()
    subs = p.add_subparsers(dest="command")
    build_parser(subs)
    return p

#: Every key this cut removed. Both are checked against every reply, including
#: the ones that never carried the second — asking anyway is what stops a later
#: edit from adding it back "because the other command has one".
CUT_KEYS = ("feature_enabled", "assignment_store_enabled")


@pytest.fixture
def persona_home(tmp_path, monkeypatch):
    """One persona in a hermetic store, with a real profile home behind it."""

    from agent_runtime.models import AgentPersona
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore

    home = tmp_path / "hermes-home"
    (home / "profiles" / "alpha").mkdir(parents=True, exist_ok=True)
    (home / "profiles" / "alpha" / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(tmp_path / "runtime"))

    persona = AgentStore().save(
        AgentPersona(
            id="widget",
            display_name="Widget Agent",
            role="dev",
            model=None,
            provider=None,
            api_mode="codex_responses",
            toolsets=["file"],
            system_prompt_path="",
            hermes_profile="alpha",
        )
    )
    PersonaInstanceStore().ensure_for_persona(persona)
    return persona


def _reply(argv: list[str], capsys) -> dict:
    args = parser().parse_args(argv)
    code = args.func(args)
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
    return {"code": code, "data": payload}


def test_persona_list_carries_the_roster_and_neither_constant(persona_home, capsys):
    out = _reply(["harness", "persona", "list", "--json"], capsys)

    assert out["code"] == 0
    # The reply still does its job: the roster is there and is not empty.
    ids = [row["persona_instance_id"] for row in out["data"]["persona_instances"]]
    assert "personainst_widget" in ids
    for key in CUT_KEYS:
        assert key not in out["data"]


def test_persona_show_carries_ok_and_neither_constant(persona_home, capsys):
    out = _reply(["harness", "persona", "show", "widget", "--json"], capsys)

    assert out["code"] == 0
    assert out["data"]["ok"] is True
    assert out["data"]["persona_instance"]["persona_instance_id"] == "personainst_widget"
    for key in CUT_KEYS:
        assert key not in out["data"]


def test_the_persona_show_not_found_arm_keeps_ok_false_and_drops_the_constant(
    persona_home, capsys
):
    """The refusal arm carried ``feature_enabled`` too, and it is the arm most
    likely to be re-grown by hand — a caller branching on a failure reply reads
    ``ok`` and ``error``, which is exactly what is left."""

    out = _reply(["harness", "persona", "show", "definitely-missing", "--json"], capsys)

    assert out["code"] == 2
    assert out["data"]["ok"] is False
    assert "definitely-missing" in out["data"]["error"]
    for key in CUT_KEYS:
        assert key not in out["data"]


def test_persona_assignments_carries_the_rows_and_neither_constant(persona_home, capsys):
    """The store is empty on a fresh hermetic root, and that is the honest
    shape to assert: ``assignments`` is present and is a list. The cut is about
    the two constants beside it, not about the rows."""

    out = _reply(["harness", "persona", "assignments", "--json"], capsys)

    assert out["code"] == 0
    assert out["data"]["ok"] is True
    assert isinstance(out["data"]["assignments"], list)
    for key in CUT_KEYS:
        assert key not in out["data"]
