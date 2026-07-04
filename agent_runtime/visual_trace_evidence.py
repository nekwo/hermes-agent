from __future__ import annotations

import json
import re
import shutil
import struct
import uuid
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
from .events import EventLog
from .models import AgentRun, Event, Proof
from .proof_rules import ProofType
from .store import ProofStore


def record_screenshot_from_progress(
    run: AgentRun,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_log: EventLog | None = None,
) -> Proof | None:
    if event_type != "run.tool.finished":
        return None
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip()
    source_path = _screenshot_path(payload)
    redaction_safe_override = False
    trace_source = "launcher_qa_tool_trace"
    if _is_launcher_qa_screenshot_tool(tool_name):
        pass
    else:
        wrapper = _terminal_wrapper_screenshot(payload, run=run)
        if wrapper is None:
            return None
        tool_name, source_path = wrapper
        redaction_safe_override = True
        trace_source = "launcher_qa_terminal_wrapper_trace"
    if not source_path:
        return None
    relative_path, artifact_exists, artifact_bytes = _materialize_artifact(run.task_id, source_path)
    width, height = _dimensions(payload, paths.store_root() / relative_path)
    redaction_status = "safe" if redaction_safe_override else _redaction_status(payload)
    nonblank = artifact_exists and artifact_bytes > 100
    fullscreen = bool(payload.get("fullscreen")) or (width >= 800 and height >= 450)
    status = "passed" if nonblank and fullscreen and redaction_status == "safe" else "failed"
    proof_id = f"screenshot_trace_{_safe_token(run.task_id)}_{_safe_token(run.stage_id or 'stage')}_{uuid.uuid4().hex[:8]}"
    proof = Proof(
        id=proof_id,
        task_id=run.task_id,
        stage_id=run.stage_id,
        type=ProofType.SCREENSHOT,
        title=f"Observed QA screenshot: {_safe_label(payload.get('label') or payload.get('target') or tool_name)}",
        path_or_value=relative_path,
        created_by=run.persona_id,
        created_at=now(),
        metadata={
            "source": trace_source,
            "authoritative": False,
            "actor_requested": run.persona_id,
            "run_id": run.id,
            "tool_name": tool_name,
            "status": status,
            "artifact_exists": artifact_exists,
            "artifact_bytes": artifact_bytes,
            "nonblank": nonblank,
            "fullscreen": fullscreen,
            "width": width or None,
            "height": height or None,
            "redaction_status": redaction_status,
        },
        redaction_status=redaction_status,
    )
    stored = ProofStore(event_log=event_log).attach(proof)
    (event_log or EventLog()).append(
        Event(
            now(),
            "visual_screenshot.recorded",
            run.task_id,
            run.id,
            run.persona_id,
            {
                "proof_id": stored.id,
                "status": status,
                "stage_id": run.stage_id,
                "tool_name": tool_name,
                "redaction_status": redaction_status,
            },
        )
    )
    return stored


def _is_launcher_qa_screenshot_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return lowered.startswith("mcp_launcher_qa_") and "screenshot" in lowered


def _terminal_wrapper_screenshot(payload: dict[str, Any], *, run: AgentRun | None = None) -> tuple[str, str] | None:
    if str(payload.get("tool_name") or payload.get("tool") or "").strip().lower() != "terminal":
        return None
    if str(payload.get("status") or "").strip().lower() not in {"passed", "ok", "success"}:
        return None
    command = str(payload.get("command_label") or payload.get("command") or payload.get("summary") or "")
    if "Invoke-LauncherQaMcpTool.ps1" not in command:
        return None
    match = re.search(r"-Tool\s+(mcp_launcher_qa_[A-Za-z0-9_]*screenshot[A-Za-z0-9_]*)", command, re.IGNORECASE)
    if match is None:
        return None
    tool_name = match.group(1)
    args = _wrapper_args(command)
    label = str(args.get("label") or _partial_json_string(command, "label") or "").strip()
    out_dir = str(args.get("out_dir") or _partial_json_string(command, "out_dir") or "").strip()
    # Bind the artifact to THIS run's time window so a stale/foreign PNG left in the
    # output directory by an earlier run can't be mtime-globbed in as fake evidence.
    source = _latest_wrapper_artifact(label=label, out_dir=out_dir, min_mtime=_run_min_mtime(run))
    if source is None:
        return None
    return tool_name, str(source)


def _run_min_mtime(run: AgentRun | None) -> float | None:
    """Earliest artifact mtime accepted for a run: its start minus a skew tolerance."""

    started = getattr(run, "started_at", None) if run is not None else None
    if started is None:
        return None
    try:
        epoch = started.timestamp() if hasattr(started, "timestamp") else float(started)
    except (TypeError, ValueError, OverflowError):
        return None
    return epoch - 120.0


def _wrapper_args(command: str) -> dict[str, Any]:
    match = re.search(r"-ArgsJson\s+'([^']+)'", command)
    if match is None:
        match = re.search(r'-ArgsJson\s+"([^"]+)"', command)
    if match is None:
        return {}
    raw = match.group(1)
    try:
        parsed = json.loads(raw.replace('\\"', '"'))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _partial_json_string(command: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)', command)
    if match is None:
        return ""
    return match.group(1).replace("...", "").strip()


def _latest_wrapper_artifact(*, label: str, out_dir: str, min_mtime: float | None = None) -> Path | None:
    roots: list[Path] = []
    if out_dir:
        roots.append(Path(out_dir))
    roots.append(Path("X:/tmp/stagec/screenshots"))
    patterns = [f"{_safe_glob_prefix(label)}*.png"] if label else []
    patterns.append("*.png")
    candidates: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for pattern in patterns:
            for path in root.glob(pattern):
                if not path.is_file():
                    continue
                if min_mtime is not None and path.stat().st_mtime < min_mtime:
                    # Older than this run started — reject as stale/foreign.
                    continue
                candidates.append(path)
        if candidates:
            break
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _screenshot_path(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("path", "screenshot_path", "image_path", "artifact_path"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        for key in ("screenshot", "image", "artifact", "data", "result"):
            found = _screenshot_path(value.get(key))
            if found:
                return found
    return ""


def _materialize_artifact(task_id: str, source_path: str) -> tuple[str, bool, int]:
    source = Path(source_path)
    if not source.is_absolute():
        relative = Path("proofs") / _safe_token(task_id) / "artifacts" / source.name
        candidate = paths.store_root() / source
        if candidate.exists():
            return source.as_posix(), True, candidate.stat().st_size
        return relative.as_posix(), False, 0
    dest = Path("proofs") / _safe_token(task_id) / "artifacts" / source.name
    full_dest = paths.store_root() / dest
    try:
        full_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, full_dest)
        return dest.as_posix(), True, full_dest.stat().st_size
    except OSError:
        return dest.as_posix(), False, 0


def _dimensions(payload: dict[str, Any], artifact: Path) -> tuple[int, int]:
    width = _safe_int(_nested_value(payload, "width"))
    height = _safe_int(_nested_value(payload, "height"))
    if width and height:
        return width, height
    return _png_dimensions(artifact)


def _nested_value(value: Any, key: str) -> Any:
    if not isinstance(value, dict):
        return None
    if key in value:
        return value.get(key)
    for child_key in ("screenshot", "image", "artifact", "data", "result"):
        child = _nested_value(value.get(child_key), key)
        if child is not None:
            return child
    return None


def _png_dimensions(path: Path) -> tuple[int, int]:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return 0, 0
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return 0, 0


def _redaction_status(payload: dict[str, Any]) -> str:
    redaction = payload.get("redaction") if isinstance(payload.get("redaction"), dict) else {}
    raw = str(payload.get("redaction_status") or "").strip().lower()
    if raw == "safe" or redaction.get("safe") is True:
        return "safe"
    return "needs_scan"


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_token(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "").strip()).strip("._:-")
    return text[:80] or "item"


def _safe_label(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text or ":/" in text or "\\" in text:
        return "screenshot"
    return text[:80]


def _safe_glob_prefix(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return text or "*"
