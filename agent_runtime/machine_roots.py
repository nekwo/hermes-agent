"""Machine-local root registry + the single config path-token chokepoint.

Why this exists
---------------
Profile ``config.yaml`` files reference on-disk locations of *other* checkouts —
the EterniaLauncher repo, the backend repo, a helper script inside one of them.
Stored as absolute paths (``X:\\Unreal Engine\\...``) those entries are unusable
on a second machine and on any non-Windows OS. Storing them *relative to the
Hermes install* does not help: the relative distance between the Hermes home and
an unrelated checkout is MORE machine-specific, not less.

The portable shape is the split the harness already uses for MCP capability vs.
MCP binding (persona ``required_mcp_servers`` is the portable capability;
``mcp_servers.<name>.command`` is the machine binding):

* the CONFIG names a LOGICAL root plus a repo-relative tail —
  ``${roots.eternia_launcher}/tool/.../stagec_qa_mcp_server${exe_suffix}``.
  That text is byte-identical on every machine and is what realm sync carries.
* THIS MACHINE binds each logical root to an absolute local path in
  ``machine_roots.json`` — a registry that is never synced (it is hard-excluded
  from realm sync exactly like ``auth.json`` and ``state.db``).

Guarantees
----------
* **One expansion chokepoint.** :func:`expand_config_paths` and
  :func:`path_token_issues` are thin wrappers over a single private walker;
  nothing else in the tree expands these tokens.
* **Correct separators everywhere.** Tails are split on ``/`` *and* ``\\`` and
  re-joined with :meth:`pathlib.Path.joinpath`, never by concatenating a
  hardcoded separator.
* **Never a fabricated path.** An unbound root, or a root bound to a path that
  does not exist, produces a typed :class:`PathTokenIssue` that rides the
  existing readiness/preflight lanes. There is no default root, no "best
  effort" join, and no silent drop.
* **Backward compatible.** A value with no ``${roots.…}`` / ``${exe_suffix}``
  token is returned unchanged — expansion is a no-op for every plain absolute
  path already in a config today.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root, get_hermes_home

logger = logging.getLogger(__name__)


MACHINE_ROOTS_FILENAME = "machine_roots.json"
MACHINE_ROOTS_SCHEMA_VERSION = 1

# ``${roots.<name>}`` — the logical-root token.
ROOT_TOKEN_RE = re.compile(r"\$\{roots\.([^}]*)\}")
# ``${exe_suffix}`` — ``.exe`` on Windows, empty elsewhere.
EXE_SUFFIX_TOKEN = "${exe_suffix}"
_ROOT_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

# A ``${roots.X}`` token is followed by a path tail that we must re-join with
# native separators. The tail ends at the first character that cannot belong to
# a path *in a config value* — whitespace, a quote, a shell metacharacter, or
# the start of another token. This is what lets a root token sit inside a
# larger command string (``Set-Location '${roots.x}'; dart mcp-server``) and
# still have only its path part normalized.
_TAIL_TERMINATORS = frozenset(" \t\r\n'\"`;|&<>,()$")

ISSUE_UNBOUND_ROOT = "unbound_root"
ISSUE_ROOT_TARGET_MISSING = "root_target_missing"
ISSUE_INVALID_ROOT_TOKEN = "invalid_root_token"
ISSUE_INVALID_REGISTRY = "invalid_registry"
ISSUE_PLATFORM_UNSUPPORTED = "platform_unsupported"

PLATFORM_WINDOWS = "windows"
PLATFORM_MACOS = "macos"
PLATFORM_LINUX = "linux"

_PLATFORM_ALIASES = {
    "windows": PLATFORM_WINDOWS,
    "win": PLATFORM_WINDOWS,
    "win32": PLATFORM_WINDOWS,
    "nt": PLATFORM_WINDOWS,
    "macos": PLATFORM_MACOS,
    "mac": PLATFORM_MACOS,
    "osx": PLATFORM_MACOS,
    "darwin": PLATFORM_MACOS,
    "linux": PLATFORM_LINUX,
    "posix": PLATFORM_LINUX,
}


@dataclass(frozen=True, slots=True)
class PathTokenIssue:
    """One typed reason a config path field could not be resolved."""

    code: str
    summary: str
    fix_hint: str
    field: str = ""
    root_name: str = ""

    def row(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "field": self.field,
            "root_name": self.root_name,
            "summary": self.summary,
            "fix_hint": self.fix_hint,
        }


class MachineRootError(RuntimeError):
    """Raised by the strict expansion form when a path token cannot resolve."""

    def __init__(self, issues: list[PathTokenIssue]):
        self.issues = list(issues)
        super().__init__("; ".join(issue.summary for issue in self.issues) or "path token unresolved")

    @property
    def code(self) -> str:
        return self.issues[0].code if self.issues else ISSUE_UNBOUND_ROOT

    @property
    def summary(self) -> str:
        return "; ".join(issue.summary for issue in self.issues) or "path token unresolved"

    @property
    def fix_hint(self) -> str:
        return self.issues[0].fix_hint if self.issues else ""

    def rows(self) -> list[dict[str, Any]]:
        return [issue.row() for issue in self.issues]


@dataclass(frozen=True, slots=True)
class MachineRoots:
    """The resolved logical-root -> absolute-local-path bindings for this machine."""

    roots: dict[str, str] = dataclass_field(default_factory=dict)
    sources: tuple[str, ...] = ()
    issues: tuple[PathTokenIssue, ...] = ()

    def get(self, name: str) -> Path | None:
        raw = self.roots.get(name)
        return Path(raw) if raw else None

    def names(self) -> list[str]:
        return sorted(self.roots)

    def row(self) -> dict[str, Any]:
        return {
            "schema_version": MACHINE_ROOTS_SCHEMA_VERSION,
            "roots": {name: self.roots[name] for name in sorted(self.roots)},
            "sources": list(self.sources),
            "issues": [issue.row() for issue in self.issues],
        }


# ── Platform ────────────────────────────────────────────────────────────────


def current_platform_key() -> str:
    """This process's platform, normalized to the config vocabulary."""

    platform = sys.platform
    if platform.startswith("win"):
        return PLATFORM_WINDOWS
    if platform == "darwin":
        return PLATFORM_MACOS
    return PLATFORM_LINUX


def exe_suffix() -> str:
    """Executable suffix for this platform (``.exe`` on Windows, else empty)."""

    return ".exe" if current_platform_key() == PLATFORM_WINDOWS else ""


def normalize_platforms(platforms: Any) -> list[str]:
    if platforms is None:
        return []
    if isinstance(platforms, str):
        raw: Iterable[Any] = [platforms]
    elif isinstance(platforms, (list, tuple, set, frozenset)):
        raw = platforms
    else:
        return []
    normalized: list[str] = []
    for item in raw:
        key = _PLATFORM_ALIASES.get(str(item or "").strip().lower())
        if key and key not in normalized:
            normalized.append(key)
    return normalized


def platform_supported(platforms: Any) -> bool:
    """True when an entry declaring ``platforms`` may run on this machine.

    An entry with no declaration is platform-agnostic and always supported —
    that keeps every config written before this feature working unchanged.
    """

    declared = normalize_platforms(platforms)
    if not declared:
        return True
    return current_platform_key() in declared


# ── Registry ────────────────────────────────────────────────────────────────


def machine_roots_registry_paths() -> tuple[Path, ...]:
    """Registry lookup order: machine-wide first, then a per-profile override.

    The machine-wide file lives beside the profiles directory because the
    bindings describe THIS MACHINE, not one profile's preferences — every
    profile on the box resolves ``${roots.eternia_launcher}`` to the same
    checkout. A per-profile file is still honoured (per-key override) so a
    profile can be pointed at a different checkout without disturbing the rest.
    """

    paths: list[Path] = []
    for candidate in (get_default_hermes_root() / MACHINE_ROOTS_FILENAME, get_hermes_home() / MACHINE_ROOTS_FILENAME):
        if candidate not in paths:
            paths.append(candidate)
    return tuple(paths)


_roots_cache: dict[tuple[Any, ...], MachineRoots] = {}


def machine_roots_cache_clear() -> None:
    """Test hook — drop the registry read memo."""

    _roots_cache.clear()


def _registry_stamp(path: Path) -> tuple[Any, ...]:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), None, None)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def load_machine_roots(*, refresh: bool = False) -> MachineRoots:
    """Read the machine-root registry (memoized on file mtime/size)."""

    paths = machine_roots_registry_paths()
    key = tuple(_registry_stamp(path) for path in paths)
    if not refresh:
        cached = _roots_cache.get(key)
        if cached is not None:
            return cached
    roots: dict[str, str] = {}
    sources: list[str] = []
    issues: list[PathTokenIssue] = []
    for path in paths:
        parsed, parse_issues = _read_registry_file(path)
        issues.extend(parse_issues)
        if parsed is None:
            continue
        sources.append(str(path))
        roots.update(parsed)
    resolved = MachineRoots(roots=roots, sources=tuple(sources), issues=tuple(issues))
    _roots_cache[key] = resolved
    return resolved


def _read_registry_file(path: Path) -> tuple[dict[str, str] | None, list[PathTokenIssue]]:
    if not path.is_file():
        return None, []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, [
            PathTokenIssue(
                code=ISSUE_INVALID_REGISTRY,
                summary=f"Machine root registry is unreadable ({type(exc).__name__}): {path}",
                fix_hint=f"Repair or delete {path}, then re-run `hermes harness roots list`.",
            )
        ]
    node = payload.get("roots") if isinstance(payload, dict) else None
    if not isinstance(node, dict):
        return None, [
            PathTokenIssue(
                code=ISSUE_INVALID_REGISTRY,
                summary=f"Machine root registry has no 'roots' object: {path}",
                fix_hint=f"Rewrite {path} as {{\"schema_version\": 1, \"roots\": {{...}}}}.",
            )
        ]
    parsed: dict[str, str] = {}
    issues: list[PathTokenIssue] = []
    for name, value in node.items():
        clean = str(name or "").strip()
        text = str(value or "").strip()
        if not _ROOT_NAME_RE.match(clean):
            issues.append(
                PathTokenIssue(
                    code=ISSUE_INVALID_REGISTRY,
                    root_name=clean,
                    summary=f"Machine root name '{clean}' is not [A-Za-z0-9_]+ ({path})",
                    fix_hint="Rename the root to letters, digits, and underscores only.",
                )
            )
            continue
        if not text:
            issues.append(
                PathTokenIssue(
                    code=ISSUE_INVALID_REGISTRY,
                    root_name=clean,
                    summary=f"Machine root '{clean}' is bound to an empty path ({path})",
                    fix_hint=f"hermes harness roots set {clean} <absolute-path> --yes",
                )
            )
            continue
        parsed[clean] = text
    return parsed, issues


def write_machine_roots(
    roots: Mapping[str, str],
    *,
    dry_run: bool,
    path: Path | None = None,
) -> dict[str, Any]:
    """THE single write site for the registry. ``dry_run`` never touches disk.

    ``_add_stage42_global_args(mutation=True)`` auto-registers ``--dry-run`` for
    every mutation verb, and a verb that forgets to READ it silently mutates on
    a preview. Honouring it here — at the store chokepoint rather than in the
    CLI handler — makes that failure mode unreachable.
    """

    target = Path(path) if path is not None else machine_roots_registry_paths()[0]
    payload = {
        "schema_version": MACHINE_ROOTS_SCHEMA_VERSION,
        "roots": {str(name): str(value) for name, value in sorted(roots.items())},
    }
    after = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    try:
        before = target.read_text(encoding="utf-8") if target.is_file() else ""
    except OSError:
        before = ""
    changed = after != before
    result: dict[str, Any] = {
        "path": str(target),
        "dry_run": bool(dry_run),
        "changed": changed,
        "roots": payload["roots"],
    }
    if dry_run or not changed:
        result["written"] = False
        return result
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(after, encoding="utf-8")
    os.replace(tmp, target)
    machine_roots_cache_clear()
    result["written"] = True
    return result


# ── Expansion (the single chokepoint) ───────────────────────────────────────


def contains_path_tokens(value: Any) -> bool:
    """True when ``value`` (str/dict/list) carries any token this module owns."""

    if isinstance(value, str):
        return EXE_SUFFIX_TOKEN in value or bool(ROOT_TOKEN_RE.search(value))
    if isinstance(value, Mapping):
        return any(contains_path_tokens(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(contains_path_tokens(item) for item in value)
    return False


def expand_config_paths(
    value: Any,
    *,
    field: str = "",
    roots: MachineRoots | None = None,
    check_target_exists: bool = True,
) -> Any:
    """Expand path tokens, raising :class:`MachineRootError` on any problem.

    Use this where a dead path must never reach a caller (spawning a process,
    handing a workdir to a subagent). Use :func:`path_token_issues` where the
    caller must report rather than raise (readiness).
    """

    issues: list[PathTokenIssue] = []
    expanded = _walk(
        value,
        field=field,
        roots=roots if roots is not None else load_machine_roots(),
        issues=issues,
        check_target_exists=check_target_exists,
    )
    if issues:
        raise MachineRootError(issues)
    return expanded


def path_token_issues(
    value: Any,
    *,
    field: str = "",
    roots: MachineRoots | None = None,
    check_target_exists: bool = True,
) -> list[PathTokenIssue]:
    """Every typed reason ``value``'s path tokens cannot resolve. Never raises."""

    issues: list[PathTokenIssue] = []
    _walk(
        value,
        field=field,
        roots=roots if roots is not None else load_machine_roots(),
        issues=issues,
        check_target_exists=check_target_exists,
    )
    return issues


def _walk(
    value: Any,
    *,
    field: str,
    roots: MachineRoots,
    issues: list[PathTokenIssue],
    check_target_exists: bool,
) -> Any:
    if isinstance(value, str):
        return _expand_text(
            value, field=field, roots=roots, issues=issues, check_target_exists=check_target_exists
        )
    if isinstance(value, Mapping):
        return {
            key: _walk(
                item,
                field=f"{field}.{key}" if field else str(key),
                roots=roots,
                issues=issues,
                check_target_exists=check_target_exists,
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _walk(
                item,
                field=f"{field}[{index}]" if field else f"[{index}]",
                roots=roots,
                issues=issues,
                check_target_exists=check_target_exists,
            )
            for index, item in enumerate(value)
        ]
    return value


def _expand_text(
    text: str,
    *,
    field: str,
    roots: MachineRoots,
    issues: list[PathTokenIssue],
    check_target_exists: bool,
) -> str:
    if "${" not in text:
        return text
    # ``${exe_suffix}`` first: it is a bare suffix with no separators, so once
    # substituted it is just the tail's last path component.
    text = text.replace(EXE_SUFFIX_TOKEN, exe_suffix())
    if not ROOT_TOKEN_RE.search(text):
        return text

    out: list[str] = []
    cursor = 0
    for match in ROOT_TOKEN_RE.finditer(text):
        out.append(text[cursor:match.start()])
        tail_end = _tail_end(text, match.end())
        tail = text[match.end():tail_end]
        resolved = _resolve_root(
            match.group(1).strip(),
            field=field,
            roots=roots,
            issues=issues,
            check_target_exists=check_target_exists,
        )
        if resolved is None:
            # Unresolved: keep the literal token + tail. Callers that must not
            # see a dead path use the raising form (or drop the entry); the
            # typed issue is already recorded either way.
            out.append(text[match.start():tail_end])
        else:
            out.append(_join_tail(resolved, tail))
        cursor = tail_end
    out.append(text[cursor:])
    return "".join(out)


def _tail_end(text: str, start: int) -> int:
    index = start
    while index < len(text) and text[index] not in _TAIL_TERMINATORS:
        index += 1
    return index


def _join_tail(root: Path, tail: str) -> str:
    """Join ``tail`` onto ``root`` with native separators.

    The tail is split on BOTH separator styles so a config authored on Windows
    (``\\tool\\build``) and one authored on macOS (``/tool/build``) resolve
    identically, and re-joined through ``Path.joinpath`` so the emitted string
    uses whatever separator this OS actually wants. Nothing here concatenates a
    hardcoded separator.
    """

    segments = [segment for segment in re.split(r"[\\/]+", tail) if segment]
    return str(root.joinpath(*segments)) if segments else str(root)


def _resolve_root(
    name: str,
    *,
    field: str,
    roots: MachineRoots,
    issues: list[PathTokenIssue],
    check_target_exists: bool,
) -> Path | None:
    where = f" (field {field})" if field else ""
    if not _ROOT_NAME_RE.match(name):
        issues.append(
            PathTokenIssue(
                code=ISSUE_INVALID_ROOT_TOKEN,
                root_name=name,
                field=field,
                summary=f"Malformed machine-root token '${{roots.{name}}}'{where}",
                fix_hint="Root names must be [A-Za-z0-9_]+ — e.g. ${roots.eternia_launcher}.",
            )
        )
        return None
    bound = roots.roots.get(name)
    if not bound:
        known = ", ".join(roots.names()) or "(none bound on this machine)"
        issues.append(
            PathTokenIssue(
                code=ISSUE_UNBOUND_ROOT,
                root_name=name,
                field=field,
                summary=f"Machine root '{name}' is not bound on this machine{where}. Bound roots: {known}",
                fix_hint=f"hermes harness roots set {name} <absolute-local-path> --yes",
            )
        )
        return None
    path = Path(bound)
    if check_target_exists and not path.exists():
        issues.append(
            PathTokenIssue(
                code=ISSUE_ROOT_TARGET_MISSING,
                root_name=name,
                field=field,
                summary=f"Machine root '{name}' is bound to a path that does not exist: {bound}{where}",
                fix_hint=f"hermes harness roots set {name} <absolute-local-path> --yes",
            )
        )
        return None
    return path


# ── MCP server resolution ───────────────────────────────────────────────────


def resolve_mcp_servers(
    servers: Mapping[str, Any],
    *,
    roots: MachineRoots | None = None,
    on_issue: Callable[[str, PathTokenIssue], None] | None = None,
    check_target_exists: bool = True,
) -> dict[str, Any]:
    """Expand path tokens and apply the platform gate over an ``mcp_servers`` map.

    A server whose declared ``platforms`` excludes this OS, or whose path tokens
    cannot resolve, is DROPPED rather than handed to a spawn with a dead path —
    "the capability is unavailable" is the honest answer, and readiness/preflight
    carry the typed reason. Servers with no tokens pass through byte-identical.
    """

    resolved_roots = roots if roots is not None else load_machine_roots()
    report = on_issue if on_issue is not None else _log_issue
    out: dict[str, Any] = {}
    for name, cfg in servers.items():
        if not isinstance(cfg, Mapping):
            out[name] = cfg
            continue
        entry = {key: value for key, value in cfg.items() if key != "platforms"}
        if not platform_supported(cfg.get("platforms")):
            report(
                str(name),
                PathTokenIssue(
                    code=ISSUE_PLATFORM_UNSUPPORTED,
                    field=f"mcp_servers.{name}",
                    summary=(
                        f"MCP server '{name}' declares platforms "
                        f"{normalize_platforms(cfg.get('platforms'))}; this machine is "
                        f"{current_platform_key()}"
                    ),
                    fix_hint=f"Provide a {current_platform_key()} binding for '{name}', or use a different capability.",
                ),
            )
            continue
        issues: list[PathTokenIssue] = []
        expanded = _walk(
            entry,
            field=f"mcp_servers.{name}",
            roots=resolved_roots,
            issues=issues,
            check_target_exists=check_target_exists,
        )
        if issues:
            for issue in issues:
                report(str(name), issue)
            continue
        out[name] = expanded
    return out


def _log_issue(name: str, issue: PathTokenIssue) -> None:
    logger.error(
        "MCP server '%s' is unavailable (%s): %s | fix: %s",
        name,
        issue.code,
        issue.summary,
        issue.fix_hint,
    )


def mcp_server_issues(
    servers: Mapping[str, Any],
    *,
    only: Iterable[str] | None = None,
    roots: MachineRoots | None = None,
    check_target_exists: bool = True,
) -> list[PathTokenIssue]:
    """Typed reasons the named MCP servers cannot bind on this machine."""

    wanted = {str(item) for item in only} if only is not None else None
    collected: list[PathTokenIssue] = []

    def _collect(_name: str, issue: PathTokenIssue) -> None:
        collected.append(issue)

    subset = {
        name: cfg
        for name, cfg in servers.items()
        if wanted is None or str(name) in wanted
    }
    resolve_mcp_servers(
        subset,
        roots=roots,
        on_issue=_collect,
        check_target_exists=check_target_exists,
    )
    return collected
