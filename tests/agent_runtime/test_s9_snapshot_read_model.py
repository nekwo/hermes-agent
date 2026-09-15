"""S9's snapshot contract cut, after the read model it also pinned was retired.

This file used to make TWO claims in one test: that the snapshot contract had
dropped the five mission sections, and that the read model's schema version and
row tables had moved with it (``READ_MODEL_SCHEMA_VERSION == 3``, ``ROW_TABLES``,
and a ``PRAGMA table_info`` check proving the ``task_id`` column was gone from
``agent_instances``).

Stage 6 (duplicate-implementation retirement, 2026-08-22) deleted
``agent_runtime/read_model.py`` and its schema. The second claim therefore has
no subject; it is NOT restated here as an absence, because
``tests/agent_runtime/test_s46_incremental_projection_lane_removal.py`` and the
``agent_runtime.read_model`` MODULE tombstone already own that, and a third copy
of one absence is how a registry stops being the authority.

The FIRST claim survives whole and is what this file is now. It is worth keeping
separately from ``test_snapshot.py``: that file asserts the sections are absent
from a built frame, while this one ties the absence to the CONTRACT VERSION —
the number a launcher decides compatibility with — so a section quietly
returning without a bump goes red here rather than nowhere.
"""

from __future__ import annotations

from agent_runtime.snapshot import SNAPSHOT_CONTRACT_VERSION, build_snapshot

#: The five sections S9 removed from the snapshot contract. Named rather than
#: inlined so the count is countable: a sixth arriving in this tuple without a
#: contract move is the defect, not a passing test.
S9_REMOVED_SECTIONS = ("goals", "stage_verification", "runs", "proofs", "incidents")


def test_s9_snapshot_contract_removes_mission_rows_and_stamps_its_version():
    snapshot = build_snapshot()

    assert snapshot["parity"]["contract_version"] == SNAPSHOT_CONTRACT_VERSION
    for key in S9_REMOVED_SECTIONS:
        assert key not in snapshot, (
            f"{key!r} is back in the snapshot frame. S9 removed it from the "
            "contract; a section that returns is a wire change and rides a "
            "SNAPSHOT_CONTRACT_VERSION bump, not a silent re-add."
        )
