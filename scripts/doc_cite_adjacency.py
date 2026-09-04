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

**ARM 1 — the cite that points OUT of this repo.** Everything above is arm 2,
and it can only judge a cite whose file this repo tracks. The canon's other
line-number corpus points at the launcher: 37 `.dart:N` path cites and 16 bare
`:N` continuations trailing a `.dart` path, measured over the gated canon at
hermes `3d3a33be3e`. Nothing on this side can ever tell a reader that one of
those numbers went stale — the launcher's mirror of this gate was built after
its own `hermes-agent/agent_runtime/office_store.py:<n>` cite rotted FOUR
times. So arm 1 is a NEGATIVE rule: a `path.ext:N` cite whose path names no
file `git ls-files` reports is REFUSED. It may name a SYMBOL; it may not name a
line. A path that resolves AMBIGUOUSLY is not foreign and stays unchecked in
both arms — refusing a cite because the probe cannot tell which of three
`models.py` it means is inventing a finding.

Arm 1 has its own budget (``--foreign-budget``, default
``docs/agent-runtime-harness/foreign-line-cites.json``), asserted in both
directions exactly as the adjacency baseline is, and it landed EMPTY: every one
of the 53 was re-anchored to the symbol its own sentence already named. The file
stays with its header, because a budget that exists at zero is the only kind
that cannot quietly grow.

    python scripts/doc_cite_adjacency.py [--root docs/agent-runtime-harness]
        [--exclude archive/] [--window 3] [--baseline PATH] [--write-baseline]
        [--foreign-budget PATH] [--write-foreign-budget]
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
DEFAULT_FOREIGN_BUDGET = "docs/agent-runtime-harness/foreign-line-cites.json"

#: The same cite shape the resolution report reads, narrowed to Python: this
#: probe needs an AST and a symbol vocabulary, and `.md`/`.json` cites have
#: neither. `paths.py:20,24` (comma list) and `harness.py:3001+` are read for
#: their FIRST number, which is the line the sentence points at.
CITE = re.compile(
    r"(?<![\w/.\-])"
    r"((?:[\w.\-]+/)*[\w.\-]+\.py)"
    r":(\d+)(?:\s*-\s*(\d+))?"
)

#: The canon's OTHER cite shape, and the one the first cut of this probe could
#: not see: a bare backticked ``:N`` that continues the path of the cite before
#: it — ``\`harness.py:1873\`, \`_cmd_characters_auto\` at \`:4776\```. The gated
#: canon carries 298 of these against 326 path cites, so leaving them invisible
#: left nearly half the line numbers in the docs ungated; the eight found in the
#: 2026-09-01 sweep were found only because they shared a sentence with a cite
#: the probe had already flagged. The whole backtick must BE the token (a
#: trailing ``+`` allowed, as ``CITE`` allows it) so that prose which merely
#: opens with a colon cannot be read as a line number.
CONTINUATION = re.compile(r"`:(\d+)(?:\s*-\s*(\d+))?\+?`")

#: What a continuation inherits FROM: any mention of a Python path, with or
#: without a line number of its own. Not ``CITE``, and the difference is
#: measured — 03's sentence reads "hermes_cli/harness.py:1693, parsed by
#: agent_runtime/patch_coverage.py::parse_fold_entities_option, :404", where
#: the ``::`` form carries no line and ``CITE`` skips it, so a rule
#: reading only ``CITE`` hands ``:404`` to *harness.py* and invents a finding.
#: The nearest preceding FILE the prose names is what a reader inherits, which
#: is what this matches.
PATH_MENTION = re.compile(r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w.\-]+\.py)(?![\w])")

#: ARM 1's extension set. Wider than ``CITE``'s because arm 1 needs no AST and
#: no symbol vocabulary — it only asks whether the path is a file this repo
#: tracks — and the corpus it exists for is entirely ``.dart``. The two data
#: shapes are carried because the canon cites them by line the same way.
FOREIGN_EXT = r"(?:dart|py|json|ya?ml)"

#: ARM 1's cite shape and the path a bare ``:N`` inherits under arm 1. Separate
#: objects from ``CITE``/``PATH_MENTION`` on purpose: widening those would put
#: ``.dart`` and ``.json`` tokens through the adjacency verdict, which parses an
#: AST and would change arm 2's counts and baseline. The two arms walk the same
#: docs and share nothing but the sentence rules.
FOREIGN_CITE = re.compile(
    r"(?<![\w/.\-])"
    r"((?:[\w.\-]+/)*[\w.\-]+\." + FOREIGN_EXT + r")"
    r":(\d+)(?:\s*-\s*(\d+))?"
)
FOREIGN_PATH_MENTION = re.compile(
    r"(?<![\w/.\-])((?:[\w.\-]+/)*[\w.\-]+\." + FOREIGN_EXT + r")(?![\w])"
)

#: Identifiers the prose offers as the subject. Read from backticked spans only
#: — bare prose words are English, and "store" in a sentence is not a claim
#: about a symbol. Dotted and called forms split on the same pass:
#: `agent_create.perform_agent_create`, `agent_chat_send(wait=false)`.
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: One backticked span, matched WITHIN A SINGLE LINE — see :func:`_spans` for
#: why the line boundary is where this rule stops.
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

#: The other half of that guard, and the one length alone does not give: an
#: identifier can be long and still be everywhere. A backticked `show`, `final`
#: or `chat` occurs all over a 5000-line module, so one of them lands in almost
#: any +/-3 window and passes a cite that is nowhere near its real subject —
#: measured on `07-observability.md|persona_commands.py:3522`, which passed on
#: those three while its actual subject `slim_chat_final_observability` sits at
#: 127/4222/4495. A word occurring more than this many times, whole-word, in the
#: CITED file is not a locator, so it is dropped from the subjects; a cite left
#: with no subject is UNCHECKED, never a pass. Dropping can only ever make a
#: cite unchecked or failed, so this rule cannot turn a red cite green.
#:
#: **20, and the corpus picked it, not taste.** Swept over the gated canon at 8 /
#: 12 / 16 / 20 / 40 (the table is in the field notes). Every candidate flips the
#: measured coincidence, so "does it catch 3522" does not choose between them.
#: What chooses is the pair of cites each end gets WRONG, read one by one:
#:
#: * **40 is too loose.** `01|harness.py:616` (cited for `realm sync revert`,
#:   actually the `resolve` parser) and `01|hermes_cli/harness.py:1343` (cited
#:   for `install-harness-skills`, actually a `--max-seconds` argument) are real
#:   rot, and at 40 both keep passing on `realm` / `sync` / `install`.
#: * **12 and 8 are too tight.** They refuse `board_id` in `board_tool.py` (13-20
#:   occurrences), whose cite is exactly right, and report it as rot.
#:
#: 20 is the only candidate that catches every confirmed rot and invents no
#: finding. 16 is inside the same gap and was swept for that reason; 20 is the
#: top of it, and a ceiling should sit at the loose end of its safe range so the
#: rule refuses as few real subjects as it can.
MAX_SUBJECT_OCCURRENCES = 20

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


def resolve_candidates(
    token: str, paths: set[str], by_name: dict[str, list[str]]
) -> list[str]:
    """Every tracked path a cite's token could name.

    Split out of :func:`resolve_path` because the two arms need different
    halves of the same answer, and folding them into one ``None`` is what made
    arm 1 impossible to state. ``[]`` means the token names NO tracked file —
    a foreign path, which arm 1 refuses when it carries a line number. Two or
    more means AMBIGUOUS, which both arms leave unchecked: refusing a cite
    because the probe cannot tell which of three `models.py` it means is
    inventing a finding.
    """

    if token in paths:
        return [token]
    if "/" in token:
        return sorted(path for path in paths if path.endswith("/" + token))
    return sorted(by_name.get(token, []))


def resolve_path(token: str, paths: set[str], by_name: dict[str, list[str]]) -> str | None:
    """The tracked path a cite names, or ``None`` when it cannot be settled.

    Same refusal as the resolution report: an ambiguous bare name is not
    guessed. Picking one of three `models.py` is how a probe invents findings.
    """

    candidates = resolve_candidates(token, paths, by_name)
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


def sentence_bounds(paragraph: str, offset: int, length: int) -> tuple[int, int]:
    """``(low, high)`` character bounds of the sentence a cite sits in.

    Split out from :func:`subject_window` because a bare ``:N`` continuation
    needs the same bounds to find the path cite it continues, and two scopes
    that could drift apart would be two rules.
    """

    low = max(0, offset - LOOKBACK)
    high = min(len(paragraph), offset + length + LOOKAHEAD)
    for match in BOUNDARY.finditer(paragraph, low, offset):
        low = match.end()
    after = BOUNDARY.search(paragraph, offset + length, high)
    if after is not None:
        high = after.start()
    return low, high


def subject_window(paragraph: str, offset: int, length: int) -> str:
    """The slice of ``paragraph`` whose backticks may speak for this cite.

    The sentence, not the paragraph: a paragraph that changes subject mid-way
    would otherwise lend one sentence's symbols to another's cite, and the probe
    would report rot that is only the writer moving on.
    """

    low, high = sentence_bounds(paragraph, offset, length)
    return paragraph[low:high]


def _spans(text: str) -> list[str]:
    """Backticked spans, paired LINE BY LINE and never across a boundary.

    The rule the junk-subject class asked for. ``BACKTICKED`` pairs greedily,
    so a line whose backticks do not balance — a ``````` fence, a code span
    the writer left open, a table row this window's own cut sliced through —
    hands its stray backtick the NEXT line's opening one. Every span after it
    in the window then inverts: the "identifiers" are the ordinary English
    sitting between two real code spans, which is where `advanced`, `also`,
    `having` and `honest` came from. Eight of the 2026-09-02 waivers were that,
    on cites read and confirmed CORRECT, and the occurrence ceiling cannot
    answer it — the words are common, but so are real short symbols.

    **The cost, measured rather than waved past.** This canon hard-wraps at ~80
    columns, so ~90 of its code spans are written across two lines and this
    rule stops reading them. Dropping a subject can only make a cite UNCHECKED
    or FAILED, never turn a red cite green — the same safety direction the
    ceiling rests on — and over the gated canon the whole change moved 190→193
    adjacent, 90→80 FAILED, and 61→67 unchecked, retiring 14 waivers against 4
    new findings that were then read one by one (three were real rot in the
    canon, one is the table-row blind spot).

    **The rule that keeps the wrap was tried and is worse.** Carrying an
    unclosed span into a line that is ITSELF unbalanced preserves a hard wrap,
    but the subject window is a character slice: when it cuts mid-span both
    boundary lines come back odd and the junk returns. Measured, that variant
    kept `07-observability.md|agent_runtime/stream.py:135-173` green on
    `advanced` / `having` / `honest` — a cite whose emitter had moved sixty
    lines away, which the strict rule reports.
    """

    return [span for line in text.split("\n") for span in BACKTICKED.findall(line)]


def subjects(text: str) -> set[str]:
    """Identifiers the prose offers, from backticked spans only."""

    found: set[str] = set()
    for span in _spans(text):
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
        self._counts: dict[str, int] = {}
        self._defined: set[str] | None = None
        self._enclosing: dict[int, set[str]] = {}

    def has(self, ident: str) -> bool:
        if ident not in self._present:
            self._present[ident] = _whole_word(ident, self.text)
        return self._present[ident]

    def defines(self, ident: str) -> bool:
        """Does this file ``def``/``class`` ``ident`` anywhere?

        The exemption from ``MAX_SUBJECT_OCCURRENCES``: a name the file DEFINES
        pins a line by construction, however often it is then used. Without it
        the ceiling refuses the file's own workhorses — `store_root` in
        `paths.py`, `StoreDriftItem` in `realm_sync.py` — and reports cites that
        land exactly on their ``def`` as rot, which is inventing a finding to
        stop inventing a finding. It rests on the same AST the ``in-symbol``
        verdict already rests on.
        """

        if self._defined is None:
            names: set[str] = set()
            if self.tree is not None:
                for node in ast.walk(self.tree):
                    if isinstance(
                        node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    ):
                        names.add(node.name)
            self._defined = names
        return ident in self._defined

    def occurrences(self, ident: str) -> int:
        """Whole-word occurrences of ``ident`` in the whole file.

        The measure behind ``MAX_SUBJECT_OCCURRENCES``: presence answers "could
        this sentence be about that file", frequency answers "could this word
        possibly point at one line of it".
        """

        if ident not in self._counts:
            self._counts[ident] = len(
                re.findall(
                    rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])", self.text
                )
            )
        return self._counts[ident]

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
    ceiling: int = MAX_SUBJECT_OCCURRENCES,
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
    # Presence is not enough: a word that occurs everywhere in the file lands in
    # some +/-3 window by coincidence and would pass the cite on nothing. It is
    # dropped from the subjects rather than counted against the cite, so an
    # over-common word can only ever make a cite UNCHECKED, never FAILED.
    present = sorted(
        ident
        for ident in named
        if target.has(ident)
        and (target.occurrences(ident) <= ceiling or target.defines(ident))
    )
    if not present:
        return NO_SUBJECT, []

    window = target.window(first, last, radius)
    if any(_whole_word(ident, window) for ident in present):
        return ADJACENT, present
    if (target.enclosing(first) | target.enclosing(last)) & set(present):
        return IN_SYMBOL, present
    return FAILED, present


class ContinuedCite:
    """A bare ``:N`` wearing the path of the cite it continues.

    Presented with the slice of the ``re.Match`` API :func:`verdict` reads, so
    a continuation is judged by exactly the same rule as the cite it continues
    — including its own subject window, which is taken at the continuation's
    own position and not at the path cite's.
    """

    def __init__(self, token: str, match: "re.Match[str]") -> None:
        self.token = token
        self.match = match

    def group(self, index: int) -> str | None:
        if index == 0:
            return self.match.group(0)
        if index == 1:
            return self.token
        return self.match.group(index - 1)

    def start(self) -> int:
        return self.match.start()


def continued_path(
    doc_lines: list[str],
    index: int,
    match: "re.Match[str]",
    mention: "re.Pattern[str]" = PATH_MENTION,
) -> str | None:
    """The path token a bare ``:N`` inherits, or ``None`` when it has none.

    The nearest preceding path MENTION in the same SENTENCE. Two choices, both
    measured rather than assumed:

    * The **sentence**, not the paragraph. Where both scopes resolve they never
      disagree (56 of 56 over the gated canon), and the 76 the paragraph would
      additionally resolve include ones it gets WRONG — "realm_sync.py: pull
      applies the ledger (_apply_skill_tombstones, :613)" would inherit
      *store.py* from a cite two sentences up. A wrong path is a
      fabricated finding; a refusal is only a missed one.
    * A path **mention**, not a path cite: see ``PATH_MENTION``.

    A continuation with no path mention before it in its sentence is counted,
    never guessed.

    ``mention`` is the vocabulary of paths a continuation may inherit. Arm 2
    passes the default (Python only, because that is all its verdict can
    judge); arm 1 passes ``FOREIGN_PATH_MENTION`` so a bare ``:N`` trailing a
    `.dart` path is refused by the same rule as the path cite it continues.
    """

    start, end = paragraph_bounds(doc_lines, index)
    paragraph = "\n".join(doc_lines[start : end + 1])
    offset = sum(len(doc_lines[i]) + 1 for i in range(start, index)) + match.start()
    low, _ = sentence_bounds(paragraph, offset, len(match.group(0)))
    preceding = list(mention.finditer(paragraph, low, offset))
    return preceding[-1].group(1) if preceding else None


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
        self.continuations_seen = 0
        self.checked = 0
        self.adjacent = 0
        self.in_symbol = 0
        self.failures: list[Finding] = []
        self.unchecked_no_subject = 0
        self.unresolved = 0
        self.continuations_unresolved = 0
        self.past_end = 0
        self.duplicate_keys: list[str] = []


def _judge_one(
    result: Walk,
    doc: str,
    lines: list[str],
    index: int,
    match,
    paths: set[str],
    by_name: dict[str, list[str]],
    targets: dict[str, Target],
    seen_keys: set[str],
    radius: int,
    ceiling: int,
) -> None:
    """Resolve one cite's file, judge it, and record the outcome.

    Shared by the two cite shapes so that a bare ``:N`` cannot end up on a
    softer rule than the path cite it continues — the only difference between
    them is how the path was arrived at.
    """

    token, first_raw, last_raw = match.group(1), match.group(2), match.group(3)
    resolved = resolve_path(token, paths, by_name)
    if resolved is None:
        result.unresolved += 1
        return
    if resolved not in targets:
        targets[resolved] = Target(resolved)
    target = targets[resolved]
    outcome, present = verdict(lines, index, match, target, radius, ceiling)
    if outcome == PAST_END:
        result.past_end += 1
        return
    if outcome == NO_SUBJECT:
        result.unchecked_no_subject += 1
        return
    result.checked += 1
    if outcome == ADJACENT:
        result.adjacent += 1
        return
    if outcome == IN_SYMBOL:
        result.in_symbol += 1
        return
    cite = f"{token}:{first_raw}" + (f"-{last_raw}" if last_raw else "")
    key = f"{doc}|{token}:{first_raw}"
    if key in seen_keys:
        result.duplicate_keys.append(key)
    seen_keys.add(key)
    result.failures.append(Finding(key, doc, index + 1, cite, present, resolved))


def canon_docs(paths: set[str], root: str, exclude: list[str]) -> list[str]:
    """The gated canon both arms walk: tracked ``.md`` under ``root``."""

    return sorted(
        path
        for path in paths
        if path.startswith(root + "/")
        and path.endswith(".md")
        and not any(fragment in path for fragment in exclude)
    )


class ForeignCite:
    """One cite carrying a LINE NUMBER into a file this repo does not track."""

    def __init__(self, key: str, doc: str, doc_line: int, cite: str) -> None:
        self.key = key
        self.doc = doc
        self.doc_line = doc_line
        self.cite = cite

    def __str__(self) -> str:
        return f"{self.doc}:{self.doc_line}  {self.cite}"


def foreign_walk(root: str, exclude: list[str]) -> list[ForeignCite]:
    """ARM 1 — every foreign line cite in the gated canon.

    The rule, and it is a NEGATIVE one: a ``path.ext:N`` cite whose ``path``
    names no file ``git ls-files`` reports is a FAILURE. Such a cite may name a
    SYMBOL and may not name a LINE, because nothing on this side can ever tell
    a reader that the number went stale — the launcher's own
    `office_store.py:<n>` cite rotted four times before that was accepted, and
    a fifth re-numbering is not a fix.

    A path that resolves AMBIGUOUSLY is NOT foreign and is not refused: see
    :func:`resolve_candidates`. This walk shares the sentence rules with arm 2
    and nothing else — it parses no AST and reads no subjects, because "is this
    file tracked" needs neither.
    """

    paths, by_name = _tracked()

    def is_foreign(token: str) -> bool:
        return not resolve_candidates(token, paths, by_name)

    found: list[ForeignCite] = []
    for doc in canon_docs(paths, root, exclude):
        lines = (REPO_ROOT / doc).read_bytes().decode("utf-8", "replace").splitlines()
        found.extend(foreign_cites_in_doc(doc, lines, is_foreign))
    return sorted(found, key=lambda item: item.key)


def foreign_cites_in_doc(
    doc: str, lines: list[str], is_foreign
) -> list[ForeignCite]:
    """ARM 1's rule for ONE doc, with no git and no filesystem in it.

    ``is_foreign`` answers "does this path token name no tracked file". Split
    out so the rule can be falsified on fabricated prose rather than only on
    the live canon, which — the arm having landed EMPTY — carries no case.
    """

    found: dict[str, ForeignCite] = {}

    def record(index: int, token: str, first: str, spelling: str) -> None:
        if not is_foreign(token):
            return
        # Same key shape as the adjacency baseline — `<doc>|<token>:<line>`,
        # without the doc's own line number, so inserting a paragraph does not
        # invalidate a waiver.
        key = f"{doc}|{token}:{first}"
        found.setdefault(key, ForeignCite(key, doc, index + 1, spelling))

    for index, line in enumerate(lines):
        for match in FOREIGN_CITE.finditer(line):
            record(index, match.group(1), match.group(2), match.group(0))
        for match in CONTINUATION.finditer(line):
            token = continued_path(lines, index, match, mention=FOREIGN_PATH_MENTION)
            if token is None:
                continue
            record(
                index, token, match.group(1),
                f"{token}{match.group(0).strip('`')}",
            )

    return sorted(found.values(), key=lambda item: item.key)


def walk(
    root: str,
    exclude: list[str],
    radius: int,
    ceiling: int = MAX_SUBJECT_OCCURRENCES,
) -> Walk:
    paths, by_name = _tracked()
    docs = canon_docs(paths, root, exclude)
    result = Walk()
    result.docs = len(docs)
    targets: dict[str, Target] = {}
    seen_keys: set[str] = set()

    for doc in docs:
        lines = (REPO_ROOT / doc).read_bytes().decode("utf-8", "replace").splitlines()
        for index, line in enumerate(lines):
            for match in CITE.finditer(line):
                result.cites_seen += 1
                _judge_one(
                    result, doc, lines, index, match, paths, by_name, targets,
                    seen_keys, radius, ceiling,
                )
            for match in CONTINUATION.finditer(line):
                result.continuations_seen += 1
                token = continued_path(lines, index, match)
                if token is None:
                    result.continuations_unresolved += 1
                    continue
                _judge_one(
                    result, doc, lines, index, ContinuedCite(token, match), paths,
                    by_name, targets, seen_keys, radius, ceiling,
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
    ceiling: int = MAX_SUBJECT_OCCURRENCES,
    foreign_budget_path: Path | None = None,
    write_foreign_budget: bool = False,
) -> int:
    if foreign_budget_path is None:
        foreign_budget_path = REPO_ROOT / DEFAULT_FOREIGN_BUDGET
    result = walk(root, exclude, radius, ceiling)

    print(f"cite-adjacency probe (+/-{radius} lines) over {result.docs} docs under {root}/")
    print(f"  python cites seen         : {result.cites_seen}")
    print(f"  bare :N continuations     : {result.continuations_seen}")
    print(f"  subject occurrence ceiling: {ceiling}")
    print(f"  machine-checkable         : {result.checked}")
    print(f"    passed, adjacent        : {result.adjacent}")
    print(f"    passed, inside symbol   : {result.in_symbol}")
    print(f"    FAILED                  : {len(result.failures)}")
    print(f"  unchecked (no subject)    : {result.unchecked_no_subject}")
    print(f"  unchecked (path ambiguous): {result.unresolved}")
    print(f"  unchecked (line past EOF) : {result.past_end}")
    print(f"  unchecked (:N, no path)   : {result.continuations_unresolved}")
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

    foreign = foreign_walk(root, exclude)
    if write_foreign_budget:
        payload = {
            "_comment": (
                "ARM 1 budget for scripts/doc_cite_adjacency.py. Every key is a "
                "cite carrying a LINE NUMBER into a file this repo does not "
                "track, kept with a written reason because it could not be "
                "re-anchored to a symbol. The gate is red on any foreign line "
                "cite NOT here AND on any key here that has stopped being one - "
                "a waiver may not outlive its rot. Burn this down; do not grow it."
            ),
            "waived": {item.key: "TODO: reason" for item in foreign},
        }
        foreign_budget_path.write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {len(foreign)} key(s) to {foreign_budget_path}")
        return 0

    foreign_budget = load_baseline(foreign_budget_path)
    foreign_keys = {item.key for item in foreign}
    foreign_unlisted = [item for item in foreign if item.key not in foreign_budget]
    foreign_stale = sorted(key for key in foreign_budget if key not in foreign_keys)

    print(f"arm 1 (foreign line cites), budget {foreign_budget_path}")
    print(f"  foreign line cites found  : {len(foreign)}")
    print(f"  budgeted                  : {len(foreign_budget)}")
    print(f"  UNBUDGETED: {len(foreign_unlisted)}")
    for item in foreign_unlisted:
        print(f"    {item}")
    print(f"  STALE BUDGET KEYS (delete the entry): {len(foreign_stale)}")
    for key in foreign_stale:
        print(f"    {key}")
    print("")

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

    if foreign_unlisted or foreign_stale:
        print(
            "foreign-line-cite arm FAILED. A cite carrying a line number into a "
            "file this repo does not track can never be told it went stale from "
            "here: name the SYMBOL instead of the line. A budget key that has "
            "stopped failing must be deleted.",
            file=sys.stderr,
        )
    if unwaived or stale:
        print(
            "cite-adjacency probe FAILED. A cite whose line is nowhere near the "
            "symbol its own paragraph names is pointing at unrelated text: "
            "re-anchor it, or name the symbol instead of the line.",
            file=sys.stderr,
        )
    if unwaived or stale or foreign_unlisted or foreign_stale:
        return 1
    print("cite-adjacency probe passed (baseline capped, nothing new, nothing stale).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=DEFAULT_ROOT)
    parser.add_argument("--window", type=int, default=3)
    parser.add_argument(
        "--occurrence-ceiling",
        type=int,
        default=MAX_SUBJECT_OCCURRENCES,
        help="an identifier occurring more than this many times in the cited "
        "file is not a locator and is dropped from the subjects. Raising it is "
        "how a cite passes on a word like `show`; see MAX_SUBJECT_OCCURRENCES.",
    )
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline from the current failures, each with a TODO "
        "reason to be filled in by hand. Never run this to make a red gate green.",
    )
    parser.add_argument("--foreign-budget", default=DEFAULT_FOREIGN_BUDGET)
    parser.add_argument(
        "--write-foreign-budget",
        action="store_true",
        help="rewrite the ARM 1 budget from the current foreign line cites, "
        "each with a TODO reason to be filled in by hand. Never run this to "
        "make a red gate green.",
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
            args.occurrence_ceiling,
            REPO_ROOT / args.foreign_budget,
            args.write_foreign_budget,
        )
    except RuntimeError as error:
        print(f"cite-adjacency probe could not run: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
