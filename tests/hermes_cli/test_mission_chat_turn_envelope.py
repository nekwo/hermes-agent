"""The mission-chat turn is PLANNED once, then COMMITTED once, under the lease.

``_cmd_mission_chat_message`` used to re-enter ITSELF to take the chat-root
lease::

    if not args._persona_chat_root_lease_acquired:
        with persona_chat_root_lease(session_id, ...):
            args._persona_chat_root_lease_acquired = True
            return _cmd_mission_chat_message(args)      # <-- the whole body, again

Three consequences, all of them defects and all of them fixed by the split:

1. **Every durable write before that line ran TWICE per turn** — ``open_chat``,
   the chat-session ensure, and the model-override persist — the first time
   with no lease held at all.
2. **The turn's phase state had to be smuggled across the re-entry** on
   ``args._stated_session_id`` / ``args._clarify_binding`` /
   ``args._dispatch_session_established``, because a second pass would otherwise
   rediscover "the caller named a session" and report every freshly minted
   dispatch thread as a plain continuation — and settle a clarify ticket twice.
3. **Post-reply bookkeeping failed silently.** The instance-state commit that
   returns the agent to ``idle`` sat in a bare ``except Exception: pass``, and
   eight ``transition_mission_chat_turn`` calls discarded their persist outcome.
   A cockpit could render an agent ``busy`` forever with no record of why.

These rows pin the fixed shape by BEHAVIOUR (which writes ran while the lease
was held) and, where only the source can say it, by AST.
"""

from __future__ import annotations

import ast
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_runtime.mission_chat_outcome import FinalizationWarningKind

from tests.hermes_cli.test_mission_chat_budget_payload import (  # type: ignore
    _args,
    _seed,
    _TranscriptDB,
    isolate_agent_runtime_root,  # noqa: F401  (re-exported fixture)
)


class _ReplyProvider:
    def __init__(self, *args, **kwargs):
        pass

    def mission_chat_reply(self, *args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            final_response="provider reply",
            input_tokens=1,
            output_tokens=2,
            total_tokens=3,
            latency_ms=4,
            profile_timing={},
            raw={},
        )


def _persona_commands_tree() -> ast.Module:
    import hermes_cli.harness as harness

    path = Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(name: str) -> ast.FunctionDef:
    for node in _persona_commands_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in persona_commands")


# --------------------------------------------------------------------------- #
# 1. The recursion is gone                                                     #
# --------------------------------------------------------------------------- #
def test_the_turn_handler_no_longer_re_enters_itself():
    """A self-call is the defect, not a style preference: it re-ran three
    durable writes and forced the phase-state smuggling below."""

    func = _func("_cmd_mission_chat_message")
    self_calls = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_cmd_mission_chat_message"
    ]
    assert not self_calls, (
        "_cmd_mission_chat_message calls itself again; the lease re-entry is "
        "back and with it the double writes"
    )


def test_the_handler_hands_off_to_exactly_one_committer():
    func = _func("_cmd_mission_chat_message")
    commits = [
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_mission_chat_commit_turn"
    ]
    assert len(commits) == 1, (
        "the plan phase must reach the sole writer through exactly one call site"
    )
    _func("_mission_chat_commit_turn")  # ...and that writer must exist


def test_no_turn_phase_state_is_smuggled_on_the_args_namespace():
    """``args._*`` existed ONLY to survive the re-entry. With one pass, a local
    is the honest carrier — and an attribute on the caller's namespace is a
    write to somebody else's object that outlives the turn."""

    offenders: list[str] = []
    for name in ("_cmd_mission_chat_message", "_mission_chat_commit_turn"):
        for node in ast.walk(_func(name)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name)
                and node.value.id == "args"
                and node.attr.startswith("_")
            ):
                offenders.append(f"{name}: args.{node.attr} @ L{node.lineno}")
    assert not offenders, (
        f"turn phase state is being stashed back onto args: {sorted(offenders)}"
    )


# --------------------------------------------------------------------------- #
# 2. The lease actually covers the writes                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def lease_witness(monkeypatch, isolate_agent_runtime_root):  # noqa: F811
    """Record, for each durable write, whether the chat-root lease was held."""

    from agent_runtime.persona_assignments import PersonaInstanceStore
    from hermes_cli import harness

    harness_module = _seed(monkeypatch, _ReplyProvider)
    depth: list[int] = []
    seen: dict[str, bool] = {}

    real_lease = harness.persona_chat_root_lease

    @contextmanager
    def _tracking_lease(session_id, **kwargs):
        depth.append(1)
        try:
            with real_lease(session_id, **kwargs):
                yield
        finally:
            depth.pop()

    monkeypatch.setattr(harness, "persona_chat_root_lease", _tracking_lease)

    def _witness(name, target, attr):
        original = getattr(target, attr)

        def _wrapped(*args, **kwargs):
            seen[name] = bool(depth)
            return original(*args, **kwargs)

        monkeypatch.setattr(target, attr, _wrapped)

    _witness("derive_from_workers", PersonaInstanceStore, "derive_from_workers")
    _witness("open_chat", PersonaInstanceStore, "open_chat")
    _witness("ensure_chat_session", harness, "_ensure_persona_chat_session")
    _witness("model_override", harness, "_resolve_chat_model_override")
    return harness_module, seen


def test_the_writes_that_can_be_leased_are(lease_witness, capsys):
    """``open_chat``, the session ensure and the model-override persist all
    happen INSIDE the lease now. Before the split all three ran outside it —
    and then again inside it, after the re-entry."""

    harness, seen = lease_witness
    assert harness._cmd_mission_chat_message(_args("lease_turn")) == 0
    capsys.readouterr()

    assert seen.get("open_chat") is True
    assert seen.get("ensure_chat_session") is True
    assert seen.get("model_override") is True


def test_the_two_writes_that_cannot_be_leased_are_named_not_hidden(
    lease_witness, capsys
):
    """Honest about the half that is NOT done.

    Two durable writes still precede the lease and cannot simply move inside it:
    the instance derive-from-workers that target resolution reads, and — on an
    omitted-session send — the mint that CREATES the session id the lease is
    keyed on. There is nothing to lock before a thread exists. Retiring these
    two is the remaining half of the plan/commit proposal; this row states the
    current truth so nobody has to re-derive it, and fails if it silently
    changes in either direction.
    """

    harness, seen = lease_witness
    assert harness._cmd_mission_chat_message(_args("lease_turn")) == 0
    capsys.readouterr()

    assert seen.get("derive_from_workers") is False


def test_each_leasable_write_happens_exactly_once_per_turn(
    monkeypatch, capsys, isolate_agent_runtime_root  # noqa: F811
):
    """The re-entry ran them twice. Count them."""

    from agent_runtime.persona_assignments import PersonaInstanceStore
    from hermes_cli import harness

    harness_module = _seed(monkeypatch, _ReplyProvider)
    calls: dict[str, int] = {}

    def _count(name, target, attr):
        original = getattr(target, attr)

        def _wrapped(*args, **kwargs):
            calls[name] = calls.get(name, 0) + 1
            return original(*args, **kwargs)

        monkeypatch.setattr(target, attr, _wrapped)

    _count("open_chat", PersonaInstanceStore, "open_chat")
    _count("ensure_chat_session", harness, "_ensure_persona_chat_session")
    _count("model_override", harness, "_resolve_chat_model_override")

    assert harness_module._cmd_mission_chat_message(_args("once_turn")) == 0
    capsys.readouterr()

    assert calls == {"open_chat": 1, "ensure_chat_session": 1, "model_override": 1}


# --------------------------------------------------------------------------- #
# 3. Finalization accounting                                                   #
# --------------------------------------------------------------------------- #
def test_a_clean_turn_carries_no_finalization_warnings(
    monkeypatch, capsys, isolate_agent_runtime_root  # noqa: F811
):
    """The key is ABSENT when nothing went wrong — its presence is the signal,
    and its absence keeps the wire byte-identical for every healthy send."""

    harness = _seed(monkeypatch, _ReplyProvider)
    assert harness._cmd_mission_chat_message(_args("clean_turn")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "finalization_warnings" not in payload


def test_a_failed_instance_state_commit_is_reported_not_swallowed(
    monkeypatch, capsys, isolate_agent_runtime_root  # noqa: F811
):
    """The worst of the silent-swallow cluster.

    This write returns the agent to ``idle`` and repoints its default chat
    thread. It lived inside ``except Exception: pass``, so a cockpit showing an
    agent stuck ``busy`` after a completed turn had no record anywhere of why.
    It still must not fail the turn — the reply is durable — so it surfaces as a
    typed warning on an otherwise-``ok`` envelope.
    """

    from agent_runtime.persona_assignments import PersonaInstanceStore

    state = {"armed": False}

    class _ArmingProvider(_ReplyProvider):
        def mission_chat_reply(self, *args, **kwargs):
            result = super().mission_chat_reply(*args, **kwargs)
            # Arm only the POST-reply write. Every earlier instance write on the
            # turn (the open_chat bind, the skill-manifest stamp) must still
            # succeed, or this row would be testing a different failure.
            state["armed"] = True
            return result

    harness = _seed(monkeypatch, _ArmingProvider)
    real_update = PersonaInstanceStore.update

    def _update(self, instance, *args, **kwargs):
        if state["armed"]:
            raise OSError("instance row is unwritable")
        return real_update(self, instance, *args, **kwargs)

    monkeypatch.setattr(PersonaInstanceStore, "update", _update)

    assert harness._cmd_mission_chat_message(_args("swallow_turn")) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True, "a bookkeeping failure must not fail the turn"
    warnings = payload["finalization_warnings"]
    assert len(warnings) == 1
    assert warnings[0]["kind"] == FinalizationWarningKind.INSTANCE_STATE_COMMIT_FAILED
    assert warnings[0]["step"] == "return_instance_to_idle"
    assert warnings[0]["detail"] == "OSError"


def test_a_non_clean_turn_journal_write_is_accounted_for(
    monkeypatch, capsys, isolate_agent_runtime_root  # noqa: F811
):
    """Eight ``transition_mission_chat_turn`` calls discarded their outcome.

    A skipped or rejected journal write (lock timeout, stale transition,
    invalid state) was indistinguishable from a clean one. Now every one names
    the step it failed on.
    """

    from agent_runtime.mission_chat_turns import MissionChatTurnPersistOutcome
    from hermes_cli import harness as harness_module

    harness = _seed(monkeypatch, _ReplyProvider)
    real = harness_module.transition_mission_chat_turn

    def _flaky(*args, **kwargs):
        outcome = real(*args, **kwargs)
        if kwargs.get("state") == harness_module.TURN_STATE_NATIVE_COMMITTED:
            return MissionChatTurnPersistOutcome.SKIPPED_LOCK_TIMEOUT
        return outcome

    monkeypatch.setattr(harness_module, "transition_mission_chat_turn", _flaky)

    assert harness._cmd_mission_chat_message(_args("journal_turn")) == 0
    payload = json.loads(capsys.readouterr().out)

    warnings = payload["finalization_warnings"]
    assert [warning["kind"] for warning in warnings] == [
        FinalizationWarningKind.TURN_RECORD_NOT_PERSISTED
    ]
    assert warnings[0]["step"] == "native_commit"
    assert warnings[0]["detail"] == "skipped_lock_timeout"


def test_every_discarded_turn_journal_write_is_routed():
    """No ``transition_mission_chat_turn`` call may drop its outcome again."""

    func = _func("_mission_chat_commit_turn")
    dropped = [
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", None) == "transition_mission_chat_turn"
    ]
    assert not dropped, (
        "turn-journal transition(s) discard their MissionChatTurnPersistOutcome "
        f"at line(s) {dropped}; route them through _route_turn_write"
    )


@pytest.mark.parametrize("kind", list(FinalizationWarningKind))
def test_every_finalization_warning_kind_is_producible(kind):
    """The enum is not a wishlist: each kind has a real producer."""

    import hermes_cli.harness as harness

    source = (
        Path(harness.__file__).with_name("harness_parts") / "persona_commands.py"
    ).read_text(encoding="utf-8")
    assert f"FinalizationWarningKind.{kind.name}" in source, (
        f"{kind.name} has no producer in the CLI lane"
    )
