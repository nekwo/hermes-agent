import threading
import time

import pytest

from agent_runtime import repo_context as _repo_context
from agent_runtime.config import harness_root_config_path
from hermes_constants import get_hermes_head_home


_PRODUCTION_WORKTREE_BASE_DIR = _repo_context._worktree_base_dir
_PRODUCTION_LEGACY_WORKTREE_BASE_DIR = (
    _repo_context.legacy_harness_worktree_base_dir
)


# The teardown tripwire that used to live here — a per-test sentinel minted
# through the shared ``monkeypatch`` and asserted still-set after the body, so
# an ``undo()`` reached through an alias/callback/getattr reddened the exact
# test — was HOISTED to the root ``tests/conftest.py`` on 2026-08-18
# (ML-14 / C21) as ``_shared_monkeypatch_pin_tripwire``. It is not gone and it
# is not weaker: it watches the same event through the same mechanism, and it
# now covers every test in the tree rather than this package alone, because the
# pins it protects (hermetic HERMES_HOME + credential blanking, the kanban
# write guard, the live-system guard, the audio guard) are the root conftest's
# and were always tree-wide. Keeping a second copy here would produce two
# teardown errors for one defect, not two facts.


@pytest.fixture
def persisted_persona_samples():
    """Install explicit persona DATA for tests that exercise deployed chat surfaces."""
    from agent_runtime.store import AgentStore
    from tests.agent_runtime.persona_samples import sample_personas

    store = AgentStore()
    for persona in sample_personas():
        store.save(persona)
    return store.list_all()


@pytest.fixture
def bounded_chat_session():
    """Pin one chat session to the pre-2026-08-09 BOUNDED tier.

    The runtime default is ``unbounded`` (operator ruling 2026-08-09 — see
    ``docs/agent-runtime-harness/UNBOUNDED_DEFAULT_PLAN_2026-08-09.md``), so a
    test whose SUBJECT is the bounded tier — the chat-lane cost policy, the
    persona-safety block set, the envelope grants table — has to say so instead
    of leaning on a default that no longer means what it used to. Leaning on it
    would not just fail; it would silently re-pin the old posture the moment
    someone "fixed" the assertion.

    This writes the real store record through the real store, so the test still
    resolves through the ONE chokepoint (``permission_options_for_chat``) rather
    than monkeypatching a mode into place.
    """

    from agent_runtime.permission_modes import PERMISSION_MODE_BOUNDED
    from agent_runtime.tool_permissions import ChatToolPermissionStore

    def _pin(persona_id: str, session_id: str = "bounded-session") -> str:
        ChatToolPermissionStore().set(
            persona_id=str(persona_id),
            session_id=session_id,
            mode=PERMISSION_MODE_BOUNDED,
            reason="test pins the historical bounded tier",
        )
        return session_id

    return _pin


@pytest.fixture
def bundled_persona_profiles():
    """Provision the explicit profile homes used by legacy runtime test data."""

    from hermes_cli.profiles import get_profile_dir

    homes = []
    for profile in ("gpt-launcher", "backend-dev", "qa"):
        home = get_profile_dir(profile)
        home.mkdir(parents=True, exist_ok=True)
        homes.append(home)
    return homes


@pytest.fixture(autouse=True)
def reset_profile_runner_runtime_resolve_cache():
    """Keep the T6 runtime-resolve memo from crossing test boundaries.

    The memo keys on (profile home, provider, model, config stamps) — none of
    which distinguish one test's monkeypatched ``resolve_runtime_provider`` from
    the next test's. Without this, a suite that patches two different resolvers
    for the same provider/model silently reads the first one's answer in the
    second test. Process-global state gets a process-global reset.
    """

    from agent_runtime.profile_runner import reset_runtime_resolve_cache

    reset_runtime_resolve_cache()
    yield
    reset_runtime_resolve_cache()


@pytest.fixture(autouse=True)
def isolate_agent_runtime_root(tmp_path, monkeypatch):
    """Pin this package's runtime root into ``tmp_path``.

    =====================================================================
    THE PINS, AND WHAT PROVES THEY WERE STILL STANDING (EG-0.1)
    =====================================================================

    ``monkeypatch`` is ONE instance per test function, shared by every fixture
    and the test body. So ``monkeypatch.undo()`` called from inside a test body
    does not drop "the patch this test made" — it unwinds the WHOLE stack,
    including the three pins below. Three tests did exactly that on 2026-08-17:

    * ``test_office_state_patches.py:751`` — dropped the projection cap, and
      three lines later wrote the OPERATOR's live store. The leaked actor
      ``ws_office_patch_test`` reached revision 67, climbing once per suite run;
    * ``test_persona_chat_continuity.py:156`` — dropped an unlock stub, then
      took a chat-root lease against the live root (the lease file is the
      physical evidence);
    * ``test_mcp_admission_r2.py:327`` — dropped a deregister stub in a
      ``finally``.

    Nothing went red. ``assert_root_config_resolution_is_hermetic`` below is
    SETUP-ONLY by design, so a mid-body unpin is invisible to it, and the tests
    themselves only assert about their own subject. All three now use a scoped
    ``pytest.MonkeyPatch.context()``.

    The fence that keeps a fourth site from being silent is NOT here: it is the
    root conftest's ``_shared_monkeypatch_pin_tripwire``, which watches a
    per-test sentinel on the same shared instance and therefore reddens an
    unwind of THESE pins too (see the note at the top of this file for the
    2026-08-18 hoist, and ``test_no_midtest_monkeypatch_undo.py`` for the
    second, structural witness).
    """

    root = tmp_path / "agent-runtime"
    monkeypatch.setenv("HERMES_AGENT_RUNTIME_ROOT", str(root))
    # Structural janitor boundary: no agent-runtime test may discover the
    # machine-global fallback or a live profile's worktrees by accident. Tests
    # that exercise explicit bases may still layer their narrower pins on top.
    monkeypatch.setattr(
        _repo_context, "_worktree_base_dir", lambda: tmp_path / "worktrees"
    )
    monkeypatch.setattr(
        _repo_context,
        "legacy_harness_worktree_base_dir",
        lambda: tmp_path / "legacy-worktrees",
    )
    yield root


#: Producer threads the hub names per generation (``serve_stream_hub.subscribe``).
_STREAM_PRODUCER_THREAD_PREFIX = "serve-stream-producer-"

#: How long the tripwire below will wait for a producer to finish winding down.
#: Bounded, and spent INSIDE the still-pinned sandbox, which is the whole reason
#: it is not a weakening: see the fixture's docstring.
_STREAM_PRODUCER_GRACE_SECONDS = 2.0
_STREAM_PRODUCER_POLL_SECONDS = 0.02


def _live_stream_producer_names() -> set:
    return {
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(_STREAM_PRODUCER_THREAD_PREFIX) and thread.is_alive()
    }


@pytest.fixture(autouse=True)
def no_serve_stream_producer_outlives_the_test(isolate_agent_runtime_root):
    """Fail LOUDLY when a ``serve-stream-producer-*`` thread survives a test body.

    =====================================================================
    THE PRODUCER TRIPWIRE (EG-4.1 / TC-1, the 2026-08-17 leak by thread)
    =====================================================================

    Same leak class as EG-0.1 above, reached by a route that tripwire cannot
    see: it watches the PINS, and this one walks out on a THREAD.

    ``StreamHub.stop()`` cannot interrupt a generator parked inside ``next()``
    (its own module docstring says so, and CPython refuses ``close()`` on an
    executing generator anyway). The real producer —
    ``hermes_cli/harness_parts/serve.py::_stream_source`` over
    ``stream_frames`` — polls its event tail every 250ms and only YIELDS on a
    frame, so on a quiet lane it surfaces once per 5s heartbeat. A test that
    attaches the REAL producer through ``serve_loop`` could therefore return
    while a daemon producer thread was still parked; the fixture above then
    unwound ``HERMES_AGENT_RUNTIME_ROOT``, and that producer's next EventLog
    read resolved against the OPERATOR's live store. Observed while building
    EG-4.1 as a ``producer_error:JSONDecodeError`` — the producer reading a
    foreign event log mid-line — and recorded as debt in
    ``docs/agent-runtime-harness/SINGLE_TRANSPORT_COLLAPSE_PLAN_2026-08-16.md``
    (TC-1, "Debt found, not fixed"). Read-only today, by luck rather than by
    design: nothing about the route made it a read.

    ``test_serve_stream_lane_parity.py`` drains its own producers and asserts the
    drain (``_drain_stream_producers``). That is one file being careful. This is
    the fence for the next author, who will not know to.

    WHY IT SHOULD NEVER FIRE. ``_stream_source`` now binds the hub's
    per-generation stop event to ``request_control``'s cancellation seam, so an
    abandoned generation returns at its own next safe point (~100ms) instead of
    at its next frame (~5s) — measured, ``hub.stop()`` comes back with zero
    producers alive. This fixture is what turns a regression in that into a red
    test instead of another silent write to the live root.

    WHY THE GRACE PERIOD IS NOT A WEAKENING. The claim being enforced is not
    "the thread was gone by the body's last line" — it is "the thread was gone
    before the sandbox was unpinned", because the unpinning is what makes a
    surviving read dangerous. This fixture DECLARES ``isolate_agent_runtime_root``,
    so it is set up after it and finalized before it: every millisecond of the
    wait below is spent with the root pin, both worktree-base pins and the
    ``HERMES_HOME`` pin still standing. A producer that exits inside the grace
    never had an unpinned moment to read in. One that does not exit inside it
    would have leaked, and is named.

    Only threads that appeared DURING this test are reported. A producer already
    running at setup belongs to whatever started it — reporting it here would
    turn one leak into a red mark on every test that ran afterwards, which is how
    a fence gets deleted for noise.
    """

    before = _live_stream_producer_names()

    yield

    leaked = _live_stream_producer_names() - before
    if not leaked:
        return
    deadline = time.monotonic() + _STREAM_PRODUCER_GRACE_SECONDS
    while leaked and time.monotonic() < deadline:
        time.sleep(_STREAM_PRODUCER_POLL_SECONDS)
        leaked = _live_stream_producer_names() - before
    assert not leaked, (
        "A SERVE STREAM PRODUCER THREAD OUTLIVED THIS TEST: "
        f"{sorted(leaked)} still alive {_STREAM_PRODUCER_GRACE_SECONDS}s after "
        "the body finished. This is the 2026-08-17 live-store leak by THREAD "
        "(EG-4.1 / TC-1), the sibling of the teardown tripwire above: in a "
        "moment this fixture returns, `isolate_agent_runtime_root` unwinds "
        "`HERMES_AGENT_RUNTIME_ROOT`, and that producer's next EventLog read "
        "resolves against the OPERATOR's live runtime root in X:/Eternia/.hermes "
        "— which is how a `producer_error:JSONDecodeError` from a foreign event "
        "log was seen in the first place. `StreamHub.stop()` cannot interrupt a "
        "generator parked in `next()`, so stopping the hub is not by itself "
        "enough.\n\n"
        "FIX, in order of preference:\n"
        "  1. Make the SOURCE stop-aware. `serve.py`'s `_stream_source` binds "
        "the hub's per-generation stop event to `request_control`'s cancellation "
        "seam so the tail loop abandons within ~100ms; if you injected your own "
        "`stream_source_factory`, take the stop event and honour it the same way "
        "(`test_serve_rpc_office_subscribe_live_hub.py::live_hub` is the shape).\n"
        "  2. Drain before returning: append one event to wake the parked "
        "generator, then wait for the thread to exit — "
        "`test_serve_stream_lane_parity.py::_drain_stream_producers`.\n"
        "  3. Shorten the cadences. A producer built at "
        "`heartbeat_interval_seconds=5.0` (the production default) is parked for "
        "up to five seconds between frames and nothing outside it can say "
        "otherwise."
    )


def assert_under(path, base, *, what: str) -> None:
    """Fail unless ``path`` resolves INSIDE ``base``.

    Shared by the guard fixture below and ``test_root_config_hermetic.py`` so
    the guard and the test that proves the guard cannot drift apart.
    """

    resolved = path.resolve()
    root = base.resolve()
    assert root == resolved or root in resolved.parents, (
        f"{what} resolved to {resolved}, which is OUTSIDE the per-test tmp dir "
        f"{root}. Root-config resolution has escaped the sandbox: this test is "
        "reading the OPERATOR's live runtime root, so what it exercises depends "
        "on a file nobody in this repo controls."
    )


@pytest.fixture(autouse=True)
def assert_root_config_resolution_is_hermetic(tmp_path, _hermetic_environment):
    """Prove — before every agent-runtime test body — that ROOT-scope config
    resolution lands inside the per-test tmp dir.

    WHY AN ASSERTION AND NOT A THIRD PIN. The ``HERMES_HOME`` pin already
    exists and already works: ``tests/conftest.py::_hermetic_environment``
    (autouse, repo-wide) redirects it to ``tmp_path/hermes_test``, and
    ``config.harness_root_config_path()`` reads ``HERMES_HOME`` through
    ``hermes_constants.get_default_hermes_root()``, so the ROOT config a test
    sees is already synthetic. Re-pinning ``HERMES_HOME`` HERE would create a
    SECOND authority for the same variable pointing at a DIFFERENT directory
    than the one the root fixture pre-populates (``sessions/``, ``cron/``,
    ``memories/``, ``skills/``), and the two would drift the first time either
    moved. What was actually missing is not a pin — it is any statement,
    anywhere, that the pin is load-bearing for this package.

    That matters here specifically. ``ROOT_ONLY_CONFIG_KEYS``
    (``agent_runtime/config.py:361``) names four readers —
    ``state_patches.delta_patches_enabled``, ``mcp_admission.admission_config``,
    ``config.chat_lane_restore_toolsets``, ``config.mission_chat_workdir`` —
    that resolve ONLY through the root config. If the root pin ever regresses,
    those four silently start answering out of the operator's live
    ``config.yaml``, every one of them gates real behavior, and NOTHING in the
    suite goes red: the tests keep passing, against a different runtime. That
    is the failure mode this fixture converts into a loud one.

    ``HERMES_HEAD_HOME`` is asserted alongside it because it is the same hole
    one variable over, and unlike ``HERMES_HOME`` it was genuinely unpinned
    until this change (see ``tests/conftest.py::_HERMES_BEHAVIORAL_VARS``). It
    is not merely read: ``get_hermes_head_home()`` selects the SessionDB the
    Mission Control transcript store WRITES to, and the Launcher's serve does
    export it, so a suite run from a Launcher-shaped shell wrote test rows into
    the operator's live ``profiles/base/state.db``.

    Declares ``_hermetic_environment`` explicitly rather than trusting autouse
    ordering: this fixture asserts a property that fixture establishes, so the
    dependency is real and stating it removes any question of which runs first.

    SETUP ONLY, deliberately. Tests that must read the live tree opt out of the
    sandbox INSIDE their own body — ``test_launcher_qa_template_drift.py``
    restores the pre-sandbox ``HERMES_HOME`` on purpose to check the real
    profile tree — and a teardown assertion would punish exactly that legitimate
    pattern for doing the honest thing.
    """

    assert_under(
        harness_root_config_path(),
        tmp_path,
        what="config.harness_root_config_path()",
    )
    assert_under(
        get_hermes_head_home(),
        tmp_path,
        what="hermes_constants.get_hermes_head_home()",
    )
    yield


@pytest.fixture
def production_worktree_base_functions():
    """Explicit opt-in for tests that characterize production base selection."""

    return _PRODUCTION_WORKTREE_BASE_DIR, _PRODUCTION_LEGACY_WORKTREE_BASE_DIR


# S7-B RULING-0 COMPAT STRIP (2026-07-16): the ``history_in_frame_config`` and
# ``inline_prompt_payloads_config`` kill-switch fixtures were removed with the
# flags they flipped. The evicted/hoisted read-model shape is the only shape;
# tests that used to assert the legacy full-in-frame / inline content now
# re-target the on-disk archive artifacts + the paged history / on-demand fetch
# paths (see test_snapshot_history_eviction.py, test_snapshot.py,
# test_persona_assignments.py, test_stage52_role_envelopes.py).


def release_to_implementation(task, *, owner_slots=("dev", "backend_dev")):
    """Retarget an old dev-mechanics fixture after removal of the stage graph."""

    task.current_stage_id = "implement"
    return task
