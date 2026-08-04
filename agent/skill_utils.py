"""Lightweight skill metadata utilities shared by prompt_builder and skills_tool.

This module intentionally avoids importing the tool registry, CLI config, or any
heavy dependency chain.  It is safe to import at module level without triggering
tool registration or provider resolution.
"""

import hashlib
import logging
import os
import re
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from hermes_constants import (
    CANONICAL_SHARED_SKILL_IDS,
    get_config_path,
    get_shared_skills_dir,
    get_skills_dir,
    is_termux,
)

logger = logging.getLogger(__name__)

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

# ── Platform mapping ──────────────────────────────────────────────────────

PLATFORM_MAP = {
    "macos": "darwin",
    "linux": "linux",
    "windows": "win32",
}

EXCLUDED_SKILL_DIRS = frozenset(
    (
        ".git",
        ".github",
        ".hub",
        ".archive",
        # Realm-skill quarantine + promotion provenance live under the shared
        # skills root but must stay resolver-invisible: ``.realm_inbox`` is a
        # byte-faithful mirror of each realm's skill packages (never live), and
        # ``.provenance`` holds promotion sidecars that live OUTSIDE any skill
        # package so they cannot change a package content hash. Neither ever
        # resolves a skill nor turns a canonical skill into a collision.
        ".realm_inbox",
        ".provenance",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    )
)

# Supporting files live inside a skill package and are loaded explicitly via
# skill_view(skill, file_path=...). They are not standalone skills and must not
# be scanned for active SKILL.md/DESCRIPTION.md entries, even if a Curator or
# archive workflow preserves a complete old skill package under references/.
SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))

# ── Org-shared skills (sync contract) ───────────────────────────
# Org mirrors live under ~/.hermes/skills/_org/<org_id>/. Resolution is
# TOKEN-GATED via a marker file the sync client writes after verifying the
# token (skills_sync_client.pull_org_skills): only the marked org's mirror is
# scanned. No marker ⇒ no org skills load. The marker is plain data (org_id
# string) so this module stays import-light; the VERIFICATION lives in the
# sync client, which is the only writer. Offline grace: the marker persists,
# so already-pulled org skills keep working without connectivity; a VERIFIED
# org change (or personal-org token) rewrites/removes it.

ORG_MIRROR_DIR_NAME = "_org"
ORG_ACTIVE_MARKER = ".active_org"
ORG_PROVENANCE_FILE = ".org-provenance.json"
# Records the fingerprint of each skill exactly as upstream sent it, so a
# later local edit is detectable and an org pull can refuse to clobber it.
ORG_BASELINE_FILE = ".org-baseline.json"


def read_active_org_id(skills_dir: Path) -> Optional[str]:
    """The org id whose mirror may resolve, or None (no org skills load)."""
    try:
        marker = skills_dir / ORG_MIRROR_DIR_NAME / ORG_ACTIVE_MARKER
        if not marker.exists():
            return None
        val = marker.read_text(encoding="utf-8").strip()
        return val or None
    except OSError:
        return None


def is_org_mirror_path(path, skills_dir: Path) -> bool:
    """True when *path* is inside the org mirror (``_org/``)."""
    try:
        rel = Path(path).resolve().relative_to(Path(skills_dir).resolve())
    except (OSError, ValueError):
        return False
    return bool(rel.parts) and rel.parts[0] == ORG_MIRROR_DIR_NAME


def org_id_of_path(path, skills_dir: Path) -> Optional[str]:
    """The ``<org_id>`` segment for a path under ``_org/<org_id>/...``."""
    try:
        rel = Path(path).resolve().relative_to(Path(skills_dir).resolve())
    except (OSError, ValueError):
        return None
    if len(rel.parts) >= 2 and rel.parts[0] == ORG_MIRROR_DIR_NAME:
        return rel.parts[1]
    return None


def is_excluded_skill_path(path, *, root: Optional[Path] = None) -> bool:
    """True if *path* should be skipped by active skill scanners.

    Use this on every ``SKILL.md`` path produced by direct ``rglob`` scans to
    prune dependency, virtualenv, VCS, cache, and progressive-disclosure
    support-package paths. Centralising the check here keeps every
    skill-scanning site in sync with the shared exclusion set.

    Accepts a Path or string.
    """
    try:
        parts = path.parts  # Path
    except AttributeError:
        from pathlib import PurePath
        parts = PurePath(str(path)).parts
    return any(part in EXCLUDED_SKILL_DIRS for part in parts) or is_skill_support_path(
        path, root=root
    )


def is_skill_support_path(path, *, root: Optional[Path] = None) -> bool:
    """True if *path* is under a support dir of an actual skill root.

    ``references/``, ``templates/``, ``assets/``, and ``scripts/`` are
    progressive-disclosure support areas when they sit directly inside a skill
    directory containing ``SKILL.md``. They are not active discovery roots for
    standalone skills. A preserved package such as
    ``some-skill/references/old-skill-package/SKILL.md`` is documentation data
    unless the caller explicitly loads it via ``file_path``.

    Legitimate categories or skill names such as ``skills/scripts/foo`` remain
    discoverable because their ``scripts`` component is not directly under a
    directory that contains ``SKILL.md``.
    """
    path_obj = path if isinstance(path, Path) else Path(str(path))
    parts = path_obj.parts
    # Last component may be a file or candidate skill directory name. Only
    # components before the leaf can be containing support directories.
    for idx, part in enumerate(parts[:-1]):
        if part not in SKILL_SUPPORT_DIRS or idx == 0:
            continue
        skill_root = Path(*parts[:idx])
        if root is not None and not path_obj.is_absolute():
            skill_root = root / skill_root
        if (skill_root / "SKILL.md").exists():
            return True
    return False


# ── Lazy YAML loader ─────────────────────────────────────────────────────

_yaml_load_fn = None


def yaml_load(content: str):
    """Parse YAML with lazy import and CSafeLoader preference."""
    global _yaml_load_fn
    if _yaml_load_fn is None:
        import yaml

        loader = getattr(yaml, "CSafeLoader", None) or yaml.SafeLoader

        def _load(value: str):
            return yaml.load(value, Loader=loader)

        _yaml_load_fn = _load
    return _yaml_load_fn(content)


# ── Frontmatter parsing ──────────────────────────────────────────────────


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Uses yaml with CSafeLoader for full YAML support (nested metadata, lists)
    with a fallback to simple key:value splitting for robustness.

    A single leading UTF-8 BOM (U+FEFF) is stripped before parsing. Windows
    GUI editors (Notepad, PowerShell ``>``) prepend one when saving a SKILL.md
    as UTF-8, and ``read_text(encoding="utf-8")`` preserves it (only
    ``utf-8-sig`` strips it). Left in place, the BOM defeats the ``---`` fence
    check below and the whole frontmatter is silently discarded — name,
    description, ``platforms`` gating, env-var setup, and conditional
    activation all vanish. See CONTRIBUTING.md "File encoding".

    Returns:
        (frontmatter_dict, remaining_body)
    """
    frontmatter: Dict[str, Any] = {}

    # Strip only a leading BOM; a BOM mid-content is data, not a marker.
    if content.startswith("\ufeff"):
        content = content[1:]
    body = content

    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_content = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    try:
        parsed = yaml_load(yaml_content)
        if isinstance(parsed, dict):
            frontmatter = parsed
    except Exception:
        # Fallback: simple key:value parsing for malformed YAML
        for line in yaml_content.strip().split("\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()

    return frontmatter, body


# ── Platform matching ─────────────────────────────────────────────────────


def skill_matches_platform_list(platforms: Any) -> bool:
    """Return True when *platforms* is compatible with the current OS."""
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    running_in_termux = is_termux()
    for platform in platforms:
        normalized = str(platform).lower().strip()
        mapped = PLATFORM_MAP.get(normalized, normalized)
        if current.startswith(mapped):
            return True
        # Termux runs a Linux userland on Android. Accept linux-tagged
        # skills regardless of whether sys.platform is "linux" (pre-3.13
        # Termux) or "android" (Python 3.13+ Termux, and any other
        # Android runtime).
        if running_in_termux and mapped == "linux":
            return True
        # Explicit termux/android tags match a Termux session too.
        if running_in_termux and mapped in ("termux", "android"):
            return True
    return False


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is compatible with the current OS.

    Skills declare platform requirements via a top-level ``platforms`` list
    in their YAML frontmatter::

        platforms: [macos]          # macOS only
        platforms: [macos, linux]   # macOS and Linux

    If the field is absent or empty the skill is compatible with **all**
    platforms (backward-compatible default).

    Termux note: on Termux/Android, ``sys.platform`` is ``"linux"`` on
    older Pythons but became ``"android"`` on Python 3.13+. Termux is a
    Linux userland riding on the Android kernel, so skills tagged
    ``linux`` are treated as compatible in Termux regardless of which
    ``sys.platform`` value Python reports. Individual Linux commands
    inside a skill may still misbehave (no systemd, BusyBox utils, no
    apt/dnf, etc.) but that is on the skill, not on platform gating.
    """
    return skill_matches_platform_list(frontmatter.get("platforms"))


# ── Environment matching ──────────────────────────────────────────────────

# Recognized environment tags and how each is detected. An environment tag is
# a *relevance* gate, not a hard-compatibility gate (that is what ``platforms:``
# is for). A skill tagged for an environment it isn't relevant to is hidden from
# the skills index / offer surfaces so it does not add noise for users who will
# never need it — but it can ALWAYS still be loaded explicitly (``skill_view``,
# ``--skills``), because an explicit request is explicit consent.
#
# Detection is cached for the process lifetime via ``_ENV_DETECT_CACHE``.
_KNOWN_ENVIRONMENTS = frozenset({"kanban", "docker", "s6"})

_ENV_DETECT_CACHE: Dict[str, bool] = {}


def _detect_environment(env: str) -> bool:
    """Return True when the named runtime environment is currently active.

    Cached per process. Unknown env names return True (fail-open: never hide a
    skill because of a tag we don't understand).
    """
    if env in _ENV_DETECT_CACHE:
        return _ENV_DETECT_CACHE[env]

    result = True
    if env == "kanban":
        # Kanban is "active" either as a dispatcher-spawned worker (the
        # dispatcher sets ``HERMES_KANBAN_TASK`` / ``HERMES_KANBAN_BOARD`` in the
        # worker env) or as an orchestrator profile that has opted into the
        # kanban toolset. Mirror the same signals the kanban tools themselves
        # gate on (``tools/kanban_tools.py``) so the offer filter agrees with
        # tool availability.
        if os.getenv("HERMES_KANBAN_TASK") or os.getenv("HERMES_KANBAN_BOARD"):
            result = True
        else:
            try:
                from tools.kanban_tools import _profile_has_kanban_toolset

                result = bool(_profile_has_kanban_toolset())
            except Exception:
                result = False
    elif env == "docker":
        try:
            from hermes_constants import is_container

            result = is_container()
        except Exception:
            result = False
    elif env == "s6":
        # The Hermes Docker image runs s6-overlay as PID 1 (/init). s6 plants
        # its runtime scaffolding under /run/s6 and ships its admin tree under
        # /package/admin/s6-overlay. Either marker means we're inside an
        # s6-supervised container.
        result = os.path.isdir("/run/s6") or os.path.isdir(
            "/package/admin/s6-overlay"
        )

    _ENV_DETECT_CACHE[env] = result
    return result


def skill_matches_environment(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is relevant to the current runtime environment.

    Skills may declare an ``environments`` list in their YAML frontmatter::

        environments: [kanban]        # only relevant when kanban is active
        environments: [s6]            # only relevant inside the s6 Docker image
        environments: [docker]        # only relevant inside any container

    If the field is absent or empty the skill is relevant in **all**
    environments (backward-compatible default).

    This is an OFFER-time filter: it controls whether a skill shows up in the
    skills index / autocomplete / slash-command list. It is intentionally NOT
    enforced by ``skill_view`` or ``--skills`` preloading — an explicit load is
    explicit consent, and load-bearing force-loads (e.g. a dispatcher pinning
    a task to a specialist skill via ``--skills``) must always succeed
    regardless of how the offer surfaces filter the skill.

    A skill matches when ANY of its declared environments is currently active
    (OR semantics, mirroring ``platforms``). Unknown env tags fail open.
    """
    environments = frontmatter.get("environments")
    if not environments:
        return True
    if not isinstance(environments, list):
        environments = [environments]
    for env in environments:
        normalized = str(env).lower().strip()
        if not normalized:
            continue
        if normalized not in _KNOWN_ENVIRONMENTS:
            # Tag we don't understand — don't hide the skill over it.
            return True
        if _detect_environment(normalized):
            return True
    return False


# ── Disabled skills ───────────────────────────────────────────────────────


_RAW_CONFIG_CACHE: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


def _raw_config_cache_clear() -> None:
    """Test hook — drop the shared raw config cache."""
    _RAW_CONFIG_CACHE.clear()


def _load_raw_config() -> Dict[str, Any]:
    """Read config.yaml with a shared mtime+size keyed cache.

    This module intentionally avoids importing ``hermes_cli.config`` on the
    skill prompt/build path. A tiny local cache gives the same repeated-read
    win without pulling the heavier CLI config stack into startup.
    """
    config_path = get_config_path()
    if not config_path.exists():
        return {}
    try:
        stat = config_path.stat()
        cache_key = (str(config_path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        cache_key = None

    if cache_key is not None:
        cached = _RAW_CONFIG_CACHE.get(cache_key)
        if cached is not None:
            return cached

    try:
        parsed = yaml_load(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("Could not read skill config %s: %s", config_path, e)
        return {}
    if not isinstance(parsed, dict):
        return {}

    if cache_key is not None:
        _RAW_CONFIG_CACHE.clear()
        _RAW_CONFIG_CACHE[cache_key] = parsed
    return parsed


def get_disabled_skill_names(platform: str | None = None) -> Set[str]:
    """Read disabled skill names from config.yaml.

    Args:
        platform: Explicit platform name (e.g. ``"telegram"``).  When
            *None*, resolves from ``HERMES_PLATFORM`` or
            ``HERMES_SESSION_PLATFORM`` env vars.  Returns the global
            disabled list, unioned with the platform-specific list when a
            platform is resolved (a globally-disabled skill stays disabled
            on every platform).

    Reads the config file directly (no CLI config imports) to stay
    lightweight.
    """
    parsed = _load_raw_config()
    if not parsed:
        return set()

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return set()

    from gateway.session_context import get_session_env
    resolved_platform = (
        platform
        or os.getenv("HERMES_PLATFORM")
        or get_session_env("HERMES_SESSION_PLATFORM")
    )
    global_disabled = _normalize_string_set(skills_cfg.get("disabled"))
    if resolved_platform:
        platform_disabled = (skills_cfg.get("platform_disabled") or {}).get(
            resolved_platform
        )
        if platform_disabled is not None:
            return global_disabled | _normalize_string_set(platform_disabled)
    return global_disabled


def _normalize_string_set(values) -> Set[str]:
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    return {str(v).strip() for v in values if str(v).strip()}


# ── External skills directories ──────────────────────────────────────────

# (config_path_str, mtime_ns) -> resolved external dirs list.  Keyed by
# mtime_ns so a config.yaml edit mid-run is picked up automatically;
# otherwise every call would re-read + re-YAML-parse the 15KB config,
# which becomes the dominant cost of ``hermes`` startup when ~120 skills
# each trigger a category lookup during banner construction (10+ seconds
# of pure waste).
_EXTERNAL_DIRS_CACHE: Dict[Tuple[str, int], List[Path]] = {}


def _external_dirs_cache_clear() -> None:
    """Test hook — drop the in-process cache."""
    _EXTERNAL_DIRS_CACHE.clear()
    _raw_config_cache_clear()


def get_external_skills_dirs() -> List[Path]:
    """Read ``skills.external_dirs`` from config.yaml and return validated paths.

    Each entry is expanded (``~`` and ``${VAR}``) and resolved to an absolute
    path.  Only directories that actually exist are returned.  Duplicates and
    paths that resolve to the local ``~/.hermes/skills/`` are silently skipped.

    Cached in-process, keyed on ``config.yaml`` mtime — the function is
    called once per skill during banner / tool-registry scans, and YAML
    parsing a non-trivial config dominates ``hermes`` cold-start time
    when the cache is absent.
    """
    config_path = get_config_path()
    if not config_path.exists():
        return []

    # Cache key: (absolute path, mtime_ns).  stat() is ~2us vs ~85ms for
    # the full YAML parse, so the fast path is nearly free.
    try:
        stat = config_path.stat()
        cache_key: Tuple[str, int] = (str(config_path), stat.st_mtime_ns)
    except OSError:
        cache_key = None  # type: ignore[assignment]

    if cache_key is not None:
        cached = _EXTERNAL_DIRS_CACHE.get(cache_key)
        if cached is not None:
            # Return a copy so callers can't mutate the cached list.
            return list(cached)

    parsed = _load_raw_config()
    if not parsed:
        return []

    skills_cfg = parsed.get("skills")
    if not isinstance(skills_cfg, dict):
        return []

    raw_dirs = skills_cfg.get("external_dirs")
    if not raw_dirs:
        result: List[Path] = []
        if cache_key is not None:
            _EXTERNAL_DIRS_CACHE[cache_key] = list(result)
        return result
    if isinstance(raw_dirs, str):
        raw_dirs = [raw_dirs]
    if not isinstance(raw_dirs, list):
        return []

    from hermes_constants import get_hermes_home

    hermes_home = get_hermes_home()
    local_skills = get_skills_dir().resolve()
    seen: Set[Path] = set()
    result = []

    for entry in raw_dirs:
        entry = str(entry).strip()
        if not entry:
            continue
        # Expand ~ and environment variables
        expanded = os.path.expanduser(os.path.expandvars(entry))
        p = Path(expanded)
        # Resolve relative paths against HERMES_HOME, not cwd
        if not p.is_absolute():
            p = (hermes_home / p).resolve()
        else:
            p = p.resolve()
        if p == local_skills:
            continue
        if p in seen:
            continue
        if p.is_dir():
            seen.add(p)
            result.append(p)
        else:
            logger.debug("External skills dir does not exist, skipping: %s", p)

    if cache_key is not None:
        _EXTERNAL_DIRS_CACHE[cache_key] = list(result)
    return result


def get_all_skills_dirs() -> List[Path]:
    """Return all skill directories: local profile ``skills/`` first, then the
    shared canonical root, then config ``external_dirs``.

    Index 0 is always the local profile skills dir (always included even if it
    doesn't exist yet — callers handle that; some callers slice ``[1:]`` to get
    the non-primary dirs). The shared canonical root
    (:func:`hermes_constants.get_shared_skills_dir`) follows — one physical dir
    every persona-profile shares and that realm sync publishes — then external
    dirs in config order. Duplicates are dropped so a shared root that equals
    the local dir (or an external entry) appears only once.
    """
    dirs: List[Path] = [get_skills_dir()]
    seen: Set[Path] = {dirs[0].expanduser()}
    for candidate in [get_shared_skills_dir(), *get_external_skills_dirs()]:
        key = candidate.expanduser()
        if key in seen:
            continue
        seen.add(key)
        dirs.append(candidate)
    return dirs


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


def _skill_root_registry_cache_clear() -> None:
    """Test hook — drop reusable physical-root candidate registries."""

    with _SKILL_ROOT_REGISTRY_LOCK:
        _SKILL_ROOT_REGISTRY_CACHE.clear()


def _skill_root_registry(root: Path) -> _SkillRootRegistry:
    """Return the candidate registry for one physical skill root.

    The fingerprint covers every resolver-visible markdown candidate plus the
    active-org marker. A changed root rebuilds only its own registry; unchanged
    roots reuse parsed frontmatter across profiles and snapshot builds.
    """

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

    manifests = list(iter_skill_index_files(root, "SKILL.md"))
    legacy = [
        path
        for path in root.rglob("*.md")
        if path.name != "SKILL.md" and not is_skill_support_path(path)
    ]
    marker = root / ORG_MIRROR_DIR_NAME / ORG_ACTIVE_MARKER
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
            frontmatter, _ = parse_frontmatter(manifest.read_text(encoding="utf-8"))
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
) -> SkillResolution:
    """Resolve a filesystem skill through the one ordered runtime registry.

    Plugin-qualified skills remain owned by the plugin registry.  This function
    owns every filesystem skill lookup, including direct/nested/frontmatter-name
    and legacy flat-file forms.  Duplicate candidates produce ``collision``;
    root ordering is descriptive only and never silently selects a winner.
    """

    name = str(identifier or "").strip()
    search_roots = list(roots) if roots is not None else get_all_skills_dirs()
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

    for root in search_roots:
        registry = _skill_root_registry(root)
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

    names = list(dict.fromkeys(str(item or "").strip() for item in identifiers))
    names = [name for name in names if name]
    search_roots = list(roots) if roots is not None else get_all_skills_dirs()
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


# Aggregate-mtime keyed cache for skill package content hashes.
#
# ``skill_package_content_hash`` was measured at 653 calls / 2.44s across one
# snapshot core (2026-07-23) — every candidate re-rglob'd + re-read from disk.
# The returned digest here is byte-identical to the uncached computation (same
# relpath+bytes algorithm); the cache only skips re-reading files that have not
# changed. INVALIDATION KEY: ``(base_dir, ((relpath, mtime_ns, size), ...))`` for
# every member file. A content edit changes ``mtime_ns``/``size`` and misses the
# cache — the same identity+mtime+size contract as ``_RAW_CONFIG_CACHE`` and
# ``parse_cache``. It cannot go stale silently on a nested content change: a
# directory-mtime-only key would (editing a nested file leaves the containing
# dir's mtime untouched), which is exactly why every member file is stamped.
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

    if skill_dir is None:
        files = [skill_md]
        base = skill_md.parent
    else:
        files = [
            path
            for path in sorted(skill_dir.rglob("*"))
            if path.is_file()
            and not any(
                part.startswith(".") or part in EXCLUDED_SKILL_DIRS
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
    persona across a snapshot build (~12.9k ``parse_frontmatter`` calls / ~3.8s
    measured 2026-07-23), yet a manifest's bytes only change when the file
    changes on disk. Cache the (read_text + parse_frontmatter) by the file's
    identity+mtime+size (``parse_cache.cached_by_mtime``) so repeats within a
    build are free while an on-disk edit invalidates the entry. Behavior is
    identical to the inline parse on a cache miss; the result is read-only, never
    mutated, so sharing the cached dict is safe.
    """
    from agent_runtime.parse_cache import cached_by_mtime

    def _load(path: Path) -> Dict[str, Any]:
        frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
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
) -> List[str]:
    """Return assigned skills whose resolved policy requires model loading."""

    names = list(dict.fromkeys(str(item or "").strip() for item in identifiers))
    names = [name for name in names if name]
    resolutions = resolve_skills(names)
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


def normalize_skill_lookup_name(identifier: str) -> str:
    """Normalize a skill identifier to a ``skill_view()``-safe relative path.

    Slash commands and cron jobs may store absolute paths to skills that live
    under ``~/.hermes/skills/`` (including via symlinks) or configured
    ``skills.external_dirs``. ``skill_view()`` rejects absolute names for
    security, so callers must translate trusted absolute paths to their
    relative form first.
    """
    raw_identifier = (identifier or "").strip()
    if not raw_identifier:
        return raw_identifier

    identifier_path = Path(raw_identifier).expanduser()
    if not identifier_path.is_absolute():
        return raw_identifier.lstrip("/")

    # Look the primary skills root up on tools.skills_tool at CALL time
    # (not via get_skills_dir()): callers and tests patch
    # ``tools.skills_tool.SKILLS_DIR`` and skill_view() itself resolves
    # against that module attribute, so normalization must agree with the
    # exact root skill_view() will enforce.  Import deferred to avoid a
    # module cycle (tools.skills_tool imports agent.skill_utils).
    try:
        from tools import skills_tool as _skills_tool
        primary_root = Path(_skills_tool.SKILLS_DIR)
    except Exception:
        primary_root = get_skills_dir()

    trusted_roots = [primary_root]
    try:
        trusted_roots.extend(get_all_skills_dirs()[1:])
    except Exception:
        pass

    # Prefer the lexical path under a trusted skill root before resolving
    # symlinks. Slash-command discovery can legitimately find a skill via
    # ~/.hermes/skills/<name> where <name> is a symlink to a checked-out
    # skill elsewhere. Resolving first turns that trusted visible path into
    # an arbitrary absolute path that skill_view() refuses to load.
    for root in trusted_roots:
        try:
            return identifier_path.relative_to(root).as_posix()
        except ValueError:
            continue

    try:
        return identifier_path.resolve().relative_to(primary_root.resolve()).as_posix()
    except Exception:
        logger.debug(
            "Skill identifier %r is an absolute path outside trusted skills "
            "roots — passing through unchanged (skill_view will reject it)",
            raw_identifier,
        )
        return raw_identifier


def _resolve_for_skill_ownership(path) -> Path:
    path_obj = path if isinstance(path, Path) else Path(str(path))
    try:
        return path_obj.expanduser().resolve()
    except (OSError, RuntimeError):
        return path_obj.expanduser().absolute()


def is_external_skill_path(path) -> bool:
    """Return True when ``path`` lives under a configured external skills dir.

    ``skills.external_dirs`` are externally owned: Hermes can discover and view
    their skills, and foreground user-directed tool calls may still edit them,
    but autonomous lifecycle maintenance must treat them as read-only. This
    helper centralizes the ownership boundary so curator/reporting/tool paths do
    not each need to re-interpret the config.
    """
    candidate = _resolve_for_skill_ownership(path)
    for root in get_external_skills_dirs():
        resolved_root = _resolve_for_skill_ownership(root)
        try:
            candidate.relative_to(resolved_root)
            return True
        except ValueError:
            continue
    return False


# ── Condition extraction ──────────────────────────────────────────────────


def extract_skill_conditions(frontmatter: Dict[str, Any]) -> Dict[str, List]:
    """Extract conditional activation fields from parsed frontmatter."""
    metadata = frontmatter.get("metadata")
    # Handle cases where metadata is not a dict (e.g., a string from malformed YAML)
    if not isinstance(metadata, dict):
        metadata = {}
    hermes = metadata.get("hermes") or {}
    if not isinstance(hermes, dict):
        hermes = {}
    return {
        "fallback_for_toolsets": hermes.get("fallback_for_toolsets", []),
        "requires_toolsets": hermes.get("requires_toolsets", []),
        "fallback_for_tools": hermes.get("fallback_for_tools", []),
        "requires_tools": hermes.get("requires_tools", []),
    }


# ── Skill config extraction ───────────────────────────────────────────────


def extract_skill_config_vars(frontmatter: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract config variable declarations from parsed frontmatter.

    Skills declare config.yaml settings they need via::

        metadata:
          hermes:
            config:
              - key: wiki.path
                description: Path to the LLM Wiki knowledge base directory
                default: "~/wiki"
                prompt: Wiki directory path

    Returns a list of dicts with keys: ``key``, ``description``, ``default``,
    ``prompt``.  Invalid or incomplete entries are silently skipped.
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, dict):
        return []
    hermes = metadata.get("hermes")
    if not isinstance(hermes, dict):
        return []
    raw = hermes.get("config")
    if not raw:
        return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    result: List[Dict[str, Any]] = []
    seen: set = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", "")).strip()
        if not key or key in seen:
            continue
        # Must have at least key and description
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        entry: Dict[str, Any] = {
            "key": key,
            "description": desc,
        }
        default = item.get("default")
        if default is not None:
            entry["default"] = default
        prompt_text = item.get("prompt")
        if isinstance(prompt_text, str) and prompt_text.strip():
            entry["prompt"] = prompt_text.strip()
        else:
            entry["prompt"] = desc
        seen.add(key)
        result.append(entry)
    return result


def discover_all_skill_config_vars() -> List[Dict[str, Any]]:
    """Scan all enabled skills and collect their config variable declarations.

    Walks every skills directory, parses each SKILL.md frontmatter, and returns
    a deduplicated list of config var dicts.  Each dict also includes a
    ``skill`` key with the skill name for attribution.

    Disabled and platform-incompatible skills are excluded.
    """
    all_vars: List[Dict[str, Any]] = []
    seen_keys: set = set()

    disabled = get_disabled_skill_names()
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.is_dir():
            continue
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            try:
                raw = skill_file.read_text(encoding="utf-8")
                frontmatter, _ = parse_frontmatter(raw)
            except Exception:
                continue

            skill_name = frontmatter.get("name") or skill_file.parent.name
            if str(skill_name) in disabled:
                continue
            if not skill_matches_platform(frontmatter):
                continue

            config_vars = extract_skill_config_vars(frontmatter)
            for var in config_vars:
                if var["key"] not in seen_keys:
                    var["skill"] = str(skill_name)
                    all_vars.append(var)
                    seen_keys.add(var["key"])

    return all_vars


# Storage prefix: all skill config vars are stored under skills.config.*
# in config.yaml.  Skill authors declare logical keys (e.g. "wiki.path");
# the system adds this prefix for storage and strips it for display.
SKILL_CONFIG_PREFIX = "skills.config"


def _resolve_dotpath(config: Dict[str, Any], dotted_key: str):
    """Walk a nested dict following a dotted key.  Returns None if any part is missing."""
    parts = dotted_key.split(".")
    current = config
    for part in parts:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def resolve_skill_config_values(
    config_vars: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Resolve current values for skill config vars from config.yaml.

    Skill config is stored under ``skills.config.<key>`` in config.yaml.
    Returns a dict mapping **logical** keys (as declared by skills) to their
    current values (or the declared default if the key isn't set).
    Path values are expanded via ``os.path.expanduser``.
    """
    config = _load_raw_config()

    resolved: Dict[str, Any] = {}
    for var in config_vars:
        logical_key = var["key"]
        storage_key = f"{SKILL_CONFIG_PREFIX}.{logical_key}"
        value = _resolve_dotpath(config, storage_key)

        if value is None or (isinstance(value, str) and not value.strip()):
            value = var.get("default", "")

        # Expand ~ in path-like values
        if isinstance(value, str) and ("~" in value or "${" in value):
            value = os.path.expanduser(os.path.expandvars(value))

        resolved[logical_key] = value

    return resolved


# ── Description extraction ────────────────────────────────────────────────

SKILL_PROMPT_DESC_LIMIT = 60


def _normalize_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Normalize a skill's description field for comparison/truncation."""
    raw_desc = frontmatter.get("description", "")
    return str(raw_desc).strip().strip("'\"") if raw_desc else ""


def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Extract a system-prompt-length description from parsed frontmatter."""
    desc = _normalize_skill_description(frontmatter)
    if not desc:
        return ""
    if len(desc) > SKILL_PROMPT_DESC_LIMIT:
        return desc[:SKILL_PROMPT_DESC_LIMIT - 3] + "..."
    return desc


def is_skill_description_truncated_for_prompt(frontmatter: Dict[str, Any]) -> bool:
    """True when the description will be truncated in the system prompt skill index."""
    desc = _normalize_skill_description(frontmatter)
    return len(desc) > SKILL_PROMPT_DESC_LIMIT


# ── File iteration ────────────────────────────────────────────────────────


def iter_skill_index_files(skills_dir: Path, filename: str):
    """Walk skills_dir yielding sorted paths matching *filename*.

    Excludes Hermes metadata, VCS, virtualenv/dependency, cache, and skill
    support directories. Support directories (references/templates/assets/
    scripts) can contain arbitrary markdown and even archived package
    ``SKILL.md`` files, but they are progressive-disclosure data loaded through
    ``skill_view(..., file_path=...)`` rather than active skill roots.

    M2 org mirrors (``_org/``): TOKEN-GATED resolution. Only the active org's
    subdir (per the sync-client-written ``.active_org`` marker) is walked;
    every other ``_org/<id>/`` (stale mirror from a previous org, or no
    marker at all) is pruned — leave an org and its skills stop resolving,
    without any manual cleanup.
    """
    skills_dir_str = str(skills_dir)
    active_org = read_active_org_id(skills_dir)
    org_root = os.path.join(skills_dir_str, ORG_MIRROR_DIR_NAME)
    matches: list[str] = []
    for root, dirs, files in os.walk(skills_dir_str, followlinks=True):
        has_skill_md = "SKILL.md" in files
        if root == skills_dir_str and ORG_MIRROR_DIR_NAME in dirs and active_org is None:
            dirs.remove(ORG_MIRROR_DIR_NAME)
        elif root == org_root:
            # Inside _org/: descend ONLY into the active org's mirror.
            dirs[:] = [d for d in dirs if d == active_org]
        dirs[:] = [
            d
            for d in dirs
            if d not in EXCLUDED_SKILL_DIRS
            and not (has_skill_md and d in SKILL_SUPPORT_DIRS)
        ]
        if filename in files:
            matches.append(os.path.join(root, filename))
    for path in sorted(matches):
        yield Path(path)


# ── Namespace helpers for plugin-provided skills ───────────────────────────

_NAMESPACE_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def parse_qualified_name(name: str) -> Tuple[Optional[str], str]:
    """Split ``'namespace:skill-name'`` into ``(namespace, bare_name)``.

    Returns ``(None, name)`` when there is no ``':'``.
    """
    if ":" not in name:
        return None, name
    return tuple(name.split(":", 1))  # type: ignore[return-value]


def is_valid_namespace(candidate: Optional[str]) -> bool:
    """Check whether *candidate* is a valid namespace (``[a-zA-Z0-9_-]+``)."""
    if not candidate:
        return False
    return bool(_NAMESPACE_RE.match(candidate))
