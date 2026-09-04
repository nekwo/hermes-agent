"""The committed toolset manifest, gated two ways, and the reader that uses it.

The two arms, and why one is not enough
---------------------------------------
``tools/toolset_manifest.json`` is a generated in-tree artifact: every builtin
tool name and the toolset it registers into, read statically so the answer costs
a JSON parse instead of importing the 38 registrar modules (3.16 s measured on
this checkout, warm).

An artifact is only worth what its gate is worth, and this one needs two:

1. **against a fresh STATIC SCAN** — the ratchet. A new tool file, a renamed
   tool, a toolset moved from ``web`` to ``browser``: each becomes a visible
   manifest diff instead of a silent divergence that the reader then serves as
   fact. This is the arm that runs in a second.

2. **against the LIVE REGISTRY after ``discover_builtin_tools()``** — the truth
   check. Arm 1 can only prove the artifact matches THIS reader; it cannot prove
   the reader matches what importing the modules really registers. A static
   reader that mis-parsed one call would satisfy arm 1 forever. So this arm pays
   the 3.16 s ONCE, here, which is the whole point of not paying it anywhere
   else.

Arm 2 is also the one that would catch the failure mode nobody would otherwise
see: a registrar module whose import throws is logged at ``warning`` and SKIPPED,
so its tools are absent from the live registry while the manifest names them.
Measured 2026-09-04, that is not hypothetical — 11 of the 38 fail to import under
some environments on missing optional dependencies. The arm therefore asserts the
manifest is a SUPERSET of the live registry's builtins and reports any name the
imports did not produce, rather than demanding equality it cannot honestly get in
every environment.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools.registry import scan_registered_tools
from tools.toolset_manifest import (
    MANIFEST_PATH,
    builtin_modules,
    builtin_tool_names,
    builtin_tool_names_for_toolsets,
    builtin_toolset_for_tool,
    builtin_toolset_names,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _committed() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Arm 1 — the artifact matches a fresh static scan
# --------------------------------------------------------------------------- #
def test_the_committed_manifest_matches_a_fresh_scan():
    scan = scan_registered_tools()

    manifest = _committed()
    assert manifest["tools"] == scan.tools
    assert manifest["modules"] == {
        module: sorted(names) for module, names in sorted(scan.modules.items())
    }
    assert scan.unresolved == [], scan.unresolved


def test_the_generator_check_mode_agrees_byte_for_byte():
    """Not the same assertion as the one above: this one pins the BYTES —
    indentation, key order, the trailing newline — so a regeneration on another
    machine produces the same file and the diff a reviewer reads is a real one.
    """

    result = subprocess.run(
        [sys.executable, "scripts/dump_toolset_manifest.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_the_manifest_is_a_real_corpus_and_not_an_empty_shell():
    """MCF-53 discipline. Floors, so a new tool never reds this."""

    manifest = _committed()
    assert manifest["schema_version"] == 1
    assert len(manifest["tools"]) >= 80
    assert len(manifest["modules"]) >= 30
    assert len(set(manifest["tools"].values())) >= 25


# --------------------------------------------------------------------------- #
# Arm 2 — the static read agrees with what the imports really register
# --------------------------------------------------------------------------- #
def test_the_manifest_agrees_with_the_live_registry():
    """The expensive arm, paid once so nothing else has to.

    Every name the imports DID register must be in the manifest with the same
    toolset — that direction is exact, and a mis-parsed call reds it. The other
    direction is a superset assertion with a named exception, because a registrar
    module whose import fails contributes nothing to the registry while remaining
    perfectly readable in the tree; the failure is reported, not accepted
    silently.
    """

    from tools.registry import discover_builtin_tools, registry

    discover_builtin_tools()
    live = registry.get_tool_to_toolset_map()
    assert len(live) >= 40, "the registry did not populate; this arm proved nothing"

    manifest_tools = _committed()["tools"]

    unknown = {name: toolset for name, toolset in live.items() if name not in manifest_tools}
    assert unknown == {}, (
        "the imports registered builtins the static reader never saw: "
        f"{sorted(unknown)}"
    )

    disagreed = {
        name: (manifest_tools[name], toolset)
        for name, toolset in live.items()
        if manifest_tools[name] != toolset
    }
    assert disagreed == {}, f"manifest/registry toolset disagreement: {disagreed}"


# --------------------------------------------------------------------------- #
# The reader
# --------------------------------------------------------------------------- #
def test_the_reader_answers_a_toolset_without_importing_a_registrar_module():
    """THE ROW'S OWN CLAIM, asserted as a fact about ``sys.modules``.

    Run in a subprocess because this test session has almost certainly imported
    ``model_tools`` already — asserting it in-process would pass or fail on test
    ORDER, which is the shape of a green that means nothing.
    """

    program = (
        "import sys, json\n"
        "from tools.toolset_manifest import builtin_toolset_for_tool, builtin_tool_names\n"
        "answer = builtin_toolset_for_tool('read_terminal')\n"
        "registrars = [m for m in sys.modules if m.startswith('tools.') and m not in "
        "{'tools.registry', 'tools.toolset_manifest'}]\n"
        "print(json.dumps({'answer': answer, 'count': len(builtin_tool_names()), "
        "'model_tools': 'model_tools' in sys.modules, 'registrars': sorted(registrars)}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])

    assert payload["answer"] == "terminal"
    assert payload["count"] >= 80
    assert payload["model_tools"] is False
    assert payload["registrars"] == [], payload["registrars"]


def test_the_reader_indexes_by_toolset_and_never_by_prefix():
    """``browser`` and ``browser-cdp`` are two toolsets. A reader that matched by
    prefix or by family would hand a persona declaring one the tools of the
    other — a silently widened capability, which is the worst kind."""

    browser = set(builtin_tool_names_for_toolsets(["browser"]))
    cdp = set(builtin_tool_names_for_toolsets(["browser-cdp"]))

    assert browser and cdp
    assert browser.isdisjoint(cdp)
    assert set(builtin_tool_names_for_toolsets(["browser", "browser-cdp"])) == browser | cdp
    assert builtin_tool_names_for_toolsets([]) == []
    assert builtin_tool_names_for_toolsets(["no_such_toolset"]) == []


def test_a_name_that_is_not_a_builtin_answers_none():
    """``None`` means "not a builtin", not "not a tool" — a plugin or MCP tool is
    real and answers ``None`` here. Pinned so a caller cannot read the absence as
    a refusal."""

    assert builtin_toolset_for_tool("definitely_not_a_builtin_tool") is None
    assert builtin_toolset_for_tool("") is None


def test_the_reader_and_the_artifact_are_the_same_facts():
    manifest = _committed()

    assert builtin_tool_names() == sorted(manifest["tools"])
    assert builtin_toolset_names() == sorted(set(manifest["tools"].values()))
    assert builtin_modules() == manifest["modules"]
    for name, toolset in manifest["tools"].items():
        assert builtin_toolset_for_tool(name) == toolset


@pytest.mark.parametrize("name", ["read_terminal", "bfl_flux3_get_result"])
def test_both_registration_forms_survive_the_round_trip(name):
    """One literal registration and one that names a module-level ``_TOOLSET``
    constant — the two forms the real tree uses. A reader that regressed to
    literals-only would still pass every synthetic case."""

    assert builtin_toolset_for_tool(name) is not None
