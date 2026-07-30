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

import importlib.util
import inspect

import pytest


REMOVED_MODULES = ("agent_runtime.recovery_flags",)


def test_the_module_is_gone():
    assert [name for name in REMOVED_MODULES if importlib.util.find_spec(name) is not None] == []


def test_importing_it_raises_module_not_found():
    for name in REMOVED_MODULES:
        with pytest.raises(ModuleNotFoundError):
            __import__(name)


def test_the_store_no_longer_carries_the_dangling_import():
    """The import was the module's only liveness — an import is not a call."""

    from agent_runtime import store

    assert not hasattr(store, "mark_incident_closed_for_recovery")
    assert "recovery_flags" not in inspect.getsource(store)


def test_the_lookalike_keep_set_survives():
    """``recovery``/``self_heal``-shaped names that are NOT this module."""

    from agent_runtime import harness_doctor, store

    # The doctor's runtime diagnostics are the live "recovery" surface.
    assert callable(harness_doctor.run_harness_doctor)
    assert callable(store.IncidentStore)
