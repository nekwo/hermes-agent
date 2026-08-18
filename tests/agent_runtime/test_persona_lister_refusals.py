"""ML-15 — the persona-lane arms that decided on a lister which silently skipped
unreadable rows.

The house pattern is EG-1.5's ``scan_actors``/``ActorScan`` and ML-8's typed
``sync_unknowable`` refusal: the rows and the count of what would not decode
have to travel TOGETHER, and any arm that WRITES or DELETES on the short answer
refuses typed rather than acting on a list it knows is incomplete. Display-only
readers carry the count instead.

Every gate here drives the unreadable count with TWO distinct values (1 then 2),
because a constant-zero or constant-one mutant would satisfy a single-valued
probe. Every write gate also asserts, with a recorder, that NOTHING was written
— a typed refusal that still performed the write would be a worse lie than the
silent skip it replaced.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from hermes_time import now

from agent_runtime import paths
from agent_runtime.errors import PersonaInstancesUnreadable, WorkspaceDeleteBlocked
from agent_runtime.persona_assignments import (
    PERSONA_ROWS_UNREADABLE,
    PersonaAssignmentStore,
    PersonaInstanceRetireError,
    PersonaInstanceStore,
    persona_instance_id_for,
)
from agent_runtime.serde import to_jsonable
from utils import atomic_json_write


# --- fixtures ------------------------------------------------------------


def _instance(placement_id: str, persona_id: str = "dev", display_name: str = "Dev"):
    return PersonaInstanceStore().add_instance(
        persona_id=persona_id,
        placement_id=placement_id,
        display_name=display_name,
    )


def _corrupt_instance_rows(count: int) -> list[str]:
    """Write *count* persona-instance files that exist and will not decode.

    Not empty files and not deleted files: the whole defect class is about a row
    that IS there and cannot be read, which is the state an interrupted write, a
    quarantined file, or a transient AV hold leaves behind. A missing row is a
    different (and correctly handled) fact.
    """

    directory = paths.persona_instances_dir()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for index in range(count):
        path = directory / f"personainst_corrupt_{index}.json"
        path.write_text('{"id": "personainst_corrupt_', encoding="utf-8")
        written.append(path.name)
    return written


def _corrupt_assignment_rows(count: int) -> list[str]:
    directory = paths.persona_assignments_dir()
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for index in range(count):
        path = directory / f"assign_corrupt_{index}.json"
        path.write_text("{not json at all", encoding="utf-8")
        written.append(path.name)
    return written


def _seed_assignment(persona_id: str, *, persona_instance_id: str, state: str = "queued"):
    from agent_runtime.models import PersonaAssignment

    ts = now()
    assignment = PersonaAssignment(
        id=f"assign_{uuid.uuid4().hex[:12]}",
        persona_instance_id=persona_instance_id,
        persona_id=persona_id,
        kind="free_floating_message",
        state=state,
        title=f"{persona_id} residual assignment",
        message="historical row",
        created_by="test",
        created_at=ts,
        updated_at=ts,
    )
    atomic_json_write(
        paths.persona_assignment_path(assignment.id),
        to_jsonable(assignment),
        indent=2,
        sort_keys=True,
    )
    return assignment


def _instance_row_bytes() -> dict[str, bytes]:
    """Every live instance row's bytes, keyed by filename.

    THE write recorder for this lane. Comparing bytes before and after is what
    turns "the arm refused" into "the arm refused AND changed nothing" — the
    steering repair's defect was a WRITE, so a gate that only asserted the
    refusal's shape would pass against a mutant that refused loudly and stripped
    the edges anyway.
    """

    directory = paths.persona_instances_dir()
    if not directory.exists():
        return {}
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.json"))}


# --- site 1: the steering repair (a delete-shaped write) -----------------


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_steering_repair_refuses_typed_when_a_row_will_not_decode(
    isolate_agent_runtime_root, corrupt_count
):
    """The strip is derived by SUBTRACTION from ``live_ids``, so an unreadable
    row reads as a retired parent and every child edge naming it is destroyed.

    Driven with two counts because the refusal must carry the REAL number, not a
    constant a mutant could mint without scanning.
    """

    store = PersonaInstanceStore()
    parent = _instance("steer_parent")
    child = _instance("steer_child", display_name="Child")
    store.steer(child.id, parent_instance_id=parent.id)

    # The parent is live and readable; the corruption is elsewhere entirely.
    # That is the point: the repair's answer for THIS child is poisoned by a row
    # that has nothing to do with it, because the predicate is set membership.
    _corrupt_instance_rows(corrupt_count)

    before = _instance_row_bytes()
    result = store.repair_missing_steering_references(apply=True)

    assert result["applied"] is False
    assert result["repaired"] == []
    assert result["repaired_count"] == 0
    assert result["refused"] == {
        "scope": "persona_instances",
        "reason": PERSONA_ROWS_UNREADABLE,
        "unreadable": corrupt_count,
    }

    # Nothing was written. The recorder is the half of this gate that a mutant
    # cannot satisfy by refusing after the fact.
    assert _instance_row_bytes() == before
    assert PersonaInstanceStore().get(child.id).steered_by == [parent.id]


def test_steering_repair_destroys_no_edge_it_cannot_prove_is_dangling(
    isolate_agent_runtime_root,
):
    """The RECORDER half, deliberately on its own gate.

    Witness diversity (C5/C16): the gate above asserts the refusal's SHAPE, so a
    mutant reverting to the short lister fails it on the shape alone — which
    would leave the actually-serious claim, that the old code performed a
    DELETE-SHAPED WRITE, unwitnessed. This gate asserts only the store: the
    child's live edge, byte for byte. Under the short lister it goes red because
    the edge is genuinely stripped, which is the defect ML-15 is about.
    """

    store = PersonaInstanceStore()
    parent = _instance("recorder_parent")
    child = _instance("recorder_child", display_name="Child")
    store.steer(child.id, parent_instance_id=parent.id)

    # THE PARENT ITSELF is the row that will not decode, which is the scenario
    # that actually destroys data: ``live_ids`` is built by reading rows, so a
    # parent that will not decode is absent from it, and the child's edge to it
    # is stripped as dangling. Corrupting an unrelated bystander row does NOT
    # exercise this — an earlier draft of this gate did exactly that and passed
    # against the mutant, because nothing pointed at the row it corrupted.
    paths.persona_instance_path(parent.id).write_text(
        '{"id": "personainst_recorder_parent"', encoding="utf-8"
    )

    before = _instance_row_bytes()
    try:
        store.repair_missing_steering_references(apply=True)
    except Exception:  # noqa: BLE001 — a raising arm is still an arm that wrote nothing
        pass

    assert _instance_row_bytes() == before
    assert PersonaInstanceStore().get(child.id).steered_by == [parent.id]


def test_steering_repair_still_strips_a_genuinely_dangling_parent(
    isolate_agent_runtime_root,
):
    """The refusal must not have retired the repair itself.

    A store that reads cleanly still strips an edge whose parent is truly gone —
    otherwise the fix would have bought its safety by disabling the feature, and
    the gate above would pass against a repair that refuses unconditionally.
    """

    store = PersonaInstanceStore()
    parent = _instance("gone_parent")
    child = _instance("live_child", display_name="Child")
    store.steer(child.id, parent_instance_id=parent.id)
    paths.persona_instance_path(parent.id).unlink()

    result = store.repair_missing_steering_references(apply=True)

    assert result["applied"] is True
    assert result["refused"] is None
    assert result["repaired_count"] == 1
    assert PersonaInstanceStore().get(child.id).steered_by == []
