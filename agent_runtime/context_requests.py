from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
from .repo_context import resolve_affected_repo_workdir

MAX_FILE_BYTES = 131_072
MAX_BUNDLE_BYTES = 262_144
MAX_DIRECTORY_ENTRIES = 80
MAX_DIRECTORY_DEPTH = 2
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|secret|token|password|authorization|bearer\s+[a-z0-9._\-]{12,})"
)


def add_context_request(task, *, actor: str, payload: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Create, dedupe, and best-effort fulfill a file-context request.

    Requests remain schema-v1-compatible dictionaries under Task.context_requests.
    The helper stores only bounded file excerpts and rejects unsafe paths/content.
    """

    if getattr(task, "context_requests", None) is None:
        task.context_requests = []
    paths = _clean_paths(payload.get("paths", []))
    paths.extend(_clean_window_paths(payload.get("windows", [])))
    roots = _allowed_roots(task, root=root)
    fingerprint = _fingerprint(actor, paths, roots)
    existing = [req for req in task.context_requests if req.get("fingerprint") == fingerprint]
    if existing:
        req = _new_request(task.id, actor, paths, payload.get("reason"), fingerprint)
        req["status"] = "superseded"
        req["failure_reason"] = "duplicate_context_request"
        req["supersedes_request_id"] = existing[-1].get("id")
        task.context_requests.append(req)
        return req

    req = _new_request(task.id, actor, paths, payload.get("reason"), fingerprint)
    _fulfill_request(req, task=task, root=root, roots=roots)
    task.context_requests.append(req)
    return req


# S29: ``fulfilled_context_bundles`` lived here — it collected the last three
# fulfilled bundles off a task for ``context_builder.build_context`` to pack into
# ``AgentContext.context_bundles``. S27 removed that builder, its only consumer,
# and no other appeared. The bundle it read is still written onto each request by
# ``_fulfill_request`` below; only the collector is gone. See
# tests/agent_runtime/test_s29_context_request_bundle_removal.py.


def has_unresolved_context_request(task) -> bool:
    for req in reversed(getattr(task, "context_requests", []) or []):
        status = req.get("status")
        if status == "open":
            return True
        if status in {"fulfilled", "fulfilled_partial", "unsupported", "superseded"}:
            return False
    return False


def _new_request(task_id: str, actor: str, paths: list[str], reason: Any, fingerprint: str) -> dict[str, Any]:
    return {
        "id": f"ctx_{uuid.uuid4().hex[:8]}",
        "task_id": task_id,
        "actor": actor,
        "paths": paths,
        "reason": str(reason or "")[:500],
        "status": "open",
        "created_at": now(),
        "fulfilled_at": None,
        "bundle_id": None,
        "failure_reason": None,
        "fingerprint": fingerprint,
    }


def _fulfill_request(req: dict[str, Any], *, task=None, root: Path | None = None, roots: list[Path] | None = None) -> None:
    roots = roots or _allowed_roots(task, root=root)
    files: list[dict[str, Any]] = []
    path_results: list[dict[str, Any]] = []
    total = 0
    for raw_path in req.get("paths", []):
        request_path, window = _parse_window_path(raw_path)
        resolved = _resolve_allowed_path(request_path, roots)
        if resolved is None:
            path_results.append(_path_result(raw_path, "unsupported", "path_outside_runtime_root_or_absolute_disallowed"))
            continue
        if not resolved.exists():
            path_results.append(_path_result(raw_path, "unsupported", "path_not_found"))
            continue
        if resolved.is_dir():
            listing = _directory_listing(resolved, raw_path)
            if listing is None:
                path_results.append(_path_result(raw_path, "unsupported", "directory_unreadable"))
                continue
            total += len(listing["content"].encode("utf-8"))
            if total > MAX_BUNDLE_BYTES:
                path_results.append(_path_result(raw_path, "unsupported", "context_bundle_too_large"))
                continue
            files.append(listing)
            path_results.append(_path_result(raw_path, "fulfilled", None))
            continue
        if not resolved.is_file():
            path_results.append(_path_result(raw_path, "unsupported", "path_not_file_or_directory"))
            continue
        try:
            data = resolved.read_bytes()
        except OSError:
            path_results.append(_path_result(raw_path, "unsupported", "path_unreadable"))
            continue
        if b"\x00" in data:
            path_results.append(_path_result(raw_path, "unsupported", "binary_file"))
            continue
        text = data.decode("utf-8", errors="replace")
        if window is not None:
            text, line_meta = _window_text(text, window)
            truncated = bool(line_meta.get("truncated"))
        elif len(data) > MAX_FILE_BYTES:
            skeleton = _file_skeleton(text, raw_path)
            total += len(skeleton["content"].encode("utf-8"))
            if total > MAX_BUNDLE_BYTES:
                path_results.append(_path_result(raw_path, "unsupported", "context_bundle_too_large"))
                continue
            files.append(skeleton)
            path_results.append(_path_result(raw_path, "fulfilled_partial", "file_too_large_use_windows"))
            continue
        else:
            truncated = False
            line_meta = None
        text, masked_count, line_count = _mask_secret_lines(text)
        if line_count and masked_count > line_count / 2:
            path_results.append(_path_result(raw_path, "unsupported", "redaction_risk"))
            continue
        total += len(text.encode("utf-8"))
        if total > MAX_BUNDLE_BYTES:
            path_results.append(_path_result(raw_path, "unsupported", "context_bundle_too_large"))
            continue
        item = {"path": raw_path, "content": text, "truncated": truncated}
        if line_meta:
            item.update(line_meta)
        if masked_count:
            item["masked_line_count"] = masked_count
        files.append(item)
        path_results.append(_path_result(raw_path, "fulfilled", None))
    req["path_results"] = path_results
    if not files:
        req["status"] = "unsupported"
        req["failure_reason"] = next((item.get("failure_reason") for item in path_results if item.get("failure_reason")), "no_readable_paths")
        return
    bundle_id = f"bundle_{hashlib.sha256('|'.join(req.get('paths', [])).encode()).hexdigest()[:12]}"
    failures = [item for item in path_results if item.get("status") != "fulfilled"]
    req["status"] = "fulfilled_partial" if failures else "fulfilled"
    req["failure_reason"] = "partial_context_unavailable" if failures else None
    req["fulfilled_at"] = now()
    req["bundle_id"] = bundle_id
    req["bundle"] = {"bundle_id": bundle_id, "request_id": req.get("id"), "files": files, "path_results": path_results}


def _directory_listing(path: Path, raw_path: str) -> dict[str, Any] | None:
    """Return a bounded, redaction-safe directory index without file contents."""

    entries: list[str] = []
    truncated = False
    try:
        candidates = sorted(
            (child for child in path.rglob("*") if child.is_file()),
            key=lambda item: item.as_posix().lower(),
        )
    except OSError:
        return None

    for child in candidates:
        try:
            relative = child.relative_to(path)
        except ValueError:
            continue
        if len(relative.parts) > MAX_DIRECTORY_DEPTH:
            continue
        display = relative.as_posix()
        if SECRET_PATTERN.search(display):
            continue
        entries.append(display)
        if len(entries) >= MAX_DIRECTORY_ENTRIES:
            truncated = True
            break

    header = [
        f"Directory listing for {raw_path}",
        f"Entries shown: {len(entries)}",
    ]
    if truncated:
        header.append("Truncated: true")
    content = "\n".join(header + [""] + entries)
    return {"path": raw_path, "content": content, "truncated": truncated, "kind": "directory_listing"}


def _path_result(path: str, status: str, failure_reason: str | None) -> dict[str, Any]:
    result = {"path": path, "status": status}
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


def _parse_window_path(raw_path: str) -> tuple[str, dict[str, int] | None]:
    match = re.search(r"#L(\d+)(?:-L?(\d+))?$", raw_path)
    if not match:
        return raw_path, None
    start = max(1, int(match.group(1)))
    end = int(match.group(2) or start)
    return raw_path[: match.start()], {"start_line": start, "max_lines": max(1, end - start + 1)}


def _clean_window_paths(windows: Any) -> list[str]:
    if not isinstance(windows, list):
        return []
    result: list[str] = []
    for item in windows:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().replace("\\", "/")
        if not path:
            continue
        start = max(1, _safe_int(item.get("start_line"), default=1))
        max_lines = min(1000, max(1, _safe_int(item.get("max_lines"), default=200)))
        encoded = f"{path}#L{start}-L{start + max_lines - 1}"
        if encoded not in result:
            result.append(encoded)
    return result[:10]


def _window_text(text: str, window: dict[str, int]) -> tuple[str, dict[str, Any]]:
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(window.get("start_line") or 1))
    max_lines = max(1, int(window.get("max_lines") or 200))
    end = min(total, start + max_lines - 1)
    selected = lines[start - 1 : end]
    numbered = [f"{idx}: {line}" for idx, line in zip(range(start, end + 1), selected)]
    return "\n".join(numbered), {
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "truncated": end < total,
        "kind": "file_window",
    }


def _file_skeleton(text: str, raw_path: str) -> dict[str, Any]:
    lines = text.splitlines()
    interesting: list[str] = []
    pattern = re.compile(r"^\s*(class|def|async\s+def|function|const|let|var|export|interface|type)\b")
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if pattern.search(stripped):
            interesting.append(f"{idx}: {stripped[:220]}")
        if len(interesting) >= 300:
            break
    if not interesting:
        interesting = [f"1: <oversize file: {len(lines)} lines; request windows with path#Lstart-Lend>"]
    content = "\n".join([
        f"Skeleton for {raw_path}",
        f"Total lines: {len(lines)}",
        "Use path#Lstart-Lend or windows[] to request exact excerpts.",
        "",
        *interesting,
    ])
    masked, masked_count, _ = _mask_secret_lines(content)
    return {"path": raw_path, "content": masked, "truncated": True, "kind": "file_skeleton", "masked_line_count": masked_count}


def _mask_secret_lines(text: str) -> tuple[str, int, int]:
    lines = text.splitlines()
    masked: list[str] = []
    count = 0
    for idx, line in enumerate(lines, start=1):
        if SECRET_PATTERN.search(line):
            masked.append(f"<line {idx} redacted>")
            count += 1
        else:
            masked.append(line)
    return "\n".join(masked), count, len(lines)


def _resolve_allowed_path(raw_path: str, roots: list[Path]) -> Path | None:
    if not raw_path or "\x00" in raw_path:
        return None
    candidate = Path(raw_path)
    candidates: list[Path] = []
    for base in roots:
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
        else:
            if any(part == ".." for part in candidate.parts):
                return None
            try:
                resolved = (base / candidate).resolve()
            except OSError:
                continue
        try:
            resolved.relative_to(base)
        except ValueError:
            continue
        candidates.append(resolved)
        if resolved.exists():
            return resolved
    return candidates[0] if candidates else None


def _allowed_roots(task, *, root: Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    if task is not None:
        for value in getattr(task, "affected_repos", []) or []:
            try:
                resolved_repo = resolve_affected_repo_workdir(str(value))
                path = resolved_repo or Path(str(value)).expanduser()
            except (OSError, RuntimeError):
                continue
            if path.exists() and path.is_dir():
                candidates.append(path)
    candidates.extend([root or Path.cwd(), paths.store_root()])
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in roots:
            roots.append(resolved)
    return roots or [Path.cwd().resolve()]


def _clean_paths(paths: Any) -> list[str]:
    if not isinstance(paths, list):
        return []
    cleaned: list[str] = []
    for item in paths:
        text = str(item).strip().replace("\\", "/")
        if text and text not in cleaned:
            cleaned.append(text)
    return cleaned[:10]


def _fingerprint(actor: str, paths: list[str], roots: list[Path] | None = None) -> str:
    root_scope = []
    for root in roots or []:
        try:
            root_scope.append(str(root.resolve()).lower())
        except OSError:
            root_scope.append(str(root).lower())
    raw = actor + "\0" + "\0".join(sorted(paths)) + "\0roots\0" + "\0".join(root_scope)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _safe_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
