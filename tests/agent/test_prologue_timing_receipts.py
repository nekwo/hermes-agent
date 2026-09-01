"""Stage 3: the prologue's receipts, and the join that carries them to disk.

``planned/chat-turn-prep-cost.md`` §5 Stage 3 asked for two things: a cache of
the tool-schema serialization keyed by the toolset tuple, and proof — "with a
receipt, not by reading" — that the system-prompt restore path hits on turn 2+.

The first half was FALSIFIED against the live record and is documented in
``chat-turn-prep-stages-3-5-field-notes-2026-09-01.md`` §2: the span that
contains that serialization (``profile_conversation_request_build_ms``) bills
**1 ms** on a warm turn, and the cache the plan describes — bounded, keyed on the
toolset frozensets AND ``registry._generation``, copy-on-out, with an explicit
hatch — already exists as ``model_tools._tool_defs_cache``. What it never had is
the receipt. So this file pins the receipts, not a second cache.

The end-to-end tests here matter more than the unit ones, because both halves of
this seam were already correct and only the JOIN was untested: a receipt whose
``timing_key`` the runner's collector silently drops, or whose key the store's
sanitizer refuses, is a receipt that never reaches a turn record — and that is
precisely how ``request_assembled`` was absent from every live record for a day
while every test agreed the wiring was healthy (§1 of the plan).
"""

from __future__ import annotations

import time
import types
from unittest.mock import MagicMock

import pytest

import agent.agent_init as agent_init_mod
import agent.conversation_loop as loop_mod
import model_tools
from agent.conversation_loop import (
    _emit_conversation_timing,
    _restore_or_build_system_prompt,
)
from agent_runtime.mission_chat_turns import safe_turn_profile_timing
from agent_runtime.profile_runner import _profile_status_callback

_RESTORE_KEY = "conversation_system_prompt_restore_ms"
_BUILD_KEY = "conversation_system_prompt_build_ms"
_TURN_CONTEXT_KEY = "conversation_turn_context_ms"


def _collecting_agent(session_db=None, prebuilt_prompt: str = "BUILT_PROMPT"):
    """The minimal agent fake plus a status_callback that records payloads."""

    agent = MagicMock()
    agent._cached_system_prompt = None
    agent.session_id = "test-session-id"
    agent.model = "test-model"
    agent.provider = "openrouter"
    agent.platform = "cli"
    agent._session_db = session_db
    agent._use_prompt_caching = False
    agent._build_system_prompt = MagicMock(return_value=prebuilt_prompt)
    payloads: list[dict] = []
    agent.status_callback = payloads.append
    return agent, payloads


def _timing_keys(payloads) -> list[str]:
    return [
        p["timing_key"]
        for p in payloads
        if isinstance(p, dict) and isinstance(p.get("timing_key"), str)
    ]


# ---------------------------------------------------------------------------
# The system-prompt restore receipt — a POSITIVE pair, exactly one per turn
# ---------------------------------------------------------------------------


class TestSystemPromptReceipt:
    def test_a_restored_prompt_reports_a_restore_and_never_a_build(self):
        """Turn 2+ of one chat: the receipt Stage 3 asked for, in the positive."""

        db = MagicMock()
        db.get_session.return_value = {"system_prompt": "prompt from turn 1"}
        agent, payloads = _collecting_agent(session_db=db)

        _restore_or_build_system_prompt(
            agent, None, [{"role": "user", "content": "hi"}]
        )

        keys = _timing_keys(payloads)
        assert _RESTORE_KEY in keys
        assert _BUILD_KEY not in keys
        agent._build_system_prompt.assert_not_called()
        assert agent._system_prompt_restored_from_session is True

    def test_a_fresh_build_reports_a_build_and_never_a_restore(self):
        """First turn of a session: the miss half, and what it cost."""

        agent, payloads = _collecting_agent(session_db=None)

        _restore_or_build_system_prompt(agent, None, [])

        keys = _timing_keys(payloads)
        assert _BUILD_KEY in keys
        assert _RESTORE_KEY not in keys
        agent._build_system_prompt.assert_called_once()
        assert agent._system_prompt_restored_from_session is False

    @pytest.mark.parametrize(
        "stored_row",
        [
            None,                             # missing row
            {"system_prompt": None},          # legacy NULL column
            {"system_prompt": ""},            # a previous turn wrote nothing
        ],
        ids=["missing", "null", "empty"],
    )
    def test_every_unusable_stored_state_reports_a_build(self, stored_row):
        """A prefix-cache miss must be visible as a miss, whatever caused it."""

        db = MagicMock()
        db.get_session.return_value = stored_row
        agent, payloads = _collecting_agent(session_db=db)

        _restore_or_build_system_prompt(
            agent, None, [{"role": "user", "content": "hi"}]
        )

        keys = _timing_keys(payloads)
        assert _BUILD_KEY in keys
        assert _RESTORE_KEY not in keys

    def test_exactly_one_of_the_pair_is_emitted_never_both_and_never_neither(self):
        """The absent-never-zero contract, stated as the invariant it is.

        A turn that entered the prologue emits exactly one. A turn that never
        entered it emits neither — which is a third state, and the reason this
        is a PAIR rather than one key plus a zero.
        """

        for row in (None, {"system_prompt": "stored"}, {"system_prompt": ""}):
            db = MagicMock()
            db.get_session.return_value = row
            agent, payloads = _collecting_agent(session_db=db)
            _restore_or_build_system_prompt(
                agent, None, [{"role": "user", "content": "hi"}]
            )
            emitted = [k for k in _timing_keys(payloads) if k in (_RESTORE_KEY, _BUILD_KEY)]
            assert len(emitted) == 1, f"row={row!r} emitted {emitted}"

    def test_a_stale_restored_flag_from_a_previous_turn_cannot_survive(self):
        """A gateway agent is reused across turns; a stale True would be a lie.

        The reset lives inside the function rather than at the call site,
        because "the call site remembered" is not a property anything can
        assert — the same argument ``mission_chat_phases`` makes for
        first-mark-wins.
        """

        agent, _ = _collecting_agent(session_db=None)
        agent._system_prompt_restored_from_session = True

        _restore_or_build_system_prompt(agent, None, [])

        assert agent._system_prompt_restored_from_session is False


# ---------------------------------------------------------------------------
# The tool-schema memo's hit/miss receipt
# ---------------------------------------------------------------------------


class TestToolSchemaMemoReceipt:
    def test_the_serializer_runs_once_across_two_builds_of_one_toolset_tuple(
        self, monkeypatch
    ):
        """The property Stage 3 wanted, asked of the EXISTING memo.

        Two consecutive resolutions of an unchanged toolset tuple: the first is
        a miss (the registry walk and the ``check_fn`` sweep are paid), the
        second is a hit. This is the cache the plan asked to be built; it exists,
        and this is the receipt that says so.
        """

        model_tools._clear_tool_defs_cache()
        computed: list[int] = []

        def _fake_compute(*args, **kwargs):
            computed.append(1)
            return [{"function": {"name": "read_file"}}]

        monkeypatch.setattr(model_tools, "_compute_tool_definitions", _fake_compute)

        misses0 = model_tools.tool_defs_cache_misses_this_thread()
        hits0 = model_tools.tool_defs_cache_hits_this_thread()

        model_tools.get_tool_definitions(enabled_toolsets=["files"], quiet_mode=True)
        assert model_tools.tool_defs_cache_misses_this_thread() == misses0 + 1
        assert model_tools.tool_defs_cache_hits_this_thread() == hits0

        model_tools.get_tool_definitions(enabled_toolsets=["files"], quiet_mode=True)
        assert model_tools.tool_defs_cache_misses_this_thread() == misses0 + 1
        assert model_tools.tool_defs_cache_hits_this_thread() == hits0 + 1
        assert len(computed) == 1, "the serializer must run once across two builds"

    def test_a_registry_epoch_change_rebuilds_rather_than_pinning_a_stale_surface(
        self, monkeypatch
    ):
        """The epoch is in the key, and §7.4 says why it must stay there.

        Under the shipped default permission mode a chat's ``enabled_toolsets``
        IS ``all_registered_toolsets()``, so a bundle pinned across a
        registration change can name a toolset that is no longer registered.
        That is a wrong answer about what the turn may do, traded for a memo
        hit — refused.
        """

        model_tools._clear_tool_defs_cache()
        computed: list[int] = []

        def _fake_compute(*args, **kwargs):
            computed.append(1)
            return [{"function": {"name": "read_file"}}]

        monkeypatch.setattr(model_tools, "_compute_tool_definitions", _fake_compute)

        model_tools.get_tool_definitions(enabled_toolsets=["files"], quiet_mode=True)
        model_tools.get_tool_definitions(enabled_toolsets=["files"], quiet_mode=True)
        assert len(computed) == 1

        misses_before = model_tools.tool_defs_cache_misses_this_thread()
        model_tools.registry._generation += 1
        try:
            model_tools.get_tool_definitions(
                enabled_toolsets=["files"], quiet_mode=True
            )
        finally:
            model_tools.registry._generation -= 1
        assert len(computed) == 2, "a registration change must rebuild the schemas"
        assert model_tools.tool_defs_cache_misses_this_thread() == misses_before + 1

    def test_the_hatch_drops_the_memo_and_keeps_the_receipt(self, monkeypatch):
        """An invalidation does not un-perform the work that was already done."""

        monkeypatch.setattr(
            model_tools,
            "_compute_tool_definitions",
            lambda *a, **k: [{"function": {"name": "read_file"}}],
        )
        model_tools.get_tool_definitions(enabled_toolsets=["files"], quiet_mode=True)
        misses = model_tools.tool_defs_cache_misses_this_thread()
        hits = model_tools.tool_defs_cache_hits_this_thread()

        model_tools._clear_tool_defs_cache()

        assert model_tools.tool_defs_cache_misses_this_thread() == misses
        assert model_tools.tool_defs_cache_hits_this_thread() == hits

    def test_a_miss_reports_a_build_and_a_hit_reports_a_cached_read(self):
        payloads: list[dict] = []
        agent_init_mod._emit_tool_defs_receipt(
            payloads.append, started=time.perf_counter(), misses_before=4, misses_after=5
        )
        assert _timing_keys(payloads) == ["agent_init_tool_defs_build_ms"]

        payloads.clear()
        agent_init_mod._emit_tool_defs_receipt(
            payloads.append, started=time.perf_counter(), misses_before=5, misses_after=5
        )
        assert _timing_keys(payloads) == ["agent_init_tool_defs_cached_ms"]

    @pytest.mark.parametrize(
        ("before", "after"),
        [(None, 5), (5, None), (None, None)],
        ids=["unreadable-before", "unreadable-after", "unreadable-both"],
    )
    def test_an_unreadable_counter_reports_NOTHING_rather_than_a_hit(
        self, before, after
    ):
        """"I could not ask" is not "it hit".

        Collapsing an unmeasured reading into the cached arm is exactly the fake
        zero the honesty contract forbids — it would make a broken counter look
        like a perfectly warm process.
        """

        payloads: list[dict] = []
        agent_init_mod._emit_tool_defs_receipt(
            payloads.append, started=time.perf_counter(), misses_before=before, misses_after=after
        )
        assert payloads == []


# ---------------------------------------------------------------------------
# The JOIN: emitter → runner collector → store sanitizer
# ---------------------------------------------------------------------------


class TestTheReceiptsReachTheDurableRecord:
    """Both halves of this seam were already correct; only the join was untested.

    Each case drives the REAL chain — ``_emit_conversation_timing`` (or the
    tool-defs receipt) into the REAL ``_profile_status_callback`` into the REAL
    ``safe_turn_profile_timing`` — and asserts the key survives all three. A
    ``timing_key`` the collector's prefix allowlist drops, or one the store's
    sanitizer refuses, is a receipt that never reaches a turn record.
    """

    @staticmethod
    def _chain():
        timing: dict = {}
        request = types.SimpleNamespace(progress_callback=None)
        return timing, _profile_status_callback(request, timing)

    def test_the_turn_context_timing_survives_collector_and_sanitizer(self):
        timing, sink = self._chain()
        agent = MagicMock()
        agent.status_callback = sink

        _emit_conversation_timing(agent, "turn_context", time.perf_counter())

        assert "profile_conversation_turn_context_ms" in timing
        block = safe_turn_profile_timing(timing)
        assert block is not None
        assert "profile_conversation_turn_context_ms" in block

    def test_the_system_prompt_pair_survives_collector_and_sanitizer(self):
        timing, sink = self._chain()
        db = MagicMock()
        db.get_session.return_value = {"system_prompt": "stored"}
        agent, _ = _collecting_agent(session_db=db)
        agent.status_callback = sink

        _restore_or_build_system_prompt(
            agent, None, [{"role": "user", "content": "hi"}]
        )

        assert f"profile_{_RESTORE_KEY}" in timing
        block = safe_turn_profile_timing(timing)
        assert block is not None
        assert f"profile_{_RESTORE_KEY}" in block
        assert f"profile_{_BUILD_KEY}" not in block

    def test_the_tool_defs_receipt_survives_collector_and_sanitizer(self):
        timing, sink = self._chain()

        agent_init_mod._emit_tool_defs_receipt(
            sink, started=time.perf_counter(), misses_before=1, misses_after=2
        )

        assert "profile_agent_init_tool_defs_build_ms" in timing
        block = safe_turn_profile_timing(timing)
        assert block is not None
        assert "profile_agent_init_tool_defs_build_ms" in block

    def test_a_receipt_that_was_never_emitted_stays_ABSENT_on_the_record(self):
        """Absent stays absent — the rule the whole plan exists to protect."""

        timing, sink = self._chain()
        _emit_conversation_timing(
            MagicMock(status_callback=sink), "turn_context", time.perf_counter()
        )
        block = safe_turn_profile_timing(timing)
        assert block is not None
        for never_emitted in (
            f"profile_{_RESTORE_KEY}",
            f"profile_{_BUILD_KEY}",
            "profile_agent_init_tool_defs_build_ms",
            "profile_agent_init_tool_defs_cached_ms",
            "session_db_open_ms",
        ):
            assert never_emitted not in block


# ---------------------------------------------------------------------------
# The turn_context timing at its APPLYING chokepoint
# ---------------------------------------------------------------------------


class _Sentinel(Exception):
    """Raised from the turn context the instant the loop reads it."""


class _TripwireContext:
    """A turn context that aborts the loop the moment its first field is read.

    ``run_conversation`` emits the ``turn_context`` timing immediately after
    ``build_turn_context`` returns and immediately before it unpacks the
    context. Aborting on the first field read therefore isolates exactly that
    window: the whole conversation loop below never runs, and the assertion is
    about the CALL SITE rather than about the helper the call site uses.
    """

    @property
    def user_message(self):
        raise _Sentinel


def test_run_conversation_times_build_turn_context_at_the_call_site(monkeypatch):
    """Declaration is not policy: the emitter existing is not the loop using it.

    A first pass of this file tested ``_emit_conversation_timing(agent,
    "turn_context", ...)`` directly and stayed GREEN when the call site was
    unwired — the helper was pinned and the chokepoint was not. This drives the
    real ``run_conversation`` prologue instead.
    """

    payloads: list[dict] = []
    agent = MagicMock()
    agent.status_callback = payloads.append

    monkeypatch.setattr(
        loop_mod, "build_turn_context", lambda *a, **k: _TripwireContext()
    )

    with pytest.raises(_Sentinel):
        loop_mod.run_conversation(agent, "hello", None, [])

    assert _TURN_CONTEXT_KEY in _timing_keys(payloads), (
        "the prologue's largest single call must be timed BY THE LOOP, not "
        "merely be timeable"
    )
