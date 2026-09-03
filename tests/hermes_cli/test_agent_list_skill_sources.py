"""`harness agent list --json` says WHICH tier answered `skills` (S0a A6c).

Plan: ``docs/agent-runtime-harness/planned/s0a-atlas-cleanup.md`` §2 A6c.

The folded queue row asked why a config-side ``skills:`` addition does not reach
a placement. The mechanism is the same store-wins merge as toolsets
(``ensure_persisted_personas`` merges ``{**catalog, **stored}``), but the answer
is NOT the same: skills have a store-writing verb with its own supersede clock
(``persona set-skills``) and the launcher's Skills console writes through it, so
for skills the store is the authority BY DESIGN. A config seed that won over it
would reintroduce the two-writer problem that clock exists to arbitrate.

What ships is therefore accounting, not a writer — and the accounting was worth
shipping on its own: run against the operator's live config on 2026-09-03, four
of five personas carried config ``skills:`` entries their store rows do not have
(``dev`` alone had six), silently, with no surface that said so.
"""

from __future__ import annotations

import argparse
import json

import pytest


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    from agent_runtime import paths

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents
    return root


def _seed_store_persona(persona_id: str, skills: list[str]):
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id=persona_id,
        display_name=persona_id,
        role="dev",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=[],
        system_prompt_path="",
        hermes_profile=None,
        skills=list(skills),
    )
    AgentStore().save(persona)
    return persona


def _config_with_skills(persona_id: str, skills: list[str]):
    import types

    return types.SimpleNamespace(
        personas={persona_id: {"role": "dev", "skills": list(skills)}},
        default_model=None,
        default_provider=None,
        default_api_mode=None,
    )


def _agent_list_rows() -> list[dict]:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(["harness", "agent", "list", "--json"])
    return args.func(args)


def test_a_store_backed_persona_reports_the_store_and_the_config_only_skills(monkeypatch):
    """The silent difference, made visible: the config declares three, the store
    row carries one, the placement gets the store's — and the row now says so
    instead of leaving an operator to diff two files."""

    from agent_runtime import config as config_module

    _seed_store_persona("dev", ["harness-dev-delivery"])
    cfg = _config_with_skills(
        "dev", ["harness-dev-delivery", "aaa-feature-delivery", "harness-handoff-recovery"]
    )

    rows = config_module.persona_skill_sources(cfg)

    assert rows["dev"]["skills_source"] == "store"
    assert rows["dev"]["catalog_only_skills"] == [
        "aaa-feature-delivery",
        "harness-handoff-recovery",
    ]


def test_a_config_only_persona_reports_the_catalog_and_no_difference():
    """ANTI-VACUITY: the second tier reports itself, and a persona whose config
    and effective lists agree reports an EMPTY difference rather than repeating
    its own skills."""

    from agent_runtime import config as config_module

    cfg = _config_with_skills("planner", ["harness-runtime-model"])

    rows = config_module.persona_skill_sources(cfg)

    assert rows["planner"]["skills_source"] == "catalog"
    assert rows["planner"]["catalog_only_skills"] == []


def test_the_accounting_writes_nothing(monkeypatch):
    """It is accounting. If it ever grows a write, the store's supersede clock
    has a second writer and this is where that shows up."""

    from agent_runtime import config as config_module
    from agent_runtime.store import AgentStore

    _seed_store_persona("dev", ["harness-dev-delivery"])
    monkeypatch.setattr(
        AgentStore, "save", lambda *a, **k: pytest.fail("skill accounting must not write")
    )

    config_module.persona_skill_sources(_config_with_skills("dev", ["aaa-feature-delivery"]))

    assert AgentStore().get("dev").skills == ["harness-dev-delivery"]


def test_the_cli_row_carries_both_keys(capsys):
    """Through the real argparse tree, because the launcher and the operator read
    the WIRE row, not the helper."""

    _seed_store_persona("dev", ["harness-dev-delivery"])

    assert _agent_list_rows() == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(item for item in payload["items"] if item["id"] == "dev")

    assert row["skills_source"] == "store"
    assert row["catalog_only_skills"] == []
    assert row["skills"] == ["harness-dev-delivery"]
