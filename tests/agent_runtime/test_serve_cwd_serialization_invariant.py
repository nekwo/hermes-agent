"""Wave-3 — the serve-cwd serialization invariant, made structural.

**The invariant.** Mission-chat workdir grounding
(``agent_runtime/mission_chat_workdir.py`` → ``AgentRunRequest.workdir`` →
``agent_runtime/profile_runner.py::_agent_workdir``) grounds a turn by calling
``os.chdir`` — which mutates **process-global** state. A ``hermes harness
serve`` process handles every request in one process, so one turn's grounding
is visible to any other turn running at the same time. That is safe today for
exactly one reason: **harness turns serialize**, because
``_execute_agent_run`` holds ``_WORKDIR_LOCK`` for the WHOLE run and
``_agent_workdir`` re-enters it around the ``chdir`` pair.

Nothing else enforces it. Remove the lock from either site and the code still
imports, still passes every functional test, and quietly becomes wrong the
first time two turns overlap — turn A's relative paths resolve into turn B's
repo, non-deterministically, with no error anywhere. That failure mode is
invisible to unit tests by construction, so it gets a structural guard
instead.

This test locks three things:

1. Every ``os.chdir`` in ``profile_runner`` is lexically inside a ``with``
   that holds ``_WORKDIR_LOCK``.
2. ``_execute_agent_run`` holds ``_WORKDIR_LOCK`` for the whole run — not just
   the instant of the ``chdir``. Guarding only the call would still let a
   second turn chdir away *mid-run*, which is the actual hazard.
3. ``_WORKDIR_LOCK`` is REENTRANT. The two guards nest; a plain ``Lock``
   deadlocks the runner on the first grounded turn.

Widening turn concurrency therefore starts by failing this test — which is the
point. The doc section that must be satisfied first:
``docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/env-determinism-audit.md`` § "The serve-cwd
concurrency invariant". The fix shape recorded there is per-run resolved
absolute paths handed to the tools instead of a process-global ``chdir``; this
test is the tripwire that makes anyone widening concurrency read it.

AST-based, mirroring the ``test_store_event_invariant`` precedent: a source
scan cannot be satisfied by a mock and does not need the harness in the loop.
"""

from __future__ import annotations

import ast
import threading
from pathlib import Path

import agent_runtime.profile_runner as profile_runner

#: The lock that serializes process-global cwd mutation.
LOCK_NAME = "_WORKDIR_LOCK"
#: The run chokepoint that must hold it for the full turn.
RUN_CHOKEPOINT = "ProfileAgentRunner._execute_agent_run"


def _module_tree() -> ast.Module:
    source = Path(profile_runner.__file__).read_text(encoding="utf-8")
    return ast.parse(source)


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_chdir(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "chdir"
    if isinstance(func, ast.Name):
        return func.id == "chdir"
    return False


def _holds_lock(with_node: ast.With | ast.AsyncWith) -> bool:
    """Does this ``with`` acquire ``_WORKDIR_LOCK`` among its items?"""

    for item in with_node.items:
        for sub in ast.walk(item.context_expr):
            if isinstance(sub, ast.Name) and sub.id == LOCK_NAME:
                return True
            if isinstance(sub, ast.Attribute) and sub.attr == LOCK_NAME:
                return True
    return False


def _guarded_by_lock(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.With, ast.AsyncWith)) and _holds_lock(current):
            return True
        current = parents.get(current)
    return False


def _enclosing_qualname(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current: ast.AST | None = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)) or "<module>"


def test_every_chdir_in_profile_runner_is_guarded_by_the_workdir_lock() -> None:
    """A new unguarded ``os.chdir`` fails CI instead of silently racing."""

    tree = _module_tree()
    parents = _parents(tree)

    chdir_calls = [node for node in ast.walk(tree) if _is_chdir(node)]
    # If this drops to zero the grounding mechanism changed shape entirely —
    # re-read the doc section before deleting this test.
    assert chdir_calls, (
        "No os.chdir found in profile_runner. If process-global chdir was "
        "replaced (e.g. by per-run resolved paths), update "
        "docs/agent-runtime-harness/archive/2026-08-22-pre-consolidation/env-determinism-audit.md and this test "
        "together — do not delete the guard silently."
    )

    unguarded = [
        f"{Path(profile_runner.__file__).name}:{node.lineno} "
        f"in {_enclosing_qualname(node, parents)}"
        for node in chdir_calls
        if not _guarded_by_lock(node, parents)
    ]
    assert not unguarded, (
        "os.chdir mutates the SERVE PROCESS working directory. Every call must "
        f"be inside a `with {LOCK_NAME}` block, or two concurrent turns will "
        "ground each other's relative paths. Unguarded:\n  "
        + "\n  ".join(unguarded)
    )


def test_the_run_chokepoint_holds_the_workdir_lock_for_the_whole_run() -> None:
    """Guarding the chdir instant is not enough — the RUN must be serialized.

    ``_agent_workdir`` holds the lock across its own chdir pair, but that alone
    would still allow a second turn to acquire it and chdir away while the
    first turn's agent is mid-execution. The whole-run acquisition in
    ``_execute_agent_run`` is what actually makes the process-global cwd a
    per-turn value.
    """

    tree = _module_tree()
    parents = _parents(tree)

    targets = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and f"{_enclosing_qualname(node, parents)}.{node.name}".endswith(RUN_CHOKEPOINT)
    ]
    assert len(targets) == 1, (
        f"Expected exactly one {RUN_CHOKEPOINT}; found {len(targets)}. The run "
        "chokepoint moved — re-point this invariant at the new one."
    )

    run = targets[0]
    holds = any(
        isinstance(node, (ast.With, ast.AsyncWith)) and _holds_lock(node)
        for node in ast.walk(run)
    )
    assert holds, (
        f"{RUN_CHOKEPOINT} no longer acquires {LOCK_NAME}. Turn serialization "
        "is the ONLY reason a process-global os.chdir is safe here; dropping "
        "it silently enables concurrent turns to fight over the serve "
        "process's working directory."
    )


def test_the_workdir_lock_is_reentrant() -> None:
    """``_execute_agent_run`` and ``_agent_workdir`` both take it, nested.

    A plain ``threading.Lock`` satisfies both AST checks above and deadlocks
    the runner on the first grounded turn, so the reentrancy is part of the
    invariant, not an implementation detail.
    """

    lock = getattr(profile_runner, LOCK_NAME)
    assert isinstance(lock, type(threading.RLock())), (
        f"{LOCK_NAME} must be a reentrant lock (threading.RLock): "
        "_execute_agent_run acquires it for the run and _agent_workdir "
        f"re-acquires it around the chdir pair. Found {type(lock)!r}."
    )
    # Prove reentrancy rather than trusting the type name.
    assert lock.acquire(blocking=False)
    try:
        assert lock.acquire(blocking=False), "lock did not re-enter"
        lock.release()
    finally:
        lock.release()
