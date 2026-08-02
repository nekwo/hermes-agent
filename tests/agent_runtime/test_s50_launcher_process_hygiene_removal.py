"""S50 deletes ``agent_runtime/launcher_process_hygiene.py`` whole (164 lines).

Operator-ruled CUT on 2026-08-01, and the twin of S49: same shape, same
importer arithmetic, one difference that makes it the cheaper of the two.

What was re-verified against the tree before the cut:

* The module's public surface was two names — ``clean_launcher_visual_processes``
  and ``launcher_visual_cleanup_needed`` — plus two private helpers. Its ENTIRE
  importer set was ``tests/agent_runtime/test_dirty_state.py`` and the S41
  binding pin. Zero production importers, exactly as with ``operator_control``:
  ``hermes_cli/harness.py`` used to bind ``launcher_visual_cleanup_needed`` and
  stopped reading it, S41 removed the binding, and that left the module with no
  way in.
* **No event contract moves.** Unlike S49, this module never constructed an
  ``Event`` and never touched ``EventLog`` — it shelled out to
  ``tasklist``/``taskkill`` and returned a receipt dict. The registry is
  untouched by S50 and ``SURVIVING_EVENT_COUNT`` does not move for it.

**The tests that went with it, and the one that did not.** Three tests in
``test_dirty_state.py`` referenced this module. Two exercised only the deleted
lane (the Windows tasklist/taskkill receipt, and the goal-text trigger
predicate) and were removed with it. The third,
``test_new_goal_hygiene_can_cleanup_launcher_visual_processes``, was mostly
vacuous — it asserted that a dict literal built two lines earlier still held the
value it had just been handed, plus ``clean_launcher_visual_processes is not
None``. Its one real gate was an unrelated removal pin
(``agent_runtime.persona_diagnostics`` stays absent), so that gate was
RETARGETED under an honest name rather than deleted with the rest. A vacuous
assertion is not coverage, and deleting it silently would have hidden that the
file's real pin lived inside it.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Two pure-ABSENCE cases left this file:

* ``test_the_hygiene_module_is_gone`` -> ``MODULE`` row
  ``agent_runtime.launcher_process_hygiene``.
* ``test_none_of_the_removed_names_survives_anywhere_in_agent_runtime`` -> four
  repo-wide ``CODE`` rows (``clean_launcher_visual_processes``,
  ``launcher_visual_cleanup_needed``, ``_parse_tasklist_csv``,
  ``_safe_process_names``). ``REMOVED_NAMES`` went with it. The registry row is
  STRICTLY STRONGER twice over: this file's scan was scoped to ``agent_runtime``
  only and matched only ``FunctionDef``/``AsyncFunctionDef`` NODES, so a
  copy-pasted helper landing in ``hermes_cli`` — or surviving as a call, an
  import binding or a string — passed it. The registry scans seven production
  packages over rendered code.

WHY THE SURVIVORS STAYED:

* ``test_no_production_module_still_imports_it`` — the import-graph gate (the
  S49 note applies verbatim: ``find_spec is None`` says nothing about a file
  that still writes the import).
* ``test_s50_moved_no_event_contract`` — a COUNT pin against S15's single
  authority, plus a PREFIX assertion (no registered type may carry
  ``launcher``) that no finite list of rows can express: it must also hold for a
  type nobody has invented yet.
* ``test_the_dirty_state_lane_this_module_sat_beside_is_untouched`` — a KEEP.
"""

from __future__ import annotations

import ast
import pathlib

import agent_runtime
from agent_runtime.decision_contract_registry import event_catalog


def test_no_production_module_still_imports_it():
    """Imports are what can resurrect a module, so imports are what this gates —
    read through the AST and scoped to production packages, so the removal-
    contract prose in this directory does not flag itself (the S48 lesson)."""

    root = pathlib.Path(agent_runtime.__file__).resolve().parents[1]
    offenders = []
    for package in ("agent_runtime", "hermes_cli", "gateway", "agent", "acp_adapter", "tools"):
        for path in (root / package).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "launcher_process_hygiene" in (node.module or ""):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if "launcher_process_hygiene" in alias.name:
                            offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], offenders


def test_s50_moved_no_event_contract():
    """Negative gate, and the one real difference from S49: this module never
    emitted. The count authority must be untouched by THIS cut."""

    from tests.agent_runtime.test_s15_event_contract_pruning import SURVIVING_EVENT_COUNT

    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
    assert [name for name in event_catalog() if "launcher" in name] == []


def test_the_dirty_state_lane_this_module_sat_beside_is_untouched():
    """Negative gate: ``dirty_state`` is a different module and stays whole."""

    from agent_runtime import dirty_state

    assert callable(dirty_state.build_dirty_state)
