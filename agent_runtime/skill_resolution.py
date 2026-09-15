"""Canonical shared-skill resolution, provenance and per-turn policy."""
import hashlib
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple
from hermes_constants import CANONICAL_SHARED_SKILL_IDS, get_shared_skills_dir, get_skills_dir
# Import skill_utils only inside consumers: its compatibility aliases import us.

_SKILL_RUNTIME_SURFACE: ContextVar[str | None] = ContextVar(
    "hermes_skill_runtime_surface", default=None
)

_SKILL_RUNTIME_ROOT_NODE_MODE: ContextVar[bool] = ContextVar(
    "hermes_skill_runtime_root_node_mode", default=False
)

@contextmanager
def skill_runtime_scope(
    *, surface: str | None, root_node_mode: bool = False
) -> Iterator[None]:
    """Bind the active skill surface/mode for prompt and tool enforcement."""

    surface_token = _SKILL_RUNTIME_SURFACE.set(surface)
    mode_token = _SKILL_RUNTIME_ROOT_NODE_MODE.set(bool(root_node_mode))
    try:
        yield
    finally:
        _SKILL_RUNTIME_ROOT_NODE_MODE.reset(mode_token)
        _SKILL_RUNTIME_SURFACE.reset(surface_token)

def current_skill_runtime_context() -> tuple[str | None, bool]:
    """Return the active surface/mode, or ``(None, False)`` outside a lane."""

    return _SKILL_RUNTIME_SURFACE.get(), _SKILL_RUNTIME_ROOT_NODE_MODE.get()

@dataclass(frozen=True, slots=True)
class SkillResolutionCandidate:
    """One filesystem skill candidate returned by the canonical resolver."""

    root: Path
    skill_dir: Path | None
    skill_md: Path
    source_kind: str

@dataclass(frozen=True, slots=True)
class SkillResolution:
    """Deterministic filesystem resolution for one skill identifier.

    ``status`` is one of ``resolved``, ``missing``, or ``collision``.  Callers
    must never choose a winner for a collision: the point of this result is to
    make the catalog, loader, readiness checks, and prompt receipts agree.
    """

    identifier: str
    status: str
    candidates: tuple[SkillResolutionCandidate, ...]

    @property
    def candidate(self) -> SkillResolutionCandidate | None:
        return self.candidates[0] if self.status == "resolved" else None

@dataclass(frozen=True, slots=True)
class _SkillRootRegistry:
    fingerprint: tuple[tuple[str, int | None, int | None], ...]
    manifests: tuple[tuple[Path | None, Path], ...]
    legacy: tuple[tuple[Path | None, Path], ...]
    manifests_by_alias: dict[str, tuple[tuple[Path | None, Path], ...]]
    legacy_by_alias: dict[str, tuple[tuple[Path | None, Path], ...]]

_SKILL_ROOT_REGISTRY_CACHE: dict[str, _SkillRootRegistry] = {}

_SKILL_ROOT_REGISTRY_LOCK = threading.Lock()

_walk_state = threading.local()

def skill_root_walks_this_thread() -> int:
    """How many physical-root registry walks this thread has paid for."""

    return int(getattr(_walk_state, "walks", 0))

def reset_skill_root_walks_for_tests() -> None:
    """Test hook — zero this thread's walk counter."""

    _walk_state.walks = 0

def _note_skill_root_walk() -> None:
    _walk_state.walks = int(getattr(_walk_state, "walks", 0)) + 1

def _skill_root_registry_cache_clear() -> None:
    """Test hook — drop reusable physical-root candidate registries."""

    with _SKILL_ROOT_REGISTRY_LOCK:
        _SKILL_ROOT_REGISTRY_CACHE.clear()

def _skill_root_registry(root: Path) -> _SkillRootRegistry:
    """Return the candidate registry for one physical skill root.

    The fingerprint covers every resolver-visible markdown candidate plus the
    active-org marker. A changed root rebuilds only its own registry; unchanged
    roots reuse parsed frontmatter across profiles and snapshot builds.

    **This function always touches the filesystem.** Its cache is keyed on the
    root and validated by fingerprint, so reaching it at all costs a walk. A
    caller that wants to avoid the walk shares a ``_root_registries`` map for
    the life of one turn instead — chat-turn-prep CP-5.
    """
    from agent import skill_utils as _skills

    _note_skill_root_walk()
    root_key = str(_resolved_path(root))
    if not root.is_dir():
        fingerprint: tuple[tuple[str, int | None, int | None], ...] = ()
        with _SKILL_ROOT_REGISTRY_LOCK:
            cached = _SKILL_ROOT_REGISTRY_CACHE.get(root_key)
            if cached is not None and cached.fingerprint == fingerprint:
                return cached
            registry = _SkillRootRegistry(fingerprint, (), (), {}, {})
            _SKILL_ROOT_REGISTRY_CACHE[root_key] = registry
            return registry

    manifests = list(_skills.iter_skill_index_files(root, "SKILL.md"))
    legacy = [
        path
        for path in root.rglob("*.md")
        if path.name != "SKILL.md" and not _skills.is_skill_support_path(path)
    ]
    marker = root / _skills.ORG_MIRROR_DIR_NAME / _skills.ORG_ACTIVE_MARKER
    fingerprint_paths = [*manifests, *legacy, marker]
    stamps: list[tuple[str, int | None, int | None]] = []
    for path in fingerprint_paths:
        try:
            relative = "/".join(path.relative_to(root).parts)
        except ValueError:
            relative = str(path)
        try:
            stat = path.stat()
            stamps.append((relative, stat.st_mtime_ns, stat.st_size))
        except OSError:
            stamps.append((relative, None, None))
    fingerprint = tuple(stamps)
    with _SKILL_ROOT_REGISTRY_LOCK:
        cached = _SKILL_ROOT_REGISTRY_CACHE.get(root_key)
        if cached is not None and cached.fingerprint == fingerprint:
            return cached

    manifest_aliases: dict[str, list[tuple[Path | None, Path]]] = {}
    for manifest in manifests:
        aliases = {manifest.parent.name}
        try:
            frontmatter, _ = _skills.parse_frontmatter(manifest.read_text(encoding="utf-8"))
        except Exception:
            frontmatter = {}
        declared = str(frontmatter.get("name") or "").strip()
        if declared:
            aliases.add(declared)
        for alias in aliases:
            manifest_aliases.setdefault(alias, []).append((manifest.parent, manifest))

    legacy_aliases: dict[str, list[tuple[Path | None, Path]]] = {}
    for path in legacy:
        legacy_aliases.setdefault(path.stem, []).append((None, path))

    registry = _SkillRootRegistry(
        fingerprint,
        tuple((manifest.parent, manifest) for manifest in manifests),
        tuple((None, path) for path in legacy),
        {key: tuple(value) for key, value in manifest_aliases.items()},
        {key: tuple(value) for key, value in legacy_aliases.items()},
    )
    with _SKILL_ROOT_REGISTRY_LOCK:
        _SKILL_ROOT_REGISTRY_CACHE[root_key] = registry
    return registry

def _resolved_path(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path.expanduser().absolute()

def skill_source_kind(root: Path) -> str:
    """Classify a resolver root without exposing its absolute path on wire."""

    resolved = _resolved_path(root)
    if resolved == _resolved_path(get_skills_dir()):
        return "profile_local"
    if resolved == _resolved_path(get_shared_skills_dir()):
        return "shared_core"
    return "external"

def resolve_skill(
    identifier: str,
    *,
    roots: List[Path] | None = None,
    categorized_identifier: str | None = None,
    _root_registries: Dict[str, _SkillRootRegistry] | None = None,
) -> SkillResolution:
    """Resolve a filesystem skill through the one ordered runtime registry.

    Plugin-qualified skills remain owned by the plugin registry.  This function
    owns every filesystem skill lookup, including direct/nested/frontmatter-name
    and legacy flat-file forms.  Duplicate candidates produce ``collision``;
    root ordering is descriptive only and never silently selects a winner.
    """
    from agent import skill_utils as _skills

    name = str(identifier or "").strip()
    search_roots = list(roots) if roots is not None else _skills.get_all_skills_dirs()
    candidates: list[SkillResolutionCandidate] = []
    seen: set[Path] = set()

    def record(root: Path, skill_dir: Path | None, skill_md: Path) -> None:
        key = _resolved_path(skill_md)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            SkillResolutionCandidate(
                root=root,
                skill_dir=skill_dir,
                skill_md=skill_md,
                source_kind=skill_source_kind(root),
            )
        )

    lookup_names = [name]
    categorized = str(categorized_identifier or "").strip()
    if categorized and categorized not in lookup_names:
        lookup_names.append(categorized)

    # chat-turn-prep CP-5: the same in/out accumulator ``resolve_skills`` takes.
    # This function is called ONCE PER NAME by the observability row's
    # ``_resolved_skill_receipt``, so without a shared map a turn pays one full
    # per-root walk for every used, queued and required-preload skill it names —
    # inside the very span the CP-9 read measured at 157–547 ms. The plan's §0.3
    # counted three walkers; this was the fourth.
    root_registries = _root_registries if _root_registries is not None else {}

    for root in search_roots:
        root_key = str(_resolved_path(root))
        registry = root_registries.get(root_key)
        if registry is None:
            registry = _skill_root_registry(root)
            root_registries[root_key] = registry
        for lookup in lookup_names:
            direct_manifest = root / lookup / "SKILL.md"
            for skill_dir, manifest in registry.manifests:
                if manifest == direct_manifest:
                    record(root, skill_dir, manifest)
            direct_legacy = (root / lookup).with_suffix(".md")
            for skill_dir, legacy in registry.legacy:
                if legacy == direct_legacy:
                    record(root, skill_dir, legacy)
        for skill_dir, manifest in registry.manifests_by_alias.get(name, ()):
            record(root, skill_dir, manifest)
        for skill_dir, legacy in registry.legacy_by_alias.get(name, ()):
            record(root, skill_dir, legacy)

    status = _skill_resolution_status(name, candidates)
    return SkillResolution(name, status, tuple(candidates))

def resolve_skills(
    identifiers: List[str],
    *,
    roots: List[Path] | None = None,
    _root_registries: Dict[str, _SkillRootRegistry] | None = None,
) -> Dict[str, SkillResolution]:
    """Resolve many bare/path identifiers with one registry walk."""
    from agent import skill_utils as _skills

    names = list(dict.fromkeys(str(item or "").strip() for item in identifiers))
    names = [name for name in names if name]
    search_roots = list(roots) if roots is not None else _skills.get_all_skills_dirs()
    found: Dict[str, list[SkillResolutionCandidate]] = {name: [] for name in names}
    seen: Dict[str, set[Path]] = {name: set() for name in names}
    root_registries = _root_registries if _root_registries is not None else {}

    def record(name: str, root: Path, skill_dir: Path | None, skill_md: Path) -> None:
        key = _resolved_path(skill_md)
        if key in seen[name]:
            return
        seen[name].add(key)
        found[name].append(
            SkillResolutionCandidate(
                root=root,
                skill_dir=skill_dir,
                skill_md=skill_md,
                source_kind=skill_source_kind(root),
            )
        )

    for root in search_roots:
        root_key = str(_resolved_path(root))
        registry = root_registries.get(root_key)
        if registry is None:
            registry = _skill_root_registry(root)
            root_registries[root_key] = registry
        for name in names:
            direct_manifest = root / name / "SKILL.md"
            for skill_dir, manifest in registry.manifests:
                if manifest == direct_manifest:
                    record(name, root, skill_dir, manifest)
            direct_legacy = (root / name).with_suffix(".md")
            for skill_dir, legacy in registry.legacy:
                if legacy == direct_legacy:
                    record(name, root, skill_dir, legacy)
            for skill_dir, manifest in registry.manifests_by_alias.get(name, ()):
                record(name, root, skill_dir, manifest)
            for skill_dir, legacy in registry.legacy_by_alias.get(name, ()):
                record(name, root, skill_dir, legacy)

    result: Dict[str, SkillResolution] = {}
    for name, candidates in found.items():
        status = _skill_resolution_status(name, candidates)
        result[name] = SkillResolution(name, status, tuple(candidates))
    return result

def _skill_resolution_status(
    identifier: str, candidates: list[SkillResolutionCandidate]
) -> str:
    if not candidates:
        return "missing"
    if len(candidates) != 1:
        return "collision"
    if (
        identifier in CANONICAL_SHARED_SKILL_IDS
        and candidates[0].source_kind != "shared_core"
    ):
        return "invalid_source"
    return "resolved"

_CONTENT_HASH_CACHE: Dict[Tuple[Any, ...], str] = {}

_CONTENT_HASH_CACHE_MAX = 4096

def _content_hash_cache_clear() -> None:
    """Test hook — drop the skill package content-hash cache."""
    _CONTENT_HASH_CACHE.clear()

def skill_package_content_hash(skill_dir: Path | None, skill_md: Path) -> str:
    """Stable content hash for the exact skill package the resolver selected.

    mtime-cached (see ``_CONTENT_HASH_CACHE``): the returned digest is identical
    to an uncached run; repeats within a build skip re-reading unchanged files.
    """
    from agent import skill_utils as _skills

    if skill_dir is None:
        files = [skill_md]
        base = skill_md.parent
    else:
        files = [
            path
            for path in sorted(skill_dir.rglob("*"))
            if path.is_file()
            and not any(
                part.startswith(".") or part in _skills.EXCLUDED_SKILL_DIRS
                for part in path.relative_to(skill_dir).parts
            )
        ]
        base = skill_dir

    def _relative(source: Path) -> str:
        try:
            return "/".join(source.relative_to(base).parts)
        except ValueError:
            return source.name

    entries: list[tuple[str, Path]] = [(_relative(source), source) for source in files]
    stamps: list[tuple[str, int | None, int | None]] = []
    for relative, source in entries:
        try:
            st = source.stat()
            stamps.append((relative, st.st_mtime_ns, st.st_size))
        except OSError:
            stamps.append((relative, None, None))
    cache_key = (str(base), tuple(stamps))
    cached = _CONTENT_HASH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    digest = hashlib.sha256()
    for relative, source in entries:
        digest.update(relative.encode("utf-8", errors="replace"))
        digest.update(b"\x00")
        try:
            digest.update(source.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\x00")
    value = digest.hexdigest()
    if len(_CONTENT_HASH_CACHE) >= _CONTENT_HASH_CACHE_MAX:
        _CONTENT_HASH_CACHE.clear()
    _CONTENT_HASH_CACHE[cache_key] = value
    return value

def skill_frontmatter_runtime_compatibility(
    frontmatter: dict[str, Any] | None,
    *,
    surface: str,
    root_node_mode: bool = False,
) -> dict[str, Any]:
    """Evaluate surface/mode compatibility from parsed skill frontmatter."""

    frontmatter = frontmatter if isinstance(frontmatter, dict) else {}
    metadata = frontmatter.get("metadata") if isinstance(frontmatter, dict) else {}
    hermes = metadata.get("hermes") if isinstance(metadata, dict) else {}
    if not isinstance(hermes, dict):
        # A skill authored for another runtime (metadata present, hermes block
        # absent or None) must degrade to defaults; this function runs for every
        # skill in the shared root on every prompt-observability build, so one
        # foreign manifest must not take down the whole lane.
        hermes = {}
    surfaces = hermes.get("surfaces")
    modes = hermes.get("modes")
    if isinstance(surfaces, str):
        surfaces = [surfaces]
    elif not isinstance(surfaces, (list, tuple, set)):
        surfaces = []
    if isinstance(modes, str):
        modes = [modes]
    elif not isinstance(modes, (list, tuple, set)):
        modes = []
    allowed_surfaces = {str(item) for item in surfaces or []}
    allowed_modes = {str(item) for item in modes or []}
    load_policy = str(hermes.get("load_policy") or "explicit")
    if allowed_surfaces and surface not in allowed_surfaces:
        return {
            "compatible": False,
            "reason": "surface_not_supported",
            "load_policy": load_policy,
        }
    active_mode = "root_node" if root_node_mode else "standard"
    if allowed_modes and active_mode not in allowed_modes:
        return {
            "compatible": False,
            "reason": "mode_not_supported",
            "load_policy": load_policy,
        }
    return {
        "compatible": True,
        "reason": "compatible",
        "surface": surface,
        "mode": active_mode,
        "load_policy": load_policy,
    }

def _cached_skill_frontmatter(skill_md: Path) -> Dict[str, Any]:
    """mtime-cached frontmatter parse for a SKILL.md manifest.

    ``skill_runtime_compatibility`` is evaluated per skill, per surface, per
    persona across a snapshot build (~12.9k ``_skills.parse_frontmatter`` calls / ~3.8s
    measured 2026-07-23), yet a manifest's bytes only change when the file
    changes on disk. Cache the (read_text + _skills.parse_frontmatter) by the file's
    identity+mtime+size (``parse_cache.cached_by_mtime``) so repeats within a
    build are free while an on-disk edit invalidates the entry. Behavior is
    identical to the inline parse on a cache miss; the result is read-only, never
    mutated, so sharing the cached dict is safe.
    """
    from agent import skill_utils as _skills
    from agent_runtime.parse_cache import cached_by_mtime

    def _load(path: Path) -> Dict[str, Any]:
        frontmatter, _ = _skills.parse_frontmatter(path.read_text(encoding="utf-8"))
        return frontmatter if isinstance(frontmatter, dict) else {}

    return cached_by_mtime(skill_md, _load, default={})

def skill_runtime_compatibility(
    candidate: SkillResolutionCandidate | None,
    *,
    surface: str,
    root_node_mode: bool = False,
) -> dict[str, Any]:
    """Evaluate declared surface/mode compatibility for a resolved skill."""

    if candidate is None:
        return {"compatible": False, "reason": "unresolved"}
    frontmatter = _cached_skill_frontmatter(candidate.skill_md)
    return skill_frontmatter_runtime_compatibility(
        frontmatter,
        surface=surface,
        root_node_mode=root_node_mode,
    )

def required_preload_skill_ids(
    identifiers: List[str],
    *,
    surface: str,
    root_node_mode: bool = False,
    _root_registries: Dict[str, _SkillRootRegistry] | None = None,
) -> List[str]:
    """Return assigned skills whose resolved policy requires model loading.

    ``_root_registries`` is chat-turn-prep CP-5's shared walk: the mission-chat
    handler hands the same map to this call and to the prompt-observability
    resolver, so the preload policy and the observability row are answered from
    ONE registry snapshot per physical root per turn instead of one each.
    """

    names = list(dict.fromkeys(str(item or "").strip() for item in identifiers))
    names = [name for name in names if name]
    resolutions = resolve_skills(names, _root_registries=_root_registries)
    required: List[str] = []
    for name in names:
        resolution = resolutions[name]
        compatibility = skill_runtime_compatibility(
            resolution.candidate,
            surface=surface,
            root_node_mode=root_node_mode,
        )
        if (
            resolution.status == "resolved"
            and compatibility.get("compatible")
            and compatibility.get("load_policy") == "required_preload"
        ):
            required.append(name)
    return required
