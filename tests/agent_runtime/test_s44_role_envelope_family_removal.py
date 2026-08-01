"""S44 retires the role_envelopes / role_checklists STORE family.

The ledger item this closes (docs/agent-runtime-harness/19-deferred-debt-ledger.md
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
``tests/agent_runtime/test_s27_live_module_dead_insides.py`` pinned it as LIVE,
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
"""

from __future__ import annotations

from importlib.util import find_spec

import pytest

from agent_runtime import checkpoint, paths, role_checklists
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

#: Everything that leaves ``role_checklists`` with the store.
REMOVED_CHECKLIST_NAMES = (
    "RoleChecklistStore",
    "RoleChecklist",
    "RoleChecklistItem",
    "checklist_for_task_stage",
    "checklist_summary",
    "item_summary",
    "normalize_role_id",
    "TaskLike",
    "_typed_stage_for_checklist",
    "_promotion_rule",
    "_template_items",
    "_item",
    "_safe_payload",
    "_dedupe",
)

#: The path helpers that addressed the two archived store directories.
REMOVED_PATH_HELPERS = (
    "role_envelopes_dir",
    "role_envelopes_task_dir",
    "role_envelope_path",
    "role_checklists_dir",
    "role_checklists_task_dir",
    "role_checklist_path",
    "role_checklist_events_dir",
    "role_checklist_event_path",
)


def test_the_role_envelope_module_is_gone():
    assert find_spec("agent_runtime.role_envelopes") is None


def test_the_checklist_store_family_is_gone():
    assert [name for name in REMOVED_CHECKLIST_NAMES if hasattr(role_checklists, name)] == []


def test_the_one_live_checklist_name_survives_and_still_rejects():
    """KEEP: the payload-structure validator, its exclusive helpers, and the
    three vocabularies it raises with."""

    from agent_runtime.decision_schema import DecisionPayloadInvalid

    assert callable(role_checklists.validate_checklist_payload_structure)
    assert callable(role_checklists._repair_message)
    assert callable(role_checklists._safe_text)
    assert role_checklists.CHECKLIST_ITEM_STATUSES
    assert role_checklists.SELF_APPROVAL_STATUSES
    assert role_checklists.CHECKLIST_UPDATE_KEYS

    with pytest.raises(DecisionPayloadInvalid):
        role_checklists.validate_checklist_payload_structure({"checklist_updates": "not-a-list"})
    with pytest.raises(DecisionPayloadInvalid):
        role_checklists.validate_checklist_payload_structure({"self_approval_status": "nonsense"})
    # A well-formed payload is still accepted.
    assert (
        role_checklists.validate_checklist_payload_structure(
            {"checklist_updates": [{"item_id": "patch", "status": "verified"}]}
        )
        is None
    )


def test_the_registry_chokepoint_still_reaches_the_validator():
    """The wire, not just the two ends: ``validate_payload_keys`` must actually
    invoke the surviving name (the relay-regression idiom)."""

    import inspect

    from agent_runtime.decision_contract_registry import validate_payload_keys

    source = inspect.getsource(validate_payload_keys)
    assert "from .role_checklists import validate_checklist_payload_structure" in source
    assert "validate_checklist_payload_structure(payload)" in source


def test_the_six_contracts_are_deregistered():
    catalog = event_catalog()
    assert [name for name in RETIRED_EVENT_TYPES if name in catalog] == []
    assert [name for name in RETIRED_EVENT_TYPES if name in ALLOWED_EVENT_TYPES] == []


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
    assert OPERATOR_SUMMARY_EVENT_TYPES <= ALLOWED_EVENT_TYPES


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


def test_the_orphaned_path_helpers_are_gone():
    assert [name for name in REMOVED_PATH_HELPERS if hasattr(paths, name)] == []


def test_the_lookalike_keep_set_survives():
    """Names one bare-word grep away from this cut — every one still live."""

    # The checkpoint classes that DO still have a writer.
    for name in ("persona_instances", "boards", "repo_bundles", "self_tests", "packet_artifacts"):
        assert name in checkpoint.ENTITY_CLASS_NAMES
    # ``self_tests`` shares the recursive per-task shape the two removals had.
    assert callable(paths.self_tests_dir)
    assert callable(paths.self_test_task_dir)
    # The decision-lane events that sat on the same registry lines.
    for name in ("run.closed", "run.progress", "run.tool.started", "run.tool.finished"):
        assert name in ALLOWED_EVENT_TYPES
    # ``role_sessions`` was already cut in wave 3; ``role_contracts`` goes in
    # S45. Neither is what this file removed.
    assert find_spec("agent_runtime.role_checklists") is not None


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
