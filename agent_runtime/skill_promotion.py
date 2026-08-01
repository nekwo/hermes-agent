"""Realm skill inbox + hash-guarded promotion core.

This module is the ONE guarded door through which downloaded / authored /
profile-local skill packages become canonical in the shared skills root
(:func:`hermes_constants.get_shared_skills_dir`). It is a pure decision layer
(:func:`classify_promotion`) plus a guarded, atomic, never-delete executor
(:func:`execute_promotion`).

Design authority: ``docs/agent-runtime-harness/SKILL_INBOX_PROMOTION_DESIGN_2026-07-24.md``.

Invariants enforced here:

- **One live namespace.** Canonical content lives only under the shared skills
  root. Downloads land in a per-realm inbox
  (``shared/skills/.realm_inbox/<realm-token>/…``) which the resolver never
  sees (``EXCLUDED_SKILL_DIRS`` — C1).
- **Never delete — archive.** A displaced canonical package or a retired source
  duplicate is *moved* into ``shared/skills/.archive/<UTC ts>/<slug-flattened>/``,
  never removed. The archive dir is excluded from resolver scans.
- **Atomic writes.** A promotion copies the source into a temp dir under the
  excluded ``.archive`` tree, then ``os.replace``s it into the canonical
  location (a same-filesystem rename). The provenance sidecar is written via
  :func:`utils.atomic_json_write`.
- **Provenance lives OUTSIDE skill dirs.** ``shared/skills/.provenance/<slug>.json``
  (``/`` → ``__``). A sidecar inside the package would change its content hash.

This module must NOT import :mod:`agent_runtime.realm_sync` — the pull pipeline
(C3) imports *this* module, so the dependency is one-directional.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any

from agent.skill_utils import (
    EXCLUDED_SKILL_DIRS,
    SKILL_SUPPORT_DIRS,
    skill_package_content_hash,
)
from hermes_constants import get_shared_skills_dir
from hermes_time import now
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Canonical action vocabularies. Pinned so callers (realm_sync C3, the harness
# CLI C4) branch on stable strings rather than re-deriving them.
PLAN_ACTIONS = frozenset(
    {
        "promote_new",
        "noop_identical",
        "hold_divergent",
        "refuse_ambiguous_source",
        "refuse_invalid",
    }
)
_REALM_INBOX_DIRNAME = ".realm_inbox"
_PROVENANCE_DIRNAME = ".provenance"
_ARCHIVE_DIRNAME = ".archive"

# One slug component: 1..120 chars from the same safe alphabet the realm-sync
# path hygiene allows. A category slug is exactly two such components joined by
# a single ``/`` (multi-level nesting is out of scope — design §"Out of scope").
_SLUG_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")

# Windows reserved device basenames. A path component whose STEM (the text
# before the first ``.``, case-insensitive, trailing dots/spaces stripped)
# resolves to one of these names is illegal as a directory/file on Windows —
# ``con``, ``con.md``, ``NUL`` etc. all address the device, not a path — so a
# realm publishing a package named after one would crash the inbox mirror on
# Windows (a pull DoS). Rejected here in :func:`_validate_slug` (so it can never
# be promoted) and skipped by the pull mirror (``_mirror_realm_skill_inbox``)
# before any write is attempted, on every platform for deterministic behaviour.
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)


def is_windows_reserved_component(component: str) -> bool:
    """True when a single path component maps to a Windows reserved device name.

    Windows resolves ``con``, ``con.txt``, ``CON``, ``nul``, ``com1`` … to the
    device regardless of extension or case, and strips trailing dots/spaces, so
    the check is on the lower-cased stem (text before the first ``.``, trimmed).
    """

    stem = str(component or "").split(".", 1)[0].strip().rstrip(".").lower()
    return stem in _WINDOWS_RESERVED_NAMES


# ── Dataclasses (pinned API) ───────────────────────────────────────────────


@dataclass(frozen=True)
class PromotionPlan:
    """Pure classification of a candidate promotion — no side effects.

    ``action`` is one of :data:`PLAN_ACTIONS`. ``classify_promotion`` emits
    ``promote_new`` / ``noop_identical`` / ``hold_divergent`` / ``refuse_invalid``;
    ``refuse_ambiguous_source`` is constructed by a caller (the CLI) that cannot
    resolve a single source and is handled as a refusal by ``execute_promotion``.
    """

    skill: str
    action: str
    source_dir: Path | None
    source_hash: str | None
    canonical_dir: Path | None
    canonical_hash: str | None
    reason: str


@dataclass(frozen=True)
class PromotionResult:
    """Outcome of :func:`execute_promotion`. ``action`` is one of
    ``promoted`` / ``noop`` / ``held`` / ``refused`` / ``dry_run``.

    ``reason_code`` is an optional MACHINE-readable companion to ``reason``:
    when the door refuses on installer-ownership policy it carries the matching
    :data:`agent_runtime.skill_publishability.PROMOTION_BLOCK_REASONS` code so a
    UI can branch on the cause instead of pattern-matching prose. ``None`` for
    outcomes whose ``reason`` is already the whole story (promoted / noop / held
    / the structural refusals classified in :func:`classify_promotion`).
    """

    skill: str
    action: str
    archived_previous_to: Path | None
    provenance_path: Path | None
    reason: str
    reason_code: str | None = None


# ── Path helpers ───────────────────────────────────────────────────────────


def _safe_token(value: str | None) -> str:
    """Sanitize a realm id / token into a filesystem-safe inbox subdir name.

    Byte-identical to ``agent_runtime.realm_sync._safe_token`` so an inbox
    written by the pull pipeline (C3) and read back here address the same dir.
    Idempotent for already-safe tokens.
    """

    text = "".join(
        ch if ch.isalnum() or ch in "_.-" else "_" for ch in str(value or "").strip()
    )
    return text.strip("._")[:120] or "item"


def realm_inbox_root() -> Path:
    """``shared/skills/.realm_inbox`` — resolver-invisible quarantine root."""

    return get_shared_skills_dir() / _REALM_INBOX_DIRNAME


def realm_inbox_dir(realm_token: str) -> Path:
    """``…/.realm_inbox/<token>`` for one realm. ``realm_token`` may be a raw
    realm id or an already-sanitized token; it is normalized via
    :func:`_safe_token`."""

    return realm_inbox_root() / _safe_token(realm_token)


def _provenance_root() -> Path:
    return get_shared_skills_dir() / _PROVENANCE_DIRNAME


def _provenance_path(skill: str) -> Path:
    return _provenance_root() / f"{skill.replace('/', '__')}.json"


def _archive_root() -> Path:
    return get_shared_skills_dir() / _ARCHIVE_DIRNAME


# ── Slug validation ────────────────────────────────────────────────────────


def _validate_slug(skill: str) -> str | None:
    """Return an error reason string when ``skill`` is not a safe canonical slug.

    Accepts at most one ``/`` (category form). Every component must match
    :data:`_SLUG_COMPONENT_RE`, must not be ``.``/``..`` and must not start with
    a dot (dot-leading components are how the excluded ``.realm_inbox`` /
    ``.provenance`` / ``.archive`` dirs are named — they can never be a promotion
    target), and must not resolve to a Windows reserved device name (``con``,
    ``nul``, ``com1`` … — including with an extension like ``con.md``). Traversal,
    absolute and drive-letter shapes are rejected as a side effect of the
    alphabet (no ``/`` inside a component, no ``\\``, no ``:``).
    """

    raw = str(skill or "").strip()
    if not raw:
        return "empty skill slug"
    if raw.startswith(("/", "\\")):
        return "absolute skill slug is not allowed"
    if ":" in raw:
        return "drive-letter / scheme in skill slug is not allowed"
    if "\\" in raw:
        return "backslash in skill slug is not allowed"
    parts = raw.split("/")
    if len(parts) > 2:
        return "multi-level (>1 category) skill nesting is not supported"
    for part in parts:
        if not part:
            return "empty slug component"
        if part in (".", ".."):
            return "traversal component in skill slug is not allowed"
        if part.startswith("."):
            return "dot-leading slug component is not allowed"
        if not _SLUG_COMPONENT_RE.match(part):
            return f"invalid slug component {part!r}"
        if is_windows_reserved_component(part):
            return f"reserved device name in skill slug component {part!r}"
    return None


def _canonical_dir_for(skill: str) -> Path:
    return get_shared_skills_dir().joinpath(*skill.split("/"))


def _package_hash(package_dir: Path) -> str:
    return skill_package_content_hash(package_dir, package_dir / "SKILL.md")


def _has_skill_md(package_dir: Path) -> bool:
    return package_dir.is_dir() and (package_dir / "SKILL.md").is_file()


# ── Classification (pure) ──────────────────────────────────────────────────


def classify_promotion(skill: str, source_dir: Path) -> PromotionPlan:
    """Classify a candidate promotion without touching the filesystem beyond
    reading hashes.

    Compares the source package hash against the canonical package hash (when a
    canonical copy exists). Does NOT consult profile packs — a profile-local
    duplicate is the *caller's* chosen source, not an authority.
    """

    slug = str(skill or "").strip()
    slug_error = _validate_slug(slug)
    source_dir = Path(source_dir) if source_dir is not None else None
    if slug_error is not None:
        return PromotionPlan(
            skill=slug,
            action="refuse_invalid",
            source_dir=source_dir,
            source_hash=None,
            canonical_dir=None,
            canonical_hash=None,
            reason=slug_error,
        )

    canonical_dir = _canonical_dir_for(slug)

    if source_dir is None or not _has_skill_md(source_dir):
        return PromotionPlan(
            skill=slug,
            action="refuse_invalid",
            source_dir=source_dir,
            source_hash=None,
            canonical_dir=canonical_dir,
            canonical_hash=None,
            reason="source package has no SKILL.md at its root",
        )

    source_hash = _package_hash(source_dir)

    # Symmetric occupancy guard — the canonical target and (for a categorized
    # slug) its parent must each be unoccupied-or-compatible before an adopt can
    # be safe. Refuse rather than ``promote_new`` when either slot is occupied by
    # something the promotion door cannot treat as a skill package:
    #
    #   F2 — categorized ``<cat>/<child>`` whose PARENT ``<cat>`` is an existing
    #        BARE skill package (has SKILL.md): writing the child INSIDE it would
    #        silently change the parent's content hash and inject a new resolvable
    #        skill with no gate. The parent may only be nothing or a pure category
    #        dir (a dir WITHOUT a SKILL.md).
    #   F1 — the target ``<slug>`` exists but is NOT a skill package (a dir
    #        without a SKILL.md — e.g. a category dir holding child skills — or a
    #        non-directory). ``execute_promotion`` would ``os.replace`` onto it and
    #        raise, aborting the whole pull. It may only be nothing (adopt) or a
    #        skill package (for the identical/divergent comparison below).
    parts = slug.split("/")
    if len(parts) == 2:
        parent_dir = get_shared_skills_dir() / parts[0]
        if parent_dir.exists() and not parent_dir.is_dir():
            return PromotionPlan(
                skill=slug,
                action="refuse_invalid",
                source_dir=source_dir,
                source_hash=source_hash,
                canonical_dir=canonical_dir,
                canonical_hash=None,
                reason=(
                    f"parent path {parent_dir} is occupied by a non-directory — "
                    "cannot host a categorized skill"
                ),
            )
        if _has_skill_md(parent_dir):
            return PromotionPlan(
                skill=slug,
                action="refuse_invalid",
                source_dir=source_dir,
                source_hash=source_hash,
                canonical_dir=canonical_dir,
                canonical_hash=None,
                reason=(
                    f"parent path {parent_dir} is an existing skill package — "
                    "refusing to write a categorized child inside a bare skill"
                ),
            )

    if canonical_dir.exists() and not _has_skill_md(canonical_dir):
        return PromotionPlan(
            skill=slug,
            action="refuse_invalid",
            source_dir=source_dir,
            source_hash=source_hash,
            canonical_dir=canonical_dir,
            canonical_hash=None,
            reason=(
                f"canonical path {canonical_dir} is occupied by a non-skill-package "
                "(directory without SKILL.md, or a file) — refusing to overwrite"
            ),
        )

    if not _has_skill_md(canonical_dir):
        return PromotionPlan(
            skill=slug,
            action="promote_new",
            source_dir=source_dir,
            source_hash=source_hash,
            canonical_dir=canonical_dir,
            canonical_hash=None,
            reason="no canonical copy — safe to adopt",
        )

    canonical_hash = _package_hash(canonical_dir)
    if canonical_hash == source_hash:
        return PromotionPlan(
            skill=slug,
            action="noop_identical",
            source_dir=source_dir,
            source_hash=source_hash,
            canonical_dir=canonical_dir,
            canonical_hash=canonical_hash,
            reason="content hashes match — converged",
        )

    return PromotionPlan(
        skill=slug,
        action="hold_divergent",
        source_dir=source_dir,
        source_hash=source_hash,
        canonical_dir=canonical_dir,
        canonical_hash=canonical_hash,
        reason=(
            "canonical differs from source — held for explicit resolution "
            f"(source={source_hash[:12]} canonical={canonical_hash[:12]})"
        ),
    )


# ── Guarded execution (atomic, never-delete) ───────────────────────────────


def _archive_dir(slug: str) -> Path:
    """Return a fresh, unique ``.archive/<UTC ts>/<slug-flattened>`` dir path.

    The parent is created; the leaf is guaranteed not to exist yet so a
    subsequent move/copy lands cleanly. Microsecond-stamped, with a numeric
    suffix as a last-resort collision breaker.
    """

    ts = now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    flat = slug.replace("/", "__")
    parent = _archive_root() / ts
    parent.mkdir(parents=True, exist_ok=True)
    dest = parent / flat
    counter = 1
    while dest.exists():
        dest = parent / f"{flat}-{counter}"
        counter += 1
    return dest


def _archive_package(package_dir: Path, slug: str) -> Path:
    """Move ``package_dir`` into the archive (never delete). Returns the dest."""

    dest = _archive_dir(slug)
    shutil.move(str(package_dir), str(dest))
    return dest


def _atomic_install(source_dir: Path, canonical_dir: Path) -> None:
    """Copy ``source_dir`` into ``canonical_dir`` atomically.

    The tree is copied into a temp dir under the excluded ``.archive`` root
    (same filesystem as the canonical location, and resolver-invisible while it
    exists), then ``os.replace``d into place. ``canonical_dir`` MUST NOT exist
    when this is called (promote_new never had one; adopt archives it first).
    """

    tmp_root = _archive_root()
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp = tmp_root / f".promote-tmp-{uuid.uuid4().hex}"
    try:
        shutil.copytree(source_dir, tmp)
        canonical_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, canonical_dir)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _normalize_source(source: dict | None) -> dict:
    """Keep only the recognized provenance-source keys."""

    source = source or {}
    kind = str(source.get("kind") or "unknown")
    out: dict[str, Any] = {"kind": kind}
    for key in ("realm_id", "profile", "path"):
        value = source.get(key)
        if value is not None:
            out[key] = str(value)
    return out


def _write_provenance(
    skill: str,
    *,
    content_hash: str,
    source: dict,
    previous_hash: str | None,
) -> Path:
    record: dict[str, Any] = {
        "skill": skill,
        "content_hash": content_hash,
        "source": _normalize_source(source),
        "promoted_at": now().astimezone(timezone.utc).isoformat(),
    }
    if previous_hash is not None:
        record["previous_hash"] = previous_hash
    path = _provenance_path(skill)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, record, sort_keys=True)
    return path


def execute_promotion(
    plan: PromotionPlan,
    *,
    source: dict,
    adopt_divergent: bool = False,
    dry_run: bool = False,
    move_source: bool = False,
) -> PromotionResult:
    """Execute a classified plan under the promotion guard.

    - ``promote_new`` → copy source → canonical; write provenance. ``move_source``
      then archives the (now-redundant) source — the collision guard for a
      profile-local duplicate.
    - ``hold_divergent`` + ``adopt_divergent`` → archive the current canonical to
      ``.archive/…`` (recording ``previous_hash``), then copy source → canonical.
      Without ``adopt_divergent`` → ``held``, no writes.
    - ``noop_identical`` → ``noop``; ``move_source`` archives the redundant source
      (the dedupe lane).
    - ``refuse_*`` → ``refused``, no writes.
    - an installer-owned source, or a target slug the hermes installer manages
      in some profile → ``refused`` with a typed ``reason_code`` (see
      :func:`agent_runtime.skill_publishability.promotion_refusal`); no writes,
      on the real run AND on a dry run.
    - ``dry_run`` on any actionable plan → ``dry_run``, no filesystem writes.

    All writes are atomic; nothing is ever deleted (displaced content is
    archived). ``archived_previous_to`` records the archive destination of
    whatever this call displaced — the previous canonical (adopt) if one was
    archived, otherwise the retired source (``move_source``).
    """

    action = plan.action

    if action in ("refuse_invalid", "refuse_ambiguous_source"):
        return PromotionResult(
            skill=plan.skill,
            action="refused",
            archived_previous_to=None,
            provenance_path=None,
            reason=plan.reason,
        )

    if action not in ("promote_new", "noop_identical", "hold_divergent"):
        # Defensive: an unknown action is treated as a refusal rather than a
        # silent write.
        return PromotionResult(
            skill=plan.skill,
            action="refused",
            archived_previous_to=None,
            provenance_path=None,
            reason=f"unknown plan action {action!r}",
        )

    # Installer-ownership policy — the LAST gate before any write path, and
    # deliberately BEFORE the dry-run short-circuit so a preview reports the
    # refusal it would actually get instead of promising a promotion the real
    # run would reject. ``noop_identical`` is exempt: it writes nothing (bar the
    # optional ``move_source`` archive of a redundant duplicate, which is the
    # collision-retiring dedupe lane and never touches the installer's copy —
    # that copy is byte-identical to canonical by definition of "identical").
    if action in ("promote_new", "hold_divergent"):
        from .skill_publishability import promotion_refusal

        refusal = promotion_refusal(plan.skill, plan.source_dir)
        if refusal is not None:
            return PromotionResult(
                skill=plan.skill,
                action="refused",
                archived_previous_to=None,
                provenance_path=None,
                reason=refusal.message,
                reason_code=refusal.code,
            )

    if dry_run:
        return PromotionResult(
            skill=plan.skill,
            action="dry_run",
            archived_previous_to=None,
            provenance_path=None,
            reason=f"dry_run: would {action} ({plan.reason})",
        )

    assert plan.source_dir is not None  # actionable plans always carry a source
    assert plan.canonical_dir is not None

    if action == "hold_divergent" and not adopt_divergent:
        return PromotionResult(
            skill=plan.skill,
            action="held",
            archived_previous_to=None,
            provenance_path=None,
            reason=plan.reason,
        )

    if action == "noop_identical":
        archived_to: Path | None = None
        if move_source:
            archived_to = _archive_package(plan.source_dir, plan.skill)
        return PromotionResult(
            skill=plan.skill,
            action="noop",
            archived_previous_to=archived_to,
            provenance_path=None,
            reason=(
                "converged; redundant source archived"
                if move_source
                else plan.reason
            ),
        )

    # promote_new, or hold_divergent with adopt_divergent=True.
    previous_hash: str | None = None
    archived_previous_to: Path | None = None
    if action == "hold_divergent":
        previous_hash = plan.canonical_hash
        archived_previous_to = _archive_package(plan.canonical_dir, plan.skill)

    # TOCTOU / occupancy guard. The canonical slot must be empty now:
    # ``promote_new`` was classified against no canonical copy, and an adopt just
    # archived the previous package away. If something the plan did not
    # anticipate occupies it — a concurrent writer, or a non-package dir (e.g. a
    # category dir holding child skills) that slipped past classification — refuse
    # instead of ``os.replace``-ing onto it, which raises and, in the pull lane,
    # would abort the entire ``pull_realm_sync``. Never overwrite blind.
    if plan.canonical_dir.exists():
        return PromotionResult(
            skill=plan.skill,
            action="refused",
            archived_previous_to=archived_previous_to,
            provenance_path=None,
            reason=(
                f"canonical path {plan.canonical_dir} is unexpectedly occupied at "
                "install time — refusing to overwrite (canonical left untouched)"
            ),
        )

    try:
        _atomic_install(plan.source_dir, plan.canonical_dir)
    except OSError as exc:
        return PromotionResult(
            skill=plan.skill,
            action="refused",
            archived_previous_to=archived_previous_to,
            provenance_path=None,
            reason=f"atomic install failed ({exc}) — canonical left untouched",
        )
    provenance_path = _write_provenance(
        plan.skill,
        content_hash=plan.source_hash or _package_hash(plan.canonical_dir),
        source=source,
        previous_hash=previous_hash,
    )

    # A profile-local promotion retires its duplicate (the collision guard).
    # Promotion from an inbox never moves (the inbox is a realm mirror).
    if move_source and plan.source_dir.exists():
        source_archive = _archive_package(plan.source_dir, plan.skill)
        if archived_previous_to is None:
            archived_previous_to = source_archive

    reason = (
        "adopted over divergent canonical (previous archived)"
        if action == "hold_divergent"
        else "promoted new canonical skill"
    )
    return PromotionResult(
        skill=plan.skill,
        action="promoted",
        archived_previous_to=archived_previous_to,
        provenance_path=provenance_path,
        reason=reason,
    )


# ── Inbox enumeration + provenance read ────────────────────────────────────


def iter_skill_packages(root: Path):
    """Yield ``(slug, package_dir)`` for skill packages directly under ``root``.

    A top-level dir containing ``SKILL.md`` is a bare package (slug = dir name).
    A top-level dir WITHOUT ``SKILL.md`` is treated as a category: each of its
    immediate child dirs that contains ``SKILL.md`` publishes as ``<cat>/<name>``
    (one level only). Dot-prefixed dirs, excluded dirs, and skill support dirs
    are skipped.
    """

    if not root.is_dir():
        return
    for top in sorted(root.iterdir(), key=lambda p: p.name):
        if not top.is_dir():
            continue
        name = top.name
        if name.startswith(".") or name in EXCLUDED_SKILL_DIRS:
            continue
        if _has_skill_md(top):
            yield name, top
            continue
        # Category form: one level deep.
        for child in sorted(top.iterdir(), key=lambda p: p.name):
            if not child.is_dir():
                continue
            cname = child.name
            if cname.startswith(".") or cname in EXCLUDED_SKILL_DIRS:
                continue
            if cname in SKILL_SUPPORT_DIRS:
                continue
            if _has_skill_md(child):
                yield f"{name}/{cname}", child


# Historical private name — ``agent_runtime.realm_sync`` imports it. Kept as an
# alias so the public rename does not require an edit to that file (another
# agent works there concurrently).
_iter_packages = iter_skill_packages


def list_inbox_packages(realm_token: str | None = None) -> list[dict]:
    """List quarantined inbox packages and how each would reconcile.

    Returns one row per ``(realm, skill)`` with the classification against the
    current canonical root::

        {"skill", "realm", "action", "source_hash", "canonical_hash", "source_dir",
         "promotion_block_reason", "promotion_block_detail"}

    ``action`` is the :func:`classify_promotion` classification
    (``promote_new`` / ``noop_identical`` / ``hold_divergent`` / ``refuse_invalid``).
    ``promotion_block_reason`` is the installer-ownership policy verdict the
    guarded door would apply on top of it (``None`` when the promotion may
    proceed) — without it a row could advertise ``promote_new`` for a package
    the very next write would refuse.
    When ``realm_token`` is given only that realm's inbox is scanned.
    """

    from .skill_publishability import promotion_refusal

    root = realm_inbox_root()
    rows: list[dict] = []
    if not root.is_dir():
        return rows

    if realm_token is not None:
        wanted = _safe_token(realm_token)
        realm_dirs = [root / wanted] if (root / wanted).is_dir() else []
    else:
        realm_dirs = [
            child
            for child in sorted(root.iterdir(), key=lambda p: p.name)
            if child.is_dir() and not child.name.startswith(".")
        ]

    for realm_dir in realm_dirs:
        token = realm_dir.name
        for slug, package_dir in iter_skill_packages(realm_dir):
            plan = classify_promotion(slug, package_dir)
            refusal = (
                promotion_refusal(slug, package_dir)
                if plan.action in ("promote_new", "hold_divergent")
                else None
            )
            rows.append(
                {
                    "skill": slug,
                    "realm": token,
                    "action": plan.action,
                    "source_hash": plan.source_hash,
                    "canonical_hash": plan.canonical_hash,
                    "source_dir": package_dir,
                    "promotion_block_reason": refusal.code if refusal else None,
                    "promotion_block_detail": refusal.message if refusal else None,
                }
            )
    return rows


# S54 removed ``promotion_provenance``: a provenance read-back accessor whose
# only callers were its own tests.

