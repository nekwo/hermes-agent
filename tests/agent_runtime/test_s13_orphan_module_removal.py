"""S13 retires the eight mission-lane orphan modules left behind by S4-S12.

Every module named here had **zero production importers** after the mission lane
came out: the goal/task machinery that called them is gone, so they could only be
reached from their own tests. They are removed as features, not refactored.

The keep-side names that merely *look* like this set are pinned below so a future
bare-word grep cannot take them out with the orphans.
"""

from __future__ import annotations



REMOVED_MODULES = (
    "agent_runtime.preflight",
    "agent_runtime.worklog",
    "agent_runtime.plan_review",
    "agent_runtime.actions",
    "agent_runtime.snapshot_audit",
    "agent_runtime.missing_input",
    "agent_runtime.skill_context",
    "agent_runtime.transitions",
)






def test_the_lookalike_keep_set_survives():
    """Names one bare-word grep away from the removal set — all still live."""

    from agent_runtime import parity, patch_coverage, snapshot, states, task_store_stub

    # ``transitions`` went; ``TaskState`` did not — board/office and the chat lane
    # still read it.
    assert states.TaskState is not None
    # ``snapshot_audit`` went; ``snapshot`` is the live read-model projection.
    assert callable(snapshot.build_snapshot)
    assert callable(parity.ProjectionAccountant)
    assert patch_coverage is not None
    assert task_store_stub.TaskStoreStub is not None
