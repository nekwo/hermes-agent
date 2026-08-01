"""Characterization matrix for the four Windows path-form translators.

Why this file exists
--------------------
``tools/environments/local.py`` is **upstream-owned**. It carries four
path translators that convert between the three spellings a path can
have on a Windows host running Git Bash:

===================  =====================================
form                 example
===================  =====================================
native               ``C:\\Users\\alice\\notes.txt``
drive-forward        ``C:/Users/alice/notes.txt``
MSYS                 ``/c/Users/alice/notes.txt``
mixed                ``/c/Users/alice\\notes.txt``
===================  =====================================

and four consumers that each need a *different* one of those forms:

* ``_msys_to_windows_path``  — for ``os.path.isdir`` / ``Popen(cwd=...)``
* ``_windows_to_msys_path``  — for bash ``builtin cd``
* ``_bash_safe_path``        — for text interpolated into a bash *script*
* ``_shell_arg_safe_path``   — for *argv* handed to a possibly-native binary

The 2026-07-30 upstream merge rehearsal (doc 18, defect #4, fixed on the
fork at ``93dc56bd9``) showed how thin the ice is here: upstream's
``_bash_safe_path`` argv rewrite contradicted the ``MSYS_NO_PATHCONV=1``
this same module sets on every bash spawn, which broke ``search_files``
content search on native Windows *and* corrupted every search pattern
containing a backslash. The contradiction still exists in upstream, where
it is PR candidate #1.

So this file is not a behavior proposal — it is a **tripwire**. It pins
the observed input→output of all four translators across the form matrix
and the edges that actually bit us, so the next upstream sync that
"tidies", merges, or unifies these functions fails loudly here with a
readable diff instead of silently breaking a tool on Windows only.

**No production edits belong in this file's change set.** If a pin below
starts failing, the question to answer first is "did upstream change the
contract on purpose?" — not "how do I make the test green?".

Host independence
-----------------
Every translator gates on the module-level ``_IS_WINDOWS`` constant and
otherwise touches nothing but ``re`` and ``str``: no filesystem, no
``os.environ``, no ``platform`` re-probe (:func:`test_translators_touch_no_host_state`
pins exactly that, and is the reason nothing here needs ``skipif``).
That makes them pure string transforms once ``_IS_WINDOWS`` is fixed, so
the Windows-semantics matrix runs identically on a Linux CI host with the
flag monkeypatched — which is precisely the point, since the fork's
Windows behavior must be provable off Windows too.
"""

import ast
import inspect

import pytest


# ---------------------------------------------------------------------------
# Function-local import (the pattern ``tools/file_operations.py`` uses for
# these same helpers) — keeps this module import-order-independent and makes
# the monkeypatched ``_IS_WINDOWS`` unambiguous at call time.
# ---------------------------------------------------------------------------

def _local_mod():
    from tools.environments import local as local_mod

    return local_mod


TRANSLATORS = (
    "_msys_to_windows_path",
    "_windows_to_msys_path",
    "_bash_safe_path",
    "_shell_arg_safe_path",
)


@pytest.fixture
def win(monkeypatch):
    """The module with Windows semantics forced on, for any host."""
    local_mod = _local_mod()
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", True)
    return local_mod


@pytest.fixture
def posix(monkeypatch):
    """The module with Windows semantics forced off, for any host."""
    local_mod = _local_mod()
    monkeypatch.setattr(local_mod, "_IS_WINDOWS", False)
    return local_mod


# ---------------------------------------------------------------------------
# Premise: the translators are pure string transforms over _IS_WINDOWS.
# ---------------------------------------------------------------------------

# Free global names each translator is allowed to read. ``str`` is the
# return annotation; ``chr`` is ``_msys_to_windows_path``'s backslash
# literal dodge. Anything else — os, platform, ntpath, Path, environ —
# would mean the matrix below is no longer host-independent.
ALLOWED_FREE_NAMES = {
    "_IS_WINDOWS",
    "re",
    "str",
    "chr",
    "_msys_to_windows_path",
    "_windows_to_msys_path",
}


def _free_global_names(func):
    """Names a function loads from module/builtin scope."""
    tree = ast.parse(inspect.getsource(func))
    fn = tree.body[0]
    params = {a.arg for a in fn.args.args}
    assigned = {
        node.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    return {
        node.id
        for node in ast.walk(fn)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id not in params
        and node.id not in assigned
    }


@pytest.mark.parametrize("name", TRANSLATORS)
def test_translators_touch_no_host_state(win, name):
    """Each translator reads only ``_IS_WINDOWS``, ``re``, and its sibling.

    This is the premise the rest of the file rests on. If an upstream sync
    makes one of them consult the filesystem or re-probe the platform, this
    fails first and tells the fork that the pins below stopped being
    host-independent (and that skipif fences are now genuinely needed).
    """
    free = _free_global_names(getattr(win, name))
    assert free <= ALLOWED_FREE_NAMES, (
        f"{name} gained host-state reads: {sorted(free - ALLOWED_FREE_NAMES)}"
    )


# ---------------------------------------------------------------------------
# The 4x4 matrix: four input forms x four translators.
# Row: (id, input, msys_to_windows, windows_to_msys, bash_safe, shell_arg_safe)
# ---------------------------------------------------------------------------

FORM_MATRIX = [
    (
        "native-backslash",
        r"C:\Users\alice\notes.txt",
        r"C:\Users\alice\notes.txt",          # already native: verbatim
        "/c/Users/alice/notes.txt",
        "/c/Users/alice/notes.txt",
        "C:/Users/alice/notes.txt",
    ),
    (
        "drive-forward-slash",
        "C:/Users/alice/notes.txt",
        "C:/Users/alice/notes.txt",           # not MSYS form: verbatim
        "/c/Users/alice/notes.txt",
        "/c/Users/alice/notes.txt",
        "C:/Users/alice/notes.txt",           # already the argv form: verbatim
    ),
    (
        "msys",
        "/c/Users/alice/notes.txt",
        r"C:\Users\alice\notes.txt",
        "/c/Users/alice/notes.txt",           # already MSYS form: verbatim
        "/c/Users/alice/notes.txt",           # already MSYS form: verbatim
        "C:/Users/alice/notes.txt",
    ),
    (
        "mixed-msys-head-backslash-tail",
        r"/c/Users/alice\notes.txt",
        r"C:\Users\alice\notes.txt",
        r"/c/Users/alice\notes.txt",          # NOT drive-qualified: verbatim,
                                              # backslashes survive (see the
                                              # _bash_safe_path second pass)
        "/c/Users/alice/notes.txt",
        "C:/Users/alice/notes.txt",
    ),
    (
        "mixed-native-head-forward-tail",
        r"C:\Users/alice\AppData/Local",
        r"C:\Users/alice\AppData/Local",      # not MSYS form: verbatim
        "/c/Users/alice/AppData/Local",
        "/c/Users/alice/AppData/Local",
        "C:/Users/alice/AppData/Local",
    ),
    (
        "mixed-drive-forward-head-backslash-tail",
        r"C:/Users/alice\notes.txt",
        r"C:/Users/alice\notes.txt",          # not MSYS form: verbatim
        "/c/Users/alice/notes.txt",
        "/c/Users/alice/notes.txt",
        "C:/Users/alice/notes.txt",
    ),
]

MATRIX_IDS = [row[0] for row in FORM_MATRIX]


@pytest.mark.parametrize("row", FORM_MATRIX, ids=MATRIX_IDS)
def test_form_matrix(win, row):
    """Pin every cell of the form x translator matrix."""
    _id, source, expect_m2w, expect_w2m, expect_bash, expect_arg = row

    assert win._msys_to_windows_path(source) == expect_m2w
    assert win._windows_to_msys_path(source) == expect_w2m
    assert win._bash_safe_path(source) == expect_bash
    assert win._shell_arg_safe_path(source) == expect_arg


def test_matrix_covers_every_form_and_translator():
    """Guard the matrix itself: 4 canonical forms x 4 translators, minimum."""
    assert {"native-backslash", "drive-forward-slash", "msys"} <= set(MATRIX_IDS)
    assert any(mid.startswith("mixed-") for mid in MATRIX_IDS)
    assert len(TRANSLATORS) == 4


# ---------------------------------------------------------------------------
# The defect that started this file: backslash-bearing NON-paths.
# ---------------------------------------------------------------------------

# Search patterns, regexes and code snippets routed through the same helpers
# that quote paths. doc 18 defect #4: a blanket backslash rewrite corrupted
# every one of these.
BACKSLASH_NON_PATHS = [
    r"func_\w+",
    r"import\s+os",
    r"absent\npattern",
    r"absent\\npattern",
    r"\bdef\b",
]


@pytest.mark.parametrize("pattern", BACKSLASH_NON_PATHS)
def test_shell_arg_safe_path_passes_backslash_patterns_through(win, pattern):
    """argv rewriting must never touch a non-drive-qualified argument.

    This is the exact regression from doc 18 defect #4: ``_shell_arg_safe_path``
    quotes ``search_files`` patterns as well as paths, so a blanket
    ``replace('\\\\', '/')`` silently rewrote ``func_\\w+`` into ``func_/w+``
    and every content search came back empty.
    """
    assert win._shell_arg_safe_path(pattern) == pattern


def test_drive_shaped_regex_is_indistinguishable(win):
    """KNOWN SHARP EDGE, pinned deliberately.

    ``_shell_arg_safe_path`` decides by shape: ``<letter>:`` followed by a
    slash of either kind. A regex that happens to start that way — ``C:\\d+``
    — is therefore rewritten like a path. There is no information available
    to the function that would let it do better; the pin exists so the edge
    is documented rather than rediscovered.
    """
    assert win._shell_arg_safe_path(r"C:\d+") == "C:/d+"


@pytest.mark.parametrize("pattern", BACKSLASH_NON_PATHS)
def test_bash_safe_path_still_corrupts_backslash_patterns(win, pattern):
    """The sibling still mangles them — BY DESIGN, and that is the trap.

    ``_bash_safe_path`` targets bash *script text*, where every backslash is
    a path separator to normalize, so it rewrites unconditionally. The fix for
    doc 18 defect #4 was not to soften this function but to stop routing argv
    through it. Pinning the corruption keeps that split honest: if a future
    sync "unifies" the two helpers, this test fails and names the reason.
    """
    corrupted = win._bash_safe_path(pattern)
    assert corrupted == pattern.replace("\\", "/")
    # ...and the argv sibling deliberately disagrees on every one of them.
    assert win._shell_arg_safe_path(pattern) != corrupted


# ---------------------------------------------------------------------------
# Non-drive-qualified input is returned verbatim (the argv escape hatch).
# ---------------------------------------------------------------------------

VERBATIM_FOR_SHELL_ARG = [
    "/home/teknium",
    "/tmp/foo",
    "relative/dir",
    r"relative\dir",
    "",
    r"\\server\share\folder",
    'python -c "import os; print(os.sep)"',
    "--include=*.py",
    "C:",  # bare drive with no separator does NOT qualify
    "/c/Users/alice",  # NOTE: MSYS form DOES qualify — see the assert below
]


@pytest.mark.parametrize(
    "value",
    [v for v in VERBATIM_FOR_SHELL_ARG if not v.startswith("/c/")],
)
def test_shell_arg_safe_path_returns_non_drive_input_verbatim(win, value):
    """Genuine POSIX paths, relative paths, UNC paths and plain arguments
    are handed back untouched — the guarantee that lets the same helper
    quote both a path and a ``python -c`` snippet."""
    assert win._shell_arg_safe_path(value) == value


def test_shell_arg_safe_path_qualifies_msys_form_first(win):
    """MSYS input is *not* verbatim: it is drive-qualified via
    ``_msys_to_windows_path`` before the shape test, so ``/c/...`` becomes
    ``C:/...``. This is the whole reason native ``rg.exe`` can resolve it."""
    assert win._shell_arg_safe_path("/c/Users/alice") == "C:/Users/alice"


# ---------------------------------------------------------------------------
# Drive-root and bare-drive edges: the four translators disagree here.
# ---------------------------------------------------------------------------

DRIVE_EDGES = [
    # (input,      m2w,      w2m,     bash,    shell_arg)
    ("/c/",        "C:\\",   "/c/",   "/c/",   "C:/"),
    ("/c",         "C:\\",   "/c",    "/c",    "C:/"),
    ("C:\\",       "C:\\",   "/c/",   "/c/",   "C:/"),
    ("C:/",        "C:/",    "/c/",   "/c/",   "C:/"),
    ("C:",         "C:",     "/c/",   "/c/",   "C:"),
]


@pytest.mark.parametrize(
    "row", DRIVE_EDGES, ids=[r[0].replace("\\", "bs") for r in DRIVE_EDGES]
)
def test_drive_root_edges(win, row):
    """Bare drive roots are where the four disagree, so pin all of them.

    Two asymmetries worth reading twice:

    * ``"C:"`` (no separator) becomes ``"/c/"`` through ``_windows_to_msys_path``
      — it is treated as the drive root — but stays ``"C:"`` through
      ``_shell_arg_safe_path``, whose shape test requires a following slash.
    * ``"/c"`` (no trailing slash) becomes ``"C:\\"`` / ``"C:/"`` going one way
      but survives ``_windows_to_msys_path`` untouched, so the MSYS->native->MSYS
      round trip is *not* the identity: ``/c`` -> ``C:\\`` -> ``/c/``.
    """
    source, expect_m2w, expect_w2m, expect_bash, expect_arg = row

    assert win._msys_to_windows_path(source) == expect_m2w
    assert win._windows_to_msys_path(source) == expect_w2m
    assert win._bash_safe_path(source) == expect_bash
    assert win._shell_arg_safe_path(source) == expect_arg


# ---------------------------------------------------------------------------
# Cygwin / WSL mount spellings: only two of the four know about them.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source", ["/cygdrive/c/Users/alice", "/mnt/c/Users/alice"])
def test_cygdrive_and_wsl_mount_forms(win, source):
    """``_msys_to_windows_path`` accepts ``/cygdrive/c`` and ``/mnt/c``;
    ``_windows_to_msys_path`` does not know them at all.

    Consequence, pinned here because it is a live gap rather than a
    preference: ``_bash_safe_path`` (which routes through the *reverse*
    translator) leaves these spellings ALONE, so a ``/cygdrive/c/...`` value
    interpolated into a Git Bash script stays in a form Git Bash cannot
    resolve — while ``_shell_arg_safe_path`` normalizes it correctly.
    """
    assert win._msys_to_windows_path(source) == r"C:\Users\alice"
    assert win._windows_to_msys_path(source) == source
    assert win._bash_safe_path(source) == source  # the gap
    assert win._shell_arg_safe_path(source) == "C:/Users/alice"


# ---------------------------------------------------------------------------
# Drive-letter case is input-dependent, not normalized.
# ---------------------------------------------------------------------------

def test_drive_letter_case_is_not_normalized(win):
    """Only the MSYS->native direction upper-cases the drive.

    ``/c/Users`` -> ``C:\\Users`` (upper), but an already-native ``c:\\Users``
    keeps its lower-case drive, so ``_shell_arg_safe_path`` emits ``C:/Users``
    or ``c:/Users`` depending purely on which spelling the caller had. Windows
    does not care; string comparisons on the results do. Pinned so a future
    normalization change is a visible decision.
    """
    assert win._msys_to_windows_path("/c/Users/alice") == r"C:\Users\alice"
    assert win._msys_to_windows_path(r"c:\Users\alice") == r"c:\Users\alice"
    assert win._shell_arg_safe_path("/c/Users/alice") == "C:/Users/alice"
    assert win._shell_arg_safe_path("c:/Users/alice") == "c:/Users/alice"
    # The reverse translator always lower-cases.
    assert win._windows_to_msys_path(r"C:\Users\alice") == "/c/Users/alice"
    assert win._windows_to_msys_path(r"c:\Users\alice") == "/c/Users/alice"


# ---------------------------------------------------------------------------
# Properties that hold across the whole corpus.
# ---------------------------------------------------------------------------

CORPUS = (
    [row[1] for row in FORM_MATRIX]
    + [row[0] for row in DRIVE_EDGES]
    + BACKSLASH_NON_PATHS
    + VERBATIM_FOR_SHELL_ARG
    + ["/cygdrive/c/Users/alice", "/mnt/c/Users/alice", "c:/users/alice"]
)


@pytest.mark.parametrize("name", TRANSLATORS)
def test_every_translator_is_idempotent(win, name):
    """``f(f(x)) == f(x)`` for all four, over the whole corpus.

    Load-bearing: these helpers are applied at several layers (a cwd can be
    translated on capture and again on use), so a non-idempotent one would
    corrupt on the second pass rather than the first — the hardest kind of
    path bug to trace.
    """
    func = getattr(win, name)
    for source in CORPUS:
        once = func(source)
        assert func(once) == once, f"{name} not idempotent on {source!r}: {once!r}"


def test_msys_native_round_trip_normalizes_rather_than_preserves(win):
    """The round trip is a normalizer, not an identity — pin which way.

    ``w2m(m2w(x))`` collapses every drive spelling onto ``/c/...`` and every
    bare root onto ``/c/``; ``m2w(w2m(x))`` collapses onto ``C:\\...``. Non-drive
    input survives both unchanged.
    """
    assert win._windows_to_msys_path(win._msys_to_windows_path(r"C:\Users\alice")) == (
        "/c/Users/alice"
    )
    assert win._windows_to_msys_path(win._msys_to_windows_path("/c")) == "/c/"
    assert win._msys_to_windows_path(win._windows_to_msys_path("C:")) == "C:\\"
    for neutral in ("/home/teknium", "relative/dir", r"func_\w+", r"\\server\share"):
        assert win._windows_to_msys_path(win._msys_to_windows_path(neutral)) == neutral
        assert win._msys_to_windows_path(win._windows_to_msys_path(neutral)) == neutral


# ---------------------------------------------------------------------------
# Off Windows, all four are the identity function.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", TRANSLATORS)
def test_all_translators_are_identity_off_windows(posix, name):
    """On a POSIX host every one of these is a no-op — ``/c/Users/x`` is a
    real path there and must never be rewritten into ``C:\\Users\\x``."""
    func = getattr(posix, name)
    for source in CORPUS:
        assert func(source) == source, f"{name} rewrote {source!r} off Windows"
