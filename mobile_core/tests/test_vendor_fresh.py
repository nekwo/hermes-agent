from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_vendor_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "vendor_upstream.py"
    spec = importlib.util.spec_from_file_location("mobile_vendor_upstream", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix != ".pyc"
    }


def test_committed_vendor_tree_is_fresh(tmp_path: Path) -> None:
    mobile_root = Path(__file__).resolve().parents[1]
    destination = tmp_path / "_vendor"
    _load_vendor_tool().vendor(mobile_root.parent, destination)
    committed = mobile_root / "src" / "hermes_mobile_core" / "_vendor"
    assert _snapshot(destination) == _snapshot(committed)


def test_vendored_registry_discovers_only_chat_completions() -> None:
    mobile_root = Path(__file__).resolve().parents[1]
    registry = (
        mobile_root
        / "src"
        / "hermes_mobile_core"
        / "_vendor"
        / "agent"
        / "transports"
        / "__init__.py"
    ).read_text(encoding="utf-8")
    import_lines = [line.strip() for line in registry.splitlines() if line.strip().startswith("import ")]
    assert import_lines == [
        "import hermes_mobile_core._vendor.agent.transports.chat_completions  # noqa: F401"
    ]
