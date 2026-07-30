"""The chat lane's run record: ``run_budget`` on the mission-chat turn journal.

The retired goal/task lane once copied this block onto
``AgentRun.llm["run_budget"]``; S34 removed that writerless field. A pure
**chat** turn never had an ``AgentRun`` — ``runs/`` belonged to the goal/task
lane — so the block reached the live envelope, was read once by whoever was
watching, and then evaporated. "Why was that reply short?" had no durable
answer five seconds after the turn settled.

The mission-chat turn journal IS the chat lane's run record, so the block lives
there, under the SAME key and in the SAME verbatim shape every other carrier
uses (``docs/agent-runtime-harness/run-budget-accounting.md`` §3).

Three claims, and the file is organised around them:

1. **The settle point writes it.** Both ways a turn can end — a completed run
   whose block rides ``profile_timing``, and a tripped run whose block rides
   ``RunBudgetExceeded`` — and for the UNTRIPPED case as well as the tripped
   one. A block that only appears when something broke re-creates the exact
   blindness the ledger was built to retire: "stopped at the bound" and
   "finished with room to spare" become indistinguishable again.
2. **Absent stays absent.** Older records, and turns that declared no budget,
   carry no key. Never an empty dict — "nothing bounded this turn" and "nobody
   accounted this turn" are different facts.
3. **The cockpit can read it.** The chat-history projection builds its rows from
   an explicit allowlist, so an unknown key does NOT ride through on its own.
   Both row shapes a turn can project as — the agent reply row, and the terminal
   marker row a reply-less budget-exhausted turn gets instead — carry it.

The blocks here are produced by driving the REAL runner, not by hand-writing a
dict: a fixture-shaped block would pin this file's idea of the contract instead
of the ledger's.
"""

from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

from agent_runtime import turn_budget
from agent_runtime.mission_chat_turns import (
    MissionChatTurnPersistOutcome,
    mission_chat_turn_record,
    persist_mission_chat_turn,
    transition_mission_chat_turn,
)
from agent_runtime.persona_chat_history import (
    _safe_recent_messages,
    _terminal_turn_marker_rows,
)
from agent_runtime.profile_runner import (
    AgentRunRequest,
    ProfileAgentRunner,
    RunBudgetExceeded,
)
from agent_runtime.run_budget import (
    ACCOUNTING_KEY,
    safe_accounting_block,
    turn_run_budget_metadata,
)


# ── driving a real bounded turn ─────────────────────────────────────────────


class _Agent:
    """Minimal agent, same shape ``test_run_budget.py`` drives the runner with."""

    def __init__(self, **kwargs):
        self.session_id = kwargs.get("session_id") or "session_chat_budget"
        self.provider = kwargs.get("provider")
        self.model = kwargs.get("model")
        self.base_url = None
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")
        self.tool_start_callback = kwargs.get("tool_start_callback")
        self.tool_complete_callback = kwargs.get("tool_complete_callback")

    def steer(self, text: str) -> bool:
        return True

    def interrupt(self, message: str | None = None) -> None:
        return None

    def run_conversation(self, user_message, system_message=None, task_id=None):
        return {
            "final_response": "ok",
            "session_id": self.session_id,
            "messages": [],
            "api_calls": 1,
            "total_tokens": 3,
        }


def _sleeping_agent(seconds: float):
    class _Sleeper(_Agent):
        def run_conversation(self, user_message, system_message=None, task_id=None):
            time.sleep(seconds)
            return super().run_conversation(user_message, system_message, task_id)

    return _Sleeper


def _run(agent_factory, **kwargs):
    return ProfileAgentRunner(agent_factory=agent_factory).run(
        AgentRunRequest(profile=None, user_message="hi", **kwargs)
    )


def _untripped_result():
    """A completed, bounded turn: a wall budget nowhere near being spent."""

    return _run(_Agent, max_wall_seconds=120.0)


def _tripped_error() -> RunBudgetExceeded:
    """A turn the hard wall killed — no result exists, the block rides the raise."""

    with pytest.raises(RunBudgetExceeded) as excinfo:
        _run(_sleeping_agent(0.8), max_wall_seconds=0.15)
    return excinfo.value


def _rows(block: dict) -> dict[str, dict]:
    return {row["kind"]: row for row in block["budgets"]}


def _settle(
    *,
    session_id: str,
    client_message_id: str,
    state: str,
    metadata: dict,
    turn_id: str = "turn_1",
):
    """Walk a turn to ``state`` through the real journal transitions."""

    persist_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        elements=[],
        state="pending",
        write_ahead=True,
    )
    transition_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        elements=[],
        state="executing",
    )
    outcome = transition_mission_chat_turn(
        session_id=session_id,
        client_message_id=client_message_id,
        turn_id=turn_id,
        elements=[],
        state=state,
        metadata=metadata,
    )
    assert outcome is MissionChatTurnPersistOutcome.PERSISTED
    return mission_chat_turn_record(
        session_id=session_id, client_message_id=client_message_id
    )


# ── claim 1: the settle point writes it, tripped AND untripped ──────────────


def test_an_untripped_turn_records_its_headroom(isolate_agent_runtime_root):
    """The case a "only record failures" design would lose.

    Nothing bounded this turn, and that is a FACT worth persisting: with the
    limit and the consumption on the record, an operator can tell a turn that
    stopped at its wall from one that finished with two minutes to spare.
    """

    result = _untripped_result()
    metadata = turn_run_budget_metadata(result=result)

    record = _settle(
        session_id="s-untripped",
        client_message_id="client_untripped",
        state="native_committed",
        metadata={"native_committed": True, "stored_reply": "ok", **metadata},
    )

    block = record[ACCOUNTING_KEY]
    assert block["bounded_by"] is None
    assert block["tripped"] == []
    wall = _rows(block)["wall"]
    assert wall["limit"] == 120 and wall["tripped"] is False and wall["remaining"] > 0
    # Verbatim: the record carries the ledger's block, not a re-derivation.
    assert block == result.profile_timing[ACCOUNTING_KEY]


def test_a_tripped_wall_records_what_bounded_the_turn(isolate_agent_runtime_root):
    """The raised path, where there is no result to carry anything.

    This is the settle point the 2026-07-26 incident produced: the turn ends on
    its wall clock, settles ``budget_exhausted``, and — before this — kept a
    one-line ``budget_summary`` and no accounting at all.
    """

    error = _tripped_error()
    metadata = turn_run_budget_metadata(error=error)

    record = _settle(
        session_id="s-tripped",
        client_message_id="client_tripped",
        state="budget_exhausted",
        metadata={
            "provider_submitted": True,
            "budget_exhausted": True,
            "budget_trigger": "wall_budget_hard_wall",
            "budget_summary": str(error)[:400],
            **metadata,
        },
    )

    block = record[ACCOUNTING_KEY]
    assert block["bounded_by"] == "wall"
    assert block["trip_reason"] == "wall_clock_exceeded"
    assert block["enforcement"] == "trips_run"
    assert _rows(block)["wall"]["tripped"] is True
    assert block == error.run_budget
    # The pre-existing wall provenance is untouched — this is additive.
    assert record["budget_exhausted"] is True
    assert record["budget_trigger"] == "wall_budget_hard_wall"


def test_the_block_survives_the_rest_of_the_journal_walk(isolate_agent_runtime_root):
    """A later transition that says nothing about budgets must not erase it.

    The store merges metadata onto the existing record, so this is really a
    guard on that merge staying a merge — a settle-time key that a projection
    transition silently dropped would be worse than never writing it.
    """

    metadata = turn_run_budget_metadata(result=_untripped_result())
    _settle(
        session_id="s-walk",
        client_message_id="client_walk",
        state="native_committed",
        metadata={"native_committed": True, "stored_reply": "ok", **metadata},
    )

    transition_mission_chat_turn(
        session_id="s-walk",
        client_message_id="client_walk",
        turn_id="turn_1",
        elements=[],
        state="projected",
        metadata={"projection_committed": True},
    )

    record = mission_chat_turn_record(
        session_id="s-walk", client_message_id="client_walk"
    )
    assert record["state"] == "projected"
    assert record[ACCOUNTING_KEY]["bounded_by"] is None


# ── claim 2: absent stays absent ────────────────────────────────────────────


def test_a_turn_that_declared_no_budget_records_an_empty_ledger_not_absence(
    isolate_agent_runtime_root,
):
    """"Accounted, nothing declared" is NOT the same as "not accounted".

    No ``max_wall_seconds``, no read/search bound, no MCP budget: the runner
    still builds its stand-in checkpoint, and the ledger still reports — with
    zero rows, because "only declared budgets appear". That block is carried
    verbatim like any other. Collapsing it to absence here would make an
    unbounded turn indistinguishable from a pre-2026-07-27 record, which is the
    distinction this key exists to keep.
    """

    result = _run(_Agent)
    block = result.profile_timing[ACCOUNTING_KEY]
    assert block["budgets"] == [] and block["bounded_by"] is None

    record = _settle(
        session_id="s-none",
        client_message_id="client_none",
        state="native_committed",
        metadata={
            "native_committed": True,
            "stored_reply": "ok",
            **turn_run_budget_metadata(result=result),
        },
    )
    assert record[ACCOUNTING_KEY] == block


def test_an_older_record_is_never_backfilled(isolate_agent_runtime_root):
    """Records written before this key existed stay silent. An empty dict here
    would be a claim that the turn was accounted and bounded by nothing."""

    record = _settle(
        session_id="s-legacy",
        client_message_id="client_legacy",
        state="native_committed",
        metadata={"native_committed": True, "stored_reply": "ok"},
    )
    assert ACCOUNTING_KEY not in record


@pytest.mark.parametrize("junk", [None, {}, "wall", 7, [], {1: "not-a-str-key"}])
def test_a_non_block_is_dropped_rather_than_stored(isolate_agent_runtime_root, junk):
    record = _settle(
        session_id=f"s-junk-{abs(hash(str(junk)))}",
        client_message_id="client_junk",
        state="native_committed",
        metadata={"native_committed": True, ACCOUNTING_KEY: junk},
    )
    assert ACCOUNTING_KEY not in record


def test_a_hostile_block_is_bounded_but_not_reshaped(isolate_agent_runtime_root):
    """Defence in depth for a foreign/corrupt record: the row list is capped and
    non-dict rows are dropped, but nothing is renamed or filled in — the
    contract the doc documents is what a reader gets."""

    hostile = {
        "bounded_by": "wall",
        "budgets": [{"kind": f"k{index}"} for index in range(80)] + ["nope"],
        "unknown_future_key": "kept",
    }

    record = _settle(
        session_id="s-hostile",
        client_message_id="client_hostile",
        state="native_committed",
        metadata={"native_committed": True, ACCOUNTING_KEY: hostile},
    )

    block = record[ACCOUNTING_KEY]
    assert len(block["budgets"]) == 32
    assert all(isinstance(row, dict) for row in block["budgets"])
    assert block["bounded_by"] == "wall"
    assert block["unknown_future_key"] == "kept"


def test_the_adapter_prefers_the_error_and_ignores_a_non_budget_exception():
    """``turn_run_budget_metadata`` is the one adapter both settle points use."""

    assert turn_run_budget_metadata(error=RuntimeError("boom")) == {}
    assert turn_run_budget_metadata() == {}
    assert turn_run_budget_metadata(result=object()) == {}
    assert safe_accounting_block({"bounded_by": None}) == {"bounded_by": None}
    assert safe_accounting_block({}) is None


# ── claim 3: the cockpit can read it ────────────────────────────────────────


class _FakeSessionDB:
    def __init__(self, messages):
        self._messages = list(messages)

    def get_messages(self, session_id, include_inactive=False):
        return list(self._messages)


def test_the_reply_row_carries_the_block(isolate_agent_runtime_root):
    """The chat-history projection hand-builds each row from an allowlist, so a
    new key on the turn record does NOT reach the cockpit by itself."""

    metadata = turn_run_budget_metadata(result=_untripped_result())
    _settle(
        session_id="s-projected",
        client_message_id="client_projected",
        state="native_committed",
        metadata={"native_committed": True, "stored_reply": "ok", **metadata},
    )

    rows, _status = _safe_recent_messages(
        _FakeSessionDB(
            [
                {
                    "role": "assistant",
                    "content": "ok",
                    "platform_message_id": "client_projected",
                    "created_at": "2026-07-27T00:00:00Z",
                }
            ]
        ),
        session_id="s-projected",
    )

    agent_rows = [row for row in rows if row.get("role") == "agent"]
    assert agent_rows, "the reply row disappeared from the projection"
    assert agent_rows[0][ACCOUNTING_KEY]["bounded_by"] is None


def test_the_terminal_marker_row_carries_the_block(isolate_agent_runtime_root):
    """A reply-less budget-exhausted turn projects ONLY as a marker row, so if
    the block did not ride here, the turns whose bound is most worth reading
    would be exactly the ones carrying none."""

    error = _tripped_error()
    _settle(
        session_id="s-marker",
        client_message_id="client_marker",
        state="budget_exhausted",
        metadata={
            "provider_submitted": True,
            "budget_exhausted": True,
            **turn_run_budget_metadata(error=error),
        },
    )

    rows = _terminal_turn_marker_rows(
        session_id="s-marker", assistant_client_message_ids=set()
    )

    assert len(rows) == 1
    assert rows[0]["settled_state"] == "budget_exhausted"
    assert rows[0][ACCOUNTING_KEY]["bounded_by"] == "wall"


def test_a_record_without_a_block_projects_no_key(isolate_agent_runtime_root):
    _settle(
        session_id="s-marker-none",
        client_message_id="client_marker_none",
        state="budget_exhausted",
        metadata={"provider_submitted": True, "budget_exhausted": True},
    )

    rows = _terminal_turn_marker_rows(
        session_id="s-marker-none", assistant_client_message_ids=set()
    )

    assert len(rows) == 1
    assert ACCOUNTING_KEY not in rows[0]


def test_the_projection_never_writes_the_journal_it_reads(isolate_agent_runtime_root):
    """Emit-path/read-only invariant. Projecting a page must not mutate the turn
    store — a projection that writes is how a read path starts inventing
    history."""

    from agent_runtime import paths

    _settle(
        session_id="s-readonly",
        client_message_id="client_readonly",
        state="native_committed",
        metadata={
            "native_committed": True,
            "stored_reply": "ok",
            **turn_run_budget_metadata(result=_untripped_result()),
        },
    )
    store_dir = paths.store_root() / "mission_chat_turns"
    before = {
        path.name: (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(store_dir.glob("*.json"))
    }
    assert before, "the turn journal wrote nothing to disk"

    _safe_recent_messages(
        _FakeSessionDB(
            [
                {
                    "role": "assistant",
                    "content": "ok",
                    "platform_message_id": "client_readonly",
                    "created_at": "2026-07-27T00:00:00Z",
                }
            ]
        ),
        session_id="s-readonly",
    )

    after = {
        path.name: (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(store_dir.glob("*.json"))
    }
    assert after == before


# ── the exec'd settle point, which no test can import ───────────────────────


def _mission_chat_message_func() -> ast.FunctionDef:
    """``harness_parts/persona_commands.py`` is ``exec``'d into harness.py's
    globals, never imported, so its body is only reachable as source text."""

    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef) and node.name == "_cmd_mission_chat_message":
            return node
    raise AssertionError("_cmd_mission_chat_message not found in persona_commands")


def _adapter_calls() -> list[ast.Call]:
    return [
        node
        for node in ast.walk(_mission_chat_message_func())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "turn_run_budget_metadata"
    ]


def test_both_settle_paths_go_through_the_one_adapter():
    """One from the completed run, one (at least) from the raised one.

    Hand-reaching into ``profile_timing`` or ``exc.run_budget`` at a settle site
    would be a second reader of the block's shape, which is the drift this whole
    module exists to prevent.
    """

    calls = _adapter_calls()
    keywords = sorted(kw.arg for call in calls for kw in call.keywords)

    assert "result" in keywords, "the completed-run settle point lost the block"
    assert keywords.count("error") >= 1, "the raised settle point lost the block"
    assert all(len(call.args) == 0 for call in calls), "the adapter is keyword-only"


def test_the_completed_settle_point_is_not_gated_on_a_budget_tripping():
    """``budget_metadata`` is spliced into the native-commit transition. The
    accounting entry must be built UNCONDITIONALLY — the checkpoint provenance
    beside it is the conditional half. Gating the block on ``budget_engaged``
    would restore the "only tripped turns are accounted" blindness."""

    assert not [
        node
        for node in ast.walk(_mission_chat_message_func())
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "budget_metadata"
            for target in node.targets
        )
    ], "budget_metadata lost its annotation — this guard reads the annotated form"

    assigns = [
        node
        for node in ast.walk(_mission_chat_message_func())
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "budget_metadata"
    ]
    assert len(assigns) == 1, "budget_metadata is no longer a single assignment"
    value = assigns[0].value
    assert isinstance(value, ast.Dict), "budget_metadata stopped being a dict literal"
    unconditional = [
        item
        for key, item in zip(value.keys, value.values)
        if key is None and not isinstance(item, ast.IfExp)
    ]
    assert any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == "turn_run_budget_metadata"
        for item in unconditional
    ), "the accounting block became conditional on the wall checkpoint engaging"


def test_the_adapter_is_imported_where_the_exec_can_see_it():
    """The trap this file's lane keeps re-learning: a name used in an exec'd
    command part resolves against harness.py's globals. A function-local import
    is the cheap, self-contained answer — and it has to actually be there."""

    func = _mission_chat_message_func()
    local_imports = {
        alias.name
        for node in ast.walk(func)
        if isinstance(node, ast.ImportFrom) and node.module == "agent_runtime.run_budget"
        for alias in node.names
    }
    assert "turn_run_budget_metadata" in local_imports


def test_the_checkpoint_reserve_seam_is_still_a_function(monkeypatch):
    """Sanity anchor for the runner harness above: the wall-checkpoint tests in
    this lane replace the reserve FUNCTION, never its math."""

    assert callable(turn_budget.checkpoint_reserve_seconds)
