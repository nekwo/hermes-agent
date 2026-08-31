"""``runtime.persona.prewarm`` — pay the per-type cost BEFORE the drop (Stage 3a).

The plan's Stage 0 convicted C2 off the operator's drop log: the first
``runtime.agent.create`` of a persona type this serve process had not resolved
recently cost ``instance_ms`` 1,906 / 3,203 / 2,858 ms, against 141 / 125 ms for
a second drop seconds later. This suite does not take that on faith — the first
test here reproduces the SHAPE in this process (a create that must probe, then
one that need not), so every later assertion is anchored to a cost that is real
on this machine rather than to a number quoted from a markdown file. It anchors
the SHAPE, not the magnitude — no test here asserts a millisecond.

What the gate has to prove, and what would make it vacuous
----------------------------------------------------------
"The create got faster" is not assertable in a unit test: a wall-clock budget on
a shared CI box is a flake generator, and the plan explicitly forbids wall-clock
deltas. So the gate is the MECHANISM, counted: ``tools.registry``'s thread-local
probe-round counter, which counts one round per availability pass in which a
``check_fn`` was genuinely executed. Zero rounds across a create means every
memo the create's wire-row projection reaches for was already filled.

A zero on its own would still be vacuous — it is also what a create pays when
some earlier test in the same session happened to warm the shared caches. So the
gate is a PAIR, and the negative arm is what makes the positive one mean
something: evict ONE ``check_fn`` cache entry by key and the very same create
pays a nonzero delta again.

By key. Never ``invalidate_check_fn_cache()``. That function is real API with a
real caller (``hermes tools enable``) and it drops the TTL cache the WHOLE
process shares — a row that reached for it handed every later test in the run a
cold re-probe of docker, playwright and sockets, and cost a lane two downstream
failures on 2026-08-21 (commit ``c7dad2f3cd``). ``check_todo_requirements`` is
the key evicted below precisely because re-probing it costs a ``return True``.
"""

from __future__ import annotations

import time

import pytest

from agent_runtime import persona_prewarm, serve_rpc
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_persona_prewarm_test"


# ── helpers ──────────────────────────────────────────────────────────────────


def _prewarm(params: dict, rid: str = "p1") -> dict:
    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.persona.prewarm",
            "params": params,
        }
    )


def _create(persona_id: str, placement_id: str, rid: str = "c1") -> dict:
    return serve_rpc.handle_request(
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.agent.create",
            "params": {
                "persona_id": persona_id,
                "workspace_id": WORKSPACE,
                "position": [1.5, -2.5],
                "idempotency_key": f"gesture-{placement_id}",
                "placement_id": placement_id,
            },
        }
    )


def _drained(timeout: float = 20.0) -> None:
    """Block until the worker has finished every queued warm.

    Polls the module's OWN in-flight set rather than any queue internal: the
    worker discards an id from ``_pending`` after the warm returns and before it
    marks the queue task done, so an empty set is the honest "nothing is warming
    right now". Raises rather than looping into pytest-timeout, so a wedged
    worker names itself instead of arriving as a bare 30 s cap.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with persona_prewarm._lock:
            if not persona_prewarm._pending:
                return
        time.sleep(0.01)
    raise AssertionError(
        "the prewarm worker never drained: still warming "
        f"{sorted(persona_prewarm._pending)}"
    )


@pytest.fixture
def seeded_workspace():
    from agent_runtime.office_store import OfficeStore

    store = OfficeStore()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


@pytest.fixture
def persona_factory():
    """Mint roster personas with distinct ids.

    Distinct because "a persona type this process has never created" is the
    whole subject: two tests sharing one id would let the first one's memos
    answer the second one's gate.
    """

    from agent_runtime.models import AgentPersona
    from agent_runtime.store import AgentStore

    def _make(persona_id: str) -> AgentPersona:
        persona = AgentPersona(
            id=persona_id,
            display_name=f"{persona_id.upper()} Agent",
            role="qa",
            model=None,
            provider=None,
            api_mode=None,
            toolsets=[],
            system_prompt_path="",
        )
        AgentStore().save(persona)
        return persona

    return _make


@pytest.fixture
def evict_one_check_fn():
    """Evict ONE ``check_fn`` TTL entry, by key, and restore nothing.

    Restoring nothing is correct: the pass under test re-probes the key and
    re-caches it itself, so the process leaves this fixture in the state it
    entered. ``check_todo_requirements`` is chosen because its body is
    ``return True`` — the eviction costs the suite nothing while still forcing a
    genuine probe round through ``_check_fn_cached``.
    """

    from tools import registry as registry_module
    from tools.todo_tool import check_todo_requirements

    def _evict() -> None:
        registry_module._check_fn_cache.pop(check_todo_requirements, None)
        registry_module._check_fn_last_good.pop(check_todo_requirements, None)

    return _evict


def _probe_rounds() -> int:
    from tools.registry import probe_rounds_this_thread

    return probe_rounds_this_thread()


# ── the cost this stage exists to remove ─────────────────────────────────────


def test_a_cold_create_pays_probe_rounds_and_the_next_one_pays_none(
    seeded_workspace, persona_factory, evict_one_check_fn
):
    """The C2 mechanism, measured in this process rather than quoted from the
    plan: the create path really does reach the registry's TTL cache, and the
    entry the first create fills is the entry the second one rides.

    It holes the cache first rather than trusting the session to be cold. Left
    to chance this test would pass or fail on whether some earlier FILE in the
    run had already resolved tool visibility inside the 30 s TTL — an
    order-dependence that would eventually red on a machine nobody could
    reproduce.
    """

    persona_factory("prewarm_cost_a")
    persona_factory("prewarm_cost_b")

    evict_one_check_fn()
    before = _probe_rounds()
    _create("prewarm_cost_a", "prewarm_cost_a_1_agent_2")
    cold_rounds = _probe_rounds() - before

    before = _probe_rounds()
    _create("prewarm_cost_b", "prewarm_cost_b_1_agent_2")
    warm_rounds = _probe_rounds() - before

    assert cold_rounds > 0, (
        "a create against a holed check_fn cache resolved tool visibility "
        "without probing anything — the cost this stage removes is not on the "
        "create path at all"
    )
    assert warm_rounds == 0, (
        "a second create moments later re-probed; the memos are not shared "
        f"across creates the way C2 says they are ({warm_rounds} rounds)"
    )


# ── the gate ─────────────────────────────────────────────────────────────────


def test_after_a_prewarm_the_create_pays_no_probe_rounds(
    seeded_workspace, persona_factory, evict_one_check_fn
):
    """THE GATE. A create of a never-created persona type, with the shared
    ``check_fn`` cache deliberately holed first, costs zero probe rounds when a
    prewarm ran in between.

    The eviction is what stops this from being a tautology: without it the entry
    is already warm from whatever ran earlier in the session and the assertion
    would hold with the prewarm deleted. Its twin below runs the identical
    sequence with the prewarm omitted and demands a NONZERO delta.
    """

    persona_factory("prewarm_gate_warm")

    evict_one_check_fn()
    reply = _prewarm({"persona_id": "prewarm_gate_warm"})
    assert reply["result"]["accepted"] is True
    _drained()

    before = _probe_rounds()
    created = _create("prewarm_gate_warm", "prewarm_gate_warm_1_agent_2")
    delta = _probe_rounds() - before

    assert "error" not in created, created
    assert delta == 0, (
        "the create still ran probe rounds after a prewarm — the warm filled "
        f"keys the create does not read ({delta} rounds)"
    )


def test_without_the_prewarm_the_same_create_pays_the_rounds_again(
    seeded_workspace, persona_factory, evict_one_check_fn
):
    """The gate's negative arm — the reason its zero means anything.

    Identical to the test above with exactly one line removed: the prewarm.
    """

    persona_factory("prewarm_gate_cold")

    evict_one_check_fn()

    before = _probe_rounds()
    created = _create("prewarm_gate_cold", "prewarm_gate_cold_1_agent_2")
    delta = _probe_rounds() - before

    assert "error" not in created, created
    assert delta > 0, (
        "a create against a holed check_fn cache probed nothing, so the gate "
        "above proves nothing about the prewarm"
    )


def test_the_prewarm_fills_the_PERSONA_keyed_readiness_memo_the_create_reads(
    seeded_workspace, persona_factory
):
    """A zero probe-round delta only covers the registry's TTL cache, and that
    cache is keyed on the ``check_fn`` — shared by every persona in the process.
    It would read zero even if the warm had resolved some OTHER persona.

    ``_cached_tool_names_for_toolsets`` cannot close that gap either, and the
    reason is worth recording: under the runtime's UNBOUNDED default
    ``_enabled_toolsets_for_chat`` resolves ``all_registered_toolsets()``
    (``persona_runtime.py:669``), so its lru key is persona-INDEPENDENT and one
    persona's entry answers every other's. An assertion on its miss count looks
    persona-specific and is not.

    ``_cached_profile_readiness_for_visibility``'s memo IS keyed per persona
    (id + profile + skills + servers + provider + model + api_mode), so it is the
    honest witness for "the warm primed THIS type". Two things are asserted, and
    the second is the load-bearing one: the create finds the SAME key (not a
    neighbouring one the warm's ``session_id=None`` happened to mint) and does
    not recompute it — the entry's stamp is untouched across the create.
    """

    from agent_runtime import tool_visibility

    persona_factory("prewarm_memo_key")

    _prewarm({"persona_id": "prewarm_memo_key"})
    _drained()

    memo = tool_visibility._profile_readiness_memo
    keys = [key for key in memo if key[0] == "prewarm_memo_key"]
    assert len(keys) == 1, (
        f"the warm left {len(keys)} readiness entries for this persona; it was "
        "supposed to leave exactly the one the create reads"
    )
    stamped_at = memo[keys[0]]["at"]

    _create("prewarm_memo_key", "prewarm_memo_key_1_agent_2")

    assert [key for key in memo if key[0] == "prewarm_memo_key"] == keys, (
        "the create resolved readiness under a DIFFERENT key than the warm "
        "filled — the warm primed a neighbour"
    )
    assert memo[keys[0]]["at"] == stamped_at, (
        "the create recomputed profile readiness despite the warm's entry "
        "being present — it did not read this memo"
    )


# ── the ack contract the launcher trigger is wired from ──────────────────────


def test_the_method_is_advertised_and_the_contract_integer_does_not_move():
    # Additive: a client discovers the method in the manifest set. The integer
    # moves only when an EXISTING method's shape changes.
    assert "runtime.persona.prewarm" in serve_rpc.manifest()["methods"]
    assert serve_rpc.manifest()["contract"] == 1


def test_the_ack_names_the_persona_and_that_a_warm_started(persona_factory):
    persona_factory("prewarm_ack")

    reply = _prewarm({"persona_id": "prewarm_ack", "correlation_id": " drop-7 "})

    assert reply["result"] == {
        "persona_id": "prewarm_ack",
        "accepted": True,
        "state": "started",
        "correlation_id": "drop-7",
    }
    _drained()


def test_a_repeat_call_starts_nothing_and_says_so(persona_factory):
    """Idempotence the caller can SEE, not one it has to trust.

    The launcher's trigger is "call this for every chip on every palette open",
    so a second call for a persona already warming must add no queue entry. The
    warm is held open with a gate so the second call provably lands while the
    first is still in flight — sleeping and hoping would test the scheduler.
    """

    persona_factory("prewarm_repeat")

    import threading

    release = threading.Event()
    real_warm = persona_prewarm.warm_persona_memos

    def _blocking_warm(persona):
        release.wait(timeout=15.0)
        return real_warm(persona)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(persona_prewarm, "warm_persona_memos", _blocking_warm)
        first = _prewarm({"persona_id": "prewarm_repeat"}, rid="p1")
        second = _prewarm({"persona_id": "prewarm_repeat"}, rid="p2")
        release.set()
        _drained()

    assert first["result"]["state"] == "started"
    assert second["result"]["state"] == "already_running"
    assert second["result"]["accepted"] is True


# ── refusals ─────────────────────────────────────────────────────────────────


def test_a_missing_persona_id_is_refused_and_nothing_is_queued():
    before = _probe_rounds()
    reply = _prewarm({})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == "persona_id_required"
    assert _probe_rounds() == before
    with persona_prewarm._lock:
        assert not persona_prewarm._pending


def test_an_unknown_persona_is_refused_with_the_CREATE_s_own_reason():
    """One id, one verdict. A launcher that prewarms an id and then creates it
    must never be told two different stories about that id, so the reason and
    the code come out of ``agent_create``'s spellings rather than a second copy
    minted here."""

    from agent_runtime.agent_create import PERSONA_NOT_FOUND_REASON

    before = _probe_rounds()
    reply = _prewarm({"persona_id": "no_such_persona_at_all"})

    assert reply["error"]["code"] == -32602
    assert reply["error"]["data"]["reason"] == PERSONA_NOT_FOUND_REASON
    assert reply["error"]["data"]["persona_id"] == "no_such_persona_at_all"
    assert "no_such_persona_at_all" in reply["error"]["message"]
    assert _probe_rounds() == before, "a refused id must not probe anything"


def test_a_profile_id_is_refused_with_its_own_reason_not_persona_not_found():
    """``runtime.agent.create`` ACCEPTS ``profile:`` ids (decision D-U1) and then
    resolves the wire row's visibility persona from the freshly minted INSTANCE.
    No instance exists at prewarm time, so the memo keys that resolution will hit
    are unknowable here.

    Refused with a reason of its own rather than ``persona_not_found``, because
    the two need opposite responses: one says "send a different id", this one
    says "this id is fine, there is just nothing to warm for it".
    """

    reply = _prewarm({"persona_id": "profile:gpt-launcher"})

    assert reply["error"]["code"] == -32602
    assert (
        reply["error"]["data"]["reason"] == "profile_persona_not_prewarmable"
    )
    assert reply["error"]["data"]["persona_id"] == "profile:gpt-launcher"


# ── the "fills memos and NOTHING else" property ──────────────────────────────


def test_the_prewarm_writes_no_store_state_and_emits_no_event(
    seeded_workspace, persona_factory
):
    """The constraint that lets this be fire-and-forget in the first place.

    A prewarm that could half-write anything would need a completion channel and
    a compensation path; this one needs neither, and the witness is the store and
    the event log read on both sides of the warm.
    """

    from agent_runtime.events import EventLog
    from agent_runtime.office_store import OfficeStore
    from agent_runtime.persona_assignments import PersonaInstanceStore

    persona_factory("prewarm_no_writes")

    events_before = len(EventLog().tail(400))
    instances_before = {i.id for i in PersonaInstanceStore().list_all()}
    actors_before = {a.actor_key for a in OfficeStore().scan_actors(WORKSPACE).actors}

    _prewarm({"persona_id": "prewarm_no_writes"})
    _drained()

    assert len(EventLog().tail(400)) == events_before
    assert {i.id for i in PersonaInstanceStore().list_all()} == instances_before
    assert {
        a.actor_key for a in OfficeStore().scan_actors(WORKSPACE).actors
    } == actors_before


def test_a_failure_inside_the_warm_never_reaches_the_caller(persona_factory):
    """The worker's swallow-and-log, asserted through the worker rather than by
    calling the swallow directly — and followed by a SECOND warm that must still
    run, because a raise that killed the worker thread would turn every later
    prewarm into a silent no-op with no symptom but the slow create it was meant
    to prevent."""

    persona_factory("prewarm_boom")
    persona_factory("prewarm_after_boom")

    ran: list[str] = []
    real_warm = persona_prewarm.warm_persona_memos

    def _exploding_warm(persona):
        if persona.id == "prewarm_boom":
            raise RuntimeError("readiness probe blew up")
        ran.append(persona.id)
        return real_warm(persona)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(persona_prewarm, "warm_persona_memos", _exploding_warm)
        boom = _prewarm({"persona_id": "prewarm_boom"}, rid="p1")
        _drained()
        after = _prewarm({"persona_id": "prewarm_after_boom"}, rid="p2")
        _drained()

    assert boom["result"]["accepted"] is True, "the raise reached the caller"
    assert after["result"]["accepted"] is True
    assert ran == ["prewarm_after_boom"], (
        "the worker did not survive the failed warm"
    )


# -- the pacing receipt (W2-H3) ----------------------------------------------


def test_a_finished_warm_logs_a_done_line_with_its_elapsed_cost(
    persona_factory, caplog
):
    """The receipt that makes this module's central claim falsifiable.

    The claim is "the memos are filled before the drop arrives", which is a race
    against the operator's gesture. With a start and no finish, the only way to
    ask "did the warm win, and by how much?" was to read the create's own cost --
    the number the warm exists to change. A start with no finish measures nothing.

    Format-pinned deliberately: an operator joins this line to the drop log by
    grepping it, so the token order is a contract, not an implementation detail.
    TIMINGS ONLY -- the assertion below also pins what must NOT be on the line.
    """

    import logging
    import re

    persona = persona_factory("prewarm_receipt")

    with caplog.at_level(logging.INFO, logger=persona_prewarm.__name__):
        _prewarm({"persona_id": "prewarm_receipt"})
        _drained()

    done = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("persona_prewarm done ")
    ]
    assert len(done) == 1, f"expected exactly one done receipt, got {done}"
    match = re.fullmatch(
        r"persona_prewarm done persona=(\S+) elapsed_ms=(\d+)", done[0]
    )
    assert match is not None, f"receipt format moved: {done[0]!r}"
    assert match.group(1) == "prewarm_receipt"
    assert int(match.group(2)) >= 0
    # Timings only. The display name is the nearest thing to a body this warm
    # ever holds, and it must not be on the wire to the log.
    assert persona.display_name not in done[0]


def test_a_failed_warm_still_reports_how_long_it_occupied_the_worker(
    persona_factory, caplog
):
    """A pacing census that could only see successes would under-count.

    There is ONE worker. A warm that failed after seconds of probing held it for
    exactly those seconds, and the queue behind it waited exactly that long. The
    failure path already warned; it now carries the elapsed cost too, and emits
    NO done line -- a receipt for work that did not finish would be a lie the
    census could not detect.
    """

    import logging
    import re

    persona_factory("prewarm_receipt_boom")

    def _exploding_warm(persona):
        raise RuntimeError("readiness probe blew up")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(persona_prewarm, "warm_persona_memos", _exploding_warm)
        with caplog.at_level(logging.INFO, logger=persona_prewarm.__name__):
            _prewarm({"persona_id": "prewarm_receipt_boom"})
            _drained()

    messages = [record.getMessage() for record in caplog.records]
    assert not [m for m in messages if m.startswith("persona_prewarm done ")], (
        "a warm that raised reported itself done"
    )
    failed = [m for m in messages if "persona prewarm failed" in m]
    assert len(failed) == 1, messages
    assert re.search(r"after \d+ ms", failed[0]), (
        f"the failure path lost its elapsed cost: {failed[0]!r}"
    )


def test_the_warm_fills_the_exact_toolset_key_the_create_reads(
    persona_factory, tmp_path, monkeypatch
):
    """The scope application inside ``warm_persona_memos`` is key ALIGNMENT.

    Measured 2026-08-22: the module's original claim ("warming without it
    primes nothing the create reads") was false in every dimension that was
    checked — the expensive inputs are process/callable-keyed and warm either
    way, and a per-session BOUNDED record can never reach this pair
    (``ChatToolPermissionStore.get`` answers ``None`` for the warm's
    ``session_id=None`` and for a create's fresh session alike). Under the
    UNBOUNDED runtime default even the ``(toolsets, blocked)`` name-cache key
    coincides, so no gate can hold the claim there — this test measured that
    too (a deleted scope call stayed green under the default posture).

    The configuration where the call IS load-bearing is an install whose
    RUNTIME DEFAULT is the bounded posture (root ``config.yaml``
    ``default_mode: profile_default`` — install-level, so it governs both the
    warm and the create, unlike session records). There the chat-lane cost cut
    makes the scoped key genuinely different from the unscoped neighbour, and
    a warm without the scope application fills the wrong one. This gate pins
    exactly that: under a bounded runtime default, a create-shaped resolve
    after a warm takes CACHE HITS ONLY on ``_cached_tool_names_for_toolsets``.
    Remove ``apply_chat_lane_tool_scope`` from the warm and this reds.
    """

    import textwrap

    from agent_runtime import tool_visibility
    from agent_runtime.parse_cache import clear_parse_cache
    from agent_runtime.persona_runtime import apply_chat_lane_tool_scope
    from agent_runtime.tool_permissions import permission_options_for_chat

    root = tmp_path / "hermes-root"
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        textwrap.dedent(
            """
            agent_runtime:
              tool_permissions:
                default_mode: profile_default
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    clear_parse_cache()

    persona = persona_factory("prewarm_keyalign")
    persona_prewarm.warm_persona_memos(persona)

    # The create's shape: a freshly minted session id, which has no permission
    # record — the same runtime-default posture the warm's ``None`` resolves.
    fresh_session = "persona_chat_personainst_prewarm_keyalign_fresh01"
    options = permission_options_for_chat(persona, session_id=fresh_session)
    apply_chat_lane_tool_scope(persona, options, session_id=fresh_session)

    before = tool_visibility._cached_tool_names_for_toolsets.cache_info()
    tool_visibility.resolve_tool_visibility(persona, options)
    after = tool_visibility._cached_tool_names_for_toolsets.cache_info()

    assert after.misses == before.misses, (
        "under the bounded runtime default, the create-shaped resolve MISSED "
        f"the toolset-name key after a warm: misses {before.misses} -> "
        f"{after.misses} — the warm filled the unscoped neighbour instead of "
        "the key the create reads"
    )
    assert after.hits > before.hits, (
        "the create-shaped resolve never consulted the toolset-name cache — "
        "this gate would be vacuous; find where the resolve reads names"
    )
