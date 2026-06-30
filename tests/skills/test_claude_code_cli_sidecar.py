from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

SKILL_DIR_ENV = "HERMES_CLAUDE_CODE_CLI_SKILL_DIR"
SKILL_DIR = Path(os.environ.get(SKILL_DIR_ENV, ""))
PYTHON_RUNNER = SKILL_DIR / "scripts" / "claude_python_runner.py"
LOW_MONITOR = SKILL_DIR / "scripts" / "claude_low_token_monitor.py"


@pytest.mark.skipif(not PYTHON_RUNNER.exists(), reason="Alice sidecar runner script not present in this environment")
def test_python_runner_has_detached_sidecar_and_status_file_contract() -> None:
    text = PYTHON_RUNNER.read_text(encoding="utf-8")

    assert "--detach" in text
    assert "spawn_detached_worker" in text
    assert "status_path" in text
    assert ".status.json" in text
    assert "CLAUDE_RUN detached=1" in text
    assert 'write_status("running"' in text
    assert "sidecar" in text.lower()

    # Quick functional smoke: built-in self-test should still pass for the runner.
    proc = subprocess.run(
        [sys.executable, str(PYTHON_RUNNER), "--self-test"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "self-test ok" in proc.stdout


@pytest.mark.skipif(not LOW_MONITOR.exists(), reason="Alice low-token monitor script not present in this environment")
def test_low_token_monitor_reads_sidecar_status_and_pid_state(tmp_path: Path) -> None:
    text = LOW_MONITOR.read_text(encoding="utf-8")

    assert "def load_status_sidecar" in text
    assert "sidecar_status_path" in text
    assert "sidecar_state" in text
    assert "sidecar_alive" in text
    assert "process_alive" in text
    assert "ALERT_RE" in text

    # Build a tiny synthetic log/status pair and run monitor once.
    log_dir = tmp_path
    log = log_dir / "t1.log"
    log.write_text("PHASE 0: smoke\nRUN foo\nRESULT exit=0\n", encoding="utf-8")
    (log_dir / "t1.status.json").write_text(
        '{"state":"done","pid":999999}',
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            str(LOW_MONITOR),
            "--card-id",
            "t1",
            "--log-dir",
            str(log_dir),
            "--glob",
            "*.log",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "Claude dev monitor: t1" in proc.stdout
    assert "sidecar:" in proc.stdout or "status:" in proc.stdout
