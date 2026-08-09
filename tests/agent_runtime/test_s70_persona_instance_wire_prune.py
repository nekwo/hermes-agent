"""S70 second half — the persona-instance wire prune that moved the snapshot
contract 53 -> 54.

Six keys left every ``persona_instances`` row. The interesting part of the cut is
not *that* they left — a constant assertion would pin that and prove nothing —
but the two properties that make the cut safe:

1. **The two alias removals lost no information.** ``current_work_assignment_id``
   and ``attached_task_id`` were byte-identical duplicates of the canonical key
   beside them. The ledger recorded ``attached_task_id`` as writer-less; it was
   not — ``current_task_id`` is written live by the steer/``goal_id`` lane — so
   the only honest justification for cutting it is that the value still ships
   under the canonical name. That is what ``test_the_alias_cut_kept_the_value``
   asserts, and it is the assertion that would have caught cutting a field that
   merely *looked* like a duplicate.

2. **Both projection lanes moved together.** The full snapshot row and the
   incremental ``state_patches`` row are separate producers of the same shape,
   and the Launcher folds whatever wire fields a patch carries with NO allowlist.
   Cutting only the snapshot copy would therefore let the first incremental
   update re-add a key the rebuild had dropped. The ledger's entry named only the
   snapshot; ``test_the_patch_lane_projects_no_key_the_snapshot_row_lacks`` is
   the gate that makes that class impossible rather than merely noticed.

The three writer-less fields that STAYED (``token_budget_used``,
``last_heartbeat_at``, ``current_assignment_id``) are pinned too, because "we
kept it on purpose" is exactly the fact a later cleanup pass will otherwise
re-discover as debt and delete.
"""

from __future__ import annotations

from agent_runtime import state_patches
from agent_runtime.models import PersonaInstance, WorkerSessionState
from agent_runtime.persona_assignments import persona_instance_summary
from agent_runtime.persona_instance_identity import (
    HELD_REASON_ACTIVE,
    classify_orphan_persona_instances,
)


#: Left the row at contract 54.
CUT_KEYS = (
    "current_work_assignment_id",
    "attached_task_id",
    "context_receipt_id",
    "compression_receipt_id",
    "tool_budget_used",
    "watchdog_warning_count",
)

#: Writer-less but READER-ful — deliberately still on the row. Each name here is
#: paired with the consumer that keeps it alive, so a future pruner has to argue
#: with the reader rather than with a bare "kept for contract".
KEPT_KEYS = {
    "token_budget_used": "Launcher token-total fallback (totalTokens ?? tokenBudgetUsed)",
    "last_heartbeat_at": "Launcher roster recency + Agent Gateway state frame + orphan heartbeat HOLD",
    "current_assignment_id": "Launcher roster fold against the persona_assignments block",
}


def _instance(**overrides) -> PersonaInstance:
    base = dict(
        id="personainst_fixture",
        persona_id="dev",
        role="dev",
        display_name="Fixture Dev",
        profile_id="gpt-launcher",
        runtime_root="runtime://fixture",
        state=WorkerSessionState.IDLE,
        mode="chat",
    )
    base.update(overrides)
    return PersonaInstance(**base)


def test_the_six_cut_keys_are_gone_from_the_snapshot_row():
    row = persona_instance_summary(_instance())
    still_present = [key for key in CUT_KEYS if key in row]
    assert not still_present, f"contract 54 removed these: {still_present}"


def test_the_writer_less_keys_with_live_readers_stayed():
    row = persona_instance_summary(_instance())
    missing = {key: why for key, why in KEPT_KEYS.items() if key not in row}
    assert not missing, (
        "these are writer-less but NOT reader-less; dropping one silently "
        f"retires the consumer named beside it: {missing}"
    )


def test_the_alias_cut_kept_the_value():
    """The whole safety argument for cutting the two aliases.

    A task-bound, assignment-bearing instance must still expose both values under
    their canonical keys. If a future edit removes the canonical key instead of
    the alias, or removes both, this goes red where a key-absence assertion would
    stay green.
    """

    row = persona_instance_summary(
        _instance(current_task_id="task_live", current_assignment_id="assign_live")
    )
    assert row["current_task_id"] == "task_live"
    assert row["current_assignment_id"] == "assign_live"


def test_the_patch_lane_projects_no_key_the_snapshot_row_lacks():
    """The incremental lane may never carry a key the full rebuild dropped."""

    instance = _instance(current_task_id="task_live")
    snapshot_keys = set(persona_instance_summary(instance))
    patch_keys = set(state_patches._persona_instance_wire_row(instance, None))
    extra = patch_keys - snapshot_keys
    assert not extra, (
        "state_patches would ADD these keys to a row the full snapshot no longer "
        f"has; the launcher folds patch fields with no allowlist: {sorted(extra)}"
    )


def test_the_store_to_wire_map_names_only_projected_fields():
    """Every wire field the steer map promises must actually be projected."""

    projected = set(state_patches._persona_instance_wire_row(_instance(), None))
    promised = {
        field
        for fields in state_patches._PERSONA_INSTANCE_STORE_TO_WIRE.values()
        for field in fields
    }
    assert promised <= projected, sorted(promised - projected)


def test_the_orphan_hold_still_protects_a_row_with_live_pointers():
    """The alias slots left ``classify_orphan_persona_instances`` in this wave.

    They were unreachable afterwards (both callers pass rows keyed on the
    canonical names), and both always equalled the canonical key beside them — so
    an active row must still be HELD, never pruned. This is the assertion that
    catches removing an alias slot whose canonical twin was not in the predicate.
    """

    row = persona_instance_summary(
        _instance(id="personainst_orphan", persona_id="ghost", role="ghost", current_task_id="task_live")
    )
    result = classify_orphan_persona_instances(
        [row],
        backed_persona_ids=(),
        backed_profile_names=(),
        profile_catalog_authoritative=True,
    )
    assert [entry["persona_instance_id"] for entry in result["held"]] == ["personainst_orphan"]
    assert result["prunable"] == []
    assert result["held"][0]["reason"] == HELD_REASON_ACTIVE
