from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_gate():
    path = Path(__file__).resolve().parents[1] / "tools" / "import_gate.py"
    spec = importlib.util.spec_from_file_location("mobile_import_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_full_source_graph_has_no_forbidden_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "hermes_mobile_core"
    assert _load_gate().scan(root) == []


def test_gate_finds_function_level_lazy_import(tmp_path: Path) -> None:
    source = tmp_path / "package"
    source.mkdir()
    (source / "lazy.py").write_text(
        "def hidden():\n    import subprocess\n    return subprocess\n",
        encoding="utf-8",
    )
    violations = _load_gate().scan(source)
    assert len(violations) == 1
    assert "forbidden import: subprocess" in violations[0].detail
