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

from hermes_time import now

from agent_runtime.events import (
    ALLOWED_EVENT_TYPES,
    OPERATOR_SUMMARY_EVENT_TYPES,
    Event,
    operator_event_summary,
)

RETIRED = "repo_bundle.delivered"






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


def test_the_rest_of_the_repo_bundle_lane_followed_at_s52():
    """INVERTED at S52 (2026-08-01), the S47 precedent.

    This test was ``test_the_repo_bundle_lane_is_not_collateral`` and it pinned
    the seven sibling types as LIVE, riding ``RepoBundleStore.update``. That was
    true when S25 wrote it and is false now: S52 deleted the store's WRITE lane
    whole -- ``update`` included -- for want of a production caller, so all
    seven followed ``delivered`` out of the registry for exactly the reason this
    file already documents.

    The pin is INVERTED rather than deleted, and it keeps asserting the
    operator-summary half, because that half is what changed shape: at S25 the
    two summary arms had to SURVIVE the cut; at S52 they had to go WITH it, or
    ``OPERATOR_SUMMARY_EVENT_TYPES`` would name de-registered types and break
    the S21 invariant. A pin whose subject reverses is evidence, not litter.
    """

    retired_with_the_write_lane = {
        "repo_bundle.created",
        "repo_bundle.updated",
        "repo_bundle.assigned",
        "repo_bundle.running",
        "repo_bundle.verified",
        "repo_bundle.rejected",
        "repo_bundle.woke",
    }
    assert retired_with_the_write_lane & ALLOWED_EVENT_TYPES == set()
    assert retired_with_the_write_lane & OPERATOR_SUMMARY_EVENT_TYPES == set()

    # The formatter arm that rendered them is unreachable and gone: the two
    # types that used to produce a sentence now produce nothing at all.
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
