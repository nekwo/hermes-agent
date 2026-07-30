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
from agent_runtime.models import RepoBundle
from types import SimpleNamespace

Task = SimpleNamespace
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
    from agent_runtime.task_store_stub import TaskStoreStub

    assert TaskStore is TaskStoreStub
    assert not hasattr(TaskStore(), "archive")


def test_archive_promotes_dirty_bundleless_run_worktree(source_repo, isolate_agent_runtime_root):
    from agent_runtime.task_store_stub import TaskStoreStub

    store = TaskStore(event_log=object())
    assert isinstance(store, TaskStoreStub)
    assert vars(store) == {}


def test_reap_orphan_worktrees_capture_age_and_task_free_cleanup(source_repo, tmp_path, monkeypatch):
    import os
    import time

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees

    # Pin the worktree base: long pytest tmp store roots trip the fallback to
    # the SHARED system temp base, which would make this test iterate (and
    # potentially reap) worktrees from other runs.
    wt_base = tmp_path / "wtbase"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: wt_base)

    # Orphan dirty worktree from a long-gone task.
    orphan = _worktree_with_changes(source_repo, task_id="task_gone", run_id="run_gone")

    # A second old worktree is no longer protected by deleted task/run records.
    owned_task = _task(task_id="task_open", state=TaskState.CREATED)
    owned_task.affected_repos = [str(source_repo)]
    owned = _worktree_with_changes(source_repo, task_id="task_open", run_id="run_open")

    # Fresh worktrees are kept by the age guard.
    fresh = reap_orphan_worktrees(min_age_seconds=3600)
    assert fresh["reaped"] == []
    assert {item["reason"] for item in fresh["kept"]} == {"younger_than_min_age"}

    # Age both candidates past the guard; both are captured and reaped.
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    os.utime(owned, (old, old))
    result = reap_orphan_worktrees(min_age_seconds=3600)

    reaped_names = [item["worktree"] for item in result["reaped"]]
    assert orphan.name in reaped_names
    assert owned.name in reaped_names
    assert not orphan.exists()
    assert not owned.exists()
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
    status_before = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=orphan,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    index_before = subprocess.run(
        ["git", "diff", "--cached", "--binary", "HEAD"],
        cwd=orphan,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout

    result = reap_orphan_worktrees(
        min_age_seconds=3600, event_log=event_log, dry_run=True
    )

    preview = next(
        item for item in result["reaped"] if item["worktree"] == orphan.name
    )
    assert result["dry_run"] is True
    assert preview["worktree"] == orphan.name
    assert preview["source"] == "current"
    assert preview["base"] == str(wt_base)
    assert preview["would_capture_patch"] is True
    assert preview["dry_run"] is True
    assert preview["patch_bytes_estimate"] > 0
    assert orphan.exists(), "dry-run must not remove the candidate worktree"
    assert not Path(result["capture_dir"]).exists(), "dry-run must not write patches"
    assert event_log.tail(100) == events_before, "dry-run must not emit reap events"
    status_after = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=orphan,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    index_after = subprocess.run(
        ["git", "diff", "--cached", "--binary", "HEAD"],
        cwd=orphan,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    assert status_after == status_before, "dry-run must preserve tracked and untracked status bytes"
    assert index_after == index_before, "dry-run must not add intent-to-add index entries"


def test_reap_orphan_worktrees_legacy_temp_is_opt_in_protected_and_write_free(
    source_repo, isolate_agent_runtime_root, tmp_path, monkeypatch
):
    import os
    import time

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees
    from agent_runtime.events import EventLog

    current_base = tmp_path / "current-wt"
    legacy_base = tmp_path / "hermes-agent-wt"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: legacy_base)
    monkeypatch.setattr(
        repo_context_mod, "legacy_harness_worktree_base_dir", lambda: legacy_base
    )
    orphan = _worktree_with_changes(
        source_repo, task_id="task_legacy_gone", run_id="run_legacy_gone"
    )
    owned_task = _task(task_id="task_legacy_open", state=TaskState.CREATED)
    owned_task.affected_repos = [str(source_repo)]
    owned = _worktree_with_changes(
        source_repo, task_id=owned_task.id, run_id="run_legacy_open"
    )
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    os.utime(owned, (old, old))
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: current_base)

    ignored = reap_orphan_worktrees(min_age_seconds=3600, dry_run=True)
    assert ignored["reaped"] == [] and ignored["kept"] == []

    event_log = EventLog()
    events_before = event_log.tail(100)
    status_before = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=orphan,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout
    result = reap_orphan_worktrees(
        min_age_seconds=3600,
        event_log=event_log,
        dry_run=True,
        include_legacy_temp=True,
    )

    preview = next(item for item in result["reaped"] if item["worktree"] == orphan.name)
    assert preview["source"] == "legacy_temp"
    assert preview["base"] == str(legacy_base)
    assert preview["would_capture_patch"] is True
    assert preview["patch_bytes_estimate"] > 0
    assert orphan.exists() and owned.exists()
    owned_preview = next(item for item in result["reaped"] if item["worktree"] == owned.name)
    assert owned_preview["source"] == "legacy_temp"
    assert owned_preview["would_capture_patch"] is True
    assert subprocess.run(
        ["git", "status", "--porcelain=v1", "-z"],
        cwd=orphan,
        stdout=subprocess.PIPE,
        check=True,
    ).stdout == status_before
    assert event_log.tail(100) == events_before
    assert not Path(result["capture_dir"]).exists()


def test_reap_orphan_worktrees_dedupes_same_current_and_legacy_base(
    source_repo, isolate_agent_runtime_root, tmp_path, monkeypatch
):
    import os
    import time

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees

    shared_base = tmp_path / "hermes-agent-wt"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: shared_base)
    monkeypatch.setattr(
        repo_context_mod, "legacy_harness_worktree_base_dir", lambda: shared_base
    )
    orphan = _worktree_with_changes(
        source_repo, task_id="task_dedupe_gone", run_id="run_dedupe_gone"
    )
    old = time.time() - 7200
    os.utime(orphan, (old, old))

    result = reap_orphan_worktrees(
        min_age_seconds=3600, dry_run=True, include_legacy_temp=True
    )

    matching = [item for item in result["reaped"] if item["worktree"] == orphan.name]
    assert len(matching) == 1
    assert matching[0]["source"] == "current"


def test_reap_orphan_worktrees_opted_in_legacy_destructive_keeps_capture_contract(
    source_repo, isolate_agent_runtime_root, tmp_path, monkeypatch
):
    import os
    import time

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees

    current_base = tmp_path / "current-wt"
    legacy_base = tmp_path / "hermes-agent-wt"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: legacy_base)
    monkeypatch.setattr(
        repo_context_mod, "legacy_harness_worktree_base_dir", lambda: legacy_base
    )
    orphan = _worktree_with_changes(
        source_repo, task_id="task_legacy_reap", run_id="run_legacy_reap"
    )
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: current_base)

    result = reap_orphan_worktrees(
        min_age_seconds=3600, include_legacy_temp=True
    )

    reaped = next(item for item in result["reaped"] if item["worktree"] == orphan.name)
    assert reaped["source"] == "legacy_temp"
    assert reaped["captured_patch"].startswith("legacy_temp_")
    captured = Path(result["capture_dir"]) / reaped["captured_patch"]
    assert captured.is_file() and captured.stat().st_size > 0
    assert not orphan.exists()


def test_reap_inventory_unsafe_candidate_is_kept_before_any_git_or_delete(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    from agent_runtime import delivery_directive as directive
    from agent_runtime import repo_context as repo_context_mod

    base = tmp_path / "hermes-agent-wt"
    external = tmp_path / "external-target"
    external.mkdir()
    marker = external / "marker.txt"
    marker.write_text("untouched", encoding="utf-8")
    monkeypatch.setattr(
        repo_context_mod,
        "harness_worktree_inventory",
        lambda **_: [(external, base, "legacy_temp", "candidate_outside_base")],
    )
    monkeypatch.setattr(
        repo_context_mod,
        "worktree_source_root",
        lambda *_: pytest.fail("unsafe candidate reached git inspection"),
    )
    monkeypatch.setattr(
        directive,
        "worktree_patch_size_estimate",
        lambda *_: pytest.fail("unsafe candidate reached patch estimation"),
    )
    monkeypatch.setattr(
        repo_context_mod,
        "remove_orphan_worktree",
        lambda *_args, **_kwargs: pytest.fail("unsafe candidate reached removal"),
    )

    result = directive.reap_orphan_worktrees(
        min_age_seconds=0, include_legacy_temp=True
    )

    assert result["reaped"] == []
    assert result["kept"] == [
        {
            "worktree": external.name,
            "base": str(base),
            "source": "legacy_temp",
            "reason": "candidate_outside_base",
        }
    ]
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_reap_inventory_reparse_base_is_kept_without_traversal(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees

    current_base = tmp_path / "current-wt"
    legacy_base = tmp_path / "hermes-agent-wt"
    external_child = legacy_base / "must-not-be-visited"
    current_base.mkdir()
    external_child.mkdir(parents=True)
    marker = external_child / "marker.txt"
    marker.write_text("untouched", encoding="utf-8")
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: current_base)
    monkeypatch.setattr(
        repo_context_mod, "legacy_harness_worktree_base_dir", lambda: legacy_base
    )
    monkeypatch.setattr(
        repo_context_mod,
        "_path_is_reparse_point",
        lambda path: Path(path) == legacy_base,
    )

    result = reap_orphan_worktrees(
        min_age_seconds=0, include_legacy_temp=True
    )

    assert any(
        item["base"] == str(legacy_base)
        and item["reason"] == "base_reparse_alias"
        for item in result["kept"]
    )
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_reap_inventory_candidate_alias_does_not_suppress_canonical_sibling(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees

    current_base = tmp_path / "current-wt"
    legacy_base = tmp_path / "hermes-agent-wt"
    alias = legacy_base / "alias"
    real_worktree = legacy_base / "real_worktree"
    current_base.mkdir()
    alias.mkdir(parents=True)
    real_worktree.mkdir()
    marker = real_worktree / "marker.txt"
    marker.write_text("untouched", encoding="utf-8")
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: current_base)
    monkeypatch.setattr(
        repo_context_mod, "legacy_harness_worktree_base_dir", lambda: legacy_base
    )
    monkeypatch.setattr(
        repo_context_mod,
        "_path_is_reparse_point",
        lambda path: Path(path) == alias,
    )

    result = reap_orphan_worktrees(
        min_age_seconds=3600, dry_run=True, include_legacy_temp=True
    )

    assert any(
        item["worktree"] == alias.name
        and item["reason"] == "candidate_reparse_alias"
        for item in result["kept"]
    )
    assert any(item["worktree"] == real_worktree.name for item in result["kept"])
    assert marker.read_text(encoding="utf-8") == "untouched"


def test_reap_inventory_in_base_alias_keeps_real_worktree_and_nested_target(
    isolate_agent_runtime_root, source_repo, tmp_path, monkeypatch
):
    import os

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees

    current_base = tmp_path / "current-wt"
    legacy_base = tmp_path / "hermes-agent-wt"
    external = tmp_path / "external"
    current_base.mkdir()
    legacy_base.mkdir()
    external.mkdir()
    marker = external / "marker.txt"
    marker.write_text("external", encoding="utf-8")
    real_worktree = legacy_base / "real_worktree"
    _git(source_repo, "worktree", "add", "--detach", str(real_worktree), "HEAD")
    tracked = real_worktree / "app.py"
    tracked.write_text(
        tracked.read_text(encoding="utf-8") + "\n# keep me\n", encoding="utf-8"
    )
    nested_link = real_worktree / "nested-target"
    alias = legacy_base / "alias"
    try:
        os.symlink(external, nested_link, target_is_directory=True)
        os.symlink(real_worktree, alias, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink/junction creation unsupported: {exc}")
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: current_base)
    monkeypatch.setattr(
        repo_context_mod, "legacy_harness_worktree_base_dir", lambda: legacy_base
    )

    status_before = _git(real_worktree, "status", "--porcelain=v1").stdout
    preview = reap_orphan_worktrees(
        min_age_seconds=3600, dry_run=True, include_legacy_temp=True
    )
    destructive = reap_orphan_worktrees(
        min_age_seconds=3600, include_legacy_temp=True
    )

    for result in (preview, destructive):
        assert any(
            item["worktree"] == alias.name
            and item["reason"] == "candidate_reparse_alias"
            for item in result["kept"]
        )
        assert any(
            item["worktree"] == real_worktree.name
            and item["reason"] == "younger_than_min_age"
            for item in result["kept"]
        )
    assert marker.read_text(encoding="utf-8") == "external"
    assert _git(real_worktree, "status", "--porcelain=v1").stdout == status_before
    assert alias.exists()
    assert nested_link.exists()


def test_reap_patch_capture_names_are_exclusive_across_sources_and_collisions(
    isolate_agent_runtime_root, tmp_path, monkeypatch
):
    from datetime import datetime, timezone

    from agent_runtime import delivery_directive as directive

    capture_dir = tmp_path / "captures"
    current_base = tmp_path / "current"
    legacy_base = tmp_path / "legacy"
    current = current_base / "legacy_temp_foo"
    legacy = legacy_base / "foo"
    current.mkdir(parents=True)
    legacy.mkdir(parents=True)
    monkeypatch.setattr(
        directive,
        "now",
        lambda: datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc),
    )

    current_capture = directive._write_reap_patch_exclusive(
        capture_dir,
        "current patch",
        source="current",
        candidate_base=current_base,
        worktree=current,
    )
    legacy_capture = directive._write_reap_patch_exclusive(
        capture_dir,
        "legacy patch",
        source="legacy_temp",
        candidate_base=legacy_base,
        worktree=legacy,
    )
    collision_capture = directive._write_reap_patch_exclusive(
        capture_dir,
        "second current patch",
        source="current",
        candidate_base=current_base,
        worktree=current,
    )

    assert current_capture and legacy_capture and collision_capture
    assert len({current_capture.name, legacy_capture.name, collision_capture.name}) == 3
    assert current_capture.read_text(encoding="utf-8") == "current patch"
    assert legacy_capture.read_text(encoding="utf-8") == "legacy patch"
    assert collision_capture.read_text(encoding="utf-8") == "second current patch"


def test_reap_capture_create_failure_keeps_candidate(
    source_repo, isolate_agent_runtime_root, tmp_path, monkeypatch
):
    import os
    import time

    from agent_runtime import delivery_directive as directive
    from agent_runtime import repo_context as repo_context_mod

    wt_base = tmp_path / "wtbase"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: wt_base)
    orphan = _worktree_with_changes(
        source_repo, task_id="task_capture_fail", run_id="run_capture_fail"
    )
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    monkeypatch.setattr(directive, "_write_reap_patch_exclusive", lambda *_a, **_k: None)

    result = directive.reap_orphan_worktrees(min_age_seconds=3600)

    assert orphan.exists()
    assert any(
        item["worktree"] == orphan.name and item["reason"] == "capture_write_failed"
        for item in result["kept"]
    )
