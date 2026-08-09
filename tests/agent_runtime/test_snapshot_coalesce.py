"""Coalesced snapshot core builds (Stage 14 slice 2).

Concurrent default-store core builds are strictly additive under the GIL
(measured 2026-07-09: one warm build 3.3s, three concurrent 8.8s EACH — the
launcher's "snapshot build 9050ms" chip). build_snapshot serializes and
coalesces the default path: arrivals during a build wait and share the NEXT
build (never the in-flight one), so a boot storm costs at most two
sequential fast builds. Injected stores bypass coalescing entirely.
"""

import threading
import time

import pytest

from agent_runtime import snapshot as snapshot_mod


@pytest.fixture(autouse=True)
def _reset_coalescer():
    with snapshot_mod._BUILD_COALESCE:
        snapshot_mod._build_coalesce_state.update(
            running=False, result=None, waiters=0
        )
    yield
    with snapshot_mod._BUILD_COALESCE:
        snapshot_mod._build_coalesce_state.update(
            running=False, result=None, waiters=0
        )


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_concurrent_storm_costs_at_most_two_builds(monkeypatch):
    calls: list[int] = []
    calls_lock = threading.Lock()
    first_build_started = threading.Event()
    release_first_build = threading.Event()

    def fake_build(**_kwargs):
        with calls_lock:
            calls.append(1)
            n = len(calls)
        first_build_started.set()
        if n == 1:
            assert release_first_build.wait(5)
        return {"n": n}

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    results: list[dict] = []
    results_lock = threading.Lock()

    def call():
        result = snapshot_mod.build_snapshot()
        with results_lock:
            results.append(result)

    first = threading.Thread(target=call)
    first.start()
    assert first_build_started.wait(5)

    storm = [threading.Thread(target=call) for _ in range(3)]
    for thread in storm:
        thread.start()
    # Let the storm enqueue as waiters behind the in-flight build.
    assert _wait_for(
        lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1
    )
    release_first_build.set()

    first.join(5)
    for thread in storm:
        thread.join(5)

    assert len(results) == 4
    # The whole storm cost exactly two builds: the in-flight one plus ONE
    # shared follow-up.
    assert len(calls) == 2
    assert sorted(result["n"] for result in results) == [1, 2, 2, 2]


def test_arrival_during_build_never_gets_the_inflight_result(monkeypatch):
    """A build already running when a caller arrives may predate that
    caller's writes — the caller must receive a build STARTED after it
    arrived."""
    release = {1: threading.Event(), 2: threading.Event()}
    calls: list[int] = []

    def fake_build(**_kwargs):
        generation = len(calls) + 1
        calls.append(generation)
        assert release[generation].wait(5)
        return {"generation": generation}

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    results: dict[str, dict] = {}
    first = threading.Thread(
        target=lambda: results.__setitem__("a", snapshot_mod.build_snapshot())
    )
    first.start()
    assert _wait_for(lambda: len(calls) == 1)

    second = threading.Thread(
        target=lambda: results.__setitem__("b", snapshot_mod.build_snapshot())
    )
    second.start()
    assert _wait_for(
        lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1
    )
    release[1].set()
    assert _wait_for(lambda: len(calls) == 2)
    release[2].set()

    first.join(5)
    second.join(5)
    assert results["a"] == {"generation": 1}
    assert results["b"] == {"generation": 2}


def test_shared_results_are_independent_copies(monkeypatch):
    calls: list[int] = []
    first_build_started = threading.Event()
    release_first_build = threading.Event()

    def fake_build(**_kwargs):
        calls.append(1)
        n = len(calls)
        first_build_started.set()
        if n == 1:
            assert release_first_build.wait(5)
        # The real core carries datetime objects — a JSON-based share copy
        # raised TypeError and silently degraded every waiter to its own
        # build (found live 2026-07-09). Pin that sharing survives them.
        from datetime import datetime, timezone

        return {
            "n": n,
            "nested": {"rows": [1, 2]},
            "generated": datetime.now(timezone.utc),
        }

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    results: list[dict] = []
    lock = threading.Lock()

    def call():
        result = snapshot_mod.build_snapshot()
        with lock:
            results.append(result)

    threads = [threading.Thread(target=call) for _ in range(3)]
    threads[0].start()
    assert first_build_started.wait(5)
    threads[1].start()
    threads[2].start()
    assert _wait_for(
        lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1
    )
    release_first_build.set()
    for thread in threads:
        thread.join(5)

    shared = [result for result in results if result["n"] == 2]
    assert len(shared) >= 2
    shared[0]["nested"]["rows"].append(99)
    shared[0]["mutated"] = True
    assert shared[1]["nested"]["rows"] == [1, 2]
    assert "mutated" not in shared[1]


def test_custom_stores_bypass_coalescing(monkeypatch):
    seen: list[dict] = []

    def fake_build(**kwargs):
        seen.append(kwargs)
        return {"custom": True}

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    sentinel = object()
    snapshot_mod.build_snapshot(task_store=sentinel)
    snapshot_mod.build_snapshot(task_store=sentinel)

    assert len(seen) == 2
    assert all(call["task_store"] is sentinel for call in seen)


def test_builder_exception_propagates_and_releases_the_gate(monkeypatch):
    attempts: list[int] = []

    def flaky_build(**_kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("store torn mid-read")
        return {"ok": True}

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", flaky_build)

    with pytest.raises(RuntimeError):
        snapshot_mod.build_snapshot()
    # The gate must not stay held after a failed build.
    assert snapshot_mod.build_snapshot() == {"ok": True}
    assert len(attempts) == 2


def test_accept_inflight_shares_the_running_build(monkeypatch):
    """``accept_inflight`` callers ride the build that is already running.

    Serve prewarms a core right after ``ready`` (T2) and the launcher's boot
    hydrate lands milliseconds later. Under the strict rule above that hydrate
    would wait for the prewarm to finish AND then pay a second build — worse
    than no prewarm at all. The hydrate is allowed to share because it carries
    the built core's own watermark and the stream replays every event after it.
    """

    calls: list[int] = []
    build_started = threading.Event()
    release_build = threading.Event()

    def fake_build(**_kwargs):
        generation = len(calls) + 1
        calls.append(generation)
        build_started.set()
        assert release_build.wait(5)
        return {"generation": generation}

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    results: dict[str, dict] = {}
    leader = threading.Thread(
        target=lambda: results.__setitem__("prewarm", snapshot_mod.build_snapshot())
    )
    leader.start()
    assert build_started.wait(5)

    joiner = threading.Thread(
        target=lambda: results.__setitem__(
            "hydrate", snapshot_mod.build_snapshot(accept_inflight=True)
        )
    )
    joiner.start()
    assert _wait_for(lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1)
    release_build.set()

    leader.join(5)
    joiner.join(5)

    assert results["prewarm"] == {"generation": 1}
    assert results["hydrate"] == {"generation": 1}
    assert calls == [1]  # ONE build served both


def test_accept_inflight_leads_its_own_build_when_nothing_is_running(monkeypatch):
    """Sharing is only ever with a build that is RUNNING. With no build in
    flight the caller builds — it never picks up a finished build's leftover
    result, which could be arbitrarily older than its arrival."""

    calls: list[int] = []

    def fake_build(**_kwargs):
        calls.append(1)
        return {"generation": len(calls)}

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)

    first = snapshot_mod.build_snapshot(accept_inflight=True)
    second = snapshot_mod.build_snapshot(accept_inflight=True)

    assert first == {"generation": 1}
    assert second == {"generation": 2}
    assert len(calls) == 2


def test_the_stream_hydrate_joins_the_running_build(monkeypatch):
    """The wiring that makes the prewarm worth having: the hydrate frame is
    the caller that opts into sharing (behavioural pin, not a call-arg pin)."""

    from agent_runtime import stream as stream_mod

    calls: list[int] = []
    build_started = threading.Event()
    release_build = threading.Event()

    def fake_build(**_kwargs):
        generation = len(calls) + 1
        calls.append(generation)
        build_started.set()
        assert release_build.wait(5)
        return {
            "generation": generation,
            "parity": {"watermark": {"event_offset": 7}},
            "generated_at": "2026-08-09T00:00:00Z",
        }

    monkeypatch.setattr(snapshot_mod, "_build_snapshot_uncoalesced", fake_build)
    monkeypatch.setattr(stream_mod, "_identity_map", lambda _snap: {})

    leader = threading.Thread(target=snapshot_mod.build_snapshot)
    leader.start()
    assert build_started.wait(5)

    frames: dict[str, dict] = {}
    hydrate = threading.Thread(
        target=lambda: frames.__setitem__("hydrate", stream_mod.hydrate_frame())
    )
    hydrate.start()
    assert _wait_for(lambda: snapshot_mod._build_coalesce_state["waiters"] >= 1)
    release_build.set()

    leader.join(5)
    hydrate.join(5)

    assert calls == [1]
    assert frames["hydrate"]["core"]["generation"] == 1
    # The shared core's own watermark rides out with it, so the stream tails
    # from exactly where that core ended — nothing is skipped.
    assert frames["hydrate"]["watermark"] == {"event_offset": 7}
