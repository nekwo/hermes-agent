"""S23 retires ``StageStatus`` -- the per-stage status of the removed stage graph.

The mission lane's typed stages went in S7/S8 and the snapshot projection that
read their status went in S9. Nothing in production has resolved a
``StageStatus`` since: the one remaining consumer, ``stage_intent``, reads
``stage.status`` off a duck-typed row and normalizes it through
``.value if hasattr(...) else str(...)``, so it never needed the enum.

The four sibling states in ``agent_runtime.states`` are pinned below -- they are
one bare-word grep away from this cut and every one of them is live.
"""

from __future__ import annotations

import pytest

import agent_runtime
from agent_runtime import states


def test_stage_status_is_gone_from_states():
    assert not hasattr(states, "StageStatus")


def test_stage_status_is_gone_from_the_package_export_surface():
    assert "StageStatus" not in agent_runtime.__all__
    with pytest.raises(ImportError):
        from agent_runtime import StageStatus  # noqa: F401


def test_the_sibling_state_enums_survive():
    """States that merely look like the removal set -- all still have callers."""

    # TaskState: board/office rows and the chat lane still read it; its legacy
    # value coercion is the reason it cannot be replaced by a bare string.
    assert states.TaskState("dev_implementing") is states.TaskState.RUNNING
    # RunState / WorkerSessionState / PossessionState are imported by models.py.
    assert states.RunState.WAITING_ON_APPROVAL == "waiting_on_approval"
    assert states.WorkerSessionState.POSSESSED == "possessed"
    assert states.PossessionState.RELEASE_PENDING == "release_pending"


def test_the_last_stage_status_consumer_outlived_the_enum_and_then_went_too():
    """RETARGETED at S45.

    This test used to construct a duck-typed stage with ``status="implementing"``
    and prove ``stage_intent.stage_requires_product_edit`` handled a bare string,
    because ``stage_intent`` was the ONE consumer S23 had to keep working after
    ``StageStatus`` was deleted.

    S45 deleted ``stage_intent`` whole — zero production importers, anchored only
    by ``test_stage_intent.py``. So the enum's last consumer is gone as well,
    which retires the question this test asked rather than answering it. Kept as
    an absence assertion: S23's whole justification for dropping ``StageStatus``
    was "the one remaining consumer reads a plain string", and a reader hitting
    that reasoning should be able to see the consumer no longer exists."""

    from importlib.util import find_spec

    assert find_spec("agent_runtime.stage_intent") is None
    # The enum stays gone, and now for a second, independent reason.
    assert not hasattr(states, "StageStatus")
