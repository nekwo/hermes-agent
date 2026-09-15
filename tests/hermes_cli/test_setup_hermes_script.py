from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "setup-hermes.sh"


def test_setup_hermes_script_is_valid_shell():
    # Resolve bash explicitly instead of letting the OS pick. On Windows
    # `subprocess.run(["bash", ...])` does NOT follow PATH order: CreateProcess
    # searches System32 first, so it silently runs the WSL launcher
    # (C:\WINDOWS\system32\bash.exe) — a shell in a different filesystem
    # namespace (/mnt/x/...) that cannot see a drive-letter path at all, and
    # reports the miss as bare exit 127 "No such file". shutil.which follows
    # PATH and finds the real bash.
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash on PATH — cannot syntax-check a shell script")
    # as_posix(), not str(): str(Path) yields "X:\wt\..." and bash consumes
    # each backslash as an escape, so the argument arrives as "X:wt..." — a
    # path-spelling artifact that reads exactly like a syntax failure.
    result = subprocess.run(
        [bash, "-n", SETUP_SCRIPT.as_posix()], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_setup_hermes_script_has_termux_path():
    content = SETUP_SCRIPT.read_text(encoding="utf-8")

    assert "is_termux()" in content
    assert ".[termux]" in content
    assert "constraints-termux.txt" in content
    assert "$PREFIX/bin" in content
