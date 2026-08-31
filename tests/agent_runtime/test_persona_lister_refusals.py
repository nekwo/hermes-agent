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
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("persisted_persona_samples")

from agent_runtime import paths
from agent_runtime.errors import PersonaInstancesUnreadable, WorkspaceDeleteBlocked
from agent_runtime.persona_assignments import (
    PERSONA_ROWS_UNREADABLE,
    PersonaAssignmentStore,
    PersonaInstanceStore,
    persona_instance_id_for,
)


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


# --- site 2: the retire's ASSIGNMENT arms, RETIRED (AX2, 2026-08-31) -----
#
# This site used to hold four gates over two refusals: ``assignments_unknowable``
# (a row that will not decode makes "none active" unprovable) and the
# ``assignment_active`` guard whose negative it was protecting. Both are gone,
# and the ML-15 argument is exactly why they could go TOGETHER rather than one
# at a time: the unknowable arm was never a fact about the retire, it was a fact
# about the OTHER arm's enumeration. Once the guard it defends stops existing,
# an unreadable assignment row is a display problem for the settle verbs and
# nothing a retire has any business refusing over.
#
# What survives here is the ONE gate that outlived the pair — a retire over a
# store whose assignment directory is unreadable must not become a NEW way to
# fail. The positive behaviour (the retire completes, the assignment row is left
# on disk) is pinned where the guard lived, in
# ``test_persona_assignments.py::test_a_residual_active_assignment_no_longer_blocks_a_retire``.


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_an_undecodable_assignment_row_is_no_longer_a_retire_refusal(
    isolate_agent_runtime_root, corrupt_count
):
    """The retire completes over rows it cannot read, and does not touch them.

    Two values for the same reason ML-15 drives every count twice: an arm that
    re-grew a fence keyed on "any unreadable row at all" would be caught by
    either, but an arm keyed on a count would not.
    """

    store = PersonaInstanceStore()
    instance = _instance("retire_me")
    corrupt = _corrupt_assignment_rows(corrupt_count)

    result = store.retire(instance.id, reason="placement deleted")

    assert result["persona_instance_id"] == instance.id
    assert not paths.persona_instance_path(instance.id).exists()
    directory = paths.persona_assignments_dir()
    assert sorted(p.name for p in directory.glob("*.json")) == sorted(corrupt)


def test_retire_succeeds_on_a_clean_store(isolate_agent_runtime_root):
    """The fence is not a disabled verb."""

    store = PersonaInstanceStore()
    instance = _instance("retire_clean")
    result = store.retire(instance.id, reason="placement deleted")
    assert result["persona_instance_id"] == instance.id
    assert not paths.persona_instance_path(instance.id).exists()


def test_a_retire_no_longer_reads_the_assignment_store_at_all(
    isolate_agent_runtime_root, monkeypatch
):
    """The strongest form of the removal: the read is GONE, not merely tolerated.

    A retire that still scanned and swallowed the fault would pass the gate
    above and would have quietly re-acquired the fail-open arm ML-15 removed.
    Exploding the scan proves the call site left rather than grew an
    ``except``.
    """

    store = PersonaInstanceStore()
    instance = _instance("retire_faulted")

    def _explode(self):
        raise AssertionError("the retire must not scan the assignment store")

    monkeypatch.setattr(PersonaAssignmentStore, "scan_all", _explode)

    result = store.retire(instance.id, reason="placement deleted")

    assert result["persona_instance_id"] == instance.id
    assert not paths.persona_instance_path(instance.id).exists()


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


# --- site 4: the clarify-ticket index completeness claim ------------------


_CLARIFY_ROOT = "persona_chat_personainst_dev_abcdef123456"


def _clarify_ticket(store, **overrides) -> str:
    values = {
        "chat_session_id": _CLARIFY_ROOT,
        "persona_instance_id": "personainst_dev",
        "persona_id": "dev",
        "asked_by_client_message_id": "agent-relay-aaaaaaaaaaaa",
    }
    values.update(overrides)
    token = store.mint(**values)
    assert token
    return token


def _corrupt_ticket_files(store, count: int) -> None:
    root = store._root_dir()
    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (root / f"deadbeef{index}.json").write_text("{ truncated", encoding="utf-8")


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_the_ticket_index_refuses_to_claim_completeness_it_does_not_have(
    isolate_agent_runtime_root, corrupt_count
):
    """The marker means "the index may be trusted as complete", and it is
    PERMANENT — once written, the full-scan fallback is never taken again.

    So a ticket skipped during the rebuild is not skipped once, it is skipped
    forever. Since the skip is usually a transient hold on a file that reads fine
    a second later, the old behaviour punched a permanent hole with a momentary
    failure.
    """

    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    store = PersonaChatClarifyTicketStore()
    _corrupt_ticket_files(store, corrupt_count)

    assert store._rebuild_index() is False
    # The completeness CLAIM is the artifact that must not exist. Its absence is
    # what keeps every later lookup on the correct-by-construction full scan.
    assert not store._index_state_path().exists()
    assert store._ensure_index() is False


def test_the_ticket_lookup_still_answers_while_the_index_is_refused(
    isolate_agent_runtime_root,
):
    """Refusing the claim must not break the feature.

    ``_ensure_index`` returning ``False`` is a contract this module already
    had — the caller falls back to the full scan, which reads the store directly.
    An unreadable neighbour therefore costs performance, never an answer, and
    that is the whole reason this fix can be a refusal rather than a repair.
    """

    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    _corrupt_ticket_files(store, 1)

    found = store.open_ticket_for_session(_CLARIFY_ROOT)
    assert found is not None and found["clarify_token"] == token


def test_the_ticket_index_is_still_built_on_a_clean_store(isolate_agent_runtime_root):
    """The fence is not a disabled index."""

    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    store = PersonaChatClarifyTicketStore()
    _clarify_ticket(store)
    assert store._rebuild_index() is True
    assert store._index_state_path().exists()


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_the_ticket_readout_states_what_it_could_not_read(
    isolate_agent_runtime_root, corrupt_count
):
    """Projection class: the count TRAVELS.

    The adoption metric is computed over the whole store, and the site that
    builds it says why: "an adoption ratio that moved because the operator asked
    to see fewer rows would be a lying metric". A file that drops out of the scan
    moves the denominator exactly that way without anyone having asked.
    """

    from agent_runtime.persona_chat_continuity import PersonaChatClarifyTicketStore

    store = PersonaChatClarifyTicketStore()
    token = _clarify_ticket(store)
    _corrupt_ticket_files(store, corrupt_count)

    records, unreadable = store.scan_tickets()
    assert unreadable == corrupt_count
    # Membership, not position: the readable ticket is still listed.
    assert {record["clarify_token"] for record in records} == {token}
    # There is deliberately no thin `list_tickets()` view beside this. It
    # returned `scan_tickets()[0]` and DROPPED the unreadable count — the
    # denominator the adoption ratio is computed over, which `scan_tickets`'
    # own docstring says must travel. One reader, one signature, no lossy
    # sibling to reach for by accident.
    assert not hasattr(store, "list_tickets")


# --- site 5: the workspace-delete cascade --------------------------------


@pytest.mark.parametrize("corrupt_count", [1, 2])
def test_workspace_delete_refuses_when_a_board_cannot_be_attributed(
    isolate_agent_runtime_root, corrupt_count
):
    """The cascade's ENUMERATION is its delete list.

    It deletes by matching ``board.workspace_id``, so a board it cannot decode is
    a board it cannot attribute — it silently survives a workspace that no longer
    exists, and no later delete can re-reach it because the row that named it is
    gone. Refused BEFORE the office subtree is removed, so the store is left
    exactly as found.
    """

    from agent_runtime.board_store import BoardStore
    from agent_runtime.store import WorkspaceStore

    workspace = WorkspaceStore().create(name="ML-15 cascade")
    BoardStore().ensure_default_board(workspace.id)

    boards_root = paths.boards_root()
    for index in range(corrupt_count):
        board_dir = boards_root / f"board_corrupt_{index}"
        board_dir.mkdir(parents=True, exist_ok=True)
        (board_dir / "board.json").write_text("{ not a board", encoding="utf-8")

    office_dir = paths.office_dir(workspace.id)
    office_existed = office_dir.exists()

    with pytest.raises(WorkspaceDeleteBlocked) as excinfo:
        WorkspaceStore().delete(workspace.id)

    assert excinfo.value.code == "workspace_boards_unreadable"
    assert excinfo.value.safe_details["unreadable"] == corrupt_count
    assert excinfo.value.safe_details["workspace_id"] == workspace.id

    # NOTHING of the cascade ran: the workspace row, its office subtree, and its
    # boards are all exactly as they were.
    assert paths.workspace_path(workspace.id).exists()
    assert office_dir.exists() == office_existed
    assert [b.board_id for b in BoardStore().list_for_workspace(workspace.id)]


def test_workspace_delete_still_cascades_on_a_clean_store(isolate_agent_runtime_root):
    """The fence is not a disabled verb: a readable store still cascades."""

    from agent_runtime.board_store import BoardStore
    from agent_runtime.store import WorkspaceStore

    workspace = WorkspaceStore().create(name="ML-15 clean cascade")
    board = BoardStore().ensure_default_board(workspace.id)

    WorkspaceStore().delete(workspace.id)

    assert not paths.workspace_path(workspace.id).exists()
    assert not paths.board_dir(board.board_id).exists()
