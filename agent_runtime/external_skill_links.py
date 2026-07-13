"""Link the shared canonical skills into external agent harnesses.

Mission Control keeps one physical skills root at
:func:`hermes_constants.get_shared_skills_dir` (``<hermes_root>/shared/skills``).
Other harnesses on the same machine — Claude Code (``~/.claude/skills``) and
Codex (``~/.codex/skills``) — discover skills from their own directories. This
module makes those directories *reference* the shared root by placing a
per-skill link (a symlink, or a directory junction on Windows when symlinks are
unprivileged) for every shared skill, so all harnesses see one physical copy.

Design guarantees:

* **Idempotent** — a second run is a no-op; correct links report ``already``.
* **Non-destructive** — a real (non-link) entry of the same name is never
  overwritten; it is reported ``skipped_real`` and left untouched.
* **Self-healing** — a managed link pointing at the wrong place is repaired, and
  a managed link into the shared root whose skill no longer exists is pruned.
  Only links this tool manages (targets resolve into the shared root) are ever
  removed — foreign links (e.g. Codex's own ``mission-control-harness`` →
  ``~/.claude``) are left alone.
* **Cross-platform** — POSIX symlinks; on Windows a symlink is attempted first
  and a directory junction (no admin / developer-mode needed) is the fallback.
* **Install-aware** — a target is skipped when its tool home (``~/.claude`` /
  ``~/.codex``) is absent, so dirs are never created for tools that aren't
  installed.
"""

from __future__ import annotations

import os
import stat as _stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from hermes_constants import get_shared_skills_dir


def default_external_skill_dirs() -> list[Path]:
    """The external harness skill dirs to keep in sync, in a stable order.

    Kept as a function (not a module constant) so ``Path.home()`` is resolved at
    call time and tests can pass their own list.
    """
    home = Path.home()
    return [home / ".claude" / "skills", home / ".codex" / "skills"]


@dataclass(frozen=True, slots=True)
class LinkAction:
    target_dir: str
    skill: str
    # linked | already | repaired | skipped_real | pruned | failed | skipped_absent
    outcome: str
    detail: str = ""


@dataclass
class LinkReport:
    actions: list[LinkAction] = field(default_factory=list)

    def add(self, target_dir: Path, skill: str, outcome: str, detail: str = "") -> None:
        self.actions.append(LinkAction(str(target_dir), skill, outcome, detail))

    def count(self, outcome: str) -> int:
        return sum(1 for a in self.actions if a.outcome == outcome)

    @property
    def ok(self) -> bool:
        return self.count("failed") == 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "summary": {
                key: self.count(key)
                for key in (
                    "linked",
                    "already",
                    "repaired",
                    "skipped_real",
                    "pruned",
                    "failed",
                    "skipped_absent",
                )
                if self.count(key)
            },
            "actions": [
                {
                    "target": a.target_dir,
                    "skill": a.skill,
                    "outcome": a.outcome,
                    "detail": a.detail,
                }
                for a in self.actions
            ],
        }


# ── link primitives (cross-platform) ─────────────────────────────────────────


def _is_managed_link(path: Path) -> bool:
    """True if *path* is a symlink OR a Windows directory junction.

    ``Path.is_symlink()`` misses junctions, so we also treat any reparse point
    as a managed link on Windows.
    """
    try:
        st = path.lstat()
    except OSError:
        return False
    if _stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _resolves_to(path: Path, src: Path) -> bool:
    try:
        return os.path.realpath(path) == os.path.realpath(src)
    except OSError:
        return False


def _resolves_under(path: Path, root: Path) -> bool:
    """True if *path*'s link target resolves to *root* or a child of it."""
    try:
        real = Path(os.path.realpath(path))
        root_real = Path(os.path.realpath(root))
    except OSError:
        return False
    return real == root_real or root_real in real.parents


def _remove_link(path: Path) -> None:
    """Remove a directory link (symlink/junction) without touching its target."""
    if os.name == "nt":
        try:
            os.rmdir(path)  # dir symlink / junction
            return
        except OSError:
            os.unlink(path)
            return
    os.unlink(path)


def _create_dir_link(link_path: Path, src: Path) -> str:
    """Create a directory link at *link_path* → *src*. Returns the mechanism used.

    Prefers a symlink; on Windows falls back to a junction when symlink creation
    is unprivileged (the common non-developer-mode case).
    """
    try:
        os.symlink(src, link_path, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(src)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "junction"
        raise OSError(
            (result.stderr or result.stdout or "mklink /J failed").strip()
        )


# ── discovery ────────────────────────────────────────────────────────────────


def shared_skill_names(shared_root: Path) -> list[str]:
    """Names of real skills (a non-dot dir containing SKILL.md) in *shared_root*."""
    if not shared_root.exists():
        return []
    names: list[str] = []
    for child in sorted(shared_root.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and (child / "SKILL.md").is_file():
            names.append(child.name)
    return names


# ── reconciliation ───────────────────────────────────────────────────────────


def _reconcile_one(report: LinkReport, ext: Path, skill: str, src: Path) -> None:
    link_path = ext / skill
    if _is_managed_link(link_path):
        if _resolves_to(link_path, src):
            report.add(ext, skill, "already")
            return
        try:
            _remove_link(link_path)
            mech = _create_dir_link(link_path, src)
            report.add(ext, skill, "repaired", mech)
        except OSError as err:
            report.add(ext, skill, "failed", f"repair: {err}")
        return
    if link_path.exists():
        # A real file/dir owns this name — never clobber user/tool content.
        report.add(ext, skill, "skipped_real", "existing non-link entry")
        return
    try:
        mech = _create_dir_link(link_path, src)
        report.add(ext, skill, "linked", mech)
    except OSError as err:
        report.add(ext, skill, "failed", f"create: {err}")


def _prune_stale(report: LinkReport, ext: Path, shared_root: Path, keep: set[str]) -> None:
    try:
        entries = list(ext.iterdir())
    except OSError:
        return
    for entry in sorted(entries):
        if entry.name in keep or not _is_managed_link(entry):
            continue
        # Only prune links WE manage — i.e. that resolve into the shared root.
        if not _resolves_under(entry, shared_root):
            continue
        try:
            _remove_link(entry)
            report.add(ext, entry.name, "pruned", "shared skill removed")
        except OSError as err:
            report.add(ext, entry.name, "failed", f"prune: {err}")


def link_shared_skills_into_external_harnesses(
    *,
    shared_root: Path | None = None,
    external_dirs: Iterable[Path] | None = None,
) -> LinkReport:
    """Reconcile every external harness skill dir against the shared root.

    Returns a :class:`LinkReport`. Never raises for individual link failures —
    those are captured as ``failed`` actions so one locked file can't abort the
    whole sweep.
    """
    shared_root = shared_root or get_shared_skills_dir()
    dirs = list(external_dirs) if external_dirs is not None else default_external_skill_dirs()
    report = LinkReport()
    skills = shared_skill_names(shared_root)
    keep = set(skills)
    for ext in dirs:
        if not ext.parent.exists():
            report.add(ext, "*", "skipped_absent", "tool home not installed")
            continue
        try:
            ext.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            report.add(ext, "*", "failed", f"mkdir: {err}")
            continue
        for skill in skills:
            _reconcile_one(report, ext, skill, shared_root / skill)
        _prune_stale(report, ext, shared_root, keep)
    return report


# ── CLI formatting ───────────────────────────────────────────────────────────


def format_report(report: LinkReport) -> str:
    lines: list[str] = []
    summary = report.to_dict()["summary"]
    if summary:
        parts = ", ".join(f"{v} {k}" for k, v in summary.items())
        lines.append(f"Shared skills → external harnesses: {parts}")
    else:
        lines.append("Shared skills → external harnesses: nothing to do")
    for a in report.actions:
        if a.outcome in ("already",):
            continue
        detail = f" ({a.detail})" if a.detail else ""
        lines.append(f"  [{a.outcome}] {a.target_dir}/{a.skill}{detail}")
    return "\n".join(lines)
