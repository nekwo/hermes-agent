from unittest.mock import patch

import pytest

from tools.environments import local as local_mod
from tools.environments.local import LocalEnvironment


@pytest.fixture
def posix_temp_dir_arm(monkeypatch):
    """Pin the POSIX arm of ``LocalEnvironment.get_temp_dir``.

    ``get_temp_dir`` branches on ``_IS_WINDOWS`` before it ever looks at
    TMPDIR/TMP/TEMP: on Windows it returns a dedicated space-free cache dir
    under HERMES_HOME, precisely because ``/tmp`` and ``%TEMP%`` are both wrong
    there. The TMPDIR-preference rule these tests pin is the OTHER arm, so name
    the arm rather than letting the host pick one.
    """
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)


class TestLocalTempDir:
    def test_uses_os_tmpdir_for_session_artifacts(self, posix_temp_dir_arm, monkeypatch):
        monkeypatch.setenv("TMPDIR", "/data/data/com.termux/files/usr/tmp")
        monkeypatch.delenv("TMP", raising=False)
        monkeypatch.delenv("TEMP", raising=False)

        with patch.object(LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = LocalEnvironment(cwd=".", timeout=10)

        assert env.get_temp_dir() == "/data/data/com.termux/files/usr/tmp"
        assert env._snapshot_path == f"/data/data/com.termux/files/usr/tmp/hermes-snap-{env._session_id}.sh"
        assert env._cwd_file == f"/data/data/com.termux/files/usr/tmp/hermes-cwd-{env._session_id}.txt"


    def test_falls_back_to_tempfile_when_tmp_missing(self, posix_temp_dir_arm, monkeypatch):
        monkeypatch.delenv("TMPDIR", raising=False)
        monkeypatch.delenv("TMP", raising=False)
        monkeypatch.delenv("TEMP", raising=False)

        with patch("tools.environments.local.os.path.isdir", return_value=False), \
             patch("tools.environments.local.os.access", return_value=False), \
             patch("tools.environments.local.tempfile.gettempdir", return_value="/cache/tmp"), \
             patch.object(LocalEnvironment, "init_session", autospec=True, return_value=None):
            env = LocalEnvironment(cwd=".", timeout=10)
            assert env.get_temp_dir() == "/cache/tmp"
            assert env._snapshot_path == f"/cache/tmp/hermes-snap-{env._session_id}.sh"
            assert env._cwd_file == f"/cache/tmp/hermes-cwd-{env._session_id}.txt"
