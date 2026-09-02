"""Probe whether a doc's `file.py:N` cite lands on the text it NAMES.

The other half of the doc-cite rot pair. `scripts/doc_cite_report.py` asks
whether a cite RESOLVES — tracked file, line exists, sha on `origin/main`. That
question is answered "yes" by every cite whose file is alive, which is exactly
the failure the queue rowed: four cites measured pointing at unrelated text as
far back as `76c6ade663`, all of them resolving perfectly. A line number into a
live module drifts on every refactor above it and never stops resolving.

This asks the harder question: **is the cited line about the thing the sentence
says it is?**

    subject named by the prose  in  cited file, within [first-3, last+3]

**What "machine-checkable" means here, stated because the honest number depends
on it.** A cite is checkable only when the prose AROUND IT names at least one
identifier that actually occurs somewhere in the cited file. `models.py:252`
in a sentence naming `AgentRecord` is checkable — `AgentRecord` is in that file,
so "is it near line 252" is a real question. A cite in a paragraph naming
nothing that exists in the file is NOT evidence of rot; it is a paragraph this
probe cannot read, and it is counted as UNCHECKED rather than folded into the
failures. Inflating the failure count with unreadable paragraphs is how a probe
starts inventing findings.

**"Around it" is the SENTENCE, not the paragraph, and that was measured rather
than assumed.** The paragraph reads well as a unit until it carries two
sentences about two different files — and this canon's do constantly.
`realm_membership.py:1-12`, cited for "fail closed", was reported rotted
because the sentence BEFORE it named `RealmStore` and `WorkspaceStore`, which
do live in that file but twenty lines below the cited docstring; `board_store
.py:8-15`, citing a quoted invariant, was reported rotted by the sentence AFTER
it naming `Board`/`BoardColumn`/`BoardCard`. Lending a neighbour's subject to a
cite is inventing a finding. Subjects are read from the sentence the cite sits
in, capped at ``LOOKBACK``/``LOOKAHEAD`` characters so an unpunctuated wall of
prose cannot widen the scope back out.

**Two ways to pass, both legitimate.**

* ``adjacent`` — a named identifier appears in the +/-3 window. The literal
  question the row asks.
* ``in-symbol`` — the cited line is INSIDE a ``def``/``class`` the prose names,
  resolved from the file's AST rather than from a text scan. A cite to line 4419
  of a 300-line function whose name the sentence gives is not rot, and calling
  it rot would push the docs toward citing the ``def`` line of everything. These
  are counted separately so the headline number can be read either way.

**The sweep budget.** The probe is a GATE with a baseline, not a report. Its
baseline (``--baseline``, default ``docs/agent-runtime-harness/
cite-adjacency-baseline.json``) carries every failure that was NOT mechanically
fixable, each with a written reason. The gate is red on any failure NOT in the
baseline, and — the ratchet — also red on any baseline entry that has stopped
failing, so a waiver cannot outlive the rot it waived. That is what lets this
land red-capped in one commit instead of demanding every cite fixed first,
without the born-red silence that kept the resolution half advisory.

A baseline key is ``<doc>|<token>:<line>`` — deliberately WITHOUT the doc's own
line number, so inserting a paragraph does not invalidate every waiver. The cost
is that a doc citing the same `file.py:N` twice shares one waiver; the walk
reports the duplicate count so it cannot happen silently.

**Fails loud on a zero-cite walk.** A probe that walks nothing prints a clean
report, and an unrun gate is indistinguishable from a passing one.

    python scripts/doc_cite_adjacency.py [--root docs/agent-runtime-harness]
        [--exclude archive/] [--window 3] [--baseline PATH] [--write-baseline]
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import defaultdict
from pathlib import Path
import re
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_ROOT = "docs/agent-runtime-harness"
DEFAULT_BASELINE = "docs/agent-runtime-harness/cite-adjacency-baseline.json"

#: The same cite shape the resolution report reads, narrowed to Python: this
#: probe needs an AST and a symbol vocabulary, and `.md`/`.json` cites have
#: neither. `paths.py:20,24` (comma list) and `harness.py:3001+` are read for
#: their FIRST number, which is the line the sentence points at.
CITE = re.compile(
    r"(?<![\w/.\-])"
    r"((?:[\w.\-]+/)*[\w.\-]+\.py)"
    r":(\d+)(?:\s*-\s*(\d+))?"
)

#: Identifiers the prose offers as the subject. Read from backticked spans only
#: — bare prose words are English, and "store" in a sentence is not a claim
#: about a symbol. Dotted and called forms split on the same pass:
#: `agent_create.perform_agent_create`, `agent_chat_send(wait=false)`.
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
BACKTICKED = re.compile(r"`([^`]+)`")

#: Hard caps on the sentence scope, for prose that never punctuates. Asymmetric
#: because the canon writes the symbol and THEN the cite; the shorter lookahead
#: still catches "(`realm_sync.py:1980`) names them and `_pulled_artifact_bytes`
#: merges each by its own rule".
LOOKBACK = 240
LOOKAHEAD = 120

#: A sentence boundary in this canon's prose: terminal punctuation followed by
#: space and something that starts a new clause, or a markdown list bullet. Not
#: a general English sentence splitter and not trying to be — it only has to
#: stop the scope from crossing into the next claim.
BOUNDARY = re.compile(
    r"(?<=[.!?:])\s+(?=[A-Z*`(\[-])"  # end of a claim, start of the next
    r"|\n\s*[-*]\s"  # a markdown bullet is its own claim
    r"|\n\s*\n"  # and so is a new block
)

#: Four characters, because `id` / `ok` / `db` in a backtick match half of every
#: Python file and would turn the probe green by coincidence.
MIN_IDENT = 4

#: Words backticked constantly in this canon that are not the subject of
#: anything. Kept short and explicit: a long stoplist is a place to hide a
#: failure by adding a word to it.
STOPWORDS = frozenset(
    {
        "self",
        "None",
        "True",
        "False",
        "json",
        "dict",
        "list",
        "type",
        "name",
        "path",
        "file",
        "line",
        "data",
        "main",
        "test",
        "docs",
        "http",
        "https",
        "note",
    }
)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, check=False, capture_output=True, text=True
    )


def _tracked() -> tuple[set[str], dict[str, list[str]]]:
    completed = _git("ls-files")
    if completed.returncode != 0:
        raise RuntimeError(f"git ls-files failed: {completed.stderr.strip()}")
    paths = {row.strip() for row in completed.stdout.splitlines() if row.strip()}
    by_name: dict[str, list[str]] = defaultdict(list)
    for path in paths:
        by_name[path.rsplit("/", 1)[-1]].append(path)
    return paths, by_name


def resolve_path(token: str, paths: set[str], by_name: dict[str, list[str]]) -> str | None:
    """The tracked path a cite names, or ``None`` when it cannot be settled.

    Same refusal as the resolution report: an ambiguous bare name is not
    guessed. Picking one of three `models.py` is how a probe invents findings.
    """

    if token in paths:
        return token
    if "/" in token:
        tail = [path for path in paths if path.endswith("/" + token)]
        return tail[0] if len(tail) == 1 else None
    candidates = by_name.get(token, [])
    return candidates[0] if len(candidates) == 1 else None


def paragraph_bounds(lines: list[str], index: int) -> tuple[int, int]:
    """The blank-line-delimited block containing ``index`` (0-based).

    The paragraph is the OUTER bound because this canon hard-wraps at ~80
    columns: the sentence naming the symbol is routinely two lines above the
    cite. It is not the subject scope — see ``subject_window``.
    """

    start = index
    while start > 0 and lines[start - 1].strip():
        start -= 1
    end = index
    while end + 1 < len(lines) and lines[end + 1].strip():
        end += 1
    return start, end


def subject_window(paragraph: str, offset: int, length: int) -> str:
    """The slice of ``paragraph`` whose backticks may speak for this cite.

    The sentence, not the paragraph: a paragraph that changes subject mid-way
    would otherwise lend one sentence's symbols to another's cite, and the probe
    would report rot that is only the writer moving on.
    """

    low = max(0, offset - LOOKBACK)
    high = min(len(paragraph), offset + length + LOOKAHEAD)
    for match in BOUNDARY.finditer(paragraph, low, offset):
        low = match.end()
    after = BOUNDARY.search(paragraph, offset + length, high)
    if after is not None:
        high = after.start()
    return paragraph[low:high]


def subjects(text: str) -> set[str]:
    """Identifiers the prose offers, from backticked spans only."""

    found: set[str] = set()
    for span in BACKTICKED.findall(text):
        for ident in IDENT.findall(span):
            if len(ident) >= MIN_IDENT and ident not in STOPWORDS:
                found.add(ident)
    return found


def _enclosing_symbols(tree: ast.AST, line: int) -> set[str]:
    """Names of every ``def``/``class`` whose body spans ``line``."""

    names: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = min([child.lineno] + [d.lineno for d in child.decorator_list])
                end = getattr(child, "end_lineno", child.lineno)
                if start <= line <= end:
                    names.add(child.name)
            visit(child)

    visit(tree)
    return names


def _whole_word(ident: str, text: str) -> bool:
    return (
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])", text) is not None
    )


class Target:
    """One cited source file, parsed once and reused across its cites."""

    def __init__(self, path: str, text: str | None = None) -> None:
        self.path = path
        if text is None:
            text = (REPO_ROOT / path).read_bytes().decode("utf-8", "replace")
        self.text = text
        self.lines = self.text.splitlines()
        try:
            self.tree: ast.AST | None = ast.parse(self.text)
        except SyntaxError:
            self.tree = None
        self._present: dict[str, bool] = {}
        self._enclosing: dict[int, set[str]] = {}

    def has(self, ident: str) -> bool:
        if ident not in self._present:
            self._present[ident] = _whole_word(ident, self.text)
        return self._present[ident]

    def window(self, first: int, last: int, radius: int) -> str:
        low = max(1, first - radius)
        high = min(len(self.lines), last + radius)
        return "\n".join(self.lines[low - 1 : high])

    def enclosing(self, line: int) -> set[str]:
        if self.tree is None:
            return set()
        if line not in self._enclosing:
            self._enclosing[line] = _enclosing_symbols(self.tree, line)
        return self._enclosing[line]


#: Every verdict one cite can get. ``PAST_END`` and ``NO_SUBJECT`` are refusals
#: to judge, not passes — they are counted apart in the report for exactly that
#: reason.
ADJACENT = "adjacent"
IN_SYMBOL = "in-symbol"
FAILED = "failed"
NO_SUBJECT = "no-subject"
PAST_END = "past-end"


def verdict(
    doc_lines: list[str],
    index: int,
    match: "re.Match[str]",
    target: Target,
    radius: int,
) -> tuple[str, list[str]]:
    """Judge ONE cite, and say which identifiers the judgement rests on.

    The whole decision, with no filesystem and no git in it: given the doc's
    lines, which line the cite is on, the cite match, and the parsed target.
    ``walk`` is the loop that feeds this; this is the rule.
    """

    token, first_raw, last_raw = match.group(1), match.group(2), match.group(3)
    first = int(first_raw)
    last = int(last_raw or first_raw)
    if last > len(target.lines):
        # The resolution report owns this class; a line past the end of the file
        # has no window to probe.
        return PAST_END, []

    start, end = paragraph_bounds(doc_lines, index)
    paragraph = "\n".join(doc_lines[start : end + 1])
    offset = sum(len(doc_lines[i]) + 1 for i in range(start, index))
    named = subjects(subject_window(paragraph, offset + match.start(), len(match.group(0))))
    # The cite's own path token is not a subject: `models` matching inside
    # `models.py` would pass every cite in the canon.
    named -= subjects(f"`{token}`")
    present = sorted(ident for ident in named if target.has(ident))
    if not present:
        return NO_SUBJECT, []

    window = target.window(first, last, radius)
    if any(_whole_word(ident, window) for ident in present):
        return ADJACENT, present
    if (target.enclosing(first) | target.enclosing(last)) & set(present):
        return IN_SYMBOL, present
    return FAILED, present


class Finding:
    """One cite whose line is nowhere near any symbol its sentence names."""

    def __init__(
        self, key: str, doc: str, doc_line: int, cite: str, named: list[str], target: str
    ) -> None:
        self.key = key
        self.doc = doc
        self.doc_line = doc_line
        self.cite = cite
        self.named = named
        self.target = target

    def __str__(self) -> str:
        names = ", ".join(sorted(self.named)[:6]) or "-"
        return f"{self.doc}:{self.doc_line}  {self.cite} -> {self.target}  names: {names}"


class Walk:
    def __init__(self) -> None:
        self.docs = 0
        self.cites_seen = 0
        self.checked = 0
        self.adjacent = 0
        self.in_symbol = 0
        self.failures: list[Finding] = []
        self.unchecked_no_subject = 0
        self.unresolved = 0
        self.past_end = 0
        self.duplicate_keys: list[str] = []


def walk(root: str, exclude: list[str], radius: int) -> Walk:
    paths, by_name = _tracked()
    docs = sorted(
        path
        for path in paths
        if path.startswith(root + "/")
        and path.endswith(".md")
        and not any(fragment in path for fragment in exclude)
    )
    result = Walk()
    result.docs = len(docs)
    targets: dict[str, Target] = {}
    seen_keys: set[str] = set()

    for doc in docs:
        lines = (REPO_ROOT / doc).read_bytes().decode("utf-8", "replace").splitlines()
        for index, line in enumerate(lines):
            for match in CITE.finditer(line):
                token, first_raw, last_raw = match.group(1), match.group(2), match.group(3)
                result.cites_seen += 1
                resolved = resolve_path(token, paths, by_name)
                if resolved is None:
                    result.unresolved += 1
                    continue
                if resolved not in targets:
                    targets[resolved] = Target(resolved)
                target = targets[resolved]
                outcome, present = verdict(lines, index, match, target, radius)
                if outcome == PAST_END:
                    result.past_end += 1
                    continue
                if outcome == NO_SUBJECT:
                    result.unchecked_no_subject += 1
                    continue
                result.checked += 1
                if outcome == ADJACENT:
                    result.adjacent += 1
                    continue
                if outcome == IN_SYMBOL:
                    result.in_symbol += 1
                    continue
                cite = f"{token}:{first_raw}" + (f"-{last_raw}" if last_raw else "")
                key = f"{doc}|{token}:{first_raw}"
                if key in seen_keys:
                    result.duplicate_keys.append(key)
                seen_keys.add(key)
                result.failures.append(
                    Finding(key, doc, index + 1, cite, present, resolved)
                )
    return result


def load_baseline(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dict(payload.get("waived", {}))


def run(
    root: str,
    exclude: list[str],
    radius: int,
    baseline_path: Path,
    write_baseline: bool,
) -> int:
    result = walk(root, exclude, radius)

    print(f"cite-adjacency probe (+/-{radius} lines) over {result.docs} docs under {root}/")
    print(f"  python cites seen         : {result.cites_seen}")
    print(f"  machine-checkable         : {result.checked}")
    print(f"    passed, adjacent        : {result.adjacent}")
    print(f"    passed, inside symbol   : {result.in_symbol}")
    print(f"    FAILED                  : {len(result.failures)}")
    print(f"  unchecked (no subject)    : {result.unchecked_no_subject}")
    print(f"  unchecked (path ambiguous): {result.unresolved}")
    print(f"  unchecked (line past EOF) : {result.past_end}")
    if result.duplicate_keys:
        print(
            f"  NOTE: {len(result.duplicate_keys)} duplicate baseline key(s) "
            "— one waiver covers every instance:"
        )
        for key in sorted(set(result.duplicate_keys)):
            print(f"    {key}")
    print("")

    if result.cites_seen == 0:
        print(
            "FATAL: the walk found zero cites. An unrun gate is indistinguishable "
            "from a passing one, so this is a failure and not a clean report.",
            file=sys.stderr,
        )
        return 2

    if write_baseline:
        payload = {
            "_comment": (
                "Sweep budget for scripts/doc_cite_adjacency.py. Every key is a "
                "cite whose line does not land near any symbol its paragraph "
                "names, kept with a written reason because it was not "
                "mechanically re-anchorable. The gate is red on any failure NOT "
                "here AND on any key here that has stopped failing - a waiver "
                "may not outlive its rot. Burn this down; do not grow it."
            ),
            "waived": {
                finding.key: "TODO: reason"
                for finding in sorted(result.failures, key=lambda f: f.key)
            },
        }
        baseline_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {len(result.failures)} key(s) to {baseline_path}")
        return 0

    baseline = load_baseline(baseline_path)
    failing_keys = {finding.key for finding in result.failures}
    unwaived = [finding for finding in result.failures if finding.key not in baseline]
    stale = sorted(key for key in baseline if key not in failing_keys)

    print(f"baseline: {len(baseline)} waived key(s) from {baseline_path}")
    print(f"UNWAIVED FAILURES: {len(unwaived)}")
    for finding in unwaived:
        print(f"  {finding}")
    print("")
    print(f"STALE WAIVERS (no longer failing - delete the entry): {len(stale)}")
    for key in stale:
        print(f"  {key}")
    print("")

    if unwaived or stale:
        print(
            "cite-adjacency probe FAILED. A cite whose line is nowhere near the "
            "symbol its own paragraph names is pointing at unrelated text: "
            "re-anchor it, or name the symbol instead of the line.",
            file=sys.stderr,
        )
        return 1
    print("cite-adjacency probe passed (baseline capped, nothing new, nothing stale).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--window", type=int, default=3)
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline from the current failures, each with a TODO "
        "reason to be filled in by hand. Never run this to make a red gate green.",
    )
    parser.add_argument("--exclude", action="append", default=[], metavar="FRAGMENT")
    args = parser.parse_args(argv)
    try:
        return run(
            args.root.rstrip("/"),
            args.exclude,
            args.window,
            REPO_ROOT / args.baseline,
            args.write_baseline,
        )
    except RuntimeError as error:
        print(f"cite-adjacency probe could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
