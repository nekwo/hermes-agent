from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_config_path, get_default_hermes_root
from hermes_time import now

from . import paths
from .profile_context import active_profile_name
from .proof_capture import CapturedArtifact, ScreenshotRequest, VideoRequest


class StageCMcpError(RuntimeError):
    pass


_MARIONETTE_KERNEL_MARKERS = (
    b"stagec_qa_command_bus",
    b"StageCDirectQaControlServer",
    b"StageCQaStage19Hooks",
    b"StageCQaSemanticControl",
    b"clickButton",
)


@dataclass(frozen=True, slots=True)
class StageCMcpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 260.0
    connect_timeout_seconds: float = 60.0
    profile_home: Path | None = None


@dataclass(frozen=True, slots=True)
class StageCMcpSmokeResult:
    ok: bool
    code: str
    summary: str


class StageCMcpJsonRpcClient:
    def __init__(self, config: StageCMcpServerConfig):
        self.config = config
        self._proc: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._next_id = 1

    def __enter__(self) -> "StageCMcpJsonRpcClient":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None:
            return
        cmd = [self.config.command, *self.config.args]
        env = os.environ.copy()
        env.update(self.config.env)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(self.config.profile_home) if self.config.profile_home else None,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise StageCMcpError(f"launcher_qa MCP spawn failed: {type(exc).__name__}") from exc
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def initialize(self) -> dict[str, Any]:
        return self.request("initialize", params={}, timeout_seconds=self.config.connect_timeout_seconds)

    def tools_list(self) -> dict[str, Any]:
        return self.request("tools/list", timeout_seconds=min(self.config.connect_timeout_seconds, 30.0))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.request(
            "tools/call",
            params={"name": name, "arguments": arguments},
            timeout_seconds=self.config.timeout_seconds,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise StageCMcpError("launcher_qa MCP tool returned no result object")
        if result.get("isError") is True:
            envelope = _decode_tool_envelope(result)
            failure = str(envelope.get("failure_class") or "mcp_tool_error")
            message = str(envelope.get("message_safe") or envelope.get("summary") or "tool error")
            raise StageCMcpError(f"{failure}: {message[:240]}")
        return _decode_tool_envelope(result)

    def request(self, method: str, *, params: dict[str, Any] | None = None, timeout_seconds: float) -> dict[str, Any]:
        if self._proc is None:
            self.start()
        assert self._proc is not None
        if self._proc.stdin is None:
            raise StageCMcpError("launcher_qa MCP stdin is unavailable")
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params
        try:
            self._proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self._proc.stdin.flush()
        except OSError as exc:
            raise StageCMcpError(f"launcher_qa MCP write failed: {type(exc).__name__}") from exc

        deadline = time.monotonic() + max(1.0, float(timeout_seconds))
        while time.monotonic() < deadline:
            if self._proc.poll() is not None and self._responses.empty():
                raise StageCMcpError(_dead_process_summary(self._proc.returncode, self._stderr_lines))
            try:
                response = self._responses.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                error = response.get("error") if isinstance(response.get("error"), dict) else {}
                message = str(error.get("message") or "JSON-RPC error")
                code = str(error.get("code") or "error")
                raise StageCMcpError(f"launcher_qa MCP {method} failed ({code}): {message[:240]}")
            return response
        raise StageCMcpError(f"launcher_qa MCP {method} timed out after {timeout_seconds:.0f}s")

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _drain_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                self._responses.put(decoded)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            safe = " ".join(line.strip().split())
            if safe:
                self._stderr_lines.append(safe[:240])
                del self._stderr_lines[:-6]


class StageCLauncherMcpVisualCaptureProvider:
    def __init__(self, config: StageCMcpServerConfig):
        self.config = config

    def capture_screenshot(self, request: ScreenshotRequest) -> CapturedArtifact:
        metadata = request.metadata if isinstance(request.metadata, dict) else {}
        pins = metadata.get("required_launch_pins") if isinstance(metadata.get("required_launch_pins"), dict) else {}
        hermes_profile = str(pins.get("hermes_profile") or metadata.get("hermes_profile") or active_profile_name()).strip()
        scenario_label = _scenario_label(request.scenario)
        provider_metadata: dict[str, Any] = {}
        if _marionette_preflight_enabled_for_config(metadata, self.config):
            preflight = _ensure_launcher_marionette_debug_target(request.task_id, metadata)
            if preflight:
                provider_metadata["stagec_marionette_preflight"] = preflight
        args: dict[str, Any] = {
            "tab": _target_tab(metadata),
            "profile": "stagec-smoke",
            "screenshot": True,
            "scenario_label": scenario_label,
            "reap_stale": bool(metadata.get("reap_stale", True)),
            "force_relaunch": bool(metadata.get("force_relaunch", True)),
            "browser_login": _metadata_bool(metadata, "browser_login", default=True),
            "screenshot_stabilize_ms": int(metadata.get("screenshot_stabilize_ms") or 750),
            "screenshot_max_retries": int(metadata.get("screenshot_max_retries") or 8),
            "screenshot_retry_delay_ms": int(metadata.get("screenshot_retry_delay_ms") or 750),
            "hermes_profile": hermes_profile,
            "harness_runtime_root": str(paths.store_root()),
        }
        if self.config.profile_home is not None:
            args["hermes_home"] = str(self.config.profile_home)
        launch_pins = _launcher_qa_launch_pins(metadata, self.config)

        try:
            envelope = self._open_app_tab(args, launch_pins=launch_pins)
        except StageCMcpError as exc:
            if not provider_metadata and _is_wrong_marionette_target_error(exc) and _auto_rebuild_enabled(metadata):
                rebuild = _run_launcher_marionette_rebuild(request.task_id, reason="wrong_debug_target_retry")
                provider_metadata["stagec_marionette_preflight"] = rebuild
                if rebuild.get("status") != "applied":
                    raise StageCMcpError(f"{exc}; marionette rebuild failed: {_safe_summary(rebuild.get('summary'))}") from exc
                envelope = self._open_app_tab(args, launch_pins=launch_pins)
            else:
                raise
        if envelope.get("ok") is not True:
            failure = str(envelope.get("failure_class") or "open_app_tab_failed")
            message = str(envelope.get("message_safe") or "open_app_tab failed")
            raise StageCMcpError(f"{failure}: {message[:240]}")
        screenshot = envelope.get("screenshot") if isinstance(envelope.get("screenshot"), dict) else {}
        image_path = str(screenshot.get("path") or "").strip()
        if not image_path:
            raise StageCMcpError("launcher_qa MCP returned no screenshot path")
        redaction = envelope.get("redaction") if isinstance(envelope.get("redaction"), dict) else {}
        return CapturedArtifact(
            path=image_path,
            capture_provider="launcher_qa",
            scenario=request.scenario,
            redaction_status="safe" if redaction.get("safe") is True else "needs_scan",
            width=_optional_int(screenshot.get("width")),
            height=_optional_int(screenshot.get("height")),
            provider_metadata=provider_metadata,
        )

    def capture_video(self, request: VideoRequest) -> CapturedArtifact:
        raise StageCMcpError("launcher_qa MCP video capture is not implemented; request screenshot proof")

    def _open_app_tab(self, args: dict[str, Any], *, launch_pins: dict[str, str]) -> dict[str, Any]:
        config = _config_with_launcher_qa_launch_pins(self.config, launch_pins)
        with StageCMcpJsonRpcClient(config) as client:
            client.initialize()
            return client.call_tool("mcp_launcher_qa_open_app_tab", args)


def default_launcher_qa_visual_provider() -> StageCLauncherMcpVisualCaptureProvider | None:
    config = load_launcher_qa_mcp_config(persona_target="qa")
    if config is None:
        return None
    return StageCLauncherMcpVisualCaptureProvider(config)


def load_launcher_qa_mcp_config(*, persona_target: str = "qa", profile_name: str | None = None) -> StageCMcpServerConfig | None:
    profile_home = _profile_home_for(persona_target=persona_target, profile_name=profile_name)
    config_path = (profile_home / "config.yaml") if profile_home is not None else get_config_path()
    if not config_path.exists():
        return None
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    server = _server_config_dict(raw, "launcher_qa")
    if not server:
        return None
    command = str(server.get("command") or "").strip()
    if not command:
        return None
    args = [str(item) for item in server.get("args") or []]
    env = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
    return StageCMcpServerConfig(
        name="launcher_qa",
        command=command,
        args=args,
        env=env,
        timeout_seconds=_positive_float(server.get("timeout"), 260.0),
        connect_timeout_seconds=_positive_float(server.get("connect_timeout"), 60.0),
        profile_home=profile_home,
    )


def smoke_launcher_qa_mcp(config: StageCMcpServerConfig) -> StageCMcpSmokeResult:
    command_issue = _command_issue(config.command)
    if command_issue:
        return StageCMcpSmokeResult(False, "command_missing", command_issue)
    try:
        with StageCMcpJsonRpcClient(config) as client:
            client.initialize()
            tools = client.tools_list().get("result", {}).get("tools", [])
    except StageCMcpError as exc:
        return StageCMcpSmokeResult(False, "not_ready", str(exc)[:300])
    names = {str(tool.get("name")) for tool in tools if isinstance(tool, dict)}
    if "mcp_launcher_qa_open_app_tab" not in names:
        return StageCMcpSmokeResult(False, "tool_missing", "launcher_qa MCP did not advertise mcp_launcher_qa_open_app_tab")
    return StageCMcpSmokeResult(True, "ready", "launcher_qa MCP initialize/tools-list succeeded")


def _ensure_launcher_marionette_debug_target(task_id: str, metadata: dict[str, Any]) -> dict[str, Any] | None:
    if not _auto_rebuild_enabled(metadata):
        return None
    launcher_repo = _launcher_repo_from_metadata(metadata)
    if launcher_repo is None:
        return None
    status = _marionette_build_status(launcher_repo)
    if status["ok"]:
        return None
    rebuild = _run_launcher_marionette_rebuild(task_id, reason=str(status["code"]), launcher_repo=launcher_repo)
    if rebuild.get("status") != "applied":
        raise StageCMcpError(f"launcher_qa marionette debug target preflight failed: {_safe_summary(rebuild.get('summary'))}")
    post = _marionette_build_status(launcher_repo)
    if not post["ok"]:
        raise StageCMcpError(
            "launcher_qa marionette rebuild completed but required markers are still missing: "
            f"{_safe_summary(post.get('summary'))}"
        )
    return rebuild


def _auto_rebuild_enabled(metadata: dict[str, Any]) -> bool:
    raw = metadata.get("auto_rebuild_marionette")
    if raw is None:
        raw = os.getenv("HERMES_STAGEC_AUTOREBUILD_MARIONETTE", "1")
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _marionette_preflight_enabled_for_config(metadata: dict[str, Any], config: StageCMcpServerConfig) -> bool:
    if not _auto_rebuild_enabled(metadata):
        return False
    if any(str(metadata.get(key) or "").strip() for key in ("launcher_repo", "launcher_repo_root")):
        return True
    if any(os.getenv(key, "").strip() for key in ("HERMES_STAGEC_LAUNCHER_REPO", "HERMES_LAUNCHER_REPO", "ETERNIA_LAUNCHER_ROOT")):
        return True
    if str(config.command or "").strip() == "fake":
        return True
    command_text = " ".join([str(config.command or ""), *[str(item) for item in config.args or []]]).lower().replace("\\", "/")
    return "stagec_qa_mcp_server" in command_text or "invoke-launcherqamcptool" in command_text


def _launcher_qa_launch_pins(metadata: dict[str, Any], config: StageCMcpServerConfig) -> dict[str, str]:
    repo_root = _first_nonempty(
        metadata.get("repoRoot"),
        metadata.get("repo_root"),
        metadata.get("launcher_repo"),
        metadata.get("launcher_repo_root"),
        config.env.get("STAGEC_QA_REPO_ROOT"),
        os.getenv("STAGEC_QA_REPO_ROOT"),
        os.getenv("HERMES_STAGEC_LAUNCHER_REPO"),
        os.getenv("HERMES_LAUNCHER_REPO"),
        os.getenv("ETERNIA_LAUNCHER_ROOT"),
    )
    launch_helper = _first_nonempty(
        metadata.get("launchHelperPath"),
        metadata.get("launch_helper_path"),
        config.env.get("STAGEC_LAUNCH_HELPER"),
        os.getenv("STAGEC_LAUNCH_HELPER"),
    )
    screenshot_helper = _first_nonempty(
        metadata.get("screenshotHelperPath"),
        metadata.get("screenshot_helper_path"),
        config.env.get("STAGEC_SCREENSHOT_HELPER"),
        os.getenv("STAGEC_SCREENSHOT_HELPER"),
    )
    if repo_root:
        launch_helper = launch_helper or _default_launch_helper(repo_root)
        screenshot_helper = screenshot_helper or _default_screenshot_helper(repo_root)
    pins: dict[str, str] = {}
    if repo_root:
        pins["repoRoot"] = repo_root
    if launch_helper:
        pins["launchHelperPath"] = launch_helper
    if screenshot_helper:
        pins["screenshotHelperPath"] = screenshot_helper
    return pins


def _config_with_launcher_qa_launch_pins(config: StageCMcpServerConfig, launch_pins: dict[str, str]) -> StageCMcpServerConfig:
    env = dict(config.env)
    for arg_key, env_key in (
        ("repoRoot", "STAGEC_QA_REPO_ROOT"),
        ("launchHelperPath", "STAGEC_LAUNCH_HELPER"),
        ("screenshotHelperPath", "STAGEC_SCREENSHOT_HELPER"),
    ):
        value = str(launch_pins.get(arg_key) or "").strip()
        if value and not env.get(env_key):
            env[env_key] = value
    if env == config.env:
        return config
    return StageCMcpServerConfig(
        name=config.name,
        command=config.command,
        args=list(config.args),
        env=env,
        timeout_seconds=config.timeout_seconds,
        connect_timeout_seconds=config.connect_timeout_seconds,
        profile_home=config.profile_home,
    )


def _default_launch_helper(repo_root: str) -> str | None:
    candidate = Path(repo_root) / "docs" / "stages" / "qa-reboot" / "scripts" / "Start-StageCDirectExe.ps1"
    return str(candidate) if candidate.exists() else None


def _default_screenshot_helper(repo_root: str) -> str | None:
    candidate = Path(repo_root) / "docs" / "stages" / "qa-reboot" / "scripts" / "Capture-StageCWindowScreenshot.ps1"
    return str(candidate) if candidate.exists() else None


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _metadata_bool(metadata: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in metadata:
        return default
    raw = metadata.get(key)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    text = str(raw or "").strip().lower()
    if text in {"0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def _launcher_repo_from_metadata(metadata: dict[str, Any]) -> Path | None:
    for key in ("launcher_repo", "launcher_repo_root"):
        raw = str(metadata.get(key) or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if path.is_dir():
                return path
    for env_key in ("HERMES_STAGEC_LAUNCHER_REPO", "HERMES_LAUNCHER_REPO", "ETERNIA_LAUNCHER_ROOT"):
        raw = os.getenv(env_key, "").strip()
        if raw:
            path = Path(raw).expanduser()
            if path.is_dir():
                return path
    try:
        from .repo_context import resolve_affected_repo_workdir

        return resolve_affected_repo_workdir("EterniaLauncher")
    except Exception:
        return None


def _marionette_build_status(launcher_repo: Path) -> dict[str, Any]:
    kernel = launcher_repo / "build" / "windows" / "x64" / "runner" / "Debug" / "data" / "flutter_assets" / "kernel_blob.bin"
    if not kernel.exists():
        return {"ok": False, "code": "missing_kernel_blob", "summary": "Launcher debug kernel blob is missing"}
    try:
        data = kernel.read_bytes()
    except OSError:
        return {"ok": False, "code": "kernel_unreadable", "summary": "Launcher debug kernel blob is unreadable"}
    missing = [marker.decode("ascii", errors="ignore") for marker in _MARIONETTE_KERNEL_MARKERS if marker not in data]
    if missing:
        return {"ok": False, "code": "missing_marionette_markers", "summary": f"Launcher debug kernel is missing Marionette markers: {missing[:5]}"}
    return {"ok": True, "code": "ready", "summary": "Launcher debug kernel contains Marionette markers"}


def _run_launcher_marionette_rebuild(task_id: str, *, reason: str, launcher_repo: Path | None = None) -> dict[str, Any]:
    launcher_repo = launcher_repo or _launcher_repo_from_metadata({})
    if launcher_repo is None:
        return {"status": "failed", "reason": _safe_token(reason), "summary": "EterniaLauncher repo could not be resolved"}
    flutter = _flutter_command()
    if flutter is None:
        return {"status": "failed", "reason": _safe_token(reason), "summary": "Flutter CLI is not available on PATH"}
    timeout_seconds = _positive_int(os.getenv("HERMES_STAGEC_MARIONETTE_REBUILD_TIMEOUT_SECONDS"), 600)
    command = [flutter, "build", "windows", "--debug", "--target", "lib/main_marionette.dart"]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(launcher_repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout_rel = _write_rebuild_artifact(task_id, "stdout", completed.stdout)
        stderr_rel = _write_rebuild_artifact(task_id, "stderr", completed.stderr)
        ok = completed.returncode == 0
        return {
            "status": "applied" if ok else "failed",
            "reason": _safe_token(reason),
            "command_label": "flutter build windows --debug --target lib/main_marionette.dart",
            "exit_code": completed.returncode,
            "duration_ms": duration_ms,
            "stdout_path": stdout_rel,
            "stderr_path": stderr_rel,
            "summary": "marionette debug target rebuilt" if ok else _safe_summary(completed.stderr or completed.stdout),
        }
    except subprocess.TimeoutExpired as exc:
        stdout_rel = _write_rebuild_artifact(task_id, "stdout", exc.stdout or "")
        stderr_rel = _write_rebuild_artifact(task_id, "stderr", exc.stderr or "")
        return {
            "status": "failed",
            "reason": _safe_token(reason),
            "command_label": "flutter build windows --debug --target lib/main_marionette.dart",
            "exit_code": "timeout",
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout_path": stdout_rel,
            "stderr_path": stderr_rel,
            "summary": f"marionette rebuild timed out after {timeout_seconds}s",
        }
    except OSError as exc:
        return {
            "status": "failed",
            "reason": _safe_token(reason),
            "command_label": "flutter build windows --debug --target lib/main_marionette.dart",
            "summary": f"marionette rebuild failed to start: {type(exc).__name__}",
        }


def _flutter_command() -> str | None:
    configured = os.getenv("HERMES_FLUTTER_COMMAND", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.exists():
            return str(path)
        resolved = shutil.which(configured)
        return resolved or configured
    return shutil.which("flutter")


def _write_rebuild_artifact(task_id: str, stream_name: str, text: str | bytes | None) -> str:
    safe_task = _safe_token(task_id)
    safe_stream = _safe_token(stream_name)
    stamp = now().strftime("%Y%m%d%H%M%S%f")
    rel = Path("proofs") / safe_task / "artifacts" / f"stagec_marionette_rebuild_{stamp}.{safe_stream}.log"
    path = paths.store_root() / rel
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        path.write_text(str(text or ""), encoding="utf-8")
    except OSError:
        return ""
    return rel.as_posix()


def _is_wrong_marionette_target_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "launch_wrong_debug_target_missing_marionette" in text or "not lib/main_marionette.dart" in text


def _profile_home_for(*, persona_target: str, profile_name: str | None) -> Path | None:
    if profile_name:
        return _profile_dir_if_exists(profile_name)
    try:
        from .config import ensure_persisted_personas

        for persona in ensure_persisted_personas():
            if persona.id == persona_target or persona.role == persona_target:
                if persona.hermes_profile:
                    return _profile_dir_if_exists(persona.hermes_profile)
    except Exception:
        pass
    return None


def _profile_dir_if_exists(name: str) -> Path | None:
    fallback = get_default_hermes_root() / "profiles" / name
    if fallback.exists():
        return fallback
    try:
        from hermes_cli.profiles import get_profile_dir, normalize_profile_name, profile_exists

        normalized = normalize_profile_name(name)
        if profile_exists(normalized):
            return get_profile_dir(normalized)
    except Exception:
        pass
    return None


def _server_config_dict(raw: dict[str, Any], name: str) -> dict[str, Any] | None:
    for key_path in (("mcp_servers",), ("mcp", "servers"), ("mcpServers",)):
        node: Any = raw
        for key in key_path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, dict) and isinstance(node.get(name), dict):
            return node[name]
    return None


def _decode_tool_envelope(result: dict[str, Any]) -> dict[str, Any]:
    content = result.get("content")
    if not isinstance(content, list):
        raise StageCMcpError("launcher_qa MCP result content is missing")
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text":
            continue
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StageCMcpError("launcher_qa MCP text content was not JSON") from exc
        if isinstance(decoded, dict):
            return decoded
    raise StageCMcpError("launcher_qa MCP result had no JSON text envelope")


def _command_issue(command: str) -> str | None:
    path = Path(command)
    if path.is_absolute() or any(sep in command for sep in ("\\", "/")):
        return None if path.exists() else "launcher_qa MCP command path does not exist"
    return None if shutil.which(command) else "launcher_qa MCP command is not on PATH"


def _dead_process_summary(returncode: int | None, stderr_lines: list[str]) -> str:
    detail = "; ".join(stderr_lines[-3:])
    return f"launcher_qa MCP exited before response (exit={returncode})" + (f": {detail}" if detail else "")


def _target_tab(metadata: dict[str, Any]) -> str:
    target = str(metadata.get("target") or "").lower()
    if "mission" in target:
        return "missionControl"
    tab = str(metadata.get("tab") or "").strip()
    return tab if tab else "missionControl"


def _scenario_label(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(value or "mission_control"))
    safe = safe.strip("._-") or "mission_control"
    return safe[:64]


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _optional_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_token(value: Any) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(value or "").strip())
    return text[:64] or "item"


def _safe_summary(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"([A-Za-z]:\\[^ \n\r\t]+|[A-Za-z]:/[^ \n\r\t]+)", "<path>", text)
    text = re.sub(r"(?i)(api[_-]?key|authorization|cookie|bearer|password|credential|secret|token)[^ \n\r\t]*", "<redacted>", text)
    return text[:240] or "no rebuild output"
