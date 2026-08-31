from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Any

from hermes_time import now

from . import paths
from .machine_roots import load_machine_roots
from .redaction import TEXT_SECRET_VALUE_ASSIGNMENT_RE

HARNESS_WORKTREE_GC_TTL_SECONDS = 24 * 60 * 60
# Bound same-day churn: keep at most this many clean worktrees per source repo.
# Older clean worktrees beyond the cap are reaped even within the TTL window.
HARNESS_WORKTREE_GC_MAX_PER_REPO = 12
# Never count-cap a worktree younger than this — a freshly created worktree is
# almost certainly an in-flight run, so age it out before it becomes eligible.
HARNESS_WORKTREE_GC_MIN_AGE_SECONDS = 15 * 60
# Launcher worktrees are large on Windows; a healthy checkout can spend more
# than a minute in Git's "Updating files" phase before the agent turn starts.
HARNESS_WORKTREE_ADD_TIMEOUT_SECONDS = 300
# Keep the base short enough that large repos with deep generated paths do not
# fail halfway through worktree materialization on Windows path limits.
HARNESS_WORKTREE_BASE_MAX_CHARS = 55


@dataclass(frozen=True, slots=True)
class RepoExecutionContext:
    """Redaction-safe execution context for a task's affected repository."""

    workdir: Path
    repo_label: str
    source: str
    context_files: tuple[str, ...] = field(default_factory=tuple)
    context_excerpts: tuple[RepoContextExcerpt, ...] = field(default_factory=tuple)

    @property
    def context_loaded_label(self) -> str:
        return ", ".join(self.context_files) if self.context_files else "none"


@dataclass(frozen=True, slots=True)
class RepoContextExcerpt:
    label: str
    content: str
    truncated: bool = False


def repo_execution_context_for_task(task, explicit_workdir=None) -> RepoExecutionContext | None:
    """Resolve the first safe affected repo workdir for persona execution.

    Returns None when the task has no affected repos. Raises ValueError when the
    task supplied affected repos but none resolve safely, matching command-proof
    fail-closed behavior.
    """

    if explicit_workdir is not None:
        workdir = Path(explicit_workdir).expanduser()
        if not workdir.is_dir():
            raise ValueError("affected repo workdir does not exist or is not a directory")
        return _context_for_workdir(workdir, source="explicit")

    affected_repos = [str(repo).strip() for repo in (getattr(task, "affected_repos", []) or []) if str(repo).strip()]
    for repo in affected_repos:
        resolved = resolve_affected_repo_workdir(repo)
        if resolved is not None:
            source = _normalize_repo_alias(repo) or "absolute"
            return _context_for_workdir(resolved, source=source)

    if affected_repos:
        raise ValueError(
            "could not resolve a valid affected repo workdir; "
            f"affected_repos={safe_affected_repo_labels(affected_repos)!r}"
        )
    return None


def isolated_repo_context_for_run(repo_ctx: RepoExecutionContext, *, task_id: str, run_id: str) -> RepoExecutionContext:
    """Create a per-run git worktree for grounded agent execution.

    KEPT DELIBERATELY (S24, 2026-07-30): this has **no production caller** since
    the worker/dispatch lane went in S5/S8 — it survives as the constructor the
    worktree suites build real worktrees with. Twelve tests in
    ``tests/agent_runtime/test_repo_context_observation.py`` pin protections
    reachable only through here (GC count cap, dirty/fresh sparing, fail-closed
    ``worktree add``, checkout timeout) including two live-incident regressions:
    junction severing that once emptied the backend venv (2026-07-01) and the
    backend ``.env`` copy whose absence broke every read-only proof (2026-07-03).
    Rebuilding the fixtures on raw ``git worktree add`` would delete those
    regressions, so this is labelled rather than left looking live. It,
    :func:`_worktree_token`, :func:`_ensure_isolated_worktree`,
    :func:`existing_run_worktrees` and :func:`remove_harness_worktree_for_repo`
    are ONE lane — retire them together or not at all. The live consumer of what
    this lane leaves on disk is ``delivery_directive.reap_orphan_worktrees``.
    """

    source_root = _git_root_for(repo_ctx.workdir)
    if source_root is None:
        message = f"cannot isolate non-git repo workdir for agent run: {repo_ctx.repo_label}"
        _log_worktree_event(
            "worktree_create_failed",
            {
                "task_id": task_id,
                "run_id": run_id,
                "repo_label": repo_ctx.repo_label,
                "reason": "source_not_git_repo",
            },
        )
        raise ValueError(message)
    base = _worktree_base_dir() / _worktree_token(source_root, task_id=task_id, run_id=run_id, repo_label=repo_ctx.repo_label)
    worktree = _ensure_isolated_worktree(source_root, base)
    return _context_for_workdir(worktree, source=f"{repo_ctx.source}-worktree")


def _worktree_token(source_root: Path, *, task_id: str, run_id: str, repo_label: str) -> str:
    raw = f"{source_root.resolve()}|{task_id}|{run_id}|{repo_label}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{_safe_path_token(repo_label)[:24]}_{digest}"


def _worktree_base_dir() -> Path:
    candidate = paths.store_root() / "wt"
    if len(str(candidate)) <= HARNESS_WORKTREE_BASE_MAX_CHARS:
        return candidate
    return Path(tempfile.gettempdir()) / "hermes-agent-wt"


def _ensure_isolated_worktree(source_root: Path, base: Path) -> Path:
    candidates = [base, *[base.with_name(f"{base.name}_{idx}") for idx in range(1, 4)]]
    last_error = ""
    base.parent.mkdir(parents=True, exist_ok=True)
    _gc_stale_harness_worktrees(source_root, base.parent, protected={candidate.resolve() for candidate in candidates})
    for index, worktree in enumerate(candidates):
        if worktree.exists() and _git_root_for(worktree) is not None:
            if not _is_detached_head(worktree):
                last_error = "existing worktree is not detached HEAD"
                _log_worktree_event(
                    "worktree_reuse_rejected",
                    {"worktree": str(worktree), "source_root_label": source_root.name, "reason": last_error},
                )
                continue
            _materialize_worktree_local_support(source_root, worktree)
            return worktree
        if index == 1:
            _run_git_quiet(source_root, ["git", "worktree", "prune"])
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=source_root,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=HARNESS_WORKTREE_ADD_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode == 0:
            for _ in range(20):
                if _git_root_for(worktree) is not None and _is_detached_head(worktree):
                    _materialize_worktree_local_support(source_root, worktree)
                    _log_worktree_event(
                        "worktree_created",
                        {"worktree": str(worktree), "source_root_label": source_root.name},
                    )
                    return worktree
                time.sleep(0.1)
            if (worktree / ".git").exists() and _is_detached_head(worktree):
                _materialize_worktree_local_support(source_root, worktree)
                _log_worktree_event(
                    "worktree_created",
                    {"worktree": str(worktree), "source_root_label": source_root.name},
                )
                return worktree
        last_error = (result.stderr or result.stdout or "").strip()[:500]
    _log_worktree_event(
        "worktree_create_failed",
        {
            "worktree_base": str(base),
            "source_root_label": source_root.name,
            "reason": last_error or "git worktree add failed",
        },
    )
    raise ValueError(f"could not create isolated git worktree for agent run: {last_error}")


def _worktree_is_reapable(worktree: Path, *, protected: set[Path]) -> bool:
    """A worktree is safe to GC only if it is a clean git worktree we don't protect."""
    try:
        resolved = worktree.resolve()
    except OSError:
        return False
    if resolved in protected or not worktree.is_dir():
        return False
    if _git_root_for(worktree) is None:
        return False
    # Never reap a worktree that carries uncommitted work.
    if _git_output(worktree, ["git", "status", "--short"]):
        return False
    return True


def _remove_harness_worktree(source_root: Path, worktree: Path, *, reason: str) -> bool:
    severed = _sever_worktree_reparse_points(worktree)
    if severed:
        _log_worktree_event(
            "worktree_links_severed",
            {"worktree": str(worktree), "count": severed, "reason": reason},
        )
    result = subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=source_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
        check=False,
    )
    if result.returncode == 0:
        _log_worktree_event(
            "worktree_gc_removed",
            {"worktree": str(worktree), "source_root_label": source_root.name, "reason": reason},
        )
        return True
    return False


def _sever_worktree_reparse_points(worktree: Path) -> int:
    """Delete link entries (junctions / symlinks) inside a worktree WITHOUT
    recursing into their targets, returning how many links were severed.

    ``git worktree remove --force`` on Git for Windows traverses directory
    junctions as plain directories, so removing a worktree that carries a
    support junction (e.g. the ``.EterniaBackendVirtualEnv`` link materialized
    for backend self-tests) deletes the REAL target's contents along with the
    worktree. This happened live on 2026-07-01: the first count-cap GC burst
    emptied the backend repo's virtualenv through exactly this traversal,
    and every backend goal afterwards lost its interpreter. Severing the link
    entries first guarantees only the link dies with the worktree.
    """

    severed = 0
    stack = [worktree]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if _entry_is_reparse_point(entry):
                    if _remove_link_entry(entry.path):
                        severed += 1
                    else:
                        _log_worktree_event(
                            "worktree_link_sever_failed",
                            {"worktree": str(worktree), "link": entry.name},
                        )
                    continue
                if entry.is_dir(follow_symlinks=False) and entry.name != ".git":
                    stack.append(Path(entry.path))
            except OSError:
                continue
    return severed


def _entry_is_reparse_point(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _remove_link_entry(path: str) -> bool:
    """Remove a link entry itself (never its target's contents)."""
    try:
        os.rmdir(path)  # junction / directory symlink: drops the reparse point only
        return True
    except OSError:
        pass
    try:
        os.unlink(path)  # file symlink
        return True
    except OSError:
        return False


def _registered_worktree_paths(source_root: Path) -> set[Path]:
    """Resolved paths of git worktrees registered to source_root (excludes the main checkout)."""
    lines = _git_output(source_root, ["git", "worktree", "list", "--porcelain"])
    found: set[Path] = set()
    seen_main = False
    for line in lines:
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree "):].strip()
        try:
            resolved = Path(raw).resolve()
        except OSError:
            continue
        if not seen_main:
            seen_main = True  # first entry is the main working tree — never GC it
            continue
        found.add(resolved)
    return found


def _gc_stale_harness_worktrees(source_root: Path, base_dir: Path, *, protected: set[Path] | None = None) -> None:
    if not base_dir.exists() or not base_dir.is_dir():
        return
    protected = protected or set()
    now_ts = time.time()
    cutoff = now_ts - HARNESS_WORKTREE_GC_TTL_SECONDS
    # Only worktrees registered to *this* source_root can be removed via it; scoping
    # to them makes the per-repo count cap correct even though base_dir is shared.
    owned = _registered_worktree_paths(source_root)
    survivors: list[tuple[float, Path]] = []
    for worktree in sorted(base_dir.iterdir()):
        try:
            resolved = worktree.resolve()
        except OSError:
            continue
        if resolved not in owned or not _worktree_is_reapable(worktree, protected=protected):
            continue
        try:
            mtime = worktree.stat().st_mtime
        except OSError:
            continue
        # TTL pass: reap clean worktrees older than the TTL (original behavior).
        if mtime < cutoff and _remove_harness_worktree(source_root, worktree, reason="ttl"):
            continue
        survivors.append((mtime, worktree))
    # Count-cap pass: keep the most recent N clean worktrees per repo and reap the
    # oldest ones beyond the cap even within the TTL — bounds same-day churn. A
    # min-age grace protects freshly created (likely in-flight) worktrees.
    if len(survivors) > HARNESS_WORKTREE_GC_MAX_PER_REPO:
        survivors.sort(key=lambda item: item[0])  # oldest first
        overflow = survivors[: len(survivors) - HARNESS_WORKTREE_GC_MAX_PER_REPO]
        for mtime, worktree in overflow:
            if now_ts - mtime < HARNESS_WORKTREE_GC_MIN_AGE_SECONDS:
                continue
            # Re-check cleanliness immediately before removal to avoid racing a run.
            if _worktree_is_reapable(worktree, protected=protected):
                _remove_harness_worktree(source_root, worktree, reason="count_cap")
    _run_git_quiet(source_root, ["git", "worktree", "prune"])


def _run_git_quiet(cwd: Path, args: list[str]) -> None:
    subprocess.run(
        args,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def existing_run_worktrees(repo_label: str, *, task_id: str, run_id: str) -> list[Path]:
    """Deterministic isolated-worktree paths that exist for (repo, task, run).

    Mirrors the candidate fan used by ``_ensure_isolated_worktree`` so callers
    (delivery-directive capture/reap) can recover a run's worktree without any
    side-channel state.
    """

    source_root = resolve_affected_repo_workdir(repo_label)
    git_root = _git_root_for(source_root) if source_root is not None else None
    if git_root is None:
        return []
    # The creation-time token used the execution context's derived label
    # (directory name), NOT the caller's repo alias/path string — recompute it
    # the same way or the hash never matches (e.g. "EterniaBackend" alias vs
    # "eternia-backend" directory).
    token_label = _safe_repo_label(source_root.resolve().name)
    base = _worktree_base_dir() / _worktree_token(git_root, task_id=task_id, run_id=run_id, repo_label=token_label)
    candidates = [base, *[base.with_name(f"{base.name}_{idx}") for idx in range(1, 4)]]
    return [candidate for candidate in candidates if candidate.is_dir() and _git_root_for(candidate) is not None]


def worktree_patch_text(worktree: Path, *, include_untracked: bool = True, timeout_seconds: int = 60) -> str:
    """Binary-safe unified patch of a worktree's changes vs HEAD.

    Raw ``git diff --binary`` stdout — deliberately NOT routed through
    ``_git_output``, which strips/drops lines and would corrupt patch context.
    ``--intent-to-add`` makes untracked files appear as new-file hunks without
    staging content.
    """

    root = _git_root_for(Path(worktree).expanduser())
    if root is None:
        return ""
    if include_untracked:
        _run_git_quiet(root, ["git", "add", "--all", "--intent-to-add"])
    try:
        # Byte-faithful capture: text mode would apply universal-newline
        # translation and silently strip CR from CRLF content, producing a
        # patch that no longer applies to CRLF working trees.
        result = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception:
        return ""
    if result.returncode not in {0, 1}:
        return ""
    text = (result.stdout or b"").decode("utf-8", errors="replace")
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def worktree_patch_size_estimate(worktree: Path, *, timeout_seconds: int = 60) -> int:
    """Estimate capture bytes without changing the worktree or its index.

    Tracked changes use the exact binary-diff byte count. Untracked content is
    represented by its file bytes plus a small per-path patch-header allowance;
    this is deliberately an estimate because producing the exact add-file patch
    would require ``git add --intent-to-add``, which is forbidden on preview.
    """

    root = _git_root_for(Path(worktree).expanduser())
    if root is None:
        return 0
    try:
        tracked = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff", "HEAD"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except Exception:
        return 0
    if tracked.returncode not in {0, 1} or untracked.returncode != 0:
        return 0
    estimate = len(tracked.stdout or b"")
    for raw_path in (untracked.stdout or b"").split(b"\0"):
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        candidate = root / relative
        try:
            content_bytes = candidate.stat().st_size if candidate.is_file() else 0
        except OSError:
            content_bytes = 0
        estimate += content_bytes + len(raw_path) + 128
    return estimate


def remove_harness_worktree_for_repo(repo_label: str, worktree: Path, *, reason: str) -> bool:
    """Public reap for a harness-managed worktree, followed by prune."""

    source_root = resolve_affected_repo_workdir(repo_label)
    git_root = _git_root_for(source_root) if source_root is not None else None
    if git_root is None:
        return False
    removed = _remove_harness_worktree(git_root, Path(worktree), reason=reason)
    if removed:
        _run_git_quiet(git_root, ["git", "worktree", "prune"])
    return removed


def legacy_harness_worktree_base_dir() -> Path:
    """Canonical pre-short-root fallback used by older Harness releases."""

    return Path(tempfile.gettempdir()) / "hermes-agent-wt"


def current_harness_worktree_base_dir() -> Path:
    return _worktree_base_dir()


def harness_worktree_inventory(
    *, include_legacy_temp: bool = False
) -> list[tuple[Path, Path, str, str | None]]:
    """Managed worktree directories with typed base provenance, deduplicated."""

    bases = [(_worktree_base_dir(), "current")]
    if include_legacy_temp:
        bases.append((legacy_harness_worktree_base_dir(), "legacy_temp"))
    rows: list[tuple[Path, Path, str, str | None]] = []
    seen: set[Path] = set()
    for base, source in bases:
        if _path_is_reparse_point(base):
            rows.append((base, base, source, "base_reparse_alias"))
            continue
        if not base.is_dir():
            continue
        try:
            resolved_base = base.resolve()
        except OSError:
            rows.append((base, base, source, "base_unresolvable"))
            continue
        for worktree in base.iterdir():
            if _path_is_reparse_point(worktree):
                rows.append((worktree, base, source, "candidate_reparse_alias"))
                continue
            if not worktree.is_dir():
                continue
            try:
                resolved = worktree.resolve()
            except OSError:
                rows.append((worktree, base, source, "candidate_unresolvable"))
                continue
            if resolved.parent != resolved_base:
                rows.append((worktree, base, source, "candidate_outside_base"))
                continue
            if resolved in seen:
                continue
            seen.add(resolved)
            rows.append((worktree, base, source, None))
    return sorted(rows, key=lambda row: paths.safe_mtime(row[0]))


def _path_is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if os.name != "nt":
            return False
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
        return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except OSError:
        return False


def worktree_source_root(worktree: Path) -> Path | None:
    """Main repository root a harness worktree belongs to (via git-common-dir)."""

    git_dir_text = _git_output(Path(worktree), ["git", "rev-parse", "--git-common-dir"], single=True)
    if not git_dir_text:
        return None
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = Path(worktree) / git_dir
    # <repo>/.git → repo root
    root = git_dir.resolve().parent
    return root if root.is_dir() else None


def remove_orphan_worktree(worktree: Path, *, reason: str) -> bool:
    """Reap a worktree by resolving its own source repo, then prune."""

    source_root = worktree_source_root(worktree)
    if source_root is None:
        return False
    removed = _remove_harness_worktree(source_root, Path(worktree), reason=reason)
    if removed:
        _run_git_quiet(source_root, ["git", "worktree", "prune"])
    return removed


def _materialize_worktree_local_support(source_root: Path, worktree: Path) -> None:
    """Add ignored local-only support needed for deterministic proofs.

    Worktrees intentionally omit untracked local files. Some repos still require
    local non-source support to run read-only proofs: the Launcher pubspec
    declares `.env` as an asset, and the backend proof command uses the
    repo-local virtualenv. We materialize only narrow allowlisted support and
    hide it in the worktree's private exclude file so clean/diff attribution
    remains about product changes.
    """

    support_patterns: list[str] = []
    source_env = source_root / ".env"
    target_env = worktree / ".env"
    if source_env.is_file() and not target_env.exists():
        try:
            if (source_root / "manage.py").is_file():
                # Django settings hard-require env values (_require_env raises on
                # a missing DJANGO_SECRET_KEY), so an empty placeholder makes
                # every read-only proof fail in the worktree — live 2026-07-03
                # (task_826869af): backend_dev blocked 3× on the empty .env even
                # with a working venv. Copy the repo-local dev env: same machine,
                # same user, hidden from diff attribution by the exclude below.
                shutil.copyfile(source_env, target_env)
            else:
                # Launcher only needs the file to exist (pubspec declares .env as
                # an asset); keep the empty placeholder so worktrees do not
                # duplicate env contents where presence alone satisfies proofs.
                target_env.write_text("", encoding="utf-8")
            support_patterns.append(".env")
        except OSError:
            _log_worktree_event("worktree_support_failed", {"worktree": str(worktree), "support": ".env"})
    elif target_env.exists():
        support_patterns.append(".env")

    source_venv = source_root / ".EterniaBackendVirtualEnv"
    target_venv = worktree / ".EterniaBackendVirtualEnv"
    if source_venv.is_dir() and not target_venv.exists():
        if _link_local_support_dir(source_venv, target_venv):
            support_patterns.append(".EterniaBackendVirtualEnv/")
        else:
            _log_worktree_event(
                "worktree_support_failed",
                {"worktree": str(worktree), "support": ".EterniaBackendVirtualEnv"},
            )
    elif target_venv.exists():
        support_patterns.append(".EterniaBackendVirtualEnv/")

    if target_venv.exists() and not _venv_has_interpreter(target_venv):
        # A hollow venv link (dir exists, no interpreter) means every agent
        # self-test that needs it will fail with a confusing per-goal discovery
        # loop. Surface it as a harness event instead of letting each goal
        # re-learn it. Live example: the 2026-07-01 GC junction traversal left
        # the real venv empty and two goals burned 4 extra model turns on it.
        _log_worktree_event(
            "worktree_support_degraded",
            {
                "worktree": str(worktree),
                "support": ".EterniaBackendVirtualEnv",
                "reason": "venv_missing_interpreter",
            },
        )

    if support_patterns:
        _append_worktree_excludes(worktree, support_patterns)


def _venv_has_interpreter(venv_dir: Path) -> bool:
    return (venv_dir / "Scripts" / "python.exe").exists() or (venv_dir / "bin" / "python").exists()


def _link_local_support_dir(source: Path, target: Path) -> bool:
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            return result.returncode == 0
        os.symlink(source, target, target_is_directory=True)
        return True
    except Exception:
        return False


def _append_worktree_excludes(worktree: Path, patterns: list[str]) -> None:
    git_dir_text = _git_output(worktree, ["git", "rev-parse", "--git-common-dir"], single=True)
    if not git_dir_text:
        return
    git_dir = Path(git_dir_text)
    if not git_dir.is_absolute():
        git_dir = worktree / git_dir
    exclude = git_dir / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.is_file() else ""
        additions = [pattern for pattern in patterns if pattern not in {line.strip() for line in existing.splitlines()}]
        if additions:
            with exclude.open("a", encoding="utf-8") as handle:
                for pattern in additions:
                    handle.write(f"\n{pattern}")
                handle.write("\n")
    except OSError:
        _log_worktree_event("worktree_support_failed", {"worktree": str(worktree), "support": "git_exclude"})


def safe_affected_repo_labels(repos: list[str] | tuple[str, ...] | None) -> list[str]:
    labels: list[str] = []
    for repo in repos or []:
        text = str(repo or "").strip()
        if not text:
            continue
        alias = _normalize_repo_alias(text)
        if alias:
            display_label = _repo_alias_display_label(alias)
            if display_label is not None:
                labels.append(display_label)
                continue
        resolved = resolve_affected_repo_workdir(text)
        if resolved is not None:
            labels.append(_safe_repo_label(resolved.name))
            continue
        if alias:
            labels.append(f"{_safe_repo_label(alias)} (unresolved)")
            continue
        name = Path(text).name if (":" in text or "/" in text or "\\" in text) else text
        labels.append(f"{_safe_repo_label(name)} (unresolved; path withheld)")
    return labels


def _git_output(workdir: Path, command: list[str], *, single: bool = False) -> Any:
    try:
        result = subprocess.run(
            command,
            cwd=workdir,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        return "" if single else []
    if result.returncode not in {0, 1}:
        return "" if single else []
    text = (result.stdout or "").rstrip()
    if single:
        return text.strip().splitlines()[0].strip() if text.strip() else ""
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def resolve_affected_repo_workdir(repo: str) -> Path | None:
    path = Path(repo).expanduser()
    if path.is_absolute() and path.is_dir():
        return _git_root_for(path) or path

    alias = _normalize_repo_alias(repo)
    root_name = _REPO_ALIAS_MACHINE_ROOTS.get(alias)
    if root_name is not None:
        resolved = load_machine_roots().get(root_name)
        if resolved is not None and resolved.is_dir():
            return _git_root_for(resolved) or resolved
    if alias in _HARNESS_REPO_ALIASES:
        root = Path(__file__).resolve().parents[1]
        if root.is_dir():
            return root
    return None


def _git_root_for(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _is_detached_head(workdir: Path) -> bool:
    return _git_output(workdir, ["git", "rev-parse", "--abbrev-ref", "HEAD"], single=True) == "HEAD"


def _log_worktree_event(event_type: str, payload: dict[str, Any]) -> None:
    try:
        path = paths.store_root() / "worktree_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "ts": now().isoformat(),
            "type": event_type,
            **payload,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        return


def _context_for_workdir(workdir: Path, *, source: str) -> RepoExecutionContext:
    resolved = workdir.resolve()
    context_files = _project_context_files(resolved)
    source_alias = str(source or "").removesuffix("-worktree")
    repo_label = _REPO_ALIAS_DISPLAY_LABELS.get(source_alias, _safe_repo_label(resolved.name))
    return RepoExecutionContext(
        workdir=resolved,
        repo_label=repo_label,
        source=_safe_repo_label(source) or "repo",
        context_files=context_files,
        context_excerpts=_project_context_excerpts(resolved),
    )


def _project_context_files(workdir: Path) -> tuple[str, ...]:
    loaded: list[str] = []
    for canonical, candidates in (
        (".hermes.md", (".hermes.md", "HERMES.md")),
        ("AGENTS.md", ("AGENTS.md", "agents.md")),
        ("CLAUDE.md", ("CLAUDE.md", "claude.md")),
        (".cursorrules", (".cursorrules",)),
    ):
        if any((workdir / candidate).is_file() for candidate in candidates):
            loaded.append(canonical)
    cursor_rules = workdir / ".cursor" / "rules"
    try:
        if cursor_rules.is_dir() and any(path.suffix == ".mdc" for path in cursor_rules.iterdir()):
            loaded.append(".cursor/rules/*.mdc")
    except OSError:
        pass
    return tuple(loaded)


def _project_context_excerpts(workdir: Path) -> tuple[RepoContextExcerpt, ...]:
    excerpts: list[RepoContextExcerpt] = []
    total_chars = 0
    for label, candidates in (
        (".hermes.md", (".hermes.md", "HERMES.md")),
        ("AGENTS.md", ("AGENTS.md", "agents.md")),
        ("CLAUDE.md", ("CLAUDE.md", "claude.md")),
        (".cursorrules", (".cursorrules",)),
    ):
        path = next((workdir / candidate for candidate in candidates if (workdir / candidate).is_file()), None)
        if path is None:
            continue
        remaining = _MAX_CONTEXT_TOTAL_CHARS - total_chars
        if remaining <= 0:
            break
        excerpt = _read_context_excerpt(path, label=label, char_limit=min(_MAX_CONTEXT_FILE_CHARS, remaining))
        if excerpt is None:
            continue
        total_chars += len(excerpt.content)
        excerpts.append(excerpt)
    return tuple(excerpts)


def _read_context_excerpt(path: Path, *, label: str, char_limit: int) -> RepoContextExcerpt | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    text, line_truncated = _sanitize_context_text(raw)
    if not text:
        return None
    truncated = line_truncated or len(text) > char_limit
    if truncated:
        text = text[:char_limit].rstrip()
    return RepoContextExcerpt(label=label, content=text, truncated=truncated)


def _sanitize_context_text(raw: str) -> tuple[str, bool]:
    lines: list[str] = []
    truncated = False
    for line in raw.replace("\x00", "").splitlines():
        clean = line.rstrip()
        if _SECRET_ASSIGNMENT_RE.search(clean):
            clean = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", clean)
        if len(clean) > _MAX_CONTEXT_LINE_CHARS:
            clean = f"{clean[:_MAX_CONTEXT_LINE_CHARS].rstrip()} ... [truncated]"
            truncated = True
        lines.append(clean)
    return "\n".join(lines).strip(), truncated


def _safe_repo_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-._")
    return cleaned[:64] or "repo"


def _safe_path_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("._")[:96] or "item"


def _normalize_repo_alias(value: str) -> str:
    stripped = value.strip().lower()
    if re.search(r"[^a-z0-9 _-]", stripped):
        return ""
    return re.sub(r"[ _-]+", "-", stripped).strip("-")


_HARNESS_REPO_ALIASES = frozenset({"agent-runtime-harness", "hermes-agent"})
_REPO_ALIAS_MACHINE_ROOTS = {
    "eterniabackend": "eternia_backend",
    "eternia-backend": "eternia_backend",
    "backend": "eternia_backend",
    "eternialauncher": "eternia_launcher",
    "eternia-launcher": "eternia_launcher",
    "frontend": "eternia_launcher",
    "launcher": "eternia_launcher",
}

_REPO_ALIAS_DISPLAY_LABELS = {
    "eterniabackend": "EterniaBackend",
    "eternia-backend": "EterniaBackend",
    "backend": "EterniaBackend",
    "eternialauncher": "EterniaLauncher",
    "eternia-launcher": "EterniaLauncher",
    "frontend": "EterniaLauncher",
    "launcher": "EterniaLauncher",
    "agent-runtime-harness": "hermes-agent",
    "hermes-agent": "hermes-agent",
}

_MAX_CONTEXT_FILE_CHARS = 2500
_MAX_CONTEXT_TOTAL_CHARS = 7000
_MAX_CONTEXT_LINE_CHARS = 500
# Single-homed in ``agent_runtime.redaction`` (see the header there: every
# local spelling of this rule was blind to JSON). The surgical value shape is
# deliberate — a repo-context excerpt is fed back to an agent, so the text
# around a removed value must stay readable.
#
# Group contract changed with the move: group(1) is now the KEY ALONE, where
# the retired local spelling captured "key + separator". The substitution below
# re-emits the ``=`` explicitly, so the rendered output is unchanged for the
# ``KEY=value`` form and normalized (``:`` -> ``=``) for the ``key: value`` one.
_SECRET_ASSIGNMENT_RE = TEXT_SECRET_VALUE_ASSIGNMENT_RE


def _repo_alias_display_label(alias: str) -> str | None:
    return _REPO_ALIAS_DISPLAY_LABELS.get(alias)
