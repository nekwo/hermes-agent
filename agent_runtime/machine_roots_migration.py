"""Rewrite machine-local absolute paths in profile configs into token form.

This is the one-way trip from "this config only works on Tony's Windows box"
to the portable ``${roots.<name>}`` / ``${exe_suffix}`` grammar owned by
:mod:`agent_runtime.machine_roots`. It is deliberately a TEXT rewrite, not a
YAML round-trip: the live profile configs carry hand-written comments and
section banners that ``yaml.safe_dump`` would erase.

Safety comes from verification rather than from trusting the rewrite: every
planned file is re-parsed, its tokens are expanded back with the same registry,
and the result is compared structurally against the original document. A plan
whose verification is non-empty is reported as unsafe and is never written.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

import yaml

from .machine_roots import (
    EXE_SUFFIX_TOKEN,
    MachineRoots,
    expand_config_paths,
    write_machine_roots,
)

# ``platforms:`` is the declarative gate this migration adds to entries that
# genuinely cannot run anywhere but Windows.
_WINDOWS_ONLY_MARKERS = (".ps1", "powershell.exe", "powershell ", "pwsh.exe")

_EXE_AFTER_TOKEN_RE = re.compile(
    r"(\$\{roots\.[A-Za-z0-9_]+\}[^\s'\"`;|&<>,()$]*?)\.exe(?=$|[\s'\"`;|&<>,()])"
)

_SNAKE_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


@dataclass(frozen=True, slots=True)
class ConfigMigration:
    """A planned rewrite of one config file."""

    path: Path
    before: str
    after: str
    replacements: int = 0
    platform_gates: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.after != self.before

    @property
    def safe(self) -> bool:
        return not self.verification

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"a/{self.path.name}",
                tofile=f"b/{self.path.name}",
                n=2,
            )
        )

    def row(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "changed": self.changed,
            "safe": self.safe,
            "replacements": self.replacements,
            "platform_gates": list(self.platform_gates),
            "verification": list(self.verification),
            "diff": self.diff(),
        }


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    roots: dict[str, str] = dataclass_field(default_factory=dict)
    files: tuple[ConfigMigration, ...] = ()
    registry: dict[str, Any] = dataclass_field(default_factory=dict)

    @property
    def safe(self) -> bool:
        return all(item.safe for item in self.files)

    def row(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "machine_roots_migration",
            "safe": self.safe,
            "roots": dict(sorted(self.roots.items())),
            "registry": dict(self.registry),
            "files": [item.row() for item in self.files],
        }


# ── Root discovery ──────────────────────────────────────────────────────────


def snake_case_root_name(text: str) -> str:
    """``EterniaLauncher`` -> ``eternia_launcher``; ``eternia-backend`` -> ``eternia_backend``."""

    spaced = _SNAKE_BOUNDARY_RE.sub("_", str(text or ""))
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", spaced).strip("_").lower()
    return re.sub(r"_+", "_", cleaned) or "root"


def suggest_roots_from_configs(config_paths: list[Path]) -> dict[str, str]:
    """Derive logical roots from the absolute paths the configs already contain.

    For every absolute path found in a config, walk UP to the nearest ancestor
    that holds a ``.git`` entry — that ancestor is a real checkout root on this
    machine, and its directory name becomes the logical root name. Nothing is
    invented: a path with no ``.git`` ancestor yields no suggestion, and the
    caller sees exactly which paths went unmapped.
    """

    found: dict[str, str] = {}
    for config_path in config_paths:
        try:
            text = Path(config_path).read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in _absolute_paths_in(text):
            repo = _nearest_repo_root(Path(raw))
            if repo is None:
                continue
            name = snake_case_root_name(repo.name)
            found.setdefault(name, str(repo))
    return found


def unmapped_absolute_paths(config_paths: list[Path], roots: MachineRoots) -> list[str]:
    """Absolute paths in the configs that no bound root covers (honest residue)."""

    patterns = [(_root_pattern(Path(value)), name) for name, value in roots.roots.items()]
    residue: list[str] = []
    for config_path in config_paths:
        try:
            text = Path(config_path).read_text(encoding="utf-8")
        except OSError:
            continue
        for raw in _absolute_paths_in(text):
            if any(pattern.match(raw) for pattern, _name in patterns):
                continue
            if raw not in residue:
                residue.append(raw)
    return residue


# Roots routinely contain SPACES, so a whitespace-terminated pattern silently
# truncates them and the migration finds nothing to do. That is true of the
# drive-letter shape ("X:\\Unreal Engine\\...") and EQUALLY true of the POSIX
# shape: a macOS checkout under "/Users/tony/My Projects/..." is the ordinary
# case on the second machine, and a segment class that stopped at the space
# discovered "/Users/tony/My" — a directory that does not exist, so
# `_nearest_repo_root` walked up from the wrong place and returned nothing.
# Both alternatives therefore tolerate internal spaces:
#   * the drive-letter branch runs to the first YAML/shell delimiter;
#   * the POSIX branch keeps its two-segment structure (so one slash in prose
#     is not a path) and lets a space appear INSIDE a segment but never as the
#     character that OPENS one — which is what keeps "budget / diff / stop"
#     from reading as a rooted path. `_absolute_paths_in` strips the ends.
# The leading lookbehinds keep a URL scheme ("https://", "docker://") from being
# read as a one-letter drive or as a rooted POSIX path.
_ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\r\n'\";|]*"
    r"|(?<![\w.:/\\])/(?:[A-Za-z0-9_.\-][A-Za-z0-9_.\- ]*/)+"
    r"(?:[A-Za-z0-9_.\-][A-Za-z0-9_.\- ]*)?"
)


def _absolute_paths_in(text: str) -> list[str]:
    seen: list[str] = []
    for match in _ABS_PATH_RE.finditer(text):
        raw = match.group(0).strip().rstrip("\\/")
        if raw and raw not in seen:
            seen.append(raw)
    return seen


def _nearest_repo_root(path: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    for node in [candidate, *candidate.parents]:
        try:
            if (node / ".git").exists():
                return node
        except OSError:
            return None
    return None


# ── Planning ────────────────────────────────────────────────────────────────


def plan_config_migration(
    config_paths: list[Path],
    roots: MachineRoots,
    *,
    add_platform_gates: bool = True,
    registry_path: Path | None = None,
) -> MigrationPlan:
    """Build the full rewrite plan. Reads only; never writes."""

    files: list[ConfigMigration] = []
    for config_path in config_paths:
        files.append(
            _plan_one(Path(config_path), roots, add_platform_gates=add_platform_gates)
        )
    registry = write_machine_roots(roots.roots, dry_run=True, path=registry_path)
    return MigrationPlan(roots=dict(roots.roots), files=tuple(files), registry=registry)


def apply_config_migration(plan: MigrationPlan, *, dry_run: bool, registry_path: Path | None = None) -> dict[str, Any]:
    """Apply a plan. ``dry_run=True`` writes nothing — not the configs, not the registry.

    The dry-run guard lives HERE (and in ``write_machine_roots``) rather than in
    the CLI handler, so a verb that forgets to thread ``args.dry_run`` cannot
    quietly mutate on a preview.
    """

    result: dict[str, Any] = {"dry_run": bool(dry_run), "safe": plan.safe, "written": [], "skipped": []}
    if not plan.safe:
        result["skipped"] = [str(item.path) for item in plan.files if not item.safe]
        result["error"] = "migration_verification_failed"
        return result
    result["registry"] = write_machine_roots(plan.roots, dry_run=dry_run, path=registry_path)
    for item in plan.files:
        if not item.changed:
            result["skipped"].append(str(item.path))
            continue
        if dry_run:
            continue
        item.path.write_text(item.after, encoding="utf-8", newline="")
        result["written"].append(str(item.path))
    return result


def _plan_one(config_path: Path, roots: MachineRoots, *, add_platform_gates: bool) -> ConfigMigration:
    try:
        before = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ConfigMigration(
            path=config_path,
            before="",
            after="",
            verification=(f"unreadable: {type(exc).__name__}",),
        )

    after, replacements = tokenize_text(before, roots)
    gates: tuple[str, ...] = ()
    if add_platform_gates and after != before:
        after, gates = _apply_platform_gates(after)
    verification = tuple(verify_roundtrip(before, after, roots, expected_gates=gates))
    return ConfigMigration(
        path=config_path,
        before=before,
        after=after,
        replacements=replacements,
        platform_gates=gates,
        verification=verification,
    )


def tokenize_text(text: str, roots: MachineRoots) -> tuple[str, int]:
    """Replace bound absolute roots with ``${roots.<name>}`` + ``${exe_suffix}``."""

    replacements = 0
    ordered = sorted(roots.roots.items(), key=lambda item: len(item[1]), reverse=True)
    for name, value in ordered:
        pattern = _root_pattern(Path(value))
        text, count = pattern.subn(f"${{roots.{name}}}", text)
        replacements += count
    text, exe_count = _EXE_AFTER_TOKEN_RE.subn(rf"\1{EXE_SUFFIX_TOKEN}", text)
    return text, replacements + exe_count


def _root_pattern(root: Path) -> re.Pattern[str]:
    """Match one root path with either separator style, case-insensitively.

    Windows paths are case-insensitive and the same root appears in the live
    configs in BOTH separator styles (``X:\\Unreal Engine\\...`` in
    ``mcp_servers``, ``X:/Unreal Engine/...`` in ``repo_scope``). One pattern
    has to catch both or the migration silently leaves half the refs behind.
    """

    parts = [part for part in re.split(r"[\\/]+", str(root)) if part]
    body = r"[\\/]+".join(re.escape(part) for part in parts)
    return re.compile(body, re.IGNORECASE)


# ── Platform gates ──────────────────────────────────────────────────────────


def _apply_platform_gates(text: str) -> tuple[str, tuple[str, ...]]:
    """Add ``platforms: [windows]`` to mcp_servers entries that are Windows-only.

    "Windows-only" here is evidence-based, not a guess: the entry's own command
    or env values invoke a ``.ps1`` script or ``powershell``/``pwsh``. Those
    cannot run on macOS/Linux at all, so declaring the gate is what lets a
    non-Windows member be told the capability is unavailable instead of being
    handed a binding that can only fail at spawn time.
    """

    try:
        document = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return text, ()
    servers = document.get("mcp_servers") if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        return text, ()

    windows_only = [
        str(name)
        for name, cfg in servers.items()
        if isinstance(cfg, dict) and "platforms" not in cfg and _is_windows_only(cfg)
    ]
    if not windows_only:
        return text, ()

    lines = text.splitlines(keepends=True)
    block = _block_bounds(lines, "mcp_servers:")
    if block is None:
        return text, ()
    start, end = block
    out: list[str] = list(lines[:start])
    applied: list[str] = []
    index = start
    while index < end:
        line = lines[index]
        out.append(line)
        name = _entry_key(line)
        if name in windows_only and name not in applied:
            child_indent = _child_indent(lines, index, end)
            out.append(f"{child_indent}platforms:\n")
            out.append(f"{child_indent}  - windows\n")
            applied.append(name)
        index += 1
    out.extend(lines[end:])
    return "".join(out), tuple(applied)


def _is_windows_only(cfg: dict[str, Any]) -> bool:
    haystack = " ".join(_flatten_strings(cfg)).lower()
    return any(marker in haystack for marker in _WINDOWS_ONLY_MARKERS)


def _flatten_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _flatten_strings(child)]
    if isinstance(value, (list, tuple)):
        return [item for child in value for item in _flatten_strings(child)]
    return []


def _block_bounds(lines: list[str], header: str) -> tuple[int, int] | None:
    """[start, end) line span of the body under a top-level ``header:`` line."""

    for index, line in enumerate(lines):
        if line.rstrip("\r\n") != header.rstrip():
            continue
        end = index + 1
        while end < len(lines):
            candidate = lines[end]
            if candidate.strip() and not candidate[:1].isspace():
                break
            end += 1
        return index + 1, end
    return None


def _entry_key(line: str) -> str | None:
    match = re.match(r"^(\s+)([A-Za-z0-9_\-]+):\s*(#.*)?$", line.rstrip("\r\n"))
    if not match:
        return None
    return match.group(2)


def _child_indent(lines: list[str], key_index: int, end: int) -> str:
    """Indent of the entry's children, read from the file rather than assumed."""

    key_indent = len(lines[key_index]) - len(lines[key_index].lstrip())
    for index in range(key_index + 1, end):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(lines[index]) - len(lines[index].lstrip())
        if indent > key_indent:
            return " " * indent
        break
    return " " * (key_indent + 2)


# ── Verification ────────────────────────────────────────────────────────────


def verify_roundtrip(
    before: str,
    after: str,
    roots: MachineRoots,
    *,
    expected_gates: tuple[str, ...] = (),
) -> list[str]:
    """Prove the rewrite is meaning-preserving; empty list == verified.

    Re-parses the rewritten YAML, expands its tokens back through the SAME
    resolution chokepoint the runtime uses, and compares structurally against
    the original document. Separator and case differences on path-shaped
    strings are tolerated (``X:/a/b`` and ``X:\\a\\b`` name the same location on
    Windows); anything else is reported.
    """

    if after == before:
        return []
    try:
        before_doc = yaml.safe_load(before) or {}
    except yaml.YAMLError as exc:
        return [f"original YAML did not parse: {type(exc).__name__}"]
    try:
        after_doc = yaml.safe_load(after) or {}
    except yaml.YAMLError as exc:
        return [f"rewritten YAML did not parse: {type(exc).__name__}"]
    expanded = expand_config_paths(after_doc, roots=roots, check_target_exists=False)
    return _diff_docs(before_doc, expanded, path="", expected_gates=set(expected_gates))


def _diff_docs(before: Any, after: Any, *, path: str, expected_gates: set[str]) -> list[str]:
    if isinstance(before, dict) and isinstance(after, dict):
        problems: list[str] = []
        for key in before:
            child = f"{path}.{key}" if path else str(key)
            if key not in after:
                problems.append(f"{child}: dropped by migration")
                continue
            problems.extend(
                _diff_docs(before[key], after[key], path=child, expected_gates=expected_gates)
            )
        for key in after:
            if key in before:
                continue
            child = f"{path}.{key}" if path else str(key)
            if key == "platforms" and _gate_owner(path) in expected_gates:
                continue
            problems.append(f"{child}: added by migration")
        return problems
    if isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            return [f"{path}: list length {len(before)} -> {len(after)}"]
        problems = []
        for index, (lhs, rhs) in enumerate(zip(before, after)):
            problems.extend(
                _diff_docs(lhs, rhs, path=f"{path}[{index}]", expected_gates=expected_gates)
            )
        return problems
    if isinstance(before, str) and isinstance(after, str):
        if before == after or _path_equivalent(before, after):
            return []
        return [f"{path}: {before!r} -> {after!r}"]
    if before != after:
        return [f"{path}: {before!r} -> {after!r}"]
    return []


def _gate_owner(path: str) -> str:
    parts = path.split(".")
    return parts[-1] if parts else ""


def _path_equivalent(lhs: str, rhs: str) -> bool:
    return _normalize_path_text(lhs) == _normalize_path_text(rhs)


def _normalize_path_text(text: str) -> str:
    return re.sub(r"[\\/]+", "/", text).lower()
