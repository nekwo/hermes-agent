from __future__ import annotations

import os
import re
import signal
import hashlib
import json
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from hermes_time import now

from . import paths
from .events import EventLog
from .models import Event
from .models import Proof, Task
from .decision_schema import DecisionPayloadInvalid
from .proof_command_policy import (
    is_generic_launcher_readiness_command,
    is_unbounded_full_suite_command,
    narrow_launcher_contract_analyze_command,
    task_requires_bounded_smoke_proof,
    task_requires_launcher_contract_consumption_proof,
)
from .proof_rules import ProofType
from .store import ProofStore

_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b("
    r"(?:[A-Za-z0-9]+_)*(?:SECRET|TOKEN|PASSWORD|PASS|CREDENTIAL|API_?KEY|KEY)(?:_[A-Za-z0-9]+)*"
    r")\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s'\"]+)"
)

_SECRET_PATTERNS = (
    _SECRET_ASSIGNMENT_PATTERN,
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._\-+/=]{12,})"),
)
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:(?:[\\/]+[^\"'<>|\r\n]+)+"),
    re.compile(r"(?i)\b[A-Z]:(?:[\\/]+[^\\/\s\"'<>|:]+)+"),
    re.compile(r"(?<![\w.-])/(?:Users|home|mnt|opt|var|tmp|Volumes)/(?:[^\s\"'<>|:]+/?)+"),
)

MAX_PROOF_EXCERPT_CHARS = 2000
PROOF_MONITOR_HEARTBEAT_SECONDS = 10
LAUNCHER_CONTRACT_ANALYZE_ADAPTER = "launcher_contract_analyze_narrowed"


@dataclass(frozen=True, slots=True)
class CommandProofResult:
    command: str
    exit_code: int | None
    timed_out: bool
    artifact_path: str
    proof: Proof


@dataclass(frozen=True, slots=True)
class AdaptedCommand:
    command: str
    original_command: str
    adapter: str | None = None


@dataclass(frozen=True, slots=True)
class _CompletedCommand:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool


PYTEST_TIMEOUT_ADAPTER = "windows_pytest_timeout_disabled"
ETERNIA_BACKEND_DJANGO_SETTINGS_ADAPTER = "eternia_backend_django_settings_export"
ETERNIA_BACKEND_VENV_PYTHON_ADAPTER = "eternia_backend_virtualenv_python"
REDACTED_WORKDIR_PREFIX_ADAPTER = "redacted_workdir_prefix_removed"


class CommandProofRunner:
    """Run bounded shell commands and persist redaction-safe proof artifacts."""

    def __init__(
        self,
        *,
        proof_store: ProofStore | None = None,
        workdir: str | Path | None = None,
        timeout_seconds: int = 300,
        artifact_root: str | Path | None = None,
        event_log: EventLog | None = None,
    ):
        self.proof_store = proof_store or ProofStore()
        self.workdir = Path(workdir).expanduser() if workdir is not None else Path.cwd()
        self.timeout_seconds = timeout_seconds
        self.artifact_root = Path(artifact_root).expanduser() if artifact_root is not None else paths.store_root()
        self.event_log = event_log or self.proof_store.event_log

    def run_commands(
        self,
        task: Task,
        *,
        stage_id: str | None,
        run_id: str | None,
        actor: str,
        commands: list[str],
        proof_intent: str | None = None,
        environment_fingerprint: str | None = None,
        environment_fingerprint_status: str | None = None,
        proof_recipe: dict[str, Any] | None = None,
    ) -> list[Proof]:
        proofs: list[Proof] = []
        clean_commands = [str(command).strip() for command in commands if str(command).strip()]
        for index, command in enumerate(clean_commands):
            proof = self._run_one(
                task,
                stage_id=stage_id,
                run_id=run_id,
                actor=actor,
                command=command,
                command_index=index,
                commands_requested=len(clean_commands),
                proof_intent=proof_intent,
                environment_fingerprint=environment_fingerprint,
                environment_fingerprint_status=environment_fingerprint_status,
                proof_recipe=proof_recipe,
            )
            proofs.append(proof)
        return proofs

    def _run_one(
        self,
        task: Task,
        *,
        stage_id: str | None,
        run_id: str | None,
        actor: str,
        command: str,
        command_index: int,
        commands_requested: int,
        proof_intent: str | None,
        environment_fingerprint: str | None,
        environment_fingerprint_status: str | None,
        proof_recipe: dict[str, Any] | None,
    ) -> Proof:
        started_at = now()
        recipe = _safe_recipe_metadata(proof_recipe)
        recipe_sandbox = _prepare_recipe_sandbox(task, recipe, command_index=command_index) if recipe else None
        dirty_before = _git_status_lines(self.workdir) if recipe and recipe.get("mode") == "no_product_edit" else None
        adapted = adapt_command_for_proof(command, workdir=self.workdir)
        narrowed_command = narrow_launcher_contract_analyze_command(
            task,
            adapted.command,
            stage_id=stage_id,
            proof_intent=proof_intent or "",
        )
        if narrowed_command != adapted.command:
            adapted = AdaptedCommand(
                command=narrowed_command,
                original_command=adapted.original_command,
                adapter=_append_command_adapter(adapted.adapter, LAUNCHER_CONTRACT_ANALYZE_ADAPTER),
            )
            self.event_log.append(
                Event(
                    now(),
                    "run.progress",
                    task.id,
                    run_id,
                    actor,
                    {
                        "type": "run.progress",
                        "phase": "proof",
                        "step": "proof_command_normalized",
                        "status": "normalized",
                        "stage_id": stage_id,
                        "command_index": command_index,
                        "commands_requested": commands_requested,
                        "summary": "Launcher contract proof command narrowed by removing redundant flutter analyze lib/main.dart.",
                        "next_expected": "run_narrowed_contract_proof",
                    },
                )
            )
        effective_command = adapted.command
        if task_requires_launcher_contract_consumption_proof(task, stage_id=stage_id, proof_intent=proof_intent or "") and is_generic_launcher_readiness_command(effective_command):
            self.event_log.append(
                Event(
                    now(),
                    "run.progress",
                    task.id,
                    run_id,
                    actor,
                    {
                        "type": "run.progress",
                        "phase": "proof",
                        "step": "proof_command_refused",
                        "status": "blocked",
                        "stage_id": stage_id,
                        "command_index": command_index,
                        "commands_requested": commands_requested,
                        "summary": "Launcher contract proof refused because the command only checks generic Flutter/Dart readiness.",
                        "next_expected": "request_contract_consumption_proof_or_neko_self_heal",
                    },
                )
            )
            raise DecisionPayloadInvalid(
                "Launcher contract proof policy failed at proof execution boundary: generic Flutter/Dart readiness commands do not prove backend contract packet consumption."
            )
        if task_requires_bounded_smoke_proof(task, stage_id=stage_id) and is_unbounded_full_suite_command(effective_command):
            self.event_log.append(
                Event(
                    now(),
                    "run.progress",
                    task.id,
                    run_id,
                    actor,
                    {
                        "type": "run.progress",
                        "phase": "proof",
                        "step": "proof_command_refused",
                        "status": "blocked",
                        "stage_id": stage_id,
                        "command_index": command_index,
                        "commands_requested": commands_requested,
                        "summary": "Proof command refused by smoke/no-edit policy before process launch.",
                        "next_expected": "request_narrower_proof_or_neko_self_heal",
                    },
                )
            )
            raise DecisionPayloadInvalid(
                "Smoke/no-edit proof command policy failed at proof execution boundary: refusing to launch an unbounded full-suite command."
            )
        readiness_failure = _backend_release_gate_readiness_failure(effective_command, self.workdir)
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code: int | None
        if readiness_failure is not None:
            self.event_log.append(
                Event(
                    now(),
                    "backend_release_gate_environment_failed",
                    task.id,
                    run_id,
                    actor,
                    {
                        "check_id": readiness_failure["check_id"],
                        "reason": readiness_failure["reason"],
                        "stage_id": stage_id,
                        "command_index": command_index,
                        "summary": "Backend release gate failed closed before launching the authoritative command.",
                    },
                )
            )
            completed = _CompletedCommand(
                stdout="",
                stderr=f"Backend release gate failed closed: {readiness_failure['reason']}",
                exit_code=None,
                timed_out=False,
            )
        else:
            invocation, shell = _shell_invocation(effective_command)
            monitor = self._monitor_callback(
                task,
                stage_id=stage_id,
                run_id=run_id,
                actor=actor,
                command=effective_command,
                command_index=command_index,
                commands_requested=commands_requested,
            )
            completed = _run_bounded_process(
                invocation,
                shell=shell,
                cwd=self.workdir,
                timeout_seconds=self.timeout_seconds,
                progress_callback=monitor,
            )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        exit_code = completed.exit_code
        timed_out = completed.timed_out
        missing_expected_markers = _missing_expected_markers(stdout, stderr, recipe, command_index=command_index)
        dirty_after = _git_status_lines(self.workdir) if recipe and recipe.get("mode") == "no_product_edit" else None
        dirty_delta = _dirty_delta(dirty_before, dirty_after)
        cleanup_receipt = _cleanup_recipe_probe_files(self.workdir, dirty_delta, recipe, recipe_sandbox)
        finished_at = now()
        duration_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))
        proof_id = self._proof_id(task.id, stage_id, run_id, command_index)
        relative_artifact = Path("proofs") / task.id / "artifacts" / f"{proof_id}.log"
        artifact_path = self.artifact_root / relative_artifact
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        redacted_command = _redact_text(effective_command)
        redacted_original_command = _redact_text(adapted.original_command)
        safe_proof_intent = _safe_proof_intent(proof_intent)
        safe_environment_fingerprint = _safe_environment_fingerprint(environment_fingerprint)
        safe_environment_fingerprint_status = _safe_environment_fingerprint_status(environment_fingerprint_status)
        artifact_lines = [
            f"command: {redacted_command}",
            f"workdir: {_display_workdir(self.workdir)}",
            f"started_at: {started_at.isoformat()}",
            f"finished_at: {finished_at.isoformat()}",
            f"exit_code: {exit_code}",
            f"timed_out: {timed_out}",
        ]
        if safe_proof_intent:
            artifact_lines.append(f"proof_intent: {safe_proof_intent}")
        if safe_environment_fingerprint:
            artifact_lines.append(f"environment_fingerprint: {safe_environment_fingerprint}")
        if safe_environment_fingerprint_status:
            artifact_lines.append(f"environment_fingerprint_status: {safe_environment_fingerprint_status}")
        if recipe:
            artifact_lines.append(f"proof_recipe_id: {recipe.get('recipe_id')}")
            artifact_lines.append(f"proof_recipe_hash: {recipe.get('recipe_hash')}")
            artifact_lines.append(f"proof_recipe_mode: {recipe.get('mode')}")
            if recipe_sandbox:
                artifact_lines.append(f"proof_recipe_sandbox: {recipe_sandbox.relative_to(self.artifact_root).as_posix() if _is_relative_to(recipe_sandbox, self.artifact_root) else recipe_sandbox.name}")
            if dirty_delta:
                artifact_lines.extend(["--- dirty_delta ---", "\n".join(dirty_delta)])
            if missing_expected_markers:
                artifact_lines.extend(["--- missing_expected_markers ---", "\n".join(missing_expected_markers)])
            if cleanup_receipt:
                artifact_lines.append(f"cleanup_receipt: {cleanup_receipt.get('status')}")
        if readiness_failure is not None:
            artifact_lines.append("environment_readiness_status: failed")
            artifact_lines.append(f"environment_readiness_check: {readiness_failure['check_id']}")
            artifact_lines.append(f"environment_readiness_reason: {readiness_failure['reason']}")
        if adapted.adapter:
            artifact_lines.extend(
                [
                    f"original_command: {redacted_original_command}",
                    f"command_adapter: {adapted.adapter}",
                ]
            )
        artifact_lines.extend(["--- stdout ---", stdout, "--- stderr ---", stderr])
        redacted_artifact = _redact_text("\n".join(artifact_lines))
        artifact_path.write_text(redacted_artifact, encoding="utf-8")
        status = "timeout" if timed_out else "passed" if exit_code == 0 else "failed"
        if recipe and recipe.get("mode") == "no_product_edit" and dirty_delta:
            status = "failed"
        if recipe and missing_expected_markers:
            status = "failed"
        if readiness_failure is not None:
            status = "failed"
        try:
            artifact_bytes = artifact_path.stat().st_size
            artifact_exists = True
        except OSError:
            artifact_bytes = None
            artifact_exists = False
        metadata = {
            "actor_requested": actor,
            "run_id": run_id,
            "command": redacted_command,
            "command_index": command_index,
            "commands_requested": commands_requested,
            "exit_code": exit_code,
            "status": status,
            "timed_out": timed_out,
            "timeout_seconds": self.timeout_seconds,
            "duration_ms": duration_ms,
            "shell": _shell_label(),
            "workdir_label": _safe_workdir_label(self.workdir),
            "workdir_is_harness_worktree": _is_harness_worktree(self.workdir),
            "workdir_head_state": _git_head_state(self.workdir),
            "artifact_exists": artifact_exists,
            "artifact_bytes": artifact_bytes,
            "artifact_relative_path": relative_artifact.as_posix(),
            "stdout_excerpt": _excerpt(_redact_text(stdout)),
            "stderr_excerpt": _excerpt(_redact_text(stderr)),
        }
        if recipe:
            metadata.update({f"proof_recipe_{key}": value for key, value in recipe.items() if key in {"recipe_id", "recipe_version", "recipe_hash", "repo_scope", "mode", "writes_product_probe", "cleanup"}})
            metadata["proof_recipe"] = recipe
            if recipe_sandbox:
                sandbox_manifest = recipe_sandbox / "manifest.json"
                metadata["proof_recipe_sandbox_manifest"] = _artifact_relative_path(sandbox_manifest, self.artifact_root)
            if dirty_before is not None:
                metadata["dirty_delta_status"] = "dirty_delta_blocked" if dirty_delta else "clean"
                metadata["dirty_delta_count"] = len(dirty_delta)
            if dirty_delta:
                metadata["dirty_delta_excerpt"] = _safe_dirty_lines(dirty_delta)
            if missing_expected_markers:
                metadata["missing_expected_markers"] = missing_expected_markers
            if cleanup_receipt:
                metadata["cleanup_status"] = cleanup_receipt.get("status")
                metadata["cleanup_file_count"] = cleanup_receipt.get("file_count")
        if safe_proof_intent:
            metadata["proof_intent"] = safe_proof_intent
        if safe_environment_fingerprint:
            metadata["environment_fingerprint"] = safe_environment_fingerprint
        if safe_environment_fingerprint_status:
            metadata["environment_fingerprint_status"] = safe_environment_fingerprint_status
        if adapted.adapter:
            metadata["original_command"] = redacted_original_command
            metadata["command_adapter"] = adapted.adapter
        if readiness_failure is not None:
            metadata["environment_readiness_status"] = "failed"
            metadata["environment_readiness_check_id"] = readiness_failure["check_id"]
            metadata["environment_readiness_reason"] = readiness_failure["reason"]
            metadata["backend_release_gate_fail_closed"] = True
        proof = Proof(
            id=proof_id,
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.TEST_RUN,
            title=f"Command proof: {redacted_command[:80]}",
            path_or_value=relative_artifact.as_posix(),
            created_by="harness",
            created_at=finished_at,
            metadata=metadata,
            redaction_status="safe",
        )
        return self.proof_store.attach(proof)

    def _monitor_callback(
        self,
        task: Task,
        *,
        stage_id: str | None,
        run_id: str | None,
        actor: str,
        command: str,
        command_index: int,
        commands_requested: int,
    ) -> Callable[[dict[str, object]], None]:
        redacted_command = _redact_text(command)

        def emit(update: dict[str, object]) -> None:
            status = str(update.get("status") or "running")
            step = f"proof_command_{status}"
            payload = {
                "type": "run.progress",
                "phase": "proof",
                "step": step,
                "status": status,
                "summary": _proof_monitor_summary(status, redacted_command, update),
                "stage_id": stage_id,
                "command_index": command_index,
                "commands_requested": commands_requested,
                "elapsed_seconds": update.get("elapsed_seconds"),
                "timeout_seconds": self.timeout_seconds,
                "pid": update.get("pid"),
                "exit_code": update.get("exit_code"),
                "timed_out": update.get("timed_out"),
            }
            payload = {key: value for key, value in payload.items() if value is not None}
            self.event_log.append(Event(now(), "run.progress", task.id, run_id, actor, payload))

        return emit

    @staticmethod
    def _proof_id(task_id: str, stage_id: str | None, run_id: str | None, command_index: int) -> str:
        parts = ["test", _safe_token(task_id), _safe_token(stage_id or "stage"), _safe_token(run_id or "run"), str(command_index), uuid.uuid4().hex[:8]]
        return "_".join(part for part in parts if part)


def _safe_recipe_metadata(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    recipe_id = _safe_recipe_token(value.get("recipe_id"))
    recipe_hash = _safe_recipe_token(value.get("recipe_hash"))
    if not recipe_id or not recipe_hash:
        return None
    mode = str(value.get("mode") or "no_product_edit").strip()
    if mode not in {"no_product_edit", "product_probe"}:
        mode = "no_product_edit"
    cleanup = str(value.get("cleanup") or "manifest_verified").strip()
    if cleanup not in {"manifest_verified", "none"}:
        cleanup = "manifest_verified"
    metadata: dict[str, Any] = {
        "recipe_id": recipe_id,
        "recipe_hash": recipe_hash,
        "mode": mode,
        "cleanup": cleanup,
        "writes_product_probe": bool(value.get("writes_product_probe")),
    }
    for key in ("recipe_version", "timeout_seconds"):
        try:
            number = int(value.get(key))
        except (TypeError, ValueError):
            continue
        if number > 0:
            metadata[key] = number
    repo_scope = _safe_recipe_token(value.get("repo_scope"))
    if repo_scope:
        metadata["repo_scope"] = repo_scope
    markers = value.get("expected_markers")
    if isinstance(markers, list):
        metadata["expected_markers"] = [_safe_recipe_token(item) for item in markers if _safe_recipe_token(item)][:10]
    markers_by_command = value.get("expected_markers_by_command")
    if isinstance(markers_by_command, list):
        safe_by_command: list[list[str]] = []
        for group in markers_by_command[:20]:
            if not isinstance(group, list):
                safe_by_command.append([])
                continue
            safe_by_command.append([_safe_recipe_token(item) for item in group if _safe_recipe_token(item)][:10])
        if safe_by_command:
            metadata["expected_markers_by_command"] = safe_by_command
    return metadata


def _backend_release_gate_readiness_failure(command: str, workdir: Path) -> dict[str, str] | None:
    if not _requires_backend_release_docker_precheck(command, workdir):
        return None
    for check_id, argv in (
        ("docker_version", ["docker", "version"]),
        ("docker_ps", ["docker", "ps"]),
    ):
        try:
            result = subprocess.run(
                argv,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                timeout=15,
            )
        except FileNotFoundError:
            return {"check_id": check_id, "reason": "docker_not_found"}
        except subprocess.TimeoutExpired:
            return {"check_id": check_id, "reason": "docker_probe_timeout"}
        except OSError:
            return {"check_id": check_id, "reason": "docker_probe_failed"}
        if result.returncode != 0:
            reason = "docker_daemon_unavailable" if check_id == "docker_ps" else "docker_unavailable"
            detail = _excerpt(_redact_text((result.stderr or result.stdout or "").strip()))
            return {"check_id": check_id, "reason": reason if not detail else f"{reason}: {detail}"}
    return None


def _requires_backend_release_docker_precheck(command: str, workdir: Path) -> bool:
    text = str(command or "").strip().lower().replace("\\", "/")
    if "scripts/test.sh" not in text:
        return False
    if "--sqlite" in text or " --dry-run" in text:
        return False
    workdir_text = str(workdir).lower().replace("\\", "/")
    return "eternia-backend" in workdir_text or "backend" in text


def _prepare_recipe_sandbox(task: Task, recipe: dict[str, Any], *, command_index: int) -> Path:
    sandbox = paths.proof_sandbox_dir(task.id, str(recipe["recipe_id"])) / f"command_{command_index}"
    sandbox.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "created_at": now().isoformat(),
        "task_id": task.id,
        "recipe_id": recipe.get("recipe_id"),
        "recipe_hash": recipe.get("recipe_hash"),
        "mode": recipe.get("mode"),
        "cleanup": recipe.get("cleanup"),
        "writes_product_probe": recipe.get("writes_product_probe"),
        "preserves_evidence": True,
    }
    (sandbox / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return sandbox


def _git_status_lines(root: Path) -> list[str] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "status", "--short", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return None
    if completed.returncode != 0:
        return None
    return sorted(line.strip().replace("\\", "/") for line in completed.stdout.splitlines() if line.strip())


def _dirty_delta(before: list[str] | None, after: list[str] | None) -> list[str]:
    if before is None or after is None:
        return []
    prior = set(before)
    return [line for line in after if line not in prior and not _is_harness_litter_status_line(line)]


def _is_harness_litter_status_line(line: str) -> bool:
    raw = str(line or "").replace("\\", "/").strip()
    path = raw[3:].strip() if len(raw) > 3 else raw
    return any(part.startswith(".hermes-tmp.") for part in path.split("/") if part)


def _missing_expected_markers(stdout: str, stderr: str, recipe: dict[str, Any] | None, *, command_index: int) -> list[str]:
    if not recipe:
        return []
    markers_by_command = recipe.get("expected_markers_by_command")
    if isinstance(markers_by_command, list) and command_index < len(markers_by_command):
        markers = markers_by_command[command_index]
    else:
        markers = recipe.get("expected_markers")
    if not isinstance(markers, list):
        return []
    output = f"{stdout}\n{stderr}".lower()
    missing: list[str] = []
    for marker in markers:
        text = str(marker or "").strip()
        if not text:
            continue
        if text.lower() not in output:
            missing.append(text[:96])
    return missing


def _cleanup_recipe_probe_files(workdir: Path, dirty_delta: list[str], recipe: dict[str, Any] | None, sandbox: Path | None) -> dict[str, Any] | None:
    if not recipe or not sandbox or not dirty_delta:
        return None
    if not recipe.get("writes_product_probe") or recipe.get("cleanup") != "manifest_verified":
        return None
    cleaned: list[str] = []
    preserved: list[str] = []
    root = workdir.resolve()
    probe_dir = sandbox / "product_probes"
    probe_dir.mkdir(parents=True, exist_ok=True)
    for line in dirty_delta:
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip()
        if not rel or rel.startswith(("/", "~")) or ":" in rel:
            continue
        target = (root / rel).resolve()
        if not _is_relative_to(target, root) or not target.is_file():
            continue
        preserved_path = probe_dir / _safe_token(rel)
        try:
            shutil.copy2(target, preserved_path)
            target.unlink()
            cleaned.append(_safe_dirty_line(line))
            preserved.append(preserved_path.name)
        except OSError:
            continue
    receipt = {
        "schema_version": 1,
        "created_at": now().isoformat(),
        "status": "cleaned" if cleaned else "nothing_cleaned",
        "file_count": len(cleaned),
        "cleaned": cleaned,
        "preserved": preserved,
    }
    (sandbox / "cleanup_receipt.json").write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return receipt


def _safe_dirty_lines(lines: list[str]) -> list[str]:
    return [_safe_dirty_line(line) for line in lines[:20]]


def _safe_dirty_line(line: str) -> str:
    text = str(line or "").replace("\\", "/").strip()
    if re.search(r"(?i)[A-Z]:/", text) or text.startswith(("/", "~")):
        return "<path-withheld>"
    return text[:180]


def _artifact_relative_path(path: Path, artifact_root: Path) -> str:
    if _is_relative_to(path, artifact_root):
        return path.relative_to(artifact_root).as_posix()
    return path.name


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _safe_recipe_token(value: Any) -> str | None:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", text):
        return None
    return text


def adapt_command_for_proof(command: str, *, windows_host: bool | None = None, workdir: str | Path | None = None) -> AdaptedCommand:
    """Normalize proof commands for the host without changing caller intent.

    Windows pytest-timeout configurations commonly use ``--timeout-method=signal``
    in repo addopts, which is POSIX-only because Windows has no SIGALRM. Harness
    command proof should collect the requested pytest evidence, not fail before
    collection due to the host-incompatible timeout plugin config. For pytest
    commands on Windows, run pytest through the active Python and override the
    incompatible addopts/plugin.
    """
    original = str(command).strip()
    if not original:
        return AdaptedCommand(command=original, original_command=original)
    adapters: list[str] = []
    adapted = _strip_redacted_workdir_prefix(original)
    if adapted != original:
        adapters.append(REDACTED_WORKDIR_PREFIX_ADAPTER)
    backend_venv_adapted = _adapt_eternia_backend_manage_py_venv(adapted, workdir=workdir)
    if backend_venv_adapted != adapted:
        adapted = backend_venv_adapted
        adapters.append(ETERNIA_BACKEND_VENV_PYTHON_ADAPTER)
    django_adapted = _adapt_eternia_backend_django_command(adapted, workdir=workdir)
    if django_adapted != adapted:
        adapted = django_adapted
        adapters.append(ETERNIA_BACKEND_DJANGO_SETTINGS_ADAPTER)
    if windows_host is None:
        windows_host = _is_windows_host()
    if windows_host:
        pytest_adapted = _adapt_windows_pytest_command(adapted)
        if pytest_adapted != adapted:
            adapted = pytest_adapted
            adapters.append(PYTEST_TIMEOUT_ADAPTER)
    if adapted == original:
        return AdaptedCommand(command=original, original_command=original)
    return AdaptedCommand(command=adapted, original_command=original, adapter=",".join(adapters) or None)


def _append_command_adapter(existing: str | None, adapter: str) -> str:
    if not existing:
        return adapter
    adapters = [item.strip() for item in existing.split(",") if item.strip()]
    if adapter not in adapters:
        adapters.append(adapter)
    return ",".join(adapters)


def _strip_redacted_workdir_prefix(command: str) -> str:
    patterns = (
        r"^\s*cd\s+(?:'|\"|`)?<path:[^>]+>(?:'|\"|`)?\s*(?:&&|;)\s*",
        r"^\s*(?:set-location|push-location)\s+(?:-literalpath\s+)?(?:'|\"|`)?<path:[^>]+>(?:'|\"|`)?\s*;+\s*",
    )
    for pattern in patterns:
        stripped = re.sub(pattern, "", command, count=1, flags=re.IGNORECASE)
        if stripped != command and stripped.strip():
            return stripped.strip()
    return command


def _adapt_eternia_backend_django_command(command: str, *, workdir: str | Path | None = None) -> str:
    if "DJANGO_SETTINGS_MODULE" in command:
        return command
    if "manage.py" in command:
        return command
    if not _looks_like_django_inline_python(command):
        return command
    if not _looks_like_eternia_backend_context(command, workdir=workdir):
        return command
    return f"export DJANGO_SETTINGS_MODULE=backend.settings && {command}"


def _adapt_eternia_backend_manage_py_venv(command: str, *, workdir: str | Path | None = None) -> str:
    if "manage.py" not in command or not _looks_like_eternia_backend_context(command, workdir=workdir):
        return command
    return adapt_eternia_backend_manage_py_command(command)


def adapt_eternia_backend_manage_py_command(command: str) -> str:
    """Rewrite a naked ``python manage.py …`` to the canonical repo-venv interpreter.

    Context-free core of ``_adapt_eternia_backend_manage_py_venv``; callers are
    responsible for asserting the command targets the EterniaBackend repo. Also
    reused when normalizing agent-facing handoff self-test commands so devs are
    told the interpreter that actually exists in a grounded worktree instead of
    re-discovering it every goal.
    """
    if ".EterniaBackendVirtualEnv/Scripts/python.exe" in command:
        return command
    stripped = re.sub(
        r"^\s*(?:source|\.)\s+(?:\.?/)?venv/Scripts/activate\s*(?:&&|;)\s*",
        "",
        command,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    match = re.match(r"^(?:python|python\.exe|python3|python3\.exe)\s+manage\.py\b(?P<rest>.*)$", stripped, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return f".EterniaBackendVirtualEnv/Scripts/python.exe manage.py{match.group('rest')}"
    return command


def _looks_like_django_inline_python(command: str) -> bool:
    lower = command.lower()
    if "python" not in lower:
        return False
    return bool(
        re.search(r"\bdjango\.setup\s*\(", lower)
        or re.search(r"\bimport\s+django\b", lower)
        or re.search(r"\bfrom\s+django(?:\.|\s+import\b)", lower)
    )


def _looks_like_eternia_backend_context(command: str, *, workdir: str | Path | None = None) -> bool:
    haystacks = [command.lower()]
    if workdir is not None:
        haystacks.append(str(workdir).lower())
    text = " ".join(haystacks)
    return any(
        marker in text
        for marker in (
            "eterniabackend",
            "eternia-backend",
            ".eterniabackendvirtualenv",
            "django_env",
        )
    )


def _adapt_windows_pytest_command(command: str) -> str:
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        return command
    if not parts:
        return command
    if _has_pytest_timeout_override(parts):
        return command
    remainder: list[str] | None = None
    if _is_pytest_executable(parts[0]):
        remainder = parts[1:]
    elif len(parts) >= 3 and _is_python_executable(parts[0]) and parts[1] == "-m" and parts[2] == "pytest":
        remainder = parts[3:]
    if remainder is None:
        return command
    adapted_parts = ["python", "-m", "pytest", "-o", "addopts=", "-p", "no:timeout", *remainder]
    return _join_command(adapted_parts)


def _is_windows_host() -> bool:
    return os.name == "nt"


def _is_pytest_executable(value: str) -> bool:
    normalized = Path(value).name.lower()
    return normalized in {"pytest", "pytest.exe"}


def _is_python_executable(value: str) -> bool:
    normalized = Path(value).name.lower()
    return normalized in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}


def _has_pytest_timeout_override(parts: list[str]) -> bool:
    joined = " ".join(parts)
    return "-p no:timeout" in joined or "--timeout-method" in joined or "addopts=" in joined


def _join_command(parts: list[str]) -> str:
    rendered: list[str] = []
    for part in parts:
        if part == "addopts=":
            rendered.append("addopts=''")
        else:
            rendered.append(shlex.quote(part))
    return " ".join(rendered)


def _safe_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", value).strip("_")[:48] or "item"


def _shell_invocation(command: str) -> tuple[str | list[str], bool]:
    """Return a shell invocation that matches Hermes operator semantics.

    On Windows, Python's ``shell=True`` dispatches through ``cmd.exe`` by
    default, but Hermes operator commands and agent-requested proof commands use
    Git Bash/POSIX syntax (single quotes, ``&&``, ``$(date ...)``, ``find``).
    Prefer bash explicitly when available so deterministic proof collection runs
    the same language the operator tested in terminal.
    """
    bash = _bash_executable()
    if bash:
        return [bash, "-lc", command], False
    return command, True


def _run_bounded_process(
    invocation: str | list[str],
    *,
    shell: bool,
    cwd: Path,
    timeout_seconds: int,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
) -> _CompletedCommand:
    kwargs: dict[str, object] = {
        "shell": shell,
        "cwd": cwd,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    with tempfile.TemporaryDirectory(prefix="hermes-proof-") as temp_dir:
        stdout_path = Path(temp_dir) / "stdout.log"
        stderr_path = Path(temp_dir) / "stderr.log"
        with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout_handle, stderr_path.open("w", encoding="utf-8", errors="replace") as stderr_handle:
            proc = subprocess.Popen(invocation, stdout=stdout_handle, stderr=stderr_handle, text=True, **kwargs)
            _emit_process_update(progress_callback, "started", proc.pid, 0, timeout_seconds)
            started = time.monotonic()
            timed_out = False
            exit_code: int | None = None
            while True:
                exit_code = proc.poll()
                elapsed_seconds = int(time.monotonic() - started)
                if exit_code is not None:
                    _emit_process_update(progress_callback, "finished", proc.pid, elapsed_seconds, timeout_seconds, exit_code=exit_code, timed_out=False)
                    break
                if elapsed_seconds >= timeout_seconds:
                    timed_out = True
                    _emit_process_update(progress_callback, "timeout", proc.pid, elapsed_seconds, timeout_seconds, timed_out=True)
                    _terminate_process_tree(proc)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)
                    exit_code = None
                    break
                wait_seconds = min(PROOF_MONITOR_HEARTBEAT_SECONDS, max(1, timeout_seconds - elapsed_seconds))
                try:
                    exit_code = proc.wait(timeout=wait_seconds)
                    elapsed_seconds = int(time.monotonic() - started)
                    _emit_process_update(progress_callback, "finished", proc.pid, elapsed_seconds, timeout_seconds, exit_code=exit_code, timed_out=False)
                    break
                except subprocess.TimeoutExpired:
                    elapsed_seconds = int(time.monotonic() - started)
                    _emit_process_update(progress_callback, "running", proc.pid, elapsed_seconds, timeout_seconds)
                    continue
        stdout = _read_process_output(stdout_path)
        stderr = _read_process_output(stderr_path)
        if timed_out:
            stderr += f"\n[command timed out after {timeout_seconds}s]"
        return _CompletedCommand(stdout=stdout, stderr=stderr, exit_code=exit_code, timed_out=timed_out)


def _emit_process_update(
    progress_callback: Callable[[dict[str, object]], None] | None,
    status: str,
    pid: int,
    elapsed_seconds: int,
    timeout_seconds: int,
    *,
    exit_code: int | None = None,
    timed_out: bool | None = None,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        {
            "status": status,
            "pid": pid,
            "elapsed_seconds": elapsed_seconds,
            "timeout_seconds": timeout_seconds,
            "exit_code": exit_code,
            "timed_out": timed_out,
        }
    )


def _read_process_output(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return
        except Exception:
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def _bash_executable() -> str | None:
    if os.name == "nt":
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\usr\bin\bash.exe",
        ):
            if Path(candidate).exists():
                return candidate
        for name in ("bash.exe", "bash"):
            resolved = shutil.which(name)
            if resolved and not _is_windows_subsystem_bash(resolved):
                return resolved
        return None
    return shutil.which("bash")


def _is_windows_subsystem_bash(path: str) -> bool:
    normalized = str(path).replace("/", "\\").lower()
    return "\\windows\\system32\\bash.exe" in normalized or "\\windowsapps\\bash.exe" in normalized


def _shell_label() -> str:
    return "bash" if _bash_executable() else ("cmd" if os.name == "nt" else "system-shell")


def _proof_monitor_summary(status: str, command: str, update: dict[str, object]) -> str:
    elapsed = update.get("elapsed_seconds")
    timeout = update.get("timeout_seconds")
    prefix = {
        "started": "Command proof started",
        "running": "Command proof still running",
        "timeout": "Command proof timed out; process tree terminated",
        "finished": "Command proof finished",
    }.get(status, "Command proof updated")
    timing = ""
    if elapsed is not None and timeout is not None:
        timing = f" ({elapsed}s/{timeout}s)"
    return f"{prefix}{timing}: {command[:120]}"


def _coerce_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _excerpt(text: str, *, limit: int = MAX_PROOF_EXCERPT_CHARS) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(normalized) <= limit:
        return normalized
    head = max(0, limit // 2)
    tail = max(0, limit - head)
    omitted = len(normalized) - head - tail
    return f"{normalized[:head]}\n[... {omitted} chars omitted ...]\n{normalized[-tail:]}"


def _display_workdir(workdir: Path) -> str:
    return "." if not workdir.is_absolute() else f"<workdir:{workdir.name}>"


def _safe_workdir_label(workdir: Path) -> str:
    label = "." if not workdir.is_absolute() else workdir.name
    return re.sub(r"[^A-Za-z0-9_. -]+", "_", label).strip()[:120] or "unknown"


def _is_harness_worktree(workdir: Path) -> bool:
    try:
        resolved = workdir.resolve()
    except OSError:
        return False
    return "wt" in {part.lower() for part in resolved.parts} or resolved.parent.name.lower() == "hermes-agent-wt"


def _git_head_state(workdir: Path) -> str:
    try:
        resolved = workdir.resolve()
    except OSError:
        return "unknown"
    if not (resolved / ".git").exists():
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=resolved,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except Exception:
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    branch = (result.stdout or "").strip()
    return "detached" if branch == "HEAD" else "branch"


def _redact_text(text: str) -> str:
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    for pattern in _ABSOLUTE_PATH_PATTERNS:
        redacted = pattern.sub(lambda match: _redacted_path(match.group(0)), redacted)
    return redacted


def _safe_proof_intent(value: str | None) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    return _redact_text(text)[:500]


def _safe_environment_fingerprint(value: str | None) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    redacted = _redact_text(text)
    if redacted == text and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", redacted):
        return redacted
    return "sha256:" + hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:16]


def _safe_environment_fingerprint_status(value: str | None) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"unknown", "recorded", "changed", "unchanged", "blocked", "missing"}:
        return text
    if re.fullmatch(r"[a-z0-9_.:-]{1,80}", text):
        return text
    return None


def _redacted_path(value: str) -> str:
    normalized = value.replace("\\", "/").rstrip("/")
    name = normalized.rsplit("/", 1)[-1] or "path"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)[:80] or "path"
    return f"<path:{safe_name}>"
