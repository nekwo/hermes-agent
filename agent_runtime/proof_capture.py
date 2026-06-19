from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from hermes_time import now


@dataclass(slots=True)
class CapturedTestRun:
    command: str
    cwd: str
    exit_code: int
    duration_ms: int
    stdout_path: str
    stderr_path: str
    started_at: str = field(default_factory=lambda: now().isoformat())
    finished_at: str = field(default_factory=lambda: now().isoformat())

    def metadata(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ScreenshotRequest:
    task_id: str
    stage_id: str | None
    scenario: str
    provider: str = "manual"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VideoRequest(ScreenshotRequest):
    duration_seconds: int = 10


@dataclass(slots=True)
class CapturedArtifact:
    path: str
    capture_provider: str
    scenario: str
    redaction_status: str = "needs_scan"
    width: int | None = None
    height: int | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None and v != {}}


class TestRunner(Protocol):
    def run(self, command: str, *, cwd: Path, timeout_s: int) -> CapturedTestRun: ...


class VisualCaptureProvider(Protocol):
    def capture_screenshot(self, request: ScreenshotRequest) -> CapturedArtifact: ...
    def capture_video(self, request: VideoRequest) -> CapturedArtifact: ...
