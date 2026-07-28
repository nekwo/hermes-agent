from __future__ import annotations

import subprocess
from pathlib import Path

from hermes_time import now

from agent_runtime.delivery_directive import (
    DEFAULT_DELIVERY_DIRECTIVE,
    DeliveryDirectiveInvalid,
    bundle_patch_path,
    capture_bundle_patch,
    execute_delivery_directive,
    normalize_delivery_directive,
    read_bundle_promotion_record,
    task_delivery_directive,
)
from agent_runtime.models import RepoBundle, Task
from agent_runtime.repo_context import RepoExecutionContext, isolated_repo_context_for_run
from agent_runtime.repo_bundles import RepoBundleStore, repo_bundle_summary
from agent_runtime.states import RunState, TaskState
from agent_runtime.store import IncidentStore, RunStore, TaskStore

import pytest


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )


@pytest.fixture()
def source_repo(tmp_path) -> Path:
    repo = tmp_path / "product-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "harness@test.local")
    _git(repo, "config", "user.name", "Harness Test")
    (repo / "app.py").write_text("print('v1')\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _bundle(source_repo: Path, *, task_id: str = "task_dd01", run_id: str | None = "run_dd01") -> RepoBundle:
    ts = now()
    return RepoBundle(
        id="bundle_dd0000000001",
        task_id=task_id,
        repo=str(source_repo),
        owner_persona_id="dev",
        state="running",
        title="Delivery directive slice",
        objective="Change app.py and add a module.",
        active_run_id=run_id,
        proof_ids=["proof_a"],
        created_at=ts,
        updated_at=ts,
    )


def _task(*, task_id: str = "task_dd01", state: TaskState = TaskState.DONE, directive: dict | None = None) -> Task:
    ts = now()
    return Task(
        id=task_id,
        title="Delivery directive goal",
        description="Prove promote/preserve/reap.",
        state=state,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        delivery_directive=directive,
    )


def _worktree_with_changes(source_repo: Path, *, task_id: str = "task_dd01", run_id: str = "run_dd01") -> Path:
    ctx = RepoExecutionContext(workdir=source_repo, repo_label=source_repo.name, source="explicit")
    wt_ctx = isolated_repo_context_for_run(ctx, task_id=task_id, run_id=run_id)
    worktree = wt_ctx.workdir
    (worktree / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (worktree / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    return worktree


def test_normalize_defaults_and_rejections():
    assert normalize_delivery_directive(None) == DEFAULT_DELIVERY_DIRECTIVE
    assert normalize_delivery_directive({"promote": "hold"})["promote"] == "hold"
    with pytest.raises(DeliveryDirectiveInvalid):
        normalize_delivery_directive({"promote": "yolo"})
    with pytest.raises(DeliveryDirectiveInvalid):
        normalize_delivery_directive({"unknown_key": True})
    with pytest.raises(DeliveryDirectiveInvalid):
        normalize_delivery_directive("promote")


def test_task_delivery_directive_survives_bad_declared_value():
    task = _task(directive={"promote": "not-a-mode"})
    assert task_delivery_directive(task) == DEFAULT_DELIVERY_DIRECTIVE


def test_mark_delivered_captures_patch_before_clearing_run(source_repo):
    worktree = _worktree_with_changes(source_repo)
    assert worktree.is_dir()
    bundle = _bundle(source_repo)
    delivered = RepoBundleStore().mark_delivered(bundle, proof_ids=["proof_b"])
    assert delivered.active_run_id is None
    assert delivered.delivery_capture["captured"] is True
    assert delivered.delivery_capture["run_id"] == "run_dd01"
    assert "app.py" in delivered.delivery_capture["changed_files"]
    assert "feature.py" in delivered.delivery_capture["changed_files"]
    patch = bundle_patch_path(delivered.task_id, delivered.id)
    assert patch.is_file() and patch.stat().st_size > 0


def test_capture_reports_clean_worktree(source_repo):
    ctx = RepoExecutionContext(workdir=source_repo, repo_label=source_repo.name, source="explicit")
    isolated_repo_context_for_run(ctx, task_id="task_dd01", run_id="run_dd01")
    result = capture_bundle_patch(_bundle(source_repo))
    assert result["captured"] is False
    assert result["reason"] == "worktree_clean"


def test_execute_directive_promotes_commits_and_reaps(source_repo):
    worktree = _worktree_with_changes(source_repo)
    store = RepoBundleStore()
    bundle = store.mark_delivered(_bundle(source_repo))
    task = _task()
    TaskStore().create(task)

    outcome = execute_delivery_directive(task, bundle)

    assert outcome["promote"]["status"] == "promoted"
    assert outcome["promote"]["commit"]
    assert outcome["worktree"]["status"] == "reaped"
    assert not worktree.exists()
    assert (source_repo / "feature.py").read_text(encoding="utf-8") == "FEATURE = True\n"
    assert (source_repo / "app.py").read_text(encoding="utf-8") == "print('v2')\n"
    log = _git(source_repo, "log", "-1", "--pretty=%s").stdout.strip()
    assert log == "Delivery directive goal"
    status = _git(source_repo, "status", "--porcelain").stdout.strip()
    assert status == ""
    record = read_bundle_promotion_record(task.id, bundle.id)
    assert record is not None and record["promote"]["status"] == "promoted"


def test_execute_directive_hold_keeps_repo_untouched_and_reaps(source_repo):
    worktree = _worktree_with_changes(source_repo)
    bundle = RepoBundleStore().mark_delivered(_bundle(source_repo))
    task = _task(directive={"promote": "hold"})

    outcome = execute_delivery_directive(task, bundle)

    assert outcome["promote"]["status"] == "held"
    assert outcome["worktree"]["status"] == "reaped"
    assert not worktree.exists()
    assert (source_repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n"
    # The captured patch still exists — nothing is lost by holding.
    assert bundle_patch_path(task.id, bundle.id).is_file()


def test_execute_directive_dirty_target_fails_keeps_worktree_opens_incident(source_repo):
    worktree = _worktree_with_changes(source_repo)
    bundle = RepoBundleStore().mark_delivered(_bundle(source_repo))
    task = _task()
    # Overlapping dirt in the target repo must block promotion.
    (source_repo / "app.py").write_text("print('local edit')\n", encoding="utf-8")

    opened: list = []

    class _RecordingIncidents(IncidentStore):
        def open(self, incident):
            opened.append(incident)
            return super().open(incident)

    outcome = execute_delivery_directive(task, bundle, incident_store=_RecordingIncidents())

    assert outcome["promote"]["status"] == "failed"
    assert outcome["promote"]["reason"] == "target_paths_dirty"
    assert outcome["worktree"]["status"] == "kept"
    assert worktree.exists()
    assert len(opened) == 1
    assert opened[0].kind == "bundle_promotion_failed"


def test_execute_directive_skips_promote_for_cancelled_task(source_repo):
    worktree = _worktree_with_changes(source_repo)
    bundle = RepoBundleStore().mark_delivered(_bundle(source_repo))
    task = _task(state=TaskState.CANCELLED)

    outcome = execute_delivery_directive(task, bundle)

    assert outcome["promote"]["status"] == "skipped"
    assert outcome["promote"]["reason"].startswith("task_state_")
    # Evidence preserved, litter reaped.
    assert bundle_patch_path(task.id, bundle.id).is_file()
    assert outcome["worktree"]["status"] == "reaped"
    assert not worktree.exists()
    assert (source_repo / "app.py").read_text(encoding="utf-8") == "print('v1')\n"


def test_bundle_summary_reports_promotion(source_repo):
    _worktree_with_changes(source_repo)
    bundle = RepoBundleStore().mark_delivered(_bundle(source_repo))
    task = _task()
    execute_delivery_directive(task, bundle)

    summary = repo_bundle_summary(bundle)
    assert summary["checkout_applied"] is True
    assert summary["checkout_status"] == "promoted"
    assert summary["delivery_contract"] == "delivery_directive"
    assert summary["promotion"]["status"] == "promoted"
    assert summary["delivery_capture"]["captured"] is True


def test_bundle_summary_without_promotion_keeps_staged_contract(source_repo):
    bundle = _bundle(source_repo)
    summary = repo_bundle_summary(bundle)
    assert summary["checkout_applied"] is False
    assert summary["checkout_status"] == "not_applied"
    assert summary["promotion"] is None


def test_archive_runs_directive_and_preserves_patch(source_repo, isolate_agent_runtime_root):
    worktree = _worktree_with_changes(source_repo)
    bundle = RepoBundleStore().mark_delivered(_bundle(source_repo))
    task = _task()
    TaskStore().create(task)

    from agent_runtime.store import ArchiveStore

    result = ArchiveStore().archive_tasks([task.id], actor="test", reason="directive test")

    assert result["archived_task_ids"] == [task.id]
    archived_entry = result["archived_tasks"][0]
    outcomes = archived_entry["delivery_directive_outcomes"]
    assert outcomes[0]["promote"]["status"] == "promoted"
    assert outcomes[0]["worktree"]["status"] == "reaped"
    assert not worktree.exists()
    assert (source_repo / "feature.py").is_file()
    archive_dir = Path(result["archive_dir"])
    archived_patch = archive_dir / "repo_bundles" / task.id / f"{bundle.id}.patch"
    assert archived_patch.is_file() and archived_patch.stat().st_size > 0
    archived_promotion = archive_dir / "repo_bundles" / task.id / f"{bundle.id}.promotion.json"
    assert archived_promotion.is_file()


def test_archive_promotes_dirty_bundleless_run_worktree(source_repo, isolate_agent_runtime_root):
    task = _task(task_id="task_bundleless")
    task.affected_repos = [str(source_repo)]
    TaskStore().create(task)
    run_store = RunStore()
    run = run_store.open_run("dev", task.id)
    worktree = _worktree_with_changes(source_repo, task_id=task.id, run_id=run.id)
    (worktree / "docs").mkdir()
    (worktree / "docs" / "stream.md").write_text("hydrate delta heartbeat schema_version\n", encoding="utf-8")
    run_store.close_run(run.id, state=RunState.COMPLETED, final_decision={"type": "complete"})

    from agent_runtime.store import ArchiveStore

    result = ArchiveStore().archive_tasks([task.id], actor="test", reason="bundleless directive test")

    assert result["archived_task_ids"] == [task.id]
    archived_entry = result["archived_tasks"][0]
    deliveries = archived_entry["delivery_directive_outcomes"][0]["task_worktree_delivery"]
    assert deliveries[0]["promote"]["status"] == "promoted"
    assert deliveries[0]["worktree"]["status"] == "reaped"
    assert not worktree.exists()
    assert (source_repo / "feature.py").read_text(encoding="utf-8") == "FEATURE = True\n"
    assert (source_repo / "docs" / "stream.md").is_file()
    archive_dir = Path(result["archive_dir"])
    bundle_id = deliveries[0]["bundle_id"]
    archived_patch = archive_dir / "repo_bundles" / task.id / f"{bundle_id}.patch"
    archived_promotion = archive_dir / "repo_bundles" / task.id / f"{bundle_id}.promotion.json"
    assert archived_patch.is_file() and archived_patch.stat().st_size > 0
    assert archived_promotion.is_file()
    assert f"{bundle_id}.promotion" not in archived_entry["repo_bundle_ids"]


def test_reap_orphan_worktrees_capture_age_and_ownership(source_repo, tmp_path, monkeypatch):
    import os
    import time

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees
    from agent_runtime.store import RunStore

    # Pin the worktree base: long pytest tmp store roots trip the fallback to
    # the SHARED system temp base, which would make this test iterate (and
    # potentially reap) worktrees from other runs.
    wt_base = tmp_path / "wtbase"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: wt_base)

    # Orphan dirty worktree from a long-gone task.
    orphan = _worktree_with_changes(source_repo, task_id="task_gone", run_id="run_gone")

    # Worktree owned by an OPEN task's run — must never be reaped.
    owned_task = _task(task_id="task_open", state=TaskState.CREATED)
    owned_task.affected_repos = [str(source_repo)]
    TaskStore().create(owned_task)
    run = RunStore().open_run("dev", "task_open")
    owned = _worktree_with_changes(source_repo, task_id="task_open", run_id=run.id)

    # Fresh worktrees are kept by the age guard.
    fresh = reap_orphan_worktrees(min_age_seconds=3600)
    assert fresh["reaped"] == []
    assert {item["reason"] for item in fresh["kept"]} <= {"younger_than_min_age", "owned_by_open_task_run"}

    # Age the orphan past the guard; the owned one stays protected regardless.
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    os.utime(owned, (old, old))
    result = reap_orphan_worktrees(min_age_seconds=3600)

    reaped_names = [item["worktree"] for item in result["reaped"]]
    assert orphan.name in reaped_names
    assert not orphan.exists()
    assert owned.exists()
    assert any(item["reason"] == "owned_by_open_task_run" for item in result["kept"])
    reaped_entry = next(item for item in result["reaped"] if item["worktree"] == orphan.name)
    assert reaped_entry.get("captured_patch")
    captured = Path(result["capture_dir"]) / reaped_entry["captured_patch"]
    assert captured.is_file() and captured.stat().st_size > 0


def test_reap_orphan_worktrees_dry_run_is_a_write_free_typed_preview(
    source_repo, isolate_agent_runtime_root, tmp_path, monkeypatch
):
    import os
    import time

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees
    from agent_runtime.events import EventLog

    wt_base = tmp_path / "wtbase"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: wt_base)
    orphan = _worktree_with_changes(
        source_repo, task_id="task_dry_run_gone", run_id="run_dry_run_gone"
    )
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    event_log = EventLog()
    events_before = event_log.tail(100)

    result = reap_orphan_worktrees(
        min_age_seconds=3600, event_log=event_log, dry_run=True
    )

    preview = next(
        item for item in result["reaped"] if item["worktree"] == orphan.name
    )
    assert result["dry_run"] is True
    assert preview == {
        "worktree": orphan.name,
        "would_capture_patch": True,
        "patch_bytes_estimate": preview["patch_bytes_estimate"],
        "dry_run": True,
    }
    assert preview["patch_bytes_estimate"] > 0
    assert orphan.exists(), "dry-run must not remove the candidate worktree"
    assert not Path(result["capture_dir"]).exists(), "dry-run must not write patches"
    assert event_log.tail(100) == events_before, "dry-run must not emit reap events"
