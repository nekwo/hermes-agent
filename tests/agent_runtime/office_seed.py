"""Seed an office the way production now requires: the workspace record FIRST.

MC-8 / P10 made ``OfficeStore.ensure_surface`` refuse typed
(``errors.WorkspaceUnresolved``) for an id no workspace record resolves. Before
that it authored a default surface for ANY string that passed ``_safe_id``, and
every office suite in this package seeded its fixture by doing exactly that —
calling ``ensure_surface`` on a bare constant.

That is not a coincidence, and it is worth saying plainly rather than quietly
fixing: the fixtures had been modelling a store shape production should never
have been able to produce, which is how the hole stayed invisible through three
reviews of this area. The live consequence was measured — a leaked test context
minted a real office in the operator's runtime root at ``ws_office_patch_test``,
135 events and a ``revision 67`` actor file, for a workspace no verb ever
authorised.

So the fixtures now create the record they always implied. The helper is shared
rather than copied into each suite for the same reason the exclusion set imports
its writers' constants: one precondition, stated once, so a suite cannot drift
into re-opening the hole by seeding some other way.

Deliberately NOT in ``conftest.py``: this is an explicit call a test makes, not
ambient setup. A fixture that silently created workspace records for every test
would hide the precondition again — the opposite of the point.
"""

from __future__ import annotations

from agent_runtime.store import WorkspaceStore


def seed_workspace_record(workspace_id: str, *, name: str | None = None):
    """Create the workspace record ``workspace_id`` names, if it is not there.

    Idempotent, because several suites seed the same constant from more than one
    helper and a second ``create`` would raise rather than converge.

    Returns the resolved ``Workspace``.
    """

    wsid = str(workspace_id or "").strip()
    if not wsid:
        raise ValueError("seed_workspace_record needs a workspace id")
    store = WorkspaceStore()
    for existing in store.list_all(include_archived=True):
        if getattr(existing, "id", None) == wsid:
            return existing
    return store.create(name=name or f"seed {wsid}", workspace_id=wsid)


def unlink_workspace_record(workspace_id: str) -> None:
    """Remove the workspace RECORD and leave the office standing.

    The honest way to build an orphaned office now that the door is shut. It
    reproduces the state that actually reaches the projection in the field — a
    surface whose workspace row is gone — without depending on the lazy-mint hole
    the fixtures used to lean on, and without going through
    ``WorkspaceStore.delete``, whose cascade ``rmtree``s the very office subtree
    the test needs.

    Writes NO realm tombstone, matching a delete that never reached a realm
    ledger; a suite that wants the deleted-vs-never-recorded distinction adds the
    ledger entry itself.
    """

    from agent_runtime import paths

    wsid = str(workspace_id or "").strip()
    if not wsid:
        raise ValueError("unlink_workspace_record needs a workspace id")
    paths.workspace_path(wsid).unlink(missing_ok=True)
