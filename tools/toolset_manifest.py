"""Answer toolset NAMES from the committed artifact — importing no registrar module.

WHAT THIS BUYS
--------------
``tools/toolset_manifest.json`` is a generated in-tree artifact (see
``scripts/dump_toolset_manifest.py``) holding every builtin tool name and the
toolset it registers into. Reading it answers the two questions almost every
name-only caller actually asks —

    which toolset is ``read_terminal`` in?
    what tools does ``agent_chat`` hold?

— for the cost of one small JSON parse, where the live registry answers them by
importing ``model_tools``, whose module scope imports all 38 registrar modules
under ``tools/``. Measured on this checkout with the AST verdict cache warm, that
import is **3.16 s**, and it is paid on every cold ``perform_agent_create`` and
every single-test run (``scripts/run_tests.sh`` points ``get_hermes_home()`` at a
fresh temp directory per file, so the home-keyed memo that would otherwise absorb
it is cold by construction).

WHAT IT DELIBERATELY DOES NOT ANSWER — read before wiring this anywhere
-----------------------------------------------------------------------
**Handlers.** A handler is a live callable. There is no static substitute for
importing the module that defines it, and nothing here pretends otherwise.

**The complete live tool set.** Two populations register into the same
``tools.registry`` singleton and are not in this tree:

* PLUGIN tools, through ``hermes_cli.plugins`` — present in
  ``registry.get_all_tool_names()`` today only because ``model_tools``' module
  scope runs ``discover_plugins()`` right after ``discover_builtin_tools()``;
* MCP tools, through ``discover_mcp_tools`` at each entry point's own startup.

So :func:`builtin_tool_names` is a SUBSET of what a fully-warmed process holds,
and a caller that needs the whole answer must union it with the registry it has
actually populated. That union is now WIRED, and how it is spelled was ruled
2026-09-04 (R135.4): ``agent_runtime.tool_visibility`` answers its NAME questions
as ``manifest`` union ``registry after an explicit, idempotent
``discover_plugins()```, which restores the plugin population without importing a
single registrar module. Plugin discovery is therefore a thing that reader DOES,
not a side effect it inherits from importing ``model_tools``.

The second answer the switch changes is the deliberate one: 11 of the 38 registrar
modules fail to import under some environments (missing optional dependencies) and
their tools are absent from the live registry while this artifact names them. That
is RIGHT for a name question and wrong for a capability question, so the line is
drawn there — **nothing that decides whether a tool can RUN may read this module**.
"Can this handler be looked up on this box" goes to the live registry via
``tool_visibility._ensure_tool_registry_populated``, which exists for exactly that
and is documented as the capability door. A caller here is asking for names; say so
at the call site.

FRESHNESS
---------
The artifact is committed and gated two ways in ``tests/tools/test_toolset_manifest.py``:
against a fresh static scan (so a new tool file is a visible diff) and against the
LIVE registry after ``discover_builtin_tools()`` (so the static read cannot drift
from what the imports really do). This module therefore does no staleness check of
its own — a check that could only re-run the scan would give back the cost the
artifact exists to remove.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence

MANIFEST_PATH = Path(__file__).resolve().parent / "toolset_manifest.json"


@lru_cache(maxsize=1)
def _manifest() -> Dict[str, Dict]:
    """The parsed artifact.

    A MISSING or unreadable artifact is an empty manifest rather than an
    exception, and that is the one defensive choice in this file. Every caller of
    a name lookup is on a preview or a summary path — an agents drawer, a HUD
    strip, a persona's declared toolsets — and none of them should fail to render
    because a checkout is mid-regeneration. An empty answer is visibly empty; a
    traceback out of a drawer is not.
    """

    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"tools": {}, "modules": {}}
    if not isinstance(data, dict):
        return {"tools": {}, "modules": {}}
    tools = data.get("tools")
    modules = data.get("modules")
    return {
        "tools": tools if isinstance(tools, dict) else {},
        "modules": modules if isinstance(modules, dict) else {},
    }


@lru_cache(maxsize=1)
def _tools() -> Dict[str, str]:
    return dict(_manifest()["tools"])


def builtin_toolset_for_tool(name: str) -> Optional[str]:
    """The toolset one BUILTIN tool registers into, or ``None``.

    ``None`` means "not a builtin", which is not the same as "not a tool" — a
    plugin or MCP tool answers ``None`` here and is perfectly real. Callers that
    can see a non-builtin must say what they mean by the ``None``.
    """

    return _tools().get(name)


def builtin_tool_names() -> List[str]:
    """Every builtin tool name, sorted."""

    return sorted(_tools())


def builtin_tool_names_for_toolsets(toolsets: Sequence[str]) -> List[str]:
    """The builtin tools belonging to any of ``toolsets``, sorted.

    Membership, not prefix or family matching: ``browser`` and ``browser-cdp``
    are two toolsets and a caller asking for one must not be handed the other.
    """

    wanted = {str(toolset) for toolset in toolsets}
    return sorted(name for name, toolset in _tools().items() if toolset in wanted)


def builtin_toolset_names() -> List[str]:
    """Every toolset a builtin registers into, sorted."""

    return sorted(set(_tools().values()))


def builtin_modules() -> Dict[str, List[str]]:
    """``{module_stem: [tool_name, …]}`` — which file owns which name.

    The fact a debugger wants when a tool is missing at runtime: it names the
    module to check for an import failure, without importing it to find out.
    """

    return {module: list(names) for module, names in _manifest()["modules"].items()}
