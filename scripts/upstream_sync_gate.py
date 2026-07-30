from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_LAUNCHER_ROOT = Path(r"X:\Unreal Engine\Engine\Launcher\EterniaLauncher")
AGENT_TOOL_SEAMS = (
    "agent/",
    "agent_runtime/",
    "hermes_cli/harness.py",
    "hermes_cli/harness_",
    "tools/agent_chat_tool.py",
)


@dataclass(frozen=True)
class GateCommand:
    name: str
    argv: list[str]
    cwd: Path


@dataclass(frozen=True)
class GateResult:
    name: str
    returncode: int
    command: str
    stdout_tail: str
    stderr_tail: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    launcher_root = Path(args.launcher_root).resolve()
    changed_paths = _changed_paths(repo_root, args.base_ref, args.changed_path)
    include_launcher = bool(args.include_launcher or _touches_agent_tool_seam(changed_paths))
    commands = build_gate_commands(
        repo_root=repo_root,
        launcher_root=launcher_root,
        include_launcher=include_launcher,
        simulate_broken_seam=args.simulate_broken_seam,
    )
    if args.dry_run:
        print(
            json.dumps(
                {
                    "verdict": "DRY_RUN",
                    "include_launcher": include_launcher,
                    "changed_paths": changed_paths,
                    "commands": [
                        {"name": command.name, "cwd": str(command.cwd), "argv": command.argv}
                        for command in commands
                    ],
                },
                indent=2,
            )
        )
        return 0

    results: list[GateResult] = []
    fail_fast = bool(args.fail_fast or args.simulate_broken_seam)
    for command in commands:
        result = run_command(command, tail_chars=args.tail_chars)
        results.append(result)
        if fail_fast and not result.ok:
            break
    passed = all(result.ok for result in results)
    payload = {
        "verdict": "PASS" if passed else "FAIL",
        "include_launcher": include_launcher,
        "changed_paths": changed_paths,
        "results": [result.__dict__ for result in results],
    }
    print(json.dumps(payload, indent=2))
    print(f"UPSTREAM_SYNC_GATE {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def build_gate_commands(
    *,
    repo_root: Path,
    launcher_root: Path,
    include_launcher: bool,
    simulate_broken_seam: bool = False,
) -> list[GateCommand]:
    python = sys.executable
    commands: list[GateCommand] = []
    if simulate_broken_seam:
        commands.append(
            GateCommand(
                "rehearsal_broken_seam",
                [python, "-c", "import agent_runtime.__upstream_sync_gate_missing_seam__"],
                repo_root,
            )
        )
    commands.extend(
        [
            GateCommand("agent_runtime_pytest", [python, "-m", "pytest", "tests/agent_runtime", "-q"], repo_root),
            GateCommand("hermes_cli_pytest", [python, "-m", "pytest", "tests/hermes_cli", "-q"], repo_root),
            GateCommand(
                "harness_no_model_smoke",
                # `--temp-root` was retired 2026-07-27 (env-determinism audit
                # §7.3): a smoke run is synthetic and ALWAYS uses a temp runtime
                # root, so the flag could only ever be ignored. Passing it now
                # is a parse error, which would fail this gate on the flag
                # instead of on the smoke.
                [python, "-m", "hermes_cli.main", "harness", "smoke", "--json", "--no-model"],
                repo_root,
            ),
        ]
    )
    if include_launcher:
        commands.append(
            GateCommand(
                "launcher_mission_control",
                ["flutter", "test", "test/features/mission_control", "--reporter=compact"],
                launcher_root,
            )
        )
    return commands


def run_command(command: GateCommand, *, tail_chars: int) -> GateResult:
    completed = subprocess.run(
        command.argv,
        cwd=str(command.cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return GateResult(
        name=command.name,
        returncode=int(completed.returncode),
        command=_quote_command(command.argv),
        stdout_tail=_tail(completed.stdout, tail_chars),
        stderr_tail=_tail(completed.stderr, tail_chars),
    )


def _changed_paths(repo_root: Path, base_ref: str | None, explicit: list[str]) -> list[str]:
    if explicit:
        return sorted({path.replace("\\", "/").lstrip("./") for path in explicit if path.strip()})
    if not base_ref:
        return []
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=str(repo_root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    return sorted({line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip()})


def _touches_agent_tool_seam(paths: Iterable[str]) -> bool:
    for raw in paths:
        path = raw.replace("\\", "/").lstrip("./")
        if any(path.startswith(prefix) for prefix in AGENT_TOOL_SEAMS):
            return True
    return False


def _tail(value: str, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return f"...<tail {limit} chars>...\n{text[-limit:]}"


def _quote_command(argv: list[str]) -> str:
    return subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(argv)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Hermes fork upstream-sync merge gate.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--launcher-root", default=str(DEFAULT_LAUNCHER_ROOT))
    parser.add_argument("--base-ref", default=None, help="Optional base ref for changed-path detection, e.g. upstream/main.")
    parser.add_argument("--changed-path", action="append", default=[], help="Explicit changed path; may be passed more than once.")
    parser.add_argument("--include-launcher", action="store_true", help="Force the Launcher Mission Control gate.")
    parser.add_argument("--simulate-broken-seam", action="store_true", help="Prepend a deterministic failing seam import for red rehearsal.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first failing lane.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned commands without running them.")
    parser.add_argument("--tail-chars", type=int, default=6000)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
