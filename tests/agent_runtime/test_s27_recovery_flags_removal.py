"""S27 retires ``agent_runtime.recovery_flags`` — the last block-recovery module.

The block-recovery lane went in S5 with ``recovery.py`` and the dispatch loop
(``tests/agent_runtime/test_recovery.py`` is its tombstone). ``recovery_flags``
survived that cut on a single import line: ``store.py:19`` named
``mark_incident_closed_for_recovery`` and never called it. Every symbol in the
module reads or writes ``task.harness_self_heal["stages"]["_mission"]`` on a
``Task`` record deleted in S8, so nothing could have called them meaningfully
either.

It is removed as a feature, not refactored. The keep-side name one bare-word
grep away is pinned below.
"""

from __future__ import annotations




REMOVED_MODULES = ("agent_runtime.recovery_flags",)








def test_the_lookalike_keep_set_survives():
    """``recovery``/``self_heal``-shaped names that are NOT this module."""

    from agent_runtime import harness_doctor, store

    # The doctor's runtime diagnostics are the live "recovery" surface.
    assert callable(harness_doctor.run_harness_doctor)
    assert callable(store.IncidentStore)
