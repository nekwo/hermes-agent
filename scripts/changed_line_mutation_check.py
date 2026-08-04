"""Run bounded, explicit mutation claims that intersect changed production lines."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLAIMS = REPO_ROOT / "tests" / "mutation_claims.json"
DEFAULT_EXEMPTIONS = REPO_ROOT / "tool" / "test_quality" / "mutation_exemptions.yaml"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain an object")
    return value


def _changed_lines(base: str, relative_path: str) -> set[int]:
    completed = subprocess.run(
        ["git", "diff", "--unified=0", base, "--", relative_path],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git diff failed for {relative_path}: {completed.stderr.strip()}")
    changed: set[int] = set()
    for row in completed.stdout.splitlines():
        match = HUNK.match(row)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        changed.update(range(start, start + count))
    return changed


def _validate_exemptions(path: Path) -> None:
    # JSON is valid YAML; keeping this dependency-free lets the selector run
    # before CI installs the repository environment.
    rows = _load_json(path).get("exemptions", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"{path}: exemptions must be a list")
    required = {"id", "path", "symbol", "operator", "reason", "owner", "issue", "expires"}
    allowed_reasons = {"equivalent", "observability-only", "generated", "contract-out-of-scope"}
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            raise RuntimeError(f"{path}: every exemption needs {sorted(required)}")
        if row["reason"] not in allowed_reasons:
            raise RuntimeError(f"{path}: invalid reason for {row['id']}: {row['reason']}")
        try:
            expiry = date.fromisoformat(str(row["expires"]))
        except ValueError as error:
            raise RuntimeError(f"{path}: invalid expiry for {row['id']}") from error
        if expiry < date.today():
            raise RuntimeError(f"{path}: exemption expired: {row['id']} ({expiry})")


def _claim_span(text: str, needle: str) -> tuple[int, set[int]]:
    if text.count(needle) != 1:
        raise RuntimeError(f"mutation source must occur exactly once; found {text.count(needle)}")
    offset = text.index(needle)
    start = text.count("\n", 0, offset) + 1
    count = needle.count("\n") + 1
    return offset, set(range(start, start + count))


def _selected_claims(base: str, claims_path: Path) -> list[dict[str, Any]]:
    rows = _load_json(claims_path).get("claims", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"{claims_path}: claims must be a list")
    selected: list[dict[str, Any]] = []
    for claim in rows:
        if not isinstance(claim, dict):
            raise RuntimeError(f"{claims_path}: claim rows must be objects")
        required = {"id", "path", "symbol", "operator", "find", "replace", "test"}
        if not required.issubset(claim):
            raise RuntimeError(f"{claims_path}: claim needs {sorted(required)}")
        target = REPO_ROOT / str(claim["path"])
        if not target.is_file():
            raise RuntimeError(f"{claim['id']}: target missing: {claim['path']}")
        text = target.read_text(encoding="utf-8")
        _, span = _claim_span(text, str(claim["find"]))
        if span & _changed_lines(base, str(claim["path"])):
            selected.append(claim)
    return selected


def _command(claim: dict[str, Any]) -> list[str]:
    raw = claim["test"]
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"{claim['id']}: test must be a non-empty string list")
    return [
        item.replace("{python}", sys.executable).replace("{repo}", str(REPO_ROOT))
        for item in raw
    ]


def _run_command(command: list[str]) -> int:
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def run(base: str, claims_path: Path, exemptions_path: Path, max_candidates: int, list_only: bool) -> int:
    _validate_exemptions(exemptions_path)
    claims = _selected_claims(base, claims_path)
    print(f"mutation candidates: {len(claims)} (cap {max_candidates})")
    for claim in claims:
        print(f"  {claim['id']}: {claim['path']}::{claim['symbol']} [{claim['operator']}]")
    if len(claims) > max_candidates:
        print("candidate cap exceeded; split the diff or raise the cap visibly", file=sys.stderr)
        return 2
    if list_only or not claims:
        return 0

    commands: dict[tuple[str, ...], list[str]] = {}
    for claim in claims:
        command = _command(claim)
        commands.setdefault(tuple(command), command)
    for command in commands.values():
        print(f"BASELINE: {' '.join(command)}")
        if _run_command(command) != 0:
            print("baseline failed; mutation result would be meaningless", file=sys.stderr)
            return 2

    survivors: list[str] = []
    for claim in claims:
        target = REPO_ROOT / str(claim["path"])
        original = target.read_bytes()
        text = original.decode("utf-8")
        _claim_span(text, str(claim["find"]))
        mutated = text.replace(str(claim["find"]), str(claim["replace"]), 1)
        try:
            target.write_text(mutated, encoding="utf-8", newline="")
            print(f"MUTATE: {claim['id']}")
            if _run_command(_command(claim)) == 0:
                survivors.append(str(claim["id"]))
            else:
                print(f"KILLED: {claim['id']}")
        finally:
            target.write_bytes(original)
    if survivors:
        print(f"SURVIVED: {', '.join(survivors)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--exemptions", type=Path, default=DEFAULT_EXEMPTIONS)
    parser.add_argument("--max-candidates", type=int, default=12)
    parser.add_argument("--list", action="store_true", dest="list_only")
    args = parser.parse_args(argv)
    try:
        return run(
            args.base,
            args.claims.resolve(),
            args.exemptions.resolve(),
            args.max_candidates,
            args.list_only,
        )
    except RuntimeError as error:
        print(f"mutation-check configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
