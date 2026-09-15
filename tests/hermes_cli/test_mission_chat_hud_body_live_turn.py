"""The situational-HUD BODY reaches the provider on a REAL mission-chat turn.

Why this file exists
--------------------
``render_situational_hud_block`` is pinned at the unit level — presence and both
absence directions of the board digest live in
``tests/agent_runtime/test_board_agent_tools.py``. What was NOT pinned is the
hop that puts that body in front of an agent.

``persona_commands.py`` is an ``exec``'d command part (``harness._load_command_parts``),
not an importable module, so every guard over the turn body was reduced to AST
source-shape assertions: "``runtime_context_envelope`` is called on
``turn_context``", "no second assembly is resolved here". Those pin the SPELLING
of the composition and say nothing about the BYTES a turn feeds — they pass just
as happily against a lane where the rendered envelope is built and then dropped
on the floor before the provider call.

So this file drives the real lane. It stands up a workspace with real board
cards, runs ``harness._cmd_mission_chat_message`` through the exec'd body, and
reads the ``situational_hud_content`` the provider was ACTUALLY handed. An
import of ``render_situational_hud_block`` would prove nothing here: the runtime
never binds this code that way, and a mock-shaped pin on a lane the runtime does
not use is the defect these rows exist to catch.

The presence row would be vacuous on its own — a lane that always said
everything, or one that always said nothing, could satisfy a single assertion —
so the absence direction runs on the SAME live lane: with no open cards the
envelope still arrives and still carries the HUD body, and the board line is the
only thing missing from it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests.hermes_cli.test_mission_chat_budget_payload import (  # type: ignore
    _args,
    _seed,
    isolate_agent_runtime_root,  # noqa: F401  (re-exported fixture)
)


class _CapturingProvider:
    """A provider that records the kwargs the turn body handed it.

    Class-level storage because the turn constructs the runtime itself; the
    instance is never visible to the test.
    """

    seen: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    def mission_chat_reply(self, *args, **kwargs):
        type(self).seen = dict(kwargs)
        return SimpleNamespace(
            final_response="provider reply",
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
            latency_ms=4,
            profile_timing={},
            raw={},
        )


@pytest.fixture
def capturing_provider():
    _CapturingProvider.seen = {}
    yield _CapturingProvider
    _CapturingProvider.seen = {}


def _workspace(name: str = "Board Lane") -> str:
    from agent_runtime.store import WorkspaceStore

    store = WorkspaceStore()
    workspace = store.create(name=name)
    store.set_active(workspace.id)
    return workspace.id


def _run_turn(harness, client_message_id: str) -> str:
    """Drive one real turn and return the envelope the provider was fed."""

    harness._cmd_mission_chat_message(_args(client_message_id))
    assert "situational_hud_content" in _CapturingProvider.seen, (
        "the turn never reached the provider with a situational_hud_content "
        "kwarg: the rendered envelope no longer reaches the model call"
    )
    return _CapturingProvider.seen["situational_hud_content"] or ""


# --------------------------------------------------------------------------- #
# 1. Presence — a live turn on a workspace with open cards                     #
# --------------------------------------------------------------------------- #
def test_a_live_turn_feeds_the_hud_body_including_the_board_digest(
    monkeypatch, capsys, isolate_agent_runtime_root, capturing_provider
):
    """The whole join, end to end: real stores, the exec'd turn body, and the
    bytes the provider received."""

    from agent_runtime.board_store import BoardStore

    workspace_id = _workspace()
    store = BoardStore()
    store.add_card(workspace_id=workspace_id, title="Q1")
    store.add_card(workspace_id=workspace_id, title="Q2")

    harness = _seed(monkeypatch, capturing_provider)
    envelope = _run_turn(harness, "hud_body_turn")
    capsys.readouterr()

    # The envelope is a full snapshot on the first turn of a fresh lineage, so
    # the hashed body is inlined rather than stubbed to "unchanged".
    assert 'delivery="snapshot"' in envelope, envelope
    assert "## Runtime Situation" in envelope, envelope

    # ...and the digest body's own sentences are in it. These are the exact
    # strings test_board_agent_tools pins on the pure renderer; asserting them
    # HERE is what proves the renderer's output is what a turn actually feeds.
    assert "- Board: 2 queued" in envelope, envelope
    assert "MAY add a card" in envelope, envelope

    # The scope line names the live workspace, so the body was resolved from
    # this turn's real store state and not from a default or a stub.
    assert "Board Lane" in envelope, envelope


# --------------------------------------------------------------------------- #
# 2. Record-at-injection parity, on the live turn                              #
# --------------------------------------------------------------------------- #
def test_the_recorded_hud_is_the_one_the_live_turn_rendered(
    monkeypatch, capsys, isolate_agent_runtime_root, capturing_provider
):
    """The operator's CONTEXT peek and the agent's fed block are ONE object.

    The turn records the HUD dict at the injection site precisely so the peek
    shows what was injected rather than a later re-derivation. That promise is
    only meaningful if both halves are taken from one live turn — which is what
    this row does.
    """

    from agent_runtime.board_store import BoardStore
    from agent_runtime.prompt_observability import (
        load_latest_prompt_observability_contexts,
    )

    workspace_id = _workspace()
    store = BoardStore()
    for title in ("Q1", "Q2", "Q3"):
        store.add_card(workspace_id=workspace_id, title=title)

    harness = _seed(monkeypatch, capturing_provider)
    envelope = _run_turn(harness, "hud_parity_turn")
    capsys.readouterr()

    assert "- Board: 3 queued" in envelope, envelope

    rows = [
        row
        for row in load_latest_prompt_observability_contexts()
        if row.get("turn_id") == "hud_parity_turn"
    ]
    assert rows, "the live turn recorded no prompt-observability row to peek at"
    recorded = rows[-1].get("situational_hud") or {}
    assert recorded.get("board") == {"queued": 3, "active": 0, "review": 0}, recorded

    # The revision the envelope advertises is the one recorded beside the dict:
    # a peek reporting a different revision would be describing another turn.
    assert f'revision="{rows[-1].get("situational_hud_revision")}"' in envelope


# --------------------------------------------------------------------------- #
# 3. Absence — the same live lane, with nothing to say about a board           #
# --------------------------------------------------------------------------- #
def test_a_live_turn_with_no_open_cards_feeds_the_body_without_a_board_line(
    monkeypatch, capsys, isolate_agent_runtime_root, capturing_provider
):
    """The nudge is advisory, so "no open cards" must produce no board line —
    and this is also what keeps row 1 honest: the body is fed either way, and
    the digest is the only difference between them."""

    _workspace("Quiet Lane")  # a workspace, but no board and no cards

    harness = _seed(monkeypatch, capturing_provider)
    envelope = _run_turn(harness, "hud_no_board_turn")
    capsys.readouterr()

    assert "## Runtime Situation" in envelope, envelope
    assert "Quiet Lane" in envelope, envelope
    assert "- Board:" not in envelope, envelope
    assert "MAY add a card" not in envelope, envelope


# --------------------------------------------------------------------------- #
# 4. The lane driven above is the exec'd one                                   #
# --------------------------------------------------------------------------- #
def test_the_turn_body_driven_here_is_the_execd_command_part():
    """Stated as an assertion so this file cannot quietly decay into a unit test.

    ``persona_commands.py`` is ``exec``'d into ``hermes_cli.harness``'s module
    globals, which is exactly why an ``import`` of the turn body would pin a
    binding the runtime never uses. The observable consequence of the ``exec``
    is that the function's ``__globals__`` IS the harness module's namespace —
    so if that stops being true, the rows above are no longer driving the lane
    they claim to and should be reconsidered rather than left asserting.
    """

    from hermes_cli import harness

    turn = getattr(harness, "_cmd_mission_chat_message", None)
    assert turn is not None, "the mission-chat turn body is not in harness globals"
    assert turn.__globals__ is vars(harness), (
        "_cmd_mission_chat_message no longer shares hermes_cli.harness's "
        "globals: it is not being exec'd into them any more"
    )
