"""S32 — ``decision_contract.parity`` retirement, the follow-on to S27.

5c16417f6 cut the simplified-contract PROJECTION half out of
``agent_runtime/simplified_contract.py``: ``DecisionProjection``,
``project_decision_for_execution``, the ``legacy_*_decision_from_*`` shims, the
three config predicates — and ``_record_parity``, which was the ONLY writer that
ever produced ``decision_contract.parity``. It recorded whether the public and
execution decision types agreed for the deterministic executor deleted in S5, and
had no caller after it.

Left registered it would be exactly the debt S15 spent a stage clearing: a shape
the prompt contract advertises, the manifest publishes, and ``EventLog.append``
accepts, that no production writer can ever produce. A text grep over the tree
(excluding the ``.claude/worktrees`` copies) finds no emitter left — only the
registry entry retired here and the retirement note in ``simplified_contract.py``
that names the deleted recorder, which the S19/S29 mention-vs-code-form rule
allows.

Unlike ``run.opened`` and ``repo_bundle.delivered``, this type never carried an
operator-summary row: ``OPERATOR_SUMMARY_EVENT_TYPES`` names six types and
``decision_contract.parity`` was never one of them, so no formatter arm and no
private helper go with it. That absence is pinned below rather than assumed.

Historical rows are untouched — ``append`` type-checks on WRITE only, so the ~580
``decision_contract.parity`` rows already in live event logs still read back
fine. The Launcher's historical-row suppressor
(``mission_agent_chat_adapter.dart``) reads those ROWS, not this registry, and is
deliberately kept.
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

RETIRED = "decision_contract.parity"


def test_decision_contract_parity_is_no_longer_a_registered_contract():
    assert RETIRED not in ALLOWED_EVENT_TYPES
    assert RETIRED not in event_catalog()


def test_appending_decision_contract_parity_is_refused(isolate_agent_runtime_root):
    with pytest.raises(ValueError):
        EventLog().append(
            Event(
                ts=now(),
                type=RETIRED,
                task_id=None,
                run_id=None,
                persona_id="dev",
                payload={
                    "mode": "shadow",
                    "status": "match",
                    "public_decision_type": "continue",
                    "execution_decision_type": "continue",
                },
            )
        )


def test_the_type_never_had_an_operator_summary_row_to_retire():
    """The delta this retirement does NOT have: no row, no formatter arm, no
    orphaned private helper — unlike run.opened and repo_bundle.delivered."""

    assert RETIRED not in OPERATOR_SUMMARY_EVENT_TYPES
    assert OPERATOR_SUMMARY_EVENT_TYPES == frozenset(
        {
            "repo_bundle.updated",
            "repo_bundle.assigned",
            "run.closed",
            "run.progress",
            "run.tool.started",
            "run.tool.finished",
        }
    )
    evt = Event(
        ts=now(),
        type=RETIRED,
        task_id=None,
        run_id=None,
        persona_id="dev",
        payload={"mode": "shadow", "status": "match"},
    )
    assert operator_event_summary(evt) is None


def test_the_recorder_and_its_projection_half_stay_gone():
    from agent_runtime import simplified_contract

    assert not hasattr(simplified_contract, "_record_parity")
    assert not hasattr(simplified_contract, "project_decision_for_execution")
    assert not hasattr(simplified_contract, "DecisionProjection")

    # What survives is the wire-value normalizer its live importers read.
    assert hasattr(simplified_contract, "public_decision_type")
    assert hasattr(simplified_contract, "public_decision_type_value")


def test_the_decision_lane_is_not_collateral():
    """``parity`` was the only decision-flavoured contract without a writer; the
    live decision/run types around it stay registered and emittable.

    S44 retarget: the four ``role_envelope.*`` types were pinned here as live
    collateral to protect. That was true at S32 — ``RoleEnvelopeStore.save`` was
    still their emitter. S44 deleted that store under the operator's ruling, so
    they moved to the retired side and are asserted below as gone. The run types
    they sat beside are untouched, which is exactly what this test exists to
    prove."""

    live = {
        "run.progress",
        "run.closed",
        "run.tool.started",
        "run.tool.finished",
    }
    assert live <= ALLOWED_EVENT_TYPES

    retired_at_s44 = {
        "role_envelope.opened",
        "role_envelope.continued",
        "role_envelope.paused",
        "role_envelope.closed",
    }
    assert retired_at_s44 & ALLOWED_EVENT_TYPES == set()
