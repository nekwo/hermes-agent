from __future__ import annotations

from agent_runtime.actions import HarnessAction, HarnessActionType


def test_run_slot_action_type_is_stable():
    assert HarnessActionType.RUN_SLOT.value == "run_slot"


def test_run_slot_action_carries_slot_id_without_breaking_legacy_fields():
    action = HarnessAction(HarnessActionType.RUN_SLOT, "task_1", reason="stage needs builder", slot_id="builder")

    assert action.task_id == "task_1"
    assert action.run_id is None
    assert action.slot_id == "builder"
    assert action.reason == "stage needs builder"
