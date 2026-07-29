"""Stage C screenshot-trace parsers — a KEEP module.

Extracted from ``agent_runtime/visual_trace_evidence.py`` ahead of the mission-lane
removal (``docs/agent-runtime-harness/16-mission-lane-removal.md`` S1/S6). That file
has two halves with opposite fates: the half that builds a ``Proof`` row and writes
it through the retired mission evidence store dies with that machinery; the half below only *reads*
a tool-progress payload and answers "was this a Stage C screenshot, and which PNG is
it?". Stage C visual proof survives, so these do.

What lives here
---------------
* :func:`is_launcher_qa_screenshot_tool` — recognises a direct
  ``mcp_launcher_qa_*screenshot*`` tool call.
* :func:`terminal_wrapper_screenshot` — recognises the *indirect* path, where the
  agent shelled out to ``Invoke-LauncherQaMcpTool.ps1`` through the terminal tool,
  and recovers ``(tool_name, png_path)`` from the command line.
* :func:`latest_wrapper_artifact` — resolves the PNG the wrapper wrote, from the
  declared ``out_dir`` or the Stage C screenshot drop
  (``X:/tmp/stagec/screenshots``), newest-first.
* :func:`png_dimensions` — 24-byte PNG header sniff for width/height.

Deliberately duck-typed: ``terminal_wrapper_screenshot`` takes ``run`` only to read
``started_at`` and does so via ``getattr``, so this module never imports ``AgentRun``
(runs are removed later in the same plan).

Staleness rule
--------------
``latest_wrapper_artifact`` binds the artifact to the caller's time window via
``min_mtime``. Without it, a stale or foreign PNG left in the output directory by an
earlier session could be mtime-globbed in and presented as this session's evidence.
Keep that argument threaded.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any

# Default Stage C screenshot drop used by ``Invoke-LauncherQaMcpTool.ps1`` when the
# command line does not declare an ``out_dir``.
STAGEC_SCREENSHOT_DROP = Path("X:/tmp/stagec/screenshots")

# Tolerance (seconds) subtracted from a run's start before rejecting an artifact as
# older than the run — absorbs clock skew between the wrapper and the store.
_RUN_MTIME_SKEW_SECONDS = 120.0


def is_launcher_qa_screenshot_tool(tool_name: str) -> bool:
    lowered = tool_name.lower()
    return lowered.startswith("mcp_launcher_qa_") and "screenshot" in lowered


def terminal_wrapper_screenshot(payload: dict[str, Any], *, run: Any | None = None) -> tuple[str, str] | None:
    """``(tool_name, png_path)`` when this terminal call was a Stage C screenshot wrapper."""

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
    args = wrapper_args(command)
    label = str(args.get("label") or partial_json_string(command, "label") or "").strip()
    out_dir = str(args.get("out_dir") or partial_json_string(command, "out_dir") or "").strip()
    # Bind the artifact to THIS run's time window so a stale/foreign PNG left in the
    # output directory by an earlier run can't be mtime-globbed in as fake evidence.
    source = latest_wrapper_artifact(label=label, out_dir=out_dir, min_mtime=run_min_mtime(run))
    if source is None:
        return None
    return tool_name, str(source)


def run_min_mtime(run: Any | None) -> float | None:
    """Earliest artifact mtime accepted for a run: its start minus a skew tolerance."""

    started = getattr(run, "started_at", None) if run is not None else None
    if started is None:
        return None
    try:
        epoch = started.timestamp() if hasattr(started, "timestamp") else float(started)
    except (TypeError, ValueError, OverflowError):
        return None
    return epoch - _RUN_MTIME_SKEW_SECONDS


def wrapper_args(command: str) -> dict[str, Any]:
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


def partial_json_string(command: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]+)', command)
    if match is None:
        return ""
    return match.group(1).replace("...", "").strip()


def latest_wrapper_artifact(*, label: str, out_dir: str, min_mtime: float | None = None) -> Path | None:
    roots: list[Path] = []
    if out_dir:
        roots.append(Path(out_dir))
    roots.append(STAGEC_SCREENSHOT_DROP)
    patterns = [f"{safe_glob_prefix(label)}*.png"] if label else []
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


def png_dimensions(path: Path) -> tuple[int, int]:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return 0, 0
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])
    return 0, 0


def safe_glob_prefix(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return text or "*"
