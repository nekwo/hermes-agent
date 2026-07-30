"""S18 retires the dead goal/task projection cluster left inside ``snapshot.py``.

S9 removed the mission ROWS from the read-model contract (contract 45 / schema 2)
but left the *builders* that used to produce them standing in the module. After
S9 nothing calls them: the frame no longer has a ``goals`` section, so
``_goal_head`` / ``_archived_tasks_frame`` are never invoked, and
``goal_detail_for_task`` — the only entry point into the whole
``_goal_projection_from_task`` → ``_task_summary`` chain — was neutered to an
unconditional ``return None`` with 51 unreachable lines behind it (including a
latent ``NameError`` on an undefined ``token``). They are removed as features,
not refactored.

Cutting ``_task_summary`` also removes the snapshot-side caller of
``delivery_directive.task_delivery_directive``, which is the prerequisite for the
separate delivery-directive residue pass (see
``docs/agent-runtime-harness/delivery-directive.md``).

The keep-side names that merely *look* like this set are pinned below so a future
bare-word grep cannot take them out with the residue (doc 16 §Hazards: ``goal``,
``board``, ``proof``, ``task`` and ``graph`` all have a keep-side meaning).
"""

from __future__ import annotations

import ast
import inspect

from agent_runtime import snapshot


REMOVED_SNAPSHOT_SYMBOLS = (
    # The neutered entry point and everything only it could reach.
    "goal_detail_for_task",
    "_goal_projection_from_task",
    "_task_summary",
    # The frame-side goal head/detail split — there is no ``goals`` section.
    "_goal_head",
    "GOAL_DETAIL_ONLY_FIELDS",
    # The archived-mission pointer stub and the archived operator-channel
    # transcript builders behind it.
    "_archived_tasks_frame",
    "_archived_operator_channels",
    "_archived_operator_channel",
    "_archived_goal_input_message",
    "_archived_assignment_message",
    "_archived_event_message",
    # Proof-gate / mission-actor leaves that only the removed chain called.
    "_roll_up_gate_state",
    "_actor_presence",
    "_proof_requirement_status",
    "_proof_evidence_ref",
    "_incident_summary",
    "_proof_summary",
)


def test_removed_snapshot_symbols_are_gone():
    assert [name for name in REMOVED_SNAPSHOT_SYMBOLS if hasattr(snapshot, name)] == []


def test_snapshot_no_longer_imports_the_task_delivery_directive():
    """The snapshot-side caller of ``task_delivery_directive`` went with
    ``_task_summary``; the module must not keep the import alive."""

    assert not hasattr(snapshot, "task_delivery_directive")
    assert "task_delivery_directive" not in inspect.getsource(snapshot)


def test_no_function_has_statements_after_an_unconditional_return():
    """The defect class this stage retires: a function neutered to ``return None``
    at the top of its body, with its old implementation left dangling behind it
    (unexecuted, unlinted, and free to rot into a ``NameError``)."""

    tree = ast.parse(inspect.getsource(snapshot))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for index, statement in enumerate(node.body):
            if isinstance(statement, ast.Return) and index < len(node.body) - 1:
                offenders.append(f"{node.name}:{statement.lineno}")
    assert offenders == []


def test_the_lookalike_keep_set_survives():
    """Names one bare-word grep away from the removal set — all still live."""

    # ``proof``: the removed proof-gate leaves are not the Stage C / accounting
    # surface, and ``parity`` remains the read-model accountant.
    from agent_runtime import parity

    assert callable(parity.ProjectionAccountant)
    assert callable(snapshot.build_snapshot)
    # ``goal``/``task``: the archived-summary reader and its cap are the fetch
    # lane behind ``harness task history``, not the evicted frame builder.
    assert callable(snapshot._archived_task_summaries)
    assert snapshot.ARCHIVED_TASKS_REF_RECENT_CAP > 0
    # ``proof``/``incident``: the live incident frame split stays.
    assert callable(snapshot._open_incidents_frame)
    # ``board``/``realm``/``workspace``: the live frames the launcher renders.
    assert callable(snapshot._realm_summary)
    assert callable(snapshot._boards_summary)
    assert callable(snapshot._workspace_summary)
    assert callable(snapshot._offices_summary)
    # The on-demand detail lane that is NOT dead: persona-instance tool detail.
    assert callable(snapshot.persona_instance_detail_for_id)


def test_live_frame_still_carries_no_mission_sections(isolate_agent_runtime_root):
    """Negative gate: removing the builders must not resurrect a section, and the
    frame the launcher reads must still build at contract 45."""

    frame = snapshot.build_snapshot()
    for section in ("goals", "archived_tasks", "proofs", "incidents", "runs", "stage_verification"):
        assert section not in frame, f"{section} must not be a top-level frame section"
    assert frame["parity"]["contract_version"] == 45
    # The kept frames the launcher renders every tick are still present.
    for section in ("boards", "workspaces", "realms", "agents"):
        assert section in frame, f"{section} is a KEEP frame and must survive"
