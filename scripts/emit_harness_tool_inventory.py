"""Generate the harness manual's in-turn tool inventory FROM the registry.

WHY THIS EXISTS
---------------
`docs/agent-runtime-harness/harness-skills/harness-runtime-model/SKILL.md` is
preloaded into every mission-chat turn (`load_policy: required_preload`) — it is
the routing model every harness agent reads before it acts. On 2026-09-03 it
named exactly ONE in-turn tool (`agent_chat_send`) in its whole Operate table and
routed "find the instances to message" and "track follow-up work" to terminal
CLI calls that `agent_chat_threads` and `board_card_add` answer in-turn. A manual
that under-reports the agent's own hands is not a documentation problem: it is
the agent shelling out for something it can call, every turn, forever.

So the inventory is GENERATED from the live tool registry and gated, the same way
`scripts/dump_cli_contract.py` gates the CLI contract: the day a tool joins or
leaves `harness_core`, the manual goes red in the repo that moved.

CONTRACT
--------
Same shape as `dump_cli_contract.py`. stdout is the artifact (the SKILL.md
block), every diagnostic goes to stderr, writes are newline-explicit (``\n``)
because the artifacts are LF files compared byte-for-byte.

    python scripts/emit_harness_tool_inventory.py            # the SKILL.md block to stdout
    python scripts/emit_harness_tool_inventory.py --check    # gate: all three artifacts fresh
    python scripts/emit_harness_tool_inventory.py --write    # regenerate all three

THE THREE ARTIFACTS
-------------------
1. The `## In-turn tools` block inside SKILL.md, between the generated markers:
   ONE ROW PER TOOLSET (15 rows), not one per tool. SKILL.md is prompt weight the
   operator pays on every turn (R-S0a-5).
2. `references/tool-inventory.md` — the full table (one row per tool) WITH descriptions, read
   on demand.
3. `references/tool-inventory.json` — the machine copy. The launcher's Agent
   Command Atlas artifact regenerates its "full inventory" section from THIS file
   rather than from a hand-transcribed tool-diff.

Deterministic by construction: sorted, no timestamps, no environment in the
output. `gated` records that a tool HAS a `check_fn` (desktop/service gating),
never that check_fn's verdict — the verdict is machine-dependent and would make
the artifact unstable across boxes.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SKILL_DIR = Path("docs/agent-runtime-harness/harness-skills/harness-runtime-model")
SKILL_MD = SKILL_DIR / "SKILL.md"
INVENTORY_MD = SKILL_DIR / "references/tool-inventory.md"
INVENTORY_JSON = SKILL_DIR / "references/tool-inventory.json"

BEGIN_MARKER = "<!-- BEGIN GENERATED: harness_core inventory -->"
END_MARKER = "<!-- END GENERATED: harness_core inventory -->"

#: One line per member toolset, hand-kept HERE (not in the manual) so the
#: generated table can carry a "use it for" column without the emitter having to
#: invent prose from a tool description.
TOOLSET_PURPOSE = {
    "agent_chat": "teammates: list, message, read, dispatches, transcript path",
    "board": "record follow-up work — planning state only",
    "clarify": "ask the operator a question mid-turn",
    "delegation": "hand a bounded subtask to a helper with fresh context",
    "terminal": "run commands; the desktop pane verbs are GUI-gated",
    "file": "read, write, patch and search files",
    "web": "search the web and pull a page's content",
    "browser": "drive a real browser: navigate, click, type, read, screenshot",
    "browser-cdp": "raw CDP and dialog handling for the same browser",
    "skills": "find, read and author skills",
    "memory": "durable profile memory",
    "todo": "your own in-turn checklist",
    "session_search": "search your own past sessions",
    "vision": "analyze an image",
    "code_execution": "run code in the sandbox",
}

#: Operate-table rows that have NO in-turn tool — the CLI is genuinely the only
#: door. Hand-kept, and cross-checked below against the Operate table so a verb
#: that grows a tool (or loses its row) reds the gate.
CLI_ONLY_VERBS = (
    "persona instance open-chat",
    "mission-chat steer",
    "mission-chat turn-resolve",
    "mission-chat queue-skill",
    "persona instance return-summary",
    "persona instance steer",
    "flow set",
)

#: Tool names the manual is allowed to mention without them being registered:
#: names it names in order to say they are GONE (the "Removed — unlearn these"
#: section) or that belong to another surface (Stage C MCP).
MANUAL_NAME_EXEMPTIONS = frozenset(
    {
        "mission_goal_create",
        "mcp_launcher_qa_get_buttons",
        "mcp_launcher_qa_get_widget_state",
    }
)

_TOOLISH = re.compile(r"`([a-z][a-z0-9_]{2,})`")


def _declared_members() -> list[str]:
    from agent_runtime.personas import HARNESS_LANE_DEFAULT_TOOLSETS
    from toolsets import expand_toolset_names

    return list(expand_toolset_names(HARNESS_LANE_DEFAULT_TOOLSETS))


def collect(root: Path) -> dict[str, Any]:
    """The inventory, read off the live registry. Imports ``model_tools``."""

    import model_tools  # noqa: F401 - importing IS what populates the registry
    from agent_runtime.personas import REGISTRY_HYGIENE_BLOCKED_TOOLS
    from agent_runtime.tool_visibility import _estimate_model_tool_tokens, _mutating_tools
    from tools.registry import registry

    members = _declared_members()
    mutating = _mutating_tools()

    toolsets: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    for toolset in members:
        names = sorted(registry.get_tool_names_for_toolset(toolset))
        toolsets.append({"name": toolset, "tools": names})
        for name in names:
            entry = registry.get_tool(name) if hasattr(registry, "get_tool") else None
            entry = entry or registry._tools.get(name)  # noqa: SLF001 - no public single-entry read
            tools.append(
                {
                    "name": name,
                    "toolset": toolset,
                    "mutating": name in mutating,
                    "gated": getattr(entry, "check_fn", None) is not None,
                    "description": _one_line(getattr(entry, "description", "") or ""),
                }
            )

    hygiene = {tool["name"] for tool in tools} & set(REGISTRY_HYGIENE_BLOCKED_TOOLS)
    if hygiene:
        raise SystemExit(
            "REFUSED: the declared set resolves registry-hygiene tools "
            f"({', '.join(sorted(hygiene))}). An inventory that lists a withheld "
            "tool is worse than no inventory — fix the declaration, not this script."
        )

    tools.sort(key=lambda row: row["name"])
    return {
        "schema_version": 1,
        "declared": ["harness_core"],
        "toolsets": toolsets,
        "tools": tools,
        "counts": {
            "tools": len(tools),
            "toolsets": len(toolsets),
            "token_estimate": _estimate_model_tool_tokens([t["name"] for t in tools]),
        },
        "cli_only_verbs": list(CLI_ONLY_VERBS),
    }


def _one_line(text: str) -> str:
    collapsed = " ".join(str(text).split())
    if len(collapsed) > 160:
        collapsed = collapsed[:157].rstrip() + "..."
    return collapsed.replace("|", "\\|")


def render_skill_block(inventory: dict[str, Any]) -> str:
    lines = [
        BEGIN_MARKER,
        "",
        f"{inventory['counts']['tools']} tools · generated from the registry by "
        "`scripts/emit_harness_tool_inventory.py` · do not edit by hand. If a tool "
        "exists for it, the tool is the answer; the full table with descriptions is "
        "`references/tool-inventory.md`.",
        "",
        "| toolset | tools | use it for |",
        "|---|---|---|",
    ]
    for row in inventory["toolsets"]:
        names = " · ".join(f"`{name}`" for name in row["tools"]) or "—"
        purpose = TOOLSET_PURPOSE.get(row["name"], "")
        lines.append(f"| `{row['name']}` | {names} | {purpose} |")
    lines.extend(["", END_MARKER])
    return "\n".join(lines) + "\n"


def render_inventory_md(inventory: dict[str, Any]) -> str:
    counts = inventory["counts"]
    lines = [
        "# In-turn tools — the full inventory",
        "",
        "GENERATED by `scripts/emit_harness_tool_inventory.py` from the live tool "
        "registry. Do not edit by hand; run the emitter with `--write`.",
        "",
        f"`harness_core` = {counts['toolsets']} member toolsets, **{counts['tools']} "
        f"callable tools**, ~{counts['token_estimate']} model tool tokens "
        "(`tool_name_envelope_v1` heuristic, not a provider bill). This is what every "
        "Eternia persona's harness lane resolves unless its profile declares "
        "something else — see canon `05-chat-turn-lane.md` §4c.",
        "",
        "`gated` means the tool carries a `check_fn` (desktop/service availability), "
        "so it may be absent on a given box. `mutating` means it crosses the mutation "
        "boundary (`tool_permissions.READ_ONLY_BLOCKS`).",
        "",
        "| tool | toolset | mutating | gated | description |",
        "|---|---|---|---|---|",
    ]
    for row in inventory["tools"]:
        lines.append(
            f"| `{row['name']}` | `{row['toolset']}` | "
            f"{'yes' if row['mutating'] else '—'} | {'yes' if row['gated'] else '—'} | "
            f"{row['description']} |"
        )
    lines.extend(
        [
            "",
            "## CLI-only verbs",
            "",
            "Operate-table rows with no in-turn tool. Reaching for the terminal for "
            "anything NOT on this list is a navigation failure worth reporting.",
            "",
        ]
        + [f"- `hermes harness {verb}`" for verb in inventory["cli_only_verbs"]]
    )
    return "\n".join(lines) + "\n"


def render_inventory_json(inventory: dict[str, Any]) -> str:
    return json.dumps(inventory, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def _skill_text(root: Path) -> str:
    return (root / SKILL_MD).read_text(encoding="utf-8")


def splice_skill(root: Path, block: str) -> str:
    text = _skill_text(root)
    if BEGIN_MARKER not in text or END_MARKER not in text:
        raise SystemExit(
            f"REFUSED: {SKILL_MD} carries no generated markers. Add\n"
            f"  {BEGIN_MARKER}\n  {END_MARKER}\n"
            "inside a `## In-turn tools` section before `## Operate`."
        )
    head, _, rest = text.partition(BEGIN_MARKER)
    _, _, tail = rest.partition(END_MARKER)
    return head + block.rstrip("\n") + tail


def cross_check(root: Path, inventory: dict[str, Any]) -> list[str]:
    """Manual-vs-registry checks that no rendering can perform for itself."""

    problems: list[str] = []
    text = _skill_text(root)
    registered = {row["name"] for row in inventory["tools"]}

    from tools.registry import registry

    all_registered = set(registry.get_all_tool_names())

    # (1) every tool-shaped backtick name the manual mentions must exist.
    for name in sorted(set(_TOOLISH.findall(text))):
        if name in MANUAL_NAME_EXEMPTIONS or name in all_registered:
            continue
        if name.startswith(("agent_chat_", "board_")) or name in {
            "clarify",
            "delegate_task",
            "read_file",
            "search_files",
        }:
            problems.append(
                f"SKILL.md names `{name}`, which is not a registered tool — a rename "
                "left the manual pointing at nothing."
            )

    # (2) the tools the routing table promises must be in the declared set.
    for name in ("agent_chat_send", "agent_chat_threads", "board_card_add", "clarify"):
        if name not in registered:
            problems.append(
                f"`{name}` is routed by the Operate table but is not in harness_core."
            )

    # (3) every CLI-only verb must still have its Operate row.
    for verb in inventory["cli_only_verbs"]:
        if verb not in text:
            problems.append(
                f"`{verb}` is listed as CLI-only but has no Operate-table row — "
                "either the row was deleted or the verb grew a tool."
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="gate: artifacts are fresh")
    parser.add_argument("--write", action="store_true", help="regenerate the artifacts")
    parser.add_argument("--root", default=str(REPO_ROOT), help="repo root to read/write")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    inventory = collect(root)
    artifacts = {
        SKILL_MD: splice_skill(root, render_skill_block(inventory)),
        INVENTORY_MD: render_inventory_md(inventory),
        INVENTORY_JSON: render_inventory_json(inventory),
    }

    if not (args.check or args.write):
        sys.stdout.write(render_skill_block(inventory))
        return 0

    problems = cross_check(root, inventory)

    if args.write:
        for relative, rendered in artifacts.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(rendered)
            print(f"[inventory] wrote {relative}", file=sys.stderr)
        if problems:
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            return 1
        return 0

    failed = False
    for relative, rendered in artifacts.items():
        path = root / relative
        if not path.is_file():
            print(
                f"TOOL INVENTORY: {relative} is MISSING.\n"
                "  Regenerate:  python scripts/emit_harness_tool_inventory.py --write",
                file=sys.stderr,
            )
            failed = True
            continue
        with open(path, "r", encoding="utf-8", newline="") as handle:
            committed = handle.read()
        if committed == rendered:
            continue
        failed = True
        diff = list(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"{relative} (committed)",
                tofile="live registry",
                n=2,
            )
        )
        print(
            f"TOOL INVENTORY DRIFT — {relative} disagrees with the registry.\n",
            file=sys.stderr,
        )
        sys.stderr.writelines(diff[:200])
        if len(diff) > 200:
            print(f"  ... {len(diff) - 200} more diff lines", file=sys.stderr)

    for problem in problems:
        print(f"TOOL INVENTORY: {problem}", file=sys.stderr)
        failed = True

    if failed:
        print(
            "\n  Regenerate:  python scripts/emit_harness_tool_inventory.py --write\n"
            "  Then READ the diff. A tool that LEFT harness_core is not a manual\n"
            "  refresh — it is a capability every harness agent just lost.",
            file=sys.stderr,
        )
        return 1

    digest = hashlib.sha256(render_inventory_json(inventory).encode("utf-8")).hexdigest()
    print(
        f"tool inventory fresh: {inventory['counts']['tools']} tools across "
        f"{inventory['counts']['toolsets']} toolsets, sha256 {digest[:16]}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
