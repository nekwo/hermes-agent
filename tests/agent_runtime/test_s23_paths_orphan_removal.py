"""S23 retires the writer-less path helpers and the writer-less checkpoint class.

Every name pinned below addressed a store directory that the mission-lane removal
(doc 16, S0-S12) archived out of the live runtime root, and that no code can write
again: the goal/task records, the ``runs``-shaped legacy task file, the proof store,
the proof sandbox recipe dir, the incident ``.txt`` sidecar, and the retired daemon
lease/status files. ``paths.queued_skills_dir`` is a second definition of a directory
``agent_runtime/queued_skills.py`` already owns -- the duplicate goes, the referenced
one stays.

The keep-side names one bare-word grep away from this set are pinned too: each of
them still has a live writer or reader outside ``paths.py``.
"""

from __future__ import annotations

from agent_runtime import checkpoint, paths


REMOVED_PATH_HELPERS = (
    "tasks_dir",
    "legacy_task_path",
    "task_storage_candidates",
    "existing_task_path",
    "proofs_dir",
    "proof_record_path",
    "proof_sandbox_task_dir",
    "proof_sandbox_dir",
    "incident_detail_path",
    "daemon_status_path",
    "daemon_lease_path",
    "queued_skills_dir",
)


def test_orphan_path_helpers_are_gone():
    assert [name for name in REMOVED_PATH_HELPERS if hasattr(paths, name)] == []


def test_the_lookalike_path_keep_set_survives():
    """Names that merely look like the removal set -- all still have live callers."""

    # goals/ is still read through task_path (persona_assignments.py) --
    # ``goals_dir`` and ``goal_path`` are load-bearing, not mission residue.
    assert callable(paths.goals_dir)
    assert callable(paths.goal_path)
    assert callable(paths.task_path)
    # store.py still lists and writes AgentRun rows.
    assert callable(paths.runs_dir)
    assert callable(paths.run_path)
    # store.py still writes Incident rows; only the .txt detail sidecar went.
    assert callable(paths.incidents_dir)
    assert callable(paths.incident_path)
    # worker_sessions.py reads the sandbox ROOT; only the per-recipe leaves went.
    assert callable(paths.proof_sandbox_root)
    # Stage C keeps its own artifact drop, deliberately not under proofs/.
    assert callable(paths.stagec_artifacts_dir)


def test_queued_skills_directory_has_exactly_one_owner():
    from agent_runtime import queued_skills

    assert not hasattr(paths, "queued_skills_dir")
    assert queued_skills._queue_dir().name == "queued_skills"


def test_checkpoint_drops_the_writerless_proofs_class():
    assert "proofs" not in checkpoint.ENTITY_CLASS_NAMES


def test_checkpoint_keeps_every_class_that_still_has_a_writer():
    # Each of these directories still has a module that persists rows into it,
    # so the class stays in the registry even though discovery omits it when the
    # directory is absent on a given root.
    for name in (
        "persona_instances",
        "persona_assignments",
        "runs",
        "incidents",
        "runtime_instances",
        "worker_sessions",
        "workspaces",
        "realms",
        "agents",
        "flow_graphs",
        "boards",
        "repo_bundles",
        "role_envelopes",
        "role_checklists",
        "self_tests",
        "packet_artifacts",
    ):
        assert name in checkpoint.ENTITY_CLASS_NAMES
