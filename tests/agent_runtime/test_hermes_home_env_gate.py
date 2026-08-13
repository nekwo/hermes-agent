"""Gate: only named authorities may read ``HERMES_HOME`` from the environment.

Why this exists
---------------

A wrong runtime root does not fail. It answers ``ok: true, count: 0`` — a
well-formed EMPTY result — so every layer above it reports success while
serving from the wrong store. Three separate 2026-08 incidents traced to that
one shape, including a fourth populated shadow runtime under ``%LOCALAPPDATA%``
that nothing on the machine pointed at.

The structural remedy is that root/scope is an INPUT, resolved once by an
authority and passed down, never re-derived by whoever needs it. A module that
reaches into ``os.environ`` for ``HERMES_HOME`` has re-derived it, and its
answer is free to disagree with the authority's — silently, because both
answers are well-formed.

This gate makes that re-derivation impossible to add by accident. It does NOT
police the resolved value; it polices who is allowed to ask the environment.

AST, not grep
-------------

An earlier gate in this repo matched the comments describing what it forbade,
so it went red on its own documentation and was weakened until it caught
nothing. This one parses the module and inspects call/subscript nodes, so
prose — including the paragraph above — is structurally invisible to it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The environment variables that name a runtime root.
GATED_NAMES = frozenset({"HERMES_HOME", "HERMES_HEAD_HOME"})

#: Fork-owned code only. Upstream (``gateway/``, ``tui_gateway/``, ``apps/``,
#: ``hermes_state.py``) is deliberately out of scope: this fork does not edit
#: it, so a gate over it could only ever be noise we learn to ignore.
SCANNED_ROOTS = (
    "agent",
    "agent_runtime",
    "hermes_cli/harness_parts",
)
SCANNED_FILES = (
    "tools/agent_chat_tool.py",
    "tools/mission_goal_tool.py",
)

#: module path -> why THIS module is allowed to ask the environment directly.
#:
#: Every entry is one of exactly two kinds: the authority that OWNS the
#: resolution, or a diagnostic that must report the raw environment precisely
#: BECAUSE it may disagree with the resolved value. Anything else — "it was
#: convenient here", "it is only a fallback" — is the defect this gate exists
#: to stop, and must take the root as an argument instead.
ALLOWED: dict[str, str] = {
    "agent_runtime/chat_session_scope.py": (
        "THE authority. Reading HERMES_HEAD_HOME *is* the ENV_HEAD_HOME rung "
        "of the resolution ladder this module owns."
    ),
    "agent_runtime/profile_context.py": (
        "The profile-binding chokepoint, and the only sanctioned WRITER: it "
        "rebinds HERMES_HOME process-globally when a profile is entered, and "
        "captures the prior value to restore it on exit."
    ),
    "agent_runtime/snapshot.py": (
        "Diagnostic. Reports the raw env under `env_HERMES_HOME` so an "
        "operator can see the environment DISAGREEING with the resolved root — "
        "which is the whole tell, and is lost if it reports the resolved value."
    ),
    "hermes_cli/harness_parts/runtime_commands.py": (
        "Diagnostic, same contract as snapshot.py: `hermes_home` in the "
        "runtime report is the raw environment, deliberately not the "
        "resolution."
    ),
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in SCANNED_ROOTS:
        files.extend(sorted((REPO_ROOT / root).rglob("*.py")))
    for name in SCANNED_FILES:
        path = REPO_ROOT / name
        if path.exists():
            files.append(path)
    return [f for f in files if "tests" not in f.parts]


def _is_process_environ(node: ast.expr) -> bool:
    """Is *node* the AMBIENT process environment (``os.environ``)?

    An INJECTED mapping is deliberately not matched. ``_hermes_home(env)`` and
    the codex spawn builder both call ``env.get("HERMES_HOME")`` on a mapping
    handed to them by their caller — which is the pattern this whole workstream
    is moving TOWARDS, not away from. Flagging those would teach the next
    reader that taking the environment as a parameter is the sin, when it is
    the remedy.

    Known limit, recorded rather than hidden: ``env = os.environ`` followed by
    ``env.get(...)`` reads as injected and passes. That is a deliberate act, not
    an accident, and this gate is aimed at accidents. Closing it needs dataflow,
    which would cost more clarity than the hole is worth.
    """

    if isinstance(node, ast.Attribute):
        return node.attr == "environ"
    return isinstance(node, ast.Name) and node.id == "environ"


def _gated_env_access(tree: ast.AST) -> list[int]:
    """Line numbers where this module asks the PROCESS environment for a root."""

    hits: list[int] = []

    def names_gated_constant(node: ast.expr | None) -> bool:
        return isinstance(node, ast.Constant) and node.value in GATED_NAMES

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            if not names_gated_constant(node.args[0]):
                continue
            func = node.func
            # os.getenv("HERMES_HOME") / getenv("HERMES_HOME")
            if isinstance(func, ast.Name) and func.id == "getenv":
                hits.append(node.lineno)
            elif isinstance(func, ast.Attribute):
                if func.attr == "getenv":
                    hits.append(node.lineno)
                # os.environ.get("HERMES_HOME") — receiver must BE the
                # process environment, not any mapping that has `.get`.
                elif func.attr == "get" and _is_process_environ(func.value):
                    hits.append(node.lineno)
        # os.environ["HERMES_HOME"], read or assigned.
        if isinstance(node, ast.Subscript) and names_gated_constant(node.slice):
            if _is_process_environ(node.value):
                hits.append(node.lineno)

    return sorted(set(hits))


@pytest.fixture(scope="module")
def scanned() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in _python_files():
        try:
            tree = ast.parse(path.read_bytes().decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - defensive
            continue
        hits = _gated_env_access(tree)
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_the_scan_finds_the_known_authorities(scanned: dict[str, list[int]]) -> None:
    """Anti-vacuity: a scan that finds NOTHING must fail, not pass.

    Every assertion below is of the form "no unexpected hits". A broken walker,
    a moved package, or a typo'd root would satisfy all of them by finding
    nothing at all — which is precisely how a gate stops working without
    anyone noticing. So first prove the scanner still sees the sites we KNOW
    are there.
    """

    assert scanned, (
        "the scan found no environment reads anywhere — the walker or "
        f"SCANNED_ROOTS is broken, not the codebase (roots: {SCANNED_ROOTS})"
    )
    assert "agent_runtime/chat_session_scope.py" in scanned, (
        "the resolution authority itself must still read HERMES_HEAD_HOME; if "
        "it genuinely stopped, this gate's premise changed and it needs "
        "rewriting rather than relaxing"
    )


def test_no_unsanctioned_root_reads(scanned: dict[str, list[int]]) -> None:
    offenders = {
        module: lines for module, lines in scanned.items() if module not in ALLOWED
    }
    assert not offenders, (
        "these modules re-derive the runtime root from the environment:\n"
        + "\n".join(
            f"  {module}:{','.join(str(line) for line in lines)}"
            for module, lines in sorted(offenders.items())
        )
        + "\n\nA wrong root returns a well-formed EMPTY answer, so a second "
        "opinion here does not fail loudly — it just serves the wrong store. "
        "Take the root/scope as an argument from the authority "
        "(agent_runtime.chat_session_scope, or the paths object the caller "
        "already holds).\n\nIf this module genuinely IS an authority or a "
        "raw-environment diagnostic, add it to ALLOWED with the reason — not "
        "a note that it is convenient."
    )


def test_allowlist_has_not_rotted(scanned: dict[str, list[int]]) -> None:
    """An allowlist entry that no longer matches anything is a lie.

    Stale exemptions are how a gate quietly turns into decoration: the module
    is refactored, the read disappears, the entry survives, and the next module
    added under that path inherits an exemption nobody granted it.
    """

    stale = sorted(set(ALLOWED) - set(scanned))
    assert not stale, (
        "these ALLOWED entries no longer read the environment — delete them:\n"
        + "\n".join(f"  {module}" for module in stale)
    )
