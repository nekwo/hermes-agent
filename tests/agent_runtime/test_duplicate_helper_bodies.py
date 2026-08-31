"""One authority per helper — a gate that can see a duplicate through its name.

WHY THIS FILE EXISTS
====================

Pass 2 of the dead-code audit (2026-08-19) found FOUR helper families copied
across ``agent_runtime/`` with byte-identical bodies, and every one of them had
survived earlier sweeps for the same reason: **nothing here reads bodies, and
the names disagreed.**

* ``_atomic_write_json`` (``serve_registry``) vs ``_write_json_atomic``
  (``serve_socket``) — the same three words in a different order.
* ``_rows`` in ``read_model`` / ``snapshot`` / ``stream`` — same name, three
  files, so a grep for the name looks like three legitimate private locals.
* ``_text`` (``stream``, ``runtime_hud``) vs ``_optional_text``
  (``read_model``) vs ``_norm`` (``workspace_scope``) — one function, four
  names. The audit predicted three; the AST found the fourth.
* ``_safe_path_token`` (``paths``) vs ``_safe_token`` (``realm_sync``,
  ``skill_promotion``) vs ``_artifact_token`` (``persona_profile_binding``,
  whose docstring says outright "Mirror of ``realm_sync._safe_token``") vs
  ``_safe_id`` (``office_store``, ``board_store``). Two of those were reached
  through FUNCTION-LOCAL imports, which no top-level import scan sees.

A duplicate is only invisible while it has nowhere obvious to live. The fold
gave the survivors homes — ``serde.py`` (a stdlib-only leaf: nothing in the
package can cycle through it) for wire-shape coercion and the atomic JSON
write, ``paths.py`` for the filesystem-name token. This gate is the half that
keeps them there.

WHAT IT ASSERTS
===============

For every MODULE-LEVEL function in ``agent_runtime/``: parse, strip the
docstring, normalize the function's own NAME away, re-render with
``ast.unparse``, hash. Two functions whose renderings collide are the same
function wearing two names, and the name is exactly what a reader was using to
tell them apart.

Docstrings are stripped for the tombstone registry's reason: a comment or a
docstring explaining that a copy is "byte-identical to X" must not be what
makes the gate pass or fail. ``persona_profile_binding._artifact_token``
documented its own duplication for months and no gate could read it.

Bodies under ``_MIN_BODY_LINES`` are exempt. A two-line ``return x or None``
collides by coincidence, not by copying, and a gate that fires on coincidence
gets muted.

THE BASELINE, AND WHY IT MAY ONLY SHRINK
========================================

``_GRANDFATHERED`` holds the groups that survived the fold, each with the
reason it was NOT folded in the same pass. It is a frozen baseline with two
properties that keep it from rotting into an allowlist:

1. **It may not grow.** A new duplicate fails the gate; adding a row is a
   deliberate visible edit with a reason, not a config tweak.
2. **A row that no longer duplicates fails too.** Fixing a group without
   deleting its row leaves a claim about the code that is no longer true — the
   silent-decay failure mode this repo has now been bitten by three times.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
from collections import defaultdict

HERMES_ROOT = pathlib.Path(__file__).resolve().parents[2]
PACKAGE = HERMES_ROOT / "agent_runtime"

#: Below this many RENDERED body lines, identical bodies are coincidence
#: rather than copying. Counted on ``ast.unparse`` output, not on statements:
#: a four-line ``try: path.unlink() / except OSError: pass`` is two statements
#: but is unmistakably a copied helper.
_MIN_BODY_LINES = 4

#: Groups that were duplicates at the 2026-08-19 fold and were deliberately
#: NOT folded, each with the reason. Keyed by the sorted ``file::name`` tuple
#: so a row names exactly which pair it excuses.
_GRANDFATHERED: dict[tuple[str, ...], str] = {
    (
        "agent_runtime/mcp_admission.py::_positive_int",
        "agent_runtime/profile_runner.py::_positive_int",
    ): (
        "a genuine duplicate, and foldable onto a serde helper — held back "
        "only to keep the fold commit reviewable. Note the tree ALSO carries "
        "four unrelated `_positive_int`s (config.py, kanban_db.py, "
        "kanban_diagnostics.py, browser_route.py) that take a `default` and "
        "are NOT this function, which is why the name alone never settled it"
    ),
    (
        "agent_runtime/persona_chat_history.py::_safe_trace_int",
        "agent_runtime/profile_runner.py::_safe_exit_code",
    ): (
        "same coercion (int-or-None, rejecting bool) under two domain names. "
        "Foldable, deferred with the pair above"
    ),
    (
        "agent_runtime/mission_chat_turns.py::_lock_fd_exclusive_nonblocking",
        "agent_runtime/persona_chat_continuity.py::_try_lock",
    ): (
        "REFUSED, not deferred. These are OS file-lock primitives and their "
        "home would be `locks.py`; one of the two files is on the audit's "
        "exclusion list, and a careless fold of lock acquire/release is how "
        "you ship a deadlock that only appears under contention. Wants its "
        "own stage with concurrency proof, not a ride on a rename commit"
    ),
    (
        "agent_runtime/mission_chat_turns.py::_unlock_fd",
        "agent_runtime/persona_chat_continuity.py::_unlock",
    ): "the release half of the pair above; same refusal",
    # ``board_content_hash == office_content_hash`` was grandfathered here at
    # this gate's first run and its row is GONE, per this file's own rule that a
    # pair which stops being duplicate loses its row in the same commit. They
    # stopped being duplicate for a reason, not by a rename: H-H12 gave the
    # office hash a second filter (``_ITEM_HASH_EXCLUDE``) over an actor's
    # ``items``, which a board row does not have. That is also the answer to the
    # row's own deferral — it wanted "its own stage with a golden-compare" before
    # folding the two onto one ``content_hash(payload)``, and the fold is now
    # further away rather than nearer, because the office lane has an exclusion
    # rule the board lane must not inherit silently.
    (
        "agent_runtime/default_scope.py::_get_realm",
        "agent_runtime/default_scope.py::_get_workspace",
    ): (
        "KEPT, with a reason. Two typed wrappers over one body in the SAME "
        "file: folding them to a generic would hand thirteen call sites an "
        "untyped return where they currently get `Realm | None` / "
        "`Workspace | None`. A TypeVar-parameterized `_get_unarchived` would "
        "preserve both, and is the right shape if this is ever revisited — "
        "but that is a design change, not a deduplication"
    ),
    (
        "agent_runtime/mcp_lane.py::_clean",
        "agent_runtime/tool_visibility.py::_clean_names",
    ): (
        "NEW at this gate's first run. Same normalize-a-name-list body under "
        "a generic name and a specific one; deferred with the pair above"
    ),
}


def _normalize_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
    """Erase everything a reader could rename without changing the function.

    The NAME first — that is what hid every family this gate was written for.
    But also the PARAMETER names and every annotation: ``read_model._rows``
    and ``snapshot._rows`` had identical bodies and differed only in that one
    of them annotated its argument ``Any`` and the other left it bare, and
    ``paths._safe_path_token`` vs ``realm_sync._safe_token`` differed only by
    ``str`` vs ``str | None``. A gate that hashes the signature verbatim
    would have missed both — which is to say, would have missed the two
    families the audit actually named.

    Defaults are NOT erased: a different default is a different function.
    """

    node.name = "_"
    node.returns = None
    args = node.args
    every = [
        *args.posonlyargs,
        *args.args,
        *([args.vararg] if args.vararg else []),
        *args.kwonlyargs,
        *([args.kwarg] if args.kwarg else []),
    ]
    mapping: dict[str, str] = {}
    for index, arg in enumerate(every):
        mapping[arg.arg] = f"a{index}"
        arg.arg = mapping[arg.arg]
        arg.annotation = None
    # ...and the parameter's USES in the body. Without this the gate is
    # defeated by `def f(raw)` vs `def f(value)`, which is a rename a copier
    # makes without thinking. Other locals are deliberately left alone: full
    # alpha-renaming would start matching functions that merely share a shape.
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in mapping:
            child.id = mapping[child.id]
        elif isinstance(child, ast.arg) and child.arg in mapping:
            child.arg = mapping[child.arg]
    ast.fix_missing_locations(node)


def _module_level_functions() -> dict[str, list[tuple[str, str]]]:
    """``body hash -> [(relative path, function name)]`` for agent_runtime."""

    groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
    files = sorted(PACKAGE.rglob("*.py"))
    assert len(files) > 50, (
        f"the duplicate scan found only {len(files)} files under {PACKAGE} — "
        "the gate would be vacuous"
    )
    for path in files:
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - a broken file is another gate's job
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            clone = ast.parse(ast.unparse(node)).body[0]
            body = clone.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                clone.body = body[1:]
            rendered = "\n".join(ast.unparse(stmt) for stmt in clone.body)
            if len(rendered.splitlines()) < _MIN_BODY_LINES:
                continue
            _normalize_signature(clone)
            digest = hashlib.sha256(ast.unparse(clone).encode()).hexdigest()[:12]
            relative = path.relative_to(HERMES_ROOT).as_posix()
            groups[digest].append((relative, node.name))
    return groups


def _duplicate_keys() -> set[tuple[str, ...]]:
    return {
        tuple(sorted(f"{path}::{name}" for path, name in members))
        for members in _module_level_functions().values()
        if len(members) > 1
    }


def test_no_new_duplicate_helper_bodies_in_agent_runtime():
    """Two module-level functions with the same body are one function."""

    unexpected = sorted(_duplicate_keys() - set(_GRANDFATHERED))
    assert unexpected == [], (
        "these agent_runtime functions have byte-identical bodies (docstrings "
        "stripped, names normalized) — fold them onto ONE authority, or add a "
        "row to _GRANDFATHERED saying why not:\n"
        + "\n".join("  " + " == ".join(group) for group in unexpected)
    )


def test_the_grandfathered_baseline_still_describes_the_code():
    """A fixed group must LOSE its row, so the baseline can only shrink.

    Without this half, folding a grandfathered pair leaves a frozen claim that
    the code contradicts, and the next reader trusts it.
    """

    live = _duplicate_keys()
    stale = sorted(group for group in _GRANDFATHERED if group not in live)
    assert stale == [], (
        "these _GRANDFATHERED groups are no longer duplicates — delete their "
        "rows in the same commit that folded them:\n"
        + "\n".join("  " + " == ".join(group) for group in stale)
    )
