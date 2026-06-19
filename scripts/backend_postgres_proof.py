"""Run/capture EterniaBackend Docker/PostgreSQL promotion proof.

This helper exists for Harness agents that need the real backend release
proof without retyping the fragile command. It intentionally rejects SQLite
and mocked-only invocations; those are useful iteration tools but not promotion
evidence for Backend product edits.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time


FORBIDDEN_MARKERS = (
    "--sqlite",
    "sqlite",
    "mocked-only",
    "mocked_only",
    "DJANGO_USE_SQLITE=1",
    "NEXUS_SKIP_INFRA=1",
)

DEFAULT_BACKEND_ROOT = Path(
    os.environ.get(
        "ETERNIABACKEND_ROOT",
        r"X:\Unreal Engine\Engine\EterniaBackend\eternia-backend",
    )
)


def _contains_forbidden_sqlite_marker(values: list[str]) -> str | None:
    haystack = " ".join(values).lower()
    for marker in FORBIDDEN_MARKERS:
        if marker.lower() in haystack:
            return marker
    return None


def resolve_backend_root(root: Path) -> Path:
    root = root.expanduser().resolve()
    missing: list[str] = []
    for rel in ("scripts/test.sh", "docs/testing/README.md"):
        if not (root / rel).exists():
            missing.append(rel)
    if missing:
        raise FileNotFoundError(
            f"{root} is not an EterniaBackend checkout; missing {', '.join(missing)}"
        )
    return root


def default_log_path(root: Path, *, label: str | None = None) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    safe_label = (label or "harness_local_postgres_full_gate").replace(" ", "_")
    return root / "qa_artifacts" / f"{safe_label}_{stamp}.log"


def build_bash_command(
    *,
    backend_root: Path,
    log_path: Path,
    keepdb: bool,
    test_args: list[str],
) -> str:
    test_cmd = ["scripts/test.sh"]
    if keepdb:
        test_cmd.append("--keepdb")
    test_cmd.extend(test_args)

    rel_log = log_path
    try:
        rel_log = log_path.relative_to(backend_root)
    except ValueError:
        pass

    pieces: list[str] = ["set -euo pipefail"]
    venv_activate = backend_root / ".EterniaBackendVirtualEnv" / "Scripts" / "activate"
    if venv_activate.exists():
        pieces.append("source .EterniaBackendVirtualEnv/Scripts/activate")
    pieces.append("mkdir -p qa_artifacts")
    pieces.append(
        f"{' '.join(shlex.quote(arg) for arg in test_cmd)} 2>&1 | tee {shlex.quote(str(rel_log).replace(os.sep, '/'))}"
    )
    return " && ".join(pieces)


def find_bash() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("bash was not found; install Git Bash or run from WSL/Linux")


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run EterniaBackend scripts/test.sh default Docker/PostgreSQL proof and tee a log.",
    )
    parser.add_argument(
        "--backend-root",
        type=Path,
        default=DEFAULT_BACKEND_ROOT,
        help="Path to the EterniaBackend checkout.",
    )
    parser.add_argument(
        "--keepdb",
        action="store_true",
        help="Pass --keepdb through to scripts/test.sh for focused reruns.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        help="Explicit log path. Defaults to backend qa_artifacts/harness_local_postgres_full_gate_<timestamp>.log.",
    )
    parser.add_argument("--label", help="Log filename label when --log is omitted.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=1800,
        help="Subprocess timeout. Full backend proof commonly takes 10-15 minutes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the exact bash command without running it.",
    )
    parser.add_argument(
        "test_args",
        nargs=argparse.REMAINDER,
        help="Optional manage.py test targets passed to scripts/test.sh after '--'.",
    )
    args = parser.parse_args(argv)

    test_args = list(args.test_args)
    if test_args and test_args[0] == "--":
        test_args = test_args[1:]

    forbidden = _contains_forbidden_sqlite_marker(test_args + [os.environ.get("DJANGO_USE_SQLITE", ""), os.environ.get("NEXUS_SKIP_INFRA", "")])
    if forbidden:
        print(
            f"Refusing non-release backend proof marker {forbidden!r}; use scripts/test.sh default Docker/PostgreSQL tier.",
            file=sys.stderr,
        )
        return 2

    try:
        backend_root = resolve_backend_root(args.backend_root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    log_path = (args.log.expanduser().resolve() if args.log else default_log_path(backend_root, label=args.label))
    command = build_bash_command(
        backend_root=backend_root,
        log_path=log_path,
        keepdb=bool(args.keepdb),
        test_args=test_args,
    )

    print(f"Backend root: {backend_root}")
    print(f"Evidence log: {log_path}")
    print("Proof tier: scripts/test.sh default Docker/PostgreSQL (not --sqlite)")
    print(f"Command: {command}")

    if args.dry_run:
        return 0

    try:
        bash = find_bash()
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    log_path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [bash, "-lc", command],
        cwd=str(backend_root),
        text=True,
        timeout=args.timeout_seconds,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(run())
