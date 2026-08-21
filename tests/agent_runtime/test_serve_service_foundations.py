"""Slice 2 of the durable runtime-root service, at the ``serve_loop`` seam:
the version handshake, the drain verb, the auth token, and discovery.

These land BEFORE any socket exists, deliberately. A durable service that
outlives the launcher silently pins last week's code (the dispatch
dead-flag-proxy incident is what a week-stale capability looks like); a socket
is reachable by any local process where an inherited stdio pipe was not; and
multiple runtime roots legitimately coexist on this machine. So the handshake,
the secret, and the registry are in place before the transport that needs them.

Frames are additive throughout: nothing here renames or removes an existing
key, and a consumer predating a new event ignores it.
"""

from __future__ import annotations

import io
import json
import os
import threading

import pytest

from hermes_cli.harness_parts.serve import (
    DRAIN_TIMEOUT_EXIT_CODE,
    DRAINING_EXIT_CODE,
    serve_loop,
)

SHUTDOWN = json.dumps({"op": "shutdown"}) + "\n"


def _request(rid: str, argv: list[str]) -> str:
    return json.dumps({"id": rid, "argv": argv}) + "\n"


def _frames(buffer: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buffer.getvalue().splitlines() if line]


def _one(frames: list[dict], event: str) -> dict:
    matches = [frame for frame in frames if frame.get("event") == event]
    assert len(matches) == 1, f"expected exactly one {event!r} frame, got {len(matches)}"
    return matches[0]


def _run(requests, *, dispatch=lambda argv: 0, **kwargs) -> tuple[list[dict], int]:
    out = io.StringIO()
    code = serve_loop(iter(requests), out, dispatch=dispatch, **kwargs)
    return _frames(out), code


# ── the isolation this whole file leans on ──────────────────────────────────


def test_the_autouse_fixture_really_isolates_the_store_root(isolate_agent_runtime_root):
    """Verified, not assumed: these tests MINT a token and REGISTER an instance
    under the resolved store root, so a leak here would write into the
    operator's live runtime."""

    from agent_runtime import paths

    resolved = paths.store_root()

    assert resolved == isolate_agent_runtime_root
    assert os.environ["HERMES_AGENT_RUNTIME_ROOT"] == str(isolate_agent_runtime_root)


# ── version handshake ───────────────────────────────────────────────────────


def test_the_ready_frame_carries_build_auth_and_instance_blocks():
    frames, code = _run([SHUTDOWN])

    ready = _one(frames, "ready")
    assert code == 0
    assert set(ready["build"]) == {"commit", "dirty", "source", "resolved_at"}
    assert ready["auth"] == {"token_file": "minted"}
    assert ready["instance"]["outcome"] == "registered"
    assert ready["instance"]["pid"] == os.getpid()
    assert ready["boot_id"] == ready["instance"]["boot_id"]
    # Existing keys are untouched — the whole change is additive.
    assert ready["pid"] == os.getpid() and ready["schema_version"] == 1


def test_the_ready_frame_never_carries_the_token_value(isolate_agent_runtime_root):
    from agent_runtime.serve_auth import read_token

    out = io.StringIO()
    serve_loop(iter([SHUTDOWN]), out, dispatch=lambda argv: 0)

    token = read_token(isolate_agent_runtime_root)
    assert token is not None and len(token) == 64
    assert token not in out.getvalue()


def test_the_version_op_answers_the_full_stamp_at_any_time():
    frames, _ = _run([json.dumps({"op": "version"}) + "\n", SHUTDOWN])

    version = _one(frames, "version")
    ready = _one(frames, "ready")
    assert version["build"]["commit"] == ready["build"]["commit"]
    assert version["build"]["source"] == ready["build"]["source"]
    # The full stamp adds the provenance + process facts the boot frame omits.
    assert {"reason", "repo_root", "pid", "boot_at", "uptime_ms"} <= set(version["build"])
    assert version["boot_id"] == ready["boot_id"]
    assert version["transport"] == "stdio"
    assert version["runtime_root"] == ready["runtime_root"]
    assert version["draining"] is False


# ── discovery ───────────────────────────────────────────────────────────────


def test_the_instance_file_exists_while_serving_and_is_gone_after_a_clean_exit(
    isolate_agent_runtime_root,
):
    from agent_runtime.serve_registry import serve_instance_path

    path = serve_instance_path(isolate_agent_runtime_root, os.getpid())
    seen: dict[str, object] = {}

    def dispatch(argv):
        # Observed from INSIDE a live request: "gone at the end" would also be
        # satisfied by a registry that never wrote anything at all.
        seen["exists"] = path.exists()
        seen["record"] = json.loads(path.read_bytes().decode("utf-8"))
        return 0

    _run([_request("r1", ["harness", "noop"]), SHUTDOWN], dispatch=dispatch)

    assert seen["exists"] is True
    assert seen["record"]["transport"] == "stdio"
    assert seen["record"]["build"]["source"] in {"git", "build_sha_file", "unknown"}
    assert not path.exists()


# ── discovery: pruning provably-dead records at boot ────────────────────────
#
# A clean exit removes its own entry; a crash deliberately does not, because a
# registry that could only be correct if every process died politely would be
# wrong exactly when it matters. The launcher's boot hygiene sweep ``taskkill
# /F``s orphan serves, which is a crash by construction — so the deliberate
# leftover accumulates without a floor (14 boots in ~19 h left 2 records on the
# operator's runtime).
#
# The sweep runs at BOOT, never on a listing, and it deletes ``stale_dead_pid``
# and nothing else. The cases below pin all three halves of that, because the
# widening — collapsing ``unknown`` into "delete" — is the direction that
# removes a RUNNING service's record, and this repo has already been bitten
# once by a recycled pid landing on an unrelated process.

#: Synthetic pids. Far above the default Windows/Linux allocation range so a
#: probe bug that reached the real OS could not accidentally find one alive.
DEAD_PID = 990_001
RECYCLED_PID = 990_002
UNKNOWN_PID = 990_003

#: The baseline recorded for every seeded record, and the one the fake probe
#: reports back for anything it considers unchanged.
_BASELINE_TICKS = 4_242_424_242
_SERVE_CMDLINE = "python -m hermes_cli.main harness serve --ndjson"


def _synthetic_probe(monkeypatch):
    """Pin classification to a table instead of to this machine's process list.

    The registry takes a ``ProcessProbe`` precisely so classification is
    testable against processes that do not exist. Patched at
    ``default_process_probe`` rather than passed in, because the call under test
    is serve's own and takes no probe argument — which is itself part of what
    these cases pin: boot must not be able to smuggle in a widened classifier.

    ``os.getpid()`` answers as a live hermes serve so THIS process's own entry —
    written by the boot a few lines before the prune — classifies ``live`` and
    is never a candidate.
    """

    from agent_runtime import serve_registry

    alive = {
        os.getpid(): True,
        DEAD_PID: False,  # -> stale_dead_pid
        RECYCLED_PID: True,  # -> stale_recycled_pid, via the start-time mismatch
        UNKNOWN_PID: None,  # -> unknown, the fail-safe direction
    }
    start_time = {
        os.getpid(): _BASELINE_TICKS,
        RECYCLED_PID: _BASELINE_TICKS + 1,  # a DIFFERENT process wearing the number
        UNKNOWN_PID: None,
    }
    probe = serve_registry.ProcessProbe(
        alive=lambda pid: alive.get(int(pid), False),
        start_time=lambda pid: start_time.get(int(pid), _BASELINE_TICKS),
        cmdline=lambda pid: _SERVE_CMDLINE,
    )
    monkeypatch.setattr(serve_registry, "default_process_probe", lambda: probe)
    return probe


def _seed_instance(root, pid: int):
    """Write one registry record for *pid*, through the real writer.

    Seeded with its OWN probe, which always reports ``_BASELINE_TICKS``. That
    separation is what makes the recycled case real: the record is written
    carrying the baseline of the process that registered, and the classifying
    probe later reports a DIFFERENT start time for the same number — which is
    precisely what pid recycling looks like from the reader's side.
    """

    from agent_runtime.serve_registry import (
        ProcessProbe,
        register_serve_instance,
        serve_instance_path,
    )

    seed_probe = ProcessProbe(
        alive=lambda _pid: True,
        start_time=lambda _pid: _BASELINE_TICKS,
        cmdline=lambda _pid: _SERVE_CMDLINE,
    )
    register_serve_instance(root, pid=pid, boot_id=f"boot-{pid}", probe=seed_probe)
    path = serve_instance_path(root, pid)
    assert path.exists(), "the probe's own seed did not write a record"
    return path


def _service_lines(frames: list[dict]) -> list[dict]:
    """The serve's structured ``_service_log`` lines, out of the frame stream.

    ``serve_loop`` swaps ``sys.stderr`` for a line proxy before any of this
    runs, so a structured stderr line arrives at the supervisor as an ordinary
    ``{"id":null,"event":"stderr","line":…}`` frame rather than on the real
    stderr. Read from the frames for that reason — it is also where a real
    supervisor would read it.
    """

    out = []
    for frame in frames:
        if frame.get("event") != "stderr":
            continue
        try:
            parsed = json.loads(frame.get("line") or "")
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("event"):
            out.append(parsed)
    return out


def test_a_provably_dead_record_is_pruned_at_boot_and_the_report_names_it(
    isolate_agent_runtime_root, monkeypatch
):
    """The whole point, in one case: the wreckage goes, the evidence stays.

    Both halves are asserted, and the second is the one that makes this
    defensible against the module's own "listing never prunes" rule. Deleting
    the record silently would tidy the evidence away exactly as a pruning
    listing would; deleting it while the report names pid, boot_id, path and
    classification on a channel correlatable by ``boot_id`` moves the evidence
    rather than destroying it.

    *Killing mutation:* drop the ``prune_stale_serve_instances`` call from serve
    boot (the record survives), or keep the call and drop the ``_service_log``
    (the record goes and nothing says so).
    """

    _synthetic_probe(monkeypatch)
    dead = _seed_instance(isolate_agent_runtime_root, DEAD_PID)

    frames, code = _run([SHUTDOWN])
    assert code == 0
    assert _one(frames, "ready")["instance"]["outcome"] == "registered"

    assert not dead.exists(), (
        "a record whose pid is provably gone survived a serve boot — the "
        "registry accumulates tombstones with no floor, because the launcher's "
        "hygiene sweep taskkill /F's orphans and they never unregister"
    )

    reports = [
        line
        for line in _service_lines(frames)
        if line.get("event") == "serve_instances_pruned"
    ]
    assert len(reports) == 1, (
        f"expected exactly one serve_instances_pruned line, got {len(reports)}"
    )
    report = reports[0]
    assert report["deleted_count"] == 1
    deleted = report["deleted"]
    assert [row["pid"] for row in deleted] == [DEAD_PID]
    assert deleted[0]["boot_id"] == f"boot-{DEAD_PID}"
    assert deleted[0]["path"] == str(dead)
    assert deleted[0]["classification"] == "stale_dead_pid", (
        "the report does not say WHY the record was deleted, so an operator "
        "reading the log cannot tell a tidy-up from a mistake"
    )
    assert report["boot_id"] == _one(frames, "ready")["boot_id"], (
        "the report is not correlatable with the boot that produced it, which "
        "is the whole reason moving the evidence into the log is acceptable"
    )


@pytest.mark.parametrize(
    "pid, expected",
    [
        (RECYCLED_PID, "stale_recycled_pid"),
        (UNKNOWN_PID, "unknown"),
    ],
)
def test_boot_never_prunes_a_recycled_or_unanswerable_record(
    isolate_agent_runtime_root, monkeypatch, pid, expected
):
    """Everything that is not provably dead SURVIVES a boot.

    ``stale_recycled_pid`` names a live process this registry no longer
    understands and an operator must be able to see it. ``unknown`` means a
    probe could not answer — and "I could not read this process's start time" is
    not evidence that it is gone. Deleting on a failed probe is how a sweep
    removes a RUNNING service's record.

    *Killing mutation:* the widening. Prune anything that is not ``live``, or
    add ``unknown`` to the deleted set. This is the direction that does real
    damage, and it is the reason this pair exists rather than a single
    happy-path case.
    """

    _synthetic_probe(monkeypatch)
    kept = _seed_instance(isolate_agent_runtime_root, pid)

    # Anti-vacuity: assert the record really is in the class this case names,
    # so a probe that quietly answered "dead" could not make it pass by
    # measuring nothing.
    from agent_runtime.serve_registry import list_serve_instances

    rows = {row["pid"]: row for row in list_serve_instances(isolate_agent_runtime_root)}
    assert rows[pid]["classification"] == expected, (
        f"the probe classified {pid} as {rows[pid]['classification']!r}, not "
        f"{expected!r} — this case is not measuring what it claims"
    )

    frames, _ = _run([SHUTDOWN])

    assert kept.exists(), (
        f"a {expected!r} record was deleted at serve boot. Only "
        "stale_dead_pid may ever be pruned: unknown is the FAIL-SAFE direction "
        "and collapsing it into 'dead' is how a sweep removes the record of a "
        "service that is still running"
    )
    assert [
        line
        for line in _service_lines(frames)
        if line.get("event") == "serve_instances_pruned"
    ] == [], "the boot logged a prune report for a boot that deleted nothing"


def test_listing_the_registry_still_never_prunes(
    isolate_agent_runtime_root, monkeypatch
):
    """The rule the boot sweep must not have relaxed.

    An operator debugging "why do I have four serves" reads the registry, and
    that read must not destroy the wreckage it is reporting. Boot is a different
    moment — a write moment, whose report relocates the evidence onto the
    service log — and this case is what keeps the two from being confused: the
    dead record is classified honestly by a listing and is still on disk
    afterwards.

    *Killing mutation:* move the prune into ``list_serve_instances``, which is
    the shape a "why not just always tidy up" refactor takes.
    """

    from agent_runtime.serve_registry import list_serve_instances

    _synthetic_probe(monkeypatch)
    dead = _seed_instance(isolate_agent_runtime_root, DEAD_PID)

    rows = list_serve_instances(isolate_agent_runtime_root)
    rows = list_serve_instances(isolate_agent_runtime_root)  # twice: still there

    assert [row["pid"] for row in rows] == [DEAD_PID]
    assert rows[0]["classification"] == "stale_dead_pid"
    assert dead.exists(), (
        "listing the registry deleted a record. A read must report the "
        "wreckage, not tidy it away before the operator has looked at it"
    )


# ── drain ───────────────────────────────────────────────────────────────────


def test_drain_lets_inflight_work_finish_refuses_new_work_and_accounts_for_both(
    isolate_agent_runtime_root,
):
    from agent_runtime.serve_registry import serve_instance_path

    started = threading.Event()
    release = threading.Event()
    dispatched: list[list[str]] = []
    woken = threading.Event()

    def dispatch(argv):
        dispatched.append(list(argv))
        if argv[-1] == "slow":
            started.set()
            assert release.wait(10)
            print("slow finished")
        return 0

    def requests():
        yield _request("slow-1", ["harness", "noop", "slow"])
        assert started.wait(10)
        yield json.dumps({"op": "drain"}) + "\n"
        yield _request("late-1", ["harness", "noop", "late"])
        release.set()
        # Stands in for the real reader, which stays parked on the pipe until
        # the drain finishes and closes it.
        assert woken.wait(10)

    out = io.StringIO()
    code = serve_loop(
        requests(),
        out,
        dispatch=dispatch,
        drain_poll_interval_seconds=0.01,
        drain_wakeup=woken.set,
    )
    frames = _frames(out)

    assert code == 0
    # The in-flight request really ran to completion...
    assert ["harness", "noop", "slow"] in dispatched
    assert any(f.get("id") == "slow-1" and f.get("line") == "slow finished" for f in frames)
    assert {"id": "slow-1", "event": "exit", "code": 0} in frames
    # ...and the late one really never reached a handler.
    assert ["harness", "noop", "late"] not in dispatched
    refusal = [f for f in frames if f.get("event") == "draining" and f.get("id") == "late-1"]
    assert len(refusal) == 1
    assert {
        "id": "late-1",
        "event": "exit",
        "code": DRAINING_EXIT_CODE,
        "draining": True,
    } in frames
    complete = _one(frames, "drain_complete")
    assert complete["requests_refused"] == 1
    assert complete["requests_completed"] == 1
    assert isinstance(complete["drain_ms"], int)
    # A drain is a clean exit, so the registry entry goes with it.
    assert not serve_instance_path(isolate_agent_runtime_root, os.getpid()).exists()
    # The typed drain frame is terminal; `shutdown` would claim a different exit.
    assert not [f for f in frames if f.get("event") == "shutdown"]


def test_drain_completes_immediately_when_nothing_is_in_flight():
    woken = threading.Event()

    def requests():
        yield json.dumps({"op": "drain"}) + "\n"
        assert woken.wait(10)

    out = io.StringIO()
    code = serve_loop(
        requests(), out, dispatch=lambda argv: 0, drain_poll_interval_seconds=0.01, drain_wakeup=woken.set
    )
    frames = _frames(out)

    assert code == 0
    assert _one(frames, "drain_complete")["requests_refused"] == 0
    assert _one(frames, "drain_complete")["requests_completed"] == 0


def test_a_drain_that_outlives_its_deadline_times_out_names_the_stuck_work_and_exits_nonzero():
    """A drain that can hang forever is not a drain.

    The deadline is the whole feature, so this proves all three of its
    obligations: the typed frame is emitted BEFORE the exit, it names WHICH
    requests are stuck, and the process-level lever is pulled with a nonzero
    code (``hard_exit`` is injected here, so no test process dies).
    """

    started = threading.Event()
    release = threading.Event()
    exits: list[int] = []
    woken = threading.Event()

    def dispatch(argv):
        started.set()
        assert release.wait(20)
        return 0

    def requests():
        yield _request("stuck-1", ["harness", "noop", "forever"])
        assert started.wait(10)
        yield json.dumps({"op": "drain", "deadline_seconds": 0.05}) + "\n"
        assert woken.wait(10)

    out = io.StringIO()
    try:
        code = serve_loop(
            requests(),
            out,
            dispatch=dispatch,
            drain_poll_interval_seconds=0.01,
            drain_wakeup=woken.set,
            hard_exit=exits.append,
        )
    finally:
        release.set()
    frames = _frames(out)

    timeout = _one(frames, "drain_timeout")
    assert timeout["stuck_request_ids"] == ["stuck-1"]
    assert timeout["deadline_seconds"] == 0.05
    assert timeout["requests_completed"] == 0
    assert exits == [DRAIN_TIMEOUT_EXIT_CODE]
    assert code == DRAIN_TIMEOUT_EXIT_CODE
    assert not [f for f in frames if f.get("event") == "drain_complete"]


def test_a_second_drain_is_reported_not_restarted():
    started = threading.Event()
    release = threading.Event()
    woken = threading.Event()

    def dispatch(argv):
        started.set()
        assert release.wait(10)
        return 0

    def requests():
        yield _request("slow-1", ["harness", "noop", "slow"])
        assert started.wait(10)
        yield json.dumps({"op": "drain"}) + "\n"
        yield json.dumps({"op": "drain"}) + "\n"
        release.set()
        assert woken.wait(10)

    out = io.StringIO()
    serve_loop(
        requests(),
        out,
        dispatch=dispatch,
        drain_poll_interval_seconds=0.01,
        drain_wakeup=woken.set,
    )
    frames = _frames(out)

    assert len([f for f in frames if f.get("event") == "draining" and f.get("id") is None]) == 1
    assert _one(frames, "drain_in_progress")["requests_refused"] == 0
    assert _one(frames, "drain_complete")["requests_completed"] == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 30.0),
        ("forever", 30.0),
        (True, 30.0),  # a bool is not a deadline
        (0, 0.05),
        (-5, 0.05),
        (10_000, 3600.0),
        (12.5, 12.5),
    ],
)
def test_a_client_supplied_deadline_is_clamped_into_the_sane_band(raw, expected):
    from hermes_cli.harness_parts.serve import _drain_deadline_seconds

    assert _drain_deadline_seconds(raw, 30.0) == expected


def test_ping_and_version_still_answer_while_draining():
    """Ops are not requests: a draining service must stay diagnosable."""

    woken = threading.Event()

    def requests():
        yield json.dumps({"op": "drain"}) + "\n"
        yield json.dumps({"op": "version"}) + "\n"
        yield json.dumps({"op": "ping"}) + "\n"
        assert woken.wait(10)

    out = io.StringIO()
    serve_loop(
        requests(),
        out,
        dispatch=lambda argv: 0,
        drain_poll_interval_seconds=0.01,
        drain_wakeup=woken.set,
    )
    frames = _frames(out)

    assert _one(frames, "version")["draining"] is True
    assert any(f.get("event") == "busy" for f in frames)


def test_cmd_serve_wires_the_process_level_drain_levers():
    """The injection contract cuts both ways: the loop's own tests run with
    both levers OFF, so the real entry point turning them ON is otherwise
    uncovered. Losing ``hard_exit`` would turn a drain TIMEOUT back into the
    unbounded hang it exists to bound — concurrent.futures' atexit hook joins
    the stuck worker on the way out.
    """

    import ast
    from pathlib import Path

    import hermes_cli.harness_parts.serve as serve_module

    tree = ast.parse(Path(serve_module.__file__).read_bytes().decode("utf-8"))
    keywords: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_serve":
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "serve_loop"
                ):
                    keywords = {kw.arg for kw in sub.keywords if kw.arg}

    assert {"drain_wakeup", "hard_exit"} <= keywords, (
        "_cmd_serve must pass drain_wakeup= and hard_exit= to serve_loop; "
        f"found {sorted(keywords)}"
    )
