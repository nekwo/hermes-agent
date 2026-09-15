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


from agent_runtime import states






def test_the_sibling_state_enums_survive():
    """States that merely look like the removal set -- all still have callers."""

    # TaskState: board/office rows and the chat lane still read it; its legacy
    # value coercion is the reason it cannot be replaced by a bare string.
    assert states.TaskState("dev_implementing") is states.TaskState.RUNNING
    # RunState / WorkerSessionState remain imported by models.py. S58 removed
    # PossessionState after its final import proved dead.
    assert states.RunState.WAITING_ON_APPROVAL == "waiting_on_approval"
    assert states.WorkerSessionState.POSSESSED == "possessed"
