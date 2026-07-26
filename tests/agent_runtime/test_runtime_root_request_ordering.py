"""Audit Q6 — a request's outcome must not depend on what ran before it.

Seven harness command handlers used to open with::

    os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", str(paths.store_root()))

and the rest — including ``_cmd_mission_chat_message``, the primary work lane —
did not. ``hermes harness serve`` re-dispatches every request through those same
handlers in ONE process, so whether the variable was set when a turn ran was a
function of **request history**. Same lane, same role, same request, different
ambient environment, decided by ancestry.

``setdefault`` at a call site is what you write when there is no entry-point
seam. The audit's ruling picks the stronger of the two available seams: **no
handler seeds it at all, and every reader resolves through
``paths.store_root()``** — one ladder (env → ``agent_runtime.store_root`` in the
root config → platform default), traced, always answering.

Two tests, because the property has two halves:

* :func:`test_a_seeding_handler_no_longer_changes_a_later_requests_outcome` —
  the behavioral half, and the direct executable statement of the audit: run a
  former SEEDER and then observe, in one process, and assert the observation is
  byte-identical to the same observation made alone in a FRESH process.
* :func:`test_no_module_seeds_the_runtime_root_variable` — the structural half.
  A source scan cannot be satisfied by a mock and needs no harness in the loop
  (the ``test_store_event_invariant.py`` /
  ``test_serve_cwd_serialization_invariant.py`` precedent). It is what keeps the
  next handler from quietly re-introducing the ancestry.

Full reader-by-reader classification:
``docs/agent-runtime-harness/env-determinism-audit.md``.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

RUNTIME_ROOT_ENV = "HERMES_AGENT_RUNTIME_ROOT"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: The one sanctioned way to write this variable: a context manager that
#: snapshots the prior value and restores it, so a request cannot leak into the
#: next one. Anything else is the bug this test exists to prevent.
SCOPED_WRITERS = {
    Path("agent_runtime/profile_context.py"),
    Path("agent_runtime/smoke.py"),
}


def _observe() -> dict:
    """Everything a request can observe about "which runtime root am I on?".

    Deliberately the union of the readers this audit inventoried, so the
    comparison below fails if ANY of them regains an ancestry dependence — not
    just the one that happened to be broken.
    """

    from agent_runtime import paths, smoke, terminal_envelope

    return {
        "store_root": str(paths.store_root()),
        "audit_root": str(terminal_envelope._audit_root(None)),
        "audit_root_source": terminal_envelope.audit_root_source(None),
        "smoke_root": str(smoke._configured_smoke_root()),
        "env_is_set": bool(os.environ.get(RUNTIME_ROOT_ENV, "").strip()),
    }


def _observe_in_a_fresh_process(env: dict[str, str]) -> dict:
    """Run :func:`_observe` in a brand-new interpreter under ``env``.

    Injects the function's own source rather than re-spelling it, so the two
    sides of the comparison cannot drift apart.
    """

    script = "\n".join(
        [
            "import json, os, sys",
            f"sys.path.insert(0, {str(REPO_ROOT)!r})",
            f"RUNTIME_ROOT_ENV = {RUNTIME_ROOT_ENV!r}",
            textwrap.dedent(inspect.getsource(_observe)),
            "print('<<<' + json.dumps(_observe()) + '>>>')",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    payload = completed.stdout.split("<<<", 1)[1].split(">>>", 1)[0]
    return json.loads(payload)


def _clean_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.pop(RUNTIME_ROOT_ENV, None)
    env["HERMES_HOME"] = str(home)
    return env


def test_a_seeding_handler_no_longer_changes_a_later_requests_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request-ordering test the audit demands, on a REAL handler.

    ``_cmd_goal_run`` is one of the seven former seeders (§2 #13). It is invoked
    here with a malformed ``--bind`` so it fails fast — but AFTER
    ``load_agent_runtime_config()``, i.e. past the exact point where the
    ``setdefault`` used to fire. Pre-Q6 this left ``HERMES_AGENT_RUNTIME_ROOT``
    exported for the rest of the process; every later request in that ``serve``
    then resolved through the env rung instead of its own.
    """

    home = tmp_path / "hermes-home"
    home.mkdir(parents=True)
    monkeypatch.delenv(RUNTIME_ROOT_ENV, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(home))

    fresh = _observe_in_a_fresh_process(_clean_env(home))

    import hermes_cli.harness as harness

    assert harness._cmd_goal_run(types.SimpleNamespace(bind=["bogus"], json=True)) == 2
    after_a_seeder = _observe()

    assert after_a_seeder == fresh
    # And the specific mechanism is gone, not merely compensated for.
    assert after_a_seeder["env_is_set"] is False
    assert RUNTIME_ROOT_ENV not in os.environ


def test_the_former_seeder_still_reaches_the_point_it_used_to_seed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard the guard: an ordering test that never runs the seeder proves nothing.

    ``_cmd_goal_run`` must still execute ``load_agent_runtime_config()`` — the
    line the ``setdefault`` sat directly after — before failing. If the handler
    is restructured so the malformed-bind path returns earlier, this fails and
    routes the reader here instead of silently hollowing out the test above.
    """

    import hermes_cli.harness as harness

    source = inspect.getsource(harness._cmd_goal_run)
    body = ast.parse(textwrap.dedent(source)).body[0].body
    first = ast.dump(body[0])
    assert "load_agent_runtime_config" in first
    assert isinstance(body[1], ast.Try)


def _seeding_calls(tree: ast.AST) -> list[ast.Call]:
    """``os.environ.setdefault("HERMES_AGENT_RUNTIME_ROOT", ...)`` calls."""

    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "setdefault":
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value == RUNTIME_ROOT_ENV:
            found.append(node)
    return found


def _unscoped_assignments(tree: ast.AST) -> list[ast.AST]:
    """``os.environ["HERMES_AGENT_RUNTIME_ROOT"] = ...`` assignments."""

    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Subscript):
                continue
            key = target.slice
            if isinstance(key, ast.Constant) and key.value == RUNTIME_ROOT_ENV:
                found.append(node)
    return found


def _python_sources() -> list[Path]:
    roots = [REPO_ROOT / "agent_runtime", REPO_ROOT / "hermes_cli"]
    return [path for root in roots for path in sorted(root.rglob("*.py"))]


def test_no_module_seeds_the_runtime_root_variable() -> None:
    """No handler may re-introduce the ancestry. The structural half of Q6."""

    offenders: list[str] = []
    for path in _python_sources():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable source
            continue
        for call in _seeding_calls(tree):
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{call.lineno}")

    assert offenders == [], (
        "A handler seeds HERMES_AGENT_RUNTIME_ROOT again, which makes a request's "
        "runtime root a function of what ran before it in the same `harness serve` "
        "process. Resolve it through agent_runtime.paths.store_root() instead. See "
        "docs/agent-runtime-harness/env-determinism-audit.md Q6. Offenders: "
        + ", ".join(offenders)
    )


def test_the_only_writers_are_the_two_save_and_restore_context_managers() -> None:
    """Writing the variable is fine — LEAKING it is not.

    ``persona_profile_context`` and the smoke root both snapshot the prior value
    and restore it in ``finally``, so a request cannot leak into the next one.
    A bare assignment anywhere else is the ``setdefault`` bug wearing a
    different hat, and would not even be caught by the test above.
    """

    offenders: list[str] = []
    for path in _python_sources():
        relative = path.relative_to(REPO_ROOT)
        if relative in SCOPED_WRITERS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable source
            continue
        for node in _unscoped_assignments(tree):
            offenders.append(f"{relative.as_posix()}:{node.lineno}")

    assert offenders == [], (
        "HERMES_AGENT_RUNTIME_ROOT is assigned outside a save/restore context "
        "manager, so it leaks into every later request in a `harness serve` "
        "process. See docs/agent-runtime-harness/env-determinism-audit.md Q6. "
        "Offenders: " + ", ".join(offenders)
    )


def test_the_sanctioned_writers_still_exist_and_still_restore() -> None:
    """The allowlist above must never become a stale exemption."""

    for relative in SCOPED_WRITERS:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert f'os.environ[{RUNTIME_ROOT_ENV!r}]' in source or (
            f'os.environ["{RUNTIME_ROOT_ENV}"]' in source
        ) or "RUNTIME_ROOT_ENV" in source
        assert "finally:" in source
