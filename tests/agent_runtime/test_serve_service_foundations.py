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
