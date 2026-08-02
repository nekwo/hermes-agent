"""S49 deletes ``agent_runtime/operator_control.py`` whole (146 lines).

Operator-ruled CUT on 2026-08-01. Like S44, this sat on a ruling rather than on
missing work: the module was already production-caller-free, but deleting it
retires three registered event contracts and therefore moves the absolute event
count and ``contract_hash()``.

What was re-verified against the tree before the cut, name by name:

* ``operator_takeover_worker`` was the module's ONLY public symbol and had
  **zero** production importers. Its whole importer set was two tests
  (``tests/agent_runtime/test_worker_sessions.py``) plus the S41 binding pin —
  and the S41 pin exists precisely because ``hermes_cli/harness.py`` used to
  bind the name and stopped reading it. Removing that binding is what left the
  module importer-free; S49 finishes the job.
* ``"worker.takeover"`` is **not** a registered capability id and never was. It
  appeared exactly once in the tree, as a string in the function's own return
  dict — never in a capability registry, a Launcher-invoked verb table, or a
  toolset. Nothing could dispatch to it.
* ``agent_runtime/production_envelope.py`` referenced ``operator_control`` only
  as prose: the H6 item's title plus two ``controls`` strings advertising the
  audited takeover workflow. Those are removed with it — an envelope item that
  advertises a control an operator cannot invoke is the same defect class as a
  registered event with no emitter. It was never an import.

**A liveness note this cut falsifies, re-derived rather than left standing.**
``run.closed`` is pinned as a near-miss survivor in
``tests/agent_runtime/test_s15_event_contract_pruning`` and in the registry
comment, both of which cited TWO live callers of ``RunStore.cancel`` ->
``close_run``: "operator takeover" and "persona-chat replacement". This cut
deletes the first. ``run.closed`` stays registered and emittable — the
persona-chat replacement in ``persona_assignments.py`` still reaches it — but
both notes are re-derived to cite one caller, not two. The S44 precedent: a
survivor pin that silently loses one of the subjects it names rots into
decoration.

**Event deregistration gates APPENDS, not reads** (S36/S37/S44 precedent). The
three contracts leave ``ALLOWED_EVENT_TYPES``, so ``EventLog.append`` refuses
them from here on, while any historical ``operator.takeover.*`` row in a live
log still deserializes and reads back. None of the three was ever in
``events.OPERATOR_SUMMARY_EVENT_TYPES``, so — unlike S25's ``run.opened`` —
there is no summary arm or formatter branch to retire alongside.

Count: 82 -> 79. The absolute count authority stays
``tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT``;
this file asserts only its own -3 delta so there is one number to maintain.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Two pure-ABSENCE cases left this file:

* ``test_the_operator_control_module_is_gone`` -> ``MODULE`` row
  ``agent_runtime.operator_control``.
* ``test_the_three_contracts_are_deregistered`` -> three ``EVENT`` rows,
  ``operator.takeover.requested`` / ``.approval_required`` / ``.applied``.

The registry additionally carries ``CODE`` rows for ``operator_takeover_worker``
and the ``"worker.takeover"`` capability STRING — a repo-wide guarantee this
file never made, and the S44/S55 reason literals survive the registry's AST
round trip.

WHY THE SURVIVORS STAYED:

* ``test_no_production_module_still_imports_it`` — an IMPORT-GRAPH gate, not a
  name gate. ``find_spec`` returning ``None`` says the module cannot be found;
  it says nothing about a production file that still writes
  ``from agent_runtime import operator_control`` and would blow up at runtime.
  Imports are what resurrect a module, so imports are what this asserts. No
  registry form expresses it.
* The APPEND refusal and ``test_historical_rows_still_read_back`` — the S36
  precedent's two halves. The registry checks REGISTRATION; only these check the
  log.
* ``test_no_operator_summary_arm_went_missing`` — a frozenset key-set pin.
* ``test_the_production_envelope_that_advertised_the_deleted_workflow_is_itself_gone``
  — an INVERTED pin recording S56's reversal of S49's own rewording, and its
  second half reads the produced ``build_status`` / ``effective_config_summary``
  dicts.
* ``test_run_closed_survives_on_its_remaining_caller`` — a KEEP + a wire pin.
* ``test_the_registry_lost_exactly_three_contracts`` — agreement with S15's
  single absolute-count authority.
"""

from __future__ import annotations


import pytest

from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES


#: The three contracts retired with their only emitter.
RETIRED_EVENT_TYPES = (
    "operator.takeover.requested",
    "operator.takeover.approval_required",
    "operator.takeover.applied",
)


def test_no_production_module_still_imports_it():
    """The import graph, not just the file: a dead module that keeps an importer
    is how a deleted subsystem gets resurrected by the next reachability pass.

    Scoped to PRODUCTION packages and read through the AST, deliberately. The
    removal-contract tests in this directory name ``operator_control`` in prose
    on purpose — that is what a removal pin is — and the S48 lesson is that a
    raw-substring gate over the whole tree flags its own docstring. Imports are
    the thing that can resurrect a module, so imports are what this asserts.
    """

    import ast
    import pathlib

    import agent_runtime

    root = pathlib.Path(agent_runtime.__file__).resolve().parents[1]
    packages = ("agent_runtime", "hermes_cli", "gateway", "agent", "acp_adapter", "tools")
    offenders = []
    for package in packages:
        for path in (root / package).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "operator_control" in (node.module or ""):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if "operator_control" in alias.name:
                            offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == [], offenders


def test_appending_a_retired_takeover_event_is_refused():
    from hermes_time import now

    from agent_runtime.events import Event, EventLog

    for event_type in RETIRED_EVENT_TYPES:
        with pytest.raises(ValueError):
            EventLog().append(
                Event(ts=now(), type=event_type, task_id="task_1", run_id=None, persona_id=None)
            )


def test_historical_rows_still_read_back(isolate_agent_runtime_root):
    """S36/S44 precedent: deregistration gates APPENDS, not reads."""

    import json

    from agent_runtime import paths
    from agent_runtime.events import EventLog

    line = {
        "ts": "2026-07-01T00:00:00+00:00",
        "type": "operator.takeover.applied",
        "task_id": "task_historical",
        "run_id": None,
        "persona_id": "dev",
        "payload": {"worker_session_id": "worker_abc", "actor": "operator"},
    }
    paths.events_path().parent.mkdir(parents=True, exist_ok=True)
    paths.events_path().write_text(json.dumps(line) + "\n", encoding="utf-8")

    rows = list(EventLog().iter_from_offset(0))
    assert [evt.type for _offset, evt in rows] == ["operator.takeover.applied"]
    assert rows[0][1].payload["worker_session_id"] == "worker_abc"


def test_no_operator_summary_arm_went_missing():
    """None of the three was ever an operator-summary type, so unlike S25's
    ``run.opened`` there is no renderer branch to retire alongside."""

    assert set(RETIRED_EVENT_TYPES) & OPERATOR_SUMMARY_EVENT_TYPES == set()
    assert OPERATOR_SUMMARY_EVENT_TYPES - {"run.closed"} <= ALLOWED_EVENT_TYPES


def test_the_production_envelope_that_advertised_the_deleted_workflow_is_itself_gone():
    """SUPERSEDED at S56, inverted rather than deleted, with the reason kept.

    S49 rewrote H6's ``controls`` so the envelope would stop claiming a
    ``worker.takeover`` workflow no operator could invoke, and this test pinned
    the three surviving claims. S56 then found two of those three were false
    too: only ``worker.resume`` was ever a registered capability (never
    ``worker.pause``), both verbs' implementations went with the worker-session
    write lane, and the "daemon stop/kill paths" claim outlived the retired
    Mission Daemon. Rewording a third time would have been the third patch on a
    recurring defect, so the whole prose block went instead.

    The assertion is inverted because the ORIGINAL concern still stands: an
    envelope item may not advertise a control no code can perform. The only way
    that can be true now is if the envelope does not exist."""
    from importlib.util import find_spec

    assert find_spec("agent_runtime.production_envelope") is None
    from agent_runtime.migrations import effective_config_summary
    from agent_runtime.status import build_status

    assert "production_envelope" not in effective_config_summary()
    assert "production_envelope" not in build_status()


def test_the_registry_lost_exactly_three_contracts():
    """Delta-only; the absolute authority is S15's SURVIVING_EVENT_COUNT."""

    from tests.agent_runtime.test_s15_event_contract_pruning import SURVIVING_EVENT_COUNT

    assert [name for name in RETIRED_EVENT_TYPES if name in event_catalog()] == []
    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
