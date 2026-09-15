"""Downstream stable ordering and call-time session-store isolation contracts."""
import pytest
import hermes_state
from hermes_state import SessionDB

@pytest.fixture
def db(tmp_path):
    database = SessionDB(tmp_path / "state.db")
    try:
        yield database
    finally:
        database.close()


def test_no_arg_sessiondb_honors_hermes_home_env_at_call_time(tmp_path, monkeypatch):
    """A no-arg ``SessionDB()`` must resolve ``HERMES_HOME`` live.

    Regression: ``DEFAULT_DB_PATH`` freezes ``get_hermes_home()`` at import, so
    a test (or in-process profile switch) that only set ``HERMES_HOME`` via env
    had its no-arg ``SessionDB()`` writes silently land in whatever home existed
    at import — on a dev box, the live profile's ``state.db``. The resolver must
    read the env on construction, WITHOUT the caller having to patch the private
    ``DEFAULT_DB_PATH`` constant.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Deliberately do NOT *pin* DEFAULT_DB_PATH — that is the whole point.
    # ``tests/conftest.py::_isolate_hermes_home`` now re-pins the constant for
    # every test, so restore the import-time value first: this case is
    # specifically about the UNpinned branch of the resolver.
    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH
    )

    db = SessionDB()
    try:
        assert db.db_path == home / "state.db"
        # End-to-end: the row is written under the redirected home, and the
        # import-time (real) home is never touched.
        db.create_session("iso-1", "cli")
        assert (home / "state.db").exists()
        assert db.db_path != hermes_state._IMPORT_DEFAULT_DB_PATH
    finally:
        db.close()


def test_pinned_default_db_path_still_wins_over_env(tmp_path, monkeypatch):
    """The historical ``monkeypatch.setattr(DEFAULT_DB_PATH, ...)`` hook keeps
    working: an explicitly pinned constant takes precedence over a diverging
    ``HERMES_HOME`` env, so the ~25 gateway/CLI tests that rely on it are
    unaffected by the call-time resolution."""
    pinned = tmp_path / "pinned" / "state.db"
    env_home = tmp_path / "env-home"
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", pinned)
    monkeypatch.setenv("HERMES_HOME", str(env_home))

    assert hermes_state._resolve_default_db_path() == pinned

    db = SessionDB()
    try:
        assert db.db_path == pinned
    finally:
        db.close()


def test_resolve_default_db_path_falls_back_to_live_home(tmp_path, monkeypatch):
    """When ``DEFAULT_DB_PATH`` is untouched, the resolver reads the current
    ``HERMES_HOME`` rather than the import-time snapshot."""
    home = tmp_path / "live-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    # See the note above: the hermetic conftest pins DEFAULT_DB_PATH per test,
    # so restore the import-time value to exercise the unpinned branch.
    monkeypatch.setattr(
        hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH
    )
    assert hermes_state.DEFAULT_DB_PATH == hermes_state._IMPORT_DEFAULT_DB_PATH
    assert hermes_state._resolve_default_db_path() == home / "state.db"


def test_rich_list_orders_deterministically_on_started_at_tie(db):
    """Sessions sharing a ``started_at`` must sort by a stable tiebreak.

    Regression: the non-``order_by_last_active`` branch ordered by
    ``started_at DESC`` alone, so two sessions created in the same clock
    tick sorted in an undefined order — an intermittent flake
    (``test_rich_list_cwd_prefix_filter``) whenever ``time.time()`` tied.
    Force the tie explicitly and assert the ``s.id DESC`` tiebreak (matching
    the method's other two ORDER BY branches) makes the order stable.
    """
    db.create_session("s1", "cli")
    db.create_session("s2", "cli")
    # Collapse both onto the same started_at — the flake condition.
    with db._lock:
        db._conn.execute("UPDATE sessions SET started_at = 1000.0")
        db._conn.commit()

    for _ in range(5):
        ids = [row["id"] for row in db.list_sessions_rich()]
        assert ids == ["s2", "s1"], f"unstable tie ordering: {ids}"

