"""Emit the builtin toolset manifest — every tool name and the toolset it joins.

WHY AN IN-TREE ARTIFACT AND NOT A CACHE
---------------------------------------
Answering "which toolset is this tool in" costs, today, an import of
``model_tools``, whose module scope imports all 38 registrar modules under
``tools/``. Measured on this checkout with the AST verdict cache warm: **3.16 s**,
paid on every cold ``perform_agent_create`` and on every single-test run.

There already IS a cache for the cheaper half of that work — the per-file
``registers tools?`` verdicts, memoized on ``(mtime_ns, size)`` under
``get_hermes_home()/cache``. It cannot help here, and the reason is structural
rather than a tuning miss: ``scripts/run_tests.sh`` points the home at a FRESH
TEMP DIRECTORY per test file, so a home-keyed memo is cold in the suite by
construction. A committed artifact is cold nowhere.

WHAT IT IS AND IS NOT
---------------------
It is the BUILTIN name/toolset map, read statically by
``tools.registry.scan_registered_tools``. It is not a handler table — a handler is
a live callable and there is no static substitute for importing the module that
defines it. It is also not the whole live registry: plugin tools register into the
same singleton through ``hermes_cli.plugins`` and MCP tools through
``discover_mcp_tools``, and neither is in this tree. A caller that needs the
complete set unions this with the registry it has actually populated.

THE GATE
--------
``--check`` fails when the committed artifact disagrees with a fresh scan, so a
new tool file is a visible manifest diff rather than a silent divergence. The
OTHER half of the gate — that the static read agrees with what the imports really
register — lives in ``tests/tools/test_toolset_manifest.py``, which pays the
3.16 s once so nothing else has to.

CONTRACT
--------
stdout is exactly one JSON object; every diagnostic goes to stderr. Writing is
opt-in (``--write``); ``--check`` compares and never writes.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.registry import scan_registered_tools  # noqa: E402

#: Bumped when the SHAPE moves, never when a tool is added or removed.
MANIFEST_SCHEMA_VERSION = 1

DEFAULT_ARTIFACT = Path("tools/toolset_manifest.json")


def build_manifest() -> dict:
    """The artifact's content, deterministic for a given tree.

    No timestamp and no machine identity anywhere in it, deliberately: an
    artifact that embedded either would diff on every regeneration and the gate
    would teach people to ignore it. Sorted throughout for the same reason —
    ``glob`` order is not a contract, and two checkouts must render one byte
    sequence.
    """

    scan = scan_registered_tools()
    if scan.unresolved:
        raise SystemExit(
            "REFUSING to write a short manifest: "
            + "; ".join(scan.unresolved)
            + "\n  A registration this reader cannot resolve statically must be "
            "made resolvable (a literal, or a module-level string constant) "
            "before the artifact can claim to be complete."
        )
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "tools": {name: scan.tools[name] for name in sorted(scan.tools)},
        "modules": {
            module: sorted(names) for module, names in sorted(scan.modules.items())
        },
    }


def render(manifest: dict) -> str:
    return json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the artifact in place instead of printing it",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when the committed artifact has drifted",
    )
    parser.add_argument(
        "--artifact",
        default=str(DEFAULT_ARTIFACT),
        help="path to the committed manifest (default: %(default)s)",
    )
    args = parser.parse_args()

    rendered = render(build_manifest())
    artifact = REPO_ROOT / args.artifact

    if args.write:
        artifact.parent.mkdir(parents=True, exist_ok=True)
        with open(artifact, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
        print(f"wrote {args.artifact}", file=sys.stderr)
        return 0

    if not args.check:
        sys.stdout.write(rendered)
        return 0

    if not artifact.exists():
        print(
            f"TOOLSET MANIFEST MISSING: {args.artifact}\n"
            "  Generate:  python scripts/dump_toolset_manifest.py --write",
            file=sys.stderr,
        )
        return 1

    with open(artifact, "r", encoding="utf-8", newline="") as handle:
        committed = handle.read()

    if committed == rendered:
        manifest = json.loads(rendered)
        print(
            f"toolset manifest fresh: {len(manifest['tools'])} tools across "
            f"{len(set(manifest['tools'].values()))} toolsets, "
            f"{len(manifest['modules'])} registrar modules",
            file=sys.stderr,
        )
        return 0

    diff = list(
        difflib.unified_diff(
            committed.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            fromfile=f"{args.artifact} (committed)",
            tofile="live static scan of tools/",
            n=2,
        )
    )
    print(
        "TOOLSET MANIFEST DRIFT -- the tools/ tree moved and the committed "
        "artifact did not.\n",
        file=sys.stderr,
    )
    sys.stderr.writelines(diff[:400])
    if len(diff) > 400:
        print(f"  ... {len(diff) - 400} more diff lines", file=sys.stderr)
    print(
        "\n  Regenerate:  python scripts/dump_toolset_manifest.py --write\n"
        "\n  Then READ the diff. A tool that LEFT a toolset is a persona that\n"
        "  silently lost a capability, and a tool that arrived in one is a\n"
        "  persona that silently gained it -- neither is a fixture update.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
