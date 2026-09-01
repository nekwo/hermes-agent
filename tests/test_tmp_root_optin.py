"""The opt-in test-temp root (suite-perf Stage 7) redirects, degrades, prunes.

The function under test is conftest's `_maybe_redirect_test_tmp`, exercised
against a passed-in environ so these tests never move the RUNNING session's
temp out from under it.
"""

import os
import time

from tests.conftest import _maybe_redirect_test_tmp


def test_absent_var_changes_nothing():
    env = {}
    assert _maybe_redirect_test_tmp(env) is None
    assert env == {}


def test_missing_directory_degrades_to_todays_behavior(tmp_path):
    env = {"HERMES_TEST_TMP_ROOT": str(tmp_path / "does-not-exist")}
    assert _maybe_redirect_test_tmp(env) is None
    assert "TMP" not in env


def test_real_root_redirects_all_three_keys_into_a_fresh_run_dir(tmp_path):
    env = {"HERMES_TEST_TMP_ROOT": str(tmp_path)}
    run_dir = _maybe_redirect_test_tmp(env)
    assert run_dir is not None
    assert os.path.isdir(run_dir)
    assert os.path.dirname(run_dir) == str(tmp_path)
    assert env["TMP"] == env["TEMP"] == env["TMPDIR"] == run_dir


def test_two_runs_get_distinct_dirs(tmp_path):
    a = _maybe_redirect_test_tmp({"HERMES_TEST_TMP_ROOT": str(tmp_path)})
    b = _maybe_redirect_test_tmp({"HERMES_TEST_TMP_ROOT": str(tmp_path)})
    assert a != b


def test_aged_run_dirs_are_pruned_and_fresh_ones_kept(tmp_path):
    old = tmp_path / "run-old"
    fresh = tmp_path / "run-fresh"
    old.mkdir()
    fresh.mkdir()
    aged = time.time() - 8 * 24 * 3600
    os.utime(old, (aged, aged))
    _maybe_redirect_test_tmp({"HERMES_TEST_TMP_ROOT": str(tmp_path)})
    assert not old.exists()
    assert fresh.exists()
