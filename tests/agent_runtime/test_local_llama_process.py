import os
from pathlib import Path
import subprocess
import sys
import time

import psutil
import pytest

from agent_runtime.local_llama.process import OwnedProcess


def test_owned_tree_exit_does_not_kill_unrelated_process(tmp_path):
    marker = tmp_path / "child.pid"
    program = ("import subprocess,sys,time;from pathlib import Path;"
               "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
               f"Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)")
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"],
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        with OwnedProcess([sys.executable, "-c", program], cwd=tmp_path) as owned:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert marker.exists()
            child = psutil.Process(int(marker.read_text()))
            assert child.is_running()
        assert owned.poll() is not None
        deadline = time.monotonic() + 5
        while child.is_running() and time.monotonic() < deadline:
            time.sleep(.05)
        assert not child.is_running()
        assert other.poll() is None
    finally:
        other.kill()
        other.wait(timeout=5)


def test_failed_launch_is_bounded(tmp_path):
    with pytest.raises(OSError):
        OwnedProcess([str(tmp_path / "missing.exe")], cwd=tmp_path)
