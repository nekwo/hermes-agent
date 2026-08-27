"""``persona instance update-profile`` must not clear skills nobody mentioned.

RED-FIRST, and it has to be: at HEAD before plan S4 the first test below FAILS.
``_cmd_persona_instance_update_profile`` passed
``skills=list(getattr(args, "skills", None) or [])``, so an omitted ``--skill``
reached ``PersonaInstanceStore.update_profile`` as an empty LIST, and the store's
own contract — ``if skills is not None or clear_skills:`` — correctly read that
as "the caller sent a list, write it" and set ``skill_overrides = []``.

The consequence was silent and total: renaming an agent
(``update-profile <id> --display-name X``) cleared every skill it was assigned,
and nothing said so. The store was never wrong. The collapse was in the command
layer, whose entire job at that line is to translate "absent" into "absent".

Every test drives the REAL argparse tree through ``args.func``. A handler poked
directly would not prove that the flag's ``default=None`` and the handler's
reading of it agree, and the two halves of that agreement are the bug.
"""

from __future__ import annotations

import argparse
import json

import pytest


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root inside this test's tmp dir, and prove it landed."""

    from agent_runtime import paths

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "write into a runtime root nobody in this repo controls."
    )
    return root


@pytest.fixture
def instance_with_two_overrides():
    """A live instance holding TWO skill overrides, written through the store.

    Written through ``update_profile`` rather than by poking the dataclass: the
    property under test is what a second ``update_profile`` does to what a first
    one wrote, so both writes go through the real chokepoint.
    """

    from agent_runtime.models import AgentPersona
    from agent_runtime.persona_assignments import PersonaInstanceStore
    from agent_runtime.store import AgentStore

    AgentStore().save(
        AgentPersona(
            id="qa",
            display_name="QA Agent",
            role="qa",
            model=None,
            provider=None,
            api_mode=None,
            toolsets=[],
            system_prompt_path="",
            skills=["harness-qa-verdict"],
        )
    )
    store = PersonaInstanceStore()
    instance = store.add_instance(
        persona_id="qa",
        placement_id="qa_upskills_agent_2",
        display_name="QA Agent",
        default_display_name="QA Agent",
        workspace_id="ws_upskills",
    )
    store.update_profile(
        instance.id, skills=["harness-qa-verdict", "harness-continuity"]
    )
    assert store.get(instance.id).skill_overrides == [
        "harness-qa-verdict",
        "harness-continuity",
    ]
    return instance.id


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _overrides(instance_id: str):
    from agent_runtime.persona_assignments import PersonaInstanceStore

    return PersonaInstanceStore().get(instance_id).skill_overrides


def test_a_display_name_edit_leaves_both_skill_overrides_in_place(
    instance_with_two_overrides, capsys
):
    """THE red-first pin. KILLING MUTATION: restore ``or []`` in
    ``_cmd_persona_instance_update_profile`` and this reds.

    ANTI-VACUITY. The probe is the STORE's list read back after the command, not
    the command's own reply — a handler that printed the old skills while
    writing ``[]`` would still fail here. And the rename is asserted to have
    LANDED, so a mutant that made the whole call a no-op (which would also leave
    the overrides alone) fails on the display name instead of passing on the
    skills.
    """

    code = _dispatch(
        [
            "harness", "persona", "instance", "update-profile",
            instance_with_two_overrides,
            "--display-name", "QA Agent Renamed",
            "--json",
        ]
    )
    data = json.loads(capsys.readouterr().out)

    assert code == 0
    assert data["ok"] is True
    # The edit the operator ASKED for happened...
    from agent_runtime.persona_assignments import PersonaInstanceStore

    assert (
        PersonaInstanceStore().get(instance_with_two_overrides).display_name
        == "QA Agent Renamed"
    )
    # ...and the one nobody asked for did not.
    assert _overrides(instance_with_two_overrides) == [
        "harness-qa-verdict",
        "harness-continuity",
    ]


def test_an_edit_that_touches_nothing_skill_shaped_still_leaves_them(
    instance_with_two_overrides, capsys
):
    """The same claim through a DIFFERENT field, so the fix cannot be a special
    case for ``--display-name``.

    ``--goal`` and ``--current-chat-goal`` reach the same store call through the
    same handler; if the collapse were re-introduced for any of them it would be
    re-introduced for all of them, because there is one expression.
    """

    code = _dispatch(
        [
            "harness", "persona", "instance", "update-profile",
            instance_with_two_overrides,
            "--current-chat-goal", "ship the slice",
            "--json",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert _overrides(instance_with_two_overrides) == [
        "harness-qa-verdict",
        "harness-continuity",
    ]


def test_the_flag_still_REPLACES_when_it_is_given(
    instance_with_two_overrides, capsys
):
    """The fix must not turn the flag off.

    ANTI-VACUITY for the pin above: a "fix" that stopped passing ``skills`` at
    all would satisfy every assertion in the two tests above and break the verb.
    ``skill_overrides`` is a REPLACE, not a merge (``models.
    apply_instance_model_overrides`` substitutes the list wholesale), so the
    dropped id must be gone.
    """

    code = _dispatch(
        [
            "harness", "persona", "instance", "update-profile",
            instance_with_two_overrides,
            "--skill", "harness-continuity",
            "--json",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert _overrides(instance_with_two_overrides) == ["harness-continuity"]


def test_clear_skills_still_clears(instance_with_two_overrides, capsys):
    """The DELIBERATE clear keeps working, and it is the only door to it.

    This is what makes the pin above a bug fix rather than a capability
    removal: an operator who means "no skills" has a flag that says so, and the
    absent flag stops being a second, silent spelling of it.
    """

    code = _dispatch(
        [
            "harness", "persona", "instance", "update-profile",
            instance_with_two_overrides,
            "--clear-skills",
            "--json",
        ]
    )
    capsys.readouterr()

    assert code == 0
    assert _overrides(instance_with_two_overrides) == []
