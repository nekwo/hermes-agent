"""Namespace guard for the exec'd ``hermes_cli/harness_parts/*.py`` command parts.

``hermes_cli/harness.py`` loads its command bodies by *exec*, not by import
(see ``_load_command_parts``): each part's source is compiled and executed
straight into harness.py's module globals. That mechanism is deliberate and
stays — but it means the two failure modes below are invisible to every other
gate we run:

1. **Silent shadowing.** A part rebinds a name harness.py (or an earlier part)
   already defined, with a *different* object. Nothing warns; the last writer
   wins, and the loser's callers silently change behaviour.
2. **Unresolvable free names.** A part's function body reads a global that no
   part and no harness.py statement ever binds. Because the read happens at
   call time, this is a ``NameError`` that only fires when an operator runs
   that one CLI verb — the class of defect that produced the ``:440``
   NameError.

This module rebuilds the exact post-load namespace (harness.py's own module
body, then the six parts in ``_load_command_parts`` order, compiled with the
same inherited ``from __future__ import annotations`` flag) and asserts both
properties statically, without running any command.

Note on annotation-only names: under ``from __future__ import annotations``
an unresolvable name that appears *only* in an annotation never evaluates, so
it cannot raise at runtime today. The guard still reports it — dropping the
future import (or a later ``typing.get_type_hints`` call) would turn it into a
live ``NameError``. Such names live in ``KNOWN_UNRESOLVED`` with a reason, and
the ledger is self-cleaning: an entry that stops being unresolvable fails the
test as stale.
"""

from __future__ import annotations

import ast
import builtins
import symtable
from pathlib import Path

import pytest

HARNESS_PATH = Path(__file__).resolve().parents[2] / "hermes_cli" / "harness.py"
PARTS_DIR = HARNESS_PATH.with_name("harness_parts")

# Same tuple, same order, as hermes_cli/harness.py::_load_command_parts.
PART_FILENAMES = (
    "persona_commands.py",
    "runtime_commands.py",
    "board.py",
    "office.py",
    "flow_commands.py",
    "checkpoint_commands.py",
)

# Free names a part reads that the post-load namespace does not provide.
# Every entry needs a reason; an entry that no longer reproduces is a stale
# ledger row and fails the test (see test_known_unresolved_ledger_is_not_stale).
KNOWN_UNRESOLVED: dict[str, dict[str, str]] = {
    "persona_commands.py": {
        "Callable": (
            "Ghost annotation: read only by two `Callable[[], None] | None` "
            "annotations, which never evaluate under the inherited "
            "`from __future__ import annotations`. Retired by the explicit "
            "import header (P0 step 3)."
        ),
    },
}


def _annotations_compiler_flag() -> int:
    """The ``from __future__ import annotations`` flag harness.py's compile inherits.

    ``compile()`` inherits the caller's future flags by default, and harness.py
    carries ``from __future__ import annotations``; passing the flag explicitly
    reproduces that here without depending on this test module's own header.
    """

    import __future__ as future_module

    return future_module.annotations.compiler_flag


def _strip_part_loader(tree: ast.Module) -> ast.Module:
    """Drop the module-level ``_load_command_parts()`` call from harness.py's AST.

    We want harness.py's *pre-parts* namespace so the parts can be layered in
    one at a time. Exactly one such statement must exist; if harness.py's shape
    changes, this fails loudly rather than silently probing the wrong thing.
    """

    kept = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "_load_command_parts"
        )
    ]
    assert len(kept) == len(tree.body) - 1, (
        "expected exactly one module-level _load_command_parts() call in harness.py; "
        f"found {len(tree.body) - len(kept)}"
    )
    tree.body = kept
    return tree


def _module_bound_names(tree: ast.Module) -> set[str]:
    """Names a module body binds at module level (defs, classes, imports, assignments)."""

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names.update(sub.id for sub in ast.walk(target) if isinstance(sub, ast.Name))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            names.update(sub.id for sub in ast.walk(node.target) if isinstance(sub, ast.Name))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None:
                    names.update(
                        sub.id for sub in ast.walk(item.optional_vars) if isinstance(sub, ast.Name)
                    )
    return names


def _free_global_loads(source: str, filename: str) -> set[str]:
    """Every name the part reads from the module namespace at run time.

    Uses ``symtable`` rather than a hand-rolled AST walk so Python's own
    scope resolution decides what is local, a parameter, a closure cell, or a
    global load — the same analysis the interpreter performs.
    """

    table = symtable.symtable(source, filename, "exec")
    free: set[str] = set()

    def walk(tbl: symtable.SymbolTable, *, module_level: bool) -> None:
        for sym in tbl.get_symbols():
            if not sym.is_referenced():
                continue
            if module_level:
                # At module level everything is "global"; only names this file
                # never binds itself are inherited reads.
                if sym.is_assigned() or sym.is_imported():
                    continue
                free.add(sym.get_name())
            elif sym.is_global():
                free.add(sym.get_name())
        for child in tbl.get_children():
            walk(child, module_level=False)

    walk(table, module_level=True)
    return free


class _LoadedNamespace:
    def __init__(self) -> None:
        self.namespace: dict[str, object] = {}
        self.bound_by_part: dict[str, set[str]] = {}
        self.collisions: list[tuple[str, str, str]] = []


@pytest.fixture(scope="module")
def loaded() -> _LoadedNamespace:
    """Reproduce harness.py's post-load globals, recording per-part bindings."""

    result = _LoadedNamespace()
    harness_source = HARNESS_PATH.read_text(encoding="utf-8")
    harness_tree = _strip_part_loader(ast.parse(harness_source, str(HARNESS_PATH)))

    namespace: dict[str, object] = {
        "__name__": "hermes_cli.harness",
        "__file__": str(HARNESS_PATH),
        "__builtins__": builtins,
    }
    exec(compile(harness_tree, str(HARNESS_PATH), "exec"), namespace)  # noqa: S102

    owner_of: dict[str, str] = {name: "harness.py" for name in namespace}
    flag = _annotations_compiler_flag()

    for filename in PART_FILENAMES:
        path = PARTS_DIR / filename
        source = path.read_text(encoding="utf-8")
        bound = _module_bound_names(ast.parse(source, str(path)))
        previous = {name: namespace[name] for name in bound if name in namespace}

        exec(compile(source, str(path), "exec", flags=flag), namespace)  # noqa: S102

        for name, before in sorted(previous.items()):
            after = namespace[name]
            if after is before:
                continue  # re-import of the identical object — idempotent, fine.
            try:
                if after == before:
                    continue  # equal constant re-declared; not a behaviour change.
            except Exception:  # pragma: no cover - exotic __eq__
                pass
            result.collisions.append((filename, name, owner_of[name]))
        for name in bound:
            owner_of.setdefault(name, filename)
        result.bound_by_part[filename] = bound

    result.namespace = namespace
    return result


def test_probe_reproduces_the_real_harness_namespace(loaded: _LoadedNamespace) -> None:
    """The probe is only meaningful if it loads what the real import loads."""

    import hermes_cli.harness as harness

    real = {name for name in vars(harness) if not name.startswith("__")}
    probe = {name for name in loaded.namespace if not name.startswith("__")}
    assert probe == real, {
        "only_in_probe": sorted(probe - real),
        "only_in_real_module": sorted(real - probe),
    }


def test_no_part_shadows_an_existing_name(loaded: _LoadedNamespace) -> None:
    """A part must not rebind a name harness.py or an earlier part already owns.

    Re-importing the *same* object is fine (that is what explicit import
    headers do); rebinding it to a different object is silent shadowing.
    """

    assert loaded.collisions == [], "\n".join(
        f"{part} rebinds '{name}' (already defined by {owner})"
        for part, name, owner in loaded.collisions
    )


def test_every_free_name_resolves_after_load(loaded: _LoadedNamespace) -> None:
    """No part may read a global the post-load namespace cannot supply."""

    resolvable = set(loaded.namespace) | set(dir(builtins))
    failures: list[str] = []
    for filename in PART_FILENAMES:
        path = PARTS_DIR / filename
        free = _free_global_loads(path.read_text(encoding="utf-8"), str(path))
        allowed = KNOWN_UNRESOLVED.get(filename, {})
        for name in sorted(free - resolvable):
            if name in allowed:
                continue
            failures.append(f"{filename}: '{name}' is read but never bound")
    assert failures == [], "\n".join(failures)


def test_known_unresolved_ledger_is_not_stale(loaded: _LoadedNamespace) -> None:
    """Every ledger entry must still reproduce, so fixes force ledger removal."""

    resolvable = set(loaded.namespace) | set(dir(builtins))
    stale: list[str] = []
    for filename, entries in KNOWN_UNRESOLVED.items():
        assert filename in PART_FILENAMES, f"ledger names unknown part {filename}"
        free = _free_global_loads(
            (PARTS_DIR / filename).read_text(encoding="utf-8"), str(PARTS_DIR / filename)
        )
        for name, reason in entries.items():
            assert reason.strip(), f"{filename}:{name} needs a reason"
            if name in resolvable or name not in free:
                stale.append(f"{filename}: '{name}' now resolves — drop the ledger entry")
    assert stale == [], "\n".join(stale)


def test_part_load_order_matches_harness(loaded: _LoadedNamespace) -> None:
    """Keep this module's part list in lockstep with _load_command_parts."""

    source = HARNESS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, str(HARNESS_PATH))
    loader = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_load_command_parts"
    )
    literals = [
        node.value
        for node in ast.walk(loader)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.endswith(".py")
    ]
    assert tuple(literals) == PART_FILENAMES
