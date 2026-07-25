"""Typed, machine-readable snapshot of the shared skills substrate — the
contract the Launcher's Skills console consumes instead of scraping the human
``hermes skills list`` table (which has no ``--json`` lane).

Mirrors the discipline of :func:`hermes_cli.harness.build_provider_visibility`:
emit *structure*, not prose, and reuse the existing engine authorities so there
is never a second source of truth. Specifically it reuses:

* :func:`hermes_constants.get_shared_skills_dir` — the one canonical skills root
  every persona references (see the shared-skills-root work);
* the same package-walk / exclusion rules the realm-sync publisher applies
  (``EXCLUDED_SKILL_DIRS``), so what the console shows == what actually syncs;
* :func:`agent_runtime.config.ensure_persisted_personas` for the persona grant
  matrix (``persona.skills`` is the same declared set Mission Control renders
  per agent);
* :func:`agent_runtime.realm_sync.read_realm_sync_sidecar` for per-realm publish
  / drift state — a pure file read, the same cheap surface the snapshot uses
  (zero git calls here).

Skills publish at **realm** granularity (the shared catalog is realm-global,
shared by every workspace in a realm); this payload reflects that — realm rows,
never per-workspace skill divergence.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .skill_publishability import REASON_SHARED_ROOT

SCHEMA = "hermes.skills_inventory/v1"


def _skill_metadata(skill_dir: Path) -> tuple[str, str]:
    """``(title, description)`` from the skill's ``SKILL.md`` frontmatter.

    Falls back to the directory slug for the title and an empty description so
    the console never renders a bare slug or crashes on a malformed manifest.
    """
    slug = skill_dir.name
    try:
        raw = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return slug, ""
    try:
        from agent.skill_utils import parse_frontmatter

        frontmatter, _ = parse_frontmatter(raw)
    except Exception:
        return slug, ""
    title = str(frontmatter.get("name") or slug).strip() or slug
    description = str(frontmatter.get("description") or "").strip()
    return title, description


def _skill_files(skill_dir: Path) -> list[Path]:
    """Every publishable file in a skill package — same prune rules the realm
    publisher uses (dotfiles + ``EXCLUDED_SKILL_DIRS`` dropped) so the count and
    hash reported here match exactly what a publish would ship."""
    from agent.skill_utils import EXCLUDED_SKILL_DIRS

    files: list[Path] = []
    for source in sorted(skill_dir.rglob("*")):
        if not source.is_file():
            continue
        rel_parts = source.relative_to(skill_dir).parts
        if any(part.startswith(".") or part in EXCLUDED_SKILL_DIRS for part in rel_parts):
            continue
        files.append(source)
    return files


def _content_hash(skill_dir: Path, files: list[Path]) -> str:
    """Stable sha256 over the package's (relative path, bytes) pairs. Lets the
    Launcher detect "this catalog changed" and compare against realm drift
    without shipping file contents."""
    digest = hashlib.sha256()
    for source in files:
        rel = "/".join(source.relative_to(skill_dir).parts)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        try:
            digest.update(source.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\x00")
    return digest.hexdigest()


def build_shared_catalog() -> tuple[Path | None, bool, list[dict[str, Any]]]:
    """Walk the canonical shared skills root. Returns ``(root, exists, rows)``.

    A directory is a skill only when it carries a ``SKILL.md`` manifest; dotdirs
    (``.archive`` / ``.hub`` / …) and manifest-less housekeeping folders are
    skipped — the same test the realm publisher applies.
    """
    from hermes_constants import get_shared_skills_dir

    root = get_shared_skills_dir()
    if root is None or not root.exists():
        return root, False, []

    catalog: list[dict[str, Any]] = []
    for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        slug = skill_dir.name
        if slug.startswith(".") or not (skill_dir / "SKILL.md").is_file():
            continue
        files = _skill_files(skill_dir)
        title, description = _skill_metadata(skill_dir)
        catalog.append(
            {
                "slug": slug,
                "title": title,
                "description": description,
                "multi_file": len(files) > 1,
                "file_count": len(files),
                "content_hash": _content_hash(skill_dir, files),
                # Publishability is a CONSTANT for this walk — every row here is
                # by construction in the shared root, the sole source realm sync
                # publishes from. Stated explicitly rather than left implicit in
                # "we only walked the shared root", so the field carries the same
                # meaning here as on the ``resolvable_skills`` rows below (where
                # it is false for most packages) and a consumer never infers it.
                "source_kind": "shared_core",
                "publishable": True,
                "publishable_reason": REASON_SHARED_ROOT,
                # Populated in build_skills_inventory once personas are known.
                "shadowed_by": [],
            }
        )
    return root, True, catalog


def _persona_local_skill_slugs(persona) -> set[str]:
    """Skill slugs living in a persona's *own* profile skills dir (which shadow
    the shared same-name skill in discovery order). Best-effort: any resolution
    failure degrades to an empty set rather than breaking the inventory."""
    profile = getattr(persona, "hermes_profile", None)
    if not profile:
        return set()
    try:
        from hermes_cli.profiles import get_profile_dir

        skills_dir = get_profile_dir(profile) / "skills"
    except Exception:
        return set()
    if not skills_dir.exists():
        return set()
    slugs: set[str] = set()
    try:
        for child in skills_dir.iterdir():
            if child.is_dir() and not child.name.startswith(".") and (child / "SKILL.md").is_file():
                slugs.add(child.name)
    except OSError:
        return set()
    return slugs


def build_realm_publish_states() -> list[dict[str, Any]]:
    """Per-realm skill publish / drift state from the sync sidecar (pure read).

    ``sync_state`` is ``None`` when the realm has never been checked (the
    Launcher renders that as "not checked", never as in-sync).
    """
    from agent_runtime.realm_sync import read_realm_sync_sidecar
    from agent_runtime.store import RealmStore

    rows: list[dict[str, Any]] = []
    for realm in RealmStore().list_all():
        sidecar = read_realm_sync_sidecar(realm.id) or {}
        rows.append(
            {
                "realm_id": realm.id,
                "name": getattr(realm, "name", realm.id) or realm.id,
                "server_bound": bool(getattr(realm, "server_id", None)),
                "sync_state": sidecar.get("state"),
                "skills_drift": list(sidecar.get("skills_drift") or []),
                # Read STRAIGHT from the store realm (fresher than the sidecar,
                # and already loaded) — the picker mode + current selection.
                "skill_publish_mode": getattr(realm, "skill_publish_mode", "all") or "all",
                "skill_selection": sorted(getattr(realm, "skill_selection", None) or []),
                "agent_publish_mode": getattr(realm, "agent_publish_mode", "workspace") or "workspace",
                "agent_selection": sorted(getattr(realm, "agent_selection", None) or []),
                "last_publish": sidecar.get("last_publish"),
                "last_pull": sidecar.get("last_pull"),
                "checked_at": sidecar.get("checked_at"),
            }
        )
    rows.sort(key=lambda row: row["realm_id"])
    return rows


def build_skills_inventory(cfg=None) -> dict[str, Any]:
    """Assemble the full ``skills_inventory/v1`` payload: shared catalog,
    per-persona grant matrix, per-realm publish/drift state, and the
    machine-wide publishability sweep.

    ``resolvable_skills`` is the honest answer to "which skills exist, and can
    they reach a realm": one typed row per resolvable package across ALL three
    resolver tiers (``profile_local`` / ``shared_core`` / ``external``), each
    carrying ``publishable`` + a typed ``publishable_reason`` and, when it
    cannot be promoted, a typed ``promotion_block_reason``. The pre-existing
    ``skills`` list is unchanged in meaning (shared root only) — a consumer that
    reads only ``skills`` sees exactly what it saw before, additively enriched.
    """
    from agent_runtime.config import ensure_persisted_personas

    from .skill_publishability import build_publishability_rows

    root, exists, catalog = build_shared_catalog()
    catalog_slugs = {entry["slug"] for entry in catalog}

    persona_rows: list[dict[str, Any]] = []
    shadowed_by: dict[str, set[str]] = {}
    for persona in ensure_persisted_personas(cfg):
        declared = sorted({s.strip() for s in (getattr(persona, "skills", None) or []) if s and s.strip()})
        local = _persona_local_skill_slugs(persona)
        for slug in local & catalog_slugs:
            shadowed_by.setdefault(slug, set()).add(persona.id)
        persona_rows.append(
            {
                "id": persona.id,
                "display_name": getattr(persona, "display_name", persona.id) or persona.id,
                "role": getattr(persona, "role", None),
                "skills": declared,
                "local_skills": sorted(local),
            }
        )
    persona_rows.sort(key=lambda row: row["id"])

    for entry in catalog:
        entry["shadowed_by"] = sorted(shadowed_by.get(entry["slug"], set()))

    resolvable = build_publishability_rows()
    return {
        "schema": SCHEMA,
        "shared_root": str(root) if root is not None else None,
        "shared_root_exists": exists,
        "skills": catalog,
        "personas": persona_rows,
        "realms": build_realm_publish_states(),
        "resolvable_skills": resolvable,
        # Counters so a consumer can render "N of M skills cannot reach a realm"
        # without re-deriving the rule, and so an empty/failed sweep is visibly
        # empty rather than silently indistinguishable from "all publishable".
        "publishability_summary": {
            "total": len(resolvable),
            "publishable": sum(1 for row in resolvable if row["publishable"]),
            "unpublishable": sum(1 for row in resolvable if not row["publishable"]),
            "promotable": sum(1 for row in resolvable if row["promotable"]),
            "blocked_by_reason": _count_by(resolvable, "promotion_block_reason"),
        },
    }


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    """``{value: count}`` over a row key, skipping ``None`` — the typed tally
    that lets a UI name every offender class instead of showing a bare total."""
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items()))
