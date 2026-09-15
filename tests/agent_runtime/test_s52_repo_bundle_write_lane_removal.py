"""S52 removes the ``RepoBundleStore`` WRITE lane and its seven contracts.

Operator-ruled CUT on 2026-08-01. This is a write-lane cut, NOT a store removal:
``status.py`` still constructs a ``RepoBundleStore`` and projects every operator
bundle row off ``list_all``, so the read side is live and stays.

What was re-verified against the tree, receiver-aware, before the cut:

* ``create_or_update_from_task``, ``attach_assignment``, ``mark_running``,
  ``mark_verified``, ``mark_rejected``, ``wake_ready_dependencies`` — zero
  production callers. Their whole caller set was ``tests/agent_runtime/
  test_repo_bundles.py``.
* ``update`` — the one that needed care, because a bare ``.update(`` grep is
  meaningless (every dict in the tree has one). Checked by receiver: its only
  call sites were the five sibling mutators above, all of which were themselves
  callerless. With them gone it had none.
* ``acquire_repo_bundle_locks`` / ``release_repo_bundle_locks`` /
  ``qa_waiting_on`` — module-level and equally callerless outside tests.
* The transitive closure that had to go WITH them or become residue: the hollow
  ``cancel_superseded`` seam (``return []``, an S21-class shape), the private
  writers ``_write`` / ``_event``, the lock helpers ``_repo_lock_conflicts`` /
  ``_write_repo_locks``, the projection pair ``desired_bundles_for_task`` /
  ``merge_desired_bundle``, and the write-time helpers ``bundle_id_for`` /
  ``safe_bundle_state`` / ``normalize_repo`` / ``safe_token`` / ``safe_text`` /
  ``_dedupe_preserve_order`` / ``owner_for_repo``. Each was reachable only from
  a deleted writer and had no importer outside the module. Leaving them would be
  the residue S25 named when it retired ``events._safe_int`` alongside the
  formatter arm that was its only caller.

**Two of the seven contracts were operator-summary types, and that is the part
S44 did not have to handle.** ``repo_bundle.updated`` and
``repo_bundle.assigned`` sat in ``events.OPERATOR_SUMMARY_EVENT_TYPES`` and
shared one formatter arm in ``operator_event_summary``. The S21/S25 invariant is
that the frozenset may not name a de-registered type, and the function
early-returns ``None`` outside it — so both the rows and the arm became
unreachable the moment the registration went, and all three go in this commit.

**A KNOWN CONSEQUENCE this wave deliberately did not fix — CLOSED AT S56.** With
both lock mutators gone, nothing could write ``repo_bundle_locks.json``, so
``repo_lock_summary()`` — still live and still published by ``status.py`` as
``repo_locks`` at the time — could only ever report
``{"lock_count": 0, "locks": []}``: the S47 item-5 defect class (a wire whose
value no code path can move). Retiring it meant editing the emitted status frame,
which belonged to the read-side/contract wave this one was scoped out of. S56 is
that wave: the summary and the ``repo_locks`` row are both gone, along with
``repo_bundle_summary`` / ``repo_bundle_delivery_summary`` /
``bundle_queue_summary`` and the three sibling rows. The pins below are INVERTED
to absence rather than dropped, so a stale producer cannot resurrect a reader.

**AND THE STORE ITSELF WENT AT S57.** S52's whole premise was "this is a
write-lane cut, NOT a store removal", because ``status.py`` still projected off
``list_all``. S56 removed those projections; that left ``RepoBundleStore`` with
zero production importers, and S57 deleted ``agent_runtime/repo_bundles.py``
whole with the ``RepoBundle`` model, ``paths.repo_bundle_path`` and the
``migration_status`` count. Every pin in this file that reached INTO the module
is therefore inverted once more, to module ABSENCE — a strictly stronger
statement than "the mutator is missing", and the form that catches a re-import.

Count: 79 -> 72. The absolute authority stays S15's ``SURVIVING_EVENT_COUNT``.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Four pure-ABSENCE cases left this file:

* ``test_the_whole_module_is_gone`` -> ``MODULE`` row
  ``agent_runtime.repo_bundles`` + ``PATH`` row
  ``agent_runtime/repo_bundles.py`` (both S57 rows; this case had already been
  inverted to module absence when S57 took the store).
* ``test_the_names_this_wave_removed_cannot_come_back_through_another_module``
  -> nineteen repo-wide ``CODE`` rows carrying the SAME distinctive-subset
  ruling this file made (``update`` / ``_write`` / ``get`` are deliberately NOT
  rows — gating ordinary store method names is a false-positive machine). The
  registry scan is wider (seven packages, not just ``agent_runtime``) and reads
  RENDERED code rather than raw text, so a re-grown reference can no longer hide
  behind, or be faked by, a comment. ``DISTINCTIVE_REMOVED_NAMES`` went with it,
  and its hand-rolled anti-vacuity assert is now the registry's
  ``test_the_scanner_is_not_vacuous``.
* ``test_the_projection_helpers_went_at_s56`` -> the S56 ``CODE`` rows
  ``repo_lock_summary`` / ``bundle_queue_summary`` / ``repo_bundle_summary`` /
  ``repo_bundle_delivery_summary`` / ``REPO_BUNDLE_DELIVERY_CONTRACT`` /
  ``REPO_BUNDLE_CHECKOUT_STATUS`` plus the S57 ``CODE`` row ``RepoBundleStore``.
  ``S56_REMOVED_READ_NAMES`` went with it. Repo-wide again subsumes the old
  ``build_status``-scoped form.
* ``test_the_seven_contracts_are_deregistered`` -> seven ``EVENT`` rows (and the
  registry additionally carries ``repo_bundle.delivered``, the S25 row that was
  retired one commit BEHIND its writer).

WHY THE SURVIVORS STAYED — this file owns the "de-registration gates APPENDS,
not reads" family, and the registry deliberately has no form for any of it:

* ``test_appending_a_retired_repo_bundle_event_is_refused`` — a call, not a
  scan.
* ``test_historical_rows_still_read_back`` — a persisted row must still
  deserialize. Nothing on the READ path consults the registry.
* ``test_the_two_operator_summary_rows_and_their_shared_arm_went_too`` and
  ``test_the_surviving_operator_summary_types_still_render`` — the S21/S25
  frozenset invariant plus a rendered-string characterization of the arm that
  survived.
* ``test_the_repo_lock_wire_is_now_a_constant`` — a produced-dict check on
  ``build_status()``, and an INVERTED pin recording S56's closure of the debt
  S52 opened.
* ``test_the_registry_lost_exactly_seven_contracts`` — agreement with S15.

``REMOVED_STORE_METHODS`` / ``REMOVED_MODULE_NAMES`` / ``SURVIVING_READ_NAMES``
are left in place: they stopped feeding tests when S57 inverted this file to
module absence, and they remain the wave's written record of exactly what left
the lane and in which order.
"""

from __future__ import annotations

import inspect

import pytest

from agent_runtime import events as events_module
from agent_runtime import status
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES


#: The seven contracts retired with the write lane.
RETIRED_EVENT_TYPES = (
    "repo_bundle.created",
    "repo_bundle.updated",
    "repo_bundle.assigned",
    "repo_bundle.running",
    "repo_bundle.verified",
    "repo_bundle.rejected",
    "repo_bundle.woke",
)

#: Every mutator that left ``RepoBundleStore``.
REMOVED_STORE_METHODS = (
    "create_or_update_from_task",
    "update",
    "attach_assignment",
    "mark_running",
    "mark_verified",
    "mark_rejected",
    "wake_ready_dependencies",
    "cancel_superseded",
    "_write",
    "_event",
)

#: Module-level names that went with them (mutators + their exclusive helpers).
REMOVED_MODULE_NAMES = (
    "acquire_repo_bundle_locks",
    "release_repo_bundle_locks",
    "qa_waiting_on",
    "desired_bundles_for_task",
    "merge_desired_bundle",
    "_repo_lock_conflicts",
    "_write_repo_locks",
    "bundle_id_for",
    "safe_bundle_state",
    "normalize_repo",
    "safe_token",
    "safe_text",
    "_dedupe_preserve_order",
    "owner_for_repo",
    "REPO_BUNDLE_STATES",
    "TERMINAL_REPO_BUNDLE_STATES",
    "DELIVERED_REPO_BUNDLE_STATES",
    "REPO_LOCK_MODES",
    "WAKE_DEPENDENCY_DELIVERED",
    "_REPO_OWNER_RULES",
)

#: The read side the S52 cut preserved. S56 took four of the five: the
#: projection helpers fed `build_status` rows (`repo_bundles`,
#: `repo_bundle_closeout`, `bundle_queue`, `repo_locks`) that no writer could
#: move, so the summaries went with the rows. `RepoBundleStore` -- the read side
#: proper -- is what S52 actually had to keep, and S57 then took THAT once S56
#: left it with zero production importers. Kept here as the record of a keep
#: that was correct for exactly one commit; the absence is a registry CODE row.
SURVIVING_READ_NAMES = ("RepoBundleStore",)


def test_appending_a_retired_repo_bundle_event_is_refused():
    from hermes_time import now

    from agent_runtime.events import Event, EventLog

    for event_type in RETIRED_EVENT_TYPES:
        with pytest.raises(ValueError):
            EventLog().append(
                Event(ts=now(), type=event_type, task_id="task_1", run_id=None, persona_id=None)
            )


def test_the_two_operator_summary_rows_and_their_shared_arm_went_too():
    """The S21/S25 rule: the frozenset may not name a de-registered type, and a
    formatter arm behind that frozenset is unreachable once the row leaves."""

    from hermes_time import now

    from agent_runtime.events import Event, operator_event_summary

    assert set(RETIRED_EVENT_TYPES) & OPERATOR_SUMMARY_EVENT_TYPES == set()
    assert OPERATOR_SUMMARY_EVENT_TYPES - {"run.closed"} <= ALLOWED_EVENT_TYPES
    assert not any(name.startswith("repo_bundle.") for name in OPERATOR_SUMMARY_EVENT_TYPES)

    for event_type in ("repo_bundle.assigned", "repo_bundle.updated"):
        evt = Event(
            ts=now(),
            type=event_type,
            task_id=None,
            run_id=None,
            persona_id=None,
            payload={"repo": "launcher", "state": "running", "reason": "run started"},
        )
        assert operator_event_summary(evt) is None

    # The arm's own text is gone from the renderer, read through the function
    # source rather than the module so the cut-site comment does not self-flag.
    arm_source = inspect.getsource(events_module.operator_event_summary)
    for phrase in ("Assigned {repo} bundle", "Updated {repo} bundle"):
        assert phrase not in arm_source, phrase


def test_the_surviving_operator_summary_types_still_render():
    """Negative gate: this cut takes the repo_bundle arm and nothing else."""

    from hermes_time import now

    from agent_runtime.events import Event, operator_event_summary

    assert {"run.closed", "run.progress", "run.tool.started", "run.tool.finished"} <= OPERATOR_SUMMARY_EVENT_TYPES

    closed = Event(
        ts=now(),
        type="run.closed",
        task_id=None,
        run_id=None,
        persona_id="dev",
        payload={"state": "completed", "decision_type": "hand_off"},
    )
    assert operator_event_summary(closed) == "Closed dev run as completed after hand_off."


def test_historical_rows_still_read_back(isolate_agent_runtime_root):
    """S36/S44 precedent: deregistration gates APPENDS, not reads."""

    import json

    from agent_runtime import paths
    from agent_runtime.events import EventLog

    line = {
        "ts": "2026-07-01T00:00:00+00:00",
        "type": "repo_bundle.verified",
        "task_id": "task_historical",
        "run_id": None,
        "persona_id": "dev",
        "payload": {"repo_bundle_id": "bundle_abc", "repo": "launcher", "state": "verified"},
    }
    paths.events_path().parent.mkdir(parents=True, exist_ok=True)
    paths.events_path().write_text(json.dumps(line) + "\n", encoding="utf-8")

    rows = list(EventLog().iter_from_offset(0))
    assert [evt.type for _offset, evt in rows] == ["repo_bundle.verified"]
    assert rows[0][1].payload["repo_bundle_id"] == "bundle_abc"


def test_the_repo_lock_wire_is_now_a_constant(isolate_agent_runtime_root):
    """INVERTED at S56 — the follow-up wave this docstring was written for.

    S52 left ``repo_lock_summary`` readable and still published as
    ``repo_locks``, with no writer able to give it a row: the S47 item-5 class,
    pinned as a constant "so the next wave finds it stated, not inferred". S56 is
    that wave. The summary and the wire row are both gone; the constant is now
    an absence. (S57 deleted the module that held the summary, so the first half
    of this pin is now covered by ``test_the_whole_module_is_gone`` and what
    remains here is the WIRE half.)
    """

    assert "repo_locks" not in status.build_status()


def test_the_registry_lost_exactly_seven_contracts():
    """Delta-only; the absolute authority is S15's SURVIVING_EVENT_COUNT."""

    from tests.agent_runtime.test_s15_event_contract_pruning import SURVIVING_EVENT_COUNT

    assert [name for name in RETIRED_EVENT_TYPES if name in event_catalog()] == []
    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
