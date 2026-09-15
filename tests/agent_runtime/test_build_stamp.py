"""``agent_runtime.build_stamp`` — the runtime's answer to "which code am I?".

The property under test throughout: an UNRESOLVABLE stamp must be typed and
empty, never fabricated. A ``dirty: false`` invented because git was missing
reads exactly like a measured one, and would tell an operator "your service is
current" about a service that is not — the same well-formed-wrong-answer shape
that made three 2026-08 root incidents survive investigation.
"""

from __future__ import annotations

import subprocess
import time

import pytest

from agent_runtime import build_stamp as build_stamp_module
from agent_runtime.build_identity import CodeTree, code_tree_for, code_tree_rule
from agent_runtime.build_stamp import (
    SOURCE_BUILD_SHA_FILE,
    SOURCE_GIT,
    SOURCE_UNKNOWN,
    build_stamp,
    reset_build_stamp_cache,
)


@pytest.fixture(autouse=True)
def _fresh_stamp():
    reset_build_stamp_cache()
    yield
    reset_build_stamp_cache()


def _git(root, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture
def real_repo(tmp_path):
    """A genuine one-commit git repo — not a mock of one.

    The stamp's whole job is to run git correctly (worktree ``.git`` files,
    ``-uno``, the timeout), so the load-bearing tests drive real git.
    """

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "stamp@test")
    _git(root, "config", "user.name", "stamp")
    (root / "tracked.txt").write_bytes(b"one\n")
    _git(root, "add", "tracked.txt")
    _git(root, "commit", "-qm", "one")
    return root


def test_stamp_reports_the_real_head_commit_and_clean_state(real_repo, monkeypatch):
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)

    stamp = build_stamp(refresh=True)

    assert stamp.commit == _git(real_repo, "rev-parse", "HEAD")
    assert stamp.dirty is False
    assert stamp.source == SOURCE_GIT
    assert stamp.reason == ""
    assert stamp.resolved is True


def test_tracked_modification_is_dirty_but_an_untracked_file_is_not(
    real_repo, monkeypatch
):
    """``-uno`` is the contract, not an implementation detail.

    "This runtime is running edited code" is a statement about TRACKED files.
    A scratch file in the checkout would otherwise pin every dev runtime to
    ``dirty: true`` forever, and a flag that is always on carries no signal.
    """

    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)

    (real_repo / "untracked.txt").write_bytes(b"scratch\n")
    assert build_stamp(refresh=True).dirty is False

    (real_repo / "tracked.txt").write_bytes(b"two\n")
    assert build_stamp(refresh=True).dirty is True


def test_worktree_dot_git_file_still_resolves_a_repo_root(real_repo, tmp_path):
    """A linked worktree's ``.git`` is a FILE, and every agent works in one.

    An ``is_dir()`` probe here would report "no repo" for exactly the
    checkouts most likely to be running unlanded code.
    """

    worktree = tmp_path / "wt"
    _git(real_repo, "worktree", "add", "-q", str(worktree), "-b", "probe")
    assert (worktree / ".git").is_file()

    resolved = build_stamp_module.repo_root_for(worktree / "tracked.txt")

    assert resolved == worktree


def test_unresolvable_repo_yields_a_typed_unknown_never_a_fabricated_value(monkeypatch):
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: None)
    monkeypatch.setattr(build_stamp_module, "_baked_sha", lambda root: None)

    stamp = build_stamp(refresh=True)

    assert stamp.commit is None
    assert stamp.dirty is None  # NOT False — absent means not measured
    assert stamp.source == SOURCE_UNKNOWN
    assert stamp.reason == "no_repo_root"
    assert stamp.resolved is False
    payload = stamp.payload()
    assert payload["commit"] is None and payload["commit_short"] is None


def test_a_hung_git_is_reported_as_a_timeout_and_never_raises(real_repo, monkeypatch):
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)
    monkeypatch.setattr(build_stamp_module, "_baked_sha", lambda root: None)

    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=build_stamp_module.GIT_TIMEOUT_SECONDS)

    monkeypatch.setattr(build_stamp_module.subprocess, "run", hang)

    stamp = build_stamp(refresh=True)

    assert stamp.commit is None
    assert stamp.reason == "git_timeout"
    assert stamp.source == SOURCE_UNKNOWN


def test_a_missing_git_binary_is_reported_not_raised(real_repo, monkeypatch):
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)
    monkeypatch.setattr(build_stamp_module, "_baked_sha", lambda root: None)
    monkeypatch.setattr(
        build_stamp_module.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")),
    )

    assert build_stamp(refresh=True).reason == "git_missing"


def test_a_known_commit_with_an_unreadable_status_keeps_the_commit_and_nulls_dirty(
    real_repo, monkeypatch
):
    """Partial knowledge is reported partially, not rounded to either extreme."""

    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)
    real_run = build_stamp_module.subprocess.run

    class _Failed:
        returncode = 1
        stdout = ""
        stderr = "boom"

    def selective(cmd, *args, **kwargs):
        if "status" in cmd:
            return _Failed()
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(build_stamp_module.subprocess, "run", selective)

    stamp = build_stamp(refresh=True)

    assert stamp.commit == _git(real_repo, "rev-parse", "HEAD")
    assert stamp.dirty is None
    assert stamp.reason == "dirty_unknown:git_failed"
    assert stamp.source == SOURCE_GIT


def test_the_baked_sha_file_answers_when_there_is_no_git(tmp_path, monkeypatch):
    """The Docker path: ``.dockerignore`` drops ``.git`` entirely."""

    (tmp_path / ".hermes_build_sha").write_bytes(b"a" * 40 + b"\n")
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: None)
    monkeypatch.setattr(build_stamp_module, "_fallback_repo_root", lambda: tmp_path)

    stamp = build_stamp(refresh=True)

    assert stamp.commit == "a" * 40
    assert stamp.source == SOURCE_BUILD_SHA_FILE
    assert stamp.dirty is None  # unknowable in an image, so not claimed


def test_the_stamp_is_resolved_once_per_process(real_repo, monkeypatch):
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)
    calls = {"n": 0}
    real_resolve = build_stamp_module._resolve

    def counted():
        calls["n"] += 1
        return real_resolve()

    monkeypatch.setattr(build_stamp_module, "_resolve", counted)

    first = build_stamp(refresh=True)
    second = build_stamp()
    third = build_stamp()

    assert calls["n"] == 1
    assert first is second is third


def test_uptime_comes_from_the_monotonic_baseline_not_the_wall_clock(monkeypatch):
    """A clock step must not make a live service look like it booted later.

    Pinning only the MONOTONIC baseline back by five seconds is the whole
    proof: if uptime were computed from ``boot_at`` (wall clock) it would be
    unaffected by this and read ~0.
    """

    monkeypatch.setattr(
        build_stamp_module, "_BOOT_MONOTONIC", time.monotonic() - 5.0
    )

    payload = build_stamp(refresh=True).payload()

    assert payload["uptime_ms"] >= 5000
    assert payload["boot_at"].endswith("Z")


def test_the_frame_block_is_the_keys_the_ready_frame_and_the_register_row_carry():
    """RS-6's census: the block the launcher reads, and nothing it has to infer.

    ``code_tree`` and ``code_tree_rule`` are the two keys RL-20 now compares on;
    ``code_tree_reason`` is the third because a null digest beside a rule is
    otherwise unreadable — "this hermes predates the key", "this is a Docker
    image", and "git timed out on the boot path" are three different facts and a
    launcher that falls back to the commit comparison should be able to say
    which one it fell back FOR.
    """

    assert set(build_stamp().frame_payload()) == {
        "commit",
        "dirty",
        "source",
        "resolved_at",
        "code_tree",
        "code_tree_rule",
        "code_tree_reason",
    }


def test_a_git_checkout_stamps_a_code_tree_and_the_rule_that_made_it(
    real_repo, monkeypatch
):
    """RS-6: the row carries the digest AND the rule, so the reader cannot drift."""

    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)

    stamp = build_stamp(refresh=True)
    block = stamp.frame_payload()

    assert stamp.source == SOURCE_GIT
    assert block["code_tree"] == code_tree_for(real_repo).code_tree
    assert isinstance(block["code_tree"], str) and len(block["code_tree"]) == 40
    assert block["code_tree_reason"] == ""
    assert block["code_tree_rule"] == code_tree_rule()


def test_a_docs_only_commit_leaves_the_code_tree_where_it_was(real_repo, monkeypatch):
    """The field defect of 2026-09-07 16:25:09Z, at the stamp's own seam.

    The commit moves — it is a real landing — and the digest does not, so RL-20
    has nothing to restart.
    """

    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)
    before = build_stamp(refresh=True)

    (real_repo / "docs").mkdir(exist_ok=True)
    (real_repo / "docs" / "plan.md").write_bytes(b"a plan\n")
    (real_repo / "NOTES.md").write_bytes(b"root prose\n")
    _git(real_repo, "add", "-A")
    _git(real_repo, "commit", "-qm", "docs only")

    after = build_stamp(refresh=True)

    assert after.commit != before.commit
    assert after.frame_payload()["code_tree"] == before.frame_payload()["code_tree"]


def test_a_non_git_source_writes_no_code_tree_and_the_row_says_why(
    tmp_path, monkeypatch
):
    """A baked sha is a commit with no tree to read. Never a fabricated digest."""

    (tmp_path / ".hermes_build_sha").write_bytes(b"a" * 40 + b"\n")
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: None)
    monkeypatch.setattr(build_stamp_module, "_fallback_repo_root", lambda: tmp_path)

    block = build_stamp(refresh=True).frame_payload()

    assert block["source"] == SOURCE_BUILD_SHA_FILE
    assert block["code_tree"] is None
    assert block["code_tree_reason"] == f"not_git:{SOURCE_BUILD_SHA_FILE}"
    # The rule is a constant of this build and is stated even when nothing was
    # measured: a reader learns which rule WOULD have applied.
    assert block["code_tree_rule"] == code_tree_rule()


def test_an_unresolvable_repo_says_not_git_unknown_rather_than_nothing(monkeypatch):
    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: None)
    monkeypatch.setattr(build_stamp_module, "_baked_sha", lambda root: None)

    block = build_stamp(refresh=True).frame_payload()

    assert block["source"] == SOURCE_UNKNOWN
    assert block["code_tree"] is None
    assert block["code_tree_reason"] == f"not_git:{SOURCE_UNKNOWN}"


def test_a_commit_whose_tree_could_not_be_read_keeps_the_commit_and_says_so(
    real_repo, monkeypatch
):
    """Partial knowledge, reported partially — the same rule ``dirty`` follows."""

    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)
    monkeypatch.setattr(
        build_stamp_module,
        "code_tree_for",
        lambda *a, **k: CodeTree(
            code_tree=None, reason="git_timeout", entry_count=None
        ),
    )

    block = build_stamp(refresh=True).frame_payload()

    assert block["commit"] == _git(real_repo, "rev-parse", "HEAD")
    assert block["source"] == SOURCE_GIT
    assert block["code_tree"] is None
    assert block["code_tree_reason"] == "git_timeout"


def test_the_code_tree_is_measured_once_per_process_like_the_commit(
    real_repo, monkeypatch
):
    """One extra subprocess at boot, not one per caller — the stamp is memoised."""

    monkeypatch.setattr(build_stamp_module, "repo_root_for", lambda *a, **k: real_repo)
    calls: list[object] = []

    def counted(root, head="HEAD"):
        calls.append(root)
        return CodeTree(code_tree="c" * 40, reason="", entry_count=1)

    monkeypatch.setattr(build_stamp_module, "code_tree_for", counted)

    build_stamp(refresh=True)
    for _ in range(4):
        build_stamp()

    assert len(calls) == 1
