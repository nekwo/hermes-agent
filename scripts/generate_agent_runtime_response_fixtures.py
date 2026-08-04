"""Generate deterministic CLI response envelopes for Launcher contract tests.

The production argparse tree and production handlers emit every fixture.  The
generator runs against an isolated empty runtime root, then normalizes only
the random error id added after the handler has classified the response.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "response_envelopes"
FIXTURE_CASES = {
    "work_list_empty.json": ["harness", "work", "list", "--json"],
    "work_peek_not_found.json": [
        "harness",
        "work",
        "peek",
        "terminal:fixture-missing",
        "--json",
    ],
}


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        normalized = {str(key): _normalize(item) for key, item in value.items()}
        if "error_id" in normalized:
            normalized["error_id"] = "err_fixture"
        return normalized
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _parser() -> argparse.ArgumentParser:
    from hermes_cli.harness import build_parser

    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    return parser


def _run(parser: argparse.ArgumentParser, argv: list[str]) -> dict[str, Any]:
    args = parser.parse_args(argv)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = args.func(args)
    return {
        "argv": argv,
        "exit_code": exit_code,
        "stdout": _normalize(json.loads(stdout.getvalue())),
    }


def _write(name: str, payload: dict[str, Any]) -> None:
    (FIXTURE_ROOT / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_manifest() -> None:
    lines = [
        f"{hashlib.sha256((FIXTURE_ROOT / name).read_bytes()).hexdigest()}  {name}"
        for name in FIXTURE_CASES
    ]
    (FIXTURE_ROOT / "MANIFEST.sha256").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def main() -> int:
    FIXTURE_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="hermes-response-fixtures-", ignore_cleanup_errors=True
    ) as temp:
        isolated_root = Path(temp)
        hermes_home = isolated_root / "hermes"
        runtime_root = isolated_root / "runtime"
        hermes_home.mkdir()
        runtime_root.mkdir()
        os.environ["HERMES_HOME"] = str(hermes_home)
        os.environ["HERMES_HEAD_HOME"] = str(hermes_home)
        os.environ["HERMES_AGENT_RUNTIME_ROOT"] = str(runtime_root)
        os.environ["LOCALAPPDATA"] = str(isolated_root / "local")

        parser = _parser()
        for name, argv in FIXTURE_CASES.items():
            _write(name, _run(parser, argv))
        _write_manifest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
