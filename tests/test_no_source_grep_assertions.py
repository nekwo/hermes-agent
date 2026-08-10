"""The source-grep test class stops growing here.

A test that reads a function's TEXT and asserts a substring appears in it
proves only that the characters exist. It never proves the branch runs, and it
never proves the branch does what the substring implies. The class fails in
both directions, and both directions were observed on this tree:

* **False RED.** Three gates fired on pure renames/code-moves on 2026-08-09 —
  the ``test_s56_config_block_removal`` roster gate and two in
  ``tests/agent/test_nous_oauth_401_guidance.py``. Nothing behavioural changed;
  the text had merely moved to a neighbouring function. The gate was measuring
  which function a line of prose sits in.
* **False GREEN.** Replaying a real collector removal left an old positive gate
  passing on the same tree, while a correctly-aimed gate went red. Deleting a
  caller while leaving a residual mention keeps every positive grep green.

So the ruling, and what this file enforces:

    Ban ``inspect.getsource`` in tests unless the result is fed to
    ``ast.parse`` and asserted against a resolved AST node, and forbid any
    POSITIVE ``in``-assertion over ``getsource`` output — a positive claim must
    be proven by executing the path.

The asymmetry is the whole point, and it is why the rule is adoptable today:

* ``assert "foo" in getsource(f)`` — **BANNED.** "The text exists" does not
  imply "the branch runs".
* ``assert "foo" not in getsource(f)`` — **ALLOWED.** These are the tombstone /
  retirement gates. Text absence is exactly the guarantee they claim, so the
  grep and the guarantee are the same statement. ~10 of these exist and stay.
* ``ast.parse(inspect.getsource(...))`` then asserting on resolved nodes —
  **APPROVED.** ~25 files already do this (see
  ``tests/agent_runtime/test_s29_snapshot_dead_local_removal.py`` for the
  canonical shape). ``ast.parse`` is the taint killer below.

Existing violations are NOT migrated. They are ledgered in
``tests/source_grep_debt.txt``, which is a debt register, not a config blob:
the count in its header is asserted against the entry count, entries are
ordered, and a ledgered violation that no longer exists FAILS this gate rather
than rotting in place. Paying debt down never requires editing this file.

Known bounds, stated rather than hidden — a gate that overclaims its reach is
the same lie it is here to stop:

1. The ruling names ``in``-assertions, so that is what is detected: ``needle in
   source`` in an ``assert`` (or a ``pytest.fail`` guard), plus ``assertIn``.
   Other positive text claims over the same string — ``source.count(...) == 2``,
   ``re.search(pattern, source)``, ``source.startswith(...)`` — are the same
   defect class wearing different syntax and are NOT caught.
2. A membership test used as a comprehension FILTER is not classified, because
   its direction lives in the later assertion, not in the filter. ``offenders =
   [m for m in mods if "x" in getsource(m)]; assert offenders == []`` is a
   correct absence gate (tests/agent_runtime/test_s27_vestigial_proof_store_chain.py)
   and must not go red; ``registered = {n for n in names if n in src}; assert
   not names - registered`` is a positive claim in the same shape
   (tests/monitoring/test_cron_health_export.py) and escapes. Deciding between
   them needs dataflow this gate deliberately does not carry.
3. A membership test hidden behind a helper that returns ``bool`` — ``def
   _has(n): return n in src`` then ``assert _has("x")`` — escapes for the same
   reason.

Widening any of these means baselining whatever it newly finds; do that as its
own pass, with its own ruling.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = REPO_ROOT / "tests"
LEDGER_PATH = TESTS_ROOT / "source_grep_debt.txt"

#: ``inspect.getsource``/``getsourcelines`` under any import spelling. Both hand
#: back the module's own text; ``getsourcefile`` does not and is not covered.
_SOURCE_READERS = frozenset({"getsource", "getsourcelines"})

#: How few files would mean the walker itself broke rather than the tree being
#: clean. The tree carries ~2.9k modules under tests/; a collapse to double
#: digits is the walker drifting, not a cleanup.
_MIN_SCANNED_FILES = 500

#: The two things a source-derived value can be. ``TEXT`` is characters and can
#: only ever prove characters exist. ``TREE`` is a resolved ``ast`` node, which
#: is the sanctioned form — membership over what comes off a tree is a
#: structural claim about the code, not a grep over its rendering.
TEXT = "text"
TREE = "tree"


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _strongest(kinds) -> str | None:
    kinds = set(kinds)
    if TEXT in kinds:
        return TEXT
    if TREE in kinds:
        return TREE
    return None


def _fails_or_raises(node: ast.AST) -> bool:
    """Does this block end the test loudly?"""

    for child in ast.walk(node):
        if isinstance(child, ast.Raise):
            return True
        if isinstance(child, ast.Call) and _called_name(child) in {"fail", "xfail"}:
            return True
    return False


class _SourceTextAnalyzer:
    """Decides, per test module, which membership tests are POSITIVE claims over
    raw source TEXT.

    Three things are modelled, because getting any of them wrong turns this gate
    into the disease it treats.

    **Text and tree are tracked separately.** ``ast.parse`` converts TEXT to
    TREE and ``ast.unparse`` converts it straight back — a helper that parses,
    strips docstrings and re-renders (``_code_without_prose`` in
    tests/agent_runtime/test_s48_cli_entity_row_consolidation.py) hands back
    TEXT and its greps are greps. Only a claim over a value that is still a
    resolved node is structural.

    **Taint is scope-local.** A module-wide taint set reads one
    ``source = inspect.getsource(...)`` and then condemns every unrelated
    ``result``/``src`` in the file — tests/gateway/test_voice_command.py alone
    has a dozen ``assert "disabled" in result.lower()`` rows with nothing to do
    with source text.

    **Polarity is tracked, not inferred from syntax.** ``assert not any(n in src
    ...)`` and ``if "x" in src: pytest.fail(...)`` are NEGATIVE claims wearing
    ``in``; ``if "x" not in src: pytest.fail(...)`` is a POSITIVE claim wearing
    ``not in``. The ban is on the claim's direction, so polarity is carried
    through ``not`` / ``any`` / ``all`` / comprehensions and inverted under a
    fail-guard.
    """

    def __init__(self, tree: ast.Module) -> None:
        self._tree = tree
        # Locally-defined functions classified by the kind their RETURN carries:
        #   _returns_always  -> that kind with nothing seeded (it reads source itself)
        #   _returns_if_text -> that kind when handed TEXT
        #   _returns_if_tree -> that kind when handed a TREE
        # The last two are separate because helpers are routinely kind-preserving:
        # ``_const_args(call)`` hands back constants off a resolved node (TREE in,
        # TREE out) while ``_code_without_prose(src)`` parses and re-renders
        # (TEXT in, TEXT out). Collapsing them into one "takes source" bucket
        # reports every AST helper in the tree as a grep.
        # A helper in none of the three LAUNDERS the taint, which is exactly what
        # ``_extract_dict_keys`` (parses, returns dict keys) does next door to
        # ``_terminal_tool_env_var_names`` (regex-scrapes, returns text-derived
        # names) in tests/tools/test_terminal_config_env_sync.py.
        self._returns_always: dict[str, str] = {}
        self._returns_if_text: dict[str, str] = {}
        self._returns_if_tree: dict[str, str] = {}
        self._local_functions: set[str] = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self._classify_local_functions()

    # -- what kind of value is this expression? ------------------------------

    def _kind(self, node: ast.AST | None, text: frozenset[str], tree: frozenset[str]) -> str | None:
        if node is None:
            return None
        if isinstance(node, ast.Call):
            return self._call_kind(node, text, tree)
        if isinstance(node, ast.Name):
            if node.id in text:
                return TEXT
            if node.id in tree:
                return TREE
            return None
        return _strongest(
            self._kind(child, text, tree) for child in ast.iter_child_nodes(node)
        )

    def _call_kind(self, node: ast.Call, text: frozenset[str], tree: frozenset[str]) -> str | None:
        name = _called_name(node)
        arguments = [*node.args, *(kw.value for kw in node.keywords)]
        argument_kinds = [self._kind(arg, text, tree) for arg in arguments]

        if name in _SOURCE_READERS:
            return TEXT
        if name == "parse":
            return TREE if TEXT in argument_kinds else None
        if name == "unparse":
            # Round-tripping back to characters is a grep again, however the
            # tree in the middle was obtained.
            return TEXT if _strongest(argument_kinds) else None
        if name in self._local_functions:
            if name in self._returns_always:
                return self._returns_always[name]
            if TEXT in argument_kinds:
                return self._returns_if_text.get(name)
            if TREE in argument_kinds:
                return self._returns_if_tree.get(name)
            return None
        # A method or builtin (``.lower()``, ``.split()``, ``"".join(...)``,
        # ``re.findall(...)``, ``ast.walk(...)``) carries its input's kind
        # through. ``iter_child_nodes`` covers ``call.func`` too, which is where
        # the receiver of a method call lives.
        return _strongest(
            self._kind(child, text, tree) for child in ast.iter_child_nodes(node)
        )

    # -- taint ---------------------------------------------------------------

    @staticmethod
    def _bound_names(target: ast.AST) -> set[str]:
        return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}

    @staticmethod
    def _bindings_in_scope(scope: ast.AST):
        """Every ``(target, value)`` binding written directly in this scope.

        Nested function bodies are skipped — they get their own scope, seeded
        with this one's result.
        """

        def walk(node: ast.AST):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(child, ast.Assign):
                    for target in child.targets:
                        yield target, child.value
                elif isinstance(child, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                    yield child.target, child.value
                elif isinstance(child, (ast.For, ast.AsyncFor)):
                    yield child.target, child.iter  # ``for src in (getsource(a), ...)``
                elif isinstance(child, (ast.With, ast.AsyncWith)):
                    for item in child.items:
                        if item.optional_vars is not None:
                            yield item.optional_vars, item.context_expr
                elif isinstance(child, ast.comprehension):
                    yield child.target, child.iter
                yield from walk(child)

        yield from walk(scope)

    def _taint_for_scope(
        self, scope: ast.AST, text: frozenset[str], tree: frozenset[str]
    ) -> tuple[frozenset[str], frozenset[str]]:
        text_names, tree_names = set(text), set(tree)
        bindings = list(self._bindings_in_scope(scope))
        # Flow-insensitive fixpoint: a name carries a kind if ANY binding of it
        # in this scope does, TEXT winning over TREE. Deliberately over- rather
        # than under-approximate — a test that reuses one name for source text
        # and for something else is already unreadable.
        for _ in range(len(bindings) + 1):
            grew = False
            for target, value in bindings:
                kind = self._kind(value, frozenset(text_names), frozenset(tree_names))
                if kind is None:
                    continue
                sink = text_names if kind is TEXT else tree_names
                for name in self._bound_names(target):
                    if name not in sink:
                        sink.add(name)
                        grew = True
            if not grew:
                break
        return frozenset(text_names), frozenset(tree_names)

    def _classify_local_functions(self) -> None:
        module_text, module_tree = self._taint_for_scope(
            self._tree, frozenset(), frozenset()
        )
        defs = [
            node
            for node in ast.walk(self._tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        def return_kind(func, text: frozenset[str], tree: frozenset[str]) -> str | None:
            local_text, local_tree = self._taint_for_scope(func, text, tree)
            return _strongest(
                self._kind(node.value, local_text, local_tree)
                for node in ast.walk(func)
                if isinstance(node, ast.Return) and node.value is not None
            )

        for _ in range(len(defs) + 1):
            grew = False
            for func in defs:
                params = frozenset(
                    arg.arg
                    for arg in [
                        *func.args.posonlyargs,
                        *func.args.args,
                        *func.args.kwonlyargs,
                    ]
                )
                # Three probes of the same body: unseeded, then with every
                # parameter standing in for TEXT, then for a TREE.
                for store, text_seed, tree_seed in (
                    (self._returns_always, module_text, module_tree),
                    (self._returns_if_text, module_text | params, module_tree),
                    (self._returns_if_tree, module_text, module_tree | params),
                ):
                    kind = return_kind(func, text_seed, tree_seed)
                    if kind and store.get(func.name) != kind:
                        store[func.name] = kind
                        grew = True
            if not grew:
                break

    # -- claims --------------------------------------------------------------

    @classmethod
    def _membership_claims(cls, node: ast.AST | None, positive: bool):
        """``(compare, op, comparator, positive)`` for every membership test in
        an asserted expression, carrying the claim's true direction."""

        if node is None:
            return
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield from cls._membership_claims(node.operand, not positive)
        elif isinstance(node, ast.BoolOp):
            for value in node.values:
                yield from cls._membership_claims(value, positive)
        elif isinstance(node, ast.Compare):
            for op, comparator in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    yield node, op, comparator, positive
        elif isinstance(node, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
            # ``any(n in src for n in NAMES)`` keeps the direction. The ``ifs``
            # are filters that build a value, not claims about it.
            yield from cls._membership_claims(node.elt, positive)
        elif isinstance(node, ast.IfExp):
            yield from cls._membership_claims(node.body, positive)
            yield from cls._membership_claims(node.orelse, positive)
        elif isinstance(node, ast.Call):
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                yield from cls._membership_claims(arg, positive)
        else:
            for child in ast.iter_child_nodes(node):
                yield from cls._membership_claims(child, positive)

    # -- violation discovery -------------------------------------------------

    def violations(self) -> list[tuple[int, str]]:
        """``(lineno, unparsed assertion)`` for every POSITIVE ``in`` over source
        text, in source order."""

        found: list[tuple[int, str]] = []
        self._visit(self._tree, frozenset(), frozenset(), sink=found)
        found.sort()
        return found

    def _visit(self, scope: ast.AST, text: frozenset[str], tree: frozenset[str], sink) -> None:
        scope_text, scope_tree = self._taint_for_scope(scope, text, tree)
        for node in self._nodes_in_scope(scope):
            if isinstance(node, ast.Call) and _called_name(node) == "assertIn":
                # unittest's spelling of the same claim. ``assertNotIn`` is the
                # sanctioned tombstone form and is left alone.
                if len(node.args) >= 2 and self._kind(node.args[1], scope_text, scope_tree) is TEXT:
                    sink.append((node.lineno, ast.unparse(node)))
                continue
            if isinstance(node, ast.Assert):
                claims = self._membership_claims(node.test, True)
            elif isinstance(node, ast.If) and not node.orelse and _fails_or_raises(node):
                # ``if <test>: pytest.fail(...)`` asserts the NEGATION of the
                # test, so the claim's direction flips.
                claims = self._membership_claims(node.test, False)
            else:
                continue
            for compare, op, comparator, positive in claims:
                asserts_presence = positive is isinstance(op, ast.In)
                if asserts_presence and self._kind(comparator, scope_text, scope_tree) is TEXT:
                    sink.append((compare.lineno, ast.unparse(compare)))
        for child in ast.iter_child_nodes(scope):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._visit(child, scope_text, scope_tree, sink)
            elif isinstance(child, ast.ClassDef):
                # A class body does not close over its methods' locals.
                self._visit(child, text, tree, sink)

    @staticmethod
    def _nodes_in_scope(scope: ast.AST):
        """Nodes belonging to this scope, not to a nested def/class."""

        def walk(node: ast.AST):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                yield child
                yield from walk(child)

        yield from walk(scope)


def _qualname_index(tree: ast.Module) -> dict[int, str]:
    """Line number -> enclosing ``Class.func`` qualname, for identity keys.

    Line numbers key the LOOKUP only; they never reach the ledger. The ledger
    entry is path + qualname + the unparsed comparison, so reformatting a file
    or inserting a helper above a violation does not churn the register.
    """

    index: dict[int, str] = {}

    def walk(node: ast.AST, prefix: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + (child.name,)
                end = getattr(child, "end_lineno", child.lineno) or child.lineno
                for line in range(child.lineno, end + 1):
                    index[line] = ".".join(name)
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, ())
    return index


def _entry(relpath: str, qualname: str, text: str) -> str:
    return f"{relpath}::{qualname}::{text}"


def _scan() -> tuple[list[str], int]:
    """Every positive source-grep assertion under ``tests/``, as ledger keys."""

    entries: list[str] = []
    scanned = 0
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        scanned += 1
        if "getsource" not in text:
            # Nothing can be source text without a reader in the module. This is
            # a speed guard, not the check.
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            # A test module that does not parse is a different, louder failure.
            continue
        relpath = path.relative_to(REPO_ROOT).as_posix()
        qualnames = _qualname_index(tree)
        for lineno, rendered in _SourceTextAnalyzer(tree).violations():
            entries.append(_entry(relpath, qualnames.get(lineno, "<module>"), rendered))
    entries.sort()
    return entries, scanned


def _read_ledger() -> tuple[list[str], int | None]:
    """Ledger entries plus the count its header declares (``None`` if absent)."""

    declared: int | None = None
    entries: list[str] = []
    for raw in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            marker = "grandfathered violations:"
            if marker in line:
                declared = int(line.split(marker, 1)[1].strip())
            continue
        entries.append(line)
    entries.sort()
    return entries, declared


_REMEDY = (
    "\n\nA positive substring claim over a function's TEXT proves the characters "
    "exist, never that the branch runs — it goes false-green on a "
    "deletion-that-leaves-residue and false-red on any refactor that moves the "
    "line to a neighbouring function.\n"
    "Prove it by EXECUTING the path instead: call the thing and assert on what "
    "it returns/emits/raises. If the guarantee really is structural, parse it — "
    "``ast.parse(inspect.getsource(mod))`` and assert against a resolved node "
    "(see tests/agent_runtime/test_s29_snapshot_dead_local_removal.py).\n"
    "``not in`` assertions are unaffected: text absence is exactly what a "
    "tombstone gate claims."
)


@pytest.fixture(scope="module")
def scan() -> tuple[list[str], int]:
    return _scan()


@pytest.mark.timeout(300)
def test_no_new_positive_source_grep_assertion(scan) -> None:
    found, scanned = scan
    assert scanned >= _MIN_SCANNED_FILES, (
        f"the walker only saw {scanned} files under {TESTS_ROOT} — it drifted off "
        "the tree, so a green here would mean nothing"
    )
    ledgered, _declared = _read_ledger()
    new = sorted((Counter(found) - Counter(ledgered)).elements())
    assert not new, (
        "New positive source-grep assertion(s) in tests/:\n"
        + "\n".join(f"  {item}" for item in new)
        + _REMEDY
        + f"\n\nThis gate does not accept new debt, so there is no ledger line to "
        f"add: fix the assertion. ({LEDGER_PATH.name} is closed to additions.)"
    )


@pytest.mark.timeout(300)
def test_source_grep_ledger_has_no_stale_entries(scan) -> None:
    """A debt register that silently keeps paid-off rows rots exactly like the
    grep pins it exists to retire."""

    found, _scanned = scan
    ledgered, _declared = _read_ledger()
    stale = sorted((Counter(ledgered) - Counter(found)).elements())
    assert not stale, (
        "Ledgered source-grep violation(s) that no longer exist:\n"
        + "\n".join(f"  {item}" for item in stale)
        + f"\n\nNice — fewer source greps. Delete the line(s) above from "
        f"tests/{LEDGER_PATH.name} and lower the count in its header so the "
        "register stays honest. (Nothing in this gate needs editing.)"
    )


@pytest.mark.timeout(300)
def test_ledger_header_count_matches_its_entries() -> None:
    """The header count is the number anyone reads to see the debt going down,
    so it is asserted rather than trusted."""

    entries, declared = _read_ledger()
    assert declared is not None, (
        f"tests/{LEDGER_PATH.name} lost its '# grandfathered violations: N' "
        "header line — that count is the register's headline."
    )
    assert declared == len(entries), (
        f"tests/{LEDGER_PATH.name} declares {declared} grandfathered violations "
        f"but lists {len(entries)}."
    )


# -- the detector's own red/green proofs -------------------------------------
#
# The gate is itself a test-quality gate, so its asymmetry is pinned on
# synthetic modules rather than assumed. Each snippet is parsed exactly the way
# the tree walk parses a real file.

def _violations_of(source: str) -> list[str]:
    return [rendered for _lineno, rendered in _SourceTextAnalyzer(ast.parse(source)).violations()]


BANNED_FORMS = {
    "direct call": "import inspect\ndef test_x():\n    assert 'foo' in inspect.getsource(mod)\n",
    "via local": "import inspect\ndef test_x():\n    src = inspect.getsource(mod)\n    assert 'foo' in src\n",
    "via text transform": "import inspect\ndef test_x():\n    src = inspect.getsource(mod).lower()\n    assert 'foo' in src\n",
    "via source helper": (
        "import inspect\n"
        "def _body(name):\n"
        "    return inspect.getsource(getattr(mod, name))\n"
        "def test_x():\n"
        "    assert 'foo' in _body('f')\n"
    ),
    "nested in any()": (
        "import inspect\n"
        "def test_x():\n"
        "    src = inspect.getsource(mod)\n"
        "    assert any(n in src for n in NAMES)\n"
    ),
    "unittest assertIn": (
        "import inspect\n"
        "class T(TestCase):\n"
        "    def test_x(self):\n"
        "        self.assertIn('foo', inspect.getsource(mod))\n"
    ),
    # A positive claim wearing ``not in`` — the inverted dodge.
    "fail-guard on absence": (
        "import inspect\n"
        "def test_x():\n"
        "    src = inspect.getsource(mod)\n"
        "    if 'foo' not in src:\n"
        "        pytest.fail('missing')\n"
    ),
    # parse -> unparse is a round trip back to characters, not a resolved node.
    "ast.unparse round trip": (
        "import ast, inspect\n"
        "def test_x():\n"
        "    src = ast.unparse(ast.parse(inspect.getsource(mod)))\n"
        "    assert 'foo' in src\n"
    ),
    "text-preserving helper": (
        "import ast, inspect\n"
        "def _code_only(src):\n"
        "    return ast.unparse(ast.parse(src))\n"
        "def test_x():\n"
        "    assert 'foo' in _code_only(inspect.getsource(mod))\n"
    ),
    "regex scrape of source": (
        "import inspect, re\n"
        "def _names():\n"
        "    return set(re.findall('X', inspect.getsource(mod)))\n"
        "def test_x():\n"
        "    assert 'foo' in _names()\n"
    ),
}

ALLOWED_FORMS = {
    "negative tombstone": "import inspect\ndef test_x():\n    assert 'foo' not in inspect.getsource(mod)\n",
    "negative via local": "import inspect\ndef test_x():\n    src = inspect.getsource(mod)\n    assert 'foo' not in src\n",
    "unittest assertNotIn": (
        "import inspect\n"
        "class T(TestCase):\n"
        "    def test_x(self):\n"
        "        self.assertNotIn('foo', inspect.getsource(mod))\n"
    ),
    "ast.parse structural": (
        "import ast, inspect\n"
        "def test_x():\n"
        "    tree = ast.parse(inspect.getsource(mod))\n"
        "    bound = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}\n"
        "    assert 'foo' in bound\n"
    ),
    "kind-preserving ast helper": (
        "import ast, inspect\n"
        "def _const_args(call):\n"
        "    return [a.value for a in call.args if isinstance(a, ast.Constant)]\n"
        "def test_x():\n"
        "    tree = ast.parse(inspect.getsource(mod))\n"
        "    for node in ast.walk(tree):\n"
        "        assert 'foo' in _const_args(node)\n"
    ),
    "ast-resolving helper launders the taint": (
        "import ast, inspect\n"
        "def _keys(src):\n"
        "    tree = ast.parse(src)\n"
        "    return {k.value for n in ast.walk(tree) if isinstance(n, ast.Dict) for k in n.keys}\n"
        "def test_x():\n"
        "    assert 'foo' in _keys(inspect.getsource(mod))\n"
    ),
    "negative claim in a fail-guard": (
        "import inspect\n"
        "def test_x():\n"
        "    src = inspect.getsource(mod)\n"
        "    for line in src.split('\\n'):\n"
        "        if 'foo' in line.strip():\n"
        "            pytest.fail('found')\n"
    ),
    "negation of a positive": (
        "import inspect\n"
        "def test_x():\n"
        "    src = inspect.getsource(mod)\n"
        "    assert not any(n in src for n in NAMES)\n"
    ),
    "unrelated membership in the same module": (
        "import inspect\n"
        "def test_a():\n"
        "    src = inspect.getsource(mod)\n"
        "    assert 'foo' not in src\n"
        "def test_b():\n"
        "    result = call()\n"
        "    assert 'disabled' in result.lower()\n"
    ),
}


@pytest.mark.parametrize("label", sorted(BANNED_FORMS))
def test_detector_catches_the_banned_form(label: str) -> None:
    assert _violations_of(BANNED_FORMS[label]), f"{label} slipped past the detector"


@pytest.mark.parametrize("label", sorted(ALLOWED_FORMS))
def test_detector_leaves_the_allowed_form_alone(label: str) -> None:
    assert _violations_of(ALLOWED_FORMS[label]) == [], f"{label} was wrongly flagged"
