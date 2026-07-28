from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_time import now

from . import paths
from .models import Proof, Task
from .proof_capture import ScreenshotRequest, VideoRequest, VisualCaptureProvider
from .proof_rules import ProofType
from .stagec_mcp_visual_provider import StageCMcpError, default_launcher_qa_visual_provider
from .store import ProofStore


@dataclass(frozen=True, slots=True)
class VisualProofResult:
    proof: Proof
    environment_blocker: bool = False
    blocker_summary: str | None = None


class VisualProofRunner:
    def __init__(self, *, proof_store: ProofStore | None = None, provider: VisualCaptureProvider | None = None, artifact_root: str | Path | None = None):
        self.proof_store = proof_store or ProofStore()
        self.provider = provider if provider is not None else default_launcher_qa_visual_provider()
        self.artifact_root = Path(artifact_root).expanduser() if artifact_root is not None else paths.store_root()

    def capture(self, task: Task, *, stage_id: str | None, run_id: str | None, actor: str, request: dict[str, Any], kind: str) -> VisualProofResult:
        if self.provider is None:
            proof = self._blocked_proof(task, stage_id=stage_id, run_id=run_id, actor=actor, request=request, summary="launcher_qa MCP capture provider is unavailable")
            return VisualProofResult(proof=proof, environment_blocker=True, blocker_summary="launcher_qa MCP capture provider is unavailable")
        scenario = str(request.get("target") or request.get("proof_requirement") or "visual_proof")[:160]
        try:
            if kind == "video":
                capture = self.provider.capture_video(VideoRequest(task_id=task.id, stage_id=stage_id, scenario=scenario, provider=str(request.get("mcp_server") or "launcher_qa"), metadata=dict(request), duration_seconds=int(request.get("duration_seconds") or 8)))
                proof_type = ProofType.VIDEO
            else:
                capture = self.provider.capture_screenshot(ScreenshotRequest(task_id=task.id, stage_id=stage_id, scenario=scenario, provider=str(request.get("mcp_server") or "launcher_qa"), metadata=dict(request)))
                proof_type = ProofType.SCREENSHOT
        except Exception as exc:
            summary = _safe_visual_failure_summary(exc)
            provider_metadata = exc.provider_metadata if isinstance(exc, StageCMcpError) else None
            proof = self._blocked_proof(
                task,
                stage_id=stage_id,
                run_id=run_id,
                actor=actor,
                request=request,
                summary=summary,
                failure_class=type(exc).__name__,
                provider_metadata=provider_metadata if isinstance(provider_metadata, dict) else None,
            )
            return VisualProofResult(proof=proof, environment_blocker=True, blocker_summary=summary)

        relative_path, artifact_exists, artifact_bytes = self._materialize_artifact(task, capture.path)
        pinned = _pins_are_present(request)
        passed = artifact_exists and artifact_bytes > 0 and pinned
        status = "passed" if passed else "failed"
        proof_id = f"{proof_type.value}_{_safe_token(task.id)}_{_safe_token(stage_id or 'stage')}_{uuid.uuid4().hex[:8]}"
        metadata = {
            "actor_requested": actor,
            "run_id": run_id,
            "status": status,
            "capture_provider": capture.capture_provider,
            "mcp_server": str(request.get("mcp_server") or ""),
            "target": str(request.get("target") or "")[:160],
            "proof_requirement": str(request.get("proof_requirement") or "")[:280],
            "artifact_exists": artifact_exists,
            "artifact_bytes": artifact_bytes,
            "artifact_relative_path": relative_path,
            "runtime_root_id": str((request.get("required_launch_pins") or {}).get("runtime_root_id") or "")[:128],
            "hermes_profile": str((request.get("required_launch_pins") or {}).get("hermes_profile") or "")[:128],
            "semantic_summary": f"{proof_type.value} capture {status}",
        }
        if isinstance(getattr(capture, "provider_metadata", None), dict) and capture.provider_metadata:
            metadata["provider_metadata"] = capture.provider_metadata
        proof = Proof(
            id=proof_id,
            task_id=task.id,
            stage_id=stage_id,
            type=proof_type,
            title=f"{proof_type.value.title()} proof: {scenario[:80]}",
            path_or_value=relative_path,
            created_by="harness",
            created_at=now(),
            metadata=metadata,
            redaction_status="safe" if passed else "needs_scan",
        )
        return VisualProofResult(proof=self.proof_store.attach(proof), environment_blocker=not passed, blocker_summary=None if passed else "visual capture artifact was blank, stale, or missing launch pins")

    def _blocked_proof(
        self,
        task: Task,
        *,
        stage_id: str | None,
        run_id: str | None,
        actor: str,
        request: dict[str, Any],
        summary: str,
        failure_class: str | None = None,
        provider_metadata: dict[str, Any] | None = None,
    ) -> Proof:
        proof_id = f"visual_blocker_{_safe_token(task.id)}_{_safe_token(stage_id or 'stage')}_{uuid.uuid4().hex[:8]}"
        metadata = {
            "actor_requested": actor,
            "run_id": run_id,
            "kind": "visual_mcp_environment_blocker",
            "status": "failed",
            "mcp_server": str(request.get("mcp_server") or ""),
            "target": str(request.get("target") or "")[:160],
            "summary": summary[:280],
            "failure_class": str(failure_class or "")[:120],
        }
        if provider_metadata:
            metadata["provider_metadata"] = provider_metadata
        proof = Proof(
            id=proof_id,
            task_id=task.id,
            stage_id=stage_id,
            type=ProofType.LOG,
            title="Visual proof environment blocker",
            path_or_value="visual_capture:blocked",
            created_by="harness",
            created_at=now(),
            metadata=metadata,
            redaction_status="safe",
        )
        return self.proof_store.attach(proof)

    def _materialize_artifact(self, task: Task, source_path: str) -> tuple[str, bool, int]:
        if not source_path:
            return "", False, 0
        source = Path(source_path)
        if not source.is_absolute():
            candidate = self.artifact_root / source
            if candidate.exists():
                size = candidate.stat().st_size
                return source.as_posix(), True, size
            return source.as_posix(), False, 0
        dest = Path("proofs") / task.id / "artifacts" / source.name
        full_dest = self.artifact_root / dest
        try:
            full_dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, full_dest)
            size = full_dest.stat().st_size
            return dest.as_posix(), True, size
        except OSError:
            return dest.as_posix(), False, 0


def _pins_are_present(request: dict[str, Any]) -> bool:
    pins = request.get("required_launch_pins")
    return isinstance(pins, dict) and bool(str(pins.get("runtime_root_id") or "").strip()) and bool(str(pins.get("hermes_profile") or "").strip())


def _safe_visual_failure_summary(exc: Exception) -> str:
    failure_class = type(exc).__name__
    message = " ".join(str(exc or "").split())
    if not message:
        return f"visual capture failed: {failure_class}"
    message = _redact_visual_failure_detail(message)
    return f"visual capture failed: {failure_class}: {message[:220]}"


def _redact_visual_failure_detail(text: str) -> str:
    redacted = re.sub(r"([A-Za-z]:\\[^ \n\r\t]+|[A-Za-z]:/[^ \n\r\t]+)", "<path>", text)
    redacted = re.sub(r"(?i)(api[_-]?key|authorization|cookie|bearer|password|credential|secret|token)[^ \n\r\t]*", "<redacted>", redacted)
    return redacted


def _safe_token(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(value or ""))
    return text[:48] or "item"
