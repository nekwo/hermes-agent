from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any

from hermes_time import now

from . import paths

HARNESS_WORKTREE_GC_TTL_SECONDS = 24 * 60 * 60


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


def command_workdir_for_task(task, explicit_workdir=None) -> Path:
    ctx = repo_execution_context_for_task(task, explicit_workdir=explicit_workdir)
    if ctx is not None:
        return ctx.workdir
    return Path.cwd()


def isolated_repo_context_for_run(repo_ctx: RepoExecutionContext, *, task_id: str, run_id: str) -> RepoExecutionContext:
    """Create a per-run git worktree for grounded agent execution."""

    source_root = _git_root_for(repo_ctx.workdir)
    if source_root is None:
        return repo_ctx
    base = _worktree_base_dir() / _worktree_token(source_root, task_id=task_id, run_id=run_id, repo_label=repo_ctx.repo_label)
    worktree = _ensure_isolated_worktree(source_root, base)
    return _context_for_workdir(worktree, source=f"{repo_ctx.source}-worktree")


def _worktree_token(source_root: Path, *, task_id: str, run_id: str, repo_label: str) -> str:
    raw = f"{source_root.resolve()}|{task_id}|{run_id}|{repo_label}"
    digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{_safe_path_token(repo_label)[:24]}_{digest}"


def _worktree_base_dir() -> Path:
    candidate = paths.store_root() / "wt"
    if len(str(candidate)) <= 90:
        return candidate
    return Path(tempfile.gettempdir()) / "hermes-agent-wt"


def _ensure_isolated_worktree(source_root: Path, base: Path) -> Path:
    candidates = [base, *[base.with_name(f"{base.name}_{idx}") for idx in range(1, 4)]]
    last_error = ""
    base.parent.mkdir(parents=True, exist_ok=True)
    _gc_stale_harness_worktrees(source_root, base.parent, protected={candidate.resolve() for candidate in candidates})
    for index, worktree in enumerate(candidates):
        if worktree.exists() and _git_root_for(worktree) is not None:
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
            timeout=60,
            check=False,
        )
        if result.returncode == 0:
            for _ in range(20):
                if _git_root_for(worktree) is not None:
                    return worktree
                time.sleep(0.1)
            if (worktree / ".git").exists():
                return worktree
        last_error = (result.stderr or result.stdout or "").strip()[:500]
    raise ValueError(f"could not create isolated git worktree for agent run: {last_error}")


def _gc_stale_harness_worktrees(source_root: Path, base_dir: Path, *, protected: set[Path] | None = None) -> None:
    if not base_dir.exists() or not base_dir.is_dir():
        return
    protected = protected or set()
    cutoff = time.time() - HARNESS_WORKTREE_GC_TTL_SECONDS
    for worktree in sorted(base_dir.iterdir()):
        try:
            resolved = worktree.resolve()
        except OSError:
            continue
        if resolved in protected or not worktree.is_dir():
            continue
        try:
            if worktree.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if _git_root_for(worktree) is None:
            continue
        status = _git_output(worktree, ["git", "status", "--short"])
        if status:
            continue
        subprocess.run(
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


def capture_repo_baseline(workdir: str | Path) -> dict[str, Any]:
    """Capture pre-run dirty state so handoff diffs do not claim old work."""

    root = _git_root_for(Path(workdir).expanduser()) or Path(workdir).expanduser()
    status_lines = _git_output(root, ["git", "status", "--short"])
    return {
        "schema_version": 1,
        "captured_at": now().isoformat(),
        "workdir_label": _safe_repo_label(root.name),
        "git_head": _git_output(root, ["git", "rev-parse", "--short", "HEAD"], single=True),
        "dirty_count": len(status_lines),
        "dirty_paths": _dirty_paths_from_status(status_lines),
        "status_lines": status_lines[:200],
    }


def git_diff_since_baseline(workdir: str | Path, baseline: dict[str, Any] | None, *, max_chars: int = 50000) -> dict[str, Any]:
    """Return the current git diff, excluding paths dirty before the run."""

    root = _git_root_for(Path(workdir).expanduser()) or Path(workdir).expanduser()
    baseline_paths = [str(path) for path in ((baseline or {}).get("dirty_paths") or []) if str(path).strip()]
    lines = _filter_diff_lines(
        _git_output(root, ["git", "diff", "--no-ext-diff", "--", "."]),
        baseline_paths=baseline_paths,
    )
    diff_text = "\n".join(lines)
    truncated = len(diff_text) > max_chars
    if truncated:
        diff_text = diff_text[:max_chars].rstrip()
    return {
        "schema_version": 1,
        "captured_at": now().isoformat(),
        "workdir_label": _safe_repo_label(root.name),
        "baseline_dirty_count": len(baseline_paths),
        "excluded_baseline_paths": baseline_paths[:50],
        "diff": diff_text,
        "diff_chars": len(diff_text),
        "truncated": truncated,
    }


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


def _dirty_paths_from_status(status_lines: list[str]) -> list[str]:
    paths: list[str] = []
    for line in status_lines:
        raw = line[3:].strip() if len(line) > 3 else line.strip()
        parts = [part.strip() for part in raw.split(" -> ") if part.strip()]
        for part in parts or [raw]:
            clean = part.strip().strip('"').replace("\\", "/")
            if clean and clean not in paths:
                paths.append(clean)
    return paths[:200]


def _filter_diff_lines(lines: list[str], *, baseline_paths: list[str]) -> list[str]:
    if not baseline_paths:
        return lines
    excluded = {path.replace("\\", "/").strip("/") for path in baseline_paths if path}
    filtered: list[str] = []
    current: list[str] = []
    drop_current = False
    for line in lines:
        if line.startswith("diff --git "):
            if current and not drop_current:
                filtered.extend(current)
            current = [line]
            drop_current = _diff_header_touches_excluded_path(line, excluded)
            continue
        current.append(line)
    if current and not drop_current:
        filtered.extend(current)
    return filtered


def _diff_header_touches_excluded_path(line: str, excluded: set[str]) -> bool:
    parts = line.split()
    candidates = []
    for part in parts[2:4]:
        if part.startswith(("a/", "b/")):
            candidates.append(part[2:].replace("\\", "/").strip("/"))
    return any(candidate in excluded for candidate in candidates)


def resolve_affected_repo_workdir(repo: str) -> Path | None:
    path = Path(repo).expanduser()
    if path.is_absolute() and path.is_dir():
        return _git_root_for(path) or path

    alias = _normalize_repo_alias(repo)
    if alias in _REPO_ALIAS_PATHS:
        for candidate in _REPO_ALIAS_PATHS[alias]:
            resolved = Path(candidate).expanduser()
            if resolved.is_dir():
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


def _context_for_workdir(workdir: Path, *, source: str) -> RepoExecutionContext:
    resolved = workdir.resolve()
    context_files = _project_context_files(resolved)
    return RepoExecutionContext(
        workdir=resolved,
        repo_label=_safe_repo_label(resolved.name),
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
            clean = _SECRET_ASSIGNMENT_RE.sub(r"\1<redacted>", clean)
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
_REPO_ALIAS_PATHS = {
    "eterniabackend": (
        "X:/Unreal Engine/Engine/EterniaBackend/eternia-backend",
        "X:/Unreal Engine/Engine/EterniaBackend",
        "X:/Unreal Engine/Engine/eternia-backend",
    ),
    "eternia-backend": (
        "X:/Unreal Engine/Engine/EterniaBackend/eternia-backend",
        "X:/Unreal Engine/Engine/EterniaBackend",
        "X:/Unreal Engine/Engine/eternia-backend",
    ),
    "backend": ("X:/Unreal Engine/Engine/EterniaBackend/eternia-backend", "X:/Unreal Engine/Engine/EterniaBackend"),
    "eternialauncher": ("X:/Unreal Engine/Engine/Launcher/EterniaLauncher",),
    "eternia-launcher": ("X:/Unreal Engine/Engine/Launcher/EterniaLauncher",),
    "frontend": ("X:/Unreal Engine/Engine/Launcher/EterniaLauncher",),
    "launcher": ("X:/Unreal Engine/Engine/Launcher/EterniaLauncher",),
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
_SECRET_ASSIGNMENT_RE = re.compile(
    r"((?:api[_-]?key|token|secret|password|authorization|bearer|credential)\s*[:=]\s*)[^,\s]+",
    re.IGNORECASE,
)


def _repo_alias_display_label(alias: str) -> str | None:
    return _REPO_ALIAS_DISPLAY_LABELS.get(alias)
