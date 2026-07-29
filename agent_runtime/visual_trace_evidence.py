from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
from .events import EventLog
from .models import AgentRun, Event, Proof
from .proof_rules import ProofType
from .stagec_trace_parsers import (
    is_launcher_qa_screenshot_tool as _is_launcher_qa_screenshot_tool,
    png_dimensions as _png_dimensions,
    terminal_wrapper_screenshot as _terminal_wrapper_screenshot,
)
from .store import ProofStore

# The Stage C trace parsers moved to ``agent_runtime.stagec_trace_parsers`` (S1 of
# the mission-lane removal): Stage C visual proof is KEEP, the ``Proof``-building
# half of this module is REMOVE. The aliases above keep this file reading exactly as
# before while the parsers live where they can survive.


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
