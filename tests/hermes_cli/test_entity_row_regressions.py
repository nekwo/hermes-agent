"""Regression pins for the 2026-07-31 audit's live entity-row defects.

Two defects shipped inside ``hermes_cli/harness.py``'s hand-written entity
rows (the snapshot's rows were correct throughout — see doc 18's post-sync
follow-ups for the structural entity_rows consolidation proposal):

1. ``_workspace_row(full=True)`` read the undefined name ``tasks`` (S8
   mission-lane residue) — ``NameError`` on every ``workspace show``,
   ``workspace delete --dry-run``, and ``workspace archive``.
2. ``_realm_row`` hardcoded ``"sync": "in_sync"`` — the exact fake the
   snapshot's own realm row forbids (absent sidecar must render as
   "not checked", never a false all-clear).

S48 (ledger item 4) has since executed the consolidation these two defects
argued for: both rows are now re-keys of ``_workspace_summary`` /
``_realm_summary``, so neither field can be answered twice again. These pins
are KEPT rather than folded into ``test_entity_row_characterization.py`` —
they are the historical record of what went wrong, and they must stay true
through any FUTURE re-shaping of the rows, not only through this one.
"""

from __future__ import annotations

import hermes_cli.harness as harness
from agent_runtime.store import RealmStore, WorkspaceStore


def _mk_realm_and_workspace():
    realm = RealmStore().create(name="row-regression-realm", server_id="srv_test")
    workspace = WorkspaceStore().create(name="row-regression-ws", realm_id=realm.id)
    return realm, workspace


def test_workspace_full_row_builds_without_mission_lane_residue(tmp_path, monkeypatch):
    _, workspace = _mk_realm_and_workspace()
    row = harness._workspace_row(workspace, full=True)
    assert row["id"] == workspace.id
    assert row["kind"] == "workspace"
    # The S8 residue field is gone rather than crashing the whole row.
    assert "goal_ids" not in row


def test_realm_row_sync_is_honest_not_a_literal(tmp_path, monkeypatch):
    realm, _ = _mk_realm_and_workspace()
    row = harness._realm_row(realm)
    # No sidecar has ever been written for this realm: the honest value is
    # None ("not checked"), matching agent_runtime/snapshot.py's realm row.
    assert row["sync"] is None
