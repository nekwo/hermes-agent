"""BW-H3: no hermes process pays a tool/plugin discovery walk it did not ask for.

``agent_runtime/tool_visibility.py`` used to carry a module-scope
``from model_tools import get_toolset_for_tool``, and that one line was the most
expensive statement in a hermes boot. ``model_tools`` is not a passive module: its
module scope runs ``discover_builtin_tools()`` (importing every module under
``tools/``) and then ``discover_plugins()`` (a walk over 54 plugin manifests). And
this module is reachable at import time from ``hermes_cli.harness``, which
``hermes_cli.main`` imports while assembling the top-level argument parser — so
EVERY ``hermes`` invocation paid the whole walk before parsing its own argv.

**The plan's own phrasing for this test would have been vacuous.** It said to
assert that "importing ``hermes_cli.main`` does not run plugin discovery" — but
measured, ``import hermes_cli.main`` takes 0.33 s and imports neither
``model_tools`` nor ``hermes_cli.plugins``. That assertion passes on the
UNMODIFIED code. The real chain is inside ``main()``, via the harness parser
build, so that is what these tests drive.

**Why subprocesses.** The property under test is about what a FRESH interpreter
imports. This test session has ``model_tools`` in ``sys.modules`` many times over
(five test modules import it at module scope), so an in-process ``sys.modules``
check would be meaningless — it would report the session's history, not the boot
path's behaviour.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Every module whose presence in a fresh interpreter's ``sys.modules`` proves the
#: discovery walk ran. ``model_tools`` is the module whose SCOPE performs it;
#: ``hermes_cli.plugins`` is what it reaches for on line 214.
_DISCOVERY_MODULES = ("model_tools", "hermes_cli.plugins")


def _run_probe(body: str, tmp_path: Path) -> dict:
    """Run ``body`` in a fresh interpreter rooted at the checkout; parse its JSON.

    The child gets its own ``HERMES_HOME`` under the test's tmp_path, so it cannot
    read or scaffold anything the operator owns.
    """

    script = (
        "import json, os, sys\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        + body
    )
    env = dict(os.environ)
    home = tmp_path / "probe_home"
    home.mkdir(parents=True, exist_ok=True)
    env["HERMES_HOME"] = str(home)
    env["HERMES_AGENT_RUNTIME_ROOT"] = str(home / "runtime")
    env.pop("HERMES_HEAD_HOME", None)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"probe failed:\n{proc.stdout}\n{proc.stderr}"
    payload = [
        line for line in proc.stdout.splitlines() if line.startswith('{"probe"')
    ]
    assert payload, f"probe printed no result:\n{proc.stdout}\n{proc.stderr}"
    return json.loads(payload[-1])


# ---------------------------------------------------------------------------
# The import path
# ---------------------------------------------------------------------------


def test_registering_the_harness_parser_does_not_run_tool_discovery(tmp_path):
    """The chain that actually cost the boot, broken.

    Anti-vacuity. *Mutation:* restore the module-scope ``from model_tools import
    get_toolset_for_tool`` in ``agent_runtime/tool_visibility.py``. *Probed
    field:* ``model_tools`` and ``hermes_cli.plugins`` absent from a FRESH
    interpreter's ``sys.modules`` after the harness parser has been built. *Why
    the mutation cannot also satisfy it:* the module cannot be in ``sys.modules``
    without having executed, and executing it is what runs the walk — there is no
    third state. Deliberately not a timing assertion: an elapsed-ms threshold
    would pass under the mutation on a warm cache and fail spuriously on a loaded
    machine.

    This also covers the whole chain rather than one edge: any FUTURE module-scope
    ``model_tools`` import anywhere reachable from ``hermes_cli.harness`` reddens
    this test, which the structural gate below cannot do.
    """

    result = _run_probe(
        "import argparse\n"
        "import hermes_cli.main  # the entry-point module\n"
        "from hermes_cli.harness import build_parser\n"
        "parser = argparse.ArgumentParser()\n"
        "build_parser(parser.add_subparsers(dest='command'))\n"
        "print(json.dumps({'probe': 'import_path',\n"
        "  'loaded': [m for m in ('model_tools', 'hermes_cli.plugins')\n"
        "             if m in sys.modules]}))\n",
        tmp_path,
    )

    assert result["loaded"] == [], (
        "the harness parser build imported "
        f"{result['loaded']} — the discovery walk is back on the import path"
    )


def test_tool_visibility_has_no_module_scope_model_tools_import():
    """The structural gate, aimed straight at the one line that regressed.

    Anti-vacuity. *Mutation:* restore the import. *Probed field:* the AST of the
    file's own module scope. This is the CHEAP, precise witness (no subprocess) and
    it is deliberately paired with the behavioural one above, because each catches
    what the other cannot: this one names the exact line and would still fire if
    the harness chain were refactored around it, while the subprocess test catches
    the same cost arriving through a different module.
    """

    source = (PROJECT_ROOT / "agent_runtime" / "tool_visibility.py").read_bytes()
    tree = ast.parse(source.decode("utf-8"))
    offenders = [
        node.module or ""
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for _ in [0]
        if (
            (isinstance(node, ast.ImportFrom) and (node.module or "") == "model_tools")
            or (
                isinstance(node, ast.Import)
                and any(alias.name == "model_tools" for alias in node.names)
            )
        )
    ]
    assert offenders == [], (
        "agent_runtime/tool_visibility.py imports model_tools at module scope; "
        "that import runs discover_builtin_tools() + discover_plugins() and is "
        "reachable from hermes_cli.main's parser build (BW-H3)"
    )


# ---------------------------------------------------------------------------
# First use still works — and this is the half that nearly shipped broken
# ---------------------------------------------------------------------------


def test_the_first_visibility_resolve_populates_the_registry_and_returns_tools(
    tmp_path,
):
    """Deferral must not silently answer "this persona has no tools".

    This is the bug the first draft of BW-H3 had, and it is worth the subprocess.
    Importing ``model_tools`` is also what REGISTERS the builtin tools into the
    ``tools.registry`` singleton ``tool_visibility`` holds. A reader that touched
    ``registry.get_all_tool_names()`` before that import would read an EMPTY
    registry, memoise the empty answer for the process's lifetime, and raise
    nothing — a wrong answer, not a failure.

    Anti-vacuity. *Mutation:* drop the ``_ensure_tool_registry_populated()`` call
    at the top of ``_cached_tool_names_for_toolsets`` and go back to calling the
    lazy lookup inside the comprehension. *Probed field:* the LENGTH of the
    returned tool tuple in a fresh interpreter where ``model_tools`` was never
    imported. Under the mutation ``get_all_tool_names()`` returns ``[]``, the
    comprehension never runs, the lookup is never called, and the result is empty
    — so the mutant is killed by the tools it fails to return, not by any flag or
    counter it could also set. Second, independent witness in the same case:
    ``model_tools`` IS in ``sys.modules`` afterwards, which pins that the deferral
    resolves on first use rather than never.
    """

    result = _run_probe(
        "from agent_runtime import tool_visibility\n"
        "before = [m for m in ('model_tools',) if m in sys.modules]\n"
        "names = tool_visibility._cached_tool_names_for_toolsets(\n"
        "    ('file',), ()\n"
        ")\n"
        "print(json.dumps({'probe': 'first_use',\n"
        "  'before': before,\n"
        "  'after': [m for m in ('model_tools',) if m in sys.modules],\n"
        "  'count': len(names),\n"
        "  'sample': sorted(names)[:5]}))\n",
        tmp_path,
    )

    assert result["before"] == [], "importing tool_visibility already loaded model_tools"
    assert result["after"] == ["model_tools"], "first use never imported model_tools"
    assert result["count"] > 0, (
        "the deferred registry read returned no tools for the 'file' toolset — "
        f"sample={result['sample']}"
    )


def test_the_toolset_lookup_shim_keeps_its_module_attribute_name(tmp_path):
    """``tool_visibility.get_toolset_for_tool`` survives as a callable attribute.

    It was a re-exported import and is now a shim; anything that referenced or
    patched the attribute must be unaffected. Asserted on a real answer, not on
    ``callable()``: a shim that resolves to the wrong function would pass the
    weaker check.
    """

    result = _run_probe(
        "from agent_runtime import tool_visibility\n"
        "from model_tools import get_toolset_for_tool as direct\n"
        "names = tool_visibility._cached_tool_names_for_toolsets(('file',), ())\n"
        "probe_name = sorted(names)[0]\n"
        "print(json.dumps({'probe': 'shim',\n"
        "  'name': probe_name,\n"
        "  'via_shim': tool_visibility.get_toolset_for_tool(probe_name),\n"
        "  'direct': direct(probe_name)}))\n",
        tmp_path,
    )

    assert result["via_shim"] == result["direct"]
    assert result["via_shim"] == "file", result


@pytest.mark.parametrize("entry", ["hermes_cli.harness", "hermes_cli.main"])
def test_neither_boot_entry_module_loads_model_tools_at_import(entry, tmp_path):
    """Both halves of the boot import, separately, so a regression names itself."""

    result = _run_probe(
        f"__import__({entry!r})\n"
        "print(json.dumps({'probe': 'entry',\n"
        "  'loaded': [m for m in ('model_tools', 'hermes_cli.plugins')\n"
        "             if m in sys.modules]}))\n",
        tmp_path,
    )

    assert result["loaded"] == [], f"importing {entry} loaded {result['loaded']}"


# ---------------------------------------------------------------------------
# The NAMES are not the VERDICT
# ---------------------------------------------------------------------------


def test_the_registered_toolset_names_cost_no_availability_probe(tmp_path):
    """``all_registered_toolsets`` wants a list of strings, not a verdict.

    ``get_available_toolsets()`` answers the same key set PLUS an ``available``
    boolean per toolset, and the boolean is the whole cost: it runs every
    toolset's ``check_fn`` — binaries probed, env read, external clients built.
    Measured on this checkout, first call, warm cache: **3.96 s**, on top of the
    4.92 s ``import model_tools`` itself. ``personas.all_registered_toolsets``
    paid all of it, and every ``perform_agent_create`` pays that function through
    the permission preview on the wire row it projects
    (``persona_instance_summary`` → ``apply_chat_lane_tool_scope``), which is why
    a single census test in a fresh interpreter cost what it did.

    The key sets are equal BY CONSTRUCTION — both are
    ``{entry.toolset for entry in <the same snapshot>}`` — so this is not a
    trade of accuracy for speed; it is not asking a question whose answer was
    thrown away.

    Anti-vacuity. *Mutation:* put ``get_available_toolsets().keys()`` back.
    *Probed field:* ``tools.registry.probe_rounds_this_thread()``, the runtime's
    own counter of availability passes that actually EXECUTED a ``check_fn`` —
    not a timing threshold, which would pass under the mutation on a warm TTL
    cache. Under the mutation the delta is one round per toolset. The same case
    asserts the names still arrive, so a "fix" that answers nothing at all does
    not pass either.
    """

    result = _run_probe(
        "from tools import registry as _registry\n"
        "from agent_runtime.personas import all_registered_toolsets\n"
        "before = _registry.probe_rounds_this_thread()\n"
        "names = all_registered_toolsets()\n"
        "after = _registry.probe_rounds_this_thread()\n"
        "print(json.dumps({'probe': 'toolset_names',\n"
        "  'rounds': after - before,\n"
        "  'count': len(names),\n"
        "  'sorted': names == sorted(names),\n"
        "  'sample': names[:4]}))\n",
        tmp_path,
    )

    assert result["count"] > 0, "the name list came back empty"
    assert result["sorted"], f"the names are not sorted: {result['sample']}"
    assert result["rounds"] == 0, (
        f"asking for {result['count']} toolset NAMES ran {result['rounds']} "
        "availability probe rounds"
    )
