"""``agent_runtime.serve_registry`` — discovery, and the classification that
makes it safe to believe.

The registry file is written by a process that may die without cleaning up, so
the load-bearing behaviour is entirely on the READ side: every entry is proven
at read time, and every probe that cannot answer lands on ``unknown`` rather
than on a guess. Guessing "mine" is how a client attaches to a stranger;
guessing "stale" is how a prune deletes a running service's entry.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from agent_runtime.serve_registry import (
    CLASSIFICATION_LIVE,
    CLASSIFICATION_STALE_DEAD_PID,
    CLASSIFICATION_STALE_RECYCLED_PID,
    CLASSIFICATION_UNKNOWN,
    ProcessProbe,
    list_serve_instances,
    prune_stale_serve_instances,
    register_serve_instance,
    serve_instance_path,
    serve_instances_dir,
    unregister_serve_instance,
)

SERVE_CMDLINE = "python -m hermes_cli.main harness serve --ndjson"


def _probe(*, alive=True, start_time=1000, cmdline=SERVE_CMDLINE) -> ProcessProbe:
    return ProcessProbe(
        alive=lambda pid: alive,
        start_time=lambda pid: start_time,
        cmdline=lambda pid: cmdline,
    )


def _register(root, pid=4242, *, probe=None, **kwargs):
    kwargs.setdefault("argv", list(sys.argv))
    return register_serve_instance(root, pid=pid, probe=probe or _probe(), **kwargs)


def _dead_pid() -> int:
    """A PID that is provably gone: spawned, waited on, and reaped."""

    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    return process.pid


def test_registration_writes_the_full_record_under_the_store_root(tmp_path):
    report = _register(tmp_path, build={"commit": "abc123", "dirty": False})

    assert report.registered is True
    path = serve_instance_path(tmp_path, 4242)
    assert path == serve_instances_dir(tmp_path) / "4242.json"
    record = json.loads(path.read_bytes().decode("utf-8"))
    assert record["pid"] == 4242
    assert record["transport"] == "stdio"
    assert record["boot_id"] == report.boot_id and len(record["boot_id"]) == 32
    assert record["build"] == {"commit": "abc123", "dirty": False}
    assert record["started_at"].endswith("Z")
    # The identity baseline the recycled-pid check compares against later.
    assert record["started_at_ticks"] == 1000
    assert record["argv_hint"]


def test_the_argv_hint_stays_a_hint(tmp_path):
    """It is read by operators and pasted into reports; other harness lanes
    carry message text in argv, so the shape never grows past a hint."""

    _register(tmp_path, argv=["C:/py/python.exe", *[f"tok{i}" for i in range(40)]])

    record = json.loads(serve_instance_path(tmp_path, 4242).read_bytes().decode("utf-8"))

    assert record["argv_hint"].startswith("python.exe tok0")
    assert len(record["argv_hint"]) <= 200
    assert "tok30" not in record["argv_hint"]


def test_a_matching_live_serve_classifies_live(tmp_path):
    _register(tmp_path)

    rows = list_serve_instances(tmp_path, probe=_probe())

    assert [row["classification"] for row in rows] == [CLASSIFICATION_LIVE]
    assert rows[0]["classification_reason"] == ""
    assert rows[0]["pid"] == 4242


def test_a_dead_pid_classifies_stale_dead(tmp_path):
    _register(tmp_path)

    rows = list_serve_instances(tmp_path, probe=_probe(alive=False))

    assert rows[0]["classification"] == CLASSIFICATION_STALE_DEAD_PID
    assert rows[0]["classification_reason"] == "pid_not_running"


def test_a_recycled_pid_is_not_believed(tmp_path):
    """Alive, but a DIFFERENT process wearing a recycled number. This repo has
    already SIGTERMed an unrelated desktop process over exactly this."""

    _register(tmp_path, probe=_probe(start_time=1000))

    rows = list_serve_instances(tmp_path, probe=_probe(start_time=7777))

    assert rows[0]["classification"] == CLASSIFICATION_STALE_RECYCLED_PID
    assert rows[0]["classification_reason"] == "start_time_mismatch"


def test_an_unreadable_start_time_is_unknown_not_live(tmp_path):
    _register(tmp_path)

    rows = list_serve_instances(tmp_path, probe=_probe(start_time=None))

    assert rows[0]["classification"] == CLASSIFICATION_UNKNOWN
    assert rows[0]["classification_reason"] == "start_time_unreadable"


def test_an_unreadable_liveness_probe_is_unknown_not_dead(tmp_path):
    """The fail-safe direction: a failed probe must never authorise a delete."""

    _register(tmp_path)
    blind = ProcessProbe(
        alive=lambda pid: None, start_time=lambda pid: 1000, cmdline=lambda pid: SERVE_CMDLINE
    )

    rows = list_serve_instances(tmp_path, probe=blind)

    assert rows[0]["classification"] == CLASSIFICATION_UNKNOWN
    assert rows[0]["classification_reason"] == "liveness_unreadable"


def test_a_live_pid_that_is_not_a_serve_is_never_live(tmp_path):
    _register(tmp_path)

    rows = list_serve_instances(tmp_path, probe=_probe(cmdline="notepad.exe"))

    assert rows[0]["classification"] != CLASSIFICATION_LIVE
    assert rows[0]["classification_reason"] == "cmdline_not_serve_like"


def test_without_an_identity_baseline_a_foreign_cmdline_means_recycled(tmp_path):
    """With no start-time baseline the command line is the ONLY recycling
    signal there is, so it has to be decisive rather than merely doubtful."""

    _register(tmp_path, probe=_probe(start_time=None))

    rows = list_serve_instances(tmp_path, probe=_probe(cmdline="firefox.exe"))

    assert rows[0]["classification"] == CLASSIFICATION_STALE_RECYCLED_PID


def test_an_unparseable_record_is_reported_not_dropped(tmp_path):
    serve_instances_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (serve_instances_dir(tmp_path) / "99.json").write_bytes(b"{not json")

    rows = list_serve_instances(tmp_path, probe=_probe())

    assert rows[0]["classification"] == CLASSIFICATION_UNKNOWN
    assert rows[0]["classification_reason"] == "record_unreadable"
    assert rows[0]["pid"] == 99


def test_listing_never_prunes(tmp_path):
    """An operator debugging "why do I have four serves" must see the wreckage,
    not a registry that tidied the evidence away before they looked."""

    _register(tmp_path)

    list_serve_instances(tmp_path, probe=_probe(alive=False))

    assert serve_instance_path(tmp_path, 4242).exists()


def test_prune_deletes_only_provably_dead_entries_and_says_which(tmp_path):
    _register(tmp_path, pid=101, probe=_probe(start_time=1))
    _register(tmp_path, pid=202, probe=_probe(start_time=2))
    _register(tmp_path, pid=303, probe=_probe(start_time=3))

    def classify(pid):
        return {101: False, 202: True, 303: True}[pid]

    probe = ProcessProbe(
        alive=classify,
        # 303 comes back with a different start time -> recycled, must be KEPT.
        start_time=lambda pid: {202: 2, 303: 999}.get(pid),
        cmdline=lambda pid: SERVE_CMDLINE,
    )

    report = prune_stale_serve_instances(tmp_path, probe=probe)

    assert report["deleted_count"] == 1
    assert [row["pid"] for row in report["deleted"]] == [101]
    assert report["deleted"][0]["classification"] == CLASSIFICATION_STALE_DEAD_PID
    assert sorted(row["pid"] for row in report["kept"]) == [202, 303]
    assert not serve_instance_path(tmp_path, 101).exists()
    assert serve_instance_path(tmp_path, 202).exists()
    assert serve_instance_path(tmp_path, 303).exists()


def test_prune_keeps_entries_it_could_not_classify(tmp_path):
    _register(tmp_path, pid=505)
    blind = ProcessProbe(
        alive=lambda pid: None, start_time=lambda pid: None, cmdline=lambda pid: None
    )

    report = prune_stale_serve_instances(tmp_path, probe=blind)

    assert report["deleted"] == []
    assert serve_instance_path(tmp_path, 505).exists()


def test_unregister_removes_the_entry_and_is_idempotent(tmp_path):
    _register(tmp_path, pid=606)

    assert unregister_serve_instance(tmp_path, pid=606) is True
    assert unregister_serve_instance(tmp_path, pid=606) is False
    assert list_serve_instances(tmp_path, probe=_probe()) == []


def test_the_real_os_probe_recognises_a_process_that_has_exited(tmp_path):
    """One pass with NO injected probe: the default path must actually work.

    Every other test here drives synthetic probes, which would all still pass
    if the real ``_pid_alive`` / start-time readers were broken.
    """

    register_serve_instance(tmp_path, pid=_dead_pid(), build=None)

    rows = list_serve_instances(tmp_path)

    assert rows[0]["classification"] == CLASSIFICATION_STALE_DEAD_PID


def test_an_absent_registry_directory_lists_empty(tmp_path):
    assert list_serve_instances(tmp_path / "never-booted", probe=_probe()) == []


def test_registration_reports_failure_instead_of_raising(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_bytes(b"not a directory\n")

    report = register_serve_instance(blocked / "root", pid=1, probe=_probe())

    assert report.registered is False
    assert report.outcome.startswith("error:")


@pytest.mark.parametrize("pid", [0, -1, "seven", None])
def test_a_record_without_a_usable_pid_is_unknown(tmp_path, pid):
    from agent_runtime.serve_registry import classify_serve_instance

    classification, reason = classify_serve_instance({"pid": pid}, probe=_probe())

    assert classification == CLASSIFICATION_UNKNOWN
    assert reason == "pid_missing"
