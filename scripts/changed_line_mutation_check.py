"""Run bounded, explicit mutation claims that intersect changed production lines.

**This gate REWRITES SOURCE FILES IN PLACE while it runs.** Each mutant is
spliced into the real file on disk, the claim's test command runs against it,
and the original bytes are restored in a ``finally``. For the duration of a run
this worktree does not hold the code you committed.

Never run pytest — or anything else that reads the tree — in the same worktree
during a run. A concurrent test run reads sabotaged source and reds tests that
pass in isolation, and at the console that is indistinguishable from a real
defect: the H landing (2026-08-31) spent ten minutes on a phantom red before
the concurrency was identified as its cause. A second MUTATING run against the
same tree is refused outright (:func:`_acquire_gate_lock`); a concurrent pytest
is not something this script can see, which is why it is written down here as
well as enforced.

Registration is not coverage, and the report says so. This gate can only run
guarantees somebody wrote down, so a run with zero candidates has two readings —
the diff touched no production source, or it touched several and nobody
registered a claim for any of them. Those printed identically until 2026-09-04
(S5 landed in the second state and read like the first), so every run now
carries a ``changed production sources:`` census naming the changed ``.py``
files no claim anchors in. It is REPORTED, never enforced: whether a given file
must carry a claim is a policy question this script has no honest default for.

Budget doctrine (ruled 2026-09-04; replaces the candidate cap). The bound is
WALL CLOCK — ``--wall-budget-seconds`` — and the candidate count is REPORTED,
never asserted. The cap it replaces was always a proxy for runtime ("one
baseline plus one mutant test run per candidate"), and the proxy kept
mis-reading the thing it stood for: symbol-overlap selection RAISES candidate
pressure by design, measured on W1-H3's own diffs at 6 -> 27, 32 -> 64 and
98 -> 104 against a cap of 20, while a PR-shaped base and a push-shaped base
over the same work select wildly different counts (on a push the base is
``HEAD~1``, and a two-commit branch selects 2 and 2). None of that changes how
long the run takes, which is the only thing the bound was ever protecting.

The budget is checked before the mutating phase begins and again before each
claim, so a run that outgrows it STOPS with what it has done and what remains
— it does not discover the overrun by being killed. The enforced number stays
readable beside the command that enforces it: CI passes its own in
``.github/workflows/tests.yml``, and a multi-stage LANDING run passes its own
the same way. See ``tool/test_quality/README.md``.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLAIMS = REPO_ROOT / "tests" / "mutation_claims.json"
DEFAULT_EXEMPTIONS = REPO_ROOT / "tool" / "test_quality" / "mutation_exemptions.yaml"
#: Held for the duration of a MUTATING run — see :func:`_acquire_gate_lock`.
#: Worktree-local by construction (``REPO_ROOT`` is this checkout's root), which
#: is the right scope: the hazard is a shared TREE, and two worktrees of one
#: clone have two trees.
LOCK_PATH = REPO_ROOT / ".mutation_gate.lock"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

#: The default WALL-CLOCK bound on a run, in seconds. 900 = fifteen minutes,
#: which is the ceiling the CI job already enforces with its own
#: ``timeout-minutes: 15`` — so the default is the number the slowest lane was
#: living under anyway, said out loud where a local run can read it.
#:
#: Ruled 2026-09-04, the same ruling the launcher's discovered-extras count
#: took (``kRepoLaneWallBudgetSeconds``): a bound on a COUNT is a bound on a
#: proxy, and this one kept drifting from the thing it stood for. Symbol-overlap
#: selection raises the count by design and a push-shaped base collapses it, and
#: neither moves the runtime. The count is still printed on every run — it is
#: the most useful number in the report — it is simply never asserted.
DEFAULT_WALL_BUDGET_SECONDS = 900.0

#: ``symbol`` spellings that mean "the whole module" rather than a definition
#: inside it. Module-scope claims are legitimate (an import, a constant, a
#: decorator argument) and there is no AST node to scope them to.
#:
#: A FOURTH legitimate case, and the honest answer to the queue row that asked
#: for one: a definition inside a module-level platform fork.
#: ``agent_runtime/locks.py`` defines ``_try_acquire``/``_prepare``/
#: ``_release`` twice, once per arm of ``if os.name == "nt"``.
#: ``_qualified_definitions`` now descends into blocks (2026-09-02), so both
#: copies ARE reachable — and that does not help, exactly as this note predicted
#: before the descent existed: the two collide under one name and an ambiguous
#: symbol is refused by design. There IS no unambiguous node, so
#: ``hh6-posix-file-lock-ignores-its-deadline`` anchors at ``module`` and says
#: which arm it means in its label (``module/_try_acquire (POSIX arm)``). Its
#: selection is therefore line-based, which is what has been selecting and
#: killing it on `ubuntu-latest` all along; ``platforms`` is what keeps it from
#: reporting SURVIVED off POSIX.
WHOLE_MODULE_SYMBOLS = frozenset({"module", "module scope", "module-scope"})

#: Statements whose bodies the definition walk descends WITHOUT adding a name:
#: a block introduces nesting, not a naming scope. ``ExceptHandler`` and
#: ``match_case`` are in the list because they are the body-carrying children
#: of ``Try`` and ``Match`` rather than statements in their own right.
BLOCK_STATEMENTS: tuple[type[ast.AST], ...] = tuple(
    node
    for node in (
        ast.If,
        ast.Try,
        getattr(ast, "TryStar", None),
        ast.With,
        ast.AsyncWith,
        ast.For,
        ast.AsyncFor,
        ast.While,
        getattr(ast, "Match", None),
        getattr(ast, "match_case", None),
        ast.ExceptHandler,
    )
    if node is not None
)

#: The hosts a claim can declare itself bound to. Two, because two is what the
#: tree distinguishes: ``agent_runtime/locks.py`` is the only module with a
#: platform fork and it forks on ``os.name == "nt"``.
PLATFORMS = frozenset({"posix", "windows"})

#: How far a claim's block is allowed to have been re-indented and still be the
#: same claim. Four levels each way covers every dedent this repo has produced
#: (an extraction out of a nested ``try`` is two); beyond it the block has more
#: likely been rewritten than moved, and a configuration error is the honest
#: answer.
MAX_REINDENT_COLUMNS = 16

#: The optional claim field holding the commit a claim's needle was DERIVED at.
#:
#: What it is for. ``find`` is a source SPELLING inside the anchored symbol. The
#: loud failure — a spelling that stopped occurring — is a configuration error
#: and impossible to miss (S8b and the S5 landing both paid it). The quiet one
#: is the reason this field exists: a needle that STILL resolves after a
#: semantic edit runs a mutation nobody re-derived, and the run goes green on a
#: guarantee that may no longer be the guarantee.
#:
#: Ruled 2026-09-04: ONE optional field carrying the commit, NO backfill, and a
#: stale marker is a WARNING in the report and never a failure. All three halves
#: are load-bearing. No backfill, so ABSENCE means "written before this schema"
#: and says nothing about the claim's health — the 289 rows here at the ruling
#: are not silently asserted to be fresh. And a warning rather than a refusal,
#: because staleness is a suspicion, not a defect: a claim whose file moved
#: underneath it is usually still correct, and a gate that refuses on suspicion
#: is a gate that gets its budget raised until it says nothing.
DERIVED_AT_KEY = "derived_at"

#: Where ``_partition_claims`` parks the resolved :class:`ClaimAnchor` on the
#: claim row. Not a claim FIELD — the schema check above rejects unknown-shaped
#: rows on the way in, and this is added after that check, by us, on our copy.
ANCHOR_KEY = "_anchor"

#: Where ``_partition_claims`` parks WHY a claim was selected — ``"lines"``
#: (the diff touched the anchored needle) or ``"symbol"`` (it touched the
#: definition around it). Same non-field status as :data:`ANCHOR_KEY`. It is
#: reported rather than kept private: symbol selection is a deliberate
#: widening, and a widening nobody can see in the output is indistinguishable
#: from the gate having gone vague.
SELECTION_KEY = "_selected_by"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"{path} must contain an object")
    return value


#: Path prefixes whose ``.py`` files are not a surface a claim can anchor in.
#: Short on purpose: every OTHER root carrying Python is code a guarantee can
#: be registered against, and nine of the registered claims already anchor in
#: ``scripts/``. Widening this list makes the census below quieter by declaring
#: code un-mutatable, which is the move it exists to detect.
NON_PRODUCTION_PREFIXES = ("tests/", "tests-js/")


def _changed_sources(base: str) -> list[str]:
    """The production ``.py`` files this diff touched, sorted.

    Deletions are excluded (``--diff-filter=d``): a file that is gone cannot
    carry an anchor, so listing it as unregistered would ask for a claim nobody
    can write.
    """

    completed = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=d", base, "--", "*.py"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {completed.stderr.strip()}")
    return sorted(
        path
        for path in (row.strip().replace("\\", "/") for row in completed.stdout.splitlines())
        if path and not path.startswith(NON_PRODUCTION_PREFIXES)
    )


def _changed_lines(base: str, relative_path: str) -> set[int]:
    completed = subprocess.run(
        ["git", "diff", "--unified=0", base, "--", relative_path],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git diff failed for {relative_path}: {completed.stderr.strip()}")
    changed: set[int] = set()
    for row in completed.stdout.splitlines():
        match = HUNK.match(row)
        if not match:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or "1")
        if count == 0:
            # A DELETION-ONLY hunk (`@@ -32 +31,0 @@`): nothing was added, so
            # `range(31, 31)` is empty and this hunk used to contribute no
            # changed line at all. Every retirement wave therefore reported
            # `candidates: 0` and shipped with zero mutation coverage BY
            # CONSTRUCTION — measured on the Z1 landing (`4bf4387760`), filed
            # the same day.
            #
            # The two new-file lines the removed text sat between are what is
            # left of it, and they are what a claim anchored beside the
            # deletion overlaps. Paired with symbol-scoped selection below,
            # this is also how a deletion INSIDE a symbol reaches that symbol's
            # claims, which is the case the row actually cared about.
            changed.update({max(start, 1), start + 1})
            continue
        changed.update(range(start, start + count))
    return changed


def _validate_exemptions(path: Path) -> None:
    # JSON is valid YAML; keeping this dependency-free lets the selector run
    # before CI installs the repository environment.
    rows = _load_json(path).get("exemptions", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"{path}: exemptions must be a list")
    required = {"id", "path", "symbol", "operator", "reason", "owner", "issue", "expires"}
    allowed_reasons = {"equivalent", "observability-only", "generated", "contract-out-of-scope"}
    for row in rows:
        if not isinstance(row, dict) or not required.issubset(row):
            raise RuntimeError(f"{path}: every exemption needs {sorted(required)}")
        if row["reason"] not in allowed_reasons:
            raise RuntimeError(f"{path}: invalid reason for {row['id']}: {row['reason']}")
        try:
            expiry = date.fromisoformat(str(row["expires"]))
        except ValueError as error:
            raise RuntimeError(f"{path}: invalid expiry for {row['id']}") from error
        if expiry < date.today():
            raise RuntimeError(f"{path}: exemption expired: {row['id']} ({expiry})")


def _current_platform() -> str:
    return "windows" if os.name == "nt" else "posix"


def _declared_platforms(claim: dict[str, Any]) -> frozenset[str] | None:
    """The hosts this claim is about, or ``None`` for "every host".

    Optional and absent from 112 of 113 rows, which is the right default: a
    claim that does not say otherwise is a claim about behaviour, and
    behaviour is not supposed to have a platform.
    """

    raw = claim.get("platforms")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw or not all(isinstance(row, str) for row in raw):
        raise RuntimeError(
            f"{claim['id']}: platforms must be a non-empty list of strings"
        )
    unknown = sorted(set(raw) - PLATFORMS)
    if unknown:
        raise RuntimeError(
            f"{claim['id']}: unknown platforms {unknown}; known: {sorted(PLATFORMS)}"
        )
    return frozenset(raw)


def _qualified_definitions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Every definition in the module, keyed by its dotted qualified name.

    Values are LISTS because a name can legitimately occur more than once in one
    module (a method spelled on two classes, a constant re-bound under a
    ``try``). An anchor over an ambiguous name is refused rather than guessed —
    guessing is how a claim ends up mutating a line in a symbol it does not
    name, which is the whole defect this anchoring replaces.

    **A block is not a naming scope, so the walk descends through it** (2026-09-02).
    ``try:`` / ``if:`` / ``with:`` / ``for:`` / ``while:`` / ``match:`` bodies
    carry no name of their own, so a ``def`` inside one belongs to whatever
    ``def``/``class`` encloses the block: ``serve_loop._drain_monitor`` is the
    helper `serve.py` defines inside a ``try:`` inside ``serve_loop``. Before
    this it was reachable by NO symbol — the claim had to name the outer
    function and say which line it meant in prose
    (``serve_loop/_drain_monitor terminal``), which hands the ``find`` the outer
    function's whole span and every sibling copy of the line in it. Every
    block-nested helper in the repo was unanchorable the same way.

    Python's own ``__qualname__`` spells these with a ``<locals>`` segment; this
    does not, because a claim's ``symbol`` is a thing a human types.
    """

    found: dict[str, list[ast.AST]] = {}

    def record(name: str, node: ast.AST) -> None:
        found.setdefault(name, []).append(node)

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qualified = prefix + child.name
                record(qualified, child)
                walk(child, qualified + ".")
                continue
            # Module- and class-level bindings are anchorable too: ``r1-
            # discriminator-weakened-to-a-bare-marker`` anchors on the
            # ``DELIBERATE_PLACEMENT_SUFFIX`` constant, which has no def line.
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        record(prefix + target.id, child)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                record(prefix + child.target.id, child)
            elif isinstance(child, BLOCK_STATEMENTS):
                # Same prefix: the block adds nesting, not a name.
                walk(child, prefix)

    walk(tree, "")
    return found


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    for line in text.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _is_whole_module(symbol: str) -> bool:
    return symbol.split("/", 1)[0].strip().lower() in WHOLE_MODULE_SYMBOLS


def _symbol_span(text: str, path: str, symbol: str) -> tuple[int, int]:
    """The character span the claim's ``find`` is anchored INSIDE.

    ``symbol`` is ``<dotted definition>`` optionally followed by ``/<label>`` —
    the label is prose naming which line inside the definition the claim is
    about (``build_parser/work_list``) and is not resolved. The dotted half is
    resolved against the module's real definitions, and a bare method name is
    accepted when it is unambiguous, because that is how the claims that
    predate this anchoring are spelled.

    Until 2026-08-30 ``symbol`` was decorative: the needle was matched against
    the WHOLE FILE and nothing ever asked whether the named symbol existed.
    ``r1-create-stops-fencing-the-supplied-placement`` had named a deleted
    ``_parse_request`` for months while its guarantee was silently being
    exercised inside ``normalize_agent_create``.
    """

    if _is_whole_module(symbol):
        return 0, len(text)
    head = symbol.split("/", 1)[0].strip()
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        raise RuntimeError(
            f"symbol {head!r} cannot be resolved: {path} does not parse as Python "
            f"({error}); use symbol \"module\" for a file with no definitions"
        ) from error
    definitions = _qualified_definitions(tree)
    nodes = definitions.get(head, [])
    if not nodes:
        # A bare name, matched as a suffix of a qualified one: ``upsert_actor``
        # resolves to ``OfficeStore.upsert_actor`` when that is the only one.
        nodes = [
            node
            for qualified, candidates in definitions.items()
            if qualified.endswith("." + head)
            for node in candidates
        ]
    if not nodes:
        raise RuntimeError(f"symbol not found in {path}: {head}")
    if len(nodes) > 1:
        raise RuntimeError(
            f"symbol is ambiguous in {path}: {head} resolves {len(nodes)} times; "
            "qualify it (Class.method)"
        )
    node = nodes[0]
    offsets = _line_offsets(text)
    end_lineno = getattr(node, "end_lineno", None) or node.lineno
    return offsets[node.lineno - 1], offsets[min(end_lineno, len(offsets) - 1)]


def _reindent(block: str, shift: int, *, shift_first_line: bool) -> str | None:
    """``block`` with every line's own indentation moved by ``shift`` columns.

    RELATIVE indentation is preserved by construction — every line moves by the
    same amount — so this recognises a block that was dedented out of a ``try``
    or indented into one, and does NOT recognise a block whose internal nesting
    changed. That is the point: a re-indent is the same code in a new place, a
    re-nest is different code.

    ``None`` when the shift is not expressible: a tab-indented line (columns are
    not a fact about tabs) or a line that would need negative indentation.
    """

    out: list[str] = []
    for index, line in enumerate(block.split("\n")):
        if not line.strip():
            out.append(line)
            continue
        if index == 0 and not shift_first_line:
            out.append(line)
            continue
        stripped = line.lstrip(" \t")
        lead = line[: len(line) - len(stripped)]
        if "\t" in lead:
            return None
        width = len(lead) + shift
        if width < 0:
            return None
        out.append(" " * width + stripped)
    return "\n".join(out)


def _candidate_offsets(span: str, needle: str) -> dict[int, tuple[int, bool]]:
    """Every place in ``span`` the claim's block could be, as ``offset → (shift, at_line_start)``.

    TWO spellings, and the split between them is what keeps a re-indent from
    swallowing a re-NEST:

    * verbatim, and only at shift 0 — today's exact match, kept whole so a claim
      that still reads byte-for-byte is never re-interpreted. It may land
      mid-line, which is how a sub-line needle anchors at all.
    * line-start, at any shift — the whole block re-indented by one constant,
      matched only where a line begins. The first line moves WITH the rest,
      which is precisely what makes relative nesting a fixed property: a block
      whose inner line gained a level relative to its opener has no constant
      shift and drops out.

    Leaving the first line verbatim while shifting the others (the third
    spelling, which the first draft had) is the bug both of those avoid — it
    matches any re-nesting at all, and re-indents the replacement wrong.
    """

    found: dict[int, tuple[int, bool]] = {}
    exact = needle
    position = span.find(exact)
    while position != -1:
        found[position] = (0, False)
        position = span.find(exact, position + 1)
    if found:
        return found
    for size in range(1, MAX_REINDENT_COLUMNS + 1):
        for shift in (-size, size):
            moved = _reindent(needle, shift, shift_first_line=True)
            if moved is None:
                continue
            position = span.find(moved)
            while position != -1:
                if position == 0 or span[position - 1] == "\n":
                    found.setdefault(position, (shift, True))
                position = span.find(moved, position + 1)
        if found:
            # The smallest shift that explains the block wins; a larger one that
            # also matched would be a different block wearing the same shape.
            break
    return found


@dataclass(frozen=True, slots=True)
class ClaimAnchor:
    """Where a claim's ``find`` actually sits in the file today.

    ``find``/``replace`` are the claim's strings AS THE FILE SPELLS THEM — the
    registered text re-indented onto the block's current column. The mutation is
    spliced at ``offset`` rather than handed to ``str.replace``, because
    uniqueness is now a property of the SYMBOL and a needle that also occurs
    earlier in the file must not be the one that gets rewritten.

    ``symbol_lines`` is the whole span of the definition the claim names —
    what SELECTION reads. Empty for a ``module``-scope claim, where the span is
    the file and "this diff touched the file" is not a claim about anything.
    """

    offset: int
    lines: set[int]
    find: str
    replace: str
    shift: int
    symbol_lines: frozenset[int] = frozenset()


def _anchor_claim(text: str, claim: dict[str, Any]) -> ClaimAnchor:
    path = str(claim["path"])
    symbol = str(claim["symbol"])
    start, end = _symbol_span(text, path, symbol)
    span = text[start:end]
    needle = str(claim["find"])

    candidates = _candidate_offsets(span, needle)
    if not candidates:
        raise RuntimeError(
            f"mutation source not found in {path}::{claim['symbol']}"
            + _reanchor_hint(text, path, needle)
        )
    if len(candidates) > 1:
        raise RuntimeError(
            f"mutation source must occur exactly once in {path}::{claim['symbol']}; "
            f"found {len(candidates)}"
        )
    position, (shift, shift_first_line) = next(iter(candidates.items()))
    moved_find = _reindent(needle, shift, shift_first_line=shift_first_line)
    moved_replace = _reindent(str(claim["replace"]), shift, shift_first_line=shift_first_line)
    assert moved_find is not None
    if moved_replace is None:
        raise RuntimeError(
            f"{claim['id']}: the replacement cannot follow the anchor's {shift:+d}-column shift"
        )
    offset = start + position
    first = text.count("\n", 0, offset) + 1
    if _is_whole_module(symbol):
        symbol_lines: frozenset[int] = frozenset()
    else:
        symbol_lines = frozenset(
            range(text.count("\n", 0, start) + 1, text.count("\n", 0, end) + 2)
        )
    return ClaimAnchor(
        offset=offset,
        lines=set(range(first, first + moved_find.count("\n") + 1)),
        find=moved_find,
        replace=moved_replace,
        shift=shift,
        symbol_lines=symbol_lines,
    )


def _reanchor_hint(text: str, path: str, needle: str) -> str:
    """Name the symbol the needle DID land in, when the claim's symbol missed.

    A stale ``symbol`` is now fatal, so the error has to carry the repair: the
    two ways a claim goes stale are a rename and an extraction, and both leave
    the guarantee sitting in a symbol this can point at.
    """

    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ""
    offsets = _line_offsets(text)
    holders: list[str] = []
    for qualified, nodes in _qualified_definitions(tree).items():
        for node in nodes:
            # Definitions only. A binding that IS the needle would name itself
            # back at the reader, and "re-anchor onto holder.limit" is not a
            # repair anybody can act on.
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            end_lineno = getattr(node, "end_lineno", None) or node.lineno
            body = text[offsets[node.lineno - 1] : offsets[min(end_lineno, len(offsets) - 1)]]
            if _candidate_offsets(body, needle):
                holders.append(qualified)
    if not holders:
        return ""
    innermost = max(holders, key=lambda name: name.count("."))
    return f"; it is in {innermost} — re-anchor the claim's symbol"


def _read_source(target: Path) -> tuple[bytes, str]:
    """The file's raw bytes, and THE text every offset in this module means.

    One reader, because the two that existed disagreed about line endings and
    the disagreement was silent until it wasn't. ``_anchor_or_raise`` used
    ``read_text`` (universal newlines: a CRLF file decodes with LF) while the
    mutate loop used ``read_bytes().decode()`` (raw, CRLF kept), so against a
    CRLF-committed file every anchor offset was one byte short per preceding
    line and the splice refused with "changed after the anchor resolved" —
    measured red on pristine `main` (`0c744aa586`) on a Windows host, and
    reachable on Linux too, since 25 tracked `.py` blobs carried CRLF and a
    checkout of those is CRLF everywhere.

    LF is the normal form on both sides: claims register their ``find`` with
    LF, so anchoring a CRLF file at all requires it. The mutant is written LF
    and the original bytes are restored in ``finally`` regardless, so a
    deliberately-CRLF file is never left rewritten by a run.
    """

    raw = target.read_bytes()
    return raw, raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _anchor_or_raise(claim: dict[str, Any]) -> tuple[ClaimAnchor, str]:
    target = REPO_ROOT / str(claim["path"])
    if not target.is_file():
        raise RuntimeError(f"{claim['id']}: target missing: {claim['path']}")
    _, text = _read_source(target)
    try:
        return _anchor_claim(text, claim), text
    except RuntimeError as error:
        raise RuntimeError(f"{claim['id']}: {error}") from error


def _partition_claims(
    base: str, claims_path: Path
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Every claim, split into the ones this diff selects and the ones it does not.

    The unselected half is RETURNED rather than dropped on the floor. A claim
    whose ``find`` still resolves but whose line the diff never touched is not
    an error and must not be run — but it is also not nothing: it is a
    registered guarantee that this run did not exercise, and a reader who sees
    only the selected list cannot tell it apart from a claim that was never
    written. Measured on the S4 landing, where
    ``s4-a-pre-plan-done-receipt-re-enters-the-skills-phase`` anchors a line the
    slice did not change and therefore never appeared in any output at all.

    Selection is by SYMBOL, not by the anchor's own two lines. The measured
    miss (H-H2, `0ecb921b9d`): that landing rewrote 82 lines of
    ``agent_create.py``, 41 of them inside ``_reply``, and rendered the exact
    two lines ``hh2-the-one-reply-builder-stops-observing-the-revision``
    anchors on (1170-1171) as unchanged CONTEXT. The claim registered FOR that
    slice was not selected by its own landing diff, and the gate said so with a
    green run. A guarantee is about a symbol's behaviour, so a diff that
    rewrote the symbol has to put it on the hook whether or not the two lines
    carrying the needle happened to survive the rewrite verbatim.

    ``module``-scope claims keep line selection: their span is the whole file,
    and "this diff touched this file" would select them on every run — which is
    not a symbol claim, it is no claim at all.
    """

    rows = _load_json(claims_path).get("claims", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"{claims_path}: claims must be a list")
    selected: list[dict[str, Any]] = []
    unselected: list[dict[str, Any]] = []
    for claim in rows:
        if not isinstance(claim, dict):
            raise RuntimeError(f"{claims_path}: claim rows must be objects")
        required = {"id", "path", "symbol", "operator", "find", "replace", "test"}
        if not required.issubset(claim):
            raise RuntimeError(f"{claims_path}: claim needs {sorted(required)}")
        # Unknown fields are refused, not ignored. `platform: "posix"` for
        # `platforms: ["posix"]` is the typo this schema invites, and an
        # ignored field would mean the claim runs on every host while its
        # author believes it is scoped — a silent SURVIVED waiting to happen.
        unknown = sorted(set(claim) - required - {"platforms", DERIVED_AT_KEY})
        if unknown:
            raise RuntimeError(f"{claims_path}: {claim['id']}: unknown claim fields: {unknown}")
        _declared_platforms(claim)
        anchor, _ = _anchor_or_raise(claim)
        # Carried on the row so the mutate loop splices the anchor this
        # selection was computed from, rather than re-deriving one and hoping
        # the two agree.
        claim[ANCHOR_KEY] = anchor
        changed = _changed_lines(base, str(claim["path"]))
        if anchor.lines & changed:
            claim[SELECTION_KEY] = "lines"
            selected.append(claim)
        elif anchor.symbol_lines & changed:
            claim[SELECTION_KEY] = "symbol"
            selected.append(claim)
        else:
            unselected.append(claim)
    return selected, unselected


def _commits_since_derivation(claim: dict[str, Any]) -> int | None:
    """How many commits have touched this claim's file since it was derived.

    ``None`` when there is nothing to say: no :data:`DERIVED_AT_KEY` on the row
    (the pre-schema majority), or a commit git cannot resolve in this checkout —
    a shallow clone and a worktree that has not fetched the sha are both normal,
    and neither is a fact about the claim. A count of ``0`` is a real answer and
    a different one: derived here, nothing has moved.

    Deliberately counts COMMITS TO THE FILE and not to the symbol. A per-symbol
    read would need the anchor resolved at the old commit — a checkout per claim
    — to say something this report is not entitled to say anyway: the output is
    a prompt to a human re-derivation, and "this file has moved 14 times since
    anyone looked at this needle" is enough to prompt one.
    """

    derived_at = str(claim.get(DERIVED_AT_KEY, "") or "").strip()
    if not derived_at:
        return None
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"{derived_at}..HEAD", "--", str(claim["path"])],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _symbol_head(claim: dict[str, Any]) -> str:
    """The dotted half of ``symbol`` — the part that resolves to a definition."""

    return str(claim["symbol"]).split("/", 1)[0].strip()


def _path_matches(path: str, wanted: str) -> bool:
    """``wanted`` names ``path``: as itself, as a directory over it, or as a tail.

    The tail spelling (``agent_create.py`` for
    ``agent_runtime/agent_create.py``) is here because it is what a person
    about to rewrite a function actually types, and the alternative is a
    pre-flight nobody runs. Segment-aligned, so ``create.py`` does not match
    ``agent_create.py``.
    """

    if path == wanted:
        return True
    if path.startswith(wanted.rstrip("/") + "/"):
        return True
    return path.endswith("/" + wanted)


def _symbol_matches(head: str, wanted: str) -> bool:
    """``wanted`` names the symbol ``head``, its owner, or one of its members.

    Three directions on purpose: the exact name, the bare name of a qualified
    one (``upsert_actor`` for ``OfficeStore.upsert_actor``, which is how the
    older claims are spelled), and a class naming everything anchored inside
    it — "I am about to rewrite ``OfficeStore``" is a real question and the
    answer is every method's claims.
    """

    return head == wanted or head.endswith("." + wanted) or head.startswith(wanted + ".")


def _claims_for(claims_path: Path, query: str) -> int:
    """Print every claim anchored at ``query``. A REPORT, never a refusal.

    The gap this closes, measured on the 2026-08-30 lifecycle merge: two claims
    anchored inside one function's remediation string, the handoff named only
    one, and the second was found by the selector's configuration error rather
    than by review. "Which claims anchor in the symbol I am about to rewrite?"
    was answerable only by reading 113 rows by eye.

    Anchors are resolved but a failure to resolve is REPORTED, not raised: the
    moment this command is most useful is mid-rewrite, when some anchors have
    already stopped resolving, and a pre-flight that dies on the first rotted
    row would be useless exactly then.
    """

    rows = _load_json(claims_path).get("claims", [])
    if not isinstance(rows, list):
        raise RuntimeError(f"{claims_path}: claims must be a list")
    wanted_path, separator, wanted_symbol = query.replace("\\", "/").partition("::")
    matched: list[dict[str, Any]] = []
    for claim in rows:
        if not isinstance(claim, dict):
            raise RuntimeError(f"{claims_path}: claim rows must be objects")
        path = str(claim.get("path", "")).replace("\\", "/")
        head = _symbol_head(claim)
        if separator:
            if _path_matches(path, wanted_path) and _symbol_matches(head, wanted_symbol):
                matched.append(claim)
            continue
        if _path_matches(path, wanted_path) or _symbol_matches(head, wanted_path):
            matched.append(claim)

    print(f"claims anchored at {query}: {len(matched)}")
    for claim in matched:
        try:
            anchor, _ = _anchor_or_raise(claim)
            where = f"lines {min(anchor.lines)}-{max(anchor.lines)}"
            if anchor.shift:
                where += f", re-indented {anchor.shift:+d}"
        except RuntimeError as error:
            where = f"ANCHOR DOES NOT RESOLVE TODAY: {error}"
        print(f"  {claim['id']}: {claim['path']}::{claim['symbol']} [{claim['operator']}] {where}")
    if not matched:
        print("  (nothing anchored there — a rewrite here moves no registered guarantee)")
    return 0


def _command(claim: dict[str, Any]) -> list[str]:
    raw = claim["test"]
    if not isinstance(raw, list) or not raw or not all(isinstance(item, str) for item in raw):
        raise RuntimeError(f"{claim['id']}: test must be a non-empty string list")
    return [
        item.replace("{python}", sys.executable).replace("{repo}", str(REPO_ROOT))
        for item in raw
    ]


def _run_command(command: list[str]) -> int:
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def _acquire_gate_lock() -> bool:
    """Exclusive-create :data:`LOCK_PATH`; ``False`` when someone already holds it.

    Only MUTATING runs take it. ``--list`` and ``--claims-for`` are inventory
    questions that never touch a source file, so locking them would refuse a
    harmless read for a reason that does not apply to it.

    The create is the test: ``open(..., "x")`` is atomic, so two runs racing for
    the lock cannot both win. There is deliberately NO liveness probe on the
    recorded pid — ``os.kill(pid, 0)`` KILLS the process on Windows, and this
    gate's primary host is Windows. A stale lock is therefore cleared by hand,
    and the refusal prints the exact path to delete. That is the honest trade: a
    manual step after a crash, in exchange for never mistaking a live run for a
    dead one.
    """

    try:
        with open(LOCK_PATH, "x", encoding="utf-8") as stream:
            stream.write(
                f"pid: {os.getpid()}\n"
                f"started: {datetime.now(timezone.utc).isoformat()}\n"
                f"argv: {' '.join(sys.argv)}\n"
            )
    except FileExistsError:
        return False
    return True


def _refuse_because_locked() -> int:
    """Print who holds the lock, in a block that pastes cleanly, and exit 2."""

    try:
        held = LOCK_PATH.read_text(encoding="utf-8").strip()
    except Exception:  # noqa: BLE001 — the lock existing is the fact; its text is a courtesy
        held = "(the lock file exists but its contents could not be read)"
    print(
        "another mutation run appears active in this worktree; if none is "
        f"running, delete {LOCK_PATH} and retry\n"
        "\n```\n" + held + "\n```",
        file=sys.stderr,
    )
    return 2


def _over_budget(started: float, wall_budget_seconds: float) -> bool:
    """Has this run spent its wall-clock budget."""

    return (time.monotonic() - started) >= wall_budget_seconds


def _refuse_over_budget(
    started: float, wall_budget_seconds: float, done: int, total: int
) -> int:
    """Exit 2, naming the numbers and the cure — the cap refusal's whole lesson.

    An exit 2 that means "your change is big" used to read as "your claims are
    bad", the wrong signal on the wrong lane, measured on the H1-H4 landing. So
    this says what was spent, what the bound was, how far the run got, and both
    cures — and the ORDER matters: raising the budget is named first, because
    splitting the diff is not available to a landing whose whole argument is
    that its stages land together.
    """

    print(
        f"wall budget exhausted: {time.monotonic() - started:.0f}s of "
        f"--wall-budget-seconds {wall_budget_seconds:.0f}, after {done} of "
        f"{total} claim(s); raise the budget visibly (a multi-stage LANDING run "
        "passes its own, e.g. --wall-budget-seconds 1800), or split the diff",
        file=sys.stderr,
    )
    return 2


def run(
    base: str,
    claims_path: Path,
    exemptions_path: Path,
    wall_budget_seconds: float,
    list_only: bool,
) -> int:
    started = time.monotonic()
    _validate_exemptions(exemptions_path)
    claims, unselected = _partition_claims(base, claims_path)
    # REPORTED, never asserted (ruled 2026-09-04). The trailing parenthetical is
    # load-bearing in a way the number is not: CI's selector greps
    # `^mutation candidates: 0 ` — with the trailing space — to decide whether to
    # install the test environment at all, so this line keeps a token after the
    # count whatever the bound is called.
    print(
        f"mutation candidates: {len(claims)} "
        f"(reported, not capped; wall budget {wall_budget_seconds:.0f}s)"
    )
    for claim in claims:
        via = " (selected by symbol)" if claim.get(SELECTION_KEY) == "symbol" else ""
        print(
            f"  {claim['id']}: {claim['path']}::{claim['symbol']} "
            f"[{claim['operator']}]{via}"
        )
    # The census that makes a ZERO attributable. A registration-gated gate is
    # silent in two completely different situations — "this diff touched no
    # production source" and "this diff touched seven and nobody registered a
    # claim for any of them" — and until this line they printed the same
    # `mutation candidates: 0` and CI skipped identically. Measured on S5, which
    # landed with zero executable claims (`--base 748687daa3^ --list` -> 0) and
    # looked exactly like a slice that needed nothing.
    #
    # Reported, never enforced: whether a changed source MUST carry a claim is a
    # policy question with a real answer for some files and no answer for
    # others, and a gate that guesses it would be turned off inside a week. This
    # only refuses to let the two zeros look alike.
    changed_sources = _changed_sources(base)
    registered_paths = {
        str(claim["path"]).replace("\\", "/") for claim in (*claims, *unselected)
    }
    unregistered = [path for path in changed_sources if path not in registered_paths]
    print(
        f"changed production sources: {len(changed_sources)} "
        f"({len(unregistered)} carry no registered claim)"
    )
    for path in unregistered:
        print(f"  NO CLAIM ANCHORS HERE: {path}")
    # AFTER the candidate line for the same reason the UNSELECTED rows are, and
    # for BOTH halves: a claim re-anchored onto a re-indented block is a fact
    # about this run whether or not the diff selected it, and a silent
    # re-anchor is the same false all-clear as a silent skip.
    for claim in (*claims, *unselected):
        anchor = claim[ANCHOR_KEY]
        if anchor.shift:
            print(
                f"RE-ANCHORED: {claim['id']} ({claim['path']}::{claim['symbol']} "
                f"re-indented {anchor.shift:+d} columns)"
            )
    # The QUIET failure, made visible. A needle that stopped occurring is a
    # configuration error nobody can miss; a needle that still resolves after a
    # semantic edit runs a mutation nobody re-derived, and until this line the
    # run said nothing at all about that. Only for the SELECTED claims: this is
    # a prompt about work this run is actually doing.
    #
    # A WARNING and never a failure (ruled 2026-09-04), and printed on stdout
    # beside the rest of the report rather than on stderr, because it is not an
    # error channel — it is a line a reader of a GREEN run is meant to read.
    for claim in claims:
        moved = _commits_since_derivation(claim)
        if moved:
            print(
                f"WARNING: stale derivation: {claim['id']} was derived at "
                f"{str(claim[DERIVED_AT_KEY])[:12]} and {claim['path']} has moved "
                f"in {moved} commit(s) since; re-derive the needle or re-stamp "
                f"{DERIVED_AT_KEY}"
            )
    if list_only:
        # Only under ``--list``, which is the inventory lane. A real run prints
        # what it is about to mutate and nothing else; this is for the reader
        # asking "and what did this diff NOT put on the hook".
        #
        # AFTER the candidate line and never instead of it: CI branches on
        # ``^mutation candidates: 0 `` to decide whether to install the test
        # environment at all, so these rows are additive and that line keeps
        # its meaning.
        for claim in unselected:
            print(f"UNSELECTED (0 changed lines): {claim['id']}")
    # Reported for BOTH lanes and always by name, because the whole point is
    # that this claim is accounted for on a host that cannot run it. The
    # alternative is what the queue row measured: a hand-run on this host is
    # permanently one known SURVIVED away from green, which is exactly the
    # "silence looks like success" state the gate exists to prevent — except
    # here the noise looks like failure and gets learned as background.
    here = _current_platform()
    runnable: list[dict[str, Any]] = []
    for claim in claims:
        declared = _declared_platforms(claim)
        if declared is not None and here not in declared:
            print(
                f"SKIPPED (platform): {claim['id']} "
                f"(declared {'/'.join(sorted(declared))}; this host is {here})"
            )
        else:
            runnable.append(claim)
    if list_only or not runnable:
        return 0
    # The budget is checked HERE, before the lock, for the reason the cap was:
    # a refused run must hold nothing, or a run that stops for being too big
    # leaves a lock behind for the split-up runs that follow it. It is checked
    # after the ``--list`` return because the inventory lane runs no tests and
    # so has nothing to bound — under the cap, a big diff could not even ask
    # what it had selected, which is the one thing it needed to know.
    if _over_budget(started, wall_budget_seconds):
        return _refuse_over_budget(started, wall_budget_seconds, 0, len(runnable))

    # Everything past here READS OR WRITES the tree, so everything past here is
    # inside the lock — the baseline runs included. They do not mutate, but they
    # are part of the same run and a second run's mutants would corrupt them
    # exactly as they corrupt the mutation runs.
    if not _acquire_gate_lock():
        return _refuse_because_locked()
    try:
        commands: dict[tuple[str, ...], list[str]] = {}
        for claim in runnable:
            command = _command(claim)
            commands.setdefault(tuple(command), command)
        for command in commands.values():
            print(f"BASELINE: {' '.join(command)}")
            if _run_command(command) != 0:
                print("baseline failed; mutation result would be meaningless", file=sys.stderr)
                return 2

        survivors: list[str] = []
        for done, claim in enumerate(runnable):
            # Checked between claims and never inside one: a run that stopped
            # mid-mutation would be a run that left a spliced file on disk,
            # which is the one thing this gate may never do. So the bound is
            # honoured at the only safe boundary, and the overrun a single very
            # slow claim can cause is bounded by that claim, not by the budget.
            if _over_budget(started, wall_budget_seconds):
                return _refuse_over_budget(
                    started, wall_budget_seconds, done, len(runnable)
                )
            target = REPO_ROOT / str(claim["path"])
            original, text = _read_source(target)
            anchor = claim[ANCHOR_KEY]
            if text[anchor.offset : anchor.offset + len(anchor.find)] != anchor.find:
                # The baseline run moved the file under us. Refusing beats splicing
                # at an offset that now points somewhere else.
                raise RuntimeError(f"{claim['id']}: {claim['path']} changed after the anchor resolved")
            # Spliced at the anchor's offset, never ``str.replace``: uniqueness is a
            # property of the SYMBOL now, so an identical line earlier in the file
            # is legal — and would be the one a first-occurrence replace rewrote.
            mutated = text[: anchor.offset] + anchor.replace + text[anchor.offset + len(anchor.find) :]
            try:
                target.write_text(mutated, encoding="utf-8", newline="")
                print(f"MUTATE: {claim['id']}")
                if _run_command(_command(claim)) == 0:
                    survivors.append(str(claim["id"]))
                else:
                    print(f"KILLED: {claim['id']}")
            finally:
                target.write_bytes(original)
    finally:
        # Released on EVERY exit — including the RuntimeError above and a
        # KeyboardInterrupt — because a lock that outlives its run turns the
        # guard into the obstruction it exists to prevent.
        LOCK_PATH.unlink(missing_ok=True)
    if survivors:
        print(f"SURVIVED: {', '.join(survivors)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # Hand-wrapped, because RawDescriptionHelpFormatter is what keeps the
        # epilog's indented cap doctrine readable and it wraps nothing at all.
        description=(
            "Run bounded, explicit mutation claims that intersect changed\n"
            "production lines.\n"
            "\n"
            "THIS GATE REWRITES SOURCE FILES IN PLACE while it runs. Never run\n"
            "pytest -- or anything else that reads the tree -- in the same\n"
            "worktree during a run: a concurrent test run reads sabotaged source\n"
            "and reds tests that pass in isolation, which at the console is\n"
            "indistinguishable from a real defect. A second MUTATING run against\n"
            "the same tree is refused (.mutation_gate.lock); a concurrent pytest\n"
            "cannot be seen from here."
        ),
        epilog=(
            "budget doctrine (replaced the candidate cap, 2026-09-04):\n"
            "  the bound is WALL CLOCK. --wall-budget-seconds defaults to 900\n"
            "  (fifteen minutes, the ceiling CI's own job timeout already\n"
            "  enforced). The candidate COUNT is reported on every run and\n"
            "  never asserted: symbol-overlap selection raises it by design and\n"
            "  a push-shaped base collapses it, and neither moves the runtime\n"
            "  the bound exists to protect. A long multi-stage LANDING run\n"
            "  passes its own budget explicitly, the way CI does, so the\n"
            "  enforced number is readable beside the command enforcing it:\n"
            "    python scripts/changed_line_mutation_check.py --base <sha> --wall-budget-seconds 1800\n"
            "  See tool/test_quality/README.md."
        ),
    )
    # Not `required=True` any more: `--claims-for` is an inventory question
    # about the registry, and there is no base to diff against when the answer
    # is wanted BEFORE the rewrite that would produce one.
    parser.add_argument("--base")
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--exemptions", type=Path, default=DEFAULT_EXEMPTIONS)
    parser.add_argument(
        "--wall-budget-seconds",
        type=float,
        default=DEFAULT_WALL_BUDGET_SECONDS,
        help=(
            "stop the run when it has spent N seconds of wall clock (default: "
            "%(default)s). Checked before the mutating phase and between "
            "claims, so an overrun stops with a report rather than being "
            "killed. The candidate count is reported, never asserted."
        ),
    )
    parser.add_argument("--list", action="store_true", dest="list_only")
    parser.add_argument(
        "--claims-for",
        metavar="SYMBOL|PATH|PATH::SYMBOL",
        help="list the claims anchored there and exit; a pre-flight, not a gate",
    )
    args = parser.parse_args(argv)
    if args.claims_for is not None:
        try:
            return _claims_for(args.claims.resolve(), args.claims_for)
        except RuntimeError as error:
            print(f"mutation-check configuration error: {error}", file=sys.stderr)
            return 2
    if args.base is None:
        parser.error("--base is required unless --claims-for is given")
    try:
        return run(
            args.base,
            args.claims.resolve(),
            args.exemptions.resolve(),
            args.wall_budget_seconds,
            args.list_only,
        )
    except RuntimeError as error:
        print(f"mutation-check configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
