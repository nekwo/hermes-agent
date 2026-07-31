"""S38 residue coverage for the canonical per-file parallel runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "hermes_run_tests_parallel",
    _REPO_ROOT / "scripts" / "run_tests_parallel.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)


def test_adaptive_worker_default_respects_cpu_without_oversubscribing() -> None:
    assert _RUNNER._adaptive_default_jobs(32) == 8
    assert _RUNNER._adaptive_default_jobs(4) == 4
    assert _RUNNER._adaptive_default_jobs(None) == 4


def test_timeout_formatter_handles_missing_subprocess_output() -> None:
    rendered = _RUNNER._format_timeout_output(None, 120.0)

    assert "timed out after 120s" in rendered
    assert "process tree terminated" in rendered
    assert "captured output unavailable" in rendered
    assert "None" not in rendered
