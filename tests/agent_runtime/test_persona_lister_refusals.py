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


# --- site 2: the retire guard -------------------------------------------


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_retire_refuses_typed_when_an_assignment_row_will_not_decode(
    isolate_agent_runtime_root, corrupt_count
):
    """The guard's own docstring: a retire must never orphan a live assignment.

    It answers by SEARCHING, so its negative is worth what its enumeration is
    worth. An unreadable assignment row made it answer "none active" and the
    retire proceeded.
    """

    store = PersonaInstanceStore()
    instance = _instance("retire_me")
    _corrupt_assignment_rows(corrupt_count)

    with pytest.raises(PersonaInstanceRetireError) as excinfo:
        store.retire(instance.id, reason="placement deleted")

    error = excinfo.value
    assert error.code == "assignments_unknowable"
    assert error.detail["unreadable"] == corrupt_count
    assert error.detail["reason"] == PERSONA_ROWS_UNREADABLE
    assert error.detail["scope"] == "persona_assignments"

    # The retire did not happen: the row is still live and was never archived.
    assert paths.persona_instance_path(instance.id).exists()
    archive_root = paths.persona_instances_archive_dir()
    assert not list(archive_root.glob("*_retire/*.json")) if archive_root.exists() else True


def test_retire_still_refuses_a_genuinely_active_assignment(isolate_agent_runtime_root):
    """The pre-existing guard is intact and keeps its OWN word.

    C16: the new refusal must not have swallowed the old one. ``assignment_active``
    and ``assignments_unknowable`` describe different conditions — "I looked and
    found one" versus "I could not look" — and an operator acts differently on
    each, so they may never collapse into one sentence.
    """

    store = PersonaInstanceStore()
    instance = _instance("retire_blocked")
    _seed_assignment("dev", persona_instance_id=instance.id, state="queued")

    with pytest.raises(PersonaInstanceRetireError) as excinfo:
        store.retire(instance.id, reason="placement deleted")

    assert excinfo.value.code == "assignment_active"
    assert paths.persona_instance_path(instance.id).exists()


def test_retire_succeeds_on_a_clean_store(isolate_agent_runtime_root):
    """The fence is not a disabled verb."""

    store = PersonaInstanceStore()
    instance = _instance("retire_clean")
    result = store.retire(instance.id, reason="placement deleted")
    assert result["persona_instance_id"] == instance.id
    assert not paths.persona_instance_path(instance.id).exists()


def test_a_store_wide_assignment_fault_no_longer_reads_as_none_active(
    isolate_agent_runtime_root, monkeypatch
):
    """The blanket ``except Exception: return []`` is gone.

    It made the guard DOUBLY fail-open: on top of the lister dropping rows it
    could not decode, any store-wide failure was converted into the same
    confident "none active" the clean path returns. A fault now travels as a
    fault instead of borrowing the success arm's sentence.
    """

    store = PersonaInstanceStore()
    instance = _instance("retire_faulted")

    def _explode(self):
        raise OSError("the assignments directory is unreadable")

    monkeypatch.setattr(PersonaAssignmentStore, "scan_all", _explode)

    with pytest.raises(OSError):
        store.retire(instance.id, reason="placement deleted")

    assert paths.persona_instance_path(instance.id).exists()


# --- site 2b: the backlink release (a different fact, its own chokepoint) --


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_owner_removal_refuses_when_child_backlinks_cannot_all_be_seen(
    isolate_agent_runtime_root, corrupt_count
):
    """"Transactionally release EVERY child backlink" is a completeness promise.

    A child whose row will not decode keeps a backlink to an id that is about to
    stop resolving, and nothing downstream will ever revisit it — the owner is
    gone, so no later sweep can rediscover what the edge pointed at.

    A DIFFERENT fact from the retire guard's (which is about assignments), so it
    is fenced at its own single chokepoint and proved on its own.
    """

    store = PersonaInstanceStore()
    parent = _instance("owner_parent")
    child = _instance("owner_child", display_name="Child")
    store.steer(child.id, parent_instance_id=parent.id)
    _corrupt_instance_rows(corrupt_count)

    before = _instance_row_bytes()
    with pytest.raises(PersonaInstancesUnreadable) as excinfo:
        store._release_parent_references(parent.id)

    assert excinfo.value.code == "persona_instances_unreadable"
    assert str(corrupt_count) in str(excinfo.value)
    assert _instance_row_bytes() == before


# --- site 3: the session-uniqueness guard and the cycle validator ---------


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_session_ownership_guard_refuses_rather_than_answering_unowned(
    isolate_agent_runtime_root, corrupt_count
):
    """A uniqueness guard's ``False`` is only as good as its enumeration.

    An owner this loop cannot see makes the guard answer "unowned" and a second
    binding lands on a session that already had one.
    """

    store = PersonaInstanceStore()
    instance = _instance("owner_a")
    _corrupt_instance_rows(corrupt_count)

    with pytest.raises(PersonaInstancesUnreadable) as excinfo:
        store._session_owned_by_other_instance("chat-session-x", instance.id)

    assert excinfo.value.code == "persona_instances_unreadable"
    assert str(corrupt_count) in str(excinfo.value)


def test_session_ownership_guard_still_detects_a_real_owner(isolate_agent_runtime_root):
    """Clean store: the guard still answers the question it exists for."""

    store = PersonaInstanceStore()
    first = _instance("owner_first")
    second = _instance("owner_second", display_name="Second")
    session_id = PersonaInstanceStore().get(first.id).default_chat_session_id
    assert session_id, "the fixture must give the first instance a chat pointer"

    assert store._session_owned_by_other_instance(session_id, second.id) is True
    assert store._session_owned_by_other_instance(session_id, first.id) is False


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_steering_cycle_check_refuses_when_an_ancestor_will_not_decode(
    isolate_agent_runtime_root, corrupt_count
):
    """The walk ADMITS by exhausting the frontier, so every node it cannot open
    is a subgraph it silently declares cycle-free.

    The corrupted rows are placed on the ancestor path itself: the parent's
    ``steered_by`` names them, so the walk genuinely reaches a node it cannot
    read rather than merely coexisting with one.
    """

    store = PersonaInstanceStore()
    parent = _instance("cycle_parent")
    child = _instance("cycle_child", display_name="Child")

    corrupt_names = _corrupt_instance_rows(corrupt_count)
    corrupt_ids = [Path(name).stem for name in corrupt_names]

    # Point the parent at the unreadable ancestors, writing the row directly so
    # the steer write path's own guards do not reject the reference we need.
    parent_path = paths.persona_instance_path(parent.id)
    raw = json.loads(parent_path.read_text(encoding="utf-8"))
    raw["steered_by"] = corrupt_ids
    parent_path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(PersonaInstancesUnreadable) as excinfo:
        store._validate_no_steering_cycle(child.id, parent.id)

    assert excinfo.value.code == "persona_instances_unreadable"
    assert str(corrupt_count) in str(excinfo.value)


def test_steering_cycle_check_still_admits_and_still_rejects_on_a_clean_store(
    isolate_agent_runtime_root,
):
    """Both answers survive: an absent ancestor is not the unreadable case.

    ``get`` re-raises ``FileNotFoundError`` for an id with no row, which really
    does end the walk — nothing is reachable through a node that is not there —
    so a dangling reference must NOT be converted into a refusal.
    """

    store = PersonaInstanceStore()
    parent = _instance("clean_parent")
    child = _instance("clean_child", display_name="Child")

    # Admits a legitimate edge.
    store._validate_no_steering_cycle(child.id, parent.id)

    # A dangling ancestor id is stripped by the repair, not refused here.
    parent_path = paths.persona_instance_path(parent.id)
    raw = json.loads(parent_path.read_text(encoding="utf-8"))
    raw["steered_by"] = ["personainst_never_existed"]
    parent_path.write_text(json.dumps(raw), encoding="utf-8")
    store._validate_no_steering_cycle(child.id, parent.id)

    # A real cycle is still rejected.
    store.steer(parent.id, parent_instance_id=child.id)
    with pytest.raises(ValueError, match="cycle"):
        store._validate_no_steering_cycle(child.id, parent.id)
