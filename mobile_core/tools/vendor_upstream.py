"""Generate the committed mobile-safe transport snapshot from upstream Hermes."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

PACKAGE_PREFIX = "hermes_mobile_core._vendor"

ALLOWLIST = (
    "agent/transports/__init__.py",
    "agent/transports/base.py",
    "agent/transports/types.py",
    "agent/transports/chat_completions.py",
    "agent/lmstudio_reasoning.py",
    "agent/moonshot_schema.py",
    "providers/base.py",
)

_DISCOVERY_BLOCK = re.compile(
    r"    try:\n        import agent\.transports\.(?:anthropic|codex|bedrock)  # noqa: F401"
    r"\n    except ImportError:\n        pass\n",
)

_PACKAGE_INITS = (
    "__init__.py",
    "agent/__init__.py",
    "providers/__init__.py",
    "tools/__init__.py",
)

_PROMPT_BUILDER_SHIM = '''"""Generated mobile shim; do not edit."""

DEVELOPER_ROLE_MODELS = ("gpt-5", "codex")
'''

_GEMINI_SHIM = '''"""Generated mobile shim; native Gemini is outside the MVP."""


def is_native_gemini_base_url(base_url):
    return False
'''

_TOOLS_REGISTRY_SHIM = '''"""Fail-loud mobile stub for the desktop tool registry."""

from hermes_mobile_core.exceptions import MobileUnsupported


def __getattr__(name):
    raise MobileUnsupported(
        f"Desktop tool registry access is unavailable in hermes-mobile-core: {name}"
    )
'''


def _rewrite(source: str, relative: str) -> str:
    if relative == "agent/transports/__init__.py":
        source = _DISCOVERY_BLOCK.sub("", source)
    source = source.replace("from agent.", f"from {PACKAGE_PREFIX}.agent.")
    source = source.replace("from providers.", f"from {PACKAGE_PREFIX}.providers.")
    source = source.replace("import agent.", f"import {PACKAGE_PREFIX}.agent.")
    return source


def _source_commit(repo_root: Path) -> str:
    # Stamp the newest commit which changed any allowlisted upstream source,
    # not the branch HEAD. Mobile-only commits would otherwise make a
    # committed stamp permanently stale immediately after it was committed.
    command = [
        "git",
        "-C",
        str(repo_root),
        "log",
        "-1",
        "--format=%H",
        "HEAD",
        "--",
        *ALLOWLIST,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode == 0:
        return result.stdout.strip()

    # A worktree created by Windows stores a Windows-absolute gitdir in its
    # .git file. The repository test wrapper runs under WSL, where Git cannot
    # resolve that pointer until it is translated to /mnt/<drive>/....
    pointer = repo_root / ".git"
    if pointer.is_file():
        value = pointer.read_text(encoding="utf-8").strip()
        if value.lower().startswith("gitdir:"):
            raw = value.split(":", 1)[1].strip().replace("\\", "/")
            match = re.match(r"^([A-Za-z]):/(.*)$", raw)
            git_dir = Path(f"/mnt/{match.group(1).lower()}/{match.group(2)}") if match else Path(raw)
            fallback = subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(git_dir),
                    "--work-tree",
                    str(repo_root),
                    "log",
                    "-1",
                    "--format=%H",
                    "HEAD",
                    "--",
                    *ALLOWLIST,
                ],
                capture_output=True,
                text=True,
            )
            if fallback.returncode == 0:
                return fallback.stdout.strip()
    raise RuntimeError(f"unable to resolve source commit: {result.stderr.strip()}")


def vendor(repo_root: Path, destination: Path) -> None:
    """Regenerate *destination* from the fixed allowlist and generated shims."""
    repo_root = repo_root.resolve()
    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    for relative in ALLOWLIST:
        source_path = repo_root / relative
        if not source_path.is_file():
            raise FileNotFoundError(f"vendored source is missing: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source_path.read_text(encoding="utf-8")
        target.write_text(_rewrite(content, relative), encoding="utf-8", newline="\n")

    for relative in _PACKAGE_INITS:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text('"""Generated vendor package."""\n', encoding="utf-8", newline="\n")

    generated = {
        "agent/prompt_builder.py": _PROMPT_BUILDER_SHIM,
        "agent/gemini_native_adapter.py": _GEMINI_SHIM,
        "tools/registry.py": _TOOLS_REGISTRY_SHIM,
    }
    for relative, content in generated.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")

    stamp = (
        f"source_commit={_source_commit(repo_root)}\n"
        "generator=mobile_core/tools/vendor_upstream.py\n"
        "api_mode=chat_completions\n"
    )
    (destination / "VENDOR_STAMP").write_text(stamp, encoding="utf-8", newline="\n")


def main() -> None:
    mobile_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=mobile_root.parent)
    parser.add_argument(
        "--destination",
        type=Path,
        default=mobile_root / "src" / "hermes_mobile_core" / "_vendor",
    )
    args = parser.parse_args()
    vendor(args.repo_root, args.destination)
    print(f"Vendored {len(ALLOWLIST)} upstream modules at {_source_commit(args.repo_root)}")


if __name__ == "__main__":
    main()
