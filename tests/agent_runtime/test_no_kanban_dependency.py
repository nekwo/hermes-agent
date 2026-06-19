import ast
from pathlib import Path


def test_agent_runtime_imports_no_kanban_modules():
    root=Path("agent_runtime")
    offenders=[]
    for path in root.glob("*.py"):
        tree=ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names=[a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names=[node.module or ""]
            else:
                continue
            if any("kanban" in name for name in names):
                offenders.append((str(path), names))
    assert offenders == []
