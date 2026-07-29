from hermes_time import now
from pathlib import Path

from agent_runtime.models import Task
from agent_runtime.proof_capture import CapturedArtifact, CapturedTestRun, ScreenshotRequest
from agent_runtime.states import TaskState


def test_captured_test_run_metadata_has_exit_code_and_paths():
    m=CapturedTestRun(command="pytest", cwd=".", exit_code=0, duration_ms=1, stdout_path="out", stderr_path="err").metadata()
    assert m["exit_code"] == 0 and m["stdout_path"] == "out"


def test_captured_artifact_metadata_omits_none_dimensions():
    m=CapturedArtifact(path="p.png", capture_provider="browser", scenario="s").metadata()
    assert "width" not in m and m["redaction_status"] == "needs_scan"


def _task():
    return Task(id="task_visual", title="Visual", description="Visual", state=TaskState.RUNNING, created_at=now(), updated_at=now(), requested_by="test")


def _request():
    return {
        "target": "mission_control",
        "proof_requirement": "state parity",
        "mcp_server": "launcher_qa",
        "required_launch_pins": {"hermes_profile": "alice", "runtime_root_id": "runtime"},
    }


def _stagec_mission_control_result(name, artifact, *, width=2560, height=1400):
    if name == "mcp_launcher_qa_open_app_tab":
        return {"ok": True, "redaction": {"safe": True}}
    if name == "mcp_launcher_qa_wait_for_state":
        return {
            "schema": "stagec_mcp_wait_for_state.safe.v1",
            "ok": True,
            "matched": 3,
            "elapsed_ms": 1250,
            "poll_count": 4,
            "selected_tab": "missionControl",
            "redaction": {"safe": True},
        }
    if name == "mcp_launcher_qa_get_navigation_state":
        return {
            "ok": True,
            "response_safe": {
                "ok": True,
                "data": {
                    "selected_tab": "missionControl",
                    "route_name": "/shell",
                    "blocking_modal": {"present": False},
                },
            },
            "redaction": {"safe": True},
        }
    if name == "mcp_launcher_qa_get_widget_state":
        return {
            "ok": True,
            "response_safe": {
                "ok": True,
                "data": {
                    "widget": "mission_control.graph",
                    "mounted": True,
                    "goal_id": "goal_test",
                    "stages": [{"id": "build"}, {"id": "verify"}],
                    "edges": [{"source": "build", "target": "verify"}],
                    "actors": [
                        {
                            "actor_id": "qa",
                            "persona_id": "qa",
                            "persona_instance_id": "personainst_qa",
                            "role": "qa",
                            "presence": "active",
                        }
                    ],
                },
            },
            "redaction": {"safe": True},
        }
    if name == "mcp_launcher_qa_screenshot_window":
        return {
            "ok": True,
            "image_path": str(artifact),
            "bounds": {"width": width, "height": height},
            "byte_count": 123456,
            "capture_method": "printwindow",
            "capture_method_used": "printwindow",
            "window_identity": {
                "pid": 4242,
                "selection_mode": "pid",
                "requested_pid": 4242,
                "pid_matches_requested": True,
                "observed_image_name": "eternia_launcher.exe",
                "expected_image_name": "eternia_launcher.exe",
            },
            "redaction": {"safe": True},
        }
    raise AssertionError(f"unexpected Stage-C tool: {name}")


def test_stagec_jsonrpc_client_can_preserve_mcp_error_envelope(monkeypatch):
    import json
    import pytest

    from agent_runtime import stagec_mcp_visual_provider as stagec

    response = {
        "result": {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "ok": False,
                            "failure_class": "helper_low_information_capture",
                            "message_safe": "Screenshot capture failed after 8 attempt(s).",
                        }
                    ),
                }
            ],
        }
    }
    client = stagec.StageCMcpJsonRpcClient(
        stagec.StageCMcpServerConfig(name="launcher_qa", command="fake")
    )
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: response)

    envelope = client.call_tool("mcp_launcher_qa_screenshot_window", {}, allow_error_envelope=True)

    assert envelope["failure_class"] == "helper_low_information_capture"
    with pytest.raises(stagec.StageCMcpError, match="helper_low_information_capture"):
        client.call_tool("mcp_launcher_qa_screenshot_window", {})


def test_stagec_provider_rebuilds_stale_marionette_before_capture(tmp_path, monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import stagec_mcp_visual_provider as stagec

    repo = tmp_path / "launcher"
    kernel = repo / "build" / "windows" / "x64" / "runner" / "Debug" / "data" / "flutter_assets" / "kernel_blob.bin"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"default lib/main.dart build")
    artifact = tmp_path / "mission_control.png"
    artifact.write_bytes(b"png")
    rebuilds = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self):
            return {}

        def call_tool(self, name, arguments, *, allow_error_envelope=False):
            assert all(marker in kernel.read_bytes() for marker in stagec._MARIONETTE_KERNEL_MARKERS)
            return _stagec_mission_control_result(name, artifact)

    def fake_rebuild(task_id, *, reason, launcher_repo=None):
        rebuilds.append({"task_id": task_id, "reason": reason, "launcher_repo": launcher_repo})
        kernel.write_bytes(b" ".join(stagec._MARIONETTE_KERNEL_MARKERS))
        return {"status": "applied", "reason": reason, "command_label": "flutter build windows --debug --target lib/main_marionette.dart"}

    monkeypatch.setattr(stagec, "_launcher_repo_from_metadata", lambda metadata: repo)
    monkeypatch.setattr(stagec, "_run_launcher_marionette_rebuild", fake_rebuild)
    monkeypatch.setattr(stagec, "StageCMcpJsonRpcClient", FakeClient)

    provider = stagec.StageCLauncherMcpVisualCaptureProvider(stagec.StageCMcpServerConfig(name="launcher_qa", command="fake"))
    capture = provider.capture_screenshot(
        ScreenshotRequest(
            task_id="task_stagec",
            stage_id="stage_1",
            scenario="mission_control",
            metadata=_request(),
        )
    )

    assert rebuilds and rebuilds[0]["reason"] == "missing_marionette_markers"
    assert Path(capture.path) == artifact
    assert capture.width == 2560
    assert capture.provider_metadata["stagec_marionette_preflight"]["status"] == "applied"


def test_stagec_provider_retries_after_wrong_marionette_target_error(tmp_path, monkeypatch):
    from agent_runtime import stagec_mcp_visual_provider as stagec

    artifact = tmp_path / "mission_control.png"
    artifact.write_bytes(b"png")
    calls = []
    rebuilds = []

    class FakeClient:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self):
            return {}

        def call_tool(self, name, arguments, *, allow_error_envelope=False):
            calls.append(name)
            if name == "mcp_launcher_qa_open_app_tab" and calls.count(name) == 1:
                raise stagec.StageCMcpError(
                    "launch_wrong_debug_target_missing_marionette: Debug EXE built against lib/main.dart, not lib/main_marionette.dart"
                )
            return _stagec_mission_control_result(name, artifact)

    def fake_rebuild(task_id, *, reason, launcher_repo=None):
        rebuilds.append(reason)
        return {"status": "applied", "reason": reason, "command_label": "flutter build windows --debug --target lib/main_marionette.dart"}

    monkeypatch.setattr(stagec, "_launcher_repo_from_metadata", lambda metadata: None)
    monkeypatch.setattr(stagec, "_run_launcher_marionette_rebuild", fake_rebuild)
    monkeypatch.setattr(stagec, "StageCMcpJsonRpcClient", FakeClient)

    provider = stagec.StageCLauncherMcpVisualCaptureProvider(stagec.StageCMcpServerConfig(name="launcher_qa", command="fake"))
    capture = provider.capture_screenshot(
        ScreenshotRequest(
            task_id="task_stagec_retry",
            stage_id="stage_1",
            scenario="mission_control",
            metadata=_request(),
        )
    )

    assert calls == [
        "mcp_launcher_qa_open_app_tab",
        "mcp_launcher_qa_open_app_tab",
        "mcp_launcher_qa_wait_for_state",
        "mcp_launcher_qa_get_navigation_state",
        "mcp_launcher_qa_get_widget_state",
        "mcp_launcher_qa_screenshot_window",
    ]
    assert rebuilds == ["wrong_debug_target_retry"]
    assert capture.path == str(artifact)
    assert capture.provider_metadata["stagec_marionette_preflight"]["reason"] == "wrong_debug_target_retry"


def test_stagec_provider_waits_for_settled_mission_control_before_capture(tmp_path, monkeypatch):
    from agent_runtime import stagec_mcp_visual_provider as stagec

    artifact = tmp_path / "mission_control.png"
    artifact.write_bytes(b"png")
    calls = []
    sleeps = []

    class FakeClient:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self):
            return {}

        def call_tool(self, name, arguments, *, allow_error_envelope=False):
            if name in {
                "mcp_launcher_qa_get_navigation_state",
                "mcp_launcher_qa_get_widget_state",
                "mcp_launcher_qa_screenshot_window",
            }:
                assert sleeps == [0.75]
            calls.append((name, arguments))
            return _stagec_mission_control_result(name, artifact)

    monkeypatch.setattr(stagec, "StageCMcpJsonRpcClient", FakeClient)
    monkeypatch.setattr(stagec.time, "sleep", sleeps.append)
    monkeypatch.setattr(stagec, "_marionette_preflight_enabled_for_config", lambda metadata, config: False)
    provider = stagec.StageCLauncherMcpVisualCaptureProvider(
        stagec.StageCMcpServerConfig(name="launcher_qa", command="fake")
    )

    capture = provider.capture_screenshot(
        ScreenshotRequest(
            task_id="task_stagec_settle",
            stage_id="stage_1",
            scenario="mission_control",
            metadata={**_request(), "semantic_settle_timeout_ms": 45_000},
        )
    )

    assert [name for name, _ in calls] == [
        "mcp_launcher_qa_open_app_tab",
        "mcp_launcher_qa_wait_for_state",
        "mcp_launcher_qa_get_navigation_state",
        "mcp_launcher_qa_get_widget_state",
        "mcp_launcher_qa_screenshot_window",
    ]
    assert calls[0][1]["screenshot"] is False
    assert calls[1][1]["timeout_ms"] == 45_000
    assert calls[1][1]["assertions"][-1] == {
        "path": "mission_control.graph.mounted",
        "equals": True,
    }
    semantic = capture.provider_metadata["stagec_semantic_envelope"]
    assert semantic["ok"] is True
    assert semantic["navigation"]["selected_tab"] == "missionControl"
    assert semantic["widget"] == {
        "widget": "mission_control.graph",
        "mounted": True,
        "goal_id": "goal_test",
        "stage_count": 2,
        "edge_count": 1,
        "actor_count": 1,
        "actors": [
            {
                "actor_id": "qa",
                "persona_id": "qa",
                "persona_instance_id": "personainst_qa",
                "role": "qa",
                "presence": "active",
            }
        ],
    }
    assert capture.provider_metadata["stagec_screenshot_envelope"] == {
        "method": "printwindow",
        "byte_count": 123456,
        "width": 2560,
        "height": 1400,
        "window_identity": {
            "pid": 4242,
            "selection_mode": "pid",
            "requested_pid": 4242,
            "pid_matches_requested": True,
            "observed_image_name": "eternia_launcher.exe",
            "expected_image_name": "eternia_launcher.exe",
        },
    }


def test_stagec_provider_uses_internal_fallback_after_settled_helper_failure(tmp_path, monkeypatch):
    from agent_runtime import stagec_mcp_visual_provider as stagec

    artifact = tmp_path / "mission_control_marionette_internal.png"
    artifact.write_bytes(b"png")
    calls = []

    class FakeClient:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self):
            return {}

        def call_tool(self, name, arguments, *, allow_error_envelope=False):
            calls.append((name, dict(arguments), allow_error_envelope))
            if name == "mcp_launcher_qa_screenshot_window":
                return {
                    "ok": False,
                    "failure_class": "helper_low_information_capture",
                    "message_safe": "Screenshot capture failed after 8 attempt(s).",
                    "helper_exit": 23,
                    "helper_response_safe": {"failure_class": "low_information_capture"},
                }
            if name == "mcp_launcher_qa_open_app_tab" and arguments.get("screenshot") is True:
                return {
                    "ok": True,
                    "screenshot": {
                        "method": "MarionetteInternalFlutter",
                        "path": str(artifact),
                        "byte_count": artifact.stat().st_size,
                        "width": 1920,
                        "height": 1080,
                        "fallback_from": "PrintWindow",
                        "fallback_reason": "helper_low_information_capture",
                        "acceptance_mode": "marionette_internal_fallback",
                    },
                    "redaction": {"safe": True},
                }
            return _stagec_mission_control_result(name, artifact, width=1920, height=1080)

    monkeypatch.setattr(stagec, "StageCMcpJsonRpcClient", FakeClient)
    monkeypatch.setattr(stagec, "_marionette_preflight_enabled_for_config", lambda metadata, config: False)
    provider = stagec.StageCLauncherMcpVisualCaptureProvider(
        stagec.StageCMcpServerConfig(name="launcher_qa", command="fake")
    )

    capture = provider.capture_screenshot(
        ScreenshotRequest(
            task_id="task_stagec_fallback",
            stage_id="stage_1",
            scenario="mission_control",
            metadata=_request(),
        )
    )

    assert [name for name, _, _ in calls] == [
        "mcp_launcher_qa_open_app_tab",
        "mcp_launcher_qa_wait_for_state",
        "mcp_launcher_qa_get_navigation_state",
        "mcp_launcher_qa_get_widget_state",
        "mcp_launcher_qa_screenshot_window",
        "mcp_launcher_qa_open_app_tab",
    ]
    assert calls[-2][2] is True
    assert calls[-1][2] is True
    fallback_args = calls[-1][1]
    assert fallback_args["screenshot"] is True
    assert fallback_args["force_relaunch"] is False
    assert fallback_args["reap_stale"] is False
    assert fallback_args["relaunch_on_dead_process"] is False
    assert capture.path == str(artifact)
    assert capture.provider_metadata["stagec_semantic_envelope"]["widget"]["mounted"] is True
    assert capture.provider_metadata["stagec_external_capture_failure"] == {
        "failure_class": "helper_low_information_capture",
        "helper_failure_class": "low_information_capture",
        "helper_exit": 23,
    }
    assert capture.provider_metadata["stagec_screenshot_envelope"] == {
        "method": "MarionetteInternalFlutter",
        "byte_count": 3,
        "width": 1920,
        "height": 1080,
        "fallback_from": "PrintWindow",
        "fallback_reason": "helper_low_information_capture",
        "acceptance_mode": "marionette_internal_fallback",
    }


def test_stagec_provider_reports_exact_semantic_settle_timeout_without_capture(tmp_path, monkeypatch):
    from agent_runtime import stagec_mcp_visual_provider as stagec

    calls = []

    class FakeClient:
        def __init__(self, config):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self):
            return {}

        def call_tool(self, name, arguments, *, allow_error_envelope=False):
            calls.append(name)
            if name == "mcp_launcher_qa_open_app_tab":
                return {"ok": True, "redaction": {"safe": True}}
            if name == "mcp_launcher_qa_wait_for_state":
                return {
                    "ok": False,
                    "failure_class": "assertion_failed",
                    "message_safe": "wait_for_state: 1 assertion(s) did not pass within 45000ms.",
                    "failed": [
                        {
                            "path": "mission_control.graph.mounted",
                            "expected": "== true",
                            "actual": "false",
                        }
                    ],
                }
            raise AssertionError("capture must not run after semantic timeout")

    monkeypatch.setattr(stagec, "StageCMcpJsonRpcClient", FakeClient)
    monkeypatch.setattr(stagec, "_marionette_preflight_enabled_for_config", lambda metadata, config: False)
    provider = stagec.StageCLauncherMcpVisualCaptureProvider(
        stagec.StageCMcpServerConfig(name="launcher_qa", command="fake")
    )

    import pytest

    with pytest.raises(stagec.StageCMcpError, match=r"assertion_failed: .*within 45000ms"):
        provider.capture_screenshot(
            ScreenshotRequest(
                task_id="task_stagec_timeout",
                stage_id="stage_1",
                scenario="mission_control",
                metadata={**_request(), "semantic_settle_timeout_ms": 45_000},
            )
        )

    assert calls == ["mcp_launcher_qa_open_app_tab", "mcp_launcher_qa_wait_for_state"]


def test_stagec_provider_uses_configured_launcher_qa_mcp_server(tmp_path, monkeypatch, isolate_agent_runtime_root):
    import os
    import sys
    from agent_runtime import stagec_mcp_visual_provider as stagec

    hermes_home = tmp_path / "hermes"
    profile_home = hermes_home / "profiles" / "launcher-qa"
    profile_home.mkdir(parents=True)
    artifact = tmp_path / "mission_control.png"
    args_capture = tmp_path / "stagec_args.json"
    server = tmp_path / "fake_stagec_mcp.py"
    server.write_text(
        """
import json
import os
import sys

artifact = os.environ["FAKE_STAGEC_ARTIFACT"]
args_path = os.environ["FAKE_STAGEC_ARGS"]
for line in sys.stdin:
    req = json.loads(line)
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        print(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"capabilities":{"tools":{}}}}), flush=True)
    elif method == "tools/list":
        print(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"tools":[{"name":"mcp_launcher_qa_open_app_tab"}]}}), flush=True)
    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        arguments = params.get("arguments", {})
        if name == "mcp_launcher_qa_open_app_tab":
            with open(args_path, "w", encoding="utf-8") as f:
                json.dump(arguments, f)
            envelope = {"ok": True, "redaction": {"safe": True}}
        elif name == "mcp_launcher_qa_wait_for_state":
            envelope = {"ok": True, "matched": 3, "elapsed_ms": 10, "poll_count": 1, "selected_tab": "missionControl", "redaction": {"safe": True}}
        elif name == "mcp_launcher_qa_get_navigation_state":
            envelope = {"ok": True, "response_safe": {"ok": True, "data": {"selected_tab": "missionControl", "route_name": "/shell", "blocking_modal": {"present": False}}}, "redaction": {"safe": True}}
        elif name == "mcp_launcher_qa_get_widget_state":
            envelope = {"ok": True, "response_safe": {"ok": True, "data": {"widget": "mission_control.graph", "mounted": True, "goal_id": "goal_test", "stage_count": 2}}, "redaction": {"safe": True}}
        elif name == "mcp_launcher_qa_screenshot_window":
            with open(artifact, "wb") as f:
                f.write(b"png")
            envelope = {"ok": True, "image_path": artifact, "bounds": {"width": 800, "height": 600}, "redaction": {"safe": True}}
        else:
            envelope = {"ok": False, "failure_class": "unexpected_tool", "message_safe": str(name)}
        print(json.dumps({"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":json.dumps(envelope)}],"isError":False}}), flush=True)
""".strip(),
        encoding="utf-8",
    )
    (hermes_home / "config.yaml").write_text(
        "agent_runtime:\n  personas:\n    qa:\n      hermes_profile: launcher-qa\n",
        encoding="utf-8",
    )
    profile_home.joinpath("config.yaml").write_text(
        "mcp_servers:\n"
        "  launcher_qa:\n"
        f"    command: {sys.executable}\n"
        f"    args: ['{server.as_posix()}']\n"
        "    env:\n"
        f"      FAKE_STAGEC_ARTIFACT: {artifact.as_posix()}\n"
        f"      FAKE_STAGEC_ARGS: {args_capture.as_posix()}\n"
        "      STAGEC_QA_REPO_ROOT: C:/stagec/repo\n"
        "      STAGEC_LAUNCH_HELPER: C:/stagec/repo/docs/stages/qa-reboot/scripts/Start-StageCDirectExe.ps1\n"
        "      STAGEC_SCREENSHOT_HELPER: C:/stagec/repo/docs/stages/qa-reboot/scripts/Capture-StageCWindowScreenshot.ps1\n"
        "    timeout: 5\n"
        "    connect_timeout: 5\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(isolate_agent_runtime_root))

    provider = stagec.default_launcher_qa_visual_provider()
    assert provider is not None
    capture = provider.capture_screenshot(
        ScreenshotRequest(
            task_id="task_visual",
            stage_id="stage_1",
            scenario="mission_control",
            metadata=_request(),
        )
    )

    assert capture.redaction_status == "safe"
    assert capture.capture_provider == "launcher_qa"
    assert Path(capture.path) == artifact
    import json

    args = json.loads(args_capture.read_text(encoding="utf-8"))
    assert args["force_relaunch"] is True
    assert args["browser_login"] is True
    assert "repoRoot" not in args
    assert "launchHelperPath" not in args
    assert "screenshotHelperPath" not in args


def test_stagec_provider_derives_launcher_helpers_from_configured_repo(tmp_path, monkeypatch, isolate_agent_runtime_root):
    from agent_runtime import stagec_mcp_visual_provider as stagec

    repo = tmp_path / "launcher"
    scripts = repo / "docs" / "stages" / "qa-reboot" / "scripts"
    scripts.mkdir(parents=True)
    launch_helper = scripts / "Start-StageCDirectExe.ps1"
    screenshot_helper = scripts / "Capture-StageCWindowScreenshot.ps1"
    launch_helper.write_text("", encoding="utf-8")
    screenshot_helper.write_text("", encoding="utf-8")
    artifact = tmp_path / "mission_control.png"
    artifact.write_bytes(b"png")
    seen = {}

    class FakeClient:
        def __init__(self, config):
            seen["env"] = dict(config.env)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def initialize(self):
            return {}

        def call_tool(self, name, arguments, *, allow_error_envelope=False):
            if name == "mcp_launcher_qa_open_app_tab":
                seen["args"] = dict(arguments)
            return _stagec_mission_control_result(name, artifact, width=1920, height=1080)

    monkeypatch.setattr(stagec, "StageCMcpJsonRpcClient", FakeClient)
    monkeypatch.setattr(stagec, "_marionette_preflight_enabled_for_config", lambda metadata, config: False)

    provider = stagec.StageCLauncherMcpVisualCaptureProvider(
        stagec.StageCMcpServerConfig(name="launcher_qa", command="fake", env={"STAGEC_QA_REPO_ROOT": str(repo)})
    )
    capture = provider.capture_screenshot(
        ScreenshotRequest(
            task_id="task_stagec_pins",
            stage_id="stage_1",
            scenario="mission_control",
            metadata=_request(),
        )
    )

    assert capture.path == str(artifact)
    assert "repoRoot" not in seen["args"]
    assert "launchHelperPath" not in seen["args"]
    assert "screenshotHelperPath" not in seen["args"]
    assert seen["env"]["STAGEC_QA_REPO_ROOT"] == str(repo)
    assert seen["env"]["STAGEC_LAUNCH_HELPER"] == str(launch_helper)
    assert seen["env"]["STAGEC_SCREENSHOT_HELPER"] == str(screenshot_helper)
