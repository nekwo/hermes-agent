"""Three LIVE event types were emitted but never registered — silent drops.

``EventLog.append`` refuses an unregistered type with ``ValueError``. Each of
these three emitters sits inside a ``try/except`` (or ``_append_store_event``'s
best-effort wrapper), so the refusal was swallowed and the row never reached
``events.jsonl``. That is worse than a crash: the stream/read-model pipeline is
watermark-gated on the EventLog, so an event-less mutation stays invisible to
every consumer (launcher snapshot, serve read model) until an unrelated event
happens to advance the offset.

afd6c0a83 found this class in passing while de-registering 67 unemittable
contracts and filed it for a separate pass. This is that pass, for the three
whose emitters are confirmed LIVE:

* ``realm.archived`` — ``RealmStore.archive`` (``agent_runtime/store.py``).
  The one realm mutation whose event never landed while ``realm.created`` /
  ``realm.updated`` / ``realm.adopted`` all did.
* ``persona_chat.deleted`` — ``_cmd_persona_chat_delete``
  (``hermes_cli/harness_parts/persona_commands.py``), the operator
  chat-delete CLI verb.
* ``worktree.orphans_reaped`` — ``reap_orphan_worktrees``
  (``agent_runtime/delivery_directive.py``), the LIVE janitor with two
  production callers (``harness_doctor`` and the ``worktree reap`` CLI verb);
  see docs/agent-runtime-harness/delivery-directive.md for the liveness ruling
  that separates it from that module's residue half.

Each contract below is derived from the ACTUAL emit call, not invented: the
summary fields are the keys the emitter passes on EVERY emission (append
validates ``summary_fields`` presence and raises under
``HERMES_EVENT_CONTRACT_STRICT``), everything else is a detail field.

STILL UNREGISTERED, NOW PERMANENTLY — ``worktree.task_reaped`` and
``bundle.worktree_reaped``. They were held back here because their emitters
(``_emit_task_reap_event`` and ``_reap_bundle_worktrees``) were reachable only
from the Task-declared directive path, which the delivery-directive liveness
ruling classed as unswept residue awaiting retirement: registering a contract
for an emitter about to be deleted would just re-create the unemittable-contract
debt S15 spent a stage clearing. S24 (354d7555a) deleted exactly those emitters
in the delivery-residue sweep, so the omission is now permanent rather than
provisional. The assertions below are unchanged — they were always "these two
are NOT registered".
"""

from __future__ import annotations

import pytest

from agent_runtime.decision_contract_registry import event_catalog, validate_event_payload
from agent_runtime.events import ALLOWED_EVENT_TYPES, Event, EventLog
from agent_runtime.models import Realm
from agent_runtime.store import RealmStore
from hermes_time import now


NEWLY_REGISTERED = ("realm.archived", "persona_chat.deleted", "worktree.orphans_reaped")

STILL_UNREGISTERED = ("worktree.task_reaped", "bundle.worktree_reaped")

# The ABSOLUTE registered-contract count has a single owner:
# tests/agent_runtime/test_s15_event_contract_pruning.SURVIVING_EVENT_COUNT
# (now 93 = 90 after S17, plus the three below). This file asserts only its own
# delta so there is one number to maintain, not three.

# The exact payloads the three emitters build, key for key.
LIVE_PAYLOADS = {
    # store.py RealmStore.archive -> _append_store_event(realm_id=, name=)
    "realm.archived": {"realm_id": "realm_alpha", "name": "Alpha"},
    # persona_commands.py _cmd_persona_chat_delete
    "persona_chat.deleted": {
        "session_id": "persona_chat_personainst_dev_live",
        "deleted_session": True,
        "cleared_bindings": ["personainst_dev_live"],
        "closed_assignment_ids": [],
        "requested_by": "cli",
    },
    # delivery_directive.py reap_orphan_worktrees
    "worktree.orphans_reaped": {"reaped_count": 2, "kept_count": 1, "captured": ["patch_a.diff"]},
}


def test_the_live_emitted_types_are_registered():
    catalog = event_catalog()
    for event_type in NEWLY_REGISTERED:
        assert event_type in ALLOWED_EVENT_TYPES, event_type
        assert event_type in catalog, event_type
        assert catalog[event_type]["display_label"], event_type

    assert set(catalog) == ALLOWED_EVENT_TYPES


def test_each_real_emit_payload_satisfies_its_contract_and_appends():
    """The append used to raise and be swallowed; now it lands a row.

    ``validate_event_payload`` returning () is the strict-mode gate: under
    ``HERMES_EVENT_CONTRACT_STRICT`` a missing summary field raises, so a
    contract whose summary fields the real emitter does not always pass would
    convert a silent drop into a CI failure.
    """

    log = EventLog()
    for event_type in NEWLY_REGISTERED:
        payload = LIVE_PAYLOADS[event_type]
        assert validate_event_payload(event_type, payload) == (), event_type
        log.append(Event(ts=now(), type=event_type, task_id=None, run_id=None, persona_id=None, payload=payload))

    appended = [event.type for event in log.tail(len(NEWLY_REGISTERED))]
    assert appended == list(NEWLY_REGISTERED)


def test_realm_archive_now_lands_its_event_through_the_live_store_path():
    """End-to-end on the one emitter this change can drive directly."""

    store = RealmStore()
    realm = store.create(name="Alpha")

    store.archive(realm.id)

    event = EventLog().tail(1)[0]
    assert event.type == "realm.archived"
    assert event.payload["realm_id"] == realm.id
    assert event.payload["name"] == "Alpha"
    assert store.get(realm.id).archived is True
    assert isinstance(realm, Realm)


def test_the_residue_half_emitters_stay_unregistered():
    for event_type in STILL_UNREGISTERED:
        assert event_type not in ALLOWED_EVENT_TYPES, event_type
        with pytest.raises(ValueError):
            EventLog().append(
                Event(ts=now(), type=event_type, task_id=None, run_id=None, persona_id=None, payload={})
            )
