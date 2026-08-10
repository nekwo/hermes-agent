from __future__ import annotations

from hermes_time import now

from agent_runtime import paths
from types import SimpleNamespace

Task = SimpleNamespace
from agent_runtime.snapshot import (
    SNAPSHOT_CONTRACT_VERSION,
    build_snapshot,
    write_snapshot,
)
from agent_runtime.states import TaskState
from agent_runtime.store import TaskStore


def _task() -> Task:
    ts = now()
    return Task(
        id="task_snapshot",
        title="Snapshot without stage graph",
        description="The chat-first snapshot remains buildable.",
        state=TaskState.CREATED,
        created_at=ts,
        updated_at=ts,
        requested_by="test",
    )


def test_snapshot_builds_without_stage_graph(isolate_agent_runtime_root) -> None:
    snapshot = build_snapshot()
    assert snapshot["parity"]["contract_version"] == SNAPSHOT_CONTRACT_VERSION
    assert "goals" not in snapshot
    assert "boards" in snapshot


def test_snapshot_stage_projections_are_empty_after_graph_removal(isolate_agent_runtime_root) -> None:
    snapshot = build_snapshot()
    for key in ("goals", "stage_verification", "runs", "proofs", "incidents"):
        assert key not in snapshot


def test_write_snapshot_remains_importable_and_persists(isolate_agent_runtime_root) -> None:
    result = write_snapshot(build_snapshot())

    assert result["parity"]["contract_version"] == SNAPSHOT_CONTRACT_VERSION
    assert paths.snapshot_path().exists()


def test_snapshot_carries_the_running_work_section(isolate_agent_runtime_root) -> None:
    """Contract 52's addition: rows + per-source health, on every frame.

    Contract 53 retires the MCP transport source because connected capability
    infrastructure is not background work.

    ``sources`` is asserted alongside ``rows`` on purpose — a consumer handed
    rows without the health block reads an unreadable lane as "nothing running",
    which is the exact silent lie the projection exists to retire.

    ``dispatch`` joined this set at WP-H2 WITHOUT a contract bump, which is a
    ruling rather than an oversight. The ledger's stated reason for bumping on
    an ADDITION (the 52 entry in ``snapshot.py``) is that a new section arriving
    under an unbumped version would be "invisible rather than merely unread" —
    the consumer has no parse for it at all. That does not hold here: contract
    52 shipped ``dispatch`` inside ``RUNNING_WORK_KINDS`` precisely so "the wire
    vocabulary is complete from the first landing and a consumer does not have
    to re-derive it when the lane arrives", and both ``sources`` and
    ``rows[].kind`` are consumed as a map and a string, so a consumer pinned at
    52 READS the lane instead of missing it.
    """

    section = build_snapshot()["running_work"]

    # ``ambient`` is the fourth key and deliberately NOT one of the other three.
    # ``rows`` / ``sources`` / ``counts`` are contract; ``ambient`` is
    # machine-local context, given its own block so it can never again be
    # concatenated into a lane's ``detail`` — which is how the producer came to
    # emit different bytes for identical work depending on import order.
    assert set(section) == {"rows", "sources", "counts", "ambient"}
    assert set(section["ambient"]) == {"home_provenance", "home_name"}
    assert isinstance(section["rows"], list)
    assert set(section["sources"]) == {
        "terminal",
        "delegation",
        "chat_turn",
        "dispatch",
        "cron_job",
    }
    for name, entry in section["sources"].items():
        assert entry["status"] in {"ok", "unavailable"}, name
    assert section["counts"]["total"] == len(section["rows"])


def test_running_work_reports_its_own_completeness_and_timing(
    isolate_agent_runtime_root,
) -> None:
    """A new projection that skipped the accountant would drop rows invisibly."""

    snapshot = build_snapshot()

    completeness = snapshot["parity"]["completeness"]["running_work"]
    assert set(completeness) >= {"considered", "included", "dropped", "reasons", "by_design"}
    assert "running_work" in snapshot["parity"]["sections_ms"]
