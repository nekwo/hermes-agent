"""Run bounded, explicit mutation claims that intersect changed production lines."""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from datetime import date
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CLAIMS = REPO_ROOT / "tests" / "mutation_claims.json"
DEFAULT_EXEMPTIONS = REPO_ROOT / "tool" / "test_quality" / "mutation_exemptions.yaml"
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

#: ``symbol`` spellings that mean "the whole module" rather than a definition
#: inside it. Module-scope claims are legitimate (an import, a constant, a
#: decorator argument) and there is no AST node to scope them to.
WHOLE_MODULE_SYMBOLS = frozenset({"module", "module scope", "module-scope"})

#: How far a claim's block is allowed to have been re-indented and still be the
#: same claim. Four levels each way covers every dedent this repo has produced
#: (an extraction out of a nested ``try`` is two); beyond it the block has more
#: likely been rewritten than moved, and a configuration error is the honest
#: answer.
MAX_REINDENT_COLUMNS = 16

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


def _qualified_definitions(tree: ast.Module) -> dict[str, list[ast.AST]]:
    """Every definition in the module, keyed by its dotted qualified name.

    Values are LISTS because a name can legitimately occur more than once in one
    module (a method spelled on two classes, a constant re-bound under a
    ``try``). An anchor over an ambiguous name is refused rather than guessed —
    guessing is how a claim ends up mutating a line in a symbol it does not
    name, which is the whole defect this anchoring replaces.
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


def run(base: str, claims_path: Path, exemptions_path: Path, max_candidates: int, list_only: bool) -> int:
    _validate_exemptions(exemptions_path)
    claims, unselected = _partition_claims(base, claims_path)
    print(f"mutation candidates: {len(claims)} (cap {max_candidates})")
    for claim in claims:
        via = " (selected by symbol)" if claim.get(SELECTION_KEY) == "symbol" else ""
        print(
            f"  {claim['id']}: {claim['path']}::{claim['symbol']} "
            f"[{claim['operator']}]{via}"
        )
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
    if len(claims) > max_candidates:
        print("candidate cap exceeded; split the diff or raise the cap visibly", file=sys.stderr)
        return 2
    if list_only or not claims:
        return 0

    commands: dict[tuple[str, ...], list[str]] = {}
    for claim in claims:
        command = _command(claim)
        commands.setdefault(tuple(command), command)
    for command in commands.values():
        print(f"BASELINE: {' '.join(command)}")
        if _run_command(command) != 0:
            print("baseline failed; mutation result would be meaningless", file=sys.stderr)
            return 2

    survivors: list[str] = []
    for claim in claims:
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
    if survivors:
        print(f"SURVIVED: {', '.join(survivors)}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    # Not `required=True` any more: `--claims-for` is an inventory question
    # about the registry, and there is no base to diff against when the answer
    # is wanted BEFORE the rewrite that would produce one.
    parser.add_argument("--base")
    parser.add_argument("--claims", type=Path, default=DEFAULT_CLAIMS)
    parser.add_argument("--exemptions", type=Path, default=DEFAULT_EXEMPTIONS)
    parser.add_argument("--max-candidates", type=int, default=12)
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
            args.max_candidates,
            args.list_only,
        )
    except RuntimeError as error:
        print(f"mutation-check configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
