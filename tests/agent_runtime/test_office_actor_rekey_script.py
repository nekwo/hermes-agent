"""The office class-key → instance-key re-key migration
(``scripts/office_actor_rekey_to_instance.py``).

An operational script whose ``--apply`` path is never exercised is a script that
gets run for the first time against production. These tests run it BOTH ways
against the hermetic per-test runtime root, and pin the two properties that
matter more than the happy path:

1. **Dry run writes nothing.** Asserted by content equality of the whole office
   tree before and after, not by "it printed DRY RUN".
2. **It refuses rather than guesses.** The derivation (agent item id → canonical
   instance id → an actual ``persona_instances/*.json``) has three places it can
   fail, and every one of them must produce a named refusal, never a key.
"""

from __future__ import annotations

import json

import pytest

from scripts.office_actor_rekey_to_instance import main
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_rekey_test"


def _instance_file(persona_instance_id: str) -> None:
    from agent_runtime import paths

    path = paths.persona_instance_path(persona_instance_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"id": persona_instance_id}), encoding="utf-8")


def _store():
    from agent_runtime.office_store import OfficeStore

    return OfficeStore()


def _office_tree() -> dict[str, str]:
    """Every file under the office root, by relative path → content."""

    from agent_runtime import paths

    root = paths.office_root()
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()
    }


def _seed_desk_first(store) -> None:
    """The shape of three of the four live ``ws_codex-test-workspace_28d285``
    actors: desk first in file order, agent second."""

    _instance_file("personainst_backend_dev_agent_29fdd71a")
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "backend_dev",
            "items": [
                {"item_id": "desk-backend_dev", "kind": "desk", "position": [1.0, 2.0], "folder": "Desks"},
                {
                    "item_id": "personainst_backend_dev_agent_29fdd71a",
                    "kind": "agent",
                    "position": [1.0, 3.0],
                    "folder": "Agents",
                    "display_name": "Backend Dev",
                    "pet_slug": "shushu",
                    "scale": 1.25,
                },
            ],
        },
    )


def test_the_dry_run_is_the_default_and_writes_absolutely_nothing(capsys):
    """The default has to be the safe one — this script's whole risk profile is
    that someone runs it to LOOK. Proven by whole-tree content equality: a
    revision bump, a new actor file, or a touched surface would all show."""

    store = _store()
    _seed_desk_first(store)
    before = _office_tree()

    assert main([]) == 0
    assert _office_tree() == before

    out = capsys.readouterr().out
    assert "DRY RUN (no writes)" in out
    assert "DRY RUN — nothing was written" in out
    # The three facts the operator asked to see, per actor.
    assert "backend_dev -> personainst_backend_dev_agent_29fdd71a" in out
    assert "items     : 2" in out
    assert "instance  : present:" in out


def test_apply_rekeys_the_actor_and_archives_the_old_class_key():
    """``--apply``, on a hermetic root. New key active with the binding set, old
    key archived (never deleted) and recorded in the resurrection guard."""

    from agent_runtime import paths

    store = _store()
    _seed_desk_first(store)

    assert main(["--apply"]) == 0

    new_key = "personainst_backend_dev_agent_29fdd71a"
    assert [a.actor_key for a in store.scan_actors(WORKSPACE).actors] == [new_key]
    actor = store.get_actor(WORKSPACE, new_key)
    assert actor.persona_instance_id == new_key
    assert actor.persona_id == "backend_dev"
    # Items survive whole — including the desk, which carries no instance id of
    # its own and would be the easiest thing to drop.
    assert [(i.item_id, i.kind) for i in actor.items] == [
        ("desk-backend_dev", "desk"),
        ("personainst_backend_dev_agent_29fdd71a", "agent"),
    ]
    assert actor.items[1].display_name == "Backend Dev"
    assert actor.items[1].pet_slug == "shushu"
    assert actor.items[1].scale == 1.25

    # Archive-never-delete, and the guard ledger records the old key.
    assert paths.office_archived_actor_path(WORKSPACE, "backend_dev").exists()
    assert store.get_surface(WORKSPACE).archived_actor_keys == ["backend_dev"]

    # And the guard is not fighting the new key: it is a different string, so
    # ``upsert_actor``'s ledger-clearing branch never fired for it.
    assert new_key not in store.get_surface(WORKSPACE).archived_actor_keys


def test_a_second_apply_is_a_no_op_because_the_actor_is_already_keyed(capsys):
    """Idempotence. Re-running must not re-archive, re-upsert, or bump anything
    — an operator WILL run this twice."""

    store = _store()
    _seed_desk_first(store)
    assert main(["--apply"]) == 0
    after_first = _office_tree()
    capsys.readouterr()

    assert main(["--apply"]) == 0
    assert _office_tree() == after_first
    assert "already-keyed" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        pytest.param(
            [{"item_id": "desk-lonely", "kind": "desk", "position": [0.0, 0.0]}],
            "0 agent-kind items",
            id="no-agent-item",
        ),
        pytest.param(
            [
                {"item_id": "personainst_a_agent_1111", "kind": "agent", "position": [0.0, 0.0]},
                {"item_id": "personainst_a_agent_2222", "kind": "agent", "position": [1.0, 0.0]},
            ],
            "2 agent-kind items",
            id="two-agent-items",
        ),
    ],
)
def test_an_actor_that_does_not_fit_the_pattern_is_skipped_and_named(items, expected, capsys):
    """No guesswork. Zero agent items has no binding to read; two has no way to
    choose between them. Both are REPORTED and left alone."""

    store = _store()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(WORKSPACE, {"persona_id": "lonely", "items": items})
    before = _office_tree()

    assert main(["--apply"]) == 0
    assert _office_tree() == before

    out = capsys.readouterr().out
    assert "verdict   : SKIP" in out
    assert expected in out
    assert "SKIP=1" in out


def test_an_unresolvable_instance_is_refused_and_the_exit_code_says_so(capsys):
    """The sharp one: the agent item id LOOKS like an instance id but no
    ``persona_instances/<id>.json`` exists. Refuse — a re-key onto an invented
    instance would key the placement to something the prune lane can never
    reap, and the actor would be unreachable by both keys."""

    store = _store()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "ghostly",
            "items": [{"item_id": "personainst_ghostly_agent_deadbeef", "kind": "agent", "position": [0.0, 0.0]}],
        },
    )
    before = _office_tree()

    assert main(["--apply"]) == 1  # non-zero: a refusal is not a clean run
    assert _office_tree() == before

    out = capsys.readouterr().out
    assert "verdict   : REFUSE" in out
    assert "MISSING:" in out
    assert "refusing to invent an instance" in out
    assert "REFUSE=1" in out


def test_the_agent_first_ordering_is_handled_the_same_as_desk_first(capsys):
    """``ws_codex-test-workspace_28d285/qa.json`` is agent-FIRST while its three
    neighbours are desk-first, and its desk is ``qa_desk`` rather than
    ``desk-qa``. Nothing here may index items positionally or pattern-match a
    desk id."""

    _instance_file("personainst_qa_agent_9c8a382f")
    store = _store()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "qa",
            "items": [
                {"item_id": "personainst_qa_agent_9c8a382f", "kind": "agent", "position": [0.0, 0.0]},
                {"item_id": "qa_desk", "kind": "desk", "position": [0.0, 1.0]},
            ],
        },
    )

    assert main(["--apply"]) == 0
    actor = store.get_actor(WORKSPACE, "personainst_qa_agent_9c8a382f")
    assert actor.persona_instance_id == "personainst_qa_agent_9c8a382f"
    assert [i.item_id for i in actor.items] == ["personainst_qa_agent_9c8a382f", "qa_desk"]
    assert "REKEY=1" in capsys.readouterr().out


def test_a_deskless_actor_rekeys_too():
    """All four ``ws_testv4_afb811`` actors have exactly one agent item and no
    desk at all. A migration that assumed a desk would skip the entire second
    live workspace."""

    _instance_file("personainst_dev_agent_8f685ad1")
    store = _store()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "dev",
            "items": [{"item_id": "personainst_dev_agent_8f685ad1", "kind": "agent", "position": [0.0, 0.0]}],
        },
    )

    assert main(["--apply"]) == 0
    actor = store.get_actor(WORKSPACE, "personainst_dev_agent_8f685ad1")
    assert actor.persona_instance_id == "personainst_dev_agent_8f685ad1"
    assert len(actor.items) == 1


def test_the_new_key_is_written_before_the_old_one_is_archived(monkeypatch, capsys):
    """The write ORDER, which is a safety claim and therefore has to be pinned.

    The two writes are not atomic. New-key-first means a crash between them
    leaves a transient DUPLICATE; old-key-first would leave the workspace with
    ZERO placements for that agent — a blank spot on the canvas with no record
    of what belonged there. Proven by failing the first write and asserting the
    old actor is still ACTIVE, because end-state alone cannot tell the two
    orderings apart.
    """

    from agent_runtime.office_store import OfficeStore

    store = _store()
    _seed_desk_first(store)

    def _boom(self, *a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(OfficeStore, "upsert_actor", _boom)
    with pytest.raises(RuntimeError, match="disk full"):
        main(["--apply"])
    capsys.readouterr()

    assert [a.actor_key for a in _store().scan_actors(WORKSPACE).actors] == ["backend_dev"]
    assert _store().get_surface(WORKSPACE).archived_actor_keys == []


def test_a_store_that_mints_an_unexpected_key_stops_the_migration(monkeypatch, capsys):
    """``OfficeStore`` is the only key authority; ``_plan_actor`` merely
    PREDICTS what it will mint. If the two ever disagree the script must stop
    with the old actor intact — archiving it against a key nobody planned for
    would strand the placement under a third name."""

    from agent_runtime.office_store import OfficeStore

    store = _store()
    _seed_desk_first(store)
    real_upsert = OfficeStore.upsert_actor

    def _drifting(self, workspace_id, payload, **kw):
        actor = real_upsert(self, workspace_id, payload, **kw)
        actor.actor_key = "personainst_something_else"
        return actor

    monkeypatch.setattr(OfficeStore, "upsert_actor", _drifting)
    with pytest.raises(RuntimeError, match="plan expected"):
        main(["--apply"])
    capsys.readouterr()

    assert store.actor_exists(WORKSPACE, "backend_dev")
    assert store.get_surface(WORKSPACE).archived_actor_keys == []


def test_an_instance_bound_actor_is_left_alone_even_if_its_item_points_elsewhere():
    """The binding on the ACTOR wins over the id on its item, always.

    An actor already bound to instance A whose agent item id happens to name a
    different real instance B must be reported ``already-keyed`` and left
    untouched. Deriving from the item here would silently re-home a live
    placement onto another agent's instance — a wrong answer that looks like a
    successful migration.
    """

    _instance_file("personainst_dev_agent_3ebfce41")
    _instance_file("personainst_dev_agent_8f685ad1")
    store = _store()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev_agent_3ebfce41",
            # Deliberately the OTHER instance's id.
            "items": [{"item_id": "personainst_dev_agent_8f685ad1", "kind": "agent", "position": [0.0, 0.0]}],
        },
    )
    before = _office_tree()

    assert main(["--apply"]) == 0
    assert _office_tree() == before
    assert [a.actor_key for a in store.scan_actors(WORKSPACE).actors] == ["personainst_dev_agent_3ebfce41"]


def test_the_rekeyed_actor_is_what_the_office_rpc_lane_then_publishes():
    """End to end: the point of the migration is that the 9th wire key stops
    being null. Read it back through the REAL projection rather than off the
    model, because the projection is what the launcher sees."""

    from agent_runtime.serve_rpc import _runtime_office_get

    store = _store()
    _seed_desk_first(store)

    before = _runtime_office_get("r", {"workspace_id": WORKSPACE})["result"]
    assert {i["persona_instance_id"] for i in before["items"]} == {None}

    assert main(["--apply"]) == 0

    after = _runtime_office_get("r", {"workspace_id": WORKSPACE})["result"]
    assert [(i["item_id"], i["persona_instance_id"]) for i in after["items"]] == [
        ("desk-backend_dev", "personainst_backend_dev_agent_29fdd71a"),
        ("personainst_backend_dev_agent_29fdd71a", "personainst_backend_dev_agent_29fdd71a"),
    ]
    assert store.scan_actors(WORKSPACE).actors  # the store, not just the projection
