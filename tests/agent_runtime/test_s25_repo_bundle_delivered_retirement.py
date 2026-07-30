"""S25 — ``repo_bundle.delivered`` retirement, the immediate follow-on to S24.

354d7555a removed ``RepoBundleStore.mark_delivered`` with the delivery-capture
path. That method was the contract's ONLY emitter: every other
``repo_bundle.*`` type still rides a live ``RepoBundleStore.update`` call
(created / updated / assigned / running / verified / rejected / woke), and this
one rode nothing. Left registered it would be exactly the debt S15 spent a stage
clearing — a shape the prompt contract advertises, the manifest publishes, and
``EventLog.append`` accepts, that no production writer can ever produce.

Same reasoning as the ``run.opened`` retirement it ships beside, including the
operator-summary row: ``operator_event_summary`` early-returns ``None`` outside
``OPERATOR_SUMMARY_EVENT_TYPES``, and that frozenset may not name a
de-registered type (events.py's invariant, pinned by
``test_s21_hollow_seam_ring``), so the ``Delivered … bundle (captured:…)``
formatter arm — and ``_safe_int``, which only that arm used — become unreachable
the moment the registration goes. Historical rows are untouched: ``append``
type-checks on WRITE only.

The two SURVIVING arms of that shared formatter branch are pinned below: this
retirement must not take ``repo_bundle.assigned`` / ``repo_bundle.updated`` with
it, and their emitters are live.
"""

from __future__ import annotations

import pytest
from hermes_time import now

from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import (
    ALLOWED_EVENT_TYPES,
    OPERATOR_SUMMARY_EVENT_TYPES,
    Event,
    EventLog,
    operator_event_summary,
)

RETIRED = "repo_bundle.delivered"


def test_repo_bundle_delivered_is_no_longer_a_registered_contract():
    assert RETIRED not in ALLOWED_EVENT_TYPES
    assert RETIRED not in event_catalog()


def test_appending_repo_bundle_delivered_is_refused(isolate_agent_runtime_root):
    with pytest.raises(ValueError):
        EventLog().append(
            Event(
                ts=now(),
                type=RETIRED,
                task_id=None,
                run_id=None,
                persona_id="dev",
                payload={"repo_bundle_id": "bundle_1", "repo": "hermes-agent", "state": "delivered"},
            )
        )


def test_the_operator_summary_row_and_its_formatter_arm_went_with_the_contract():
    assert RETIRED not in OPERATOR_SUMMARY_EVENT_TYPES
    evt = Event(
        ts=now(),
        type=RETIRED,
        task_id=None,
        run_id=None,
        persona_id="dev",
        payload={"repo": "hermes-agent", "state": "delivered", "diff_captured": False},
    )
    assert operator_event_summary(evt) is None


def test_the_repo_bundle_lane_is_not_collateral():
    """Every other repo_bundle type still rides a live RepoBundleStore.update;
    only ``delivered`` lost its writer."""

    live = {
        "repo_bundle.created",
        "repo_bundle.updated",
        "repo_bundle.assigned",
        "repo_bundle.running",
        "repo_bundle.verified",
        "repo_bundle.rejected",
        "repo_bundle.woke",
    }
    assert live <= ALLOWED_EVENT_TYPES

    assigned = Event(
        ts=now(),
        type="repo_bundle.assigned",
        task_id=None,
        run_id=None,
        persona_id=None,
        payload={"repo": "launcher", "state": "assigned"},
    )
    assert operator_event_summary(assigned) == "Assigned launcher bundle (assigned)."

    updated = Event(
        ts=now(),
        type="repo_bundle.updated",
        task_id=None,
        run_id=None,
        persona_id=None,
        payload={"repo": "launcher", "state": "running", "reason": "run started"},
    )
    assert operator_event_summary(updated) == "Updated launcher bundle to running: run started."


def test_the_delivered_only_int_helper_went_with_its_arm():
    """``_safe_int`` existed for the delivered arm's ``proof_count`` suffix and
    had no other caller — a private helper outliving its only branch is the
    residue this wave exists to stop leaving behind."""

    from agent_runtime import events

    assert not hasattr(events, "_safe_int")
