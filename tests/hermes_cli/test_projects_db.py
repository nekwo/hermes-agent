"""Tests for the per-profile Projects store (hermes_cli/projects_db)."""

from __future__ import annotations

import os

import pytest

from hermes_cli import projects_db as pdb


@pytest.fixture
def conn(tmp_path):
    c = pdb.connect(db_path=tmp_path / "projects.db")
    try:
        yield c
    finally:
        c.close()






def test_discovery_policy_change_clears_only_discovered_rows(conn):
    project_id = pdb.create_project(conn, name="Explicit", folders=["/www/explicit"])
    pdb.record_discovered_repos(
        conn, [("/www/scanned", "scanned")], policy_key="policy-a"
    )

    assert pdb.reconcile_discovered_repos_policy(conn, "policy-b") is True
    assert pdb.list_discovered_repos(conn) == []
    assert pdb.get_project(conn, project_id) is not None
    assert pdb.get_discovery_policy_key(conn) == "policy-b"






def test_create_get_list(conn, tmp_path):
    # An already-absolute, already-normalized path of the HOST's own
    # spelling. A "/tmp/hermes" literal is a POSIX spelling, not a
    # portable absolute path: create_project() normalizes folders through
    # os.path.abspath(), which on Windows prepends the current drive and
    # yields "X:\tmp\hermes". Using tmp_path keeps the round-trip
    # (normalize -> store -> read back) as the thing under test on both
    # platforms, and keeps the test off a shared /tmp.
    folder = str(tmp_path / "hermes")
    pid = pdb.create_project(conn, name="Hermes Agent", folders=[folder])
    proj = pdb.get_project(conn, pid)

    assert proj is not None
    assert proj.slug == "hermes-agent"
    assert proj.name == "Hermes Agent"
    # First folder becomes primary.
    assert proj.primary_path == folder
    assert [f.path for f in proj.folders] == [folder]
    assert proj.folders[0].is_primary is True

    # Lookup by slug too.
    assert pdb.get_project(conn, "hermes-agent").id == pid
    assert len(pdb.list_projects(conn)) == 1












def test_project_for_path_skips_archived(conn):
    pid = pdb.create_project(conn, name="P", folders=["/www/app"])
    pdb.archive_project(conn, pid)

    assert pdb.project_for_path(conn, "/www/app/src") is None
    # Archived hidden from the default list but visible with include_archived.
    assert pdb.list_projects(conn) == []
    assert len(pdb.list_projects(conn, include_archived=True)) == 1

    pdb.restore_project(conn, pid)
    assert pdb.project_for_path(conn, "/www/app/src").id == pid






def test_per_profile_isolation(tmp_path):
    # Two distinct DB paths stand in for two profiles' HERMES_HOME.
    a = pdb.connect(db_path=tmp_path / "a" / "projects.db")
    b = pdb.connect(db_path=tmp_path / "b" / "projects.db")
    try:
        # Host-spelled absolute paths — see test_create_get_list for why a
        # "/a/scanned" literal is not portable through _normalize_path().
        project_root = str(tmp_path / "only_in_a")
        scanned_root = str(tmp_path / "only_in_a" / "scanned")
        pdb.create_project(a, name="Only In A", folders=[project_root])
        pdb.record_discovered_repos(a, [(scanned_root, "scanned")])

        assert [p.slug for p in pdb.list_projects(a)] == ["only-in-a"]
        assert pdb.list_projects(b) == []
        assert [row["root"] for row in pdb.list_discovered_repos(a)] == [
            scanned_root
        ]
        assert pdb.list_discovered_repos(b) == []
    finally:
        a.close()
        b.close()


