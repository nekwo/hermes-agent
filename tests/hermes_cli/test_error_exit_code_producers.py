"""Every exit code in the taxonomy must be one this stack can actually spend.

WHY
===

`ERROR_EXIT_CODES` is the harness's exit-status contract: the launcher and
every operator script branch on the numbers, and the `error.code` string is
what a human is told went wrong. Before 2026-08-19 it carried 71 rows, and
**25 of them existed only as their own dict entry** — no `code=` site, no
exception mapping, no producer of any kind in either repo. A taxonomy that
lists conditions the runtime cannot reach is not a superset, it is a claim
about the system that is false, and it is read as documentation.

Two of them were worse than inert. `goal_not_found`'s hint sent the operator to
`hermes harness goal list`, a verb that no longer exists. A code nothing can
spend, hinting at a command nobody can run.

The 25 went. This gate is why the 26th cannot arrive quietly.

WHAT COUNTS AS A PRODUCER
=========================

Three ways this process can spend a code, all derived by AST:

1. **A literal at a producing site** — `emit_harness_error(..., code="x")`,
   `_error_envelope("x", ...)`, `raise ValueError("x")` (the `ValueError` arm
   of `_error_code_for_exception` maps `str(exc)` straight through when it is a
   table key), an exception class carrying `code = "x"`.
2. **A mapped exception class WITH at least one `raise` site.** The mapping
   alone is not enough, and that distinction is the whole point of this gate:
   `InvalidTransition`, `StaleRun`, `ProofMissing` and `RuntimeRootMismatch`
   were all mapped and none was ever raised, so four codes looked produced and
   were not.
3. **`scripts/install-mission-control-hermes.ps1`**, which the launcher runs
   and which really does exit with the `install_*` family. Not Python, and
   omitted from the lane's original grep roots — which is how those four came
   within one commit of being deleted as dead.

The scan reads the AST, never the source text, because a retirement comment
naming a removed code is exactly the thing a text scan cannot tell from a
producer.

THE BASELINE
============

`_KEPT_UNSPENDABLE` holds codes hermes cannot spend today that are kept anyway,
each with the reason. Like every baseline in this repo it has a second test
asserting each row is still unspendable, so it can only shrink and a wired-up
code cannot leave a stale claim behind.
"""

from __future__ import annotations

import ast
import functools
import pathlib
import re

import pytest

HERMES_ROOT = pathlib.Path(__file__).resolve().parents[2]
SUPPORT = HERMES_ROOT / "hermes_cli" / "harness_support.py"

_PACKAGES = (
    "agent_runtime", "hermes_cli", "tools", "agent", "acp_adapter", "gateway",
    "scripts", "cron", "providers", "tui_gateway", "apps", "mobile_core",
)
_SKIP = {"__pycache__", ".venv", "venvs", "node_modules", ".git"}

#: Codes hermes cannot spend, kept deliberately. Each row says who reads it.
_KEPT_UNSPENDABLE: dict[str, str] = {
    "wrong_runtime_root": (
        "kept although `RuntimeRootMismatch` was never raised and its mapping "
        "row went with it. The launcher spells this same word as a live "
        "`MissionSnapshotHealth` value (mission_control_snapshot.dart), and "
        "the code stays spendable through the ValueError arm. Two lanes, one "
        "word — do not fold them without deciding which owns the spelling"
    ),
    "proof_missing": (
        "same shape as wrong_runtime_root: the class went, the word did not. "
        "The launcher's repository fakes, office page test and stage38 "
        "snapshot fixtures all carry it as proof-gate STATE, a different lane "
        "from the exit taxonomy"
    ),
    "blueprint_not_found": (
        "the only reader is a launcher stage38 fixture, "
        "goal_create_error.blueprint_not_found.json. Kept pending a cross-stack "
        "decision: the goal-create lane it belongs to is retired, so this is "
        "most likely a launcher fixture to reap rather than a hermes row"
    ),
    "provider_auth_expired": (
        "CROSS-STACK DEFECT, recorded not fixed: a launcher test "
        "(mission_control_snapshot_test.dart) asserts on a code hermes has no "
        "way to emit. The fix is a launcher one — either wire a hermes "
        "producer or drop the launcher assertion — and deleting this row first "
        "would turn an honest gap into a silent one"
    ),
}


def _strip_docstrings(tree: ast.Module) -> ast.Module:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def _production_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for package in _PACKAGES:
        root = HERMES_ROOT / package
        if root.is_dir():
            files += [f for f in root.rglob("*.py") if not any(p in _SKIP for p in f.parts)]
    files += sorted(HERMES_ROOT.glob("*.py"))
    return files


@functools.lru_cache(maxsize=1)
def _table() -> dict[str, int]:
    tree = ast.parse(SUPPORT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ERROR_EXIT_CODES" for t in node.targets
        ):
            return {k.value: v.value for k, v in zip(node.value.keys, node.value.values)}
    raise AssertionError("ERROR_EXIT_CODES not found — this gate would be vacuous")


@functools.lru_cache(maxsize=1)
def _mapped_classes() -> dict[str, set[str]]:
    """`code -> {exception class names}` read out of `_error_code_for_exception`."""

    mapping: dict[str, set[str]] = {}
    tree = ast.parse(SUPPORT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "_error_code_for_exception"):
            continue
        for child in ast.walk(node):
            if (
                isinstance(child, ast.If)
                and isinstance(child.test, ast.Call)
                and getattr(child.test.func, "id", None) == "isinstance"
                and len(child.test.args) > 1
            ):
                target = child.test.args[1]
                names = [
                    a.id
                    for a in (target.elts if isinstance(target, ast.Tuple) else [target])
                    if isinstance(a, ast.Name)
                ]
                for stmt in child.body:
                    if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Constant):
                        for name in names:
                            mapping.setdefault(stmt.value.value, set()).add(name)
            if (
                isinstance(child, ast.Tuple)
                and len(child.elts) == 2
                and isinstance(child.elts[0], ast.Name)
                and isinstance(child.elts[1], ast.Constant)
            ):
                mapping.setdefault(child.elts[1].value, set()).add(child.elts[0].id)
    return mapping


@functools.lru_cache(maxsize=1)
def _scan() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """`(code -> literal producer sites, class name -> raise sites)`."""

    table = _table()
    literals: dict[str, list[str]] = {code: [] for code in table}
    raises: dict[str, list[str]] = {}
    files = _production_files()
    assert len(files) > 500, f"only {len(files)} production files scanned — vacuous"

    # Cheap TEXT pre-filter before the expensive parse, and it must be keyed on
    # the right thing for each half. Nearly every Python file contains the word
    # `raise`, so filtering the raise scan on that is no filter at all — it is
    # keyed on the ~10 mapped exception CLASS names instead. Both filters are
    # exact rather than heuristic: a name absent from the raw text cannot be
    # present in the AST. (The converse is not true, which is why everything
    # that survives a filter is still parsed and inspected structurally.)
    keys = tuple(table)
    class_names = tuple({name for names in _mapped_classes().values() for name in names})
    support_relative = SUPPORT.relative_to(HERMES_ROOT).as_posix()

    for path in files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        relative = path.relative_to(HERMES_ROOT).as_posix()
        want_raises = any(name in raw for name in class_names)
        want_literals = relative != support_relative and any(key in raw for key in keys)
        if not (want_raises or want_literals):
            continue
        try:
            tree = _strip_docstrings(ast.parse(raw))
        except SyntaxError:  # pragma: no cover - another gate's problem
            continue
        for node in ast.walk(tree):
            if want_raises and isinstance(node, ast.Raise) and node.exc is not None:
                func = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name:
                    raises.setdefault(name, []).append(f"{relative}:{node.lineno}")
            if (
                want_literals
                and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in literals
            ):
                literals[node.value].append(f"{relative}:{node.lineno}")

    installer = HERMES_ROOT / "scripts" / "install-mission-control-hermes.ps1"
    assert installer.is_file(), (
        "the installer PS1 is missing — it produces the install_* family, and "
        "without it this gate would report four live codes as dead (which is "
        "exactly what the lane that first censused this table did)"
    )
    text = installer.read_text(encoding="utf-8", errors="replace")
    for code in table:
        if re.search(r'["\']%s["\']' % re.escape(code), text):
            literals[code].append("scripts/install-mission-control-hermes.ps1")
    return literals, raises


@functools.lru_cache(maxsize=1)
def _unspendable() -> dict[str, int]:
    table = _table()
    literals, raises = _scan()
    mapping = _mapped_classes()
    dead = {}
    for code, exit_code in table.items():
        if literals[code]:
            continue
        if any(raises.get(name) for name in mapping.get(code, ())):
            continue
        dead[code] = exit_code
    return dead


#: Paid ONCE, at import, like the tombstone registry's own walks. The scan
#: parses 900+ files; billing it to whichever test runs first puts a
#: multi-second cost inside one item's budget and makes the others look free.
_UNSPENDABLE_AT_IMPORT = dict(_unspendable())


def test_the_producer_scan_is_paid_once():
    """The warm's guard: five items share one scan or the cost multiplies."""

    assert _scan.cache_info().misses == 1, _scan.cache_info()


def test_every_exit_code_has_a_producer():
    unexpected = sorted(set(_unspendable()) - set(_KEPT_UNSPENDABLE))
    assert unexpected == [], (
        "these ERROR_EXIT_CODES rows exist only as their own dict entry — no "
        "`code=` site, no `raise ValueError(<code>)`, no mapped-and-raised "
        "exception class, and not the installer PS1. A taxonomy row nothing "
        "can spend is read as documentation of a state this runtime can reach:"
        f"\n  {unexpected}\nWire a producer, or delete the row, or add it to "
        "_KEPT_UNSPENDABLE with the reader that justifies it."
    )


def test_the_kept_unspendable_baseline_still_describes_the_code():
    """A code that gained a producer must LOSE its row, so the list shrinks."""

    now_spendable = sorted(set(_KEPT_UNSPENDABLE) - set(_unspendable()))
    assert now_spendable == [], (
        "these codes are in _KEPT_UNSPENDABLE but now HAVE a producer — delete "
        f"their rows in the commit that wired them: {now_spendable}"
    )


def test_the_installer_family_is_recognised_as_produced():
    """The four `install_*` codes are the reason class 3 exists.

    They are produced by a PowerShell script the launcher runs. A census that
    only looked at Python called them dead and came one commit from deleting a
    live contract. This asserts the gate keeps seeing them.
    """

    literals, _ = _scan()
    installer_codes = sorted(code for code in _table() if code.startswith("install_"))
    assert len(installer_codes) == 4, installer_codes
    for code in installer_codes:
        assert any("install-mission-control-hermes.ps1" in site for site in literals[code]), (
            f"{code} is no longer traced to the installer that exits with it"
        )


@pytest.mark.parametrize("code", sorted(_KEPT_UNSPENDABLE))
def test_every_kept_row_is_still_in_the_table(code: str):
    """A baseline row for a code nobody kept is a comment pretending to be a gate."""

    assert code in _table(), (
        f"{code} is in _KEPT_UNSPENDABLE but no longer in ERROR_EXIT_CODES"
    )
