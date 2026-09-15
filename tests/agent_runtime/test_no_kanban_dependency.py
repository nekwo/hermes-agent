"""``agent_runtime`` must not import the kanban lane.

RE-AIMED 2026-08-19 (MCF-53 sweep). The gate had four independent ways to scan
nothing or to miss the live form, and it asserted an EMPTY list, so every one of
them was a silent pass:

1. ``Path("agent_runtime")`` is CWD-RELATIVE. Run from anywhere but the repo
   root the glob yields no file and the assertion is ``[] == []``. Anchored to
   the package's own ``__file__`` now, which cannot depend on the invocation.
2. ``glob("*.py")`` is not recursive: 127 of the package's 129 files were
   scanned and ``agent_runtime/blueprints/`` was never parsed at all.
3. The ``ImportFrom`` arm read ``node.module`` alone, so
   ``from agent import kanban_stop`` — where the module name is an ALIAS and
   ``node.module`` is just ``agent`` — was invisible. ``agent/kanban_stop.py``
   exists in production today, so that was a live bypass, and it is the same
   blind spot the s49/s50 import gates carried.
4. No anti-vacuity of any kind. A rename or a move of the package left the gate
   green forever.
"""

import ast
from pathlib import Path

import agent_runtime

#: The package's own location, so the scan cannot depend on the caller's cwd.
_ROOT = Path(agent_runtime.__file__).resolve().parent

#: A floor, not a guess: the package carries ~129 modules. A collapse to double
#: digits is the walk breaking, not the package shrinking.
_MIN_SCANNED = 100


def _imported_names(node: ast.AST) -> list[str]:
    """Every module name an import statement NAMES, in both spellings.

    ``import a.b`` puts the module in ``alias.name``; ``from a import b`` puts
    the PACKAGE in ``node.module`` and the module in ``alias.name``. Reading only
    the first of those was defect 3 above.
    """

    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""] + [alias.name for alias in node.names]
    return []


def test_agent_runtime_imports_no_kanban_modules():
    scanned = 0
    offenders = []
    for path in _ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        scanned += 1
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            names = _imported_names(node)
            if any("kanban" in name for name in names):
                offenders.append((str(path.relative_to(_ROOT)), names))
    assert scanned >= _MIN_SCANNED, (
        f"the walk visited {scanned} files under {_ROOT}; an absence gate over "
        "an empty scan passes forever"
    )
    assert offenders == []


def test_the_import_reader_sees_both_spellings():
    """ANTI-VACUITY for the reader itself, on synthetic source.

    The gate above asserts an empty result, which is the shape that keeps
    passing quietly once the machinery under it stops working.
    """

    tree = ast.parse(
        "import agent_runtime.kanban_board\n"
        "from agent import kanban_stop\n"
        "from agent_runtime.kanban_board import Card\n"
        "from agent_runtime import board_store\n"
    )
    named = [name for node in ast.walk(tree) for name in _imported_names(node)]
    assert "agent_runtime.kanban_board" in named, "the `import a.b` form is unread"
    assert "kanban_stop" in named, "the `from a import b` alias form is unread"
    assert sorted(set(name for name in named if "kanban" in name)) == [
        "agent_runtime.kanban_board",
        "kanban_stop",
    ]
    assert "board_store" in named, "a live sibling import is unread"
