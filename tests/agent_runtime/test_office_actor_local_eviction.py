"""AX7 — a DIAGNOSTIC repair evicts locally; only an AUTHORED delete tombstones.

The ruling (operator, 2026-08-30) splits the office delete by INTENT, not by
origin and not by id spelling. An operator-clicked delete is authored: the
tombstone and its realm-wide propagation are the point, on whichever machine the
click happens — that is what makes a delete stick against a peer that still
holds the row. A doctor remediation, a dispatch step or a census cleanup is
diagnostic: it targets THIS install's projection, nobody asked for a realm-wide
delete, and minting one would delete the placement on every machine in the realm
to fix a local display.

The live case it was ruled against: `personainst_neko_supervisor_agent_9682caf4`
— archived at revision 3 on the Mac by a dispatch-ordered REPAIR while still
active at revision 2 at its Windows origin.

Only the LEDGER write differs. Archive-never-delete is not what is being traded,
which is what the restore case below pins.
"""

from __future__ import annotations

import pytest

from agent_runtime import paths
from agent_runtime.office_store import OfficeStore
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_local_eviction"
INSTANCE = "personainst_qa_agent_c0ffee01"


def _payload() -> dict:
    return {
        "persona_id": "qa",
        "persona_instance_id": INSTANCE,
        "items": [
            {
                "item_id": INSTANCE,
                "kind": "agent",
                "position": [1.0, 2.0],
                "folder": "Agents",
                "display_name": "QA Agent",
            }
        ],
    }


@pytest.fixture
def store():
    seed_workspace_record(WORKSPACE)
    store = OfficeStore()
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


@pytest.fixture
def placed(store):
    return store.upsert_actor(WORKSPACE, _payload(), updated_by="operator")


def _ledger(store) -> list[str]:
    return list(store.get_surface(WORKSPACE).archived_actor_keys)


def test_an_authored_delete_records_the_tombstone(store, placed):
    """The DEFAULT, and the anti-vacuity twin of the case below: same store,
    same actor, one keyword apart — so an empty ledger there is the flag's doing
    and not a fixture that never writes one."""

    store.remove_actor(WORKSPACE, placed.actor_key, reason="operator")

    assert _ledger(store) == [placed.actor_key]
    assert paths.office_archived_actor_path(WORKSPACE, placed.actor_key).exists()
    assert not paths.office_actor_path(WORKSPACE, placed.actor_key).exists()


def test_a_local_eviction_archives_the_actor_and_mints_no_realm_intent(store, placed):
    """THE pin. The row leaves this install's projection; the ledger — the half
    a realm pull replicates — is untouched, so nothing is asserted about any
    other machine."""

    store.remove_actor(
        WORKSPACE, placed.actor_key, reason="local_eviction", record_tombstone=False
    )

    assert _ledger(store) == []
    # Archive-never-delete is NOT what the mode trades away.
    assert paths.office_archived_actor_path(WORKSPACE, placed.actor_key).exists()
    assert not paths.office_actor_path(WORKSPACE, placed.actor_key).exists()


def test_a_locally_evicted_actor_is_still_restorable(store, placed):
    """The consequence of keeping the archive copy, asserted rather than
    assumed: the diagnostic mode is reversible by the sanctioned verb."""

    store.remove_actor(WORKSPACE, placed.actor_key, record_tombstone=False)

    restored = store.restore_actor(WORKSPACE, placed.actor_key)

    assert restored.state == "active"
    assert paths.office_actor_path(WORKSPACE, placed.actor_key).exists()


def test_a_local_eviction_leaves_an_earlier_tombstone_alone(store):
    """A ledger it did not write, it also does not clear. The mode withholds
    one write; it is not a second authority over the ledger's contents."""

    first = store.upsert_actor(WORKSPACE, _payload(), updated_by="operator")
    store.remove_actor(WORKSPACE, first.actor_key, reason="operator")
    assert _ledger(store) == [first.actor_key]

    second = store.upsert_actor(
        WORKSPACE,
        {
            "persona_id": "dev",
            "persona_instance_id": "personainst_dev_agent_beefbeef",
            "items": [
                {
                    "item_id": "personainst_dev_agent_beefbeef",
                    "kind": "agent",
                    "position": [3.0, 4.0],
                    "folder": "Agents",
                    "display_name": "Dev Agent",
                }
            ],
        },
        updated_by="operator",
    )
    store.remove_actor(WORKSPACE, second.actor_key, record_tombstone=False)

    assert _ledger(store) == [first.actor_key]


def test_the_cli_flag_reaches_the_store_and_says_so_on_the_ack(store, placed, capsys):
    """The flag is wired, and the run that skipped the tombstone is
    distinguishable from the run that wrote one — the two differ in exactly one
    invisible byte otherwise."""

    import json
    from types import SimpleNamespace

    from hermes_cli.harness_parts.office import _cmd_office_actor_remove

    code = _cmd_office_actor_remove(
        SimpleNamespace(
            workspace=WORKSPACE,
            actor=placed.actor_key,
            reason=None,
            expect_revision=None,
            local_only=True,
            dry_run=False,
            json=True,
            output="json",
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert _ledger(store) == []
    codes = [row["code"] for row in payload.get("warnings") or []]
    assert "office_actor_local_eviction" in codes
