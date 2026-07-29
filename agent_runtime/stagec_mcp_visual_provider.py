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

from . import machine_roots, paths
from .machine_roots import MachineRootError, expand_config_paths
from .profile_context import active_profile_name
from .proof_capture import CapturedArtifact, ScreenshotRequest, VideoRequest


class StageCMcpError(RuntimeError):
    def __init__(self, message: str, *, provider_metadata: dict[str, Any] | None = None):
        super().__init__(message)
        self.provider_metadata = dict(provider_metadata or {})


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


@dataclass(frozen=True, slots=True)
class StageCMcpConfigResolution:
    """Why the launcher_qa binding is (un)available on THIS machine.

    ``load_launcher_qa_mcp_config`` collapses this to ``config or None`` for
    back-compat; anything that reports to a human (preflight, readiness) should
    read the typed ``code``/``summary``/``fix_hint`` so an unbound machine root
    is never displayed as a generic "not configured".
    """

    config: StageCMcpServerConfig | None
    code: str
    summary: str
    fix_hint: str = ""
    issues: tuple[dict[str, Any], ...] = ()

    @property
    def ok(self) -> bool:
        return self.config is not None


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

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        allow_error_envelope: bool = False,
    ) -> dict[str, Any]:
        response = self.request(
            "tools/call",
            params={"name": name, "arguments": arguments},
            timeout_seconds=self.config.timeout_seconds,
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise StageCMcpError("launcher_qa MCP tool returned no result object")
        envelope = _decode_tool_envelope(result)
        if result.get("isError") is True and not allow_error_envelope:
            failure = str(envelope.get("failure_class") or "mcp_tool_error")
            message = str(envelope.get("message_safe") or envelope.get("summary") or "tool error")
            raise StageCMcpError(f"{failure}: {message[:240]}")
        return envelope

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
            "screenshot": _target_tab(metadata) != "missionControl",
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
            envelope = self._capture_app_tab(args, metadata=metadata, launch_pins=launch_pins)
        except StageCMcpError as exc:
            if not provider_metadata and _is_wrong_marionette_target_error(exc) and _auto_rebuild_enabled(metadata):
                rebuild = _run_launcher_marionette_rebuild(request.task_id, reason="wrong_debug_target_retry")
                provider_metadata["stagec_marionette_preflight"] = rebuild
                if rebuild.get("status") != "applied":
                    raise StageCMcpError(f"{exc}; marionette rebuild failed: {_safe_summary(rebuild.get('summary'))}") from exc
                envelope = self._capture_app_tab(args, metadata=metadata, launch_pins=launch_pins)
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
        semantic = envelope.get("semantic")
        if isinstance(semantic, dict):
            provider_metadata["stagec_semantic_envelope"] = semantic
        screenshot_evidence = _stagec_screenshot_evidence(screenshot)
        if screenshot_evidence:
            provider_metadata["stagec_screenshot_envelope"] = screenshot_evidence
        external_failure = envelope.get("external_capture_failure")
        if isinstance(external_failure, dict):
            provider_metadata["stagec_external_capture_failure"] = external_failure
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

    def _capture_app_tab(
        self,
        args: dict[str, Any],
        *,
        metadata: dict[str, Any],
        launch_pins: dict[str, str],
    ) -> dict[str, Any]:
        config = _config_with_launcher_qa_launch_pins(self.config, launch_pins)
        with StageCMcpJsonRpcClient(config) as client:
            client.initialize()
            opened = client.call_tool("mcp_launcher_qa_open_app_tab", args)
            _require_stagec_envelope_ok(opened, fallback="open_app_tab_failed")
            if args.get("tab") != "missionControl":
                return opened

            timeout_ms = min(_positive_int(metadata.get("semantic_settle_timeout_ms"), 60_000), 60_000)
            poll_ms = min(_positive_int(metadata.get("semantic_settle_poll_ms"), 500), 5_000)
            wait = client.call_tool(
                "mcp_launcher_qa_wait_for_state",
                {
                    "assertions": [
                        {"path": "navigation.selected_tab", "equals": "missionControl"},
                        {"path": "blocking_modal.present", "equals": False},
                        {"path": "mission_control.graph.mounted", "equals": True},
                    ],
                    "timeout_ms": timeout_ms,
                    "poll_ms": max(poll_ms, 25),
                    "evidence_level": "compact",
                },
            )
            if wait.get("ok") is not True:
                failure = str(wait.get("failure_class") or "semantic_settle_failed")
                message = str(
                    wait.get("message_safe")
                    or f"Mission Control semantic target did not settle within {timeout_ms}ms"
                )
                raise StageCMcpError(f"{failure}: {message[:240]}")

            stabilize_ms = min(max(int(args.get("screenshot_stabilize_ms") or 0), 0), 15_000)
            if stabilize_ms:
                time.sleep(stabilize_ms / 1000.0)
            navigation = client.call_tool("mcp_launcher_qa_get_navigation_state", {})
            widget = client.call_tool(
                "mcp_launcher_qa_get_widget_state",
                {"widget": "mission_control.graph", "evidence_level": "compact"},
            )
            _require_stagec_envelope_ok(navigation, fallback="navigation_state_failed")
            _require_stagec_envelope_ok(widget, fallback="mission_control_graph_failed")
            navigation_data = _stagec_envelope_data(navigation)
            widget_data = _stagec_envelope_data(widget)
            blocking_modal = navigation_data.get("blocking_modal")
            modal_present = blocking_modal.get("present") if isinstance(blocking_modal, dict) else None
            if (
                navigation_data.get("selected_tab") != "missionControl"
                or modal_present is not False
                or widget_data.get("mounted") is not True
            ):
                raise StageCMcpError(
                    "semantic_state_changed_before_capture: Mission Control was not settled at the final semantic probe"
                )

            semantic = _settled_mission_control_envelope(wait, navigation_data, widget_data)

            screenshot = client.call_tool(
                "mcp_launcher_qa_screenshot_window",
                {
                    "label": _scenario_label(str(args.get("scenario_label") or "mission_control")),
                    "max_retries": int(args.get("screenshot_max_retries") or 8),
                    "retry_delay_ms": int(args.get("screenshot_retry_delay_ms") or 750),
                },
                allow_error_envelope=True,
            )
            if screenshot.get("ok") is not True:
                external_failure = _stagec_capture_failure_evidence(screenshot, fallback="screenshot_window_failed")
                if _is_semantic_pixel_capture_failure(screenshot):
                    fallback_args = {
                        **args,
                        "screenshot": True,
                        "force_relaunch": False,
                        "reap_stale": False,
                        "relaunch_on_dead_process": False,
                    }
                    fallback_envelope = client.call_tool(
                        "mcp_launcher_qa_open_app_tab",
                        fallback_args,
                        allow_error_envelope=True,
                    )
                    if fallback_envelope.get("ok") is not True:
                        fallback_failure = _stagec_capture_failure_evidence(
                            fallback_envelope,
                            fallback="marionette_internal_fallback_failed",
                        )
                        failure = str(fallback_envelope.get("failure_class") or "marionette_internal_fallback_failed")
                        message = str(
                            fallback_envelope.get("message_safe")
                            or "Marionette internal screenshot fallback did not produce proof"
                        )
                        raise StageCMcpError(
                            f"{failure}: {message[:240]}",
                            provider_metadata={
                                "stagec_semantic_envelope": semantic,
                                "stagec_external_capture_failure": external_failure,
                                "stagec_internal_fallback_failure": fallback_failure,
                            },
                        )
                    fallback_screenshot = (
                        fallback_envelope.get("screenshot")
                        if isinstance(fallback_envelope.get("screenshot"), dict)
                        else {}
                    )
                    image_path = str(fallback_screenshot.get("path") or "").strip()
                    if not image_path:
                        raise StageCMcpError(
                            "marionette_internal_fallback_missing_artifact: fallback returned no screenshot path",
                            provider_metadata={
                                "stagec_semantic_envelope": semantic,
                                "stagec_external_capture_failure": external_failure,
                                "stagec_internal_fallback_failure": {
                                    "failure_class": "marionette_internal_fallback_missing_artifact"
                                },
                            },
                        )
                    return {
                        "ok": True,
                        "screenshot": fallback_screenshot,
                        "redaction": fallback_envelope.get("redaction"),
                        "semantic": semantic,
                        "external_capture_failure": external_failure,
                    }
                failure = str(screenshot.get("failure_class") or "screenshot_window_failed")
                message = str(screenshot.get("message_safe") or "screenshot_window failed")
                raise StageCMcpError(
                    f"{failure}: {message[:240]}",
                    provider_metadata={
                        "stagec_semantic_envelope": semantic,
                        "stagec_external_capture_failure": external_failure,
                    },
                )
            bounds = screenshot.get("bounds") if isinstance(screenshot.get("bounds"), dict) else {}
            window_identity = _stagec_window_identity_evidence(screenshot.get("window_identity"))
            return {
                "ok": True,
                "screenshot": {
                    "path": screenshot.get("image_path"),
                    "width": bounds.get("width"),
                    "height": bounds.get("height"),
                    "method": screenshot.get("capture_method_used") or screenshot.get("capture_method"),
                    "byte_count": screenshot.get("byte_count"),
                    "window_identity": window_identity or None,
                },
                "redaction": screenshot.get("redaction"),
                "semantic": semantic,
            }


def default_launcher_qa_visual_provider() -> StageCLauncherMcpVisualCaptureProvider | None:
    config = load_launcher_qa_mcp_config(persona_target="qa")
    if config is None:
        return None
    return StageCLauncherMcpVisualCaptureProvider(config)


def load_launcher_qa_mcp_config(*, persona_target: str = "qa", profile_name: str | None = None) -> StageCMcpServerConfig | None:
    """Back-compat accessor — the config, or ``None`` when it is unusable here.

    Callers that must explain the ``None`` (preflight, readiness) should use
    :func:`resolve_launcher_qa_mcp_config` instead of guessing "not configured".
    """

    return resolve_launcher_qa_mcp_config(persona_target=persona_target, profile_name=profile_name).config


def resolve_launcher_qa_mcp_config(
    *, persona_target: str = "qa", profile_name: str | None = None
) -> StageCMcpConfigResolution:
    profile_home = _profile_home_for(persona_target=persona_target, profile_name=profile_name)
    config_path = (profile_home / "config.yaml") if profile_home is not None else get_config_path()
    if not config_path.exists():
        return StageCMcpConfigResolution(
            None,
            "missing",
            f"No Hermes profile config at {config_path}",
            "Configure mcp_servers.launcher_qa in the launcher-qa Hermes profile.",
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return StageCMcpConfigResolution(
            None,
            "config_unreadable",
            f"Hermes profile config did not parse ({type(exc).__name__}): {config_path}",
            "Repair the profile config.yaml, then re-run preflight.",
        )
    server = _server_config_dict(raw, "launcher_qa")
    if not server:
        return StageCMcpConfigResolution(
            None,
            "missing",
            "launcher_qa MCP server is not configured for the QA persona",
            "Configure mcp_servers.launcher_qa in the launcher-qa Hermes profile.",
        )
    if not machine_roots.platform_supported(server.get("platforms")):
        declared = machine_roots.normalize_platforms(server.get("platforms"))
        here = machine_roots.current_platform_key()
        return StageCMcpConfigResolution(
            None,
            "platform_unsupported",
            f"launcher_qa MCP is declared for {declared}; this machine is {here}",
            f"Stage C visual proof needs a {declared} host; capture it there or provide a {here} binding.",
        )
    try:
        server = expand_config_paths(
            {key: value for key, value in server.items() if key != "platforms"},
            field="mcp_servers.launcher_qa",
        )
    except MachineRootError as exc:
        return StageCMcpConfigResolution(
            None,
            exc.code,
            exc.summary,
            exc.fix_hint,
            tuple(exc.rows()),
        )
    command = str(server.get("command") or "").strip()
    if not command:
        return StageCMcpConfigResolution(
            None,
            "missing",
            "launcher_qa MCP server entry has no command",
            "Set mcp_servers.launcher_qa.command in the launcher-qa Hermes profile.",
        )
    args = [str(item) for item in server.get("args") or []]
    env = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
    return StageCMcpConfigResolution(
        StageCMcpServerConfig(
            name="launcher_qa",
            command=command,
            args=args,
            env=env,
            timeout_seconds=_positive_float(server.get("timeout"), 260.0),
            connect_timeout_seconds=_positive_float(server.get("connect_timeout"), 60.0),
            profile_home=profile_home,
        ),
        "ready",
        "launcher_qa MCP binding resolved",
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
    # Env rung: gate on the SAME validated content the consumer requires, not
    # on the bare presence of a variable. ``_launcher_repo_from_metadata`` only
    # accepts one of these keys when it names a real directory; enabling here on
    # presence alone let a stale value inherited from process ancestry (a warm
    # ``hermes harness serve`` keeps whatever env it booted with) switch on a
    # preflight that then rebuilt a DIFFERENT repo than the variable named — or
    # none at all. ``flutter build`` is not a cheap surprise. One predicate, one
    # answer.
    if _env_launcher_repo() is not None:
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


#: Environment keys that may name the launcher repo, in precedence order. ONE
#: spelling, shared by the enable predicate and the resolver, so the two can
#: never answer "is a launcher repo named here?" differently.
LAUNCHER_REPO_ENV_KEYS: tuple[str, ...] = (
    "HERMES_STAGEC_LAUNCHER_REPO",
    "HERMES_LAUNCHER_REPO",
    "ETERNIA_LAUNCHER_ROOT",
)


def _env_launcher_repo() -> Path | None:
    """The launcher repo the ENVIRONMENT names, or ``None``.

    Validated content, never bare presence: a variable set to a path that is
    not a directory on this machine names no repo, and must not be able to
    switch on behavior that then resolves somewhere else. Never raises — a
    disconnected network root answers "no" rather than failing the caller.
    """

    for env_key in LAUNCHER_REPO_ENV_KEYS:
        raw = os.getenv(env_key, "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).expanduser()
            if path.is_dir():
                return path
        except OSError:  # pragma: no cover - defensive
            continue
    return None


def _launcher_repo_from_metadata(metadata: dict[str, Any]) -> Path | None:
    for key in ("launcher_repo", "launcher_repo_root"):
        raw = str(metadata.get(key) or "").strip()
        if raw:
            path = Path(raw).expanduser()
            if path.is_dir():
                return path
    env_repo = _env_launcher_repo()
    if env_repo is not None:
        return env_repo
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
    """Persist one marionette-rebuild stream under the Stage C artifact dir.

    This used to write into the proof store — the only such write in this file — and
    it never produced a ``Proof`` row, just a build log referenced by ``stdout_path``
    / ``stderr_path`` in the rebuild result dict. That store is removed with the
    mission lane (S6/S9) and is already quarantined out of the live runtime root, so
    this now targets ``paths.stagec_artifacts_task_dir``.

    ``task_id`` is a *filename grouping token* here and nothing more — the caller
    passes ``request.task_id`` straight through, no Task record is read, and
    ``request.stage_id`` is never consulted on this path.
    """

    safe_stream = _safe_token(stream_name)
    stamp = now().strftime("%Y%m%d%H%M%S%f")
    path = paths.stagec_artifacts_task_dir(task_id) / f"stagec_marionette_rebuild_{stamp}.{safe_stream}.log"
    try:
        rel = path.relative_to(paths.store_root())
    except ValueError:
        rel = path
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


def _require_stagec_envelope_ok(envelope: dict[str, Any], *, fallback: str) -> None:
    if envelope.get("ok") is True:
        return
    failure = str(envelope.get("failure_class") or fallback)
    message = str(envelope.get("message_safe") or fallback.replace("_", " "))
    raise StageCMcpError(f"{failure}: {message[:240]}")


def _is_semantic_pixel_capture_failure(envelope: dict[str, Any]) -> bool:
    return str(envelope.get("failure_class") or "").strip() in {
        "helper_blank_capture",
        "helper_low_information_capture",
    }


def _stagec_capture_failure_evidence(envelope: dict[str, Any], *, fallback: str) -> dict[str, Any]:
    helper = envelope.get("helper_response_safe")
    helper_data = helper if isinstance(helper, dict) else {}
    screenshot = envelope.get("screenshot")
    screenshot_data = screenshot if isinstance(screenshot, dict) else {}
    diagnostics = screenshot_data.get("helper_diagnostics")
    diagnostic_data = diagnostics if isinstance(diagnostics, dict) else {}
    internal = diagnostic_data.get("internal_screenshot_fallback")
    internal_data = internal if isinstance(internal, dict) else {}
    evidence = {
        "failure_class": str(envelope.get("failure_class") or fallback)[:120],
        "helper_failure_class": str(helper_data.get("failure_class") or "")[:120],
        "helper_exit": _optional_int(envelope.get("helper_exit")),
        "original_screenshot_failure_class": str(
            diagnostic_data.get("original_screenshot_failure_class") or ""
        )[:120],
        "internal_fallback_failure_class": str(internal_data.get("failure_class") or "")[:120],
    }
    return {key: value for key, value in evidence.items() if value not in {None, ""}}


def _stagec_screenshot_evidence(screenshot: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "method",
        "byte_count",
        "width",
        "height",
        "fallback_from",
        "fallback_reason",
        "acceptance_mode",
        "acceptance_reason",
        "window_identity",
    )
    return {key: screenshot.get(key) for key in keys if screenshot.get(key) is not None}


def _stagec_window_identity_evidence(value: Any) -> dict[str, Any]:
    identity = value if isinstance(value, dict) else {}
    keys = (
        "hwnd",
        "pid",
        "class",
        "visible",
        "iconic_at_match",
        "cloaked",
        "selection_rule",
        "selection_mode",
        "requested_pid",
        "pid_matches_requested",
        "title_matches_prefix",
        "observed_image_name",
        "expected_image_name",
    )
    return {key: identity.get(key) for key in keys if identity.get(key) is not None}


def _stagec_envelope_data(envelope: dict[str, Any]) -> dict[str, Any]:
    response = envelope.get("response_safe")
    source = response if isinstance(response, dict) else envelope
    data = source.get("data")
    return data if isinstance(data, dict) else source


def _settled_mission_control_envelope(
    wait: dict[str, Any],
    navigation: dict[str, Any],
    widget: dict[str, Any],
) -> dict[str, Any]:
    blocking_modal = navigation.get("blocking_modal")
    modal = blocking_modal if isinstance(blocking_modal, dict) else {}
    graph_keys = (
        "widget",
        "mounted",
        "goal_id",
        "blueprint_id",
        "blueprint_version",
        "active_stage_id",
    )
    graph = {key: widget.get(key) for key in graph_keys if key in widget}
    for list_key, count_key in (("stages", "stage_count"), ("edges", "edge_count"), ("actors", "actor_count")):
        values = widget.get(list_key)
        if isinstance(values, list):
            graph[count_key] = len(values)
    actors = widget.get("actors")
    if isinstance(actors, list):
        actor_keys = (
            "actor_id",
            "persona_id",
            "persona_instance_id",
            "display_name",
            "role",
            "presence",
            "stage_id",
            "goal_id",
            "state_label",
        )
        graph["actors"] = [
            {key: actor.get(key) for key in actor_keys if key in actor}
            for actor in actors[:16]
            if isinstance(actor, dict)
        ]
    return {
        "schema": "stagec_harness_settled_target.safe.v1",
        "ok": True,
        "target": "missionControl",
        "acceptance": "mission_control.graph.mounted",
        "wait": {
            key: wait.get(key)
            for key in ("schema", "matched", "elapsed_ms", "poll_count", "selected_tab")
            if wait.get(key) is not None
        },
        "navigation": {
            "selected_tab": navigation.get("selected_tab"),
            "route_name": navigation.get("route_name"),
            "blocking_modal": {"present": modal.get("present")},
        },
        "widget": graph,
        "redaction": {"safe": True},
    }


def _safe_token(value: Any) -> str:
    text = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(value or "").strip())
    return text[:64] or "item"


def _safe_summary(value: Any) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(r"([A-Za-z]:\\[^ \n\r\t]+|[A-Za-z]:/[^ \n\r\t]+)", "<path>", text)
    text = re.sub(r"(?i)(api[_-]?key|authorization|cookie|bearer|password|credential|secret|token)[^ \n\r\t]*", "<redacted>", text)
    return text[:240] or "no rebuild output"
