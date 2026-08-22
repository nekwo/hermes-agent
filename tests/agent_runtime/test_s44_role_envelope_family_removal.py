"""S44 retires the role_envelopes / role_checklists STORE family.

The ledger item this closes (docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/19-deferred-debt-ledger.md
item 1) sat on an operator ruling, not on missing work: the family was already
production-caller-free, but cutting it retires six registered event contracts and
therefore moves the absolute event count and ``contract_hash()``. The operator
ruled CUT on 2026-07-31.

What was actually verified before the cut, name by name:

* ``agent_runtime/role_envelopes.py`` had exactly ONE production edge and it
  pointed OUTWARD — ``checkpoint``'s ``role_envelopes`` EntityClass reads the
  directory, nothing imports the module. Its only importer was
  ``tests/agent_runtime/test_projector.py``, where the import was never used.
  S43 already cut ``role_envelope_summary`` from it for the same reason.
* ``RoleEnvelopeStore.open_or_resume`` annotates ``task: Task``. The ``Task``
  record went at S8 and this module never imported it — a latent ``NameError``
  that ``from __future__ import annotations`` hid, exactly the S27 pattern. A
  module whose entry point cannot survive ``typing.get_type_hints`` has not had a
  caller in a long time.
* ``agent_runtime/role_checklists.py`` keeps ONE name:
  ``validate_checklist_payload_structure``, reached from
  ``decision_contract_registry.validate_payload_keys`` (:177, :181) on every
  typed decision. Everything else in the module was reachable only from
  ``role_envelopes`` and dies with it. The module stays in place as a small leaf
  rather than moving — the smallest honest change.

**Wave-3's ``checklist_for_task_stage`` ruling is transitively falsified.**
``test_s27_live_module_dead_insides.py`` (a name that never existed under it;
the pin is in ``tests/agent_runtime/test_tombstone_registry.py`` -- corrected
MCF-78 2026-08-20) pinned it as LIVE,
and that was true *at the time*: ``RoleChecklistStore.open_or_create`` called it
and ``role_envelopes.py:91`` called that. The whole justification was the
``role_envelopes`` import. With that importer gone the chain is dead from its
root, so the S27 witness is RETARGETED (never deleted) to record the reversal.

**Event deregistration gates APPENDS, not reads** — the S36/S37 precedent. The
six contracts leave ``ALLOWED_EVENT_TYPES``, so ``EventLog.append`` refuses them
from here on, while historical ``role_envelope.*`` / ``role_checklist.*`` rows in
the live log still deserialize and read back. Nothing on the read path validates
a persisted row against the registry, and none of the six ever appeared in
``events.OPERATOR_SUMMARY_EVENT_TYPES``, so there is no summary arm to retire
with them (unlike S25's ``run.opened``).

Count: 88 -> 82. The absolute count authority stays
``tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT``;
this file asserts only its own -6 delta so there is one number to maintain.

-----------------------------------------------------------------------------
MIGRATED TO ``test_tombstone_registry.py`` (2026-08-01)
-----------------------------------------------------------------------------

Four pure-ABSENCE cases left this file for the data-driven registry, which
enforces them over a WIDER production scope with one comment- and
docstring-immune AST scanner instead of this file's hand-rolled ``hasattr``
loops:

* ``test_the_role_envelope_module_is_gone`` -> registry ``MODULE`` row
  ``agent_runtime.role_envelopes``.
* ``test_the_checklist_store_family_is_gone`` -> fourteen registry ``ATTR`` rows
  scoped to ``agent_runtime.role_checklists`` (the registry carries FOUR MORE
  than the tuple here did: ``stage_checklist_hud``,
  ``validate_decision_checklist_payload``,
  ``sanitize_decision_checklist_payload``,
  ``apply_decision_checklist_updates``). ``REMOVED_CHECKLIST_NAMES`` went with
  it.
* ``test_the_orphaned_path_helpers_are_gone`` -> eight registry ``ATTR`` rows
  scoped to ``agent_runtime.paths``. ``REMOVED_PATH_HELPERS`` went with it.
* ``test_the_six_contracts_are_deregistered`` -> six registry ``EVENT`` rows.

WHY THE SURVIVORS STAYED. Every remaining case asserts something a name-scan
cannot: the surviving validator's REJECTION behaviour and the chokepoint wire
that reaches it; the APPEND refusal and the "historical rows still read back"
half of the S36 precedent (the registry checks registration, never the log);
the ``OPERATOR_SUMMARY_EVENT_TYPES`` / ``checkpoint.ENTITY_CLASS_NAMES``
key-set pins, which are facts about produced collections; the lookalike KEEP set
and the ``repo_bundles`` inverted pin; and the delta test's agreement with S15's
single count authority. ``RETIRED_EVENT_TYPES`` stays because three survivors
read it.
"""

from __future__ import annotations

from importlib.util import find_spec

import pytest

from agent_runtime import checkpoint, paths
from agent_runtime.decision_contract_registry import event_catalog
from agent_runtime.events import ALLOWED_EVENT_TYPES, OPERATOR_SUMMARY_EVENT_TYPES


#: The six contracts retired with their only emitters.
RETIRED_EVENT_TYPES = (
    "role_envelope.opened",
    "role_envelope.continued",
    "role_envelope.paused",
    "role_envelope.closed",
    "role_checklist.created",
    "role_checklist.item_updated",
)


def test_appending_a_retired_role_event_is_refused():
    from hermes_time import now

    from agent_runtime.events import Event, EventLog

    for event_type in RETIRED_EVENT_TYPES:
        with pytest.raises(ValueError):
            EventLog().append(
                Event(ts=now(), type=event_type, task_id="task_1", run_id=None, persona_id=None)
            )


def test_historical_rows_still_read_back(isolate_agent_runtime_root):
    """S36 precedent: deregistration gates APPENDS, not reads. A pre-existing
    ``role_envelope.*`` line in the log must still deserialize."""

    import json

    from agent_runtime.events import EventLog

    line = {
        "ts": "2026-07-01T00:00:00+00:00",
        "type": "role_envelope.opened",
        "task_id": "task_historical",
        "run_id": None,
        "persona_id": "dev",
        "payload": {"envelope_id": "envelope_abc", "role_id": "dev"},
    }
    paths.events_path().parent.mkdir(parents=True, exist_ok=True)
    paths.events_path().write_text(json.dumps(line) + "\n", encoding="utf-8")

    rows = list(EventLog().iter_from_offset(0))
    assert [evt.type for _offset, evt in rows] == ["role_envelope.opened"]
    assert rows[0][1].payload["envelope_id"] == "envelope_abc"


def test_no_operator_summary_arm_went_missing():
    """None of the six was ever an operator-summary type, so unlike S25's
    ``run.opened`` there is no renderer branch to retire alongside."""

    assert set(RETIRED_EVENT_TYPES) & OPERATOR_SUMMARY_EVENT_TYPES == set()
    assert OPERATOR_SUMMARY_EVENT_TYPES - {"run.closed"} <= ALLOWED_EVENT_TYPES


def test_the_two_writerless_checkpoint_classes_are_gone():
    """Both directories were archived aside as writer-less on 2026-07-30; with
    the stores gone the EntityClass rows pointed at nothing any code can fill."""

    assert "role_envelopes" not in checkpoint.ENTITY_CLASS_NAMES
    assert "role_checklists" not in checkpoint.ENTITY_CLASS_NAMES
    assert [
        entity.name
        for entity in checkpoint.ENTITY_CLASSES
        if entity.name in {"role_envelopes", "role_checklists"}
    ] == []


def test_the_lookalike_keep_set_survives():
    """Names one bare-word grep away from this cut — every one still live."""

    # The checkpoint classes that DO still have a writer.
    for name in ("persona_instances", "boards"):
        assert name in checkpoint.ENTITY_CLASS_NAMES
    # INVERTED at S56 (2026-08-01), not deleted: ``repo_bundles`` was pinned in
    # the line above as a class that still has a writer. That stopped being true
    # when S52 removed the bundle write lane and S56 removed the four status
    # projections that were its last readers, so the class went by exactly the
    # rule S44 applied to ``role_envelopes`` / ``role_checklists``. Owned by
    # tests/agent_runtime/test_s52_repo_bundle_write_lane_removal.py.
    assert "repo_bundles" not in checkpoint.ENTITY_CLASS_NAMES
    # The decision-lane events that sat on the same registry lines.
    for name in ("run.progress", "run.tool.started", "run.tool.finished"):
        assert name in ALLOWED_EVENT_TYPES
    assert "run.closed" in OPERATOR_SUMMARY_EVENT_TYPES
    # ``role_sessions`` was already cut in wave 3; ``role_contracts`` goes in
    # S45. Neither is what this file removed.
    assert find_spec("agent_runtime.role_checklists") is None


def test_the_registry_lost_exactly_six_contracts():
    """Delta-only; the absolute authority is S15's SURVIVING_EVENT_COUNT.

    S49 correction: this test's docstring said "delta-only" while the body
    pinned ``SURVIVING_EVENT_COUNT == 82`` — a SECOND copy of the absolute
    count, in the one file that promised not to hold one. Every later wave that
    legitimately moves the count broke this line, which is precisely the
    "one number to maintain" rule it was written to honour. The absolute
    assertion is dropped; what stays is S44's actual invariant — its six types
    are gone, and the catalog still agrees with the single authority.
    """

    from tests.agent_runtime.test_s15_event_contract_pruning import SURVIVING_EVENT_COUNT

    assert [name for name in RETIRED_EVENT_TYPES if name in event_catalog()] == []
    assert len(event_catalog()) == SURVIVING_EVENT_COUNT
