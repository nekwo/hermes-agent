"""S76 (§AX AX2) — the writerless persona-assignment lane leaves the wire.

Three keys leave, from two producers, and the tombstone registry's own rule is
why this file exists at all: a wire-key cut may not be pinned by a CODE row.
A name scan asserts that a SPELLING is absent from source; the fact that
actually matters is that the built FRAME no longer carries the key, and the only
honest way to check that is to build one.

WHAT LEFT, AND WHY EACH ONE COULD

* ``persona_assignments`` — the block itself. S8 had already evicted ``recent``
  to a pointer and kept ``active`` "for the launcher roster"; the launcher then
  deleted every read of the block (``6bf48ba26``) and keeps a test feeding a
  payload that still carries it while asserting nothing in it reaches the
  instance. The projection's cost was a directory walk of the assignment store on
  every snapshot AND every ``harness status``.
* ``persona_instance_runtime.assignment_store_enabled`` — a constant ``true``
  since the day it was written, and the one wire trace of a feature gate S56
  already removed the other half of.
* ``warnings`` — the top-level list. Its ONLY producer was
  ``_snapshot_warnings``, its only code was ``agent_already_assigned``, and its
  only input was the assignment list. It never had a launcher reader at all: the
  bridge mapper's forward allowlist does not carry the key, so it never reached
  ``MissionControlSnapshot.fromJson``, and the launcher tombstoned the code
  itself with the retired bridge-error vocabulary.

WHAT DELIBERATELY STAYED, so a later pass argues with the reader and not with a
bare "kept for contract": ``persona_instances[].current_assignment_id`` (parsed
live by the launcher into ``MissionAgentInstance.currentAssignmentId``, and
written hermes-side by the settle verbs), and the whole
``persona_assignments/`` directory on disk — read paths retire, stored bytes do
not.

The contract version does NOT move for this cut. The ruling and its argument are
written at ``snapshot._parity_envelope``'s version history under "54 KEPT
(AX2)"; ``test_snapshot_contract_version_authority`` is the file that owns the
number, and this one deliberately states no literal.
"""

from __future__ import annotations

import pytest

from agent_runtime.models import AgentPersona
from agent_runtime.snapshot import build_snapshot
from agent_runtime.status import build_status
from agent_runtime.store import AgentStore

pytestmark = pytest.mark.usefixtures("isolate_agent_runtime_root")


#: Every key this cut removed, spelled as the dotted path a reader would look
#: it up by. Both producers are asked for all three: ``status`` never carried
#: ``warnings``, and asking anyway is what stops a later edit from adding it
#: there because "the snapshot has one".
CUT_PATHS = (
    ("persona_assignments",),
    ("persona_instance_runtime", "assignment_store_enabled"),
    ("warnings",),
)


def _seed_persona() -> None:
    AgentStore().save(
        AgentPersona("dev", "Dev", "custom", None, None, None, ["file"], "")
    )


def _lookup(frame: dict, path: tuple[str, ...]):
    """``KeyError``-free walk. Returns a sentinel for "not there"."""

    cursor = frame
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return _MISSING
        cursor = cursor[key]
    return cursor


_MISSING = object()


@pytest.mark.parametrize("path", CUT_PATHS, ids=lambda path: ".".join(path))
def test_the_snapshot_frame_no_longer_carries_the_cut_key(path):
    _seed_persona()
    assert _lookup(build_snapshot(), path) is _MISSING


@pytest.mark.parametrize("path", CUT_PATHS, ids=lambda path: ".".join(path))
def test_the_status_frame_no_longer_carries_the_cut_key(path):
    _seed_persona()
    assert _lookup(build_status(), path) is _MISSING


def test_the_surviving_runtime_block_still_answers_its_own_question():
    """``persona_instance_runtime`` is pruned, not deleted.

    The block's ``enabled`` field belongs to a different lane (S56 unconditioned
    the roster on it and the key stayed as the wire's statement that it did), so
    a cut that took the whole block would have been the assignment lane reaching
    into a neighbour's contract.
    """

    _seed_persona()
    for frame in (build_snapshot(), build_status()):
        assert frame["persona_instance_runtime"] == {"enabled": True}


def test_neither_producer_opens_the_assignment_store_any_more(monkeypatch):
    """The strongest form: the READ is gone, not merely its projection.

    Emitting no key while still walking the directory would leave the whole cost
    this stage was about — a store scan on every snapshot build and every
    ``harness status`` — and nothing above would notice.
    """

    from agent_runtime import persona_assignments

    def _explode(self):
        raise AssertionError("no wire producer may scan the assignment store")

    monkeypatch.setattr(persona_assignments.PersonaAssignmentStore, "scan_all", _explode)

    _seed_persona()
    assert build_snapshot()["agents"]
    assert build_status()["agents"] is not None


def test_the_instance_row_keeps_the_assignment_pointer_the_launcher_parses():
    """``current_assignment_id`` survives the block that used to explain it.

    S70 kept this field with the reason "Launcher roster fold against the
    persona_assignments block". That fold is gone — but the field is not
    orphaned by it: the launcher parses ``current_assignment_id`` straight onto
    ``MissionAgentInstance``, and hermes' settle verbs write it. The reason
    changed; the verdict did not, and recording that here is what stops the next
    pruner from cutting it on a stale justification.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore

    _seed_persona()
    instance = PersonaInstanceStore().add_instance(
        persona_id="dev", placement_id="wire_prune", display_name="Dev"
    )
    rows = build_snapshot()["persona_instances"]
    assert "current_assignment_id" in rows[instance.id]
