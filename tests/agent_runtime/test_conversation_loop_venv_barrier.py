"""The venv-install barrier is armed at the TURN LOOP, so every lane is covered.

``ProfileAgentRunner.run`` arms the barrier for the harness lanes (operator
mission chat, mission-run workers, dispatch) — see
``tests/agent_runtime/test_turn_venv_install_barrier.py``. The CLI-interactive
lane (``hermes chat`` → ``HermesCLI.chat`` → ``AIAgent.run_conversation``) never
routes through that runner, so it ran a live turn with lazy installs still
permitted: exactly the shape of the 2026-08-09 incident, where a mid-turn
``pip install`` of a provider SDK rewrote the interpreter the turn was running
in and took Mission Control down.

``agent.conversation_loop.run_conversation`` is the one function every lane
reaches (verified: ``AIAgent.run_conversation`` is its only non-test caller, and
every lane goes through that forwarder), so arming there arms everywhere. These
tests pin the arming, the reason-inheritance rule, the release, and the
structural invariant that no second entry point can slip past the wrapper.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import conversation_loop
from tools import lazy_deps


@pytest.fixture(autouse=True)
def _barrier_is_disarmed():
    """Fail loudly if a leaked scope from another test poisons these pins."""

    assert lazy_deps.venv_install_denial() is None
    yield
    assert lazy_deps.venv_install_denial() is None


def _observing_loop(observed: dict):
    """Stand-in turn body that records the barrier state it ran under."""

    def _loop(agent, user_message, **kwargs):
        observed["denial"] = lazy_deps.venv_install_denial()
        observed["mutation_denial"] = lazy_deps.venv_mutation_denial()
        observed["kwargs"] = kwargs
        return {"final_response": "ok"}

    return _loop


def test_turn_loop_runs_with_venv_installs_denied(monkeypatch):
    observed: dict = {}
    monkeypatch.setattr(
        conversation_loop, "_run_conversation", _observing_loop(observed)
    )

    result = conversation_loop.run_conversation(object(), "hello")

    assert result == {"final_response": "ok"}
    assert observed["denial"] == "an agent turn (conversation loop)"
    # The enforcement answer, not just the raw barrier state: an install
    # attempted here would actually be refused.
    assert observed["mutation_denial"] is not None
    assert "must never mutate" in observed["mutation_denial"]


def test_outer_reason_is_inherited_rather_than_masked(monkeypatch):
    """The harness lane's profile-named reason must survive the inner scope.

    The barrier reports the INNERMOST reason, so a generic inner scope would
    otherwise erase ``profile='base'`` from the operator-facing refusal.
    """

    observed: dict = {}
    monkeypatch.setattr(
        conversation_loop, "_run_conversation", _observing_loop(observed)
    )

    with lazy_deps.deny_venv_installs("an agent turn (profile='base')"):
        conversation_loop.run_conversation(object(), "hello")

    assert observed["denial"] == "an agent turn (profile='base')"


def test_outer_scope_survives_the_inner_release(monkeypatch):
    """Inheriting the same string must not pop the OUTER scope on exit."""

    monkeypatch.setattr(
        conversation_loop, "_run_conversation", _observing_loop({})
    )

    with lazy_deps.deny_venv_installs("an agent turn (profile='base')"):
        conversation_loop.run_conversation(object(), "hello")
        assert lazy_deps.venv_install_denial() == "an agent turn (profile='base')"


def test_barrier_released_after_a_normal_turn(monkeypatch):
    monkeypatch.setattr(
        conversation_loop, "_run_conversation", _observing_loop({})
    )

    conversation_loop.run_conversation(object(), "hello")

    assert lazy_deps.venv_install_denial() is None


def test_barrier_released_when_the_turn_raises(monkeypatch):
    def _boom(agent, user_message, **kwargs):
        raise RuntimeError("turn exploded")

    monkeypatch.setattr(conversation_loop, "_run_conversation", _boom)

    with pytest.raises(RuntimeError, match="turn exploded"):
        conversation_loop.run_conversation(object(), "hello")

    assert lazy_deps.venv_install_denial() is None


def test_every_turn_argument_is_forwarded_unchanged(monkeypatch):
    """The wrapper must not quietly drop a turn parameter.

    A forwarder written by hand is exactly where a parameter goes missing, and
    a dropped ``moa_config`` / ``persist_user_*`` would be invisible until a
    specific lane misbehaved in production.
    """

    observed: dict = {}
    monkeypatch.setattr(
        conversation_loop, "_run_conversation", _observing_loop(observed)
    )

    sentinel = {name: object() for name in (
        "system_message",
        "conversation_history",
        "task_id",
        "stream_callback",
        "persist_user_message",
        "persist_user_timestamp",
        "persist_user_display_kind",
        "persist_user_display_metadata",
        "moa_config",
        "reuse_current_user_message",
    )}
    conversation_loop.run_conversation(object(), "hello", **sentinel)

    assert observed["kwargs"] == sentinel


def test_wrapper_and_body_signatures_stay_in_lockstep():
    """A parameter added to one side must not silently stop reaching the other.

    Read off the REAL functions (no monkeypatch in this test), otherwise the
    stand-in's ``**kwargs`` would make the comparison vacuous.
    """

    public = inspect.signature(conversation_loop.run_conversation)
    private = inspect.signature(conversation_loop._run_conversation)
    assert list(public.parameters) == list(private.parameters)
    assert [p.default for p in public.parameters.values()] == [
        p.default for p in private.parameters.values()
    ]


def test_operator_installs_still_work_outside_a_turn(monkeypatch):
    """The barrier is per-TURN, not per-process and not per-session.

    ``hermes tools`` / ``hermes setup`` / ``hermes doctor --fix`` /
    ``hermes postinstall`` are top-level commands with no turn on the stack, so
    they must reach the real installer. This pin goes red if the barrier is
    ever armed at import, at session start, or left armed after a turn — the
    three wrong ways to "cover every lane".
    """

    monkeypatch.setattr(
        conversation_loop, "_run_conversation", _observing_loop({})
    )
    calls: list[tuple] = []

    def _fake_pip(specs, *args, **kwargs):
        calls.append((tuple(specs), kwargs))
        return SimpleNamespace(success=True, stdout="installed", stderr="")

    monkeypatch.setattr(lazy_deps, "_venv_pip_install", _fake_pip)
    # Isolate the barrier axis: the config gate is a separate, already-tested
    # refusal and must not be what makes this pin pass.
    monkeypatch.setattr(lazy_deps, "_allow_lazy_installs", lambda: True)

    # Before any turn.
    assert lazy_deps.venv_mutation_denial() is None
    before = lazy_deps.install_specs(["some-package==1.0"])
    assert before.blocked is False
    assert len(calls) == 1, "the operator install reached the real installer"

    # A turn runs and finishes.
    conversation_loop.run_conversation(object(), "hello")

    # After the turn the operator lane is open again.
    assert lazy_deps.venv_mutation_denial() is None
    after = lazy_deps.install_specs(["some-package==1.0"])
    assert after.blocked is False
    assert len(calls) == 2, "the post-turn operator install reached it too"

    # …and the same call DURING a turn is refused, so the pin above is not
    # simply proving the installer is unreachable in tests.
    with lazy_deps.deny_venv_installs("an agent turn (probe)"):
        during = lazy_deps.install_specs(["some-package==1.0"])
    assert during.blocked is True
    assert len(calls) == 2, "no pip ran while a turn was live"


def test_the_cli_lane_reaches_this_wrapper():
    """Structural proof that arming here covers ``hermes chat``.

    The CLI-interactive lane is ``HermesCLI.chat`` → ``AIAgent.run_conversation``
    → ``agent.conversation_loop.run_conversation``. The middle hop is the claim
    that makes this file's arming site sufficient, so it is asserted rather
    than assumed — via AST, so a mention inside a docstring or comment cannot
    satisfy it.
    """

    import run_agent

    tree = ast.parse(Path(run_agent.__file__).read_text(encoding="utf-8"))
    forwarders = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_conversation"
    ]
    assert forwarders, "AIAgent.run_conversation not found in run_agent.py"

    imports_the_loop = any(
        isinstance(inner, ast.ImportFrom)
        and inner.module == "agent.conversation_loop"
        and any(alias.name == "run_conversation" for alias in inner.names)
        for node in forwarders
        for inner in ast.walk(node)
    )
    calls_the_loop = any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id == "run_conversation"
        for node in forwarders
        for inner in ast.walk(node)
    )
    assert imports_the_loop and calls_the_loop, (
        "AIAgent.run_conversation must delegate to "
        "agent.conversation_loop.run_conversation — otherwise the CLI lane is "
        "no longer behind the turn barrier"
    )


def test_the_loop_body_has_exactly_one_caller_and_it_is_the_wrapper():
    """Structural pin: no second entry point may bypass the barrier.

    Arming a wrapper only holds while the wrapper is the sole door. This walks
    the module's AST (not a grep, so a name inside a string or comment cannot
    satisfy it) and asserts every ``_run_conversation`` call sits inside
    ``run_conversation``.
    """

    source = Path(conversation_loop.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    callers: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "_run_conversation"
            ):
                callers.append(node.name)

    assert callers == ["run_conversation"], (
        "_run_conversation must be reachable only through the barrier wrapper; "
        f"found callers: {callers}"
    )
