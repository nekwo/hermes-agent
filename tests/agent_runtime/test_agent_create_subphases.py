"""W3-H1 — the create's mint, split into named spans, and what the split convicts.

``perform_agent_create`` reported ONE number for the whole mint
(``phases.instance_ms``), and W3 opened with that number unattributable: the
prewarm's own receipts showed the whole persona catalog warmed at boot in ~1.2 s
(live, 2026-08-22 14:48Z: backend_dev 438 ms, base 109, dev 202,
neko_supervisor 250, qa 157) and the first drop of that session STILL paid
``rpc_instance_ms=2030``, against 78 ms for the second drop eleven seconds
later. The stage named a prime suspect — the 3a docstring's own caveat, that
``warm_persona_memos`` resolves with ``session_id=None`` while the create
resolves with the freshly minted session id, so the warm might be filling a
NEIGHBOURING memo key.

**This suite convicts, and the verdict is not the suspect.** Measured here in a
hermetic home, on this machine, with the ``check_fn`` cache deliberately holed:

* an UNWARMED create bills ``instance_ms`` 2,781 ms, of which
  ``chat_lane_scope_ms`` alone is **2,421** — ``apply_chat_lane_tool_scope``,
  not ``resolve_tool_visibility``, which measures **0** because the scope
  application has already filled every shared cache it would have reached;
* the SAME create after ``warm_persona_memos`` for the same persona bills
  ``instance_ms`` 281 ms with ``chat_lane_scope_ms`` at **15** and zero probe
  rounds. The warm fills exactly the key the create reads. The neighbouring-key
  suspicion is ACQUITTED at HEAD.

No test below asserts a millisecond, and none can reproduce those magnitudes:
2,421 ms is the registry populate plus the whole toolset sweep, which is
process-lifetime state a shared pytest session has already paid for and cannot
safely un-pay (see :func:`rounds_per_projection_read`). The gates are the
COUNTED mechanism instead — ``tools.registry``'s probe-round counter, attributed
per projection read.

**The sweep half of that verdict was RETIRED on 2026-09-02, and the numbers
above are historical from that date.** ``apply_chat_lane_tool_scope`` reached
the toolset sweep only through ``personas.all_registered_toolsets``, which asked
``get_available_toolsets()`` for a list of NAMES and paid one availability round
per toolset for an ``available`` boolean it discarded; it now asks
``get_registered_toolset_names()``, identical key set, no probe. A create bills
ZERO rounds to every one of the three reads, warmed or not, which is what
:func:`test_a_mint_bills_no_probe_rounds_to_any_projection_read` asserts in
place of the pair that measured the cost. The registry POPULATE — importing the
38 modules under ``tools/`` — is untouched and is still the larger half of the
2,421 ms; it is not counted by this instrument and never was. See
``docs/agent-runtime-harness/planned/serve-small-batch-field-notes-2026-09-02.md``
§2.
"""

from __future__ import annotations

import logging

import pytest

from agent_runtime import agent_create_phases, persona_prewarm
from agent_runtime.agent_create_phases import (
    NO_PHASES,
    PHASE_ORDER,
    CreateSubphases,
)
from tests.agent_runtime.office_seed import seed_workspace_record

WORKSPACE = "ws_create_subphase_test"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_workspace():
    from agent_runtime.office_store import OfficeStore

    store = OfficeStore()
    seed_workspace_record(WORKSPACE)
    store.ensure_surface(WORKSPACE, created_by="seed")
    return store


@pytest.fixture
def persona_factory():
    """Mint roster personas with distinct ids — one per test, never shared.

    "A persona type this process has not resolved" is the whole subject; two
    tests sharing an id would let the first one's memos answer the second's gate.
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

    The same fixture ``test_persona_prewarm`` uses and for the same reason:
    never ``invalidate_check_fn_cache()``, which drops the cache the whole
    process shares and hands every later test in the run a cold re-probe of
    docker, playwright and sockets. ``check_todo_requirements``'s body is
    ``return True``, so holing it costs the suite nothing while still forcing a
    genuine probe round.
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


def _create(persona_id: str, placement_id: str):
    """The REAL create path — the service function the RPC shim calls."""

    from agent_runtime.agent_create import perform_agent_create

    outcome = perform_agent_create(
        {
            "persona_id": persona_id,
            "workspace_id": WORKSPACE,
            "position": [1.5, -2.5],
            "idempotency_key": f"gesture-{placement_id}",
            "placement_id": placement_id,
        }
    )
    assert outcome.result is not None, outcome.refusal
    return outcome.result


def _spans_of_the_only_receipt(caplog) -> dict[str, int]:
    """Parse the ``phases=`` field off the single receipt in ``caplog``.

    Parsed from the FORMATTED line rather than read off the recorder object,
    because the line is what an operator greps and a receipt that measured
    correctly and printed wrongly is still a broken instrument.
    """

    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("agent_create_phases ")
    ]
    assert len(lines) == 1, f"expected exactly one create receipt, got {lines}"
    field = next(
        part for part in lines[0].split(" ") if part.startswith("phases=")
    )[len("phases=") :]
    if field == NO_PHASES:
        return {}
    spans: dict[str, int] = {}
    for entry in field.split(","):
        key, _, value = entry.partition(":")
        spans[key] = int(value)
    return spans


# ── the receipt's own contract ───────────────────────────────────────────────


def test_a_completed_mint_emits_one_receipt_naming_its_persona_and_its_spans(
    seeded_workspace, persona_factory, caplog
):
    """One line per mint, with the mint's own ``instance_ms`` on it.

    ``instance_ms`` is repeated from the create result on purpose — the receipt
    has to be readable without fetching the RPC reply — so this asserts the two
    are the SAME number rather than two measurements of one span.
    """

    persona_factory("subphase_receipt")

    with caplog.at_level(logging.INFO, logger=agent_create_phases.__name__):
        result = _create("subphase_receipt", "subphase_receipt_1_agent_2")

    line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("agent_create_phases ")
    )
    assert " persona=subphase_receipt " in line
    assert f" instance_ms={result['phases']['instance_ms']} " in line
    assert line.split(" ")[-1].startswith("pid=")

    spans = _spans_of_the_only_receipt(caplog)
    # Every span a healthy mint passes through, and nothing this module does not
    # document. A key here that PHASE_ORDER does not carry would be a number in
    # the operator's log that nothing explains.
    assert set(spans) <= set(PHASE_ORDER)
    for key in (
        "chat_root_ms",
        "create_patch_ms",
        "wire_row_ms",
        "chat_lane_scope_ms",
        "tool_visibility_ms",
        "instance_write_ms",
        "spawned_by_write_ms",
    ):
        assert key in spans, f"the mint never billed {key}"
    # Printed in PHASE_ORDER, so two receipts of the same shape are diffable by
    # eye and a reader learns the nesting from the order.
    assert list(spans) == [key for key in PHASE_ORDER if key in spans]


def test_an_unentered_span_is_absent_from_the_receipt_never_a_zero():
    """Honesty rule 1, asserted on the formatter directly.

    A span that never ran and a span that ran and cost under a millisecond are
    two different facts, and a receipt that spelled both ``0`` would be exactly
    the instrument this lane's launcher twin was built to avoid. Driven through
    the recorder rather than through a create, because the create has no arm
    that skips ``chat_root_ms`` — the fact under test is the FORMATTER's, and a
    property only assertable via a production arm that does not exist yet is a
    property nothing holds.
    """

    empty = CreateSubphases()
    assert empty.snapshot() == {}
    assert empty.formatted() == NO_PHASES

    partial = CreateSubphases()
    partial.record("chat_root_ms", 0)
    partial.record("tool_visibility_ms", 7)
    # ``chat_root_ms`` RAN and cost under a millisecond: that zero is a
    # measurement and must be printed. The eight spans that never ran must not
    # appear at all.
    assert partial.formatted() == "chat_root_ms:0,tool_visibility_ms:7"
    assert "permission_options_ms" not in partial.snapshot()


def test_a_span_entered_twice_sums_rather_than_reporting_only_its_last_visit():
    """Honesty rule 3. The same accumulate-never-overwrite contract
    ``snapshot._timed_section`` holds, so a block a future create enters twice
    bills twice instead of silently losing the first visit."""

    recorder = CreateSubphases()
    recorder.record("wire_row_ms", 30)
    recorder.record("wire_row_ms", 12)
    assert recorder.snapshot()["wire_row_ms"] == 42


def test_an_undocumented_span_key_is_refused_at_the_call_site():
    """A key not in ``PHASE_ORDER`` would print a number nothing explains, and
    would silently vanish from the line (the formatter walks PHASE_ORDER), which
    is the worse of the two failures — a call site that believes it is measuring
    something and is not."""

    with pytest.raises(KeyError):
        CreateSubphases().record("mystery_ms", 5)


def test_the_instrument_is_inert_for_every_caller_but_a_create():
    """``persona_instance_summary`` runs once per persona per snapshot build.
    The span sites on it must cost nothing when no create is recording, and —
    more importantly — must not leak a create's spans into a build that happens
    to run on the same thread afterwards."""

    with agent_create_phases.timed_create_subphase("wire_row_ms"):
        pass  # no recorder installed: nothing to record into, and no raise

    with agent_create_phases.capture_create_subphases() as recorder:
        with agent_create_phases.timed_create_subphase("wire_row_ms"):
            pass
    assert "wire_row_ms" in recorder.snapshot()

    after = CreateSubphases()
    with agent_create_phases.timed_create_subphase("wire_row_ms"):
        pass
    assert after.snapshot() == {}


# ── the conviction ───────────────────────────────────────────────────────────


@pytest.fixture
def rounds_per_projection_read(monkeypatch):
    """Bill each of the projection's three reads its own probe-round delta.

    **Why counted rounds and not the receipt's milliseconds.** The receipt's
    magnitudes are only separable in a genuinely COLD process — the 2,421 ms is
    the registry populate plus the whole toolset sweep, and neither can be
    undone mid-session (``invalidate_check_fn_cache()`` is real API that would
    hand every later test in the run a cold re-probe of docker, playwright and
    sockets; the ``import model_tools`` behind the registry cannot be unwound at
    all). Inside a shared pytest process every span of an "unwarmed" create
    measures single-digit milliseconds and a ranking between them would be
    asserting scheduler noise.

    The probe ROUND is the instrument that survives that, and it is the one this
    lane already trusts (``test_persona_prewarm``'s whole gate is built on it).
    One holed ``check_fn`` entry is enough to make the sweep run for real, and
    attributing the resulting rounds to a NAMED read answers exactly the
    question the receipt's milliseconds answer on a cold box: which of the
    projection's three calls is the one paying.

    Patched at the modules the create actually reaches through —
    ``persona_runtime`` (imported lazily inside ``persona_instance_summary``)
    and ``persona_assignments`` (module-level import) — so a create routed some
    other way would show up as an unattributed remainder rather than as a
    silently passing zero.
    """

    from agent_runtime import persona_assignments, persona_runtime, tool_permissions

    billed: dict[str, int] = {
        "permission_options": 0,
        "chat_lane_scope": 0,
        "tool_visibility": 0,
    }

    def _billing(key: str, real):
        def _wrapper(*args, **kwargs):
            before = _probe_rounds()
            try:
                return real(*args, **kwargs)
            finally:
                billed[key] += _probe_rounds() - before

        return _wrapper

    monkeypatch.setattr(
        persona_assignments,
        "permission_options_for_chat",
        _billing("permission_options", tool_permissions.permission_options_for_chat),
    )
    monkeypatch.setattr(
        persona_runtime,
        "apply_chat_lane_tool_scope",
        _billing("chat_lane_scope", persona_runtime.apply_chat_lane_tool_scope),
    )
    monkeypatch.setattr(
        persona_assignments,
        "resolve_tool_visibility",
        _billing("tool_visibility", persona_assignments.resolve_tool_visibility),
    )
    return billed


def test_a_mint_bills_no_probe_rounds_to_any_projection_read(
    seeded_workspace, persona_factory, evict_one_check_fn, rounds_per_projection_read, caplog
):
    """WHERE THE UNWARMED COST LIVES — and since 2026-09-02 the answer is nowhere.

    This case replaces a PAIR. W3-H1 convicted ``apply_chat_lane_tool_scope`` as
    the read that paid every probe round an unwarmed create ran (``instance_ms``
    2,781 of which ``chat_lane_scope_ms`` 2,421, ``tool_visibility_ms`` 0), and
    the gate beside it asserted that a warm took those rounds to zero. Both
    stopped being able to say anything the day the rounds stopped existing:
    ``personas.all_registered_toolsets`` — which
    ``apply_chat_lane_tool_scope`` calls on the unbounded default — asked
    ``get_available_toolsets()`` for a list of NAMES, and that call ran one
    availability round per toolset to compute an ``available`` boolean it
    discarded. It now asks ``get_registered_toolset_names()``, whose key set is
    identical by construction. The warmed gate became a tautology (zero with or
    without the warm) and its negative arm became unsatisfiable; the negative
    arm is what said so, which is the pair working exactly as designed.

    What is asserted now is the stronger statement neither could make: with the
    shared ``check_fn`` cache deliberately HOLED, a create bills zero rounds to
    all three named reads and zero overall. The eviction is still what stops it
    being run-order dependent — without it the entry is warm from whatever ran
    earlier and this would pass on the old code too.

    The span attribution the receipt carries is untouched and still asserted:
    ``chat_lane_scope_ms >= tool_visibility_ms``. What the warm is still worth
    is an open row for the 3a stage's owner — see
    ``planned/serve-small-batch-field-notes-2026-09-02.md`` §2; the per-persona
    readiness memo it fills is still pinned in ``test_persona_prewarm.py``.
    """

    persona_factory("subphase_cold")
    evict_one_check_fn()

    before = _probe_rounds()
    with caplog.at_level(logging.INFO, logger=agent_create_phases.__name__):
        _create("subphase_cold", "subphase_cold_1_agent_2")
    rounds = _probe_rounds() - before
    spans = _spans_of_the_only_receipt(caplog)

    assert rounds == 0, (
        "a create against a HOLED check_fn cache ran "
        f"{rounds} availability probe rounds — the create path is asking for an "
        f"availability verdict again (billed {rounds_per_projection_read}, "
        f"spans {spans})"
    )
    assert rounds_per_projection_read == {
        "permission_options": 0,
        "chat_lane_scope": 0,
        "tool_visibility": 0,
    }, rounds_per_projection_read
    # The receipt has to AGREE with the attribution, or the instrument the
    # operator reads and the mechanism the gate counts have come apart.
    assert spans["chat_lane_scope_ms"] >= spans["tool_visibility_ms"], spans
