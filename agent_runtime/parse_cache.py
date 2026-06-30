"""Process-wide, mtime-keyed parse cache for hot, idempotent file loads.

Snapshot builds resolve every persona's profile/skill/config state, and each
persona summary re-resolves the same profile + skill tree several times (readiness
+ four tool-visibility passes). Those leaf loads — YAML config/meta files, skill
frontmatter, file hashes — are pure functions of file content, so re-parsing them
dozens of times per build is wasted work (profiled as the dominant snapshot cost:
YAML scanning ≫ everything else).

This caches by ``(path, mtime_ns, size)`` so a file edit invalidates the entry
(safe in a long-lived daemon) while repeats within a build are free. Caches are
bounded and self-clearing.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable

_MISSING = object()
_MAX_ENTRIES = 4096

_value_cache: dict[tuple, Any] = {}
_sha_cache: dict[tuple, str] = {}


def _stamp(path: Path) -> tuple | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)


def _bounded_set(cache: dict, key: tuple, value: Any) -> None:
    if len(cache) >= _MAX_ENTRIES:
        cache.clear()
    cache[key] = value


def cached_by_mtime(path: Path, loader: Callable[[Path], Any], *, default: Any = None) -> Any:
    """Return ``loader(path)``, memoized by the file's identity+mtime.

    On a missing file or loader error, returns ``default`` (and does not cache,
    so a transient error self-heals on the next call).
    """

    key = _stamp(path)
    if key is None:
        return default
    cached = _value_cache.get(key, _MISSING)
    if cached is not _MISSING:
        return cached
    try:
        value = loader(path)
    except Exception:
        return default
    _bounded_set(_value_cache, key, value)
    return value


def cached_yaml_file(path: Path, *, default: Any = None) -> Any:
    """mtime-cached ``yaml.safe_load`` of a file. Returns ``default`` if absent/bad."""

    def _load(p: Path) -> Any:
        import yaml

        return yaml.safe_load(p.read_text(encoding="utf-8"))

    return cached_by_mtime(path, _load, default=default)


def cached_file_sha256(path: Path) -> str:
    """mtime-cached SHA-256 of a file (same ``sha256:<hex>`` shape as the original).

    Falls back to a direct (uncached) hash when the file cannot be stat'd, so the
    original FileNotFoundError semantics are preserved for callers that don't
    guard existence.
    """

    key = _stamp(path)
    if key is not None:
        hit = _sha_cache.get(key)
        if hit is not None:
            return hit
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    value = "sha256:" + digest.hexdigest()
    if key is not None:
        _bounded_set(_sha_cache, key, value)
    return value


def clear_parse_cache() -> None:
    """Drop all cached entries (tests / explicit invalidation)."""

    _value_cache.clear()
    _sha_cache.clear()
