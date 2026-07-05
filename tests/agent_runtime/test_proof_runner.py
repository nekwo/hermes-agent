from __future__ import annotations

import shlex
import subprocess
import time

import pytest

from hermes_time import now

from agent_runtime.decision_schema import DecisionPayloadInvalid
from agent_runtime.events import EventLog
from agent_runtime.models import Proof, Task
from agent_runtime.proof_rules import ProofType
from agent_runtime.proof_runner import (
    CommandProofRunner,
    _redact_text,
    adapt_command_for_proof,
)
from agent_runtime.states import TaskState
from agent_runtime.store import ProofStore


def make_task() -> Task:
    ts = now()
    return Task(id="task_1", title="T", description="d", state=TaskState.RUNNING, created_at=ts, updated_at=ts, requested_by="tony")


def make_smoke_task() -> Task:
    ts = now()
    return Task(
        id="task_smoke",
        title="Stage 46 backend smoke",
        description="No product edits; collect bounded smoke proof only.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        risk_flags=["real_token_smoke", "no_product_edits"],
    )


def make_launcher_contract_task() -> Task:
    ts = now()
    return Task(
        id="task_launcher_contract",
        title="Stage 47 Launcher contract smoke",
        description="Launcher must consume the joined backend proof packet before QA.",
        state=TaskState.RUNNING,
        created_at=ts,
        updated_at=ts,
        requested_by="tony",
        acceptance_criteria=["A Launcher contract proof names the backend proof and consumed packet."],
        risk_flags=["cross_stack_contract"],
    )


def test_command_proof_runner_attaches_redacted_test_run_proof(tmp_path):
    task = make_task()
    store = ProofStore()
    runner = CommandProofRunner(proof_store=store, workdir=tmp_path, timeout_seconds=10)

    proofs = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=["printf 'ok SECRET_KEY=supersecret\\n'"],
    )

    assert len(proofs) == 1
    proof = proofs[0]
    assert proof.type == ProofType.TEST_RUN
    assert proof.task_id == task.id
    assert proof.stage_id == "stage_1"
    assert proof.created_by == "harness"
    assert proof.redaction_status == "safe"
    assert proof.metadata["exit_code"] == 0
    assert proof.metadata["command_index"] == 0
    assert proof.path_or_value.endswith(".log")
    artifact = runner.artifact_root / proof.path_or_value
    assert artifact.exists()
    content = artifact.read_text(encoding="utf-8")
    assert "supersecret" not in content
    assert "[REDACTED]" in content
    assert "workdir: <workdir:" in content
    stored = store.get(proof.id)
    assert stored.metadata["exit_code"] == 0
    assert "supersecret" not in stored.metadata["command"]
    assert stored.metadata["workdir_label"] == tmp_path.name
    assert stored.metadata["artifact_exists"] is True
    assert isinstance(stored.metadata["artifact_bytes"], int)
    assert stored.metadata["artifact_relative_path"] == proof.path_or_value
    assert "supersecret" not in stored.metadata["stdout_excerpt"]
    assert "[REDACTED]" in stored.metadata["stdout_excerpt"]
    event = store.event_log.tail(1)[0]
    assert event.type == "proof.attached"
    assert event.run_id == "run_1"
    assert event.persona_id == "dev"
    assert event.payload["phase"] == "proof"
    assert event.payload["severity"] == "info"
    assert event.payload["step"] == "command_proof"
    assert event.payload["status"] == "passed"
    assert event.payload["exit_code"] == 0
    assert event.payload["duration_ms"] >= 0
    assert event.payload["proof_id"] == proof.id
    assert event.payload["next_expected"] == "hand_off"
    assert "Command proof passed" in event.payload["summary"]


def test_command_proof_redaction_preserves_safe_key_substrings(tmp_path):
    task = make_task()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    proofs = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=[
            "python -c \"import json; print(json.dumps({'ok': True}, sort_keys=True))\""
        ],
    )

    artifact = runner.artifact_root / proofs[0].path_or_value
    content = artifact.read_text(encoding="utf-8")
    assert "sort_keys=True" in content
    assert "supersecret" not in content
    assert "api_key=abc" not in content
    assert "private_key=def" not in content
    redacted = _redact_text(
        "sort_keys=True monkey=True keyboard=True SECRET_KEY=supersecret "
        "api_key=abc private_key=def QUOTED_TOKEN=\"quoted-secret\" password='single-secret'"
    )
    assert "sort_keys=True" in redacted
    assert "monkey=True" in redacted
    assert "keyboard=True" in redacted
    assert "supersecret" not in redacted
    assert "abc" not in redacted
    assert "def" not in redacted
    assert "quoted-secret" not in redacted
    assert "single-secret" not in redacted
    assert "SECRET_KEY=[REDACTED]" in redacted
    assert "api_key=[REDACTED]" in redacted
    assert "private_key=[REDACTED]" in redacted
    assert "QUOTED_TOKEN=[REDACTED]" in redacted
    assert "password=[REDACTED]" in redacted


def test_command_proof_redaction_masks_absolute_paths_from_output():
    redacted = _redact_text(
        r'{"store_root": "X:\\Eternia\\.hermes\\agent-runtime", "interpreter": "C:\\Python312\\python.exe", "home": "/Users/beast/.hermes/profiles/alice"}'
    )

    assert "X:" not in redacted
    assert "C:" not in redacted
    assert "/Users/beast" not in redacted
    assert "<path:agent-runtime>" in redacted
    assert "<path:python.exe>" in redacted
    assert "<path:alice>" in redacted


def test_command_proof_runner_does_not_promote_unsafe_actor_to_event_persona(tmp_path):
    task = make_task()
    store = ProofStore()
    runner = CommandProofRunner(proof_store=store, workdir=tmp_path, timeout_seconds=10)

    runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev/../../token",
        commands=["printf ok"],
    )

    event = store.event_log.tail(1)[0]
    assert event.persona_id == "harness"
    assert "dev/" not in str(event)


def test_proof_store_attach_sanitizes_metadata_promoted_to_event():
    store = ProofStore()
    proof = Proof(
        id="proof_safe",
        task_id="task_1",
        stage_id="stage_1",
        type=ProofType.TEST_RUN,
        title="t",
        path_or_value="proofs/task_1/artifacts/proof_safe.log",
        created_by="harness",
        created_at=now(),
        metadata={
            "actor_requested": "dev",
            "run_id": "C:/Users/example/token.log",
            "status": "PASSED",
            "exit_code": -9,
            "duration_ms": -1,
        },
        redaction_status="safe",
    )

    store.attach(proof)

    event = store.event_log.tail(1)[0]
    assert event.run_id is None
    assert event.persona_id == "dev"
    assert event.payload["status"] == "passed"
    assert event.payload["severity"] == "info"
    assert event.payload["next_expected"] == "hand_off"
    assert event.payload["exit_code"] == -9
    assert "duration_ms" not in event.payload
    assert "C:/Users" not in str(event)
    assert "-1ms" not in event.payload["summary"]


def test_command_proof_runner_records_failed_command_as_proof(tmp_path):
    task = make_task()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    proofs = runner.run_commands(task, stage_id="stage_1", run_id="run_1", actor="dev", commands=["exit 7"])

    assert proofs[0].metadata["exit_code"] == 7
    assert proofs[0].metadata["status"] == "failed"


def test_command_proof_runner_records_intent_and_safe_environment_fingerprint(tmp_path):
    task = make_task()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    proofs = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=["printf ok"],
        proof_intent="Prove the focused smoke command passes before QA handoff.",
        environment_fingerprint=r"C:\Users\beast\secret\env.txt",
        environment_fingerprint_status="unchanged",
    )

    proof = proofs[0]
    assert proof.metadata["proof_intent"] == "Prove the focused smoke command passes before QA handoff."
    assert proof.metadata["environment_fingerprint"].startswith("sha256:")
    assert proof.metadata["environment_fingerprint_status"] == "unchanged"
    artifact = runner.artifact_root / proof.path_or_value
    content = artifact.read_text(encoding="utf-8")
    assert "proof_intent: Prove the focused smoke command passes before QA handoff." in content
    assert "C:\\Users" not in content
    assert "secret\\env" not in content


def test_command_proof_runner_times_out_runaway_process_tree(tmp_path):
    task = make_task()
    store = ProofStore()
    runner = CommandProofRunner(proof_store=store, workdir=tmp_path, timeout_seconds=1)
    started = time.monotonic()

    proofs = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=["python -c \"import time; time.sleep(20)\""],
    )

    assert time.monotonic() - started < 10
    assert proofs[0].metadata["timed_out"] is True
    assert proofs[0].metadata["status"] == "timeout"
    assert proofs[0].metadata["exit_code"] is None
    events = [event for event in store.event_log.tail(20) if event.type == "run.progress" and event.run_id == "run_1"]
    statuses = [event.payload.get("status") for event in events]
    assert "started" in statuses
    assert "timeout" in statuses
    timeout_event = [event for event in events if event.payload.get("status") == "timeout"][-1]
    assert timeout_event.payload["phase"] == "proof"
    assert timeout_event.payload["step"] == "proof_command_timeout"
    assert timeout_event.payload["timed_out"] is True


def test_command_proof_runner_refuses_smoke_full_suite_before_spawn(tmp_path):
    store = ProofStore()
    runner = CommandProofRunner(proof_store=store, workdir=tmp_path, timeout_seconds=10)
    command = "cd '/x/Unreal Engine/Engine/EterniaBackend/eternia-backend' && . .EterniaBackendVirtualEnv/Scripts/activate && python -V && python manage.py test --noinput"

    with pytest.raises(DecisionPayloadInvalid, match="proof execution boundary"):
        runner.run_commands(
            make_smoke_task(),
            stage_id="backend_dev_harness_smoke",
            run_id="run_1",
            actor="backend_dev",
            commands=[command],
        )
    event = [event for event in store.event_log.tail(10) if event.type == "run.progress" and event.run_id == "run_1"][-1]
    assert event.payload["step"] == "proof_command_refused"
    assert event.payload["status"] == "blocked"


def test_command_proof_runner_refuses_generic_launcher_contract_proof_before_spawn(tmp_path):
    store = ProofStore()
    runner = CommandProofRunner(proof_store=store, workdir=tmp_path, timeout_seconds=10)

    with pytest.raises(DecisionPayloadInvalid, match="Launcher contract proof policy"):
        runner.run_commands(
            make_launcher_contract_task(),
            stage_id="launcher_contract_smoke",
            run_id="run_1",
            actor="dev",
            commands=["flutter --version"],
            proof_intent="Validate backend proof packet consumption before QA.",
        )
    event = [event for event in store.event_log.tail(10) if event.type == "run.progress" and event.run_id == "run_1"][-1]
    assert event.payload["step"] == "proof_command_refused"
    assert event.payload["status"] == "blocked"
    assert "generic Flutter/Dart readiness" in event.payload["summary"]


def test_command_proof_runner_narrows_launcher_contract_main_analyze_before_spawn(tmp_path):
    store = ProofStore()
    runner = CommandProofRunner(proof_store=store, workdir=tmp_path, timeout_seconds=10)
    command = (
        "python -c \"contract_packet='packet_backend_stage47_contract_v1'; print('contract_packet_consumed')\" "
        "&& flutter analyze lib/main.dart"
    )

    proofs = runner.run_commands(
        make_launcher_contract_task(),
        stage_id="launcher_contract_smoke",
        run_id="run_1",
        actor="dev",
        commands=[command],
        proof_intent="Validate backend proof packet consumption before QA.",
    )

    proof = proofs[0]
    assert proof.metadata["exit_code"] == 0
    assert "flutter analyze lib/main.dart" not in proof.metadata["command"]
    assert "flutter analyze lib/main.dart" in proof.metadata["original_command"]
    assert "launcher_contract_analyze_narrowed" in proof.metadata["command_adapter"]
    event = [event for event in store.event_log.tail(10) if event.type == "run.progress" and event.run_id == "run_1"][0]
    assert event.payload["step"] == "proof_command_normalized"


def test_command_proof_runner_records_bounded_redacted_output_excerpts(tmp_path):
    task = make_task()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    proofs = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="dev",
        commands=["python -c \"print('a' * 2500); print('SECRET_TOKEN=hide-me')\""],
    )

    excerpt = proofs[0].metadata["stdout_excerpt"]
    assert len(excerpt) <= 2200
    assert "chars omitted" in excerpt
    assert "hide-me" not in excerpt
    assert "SECRET_TOKEN=[REDACTED]" in excerpt


def test_windows_pytest_command_proof_disables_sigalrm_timeout_mode():
    adapted = adapt_command_for_proof("pytest tests/agent_runtime -q", windows_host=True)

    assert adapted.command == "python -m pytest -o addopts='' -p no:timeout tests/agent_runtime -q"
    assert adapted.original_command == "pytest tests/agent_runtime -q"
    assert adapted.adapter == "windows_pytest_timeout_disabled"


def test_windows_python_m_pytest_command_proof_disables_sigalrm_timeout_mode():
    adapted = adapt_command_for_proof("python -m pytest tests/agent_runtime/test_proof_runner.py -q", windows_host=True)

    assert adapted.command == "python -m pytest -o addopts='' -p no:timeout tests/agent_runtime/test_proof_runner.py -q"
    assert adapted.adapter == "windows_pytest_timeout_disabled"


def test_pytest_command_proof_preserves_explicit_timeout_adapter_options():
    command = "python -m pytest -o addopts='' -p no:timeout tests/agent_runtime -q"
    adapted = adapt_command_for_proof(command, windows_host=True)

    assert adapted.command == command
    assert adapted.adapter is None


def test_eternia_backend_inline_django_proof_exports_settings(tmp_path):
    backend_root = tmp_path / "eternia-backend"
    backend_root.mkdir()
    command = (
        ". .EterniaBackendVirtualEnv/Scripts/activate && python - <<'PY'\n"
        "import os\n"
        "os.environ.setdefault('DJANGO_ENV', 'dev')\n"
        "import django\n"
        "django.setup()\n"
        "PY"
    )

    adapted = adapt_command_for_proof(command, windows_host=False, workdir=backend_root)

    assert adapted.command.startswith("export DJANGO_SETTINGS_MODULE=backend.settings && ")
    assert adapted.original_command == command
    assert adapted.adapter == "eternia_backend_django_settings_export"


def test_eternia_backend_inline_django_adapter_skips_manage_py(tmp_path):
    backend_root = tmp_path / "eternia-backend"
    backend_root.mkdir()
    command = "python manage.py check"

    adapted = adapt_command_for_proof(command, windows_host=False, workdir=backend_root)

    assert adapted.command == ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"
    assert adapted.adapter == "eternia_backend_virtualenv_python"


def test_eternia_backend_manage_py_adapter_repairs_wrong_venv_activation(tmp_path):
    backend_root = tmp_path / "eternia-backend"
    backend_root.mkdir()
    command = "source venv/Scripts/activate && python manage.py check"

    adapted = adapt_command_for_proof(command, windows_host=False, workdir=backend_root)

    assert adapted.command == ".EterniaBackendVirtualEnv/Scripts/python.exe manage.py check"
    assert adapted.original_command == command
    assert adapted.adapter == "eternia_backend_virtualenv_python"


def test_command_proof_runner_records_adapted_pytest_command_metadata(tmp_path, monkeypatch):
    import agent_runtime.proof_runner as proof_runner

    monkeypatch.setattr(proof_runner, "_is_windows_host", lambda: True)
    task = make_task()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    proofs = runner.run_commands(task, stage_id="stage_1", run_id="run_1", actor="dev", commands=["pytest --version"])

    proof = proofs[0]
    assert proof.metadata["command"] == "python -m pytest -o addopts='' -p no:timeout --version"
    assert proof.metadata["original_command"] == "pytest --version"
    assert proof.metadata["command_adapter"] == "windows_pytest_timeout_disabled"
    artifact = runner.artifact_root / proof.path_or_value
    content = artifact.read_text(encoding="utf-8")
    assert "command: python -m pytest -o addopts='' -p no:timeout --version" in content


def test_command_proof_runner_records_django_settings_adapter_metadata(tmp_path):
    backend_root = tmp_path / "eternia-backend"
    backend_root.mkdir()
    task = make_task()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=backend_root, timeout_seconds=10)

    proofs = runner.run_commands(
        task,
        stage_id="stage_1",
        run_id="run_1",
        actor="backend_dev",
        commands=["python - <<'PY'\nprint('import django')\nPY"],
    )

    proof = proofs[0]
    assert proof.metadata["status"] == "passed"
    assert proof.metadata["command"].startswith("export DJANGO_SETTINGS_MODULE=backend.settings && ")
    assert proof.metadata["original_command"] == "python - <<'PY'\nprint('import django')\nPY"
    assert proof.metadata["command_adapter"] == "eternia_backend_django_settings_export"
    artifact = runner.artifact_root / proof.path_or_value
    assert "command_adapter: eternia_backend_django_settings_export" in artifact.read_text(encoding="utf-8")


def test_backend_release_gate_fails_closed_when_docker_is_down(tmp_path, monkeypatch, isolate_agent_runtime_root):
    import agent_runtime.proof_runner as proof_runner

    backend_root = tmp_path / "eternia-backend"
    backend_root.mkdir()
    docker_calls = []

    def fake_docker_probe(argv, **_kwargs):
        docker_calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Cannot connect to Docker daemon")

    def fail_if_authoritative_command_launches(*_args, **_kwargs):
        raise AssertionError("backend release command launched despite failed Docker readiness")

    monkeypatch.setattr(proof_runner.subprocess, "run", fake_docker_probe)
    monkeypatch.setattr(proof_runner, "_run_bounded_process", fail_if_authoritative_command_launches)

    task = make_task()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=backend_root, timeout_seconds=10)

    proofs = runner.run_commands(
        task,
        stage_id="backend_release",
        run_id="run_1",
        actor="backend_dev",
        commands=["./scripts/test.sh"],
    )

    proof = proofs[0]
    assert docker_calls == [["docker", "version"]]
    assert proof.metadata["status"] == "failed"
    assert proof.metadata["exit_code"] is None
    assert proof.metadata["backend_release_gate_fail_closed"] is True
    assert proof.metadata["environment_readiness_check_id"] == "docker_version"
    assert "docker_unavailable" in proof.metadata["environment_readiness_reason"]
    artifact = runner.artifact_root / proof.path_or_value
    assert "environment_readiness_status: failed" in artifact.read_text(encoding="utf-8")
    events = EventLog().for_task(task.id, limit=10)
    assert any(event.type == "backend_release_gate_environment_failed" for event in events)


def test_redacted_workdir_prefix_is_removed_before_command_proof(tmp_path):
    adapted = adapt_command_for_proof("cd '<path:EterniaLauncher>' && flutter analyze tool/test/prod/smoke_runner.dart", windows_host=False, workdir=tmp_path)

    assert adapted.command == "flutter analyze tool/test/prod/smoke_runner.dart"
    assert adapted.original_command == "cd '<path:EterniaLauncher>' && flutter analyze tool/test/prod/smoke_runner.dart"
    assert adapted.adapter == "redacted_workdir_prefix_removed"


def test_redacted_workdir_prefix_adapter_combines_with_pytest_windows_adapter(tmp_path):
    adapted = adapt_command_for_proof("Set-Location '<path:repo>'; pytest tests/agent_runtime -q", windows_host=True, workdir=tmp_path)

    assert adapted.command == "python -m pytest -o addopts='' -p no:timeout tests/agent_runtime -q"
    assert adapted.adapter == "redacted_workdir_prefix_removed,windows_pytest_timeout_disabled"


def test_command_proof_runner_uses_posix_shell_for_quoted_paths(tmp_path):
    task = make_task()
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    runner = CommandProofRunner(proof_store=ProofStore(), workdir=tmp_path, timeout_seconds=10)

    command = f"cd {shlex.quote(workspace.as_posix())} && printf 'posix-ok' > proof.txt && test -f proof.txt"
    proofs = runner.run_commands(task, stage_id="stage_1", run_id="run_1", actor="qa", commands=[command])

    assert proofs[0].metadata["exit_code"] == 0
    assert proofs[0].metadata["status"] == "passed"
    assert (workspace / "proof.txt").read_text(encoding="utf-8") == "posix-ok"
