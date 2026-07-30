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


def test_stage_intent_still_reads_a_plain_status_string():
    """The one consumer never depended on the enum -- prove it on a bare str."""

    from types import SimpleNamespace

    from agent_runtime.stage_intent import stage_requires_product_edit

    task = SimpleNamespace(
        id="task_s23",
        title="No product edits",
        description="Request the existing proof recipe without editing product code.",
        affected_repos=["EterniaBackend"],
        risk_flags=["no_product_edits"],
    )
    stage = SimpleNamespace(
        id="backend_contract_smoke",
        title="Backend Contract Smoke",
        objective="Request the existing backend_contract_smoke no_product_edit proof recipe without modifying product repositories.",
        status="implementing",
        acceptance_criteria=["No product repository edits are made."],
        test_plan=["request_test_run with stage_id=backend_contract_smoke and recipe_id=backend_contract_smoke"],
        affected_paths=[],
        audit_notes=[],
        corrections=[],
    )

    assert stage_requires_product_edit(task, stage) is False
