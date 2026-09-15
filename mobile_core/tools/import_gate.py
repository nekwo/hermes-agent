"""Static full-source import gate for the mobile package."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import NamedTuple


FORBIDDEN_PREFIXES = (
    "subprocess",
    "pty",
    "ptyprocess",
    "termios",
    "tty",
    "psutil",
    "fastapi",
    "uvicorn",
    "mcp",
    "agent_runtime",
    "harness",
    "mission_control",
    "worktree",
    "daemon",
    "stagec",
    "tools.lazy_deps",
    "tools.browser",
    "tools.terminal",
    "tools.file",
    "tools.code",
    "hermes_cli.auth",
    "hermes_cli.config",
    "dotenv",
)

FORBIDDEN_CALLS = {
    "exec",
    "eval",
    "__import__",
    "os.system",
    "os.popen",
    "importlib.import_module",
    "pip.main",
}


class Violation(NamedTuple):
    path: Path
    line: int
    detail: str


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _forbidden(module: str) -> bool:
    normalized = module.strip().lower()
    return any(
        normalized == prefix or normalized.startswith(prefix + ".")
        for prefix in FORBIDDEN_PREFIXES
    )


def scan(package_root: Path) -> list[Violation]:
    """Scan every Python file, including imports nested inside functions."""
    violations: list[Violation] = []
    for path in sorted(package_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                if _forbidden(module):
                    violations.append(Violation(path, node.lineno, f"forbidden import: {module}"))
            if isinstance(node, ast.Call):
                call_name = _dotted_name(node.func)
                if call_name in FORBIDDEN_CALLS:
                    violations.append(Violation(path, node.lineno, f"forbidden dynamic call: {call_name}"))
                if call_name in {"run", "Popen", "check_call", "check_output"}:
                    # Catch aliased subprocess entry points conservatively.
                    violations.append(Violation(path, node.lineno, f"forbidden process call: {call_name}"))
    return violations


def main() -> None:
    default = Path(__file__).resolve().parents[1] / "src" / "hermes_mobile_core"
    parser = argparse.ArgumentParser()
    parser.add_argument("package_root", nargs="?", type=Path, default=default)
    args = parser.parse_args()
    violations = scan(args.package_root)
    for item in violations:
        print(f"{item.path}:{item.line}: {item.detail}")
    raise SystemExit(1 if violations else 0)


if __name__ == "__main__":
    main()
