"""The env sandbox, asserted from INSIDE a test rather than read off a list.

``tests/conftest.py::_hermetic_environment`` deletes every name in
``_HERMES_BEHAVIORAL_VARS`` before each test. That fixture is the only thing
standing between the suite and the operator's shell, and its failure mode is
silent by construction: a variable that is missing from the frozenset does not
raise, it just makes the suite take a different branch on one machine than on
another, with everything green on both.

That is not hypothetical. ``HERMES_HEAD_HOME`` sat outside the set until
``5a0db7bad3``, and a suite run from a Launcher-shaped shell wrote test rows into
the operator's live profile. ``HERMES_GATEWAY_DETACHED`` sat outside it until
this file landed, and it is strictly a branch selector:
``hermes_cli/gateway.py:1737`` short-circuits on it and never reaches the
``isatty()`` fallback, so ``gateway.py:4924`` installed ``SIG_IGN`` and
``SetConsoleCtrlHandler(NULL, TRUE)`` in whichever direction the operator's shell
happened to point.

=============================================================================
WHY A RUNTIME ASSERTION, AND NOT AN ASSERTION ABOUT THE FROZENSET
=============================================================================
A test that asserted ``"HERMES_GATEWAY_DETACHED" in _HERMES_BEHAVIORAL_VARS``
would be checking that a list contains a string. It would pass just as happily
if the fixture that consumes the list were deleted, reordered behind another
autouse fixture, or changed from ``delenv`` to ``setenv``. What has to be true is
a property of the RUNNING test: at the moment a test body executes, these names
must not be visible in ``os.environ``. So that is what is driven, through the
same ``os.environ`` the production readers call ``os.getenv`` on.

The three checks below fail in three different directions on purpose, because
each one alone would be satisfiable while the guard was hollow:

* :func:`test_the_leak_prone_var_is_not_visible_to_a_running_test` is the gate.
  Export any of these names and it goes red naming the variable. But on a
  machine that never exports them it would pass even with the fixture deleted;
* :func:`test_every_guarded_var_is_actually_in_the_blanking_set` states WHY the
  name is absent — because the fixture is told to delete it — so a hollowed-out
  frozenset is red everywhere, not only where the var happens to be set;
* :func:`test_each_guarded_var_is_witnessed_on_a_live_production_reader` states
  why the entry is worth having at all. A blanked variable that nothing reads
  any more is decoration, and a list nobody prunes is how the next reader gets
  added without an entry.

DELETED, NOT PINNED. Following the ``HERMES_HEAD_HOME`` ruling: unset is what CI
has, and every reader below degrades to a sandboxed default when the variable is
ABSENT. A pin would be a second authority that drifts from the first —
``HERMES_BIN=<tmpdir>/hermes`` is still a path claiming an executable that is not
there, and it is strictly worse than no answer.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tests.conftest import _HERMES_BEHAVIORAL_VARS


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


#: name -> (module that READS it, why the read makes the variable load-bearing).
#:
#: The module is the witness. Every entry claims "production picks a branch or a
#: filesystem path from this", and that claim is driven below against the named
#: module's AST rather than trusted — an entry whose reader has gone is an
#: exemption for a hazard that no longer exists, which is how these lists rot.
LEAK_PRONE_VARS: dict[str, tuple[str, str]] = {
    "HERMES_GATEWAY_DETACHED": (
        "hermes_cli/gateway.py",
        ":1737 short-circuits the isatty() fallback on it; :4924 then installs "
        "SIG_IGN for SIGINT/SIGBREAK and SetConsoleCtrlHandler(NULL, TRUE)",
    ),
    "HERMES_HEAD_HOME": (
        "hermes_constants.py",
        ":107 returns it verbatim and it OUTRANKS the sandboxed HERMES_HOME; it "
        "selects the SessionDB the transcript store WRITES to, and :130 flips "
        "hermes_head_home_is_authoritative() with it",
    ),
    "HERMES_PROFILE": (
        "agent_runtime/profile_context.py",
        ":129 resolves the active profile from it; hermes_cli/kanban_db.py:9450 "
        "and tools/kanban_tools.py:877 default the author name to it",
    ),
    "HERMES_AUTH_HOME": (
        "hermes_constants.py",
        ":109 is the ONE reader of this authority — `get_hermes_auth_home()`, "
        "context-local override first and this env var second. "
        "`hermes_cli/auth.py::_global_auth_file_path` took it as the explicit "
        "per-profile credential root out of raw `os.environ` until "
        "`e567a9ff00` routed it through here, which is why the witness had to "
        "be repointed rather than the name dropped",
    ),
    "HERMES_SHARED_AUTH_DIR": (
        "hermes_cli/auth.py",
        ":5137 overrides the shared-secret directory the auth resolver reads",
    ),
    "HERMES_OPTIONAL_SKILLS": (
        "hermes_constants.py",
        ":318 overrides the optional-skill discovery root",
    ),
    "HERMES_OPTIONAL_MCPS": (
        "hermes_constants.py",
        ":334 overrides the optional-MCP catalog root",
    ),
    "HERMES_BUNDLED_SKILLS": (
        "hermes_constants.py",
        ":350 overrides the bundled-skill discovery root",
    ),
    "HERMES_SHARED_SKILLS": (
        "hermes_constants.py",
        ":380 overrides the shared-skill root — and three test files already pop "
        "it by hand (test_realm_sync_skill_inbox.py:56, test_skill_promotion.py:35, "
        "tests/agent/test_external_skills.py:88), which is the argument for "
        "blanking it centrally instead",
    ),
    "HERMES_BUNDLED_PLUGINS": (
        "hermes_cli/plugins.py",
        ":62 overrides the bundled-plugin discovery root",
    ),
    "HERMES_BIN": (
        "hermes_cli/kanban_db.py",
        ":9258 selects the `hermes` executable dispatched workers are spawned with",
    ),
    "HERMES_TUI_DIR": (
        "hermes_cli/main.py",
        ":1973 makes _make_tui_argv prefer a prebuilt bundle over the source tree",
    ),
    "HERMES_WEB_DIST": (
        "hermes_cli/main.py",
        ":10219/:10380 select the dashboard dist dir that `hermes dashboard` "
        "validates and serves",
    ),
    "HERMES_REAL_HOME": (
        "hermes_constants.py",
        ":871 selects the home an ACP child process inherits",
    ),
}


def _env_names_read_by(module_path: Path) -> set[str]:
    """Every string literal used as an env-var key in ``module_path``.

    AST-based, not substring: the names appear in prose in docstrings all over
    this tree (``hermes_constants.py:332`` mentions HERMES_OPTIONAL_MCPS one line
    above the read), and a witness that a comment can satisfy is not a witness.
    Covers the three read idioms in this repo: ``os.getenv("X")``,
    ``os.environ.get("X")`` / ``env.get("X")``, and ``os.environ["X"]``.
    """

    tree = ast.parse(module_path.read_text(encoding="utf-8", errors="replace"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"getenv", "get"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
        elif isinstance(node, ast.Compare):
            # ``"X" in os.environ`` — a membership test IS a read.
            for operand in (node.left, *node.comparators):
                if isinstance(operand, ast.Constant) and isinstance(operand.value, str):
                    names.add(operand.value)
    return names


@pytest.mark.parametrize("name", sorted(LEAK_PRONE_VARS))
def test_the_leak_prone_var_is_not_visible_to_a_running_test(name: str):
    """THE GATE, and it reads the same ``os.environ`` production reads.

    Export any of these in the shell that runs pytest and this goes red, naming
    the variable — which is exactly the mutation the fixture exists to survive.
    """

    _, why = LEAK_PRONE_VARS[name]
    assert name not in os.environ, (
        f"{name} leaked into a test from the ambient environment ({why}). It "
        "must be listed in tests/conftest.py::_HERMES_BEHAVIORAL_VARS so "
        "_hermetic_environment deletes it. Do not re-PIN it to a placeholder: "
        "unset is what CI has, and unset is what degrades to the sandboxed "
        "default."
    )


def test_every_guarded_var_is_actually_in_the_blanking_set():
    """Anti-vacuity for the gate above.

    ``os.environ`` not containing a name proves nothing on a machine where the
    name was never set — the gate above would pass forever on CI even if the
    fixture had stopped deleting anything. This half states WHY it is absent.
    """

    missing = sorted(name for name in LEAK_PRONE_VARS if name not in _HERMES_BEHAVIORAL_VARS)
    assert missing == [], (
        f"{missing} are guarded here but not blanked by _hermetic_environment. "
        "The absence assertion above would then be passing by accident on any "
        "machine that happens not to export them."
    )


@pytest.mark.parametrize("name", sorted(LEAK_PRONE_VARS))
def test_each_guarded_var_is_witnessed_on_a_live_production_reader(name: str):
    """The entry's own premise, asserted rather than trusted.

    Blanking a variable nothing reads costs nothing and proves nothing, so an
    entry with no reader left is decoration — and a list carrying decoration is
    one nobody prunes, which is how the NEXT reader gets added without an entry.
    If a reader is genuinely retired, drop the name from both this map and
    ``_HERMES_BEHAVIORAL_VARS`` in the commit that retires it.
    """

    module_name, why = LEAK_PRONE_VARS[name]
    module = _repo_root() / module_name
    assert module.is_file(), f"{module_name} no longer exists; the entry for {name} is stale"
    assert name in _env_names_read_by(module), (
        f"{module_name} no longer reads {name} as an env var ({why}). Either the "
        "reader moved — repoint this entry at it — or it was retired, in which "
        "case drop the name from _HERMES_BEHAVIORAL_VARS too."
    )


def test_the_witness_rejects_a_mere_mention():
    """RED-PROOF for the witness, run permanently.

    The failure mode of a name-based witness is that a docstring satisfies it.
    ``hermes_constants.py:332`` names HERMES_OPTIONAL_MCPS in prose one line
    above the real read, so this is not a theoretical hole in this tree.
    """

    read_only_in_prose = ast.parse(
        '"""Set HERMES_OPTIONAL_MCPS to override."""\n'
        'HERMES_BIN = "not an env read"\n'
        'log("HERMES_TUI_DIR is unset")\n'
    )
    # The helper works on a path, so drive its core the same way it does.
    names: set[str] = set()
    for node in ast.walk(read_only_in_prose):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"getenv", "get"} and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
    assert names == set(), f"prose satisfied the witness: {names}"


def test_the_guarded_set_is_not_silently_shrinking():
    """A ratchet. Deleting a name from ``LEAK_PRONE_VARS`` is how this file would
    stop guarding without any test going red, so the count is stated once.

    Raise it when you add a var. Lowering it is the reviewed edit: say which
    production reader went away, in the commit that removes the reader.
    """

    assert len(LEAK_PRONE_VARS) == 14
