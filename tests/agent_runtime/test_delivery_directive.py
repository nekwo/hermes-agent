"""Live half of ``delivery_directive``: the orphan janitor + the promotion read.

S24 removed the Task-declared directive path and the terminal-settle executors
(see ``docs/agent-runtime-harness/delivery-directive.md``). What this file covers
is what still has production callers:

* ``reap_orphan_worktrees`` — ``hermes harness worktree reap`` and
  ``harness doctor --fix``. Every protection is pinned here: the age guard, the
  ``wt_reaped_patches/`` capture-before-delete contract, dry-run write-freedom,
  opt-in legacy-temp handling, reparse-alias safety, and the registered
  ``worktree.orphans_reaped`` emission.
* ``read_bundle_promotion_record`` — the parse-safety contract on
  already-written promotion records (absent / malformed / valid). S56 deleted
  ``repo_bundles.repo_bundle_summary``, which was this read's last production
  caller, so what remains here covers the reader's own safety, not a live
  projection. See the S56 follow-up note: the read is now caller-less and is a
  deletion candidate for a later wave — that is a PRODUCTION decision, not one
  this test file may make.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hermes_time import now

from agent_runtime.delivery_directive import (
    bundle_promotion_record_path,
    read_bundle_promotion_record,
)
from agent_runtime.models import RepoBundle
from agent_runtime.repo_context import RepoExecutionContext, isolated_repo_context_for_run

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
        state="delivered_waiting_for_qa",
        title="Delivery directive slice",
        objective="Change app.py and add a module.",
        active_run_id=run_id,
        proof_ids=["proof_a"],
        created_at=ts,
        updated_at=ts,
    )


def _worktree_with_changes(source_repo: Path, *, task_id: str = "task_dd01", run_id: str = "run_dd01") -> Path:
    """Build a real dirty worktree for the janitor to find.

    Uses the kept worktree creator (see its docstring): the janitor must be
    tested against worktrees shaped exactly like the ones that lane leaves on
    disk, not against hand-rolled directories.
    """

    ctx = RepoExecutionContext(workdir=source_repo, repo_label=source_repo.name, source="explicit")
    wt_ctx = isolated_repo_context_for_run(ctx, task_id=task_id, run_id=run_id)
    worktree = wt_ctx.workdir
    (worktree / "app.py").write_text("print('v2')\n", encoding="utf-8")
    (worktree / "feature.py").write_text("FEATURE = True\n", encoding="utf-8")
    return worktree


def _write_promotion_record(bundle: RepoBundle, outcome: dict) -> Path:
    record_path = bundle_promotion_record_path(bundle.task_id, bundle.id)
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(outcome, sort_keys=True), encoding="utf-8")
    return record_path


def test_promotion_record_read_is_absent_malformed_and_valid_safe(
    source_repo, isolate_agent_runtime_root
):
    bundle = _bundle(source_repo)

    assert read_bundle_promotion_record(bundle.task_id, bundle.id) is None

    record_path = _write_promotion_record(bundle, {"promote": {"status": "promoted", "commit": "abc1234"}})
    record = read_bundle_promotion_record(bundle.task_id, bundle.id)
    assert record is not None and record["promote"]["commit"] == "abc1234"

    record_path.write_text("{not json", encoding="utf-8")
    assert read_bundle_promotion_record(bundle.task_id, bundle.id) is None

    record_path.write_text('["a list, not a record"]', encoding="utf-8")
    assert read_bundle_promotion_record(bundle.task_id, bundle.id) is None


# S56 deleted the two bundle-summary cases that stood here
# (``test_bundle_summary_still_labels_a_historical_promotion`` and
# ``test_bundle_summary_without_promotion_keeps_staged_contract``). Their only
# subject was ``repo_bundles.repo_bundle_summary`` and its
# ``checkout_applied`` / ``checkout_status`` / ``delivery_contract`` /
# ``closeout_label`` labelling, all deleted with the ``repo_bundles`` /
# ``repo_bundle_closeout`` wire rows they fed. Nothing else in this file
# depended on them; the janitor coverage below is untouched.


def test_no_archive_choke_point_survives_to_run_a_directive(isolate_agent_runtime_root):
    """The executors' only caller was ``ArchiveStore.archive_tasks`` on the
    ``TaskStore``. Ruling R-3 keeps the store as a permanent stub — pinned here
    so nobody re-grows an archive hook and wires a settle-time promote to it."""

    from agent_runtime.store import TaskStore
    from agent_runtime.task_store_stub import TaskStoreStub

    assert TaskStore is TaskStoreStub
    assert not hasattr(TaskStore(), "archive")
    assert vars(TaskStore(event_log=object())) == {}


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

    # A second old worktree: no task/run record protects anything anymore, so
    # the age guard and the capture contract are the only protection left.
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


def test_reap_orphan_worktrees_appends_its_registered_event(
    source_repo, isolate_agent_runtime_root, tmp_path, monkeypatch
):
    """``worktree.orphans_reaped`` was emitted-but-unregistered until wave 1;
    the append used to raise inside the emitter's ``except`` and drop the row.
    A reap that lands nothing observable is a reap nobody can audit."""

    import os
    import time

    from agent_runtime import repo_context as repo_context_mod
    from agent_runtime.delivery_directive import reap_orphan_worktrees
    from agent_runtime.events import ALLOWED_EVENT_TYPES, EventLog

    assert "worktree.orphans_reaped" in ALLOWED_EVENT_TYPES

    wt_base = tmp_path / "wtbase"
    monkeypatch.setattr(repo_context_mod, "_worktree_base_dir", lambda: wt_base)
    orphan = _worktree_with_changes(source_repo, task_id="task_evt", run_id="run_evt")
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    event_log = EventLog()

    result = reap_orphan_worktrees(min_age_seconds=3600, event_log=event_log)

    event = event_log.tail(1)[0]
    assert event.type == "worktree.orphans_reaped"
    assert event.payload["reaped_count"] == len(result["reaped"])
    assert event.payload["kept_count"] == len(result["kept"])
    assert event.payload["captured"] == [
        item["captured_patch"] for item in result["reaped"] if item.get("captured_patch")
    ]
    assert event.payload["captured"], "a dirty candidate must report its capture"


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
    owned = _worktree_with_changes(
        source_repo, task_id="task_legacy_open", run_id="run_legacy_open"
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
