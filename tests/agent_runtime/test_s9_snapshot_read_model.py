from __future__ import annotations

import sqlite3

from agent_runtime.read_model import READ_MODEL_SCHEMA_VERSION, ROW_TABLES, ReadModel
from agent_runtime.snapshot import build_snapshot


def test_s9_snapshot_contract_removes_mission_rows_and_bumps_versions(tmp_path):
    # B4 bumped 2 -> 3 when the duplicate per-section projections_misc rows were
    # dropped; the version is what makes an old database clear and rebuild
    # instead of serving rows no writer maintains any more.
    assert READ_MODEL_SCHEMA_VERSION == 3
    assert ROW_TABLES == ("agent_instances", "operator_channels")

    snapshot = build_snapshot()
    assert snapshot["parity"]["contract_version"] == 45
    for key in ("goals", "stage_verification", "runs", "proofs", "incidents"):
        assert key not in snapshot

    model = ReadModel(tmp_path / "read_model.db")
    with model.connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_instances)")}
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(agent_instances)")}
    assert "task_id" not in columns
    assert indexes == {"sqlite_autoindex_agent_instances_1"}
