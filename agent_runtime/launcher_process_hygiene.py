from __future__ import annotations

import csv
import os
import subprocess
from typing import Any, Callable, Sequence


LAUNCHER_VISUAL_PROCESS_NAMES = (
    "eternia_launcher.exe",
    "stagec_qa_mcp_server.exe",
)


Runner = Callable[..., subprocess.CompletedProcess]


def clean_launcher_visual_processes(
    *,
    enabled: bool,
    process_names: Sequence[str] = LAUNCHER_VISUAL_PROCESS_NAMES,
    runner: Runner | None = None,
    is_windows: bool | None = None,
) -> dict[str, Any]:
    """Clear stale Launcher/Stage C proof processes before visual goals.

    The cleanup is intentionally narrow: it only targets exact executable image
    names that are known to stale-lock Launcher debug builds or Stage C MCP
    sessions. It does not touch repositories or Harness evidence.
    """

    names = _safe_process_names(process_names)
    if not enabled:
        return {
            "enabled": False,
            "supported": bool(is_windows if is_windows is not None else os.name == "nt"),
            "process_names": names,
            "detected": [],
            "terminated_pids": [],
            "failed_pids": [],
            "changed": False,
            "summary": "launcher visual process cleanup not requested",
        }
    windows = bool(is_windows if is_windows is not None else os.name == "nt")
    if not windows:
        return {
            "enabled": True,
            "supported": False,
            "process_names": names,
            "detected": [],
            "terminated_pids": [],
            "failed_pids": [],
            "changed": False,
            "summary": "launcher visual process cleanup is only supported on Windows",
        }
    run = runner or subprocess.run
    detected: list[dict[str, Any]] = []
    errors: list[str] = []
    for name in names:
        try:
            completed = run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            errors.append(f"{name}: tasklist_failed")
            continue
        if getattr(completed, "returncode", 0) != 0:
            errors.append(f"{name}: tasklist_nonzero")
            continue
        detected.extend(_parse_tasklist_csv(name, getattr(completed, "stdout", "") or ""))

    terminated: list[int] = []
    failed: list[int] = []
    for proc in detected:
        pid = proc.get("pid")
        if not isinstance(pid, int):
            continue
        try:
            killed = run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except Exception:
            failed.append(pid)
            continue
        if getattr(killed, "returncode", 1) == 0:
            terminated.append(pid)
        else:
            failed.append(pid)

    summary = "clean"
    if detected:
        summary = f"terminated {len(terminated)}/{len(detected)} launcher visual process(es)"
    if failed:
        summary = f"{summary}; failed_pids={len(failed)}"
    if errors:
        summary = f"{summary}; query_errors={len(errors)}"
    return {
        "enabled": True,
        "supported": True,
        "process_names": names,
        "detected": detected,
        "terminated_pids": terminated,
        "failed_pids": failed,
        "changed": bool(terminated),
        "summary": summary,
        "errors": errors,
    }


def launcher_visual_cleanup_needed(*parts: str | None) -> bool:
    text = " ".join(part or "" for part in parts).lower()
    if not text.strip():
        return False
    exact_markers = (
        "stage c",
        "stagec",
        "marionette",
        "mcp screenshot",
        "mcp/qa screenshot",
        "screenshot proof",
        "video proof",
        "visual proof",
        "flutter build windows",
        "main_marionette",
    )
    if any(marker in text for marker in exact_markers):
        return True
    return "mission control" in text and any(marker in text for marker in ("launcher", "screenshot", "visual", "agent view", "terminal"))


def _parse_tasklist_csv(expected_name: str, stdout: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in csv.reader(line for line in stdout.splitlines() if line.strip()):
        if len(row) < 2:
            continue
        image = str(row[0] or "").strip()
        if image.lower() != expected_name.lower():
            continue
        try:
            pid = int(str(row[1]).strip())
        except (TypeError, ValueError):
            continue
        items.append({"name": expected_name, "pid": pid})
    return items


def _safe_process_names(process_names: Sequence[str]) -> list[str]:
    safe: list[str] = []
    for name in process_names:
        text = str(name or "").strip()
        if not text or text in safe:
            continue
        if text.lower() not in LAUNCHER_VISUAL_PROCESS_NAMES:
            continue
        safe.append(text)
    return safe
