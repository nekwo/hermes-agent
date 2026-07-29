from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from hermes_time import now
from utils import atomic_json_write

from . import paths
from .events import EventLog
from .models import AgentRun, Event


@dataclass(slots=True)
class SelfTestEvidence:
    schema_version: int
    evidence_id: str
    task_id: str
    worker_session_id: str | None
    run_id: str
    persona_id: str
    stage_id: str | None
    repo_label: str | None
    workdir_label: str | None
    command_label: str
    command_hash: str
    exit_code: int | None
    status: str
    started_at: str
    finished_at: str
    elapsed_ms: int | None
    stdout_path: str | None
    stderr_path: str | None
    stdout_excerpt: str | None
    stderr_excerpt: str | None
    redaction_status: str
    source: str
    satisfies_release_gate: bool = False

    def jsonable(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


class SelfTestEvidenceStore:
    def __init__(self, event_log: EventLog | None = None):
        self.event_log = event_log or EventLog()

    def save(self, evidence: SelfTestEvidence) -> SelfTestEvidence:
        path = paths.self_test_record_path(evidence.task_id, evidence.evidence_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(path, evidence.jsonable(), indent=2, sort_keys=True)
        self.event_log.append(
            Event(
                now(),
                "self_test.recorded",
                evidence.task_id,
                evidence.run_id,
                evidence.persona_id,
                {
                    "evidence_id": evidence.evidence_id,
                    "status": evidence.status,
                    "stage_id": evidence.stage_id,
                    "command_label": evidence.command_label[:220],
                    "command_hash": evidence.command_hash,
                    "exit_code": evidence.exit_code,
                    "redaction_status": evidence.redaction_status,
                },
            )
        )
        repeat_count = self.failed_repeat_count(
            task_id=evidence.task_id,
            stage_id=evidence.stage_id,
            command_hash=evidence.command_hash,
        )
        if evidence.status in {"failed", "timeout"} and repeat_count > 1:
            self.event_log.append(
                Event(
                    now(),
                    "self_test.loop_detected",
                    evidence.task_id,
                    evidence.run_id,
                    evidence.persona_id,
                    {
                        "command_hash": evidence.command_hash,
                        "repeat_count": repeat_count,
                        "stage_id": evidence.stage_id,
                        "summary": "Same self-test command failed repeatedly without a recorded changed signal.",
                    },
                )
            )
        return evidence

    def get(self, evidence_id: str) -> SelfTestEvidence:
        for path in paths.self_tests_dir().glob(f"*/{_safe_token(evidence_id)}.json"):
            return _read_evidence(path)
        raise FileNotFoundError(evidence_id)

    def list_for_task(self, task_id: str) -> list[SelfTestEvidence]:
        root = paths.self_test_task_dir(task_id)
        if not root.exists():
            return []
        return [_read_evidence(path) for path in sorted(root.glob("selftest_*.json"))]

    def failed_repeat_count(self, *, task_id: str, stage_id: str | None, command_hash: str) -> int:
        return len(
            [
                item
                for item in self.list_for_task(task_id)
                if item.command_hash == command_hash
                and item.stage_id == stage_id
                and item.status in {"failed", "timeout"}
            ]
        )


def record_self_test_from_progress(
    run: AgentRun,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_log: EventLog | None = None,
) -> SelfTestEvidence | None:
    if event_type != "run.tool.finished":
        return None
    tool_name = str(payload.get("tool_name") or payload.get("tool") or "").strip().lower()
    if tool_name and tool_name not in {
        "terminal",
        "execute_code",
        "code_execution",
        "shell",
        "shell_command",
        "functions.shell_command",
        "powershell",
        "pwsh",
    }:
        return None
    command = _command_from_payload(payload)
    if not looks_like_self_test_command(command):
        return None
    status = _status_from_payload(payload)
    stdout = _safe_excerpt(payload.get("stdout") or payload.get("stdout_excerpt"))
    stderr = _safe_excerpt(payload.get("stderr") or payload.get("stderr_excerpt"))
    evidence_id = f"selftest_{uuid.uuid4().hex[:10]}"
    command_hash = _command_hash(command)
    stdout_path = _write_artifact(run.task_id, evidence_id, "stdout", stdout)
    stderr_path = _write_artifact(run.task_id, evidence_id, "stderr", stderr)
    evidence = SelfTestEvidence(
        schema_version=1,
        evidence_id=evidence_id,
        task_id=run.task_id,
        worker_session_id=_safe_optional_token(payload.get("worker_session_id")),
        run_id=run.id,
        persona_id=run.persona_id,
        stage_id=str(payload.get("stage_id") or run.stage_id or "").strip() or None,
        repo_label=_safe_optional_label(payload.get("repo_label")),
        workdir_label=_safe_optional_label(payload.get("workdir_label")),
        command_label=_safe_command_label(command),
        command_hash=command_hash,
        exit_code=_safe_int_or_none(payload.get("exit_code")),
        status=status,
        started_at=str(payload.get("started_at") or now().isoformat()),
        finished_at=str(payload.get("finished_at") or now().isoformat()),
        elapsed_ms=_safe_int_or_none(payload.get("duration_ms") or payload.get("elapsed_ms")),
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        stdout_excerpt=stdout[:500] if stdout else None,
        stderr_excerpt=stderr[:500] if stderr else None,
        redaction_status=_redaction_status(command, stdout, stderr),
        source="worker_tool",
        satisfies_release_gate=False,
    )
    saved = SelfTestEvidenceStore(event_log=event_log).save(evidence)
    return saved


def looks_like_self_test_command(command: object) -> bool:
    text = str(command or "").strip().lower()
    if not text:
        return False
    preflight_only = ("--version", "doctor", " where ", " which ", "docker ps", "docker version")
    if any(marker in f" {text} " for marker in preflight_only):
        return False
    markers = (
        "flutter test",
        "flutter analyze",
        "dart analyze",
        "pytest",
        "python -m pytest",
        "manage.py test",
        "manage.py check",
        "npm test",
        "pnpm test",
    )
    return any(marker in text for marker in markers)


def self_test_summary(evidence: SelfTestEvidence) -> dict[str, Any]:
    return {
        "evidence_id": evidence.evidence_id,
        "task_id": evidence.task_id,
        "run_id": evidence.run_id,
        "persona_id": evidence.persona_id,
        "stage_id": evidence.stage_id,
        "status": evidence.status,
        "exit_code": evidence.exit_code,
        "command_label": evidence.command_label,
        "redaction_status": evidence.redaction_status,
        "satisfies_release_gate": evidence.satisfies_release_gate,
    }


def _relative_runtime_path(path) -> str:
    resolved = path.resolve()
    root = paths.store_root().resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.name


def _read_evidence(path) -> SelfTestEvidence:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return SelfTestEvidence(
        schema_version=int(raw.get("schema_version", 1)),
        evidence_id=str(raw["evidence_id"]),
        task_id=str(raw["task_id"]),
        worker_session_id=raw.get("worker_session_id"),
        run_id=str(raw["run_id"]),
        persona_id=str(raw["persona_id"]),
        stage_id=raw.get("stage_id"),
        repo_label=raw.get("repo_label"),
        workdir_label=raw.get("workdir_label"),
        command_label=str(raw["command_label"]),
        command_hash=str(raw["command_hash"]),
        exit_code=raw.get("exit_code"),
        status=str(raw["status"]),
        started_at=str(raw["started_at"]),
        finished_at=str(raw["finished_at"]),
        elapsed_ms=raw.get("elapsed_ms"),
        stdout_path=raw.get("stdout_path"),
        stderr_path=raw.get("stderr_path"),
        stdout_excerpt=raw.get("stdout_excerpt"),
        stderr_excerpt=raw.get("stderr_excerpt"),
        redaction_status=str(raw.get("redaction_status") or "needs_scan"),
        source=str(raw.get("source") or "worker_tool"),
        satisfies_release_gate=bool(raw.get("satisfies_release_gate", False)),
    )


def _command_from_payload(payload: dict[str, Any]) -> str:
    for key in ("command", "command_label", "command_full", "summary", "detail"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _status_from_payload(payload: dict[str, Any]) -> str:
    raw = str(payload.get("status") or "").strip().lower()
    exit_code = _safe_int_or_none(payload.get("exit_code"))
    if raw in {"passed", "failed", "timeout"}:
        return raw
    if raw in {"success", "ok", "complete", "completed"}:
        return "passed"
    if raw in {"error", "failure"}:
        return "failed"
    if exit_code is not None:
        return "passed" if exit_code == 0 else "failed"
    # No explicit status and no exit code. The observed lane must stay HONEST:
    # defaulting to "passed" here recorded failed self-tests as passing (live
    # 2026-07-03 — the empty-.env DJANGO_SECRET_KEY tracebacks and the emptied
    # backend venv both logged status=passed, exit_code=None). Infer failure
    # from an actual crash signature in the captured output; otherwise mark it
    # "unknown" rather than claim success. The R1 observed-lane gate is
    # presence-based (any agent_tool_trace proof satisfies it) and the
    # authoritative harness re-run is what enforces pass/fail, so "unknown"
    # narrows only the HUD display — it never green-lights unproven work.
    if _output_shows_failure(payload):
        return "failed"
    return "unknown"


# Only UNAMBIGUOUS crash signatures. A false "failed" (marking a real pass as
# failed) is worse than an honest "unknown", so generic tokens like " failed"
# (matches pytest's "0 failed") or "error" (matches "No issues/errors found")
# are deliberately excluded — they only reliably distinguish pass from fail
# alongside an exit code, which is handled above.
_FAILURE_SIGNATURES = (
    "traceback (most recent call last)",
    "modulenotfounderror",
    "importerror",
    "runtimeerror",
    "assertionerror",
    "syntaxerror",
    "fatal error",
    "command not found",
    "is not recognized as an internal or external command",
    "no module named",
)


def _output_shows_failure(payload: dict[str, Any]) -> bool:
    haystack = " ".join(
        str(payload.get(key) or "")
        for key in ("stderr", "stderr_excerpt", "stdout", "stdout_excerpt", "summary", "detail")
    ).lower()
    if not haystack.strip():
        return False
    return any(signature in haystack for signature in _FAILURE_SIGNATURES)


def _write_artifact(task_id: str, evidence_id: str, stream: str, text: str | None) -> str | None:
    if not text:
        return None
    root = paths.self_test_artifacts_dir(task_id)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{_safe_token(evidence_id)}.{stream}.txt"
    path.write_text(text, encoding="utf-8")
    return str(path.relative_to(paths.store_root())).replace("\\", "/")


def _safe_excerpt(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if _looks_sensitive(text):
        return "redacted"
    return text[:4000]


def _safe_command_label(command: str) -> str:
    text = " ".join(str(command or "").strip().split())
    if not text:
        return "self-test command"
    if _looks_sensitive(text):
        return "redacted self-test command"
    return text[:500]


def _command_hash(command: str) -> str:
    return hashlib.sha256(str(command or "").strip().encode("utf-8")).hexdigest()[:16]


def _redaction_status(*values: str | None) -> str:
    return "needs_scan" if any(value and _looks_sensitive(value) for value in values) else "safe"


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in ("secret", "token", "password", "credential", "authorization", "cookie", "bearer", "api_key", "sk-")):
        return True
    return False


def _safe_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _safe_optional_token(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", text):
        return None
    return text


def _safe_optional_label(value: object) -> str | None:
    text = str(value or "").strip()
    if not text or _looks_sensitive(text):
        return None
    return text[:96]


def _safe_token(value: object) -> str:
    text = str(value or "").strip()
    cleaned = "".join(ch if ch.isalnum() or ch in "_.-" else "_" for ch in text)
    return cleaned.strip("._")[:120] or "item"
