from __future__ import annotations

import hashlib
import shutil
import subprocess
import uuid
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_time import now

from .events import EventLog
from .dirty_state import no_product_edit_dirty_check
from .models import Event, Incident, Task
from .packets import latest_packet
from .repo_context import safe_affected_repo_labels
from .stagec_mcp_visual_provider import (
    load_launcher_qa_mcp_config,
    resolve_launcher_qa_mcp_config,
    smoke_launcher_qa_mcp,
)
from .states import TaskState
from .store import IncidentStore, TaskStore


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    id: str
    ok: bool
    token: str
    detail: str
    actionable_fix: str
    blocking: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PreflightResult:
    checks: list[PreflightCheck]
    ok: bool
    environment_fingerprint: str
    blocker: dict[str, Any] | None = None
    proof_payload: dict[str, Any] | None = None


def run_preflight(task: Task, *, stage: object | None = None, persona_target: str) -> PreflightResult:
    checks: list[PreflightCheck] = []
    if _repo_clean_required(task, stage=stage):
        checks.append(_repo_clean_check(task))
    if not _dependency_sensitive(task, stage=stage, persona_target=persona_target):
        fingerprint = environment_fingerprint(checks)
        blocker_check = next((check for check in checks if not check.ok and check.blocking), None)
        blocker = _blocker_from_check(blocker_check, fingerprint=fingerprint, persona_target=persona_target) if blocker_check else None
        return PreflightResult(checks=checks, ok=blocker_check is None, environment_fingerprint=fingerprint, blocker=blocker, proof_payload=blocker)

    checks.append(_runtime_root_check())
    if _backend_required(task, persona_target=persona_target):
        if _docker_required(task, stage=stage):
            checks.append(_docker_check())
        checks.append(_python_check("backend_venv_import"))
    if _launcher_required(task, stage=stage, persona_target=persona_target):
        checks.append(_flutter_check())
    if _visual_proof_required(task, stage=stage, persona_target=persona_target):
        checks.append(_mcp_exposure_check())

    fingerprint = environment_fingerprint(checks)
    blocker_check = next((check for check in checks if not check.ok and check.blocking), None)
    blocker = _blocker_from_check(blocker_check, fingerprint=fingerprint, persona_target=persona_target) if blocker_check else None
    return PreflightResult(checks=checks, ok=blocker_check is None, environment_fingerprint=fingerprint, blocker=blocker, proof_payload=blocker)


def environment_fingerprint(checks: list[PreflightCheck]) -> str:
    tokens = [f"{check.id}={check.token}" for check in checks]
    return hashlib.sha256(("env\0" + "\0".join(sorted(tokens))).encode("utf-8")).hexdigest()[:16]


def _blocker_from_check(check: PreflightCheck | None, *, fingerprint: str, persona_target: str) -> dict[str, Any] | None:
    if check is None:
        return None
    return {
        "check_id": check.id,
        "detail": check.detail,
        "actionable_fix": check.actionable_fix,
        "environment_fingerprint": fingerprint,
        "persona_target": persona_target,
        "metadata": dict(check.metadata),
    }


def environment_fingerprint_status(task: Task, stage_id: str | None, fingerprint: str) -> str:
    current = _stage_self_heal(task, stage_id).get("last_environment_fingerprint")
    if not current:
        return "unknown"
    return "changed" if current != fingerprint else "unchanged"


def open_preflight_blocker(
    task: Task,
    result: PreflightResult,
    *,
    persona_target: str,
    incident_store: IncidentStore,
    task_store: TaskStore,
    stage_id: str | None = None,
    run_id: str | None = None,
) -> Incident | None:
    if result.ok or not result.blocker:
        _persist_environment_fingerprint(task, stage_id, result.environment_fingerprint, "unchanged")
        task_store.update(task, actor="harness", reason="preflight passed")
        return None
    status = environment_fingerprint_status(task, stage_id, result.environment_fingerprint)
    blocker = result.blocker
    remediation = blocker.get("metadata") if isinstance(blocker.get("metadata"), dict) else {}
    metadata = {
        "kind": "preflight",
        "check_id": str(blocker.get("check_id")),
        "ok": False,
        "status": "failed",
        "environment_fingerprint": result.environment_fingerprint,
        "fingerprint_status": status,
        "detail": _compact(blocker.get("detail")),
        "actionable_fix": _compact(blocker.get("actionable_fix")),
        "persona_target": persona_target,
        "run_id": run_id,
    }
    for key in ("remediation_action", "remediation_status", "remediation_detail", "remediation_wait_seconds"):
        value = remediation.get(key)
        if value is not None:
            metadata[key] = _compact(value) if isinstance(value, str) else value
    for key in ("dirty_labels", "dirty_count", "repos"):
        value = remediation.get(key)
        if value is not None:
            metadata[key] = _safe_preflight_metadata(value)
    blocking_event_id = f"preflight_blocked_{uuid.uuid4().hex[:8]}"
    incident = Incident(
        id=f"inc_{uuid.uuid4().hex[:8]}",
        task_id=task.id,
        run_id=run_id,
        kind="environment_blocker",
        summary=f"Preflight blocked {persona_target}: {metadata['check_id']}",
        detail_path=None,
        opened_at=now(),
        metadata={
            "check_id": str(metadata["check_id"]),
            "environment_fingerprint": result.environment_fingerprint,
            "blocking_event_id": blocking_event_id,
            "stage_id": stage_id or "",
            "persona_target": persona_target,
            "dirty_labels": metadata.get("dirty_labels") or [],
        },
    )
    incident_store.open(incident)
    if task.state not in {TaskState.DONE, TaskState.CANCELLED, TaskState.FAILED}:
        task.state = TaskState.RUNNING
    task.open_incident_ids = _dedupe(list(task.open_incident_ids or []), [incident.id])
    _persist_environment_fingerprint(task, stage_id, result.environment_fingerprint, status)
    task.updated_at = now()
    task_store.update(task, actor="harness", reason="preflight environment blocker")
    return incident


def record_preflight_pass(
    task: Task,
    result: PreflightResult,
    *,
    persona_target: str,
    task_store: TaskStore,
    stage_id: str | None = None,
    event_log: EventLog | None = None,
) -> bool:
    remediations = _successful_remediations(result)
    if not remediations:
        return False
    status = environment_fingerprint_status(task, stage_id, result.environment_fingerprint)
    _persist_environment_fingerprint(task, stage_id, result.environment_fingerprint, "changed" if status == "changed" else "unchanged")
    task.updated_at = now()
    task_store.update(task, actor="harness", reason="preflight remediation applied")
    (event_log or EventLog()).append(
        Event(
            ts=now(),
            type="task.preflight",
            task_id=task.id,
            run_id=None,
            persona_id=persona_target,
            payload={
                "status": "passed",
                "stage_id": stage_id,
                "persona_target": persona_target,
                "environment_fingerprint": result.environment_fingerprint,
                "remediations": remediations,
            },
        )
    )
    return True


def _dependency_sensitive(task: Task, *, stage: object | None, persona_target: str) -> bool:
    return (
        _backend_required(task, persona_target=persona_target)
        or _launcher_required(task, stage=stage, persona_target=persona_target)
        or _visual_proof_required(task, stage=stage, persona_target=persona_target)
    )


def _repo_clean_required(task: Task, *, stage: object | None) -> bool:
    flags = {str(flag).strip().lower() for flag in (getattr(task, "risk_flags", []) or [])}
    if "no_product_edits" in flags or "routing_burn_in_only" in flags:
        return True
    text = _task_text(task, stage=stage)
    return "no product edit" in text or "no product edits" in text


def _repo_clean_check(task: Task) -> PreflightCheck:
    result = no_product_edit_dirty_check(task)
    dirty_labels = result["dirty_labels"]
    if result["ok"]:
        return PreflightCheck(
            "repo_clean",
            True,
            "repo_clean=clean",
            "Affected repositories are clean before no-product-edit dispatch.",
            "No action needed.",
            metadata={"repos": [{"label": item["label"], "dirty": item["dirty"], "dirty_count": item["dirty_count"]} for item in result["repos"]]},
        )
    baseline = _repo_clean_baseline(task)
    unchanged = _dirty_repos_unchanged_from_baseline(result["repos"], baseline)
    if unchanged["ok"]:
        return PreflightCheck(
            "repo_clean",
            True,
            "repo_clean=baseline_unchanged",
            "Affected repositories have pre-existing dirty state, but no dirty signature changed since goal creation.",
            "No action needed; continue no-product-edit dispatch using baseline-delta proof.",
            metadata={
                "baseline_delta_status": "unchanged",
                "dirty_labels": dirty_labels,
                "dirty_count": result["dirty_count"],
                "repos": unchanged["repos"],
            },
        )
    label_text = ", ".join(str(label) for label in dirty_labels[:4])
    if len(dirty_labels) > 4:
        label_text += f" +{len(dirty_labels) - 4}"
    return PreflightCheck(
        "repo_clean",
        False,
        "repo_clean=dirty",
        f"Dirty repository state before no-product-edit dispatch: {label_text}.",
        "Commit/stash/revert changed repo state, rerun from a fresh goal baseline, or remove the no_product_edits risk flag for an edit-capable mission.",
        metadata={
            "baseline_delta_status": unchanged["status"],
            "dirty_labels": dirty_labels,
            "dirty_count": result["dirty_count"],
            "repos": [
                {
                    "label": item["label"],
                    "dirty": item["dirty"],
                    "dirty_count": item["dirty_count"],
                    "status_excerpt": item["status_excerpt"][:5],
                    "error": item.get("error"),
                }
                for item in result["repos"]
            ],
        },
    )


def _repo_clean_baseline(task: Task) -> dict[str, Any]:
    root = getattr(task, "harness_self_heal", None)
    if not isinstance(root, dict):
        return {"repos": []}
    baseline = root.get("repo_clean_baseline")
    return baseline if isinstance(baseline, dict) else {"repos": []}


def _dirty_repos_unchanged_from_baseline(current_repos: list[dict[str, Any]], baseline: dict[str, Any]) -> dict[str, Any]:
    baseline_repos = baseline.get("repos") if isinstance(baseline, dict) else None
    if not isinstance(baseline_repos, list) or not baseline_repos:
        return {"ok": False, "status": "baseline_missing", "repos": []}
    baseline_by_label = {str(repo.get("label") or ""): repo for repo in baseline_repos if isinstance(repo, dict)}
    changed = []
    compared = []
    for repo in current_repos:
        if not isinstance(repo, dict) or not (repo.get("dirty") or repo.get("error")):
            continue
        label = str(repo.get("label") or "")
        before = baseline_by_label.get(label)
        item = {
            "label": label[:80],
            "dirty": bool(repo.get("dirty")),
            "dirty_count": int(repo.get("dirty_count") or 0),
            "error": str(repo.get("error") or "")[:80] or None,
            "baseline_unchanged": before is not None and _repo_signature(repo) == _repo_signature(before),
        }
        compared.append(item)
        if not item["baseline_unchanged"]:
            changed.append(label)
    if changed:
        return {"ok": False, "status": "baseline_changed", "changed_labels": changed, "repos": compared}
    return {"ok": bool(compared), "status": "baseline_unchanged", "repos": compared}


def _repo_signature(repo: dict[str, Any]) -> tuple:
    return (
        bool(repo.get("dirty")),
        int(repo.get("dirty_count") or 0),
        str(repo.get("error") or ""),
        tuple(str(item) for item in list(repo.get("status_excerpt") or [])[:20]),
    )


def _backend_required(task: Task, *, persona_target: str) -> bool:
    if persona_target == "dev":
        return False
    if persona_target == "backend_dev":
        return True
    labels = " ".join(safe_affected_repo_labels(list(getattr(task, "affected_repos", []) or []))).lower()
    text = _task_text(task)
    return "backend" in labels or "backend" in text or "docker" in text or "compose" in text


def _launcher_required(task: Task, *, stage: object | None, persona_target: str) -> bool:
    if persona_target == "backend_dev":
        return False
    labels = " ".join(safe_affected_repo_labels(list(getattr(task, "affected_repos", []) or []))).lower()
    text = _task_text(task, stage=stage)
    return "launcher" in labels or "eternialauncher" in labels or "launcher" in text or "flutter" in text


def _docker_required(task: Task, *, stage: object | None) -> bool:
    text = _task_text(task, stage=stage)
    return "docker" in text or "compose" in text or "container" in text


def _visual_proof_required(task: Task, *, stage: object | None = None, persona_target: str) -> bool:
    if persona_target == "backend_dev":
        return False
    if getattr(task, "requires_visual_proof", False) or getattr(stage, "requires_visual_proof", False):
        return True
    if _latest_handoff_visual_required(task, stage=stage) is False:
        return False
    text = _task_text(task, stage=stage)
    return any(marker in text for marker in ("mission control", "screenshot", "video", "visual", "stage c", "mcp"))


def _latest_handoff_visual_required(task: Task, *, stage: object | None = None) -> bool | None:
    stage_id = getattr(stage, "id", None) or getattr(task, "current_stage_id", None)
    try:
        packet = latest_packet(task.id, "handoff_packet", stage_id=stage_id)
    except Exception:
        return None
    body = packet.get("body") if isinstance(packet, dict) else None
    proof_gate = body.get("proof_gate") if isinstance(body, dict) else None
    if isinstance(proof_gate, dict) and isinstance(proof_gate.get("visual_required"), bool):
        return proof_gate.get("visual_required")
    return None


def _runtime_root_check() -> PreflightCheck:
    """Is the resolved runtime root actually USABLE? (audit Q4)

    This check used to be vacuous. It asked "did the resolver answer?" —
    ``ok = bool(str(store_root()).strip())`` — and ``resolve_runtime`` always
    answers, because its last rung is an unconditional platform default. So the
    only way to fail was for ``store_root()`` to RAISE, and the check happily
    reported ``runtime_root=present`` for a path that does not exist on this
    machine, then told the operator to configure a variable it never read. A
    vacuous check is worse than an absent one: it spends operator trust and pays
    nothing back.

    It now reports the resolution honestly — which layer won, the resolved path,
    whether it exists, and whether it looks like a store — and fails on exactly
    one condition: **resolved via the DEFAULT layer and it does not look like a
    store**. ``resolution.suspect_default_root`` already computes precisely that
    predicate; this is its first consumer that can act on it.

    Why that one condition. An env- or config-resolved root is an explicit
    operator statement — honor it even if it is not populated yet, since a first
    run legitimately creates it. The default rung is not a statement; it is what
    the resolver falls back to when nobody said anything. A default root with no
    ``tasks/`` in it means no store was ever initialized here, which is the real
    failure this check was always supposed to catch.
    """

    try:
        from .paths import store_root
        from .resolution import resolve_runtime, suspect_default_root

        resolution = resolve_runtime()
        # Keep the fail-fast behavior of the accessor (probe isolation refuses
        # here rather than let a probe run resolve the live store).
        store_root()
    except Exception as exc:
        return PreflightCheck(
            "runtime_root",
            False,
            "runtime_root=unresolvable",
            f"The runtime root could not be resolved ({type(exc).__name__}).",
            "Check agent_runtime.store_root in the root config.yaml, and HERMES_REQUIRE_ISOLATED_ROOT if this is a probe run.",
        )

    root = Path(resolution.store_root)
    exists = root.exists()
    looks_like_store = (root / "tasks").is_dir()
    ok = not suspect_default_root(resolution)
    token = (
        f"runtime_root={'ok' if ok else 'uninitialized'} layer={resolution.layer} "
        f"path={root} exists={str(exists).lower()} store={str(looks_like_store).lower()}"
    )
    if ok:
        detail = f"Runtime root resolved via the '{resolution.layer}' layer: {root}"
        actionable_fix = "No action needed."
    else:
        detail = (
            f"No runtime root is configured, so the resolver fell back to the platform "
            f"default ({root}), and no store has been initialized there."
        )
        actionable_fix = (
            "Set agent_runtime.store_root in the root config.yaml (or export "
            "HERMES_AGENT_RUNTIME_ROOT) to name the store this machine should use, or "
            "run the harness once to initialize a store at the default location."
        )
    return PreflightCheck("runtime_root", ok, token, detail, actionable_fix)


def _docker_check() -> PreflightCheck:
    if shutil.which("docker") is None:
        return PreflightCheck("docker_engine", False, "docker_engine=absent", "Docker CLI is not available", "Install/start Docker Desktop before backend compose proof.")
    if _docker_info_ok():
        return PreflightCheck("docker_engine", True, "docker_engine=up", "Docker engine is reachable", "No action needed.")
    if not _docker_autostart_enabled():
        return PreflightCheck(
            "docker_engine",
            False,
            "docker_engine=down",
            "Docker engine is not reachable",
            "Start Docker Desktop and wait for the Linux engine.",
            metadata={
                "remediation_action": "docker_desktop_autostart",
                "remediation_status": "disabled",
                "remediation_detail": "HERMES_PREFLIGHT_DOCKER_AUTOSTART disabled auto-start.",
            },
        )
    start = _start_docker_desktop()
    wait_seconds = _docker_autostart_wait_seconds()
    if start.get("ok") and _wait_for_docker_engine(wait_seconds):
        return PreflightCheck(
            "docker_engine",
            True,
            "docker_engine=up_after_autostart",
            "Docker engine became reachable after Harness started Docker Desktop.",
            "No action needed.",
            metadata={
                "remediation_action": "docker_desktop_autostart",
                "remediation_status": "applied",
                "remediation_detail": _compact(start.get("detail")),
                "remediation_wait_seconds": wait_seconds,
            },
        )
    status = "failed" if start.get("ok") else "unavailable"
    detail = (
        f"Docker engine is not reachable after auto-start attempt and {wait_seconds}s wait."
        if start.get("ok")
        else f"Docker Desktop auto-start unavailable: {_compact(start.get('detail'))}"
    )
    return PreflightCheck(
        "docker_engine",
        False,
        f"docker_engine=down_after_autostart_{status}",
        detail,
        "Start Docker Desktop and wait for the Linux engine.",
        metadata={
            "remediation_action": "docker_desktop_autostart",
            "remediation_status": status,
            "remediation_detail": _compact(start.get("detail")),
            "remediation_wait_seconds": wait_seconds,
        },
    )


def _docker_info_ok(*, timeout: int = 8) -> bool:
    try:
        completed = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False
    return completed.returncode == 0


def _docker_autostart_enabled() -> bool:
    raw = os.getenv("HERMES_PREFLIGHT_DOCKER_AUTOSTART", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _docker_autostart_wait_seconds() -> int:
    raw = os.getenv("HERMES_PREFLIGHT_DOCKER_AUTOSTART_SECONDS", "90").strip()
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 90
    return max(0, min(seconds, 180))


def _start_docker_desktop() -> dict[str, Any]:
    exe = _docker_desktop_exe()
    if exe is None:
        return {"ok": False, "detail": "Docker Desktop executable was not found."}
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        subprocess.Popen(
            [str(exe)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "detail": "Docker Desktop start requested."}


def _docker_desktop_exe() -> Path | None:
    candidates: list[Path] = []
    env_path = os.getenv("HERMES_DOCKER_DESKTOP_EXE", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    for env_key in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        root = os.getenv(env_key, "").strip()
        if root:
            candidates.append(Path(root) / "Docker" / "Docker" / "Docker Desktop.exe")
    command = shutil.which("Docker Desktop") or shutil.which("Docker Desktop.exe")
    if command:
        candidates.append(Path(command))
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _wait_for_docker_engine(timeout_seconds: int) -> bool:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        if _docker_info_ok():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(3.0, remaining))


def _python_check(check_id: str) -> PreflightCheck:
    return PreflightCheck(check_id, shutil.which("python") is not None, f"{check_id}=present" if shutil.which("python") else f"{check_id}=absent", "Python interpreter is available", "Activate/install the backend Python environment.")


def _flutter_check() -> PreflightCheck:
    present = shutil.which("flutter") is not None
    return PreflightCheck("flutter", present, "flutter=present" if present else "flutter=absent", "Flutter CLI is available" if present else "Flutter CLI is not available", "Install Flutter or make flutter available on PATH before Launcher proof.")


def _mcp_exposure_check() -> PreflightCheck:
    config = load_launcher_qa_mcp_config(persona_target="qa")
    if config is None:
        # Explain the miss instead of reporting a flat "missing": an unbound
        # machine root and an unconfigured server need different fixes, and the
        # operator must not have to guess which one they hit.
        reason = resolve_launcher_qa_mcp_config(persona_target="qa")
        return PreflightCheck(
            "launcher_qa_mcp",
            False,
            f"launcher_qa_mcp={reason.code or 'missing'}",
            reason.summary or "launcher_qa MCP server is not configured for the QA persona",
            reason.fix_hint or "Configure mcp_servers.launcher_qa in the launcher-qa Hermes profile.",
        )
    result = smoke_launcher_qa_mcp(config)
    return PreflightCheck(
        "launcher_qa_mcp",
        result.ok,
        f"launcher_qa_mcp={result.code}",
        result.summary,
        "Repair the launcher_qa Stage C MCP command/profile or run the Launcher marionette debug target before visual proof.",
    )


def _task_text(task: Task, *, stage: object | None = None) -> str:
    parts = [
        getattr(task, "title", ""),
        getattr(task, "description", ""),
        " ".join(getattr(task, "acceptance_criteria", []) or []),
        getattr(stage, "title", "") if stage else "",
        getattr(stage, "objective", "") if stage else "",
        " ".join(getattr(stage, "test_plan", []) or []) if stage else "",
    ]
    return " ".join(str(part or "") for part in parts).lower()


def _persist_environment_fingerprint(task: Task, stage_id: str | None, fingerprint: str, status: str, *, failed_proof_id: str | None = None) -> None:
    root = dict(getattr(task, "harness_self_heal", {}) or {})
    stages = dict(root.get("stages") or {})
    key = stage_id or "_mission"
    state = dict(stages.get(key) or {})
    state["last_environment_fingerprint"] = fingerprint
    state["environment_fingerprint_status"] = status
    if failed_proof_id:
        failed = list(state.get("last_failed_proof_ids") or [])
        if failed_proof_id not in failed:
            failed.append(failed_proof_id)
        state["last_failed_proof_ids"] = failed
    stages[key] = state
    root["stages"] = stages
    task.harness_self_heal = root


def _stage_self_heal(task: Task, stage_id: str | None) -> dict[str, Any]:
    root = getattr(task, "harness_self_heal", {}) or {}
    stages = root.get("stages") if isinstance(root, dict) else {}
    state = stages.get(stage_id or "_mission") if isinstance(stages, dict) else {}
    return state if isinstance(state, dict) else {}


def _safe_token(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(value or ""))
    return text[:48] or "item"


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split())[:500]


def _safe_preflight_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 3:
        return "<truncated>"
    if isinstance(value, str):
        return _compact(value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, list):
        return [_safe_preflight_metadata(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        return {
            _compact(key)[:80]: _safe_preflight_metadata(item, depth=depth + 1)
            for key, item in list(value.items())[:40]
        }
    return _compact(value)


def _dedupe(existing: list[str], new_items: list[str]) -> list[str]:
    seen = set(existing)
    result = list(existing)
    for item in new_items:
        if item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _successful_remediations(result: PreflightResult) -> list[dict[str, Any]]:
    remediations: list[dict[str, Any]] = []
    for check in result.checks:
        if not check.ok or check.metadata.get("remediation_status") != "applied":
            continue
        remediations.append(
            {
                "check_id": check.id,
                "remediation_action": _compact(check.metadata.get("remediation_action")),
                "remediation_status": "applied",
                "remediation_wait_seconds": check.metadata.get("remediation_wait_seconds"),
            }
        )
    return remediations
