"""``harness status --json`` gains the two questions a durable runtime-root
service makes newly askable: *what code answered me*, and *how many serves are
running against this root*.

Both were unanswerable before. The runtime is an editable install, so nobody
had to ask which code a serve was on; and nothing anywhere counted the serves
against a root, which is why "is the launcher talking to the runtime I think it
is" has been a matter of inference in every 2026-08 transport incident.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import pytest

from hermes_cli.harness import build_parser


@pytest.fixture
def isolated_root(tmp_path, monkeypatch):
    root = tmp_path / "agent-runtime"
    root.mkdir(parents=True)
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    return root


@pytest.fixture(autouse=True)
def cheap_status(monkeypatch):
    """The service blocks are the subject; the rest of the envelope is not."""

    monkeypatch.setattr(
        "hermes_cli.harness.build_status",
        lambda: {
            "open_incidents": 0,
            "dirty_summary": "runtime=clean",
            "runtime_health": {"ok": True},
        },
    )


def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(argv)
    return args.func(args)


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip())


def _dead_pid() -> int:
    """A PID that is provably gone: spawned, waited on, and reaped."""

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


def test_runtime_build_names_the_process_that_answered(isolated_root, capsys):
    assert _run(["harness", "status", "--json"]) == 0

    build = _payload(capsys)["runtime_build"]

    # Run as a plain CLI, the answering process IS the install. Answered
    # through serve it would report the SERVICE — which is the whole point:
    # the two readings disagreeing is what "the service is stale" looks like.
    assert build["answered_by"] == "cli"
    assert set(build) >= {"commit", "dirty", "source", "resolved_at", "reason", "pid"}
    assert build["pid"] > 0


def test_serve_instances_are_listed_with_their_classification(isolated_root, capsys):
    from agent_runtime.serve_registry import register_serve_instance

    register_serve_instance(isolated_root, pid=_dead_pid())

    assert _run(["harness", "status", "--json"]) == 0

    rows = _payload(capsys)["serve_instances"]
    assert len(rows) == 1
    assert rows[0]["classification"] == "stale_dead_pid"
    assert rows[0]["classification_reason"] == "pid_not_running"


def test_a_plain_status_reports_stale_entries_and_deletes_nothing(isolated_root, capsys):
    from agent_runtime.serve_registry import register_serve_instance, serve_instance_path

    dead = _dead_pid()
    register_serve_instance(isolated_root, pid=dead)

    _run(["harness", "status", "--json"])
    _payload(capsys)

    assert serve_instance_path(isolated_root, dead).exists()


def test_prune_stale_deletes_only_the_provably_dead_and_says_what_it_deleted(
    isolated_root, capsys
):
    import os

    from agent_runtime.serve_registry import register_serve_instance, serve_instance_path

    dead = _dead_pid()
    register_serve_instance(isolated_root, pid=dead)
    # This test process: alive with a matching start time, but its command line
    # is pytest rather than a hermes serve — unclassifiable, and therefore
    # NEVER deleted. Fail-safe direction, proven against the real OS probe.
    register_serve_instance(isolated_root, pid=os.getpid())

    assert _run(["harness", "status", "--json", "--prune-stale"]) == 0

    payload = _payload(capsys)
    pruned = payload["serve_instances_pruned"]
    assert pruned["deleted_count"] == 1
    assert [row["pid"] for row in pruned["deleted"]] == [dead]
    assert [row["pid"] for row in pruned["kept"]] == [os.getpid()]
    assert not serve_instance_path(isolated_root, dead).exists()
    assert serve_instance_path(isolated_root, os.getpid()).exists()
    # The list reflects post-prune reality.
    assert [row["pid"] for row in payload["serve_instances"]] == [os.getpid()]


def test_the_human_line_reports_the_build_and_the_serve_count(isolated_root, capsys):
    from agent_runtime.serve_registry import register_serve_instance

    register_serve_instance(isolated_root, pid=_dead_pid())

    assert _run(["harness", "status"]) == 0

    line = capsys.readouterr().out.strip()
    assert "build=" in line
    assert line.endswith("serves=0/1")  # zero live, one entry on disk
