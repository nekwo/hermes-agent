"""T2 + T5 of ``PERF_SEND_ANALYSIS_2026-08-09``: admission attribution + chat compaction.

**T2** asked for an admission-outcome CACHE keyed on (persona, server set,
manifest hash), on a premise that per-turn re-registration costs 2.35-3.4 s.
Measured against a real 60-tool stdio MCP server on 2026-08-09, that premise does
not hold: a WARM re-registration of all 60 tools is **6-8 ms** and teardown is
0.2-0.3 ms, while a COLD admission — spawn + MCP handshake + ``tools/list`` — is
**3,197 ms**. The analysis' three probe turns each ran in a fresh CLI process, so
all three paid a cold spawn and reported turn-1 numbers as steady state. A cache
of registered tool definitions cannot remove a spawn (a tool call needs a live
session, not a remembered schema), so it would buy ~6 ms in exchange for a new
cross-persona leak surface on the isolation chokepoint. It was not built.

What was built instead is the attribution that makes the claim CHECKABLE in
production — ``transport_paths`` on the outcome, ``mcp_admission_transport`` /
``mcp_admission_cold_servers`` on the turn's ``profile_timing`` — plus the
isolation pin the cache would have needed, aimed at the reuse path that actually
exists: the warm re-registration must apply THIS run's tool filter, never the
one the transport was first registered under.

**T5** gives the mission-chat lane a compaction threshold it can actually reach
(``agent_runtime.mission_chat.compaction_threshold_tokens``, default 150,000)
and makes the number the operator surface renders a READING off the live
compressor rather than a ``window x ratio`` derivation that cannot see the cap.
"""

from __future__ import annotations

import pytest

from agent_runtime.config import _mission_chat_config
from agent_runtime.mcp_admission import (
    LANE_MISSION_CHAT,
    TRANSPORT_COLD,
    TRANSPORT_WARM,
    McpAdmission,
    admit_mcp_servers,
    classify_admission_transport,
    teardown_mcp_admission,
)
from agent_runtime.profile_runner import _apply_chat_compaction_threshold
from agent_runtime.prompt_observability import (
    COMPACTION_BASIS_LIVE_COMPRESSOR,
    COMPACTION_BASIS_MODEL_RATIO,
    _context_budget,
    _safe_final_model_input,
)
from agent_runtime.runtime_config import MissionChatConfig


# ── T5: the compaction threshold write path ─────────────────────────────────


class _FakeCompressor:
    """The four attributes ``_apply_chat_compaction_threshold`` touches.

    Mirrors ``agent.context_compressor.ContextCompressor``'s *contract*, not its
    internals: ``threshold_tokens`` is read/written, ``threshold_tokens_cap`` is
    the first-class field ``update_model`` re-applies, and ``context_length`` is
    the window the cap is clamped against.
    """

    def __init__(self, *, threshold_tokens: int, context_length: int):
        self.threshold_tokens = threshold_tokens
        self.context_length = context_length
        self.threshold_percent = (
            threshold_tokens / context_length if context_length else 0.0
        )
        self.threshold_tokens_cap = None


def _luna_compressor() -> _FakeCompressor:
    """The lane's real shape: a 1.05 M window compacting at 892,500.

    These are the measured live values, and the gap between them is the whole
    finding — a chat root reaches 892,500 approximately never, so compaction is
    a bound that exists on paper only.
    """

    return _FakeCompressor(threshold_tokens=892_500, context_length=1_050_000)


def test_lane_cap_pulls_the_unreachable_threshold_down_to_something_a_root_hits():
    compressor = _luna_compressor()

    receipt = _apply_chat_compaction_threshold(compressor, 150_000, source="lane_default")

    assert compressor.threshold_tokens == 150_000
    # The cap — not just the live value — because that is what survives a model
    # switch. Writing only `threshold_tokens` would silently restore 892,500 the
    # first time the turn switched models.
    assert compressor.threshold_tokens_cap == 150_000
    assert receipt["applied"] is True
    assert receipt["source"] == "lane_default"
    assert receipt["model_threshold_tokens"] == 892_500
    assert receipt["effective_threshold_tokens"] == 150_000


def test_lane_cap_never_raises_a_small_window_models_own_threshold():
    """The cap can only make compaction fire EARLIER.

    Discriminating on purpose: a 32 k-window model already compacts at 27,200,
    and an implementation that simply assigned the lane number would push it out
    to 150,000 — nearly 5x the model's entire window — and the turn would blow
    the context instead of compacting. That is the failure this direction exists
    to prevent, so the fixture is a model where the two directions DISAGREE.
    """

    compressor = _FakeCompressor(threshold_tokens=27_200, context_length=32_000)

    receipt = _apply_chat_compaction_threshold(compressor, 150_000, source="lane_default")

    assert compressor.threshold_tokens == 27_200
    assert receipt["applied"] is False
    assert receipt["reason"] == "model_threshold_already_lower"
    assert receipt["effective_threshold_tokens"] == 27_200


def test_a_turn_override_still_wins_in_both_directions():
    """The override is an operator instruction, not a second cap.

    Both directions are asserted because only the RAISING one discriminates: an
    implementation that routed the override through the cap would pass the
    lowering case (a cap lowers) and silently ignore every raise — including the
    ``--compression-threshold-tokens`` compression-proof seam turns that set a
    threshold deliberately far from the model's own.
    """

    lowering = _luna_compressor()
    _apply_chat_compaction_threshold(lowering, 1_000, source="turn_override")
    assert lowering.threshold_tokens == 1_000

    raising = _FakeCompressor(threshold_tokens=27_200, context_length=32_000)
    receipt = _apply_chat_compaction_threshold(raising, 30_000, source="turn_override")
    assert raising.threshold_tokens == 30_000
    assert receipt["applied"] is True
    assert receipt["source"] == "turn_override"


def test_the_receipt_still_says_applied_on_a_reused_actors_second_turn():
    """``applied`` describes the STATE, not whether this call moved a number.

    The warm serve lane reuses a resident actor across turns, so from turn 2
    onward the compressor already holds the capped value and nothing is written.
    A did-it-change reading would report ``applied: False`` with
    ``model_threshold_already_lower`` on every turn but the first — telling an
    operator the cap was inactive, and blaming the model, on the exact lane the
    cap exists for. This is the second call against ONE compressor, which is the
    only fixture where the two readings disagree.
    """

    compressor = _luna_compressor()
    _apply_chat_compaction_threshold(compressor, 150_000, source="lane_default")

    second = _apply_chat_compaction_threshold(compressor, 150_000, source="lane_default")

    assert compressor.threshold_tokens == 150_000
    assert second["applied"] is True
    assert "reason" not in second


def test_zero_is_the_documented_rollback_and_leaves_the_model_threshold_alone():
    compressor = _luna_compressor()

    receipt = _apply_chat_compaction_threshold(compressor, 0, source="lane_default")

    assert compressor.threshold_tokens == 892_500
    assert compressor.threshold_tokens_cap is None
    assert receipt["applied"] is False
    assert receipt["reason"] == "lane_cap_disabled"


def test_an_unusable_compressor_yields_a_receipt_instead_of_failing_the_turn():
    class _Opaque:
        @property
        def threshold_tokens(self):  # pragma: no cover - raised, never returned
            raise RuntimeError("no compressor here")

    receipt = _apply_chat_compaction_threshold(_Opaque(), 150_000, source="lane_default")

    assert receipt["applied"] is False
    assert receipt["reason"] == "unavailable"


# ── T5: the config knob ─────────────────────────────────────────────────────


def test_absent_stanza_ships_the_default_threshold():
    assert _mission_chat_config({}).compaction_threshold_tokens == 150_000
    assert MissionChatConfig().compaction_threshold_tokens == 150_000


def test_explicit_zero_disables_the_cap_rather_than_restoring_the_default():
    """The rollback spelling must survive the parser.

    Discriminating against the obvious implementation: the sibling knobs all use
    ``_clamped_positive_int``, which maps every non-positive value onto the
    default — so ``compaction_threshold_tokens: 0`` would come back as 150,000
    and an operator's disable would silently re-enable the feature.
    """

    assert _mission_chat_config({"compaction_threshold_tokens": 0}).compaction_threshold_tokens == 0
    assert _mission_chat_config({"compaction_threshold_tokens": -5}).compaction_threshold_tokens == 0


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5_000, 16_000),          # below the floor: a cap under turn-1's own prefix
        (9_999_999, 2_000_000),   # above the ceiling
        ("not a number", 150_000),
        (None, 150_000),
        (True, 150_000),          # bool is not an int here
        (250_000, 250_000),
    ],
)
def test_the_configured_threshold_is_clamped_not_trusted(raw, expected):
    assert _mission_chat_config({"compaction_threshold_tokens": raw}).compaction_threshold_tokens == expected


# ── T5: the receipt an operator reads ───────────────────────────────────────


_MODEL_SELECTION = {"effective_model": "gpt-5.6-luna", "effective_provider": "openai-codex"}


def test_the_context_budget_reports_the_live_threshold_not_the_window_ratio():
    """A 150 k cap must not render as "compaction at 892,500".

    The fixture is chosen so the two answers CANNOT coincide: the derivation is
    window x ratio and the reading is 150,000, so a budget that ignored the
    recorded row would show a different, larger number.
    """

    budget = _context_budget(
        _MODEL_SELECTION,
        {"context_compaction": {"schema_version": 1, "effective_threshold_tokens": 150_000}},
        None,
    )

    assert budget["compaction_tokens"] == 150_000
    assert budget["compaction_basis"] == COMPACTION_BASIS_LIVE_COMPRESSOR
    # The derivation is still reported, so the two are comparable rather than
    # one silently replacing the other.
    assert budget["compaction_tokens_model_ratio"] != 150_000
    assert budget["compaction_tokens_model_ratio"] > 150_000


def test_a_lane_that_records_no_compressor_keeps_the_derivation_and_says_so():
    budget = _context_budget(_MODEL_SELECTION, {"messages": []}, None)

    assert budget["compaction_basis"] == COMPACTION_BASIS_MODEL_RATIO
    assert budget["compaction_tokens"] == budget["compaction_tokens_model_ratio"]


def test_a_malformed_compaction_row_falls_back_rather_than_rendering_a_guess():
    for bogus in ({"effective_threshold_tokens": 0}, {"effective_threshold_tokens": "150000"}, "nope"):
        budget = _context_budget(_MODEL_SELECTION, {"context_compaction": bogus}, None)
        assert budget["compaction_basis"] == COMPACTION_BASIS_MODEL_RATIO


def test_the_redaction_safe_copy_keeps_the_compaction_row():
    """The whitelist is the reason this needs a pin.

    ``_safe_final_model_input`` rebuilds the row key by key, and the deferred
    refresh path (``_context_budget_needs_refresh``) re-derives the budget from
    the SAFE copy — so a dropped key would restore the wrong compaction number on
    exactly the rows an operator inspects after the fact. The value is 150,000,
    which differs from the derivation, so a drop is visible.
    """

    safe = _safe_final_model_input(
        {
            "messages": [],
            "context_compaction": {
                "schema_version": 1,
                "effective_threshold_tokens": 150_000,
                "threshold_tokens_cap": 150_000,
                "context_length": 1_050_000,
                "compression_in_place": False,
            },
        }
    )

    assert safe["context_compaction"]["effective_threshold_tokens"] == 150_000
    assert safe["context_compaction"]["threshold_tokens_cap"] == 150_000
    assert safe["context_compaction"]["compression_in_place"] is False
    assert _context_budget(_MODEL_SELECTION, safe, None)["compaction_tokens"] == 150_000


# ── T2: transport attribution + the reuse isolation pin ─────────────────────


@pytest.fixture
def clean_registry():
    """Leave the process-global tool registry exactly as we found it."""

    from tools.registry import registry

    yield registry
    for toolset in list(registry.get_registered_toolset_names() or []):
        if not str(toolset).startswith("mcp-"):
            continue
        for name in list(registry.get_tool_names_for_toolset(toolset) or []):
            registry.deregister(name)


class _FakeMcpTool:
    def __init__(self, name: str):
        self.name = name
        self.description = f"fake {name}"
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeConnectedServer:
    """A CONNECTED server task: live session, tools already listed, no transport."""

    def __init__(self, name: str, tool_names):
        self.name = name
        self.session = object()
        self.tool_timeout = 5.0
        self._tools = [_FakeMcpTool(tool) for tool in tool_names]


_SURFACE = ("bench_read", "bench_write", "bench_delete")


def _admission(server: str = "bench", *, include=None) -> McpAdmission:
    config: dict[str, object] = {"command": "noop"}
    if include is not None:
        config["tools"] = {"include": list(include)}
    return McpAdmission(
        lane=LANE_MISSION_CHAT,
        role="qa",
        permission_mode="profile_default",
        enabled=True,
        requested=(server,),
        server_names=(server,),
        server_configs={server: config},
    )


def _registered(registry, server: str = "bench") -> set[str]:
    return {
        name.rsplit("__", 1)[-1]
        for name in (registry.get_tool_names_for_toolset(f"mcp-{server}") or [])
    }


def test_a_server_with_no_live_session_classifies_cold(monkeypatch):
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(mcp_tool, "_servers", {}, raising=False)

    assert classify_admission_transport(["bench"]) == {"bench": TRANSPORT_COLD}


def test_a_connected_server_classifies_warm_and_a_parked_one_does_not(monkeypatch):
    """Parked (``session is None``) must read COLD, not warm.

    That is not a naming quibble: ``_default_registrar`` routes on exactly this
    predicate and sends a parked server through ``register_mcp_servers`` (the
    only path with wake handling). A label that said "warm" for a server the
    registrar treats as cold would be a receipt that disagrees with the code it
    describes — worse than no receipt.
    """

    import tools.mcp_tool as mcp_tool

    parked = _FakeConnectedServer("parked", _SURFACE)
    parked.session = None
    monkeypatch.setattr(
        mcp_tool,
        "_servers",
        {"bench": _FakeConnectedServer("bench", _SURFACE), "parked": parked},
        raising=False,
    )

    assert classify_admission_transport(["bench", "parked"]) == {
        "bench": TRANSPORT_WARM,
        "parked": TRANSPORT_COLD,
    }


def test_the_outcome_carries_the_transport_path_the_turn_actually_took(
    monkeypatch, clean_registry
):
    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(
        mcp_tool, "_servers", {"bench": _FakeConnectedServer("bench", _SURFACE)}, raising=False
    )

    outcome = admit_mcp_servers(_admission())

    assert outcome.admitted == ("bench",)
    assert dict(outcome.transport_paths) == {"bench": TRANSPORT_WARM}
    teardown_mcp_admission(outcome.admitted)


def test_a_caller_supplied_registrar_gets_no_transport_label(monkeypatch, clean_registry):
    """Silence beats a confident label for a path that was never taken."""

    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(
        mcp_tool, "_servers", {"bench": _FakeConnectedServer("bench", _SURFACE)}, raising=False
    )

    outcome = admit_mcp_servers(_admission(), register=lambda servers: [])

    assert dict(outcome.transport_paths) == {}


def test_a_warm_transport_re_registers_under_THIS_runs_tool_filter(
    monkeypatch, clean_registry
):
    """The isolation pin T2's cache would have needed, on the reuse path we have.

    Sequence is the one a warm multi-persona serve process actually produces:
    persona A admits ``bench`` with no filter (full surface), its registry scope
    is torn down, and persona B then admits the SAME warm transport with a
    one-tool include list. B must see one tool.

    This is the fixture a leaky reuse would fail. Any implementation that
    remembered A's registered tool definitions and reinstalled them for B —
    which is precisely what an admission cache keyed on the server set or the
    manifest hash does, since both are identical across the two runs — hands B
    ``bench_write`` and ``bench_delete``, tools its own configuration excludes.
    The transport is deliberately left WARM between the two admissions so the
    reuse path is the one under test; on a cold transport the second admission
    would re-list from scratch and the leak could not appear.
    """

    import tools.mcp_tool as mcp_tool

    monkeypatch.setattr(
        mcp_tool, "_servers", {"bench": _FakeConnectedServer("bench", _SURFACE)}, raising=False
    )

    wide = admit_mcp_servers(_admission())
    assert _registered(clean_registry) == set(_SURFACE)
    teardown_mcp_admission(wide.admitted)
    assert _registered(clean_registry) == set()
    # The transport itself survived the teardown — that is the state the reuse
    # path exists for, and the state a cache would have been tempted by.
    assert mcp_tool._servers["bench"].session is not None

    narrow = admit_mcp_servers(_admission(include=["bench_read"]))
    try:
        assert dict(narrow.transport_paths) == {"bench": TRANSPORT_WARM}
        assert _registered(clean_registry) == {"bench_read"}
    finally:
        teardown_mcp_admission(narrow.admitted)
