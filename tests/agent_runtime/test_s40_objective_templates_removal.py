"""S40 removes ``agent_runtime/objective_templates.py`` whole.

The module rendered the per-node "templated objective" first message for the
mission lane's stage graph: a ``(owner_slot.role x output_type) -> objective``
registry keyed on a goal, a ``MissionPlanStage``, and a stage output type. S7
deleted the stage graph, so nothing has been able to call ``render_objective``
since; the 2026-07-31 reachability audit found the only surviving reference
anywhere in the repo is the prose sentence in
``docs/agent-runtime-harness/02-execution-engine.md`` that proposed it.

Nothing here touches the event registry: the module never emitted, so
``event_catalog()`` stays at 88 (pinned by
``test_s15_event_contract_pruning.py``).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import agent_runtime


def _agent_runtime_dir() -> Path:
    return Path(agent_runtime.__file__).parent


def test_the_objective_template_module_is_gone():
    assert not (_agent_runtime_dir() / "objective_templates.py").exists()


def test_the_objective_template_module_is_not_importable():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agent_runtime.objective_templates")


def test_no_source_or_doc_still_names_the_removed_renderer():
    root = _agent_runtime_dir().parent
    hits: list[str] = []
    for path in root.rglob("*"):
        if path.suffix not in {".py", ".md"} or not path.is_file():
            continue
        parts = set(path.parts)
        if parts & {".git", "__pycache__", ".venv", "venvs", "node_modules"}:
            continue
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file
            continue
        if "objective_templates" in text or "render_objective" in text:
            hits.append(str(path.relative_to(root)))
    assert not hits, f"stale references to the removed renderer: {hits}"
