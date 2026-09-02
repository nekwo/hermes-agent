"""The gateway fence holds — including in the window a fixture cannot reach.

Measured escape this pins (2026-08-31, this Windows workstation)::

    test_update_autostash.py::test_cmd_update_skips_stash_restore_when_reset_fails
      -> cmd_update -> _cmd_update_impl
           atexit.register(_resume_windows_gateways_after_update,
                           {"cold_start_if_installed": True, ...})
    -- interpreter exit, every monkeypatch undone --
      -> _cold_start_windows_gateway_after_update -> _spawn_detached
      -> Popen: python.exe -m hermes_cli.main --profile alice gateway run

Each layer gets its own claim, and each claim is red when only that layer is
reverted (proof recorded in
``docs/agent-runtime-harness/planned/dcw-h2-field-notes-2026-08-31.md``):

* L1 — ``_cmd_update_impl`` never registers the atexit handler.
* L2 — ``gateway_windows._spawn_detached`` refuses for the whole session.
* L3 — the spawn primitives refuse a backend argv *outside* any fixture
  window, which is what the atexit escape needed.
"""

from __future__ import annotations

import atexit
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from tests.hermes_cli import _gateway_fence
from tests.hermes_cli._gateway_fence import GatewayFenceViolation


# ── L3: the classifier ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cmd",
    [
        # The exact argv measured leaving this suite.
        [sys.executable, "-m", "hermes_cli.main", "--profile", "alice", "gateway", "run"],
        # Entry-point spellings.
        ["hermes", "gateway", "run"],
        ["hermes.exe", "serve"],
        ["/opt/hermes/.venv/bin/hermes", "dashboard"],
        # Wrapped in a shell: the subcommand lives inside one argv element.
        ["bash", "-c", "hermes gateway run"],
        "hermes serve",
        # The Windows persistence artifacts.
        [r"C:\Windows\System32\wscript.exe", r"C:\Users\x\.hermes\gateway.vbs"],
        [r"C:\Users\x\.hermes\gateway.cmd"],
        # A mutating schtasks verb against the real Task Scheduler.
        ["schtasks", "/Create", "/TN", "Hermes_Gateway_alice", "/TR", "x"],
        ["schtasks.EXE", "/Run", "/TN", "Hermes_Gateway_alice"],
    ],
)
def test_classifier_refuses_live_gateway_commands(cmd):
    assert _gateway_fence.classify(cmd) is not None


@pytest.mark.parametrize(
    "cmd",
    [
        ["git", "status"],
        ["node", "--version"],
        ["hermes", "profile", "list"],
        ["hermes", "--profile", "alice", "config", "get", "model"],
        # /Query is read-only: reading a task cannot start one, and the
        # install-detection branch stays testable.
        ["schtasks", "/Query", "/TN", "Hermes_Gateway_alice"],
        # "gateway" as a NOUN in a path is not a subcommand of a hermes entry.
        ["cat", "/var/log/gateway/run.log"],
    ],
)
def test_classifier_allows_ordinary_commands(cmd):
    assert _gateway_fence.classify(cmd) is None


def test_classifier_refuses_a_hermes_run_pointed_at_the_real_store():
    """The second half of the escape: not just *a* gateway, the operator's."""
    real_root = _gateway_fence.real_root()
    if real_root is None:
        pytest.skip("no default hermes root resolvable on this host")
    reason = _gateway_fence.classify(
        ["hermes", "config", "get", "model"],
        env={"HERMES_HOME": str(real_root / "profiles" / "alice")},
    )
    assert reason is not None
    assert "REAL store" in reason


def test_the_agent_browser_capability_probe_is_not_refused():
    """The over-refusal this arm was narrowed away from, pinned so it stays gone.

    ``hermes doctor`` probes an agent-browser shim that lives inside the
    operator's live profile. That IS a finding -- the suite reads a tool out of
    a real profile home -- but it is a ``--version`` call that starts nothing,
    and refusing it turned 44 tests across 8 files red for a hazard this fence
    is not about.

    The path is resolved by ``shutil.which("agent-browser")`` off the
    operator's PATH, which ``run_tests.sh`` forwards verbatim. It was recorded
    as doctor's import-time ``HERMES_HOME`` binding; that was wrong, and was
    re-measured 2026-09-02 when the binding moved to call time and this spawn
    did not move with it.
    """
    real_root = _gateway_fence.real_root()
    if real_root is None:
        pytest.skip("no default hermes root resolvable on this host")
    probe = str(real_root / "profiles" / "alice" / "node" / "agent-browser.CMD")
    assert _gateway_fence.classify([probe, "--version"]) is None


@pytest.mark.parametrize(
    "argv",
    [
        ["npm", "--prefix", "{root}", "install"],
        ["node", "{root}/profiles/alice/node/whatever.js"],
        ["{root}/profiles/alice/node/agent-browser.CMD", "install"],
        ["{root}/profiles/alice/node/agent-browser.CMD", "--version", "--json"],
    ],
    ids=["npm-prefix", "node-script", "agent-browser-install", "probe-plus-a-flag"],
)
def test_only_the_version_probe_itself_is_exempt(argv):
    """The exemption is the PROBE, not "any argv without a hermes entry point".

    Spelled the old way, every one of these reached the operator's store
    unremarked -- an ``npm install`` into a real profile is exactly the class
    of write this fence exists to stop, and it was allowed for the sake of one
    read-only ``--version``.
    """
    real_root = _gateway_fence.real_root()
    if real_root is None:
        pytest.skip("no default hermes root resolvable on this host")
    root = str(real_root).replace("\\", "/")
    reason = _gateway_fence.classify([tok.replace("{root}", root) for tok in argv])
    assert reason is not None
    assert "REAL store" in reason


# ── L3: the primitives actually refuse ─────────────────────────────────────
#
# Every test below carries ``live_system_guard_bypass``, and the mark is the
# claim rather than a convenience. Inside an ordinary test body the root
# conftest's autouse ``_live_system_guard`` sits IN FRONT of this fence and
# raises first — so an unmarked test would prove that guard, not this one, and
# would keep passing if this fence were deleted. The mark switches that
# fixture off for the test, reproducing the condition of the window the escape
# actually used (atexit: no fixture wrappers left anywhere) while everything
# else stays normal. What survives to refuse the spawn is this fence alone.


@pytest.mark.live_system_guard_bypass
def test_popen_refuses_the_measured_argv():
    with pytest.raises(GatewayFenceViolation) as excinfo:
        subprocess.Popen(
            [sys.executable, "-m", "hermes_cli.main", "--profile", "alice", "gateway", "run"]
        )
    assert "START a live hermes backend" in str(excinfo.value)


@pytest.mark.live_system_guard_bypass
def test_subprocess_run_refuses_the_measured_argv():
    with pytest.raises(GatewayFenceViolation):
        subprocess.run([sys.executable, "-m", "hermes_cli.main", "gateway", "run"])


@pytest.mark.live_system_guard_bypass
def test_fence_survives_a_teardown_of_every_monkeypatch():
    """The property that separates a process fence from a fixture guard.

    A fixture guard captures whatever ``subprocess.Popen`` is at setup and
    puts it back at teardown. A teardown here must therefore land on the
    FENCE's wrapper, not on the raw ``Popen`` — otherwise every teardown in
    the run reopens the hole for a moment, and the last teardown of the
    session reopens it for good.

    Scoped context rather than the shared ``monkeypatch`` fixture: ``undo()``
    takes no argument and would unwind every autouse guard in the tree (the
    ``_shared_monkeypatch_pin_tripwire`` in tests/conftest.py catches exactly
    that, and is right to).
    """
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(subprocess, "Popen", lambda *a, **k: "not the real thing")
        assert subprocess.Popen("anything") == "not the real thing"
    with pytest.raises(GatewayFenceViolation):
        subprocess.Popen([sys.executable, "-m", "hermes_cli.main", "gateway", "run"])


@pytest.mark.live_system_guard_bypass
def test_fence_holds_on_a_background_thread():
    """A thread outlives the test body; the fence is not thread-local."""
    caught: list[BaseException] = []

    def _spawn():
        try:
            subprocess.Popen(["hermes", "gateway", "run"])
        except BaseException as exc:  # noqa: BLE001 — recording, then re-asserted
            caught.append(exc)

    worker = threading.Thread(target=_spawn)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert caught and isinstance(caught[0], GatewayFenceViolation)


# ── L2: the product chokepoint ─────────────────────────────────────────────


def test_spawn_detached_is_fenced_for_the_whole_session():
    gateway_windows = pytest.importorskip("hermes_cli.gateway_windows")
    with pytest.raises(GatewayFenceViolation) as excinfo:
        gateway_windows._spawn_detached()
    assert "single chokepoint" in str(excinfo.value)


def test_cold_start_helper_cannot_reach_a_live_spawn(monkeypatch):
    """Drive the exact function the atexit handler called.

    ``_cold_start_windows_gateway_after_update`` swallows every exception by
    design (it is best-effort), so the claim is not "it raises" — it is that
    the fence, not luck, is what stops it: with the spawn fenced it prints no
    PID, and the fenced ``_spawn_detached`` is the callee it reached.
    """
    from hermes_cli import gateway_windows, update_cmd

    monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(_is_windows=lambda: True))
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda **_k: [])
    calls: list[object] = []
    real_fenced = gateway_windows._spawn_detached

    def _recording(script_path=None):
        calls.append(script_path)
        return real_fenced(script_path)

    monkeypatch.setattr(gateway_windows, "_spawn_detached", _recording)

    update_cmd._cold_start_windows_gateway_after_update()

    assert calls == [None], "the cold-start path did not reach _spawn_detached"


# ── L1: the atexit registration never happens ──────────────────────────────


def test_update_never_parks_a_gateway_resume_on_atexit(monkeypatch):
    """The root cause: no token, so nothing is parked for interpreter exit.

    Asserting on ``atexit.register`` rather than on the return value is
    deliberate — the escape was not that a token existed, it was that a
    callable holding it survived into a window with no guards left.
    """
    from hermes_cli import main as cli_main

    registered: list[object] = []
    monkeypatch.setattr(
        atexit, "register", lambda func, *a, **k: registered.append((func, a)) or func
    )

    monkeypatch.setattr(cli_main, "_is_windows", lambda: True)
    assert cli_main._pause_windows_gateways_for_update() is None
    assert registered == []


def test_the_pause_seam_is_defaulted_for_every_test_in_this_directory():
    """The seam itself, not just its answer on this host.

    Checking the RETURN VALUE alone is not a fence test, and measuring proved
    it: with the conftest default reverted, the production function still
    answered ``None`` here, because under a hermetic HERMES_HOME
    ``_profile_suffix()`` is empty, the task name resolves to the default
    ``Hermes_Gateway``, and this operator has only ``Hermes_Gateway_alice``
    registered. The escape depends on which task name the process happens to
    resolve — which is exactly the host-shaped fact a test must not rest on.
    (Reverted, ``test_update_autostash.py`` DOES bring the atexit registration
    and a real ``schtasks /Query /TN Hermes_Gateway_alice`` straight back;
    proof recorded in the field notes.)

    So the claim is the seam: for every test in this directory, the callable
    bound at ``hermes_cli.main`` is the conftest's default and not
    production's. That is red the moment the default is removed, on any host.
    """
    from hermes_cli import main as cli_main
    from hermes_cli import update_cmd

    bound = cli_main._pause_windows_gateways_for_update
    assert bound is not update_cmd._pause_windows_gateways_for_update, (
        "the autouse _no_windows_gateway_pause_token default is not installed — "
        "_cmd_update_impl would read this machine's live gateway table and "
        "Scheduled Task, and park a resume on atexit"
    )
    assert bound() is None


# ── Scoping: armed here, transparent everywhere else ───────────────────────
#
# The fence's first shape installed and refused in one act, and that reds
# another directory's honest work: importing this conftest at COLLECTION time
# armed a process-global refusal, and tests/agent_runtime's e2e tests spawn
# real `python -m hermes_cli.main harness serve` children on purpose. Measured
# 2026-08-31 on 671ae4f9a7 —
#
#   pytest tests/hermes_cli/test_env_export_prefix.py \
#          tests/agent_runtime/test_serve_socket_child_e2e.py -q
#   -> 2 failed, GatewayFenceViolation on a legitimate serve child
#
# — and 6 failed / 5 errors in the wave-close combined run. The claims below
# pin both halves of the fix: still refusing in here, transparent out there.


@pytest.mark.live_system_guard_bypass
def test_the_fence_is_armed_while_a_test_in_this_directory_runs():
    """The autouse arming fixture is what makes every L3 claim above true."""
    assert _gateway_fence.is_installed()
    assert _gateway_fence.is_armed()


@pytest.mark.live_system_guard_bypass
def test_a_disarmed_fence_passes_a_backend_argv_straight_through():
    """Out-of-directory spawns must reach production untouched.

    Disarming is what tests/agent_runtime gets for the whole of its run. The
    classifier still RECOGNISES the argv — that is deliberate, the fence has
    not forgotten what a backend looks like — but the wrapper must not act on
    it, or a real serve child never starts.

    Nothing is spawned here: the command is a hermes backend argv that would
    otherwise be refused, and it is only ever handed to the classifier and to
    a stand-in that records instead of executing.
    """
    argv = [sys.executable, "-m", "hermes_cli.main", "harness", "serve", "--ndjson"]
    assert _gateway_fence.classify(argv) is not None, (
        "the classifier should still recognise a backend argv while disarmed"
    )

    recorded: list[object] = []
    real_popen = subprocess.Popen
    try:
        _gateway_fence.disarm()
        assert not _gateway_fence.is_armed()
        # Stand in for the OS call so the pass-through is observable without
        # starting anything: if the fence still refused, this never runs.
        subprocess.Popen = lambda cmd, *a, **k: recorded.append(cmd)
        real_popen.__init__  # noqa: B018 — keep the reference obviously live
        subprocess.Popen(argv)
    finally:
        subprocess.Popen = real_popen
        _gateway_fence.arm()

    assert recorded == [argv], "a disarmed fence refused a spawn it should pass"
    assert _gateway_fence.is_armed(), "the fixture's arming must be restored"


@pytest.mark.live_system_guard_bypass
def test_disarming_cannot_lift_the_session_finish_latch():
    """Once latched, ``disarm()`` is a no-op — the atexit window stays covered.

    ``pytest_sessionfinish`` latches the refusal on for whatever the process
    does next. Nothing runs after that point that could legitimately want it
    off, and a stray ``disarm()`` from a lingering fixture teardown must not be
    able to reopen the window the escape used. Proven directly rather than
    trusted, then wound back so the rest of the run is unaffected.
    """
    from tests.hermes_cli import _gateway_fence as fence

    latched_before = fence._LATCHED
    try:
        fence.arm_permanently()
        fence.disarm()
        assert fence.is_armed()
    finally:
        fence._LATCHED = latched_before
        fence.arm()
