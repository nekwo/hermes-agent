"""Session-scoped I/O + parse cache for the source-walk gates.

WHAT THIS IS — and, more importantly, what it is not. The source-walk gates in
this directory (the S-wave removal gates, the stream-routing pin, the roster
bypass contract, the hermes-home env gate) each re-read and re-``ast.parse``
the same unchanged production tree inside every test: measured 2026-09-01,
that duplication was ~190 s of a serial ``tests/agent_runtime`` run, with
single files paying 11–28 s for six or seven tests that each repeat one
identical walk (``docs/agent-runtime-harness/planned/
hermes-suite-perf-field-notes-2026-09-01.md`` §6). The tree cannot change
mid-run, so the repetition buys nothing.

This module memoizes exactly three primitives — file text, the parsed AST of
that text, and a git query's output lines — for the life of the process.

It is NOT an enumeration authority. The vault's gate ruling ("enumerate from
the thing itself") stays where it was: every gate keeps its OWN walk, its own
glob/rglob/`git ls-files` spelling, its own skip-lists and its own
anti-vacuity assertions. A gate that used to call
``ast.parse(path.read_text(...))`` now calls :func:`parsed` with the same
path and the same decode-errors mode — same bytes, same tree, produced once
instead of once per test. Nothing here decides WHICH files a gate looks at.

Sharing rules the ported gates must honor (and currently do):

* Returned ASTs are shared objects — read them (``ast.walk``), never mutate
  or annotate them. A gate that needs to rewrite a tree (the tombstone
  registry's docstring-stripper) keeps its own parse and is deliberately NOT
  ported to this module.
* Only immutable-during-the-run paths belong here: production sources and
  git metadata of the checkout. Never route a tmp-dir path through this cache
  — a test that writes then re-reads a file would read the stale first
  version. (Per-test tmp paths are unique per test, so cross-test poisoning
  is impossible; the rule guards against SAME-test rewrites.)
* Parse/read failures are not cached (``lru_cache`` does not cache raised
  exceptions) — a gate's own per-file ``except SyntaxError`` handling behaves
  exactly as before.

Under ``scripts/run_tests_parallel.py`` each test file is its own process, so
this cache warms once per file — which is precisely the sharing that already
exists there; what it removes is the intra-file, per-test repetition, in
every runner.

MEMORY LIFETIME — the first design retained every AST for the life of the
process, and the full production tree parses to **785 MB of AST objects**
(measured 2026-09-01, 839 files). In a serial full-directory run that
ballast rode through thousands of later tests and the resulting GC/paging
pressure pushed a 13 s gate over the 30 s pytest-timeout ceiling — a crash
the 9-file verification run could not see. So the caches are now cleared at
test-MODULE boundaries by an autouse fixture in this directory's conftest:
within one gate module every test shares one parse (the entire win — the
duplication was per-test), across modules the tree is re-read (a bounded
~10 s per walking module, instead of 785 MB forever), and peak retention is
one module's walk set, held only while that module runs.
"""

from __future__ import annotations

import ast
import functools
import subprocess
from pathlib import Path

__all__ = ["text", "parsed", "git_lines", "clear"]


def clear() -> None:
    """Drop everything cached. Called at test-module teardown (see above)."""
    text.cache_clear()
    parsed.cache_clear()
    git_lines.cache_clear()


@functools.lru_cache(maxsize=None)
def text(path: str | Path, errors: str = "strict") -> str:
    """The file's text, read once per (path, errors) for the process."""
    return Path(path).read_text(encoding="utf-8", errors=errors)


@functools.lru_cache(maxsize=None)
def parsed(path: str | Path, errors: str = "strict") -> ast.Module:
    """The file's AST, parsed once per (path, errors) for the process.

    Shared object — callers read, never mutate. See module docstring.
    """
    return ast.parse(text(path, errors))


@functools.lru_cache(maxsize=None)
def git_lines(args: tuple[str, ...], cwd: str) -> tuple[str, ...]:
    """Stdout lines of ``git *args`` run in ``cwd``, executed once per query.

    Raises ``RuntimeError`` on a non-zero exit so a broken enumeration fails
    loudly at the caller — never an empty tuple standing in for an answer.
    Failures are not cached; a retry re-runs git.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr!r}"
        )
    return tuple(result.stdout.splitlines())
