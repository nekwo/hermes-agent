"""``harness agent retire`` — the operator's serve-absent inverse of the create.

Every test drives the REAL argparse tree and dispatches through ``args.func``,
never by poking the handler: a handler nothing routes to is a verb no operator
can run, and this program has been bitten by exactly that before.

The claim the suite exists for, beyond "it retires": the OTHER door —
``harness persona instance retire`` — is the same function, so a scripted
operator gets the same ack whichever verb they typed. That is asserted by
running both and comparing the payloads, not by reading the two handlers.

Nothing here spawns a ``harness serve``. As with the create, the claim is
precisely that none is needed.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent_runtime import paths
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_agent_retire_verb"


@pytest.fixture(autouse=True)
def hermetic_runtime_root(tmp_path, monkeypatch):
    """Pin the runtime root INSIDE this test's tmp dir, and prove it landed.

    The same guard the create verb's suite carries, for the same reason: these
    tests archive real rows, and a resolution regression would archive the
    OPERATOR's.
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    resolved = paths.store_root().resolve()
    assert resolved == root.resolve() or root.resolve() in resolved.parents, (
        f"store_root() resolved to {resolved}, OUTSIDE {root}: this test would "
        "write into a runtime root nobody in this repo controls."
    )
    return root


@pytest.fixture
def qa_persona():
    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    persona = AgentPersona(
        id="qa",
        display_name="QA Agent",
        role="qa",
        model=None,
        provider=None,
        api_mode=None,
        toolsets=[],
        system_prompt_path="",
    )
    AgentStore().save(persona)
    return persona


@pytest.fixture
def seeded_workspace():
    from agent_runtime.office_store import OfficeStore

    seed_workspace_record(WORKSPACE)
    store = OfficeStore()
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


def _dispatch(argv: list[str]) -> int:
    from hermes_cli import harness

    root = argparse.ArgumentParser(prog="hermes")
    harness.build_parser(root.add_subparsers(dest="command"))
    args = root.parse_args(argv)
    return args.func(args)


def _place(capsys, placement_id: str = "qa_verb_retire_1") -> dict:
    code = _dispatch(
        [
            "harness", "agent", "create",
            "--persona", "qa",
            "--workspace", WORKSPACE,
            "--pos", "2", "2",
            "--placement-id", placement_id,
            "--idempotency-key", f"verb-retire-{placement_id}",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    return payload


def _retire(capsys, instance_id: str, *extra: str) -> tuple[int, dict]:
    code = _dispatch(["harness", "agent", "retire", instance_id, "--json", *extra])
    return code, json.loads(capsys.readouterr().out)


def _live_actor_keys() -> set:
    from agent_runtime.office_store import OfficeStore

    return {actor.actor_key for actor in OfficeStore().list_actors(WORKSPACE)}


# ── the verb retires a placement ─────────────────────────────────────────────


def test_the_verb_archives_both_halves_and_names_the_actors(
    qa_persona, seeded_workspace, capsys
):
    """ANTI-VACUITY: the probe is the OFFICE store, not the ack.

    The kill-mutation is "route the verb at the roster archive alone" — which is
    what the launcher's two-lane removal degenerated to whenever its second lane
    failed. That mutant returns a perfectly plausible ack with the desk still on
    the canvas, so the desk is what gets read.
    """

    placed = _place(capsys)
    assert placed["actor_key"] in _live_actor_keys()

    code, data = _retire(capsys, placed["persona_instance_id"])

    assert code == 0
    assert data["ok"] is True
    assert data["archived_actor_keys"] == [placed["actor_key"]]
    assert data["office_archive_failures"] == []
    assert data["already_retired"] is False
    assert placed["actor_key"] not in _live_actor_keys()
    assert not paths.persona_instance_path(placed["persona_instance_id"]).exists()


def test_a_second_retire_is_answered_not_refused(qa_persona, seeded_workspace, capsys):
    """KILLING MUTATION: drop the tombstone probe in the service's ``not_found``
    arm — the exit code becomes 3 and this reds on ``code == 0``.

    An operator who runs the verb twice (or a cron that does) must not have to
    tell "already done" apart from "wrong id" by reading prose.
    """

    placed = _place(capsys, placement_id="qa_verb_retire_2")
    _retire(capsys, placed["persona_instance_id"])

    code, data = _retire(capsys, placed["persona_instance_id"])

    assert code == 0
    assert data["already_retired"] is True
    assert data["archived_actor_keys"] == [placed["actor_key"]]


def test_an_unknown_id_still_exits_not_found(qa_persona, capsys):
    """ANTI-VACUITY for the replay above. ``already_retired`` reads a TOMBSTONE;
    an id that never existed has none, and must keep its own exit code (3, the
    create verb's ``4001`` family) so a script can branch on it.
    """

    code, data = _retire(capsys, "personainst_never_existed")

    assert code == 3
    assert data["ok"] is False
    assert data["reason"] == "not_found"


def test_a_guard_refusal_exits_conflict_and_names_its_reason(qa_persona, capsys):
    from agent_runtime.persona_assignments import PersonaInstanceStore

    canonical = PersonaInstanceStore().ensure_for_persona(qa_persona)

    code, data = _retire(capsys, canonical.id)

    assert code == 4
    assert data["reason"] == "canonical_persona_channel"
    assert paths.persona_instance_path(canonical.id).exists()


# ── the two doors are one function ───────────────────────────────────────────


def test_persona_instance_retire_produces_the_identical_ack(
    qa_persona, seeded_workspace, capsys
):
    """KILLING MUTATION: point ``persona instance retire`` back at
    ``PersonaInstanceStore.retire`` directly — its payload loses
    ``archived_actor_keys`` / ``office_archive_failures`` / ``already_retired``
    and the key-set comparison reds.

    Two placements are used rather than one because a retire is not repeatable
    against the same row; the comparison is therefore of the ack's SHAPE and of
    every field that is not the row's own identity, which is exactly what "the
    same ack" can mean across two different targets.
    """

    first = _place(capsys, placement_id="qa_verb_retire_3")
    second = _place(capsys, placement_id="qa_verb_retire_4")

    _, agent_door = _retire(capsys, first["persona_instance_id"])

    code = _dispatch(
        [
            "harness", "persona", "instance", "retire",
            second["persona_instance_id"],
            "--json",
        ]
    )
    instance_door = json.loads(capsys.readouterr().out)
    assert code == 0

    ack = instance_door["persona_instance_retired"]
    # The envelope each verb has always had differs; the ACK does not.
    assert set(ack) == set(agent_door) - {"ok", "resolution"}
    assert ack["archived_actor_keys"] == [second["actor_key"]]
    assert ack["office_archive_failures"] == []
    assert ack["already_retired"] is False
    assert ack["persona_id"] == agent_door["persona_id"] == "qa"


def test_the_instance_door_refuses_with_the_services_typed_reason(
    qa_persona, seeded_workspace, capsys
):
    """The refusal is the SERVICE's, translated — not a second ``except`` over
    the same store guard. ``persona instance retire`` keeps its own historical
    spelling (``error``/``code`` carry the reason word, exit 2), and the reason
    word itself comes from the one place that maps the guard.
    """

    code = _dispatch(
        ["harness", "persona", "instance", "retire", "personainst_nope", "--json"]
    )
    data = json.loads(capsys.readouterr().out)

    assert code == 2
    assert data["ok"] is False
    assert data["error"] == "not_found"
    assert data["code"] == "not_found"


# ── the operator surface ─────────────────────────────────────────────────────


def test_the_human_readable_line_names_what_left_the_canvas(
    qa_persona, seeded_workspace, capsys
):
    """Without ``--json`` the verb still has to answer the question it exists for
    — which desks went — rather than printing an id and leaving the operator to
    go look.
    """

    placed = _place(capsys, placement_id="qa_verb_retire_5")

    code = _dispatch(["harness", "agent", "retire", placed["persona_instance_id"]])
    out = capsys.readouterr().out

    assert code == 0
    assert placed["persona_instance_id"] in out
    assert placed["actor_key"] in out
