"""Every coverage claim in a doc or docstring must name a test that EXISTS.

The class this gate exists for
-----------------------------
A prose claim of coverage is the one thing no coverage tool contradicts.
``pytest --cov`` measures the tests that ran; it cannot see a sentence in a
shipped design doc saying "this seam is pinned by ``test_x.py::test_y``" when
``test_y`` was deleted eight months ago. A reader trusts the sentence, skips
writing the test, and treats the seam as verified. The MCF-78/MCF-75 sweep
(2026-08-20) found the hermes tree carrying claims whose named guard had been
deleted the week after the doc was written — including one, the ``skills_sync``
frozen-constant AST sweep, whose invariant was silently violated eleven times by
an upstream merge one day after its guard went.

WHAT THIS GATE CANNOT SEE — read this before trusting a green run
-----------------------------------------------------------------
**This is an EXISTENCE check. It proves a name resolves. It does NOT prove the
test asserts what the prose claims.**

A test that exists but asserts something *other* than the documented behaviour
is invisible here and always will be. The launcher's sibling gate
(``test/architecture/coverage_claims_resolve_test.dart``) scores 33 of the 35
confirmed findings in that sweep, and **the two it misses are exactly the two
VACUOUS ones** — a docstring naming three production seams that appear nowhere
in its own test body, and a comment claiming "three suites plus the paint
fences pin it byte-for-byte" where two of the named pinners never touch the
line. Both name things that exist. Both are lies.

An existence gate that lets a reader infer semantic coverage is this row's own
defect one level up, so the failure message says so too. The only real detector
for vacuity is a periodic MCF-53-style sweep with kill-proof discipline: mutate
the production behaviour, watch the named test go red, or it was never a pin.

Anti-vacuity of the gate itself (MCF-53 discipline)
---------------------------------------------------
MCF-53 found 32 of 155 gate rows scanning ZERO files and green since written,
because ``_production_files`` required a scope token to be a DIRECTORY. A
census instruction that returns zero forever is the disease. So this gate
asserts FLOORS on everything it counted (:func:`test_the_gate_scanned_a_real_corpus`)
and pins its reader against synthetic text independent of the tree
(:func:`test_the_reader_finds_a_planted_claim_and_clears_a_real_one`).

The four arms
-------------
``NAMED``  ``test_x.py::some_name`` (and the ``::name`` continuation form the
           house docs use for a run of ids from one file) — always a claim,
           anywhere, fenced or not. Resolution is by **AST**, not substring: a
           name inside a string literal does not satisfy it. Load-bearing —
           the worst hermes findings live in files that still exist.
``PATH``   a ``tests/.../test_x.py`` path carrying a coverage phrase.
``BARE``   a bare ``test_x.py`` filename carrying a coverage phrase.
``MEMBER`` a backticked bare function id ("covered by ``test_space_to_underscore``
           in ``tests/run_agent/test_repair_tool_call_name.py``") resolved
           against the test path in the same window.

The coverage-phrase requirement on the last three arms is deliberate, and its
absence is what made the launcher gate red 333 rows across 94 documents on
first run: **a filename in a planning tree is not an assertion that the file
exists.** "Test: `tests/hermes_cli/test_mcp_test_env_overrides.py`" under a
plan's Deliverables heading is a proposal. "the invariant is pinned by
`tests/agent_runtime/test_runtime_resolve_cache.py`" is a claim. A gate nobody
can go green on is a gate somebody deletes.

Negative context is exempt for the same reason in the other direction: a
sentence recording that ``test_repo_bundles.py`` **was deleted** is the honest
thing a doc can do about a retired test, and reddening on it would teach
writers to stay silent instead.
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"

#: Markdown roots. Root-level ``*.md`` is included: AGENTS.md and the READMEs
#: carry runner examples, and a rotted claim there is read by every contributor.
_MD_DIRS = ("docs",)

#: Python roots whose comments and docstrings are scanned. ``tests`` is in
#: deliberately — a test docstring pointing at a sibling suite is the exact
#: WRONG-NAME shape this gate exists for, and it is the densest corpus in the
#: repo for it.
_PY_DIRS = (
    "agent",
    "agent_runtime",
    "hermes_cli",
    "tools",
    "gateway",
    "tui_gateway",
    "cron",
    "scripts",
    "tests",
)

#: ``file.py::name``. The filename half is required so a bare ``::`` in prose
#: (or a C++-ish token) cannot manufacture a claim.
_NAMED = re.compile(r"\b(test_[A-Za-z0-9_]+\.py)::([A-Za-z_][A-Za-z0-9_]*)")

#: ANY ``module.py::symbol``, test or not. Needed to anchor the continuation
#: form correctly: a doc listing ``tools/agent_chat_dispatch.py::shutdown_x``
#: and then ``::is_supervised_here`` is naming two PRODUCTION exports, and a
#: reader that blindly attaches the second to the last *test* file it saw
#: manufactures a claim nobody made.
_ANY_QUALIFIED = re.compile(r"\b([A-Za-z0-9_][A-Za-z0-9_./-]*\.py)::([A-Za-z_][A-Za-z0-9_]*)")

#: The continuation form. House docs write a run of ids from ONE file as
#: ``test_x.py::test_a`` then ``::test_b``, ``::test_c`` — four of the eight
#: hermes citations the MCF-78 sweep set out to fix are written this way, and
#: the sweep itself only counted the ones spelled in full. A token is not the
#: unit of meaning.
_CONTINUATION = re.compile(r"(?<![\w.)\]])::([A-Za-z_][A-Za-z0-9_]*)")

#: How far back a ``::name`` continuation may look for the file it belongs to.
_CONTINUATION_WINDOW = 15

#: A ``tests/``-rooted path. ``(?<![\w/])`` keeps it from biting the tail of a
#: FOREIGN tree's path -- ``libs/cua-driver/rust/tests/integration/test_x.py``
#: is another repo's suite and is not this gate's business.
_PATH = re.compile(r"(?<![\w/])(tests/[A-Za-z0-9_./-]*?test_[A-Za-z0-9_]+\.py)")
_BARE = re.compile(r"(?<![\w/])(test_[A-Za-z0-9_]+\.py)")

#: How many lines back the claim phrase (and the negative-context exemption)
#: may live. Comment blocks and prose wrap: ``profile_runner.py`` writes
#: "the invariant is pinned by" on one ``#:`` line and the path on the next,
#: and a strictly line-oriented reader sees neither half as a claim. A recent
#: hermes sweep found exactly that defect in a gate whose lane pretty-printed.
_PHRASE_WINDOW = 2

#: A coverage assertion, not a mention. Kept narrow on purpose.
_CLAIM_PHRASE = re.compile(
    r"\b(pin|pins|pinned|guard|guards|guarded|gate|gates|witness|"
    r"cover|covers|covered|coverage|"
    r"assert|asserts|asserted|verif(?:y|ies|ied)|regression-locked|"
    r"prove|proves|proven|enforce|enforces|enforced|exercise|exercises|"
    r"exercised|tested|see|see also)\b",
    re.IGNORECASE,
)

#: A bare function id in prose: "covered by ``test_space_to_underscore`` in
#: ``tests/run_agent/test_repair_tool_call_name.py``". The ``::`` arm cannot
#: see this shape and the sweep that found it had to read for it by hand. Only
#: fires when a resolvable test PATH is in the same window, which is what makes
#: it checkable at all.
_MEMBER = re.compile(r"`(test_[a-z0-9_]+)`")

#: A sentence RECORDING a test's absence is not a claim of its presence.
#: A stated absence beats an implied presence, so it must not be punished.
_NEGATIVE_CONTEXT = re.compile(
    r"\b(deleted|delete|removed|retired|gone|dropped|never existed|"
    r"no longer|does not exist|doesn't exist|absent|missing|uncovered|"
    r"unpinned|renamed|supersed(?:e|ed|es)|would be|planned|proposed|"
    r"new file|to be written|not built|originally|formerly|previously|"
    r"used to|this line said|has never existed)\b",
    re.IGNORECASE,
)

#: The negative-context window is SYMMETRIC — a correction note writes the dead
#: id first and the words "which has never existed" on the NEXT line, and a
#: reader that only looks backwards reds on the very annotation that fixed the
#: defect, which teaches writers to delete the history instead of recording it.
_CONTEXT_BEFORE = 3
_CONTEXT_AFTER = 3

#: ...but the window is scoped to the SENTENCE around each match, not to whole
#: lines. This gate's own kill-proof caught the difference: seeding a bad
#: citation into doc 16's follow-up bullet stayed GREEN, because an unrelated
#: clause four words earlier in the same paragraph said "are removed" and the
#: line-window exemption swallowed the claim whole. A gate that cannot fail is
#: not a gate. Boundaries: sentence-final punctuation followed by whitespace, a
#: blank line, or the start of a list item / table row.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s|\n[ \t]*\n|\n[ \t]*(?=[-*+|])")


def _blank(pattern: re.Pattern[str], text: str) -> str:
    """Mask every match with spaces, PRESERVING LENGTH.

    Offsets into the masked string must still index the original line, because
    the sentence-scoped context lookup is computed from them. A plain
    ``pattern.sub(" ", text)`` shifts every later match and silently reads the
    wrong sentence.
    """

    return pattern.sub(lambda m: " " * (m.end() - m.start()), text)


#: Comment / docstring / markdown line furniture, stripped before the phrase
#: and negation regexes read a span. Without this, "which never\n# existed"
#: does not match ``never existed`` -- the reader would be line-oriented again,
#: just one level down, and a correction note would red on itself.
_LINE_FURNITURE = re.compile(r"(?m)^[ \t]*(?:#:?|\*|>|//)[ \t]?")


def _local_context(window: str, offset: int) -> str:
    """The sentence-ish span of ``window`` containing ``offset``, normalised."""

    start, end = 0, len(window)
    for match in _SENTENCE_BREAK.finditer(window):
        position = match.end()
        if position <= offset:
            start = position
        else:
            end = match.start()
            break
    span = _LINE_FURNITURE.sub("", window[start:end])
    return " ".join(span.split())

#: Whole-document exemption for a plan. The phrase rule already handles most
#: planning prose; this catches a plan that writes in the present tense.
#: ``active`` counts: a plan whose status is ACTIVE is by definition work that
#: has not landed, and its Files/Tests lists are proposals.
_PLAN_DOC = re.compile(
    r"^\s*>?\s*\*{0,2}(?:status|state)\*{0,2}\s*[:=]\s*\**\s*"
    r"(planned|proposed|proposal|draft|plan\b|active|wip|in progress|todo)",
    re.IGNORECASE | re.MULTILINE,
)

#: House naming: ``*_PLAN_*.md`` / ``*-plan.md`` is a staged implementation
#: plan, and its Files / Tests / Gate rows are proposals until they land.
_PLAN_NAME = re.compile(r"(?:^|[_-])plan(?:[_-]|\.md$)", re.IGNORECASE)

#: ...UNLESS the doc has since been stamped done. That flip -- status line
#: changes, nobody re-reads the matrix -- is exactly how the launcher's
#: nexus-a/g and stage-L2/L5/L7 docs rotted into false claims.
#: Must be a STATUS DECLARATION, not any occurrence of the word. A plan whose
#: prose happens to say "WV-L6's caller landed" is still a plan; a bare
#: word-match here silently un-exempts half the plan corpus and the gate starts
#: reddening on proposals.
_COMPLETED_STAMP = re.compile(
    r"^\s*>?\s*\*{0,2}(?:status|state)\*{0,2}\s*[:=]\s*\**\s*"
    r"(shipped|landed|complete|completed|done|final|verified complete)",
    re.IGNORECASE | re.MULTILINE,
)

_FENCE = re.compile(r"^\s*(?:```|~~~)")


def _is_plan_doc(path: Path, text: str) -> bool:
    head = text[:2500]
    if _COMPLETED_STAMP.search(head):
        return False
    return bool(_PLAN_DOC.search(head)) or bool(_PLAN_NAME.search(path.name))

_WHAT_THIS_GATE_CANNOT_SEE = (
    "\n"
    "NOTE ON WHAT A GREEN RUN HERE DOES NOT MEAN:\n"
    "  This gate is an EXISTENCE check. It proves the named test resolves.\n"
    "  It CANNOT prove that test asserts what the prose claims. A test that\n"
    "  exists but pins something else (the VACUOUS class) is invisible here\n"
    "  and always will be -- the launcher's sibling gate scores 33/35 and the\n"
    "  2 it misses are exactly the vacuous pair. Do not read green as\n"
    "  'the seam is covered'; read it as 'the citation is not rotted'.\n"
)


# ── corpus ───────────────────────────────────────────────────────────────────


def _read(path: Path) -> str:
    """Always UTF-8. Never let a cp1252 decode invent or lose a citation."""

    return path.read_text(encoding="utf-8", errors="replace")


def _md_files() -> list[Path]:
    out: list[Path] = []
    for name in _MD_DIRS:
        root = REPO_ROOT / name
        if root.is_dir():
            out.extend(sorted(root.rglob("*.md")))
    out.extend(sorted(REPO_ROOT.glob("*.md")))
    return out


#: This file is excluded from its own corpus. Its docstrings quote claim
#: SYNTAX (``test_x.py::test_y``) as illustration rather than asserting that
#: any such test exists, and a gate that reddened on its own examples would be
#: unfixable except by making the documentation worse. The exclusion is
#: deliberate, narrow, and stated here rather than left for a reader to
#: discover -- a stated absence beats an implied presence.
_SELF = Path(__file__).resolve()


def _py_files() -> list[Path]:
    out: list[Path] = []
    for name in _PY_DIRS:
        root = REPO_ROOT / name
        if root.is_dir():
            out.extend(p for p in sorted(root.rglob("*.py")) if p.resolve() != _SELF)
    return out


_TEST_FILE_INDEX: dict[str, list[Path]] | None = None


def _test_files_by_name() -> dict[str, list[Path]]:
    """Filename -> every test file on disk carrying it. Walked ONCE.

    Cached deliberately: an uncached rglob per claim turns a 90-claim scan into
    tens of thousands of directory walks and the gate times out instead of
    failing, which reads to a reader exactly like flakiness.
    """

    global _TEST_FILE_INDEX
    if _TEST_FILE_INDEX is None:
        index: dict[str, list[Path]] = {}
        if TESTS_ROOT.is_dir():
            for path in TESTS_ROOT.rglob("test_*.py"):
                index.setdefault(path.name, []).append(path)
        _TEST_FILE_INDEX = index
    return _TEST_FILE_INDEX


_NAME_CACHE: dict[Path, set[str]] = {}


def _defined_names(path: Path) -> set[str]:
    """Every def/class/assignment name in a test file, at any nesting depth.

    AST, not substring: ``"test_foo"`` appearing inside a string literal (a
    tombstone ledger, an id list, an error message) must NOT satisfy a claim
    that ``test_foo`` is a test. Module- and class-level constants count --
    ``test_telegram_noise_filter.py::VISIBLE_COMPRESSION_MESSAGES`` is a real,
    resolvable citation of a parametrize table.
    """

    if path in _NAME_CACHE:
        return _NAME_CACHE[path]
    try:
        tree = ast.parse(_read(path))
    except SyntaxError:  # pragma: no cover - a broken test file is its own red
        _NAME_CACHE[path] = set()
        return _NAME_CACHE[path]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    _NAME_CACHE[path] = names
    return names


# ── the reader ───────────────────────────────────────────────────────────────


class Claim(tuple):
    """(arm, source, lineno, target, line) — a tuple so pytest prints it flat."""

    __slots__ = ()

    @property
    def arm(self) -> str:
        return self[0]

    @property
    def source(self) -> str:
        return self[1]

    @property
    def lineno(self) -> int:
        return self[2]

    @property
    def target(self) -> str:
        return self[3]


def _comment_and_docstring_lines(text: str) -> list[tuple[int, str]]:
    """(lineno, text) for comment lines and docstring lines only.

    Live code is excluded: ``importlib.import_module("tests.x")`` is machinery,
    not a claim, and a gate that reads it would red on its own helpers.
    """

    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            out.append((i, line))
    try:
        with warnings.catch_warnings():
            # Upstream files carry unescaped regex literals; their SyntaxWarnings
            # are noise from THIS gate's parse, not a finding of it.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc or not node.body:
            continue
        start = getattr(node.body[0], "lineno", 1)
        for offset, doc_line in enumerate(doc.splitlines()):
            out.append((start + offset, doc_line))
    return out


def _scan_lines(
    source: str,
    lines: list[tuple[int, str]],
    *,
    is_plan_doc: bool,
    fenced: frozenset[int] = frozenset(),
) -> list[Claim]:
    """Extract claims from (lineno, text) pairs of ONE source file."""

    claims: list[Claim] = []
    #: The last ``<something>.py`` seen before a ``::``, and where. A
    #: continuation only inherits it when it was a TEST file.
    anchor_file: str | None = None
    anchor_line = -(10**9)
    texts = [text for _, text in lines]

    for index, (lineno, line) in enumerate(lines):
        prev_tail = texts[index - 1] if index else ""
        before = texts[max(0, index - _CONTEXT_BEFORE) : index]
        after = texts[index + 1 : index + _CONTEXT_AFTER + 1]
        window = "\n".join([*before, line, *after])
        line_offset = sum(len(t) + 1 for t in before)

        def _around(match_start: int) -> str:
            return _local_context(window, line_offset + match_start)

        def _negated(match_start: int) -> bool:
            return bool(_NEGATIVE_CONTEXT.search(_around(match_start)))

        def _claimed(match_start: int) -> bool:
            return bool(_CLAIM_PHRASE.search(_around(match_start)))

        # ── NAMED, in source order so an intervening production id resets the
        #    anchor rather than being absorbed by the previous test file.
        events = [(m.start(), "q", m) for m in _ANY_QUALIFIED.finditer(line)]
        events += [
            (m.start(), "c", m)
            for m in _CONTINUATION.finditer(line)
            if not any(q.start() <= m.start() < q.end() for _, _, q in events)
        ]
        for _, kind, match in sorted(events, key=lambda e: e[0]):
            if kind == "q":
                anchor_file, anchor_line = match.group(1), lineno
                filename = Path(match.group(1)).name
                if filename.startswith("test_") and not _negated(match.start()):
                    claims.append(
                        Claim(("NAMED", source, lineno, f"{filename}::{match.group(2)}", line.strip()))
                    )
                continue
            if anchor_file is None or lineno - anchor_line > _CONTINUATION_WINDOW:
                continue
            if _negated(match.start()):
                continue
            filename = Path(anchor_file).name
            if not filename.startswith("test_"):
                continue
            claims.append(
                Claim(("NAMED", source, lineno, f"{filename}::{match.group(1)}", line.strip()))
            )

        rest = _blank(_ANY_QUALIFIED, line)

        if lineno in fenced or is_plan_doc:
            continue

        window_paths = [
            m.group(1) for m in _PATH.finditer(_blank(_ANY_QUALIFIED, window))
        ]
        for match in _PATH.finditer(rest):
            if _negated(match.start()) or not _claimed(match.start()):
                continue
            claims.append(Claim(("PATH", source, lineno, match.group(1), line.strip())))
        for match in _MEMBER.finditer(rest):
            if _negated(match.start()) or not _claimed(match.start()):
                continue
            # A backticked ``test_x`` that is itself a test FILE stem is a file
            # reference written without its extension, not a function id; the
            # BARE/PATH arms own it and the MEMBER arm must not invent a
            # member claim out of it.
            if f"{match.group(1)}.py" in _test_files_by_name():
                continue
            # "…is asserted in ``test_x`` BELOW" — a test file naming its own
            # member. It belongs to THIS file, not to whichever sibling path
            # happens to be nearest in the window.
            if source.startswith("tests/") and match.group(1) in _defined_names(
                REPO_ROOT / source
            ):
                continue
            for path in window_paths:
                if (REPO_ROOT / path).is_file():
                    claims.append(
                        Claim(("MEMBER", source, lineno, f"{path}::{match.group(1)}", line.strip()))
                    )
                    break
        remainder = _blank(_PATH, rest)
        # Rejoin a path broken by a line wrap (``.../integration/`` then
        # ``test_x.py``) so the ``(?<![\w/])`` guard can still see the slash.
        joined_prefix = prev_tail.rstrip() if prev_tail.rstrip().endswith("/") else ""
        for match in _BARE.finditer(remainder):
            if _negated(match.start()) or not _claimed(match.start()):
                continue
            if joined_prefix and match.start() == len(remainder) - len(remainder.lstrip()):
                continue  # first token on the line continues a foreign path
            claims.append(Claim(("BARE", source, lineno, match.group(1), line.strip())))

    return claims


def _fenced_linenos(text: str) -> frozenset[int]:
    inside = False
    out: set[int] = set()
    for i, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            inside = not inside
            out.add(i)
            continue
        if inside:
            out.add(i)
    return frozenset(out)


def collect_claims() -> tuple[list[Claim], dict[str, int]]:
    claims: list[Claim] = []
    census = {"md_files": 0, "py_files": 0, "test_files": 0}

    for path in _md_files():
        census["md_files"] += 1
        text = _read(path)
        rel = path.relative_to(REPO_ROOT).as_posix()
        claims.extend(
            _scan_lines(
                rel,
                list(enumerate(text.splitlines(), 1)),
                is_plan_doc=_is_plan_doc(path, text),
                fenced=_fenced_linenos(text),
            )
        )

    for path in _py_files():
        census["py_files"] += 1
        text = _read(path)
        # Cheap pre-filter. A file with no ``test_`` substring anywhere cannot
        # carry a claim under any arm, and AST-parsing ~3.9k files to learn
        # that costs more than the whole rest of the gate. Counted in the
        # census either way, so the floor still proves the walk happened.
        if "test_" not in text:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        claims.extend(
            _scan_lines(rel, _comment_and_docstring_lines(text), is_plan_doc=False)
        )

    by_name = _test_files_by_name()
    census["test_files"] = sum(len(v) for v in by_name.values())
    return claims, census


def _unresolved(claims: list[Claim]) -> list[tuple[Claim, str]]:
    by_name = _test_files_by_name()
    bad: list[tuple[Claim, str]] = []

    for claim in claims:
        target = claim.target
        if claim.arm == "MEMBER":
            path, _, member = target.partition("::")
            if member not in _defined_names(REPO_ROOT / path):
                bad.append((claim, f"{path} defines no {member!r}"))
        elif claim.arm == "NAMED":
            filename, _, member = target.partition("::")
            candidates = by_name.get(filename)
            if not candidates:
                bad.append((claim, f"no test file named {filename} exists under tests/"))
                continue
            for candidate in candidates:
                if member in _defined_names(candidate):
                    break
            else:
                where = ", ".join(
                    c.relative_to(REPO_ROOT).as_posix() for c in sorted(candidates)
                )
                bad.append((claim, f"{filename} exists ({where}) but defines no {member!r}"))
        elif claim.arm == "PATH":
            if not (REPO_ROOT / target).exists():
                bad.append((claim, f"path {target} does not exist"))
        else:  # BARE
            if target not in by_name:
                bad.append((claim, f"no test file named {target} exists under tests/"))
    return bad


def _suggest(target: str) -> str:
    """Nearest existing names — most findings of this class are pure renames."""

    import difflib

    filename = target.split("::")[0]
    by_name = _test_files_by_name()
    if "::" in target:
        member = target.split("::", 1)[1]
        pool: set[str] = set()
        for candidate in by_name.get(filename, []):
            pool |= {n for n in _defined_names(candidate) if n.startswith(("test_", "Test"))}
        if pool:
            near = difflib.get_close_matches(member, sorted(pool), n=3, cutoff=0.45)
            return ", ".join(f"{filename}::{n}" for n in near) if near else "(no near name in that file)"
    near = difflib.get_close_matches(filename, sorted(by_name), n=3, cutoff=0.5)
    return ", ".join(near) if near else "(no near filename)"


# ── the gate ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def scan() -> tuple[list[Claim], dict[str, int]]:
    return collect_claims()


def test_every_coverage_claim_names_a_test_that_exists(scan) -> None:
    """The gate. A doc-named test id must resolve to something on disk.

    Being green here means NO CITATION IS ROTTED. It does not mean any seam is
    covered -- see the module docstring and the failure text below.
    """

    claims, _ = scan
    bad = _unresolved(claims)
    if not bad:
        return
    report = "\n".join(
        f"  [{claim.arm}] {claim.source}:{claim.lineno}\n"
        f"      claim : {claim.target}\n"
        f"      why   : {reason}\n"
        f"      near  : {_suggest(claim.target)}\n"
        f"      line  : {claim[4][:160]}"
        for claim, reason in bad
    )
    pytest.fail(
        f"{len(bad)} coverage claim(s) name a test that does not exist.\n\n"
        f"{report}\n\n"
        "Fix each by REPOINTING at the test that really covers the behaviour, "
        "or -- where no such test exists and the seam is real -- by saying "
        "UNCOVERED SEAM in the doc. Do not just delete the sentence: a false "
        "claim replaced by silence reads the same to the next reader.\n"
        + _WHAT_THIS_GATE_CANNOT_SEE,
        pytrace=False,
    )


#: Floors, set below today's measured corpus with room for ordinary churn.
#: A collapse means the extractor broke, not that the docs got honest.
#: Measured at MCF-78 remediation (2026-08-20, this commit): md 119, py 3881,
#: test files 2934, NAMED 46, PATH 198, BARE 140, MEMBER 3. Floors sit under
#: those with room for ordinary churn.
#:
#: MEMBER's population is genuinely small (3), so its floor is weak evidence on
#: its own -- say so rather than dress it up. Its real proof is the synthetic
#: case in :func:`test_the_reader_finds_a_planted_claim_and_clears_a_real_one`,
#: which is independent of how many the tree happens to contain today.
_FLOORS = {
    "md_files": 100,
    "py_files": 3000,
    "test_files": 2000,
    "NAMED": 30,
    "PATH": 120,
    "BARE": 90,
    "MEMBER": 1,
}


def test_the_gate_scanned_a_real_corpus(scan) -> None:
    """MCF-53's lesson, applied to this gate: prove the census is non-empty.

    32 of 155 rows in that sweep scanned ZERO files and were green from the day
    they were written, because the scope token had to be a DIRECTORY and
    theirs were files. ``assert offenders == []`` over nothing is the disease.
    """

    claims, census = scan
    measured = dict(census)
    for arm in ("NAMED", "PATH", "BARE", "MEMBER"):
        measured[arm] = sum(1 for c in claims if c.arm == arm)

    short = {k: (measured[k], floor) for k, floor in _FLOORS.items() if measured[k] < floor}
    assert not short, (
        "the coverage-claim reader scanned less than it must -- the extractor "
        f"is broken, not the docs. measured vs floor: {short}. Full census: {measured}"
    )


def test_the_reader_finds_a_planted_claim_and_clears_a_real_one(tmp_path) -> None:
    """Pin the reader against synthetic text, independent of the tree.

    Without this, a regex that stopped matching anything would still leave the
    main gate green -- the exact failure mode the floors above also guard, from
    the other side.
    """

    real_named = "test_coverage_claims_resolve.py::test_the_gate_scanned_a_real_corpus"
    text = "\n".join(
        [
            "# real, resolvable:",
            f"the census floor is pinned by `{real_named}`",
            "`::test_the_reader_finds_a_planted_claim_and_clears_a_real_one` too",
            "the path is covered by `tests/test_coverage_claims_resolve.py`",
            "covered by `test_ast_resolution_is_not_substring_resolution` in that file",
            "",
            "# planted, unresolvable:",
            "the seam is pinned by `test_coverage_claims_resolve.py::test_a_name_that_is_not_there`",
            "and covered by `tests/agent_runtime/test_a_file_that_is_not_there.py`",
            "and covered by `test_another_file_that_is_not_there.py`",
            "",
            "# must NOT count -- a recorded absence, not a claim:",
            "`tests/agent_runtime/test_repo_bundles.py` was deleted with the module",
            "# must NOT count -- a plan line with no coverage phrase:",
            "- Test: `tests/agent_runtime/test_some_future_thing.py`",
        ]
    )
    lines = list(enumerate(text.splitlines(), 1))
    claims = _scan_lines("<synthetic>", lines, is_plan_doc=False)

    arms = {
        arm: [c for c in claims if c.arm == arm]
        for arm in ("NAMED", "PATH", "BARE", "MEMBER")
    }
    assert len(arms["NAMED"]) == 3, arms["NAMED"]
    assert len(arms["PATH"]) == 2, arms["PATH"]
    assert len(arms["BARE"]) == 1, arms["BARE"]
    assert len(arms["MEMBER"]) == 1, arms["MEMBER"]
    assert arms["MEMBER"][0].target == (
        "tests/test_coverage_claims_resolve.py::test_ast_resolution_is_not_substring_resolution"
    )

    bad = _unresolved(claims)
    assert len(bad) == 3, bad
    assert {c.target for c, _ in bad} == {
        "test_coverage_claims_resolve.py::test_a_name_that_is_not_there",
        "tests/agent_runtime/test_a_file_that_is_not_there.py",
        "test_another_file_that_is_not_there.py",
    }

    # The continuation form must inherit the file from the id above it, and
    # must NOT reach across an arbitrary distance.
    far = _scan_lines(
        "<synthetic-far>",
        [(1, "`test_coverage_claims_resolve.py::test_the_gate_scanned_a_real_corpus`")]
        + [(n, "filler") for n in range(2, 40)]
        + [(40, "`::test_a_name_that_is_not_there`")],
        is_plan_doc=False,
    )
    assert [c.target for c in far] == [
        "test_coverage_claims_resolve.py::test_the_gate_scanned_a_real_corpus"
    ]


def test_ast_resolution_is_not_substring_resolution() -> None:
    """A name inside a STRING must not satisfy a claim that it is a test.

    Tombstone ledgers, parametrize id lists and error messages are full of
    ``"test_..."`` strings. Substring resolution would let a doc cite a test
    that only exists as a quoted id in another test's data table.
    """

    scratch = TESTS_ROOT / "_coverage_claim_ast_probe.py"
    assert not scratch.exists(), "probe path collided with a real file"
    names = _defined_names(Path(__file__))
    assert "test_ast_resolution_is_not_substring_resolution" in names
    assert "_FLOORS" in names  # module constants resolve -- a real citation form
    assert "test_a_name_that_is_not_there" not in names, (
        "that identifier appears in this file ONLY inside string literals; if "
        "the resolver sees it, it is reading text, not the AST"
    )
