"""Can a resolvable skill actually reach a realm — and may it be promoted?

The resolver (:func:`agent.skill_utils.get_all_skills_dirs`) walks three tiers,
already typed by :func:`agent.skill_utils.skill_source_kind`:

======================  ==========================================  ============
``source_kind``         root                                        publishes?
======================  ==========================================  ============
``profile_local``       ``<profile_home>/skills`` (index 0, WINS)   **no**
``shared_core``         :func:`hermes_constants.get_shared_skills_dir`  **yes**
``external``            ``skills.external_dirs`` from config.yaml   **no**
======================  ==========================================  ============

``agent_runtime.realm_sync._skill_artifacts`` reads ONLY the shared root, so a
skill can be fully resolvable to a running agent and still be *structurally
incapable* of reaching a realm. Before this module nothing in the product said
so: a profile-local skill simply reported an empty ``realm_sync`` list, which
reads as "no realms" rather than "cannot travel". This module is the ONE place
that answers both questions with a typed reason, so every surface (the
``skills_inventory/v1`` contract, the per-persona prompt-observability skills
catalog, the ``hermes harness skills`` verbs, and the promotion door itself)
agrees.

**The shared root stays the SOLE publish source** (operator ruling, 2026-07-25).
Publishing ``profile_local`` was considered and rejected: the installer already
distributes the bundled catalog to every member, so a realm republishing it
creates a second authority for the same artifact, turns every hermes version
bump into HELD-divergent noise on every member's pull, and re-creates the
multi-root collision class this repo already paid to retire. The answer for a
profile-local skill is therefore *promotion into the shared root* through the
existing guarded door (:mod:`agent_runtime.skill_promotion`), never a second
publish source.

Installer-owned packages are the exception that must NOT be offered for
promotion. ``profile_local`` on this machine is overwhelmingly the **bundled
hermes catalog** (``apple``, ``data-science``, ``devops``, ``gaming``, ``gifs``,
``red-teaming``, ``yuanbao`` …) which ``tools.skills_sync`` materializes into
every profile and tracks in ``<profile_home>/skills/.bundled_manifest``.
Promoting one of those:

1. **forks the installer's catalog** — the shared root would carry a second copy
   of an artifact the installer already ships to every member, which goes stale
   at the next hermes bump; and
2. **guarantees a resolver collision** — every profile keeps its installer copy
   at resolver index 0 while the promoted copy sits in the shared root, so
   ``resolve_skill`` sees two candidates and refuses to pick a winner
   (``skill_collision`` — the exact readiness failure the shared-skills-root
   migration was paid to retire).

Both refusals are enforced at the write paths of the guarded door (see
:func:`promotion_refusal`, called from
:func:`agent_runtime.skill_promotion.execute_promotion`) and reported ahead of
time on every inventory row, so the affordance is suppressed *and* the action is
refused — never one without the other.

Bundled-state detection reuses ``tools.skills_sync``'s own primitives
(``_dir_hash`` for the manifest's MD5 contract, ``_read_skill_name`` for the
frontmatter-name key it records) rather than re-deriving them: a private copy of
a hash algorithm that must agree with a manifest written elsewhere is a silent
mis-classification waiting to happen. If those primitives ever move, the import
fails and every profile package classifies as
:data:`BLOCK_INSTALLER_STATE_UNKNOWN` — fail-closed and loudly typed, never
silently "promotable".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

BUNDLED_MANIFEST_FILENAME = ".bundled_manifest"
SKILLS_DIRNAME = "skills"

# ── Publishability reason codes (why a skill can / cannot reach a realm) ────

REASON_SHARED_ROOT = "shared_root"
REASON_PROFILE_LOCAL_ONLY = "profile_local_only"
REASON_EXTERNAL_DIR_ONLY = "external_dir_only"
REASON_UNKNOWN_ROOT = "unknown_root"

# ── Promotion-block reason codes (why the guarded door will not admit it) ───

BLOCK_ALREADY_CANONICAL = "already_canonical"
BLOCK_INSTALLER_OWNED_PRISTINE = "installer_owned_pristine"
BLOCK_INSTALLER_OWNED_EDITED = "installer_owned_edited"
BLOCK_INSTALLER_STATE_UNKNOWN = "installer_owned_state_unknown"
BLOCK_INSTALLER_SLUG_RESERVED = "installer_catalog_slug_reserved"

PROMOTION_BLOCK_REASONS = frozenset(
    {
        BLOCK_ALREADY_CANONICAL,
        BLOCK_INSTALLER_OWNED_PRISTINE,
        BLOCK_INSTALLER_OWNED_EDITED,
        BLOCK_INSTALLER_STATE_UNKNOWN,
        BLOCK_INSTALLER_SLUG_RESERVED,
    }
)

# The escape hatch we name in every installer-owned refusal. Deliberate friction
# with an honest route, not a wall: an operator who genuinely wants their edits
# shared authors the work as a package of their OWN (a distinct slug, outside a
# profile's installer-managed tree) and promotes that. The promoted artifact is
# then unambiguously theirs — the installer keeps owning its catalog, members'
# updates keep flowing, and the provenance sidecar records the origin.
_ESCAPE_HATCH = (
    "To share your edits, copy the package to a distinct slug outside any "
    "profile's installer-managed skills tree and promote that "
    "(`hermes harness skills promote <new-slug> --from-path <dir>`) — the "
    "installer keeps owning its catalog and your copy travels as your own skill."
)


# ── Typed records ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromotionRefusal:
    """Why the promotion door refuses a package. ``code`` is one of
    :data:`PROMOTION_BLOCK_REASONS`; ``message`` is the operator-facing text
    (never a bare bool forcing the UI to invent a generic string)."""

    code: str
    message: str
    manifest_name: str | None = None
    profile: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "manifest_name": self.manifest_name,
            "profile": self.profile,
        }


@dataclass(frozen=True)
class SkillPublishability:
    """One resolvable skill package and whether it can travel to a realm.

    ``root_label`` is a *label* (``shared`` / ``profile:<name>`` /
    ``external:<dirname>``), never an absolute path — the same discipline
    :func:`agent.skill_utils.skill_source_kind` follows so a runtime root is
    never leaked on the wire.
    """

    skill: str
    source_kind: str
    root_label: str
    publishable: bool
    publishable_reason: str
    publishable_detail: str
    promotable: bool
    promotion_block_reason: str | None
    promotion_block_detail: str | None
    installer_owned: bool
    installer_edited: bool
    content_hash: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "skill": self.skill,
            "source_kind": self.source_kind,
            "root_label": self.root_label,
            "publishable": self.publishable,
            "publishable_reason": self.publishable_reason,
            "publishable_detail": self.publishable_detail,
            "promotable": self.promotable,
            "promotion_block_reason": self.promotion_block_reason,
            "promotion_block_detail": self.promotion_block_detail,
            "installer_owned": self.installer_owned,
            "installer_edited": self.installer_edited,
            "content_hash": self.content_hash,
        }


# ── tools.skills_sync primitives (single source of truth, fail-closed) ──────


def _sync_primitives():
    """Return ``(_dir_hash, _read_skill_name)`` from :mod:`tools.skills_sync`.

    Imported lazily and by name so the manifest's hash contract and its
    frontmatter-name key can never drift from a private copy here. Returns
    ``(None, None)`` when unavailable — callers then classify the package as
    :data:`BLOCK_INSTALLER_STATE_UNKNOWN` (fail-closed) instead of guessing.

    Note ``tools.skills_sync`` freezes ``HERMES_HOME`` into module-level
    constants at import time; only these two *pure* helpers are used here, never
    those constants, so a per-profile / per-test home is honoured.
    """

    try:
        from tools.skills_sync import _dir_hash, _read_skill_name

        return _dir_hash, _read_skill_name
    except Exception:  # noqa: BLE001 — any import failure means "unknown"
        logger.warning(
            "tools.skills_sync bundled-manifest primitives unavailable; "
            "installer-owned skill packages will classify as "
            "%s and stay unpromotable (fail-closed)",
            BLOCK_INSTALLER_STATE_UNKNOWN,
        )
        return None, None


# ── Caches (identity + mtime + size keyed, same contract as _RAW_CONFIG_CACHE) ──

_MANIFEST_CACHE: dict[tuple[str, int, int], dict[str, str]] = {}
_DIR_HASH_CACHE: dict[tuple[Any, ...], str] = {}
_CACHE_MAX = 2048


def cache_clear() -> None:
    """Test hook — drop the manifest / directory-hash caches."""

    _MANIFEST_CACHE.clear()
    _DIR_HASH_CACHE.clear()


def read_bundled_manifest(manifest_path: Path) -> dict[str, str]:
    """Parse a ``.bundled_manifest`` into ``{skill_name: origin_hash}``.

    Mirrors ``tools.skills_sync._read_manifest`` (v2 ``name:hash`` lines, v1
    bare names → empty hash) but takes an explicit path: the upstream reader is
    hard-wired to the *current* profile's manifest and cannot read another
    profile's. Cached on ``(path, mtime_ns, size)``.
    """

    try:
        stat = manifest_path.stat()
        key: tuple[str, int, int] | None = (
            str(manifest_path),
            stat.st_mtime_ns,
            stat.st_size,
        )
    except OSError:
        return {}
    cached = _MANIFEST_CACHE.get(key)
    if cached is not None:
        return cached

    entries: dict[str, str] = {}
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            name, _, origin = line.partition(":")
            entries[name.strip()] = origin.strip()
        else:
            # v1 format: no recorded origin hash → pristine/edited is unknowable.
            entries[line] = ""
    if len(_MANIFEST_CACHE) >= _CACHE_MAX:
        _MANIFEST_CACHE.clear()
    _MANIFEST_CACHE[key] = entries
    return entries


def _cached_dir_hash(package_dir: Path) -> str | None:
    """``tools.skills_sync._dir_hash`` with an mtime+size invalidation cache.

    The digest is byte-identical to the uncached call; the cache only skips
    re-reading files whose ``(relpath, mtime_ns, size)`` stamp is unchanged —
    the same invalidation contract as
    ``agent.skill_utils._CONTENT_HASH_CACHE``. Returns ``None`` when the
    upstream primitive is unavailable.
    """

    dir_hash, _ = _sync_primitives()
    if dir_hash is None:
        return None
    stamps: list[tuple[str, int | None, int | None]] = []
    try:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = "/".join(path.relative_to(package_dir).parts)
            try:
                st = path.stat()
                stamps.append((rel, st.st_mtime_ns, st.st_size))
            except OSError:
                stamps.append((rel, None, None))
    except OSError:
        return None
    key = (str(package_dir), tuple(stamps))
    cached = _DIR_HASH_CACHE.get(key)
    if cached is not None:
        return cached
    value = dir_hash(package_dir)
    if len(_DIR_HASH_CACHE) >= _CACHE_MAX:
        _DIR_HASH_CACHE.clear()
    _DIR_HASH_CACHE[key] = value
    return value


def package_manifest_name(package_dir: Path) -> str:
    """The name ``tools.skills_sync`` records for this package in the manifest.

    The manifest is keyed on the SKILL.md frontmatter ``name`` (falling back to
    the directory name) — exactly what ``_discover_bundled_skills`` records — so
    a package under a category dir (``skills/mlops/axolotl``) is tracked as
    ``axolotl``, not by its path.
    """

    _, read_name = _sync_primitives()
    if read_name is None:
        return package_dir.name
    try:
        return read_name(package_dir / "SKILL.md", package_dir.name)
    except Exception:  # noqa: BLE001 — a malformed manifest degrades to the slug
        return package_dir.name


# ── Profile skills roots ───────────────────────────────────────────────────


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path.expanduser().absolute()


def profile_skills_roots() -> list[Path]:
    """Every ``<profile_home>/skills`` root on this machine.

    The active profile's root plus the default profile's and every
    ``<root>/profiles/<name>/skills``. Used only to build the installer-owned
    *name* union (:func:`installer_catalog_names`) — no hashing.
    """

    from hermes_constants import get_default_hermes_root, get_skills_dir

    roots: list[Path] = []
    seen: set[Path] = set()

    def add(candidate: Path) -> None:
        key = _resolved(candidate)
        if key in seen:
            return
        seen.add(key)
        roots.append(candidate)

    try:
        add(get_skills_dir())
    except Exception:  # noqa: BLE001 — a broken home must not break reporting
        pass
    try:
        root = get_default_hermes_root()
    except Exception:  # noqa: BLE001
        return roots
    add(root / SKILLS_DIRNAME)
    profiles_root = root / "profiles"
    try:
        children = sorted(profiles_root.iterdir(), key=lambda p: p.name)
    except OSError:
        return roots
    for child in children:
        if child.is_dir() and not child.name.startswith("."):
            add(child / SKILLS_DIRNAME)
    return roots


def profile_label_for_skills_root(skills_root: Path) -> str:
    """``default`` / ``<profile-name>`` for a ``<profile_home>/skills`` root."""

    home = Path(skills_root).parent
    name = home.name
    if home.parent.name == "profiles":
        return name
    return "default"


def owning_profile_skills_root(path: Path) -> Path | None:
    """The installer-managed ``<profile_home>/skills`` root containing ``path``.

    A root qualifies only when it is named ``skills`` AND carries a
    ``.bundled_manifest`` — the marker ``tools.skills_sync`` writes when it
    materializes the bundled catalog there. The canonical shared root is
    explicitly excluded (it is never installer-managed), which is also why a
    realm-inbox source (``shared/skills/.realm_inbox/…``) never matches.
    Returns ``None`` when nothing above ``path`` is installer-managed.
    """

    from hermes_constants import get_shared_skills_dir

    try:
        shared = _resolved(get_shared_skills_dir())
    except Exception:  # noqa: BLE001
        shared = None
    candidate = _resolved(Path(path))
    for parent in [candidate, *candidate.parents]:
        if parent.name != SKILLS_DIRNAME:
            continue
        if shared is not None and parent == shared:
            return None
        if (parent / BUNDLED_MANIFEST_FILENAME).is_file():
            return parent
    return None


def installer_catalog_names() -> frozenset[str]:
    """Union of every installer-managed skill name across all profiles.

    Names only (manifest keys) — no hashing, so this stays cheap enough to
    consult on the realm-pull write path. A promotion whose target slug lands on
    one of these names is refused: the promoted copy would sit in the shared
    root while every profile keeps the installer's copy at resolver index 0,
    which is a guaranteed ``skill_collision``.
    """

    names: set[str] = set()
    for skills_root in profile_skills_roots():
        manifest = skills_root / BUNDLED_MANIFEST_FILENAME
        if not manifest.is_file():
            continue
        names.update(name for name in read_bundled_manifest(manifest) if name)
    return frozenset(names)


# ── Installer ownership of one package ─────────────────────────────────────


def classify_installer_ownership(package_dir: Path) -> PromotionRefusal | None:
    """Classify ``package_dir`` as installer-owned, or ``None`` when it is not.

    Pristine vs edited is decided by comparing the package's current
    ``_dir_hash`` against the origin hash the manifest recorded at the last
    sync — the SAME test ``tools.skills_sync`` itself uses to decide what an
    update may overwrite. Both are refusals; they are distinct codes because the
    remediation differs: a pristine copy is a pure duplicate of what the
    installer already ships (nothing is lost by refusing), while an edited copy
    carries real operator work that deserves the named escape hatch.
    """

    package_dir = Path(package_dir)
    skills_root = owning_profile_skills_root(package_dir)
    if skills_root is None:
        return None
    manifest = read_bundled_manifest(skills_root / BUNDLED_MANIFEST_FILENAME)
    if not manifest:
        return None
    name = package_manifest_name(package_dir)
    if name not in manifest:
        return None

    profile = profile_label_for_skills_root(skills_root)
    origin_hash = manifest.get(name) or ""
    current_hash = _cached_dir_hash(package_dir)

    if current_hash is None or not origin_hash:
        detail = (
            "no recorded origin hash"
            if current_hash is not None
            else "the bundled-manifest hash primitive is unavailable"
        )
        return PromotionRefusal(
            code=BLOCK_INSTALLER_STATE_UNKNOWN,
            message=(
                f"{name!r} is tracked by profile {profile!r}'s bundled manifest but "
                f"its installer state cannot be determined ({detail}). Refusing to "
                "promote rather than risk forking the installer's catalog. "
                + _ESCAPE_HATCH
            ),
            manifest_name=name,
            profile=profile,
        )

    if current_hash == origin_hash:
        return PromotionRefusal(
            code=BLOCK_INSTALLER_OWNED_PRISTINE,
            message=(
                f"{name!r} is an unmodified installer-owned skill (bundled with "
                f"hermes, materialized into profile {profile!r}). Promoting it "
                "would fork the installer's catalog — every member already "
                "receives this package from the installer, and the promoted copy "
                "would collide with theirs at resolver index 0 and go stale at "
                "the next hermes upgrade."
            ),
            manifest_name=name,
            profile=profile,
        )

    return PromotionRefusal(
        code=BLOCK_INSTALLER_OWNED_EDITED,
        message=(
            f"{name!r} is an installer-owned skill (bundled with hermes) that you "
            f"have edited locally in profile {profile!r}. Promoting it under the "
            "installer's own name would fork the catalog and guarantee a "
            "skill_collision on every machine that still receives the installer's "
            "copy. " + _ESCAPE_HATCH
        ),
        manifest_name=name,
        profile=profile,
    )


def promotion_refusal(skill: str, source_dir: Path | None) -> PromotionRefusal | None:
    """The one policy seam the guarded door consults before any write.

    Returns a typed refusal, or ``None`` when the promotion may proceed. Two
    independent rules, both about the installer's catalog:

    1. **Source identity** — the source package is installer-owned
       (:func:`classify_installer_ownership`). Refused whatever the target slug:
       a pristine copy is a pure duplicate, an edited one must travel under its
       own identity (see :data:`_ESCAPE_HATCH`).
    2. **Target identity** — the target slug (bare name, or the leaf of a
       ``<category>/<name>`` slug) is a name the installer manages in some
       profile. Refused whatever the source: the shared-root copy would collide
       with every profile's installer copy.

    Rule 2 also covers realm pulls: a realm publishing a package named after a
    bundled skill would otherwise auto-promote into the shared root and break
    resolution for every persona. The pull isolates refusals per package
    (``SkillSyncSummary.refused``), so one such package can never abort a pull.
    """

    if source_dir is not None:
        owned = classify_installer_ownership(Path(source_dir))
        if owned is not None:
            return owned

    slug = str(skill or "").strip()
    if not slug:
        return None
    parts = [part for part in slug.split("/") if part]
    candidates = {slug, parts[-1]} if parts else {slug}
    reserved = installer_catalog_names()
    hit = sorted(candidates & reserved)
    if not hit:
        return None
    return PromotionRefusal(
        code=BLOCK_INSTALLER_SLUG_RESERVED,
        message=(
            f"{hit[0]!r} is a skill name the hermes installer manages in this "
            "machine's profiles. A shared-root copy under that name would collide "
            "with the installer's copy at resolver index 0 on every profile "
            "(skill_collision) and go stale at the next upgrade. Choose a "
            "distinct slug."
        ),
        manifest_name=hit[0],
        profile=None,
    )


# ── Publishability of one package ──────────────────────────────────────────


def _root_label(root: Path, source_kind: str) -> str:
    if source_kind == "shared_core":
        return "shared"
    if source_kind == "profile_local":
        return f"profile:{profile_label_for_skills_root(root)}"
    return f"external:{Path(root).name}"


_PUBLISHABLE_DETAIL = {
    REASON_SHARED_ROOT: (
        "Lives in the canonical shared skills root — the one source realm sync "
        "publishes from."
    ),
    REASON_PROFILE_LOCAL_ONLY: (
        "Lives only in a profile's own skills/ root. Realm publish reads the "
        "shared root exclusively, so this package cannot reach a realm as it "
        "stands — promote it into the shared root to make it travel."
    ),
    REASON_EXTERNAL_DIR_ONLY: (
        "Lives only in a configured skills.external_dirs directory. Realm "
        "publish reads the shared root exclusively, so this package cannot "
        "reach a realm as it stands — promote it into the shared root to make "
        "it travel."
    ),
    REASON_UNKNOWN_ROOT: (
        "Resolved from a root the publish lane does not recognize; treat as "
        "unable to reach a realm until it is promoted into the shared root."
    ),
}

_REASON_FOR_SOURCE_KIND = {
    "shared_core": REASON_SHARED_ROOT,
    "profile_local": REASON_PROFILE_LOCAL_ONLY,
    "external": REASON_EXTERNAL_DIR_ONLY,
}


def classify_publishability(
    skill: str,
    package_dir: Path,
    root: Path,
    *,
    source_kind: str | None = None,
    content_hash: str | None = None,
) -> SkillPublishability:
    """Full typed publishability + promotability for one resolvable package."""

    from agent.skill_utils import skill_source_kind

    if source_kind is None:
        try:
            source_kind = skill_source_kind(root)
        except Exception:  # noqa: BLE001
            source_kind = "external"
    reason = _REASON_FOR_SOURCE_KIND.get(source_kind, REASON_UNKNOWN_ROOT)
    publishable = reason == REASON_SHARED_ROOT

    if publishable:
        refusal = PromotionRefusal(
            code=BLOCK_ALREADY_CANONICAL,
            message="Already canonical in the shared root — nothing to promote.",
        )
        owned: PromotionRefusal | None = None
    else:
        owned = classify_installer_ownership(package_dir)
        refusal = owned or promotion_refusal(skill, package_dir)

    return SkillPublishability(
        skill=skill,
        source_kind=source_kind,
        root_label=_root_label(root, source_kind),
        publishable=publishable,
        publishable_reason=reason,
        publishable_detail=_PUBLISHABLE_DETAIL[reason],
        promotable=refusal is None,
        promotion_block_reason=refusal.code if refusal else None,
        promotion_block_detail=refusal.message if refusal else None,
        installer_owned=owned is not None,
        installer_edited=bool(owned and owned.code == BLOCK_INSTALLER_OWNED_EDITED),
        content_hash=content_hash,
    )


# ── Machine-wide sweep ─────────────────────────────────────────────────────


def build_publishability_rows(roots: Iterable[Path] | None = None) -> list[dict[str, Any]]:
    """Every resolvable skill package across the resolver's roots, with its
    typed publishability and promotability.

    Names ALL offenders in one pass — a surface that shows only the shared root
    is exactly how "resolvable but structurally unable to travel" stayed
    invisible. Rows are sorted by ``(source_kind, skill)`` with the publishable
    tier first so the unpublishable set reads as a block.
    """

    from agent.skill_utils import (
        get_all_skills_dirs,
        skill_package_content_hash,
        skill_source_kind,
    )

    from .skill_promotion import iter_skill_packages

    search_roots = list(roots) if roots is not None else get_all_skills_dirs()
    rows: list[dict[str, Any]] = []
    for root in search_roots:
        if not Path(root).is_dir():
            continue
        try:
            source_kind = skill_source_kind(root)
        except Exception:  # noqa: BLE001
            source_kind = "external"
        for slug, package_dir in iter_skill_packages(Path(root)):
            try:
                content_hash = skill_package_content_hash(
                    package_dir, package_dir / "SKILL.md"
                )
            except Exception:  # noqa: BLE001 — an unreadable package still reports
                content_hash = None
            rows.append(
                classify_publishability(
                    slug,
                    package_dir,
                    Path(root),
                    source_kind=source_kind,
                    content_hash=content_hash,
                ).as_dict()
            )

    order = {"shared_core": 0, "profile_local": 1, "external": 2}
    rows.sort(key=lambda row: (order.get(row["source_kind"], 3), row["skill"], row["root_label"]))
    return rows
