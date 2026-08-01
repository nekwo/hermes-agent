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
    """Gates on a live REFERENCE, not on any mention.

    FIXED 2026-07-31 (S44/S45 wave); the failure it fixes was NOT caused by this
    wave. As originally written the gate forbade the strings
    ``objective_templates`` / ``render_objective`` anywhere in any ``.py`` or
    ``.md`` in the repo — and the very next commit to land after S40
    (``2b0d8dd94``, which opened the deferred-debt ledger) broke it by recording
    *"``objective_templates.py`` whole"* on the S40 line of
    ``docs/agent-runtime-harness/16-mission-lane-removal.md``. That is not a
    stale reference; it is the removal record doing its job. The gate was
    already red on ``main`` before this wave touched anything.

    The defect class is one this repo's own witnesses already name — see
    ``test_s29_context_request_bundle_removal``: *"Gates on the CODE forms
    (definition, call, import), not on any mention"*. A prose gate that cannot
    tell "this code calls the removed thing" from "this document says we removed
    it" makes the removal log unwritable, and the cheapest way to make it pass is
    to stop documenting removals — exactly backwards.

    So: ``.py`` files keep the absolute gate (no definition, call, or import can
    survive anywhere in the package), while Markdown is gated on the CODE forms
    only. A doc naming the module in prose is allowed and expected."""

    root = _agent_runtime_dir().parent
    doc_code_forms = (
        "def render_objective",
        "render_objective(",
        "import objective_templates",
        "from .objective_templates",
        "from agent_runtime.objective_templates",
        "objective_templates.render",
    )
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
        if path.suffix == ".py":
            if "objective_templates" in text or "render_objective" in text:
                hits.append(str(path.relative_to(root)))
        elif any(form in text for form in doc_code_forms):
            hits.append(str(path.relative_to(root)))
    assert not hits, f"stale references to the removed renderer: {hits}"
