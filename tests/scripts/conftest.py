"""Shared setup for the `scripts/` gate tests.

One fixture, and it exists for a reason worth stating: several tests here drive
``changed_line_mutation_check.run(..., list_only=False)``, which is a MUTATING
run and therefore takes the worktree lock the gate added on 2026-09-01. Without
this, those tests would exclusive-create ``.mutation_gate.lock`` in the REAL
repo root as a side effect — and, worse, would be REFUSED whenever they run
inside a live gate run, which is exactly what happens when a diff touches
``run`` and the gate baselines its own tests. That failure was measured (the
first gate run of this branch reported "baseline failed" on
``test_the_mutation_is_spliced_at_the_anchor_not_at_the_first_occurrence``).

Re-rooting the lock rather than disabling it keeps the lock's own behaviour
under test: ``tests/test_mutation_gate_worktree_lock.py`` sets its own
``LOCK_PATH`` explicitly and is unaffected by this.
"""

from __future__ import annotations

import pytest

from scripts import changed_line_mutation_check as _gate


@pytest.fixture(autouse=True)
def mutation_gate_lock_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(_gate, "LOCK_PATH", tmp_path / ".mutation_gate.lock")
