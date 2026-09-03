"""``persona tool-diff`` reports WHERE the capability came from (S0a A2).

Plan: ``docs/agent-runtime-harness/planned/s0a-atlas-cleanup.md`` §2 A2.

The defect this closes is an accounting one. Three copies of a per-persona
``toolsets`` list exist (profile config, store row, realm-sync body) and since
S0a A1 none of them admits anything — the harness lane reads the bound profile's
own ``toolsets:`` key. A preview that printed the persona list (and nothing about
the declaration) let an operator read a capability set no turn had ever run with,
with no way to tell a declaration from a default.

Every case drives the REAL argparse tree through ``args.func``, because the wire
row and the text line are both operator-facing surfaces and a handler poked
directly would not prove the verb still spells its positional argument.
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


@pytest.fixture
def bound_profile_home():
    """Provision the profile home the seeded persona is bound to.

    Local rather than imported: ``tests/agent_runtime/conftest.py``'s
    ``bundled_persona_profiles`` does not reach this package, and the
    declaration reader resolves the PERSONA's bound profile — an absent home
    would resolve ``profile_unresolved`` and this file would be asserting the
    fallback instead of the read.
    """

    from hermes_cli.profiles import get_profile_dir

    home = get_profile_dir("gpt-launcher")
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("agent:\n  model: gpt-5.5\n", encoding="utf-8")
    from agent_runtime.parse_cache import clear_parse_cache

    clear_parse_cache()
    return home


def _seed_persona(persona_id: str = "dev", *, toolsets: list[str] | None = None):
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id=persona_id,
        display_name="Launcher Dev Agent",
        role="dev",
        model=None,
        provider=None,
        api_mode="codex_responses",
        toolsets=list(toolsets if toolsets is not None else ["kanban", "messaging"]),
        system_prompt_path="",
        hermes_profile="gpt-launcher",
    )
    AgentStore().save(persona)
    return persona


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _tool_diff(persona_id: str, *flags: str) -> int:
    return _dispatch(["harness", "persona", "tool-diff", persona_id, *flags])


def test_the_json_row_carries_the_declaration_and_marks_the_legacy_list_inert(
    bound_profile_home, capsys
):
    _seed_persona()

    assert _tool_diff("dev", "--json") == 0
    payload = json.loads(capsys.readouterr().out)["tool_visibility"]

    declaration = payload["toolset_declaration"]
    assert declaration["declared"] == ["harness_core"]
    assert declaration["source"] in {"lane_default", "profile_config"}
    # The stale store list travels — labelled, in one object, beside what is
    # actually in force. Visible, not obeyed.
    assert declaration["persona_list"] == ["kanban", "messaging"]
    assert payload["persona_toolsets"] == ["kanban", "messaging"]
    assert payload["persona_toolsets_in_force"] is False
    # ... and none of it reached the resolved set.
    assert "kanban" not in payload["effective_toolsets"]
    assert payload["effective_toolsets"] == declaration["toolsets"]


def test_the_text_mode_names_the_source_and_the_ignored_list(
    bound_profile_home, capsys
):
    """The operator's actual read. Without these two lines the only observable
    difference between "the profile declares harness_core" and "this profile
    declares nothing and the lane defaulted" is invisible."""

    _seed_persona()

    assert _tool_diff("dev") == 0
    out = capsys.readouterr().out

    # 43 -> 44: S2b registered ``agent_chat_installs`` into the ``agent_chat``
    # toolset, which ``harness_core`` includes by NAME. Re-measured with the S0a
    # ratchet in the same wave (``test_harness_core_ratchet.py``), never adjusted
    # to make a red go green.
    assert "dev: 44 tools" in out
    assert "toolsets: harness_core (" in out
    assert "persona-level toolsets list ignored (legacy" in out
    assert "kanban" in out


def test_a_persona_with_no_legacy_list_prints_no_ignore_line(
    bound_profile_home, capsys
):
    """ANTI-VACUITY for the line above: it is conditional on a real divergence,
    not printed for every persona."""

    _seed_persona(toolsets=[])

    assert _tool_diff("dev") == 0
    out = capsys.readouterr().out

    assert "toolsets: harness_core (" in out
    assert "persona-level toolsets list ignored" not in out


def test_the_verb_still_requires_its_positional_persona_id():
    """The manual's View row used to say ``persona tool-diff --json`` with no id
    (S0a §0.4) — a documented command that exits 2. A4 fixes the row; this pins
    the fact the row has to respect."""

    with pytest.raises(SystemExit):
        _dispatch(["harness", "persona", "tool-diff", "--json"])
