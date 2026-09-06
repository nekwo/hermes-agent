"""Importing ``hermes_cli.main`` is not "running hermes" — the gate that says so.

THE TWO MEASUREMENTS THIS PINS (both 2026-09-01, both in a pytest process).

1. The pre-parse ran from ``main.py``'s module scope and read whatever
   ``sys.argv`` the process had. Under pytest that argv is pytest's, so
   ``pytest ... -p markdump`` was read as ``--profile markdump``, resolved
   nothing, and called ``sys.exit(1)`` from inside a collection import. pytest
   reports that as a bare ``INTERNALERROR> SystemExit: 1`` with no line naming
   the cause; it was reproduced three times before it was understood, wearing
   the costume of a cross-directory test interaction.
2. With no ``-p`` at all, the same import read the OPERATOR's live
   ``<root>/active_profile`` and pointed the whole session's ``HERMES_HOME`` at
   their live profile. No fixture is active during collection, so the hermetic
   pins one layer below could not close that window, and any module-level cache
   seeded during collection kept the live root.

WHAT IS ASSERTED, AND WHY IT CANNOT BE VACUOUS. The two import probes run OUT OF
PROCESS with a fabricated hermes root, because the thing under test mutates the
importing process's ``os.environ`` and ``sys.argv``. Each is arranged so the
UNFIXED code produces a different observable than the fixed one:

* the exit probe names a profile that does not exist, so unfixed it exits 1 —
  the probe asserts a 0 return code and a printed payload, which a
  ``sys.exit(1)`` cannot produce;
* the repoint probe seeds a REAL profile behind ``active_profile``, so unfixed
  ``HERMES_HOME`` moves — the probe compares the value after the import against
  the value before it, neither of which the import writes when the gate holds;
* the entrypoint probe drives a real invocation shape (``argv[0]`` is the
  installed ``hermes`` script) and asserts the override still lands:
  ``HERMES_HOME`` on the profile, ``HERMES_PROFILE_RESOLUTION`` on ``flag``, and
  the flag stripped out of ``argv``. Without it the whole gate could be
  satisfied by never applying the override at all.

The console-script set is pinned against ``pyproject.toml``'s
``[project.scripts]`` rather than restated, so a fourth entry point cannot be
added without someone answering whether it needs the pre-parse.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli._profile_bootstrap import (
    HERMES_CONSOLE_SCRIPTS,
    entrypoint_name,
    is_hermes_cli_entrypoint,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SOURCE = PROJECT_ROOT / "hermes_cli" / "_profile_bootstrap.py"
MAIN_SOURCE = PROJECT_ROOT / "hermes_cli" / "main.py"


# ---------------------------------------------------------------------------
# The set, enumerated from the packaging metadata that creates it
# ---------------------------------------------------------------------------


def _declared_console_scripts() -> set[str]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        pytest.skip("tomllib is required to read pyproject.toml")
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(data["project"]["scripts"])


def test_the_allowlist_is_exactly_the_installed_console_scripts():
    """A new console script must be ruled on, not silently excluded.

    Both directions matter. A name in the allowlist that pip never installs is
    an entrypoint nobody can reach; a script pip installs that is missing from
    the allowlist loses the profile pre-parse the day it is added, which is a
    silent behaviour change on a real invocation.
    """

    assert set(HERMES_CONSOLE_SCRIPTS) == _declared_console_scripts()


@pytest.mark.parametrize(
    "argv0",
    [
        "/usr/local/bin/hermes",
        r"C:\venv\Scripts\hermes.exe",
        r"C:\venv\Scripts\hermes-script.py",
        "/nix/store/abc-hermes/bin/.hermes-wrapped",
        "hermes",
        "hermes-acp",
        "/usr/local/bin/hermes-agent",
    ],
)
def test_every_shipped_launcher_shape_is_recognised_as_the_cli(argv0):
    assert is_hermes_cli_entrypoint("hermes_cli.main", argv0=argv0) is True


@pytest.mark.parametrize(
    "argv0",
    [
        "/usr/local/bin/pytest",
        r"C:\venv\Scripts\pytest.exe",
        "/usr/lib/python3.12/site-packages/pytest/__main__.py",
        "-c",
        "",
        "/usr/local/bin/hermes-qa",
        "/usr/local/bin/nothermes",
    ],
)
def test_nothing_else_answers_true(argv0):
    assert is_hermes_cli_entrypoint("hermes_cli.main", argv0=argv0) is False


def test_running_the_module_as_main_is_the_dash_m_arm():
    """``python -m hermes_cli.main`` is how every gateway service is spawned.

    runpy executes the module AS ``__main__`` there, and ``argv[0]`` is the
    module file, so the caller's own ``__name__`` is the only honest signal —
    and one a test importing the module under its dotted name cannot produce.
    """

    assert is_hermes_cli_entrypoint("__main__", argv0="/usr/local/bin/pytest") is True


def test_entrypoint_name_strips_launcher_decoration_only():
    assert entrypoint_name(r"C:\venv\Scripts\hermes.exe") == "hermes"
    assert entrypoint_name("/nix/store/x/bin/.hermes-wrapped") == "hermes"
    # ...and does not chew into a genuinely different program's name.
    assert entrypoint_name("/usr/bin/hermes-qa") == "hermes-qa"


def test_the_directory_is_dropped_under_either_separator_on_either_host():
    """``argv[0]`` carries the syntax of the launcher that produced it, not of
    the host reading it.

    ``os.path.basename`` knows only the running host's separator, so on the
    Linux CI runners it handed the whole Windows path back and the gate
    compared ``c:\\venv\\scripts\\hermes`` against the console-script names —
    green on Windows, red on every runner since this file landed. The rule is
    that BOTH separators end a directory, whoever is asking.
    """

    assert entrypoint_name(r"C:\venv\Scripts\hermes.exe") == "hermes"
    assert entrypoint_name("C:/venv/Scripts/hermes.exe") == "hermes"
    assert entrypoint_name(r"C:\venv/Scripts\hermes.exe") == "hermes"
    assert entrypoint_name("/usr/local/bin/hermes") == "hermes"
    # A drive-relative argv0 carries no separator at all; ntpath.basename drops
    # the drive, so this keeps parity with it.
    assert entrypoint_name("C:hermes.exe") == "hermes"
    # Quoting and surrounding whitespace are still stripped before any of that.
    assert entrypoint_name('  "C:\\venv\\Scripts\\hermes.exe"  ') == "hermes"


# ---------------------------------------------------------------------------
# The module that owns the pre-parse does nothing at import
# ---------------------------------------------------------------------------


def test_the_bootstrap_module_executes_no_statement_at_import():
    """Structural, not behavioural: the module body may only DEFINE things.

    The defect was a module body that ran the pre-parse. Asserting "importing it
    was harmless this once" would pass against a body that calls something
    harmless today; asserting the body contains no call at all is the property.
    """

    tree = ast.parse(BOOTSTRAP_SOURCE.read_text(encoding="utf-8"))
    executed = [
        node
        for node in tree.body
        if not isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Assign,
                ast.AnnAssign,
            ),
        )
        and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    assert executed == [], [ast.dump(node) for node in executed]


def test_mains_module_scope_call_is_gated():
    """``main.py`` may not call the pre-parse unconditionally.

    Read off the module body rather than by importing: a source walk is the
    right instrument for "this must never be written", and importing ``main``
    to check its own import is circular.
    """

    tree = ast.parse(MAIN_SOURCE.read_text(encoding="utf-8"))
    bare_calls = [
        node.lineno
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "_apply_profile_override"
    ]
    assert bare_calls == [], bare_calls

    gated = [
        node.lineno
        for node in tree.body
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Call)
        and getattr(node.test.func, "id", None) == "_is_hermes_cli_entrypoint"
    ]
    assert len(gated) == 1, gated


# ---------------------------------------------------------------------------
# The real import, out of process, both directions
# ---------------------------------------------------------------------------


_IMPORT_PROBE = textwrap.dedent(
    """
    import json, os, sys
    sys.argv = json.loads(os.environ.pop("PROBE_ARGV"))
    sys.path.insert(0, os.environ["PROBE_ROOT"])
    before = os.environ.get("HERMES_HOME")
    import hermes_cli.main  # noqa: F401
    sys.stdout.write("PROBE" + json.dumps({
        "home_before": before,
        "home_after": os.environ.get("HERMES_HOME"),
        "resolution": os.environ.get("HERMES_PROFILE_RESOLUTION"),
        "argv": sys.argv,
    }) + chr(10))
    """
)


def _make_root(tmp_path: Path, profiles=(), active: str | None = None) -> Path:
    root = tmp_path / "hermesroot"
    (root / "profiles").mkdir(parents=True)
    for name in profiles:
        profile = root / "profiles" / name
        profile.mkdir()
        (profile / ".env").write_text("", encoding="utf-8")
    if active is not None:
        (root / "active_profile").write_text(active, encoding="utf-8")
    return root


def _run_import_probe(root: Path, argv: list[str]) -> dict:
    env = dict(os.environ)
    env["PROBE_ROOT"] = str(PROJECT_ROOT)
    env["PROBE_ARGV"] = json.dumps(argv)
    env["HERMES_HOME"] = str(root)
    env.pop("HERMES_PROFILE_RESOLUTION", None)
    env.pop("HERMES_S6_SUPERVISED_CHILD", None)
    proc = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        "importing hermes_cli.main must not terminate the process\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    payloads = [
        line[len("PROBE") :]
        for line in proc.stdout.splitlines()
        if line.startswith("PROBE")
    ]
    assert payloads, f"probe printed nothing\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    return json.loads(payloads[-1])


def test_a_pytest_shaped_argv_neither_exits_nor_is_read_as_a_profile(tmp_path):
    """Measurement 1: ``pytest ... -p <plugin>`` was consumed as ``--profile``.

    ``markdump`` is a legal profile NAME and no profile on disk, which is what
    made the unfixed path raise FileNotFoundError and ``sys.exit(1)`` out of a
    collection import. A zero return code with a payload is only reachable when
    the pre-parse never ran.
    """

    root = _make_root(tmp_path)
    result = _run_import_probe(
        root, ["/usr/local/bin/pytest", "-p", "markdump", "tests/hermes_cli"]
    )

    assert result["home_after"] == result["home_before"] == str(root)
    assert result["argv"] == [
        "/usr/local/bin/pytest",
        "-p",
        "markdump",
        "tests/hermes_cli",
    ]


def test_a_pytest_shaped_argv_does_not_repoint_hermes_home_at_the_sticky_profile(
    tmp_path,
):
    """Measurement 2: the sticky ``active_profile`` marker captured the session.

    The profile here EXISTS, so the unfixed import succeeds and silently moves
    ``HERMES_HOME`` to ``<root>/profiles/alice`` — the live-profile window, in
    miniature. Comparing after against before pins a value the import itself
    never writes when the gate holds.
    """

    root = _make_root(tmp_path, profiles=("alice",), active="alice")
    result = _run_import_probe(root, ["/usr/local/bin/pytest", "tests/hermes_cli"])

    assert result["home_after"] == result["home_before"] == str(root)
    assert result["home_after"] != str(root / "profiles" / "alice")


def test_the_real_hermes_entrypoint_still_applies_the_override(tmp_path):
    """The other direction: nothing changes for a real ``hermes -p X ...``.

    ``argv[0]`` is the installed console script, which is the whole difference
    from the two probes above. Three separate facts are read back, and the
    pre-parse is the only writer of any of them: the resolved home, the
    ``flag`` rung stamped by the branch that consumed the flag, and an argv with
    the flag stripped so argparse never sees it.
    """

    root = _make_root(tmp_path, profiles=("alice",))
    result = _run_import_probe(
        root, [str(tmp_path / "bin" / "hermes"), "--profile", "alice", "status"]
    )

    assert result["home_after"] == str(root / "profiles" / "alice")
    assert result["resolution"] == "flag"
    assert result["argv"] == [str(tmp_path / "bin" / "hermes"), "status"]
